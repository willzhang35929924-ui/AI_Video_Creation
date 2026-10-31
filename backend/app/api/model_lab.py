"""Explicit local discovery and isolated real experiment actions."""
import copy
from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.core.db import get_db
from app.core.errors import AppError
from app.models import AuditEvent
from app.models.model_lab import LocalModel, ModelExperiment
from app.schemas_model_lab import (ModelDirectoryInput, ModelDirectoryView, ModelScanInput, LocalModelView,
    ModelExperimentInput, ModelExperimentView, ModelLoadTestInput, ModelLabOverview, ModelCaseReviewInput, ModelDefaultInput)
from app.services import model_lab as lab

router = APIRouter(prefix='/api/model-lab')


@router.get('', response_model=ModelLabOverview)
def overview(session: Session = Depends(get_db)):
    return lab.overview(session)


@router.get('/directories', response_model=list[ModelDirectoryView])
def directories(session: Session = Depends(get_db)):
    return lab.overview(session)['directories']


@router.post('/directories', response_model=ModelDirectoryView, status_code=201)
def directory(payload: ModelDirectoryInput, session: Session = Depends(get_db)):
    row = lab.add_directory(session, payload.path, payload.label)
    session.commit()
    return {'id': row.id, 'path': row.path, 'label': row.label, 'enabled': row.enabled, 'scan': row.scan_json}


@router.post('/scan', response_model=ModelLabOverview)
async def scan(payload: ModelScanInput, request: Request, session: Session = Depends(get_db)):
    result = await lab.scan_models(session, request.app.state.settings, payload.directory_ids)
    session.commit(); return result


@router.get('/models', response_model=list[LocalModelView])
def models(session: Session = Depends(get_db)):
    return lab.overview(session)['models']


@router.post('/models/{model_id}/load-test', response_model=ModelExperimentView, status_code=202)
def load_test(model_id: str, payload: ModelLoadTestInput, request: Request, session: Session = Depends(get_db)):
    row = lab.create_experiment(session, request.app.state.settings, payload, load_model_id=model_id)
    session.commit(); return lab.experiment_view(row)


@router.post('/experiments', response_model=ModelExperimentView, status_code=202)
def create(payload: ModelExperimentInput, request: Request, session: Session = Depends(get_db)):
    row = lab.create_experiment(session, request.app.state.settings, payload)
    session.commit(); return lab.experiment_view(row)


@router.get('/experiments', response_model=list[ModelExperimentView])
def experiments(session: Session = Depends(get_db)):
    return lab.overview(session)['experiments']


@router.get('/experiments/{experiment_id}', response_model=ModelExperimentView)
def experiment(experiment_id: str, session: Session = Depends(get_db)):
    return lab.experiment_view(lab.required(session, ModelExperiment, experiment_id))


@router.post('/experiments/{experiment_id}/pause', response_model=ModelExperimentView)
def pause(experiment_id: str, session: Session = Depends(get_db)):
    row = lab.required(session, ModelExperiment, experiment_id)
    if row.status not in {'queued', 'running'}:
        raise AppError(409, 'lab_not_active', '只有等待或执行中的实验可暂停；已受理任务继续保留真实状态')
    row.status = 'paused'; session.commit(); return lab.experiment_view(row)


@router.post('/experiments/{experiment_id}/resume', response_model=ModelExperimentView)
def resume(experiment_id: str, session: Session = Depends(get_db)):
    row = lab.required(session, ModelExperiment, experiment_id)
    if row.status != 'paused' or any(c['status'] == 'unknown' for c in row.cases_json):
        raise AppError(409, 'lab_resume_blocked', '只有已暂停且无结果未知请求的实验可继续；不会自动重复计费')
    row.status = 'queued'; session.commit(); return lab.experiment_view(row)


@router.post('/experiments/{experiment_id}/cases/{case_id}/review', response_model=ModelExperimentView)
def review(experiment_id: str, case_id: str, payload: ModelCaseReviewInput, session: Session = Depends(get_db)):
    row = lab.required(session, ModelExperiment, experiment_id)
    cases = copy.deepcopy(row.cases_json)
    case = next((c for c in cases if c['id'] == case_id), None)
    if not case or case['status'] != 'generated':
        raise AppError(409, 'lab_review_requires_output', '仅能评价本实验已真实生成并解码的视频')
    if payload.best:
        for other in cases:
            other['review'] = {**other.get('review', {}), 'best': False}
    case['review'] = {'source': 'human', 'status': 'REVIEWED', **payload.model_dump()}
    row.cases_json = cases
    session.add(AuditEvent(object_type='model_experiment', object_id=row.id, action='human_quality_review', details_json={'case_id': case_id, **case['review']}))
    session.commit(); return lab.experiment_view(row)


@router.post('/experiments/{experiment_id}/cases/{case_id}/set-default')
def set_default(experiment_id: str, case_id: str, payload: ModelDefaultInput, session: Session = Depends(get_db)):
    row = lab.required(session, ModelExperiment, experiment_id)
    result = lab.save_default(session, row, case_id)
    session.commit(); return result
