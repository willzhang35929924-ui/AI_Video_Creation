"""Reusable reference sources; all inference runs through persisted jobs."""
from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError
from app.core.paths import safe_media_path
from app.models.reference_video import ReferenceVideo, VideoAnalysis, StyleProfile
from app.schemas_reference_video import (AnalyzeReferenceVideo, CreateStyleProfile, EditStyleProfile,
                                         ReferenceVideoEdit, RetryVideoAnalysis)
from app.services import reference_video_analysis as service

router = APIRouter(prefix='/api/reference-videos', tags=['reference-videos'])


@router.get('')
def list_videos(request: Request, q: str = Query('', max_length=200), category: str = Query('', max_length=100),
                session: Session = Depends(get_db)):
    query = select(ReferenceVideo).where(ReferenceVideo.state == 'active')
    if q.strip():
        query = query.where((ReferenceVideo.name.ilike('%' + q.strip() + '%')) |
                            (ReferenceVideo.notes.ilike('%' + q.strip() + '%')) |
                            (ReferenceVideo.source_note.ilike('%' + q.strip() + '%')))
    if category:
        query = query.where(ReferenceVideo.category == category)
    items = session.scalars(query.order_by(ReferenceVideo.created_at.desc()).limit(500))
    return {'items': [service.video_dto(session, item) for item in items],
            'limits': {'max_bytes': service.MAX_BYTES, 'max_duration_seconds': service.MAX_DURATION,
                       'max_batch': 10, 'max_frames': 24, 'formats': sorted(service.FORMATS)},
            'analysis_version': service.ANALYSIS_VERSION,
            'storage_policy': 'controlled originals retained; no persistent extracted frames or audio'}


@router.post('', status_code=201)
async def upload_videos(request: Request, files: list[UploadFile] = File(...),
                        category: str = Form('', max_length=100), source_note: str = Form('', max_length=2000),
                        usage_scope: str = Form('abstract_style_only', max_length=2000),
                        session: Session = Depends(get_db)):
    if not 1 <= len(files) <= 10:
        raise AppError(422, 'reference_video_batch', '每次请选择1至10条参考视频')
    # Decode is CPU/I/O work and must not block the application event loop.
    import asyncio
    items, errors, duplicates = [], [], []
    for file in files:
        try:
            data = await file.read(service.MAX_BYTES + 1)
            item, duplicate = await asyncio.to_thread(service.import_video, session, request.app.state.settings,
                file.filename or '', data, category=category, source_note=source_note, usage_scope=usage_scope)
            items.append(service.video_dto(session, item))
            if duplicate:
                duplicates.append(item.id)
        except AppError as exc:
            errors.append({'filename': file.filename, 'error': {'code': exc.code, 'message': exc.message}})
        finally:
            await file.close()
    return {'items': items, 'errors': errors, 'duplicate_ids': duplicates}


# Static subroutes precede /{identifier}; profile IDs never resolve as videos.
@router.get('/style-profiles')
def list_profiles(session: Session = Depends(get_db)):
    return {'items': [service.profile_dto(p) for p in session.scalars(select(StyleProfile).order_by(StyleProfile.created_at.desc()).limit(500))]}


@router.post('/style-profiles', status_code=201)
def create_profile(payload: CreateStyleProfile, session: Session = Depends(get_db)):
    return service.profile_dto(service.create_style_profile(session, payload))


@router.get('/style-profiles/{identifier}')
def get_profile(identifier: str, session: Session = Depends(get_db)):
    return service.profile_dto(service.get_style_profile(session, identifier))


@router.patch('/style-profiles/{identifier}')
def edit_profile(identifier: str, payload: EditStyleProfile, session: Session = Depends(get_db)):
    item = service.get_style_profile(session, identifier)
    if item.version != payload.version:
        raise AppError(409, 'style_profile_stale', '风格档案已变化，请刷新')
    if payload.primary_video_id and payload.primary_video_id not in item.video_ids_json:
        raise AppError(422, 'style_primary_source', '主参考必须属于此档案')
    item.name, item.user_notes, item.primary_video_id = payload.name, payload.user_notes, payload.primary_video_id
    analyses = [service.get_analysis(session, aid) for aid in item.analysis_ids_json]
    item.profile_json = service.build_style_profile(analyses, item.primary_video_id)
    item.version += 1
    session.commit()
    return service.profile_dto(item)


@router.get('/analyses/{identifier}')
def analysis(identifier: str, session: Session = Depends(get_db)):
    return service.analysis_dto(service.get_analysis(session, identifier))


@router.post('/analyses/{identifier}/retry', status_code=201)
def retry(identifier: str, payload: RetryVideoAnalysis, request: Request, session: Session = Depends(get_db)):
    return service.analysis_dto(service.retry_analysis(session, request.app.state.settings, identifier, payload))


@router.get('/{identifier}')
def detail(identifier: str, session: Session = Depends(get_db)):
    return service.video_dto(session, service.get_video(session, identifier))


@router.patch('/{identifier}')
def edit(identifier: str, payload: ReferenceVideoEdit, session: Session = Depends(get_db)):
    item = service.get_video(session, identifier, active=True)
    from app.services.motion_references import active_motion_jobs
    if active_motion_jobs(session,identifier):raise AppError(409,'motion_reference_in_use','参考视频正在被生成任务使用，暂不能修改')
    if item.revision != payload.revision:
        raise AppError(409, 'reference_video_stale', '参考视频已变化，请刷新')
    if any(len(tag) > 100 for tag in payload.tags):
        raise AppError(422, 'reference_video_tag', '每个标签不超过100字')
    for field in ('name', 'category', 'notes', 'source_note', 'usage_scope'):
        setattr(item, field, getattr(payload, field))
    item.tags_json = list(dict.fromkeys(tag.strip() for tag in payload.tags if tag.strip()))
    item.revision += 1
    session.commit()
    return service.video_dto(session, item)


@router.get('/{identifier}/references')
def references(identifier: str, session: Session = Depends(get_db)):
    service.get_video(session, identifier)
    return service.references_for_video(session, identifier)


@router.delete('/{identifier}')
def archive(identifier: str, revision: int = Query(ge=1), session: Session = Depends(get_db)):
    return service.archive_video(session, identifier, revision)


@router.get('/{identifier}/analyses')
def analyses(identifier: str, session: Session = Depends(get_db)):
    service.get_video(session, identifier)
    return {'items': [service.analysis_dto(a) for a in session.scalars(select(VideoAnalysis)
        .where(VideoAnalysis.video_id == identifier).order_by(VideoAnalysis.version.desc()))]}


@router.post('/{identifier}/analyses', status_code=201)
def analyze(identifier: str, payload: AnalyzeReferenceVideo, request: Request, session: Session = Depends(get_db)):
    return service.analysis_dto(service.create_analysis(session, request.app.state.settings, identifier, payload))


@router.get('/{identifier}/frames')
def preview_frame(identifier: str, request: Request, timestamp_seconds: float = Query(ge=0, le=service.MAX_DURATION),
                   session: Session = Depends(get_db)):
    item = service.get_video(session, identifier)
    if timestamp_seconds >= item.metadata_json['duration_seconds']:
        raise AppError(422, 'reference_video_timestamp', '时间戳超出视频时长')
    path = safe_media_path(request.app.state.settings.media_root, item.relative_path, must_exist=True)
    if service.file_digest(path) != item.sha256:
        raise AppError(409, 'reference_video_changed', '原视频SHA已变化')
    last_timestamp = max(0, item.metadata_json['duration_seconds'] - max(.1, 1 / max(item.metadata_json.get('fps', 1), 1)))
    data = service.extract_frame(request.app.state.settings, path, min(timestamp_seconds, last_timestamp))
    return Response(data, media_type='image/jpeg', headers={'Cache-Control': 'no-store', 'X-Frame-SHA256': service.digest(data)})
