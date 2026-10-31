"""Read-only discovery and durable, isolated real video experiments.

Discovery reads safetensors headers; it never imports a model implementation.
Only a real provider job and a fully decoded output can verify a candidate.
"""
from __future__ import annotations

import asyncio
import collections
import copy
import hashlib
import json
import logging
import os
import shutil
import struct
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Request
from sqlalchemy import select

from app import models as m
from app.core.config import PROJECT_ROOT
from app.core.errors import AppError
from app.core.paths import safe_media_path, media_relative
from app.models.entities import new_id, utcnow
from app.models.model_lab import LocalModel, ModelDirectory, ModelExperiment, ModelLabDefault
from app.schemas import GenerationRequest
from app.services.ai_services import service_snapshot

# Unknown results forbid resubmission within their experiment; they do not prove
# another project's GPU is occupied. Real provider queue admission remains final.
ACTIVE = {'queued', 'running', 'submitting'}
ALLOWED_CASE_PARAMETERS = {'steps'}
WEIGHT_EXTENSIONS = {'.safetensors', '.gguf', '.ckpt', '.onnx', '.pt', '.pth', '.bin'}
LIMITATIONS = ['文件存在与节点已识别不代表模型可运行；需要独立实测生成和完整视频解码。',
               '人工视觉评分与技术媒体验证分开；没有自动电影质量分数。',
               '没有独立第二套视频权重时，只能做单模型参数对照。',
               'H3 Max 为托管云端产品，没有在本机发现可公开下载的独立权重；未配置、未调用。']


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), default=str).encode()).hexdigest()


def file_sha(path):
    hasher = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            hasher.update(chunk)
    return hasher.hexdigest()


def required(session, model, identifier):
    row = session.get(model, identifier)
    if row is None:
        raise AppError(404, 'model_lab_not_found', '模型实验记录不存在')
    return row


def tensor_header(path):
    """Read bounded JSON header only, never torch.load or pickle."""
    path = Path(path)
    stat = path.stat()
    info = {'path': str(path), 'name': path.name, 'bytes': stat.st_size, 'mtime_ns': stat.st_mtime_ns,
            'extension': path.suffix.lower(), 'loaded': False, 'full_weight_hash': 'NOT RUN'}
    if path.suffix.lower() != '.safetensors':
        with path.open('rb') as stream:
            magic = stream.read(24)
        info.update(magic_hex=magic.hex(), state='unknown_format', diagnostic='未反序列化非safetensors权重，无法据文件名确认架构')
        if magic.startswith(b'GGUF') and len(magic) >= 24:
            info.update(format='GGUF', gguf_version=struct.unpack('<I', magic[4:8])[0], tensor_count=struct.unpack('<Q', magic[8:16])[0])
        return info
    with path.open('rb') as stream:
        prefix = stream.read(8)
        if len(prefix) != 8:
            raise ValueError('safetensors文件头不完整')
        length = struct.unpack('<Q', prefix)[0]
        if not 2 <= length <= min(32 * 1024 * 1024, stat.st_size - 8):
            raise ValueError('safetensors头大小无效或超出只读诊断上限')
        raw = stream.read(length)
    data = json.loads(raw)
    tensors = {k: v for k, v in data.items() if k != '__metadata__'}
    if not tensors or any(not isinstance(v, dict) or 'data_offsets' not in v or 'shape' not in v or 'dtype' not in v for v in tensors.values()):
        raise ValueError('safetensors张量描述无效')
    extent = max(v['data_offsets'][1] for v in tensors.values())
    if extent + length + 8 != stat.st_size:
        raise ValueError('safetensors头声明的数据范围与实际文件大小不一致')
    info.update(state='header_valid', header_sha256=hashlib.sha256(raw).hexdigest(), tensor_count=len(tensors),
        dtype_counts=dict(collections.Counter(v['dtype'] for v in tensors.values())),
        tensor_examples=list(tensors)[:8], metadata={k: str(v)[:3000] for k, v in data.get('__metadata__', {}).items() if 'thumbnail' not in k.lower()},
        architecture_evidence='safetensors header/tensor names; weights not loaded')
    return info


def add_directory(session, path, label=''):
    # Local disk only; an editable text field must not cause implicit UNC/SMB access.
    if str(path).startswith(('\\\\', '//')) or '://' in str(path):
        raise AppError(422, 'local_model_directory_required', '请选择本机磁盘目录，不接受网络共享或URL')
    target = Path(path).expanduser().resolve()
    if not target.is_dir():
        raise AppError(422, 'model_directory_missing', '模型目录不存在或不可读取')
    row = session.scalar(select(ModelDirectory).where(ModelDirectory.path == str(target)))
    if row is None:
        row = ModelDirectory(path=str(target), label=label or target.name, enabled=True)
        session.add(row); session.flush()
    elif label:
        row.label = label
    return row


def _upsert_model(session, directory, path, *, name, kind, model_id=None, service=None, workflow=None, scan):
    path = str(path)
    query = select(LocalModel).where(LocalModel.path == path)
    candidates = list(session.scalars(query))
    if service is None and kind == 'unknown':
        configured = next((r for r in candidates if r.service_id and r.kind == 'video'), None)
        if configured is not None:
            return configured  # raw discovery must not duplicate or overwrite its actual adapter mapping
    row = next((r for r in candidates if r.service_id == (service.id if service else None)), None)
    if service and row is None:
        row = next((r for r in candidates if r.service_id is None and r.kind == kind), None)
    fingerprint = digest({'components': [{k: c.get(k) for k in ('path', 'bytes', 'mtime_ns', 'header_sha256', 'sha256', 'state')} for c in scan.get('components', [])],
                          'workflow_sha256': scan.get('workflow_sha256'), 'model_id': model_id or name,
                          'service_revision': scan.get('service_revision'), 'service_base_url': scan.get('service_base_url')})
    if row is None:
        row = LocalModel(path=path, name=name, kind=kind, model_id=model_id or name, fingerprint=fingerprint)
        session.add(row)
    elif row.fingerprint != fingerprint:
        row.verification_json = {'state': 'not_tested', 'previous_evidence': row.verification_json, 'reason': '模型组件或工作流指纹已改变，需要重新实测'}
    row.name, row.kind = name, kind
    row.model_id, row.fingerprint = model_id or name, fingerprint
    row.directory_id = directory.id if directory else row.directory_id
    row.service_id = service.id if service else row.service_id
    row.workflow_path = str(workflow) if workflow else row.workflow_path
    row.scan_json = scan
    session.flush()
    return row


def _language_group(path):
    config_path = path / 'config.json'
    if not config_path.is_file() or config_path.stat().st_size > 2 * 1024 * 1024:
        return None
    config = json.loads(config_path.read_text(encoding='utf-8-sig'))
    architecture = ' '.join(config.get('architectures') or [])
    language = any(t in architecture for t in ('CausalLM', 'MiniMaxM3SparseForConditionalGeneration'))
    if not language:
        return None
    index_path = path / 'model.safetensors.index.json'
    files = []
    if index_path.is_file() and index_path.stat().st_size < 32 * 1024 * 1024:
        index = json.loads(index_path.read_text(encoding='utf-8-sig'))
        files = sorted(set(index.get('weight_map', {}).values()))
        if any(Path(x).is_absolute() or '..' in Path(x).parts for x in files):
            raise ValueError('权重索引包含不安全的路径')
    else:
        files = [p.name for p in path.glob('*.safetensors')]
    components, diagnostics = [], ['多模态语言条件生成架构：文本输出，不能用于视频生成。', '未加载权重；语言推理后端、显存与卸载方案未验证。']
    for name in files:
        weight = path / name
        if not weight.is_file():
            components.append({'path': str(weight), 'state': 'missing'}); continue
        try: components.append(tensor_header(weight))
        except (OSError, ValueError, KeyError) as exc: components.append({'path': str(weight), 'state': 'invalid', 'error': str(exc)})
    components.append({'path': str(config_path), 'bytes': config_path.stat().st_size, 'sha256': file_sha(config_path), 'state': 'present'})
    size = sum(c.get('bytes', 0) for c in components)
    if size > 48 * 1024**3:
        diagnostics.append('权重存储超过48GiB；本机约32GiB内存+16GiB显存，没有已验证的大模型分页卸载服务。')
    return {'components': components, 'component_status': 'complete' if files and all(c['state'] in ('header_valid', 'present') for c in components) else 'missing',
            'diagnostics': diagnostics, 'architecture': architecture, 'model_type': config.get('model_type'),
            'quantization': config.get('quantization_config'), 'stored_weight_bytes': size,
            'classification_source': 'config.architectures + actual tensor headers; directory label is not release authentication'}


def _ensure_hosted_catalog(session):
    path = 'hosted://minimax/h3-max'
    existing = session.scalar(select(LocalModel).where(LocalModel.path == path))
    if existing:
        return existing
    row = LocalModel(name='MiniMax H3 Max（仅托管云端，未配置）', kind='video', path=path, model_id='H3 Max', fingerprint=digest(path),
        scan_json={'component_status': 'weights_unavailable', 'components': [], 'diagnostics': [
            'H3 Max是独立托管产品，未发现官方独立公开权重；不能下载基础H3冒充Max。',
            '本机未配置相应云端服务，未调用、未计费；本地加载已禁用。'],
            'hosted_only': True, 'sources': ['https://github.com/MiniMax-AI/awesome-minimax-h3-integration', 'https://fal.ai/learn/devs/minimax-h3-vs-minimax-h3-max']},
        verification_json={'state': 'not_tested', 'native_execution': 'NOT RUN'})
    session.add(row); session.flush(); return row


async def scan_models(session, settings, directory_ids=None):
    # Probe before the first write/flush: an offline service must not hold SQLite's
    # writer lock while other projects are being reviewed or imported.
    services = list(session.scalars(select(m.AIService).where(m.AIService.module == 'video_generation', m.AIService.enabled.is_(True))))
    node_probes = {}
    from app.services.ai_services import service_request
    for service in services:
        if service.provider != 'comfyui': continue
        snapshot = service_snapshot(service)
        probe = {**snapshot, 'settings': {**snapshot['settings'], 'timeout': 10}}
        try:
            response = await service_request(settings, probe, 'GET', '/object_info')
            response.raise_for_status()
            body = response.json()
            node_probes[service.id] = (body if isinstance(body, dict) else {}, [])
        except Exception as exc:
            node_probes[service.id] = ({}, ['ComfyUI只读节点诊断失败：' + type(exc).__name__])
    if directory_ids is None:
        directories = list(session.scalars(select(ModelDirectory).where(ModelDirectory.enabled.is_(True))))
    else:
        directories = [required(session, ModelDirectory, identifier) for identifier in directory_ids]
    default = settings.media_root.parent / 'comfyui_runtime/models'
    if default.is_dir() and all(Path(d.path) != default.resolve() for d in directories):
        directories.append(add_directory(session, default, '项目 ComfyUI 模型'))
    file_info, file_directories, errors = {}, {}, []
    for directory in directories:
        base = Path(directory.path)
        group_roots, count = [], 0
        try:
            # os.walk does not follow directory symlinks; no network requests or model code.
            for root, dirs, files in os.walk(base, followlinks=False):
                dirs[:] = [d for d in dirs if d not in {'.cache', '.download', '.git', 'node_modules', 'site-packages'}]
                current = Path(root)
                if any(current.is_relative_to(group) for group in group_roots):
                    continue
                if 'config.json' in files:
                    try:
                        language = _language_group(current)
                        if language:
                            _upsert_model(session, directory, current, name=current.name, kind='language', scan=language)
                            group_roots.append(current); count += len(language['components']); dirs[:] = []; continue
                    except (OSError, ValueError, KeyError) as exc: errors.append(str(exc))
                for name in files:
                    path = current / name
                    if path.suffix.lower() not in WEIGHT_EXTENSIONS: continue
                    count += 1
                    if count > 2048: raise ValueError('本次目录扫描超过2048个模型文件，请指定更小目录')
                    try:
                        item = tensor_header(path)
                        file_info[str(path.resolve())], file_directories[str(path.resolve())] = item, directory
                        folder = current.name.lower()
                        kind = {'vae': 'vae', 'text_encoders': 'encoder', 'clip': 'encoder', 'loras': 'lora', 'upscale_models': 'upscale', 'latent_upscale_models': 'upscale', 'checkpoints': 'image'}.get(folder, 'unknown')
                        _upsert_model(session, directory, path.resolve(), name=name, kind=kind,
                            scan={'components': [item], 'component_status': 'complete' if item['state'] == 'header_valid' else 'unknown',
                                  'diagnostics': ['只读头验证，不代表可运行；没有已验证的对应视频工作流。'], 'role_source': 'storage category + header; generic filenames remain unverified'})
                    except (OSError, ValueError, KeyError) as exc: errors.append(str(path) + ': ' + str(exc))
            directory.scan_json = {'at': utcnow().isoformat(), 'status': 'PASS' if not errors else 'PARTIAL', 'file_count': count, 'errors': errors[-20:]}
        except (OSError, ValueError) as exc:
            directory.scan_json = {'at': utcnow().isoformat(), 'status': 'BLOCKED', 'error': str(exc), 'file_count': count}
    # Previously registered directories remain dependency sources for a targeted rescan.
    # Their headers are refreshed instead of trusting an old "complete" flag.
    for previous in session.scalars(select(LocalModel)):
        for component in previous.scan_json.get('components', []):
            path = Path(component.get('path', ''))
            if component.get('state') == 'header_valid' and str(path) not in file_info:
                try:
                    file_info[str(path)] = tensor_header(path)
                    if previous.directory_id:
                        file_directories[str(path)] = session.get(ModelDirectory, previous.directory_id)
                except (OSError, ValueError, KeyError): pass
    # Configured real adapters are mapped to their actual graph, not a name-only menu.
    for service in services:
        snapshot = service_snapshot(service)
        if service.provider == 'video_api':
            _upsert_model(session, None, 'service://' + service.id + '/' + (service.default_model or ''), name=service.name,
                kind='video', model_id=service.default_model or '', service=service,
                scan={'components': [], 'component_status': 'complete' if service.settings_json.get('video_contract') in {'video-jobs-v1','wan26-i2v','lk888-h3-v2'} else 'unknown',
                      'diagnostics': ['云端视频能力必须通过实际任务和完整解码，连接成功不等于视频已验证。'], 'provider': 'video_api', 'service_revision': service.revision,
                      'service_base_url': snapshot['base_url'], 'seed_reproducibility': '此服务未接收 seed，无法保证复现' if service.settings_json.get('video_contract') == 'lk888-h3-v2' else '服务支持接收seed；是否可复现未验证'})
            continue
        if service.provider != 'comfyui': continue
        try:
            workflow = Path(service.settings_json.get('workflow_path') or settings.h3_workflow_api_json or '')
            graph = json.loads(workflow.read_text(encoding='utf-8-sig'))
            nodes = graph.get('prompt', {})
            if not any(v.get('class_type') == 'MiniMaxH3ReferenceToVideo' for v in nodes.values()): continue
            model_names = [n['inputs']['unet_name'] for n in nodes.values() if n.get('class_type') == 'UNETLoader']
            required_files = [n['inputs'][field] for n in nodes.values() for field in ('unet_name', 'clip_name', 'vae_name', 'lora_name') if field in n.get('inputs', {})]
            components = []
            for name in required_files:
                matches = [item for item in file_info.values() if item['name'] == name]
                components.append(matches[0] if len(matches) == 1 else {'name': name, 'state': 'missing' if not matches else 'ambiguous'})
            available, diagnostics = node_probes.get(service.id, ({}, ['当前服务未诊断']))
            diagnostics = list(diagnostics)
            for node in nodes.values():
                definition = available.get(node.get('class_type'))
                if not definition: diagnostics.append('节点未识别：' + str(node.get('class_type'))); continue
                fields = definition.get('input', {}).get('required', {})
                for field in ('unet_name', 'clip_name', 'vae_name', 'lora_name'):
                    if field in node.get('inputs', {}) and field in fields and isinstance(fields[field][0], list) and node['inputs'][field] not in fields[field][0]:
                        diagnostics.append('模型未被当前服务识别：' + node['inputs'][field])
            complete = components and all(c.get('state') == 'header_valid' for c in components)
            if model_names:
                primary = next((item for item in components if item.get('name') == model_names[0]), {})
                _upsert_model(session, file_directories.get(primary.get('path')), primary.get('path') or 'unresolved://' + service.id, name=service.name,
                    kind='video', model_id=model_names[0], service=service, workflow=workflow,
                    scan={'components': components, 'component_status': 'complete' if complete else 'missing',
                          'workflow_sha256': file_sha(workflow), 'workflow_name': graph.get('_video_factory', {}).get('name'),
                          'diagnostics': diagnostics or ['节点和组件可识别；当前加载/生成未测试。'], 'provider': 'comfyui', 'service_revision': service.revision,
                          'service_base_url': snapshot['base_url'], 'nodes_recognized': not diagnostics,
                          'seed_reproducibility': '工作流明确映射随机种子，GPU确定性未保证'})
        except (OSError, ValueError, KeyError) as exc: errors.append('配置工作流无法读取：' + str(exc))
    _ensure_hosted_catalog(session)
    session.add(m.AuditEvent(object_type='model_inventory', object_id='local', action='scan_headers_only', details_json={'directory_ids': [d.id for d in directories], 'errors': errors, 'gpu_loaded': False}))
    session.flush()
    return overview(session)


def _component_unchanged(model):
    for component in model.scan_json.get('components', []):
        if component.get('state') not in ('header_valid', 'present'): return False
        path = Path(component.get('path', ''))
        try:
            st = path.stat()
            if st.st_size != component.get('bytes') or ('mtime_ns' in component and st.st_mtime_ns != component['mtime_ns']): return False
            if component.get('sha256') and file_sha(path) != component['sha256']: return False
            if component.get('header_sha256') and tensor_header(path).get('header_sha256') != component['header_sha256']: return False
        except (OSError, ValueError, KeyError): return False
    if model.workflow_path:
        try:
            if file_sha(Path(model.workflow_path)) != model.scan_json.get('workflow_sha256'): return False
        except OSError: return False
    return True


def model_view(row, session=None):
    evidence = row.verification_json or {}
    state = evidence.get('state', 'not_tested')
    verified = state == 'verified' and evidence.get('fingerprint') == row.fingerprint and _component_unchanged(row)
    if session is not None and row.service_id:
        service = session.get(m.AIService, row.service_id)
        verified = verified and bool(service and service.enabled and service.revision == row.scan_json.get('service_revision') and service.base_url == row.scan_json.get('service_base_url'))
    return {'id': row.id, 'name': row.name, 'kind': row.kind, 'path': row.path, 'model_id': row.model_id,
            'service_id': row.service_id, 'workflow_path': row.workflow_path, 'fingerprint': row.fingerprint,
            'component_status': row.scan_json.get('component_status', 'unknown'),
            'load_status': 'verified' if verified else 'failed' if state == 'failed' else 'not_tested',
            'eligible_for_comparison': verified and row.kind == 'video' and bool(row.service_id) and row.scan_json.get('component_status') == 'complete',
            'diagnostics': row.scan_json.get('diagnostics', []), 'components': row.scan_json.get('components', []),
            'metadata': {k: v for k, v in row.scan_json.items() if k not in ('components', 'diagnostics')}, 'evidence': evidence}


def experiment_view(row):
    cases = copy.deepcopy(row.cases_json)
    for case in cases:
        if case.get('relative_path'): case['video_url'] = '/api/media/' + case['relative_path']
    return {'id': row.id, 'name': row.name, 'mode': row.mode, 'status': row.status,
            'source_shot_id': row.source_shot_id, 'test_project_id': row.test_project_id, 'test_shot_id': row.test_shot_id,
            'snapshot': row.snapshot_json, 'cases': cases, 'results': row.result_json, 'error': row.error_message,
            'created_at': row.created_at.isoformat()}


def overview(session):
    default = session.get(ModelLabDefault, 'video_generation')
    return {'directories': [{'id': d.id, 'path': d.path, 'label': d.label, 'enabled': d.enabled, 'scan': d.scan_json} for d in session.scalars(select(ModelDirectory).order_by(ModelDirectory.created_at))],
            'models': [model_view(r, session) for r in session.scalars(select(LocalModel).order_by(LocalModel.created_at))],
            'experiments': [experiment_view(r) for r in session.scalars(select(ModelExperiment).order_by(ModelExperiment.created_at.desc()))],
            'default': default.configuration_json if default else None, 'limitations': LIMITATIONS}


def _case_parameters(model, parameters, width, height):
    if set(parameters) - ALLOWED_CASE_PARAMETERS:
        raise AppError(422, 'unsupported_lab_parameters', '实验只接受当前实际适配的steps参数，禁止任意注入工作流字段')
    service = model.scan_json.get('provider')
    if service == 'comfyui':
        if width % 32 or height % 32 or max(width, height) > 1344:
            raise AppError(422, 'lab_local_dimensions', '本机H3实验宽高必须为32的倍数且不超过1344；不能冒充原生4K')
        steps = parameters.get('steps', 20)
        if not isinstance(steps, int) or isinstance(steps, bool) or not 1 <= steps <= 60:
            raise AppError(422, 'lab_invalid_steps', '采样步数必须是1至60的整数')
        return {'width': width, 'height': height, 'steps': steps}
    if parameters:
        raise AppError(422, 'cloud_steps_unsupported', '当前云端video-jobs-v1不支持steps，未发送无效参数')
    return {'width': width, 'height': height}


def _source_inputs(session, settings, source, prompt_override):
    from app.services.screenplay import generation_inputs, uses_structured_screenplay
    from app.api.routes import build_prompt, script_for_shot
    script = script_for_shot(session, source)
    project = required(session, m.Project, source.project_id)
    if project.status == 'deleted' or source.status != 'approved' or not script or script.status != 'approved':
        raise AppError(409, 'lab_source_not_approved', '实验需要一条未删除且已批准脚本和分镜；不会修改正式项目状态')
    if project.source_type == 'model_lab':
        raise AppError(409, 'lab_recursive_source', '请选择正式镜头作为输入，不复制另一个实验的内部镜头')
    if uses_structured_screenplay(session, source):
        version, references, prompt, snapshot = generation_inputs(session, source, settings)
        if snapshot.get('cinematic') and not snapshot['cinematic']['preflight']['ready']:
            raise AppError(409, 'lab_source_preflight', '正式镜头关键画面和制作预检尚未完成')
        negative = ''
    else:
        version = required(session, m.CharacterVersion, source.character_asset_version_id)
        references = []
        for asset_id in source.reference_asset_ids_json:
            asset = required(session, m.CharacterAsset, asset_id)
            if asset.status != 'approved' or asset.character_version_id != version.id or not asset.mime_type.startswith('image/'):
                raise AppError(409, 'lab_reference_unapproved', '参考图必须属于对应已批准人物版本')
            references.append({'asset_id': asset.id, 'asset_type': asset.asset_type, 'relative_path': asset.relative_path, 'absolute_path': str(safe_media_path(settings.media_root, asset.relative_path, must_exist=True)), 'sha256': asset.sha256, 'character_id': version.character_id, 'character_version_id': version.id})
        prompt, negative = build_prompt(source, version, None)
        snapshot = {'source_shot_id': source.id, 'participants': [{'character_id': version.character_id, 'character_version_id': version.id}]}
    if version.approval_state != 'approved' or not references:
        raise AppError(409, 'lab_inputs_unapproved', '需要正式人物版本和至少一张已批准参考图')
    snapshot['model_lab_negative_prompt'] = negative
    return version, references, prompt_override or prompt, snapshot


def create_experiment(session, settings, payload, *, load_model_id=None):
    if settings.h3_demo_mode:
        raise AppError(409, 'lab_real_only', '模型实验只运行真实服务，不能用Demo获得验证状态')
    source = required(session, m.StoryboardShot, payload.shot_id)
    expected_ratio = {'16:9': 16 / 9, '9:16': 9 / 16, '1:1': 1}.get(source.aspect_ratio)
    if expected_ratio and abs(payload.width / payload.height - expected_ratio) > .025:
        raise AppError(422, 'lab_aspect_mismatch', '实验像素宽高必须匹配原镜头画幅，不能任意拉伸人物')
    specs = [{'model_id': load_model_id, 'label': '明确实测加载与视频生成', 'parameters': {'steps': payload.steps}}] if load_model_id else [c.model_dump() for c in payload.cases]
    mode = 'load_test' if load_model_id else payload.mode
    models, case_parameters = [], []
    for case in specs:
        model = required(session, LocalModel, case['model_id'])
        if model.kind != 'video' or not model.service_id or model.scan_json.get('component_status') != 'complete':
            raise AppError(409, 'lab_video_model_blocked', '此候选不是组件齐备且已配置的视频模型', {'model_id': model.id, 'diagnostics': model.scan_json.get('diagnostics')})
        if not _component_unchanged(model):
            raise AppError(409, 'lab_model_changed', '模型文件或工作流已改变，请重新扫描并实测')
        if not load_model_id and not model_view(model, session)['eligible_for_comparison']:
            raise AppError(409, 'lab_model_unverified', '普通对比只接受真实生成及完整解码已验证模型；请先使用单独实测入口')
        service = required(session, m.AIService, model.service_id)
        if not service.enabled or service.revision != model.scan_json.get('service_revision') or service.base_url != model.scan_json.get('service_base_url'):
            raise AppError(409, 'lab_service_changed', '服务配置已改变，请重新扫描诊断')
        if service_snapshot(service)['is_cloud'] and not payload.allow_cloud_costs:
            raise AppError(409, 'lab_cloud_cost_confirmation', '本次包含真实云端费用，需要明确允许后才能创建任务')
        if load_model_id and model.scan_json.get('provider') == 'video_api':
            case['parameters'] = {}  # this adapter cannot receive a sampler step count
        models.append(model); case_parameters.append(_case_parameters(model, case['parameters'], payload.width, payload.height))
    unique_models = {(r.scan_json.get('provider'), r.model_id, r.scan_json.get('workflow_sha256')) for r in models}
    if mode == 'cross_model' and len({(r.scan_json.get('provider'), r.model_id) for r in models}) < 2:
        raise AppError(422, 'lab_requires_distinct_models', 'H3的不同步数不是不同模型；请选择参数对照模式')
    if mode == 'cross_model':
        primary_headers = [next((c.get('header_sha256') for c in r.scan_json.get('components', []) if c.get('name') == r.model_id), None) for r in models]
        identified = [h for h in primary_headers if h]
        if len(identified) != len(set(identified)):
            raise AppError(422, 'lab_duplicate_weight_identity', '两个候选的实际主权重头相同，不能把重命名权重当作跨模型对照')
    if mode == 'parameters' and len(unique_models) != 1:
        raise AppError(422, 'lab_parameters_same_model', '参数对照要求同一个真实模型及同一工作流')
    if mode == 'parameters' and len({digest(x) for x in case_parameters}) != len(case_parameters):
        raise AppError(422, 'lab_duplicate_case', '参数对照不能提交完全相同的重复参数')
    version, references, prompt, frozen = _source_inputs(session, settings, source, payload.prompt)
    experiment_id = new_id('experiment')
    # Inputs are copied to new owned paths so deletion/edit of a source file cannot silently alter the experiment.
    copied = []
    for index, ref in enumerate(references):
        original = Path(ref['absolute_path'])
        actual = file_sha(original)
        if actual != ref['sha256']:
            raise AppError(409, 'lab_reference_hash_changed', '正式参考图实际SHA与记录不一致，未提交实验')
        target = safe_media_path(settings.media_root, f'model_lab/{experiment_id}/inputs/{index + 1}_{actual}{original.suffix.lower()}')
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, target)
        copied.append({**{k: v for k, v in ref.items() if k != 'absolute_path'}, 'source_relative_path': ref['relative_path'], 'relative_path': media_relative(settings.media_root, target)})
        session.add(m.ManagedFile(relative_path=media_relative(settings.media_root, target), state='active', ownership_json=[{'table': 'model_experiments', 'id': experiment_id}], metadata_json={'verified_owned': True, 'sha256': actual, 'kind': 'experiment_input_copy'}))
    project = m.Project(name='模型实验 · ' + (getattr(payload, 'name', None) or experiment_id), source_type='model_lab', source_text='', status='experiment')
    session.add(project); session.flush()
    shot = m.StoryboardShot(project_id=project.id, order=1, scene_title='独立实验镜头', visual_prompt=source.visual_prompt,
        action=source.action, duration_seconds=source.duration_seconds, aspect_ratio=source.aspect_ratio,
        character_id=version.character_id, character_asset_version_id=version.id,
        reference_asset_ids_json=list(source.reference_asset_ids_json), status='approved')
    session.add(shot); session.flush()
    session.add(m.ScriptRevision(project_id=project.id, revision=1, parsed_json={'model_lab': True, 'explicit_shot_ids': [shot.id]}, raw_text=prompt, status='approved', approved_at=utcnow(), approved_by='local-user-model-experiment'))
    snapshot = {'source_project_id': source.project_id, 'source_shot_id': source.id, 'prompt': prompt,
        'negative_prompt': frozen.get('model_lab_negative_prompt', ''), 'references': copied, 'source_snapshot': frozen,
        'character_version_id': version.id, 'seed': payload.seed, 'width': payload.width, 'height': payload.height,
        'duration_seconds': source.duration_seconds, 'aspect_ratio': source.aspect_ratio, 'allow_cloud_costs': payload.allow_cloud_costs,
        'reference_manifest': frozen.get('reference_manifest', {}), 'input_copy_policy': 'immutable copies; source identity IDs retained',
        'visual_review': 'NOT RUN; technical validation is not identity or movie realism approval'}
    snapshot['input_sha256'] = digest(snapshot)
    cases = []
    for spec, model, parameters in zip(specs, models, case_parameters):
        service = required(session, m.AIService, model.service_id)
        cases.append({'id': new_id('case'), 'label': spec['label'] or model.name, 'model_registry_id': model.id,
            'model_id': model.model_id, 'model_name': model.name, 'fingerprint': model.fingerprint,
            'service_snapshot': service_snapshot(service, model.model_id), 'workflow_sha256': model.scan_json.get('workflow_sha256'),
            'parameters': parameters, 'status': 'pending', 'deviations': ['Seed is submitted; bit-for-bit GPU/service determinism is not verified.'],
            'review': {'source': 'human', 'status': 'NOT RUN', 'notes': '', 'scores': {}, 'best': False}})
        if snapshot['negative_prompt'] and model.scan_json.get('provider') == 'comfyui':
            cases[-1]['deviations'].append('Negative prompt retained for lineage; this H3 graph has no native negative-conditioning mapping and cannot apply an unsupported node input.')
    row = ModelExperiment(id=experiment_id, name=getattr(payload, 'name', None) or '明确实测 · ' + models[0].name, mode=mode,
        status='queued', source_shot_id=source.id, test_project_id=project.id, test_shot_id=shot.id,
        snapshot_json=snapshot, cases_json=cases, result_json={'comparison_type': 'single_model_parameter_comparison' if mode == 'parameters' else mode, 'formal_project_modified': False})
    session.add(row)
    session.add(m.AuditEvent(object_type='model_experiment', object_id=row.id, action='created_isolated', details_json={'source_shot_id': source.id, 'test_shot_id': shot.id, 'mode': mode, 'input_sha256': snapshot['input_sha256']}))
    session.flush(); return row


def frozen_generation_inputs(session, settings, shot, payload):
    identifiers = payload.parameters
    experiment_id, case_id = identifiers.get('model_lab_experiment_id'), identifiers.get('model_lab_case_id')
    if not experiment_id and not case_id: return None
    experiment = required(session, ModelExperiment, experiment_id)
    case = next((c for c in experiment.cases_json if c['id'] == case_id), None)
    snapshot = copy.deepcopy(experiment.snapshot_json)
    if not case or experiment.test_shot_id != shot.id or experiment.test_project_id != shot.project_id or experiment.status not in {'running', 'paused'} or case['status'] != 'dispatching':
        raise AppError(409, 'lab_frozen_scope_mismatch', '实验冻结输入只能用于当前独立实验镜头及已登记提交case')
    if snapshot.pop('input_sha256', None) != digest(snapshot):
        raise AppError(409, 'lab_snapshot_changed', '实验输入快照已变化，拒绝提交')
    expected = {**case['parameters'], 'requested_mode': 'real', 'model_lab_experiment_id': experiment.id,
        'model_lab_case_id': case_id, 'model_lab_fingerprint': case['fingerprint'], 'model_lab_input_sha256': experiment.snapshot_json['input_sha256']}
    if payload.parameters != expected or payload.service_id != case['service_snapshot']['service_id'] or payload.model_id != case['model_id'] or payload.seed != snapshot['seed']:
        raise AppError(409, 'lab_parameters_changed', '请求参数、服务、种子或模型与冻结case不一致')
    model = required(session, LocalModel, case['model_registry_id'])
    service = required(session, m.AIService, payload.service_id)
    if model.fingerprint != case['fingerprint'] or not _component_unchanged(model) or service.revision != case['service_snapshot']['service_revision']:
        raise AppError(409, 'lab_frozen_model_changed', '模型或服务在实验创建后改变，未自动切换')
    references = []
    for ref in snapshot['references']:
        path = safe_media_path(settings.media_root, ref['relative_path'], must_exist=True)
        if file_sha(path) != ref['sha256']:
            raise AppError(409, 'lab_frozen_reference_changed', '实验冻结参考文件已变化，拒绝提交')
        references.append({**ref, 'absolute_path': str(path)})
    version = required(session, m.CharacterVersion, snapshot['character_version_id'])
    provenance = copy.deepcopy(snapshot['source_snapshot'])
    provenance['model_lab_scope'] = {'experiment_id': experiment.id, 'case_id': case_id, 'input_sha256': experiment.snapshot_json['input_sha256'], 'source_shot_id': experiment.source_shot_id}
    provenance['model_lab_negative_prompt'] = snapshot.get('negative_prompt', '')
    return version, references, snapshot['prompt'], provenance


def default_generation_parameters(session, snapshot):
    if not snapshot: return {}
    row = session.get(ModelLabDefault, 'video_generation')
    config = row.configuration_json if row else {}
    if config.get('service_id') != snapshot.get('service_id') or config.get('model_id') != snapshot.get('model_id') or config.get('workflow_sha256') != snapshot.get('workflow_sha256'):
        return {}
    model = session.get(LocalModel, config.get('model_registry_id'))
    if not model or model.fingerprint != config.get('fingerprint') or not _component_unchanged(model): return {}
    return {k: v for k, v in config.get('parameters', {}).items() if k in {'width', 'height', 'steps'}}


def _real_output(session, settings, experiment, case, job):
    video = session.scalar(select(m.VideoRevision).where(m.VideoRevision.generation_job_id == job.id))
    if not video or job.provider == 'demo' or not job.provider_job_id or job.status not in {'generated', 'succeeded'}:
        raise AppError(409, 'lab_real_output_missing', '尚无真实生成的视频记录')
    if job.parameters_json.get('model_lab_fingerprint') != case['fingerprint'] or job.parameters_json.get('model_lab_input_sha256') != experiment.snapshot_json['input_sha256'] or job.prompt != experiment.snapshot_json['prompt']:
        raise AppError(409, 'lab_output_scope_mismatch', '任务的模型、冻结输入或提示词不匹配')
    if job.seed != experiment.snapshot_json['seed'] or any(job.parameters_json.get(k) != v for k, v in case['parameters'].items()):
        raise AppError(409, 'lab_actual_parameters_mismatch', '实际任务参数或随机种子与冻结case不一致')
    if job.reference_assets_json != experiment.snapshot_json['references']:
        raise AppError(409, 'lab_actual_references_mismatch', '实际送入任务的参考顺序、角色ID或文件SHA与冻结输入不一致')
    service = job.parameters_json.get('service_snapshot', {})
    if service.get('model_id') != case['model_id'] or service.get('service_id') != case['service_snapshot']['service_id'] or service.get('workflow_sha256') != case.get('workflow_sha256'):
        raise AppError(409, 'lab_actual_model_mismatch', '实际生成模型或工作流与实验case不一致')
    decode = session.scalar(select(m.QualityCheck).where(m.QualityCheck.video_revision_id == video.id, m.QualityCheck.check_type == 'media_decode', m.QualityCheck.status == 'passed'))
    details = decode.details_json if decode else {}
    if details.get('full_decode', {}).get('exit_code') != 0:
        raise AppError(409, 'lab_full_decode_required', '完整视频解码证据缺失，不能把模型标记为已验证')
    path = safe_media_path(settings.media_root, video.relative_path, must_exist=True)
    actual_sha = file_sha(path)
    if video.metadata_json.get('sha256') != actual_sha:
        raise AppError(409, 'lab_output_hash_changed', '实际视频文件SHA与生成记录不一致')
    stream = next((s for s in details.get('streams', []) if s.get('codec_type') == 'video'), {})
    if stream.get('width') != case['parameters']['width'] or stream.get('height') != case['parameters']['height']:
        raise AppError(409, 'lab_resolution_mismatch', '实际生成分辨率与实验请求不一致，未判定成功')
    duration = float(details.get('format', {}).get('duration', 0))
    if duration <= 0 or abs(duration - experiment.snapshot_json['duration_seconds']) > .75:
        raise AppError(409, 'lab_duration_mismatch', '实际时长与冻结实验条件不一致')
    elapsed = None
    if job.started_at and job.finished_at: elapsed = (job.finished_at - job.started_at).total_seconds()
    case.update(status='generated', video_id=video.id, relative_path=video.relative_path,
        output_sha256=actual_sha, metadata={**video.metadata_json, 'actual_probe': details, 'elapsed_seconds': elapsed,
        'requested_resolution': {k: case['parameters'][k] for k in ('width', 'height')}, 'processing': 'native_workflow_output',
        'visual_quality': 'NOT RUN; human review required', 'seed': job.seed}, completed_at=utcnow().isoformat())
    model = required(session, LocalModel, case['model_registry_id'])
    if model.fingerprint == case['fingerprint'] and _component_unchanged(model):
        model.verification_json = {'state': 'verified', 'fingerprint': case['fingerprint'], 'job_id': job.id, 'provider_job_id': job.provider_job_id,
            'experiment_id': experiment.id, 'video_id': video.id, 'output_sha256': actual_sha, 'full_decode': details['full_decode'],
            'actual_width': stream['width'], 'actual_height': stream['height'], 'duration_seconds': duration,
            'checked_at': utcnow().isoformat(), 'scope': 'This model/workflow/component stat+header fingerprint and these successful parameters only; no untested resolutions promised'}


async def advance_experiment(app, settings, session, experiment):
    if experiment.status in {'completed', 'completed_with_errors', 'paused', 'cancelled'}: return
    if required(session, m.Project, experiment.test_project_id).status == 'deleted' or required(session, m.StoryboardShot, experiment.test_shot_id).status == 'deleted':
        experiment.status = 'cancelled'; experiment.error_message = '独立实验项目或镜头已删除；未继续提交'; session.commit(); return
    cases = copy.deepcopy(experiment.cases_json)
    experiment.status = 'running'
    for case in cases:
        if case['status'] in {'dispatching', 'running'}:
            job = session.get(m.GenerationJob, case.get('job_id')) if case.get('job_id') else None
            if not job:
                job = next((j for j in session.scalars(select(m.GenerationJob).where(m.GenerationJob.shot_id == experiment.test_shot_id).order_by(m.GenerationJob.created_at.desc())) if j.parameters_json.get('model_lab_case_id') == case['id']), None)
            if not job:
                case.update(status='unknown', error='提交中断但没有可核对的本地任务；未自动重提以避免重复费用')
            else:
                case['job_id'] = job.id
                if job.status in {'generated', 'succeeded'}:
                    try: _real_output(session, settings, experiment, case, job)
                    except AppError as exc: case.update(status='failed', error=exc.message, error_code=exc.code)
                elif job.status in {'failed', 'cancelled', 'unknown'}:
                    case.update(status=job.status, error=job.error_message or job.status, error_code=job.error_code)
                    model = session.get(LocalModel, case['model_registry_id'])
                    if experiment.mode == 'load_test' and model and job.status == 'failed':
                        model.verification_json = {'state': 'failed', 'fingerprint': case['fingerprint'], 'job_id': job.id, 'error': job.error_message, 'error_code': job.error_code, 'checked_at': utcnow().isoformat()}
                else:
                    experiment.cases_json = cases; session.commit(); return
    if any(c['status'] == 'unknown' for c in cases):
        experiment.status = 'completed_with_errors'; experiment.error_message = '含结果未知请求，需核对原任务；不会自动重试'
        experiment.cases_json = cases; session.commit(); return
    pending = next((c for c in cases if c['status'] == 'pending'), None)
    if pending:
        # Give production GPU tasks priority. The provider also checks the real queue.
        busy = session.scalar(select(m.GenerationJob.id).where(m.GenerationJob.status.in_(ACTIVE)).limit(1))
        busy = busy or session.scalar(select(m.CharacterGenerationJob.id).where(m.CharacterGenerationJob.status.in_(ACTIVE)).limit(1))
        busy = busy or session.scalar(select(m.ProductionVisual.id).where(m.ProductionVisual.status.in_(ACTIVE)).limit(1))
        busy = busy or session.scalar(select(m.QualityRender.id).where(
            m.QualityRender.status.in_({'queued', 'running'}),
            m.QualityRender.snapshot_json['ai_super_resolution'].as_boolean().is_(True)).limit(1))
        if busy:
            experiment.cases_json = cases; session.commit(); return
        pending.update(status='dispatching', started_at=utcnow().isoformat())
        experiment.cases_json = cases; session.commit()
        from app.api.routes import generate_for_shot
        parameters = {**pending['parameters'], 'requested_mode': 'real', 'model_lab_experiment_id': experiment.id,
            'model_lab_case_id': pending['id'], 'model_lab_fingerprint': pending['fingerprint'], 'model_lab_input_sha256': experiment.snapshot_json['input_sha256']}
        try:
            job, _ = await generate_for_shot(Request({'type': 'http', 'app': app}), session,
                required(session, m.StoryboardShot, experiment.test_shot_id), GenerationRequest(seed=experiment.snapshot_json['seed'], parameters=parameters,
                    service_id=pending['service_snapshot']['service_id'], model_id=pending['model_id']))
            pending.update(job_id=job.id, status='running')
        except AppError as exc:
            found = next((j for j in session.scalars(select(m.GenerationJob).where(m.GenerationJob.shot_id == experiment.test_shot_id).order_by(m.GenerationJob.created_at.desc())) if j.parameters_json.get('model_lab_case_id') == pending['id']), None)
            pending.update(status='unknown' if found and found.status == 'unknown' else 'failed', error=exc.message, error_code=exc.code)
            if found: pending['job_id'] = found.id
        except Exception as exc:
            pending.update(status='unknown', error='提交结果未知：' + type(exc).__name__ + '；未自动重试')
        session.refresh(experiment, ['status'])
        experiment.cases_json = cases; session.commit(); return
    experiment.status = 'completed' if all(c['status'] == 'generated' for c in cases) else 'completed_with_errors'
    experiment.cases_json = cases
    experiment.result_json = {**experiment.result_json, 'successful_cases': sum(c['status'] == 'generated' for c in cases), 'case_count': len(cases), 'automatic_visual_score': 'NOT RUN', 'source_project_mutated': False}
    session.commit()


async def model_lab_worker(app, settings, session_factory):
    while True:
        try:
            with session_factory() as session:
                experiments = list(session.scalars(select(ModelExperiment).where(ModelExperiment.status.in_(['queued', 'running'])).order_by(ModelExperiment.created_at)))
                for experiment in experiments: await advance_experiment(app, settings, session, experiment)
        except asyncio.CancelledError: raise
        except Exception: logging.getLogger(__name__).exception('Model lab worker failed; durable state retained')
        await asyncio.sleep(2)


def save_default(session, experiment, case_id):
    case = next((c for c in experiment.cases_json if c['id'] == case_id), None)
    if not case or case['status'] != 'generated':
        raise AppError(409, 'lab_default_requires_success', '只有真实生成且完整解码成功的case可设为后续默认配置')
    model = required(session, LocalModel, case['model_registry_id'])
    service = required(session, m.AIService, case['service_snapshot']['service_id'])
    if not model_view(model, session)['eligible_for_comparison'] or model.fingerprint != case['fingerprint'] or not _component_unchanged(model) or not service.enabled or service.revision != case['service_snapshot']['service_revision']:
        raise AppError(409, 'lab_default_stale', '模型或服务已改变，不能将旧实验设为当前默认')
    for other in session.scalars(select(m.AIService).where(m.AIService.module == 'video_generation')):
        other.is_default = other.id == service.id
    service.default_model = case['model_id']
    config = {'experiment_id': experiment.id, 'case_id': case_id, 'model_registry_id': model.id, 'model_id': case['model_id'],
              'service_id': service.id, 'fingerprint': model.fingerprint, 'workflow_sha256': case.get('workflow_sha256'),
              'parameters': case['parameters'], 'saved_at': utcnow().isoformat(), 'explicit_user_action': True}
    row = session.get(ModelLabDefault, 'video_generation')
    if row is None: row = ModelLabDefault(id='video_generation'); session.add(row)
    row.configuration_json = config
    session.add(m.AuditEvent(object_type='model_lab_default', object_id='video_generation', action='explicit_case_selected', details_json=config))
    return config
