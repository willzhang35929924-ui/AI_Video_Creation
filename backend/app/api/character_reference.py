from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session
from app import models as m
from app.core.db import get_db
from app.schemas_character_reference import CharacterReferenceSave, ReferencePurpose
from app.services.character_reference import MAX_UPLOAD_BYTES, public_asset, reference_overview, save_references, upload_reference

router = APIRouter(prefix='/api/studio/projects')


@router.get('/{project_id}/character-references')
def overview(project_id: str, request: Request, session: Session = Depends(get_db)):
    return reference_overview(session, request.app.state.settings, project_id)


@router.post('/{project_id}/character-references/{character_id}/uploads', status_code=201)
async def upload(project_id: str, character_id: str, request: Request, file: UploadFile = File(...),
        purpose: ReferencePurpose = Form('identity_front'), source_note: str = Form('', max_length=2000),
        session: Session = Depends(get_db)):
    settings = request.app.state.settings
    data = await file.read(min(settings.import_max_bytes, MAX_UPLOAD_BYTES) + 1)
    asset = upload_reference(session, settings, project_id, character_id, file.filename or '', data, purpose, source_note)
    session.commit()
    return public_asset(asset)


@router.put('/{project_id}/character-references/{character_id}')
def save(project_id: str, character_id: str, payload: CharacterReferenceSave, request: Request,
        session: Session = Depends(get_db)):
    if session.get_bind().dialect.name == 'sqlite':
        session.connection().exec_driver_sql('BEGIN IMMEDIATE')
    else:
        session.scalar(select(m.Project).where(m.Project.id == project_id).with_for_update())
    result = save_references(session, request.app.state.settings, project_id, character_id, payload)
    session.commit()
    return result
