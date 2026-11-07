import json
import os
from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.routes import audit, required, row_dict
from app.core.db import get_db
from app.core.errors import AppError
from app.core.secrets import protect
from app.models import AIService, Project, ProjectServiceBinding
from app.models.entities import utcnow
from app.schemas_product import BindingRequest, ServiceCreate, ServicePatch
from app.services.ai_services import (MODULES, BUILTIN_VIDEO_WORKFLOWS, normalize_service_settings,
    probe_service, public_service, validate_configuration)

router = APIRouter(prefix='/api')


@router.get('/settings/ai-service-options')
def service_options():
    container = os.getenv('VIDEO_FACTORY_CONTAINER', '').lower() in {'1', 'true', 'yes'}
    host = 'host.docker.internal' if container else '127.0.0.1'
    return {'container': container, 'defaults': {
        'comfyui': f'http://{host}:8189', 'local_llm': f'http://{host}:1234/v1',
        'context_ir': f'http://{host}:8788'}, 'video_workflows': BUILTIN_VIDEO_WORKFLOWS,
        'notice': '模型独立安装并启动；连接测试不下载权重、不生成付费任务。语音和口型固定运行时尚不支持任意HTTP接口。'}


@router.get('/settings/ai-services')
def list_services(module: str | None = None, session: Session = Depends(get_db)):
    query = select(AIService).order_by(AIService.created_at)
    if module:
        query = query.where(AIService.module == module)
    return [public_service(row) for row in session.scalars(query)]


@router.post('/settings/ai-services', status_code=201)
def create_service(payload: ServiceCreate, session: Session = Depends(get_db)):
    if payload.api_key and payload.api_key.get_secret_value() and set(payload.api_key.get_secret_value().strip()) <= {'*','•','●'}:
        raise AppError(422,'masked_key_forbidden','遮罩不是有效密钥')
    options = normalize_service_settings(payload.module, payload.provider, payload.base_url, payload.settings)
    validate_configuration(payload.module, payload.provider, payload.base_url, options)
    if payload.provider == 'ffmpeg' and (payload.default_model or payload.models or payload.api_key):
        raise AppError(422, 'ffmpeg_not_ai', 'FFmpeg 不需要模型或 API Key')
    key = payload.api_key.get_secret_value() if payload.api_key else ''
    row = AIService(module=payload.module, provider=payload.provider, name=payload.name, base_url=payload.base_url.rstrip('/'), enabled=payload.enabled, default_model=payload.default_model, models_json=[{'id': value, 'source': 'manual', 'capability': 'unverified'} for value in payload.models], settings_json=options, encrypted_secret=protect(key) if key else None)
    session.add(row)
    session.flush()
    audit(session, 'ai_service', row.id, 'created', {'module': row.module, 'provider': row.provider, 'has_api_key': bool(row.encrypted_secret)})
    session.commit()
    return public_service(row)


@router.patch('/settings/ai-services/{service_id}')
def patch_service(service_id: str, payload: ServicePatch, session: Session = Depends(get_db)):
    row = required(session, AIService, service_id, 'AI 服务')
    if payload.api_key is not None and payload.api_key.get_secret_value().strip() and set(payload.api_key.get_secret_value().strip()) <= {'*','•','●'}:
        raise AppError(422, 'masked_key_forbidden', '遮罩不是有效密钥；保留原密钥时请留空此字段')
    if payload.clear_api_key and not payload.confirm_clear:
        raise AppError(409, 'clear_confirmation_required', '清除密钥需要明确二次确认')
    options = payload.settings if payload.settings is not None else row.settings_json
    base = payload.base_url if payload.base_url is not None else row.base_url
    options = normalize_service_settings(row.module, row.provider, base, options)
    validate_configuration(row.module, row.provider, base, options)
    if row.provider == 'ffmpeg' and (payload.api_key or payload.default_model or payload.models):
        raise AppError(422, 'ffmpeg_not_ai', 'FFmpeg 不使用密钥或模型')
    for field in ('name', 'enabled', 'default_model'):
        value = getattr(payload, field)
        if value is not None:
            setattr(row, field, value)
    base_changed = base.rstrip('/') != row.base_url
    row.base_url, row.settings_json = base.rstrip('/'), options
    if base_changed:
        row.models_json = [entry for entry in row.models_json if entry.get('source') == 'manual']
    if payload.models is not None:
        discovered = [entry for entry in row.models_json if entry.get('source') != 'manual']
        row.models_json = discovered + [{'id': value, 'source': 'manual', 'capability': 'unverified'} for value in payload.models if value not in {entry['id'] for entry in discovered}]
    if payload.clear_api_key:
        row.encrypted_secret = None
    elif payload.api_key is not None and payload.api_key.get_secret_value():
        row.encrypted_secret = protect(payload.api_key.get_secret_value())
    row.revision += 1
    row.last_test_json = {}
    row.capabilities_json = {}
    audit(session, 'ai_service', row.id, 'updated', {'revision': row.revision, 'key_changed': payload.api_key is not None or payload.clear_api_key})
    session.commit()
    return public_service(row)


@router.post('/settings/ai-services/{service_id}/set-default')
def set_default(service_id: str, session: Session = Depends(get_db)):
    row = required(session, AIService, service_id, 'AI 服务')
    if not row.enabled:
        raise AppError(409, 'service_disabled', '停用服务不能设为默认')
    for other in session.scalars(select(AIService).where(AIService.module == row.module)):
        other.is_default = other.id == row.id
    audit(session, 'ai_service', row.id, 'set_default', {'module': row.module})
    session.commit()
    return public_service(row)


@router.delete('/settings/ai-services/{service_id}')
def disable_service(service_id: str, session: Session = Depends(get_db)):
    row = required(session, AIService, service_id, 'AI 服务')
    row.enabled = row.is_default = False
    row.revision += 1
    audit(session, 'ai_service', row.id, 'disabled', {'historical_snapshots_preserved': True})
    session.commit()
    return public_service(row)


@router.post('/settings/ai-services/{service_id}/test')
@router.post('/settings/ai-services/{service_id}/refresh-models')
async def test_service(request: Request, service_id: str, session: Session = Depends(get_db)):
    row = required(session, AIService, service_id, 'AI 服务')
    result = await probe_service(row, request.app.state.settings)
    row.last_test_json = result
    row.capabilities_json = {**result['capabilities'], **{key: value for key, value in row.capabilities_json.items() if value.get('state') == 'verified'}}
    if result['models']:
        manual = [entry for entry in row.models_json if entry.get('source') == 'manual']
        row.models_json = result['models'] + [entry for entry in manual if entry['id'] not in {model['id'] for model in result['models']}]
    if result['status'] == 'connected':
        row.last_success_at = utcnow()
    audit(session, 'ai_service', row.id, 'connection_test', {'status': result['status'], 'http_status': result['http_status'], 'elapsed_ms': result['elapsed_ms']})
    session.commit()
    return public_service(row)


@router.get('/projects/{project_id}/ai-services')
def project_services(project_id: str, session: Session = Depends(get_db)):
    required(session, Project, project_id, '项目')
    return [row_dict(row) for row in session.scalars(select(ProjectServiceBinding).where(ProjectServiceBinding.project_id == project_id))]


@router.put('/projects/{project_id}/ai-services/{module}')
def set_project_service(project_id: str, module: str, payload: BindingRequest, session: Session = Depends(get_db)):
    required(session, Project, project_id, '项目')
    service = required(session, AIService, payload.service_id, '服务')
    if module not in MODULES or service.module != module or not service.enabled:
        raise AppError(422, 'module_mismatch', '项目覆盖必须选择本模块已启用服务')
    row = session.scalar(select(ProjectServiceBinding).where(ProjectServiceBinding.project_id == project_id, ProjectServiceBinding.module == module))
    if row is None:
        row = ProjectServiceBinding(project_id=project_id, module=module, service_id=service.id)
        session.add(row)
    row.service_id, row.model_id = service.id, payload.model_id
    audit(session, 'project', project_id, 'service_override', {'module': module, 'service_id': service.id, 'model_id': payload.model_id})
    session.commit()
    return row_dict(row)


@router.delete('/projects/{project_id}/ai-services/{module}')
def clear_project_service(project_id: str, module: str, session: Session = Depends(get_db)):
    required(session,Project,project_id,'项目')
    row=session.scalar(select(ProjectServiceBinding).where(ProjectServiceBinding.project_id==project_id,ProjectServiceBinding.module==module))
    if row:session.delete(row)
    audit(session,'project',project_id,'service_override_cleared',{'module':module})
    session.commit()
    return {'module':module,'source':'module_default'}
