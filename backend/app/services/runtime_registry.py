"""Fixed deployment manifests; HTTP never supplies executable or model paths."""
import functools
import hashlib
import json
import os
import subprocess
from pathlib import Path
from app.core.config import PROJECT_ROOT
from app.core.errors import AppError
from app.schemas_routing import RuntimeManifest, RuntimeReceipt

MANIFEST_ROOT=Path('E:/VideoFactoryRuntime/manifests')
FAMILIES={'cosyvoice3':('cosyvoice3.json','run_cosyvoice3.py','cosyvoice3-0.5b-2512'),
    'indextts25':('indextts25.json','run_indextts25.py','indextts-2.5'),
    'latentsync15':('latentsync15.json','run_latentsync15.py','latentsync-1.5'),
    'seedvr2':('seedvr2.json',None,'seedvr2_3b_fp8_e4m3fn.safetensors')}
REFERENCE_TTS_FAMILIES=frozenset({'cosyvoice3','indextts25'})

@functools.lru_cache(maxsize=256)
def _digest(path,size,modified):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(4*1024*1024),b''):digest.update(block)
    return digest.hexdigest()

def file_sha(path):
    path=Path(path);stat=path.stat()
    return _digest(str(path.resolve()),stat.st_size,stat.st_mtime_ns)

@functools.lru_cache(maxsize=32)
def _decode(path,sha,ffmpeg,ffprobe):
    info=subprocess.run([ffprobe,'-v','error','-show_streams','-show_format','-of','json',path],capture_output=True,timeout=30,shell=False)
    if info.returncode:raise ValueError('Smoke output probe failed')
    raw=json.loads(info.stdout)
    if float(raw.get('format',{}).get('duration') or 0)<=0:raise ValueError('Smoke output duration is missing')
    result=subprocess.run([ffmpeg,'-v','error','-xerror','-i',path,'-f','null','-'],capture_output=True,timeout=180,shell=False)
    if result.returncode:raise ValueError('Smoke output full decode failed')
    return {'streams':[{k:s.get(k) for k in ('codec_type','codec_name','width','height','sample_rate','channels')} for s in raw.get('streams',[])],
            'duration':float(raw['format']['duration']),'full_decode':True,'sha256':sha}

def manifest_status(family,settings,require_verified=False):
    result={'family':family,'state':'unavailable','reasons':[]}
    if family not in FAMILIES:
        result['reasons']=['No fixed adapter for this runtime family'];return result
    if os.getenv('VIDEO_FACTORY_CONTAINER') == '1':
        result['reasons']=['此固定本机运行时依赖 Windows 模型部署清单和可执行环境，无法在 Linux 容器中验证或启动；请在宿主机使用原生部署，或配置受支持的外部服务。']
        if require_verified:
            raise AppError(409,'runtime_unverified','容器内本机模型运行时不可用',{'family':family,'reasons':result['reasons']})
        return result
    filename,helper,model=FAMILIES[family];path=MANIFEST_ROOT/filename
    try:
        raw=json.loads(path.read_text('utf-8-sig'))
        if raw.get('protocol')!='video-factory-deployment-1':raise ValueError('Deployment protocol missing')
        manifest=RuntimeManifest.model_validate(raw)
        if manifest.family!=family or manifest.model_id!=model:raise ValueError('Manifest family/model differs from fixed adapter')
        if manifest.dependency_status!='installed':raise ValueError('Runtime dependencies have not been installed/verified')
        if helper:
            expected=(PROJECT_ROOT/'scripts'/helper).resolve()
            if Path(manifest.helper).resolve()!=expected:raise ValueError('Helper is not the fixed project adapter')
            if not manifest.helper_sha256 or file_sha(expected)!=manifest.helper_sha256:raise ValueError('Helper SHA differs')
            if not Path(manifest.python).is_file() or not Path(manifest.repo).is_dir() or not Path(manifest.model_dir).is_dir():raise ValueError('Runtime executable/repository/model directory unavailable')
        else:
            expected=(PROJECT_ROOT/'workflows/seedvr2_video_v1.json').resolve()
            if Path(manifest.workflow_path).resolve()!=expected or file_sha(expected)!=manifest.workflow_sha256:raise ValueError('SeedVR workflow differs from fixed verified graph')
        if not manifest.components:raise ValueError('Manifest has no model components')
        for component in manifest.components:
            file=Path(component.path)
            if file.stat().st_size!=component.bytes or file_sha(file)!=component.sha256:raise ValueError('Model component missing or SHA changed: '+component.role)
        result.update(state='not_tested',manifest=manifest.model_dump(),manifest_sha256=file_sha(path))
        smoke=manifest.smoke_test
        if manifest.load_status!='verified' or smoke.get('status')!='PASS':
            result['reasons']=['No successful model load and real smoke output'];return result
        receipt=RuntimeReceipt.model_validate(json.loads(Path(smoke['receipt_path']).read_text('utf-8-sig')))
        output=Path(smoke['output_path']).resolve()
        if receipt.status!='succeeded' or receipt.model_id!=model or receipt.slot not in manifest.task_capabilities:raise ValueError('Smoke receipt identity/status mismatch')
        if not receipt.output_path or Path(receipt.output_path).resolve()!=output:raise ValueError('Smoke output does not match receipt')
        sha=file_sha(output)
        if sha!=smoke.get('output_sha256') or sha!=receipt.output_sha256:raise ValueError('Smoke output SHA differs')
        decoded=_decode(str(output),sha,settings.ffmpeg_path,settings.ffprobe_path)
        expected_type='audio' if family in REFERENCE_TTS_FAMILIES else 'video'
        if not any(s['codec_type']==expected_type for s in decoded['streams']):raise ValueError('Smoke output is not the required media type')
        result.update(state='verified',evidence={'receipt_path':smoke['receipt_path'],'output_sha256':sha,'actual':decoded})
    except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError) as exc:
        result['reasons']=[type(exc).__name__+': '+str(exc)[:500]]
    if require_verified and result['state']!='verified':raise AppError(409,'runtime_unverified','运行时未通过文件、依赖和真实输出验证',{'family':family,'reasons':result['reasons']})
    return result
