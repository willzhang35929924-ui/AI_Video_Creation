"""Authoring before project creation; generation always runs in durable worker."""
import asyncio
from pathlib import Path
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.core.db import get_db
from app.core.errors import AppError
from app.models.screenplay_generation import ScreenplayGenerationJob, ScreenplayDocument
from app.schemas_screenplay_generation import GenerateScreenplay, ApproveScreenplay, ReviseScreenplay, ExportRetry, RetryScreenplay, CancelScreenplay, ResumeScreenplay, BatchCancelScreenplay, BatchDeleteScreenplay, BatchResumeScreenplay
from app.services import screenplay_generation as service
from app.services.screenplay_generation_media import upload_asset,asset_dto,LIMITS

router=APIRouter(prefix='/api/screenplay',tags=['screenplay-generation'])
_approval_lock=asyncio.Lock()

@router.get('/options')
def options(request:Request,session:Session=Depends(get_db)):
    return service.options(session,request.app.state.settings)

@router.post('/assets',status_code=201)
async def upload(request:Request,file:UploadFile=File(...),input_mode:str=Form(...),source_note:str=Form(''),session:Session=Depends(get_db)):
    if input_mode not in {'image','video'}:raise AppError(422,'screenplay_asset_mode','请选择图片或视频素材')
    if len(source_note)>4000:raise AppError(422,'screenplay_source_note','素材来源说明过长')
    data=await file.read(LIMITS[input_mode]['max_bytes']+1)
    filename=Path((file.filename or 'source').replace('\\','/')).name[:240]
    asset=await asyncio.to_thread(upload_asset,session,request.app.state.settings,input_mode,filename,data,source_note)
    return asset_dto(asset)

@router.post('/jobs',status_code=201)
def create(request:Request,payload:GenerateScreenplay,session:Session=Depends(get_db)):
    job=service.create_generation(session,request.app.state.settings,payload)
    return service.dto(session,job)

@router.post('/capability-tests',status_code=201)
def test(request:Request,payload:GenerateScreenplay,session:Session=Depends(get_db)):
    job=service.create_generation(session,request.app.state.settings,payload,capability_test=True)
    return service.dto(session,job)

@router.post('/batches',status_code=201)
def batches(request:Request,payload:GenerateScreenplay,session:Session=Depends(get_db)):
    if payload.input_mode!='image' or payload.product_mode!='separate' or not payload.product_selections:
        raise AppError(422,'screenplay_batch_mode','分别生成只适用于明确选择的产品图库')
    import hashlib
    from app.models.product_library import Product
    jobs=[];errors=[]
    for selection in payload.product_selections:
        product=session.get(Product,selection.product_id)
        if not product:raise AppError(404,'product_missing','批量生成中的产品不存在')
        name=payload.project_name+' · '+product.name
        if len(name)>200:raise AppError(422,'screenplay_batch_name','批量项目名称加产品名超过200字，请缩短项目备注')
    for selection in payload.product_selections:
        product=session.get(Product,selection.product_id)
        values=payload.model_dump();values.update(product_selections=[selection.model_dump()],product_mode='single',project_name=payload.project_name+' · '+product.name,
            idempotency_key='batch_'+hashlib.sha256((payload.idempotency_key+':'+product.id).encode()).hexdigest())
        try:jobs.append(service.dto(session,service.create_generation(session,request.app.state.settings,GenerateScreenplay.model_validate(values))))
        except AppError as error:
            session.rollback();errors.append({'product_id':product.id,'code':error.code,'error':error.message})
    if not jobs and errors:raise AppError(409,'screenplay_batch_failed','本批未创建生成任务',{'errors':errors})
    return {'jobs':jobs,'errors':errors,'partial':bool(errors),'name_policy':'项目备注 · 产品名称；每份Word及审核项目保留此明确名称'}

@router.get('/jobs')
def listing(limit:int=50,session:Session=Depends(get_db)):
    if not 1<=limit<=100:raise AppError(422,'screenplay_list_limit','列表数量须为1至100')
    return [service.dto(session,item) for item in session.scalars(select(ScreenplayGenerationJob).order_by(ScreenplayGenerationJob.created_at.desc()).limit(limit))]

@router.get('/jobs/{identifier}')
def detail(identifier:str,session:Session=Depends(get_db)):
    return service.dto(session,service.get_job(session,identifier))

@router.post('/jobs/{identifier}/approve')
async def approve(identifier:str,request:Request,payload:ApproveScreenplay,session:Session=Depends(get_db)):
    async with _approval_lock:
        job=service.approve(session,request.app.state.settings,identifier,payload)
        return service.dto(session,job)

@router.post('/jobs/{identifier}/revise',status_code=201)
def revise(identifier:str,request:Request,payload:ReviseScreenplay,session:Session=Depends(get_db)):
    return service.dto(session,service.revise(session,request.app.state.settings,identifier,payload))

@router.post('/jobs/{identifier}/export-retry')
async def export_retry(identifier:str,request:Request,payload:ExportRetry,session:Session=Depends(get_db)):
    async with _approval_lock:
        return service.dto(session,service.retry_export(session,request.app.state.settings,identifier,payload.expected_revision))

@router.post('/jobs/{identifier}/export-new-version')
async def export_new_version(identifier:str,request:Request,payload:ExportRetry,session:Session=Depends(get_db)):
    async with _approval_lock:
        return service.dto(session,service.export_new_version(session,request.app.state.settings,identifier,payload.expected_revision))

@router.post('/jobs/{identifier}/retry',status_code=201)
def retry(identifier:str,request:Request,payload:RetryScreenplay,session:Session=Depends(get_db)):
    return service.dto(session,service.retry_generation(session,request.app.state.settings,identifier,payload))

@router.post('/jobs/batch-cancel',status_code=200)
def batch_cancel(payload:BatchCancelScreenplay,request:Request,session:Session=Depends(get_db)):
    return {'results':service.batch_cancel(session,request.app.state.settings,payload)}

@router.delete('/jobs/batch',status_code=200)
def batch_delete(payload:BatchDeleteScreenplay,request:Request,session:Session=Depends(get_db)):
    return {'results':service.batch_delete(session,request.app.state.settings,payload)}

@router.post('/jobs/batch-resume',status_code=201)
def batch_resume(payload:BatchResumeScreenplay,request:Request,session:Session=Depends(get_db)):
    return {'results':service.batch_resume(session,request.app.state.settings,payload)}

@router.post('/jobs/{identifier}/cancel')
def cancel(identifier:str,payload:CancelScreenplay,session:Session=Depends(get_db)):
    return service.dto(session,service.cancel_generation(session,identifier,payload))

@router.post('/jobs/{identifier}/resume',status_code=201)
def resume(identifier:str,request:Request,payload:ResumeScreenplay,session:Session=Depends(get_db)):
    return service.dto(session,service.resume_generation(session,request.app.state.settings,identifier,payload))

@router.get('/documents/{identifier}/download')
def download(identifier:str,request:Request,session:Session=Depends(get_db)):
    document=session.get(ScreenplayDocument,identifier)
    if not document:raise AppError(404,'screenplay_document_missing','剧本文档不存在')
    job=service.get_job(session,document.job_id)
    document,path=service._check_document(session,request.app.state.settings,job,identifier)
    filename=''.join(c if c not in '/\\\r\n\x00' else '_' for c in job.project_name)[:160]+f'_v{document.version}.docx'
    return FileResponse(path,media_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document',filename=filename)
