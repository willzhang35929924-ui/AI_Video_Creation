import hashlib
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.api.routes import audit, required, row_dict
from app.core.db import get_db
from app.core.errors import AppError
from app.models import Character, CharacterAsset, CharacterBatch, CharacterGenerationJob, CharacterVersion
from app.schemas_product import AnchorRequest, BatchRequest, BatchReview, FactoryCharacterCreate, ProductAssetReview, ProfileUpdate
from app.services.ai_services import service_request
from app.services.character_factory import build_batch_plan, enqueue_retry, update_batch

router = APIRouter(prefix='/api')


@router.post('/character-factory/characters', status_code=201)
def create_factory_character(payload: FactoryCharacterCreate, session: Session = Depends(get_db)):
    profile = payload.profile.model_dump()
    character = Character(id=payload.id, slug=payload.id, display_name=payload.name, description=profile['description'])
    version = CharacterVersion(character_id=payload.id, version=1, identity_json={'profile': profile, 'fixed_identity': profile['description'] + '; ' + '; '.join(profile['fixed_traits']), 'default_clothing': next((item.get('description', '') for item in profile['wardrobes'] if item.get('id') == 'default'), '')})
    session.add(character)
    try:
        session.flush(); session.add(version); session.flush()
        audit(session, 'character', character.id, 'factory_created', {'identity_version_id': version.id, 'age': profile['age']})
        session.commit()
    except IntegrityError:
        session.rollback(); raise AppError(409, 'character_conflict', '人物 ID 已存在，请选择该人物或使用新的稳定 ID')
    return {'character': row_dict(character), 'version': row_dict(version)}


def batch_dict(session, batch):
    result = row_dict(batch)
    result['contact_sheet_url'] = '/api/media/' + batch.contact_sheet_path if batch.contact_sheet_path else None
    jobs = session.scalars(select(CharacterGenerationJob).where(CharacterGenerationJob.batch_id == batch.id).order_by(CharacterGenerationJob.created_at)).all()
    result['jobs'] = []
    for job in jobs:
        item = row_dict(job)
        asset = session.get(CharacterAsset, job.result_asset_id) if job.result_asset_id else None
        item['asset'] = {**row_dict(asset), 'media_url': '/api/media/' + asset.relative_path} if asset else None
        result['jobs'].append(item)
    result['counts'] = {state: sum(job.status == state for job in jobs) for state in {job.status for job in jobs}}
    return result


@router.get('/character-factory/characters')
def character_overview(session: Session = Depends(get_db)):
    result = []
    for character in session.scalars(select(Character).order_by(Character.created_at.desc())):
        versions = session.scalars(select(CharacterVersion).where(CharacterVersion.character_id == character.id).order_by(CharacterVersion.version.desc())).all()
        current = next((version for version in versions if version.id == character.current_approved_version_id), versions[0] if versions else None)
        assets = session.scalars(select(CharacterAsset).where(CharacterAsset.character_version_id.in_([version.id for version in versions]))).all() if versions else []
        anchor = session.get(CharacterAsset, current.standard_face_asset_id) if current and current.standard_face_asset_id else next((asset for asset in assets if current and asset.character_version_id == current.id and asset.is_primary_reference and asset.status == 'approved'), None)
        batches = session.scalars(select(CharacterBatch).where(CharacterBatch.character_id == character.id).order_by(CharacterBatch.created_at.desc())).all()
        active = next((batch for batch in batches if batch.status in ('queued', 'running', 'paused')), None)
        status = 'generating' if active and not active.paused else 'approved' if current and current.approval_state == 'approved' else 'needs_review' if any(asset.status in ('needs_review', 'pending') for asset in assets) else 'awaiting_anchor' if current and not anchor else 'draft'
        if status=='approved' and current.identity_json.get('studio_acceptance',{}).get('actor')=='automation' and character.current_approved_version_id!=current.id:
            status='technical_accepted'
        result.append({**row_dict(character), 'factory_status': status, 'approved_assets': sum(asset.status == 'approved' for asset in assets), 'pending_assets': sum(asset.status in ('pending', 'needs_review') for asset in assets), 'asset_count': len(assets), 'identity_version': current.version if current else None, 'version_id': current.id if current else None, 'avatar_url': '/api/media/' + anchor.relative_path if anchor else None, 'latest_batch_status': batches[0].status if batches else None})
    return result


@router.patch('/characters/{character_id}/profile')
def update_profile(character_id: str, payload: ProfileUpdate, session: Session = Depends(get_db)):
    version = required(session, CharacterVersion, payload.version_id, '身份版本')
    if version.character_id != character_id or version.approval_state == 'approved':
        raise AppError(409, 'profile_locked', '请选择本人物的未发布版本；正式身份不可覆盖')
    profile = payload.profile.model_dump()
    if version.anchor_locked_at:
        old = version.identity_json.get('profile', {})
        fixed_keys = ('age', 'description', 'facial_features', 'hair', 'eyes', 'skin', 'identifying_marks', 'body', 'fixed_traits')
        if any(old.get(key) != profile.get(key) for key in fixed_keys):
            raise AppError(409, 'anchor_identity_locked', '身份锚点已锁定；固定特征变化必须创建新版本')
    version.identity_json = {**version.identity_json, 'profile': profile, 'fixed_identity': profile['description'] + '; ' + '; '.join(profile['fixed_traits']), 'default_clothing': next((outfit.get('description', '') for outfit in profile['wardrobes'] if outfit.get('id') == 'default'), '')}
    audit(session, 'character_version', version.id, 'structured_profile_saved', {'character_id': character_id, 'version': version.version})
    session.commit()
    return row_dict(version)


@router.post('/characters/{character_id}/batch-preview')
def preview_batch(request: Request, character_id: str, payload: BatchRequest, session: Session = Depends(get_db)):
    return build_batch_plan(session, request.app.state.settings, character_id, payload)


@router.post('/characters/{character_id}/batch-generate', status_code=202)
def submit_batch(request: Request, character_id: str, payload: BatchRequest, session: Session = Depends(get_db)):
    plan = build_batch_plan(session, request.app.state.settings, character_id, payload)
    if not payload.confirm or payload.preview_hash != plan['preview_hash']:
        raise AppError(409, 'preview_confirmation_required', '请审核当前完整任务预览并确认，参数变化后需重新预览')
    batch = CharacterBatch(character_id=character_id, character_version_id=payload.version_id, kind=payload.kind, snapshot_json=plan)
    session.add(batch); session.flush()
    for task in plan['jobs']:
        session.add(CharacterGenerationJob(batch_id=batch.id, asset_type=task['asset_type'], prompt=task['prompt'], negative_prompt=task['negative_prompt'], seed=task['seed'], snapshot_json={key: value for key, value in plan.items() if key not in ('jobs', 'preview_hash')} | {'type': task['type'], 'attempt': 0}))
    audit(session, 'character_batch', batch.id, 'confirmed_and_queued', {'count': len(plan['jobs']), 'service_id': plan['service_snapshot']['service_id'], 'reference_asset_id': plan['reference']['id'] if plan['reference'] else None})
    session.commit()
    return batch_dict(session, batch)


@router.get('/characters/{character_id}/batches')
def list_batches(character_id: str, session: Session = Depends(get_db)):
    return [batch_dict(session, batch) for batch in session.scalars(select(CharacterBatch).where(CharacterBatch.character_id == character_id).order_by(CharacterBatch.created_at.desc()))]


@router.get('/character-generation-jobs/{batch_id}')
def get_batch(batch_id: str, session: Session = Depends(get_db)):
    return batch_dict(session, required(session, CharacterBatch, batch_id, '素材批次'))


@router.post('/character-generation-jobs/{batch_id}/pause')
def pause_batch(batch_id: str, session: Session = Depends(get_db)):
    batch = required(session, CharacterBatch, batch_id, '素材批次'); batch.paused = True
    audit(session, 'character_batch', batch.id, 'paused_pending_only', {'running_tasks_continue': True}); session.commit()
    return batch_dict(session, batch)


@router.post('/character-generation-jobs/{batch_id}/resume')
def resume_batch(batch_id: str, session: Session = Depends(get_db)):
    batch = required(session, CharacterBatch, batch_id, '素材批次'); batch.paused = False
    audit(session, 'character_batch', batch.id, 'resumed'); session.commit()
    return batch_dict(session, batch)


@router.post('/character-image-jobs/{job_id}/cancel')
async def cancel_item(request: Request, job_id: str, session: Session = Depends(get_db)):
    job = required(session, CharacterGenerationJob, job_id, '素材任务')
    if job.status == 'queued':
        job.status = 'cancelled'
    elif job.status in ('submitted', 'running') and job.snapshot_json['service_snapshot']['provider'] == 'comfyui':
        snapshot = job.snapshot_json['service_snapshot']
        queue = (await service_request(request.app.state.settings, snapshot, 'GET', '/queue')).json()
        if any(row[1] == job.provider_job_id for row in queue.get('queue_running', [])):
            response = await service_request(request.app.state.settings, snapshot, 'POST', '/interrupt', json={'prompt_id': job.provider_job_id})
        else:
            response = await service_request(request.app.state.settings, snapshot, 'POST', '/queue', json={'delete': [job.provider_job_id]})
        response.raise_for_status(); job.status = 'cancelling'
    else:
        raise AppError(409, 'cannot_confirm_cancel', '任务已结束或服务没有已验证的取消接口；未宣称任务停止')
    audit(session, 'character_generation_job', job.id, 'cancel_requested', {'actual_status': job.status}); session.commit()
    return row_dict(job)


@router.post('/character-generation-jobs/{batch_id}/cancel')
async def cancel_batch(request: Request, batch_id: str, session: Session = Depends(get_db)):
    batch = required(session, CharacterBatch, batch_id, '素材批次'); batch.paused = True
    outcomes = []
    for job in session.scalars(select(CharacterGenerationJob).where(CharacterGenerationJob.batch_id == batch.id, CharacterGenerationJob.status.in_(['queued', 'submitted', 'running']))):
        try:
            outcomes.append(await cancel_item(request, job.id, session))
        except AppError as exc:
            outcomes.append({'id': job.id, 'status': job.status, 'error': exc.message})
    session.commit()
    return {'batch': batch_dict(session, batch), 'outcomes': outcomes}


@router.post('/character-generation-jobs/{batch_id}/retry')
def retry_batch(batch_id: str, session: Session = Depends(get_db)):
    batch = required(session, CharacterBatch, batch_id, '素材批次')
    for job in session.scalars(select(CharacterGenerationJob).where(CharacterGenerationJob.batch_id == batch.id, CharacterGenerationJob.status.in_(['failed', 'cancelled']))).all():
        enqueue_retry(session, job)
    session.commit()
    return batch_dict(session, batch)


@router.post('/character-image-jobs/{job_id}/reconcile')
async def reconcile_item(request: Request, job_id: str, session: Session = Depends(get_db)):
    job = required(session, CharacterGenerationJob, job_id, '素材任务')
    if job.status != 'failed' or not job.provider_job_id or job.snapshot_json['service_snapshot']['provider'] != 'comfyui':
        raise AppError(409, 'not_reconcilable', '仅可重新核对有 ComfyUI 任务 ID 的失败任务')
    snapshot = job.snapshot_json['service_snapshot']
    from app.services.character_factory import credential_for
    response = await service_request(request.app.state.settings, snapshot, 'GET', '/history/' + job.provider_job_id, credential_for(session, snapshot))
    response.raise_for_status()
    actual = response.json().get(job.provider_job_id)
    if not actual or actual.get('status', {}).get('status_str') != 'success':
        raise AppError(409, 'completed_history_missing', '实际历史尚无成功记录，未更改任务状态或重新提交')
    job.status, job.error_message = 'submitted', '已找到真实成功历史，等待工作器校验并导入输出'
    audit(session, 'character_generation_job', job.id, 'actual_history_reconciled', {'provider_job_id':job.provider_job_id})
    session.commit()
    return row_dict(job)


@router.post('/character-assets/{asset_id}/regenerate', status_code=202)
def regenerate_asset(asset_id: str, session: Session = Depends(get_db)):
    asset = required(session, CharacterAsset, asset_id, '素材')
    version = required(session, CharacterVersion, asset.character_version_id, '版本')
    if version.approval_state == 'approved' or asset.id == version.standard_face_asset_id:
        raise AppError(409, 'locked_asset', '正式版本或已锁定标准脸不能替换，请创建新身份版本')
    previous = required(session, CharacterGenerationJob, asset.metadata_json.get('generation_job_id'), '原始生成任务')
    if version.anchor_locked_at and previous.snapshot_json.get('kind')=='candidates':
        raise AppError(409,'candidate_phase_closed','标准脸已锁定；新的正面素材应通过参考图批次生成，不能重放无参考的候选任务')
    created = enqueue_retry(session, previous); session.commit()
    return row_dict(created)


def review_one(session, asset_id, payload):
    asset = required(session, CharacterAsset, asset_id, '素材')
    version = required(session, CharacterVersion, asset.character_version_id, '版本')
    if version.approval_state == 'approved' or version.standard_face_asset_id == asset.id:
        raise AppError(409, 'locked_asset', '正式版本与身份锚点不可改变；请创建新版本')
    asset.status, asset.review_reason, asset.reviewed_by = payload.status, payload.reason, payload.operator
    job = session.get(CharacterGenerationJob,asset.metadata_json.get('generation_job_id')) if asset.metadata_json.get('generation_job_id') else None
    if job and job.result_asset_id==asset.id:
        job.status='needs_review' if payload.status=='needs_review' else 'completed'
    asset.metadata_json = {**asset.metadata_json, 'review_checks': payload.checks, 'reviewed_at': datetime.now(timezone.utc).isoformat()}
    audit(session, 'character_asset', asset.id, 'visual_review', {'status': payload.status, 'reason': payload.reason, 'operator': payload.operator})
    return asset


@router.post('/character-assets/{asset_id}/review')
def review_asset(asset_id: str, payload: ProductAssetReview, session: Session = Depends(get_db)):
    asset = review_one(session, asset_id, payload); session.commit()
    return row_dict(asset)


@router.post('/character-assets/batch-review')
def review_assets(payload: BatchReview, session: Session = Depends(get_db)):
    assets = [review_one(session, identifier, payload.review) for identifier in payload.asset_ids]
    if len({asset.character_version_id for asset in assets}) != 1:
        raise AppError(422, 'mixed_identity_versions', '批量审核不能混合不同人物版本')
    session.commit()
    return [row_dict(asset) for asset in assets]


@router.post('/characters/{character_id}/standard-face')
def standard_face(character_id: str, payload: AnchorRequest, session: Session = Depends(get_db)):
    asset = required(session, CharacterAsset, payload.asset_id, '标准脸素材')
    version = required(session, CharacterVersion, asset.character_version_id, '身份版本')
    if version.character_id != character_id or asset.status != 'approved' or asset.asset_type != 'front_face':
        raise AppError(409, 'approved_front_face_required', '必须选择本人物版本中人工批准的正脸图片')
    if version.standard_face_asset_id or version.approval_state == 'approved':
        raise AppError(409, 'anchor_locked', '身份锚点已锁定；更换必须创建新身份版本')
    if not payload.confirm_anchor_lock:
        raise AppError(409, 'anchor_confirmation_required', '请明确确认锁定身份锚点')
    for other in session.scalars(select(CharacterAsset).where(CharacterAsset.character_version_id == version.id)):
        other.is_primary_reference = other.id == asset.id
    version.standard_face_asset_id, version.anchor_locked_at = asset.id, datetime.now(timezone.utc)
    audit(session, 'character_version', version.id, 'standard_face_locked', {'asset_id': asset.id, 'sha256': asset.sha256, 'operator': payload.operator, 'reason': payload.reason})
    session.commit()
    return row_dict(version)
