"""DPAPI, macOS Keychain and explicitly configured container encryption.

Container ciphertext is bound to a private, persistent Fernet master-key file.
No credential is stored as plaintext and no missing key is silently replaced.
"""
import ctypes
import os
import re
import sys
import uuid
import sqlite3
import stat
from pathlib import Path
from ctypes import wintypes

from app.core.errors import AppError


class Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def _blob(data: bytes):
    buffer = ctypes.create_string_buffer(data, len(data))
    return Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def _crypt(data: bytes, decrypt=False) -> bytes:
    if os.name != 'nt':
        raise AppError(503, 'secure_store_unavailable', '此部署未配置安全密钥存储；禁止明文保存')
    source, keep_alive = _blob(data)
    output = Blob()
    entropy, entropy_buffer = _blob(b'video-factory-ai-services-v1')
    library = ctypes.WinDLL('crypt32', use_last_error=True)
    library.CryptProtectData.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    library.CryptUnprotectData.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    if decrypt:
        ok = library.CryptUnprotectData(ctypes.byref(source), None, ctypes.byref(entropy), None, None, 1, ctypes.byref(output))
    else:
        ok = library.CryptProtectData(ctypes.byref(source), 'Video Factory AI service', ctypes.byref(entropy), None, None, 1, ctypes.byref(output))
    if not ok:
        raise AppError(503, 'secure_store_failed', 'Windows DPAPI 不可用；请使用创建该配置的 Windows 用户')
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        local_free = ctypes.windll.kernel32.LocalFree
        local_free.argtypes = [ctypes.c_void_p]
        local_free(ctypes.cast(output.data, ctypes.c_void_p))


KEYCHAIN_PREFIX = b'video-factory-keychain-v1:'
KEYCHAIN_SERVICE = 'Video Factory AI Services'
FERNET_PREFIX = b'video-factory-fernet-v1:'
KEY_FILE_ENV = 'VIDEO_FACTORY_SECRET_KEY_FILE'


def _container_encryption_selected():
    return bool(os.getenv(KEY_FILE_ENV, '').strip()) or os.getenv('VIDEO_FACTORY_CONTAINER', '').lower() in {'1','true','yes','on'}


def container_key_path():
    raw=os.getenv(KEY_FILE_ENV, '').strip()
    if not raw:
        raise AppError(503,'secret_master_key_not_configured','容器密钥存储未配置 VIDEO_FACTORY_SECRET_KEY_FILE；禁止明文保存或临时密钥回退')
    path=Path(raw)
    if not path.is_absolute():
        raise AppError(503,'secret_master_key_path','容器主密钥文件必须使用持久化目录中的绝对路径')
    return path


def _read_container_key():
    path=container_key_path()
    try:
        if path.is_symlink():raise AppError(503,'secret_master_key_permissions','主密钥必须是当前容器用户拥有的普通私有文件，不能使用符号链接')
        flags=os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)
        with os.fdopen(os.open(path,flags),'rb') as stream:
            metadata=os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size>128:
                raise AppError(503,'secret_master_key_invalid','容器主密钥文件格式无效')
            if os.name=='posix' and (metadata.st_uid!=os.getuid() or stat.S_IMODE(metadata.st_mode)!=0o600):
                raise AppError(503,'secret_master_key_permissions','主密钥文件必须归当前容器用户所有且权限为0600；请恢复正确所有者和权限')
            return stream.read().strip()
    except FileNotFoundError as error:
        raise AppError(503,'secret_master_key_missing','持久化主密钥文件丢失；请从备份恢复原密钥，现有API密钥不会被明文读取或自动改用新密钥') from error
    except OSError as error:
        raise AppError(503,'secret_master_key_unreadable','容器主密钥文件不可读；请检查持久化卷与用户权限') from error


def _container_cipher():
    try:
        from cryptography.fernet import Fernet
    except ImportError as error:
        raise AppError(503,'secret_crypto_dependency_missing','容器加密依赖未安装，请使用锁定的Docker生产依赖重新构建') from error
    try:return Fernet(_read_container_key())
    except ValueError as error:raise AppError(503,'secret_master_key_invalid','容器主密钥不是有效Fernet密钥，请恢复原文件') from error


def _database_service_secrets(database_url):
    from sqlalchemy.engine import make_url
    url=make_url(database_url)
    if url.get_backend_name()!='sqlite' or not url.database or url.database==':memory:':
        raise AppError(503,'container_database_unsupported','当前容器部署只支持持久化SQLite文件')
    database=Path(url.database)
    if not database.exists():return []
    try:
        with sqlite3.connect('file:'+database.resolve().as_posix()+'?mode=ro',uri=True) as connection:
            exists=connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_services'").fetchone()
            if not exists:return []
            return [bytes(row[0]) for row in connection.execute('SELECT encrypted_secret FROM ai_services WHERE encrypted_secret IS NOT NULL') if row[0]]
    except (sqlite3.Error,TypeError) as error:
        raise AppError(503,'secret_database_unreadable','不能确认数据库中的既有密钥；未生成替换主密钥，请检查数据库备份') from error


def ensure_container_secret_key(database_url):
    """Initialize only an empty credential store; validate existing ciphertext."""
    path=container_key_path();existing=_database_service_secrets(database_url)
    if not path.exists():
        if existing:raise AppError(503,'secret_master_key_missing','数据库已有加密凭据但主密钥文件缺失；必须恢复原密钥，禁止自动生成替换密钥')
        try:
            from cryptography.fernet import Fernet
            path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb') as stream:
                stream.write(Fernet.generate_key()+b'\n');stream.flush();os.fsync(stream.fileno())
        except FileExistsError:pass  # Another startup completed creation first.
        except ImportError as error:raise AppError(503,'secret_crypto_dependency_missing','缺少容器加密依赖，请重新构建生产镜像') from error
        except OSError as error:raise AppError(503,'secret_master_key_unwritable','无法在持久化卷创建私有主密钥文件，请检查目录权限') from error
    cipher=_container_cipher()
    from cryptography.fernet import InvalidToken
    for value in existing:
        if not value.startswith(FERNET_PREFIX):
            raise AppError(503,'secret_migration_required','数据库含Windows DPAPI/macOS钥匙串凭据，不能直接由Linux容器解密；请使用纯净部署并在设置页重新配置，勿覆盖原库')
        try:cipher.decrypt(value[len(FERNET_PREFIX):])
        except InvalidToken as error:raise AppError(503,'secret_master_key_mismatch','主密钥与现有数据库不匹配；请同时恢复匹配的数据库和主密钥文件') from error
    return {'backend':'fernet_file','key_file_configured':True,'existing_credentials_validated':len(existing)}


def _mac_keyring():
    try:
        from keyring.backends.macOS import Keyring
        return Keyring()
    except Exception as exc:
        raise AppError(503, 'secure_store_unavailable', 'macOS 钥匙串不可用，请在已登录的 Mac 用户会话中运行') from exc


def storage_label(value: bytes | None):
    if not value:
        return None
    if value.startswith(FERNET_PREFIX):return 'Container Fernet (persistent private master key)'
    return 'macOS Keychain (current user)' if value.startswith(KEYCHAIN_PREFIX) else 'Windows DPAPI (current user)'


def protect(value: str) -> bytes:
    if _container_encryption_selected():return FERNET_PREFIX+_container_cipher().encrypt(value.encode('utf-8'))
    if sys.platform == 'darwin':
        account = uuid.uuid4().hex
        try:
            _mac_keyring().set_password(KEYCHAIN_SERVICE, account, value)
        except Exception as exc:
            raise AppError(503, 'secure_store_failed', '无法写入 macOS 钥匙串；密钥未保存到磁盘明文') from exc
        return KEYCHAIN_PREFIX + account.encode('ascii')
    return _crypt(value.encode('utf-8'))


def reveal(value: bytes | None) -> str:
    if value and value.startswith(FERNET_PREFIX):
        cipher=_container_cipher()
        from cryptography.fernet import InvalidToken
        try:return cipher.decrypt(value[len(FERNET_PREFIX):]).decode('utf-8')
        except (InvalidToken,UnicodeDecodeError) as error:raise AppError(503,'secret_master_key_mismatch','容器主密钥与此凭据不匹配，请恢复原密钥或在设置页明确重新填写') from error
    if value and value.startswith(KEYCHAIN_PREFIX):
        if sys.platform != 'darwin':
            raise AppError(503, 'secret_migration_required', '此密钥属于 Mac 钥匙串，请在本机设置页重新填写')
        try:
            secret = _mac_keyring().get_password(KEYCHAIN_SERVICE, value[len(KEYCHAIN_PREFIX):].decode('ascii'))
            if secret is None:
                raise ValueError('Keychain item unavailable')
            return secret
        except Exception as exc:
            raise AppError(503, 'secure_store_failed', '无法读取此 Mac 用户的钥匙串，请在设置页重新填写密钥') from exc
    if value and sys.platform == 'darwin':
        raise AppError(503, 'secret_migration_required', '此密钥属于 Windows DPAPI，请在 Mac 设置页重新填写')
    return _crypt(value, True).decode('utf-8') if value else ''


def redact(value, secret=''):
    if isinstance(value, dict):
        return {key: ('***' if re.search(r'api.?key|authorization|token|secret', str(key), re.I) else redact(item, secret)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, secret) for item in value]
    if isinstance(value, str):
        if secret:
            value = value.replace(secret, '***')
        value = re.sub(r'(?i)(bearer\s+)[^\s"<>]+', r'\1***', value)
        value = re.sub(r'(?i)([?&](?:api_key|key|token|secret)=)[^&\s]+', r'\1***', value)
        return re.sub(r'\bsk-[A-Za-z0-9_-]{10,}', '***', value)
    return value


def delete_keychain_reference(account: str):
    """Delete only an unshared opaque Keychain account; never return its secret."""
    if sys.platform != 'darwin':
        raise AppError(503,'keychain_cleanup_requires_mac','该钥匙串凭据需在所属Mac用户会话中清理')
    if not re.fullmatch(r'[a-f0-9]{32}',account):
        raise AppError(422,'invalid_keychain_reference','钥匙串引用无效')
    keyring=_mac_keyring()
    if keyring.get_password(KEYCHAIN_SERVICE,account) is not None:
        keyring.delete_password(KEYCHAIN_SERVICE,account)
    if keyring.get_password(KEYCHAIN_SERVICE,account) is not None:
        raise AppError(503,'keychain_cleanup_failed','钥匙串条目仍存在，请重试凭据清理')
