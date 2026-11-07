"""Optimistic, idempotent user repair commands; no direct model calls."""
from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models as m
from app.core.db import get_db
from app.schemas_studio_audio_resolution import AudioDurationApply, AudioDurationChoice
from app.services import studio_audio_resolution as service
from app.services.studio_project_guard import project_guard

router = APIRouter(prefix='/api/studio')


@router.get('/projects/{project_id}/audio-duration/{node_id}')
def inspect(project_id: str, node_id: str, request: Request, session: Session = Depends(get_db)):
    return service.inspect(session, request.app.state.settings, project_id, node_id)


@router.post('/projects/{project_id}/audio-duration/{node_id}/preview')
def preview(project_id: str, node_id: str, payload: AudioDurationChoice, request: Request,
            session: Session = Depends(get_db)):
    return service.preview(session, request.app.state.settings, project_id, node_id, payload)


@router.post('/projects/{project_id}/audio-duration/{node_id}/apply')
async def apply(project_id: str, node_id: str, payload: AudioDurationApply, request: Request,
                session: Session = Depends(get_db)):
    async with project_guard(request.app, project_id):
        try:
            session.expire_all()
            if session.get_bind().dialect.name == 'sqlite':
                session.connection().exec_driver_sql('BEGIN IMMEDIATE')
            else:
                session.scalar(select(m.Project).where(m.Project.id == project_id).with_for_update())
            result = service.apply(session, request.app.state.settings, project_id, node_id, payload)
            session.commit()
            return result
        except Exception:
            session.rollback()
            raise
