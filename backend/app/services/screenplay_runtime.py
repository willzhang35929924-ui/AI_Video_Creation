"""On-demand startup for one fixed local model; HTTP never supplies commands."""
import asyncio
import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import weakref
from urllib.parse import urlsplit

import httpx
from app.core.config import PROJECT_ROOT
from app.core.errors import AppError
from app.core.paths import safe_media_path

BASE='http://127.0.0.1:8771/v1'
MODEL='Qwen/Qwen3-VL-2B-Instruct'
PYTHON=Path('E:/VideoFactoryRuntime/envs/screenplay_qwen/Scripts/python.exe')
SCRIPT=PROJECT_ROOT/'scripts/serve_screenplay_qwen.py'
MANIFEST=Path('E:/VideoFactoryRuntime/manifests/screenplay_qwen_environment.json')
_locks=weakref.WeakKeyDictionary()
_process=None

def factory_base_url(*, required=True):
    """Configured local factory protocol; never infer a host from client data."""
    if os.getenv('VIDEO_FACTORY_CONTAINER')!='1':
        return BASE
    value=os.getenv('LOCAL_SCREENPLAY_BASE_URL','').strip().rstrip('/')
    if not value:
        if not required:return None
        raise AppError(409,'screenplay_local_not_configured','容器尚未配置本地视频分析工厂地址；请设置 LOCAL_SCREENPLAY_BASE_URL 与受信任的本地服务 origin，或不启用本地模型分析')
    try:
        parsed=urlsplit(value)
        valid=(parsed.scheme in {'http','https'} and parsed.hostname and parsed.path=='/v1' and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment)
        _=parsed.port
    except ValueError:
        valid=False
    if not valid:raise AppError(409,'screenplay_local_configuration','LOCAL_SCREENPLAY_BASE_URL 必须是无凭据和查询参数的本地工厂 /v1 地址')
    from app.core.trusted_service_origins import is_trusted_local_service
    if not is_trusted_local_service(value):
        raise AppError(409,'screenplay_local_untrusted','本地工厂 origin 未在 TRUSTED_LOCAL_SERVICE_ORIGINS 中明确允许，未发送素材')
    return value

def factory_snapshot():
    return {'provider':'local_llm','base_url':factory_base_url(),'model_id':MODEL,'is_cloud':False}

def factory_health_matches(health):
    """Compare a trusted host's actual identity without treating its path as local."""
    if not isinstance(health,dict) or health.get('service')!='video-factory-screenplay-qwen-v1':return False
    if health.get('model_id')!=MODEL or health.get('offline') is not True:return False
    if not isinstance(health.get('workspace'),str) or not health['workspace'].strip():return False
    if not isinstance(health.get('busy'),bool) or not isinstance(health.get('loaded'),bool):return False
    try:expected=hashlib.sha256(SCRIPT.read_bytes()).hexdigest()
    except OSError:return False
    if health.get('script_sha256')!=expected:return False
    workspace=os.getenv('LOCAL_SCREENPLAY_EXPECTED_WORKSPACE','').strip().replace('\\','/').rstrip('/')
    return not workspace or health['workspace'].replace('\\','/').rstrip('/').casefold()==workspace.casefold()

async def _ensure_container(base):
    async with httpx.AsyncClient(timeout=3,trust_env=False,follow_redirects=False) as client:
        try:
            response=await client.get(base[:-3]+'/health');response.raise_for_status();health=response.json()
        except (httpx.HTTPError,ValueError) as error:
            raise AppError(409,'screenplay_local_health','配置的本地模型工厂不可达或健康检查失败；请启动宿主机服务并检查地址，未发送素材或重复启动') from error
    if not factory_health_matches(health):
        raise AppError(409,'screenplay_local_identity','本地模型工厂身份、离线状态、脚本SHA或预期工作区不符，未发送素材')

async def ensure_local_service(snapshot):
    if snapshot.get('provider')!='local_llm':return
    requested=snapshot.get('base_url','').rstrip('/')
    if os.getenv('VIDEO_FACTORY_CONTAINER')=='1':
        configured=factory_base_url(required=False)
        if requested==BASE and not configured:
            raise AppError(409,'screenplay_local_not_configured','容器的127.0.0.1不是宿主机，请配置并信任实际本地语言服务地址')
        if requested!=configured:return  # Other explicitly configured services use the standard service boundary.
        if snapshot.get('model_id')!=MODEL:raise AppError(409,'screenplay_local_model','此工厂只支持已部署的 Qwen3-VL-2B 模型')
        loop=asyncio.get_running_loop()
        async with _locks.setdefault(loop,asyncio.Lock()):await _ensure_container(configured)
        return
    if requested!=BASE:return
    if snapshot.get('model_id')!=MODEL:raise AppError(409,'screenplay_local_model','此固定本地服务只支持已部署的Qwen3-VL-2B模型')
    loop=asyncio.get_running_loop()
    async with _locks.setdefault(loop,asyncio.Lock()):await _ensure()

async def maybe_transcribe_audio(snapshot,settings,source):
    if snapshot.get('provider')!='local_llm' or not source.get('audio_relative_path'):
        return source
    factory=factory_base_url(required=False)
    if not factory or snapshot.get('base_url','').rstrip('/')!=factory:
        return source
    path=safe_media_path(settings.media_root,source['audio_relative_path'],must_exist=True)
    data=path.read_bytes()
    if len(data)>3_000_000 or hashlib.sha256(data).hexdigest()!=source.get('audio_sha256'):
        raise AppError(409,'screenplay_audio_source_changed','视频原音轨大小或SHA发生变化，未转写')
    await ensure_local_service(snapshot)
    async with httpx.AsyncClient(timeout=None,trust_env=False) as client:
        response=await client.post(factory[:-3]+'/factory/transcribe',json={'audio_base64':base64.b64encode(data).decode('ascii')})
        response.raise_for_status();result=response.json()
    receipt=result.get('receipt',{})
    if receipt.get('model_id')!='openai/whisper-base' or receipt.get('input_sha256')!=source['audio_sha256'] or (os.getenv('VIDEO_FACTORY_CONTAINER')=='1' and receipt.get('offline') is not True):
        raise AppError(502,'screenplay_transcription_receipt','音轨转写模型或输入SHA回执不符')
    return {**source,'audio_transcription':'transcribed_unverified','transcript':{
        'text':result.get('text',''),'segments':result.get('segments',[]),'language':result.get('language'),
        'speaker':'unknown','needs_review':True},'transcription_receipt':receipt}

async def _ensure():
    global _process
    expected=hashlib.sha256(SCRIPT.read_bytes()).hexdigest()
    async with httpx.AsyncClient(timeout=3,trust_env=False) as client:
        try:
            response=await client.get('http://127.0.0.1:8771/health');response.raise_for_status();health=response.json()
            if health.get('service')!='video-factory-screenplay-qwen-v1' or health.get('script_sha256')!=expected or Path(health.get('workspace','')).resolve()!=PROJECT_ROOT.resolve():
                raise AppError(409,'screenplay_port_in_use','8771端口服务身份或代码版本不符，未停止其他进程')
            return
        except httpx.ConnectError:pass
        except (httpx.HTTPError,ValueError) as error:raise AppError(409,'screenplay_local_health','本地剧本服务健康检查失败，未启动重复服务') from error
    try:manifest=json.loads(MANIFEST.read_text('utf-8-sig'))
    except (OSError,ValueError) as error:raise AppError(409,'screenplay_local_not_installed','本地Qwen独立环境尚未安装验证') from error
    if manifest.get('status')!='PASS' or not PYTHON.is_file():raise AppError(409,'screenplay_local_not_installed','本地Qwen独立环境尚未安装验证')
    logs=PROJECT_ROOT/'logs';logs.mkdir(exist_ok=True)
    with (logs/'screenplay_model_stdout.log').open('ab') as stdout,(logs/'screenplay_model_stderr.log').open('ab') as stderr:
        _process=subprocess.Popen([str(PYTHON),str(SCRIPT)],cwd=PROJECT_ROOT,stdout=stdout,stderr=stderr,
            env={**os.environ,'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8','PYTHONDONTWRITEBYTECODE':'1'},
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),shell=False)
    # Existing one-click shutdown already validates and stops recorded process trees.
    pidfile=PROJECT_ROOT/'data/pids/dev-processes.json'
    if pidfile.exists():
        value=json.loads(pidfile.read_text('utf-8-sig'))
        if Path(value.get('projectRoot','')).resolve()==PROJECT_ROOT.resolve():
            value['processes']=[r for r in value.get('processes',[]) if r.get('kind')!='screenplay_model']+[
              {'kind':'screenplay_model','pid':_process.pid,'startedAtUtc':dt.datetime.now(dt.timezone.utc).isoformat(),
               'executablePath':str(PYTHON),'workingDirectory':str(PROJECT_ROOT)}]
            temporary=pidfile.with_suffix('.screenplay.tmp');temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2),'utf-8-sig');temporary.replace(pidfile)
    async with httpx.AsyncClient(timeout=2,trust_env=False) as client:
        for _ in range(40):
            if _process.poll() is not None:raise AppError(409,'screenplay_local_start_failed','本地剧本服务启动失败，请检查本机服务日志')
            try:
                response=await client.get('http://127.0.0.1:8771/health')
                if response.status_code==200 and response.json().get('script_sha256')==expected:return
            except (httpx.HTTPError,ValueError):pass
            await asyncio.sleep(.5)
    raise AppError(409,'screenplay_local_start_timeout','本地剧本服务启动超时，保留现有进程供核对，未重复启动')
