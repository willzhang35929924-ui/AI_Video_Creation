"""Configuration, keychain and asynchronous video operations for the local UI."""
import ctypes
import ctypes.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.parse import quote, urlparse

HOME=Path(__file__).resolve().parent
PROFILES=HOME/'云端配置'
OUTPUT=HOME/'云端视频'
SERVICE='VideoFactory.CloudVideo'
DEFAULT_IMAGE='https://file-ai.linkfox.com/UPLOAD/MANAGE/fe324e37578d49a0aea7eb4d0ce3060f.jpeg?x-oss-process=image/resize,m_mfit,w_512,h_512&dv2=646174613a7328363a6d6f64656c5f3129'
DEFAULT_PROMPT='以参考图中的人物和服饰为主体，简洁明亮的摄影棚，人物自然微笑并轻轻转身展示服饰，镜头平稳缓慢推进。自然中文女声说：每天的自在，从这一身开始。柔和背景音乐，无字幕，无品牌标识。'
DEFAULT_TEMPLATE={'model':'{{model}}','prompt':'{{prompt}}','image_url':'{{image_url}}','duration':'{{duration}}'}

class MacKeychain:
    def __init__(self):
        self.sec=ctypes.CDLL('/System/Library/Frameworks/Security.framework/Security')
        self.cf=ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        self.sec.SecKeychainAddGenericPassword.argtypes=[ctypes.c_void_p,ctypes.c_uint32,ctypes.c_char_p,ctypes.c_uint32,ctypes.c_char_p,ctypes.c_uint32,ctypes.c_void_p,ctypes.POINTER(ctypes.c_void_p)]
        self.sec.SecKeychainFindGenericPassword.argtypes=[ctypes.c_void_p,ctypes.c_uint32,ctypes.c_char_p,ctypes.c_uint32,ctypes.c_char_p,ctypes.POINTER(ctypes.c_uint32),ctypes.POINTER(ctypes.c_void_p),ctypes.POINTER(ctypes.c_void_p)]
        self.sec.SecKeychainItemDelete.argtypes=[ctypes.c_void_p]
        self.sec.SecKeychainItemFreeContent.argtypes=[ctypes.c_void_p,ctypes.c_void_p]
        self.cf.CFRelease.argtypes=[ctypes.c_void_p]
    def _find(self, ident):
        service=SERVICE.encode(); account=ident.encode(); length=ctypes.c_uint32(); data=ctypes.c_void_p(); item=ctypes.c_void_p()
        status=self.sec.SecKeychainFindGenericPassword(None,len(service),service,len(account),account,ctypes.byref(length),ctypes.byref(data),ctypes.byref(item))
        return status,length.value,data,item
    def set(self,ident,secret):
        b=secret.encode(); service=SERVICE.encode(); account=ident.encode(); item=ctypes.c_void_p()
        status=self.sec.SecKeychainAddGenericPassword(None,len(service),service,len(account),account,len(b),ctypes.c_char_p(b),ctypes.byref(item))
        if item: self.cf.CFRelease(item)
        if status: raise RuntimeError(f'钥匙串保存失败（系统代码 {status}）；未保存配置。')
    def get(self,ident):
        status,length,data,item=self._find(ident)
        if status: raise RuntimeError(f'无法读取钥匙串密钥（系统代码 {status}）。')
        try: return ctypes.string_at(data,length).decode()
        finally:
            if data: self.sec.SecKeychainItemFreeContent(None,data)
            if item: self.cf.CFRelease(item)
    def delete(self,ident):
        status,length,data,item=self._find(ident)
        if status==-25300: return
        if status: raise RuntimeError(f'无法访问钥匙串（系统代码 {status}）。')
        try:
            status=self.sec.SecKeychainItemDelete(item)
            if status: raise RuntimeError(f'钥匙串删除失败（系统代码 {status}）。')
        finally:
            if data: self.sec.SecKeychainItemFreeContent(None,data)
            if item: self.cf.CFRelease(item)
        status,_,data,item=self._find(ident)
        if data: self.sec.SecKeychainItemFreeContent(None,data)
        if item: self.cf.CFRelease(item)
        if status!=-25300: raise RuntimeError('未能确认钥匙串密钥已删除。')

class WindowsCredentialManager:
    """Windows generic credentials scoped to the signed-in user and machine."""
    class FILETIME(ctypes.Structure):
        _fields_=[('low',ctypes.c_uint32),('high',ctypes.c_uint32)]
    class CREDENTIAL(ctypes.Structure):
        pass
    def __init__(self, library=None, last_error=None):
        if library is None:
            library=ctypes.WinDLL('Advapi32.dll',use_last_error=True)
        self.lib=library
        self.error=last_error or ctypes.get_last_error
        P=ctypes.c_void_p; W=ctypes.c_wchar_p; D=ctypes.c_uint32
        # FILETIME is two 32-bit words, followed by a DWORD and an aligned pointer.
        if not hasattr(self.CREDENTIAL,'_fields_'):
            self.CREDENTIAL._fields_=[('Flags',D),('Type',D),('TargetName',W),('Comment',W),
                ('LastWritten',self.FILETIME),('CredentialBlobSize',D),('CredentialBlob',P),
                ('Persist',D),('AttributeCount',D),('Attributes',P),('TargetAlias',W),('UserName',W)]
        self.lib.CredWriteW.argtypes=[ctypes.POINTER(self.CREDENTIAL),D]; self.lib.CredWriteW.restype=ctypes.c_int
        self.lib.CredReadW.argtypes=[W,D,D,ctypes.POINTER(ctypes.POINTER(self.CREDENTIAL))]; self.lib.CredReadW.restype=ctypes.c_int
        self.lib.CredDeleteW.argtypes=[W,D,D]; self.lib.CredDeleteW.restype=ctypes.c_int
        self.lib.CredFree.argtypes=[P]; self.lib.CredFree.restype=None
    @staticmethod
    def target(ident): return SERVICE+'/'+ident
    def set(self,ident,secret):
        raw=secret.encode('utf-8')
        if not raw or len(raw)>2560: raise ValueError('API Key 为空或超过 Windows 凭据长度限制。')
        data=ctypes.create_string_buffer(raw)
        credential=self.CREDENTIAL(); credential.Type=1; credential.TargetName=self.target(ident)
        credential.CredentialBlobSize=len(raw); credential.CredentialBlob=ctypes.cast(data,ctypes.c_void_p)
        credential.Persist=2; credential.UserName='VideoFactory'
        if not self.lib.CredWriteW(ctypes.byref(credential),0): raise RuntimeError(f'Windows 凭据保存失败（系统代码 {self.error()}）。')
    def get(self,ident):
        ptr=ctypes.POINTER(self.CREDENTIAL)()
        if not self.lib.CredReadW(self.target(ident),1,0,ctypes.byref(ptr)): raise RuntimeError(f'无法读取 Windows 凭据（系统代码 {self.error()}）。')
        try: return ctypes.string_at(ptr.contents.CredentialBlob,ptr.contents.CredentialBlobSize).decode('utf-8')
        finally: self.lib.CredFree(ctypes.cast(ptr,ctypes.c_void_p))
    def delete(self,ident):
        if not self.lib.CredDeleteW(self.target(ident),1,0):
            error=self.error()
            if error!=1168: raise RuntimeError(f'Windows 凭据删除失败（系统代码 {error}）。')
        ptr=ctypes.POINTER(self.CREDENTIAL)()
        if self.lib.CredReadW(self.target(ident),1,0,ctypes.byref(ptr)):
            self.lib.CredFree(ctypes.cast(ptr,ctypes.c_void_p)); raise RuntimeError('未能确认 Windows 凭据已删除。')
        if self.error()!=1168: raise RuntimeError(f'无法确认凭据删除状态（系统代码 {self.error()}）。')

def Keychain():
    if sys.platform=='darwin': return MacKeychain()
    if sys.platform=='win32': return WindowsCredentialManager()
    raise RuntimeError('当前系统没有受支持的安全凭据存储。')

def safe_id(ident):
    if not re.fullmatch(r'[0-9a-f]{32}',ident): raise ValueError('配置编号无效')
    return ident

def directory(ident):
    p=PROFILES/safe_id(ident)
    if p.is_symlink(): raise ValueError('配置目录是符号链接，已拒绝访问')
    return p

def atomic_json(path, value):
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.tmp-',dir=str(path.parent))
    try:
        if hasattr(os,'fchmod'): os.fchmod(fd,0o600)
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(value,f,ensure_ascii=False,indent=2); f.flush(); os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

def validate_url(url, allow_placeholder=False):
    probe=url.replace('{task_id}','test') if allow_placeholder else url
    p=urlparse(probe)
    if p.scheme!='https' or not p.hostname or p.username or p.password or p.fragment: raise ValueError('接口必须使用不含账号密码或片段的 HTTPS 地址。')
    return url

def template_value(value):
    return json.loads(value) if isinstance(value,str) else value

def validate(profile):
    p=dict(profile)
    if not p.get('name','').strip(): raise ValueError('请填写配置名称。')
    if p.get('kind') not in ('linkfox','generic'): raise ValueError('不支持的协议类型。')
    validate_url(p.get('submit_url',''))
    validate_url(p.get('poll_url',''),True)
    if p['kind']=='generic':
        if p.get('poll_method') not in ('GET','POST'): raise ValueError('查询方法必须为 GET 或 POST。')
        if p['poll_method']=='GET' and '{task_id}' not in p['poll_url']: raise ValueError('GET 查询地址必须包含 {task_id}。')
        header=p.get('auth_header','Authorization'); prefix=p.get('auth_prefix','Bearer ')
        if not isinstance(header,str) or not re.fullmatch(r'[A-Za-z0-9-]+',header): raise ValueError('认证请求头名称无效。')
        if not isinstance(prefix,str) or '\r' in prefix or '\n' in prefix: raise ValueError('认证前缀无效。')
        for key in ('success_values','failure_values'):
            if not isinstance(p.get(key),str) or not p[key].strip(): raise ValueError(key+' 必须是非空的逗号分隔文本。')
        for key in ('submit_template','poll_template'):
            try:
                value=template_value(p.get(key,{}))
                if not isinstance(value,(dict,list)): raise ValueError('expected object or array')
            except Exception as exc: raise ValueError(key+' 必须是有效 JSON 对象或数组。') from exc
        for key in ('id_path','status_path','result_path'):
            if not p.get(key,'').strip(): raise ValueError(key+' 不能为空。')
    if not p.get('model','').strip(): raise ValueError('请填写模型名称。')
    return p

def list_profiles():
    if not PROFILES.exists(): return []
    result=[]
    for entry in PROFILES.iterdir():
        if entry.is_symlink() or not entry.is_dir() or not re.fullmatch(r'[0-9a-f]{32}',entry.name): continue
        try:
            p=json.loads((entry/'profile.json').read_text(encoding='utf-8'))
            if p.get('id')==entry.name: result.append(p)
        except (OSError, ValueError): continue
    return sorted(result,key=lambda x:(x.get('name','').casefold(),x['id']))

def create_profile(profile,key,keychain=None):
    if not key.strip(): raise ValueError('请填写 API Key。')
    p=validate(profile); p['id']=uuid.uuid4().hex
    PROFILES.mkdir(mode=0o700,parents=True,exist_ok=True)
    d=directory(p['id']); d.mkdir(mode=0o700)
    kc=keychain or Keychain()
    try:
        kc.set(p['id'],key)
        try: atomic_json(d/'profile.json',p)
        except Exception:
            kc.delete(p['id']); raise
    except Exception:
        try: d.rmdir()
        except OSError: pass
        raise
    return p

def update_profile(ident, changes):
    path=directory(ident)/'profile.json'
    if path.is_symlink() or not path.is_file(): raise ValueError('配置文件不存在或是符号链接。')
    current=json.loads(path.read_text(encoding='utf-8'))
    if current.get('id')!=ident: raise ValueError('配置编号不匹配。')
    updated={**current,**changes,'id':ident}
    validate(updated)
    atomic_json(path,updated)
    return updated

def test_settings(profile):
    """Return an explicitly read-only probe contract; never submit video jobs."""
    p=validate(profile)
    if p['kind']=='linkfox':
        parsed=urlparse(p['submit_url'])
        return {'url':parsed.scheme+'://'+parsed.netloc+'/v1/userPlan/info',
                'method':'POST','body':{},'path':'code','expected':'200',
                'header':'Authorization','prefix':'Bearer '}
    url=p.get('test_url','').strip()
    if not url: raise ValueError('尚未配置只读测试接口，请先设置测试接口。')
    validate_url(url)
    method=p.get('test_method','GET')
    if method not in ('GET','POST'): raise ValueError('测试方法必须为 GET 或 POST。')
    path=p.get('test_success_path','').strip()
    expected=p.get('test_success_value','').strip()
    if not path or not expected: raise ValueError('请填写测试响应中的成功字段和期望值。')
    # Never test the configured task creation endpoint, even if marked read-only.
    test_parsed=urlparse(url); submit_parsed=urlparse(p['submit_url'])
    if (test_parsed.scheme,test_parsed.netloc,test_parsed.path.rstrip('/')) == (submit_parsed.scheme,submit_parsed.netloc,submit_parsed.path.rstrip('/')):
        raise ValueError('测试地址不能与视频提交地址相同。')
    body=template_value(p.get('test_body',{}))
    if not isinstance(body,(dict,list)): raise ValueError('测试请求体必须是 JSON 对象或数组。')
    return {'url':url,'method':method,'body':body,'path':path,'expected':expected,
            'header':p.get('auth_header','Authorization'),'prefix':p.get('auth_prefix','Bearer ')}

def probe_connection(profile,key):
    checked=int(time.time())
    try: cfg=test_settings(profile)
    except (ValueError, TypeError, AttributeError) as exc:
        return {'state':'unconfigured','message':str(exc),'checked_at':checked}
    if not key.strip(): return {'state':'failed','message':'没有提供 API Key。','checked_at':checked}
    opts=('url = '+json.dumps(cfg['url'])+'\nheader = '+json.dumps(cfg['header']+': '+cfg['prefix']+key)
          +'\nheader = "Content-Type: application/json"\n')
    if cfg['method']=='POST': opts+='data = '+json.dumps(json.dumps(cfg['body'],ensure_ascii=True))+'\n'
    else: opts+='request = "GET"\n'
    try:
        proc=subprocess.run(['curl','--silent','--show-error','--max-time','15','--max-filesize','1048576',
                             '--write-out','\n%{http_code}','--config','-'],input=opts,text=True,
                            capture_output=True,timeout=20)
    except (subprocess.TimeoutExpired, OSError):
        return {'state':'failed','message':'连接超时或无法启动网络请求。','checked_at':checked}
    if proc.returncode:
        message='连接超时。' if proc.returncode==28 else '网络连接或 TLS 验证失败。'
        return {'state':'failed','message':message,'checked_at':checked}
    try:
        body,http=proc.stdout.rsplit('\n',1); code=int(http)
        if code in (401,403): raise PermissionError(f'认证或权限被拒绝（HTTP {code}）。')
        if not 200<=code<300: raise ValueError(f'接口返回 HTTP {code}。')
        data=json.loads(body); actual=lookup(data,cfg['path'])
        if str(actual).lower()!=cfg['expected'].lower(): raise ValueError('响应中的成功条件不匹配；请检查密钥、权限和条件。')
    except (ValueError, KeyError, IndexError, TypeError, PermissionError) as exc:
        message=str(exc) if isinstance(exc,(PermissionError,ValueError)) and not isinstance(exc,json.JSONDecodeError) else '测试响应不是预期的 JSON 或缺少成功字段。'
        return {'state':'failed','message':message,'checked_at':checked}
    message=('只读接口和认证验证通过；未验证视频生成。' if profile['kind']=='linkfox'
             else '只读接口响应符合成功条件；认证和视频生成能力需向供应商确认。')
    return {'state':'connected','message':message,'checked_at':checked}

def remove_profile(ident,keychain=None):
    d=directory(ident)
    if not d.is_dir(): raise ValueError('配置不存在。')
    # Refuse unknown files or links, preventing unintended deletion of unrelated data.
    entries=list(d.iterdir()); allowed={'profile.json','history.json'}
    if any(p.name not in allowed or p.is_symlink() or not p.is_file() for p in entries): raise RuntimeError('配置目录存在未识别文件；已停止，请人工检查。')
    (keychain or Keychain()).delete(ident)
    for p in entries: p.unlink()
    d.rmdir()
    if d.exists(): raise RuntimeError('配置目录仍存在，删除未完成。')

def get_history(ident):
    f=directory(ident)/'history.json'
    if not f.exists(): return []
    if f.is_symlink(): raise ValueError('历史记录是符号链接')
    data=json.loads(f.read_text(encoding='utf-8'))
    if not isinstance(data,list): raise ValueError('历史记录格式无效')
    return data

def put_history(ident,entry):
    items=get_history(ident)
    items=[x for x in items if x.get('task_id') != entry.get('task_id')]
    items.insert(0,entry); atomic_json(directory(ident)/'history.json',items)

def lookup(obj,path):
    current=obj
    for part in path.split('.'):
        if isinstance(current,list): current=current[int(part)]
        else: current=current[part]
    return current

def fill(value,variables):
    if isinstance(value,dict): return {k:fill(v,variables) for k,v in value.items()}
    if isinstance(value,list): return [fill(v,variables) for v in value]
    if not isinstance(value,str): return value
    for name,v in variables.items():
        if value=='{{'+name+'}}': return v
        value=value.replace('{{'+name+'}}',str(v))
    return value

def call_json(url,method,key,body=None,auth_header='Authorization',auth_prefix='Bearer '):
    validate_url(url)
    opts='url = '+json.dumps(url)+'\nheader = '+json.dumps(auth_header+': '+auth_prefix+key)+'\nheader = "Content-Type: application/json"\n'
    if method=='POST': opts+='data = '+json.dumps(json.dumps(body,ensure_ascii=True))+'\n'
    else: opts+='request = "GET"\n'
    try: proc=subprocess.run(['curl','--silent','--show-error','--fail-with-body','--max-time','45','--config','-'],input=opts,text=True,capture_output=True,timeout=50)
    except subprocess.TimeoutExpired as exc: raise RuntimeError('请求超时，结果可能未知；请先核对供应商任务。') from exc
    if proc.returncode: raise RuntimeError('接口请求失败；如刚提交任务，结果可能未知，请先核对供应商后台。')
    try: return json.loads(proc.stdout)
    except ValueError as exc: raise RuntimeError('接口未返回有效 JSON；请核对协议及供应商任务。') from exc

def download(url,ident,task):
    validate_url(url)
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,150}',task) or task in ('.','..'): raise ValueError('任务编号不适合用作文件名。')
    OUTPUT.mkdir(mode=0o700,parents=True,exist_ok=True)
    if OUTPUT.is_symlink(): raise ValueError('视频目录是符号链接')
    target=OUTPUT/(safe_id(ident)+'-'+task+'.mp4'); partial=target.with_suffix('.part')
    opts='url = '+json.dumps(url)+'\n'
    p=subprocess.run(['curl','--fail','--silent','--show-error','--location','--max-time','180','--output',str(partial),'--config','-'],input=opts,text=True,capture_output=True,timeout=185)
    if p.returncode: raise RuntimeError('视频下载失败，可选择原任务恢复查询。')
    if partial.stat().st_size<128: raise RuntimeError('下载内容过小，请核对任务结果。')
    os.replace(partial,target); return str(target)

def generate(profile,key,prompt,image,duration,resolution,ratio,existing='',progress=lambda *a:None):
    p=validate(profile); ident=safe_id(p['id']); task=existing.strip()
    variables={'model':p['model'],'prompt':prompt,'image_url':image,'duration':duration,'resolution':resolution,'aspect_ratio':ratio,'task_id':task}
    if not task:
        if p['kind']=='linkfox':
            body={'videoType':p['model'],'prompt':prompt,'imageList':[image],'videoTime':duration,'resolution':resolution,'aspectRatio':ratio}
        else: body=fill(template_value(p['submit_template']),variables)
        auth={'auth_header':p.get('auth_header','Authorization'),'auth_prefix':p.get('auth_prefix','Bearer ')}
        data=call_json(p['submit_url'],'POST',key,body,**auth)
        if p['kind']=='linkfox' and str(data.get('code'))!='200': raise RuntimeError('LinkFox 拒绝：'+str(data.get('msgKey') or data.get('msg')))
        try: task=str(lookup(data,p['id_path']))
        except (KeyError,IndexError,TypeError,ValueError) as exc: raise RuntimeError('提交后未取得任务编号，可能已受理；请在供应商后台核对，勿重复提交。') from exc
        if not task or task=='None': raise RuntimeError('提交结果没有任务编号，请在供应商后台核对。')
        progress('任务编号 '+task+'：已提交')
        try: put_history(ident,{'task_id':task,'status':'submitted','created_at':int(time.time())})
        except Exception as exc: raise RuntimeError('任务已受理，编号 '+task+'；但本地历史保存失败，请记录编号后恢复查询。') from exc
    progress('任务编号 '+task+'：查询中')
    variables['task_id']=task
    for _ in range(80):
        url=p['poll_url'].replace('{task_id}',quote(task,safe=''))
        method=p.get('poll_method','POST')
        body={'id':task} if p['kind']=='linkfox' else fill(template_value(p.get('poll_template',{})),variables)
        auth={'auth_header':p.get('auth_header','Authorization'),'auth_prefix':p.get('auth_prefix','Bearer ')}
        data=call_json(url,method,key,body,**auth)
        if p['kind']=='linkfox' and str(data.get('code'))!='200': raise RuntimeError('查询失败，任务编号 '+task+'；请恢复查询。')
        try: status=str(lookup(data,p['status_path'])).strip().lower()
        except (KeyError,IndexError,TypeError,ValueError) as exc: raise RuntimeError('任务 '+task+' 状态路径无效；请核对配置。') from exc
        progress('任务 '+task+'：'+status)
        success=[x.strip().lower() for x in p.get('success_values','3').split(',') if x.strip()]
        failed=[x.strip().lower() for x in p.get('failure_values','4').split(',') if x.strip()]
        if status in failed:
            put_history(ident,{'task_id':task,'status':'failed','created_at':int(time.time())}); raise RuntimeError('供应商报告任务失败：'+task)
        if status in success:
            try: url=lookup(data,p['result_path'])
            except (KeyError,IndexError,TypeError,ValueError) as exc: raise RuntimeError('任务 '+task+' 已完成但结果路径无效。') from exc
            output=download(url,ident,task)
            put_history(ident,{'task_id':task,'status':'completed','created_at':int(time.time()),'file':output})
            return output,task
        time.sleep(15)
    raise RuntimeError('查询超时；可用原任务编号恢复，避免重复扣费：'+task)
