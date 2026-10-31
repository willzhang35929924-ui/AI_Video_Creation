"""Project voice preparation, separate from AI generation and service configuration."""
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.schemas_studio_voice import ProjectVoicePreparation, ProjectVoiceSave
from app.services.studio_voice import voice_preparation, save_voice
from app import models as m
from sqlalchemy import select

router = APIRouter(prefix='/api/studio')


@router.get('/projects/{project_id}/voices', response_model=ProjectVoicePreparation)
def voices(project_id: str, request: Request, allow_cloud_costs: bool = False, allow_paid_fallback: bool = False,
           session: Session = Depends(get_db)):
    return voice_preparation(session, request.app.state.settings, project_id, allow_cloud_costs, allow_paid_fallback)


@router.put('/projects/{project_id}/voices/{character_id}', response_model=ProjectVoicePreparation)
def save(project_id: str, character_id: str, payload: ProjectVoiceSave, request: Request,
         allow_cloud_costs: bool = False, allow_paid_fallback: bool = False, session: Session = Depends(get_db)):
    # Serialize optimistic-token checks before reading any mutable group rows.
    # SQLite's deferred reader transaction otherwise permits two callers to
    # observe the same token before either acquires its write lock.
    if session.get_bind().dialect.name == 'sqlite':
        session.connection().exec_driver_sql('BEGIN IMMEDIATE')
    else:
        session.scalar(select(m.Project).where(m.Project.id == project_id).with_for_update())
    result = save_voice(session, request.app.state.settings, project_id, character_id, payload, allow_cloud_costs, allow_paid_fallback)
    session.commit()
    return result
