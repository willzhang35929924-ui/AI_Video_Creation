"""Existing Comfy Python + official Real-ESRGAN. No implicit model downloads."""
import hashlib,json,os
from pathlib import Path
from app.core.config import PROJECT_ROOT

def runtime(settings):
    return {'python':os.getenv('VIDEO_FACTORY_UPSCALE_PYTHON','D:/Dev/Python/Envs/ComfyUI-Independent-20261011/Scripts/python.exe'),
        'model':str(settings.media_root.parent/'comfyui_runtime/models/upscale_models/RealESRGAN_x4plus.pth'),
        'script':str(PROJECT_ROOT/'scripts/upscale_video_realesrgan.py'),
        'verification':str(settings.media_root.parent/'super_resolution/verified.json')}

def status(settings):
    paths=runtime(settings)
    missing=[key for key in ['python','model','script'] if not Path(paths[key]).is_file()]
    result={'state':'not_configured' if missing else 'detected_not_tested','model':'RealESRGAN_x4plus',
        'method':'image_model_applied_to_every_video_frame','temporal_model':False,
        'verified_resolutions':[],'verified_aspect_ratios':[],'missing':missing,'runtime':paths,
        'limits':['图像模型逐帧处理，不提供时序一致性保证','需检查闪烁、伪造细节与过锐化','不得称原生4K生成']}
    if missing:return result
    model_sha=hashlib.sha256(Path(paths['model']).read_bytes()).hexdigest();result['model_sha256']=model_sha
    script_sha=hashlib.sha256(Path(paths['script']).read_bytes()).hexdigest();result['script_sha256']=script_sha
    try:
        evidence=json.loads(Path(paths['verification']).read_text('utf-8'))
        output=(settings.media_root/evidence['relative_path']).resolve()
        if evidence.get('model_sha256')==model_sha and evidence.get('script_sha256')==script_sha and evidence.get('full_decode')==0 and evidence.get('all_frames_processed') and output.is_relative_to(settings.media_root.resolve()) and output.is_file() and hashlib.sha256(output.read_bytes()).hexdigest()==evidence.get('sha256'):
            result.update(state='verified',verified_resolutions=['4k'],verified_aspect_ratios=[evidence.get('aspect_ratio','16:9')],evidence=evidence)
    except (OSError,ValueError,KeyError):pass
    return result
