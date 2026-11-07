from __future__ import annotations

import asyncio
import ipaddress
import json
import ssl
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select

from app.core.config import PROJECT_ROOT
from app.core.errors import AppError
from app.core.secrets import redact, reveal, storage_label
from app.models import AIService, ProjectServiceBinding
from app.models.entities import utcnow
from app.services.imports import pin_public_url
from app.core.trusted_service_origins import (confirmed_local_origin, is_trusted_local_service,
    is_local_provider, is_loopback_service, pin_local_service_url, service_origin)

MODULES = {'character_generation': {'comfyui','openai_compatible','cloud_draft'},
    'video_generation': {'comfyui','video_api','cloud_draft'}, 'final_assembly': {'ffmpeg'},
    'language_processing':{'openai_compatible','local_llm','cloud_draft'},
    'dialogue_generation':{'local_runtime','openai_tts','cloud_draft'},
    'lip_sync':{'local_runtime','cloud_draft'},'video_upscale':{'comfyui','local_runtime','cloud_draft'},
    'quality_check':{'ffmpeg','openai_compatible','cloud_draft'}}

BUILTIN_VIDEO_WORKFLOWS = (
    {'id': 'context_ir', 'label': 'Context-IR 三档：Fast 4 / Quality 20 / Quality 30', 'h3_context_ir': True},
    {'id': 'native_h3_r2v', 'label': '原生 H3 参考图工作流（20步）', 'h3_context_ir': False},
)


def normalize_service_settings(module, provider, base_url, options):
    options = dict(options)
    confirmed = confirmed_local_origin(base_url, options)
    if confirmed:
        options['local_service_origin'] = confirmed
    profile = options.get('workflow_profile')
    if profile == 'context_ir':
        options['h3_context_ir'] = True
        options.pop('workflow_path', None)
    elif profile == 'native_h3_r2v':
        options['h3_context_ir'] = False
        options['workflow_path'] = str(PROJECT_ROOT / 'workflows/h3_r2v_character_v1.json')
    return options


def validate_configuration(module, provider, base_url, options):
    if provider not in MODULES.get(module, set()):
        raise AppError(422, 'adapter_not_supported', '此业务模块尚未实现该服务适配器')
    if any(key.lower() in {'api_key', 'token', 'authorization', 'secret', 'password', 'headers'} for key in options):
        raise AppError(422, 'secret_in_settings', '密钥只能通过专用密码字段保存')
    allowed = {'timeout', 'concurrency', 'width', 'height', 'duration', 'candidate_model', 'text_encoder', 'vae', 'workflow_path', 'workflow_profile', 'h3_context_ir', 'local_service_origin', 'ffmpeg_path', 'ffprobe_path', 'fps', 'codec', 'audio_policy', 'image_api_format', 'video_contract','max_tokens','runtime_family','voice','response_format','max_prompt_characters'}
    if set(options) - allowed or any(isinstance(value, (dict, list)) for value in options.values()):
        raise AppError(422, 'invalid_service_settings', '仅接受当前适配器定义的简单参数，禁止在普通配置中保存认证信息')
    if options.get('local_service_origin'):
        if provider not in {'comfyui', 'local_llm'}:
            raise AppError(422, 'local_confirmation_not_supported', '仅本地 ComfyUI 和本地语言服务可确认本地地址')
        confirmed_local_origin(base_url, options)
    if options.get('workflow_profile') or 'h3_context_ir' in options:
        if provider != 'comfyui' or module != 'video_generation':
            raise AppError(422, 'workflow_profile_not_supported', '内置视频工作流仅用于 ComfyUI 视频服务')
        if options.get('workflow_profile') not in (None, '', 'context_ir', 'native_h3_r2v'):
            raise AppError(422, 'workflow_profile_invalid', '请选择已审核的内置 H3 工作流')
        if 'h3_context_ir' in options and type(options['h3_context_ir']) is not bool:
            raise AppError(422, 'workflow_profile_invalid', 'Context-IR 标志必须为布尔值')
    if provider == 'video_api' and options.get('video_contract') not in (None, '', 'video-jobs-v1', 'wan26-i2v', 'lk888-h3-v2'):
        raise AppError(422, 'video_contract_invalid', '请选择已实现的视频接口契约')
    prompt_limit = options.get('max_prompt_characters')
    if prompt_limit is not None:
        if provider != 'video_api' or options.get('video_contract') != 'video-jobs-v1':
            raise AppError(422, 'prompt_limit_not_configurable', '仅自定义云端视频契约可填写字数上限；专用适配器按接口限制，本地视频不设应用层字数上限')
        if type(prompt_limit) is not int or prompt_limit < 1:
            raise AppError(422, 'invalid_prompt_limit', '云端提示词字数上限必须为正整数或留空')
    if provider == 'video_api' and options.get('video_contract') == 'lk888-h3-v2':
        target = urlsplit(base_url)
        if target.scheme != 'https' or target.hostname != 'api.lk888.ai' or target.port not in (None, 443) or target.path.rstrip('/') not in ('', '/v1'):
            raise AppError(422, 'lk888_endpoint_invalid', '零度 H3 请选择 https://api.lk888.ai 或 https://api.lk888.ai/v1')
    if provider == 'video_api' and options.get('video_contract') == 'wan26-i2v' and urlsplit(base_url).path.rstrip('/') != '/api/v1':
        raise AppError(422, 'wan_endpoint_invalid', '万相 2.6 请使用供应商原生 /api/v1 地址，不要填写聊天接口 /compatible-mode/v1')
    if provider=='local_runtime':
        if base_url or set(options)-{'timeout','concurrency','runtime_family'}:raise AppError(422,'runtime_fixed_configuration','本地运行时只接受已登记family，不接受路径、命令或URL')
        if options.get('runtime_family') not in {'cosyvoice3','indextts25','latentsync15'}:raise AppError(422,'runtime_adapter_missing','此family没有固定CLI适配器')
        if options.get('runtime_family')=='indextts25' and module!='dialogue_generation':raise AppError(422,'runtime_module_mismatch','IndexTTS 2.5 仅用于角色配音模块')
    elif provider=='cloud_draft' and not base_url:
        pass  # An explicitly unavailable draft need not invent a live endpoint.
    elif provider != 'ffmpeg':
        url = urlsplit(base_url)
        if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise AppError(422, 'invalid_service_url', '服务地址必须是 HTTP(S)，禁止嵌入凭据、查询参数或片段')
        if provider not in ('comfyui','local_llm') and url.scheme != 'https':
            raise AppError(422, 'cloud_https_required', '云端兼容服务必须使用 HTTPS')
        trusted_local = is_trusted_local_service(base_url, options) if provider in {'comfyui', 'local_llm'} else False
        if provider == 'comfyui' and url.scheme == 'http' and url.hostname not in ('127.0.0.1', 'localhost', '::1') and not trusted_local:
            raise AppError(422, 'local_comfy_only', '请在服务设置中确认此宿主机或局域网本地地址，再保存 ComfyUI 配置')
        if provider=='local_llm' and url.hostname not in ('127.0.0.1','localhost','::1') and not trusted_local:raise AppError(422,'local_llm_loopback','请确认此本地语言服务地址；只允许环回或明确确认的宿主网关、私网及本机服务名称')
        if provider=='openai_tts' and (url.hostname!='api.openai.com' or url.path.rstrip('/')!='/v1'):raise AppError(422,'official_tts_endpoint','OpenAI语音适配器只使用官方https://api.openai.com/v1')
    elif base_url:
        raise AppError(422, 'ffmpeg_has_no_url', '本地 FFmpeg 不使用 base_url 或模型列表')
    try:
        raw_timeout = options.get('timeout', None)
        timeout = None if raw_timeout is None else int(raw_timeout)
        concurrency=int(options.get('concurrency',1))
        dimensions={field:int(options[field]) for field in ('width','height') if field in options}
    except (TypeError,ValueError,OverflowError) as exc:
        raise AppError(422,'invalid_numeric_settings','超时、并发和分辨率必须是有效数字') from exc
    if timeout is not None and timeout < 3:
        raise AppError(422, 'invalid_timeout', '请求超时留空表示无上限；设置数值时须至少为 3 秒')
    if not 1 <= concurrency <= (1 if provider in ('comfyui','ffmpeg','local_runtime','local_llm') else 4):
        raise AppError(422, 'invalid_concurrency', '本地 GPU/渲染默认串行；云端并发上限为 4')
    # Unknown options are never passed directly into shell commands or headers.
    for field in ('width', 'height'):
        if field in dimensions and (not 128 <= dimensions[field] <= 3840 or dimensions[field] % 8):
            raise AppError(422, 'invalid_dimensions', '宽高必须为 128–3840 的 8 倍数')


def public_service(service):
    return {'id': service.id, 'module': service.module, 'name': service.name, 'provider': service.provider,
            'base_url': service.base_url, 'enabled': service.enabled, 'is_default': service.is_default,
            'default_model': service.default_model, 'models': service.models_json, 'settings': service.settings_json,
            'capabilities': service.capabilities_json, 'last_test': service.last_test_json,
            'last_success_at': service.last_success_at.isoformat() if service.last_success_at else None,
            'has_api_key': bool(service.encrypted_secret), 'api_key_mask': '***' if service.encrypted_secret else '',
            'key_storage': storage_label(service.encrypted_secret),
            'trusted_local_endpoint': service.provider in {'comfyui', 'local_llm'} and (is_loopback_service(service.base_url) or is_trusted_local_service(service.base_url, service.settings_json)),
            'revision': service.revision, 'created_at': service.created_at.isoformat()}


def service_snapshot(service, model_id=None):
    return {'service_id': service.id, 'service_revision': service.revision, 'provider': service.provider,
            'name': service.name, 'module': service.module, 'base_url': service.base_url,
            'model_id': model_id or service.default_model, 'settings': dict(service.settings_json),
            'is_cloud': not is_local_provider(service.provider, service.base_url, service.settings_json),
            'captured_at': utcnow().isoformat()}


def resolve_service(session, module, service_id=None, project_id=None, model_id=None):
    if not service_id and project_id:
        binding = session.scalar(select(ProjectServiceBinding).where(ProjectServiceBinding.project_id == project_id, ProjectServiceBinding.module == module))
        if binding:
            service_id, model_id = binding.service_id, model_id or binding.model_id
    service = session.get(AIService, service_id) if service_id else session.scalar(select(AIService).where(AIService.module == module, AIService.is_default.is_(True)))
    if not service or not service.enabled or service.module != module:
        raise AppError(409, 'service_not_configured', '请为当前业务选择并启用一个有效服务配置')
    return service, service_snapshot(service, model_id)


def seed_local_services(session, settings):
    from app.models import DeletionTask
    deleted_modules = {r['values'].get('module') for task in session.scalars(select(DeletionTask)) if task.status not in ('planned','database_failed','restored') for r in task.plan_json.get('records',[]) if r['table']=='ai_services'}
    for module, name, provider, model, options in [
        ('character_generation', '本机人物素材 · ComfyUI', 'comfyui', 'flux-2-klein-4b-fp8.safetensors', {'timeout': None, 'concurrency': 1, 'width': 832, 'height': 1024, 'candidate_model': 'flux-2-klein-4b-fp8.safetensors', 'text_encoder': 'qwen_3_4b.safetensors', 'vae': 'flux2-vae.safetensors'}),
        ('video_generation', '本机 MiniMax H3 R2V', 'comfyui', 'minimax_h3_ref2va_pruned_w6a8.safetensors', {'timeout': None, 'concurrency': 1, 'width': 640, 'height': 384, 'duration': 5, 'workflow_path': str(settings.h3_workflow_api_json or PROJECT_ROOT / 'workflows/h3_r2v_character_v1.json')}),
        ('final_assembly', '本机 FFmpeg', 'ffmpeg', '', {'timeout': None, 'concurrency': 1, 'ffmpeg_path': settings.ffmpeg_path, 'ffprobe_path': settings.ffprobe_path, 'width': 640, 'height': 384, 'fps': 24, 'codec': 'libx264', 'audio_policy': 'preserve'})]:
        if provider == 'comfyui' and not settings.seed_local_model_services:
            continue
        if module not in deleted_modules and session.scalar(select(AIService.id).where(AIService.module == module)) is None:
            session.add(AIService(module=module, name=name, provider=provider, base_url='' if provider == 'ffmpeg' else settings.comfyui_base_url, is_default=True, default_model=model, settings_json=options))
    session.commit()


async def service_request(settings, snapshot, method, endpoint, secret='', *, client_timeout=None, extra_headers=None, **kwargs):
    base = snapshot['base_url'].rstrip('/')
    url = base + endpoint
    parsed = urlsplit(url)
    headers = {'Authorization': 'Bearer ' + secret} if secret else {}
    # Only the official asynchronous video-task switch is supported. Never
    # allow caller-controlled authorization, host, or other arbitrary headers.
    if extra_headers is not None:
        if extra_headers != {'X-DashScope-Async': 'enable'}:
            raise AppError(422, 'unsupported_service_headers', '不支持此服务请求头')
        headers.update(extra_headers)
    extensions = {}
    trusted_local = snapshot.get('provider') in {'comfyui', 'local_llm'} and (is_loopback_service(base) or is_trusted_local_service(base, snapshot.get('settings')))
    if trusted_local:
        if service_origin(url) != service_origin(base):
            raise AppError(422, 'service_origin_changed', '本地服务请求不能跳转到其他地址')
        url, sni, host = await asyncio.to_thread(pin_local_service_url, url)
        headers['Host'] = host
        extensions['sni_hostname'] = sni
    else:
        restricted = replace(settings, allow_external_doc_import=True, doc_import_allowed_hosts=(parsed.hostname,))
        target, sni, host = await asyncio.to_thread(pin_public_url, url, restricted)
        headers['Host'] = host
        extensions['sni_hostname'] = sni
        url = target
    configured_timeout = snapshot.get('settings', {}).get('timeout')
    timeout = None if configured_timeout is None else float(configured_timeout)
    if client_timeout is not None:
        timeout = client_timeout
    async with httpx.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=False) as client:
        response = await client.request(method, url, headers=headers, extensions=extensions, **kwargs)
        if 300 <= getattr(response, 'status_code', 200) < 400:
            raise AppError(422, 'service_redirect_refused', '模型服务返回了重定向；请直接配置最终服务地址，系统不会转发密钥到其他地址')
        return response


async def download_public_video(settings, url, *, max_bytes=500*1024*1024):
    """Stream a signed public HTTPS video; never forward provider credentials."""
    try:
        parsed = urlsplit(url) if isinstance(url, str) else None
        if (not parsed or parsed.scheme != 'https' or not parsed.hostname or
                parsed.username is not None or parsed.password is not None or parsed.fragment or '#' in url):
            raise ValueError('invalid video URL')
        _ = parsed.port
    except (ValueError, TypeError) as error:
        raise AppError(422, 'unsafe_video_url', '云端输出必须是可公开访问的 HTTPS 视频地址') from error
    if not isinstance(max_bytes,int) or max_bytes <= 0:
        raise ValueError('max_bytes must be a positive integer')
    restricted = replace(settings, allow_external_doc_import=True, doc_import_allowed_hosts=(parsed.hostname,))
    target, sni, host = await asyncio.to_thread(pin_public_url, url, restricted)
    content=bytearray()
    try:
        async with asyncio.timeout(300):
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0), trust_env=False, follow_redirects=False) as client:
                async with client.stream('GET', target, headers={'Host':host,'Accept-Encoding':'identity'},
                                         extensions={'sni_hostname':sni}) as response:
                    response.raise_for_status()
                    if response.status_code != 200:
                        raise AppError(502, 'video_download_incomplete', '云端视频下载响应不是完整文件')
                    encoding=response.headers.get('content-encoding','').strip().lower()
                    if encoding not in ('','identity'):
                        raise AppError(422, 'video_download_encoding', '云端视频使用不支持的压缩传输编码')
                    length=response.headers.get('content-length')
                    if length:
                        try:
                            declared=int(length)
                        except ValueError as error:
                            raise AppError(502,'video_download_invalid_length','云端视频长度响应无效') from error
                        if declared<0:raise AppError(502,'video_download_invalid_length','云端视频长度响应无效')
                        if declared>max_bytes:raise AppError(422,'video_too_large','云端视频超过 500 MiB 本地导入上限')
                    async for chunk in response.aiter_raw(chunk_size=64*1024):
                        if len(content)+len(chunk)>max_bytes:
                            raise AppError(422,'video_too_large','云端视频超过 500 MiB 本地导入上限')
                        content.extend(chunk)
    except TimeoutError as error:
        raise AppError(504,'video_download_timeout','云端视频下载超过总时限；可保留任务 ID 并稍后核对') from error
    return content


def ffmpeg_probe(options):
    executable = str(options.get('ffmpeg_path', ''))
    if Path(executable).name.lower() not in ('ffmpeg', 'ffmpeg.exe'):
        raise AppError(422, 'invalid_ffmpeg', '请选择 FFmpeg 可执行文件')
    result = {}
    for name, args in [('version', ['-version']), ('encoders', ['-hide_banner', '-encoders']), ('transitions', ['-hide_banner', '-h', 'filter=xfade'])]:
        output = subprocess.run([executable, *args], capture_output=True, encoding='utf-8', errors='replace', timeout=15, shell=False)
        if output.returncode:
            raise AppError(422, 'ffmpeg_probe_failed', 'FFmpeg 检测失败')
        result[name] = output.stdout
    return result


def _exception_contains(error, kind):
    """Inspect the exception chain without exposing URLs, headers or secrets."""
    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, kind):
            return True
        pending.extend(value for value in (current.__cause__, current.__context__) if value is not None)
        pending.extend(value for value in current.args if isinstance(value, BaseException))
    return False


def _probe_failure(error):
    """Give actionable, deliberately bounded diagnostics; never return provider bodies."""
    if isinstance(error, AppError):
        return error.code, error.message
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        if status == 401:
            return 'authentication_failed', '服务返回 HTTP 401：认证未通过，请检查 API Key 和服务地址；未执行生成。'
        if status == 403:
            return 'permission_denied', '服务返回 HTTP 403：账号、密钥权限或服务地区可能受限，请核对供应商权限；未执行生成。'
        if status == 429:
            return 'rate_limited', '服务返回 HTTP 429：请求频率或额度受限，请检查供应商配额后重试；未执行生成。'
        if status >= 500:
            return 'provider_error', f'服务返回 HTTP {status}：供应商暂时出错，请稍后重试；未执行生成。'
        if 300 <= status < 400:
            return 'redirect_not_followed', f'服务返回 HTTP {status} 重定向；请填写供应商实际 API 地址，连接测试不会自动转发密钥。'
        return 'http_error', f'服务返回 HTTP {status}：请核对 API 地址和接口要求；未执行生成。'
    if isinstance(error, (httpx.TimeoutException, subprocess.TimeoutExpired)):
        return 'timeout', '连接测试超时，请检查服务地址、网络和服务状态；未执行生成。'
    if _exception_contains(error, ssl.SSLError):
        return 'tls_error', 'TLS 安全连接失败，请检查证书、系统时间和服务地址；未执行生成。'
    if isinstance(error, httpx.ConnectError):
        return 'connection_failed', '无法连接服务，请检查地址、网络及服务是否可访问；未执行生成。'
    if isinstance(error, httpx.HTTPError):
        return 'network_error', '网络请求失败，请检查连接和服务状态；未执行生成。'
    if isinstance(error, ValueError):
        return 'invalid_response', '服务返回了无法识别的数据格式；请检查该地址是否为兼容的 API 入口。'
    return 'probe_error', '连接检查未完成，请检查服务配置及运行环境；未执行生成。'


async def probe_service(service, settings):
    started = time.perf_counter()
    snapshot = service_snapshot(service)
    secret = reveal(service.encrypted_secret)
    result = {'status': 'failed', 'http_status': None, 'capabilities': {}, 'models': [], 'checked_at': utcnow().isoformat()}
    try:
        if service.provider=='cloud_draft':
            result.update(status='unsupported',error='此云端档案没有执行适配器；未发请求，不能选择为已验证服务')
        elif service.provider=='local_runtime':
            from app.services.runtime_registry import manifest_status
            runtime=manifest_status(service.settings_json.get('runtime_family',''),settings)
            result.update(status='connected' if runtime['state']=='verified' else 'unavailable',runtime=runtime,
                capabilities={slot:{'state':runtime['state'],'model_id':runtime.get('manifest',{}).get('model_id'),'source':'fixed deployment manifest and actual decoded smoke output'} for slot in runtime.get('manifest',{}).get('task_capabilities',[])})
        elif service.provider == 'ffmpeg':
            details = await asyncio.to_thread(ffmpeg_probe, service.settings_json)
            result.update(status='connected', version=details['version'].splitlines()[0], source='actual local ffmpeg -version/-encoders/filter=xfade', capabilities={name: {'state': 'detected', 'source': 'FFmpeg output'} for name, exists in [('h264', 'libx264' in details['encoders']), ('aac', ' AAC ' in details['encoders'] or 'aac ' in details['encoders']), ('transitions', 'slideleft' in details['transitions'])] if exists})
        elif service.provider == 'comfyui':
            response = await service_request(settings, snapshot, 'GET', '/object_info', secret, client_timeout=10.0)
            result['http_status'] = response.status_code
            response.raise_for_status()
            nodes = response.json()
            if not isinstance(nodes, dict):
                raise ValueError('ComfyUI object_info must be an object')
            result.update(status='reachable', reachable=True)
            def choices(node, field):
                value = nodes.get(node, {}).get('input', {}).get('required', {}).get(field, [[]])[0]
                return value if isinstance(value, list) else []
            checkpoint = choices('CheckpointLoaderSimple', 'ckpt_name')
            diffusion = choices('UNETLoader', 'unet_name')
            models = checkpoint + diffusion if service.module == 'character_generation' else diffusion
            capabilities = {}
            required_edit = ('ReferenceLatent', 'Flux2Scheduler', 'VAELoader', 'CLIPLoader', 'UNETLoader', 'LoadImage')
            if service.module == 'character_generation':
                candidate=service.settings_json.get('candidate_model') or service.default_model
                klein_nodes=('UNETLoader','CLIPLoader','VAELoader','CLIPTextEncode','ConditioningZeroOut','CFGGuider','KSamplerSelect','RandomNoise','EmptyFlux2LatentImage','Flux2Scheduler','SamplerCustomAdvanced','VAEDecode','SaveImage')
                if candidate=='flux-2-klein-4b-fp8.safetensors' and candidate in diffusion and all(name in nodes for name in klein_nodes) and service.settings_json.get('text_encoder') in choices('CLIPLoader','clip_name') and service.settings_json.get('vae') in choices('VAELoader','vae_name'):
                    capabilities['text_to_image']={'state':'detected','source':'native Klein text-to-image nodes + installed selected models','model_id':candidate}
                elif candidate in checkpoint and 'KSampler' in nodes:
                    capabilities['text_to_image'] = {'state': 'detected', 'source': 'object_info SDXL loader/sampler'}
                if all(name in nodes for name in required_edit) and service.default_model in diffusion and service.settings_json.get('text_encoder') in choices('CLIPLoader', 'clip_name') and service.settings_json.get('vae') in choices('VAELoader', 'vae_name'):
                    capabilities['reference_image'] = {'state': 'detected', 'source': 'native ReferenceLatent + installed selected models'}
            elif service.module == 'video_generation' and service.settings_json.get('h3_context_ir') is True:
                from app.services.h3_context_provider import require_context_qwen
                class RuntimeProbeClient:
                    async def get(self, url, **_kwargs):
                        if service_origin(url) != service_origin(snapshot['base_url']):
                            raise AppError(422, 'service_origin_changed', '运行时探测不能连接其他地址')
                        reply = await service_request(settings, snapshot, 'GET', urlsplit(url).path, secret, client_timeout=10.0)
                        if reply.status_code == 200:
                            receipt = reply.json()
                            if isinstance(receipt, dict):
                                result['runtime'] = receipt
                        return reply
                await require_context_qwen(RuntimeProbeClient(), snapshot['base_url'].rstrip('/'), runtime_required=True)
                capabilities['reference_to_video'] = {'state': 'detected', 'source': 'Context-IR native runtime identity, node/model checks and local Qwen vision/32K readiness; inference remains unverified'}
                capabilities['reference_video'] = {'state': 'detected', 'source': 'Context-IR native runtime readiness; inference remains unverified'}
                result['notice'] = '本地 Context-IR 运行时与 Qwen 就绪；尚未执行本次真实视频生成。'
            elif service.module == 'video_generation':
                try:
                    graph = json.loads(Path(service.settings_json.get('workflow_path') or settings.h3_workflow_api_json).read_text(encoding='utf-8'))
                    prompt, mapping = graph['prompt'], graph['_video_factory']
                    valid = mapping.get('mode') == 'reference_to_video' and bool(mapping.get('reference_paths')) and any(node['class_type']=='MiniMaxH3ReferenceToVideo' for node in prompt.values())
                    selected_models = []
                    for node in prompt.values():
                        valid = valid and node['class_type'] in nodes
                        for key in ('unet_name','clip_name','vae_name'):
                            if key in node['inputs']:
                                valid = valid and node['inputs'][key] in choices(node['class_type'],key)
                                if key=='unet_name': selected_models.append(node['inputs'][key])
                    if valid and service.default_model in selected_models:
                        capabilities['reference_to_video'] = {'state':'detected','source':'configured native H3 graph; all nodes and exact loader models detected'}
                        capabilities['revision'] = {'state':'detected','source':'same-reference regeneration; no pixel-local editing'}
                    else:
                        result['workflow_error'] = '选定模型与 R2V 工作流节点/权重未全部匹配，能力保持未验证'
                except (OSError,ValueError,KeyError,TypeError):
                    result['workflow_error'] = '无法验证实际 R2V 工作流文件'
            result.update(status='connected', models=[{'id': model, 'source': 'ComfyUI object_info loader choices'} for model in dict.fromkeys(models)], capabilities=capabilities, source='GET /object_info; node/file detection is distinct from actual inference')
            result['reachable'] = True
            if result.get('workflow_error'):
                result.update(status='reachable', error=result['workflow_error'] + '；请核对所选内置工作流、安装对应节点与权重后重试', diagnostic_code='comfy_workflow_not_ready', generation_ready=False)
            elif service.module == 'character_generation' and not capabilities:
                result.update(status='reachable', error='ComfyUI 可以连接，但所选图像模型、编码器、VAE或必要节点不齐；请按已安装权重填写模型后重试', diagnostic_code='comfy_image_not_ready', generation_ready=False)
        else:
            probe_snapshot = snapshot
            if service.provider == 'video_api' and service.settings_json.get('video_contract') == 'wan26-i2v':
                base = snapshot['base_url'].rstrip('/')
                if not base.endswith('/api/v1'):
                    raise AppError(422, 'wan_endpoint_invalid', '万相 2.6 请使用供应商原生 /api/v1 地址')
                probe_snapshot = {**snapshot, 'base_url': base[:-len('/api/v1')] + '/compatible-mode/v1'}
                result['model_list_source'] = 'same provider origin, OpenAI-compatible /models; not a video task test'
            response = await service_request(settings, probe_snapshot, 'GET', '/models', secret, client_timeout=10.0)
            result['http_status'] = response.status_code
            if service.provider == 'video_api':
                contract = service.settings_json.get('video_contract', '')
                result['video_contract'] = {'configured': contract or None, 'state': 'unverified',
                    'source': '模型列表检查不验证 /video/jobs 协议或视频生成能力'}
            if response.status_code in (404, 405):
                result.update(status='reachable', error='服务器有响应，但此地址未提供 /models；请核对 API 地址并手动填写模型 ID。认证和生成能力均未验证。', model_list='not_available', diagnostic_code='model_list_unavailable')
            else:
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or not isinstance(payload.get('data'), list):
                    raise ValueError('model-list response format is not supported')
                result.update(status='connected', models=[{'id': str(row['id']), 'source': 'provider GET /models'} for row in payload.get('data', []) if isinstance(row, dict) and 'id' in row], capabilities={key: {'state': 'unverified', 'source': 'listing models does not prove generation support'} for key in ('text_to_image', 'reference_image') if service.module == 'character_generation'})
                result['notice'] = ('模型列表请求成功；该检查不证明视频任务协议或视频生成可用。' if service.provider == 'video_api'
                                    else '模型列表请求成功；该检查不证明具体生成能力可用。')
                if service.provider == 'video_api' and service.settings_json.get('video_contract') == 'wan26-i2v':
                    if service.default_model and service.default_model not in {entry['id'] for entry in result['models']}:
                        result['notice'] = '认证和模型列表请求成功，但列表未列出所选万相模型；请在供应商控制台确认模型权限，生成能力仍未验证。'
    except (httpx.HTTPError, ValueError, OSError, subprocess.TimeoutExpired, AppError) as exc:
        result['diagnostic_code'], result['error'] = _probe_failure(exc)
        if service.provider == 'comfyui' and isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 403:
            result.update(diagnostic_code='local_client_rejected',
                          error='本地节点拒绝了当前客户端（HTTP 403）。请在原生节点日志核对来源 IP，配置 allowed_clients 和防火墙；不要填写云端 API 密钥。')
    result['elapsed_ms'] = round((time.perf_counter() - started) * 1000, 1)
    return redact(result, secret)
