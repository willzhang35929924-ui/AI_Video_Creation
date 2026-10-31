"""Global recordings reuse existing project-owned references and TTS queues."""
from __future__ import annotations

import hashlib
import math
import os
import subprocess
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError, OperationalError

from app import models as m
from app.models.voice_library import VoiceLibraryAsset
from app.core.errors import AppError
from app.core.paths import safe_media_path
from app.schemas_studio_voice import ProjectVoiceSave
from app.services.runtime_registry import REFERENCE_TTS_FAMILIES, file_sha, manifest_status

SOURCE_DIRECTORY = Path(os.getenv('LOCAL_RECORDINGS_DIR',str(Path(__file__).resolve().parents[3]/'data'/'imports'/'recordings')))
AUDIO_SUFFIXES = {'.wav', '.flac', '.mp3', '.m4a', '.aac', '.ogg'}
MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_BATCH_FILES = 20

def source_directory(settings):
    if os.getenv('VIDEO_FACTORY_CONTAINER') != '1':
        return SOURCE_DIRECTORY
    data_root = settings.media_root.parent.resolve()
    imports = (data_root / 'imports').resolve()
    value = Path(os.getenv('LOCAL_RECORDINGS_DIR', str(imports / 'recordings')))
    if not value.is_absolute():
        raise AppError(422, 'voice_source_configuration', 'LOCAL_RECORDINGS_DIR 必须是数据目录 imports 下的绝对路径')
    root = value.resolve()
    if not imports.is_relative_to(data_root) or not root.is_relative_to(imports):
        raise AppError(422, 'voice_source_configuration', '本地录音目录必须位于持久化数据目录 imports 内；也可以通过页面上传录音')
    return root


def _tags(values):
    if any(not isinstance(value, str) or not 1 <= len(value.strip()) <= 80 for value in values):
        raise AppError(422, 'voice_tags_invalid', '标签或角色类型需为1至80个字符')
    return list(dict.fromkeys(value.strip() for value in values))


def get_asset(session, identifier, include_deleted=False):
    item = session.get(VoiceLibraryAsset, identifier)
    if not item or item.state != 'active' and not include_deleted:
        raise AppError(404, 'voice_library_missing', '配音素材不存在或已移除')
    return item


def reanalyze(session,settings,identifier):
    item=get_asset(session,identifier);meta=dict(item.metadata_json or {})
    if meta.get('actual',{}).get('duration',0)>30:raise AppError(422,'voice_reanalysis_limit','声音参考重新检查仅处理30秒内素材；较长录音仍可试听，请先选择短参考副本')
    try:
        path=safe_media_path(settings.media_root,item.relative_path,must_exist=True)
        if file_sha(path)!=item.sha256:raise AppError(409,'voice_library_changed','原音频SHA变化，未将新字节视为已获授权声音')
        from app.services.media_processing import _probe
        actual=_probe(path,settings,True)
        meta.update(actual=actual,reanalysis_status='succeeded',reanalysis_method='local_full_ffmpeg_decode_and_sha256')
    except (AppError,OSError) as error:
        meta.update(actual={**meta.get('actual',{}),'full_decode':False},reanalysis_status='unavailable',reanalysis_error=getattr(error,'code',type(error).__name__))
    item.metadata_json=meta;item.revision+=1;session.commit();return view(session,settings,item)


def compatible_services(session, settings):
    result, checked = [], {}
    for service in session.scalars(select(m.AIService).where(m.AIService.module == 'dialogue_generation', m.AIService.enabled.is_(True))):
        family = service.settings_json.get('runtime_family')
        if service.provider != 'local_runtime' or family not in REFERENCE_TTS_FAMILIES:
            continue
        if family not in checked:
            checked[family] = manifest_status(family, settings)
        runtime = checked[family]
        verified = runtime['state'] == 'verified' and runtime.get('manifest', {}).get('model_id') == service.default_model
        result.append({'id': service.id, 'name': service.name, 'runtime_family': family,
                       'model_id': service.default_model, 'verified': verified})
    return result


def view(session, settings, item, services=None):
    meta = item.metadata_json or {}
    actual = meta.get('actual', {})
    duration = actual.get('duration')
    eligible = (actual.get('full_decode') is True and isinstance(duration, (int, float))
                and math.isfinite(duration) and .1 <= duration <= 30)
    authorized = meta.get('reference_authorized') is True
    try:
        original = safe_media_path(settings.media_root, item.relative_path, must_exist=True)
        intact = file_sha(original) == item.sha256
    except (AppError, OSError):
        intact = False
    preview_available = False
    if item.preview_relative_path:
        try:
            preview_available = safe_media_path(settings.media_root, item.preview_relative_path, must_exist=True).is_file()
        except (AppError, OSError):
            pass
    services = compatible_services(session, settings) if services is None else services
    available = any(service['verified'] for service in services)
    reference = eligible and authorized and intact and item.state == 'active'
    reason = ('库内原始文件丢失或内容变化，请重新导入并核对，未标记为可用。' if not intact else
              '仅供试听；尚未授权将录音用于角色声音参考。' if not authorized else
              '可试听；时长不在本地参考适配器0.1至30秒范围，未自动截断。' if not eligible else
              '已授权参考，但当前没有通过实际运行校验的兼容本地TTS服务。' if not available else
              '可供已验证的本地参考TTS使用；音色相似度仍需实际生成后试听验收。')
    return {'id': item.id, 'name': item.name, 'language': item.language, 'tags': item.tags_json,
            'source_type': item.source_type, 'source_note': meta.get('source_note', ''),
            'voice_description': meta.get('voice_description', ''), 'role_types': meta.get('role_types', []),
            'relative_path': item.relative_path, 'preview_relative_path': item.preview_relative_path,
            'sha256': item.sha256, 'file_size': item.file_size, 'actual': actual,
            'media_url': '/api/media/' + item.relative_path,
            'preview_url': '/api/media/' + (item.preview_relative_path if preview_available else item.relative_path),
            'integrity_status': 'verified' if intact else 'unavailable',
            'reference_authorized': authorized,
            'capabilities': {'playback': actual.get('full_decode') is True and intact and item.state == 'active',
                             'voice_reference': reference, 'tts': reference and available,
                             'voice_clone': False, 'reason': reason,
                             'clone_verification': 'NOT RUN', 'compatible_services': services},
            'state': item.state, 'revision': item.revision,
            'created_at': item.created_at, 'updated_at': item.updated_at}


def listing(session, settings, q='', tag=None, language=None):
    items = list(session.scalars(select(VoiceLibraryAsset).where(VoiceLibraryAsset.state == 'active')
                                .order_by(VoiceLibraryAsset.created_at.desc())))
    query = q.strip().casefold()
    if query:
        items = [item for item in items if query in ' '.join([item.name, item.language, *item.tags_json,
                      str((item.metadata_json or {}).get('source_note', ''))]).casefold()]
    if tag:
        items = [item for item in items if tag in item.tags_json]
    if language:
        items = [item for item in items if language == item.language]
    services = compatible_services(session, settings)
    return {'items': [view(session, settings, item, services) for item in items],
            'limits': {'max_file_bytes': MAX_FILE_BYTES, 'max_batch_files': MAX_BATCH_FILES,
                       'reference_min_seconds': .1, 'reference_max_seconds': 30},
            'source_directory': str(source_directory(settings)),
            'capability_note': '导入与试听不代表声音克隆授权；仅在明确授权后才可作为本地声音参考。'}


def import_bytes(session, settings, filename, data, *, name=None, language='und', tags=None,
                 source_note='', reference_authorized=False, source_type='uploaded_recording', source_path=None):
    suffix = Path(filename).suffix.lower()
    if suffix not in AUDIO_SUFFIXES:
        raise AppError(415, 'voice_audio_type', '请选择WAV/FLAC/MP3/M4A/AAC/OGG音频')
    if not data or len(data) > MAX_FILE_BYTES:
        raise AppError(413, 'voice_audio_size', '音频为空或超过100MiB')
    clean_tags = _tags(tags or [])
    language = language.strip()
    if not language:
        raise AppError(422, 'voice_language_empty', '语言不能为空，可使用und表示未注明')
    sha = hashlib.sha256(data).hexdigest()
    # Do not retain a SQLite read/write transaction while FFmpeg decodes a file;
    # the production worker must be able to persist its own progress meanwhile.
    with Session(session.get_bind()) as reader:
        duplicate_id = reader.scalar(select(VoiceLibraryAsset.id).where(VoiceLibraryAsset.sha256 == sha))
    duplicate = session.get(VoiceLibraryAsset, duplicate_id) if duplicate_id else None
    if duplicate:
        if duplicate.state != 'active':
            raise AppError(409, 'voice_duplicate_removed', '此文件已存在于已移除素材中，请先恢复或处理删除记录')
        path = safe_media_path(settings.media_root, duplicate.relative_path, must_exist=True)
        if file_sha(path) != sha:
            raise AppError(409, 'voice_library_changed', '已入库的相同音频文件发生变化，请核对文件')
        return duplicate, True
    relative = f'voice_library/{uuid.uuid4().hex}/original{suffix}'
    preview_relative = relative.rsplit('/', 1)[0] + '/preview.wav'
    original = safe_media_path(settings.media_root, relative)
    preview = safe_media_path(settings.media_root, preview_relative)
    original.parent.mkdir(parents=True, exist_ok=False)
    created = []
    try:
        with original.open('xb') as stream:
            stream.write(data)
        created.append(original)
        from app.services.media_processing import _probe
        actual = _probe(original, settings, True)
        if not math.isfinite(actual['duration']) or actual['duration'] <= 0:
            raise AppError(422, 'voice_duration_invalid', '音频时长无效')
        command = [settings.ffmpeg_path, '-nostdin', '-v', 'error', '-xerror', '-n', '-i', str(original),
                   '-t', '12', '-vn', '-ar', '24000', '-ac', '1', '-c:a', 'pcm_s16le', str(preview)]
        result = subprocess.run(command, capture_output=True, timeout=180, shell=False,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if preview.exists():
            created.append(preview)
        if result.returncode:
            raise AppError(422, 'voice_preview_failed', '音频预览生成失败，未改动原始录音')
        preview_actual = _probe(preview, settings, True)
        item = VoiceLibraryAsset(name=(name or Path(filename).stem).strip()[:240] or '录音', language=language,
            tags_json=clean_tags, relative_path=relative, preview_relative_path=preview_relative,
            sha256=sha, file_size=len(data), source_type=source_type,
            metadata_json={'actual': actual, 'preview_actual': preview_actual, 'original_filename': Path(filename).name,
                'source_note': source_note, 'reference_authorized': reference_authorized,
                'authorization_scope': 'local_voice_reference_only' if reference_authorized else 'playback_only',
                'source_path': source_path, 'original_preserved': True,
                'voice_identity_verification': 'NOT RUN', 'preview_policy': 'separate_first_12_seconds_pcm_wav'})
        session.add(item)
        session.flush()
        for path, rel, digest in ((original, relative, sha), (preview, preview_relative, file_sha(preview))):
            session.add(m.ManagedFile(relative_path=rel, state='active',
                ownership_json=[{'table': 'voice_library_assets', 'id': item.id}],
                metadata_json={'verified_owned': True, 'sha256': digest, 'kind': 'voice_library'}))
        session.add(m.AuditEvent(object_type='voice_library', object_id=item.id, action='imported',
            details_json={'sha256': sha, 'reference_authorized': reference_authorized,
                          'source_type': source_type, 'original_preserved': True}))
        session.flush()
        return item, False
    except Exception as error:
        for path in reversed(created):
            if path.is_file():
                path.unlink()
        if original.parent.is_dir() and not any(original.parent.iterdir()):
            original.parent.rmdir()
        if isinstance(error, (IntegrityError, OperationalError)):
            raise AppError(409, 'voice_import_busy', '音频正被另一个窗口导入或数据库暂忙，请刷新后重试') from error
        if isinstance(error, (OSError, subprocess.SubprocessError, ValueError, KeyError)):
            raise AppError(422, 'voice_import_failed', '音频校验或预览处理失败；原始录音未改动') from error
        raise


def import_path(session, settings, raw_path, **metadata):
    try:
        path = Path(raw_path).resolve(strict=True)
        root = source_directory(settings).resolve(strict=True)
    except (OSError, ValueError, RuntimeError):
        raise AppError(422, 'voice_source_missing', '指定的本地录音或录音目录不存在；请先放入配置的 imports 目录，或通过页面上传')
    if not path.is_relative_to(root) or not path.is_file():
        raise AppError(422, 'voice_source_scope', '仅可从指定录音目录导入文件')
    if path.stat().st_size > MAX_FILE_BYTES:
        raise AppError(413, 'voice_audio_size', '音频超过100MiB')
    try:
        data = path.read_bytes()
    except OSError:
        raise AppError(422, 'voice_source_unreadable', '本地录音不可读取；原始文件未改动')
    return import_bytes(session, settings, path.name, data, source_type='local_recording',
                        source_path=str(path), **metadata)


def references(session, identifier):
    get_asset(session, identifier, True)
    groups = {}
    for audio in session.scalars(select(m.AudioAsset)):
        if (audio.metadata_json or {}).get('voice_library_asset_id') != identifier:
            continue
        project = session.get(m.Project, audio.project_id)
        group = groups.setdefault(audio.project_id, {'project_id': audio.project_id, 'name': project.name if project else '项目缺失',
                                  'audio_ids': [], 'character_ids': []})
        group['audio_ids'].append(audio.id)
        if audio.character_version_id:
            version = session.get(m.CharacterVersion, audio.character_version_id)
            if version and version.character_id not in group['character_ids']:
                group['character_ids'].append(version.character_id)
    return {'projects': list(groups.values()), 'reference_count': sum(len(row['audio_ids']) for row in groups.values())}


def update(session, settings, identifier, payload):
    item = get_asset(session, identifier)
    if item.revision != payload.expected_revision:
        raise AppError(409, 'voice_library_changed', '配音素材已在其他窗口修改，请刷新')
    changes = payload.model_dump(exclude_unset=True, exclude={'expected_revision'})
    meta = dict(item.metadata_json or {})
    for field, value in changes.items():
        if value is None:
            raise AppError(422, 'voice_metadata_null', '元数据字段不可设为空值')
        if field == 'name':
            if not value.strip():
                raise AppError(422, 'voice_name_empty', '声音名称不能为空')
            item.name = value.strip()
        elif field == 'language':
            if not value.strip():
                raise AppError(422, 'voice_language_empty', '语言不能为空，可使用und表示未注明')
            item.language = value.strip()
        elif field == 'tags':
            item.tags_json = _tags(value)
        elif field == 'role_types':
            meta[field] = _tags(value)
        else:
            meta[field] = value
    if 'reference_authorized' in changes:
        meta['authorization_scope'] = 'local_voice_reference_only' if meta['reference_authorized'] else 'playback_only'
    item.metadata_json = meta
    item.revision += 1
    session.add(m.AuditEvent(object_type='voice_library', object_id=item.id, action='metadata_updated',
        details_json={'fields': sorted(changes), 'revision': item.revision}))
    session.flush()
    return view(session, settings, item)


def assert_reference_authorized(session, audio):
    """Guard only new library references; historical references remain compatible."""
    identifier = (audio.metadata_json or {}).get('voice_library_asset_id') if audio else None
    if not identifier:
        return
    item = get_asset(session, identifier)
    if (item.metadata_json or {}).get('reference_authorized') is not True:
        raise AppError(409, 'voice_reference_authorization_required', '此录音仅供试听，尚未授权用作角色声音参考')


def bind(session, settings, identifier, payload):
    from app.services.studio_voice import save_voice
    item = get_asset(session, identifier)
    version = session.get(m.CharacterVersion, payload.version_id)
    if not version or version.character_id != payload.character_id:
        raise AppError(422, 'voice_version_scope', '人物版本与角色不匹配')
    from app.services.studio_voice import _current
    _, people, _ = _current(session, payload.project_id)
    if not any(person.character_id == payload.character_id and person.character_version_id == payload.version_id for person in people):
        raise AppError(422, 'voice_version_scope', '人物版本未绑定到当前项目的当前剧本')
    actual = (item.metadata_json or {}).get('actual', {})
    if actual.get('full_decode') is not True or not .1 <= float(actual.get('duration', 0)) <= 30:
        raise AppError(422, 'voice_reference_duration', '角色参考音频需为0.1至30秒；可保留在库中试听')
    path = safe_media_path(settings.media_root, item.relative_path, must_exist=True)
    if file_sha(path) != item.sha256:
        raise AppError(409, 'voice_library_changed', '配音库原始文件发生变化')
    audio = next((asset for asset in session.scalars(select(m.AudioAsset).where(m.AudioAsset.project_id == payload.project_id,
                m.AudioAsset.character_version_id == payload.version_id))
                if (asset.metadata_json or {}).get('voice_library_asset_id') == item.id), None)
    if audio is None:
        audio = m.AudioAsset(project_id=payload.project_id, character_version_id=payload.version_id,
            relative_path=item.relative_path, sha256=item.sha256,
            metadata_json={'voice_library_asset_id': item.id, 'voice_library_revision': item.revision,
                'actual': actual, 'source_type': 'voice_library',
                'reference_authorized': (item.metadata_json or {}).get('reference_authorized') is True,
                'authorization_source': 'voice_library_current_metadata; snapshot here records binding time only',
                'source_note': (item.metadata_json or {}).get('source_note', ''),
                'human_approved': False, 'voice_identity_verification': 'NOT RUN'})
        session.add(audio)
        session.flush()
    return save_voice(session, settings, payload.project_id, payload.character_id,
        ProjectVoiceSave(version_id=payload.version_id, audio_id=audio.id, expected_token=payload.expected_token))
