"""Selection and immutable snapshots for existing local H3 and custom JSON video jobs.

The cloud adapter implements this application's documented /video/jobs contract.
It is never identified as local H3, nor capability-verified until output decodes.
"""
import base64
import hashlib
import json
import httpx
from io import BytesIO
from dataclasses import replace
from pathlib import Path
from urllib.parse import quote, urlsplit
from PIL import Image, UnidentifiedImageError

from app.core.errors import AppError
from app.core.secrets import reveal
from app.core.trusted_service_origins import is_loopback_service, is_trusted_local_service, service_origin
from app.models import AIService
from app.services.ai_services import resolve_service, service_request, download_public_video
from app.services.providers import JobAccepted, ComfyUIH3Provider


def local_settings(settings, snapshot):
    # The adapter uses direct HTTP. Only loopback or an explicit exact local
    # origin can use that transport; arbitrary public/private hosts cannot.
    url=snapshot['base_url']
    try:
        service_origin(url)
        valid_url = '?' not in url and '#' not in url and '@' not in urlsplit(url).netloc
    except AppError:
        valid_url = False
    if not valid_url or not (is_loopback_service(url) or is_trusted_local_service(url)):
        raise AppError(409, 'remote_comfy_not_supported', 'H3 仅允许回环或管理员配置的受信本地 ComfyUI 地址')
    workflow = Path(snapshot['settings'].get('workflow_path') or settings.h3_workflow_api_json or '')
    return replace(settings, comfyui_base_url=snapshot['base_url'], h3_workflow_api_json=workflow, comfyui_client_timeout_seconds=snapshot.get('settings',{}).get('timeout'))


def select_video_service(session, settings, project_id, service_id=None, model_id=None):
    service, snapshot = resolve_service(session, 'video_generation', service_id, project_id, model_id)
    if service.provider == 'comfyui':
        resolved = local_settings(settings, snapshot)
        graph = json.loads(resolved.h3_workflow_api_json.read_text(encoding='utf-8'))
        loaded = [node['inputs'].get('unet_name') for node in graph['prompt'].values() if node['class_type'] == 'UNETLoader']
        if snapshot['model_id'] not in loaded:
            raise AppError(409, 'workflow_model_mismatch', '所选模型与实际 H3 工作流 UNET 不一致，请配置匹配工作流')
        snapshot['workflow_sha256'] = hashlib.sha256(resolved.h3_workflow_api_json.read_bytes()).hexdigest()
        snapshot['workflow_version'] = graph['_video_factory']['name']
        if service.encrypted_secret:
            raise AppError(409, 'local_auth_not_supported', '当前本地 H3 工作流不支持认证头；请使用本机隔离 ComfyUI 服务')
        return service, snapshot, ComfyUIH3Provider(resolved)
    if service.provider != 'video_api':
        raise AppError(409, 'video_adapter_unavailable', '该配置没有实现的视频适配器')
    contract = service.settings_json.get('video_contract')
    if contract == 'lk888-h3-v2':
        from app.services.lk888_h3 import Lk888H3Video
        return service, snapshot, Lk888H3Video(settings, snapshot, reveal(service.encrypted_secret))
    if contract == 'wan26-i2v':
        return service, snapshot, Wan26ImageToVideo(settings, snapshot, reveal(service.encrypted_secret))
    if contract != 'video-jobs-v1':
        raise AppError(409, 'video_contract_required', '请选择实际支持的视频接口协议；聊天接口不能生成视频')
    return service, snapshot, CloudVideoJobs(settings, snapshot, reveal(service.encrypted_secret))


class CloudVideoJobs:
    name='cloud_video_jobs_v1'
    # No universal cloud prompt limit exists. This optional ceiling belongs to
    # the selected service profile, never to a local model or another provider.
    def __init__(self, settings, snapshot, secret):
        self.settings, self.snapshot, self.secret = settings, snapshot, secret

    async def submit(self, request):
        if not isinstance(request.prompt, str) or not request.prompt.strip():
            raise AppError(422, 'cloud_prompt_invalid', '云端视频提示词不能为空')
        limit = self.snapshot.get('settings', {}).get('max_prompt_characters')
        if limit is not None and (type(limit) is not int or limit < 1):
            raise AppError(422, 'invalid_prompt_limit', '云端提示词字数上限必须为正整数或留空')
        if limit is not None and len(request.prompt) > limit:
            raise AppError(422, 'cloud_prompt_too_long',
                           f'当前云端视频服务配置的提示词上限为 {limit} 字（本次 {len(request.prompt)} 字）；请在成片审核中编辑本次提示词后重试，系统不会自动截断')
        references=[]
        for ref in request.reference_assets:
            try:
                data=Path(ref['absolute_path']).read_bytes()
            except (KeyError,TypeError,OSError) as exc:
                raise AppError(409,'reference_unreadable','已锁定参考图无法读取') from exc
            if hashlib.sha256(data).hexdigest()!=ref['sha256']:
                raise AppError(409,'reference_hash_changed','已锁定参考图哈希已变化')
            try:
                with Image.open(BytesIO(data)) as image:
                    mime={'PNG':'image/png','JPEG':'image/jpeg','WEBP':'image/webp','BMP':'image/bmp','GIF':'image/gif'}.get(image.format)
                    image.verify()
            except (UnidentifiedImageError,OSError,ValueError,Image.DecompressionBombError) as exc:
                raise AppError(422,'reference_invalid','参考图无法完整解码') from exc
            if not mime:
                raise AppError(422,'reference_invalid','云端参考图格式不受支持')
            references.append({'asset_id':ref['asset_id'],'image_url':'data:'+mime+';base64,'+base64.b64encode(data).decode()})
        try:
            response=await service_request(self.settings,self.snapshot,'POST','/video/jobs',self.secret,json={'client_request_id':request.job_id,'model':self.snapshot['model_id'],'prompt':request.prompt,'duration_seconds':request.duration_seconds,'width':request.parameters['width'],'height':request.parameters['height'],'fps':24,'reference_images':references,'seed':request.seed})
        except httpx.TransportError as exc:
            raise AppError(502,'cloud_submission_unknown','云端提交结果未知，未自动重试；请按本地任务 ID 在供应商核对，避免重复计费') from exc
        if response.status_code==408 or response.status_code>=500:
            raise AppError(502,'cloud_submission_unknown',f'云端提交 HTTP {response.status_code}，受理结果未知；请到供应商后台核对，未自动重提')
        if response.status_code>=400:
            raise AppError(502,'cloud_video_submit_failed',f'云端视频提交 HTTP {response.status_code}；未返回原始认证或服务响应')
        try:
            payload=response.json()
        except (ValueError, TypeError) as exc:
            raise AppError(502,'cloud_submission_unknown','云端已响应成功，但无法读取真实任务 ID；可能已经计费，请按本地任务 ID 核对，未自动重提') from exc
        if not isinstance(payload,dict) or not isinstance(payload.get('id'),str) or not payload['id']:
            raise AppError(502,'cloud_submission_unknown','云端已响应成功但缺少真实任务 ID；受理结果未知，请按本地任务 ID 核对，未自动重提')
        return JobAccepted(payload['id'],'queued',metadata={'workflow':'custom-video-jobs-v1','provider':'cloud_video_jobs_v1','mode':'cloud'})

    async def get_job(self,identifier):
        response=await service_request(self.settings,self.snapshot,'GET','/video/jobs/'+quote(identifier,safe=''),self.secret)
        if response.status_code>=400:
            raise AppError(502,'cloud_video_status_failed',f'云端状态 HTTP {response.status_code}；保留原始任务 ID')
        payload=response.json()
        mapping={'queued':'queued','running':'running','succeeded':'generated','failed':'failed','cancelled':'cancelled'}
        if payload.get('status') not in mapping:
            raise AppError(502,'cloud_video_invalid_status','服务未返回 video-jobs-v1 定义的实际状态')
        return {'provider_job_id':identifier,'status':mapping[payload['status']],'output_url':payload.get('output_url'),'raw':{'id':identifier,'status':payload['status']},'error':'云端报告生成失败；请在供应商后台核查任务 '+identifier if payload['status']=='failed' else None}

    async def download(self,url):
        return await download_public_video(self.settings,url)

    async def cancel(self,identifier):
        # No generic cancellation semantics can be assumed from job submission.
        raise AppError(409,'cloud_cancel_unverified','此自定义视频契约未验证取消能力；原任务保持真实状态，请在供应商后台处理')


class Wan26ImageToVideo(CloudVideoJobs):
    """Wan 2.6 documented asynchronous first-frame API, not multi-reference R2V."""
    name='cloud_wan26_i2v'
    MODELS=frozenset({'wan2.6-i2v', 'wan2.6-i2v-flash'})

    async def submit(self, request):
        base=urlsplit(self.snapshot['base_url'])
        if base.scheme!='https' or base.path.rstrip('/')!='/api/v1':
            raise AppError(422,'wan_base_url_invalid','万相首帧接口需要 HTTPS 原生 API 地址，路径为 /api/v1；不能使用聊天 compatible-mode 地址')
        model=self.snapshot.get('model_id')
        if model not in self.MODELS:
            raise AppError(422,'wan_model_unsupported','请选择 wan2.6-i2v 或 wan2.6-i2v-flash；qwen3.8-max 是语言模型')
        if len(request.reference_assets)!=1:
            raise AppError(422,'wan_single_frame_required','万相首帧接口只接受一张已审核首帧；当前引用数量不为一，不会忽略其他参考图')
        if not request.prompt or len(request.prompt)>1500 or len(request.negative_prompt or '')>500:
            raise AppError(422,'wan_prompt_length','万相 2.6 提示词须为 1–1500 字，负面提示词最多 500 字；不会静默截断')
        duration=request.duration_seconds
        if not isinstance(duration,(int,float)) or duration!=int(duration) or not 2<=duration<=15:
            raise AppError(422,'wan_duration_invalid','万相 2.6 视频时长必须为 2–15 秒的整数')
        if request.seed is not None and (not isinstance(request.seed,int) or not 0<=request.seed<=2147483647):
            raise AppError(422,'wan_seed_invalid','万相种子须为 0–2147483647 的整数')
        try:
            dimensions=(int(request.parameters.get('width',0)),int(request.parameters.get('height',0)))
        except (TypeError,ValueError,OverflowError) as exc:
            raise AppError(422,'wan_resolution_invalid','万相输出宽高必须为有效整数') from exc
        sizes={(1280,720):'720P',(720,1280):'720P',(1920,1080):'1080P',(1080,1920):'1080P'}
        if dimensions not in sizes:
            raise AppError(422,'wan_resolution_invalid','请设置 1280×720、720×1280、1920×1080 或 1080×1920；服务会近似输出尺寸，实际规格须验证')
        ref=request.reference_assets[0]
        try:
            data=Path(ref['absolute_path']).read_bytes()
        except (KeyError,TypeError,OSError) as exc:
            raise AppError(409,'wan_frame_unreadable','已审核首帧无法读取；请重新检查素材') from exc
        if hashlib.sha256(data).hexdigest()!=ref['sha256']:
            raise AppError(409,'reference_hash_changed','已审核首帧文件哈希已变化')
        if len(data)>20*1024*1024:
            raise AppError(422,'wan_image_too_large','万相 2.6 首帧图片不能超过 20 MiB')
        try:
            with Image.open(BytesIO(data)) as img:
                img.verify()
            with Image.open(BytesIO(data)) as img:
                mime={'PNG':'image/png','JPEG':'image/jpeg','WEBP':'image/webp','BMP':'image/bmp'}.get(img.format)
                if not mime or 'A' in img.getbands() or 'transparency' in img.info or not all(240<=v<=8000 for v in img.size):
                    raise AppError(422,'wan_frame_invalid','首帧须为不透明 JPEG/PNG/WEBP/BMP，宽高均为 240–8000 像素')
                if abs(img.width/img.height-dimensions[0]/dimensions[1])>.025:
                    raise AppError(422,'wan_frame_aspect_mismatch','首帧宽高比与视频输出不一致；请使用匹配画幅的已审核首帧')
        except (UnidentifiedImageError,OSError,ValueError,Image.DecompressionBombError) as exc:
            raise AppError(422,'wan_frame_invalid','首帧图像无法解码') from exc
        body={'model':model,'input':{'img_url':'data:'+mime+';base64,'+base64.b64encode(data).decode(),
              'prompt':request.prompt,'negative_prompt':request.negative_prompt or ''},
              'parameters':{'resolution':sizes[dimensions], 'duration':int(duration), 'prompt_extend':False,
                            **({'seed':request.seed} if request.seed is not None else {})}}
        try:
            response=await service_request(self.settings,self.snapshot,'POST','/services/aigc/video-generation/video-synthesis',self.secret,
                                           json=body,extra_headers={'X-DashScope-Async':'enable'},client_timeout=120.0)
        except httpx.TransportError as exc:
            raise AppError(502,'cloud_submission_unknown','万相提交结果未知，未自动重试；请到供应商后台核对，避免重复计费') from exc
        if response.status_code==408 or response.status_code>=500:
            raise AppError(502,'cloud_submission_unknown',f'万相提交 HTTP {response.status_code}，受理结果未知；请到供应商后台核对，未自动重提')
        if response.status_code>=400:
            raise AppError(502,'cloud_video_submit_failed',f'万相视频提交 HTTP {response.status_code}；请检查权限、额度和模型配置')
        try:
            output=response.json().get('output')
        except (ValueError,TypeError,AttributeError) as exc:
            raise AppError(502,'cloud_submission_unknown','万相已响应但任务 ID 无法读取；请在供应商后台核对，未自动重提') from exc
        identifier=output.get('task_id') if isinstance(output,dict) else None
        if not isinstance(identifier,str) or not identifier.strip():
            raise AppError(502,'cloud_submission_unknown','万相已响应但缺少任务 ID；可能已经计费，未自动重提')
        return JobAccepted(identifier,'queued',metadata={'workflow':'wan26-i2v-first-frame','provider':self.name,'mode':'cloud'})

    async def get_job(self,identifier):
        response=await service_request(self.settings,self.snapshot,'GET','/tasks/'+quote(identifier,safe=''),self.secret,client_timeout=30.0)
        if response.status_code>=400:
            raise AppError(502,'cloud_video_status_unavailable',f'万相任务查询 HTTP {response.status_code}，保留任务 ID 并停止自动重复提交')
        try:
            output=response.json()['output']; state=output['task_status']
            if output.get('task_id') not in (None,identifier):raise ValueError('mismatched task id')
        except (ValueError,TypeError,KeyError) as exc:
            raise AppError(502,'cloud_video_status_unavailable','万相任务状态无法解析，保留任务 ID') from exc
        states={'PENDING':'queued','RUNNING':'running','SUCCEEDED':'generated','FAILED':'failed','CANCELED':'cancelled','UNKNOWN':'unknown'}
        if state not in states:
            raise AppError(502,'cloud_video_status_unavailable','万相返回未知任务状态，保留任务 ID')
        if state=='SUCCEEDED' and not isinstance(output.get('video_url'),str):
            raise AppError(502,'cloud_video_status_unavailable','万相成功状态缺少视频地址，保留任务 ID')
        return {'provider_job_id':identifier,'status':states[state],'output_url':output.get('video_url') if state=='SUCCEEDED' else None,
                'raw':{'task_id':identifier,'task_status':state},'error':'万相任务失败或状态未知，请核对供应商后台原任务' if state in {'FAILED','UNKNOWN'} else None}


def existing_provider(session,settings,snapshot):
    if snapshot['provider']=='comfyui':
        return ComfyUIH3Provider(local_settings(settings,snapshot))
    service=session.get(AIService,snapshot['service_id'])
    if not service or not service.enabled:
        raise AppError(409,'service_disabled','历史任务服务已停用；快照未改变，请恢复服务后核对原任务')
    contract=snapshot.get('settings',{}).get('video_contract')
    if contract=='lk888-h3-v2':
        from app.services.lk888_h3 import Lk888H3Video
        adapter=Lk888H3Video
    else:
        adapter=Wan26ImageToVideo if contract=='wan26-i2v' else CloudVideoJobs
    return adapter(settings,snapshot,reveal(service.encrypted_secret))
