"""Local reusable voice library. No cloud upload or implicit cloning."""
import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError
from app.schemas_voice_library import VoiceLibraryBind, VoiceLibraryImport, VoiceLibraryPatch
from app.schemas_studio_voice import ProjectVoicePreparation
from app.services import voice_library as library

router = APIRouter(prefix='/api/voice-library', tags=['voice-library'])


def _write_lock(session):
    if session.get_bind().dialect.name == 'sqlite':
        session.connection().exec_driver_sql('BEGIN IMMEDIATE')


def _failure(filename, error):
    if isinstance(error, AppError):
        return {'filename': filename, 'code': error.code, 'message': error.message}
    raise error


@router.get('')
def overview(request: Request, q: str = '', tag: str | None = None, language: str | None = None,
             session: Session = Depends(get_db)):
    return library.listing(session, request.app.state.settings, q, tag, language)


@router.post('/uploads', status_code=201)
async def upload(request: Request, files: list[UploadFile] = File(...), name: str | None = Form(None),
                 language: str = Form('und'), tags: str = Form('[]'), source_note: str = Form(''),
                 reference_authorized: bool = Form(False), session: Session = Depends(get_db)):
    if not 1 <= len(files) <= library.MAX_BATCH_FILES:
        raise AppError(422, 'voice_batch_size', '每批可导入1至20条录音')
    try:
        parsed_tags = json.loads(tags) if tags.strip().startswith('[') else [tag.strip() for tag in tags.split(',') if tag.strip()]
        if not isinstance(parsed_tags, list):
            raise ValueError()
        metadata = VoiceLibraryImport(paths=['upload'], name=name, language=language, tags=parsed_tags,
            source_note=source_note, reference_authorized=reference_authorized).model_dump(exclude={'paths'})
    except (ValueError, TypeError):
        raise AppError(422, 'voice_metadata_invalid', '音频元数据无效；标签需为JSON数组或逗号分隔文字')
    items, duplicates, failures = [], [], []
    for file in files:
        data = await file.read(library.MAX_FILE_BYTES + 1)
        try:
            with session.begin_nested():
                item, duplicate = await asyncio.to_thread(library.import_bytes, session, request.app.state.settings,
                    file.filename or '', data, **metadata)
                items.append(item)
                if duplicate:
                    duplicates.append(item.id)
            session.commit()
        except AppError as error:
            session.rollback()
            failures.append(_failure(file.filename or '', error))
    session.commit()
    services = library.compatible_services(session, request.app.state.settings)
    return {'items': [library.view(session, request.app.state.settings, item, services) for item in items],
            'duplicates': duplicates, 'failures': failures}


@router.post('/imports', status_code=201)
def import_local(payload: VoiceLibraryImport, request: Request, session: Session = Depends(get_db)):
    items, duplicates, failures = [], [], []
    metadata = payload.model_dump(exclude={'paths'})
    for path in payload.paths:
        try:
            with session.begin_nested():
                item, duplicate = library.import_path(session, request.app.state.settings, path, **metadata)
                items.append(item)
                if duplicate:
                    duplicates.append(item.id)
            session.commit()
        except AppError as error:
            session.rollback()
            failures.append(_failure(Path(path).name, error))
    session.commit()
    services = library.compatible_services(session, request.app.state.settings)
    return {'items': [library.view(session, request.app.state.settings, item, services) for item in items],
            'duplicates': duplicates, 'failures': failures}


@router.get('/{identifier}')
def detail(identifier: str, request: Request, session: Session = Depends(get_db)):
    return library.view(session, request.app.state.settings, library.get_asset(session, identifier))


@router.patch('/{identifier}')
def patch(identifier: str, payload: VoiceLibraryPatch, request: Request, session: Session = Depends(get_db)):
    _write_lock(session)
    result = library.update(session, request.app.state.settings, identifier, payload)
    session.commit()
    return result


@router.get('/{identifier}/references')
def references(identifier: str, session: Session = Depends(get_db)):
    return library.references(session, identifier)


@router.post('/{identifier}/bind', response_model=ProjectVoicePreparation)
def bind(identifier: str, payload: VoiceLibraryBind, request: Request, session: Session = Depends(get_db)):
    _write_lock(session)
    result = library.bind(session, request.app.state.settings, identifier, payload)
    session.commit()
    return result


@router.post('/{identifier}/reanalyze')
def reanalyze(identifier:str,request:Request,session:Session=Depends(get_db)):
    return library.reanalyze(session,request.app.state.settings,identifier)

@router.delete('/{identifier}')
def delete(identifier: str, mode: str = 'remove', session: Session = Depends(get_db)):
    library.get_asset(session, identifier)
    raise AppError(409, 'voice_delete_preview_required', '请先在内容管理中预览引用与文件影响，再确认删除或仅解除关联',
                   {'resource': 'voice_library', 'id': identifier, 'mode': mode,
                    'references': library.references(session, identifier)})
