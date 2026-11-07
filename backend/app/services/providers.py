from __future__ import annotations

import copy
import asyncio
import json
import hashlib
import math
import re
import shutil
import uuid
import weakref
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import httpx

from app.core.config import PROJECT_ROOT, Settings
from app.core.errors import AppError
from app.core.trusted_service_origins import local_service_client
from app.services.gpu_gate import gpu_submission

_motion_preprocess_locks=weakref.WeakKeyDictionary()


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def bind_ordered_reference_slots(prompt,metadata,references):
    """Replace stale template slots with the exact supplied, ordered image set."""
    targets=[key for key,node in prompt.items() if node['class_type']=='MiniMaxH3ReferenceToVideo']
    if len(targets)!=1:raise AppError(422,'workflow_reference_target_ambiguous','R2V工作流必须只有一个明确参考目标节点')
    if not 1<=len(references)<=9:raise AppError(422,'workflow_reference_limit','H3需要1至9张明确参考，未截断输入')
    paths=metadata.get('reference_paths') or []
    if not paths or len(paths[0])!=3 or paths[0][1:]!=['inputs','image'] or prompt.get(paths[0][0],{}).get('class_type')!='LoadImage':
        raise AppError(422,'workflow_reference_mapping_invalid','工作流需要明确LoadImage映射，未猜测自定义参考节点')
    target=prompt[targets[0]]['inputs']
    for key in list(target):
        if key.startswith('ref_images.'):del target[key]
    result=[]
    for index,_ in enumerate(references):
        node_id=paths[0][0] if index==0 else 'factory_ref_'+str(index)
        if index:prompt[node_id]={'class_type':'LoadImage','inputs':{'image':''}}
        target['ref_images.ref_image_'+str(index)]=[node_id,0]
        result.append([node_id,'inputs','image'])
    metadata['reference_paths']=result
    metadata['reference_tags']=[f'<Picture {i}>' for i in range(1,len(references)+1)]
    return result


def _workflow_value(prompt, path):
    if not isinstance(path, list) or not path or not all(isinstance(part, str) for part in path):
        raise AppError(503, 'h3_sampling_contract_invalid', '采样契约字段路径无效')
    current = prompt
    for part in path:
        if not isinstance(current, dict) or part not in current:
            raise AppError(503, 'h3_sampling_contract_invalid', '采样契约字段不存在', {'path': path})
        current = current[part]
    return current


def _sampling_ancestors(prompt, node_id, *, skip_conditioning=False):
    """Follow actual API links; mere presence of an unused LoRA is insufficient."""
    visited, visiting = set(), set()
    def visit(identifier):
        if identifier in visiting:
            raise AppError(503, 'h3_sampling_contract_invalid', '固定采样图存在循环连接')
        if identifier in visited:
            return
        node = prompt.get(identifier)
        if not isinstance(node, dict) or not isinstance(node.get('inputs'), dict):
            raise AppError(503, 'h3_sampling_contract_invalid', '固定采样图引用了缺失节点')
        visiting.add(identifier)
        for key, value in node['inputs'].items():
            if skip_conditioning and node['class_type'] == 'BasicGuider' and key == 'conditioning':
                continue
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and type(value[1]) is int:
                if value[1] < 0:
                    raise AppError(503, 'h3_sampling_contract_invalid', '固定采样图输出索引无效')
                visit(value[0])
        visiting.remove(identifier)
        visited.add(identifier)
    visit(node_id)
    return visited


def validate_sampling_contract(prompt, metadata):
    """Validate a pinned sampling recipe, not model compatibility or its quality.

    sampling_contract v1 contains steps, sampler_node and locked_inputs entries
    {path: [node_id, 'inputs', field], value: expected_json_value}. Every input
    in the sampler/guider/model/sigma chain must be pinned, except the dynamic
    conditioning, noise and latent inputs. Old workflows remain configurable.
    """
    if 'sampling_contract' not in metadata:
        return None
    contract = metadata['sampling_contract']
    allowed = {'version', 'steps', 'sampler_node', 'locked_inputs'}
    if (not isinstance(contract, dict) or set(contract) != allowed or
            type(contract['version']) is not int or contract['version'] != 1 or
            type(contract['steps']) is not int or not 1 <= contract['steps'] <= 50 or
            not isinstance(contract['sampler_node'], str)):
        raise AppError(503, 'h3_sampling_contract_invalid', '不支持的固定采样契约或版本')
    sampler_id = contract['sampler_node']
    sampler = prompt.get(sampler_id, {})
    if sampler.get('class_type') != 'SamplerCustomAdvanced' or not isinstance(sampler.get('inputs'), dict):
        raise AppError(503, 'h3_sampling_contract_invalid', '固定采样契约必须指向实际 SamplerCustomAdvanced')
    output_path = metadata.get('output_prefix_path')
    _workflow_value(prompt, output_path)
    if sampler_id not in _sampling_ancestors(prompt, output_path[0]):
        raise AppError(503, 'h3_sampling_contract_invalid', '固定采样器未连接到实际保存输出')
    critical = {sampler_id}
    for key in ('sigmas', 'sampler', 'guider'):
        link = sampler['inputs'].get(key)
        if not (isinstance(link, list) and len(link) == 2 and isinstance(link[0], str) and type(link[1]) is int and link[1] >= 0):
            raise AppError(503, 'h3_sampling_contract_invalid', '固定采样器缺少有效 ' + key + ' 连接')
        critical.update(_sampling_ancestors(prompt, link[0], skip_conditioning=True))
    entries = contract['locked_inputs']
    if not isinstance(entries, list) or not 1 <= len(entries) <= 512:
        raise AppError(503, 'h3_sampling_contract_invalid', '固定采样契约缺少锁定输入')
    locked = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {'path', 'value'}:
            raise AppError(503, 'h3_sampling_contract_invalid', '固定采样契约锁定项无效')
        path = entry['path']
        actual = _workflow_value(prompt, path)
        if len(path) != 3 or path[1] != 'inputs' or path[0] not in critical or tuple(path) in locked:
            raise AppError(503, 'h3_sampling_contract_invalid', '固定采样契约含无效、重复或未执行的锁定节点')
        if json.dumps(actual, sort_keys=True) != json.dumps(entry['value'], sort_keys=True):
            raise AppError(409, 'h3_sampling_contract_changed', '采样器、LoRA 或 Sigma 配置已偏离固定契约，未提交生成', {'path': path})
        locked.add(tuple(path))
    for identifier in critical:
        for key in prompt[identifier]['inputs']:
            if identifier == sampler_id and key not in ('sigmas', 'sampler', 'guider'):
                continue
            if prompt[identifier]['class_type'] == 'BasicGuider' and key == 'conditioning':
                continue
            if (identifier, 'inputs', key) not in locked:
                raise AppError(503, 'h3_sampling_contract_invalid', '固定采样契约未锁定完整执行链', {'node': identifier, 'input': key})
    sigma_nodes = _sampling_ancestors(prompt, sampler['inputs']['sigmas'][0], skip_conditioning=True)
    sources = [prompt[node] for node in sigma_nodes if prompt[node]['class_type'] in ('ManualSigmas', 'BasicScheduler')]
    if len(sources) != 1:
        raise AppError(503, 'h3_sampling_contract_invalid', '无法明确核验固定 Sigma 的实际步数')
    source = sources[0]
    if source['class_type'] == 'ManualSigmas':
        try:
            values = [float(value) for value in re.split(r'[\s,]+', source['inputs']['sigmas'].strip()) if value]
        except (KeyError, TypeError, AttributeError, ValueError) as exc:
            raise AppError(503, 'h3_sampling_contract_invalid', 'ManualSigmas 必须是完整数字序列') from exc
        if (len(values) != contract['steps'] + 1 or not all(math.isfinite(value) and value >= 0 for value in values) or
                values[-1] != 0 or not all(left > right for left, right in zip(values, values[1:]))):
            raise AppError(503, 'h3_sampling_contract_invalid', 'ManualSigmas 数量、顺序或终点与固定步数不符')
    elif type(source['inputs'].get('steps')) is not int or source['inputs']['steps'] != contract['steps']:
        raise AppError(503, 'h3_sampling_contract_invalid', 'Scheduler 实际步数与固定契约不符')
    return contract['steps']


def apply_sampling_steps(prompt, metadata, parameters):
    fixed = validate_sampling_contract(prompt, metadata)
    path = metadata.get('steps_path')
    if fixed is None and not path:
        return
    value = parameters.get('steps')
    if value is None:
        value = fixed if fixed is not None else _workflow_value(prompt, path)
    try:
        steps = int(value)
        if isinstance(value, bool) or (isinstance(value, float) and value != steps) or not 1 <= steps <= 50:
            raise ValueError
    except (ValueError, TypeError, OverflowError) as exc:
        raise AppError(422, 'invalid_steps', '采样步数需为1..50的整数') from exc
    if fixed is not None:
        if steps != fixed:
            raise AppError(409, 'h3_sampling_steps_locked', f'此工作流固定为{fixed}步；画质预设不能覆盖蒸馏采样契约')
        metadata['sampling_receipt'] = {'policy': 'workflow_locked', 'steps': fixed, 'contract_version': 1}
        return
    ComfyUIH3Provider._set_path(prompt, path, steps)
    metadata['sampling_receipt'] = {'policy': 'configurable', 'steps': steps, 'source': 'request' if parameters.get('steps') is not None else 'workflow'}


@dataclass(slots=True)
class ProviderHealth:
    provider: str
    connected: bool
    ready: bool
    reference_to_video_available: bool
    mode: str
    base_url: str | None = None
    version: str | None = None
    workflow_name: str | None = None
    checked_at: str = field(default_factory=_utc_iso)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class VideoGenerationRequest:
    job_id: str
    project_id: str
    shot_id: str
    prompt: str
    negative_prompt: str
    duration_seconds: float
    aspect_ratio: str
    seed: int | None
    parameters: dict[str, Any]
    reference_assets: list[dict[str, Any]]
    output_dir: Path
    instruction: str | None = None
    motion_references: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class JobAccepted:
    provider_job_id: str
    status: str
    output_path: Path | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class VideoGenerationProvider(Protocol):
    async def health(self) -> ProviderHealth: ...
    async def submit(self, request: VideoGenerationRequest) -> JobAccepted: ...
    async def get_job(self, job_id: str) -> dict[str, Any]: ...
    async def cancel(self, job_id: str) -> bool: ...


class DemoVideoProvider:
    """Deterministic, visibly watermarked video provider; never represents output as H3."""

    name = "demo"

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            provider=self.name,
            connected=True,
            ready=True,
            reference_to_video_available=False,
            mode="demo_watermarked_video",
            workflow_name="demo-watermarked-video-v1",
        )

    async def submit(self, request: VideoGenerationRequest) -> JobAccepted:
        request.output_dir.mkdir(parents=True, exist_ok=True)
        fixture = PROJECT_ROOT / "fixtures" / "demo_status_replay.mp4"
        if not fixture.exists() or fixture.stat().st_size == 0:
            raise AppError(
                503,
                "demo_fixture_missing",
                "Demo 视频素材缺失；不会用空文件或假成功替代",
            )
        output_path = request.output_dir / "DEMO_STATUS_REPLAY_NOT_REAL_H3.mp4"
        shutil.copyfile(fixture, output_path)
        return JobAccepted(
            provider_job_id=f"demo-{uuid.uuid4().hex}",
            status="succeeded",
            output_path=output_path,
            metadata={
                "media_kind": "demo_video",
                "playable_video": True,
                "watermark_burned_in": True,
                "disclaimer": "模拟流程通过；真实模型未验证",
            },
        )

    async def get_job(self, job_id: str) -> dict[str, Any]:
        return {"provider_job_id": job_id, "status": "succeeded", "progress": 1.0}

    async def cancel(self, _job_id: str) -> bool:
        return False


class ComfyUIH3Provider:
    name = "comfyui_h3"

    def __init__(self, settings: Settings):
        self.settings = settings

    def _load_workflow(self) -> tuple[dict[str, Any], dict[str, Any]]:
        path = self.settings.h3_workflow_api_json
        if not path:
            raise AppError(503, "h3_workflow_not_configured", "尚未配置 H3 API 工作流 JSON")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise AppError(503, "h3_workflow_not_found", "H3 API 工作流文件不存在") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise AppError(503, "h3_workflow_invalid", "H3 API 工作流文件无法读取") from exc
        metadata = payload.get("_video_factory")
        prompt = payload.get("prompt")
        if not isinstance(metadata, dict) or not isinstance(prompt, dict):
            raise AppError(
                503,
                "h3_workflow_unverified",
                "工作流必须包含 _video_factory 映射和 ComfyUI API prompt 图；不会猜测节点输入",
            )
        prompt_path = metadata.get("prompt_path")
        if not isinstance(prompt_path, list) or not prompt_path:
            raise AppError(503, "h3_workflow_unverified", "工作流缺少 prompt_path 映射")
        if not all(isinstance(node, dict) and "class_type" in node for node in prompt.values()):
            raise AppError(503, "h3_workflow_invalid", "prompt 不是 ComfyUI API 格式节点图")
        validate_sampling_contract(prompt, metadata)
        return copy.deepcopy(prompt), metadata

    @staticmethod
    def _set_path(document: dict[str, Any], path: list[str], value: Any) -> None:
        current: Any = document
        for part in path[:-1]:
            if not isinstance(current, dict) or part not in current:
                raise AppError(503, "h3_workflow_mapping_invalid", "工作流字段映射不存在")
            current = current[part]
        if not isinstance(current, dict):
            raise AppError(503, "h3_workflow_mapping_invalid", "工作流字段映射目标无效")
        current[path[-1]] = value

    async def health(self) -> ProviderHealth:
        workflow_name = None
        workflow_ready = False
        workflow_error = None
        try:
            _prompt, metadata = self._load_workflow()
            workflow_name = str(metadata.get("name") or "configured-h3-workflow")
            workflow_ready = bool(metadata.get("reference_paths")) and metadata.get("mode") == "reference_to_video"
            if not workflow_ready:
                workflow_error = "工作流缺少真实 R2V 参考图映射"
        except AppError as exc:
            workflow_error = exc.message
        try:
            timeout = 5.0  # Keep health probes bounded; generation requests use the service profile timeout.
            async with local_service_client([self.settings.comfyui_base_url], timeout=timeout) as client:
                stats_response, nodes_response = await client.get(
                    f"{self.settings.comfyui_base_url}/system_stats"
                ), await client.get(f"{self.settings.comfyui_base_url}/object_info")
                stats_response.raise_for_status()
                nodes_response.raise_for_status()
                stats = stats_response.json()
                nodes = nodes_response.json()
            h3_nodes = "MiniMaxH3ReferenceToVideo" in nodes
            issues = []
            if workflow_ready:
                for node in _prompt.values():
                    definition = nodes.get(node["class_type"])
                    if not definition:
                        issues.append(f"节点未注册: {node['class_type']}")
                        continue
                    fields = definition.get("input", {}).get("required", {})
                    for field in ("unet_name", "clip_name", "vae_name"):
                        if field in node["inputs"] and field in fields:
                            options = fields[field][0]
                            if isinstance(options, list) and node["inputs"][field] not in options:
                                issues.append(f"模型未安装/未识别: {node['inputs'][field]}")
            version = None
            if isinstance(stats, dict):
                system = stats.get("system") or {}
                version = str(system.get("comfyui_version") or system.get("version") or "") or None
            return ProviderHealth(
                provider=self.name,
                connected=True,
                ready=False,
                reference_to_video_available=workflow_ready and h3_nodes and not issues,
                mode="real",
                base_url=self.settings.comfyui_base_url,
                version=version,
                workflow_name=workflow_name,
                error=(
                    workflow_error
                    if not workflow_ready
                    else "未在 object_info 中确认原生 MiniMaxH3ReferenceToVideo 节点"
                    if not h3_nodes
                    else "；".join(issues) if issues
                    else "节点与权重文件可识别；模型加载和输出质量以实际任务结果为准"
                ),
            )
        except (httpx.HTTPError, ValueError) as exc:
            return ProviderHealth(
                provider=self.name,
                connected=False,
                ready=False,
                reference_to_video_available=False,
                mode="real",
                base_url=self.settings.comfyui_base_url,
                workflow_name=workflow_name,
                error=f"ComfyUI 不可用: {type(exc).__name__}",
            )

    async def submit(self, request: VideoGenerationRequest) -> JobAccepted:
        prepared=[]
        if request.motion_references:
            if len(request.motion_references)>3:raise AppError(422,'motion_reference_limit','动作参考最多3条')
            from app.services.motion_references import prepare_motion_reference
            loop=asyncio.get_running_loop()
            lock=_motion_preprocess_locks.setdefault(loop,asyncio.Lock())
            async with lock:
                for ref in request.motion_references:
                    prepared.append(await asyncio.to_thread(prepare_motion_reference,self.settings,ref))
        return await self._submit_prepared(request,prepared)

    @gpu_submission
    async def _submit_prepared(self, request: VideoGenerationRequest, prepared) -> JobAccepted:
        prompt, metadata = self._load_workflow()
        self._set_path(prompt, metadata["prompt_path"], request.prompt)
        negative_path = metadata.get("negative_prompt_path")
        if negative_path:
            self._set_path(prompt, negative_path, request.negative_prompt)
        length_path = metadata.get("length_path")
        if length_path:
            frames = max(5, round(request.duration_seconds * 24))
            self._set_path(prompt, length_path, frames + (5 - frames % 17) % 17)
        seed_path = metadata.get("seed_path")
        if seed_path and request.seed is not None:
            self._set_path(prompt, seed_path, request.seed)
        reference_paths = bind_ordered_reference_slots(prompt,metadata,request.reference_assets)
        manifest=copy.deepcopy(request.parameters.get('reference_manifest'))
        if manifest:
            expected=manifest.get('references',[])
            if len(expected)!=len(request.reference_assets) or any(r.get('picture_index')!=i or any(r.get(k)!=ref.get(k) for k in ['asset_id','sha256','character_id','character_version_id']) for i,(r,ref) in enumerate(zip(expected,request.reference_assets),1)):
                raise AppError(409,'reference_manifest_mismatch','模型实际参考顺序与身份快照不一致，拒绝提交')
            metadata['reference_manifest']=manifest
        if request.reference_assets and len(reference_paths) < len(request.reference_assets):
            raise AppError(422, "workflow_reference_limit", "工作流映射不足以容纳所选参考素材")
        if not request.reference_assets:
            raise AppError(422, "reference_required", "R2V 必须引用真实已审核图片")
        if metadata.get("mode") != "reference_to_video" or not any(n["class_type"] == "MiniMaxH3ReferenceToVideo" for n in prompt.values()):
            raise AppError(422, "r2v_workflow_required", "必须使用官方原生 R2V 节点")
        if metadata.get("output_prefix_path"):
            self._set_path(prompt, metadata["output_prefix_path"], f"projects/{request.project_id}/{request.shot_id}/{request.job_id}")
        for field, default in (("width", 640), ("height", 384)):
            path = metadata.get(f"{field}_path")
            if path:
                value = int(request.parameters.get(field, default))
                if field in {"width", "height"} and (value < 32 or value > 1344 or value % 32):
                    raise AppError(422, "invalid_resolution", "分辨率必须为32的倍数，且不超过1344")
                self._set_path(prompt, path, value)
        apply_sampling_steps(prompt, metadata, request.parameters)
        logdir = PROJECT_ROOT / "logs" / "jobs" / request.job_id
        logdir.mkdir(parents=True, exist_ok=True)
        try:
            async with local_service_client([self.settings.comfyui_base_url], timeout=self.settings.comfyui_client_timeout_seconds) as client:
                uploads=[]
                for index, ref in enumerate(request.reference_assets):
                    path = Path(ref["absolute_path"])
                    data = path.read_bytes()
                    if hashlib.sha256(data).hexdigest() != ref["sha256"]:
                        raise AppError(409, "reference_hash_changed", "锁定的参考素材哈希已变化")
                    response = await client.post(f"{self.settings.comfyui_base_url}/upload/image", files={"image": (f"{ref['sha256']}.png", data, "image/png")}, data={"type": "input", "subfolder": "video_factory", "overwrite": "false"})
                    response.raise_for_status()
                    uploaded = response.json()
                    filename = "/".join(filter(None, [uploaded.get("subfolder"), uploaded["name"]]))
                    self._set_path(prompt, reference_paths[index], filename)
                    uploads.append({'picture_index':index+1,'asset_id':ref.get('asset_id'),'character_id':ref.get('character_id'),
                                    'character_version_id':ref.get('character_version_id'),'sha256':ref['sha256'],
                                    'workflow_slot':'ref_images.ref_image_'+str(index),'load_image_node':reference_paths[index][0],'actual_uploaded_image':filename})
                motion_uploads=[]
                if request.motion_references:
                    if len(request.motion_references)>3:raise AppError(422,'motion_reference_limit','动作参考最多3条')
                    definitions=(await client.get(f"{self.settings.comfyui_base_url}/object_info"))
                    definitions.raise_for_status();nodes=definitions.json()
                    try:
                        loader=nodes['LoadVideo'];split=nodes['GetVideoComponents']
                        video_spec=nodes['MiniMaxH3ReferenceToVideo']['input']['optional']['ref_videos']
                        valid=(loader['output'][0]=='VIDEO' and 'file' in loader['input']['required'] and
                               split['input']['required']['video'][0]=='VIDEO' and split['output'][0]=='IMAGE' and
                               video_spec[0]=='COMFY_AUTOGROW_V3' and video_spec[1]['template']['input']['required']['ref_video'][0]=='IMAGE' and
                               video_spec[1]['template']['prefix']=='ref_video_' and video_spec[1]['template']['max']>=len(request.motion_references))
                    except (KeyError,IndexError,TypeError):valid=False
                    if not valid:raise AppError(409,'motion_nodes_unsupported','本机视频参考节点或接口版本不兼容，未提交生成')
                    targets=[key for key,node in prompt.items() if node['class_type']=='MiniMaxH3ReferenceToVideo']
                    if len(targets)!=1:raise AppError(422,'motion_target_invalid','视频参考必须连接唯一的 H3 节点')
                    target=prompt[targets[0]]['inputs']
                    if any(key.startswith('ref_video_audios.') for key in target):raise AppError(422,'motion_audio_forbidden','此工作流含原声参考，未提交生成')
                    for key in list(target):
                        if key.startswith('ref_videos.'):del target[key]
                    for index,(ref,(data,processed)) in enumerate(zip(request.motion_references,prepared)):
                        if not request.job_id or not all(c.isalnum() or c in '_-' for c in request.job_id) or len(request.job_id)>64:
                            raise AppError(422,'motion_job_id_invalid','动作参考任务 ID 无效，未提交生成')
                        filename='vf_motion_'+request.job_id+'_'+str(index)+'_'+processed['sha256'][:16]+'.mp4'
                        receipt={'job_id':request.job_id,'status':'uploading','uploads':motion_uploads,
                                 'pending_name':filename,'retention':'ComfyUI input retained for provenance; no unsafe auto-delete'}
                        (logdir/'motion_reference_transfer.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
                        response=await client.post(f"{self.settings.comfyui_base_url}/upload/image",files={'image':(filename,data,'video/mp4')},data={'type':'input','overwrite':'false'})
                        response.raise_for_status();uploaded=response.json()
                        receipt.update(status='uploaded_unverified',upload_response={key:uploaded.get(key) for key in ('name','subfolder','type')})
                        (logdir/'motion_reference_transfer.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
                        if uploaded.get('name')!=filename or uploaded.get('subfolder','') or uploaded.get('type')!='input':
                            raise AppError(502,'motion_reference_upload_invalid','动作参考上传位置不符，未提交生成')
                        view=await client.get(f"{self.settings.comfyui_base_url}/view",params={'filename':filename,'subfolder':'','type':'input'})
                        view.raise_for_status()
                        if hashlib.sha256(view.content).hexdigest()!=processed['sha256']:
                            raise AppError(502,'motion_reference_upload_changed','上传动作参考的哈希不符，未提交生成')
                        load=f'factory_motion_load_{index}';components=f'factory_motion_components_{index}'
                        if load in prompt or components in prompt:raise AppError(422,'motion_node_collision','动作参考节点 ID 冲突，未提交生成')
                        prompt[load]={'class_type':'LoadVideo','inputs':{'file':filename}}
                        prompt[components]={'class_type':'GetVideoComponents','inputs':{'video':[load,0]}}
                        target[f'ref_videos.ref_video_{index}']=[components,0]
                        motion_uploads.append({'video_index':index+1,'video_id':ref['video_id'],'source_sha256':ref['source_sha256'],
                            'expected_revision':ref['expected_revision'],'trim_start_seconds':ref['trim_start_seconds'],
                            'trim_end_seconds':ref['trim_end_seconds'],'effective_frames':ref['effective_frames'],
                            'effective_duration_seconds':ref['effective_duration_seconds'],'processed':processed,
                            'workflow_slot':f'ref_videos.ref_video_{index}','load_video_node':load,
                            'components_node':components,'actual_uploaded_video':filename,'audio_included':False})
                        receipt.update(status='uploaded_verified',uploads=motion_uploads,pending_name=None)
                        (logdir/'motion_reference_transfer.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
                    metadata['motion_reference_uploads']=motion_uploads
                metadata['reference_uploads']=uploads
                (logdir/'reference_manifest.json').write_text(json.dumps({'reference_manifest':manifest,'actual_uploads':uploads,'motion_reference_uploads':motion_uploads},ensure_ascii=False,indent=2),encoding='utf-8')
                (logdir / "submitted_prompt.json").write_text(json.dumps({"prompt": prompt, "metadata": metadata}, ensure_ascii=False, indent=2), encoding="utf-8")
                try:
                    response = await client.post(
                        f"{self.settings.comfyui_base_url}/prompt",
                        json={"prompt": prompt, "client_id": request.job_id},
                    )
                except httpx.TransportError as exc:
                    if request.motion_references:
                        raise AppError(502,'motion_submission_unknown','动作参考生成提交结果未知；保留任务，不自动重发') from exc
                    raise
                (logdir / "submission_response.json").write_text(response.text, encoding="utf-8")
                if response.status_code >= 400:
                    if request.motion_references and (response.status_code==408 or response.status_code>=500):
                        raise AppError(502,'motion_submission_unknown','动作参考提交返回服务器错误，受理状态不明；保留任务，不自动重发')
                    raise AppError(502, "comfyui_validation_failed", "ComfyUI 工作流验证失败", {"response": response.json(), "log": str(logdir)})
                try:payload = response.json()
                except ValueError as exc:
                    if request.motion_references:raise AppError(502,'motion_submission_unknown','动作参考提交已响应但结果无法解析；保留任务，不自动重发') from exc
                    raise
        except httpx.TimeoutException as exc:
            raise AppError(504, "comfyui_timeout", "ComfyUI 提交超时") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise AppError(502, "comfyui_submit_failed", "ComfyUI 拒绝或无法处理任务") from exc
        if not isinstance(payload,dict):
            if request.motion_references:raise AppError(502,'motion_submission_unknown','动作参考提交响应格式无效；保留任务，不自动重发')
            raise AppError(502,'comfyui_invalid_response','ComfyUI 响应格式无效')
        prompt_id = payload.get("prompt_id")
        if not prompt_id:
            if request.motion_references:raise AppError(502,'motion_submission_unknown','动作参考提交响应缺少任务 ID；保留任务，不自动重发')
            raise AppError(502, "comfyui_invalid_response", "ComfyUI 响应缺少 prompt_id")
        return JobAccepted(str(prompt_id), "queued", metadata={"workflow": metadata.get("name"), "workflow_sha256": hashlib.sha256(json.dumps(prompt, sort_keys=True).encode()).hexdigest(), "log_dir": str(logdir),'reference_manifest':manifest,'reference_uploads':uploads,'motion_reference_uploads':motion_uploads,'sampling_receipt':metadata.get('sampling_receipt')})

    async def get_job(self, job_id: str) -> dict[str, Any]:
        try:
            async with local_service_client([self.settings.comfyui_base_url], timeout=10.0) as client:
                response = await client.get(f"{self.settings.comfyui_base_url}/history/{job_id}")
                response.raise_for_status()
                payload = response.json()
                queue = (await client.get(f"{self.settings.comfyui_base_url}/queue")).json() if job_id not in payload else {}
        except httpx.HTTPError as exc:
            raise AppError(502, "comfyui_status_failed", "无法读取 ComfyUI 任务状态") from exc
        except ValueError as exc:
            raise AppError(502, "comfyui_invalid_history", "ComfyUI 历史不是有效 JSON") from exc
        history = payload.get(job_id)
        if history:
            state = history.get("status", {})
            messages = state.get("messages", [])
            if state.get("status_str") == "error":
                failures = [entry[1] for entry in messages if isinstance(entry, list) and len(entry) > 1 and entry[0] == "execution_error"]
                failure = failures[-1] if failures else {}
                summary = f"Node {failure.get('node_id', '?')} ({failure.get('node_type', '?')}): {failure.get('exception_type', 'ComfyUI error')}: {failure.get('exception_message', 'Execution failed')}"
                return {"provider_job_id": job_id, "status": "failed", "raw": history, "error": summary.strip()}
            if state.get("completed") and state.get("status_str") == "success":
                return {"provider_job_id": job_id, "status": "generated", "raw": history}
        if any(str(row[1]) == job_id for row in queue.get("queue_running", [])):
            status = "running"
        elif any(str(row[1]) == job_id for row in queue.get("queue_pending", [])):
            status = "queued"
        elif not history:
            return {"provider_job_id": job_id, "status": "reconciling", "error": "正在重查 ComfyUI 队列与历史边界状态", "raw": queue}
        else:
            status = "running"
        return {"provider_job_id": job_id, "status": status, "raw": history or queue}

    async def cancel(self, job_id: str) -> bool:
        try:
            async with local_service_client([self.settings.comfyui_base_url], timeout=self.settings.comfyui_client_timeout_seconds) as client:
                queue = (await client.get(f"{self.settings.comfyui_base_url}/queue")).json()
                if any(str(row[1]) == job_id for row in queue.get("queue_running", [])):
                    response = await client.post(f"{self.settings.comfyui_base_url}/interrupt", json={"prompt_id": job_id})
                    response.raise_for_status()
                response = await client.post(
                    f"{self.settings.comfyui_base_url}/queue",
                    json={"delete": [job_id]},
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise AppError(502, "comfyui_cancel_failed", "ComfyUI 取消任务失败") from exc
        return True


def provider_for(settings: Settings) -> VideoGenerationProvider:
    if settings.h3_demo_mode:
        return DemoVideoProvider()
    return ComfyUIH3Provider(settings)

