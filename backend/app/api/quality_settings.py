from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from app import models as m
from app.core.db import get_db
from app.core.errors import AppError
from app.models.quality import QualityRender,QualityProfile
from app.schemas_quality import QualityProfileInput, QualityProfileDTO, QualityRenderInput, QualityRenderDTO, QualitySettings
from app.services.video_quality import profile, save_profile, create_render, render_dto, plan, MODES, seedvr_capabilities

router=APIRouter(prefix='/api/quality',tags=['quality'])

@router.get('/profiles/{scope}/{target_id}',response_model=QualityProfileDTO)
def get_profile(scope:str,target_id:str,request:Request,session:Session=Depends(get_db)):
    return profile(session,scope,target_id,request.app.state.settings)

@router.put('/profiles/{scope}/{target_id}',response_model=QualityProfileDTO)
def put_profile(scope:str,target_id:str,payload:QualityProfileInput,request:Request,session:Session=Depends(get_db)):
    result=save_profile(session,scope,target_id,payload,request.app.state.settings);session.commit();return result

@router.delete('/profiles/shot/{target_id}',response_model=QualityProfileDTO)
def inherit_shot(target_id:str,request:Request,session:Session=Depends(get_db)):
    profile(session,'shot',target_id)  # validate scope before any deletion
    item=session.scalar(select(QualityProfile).where(QualityProfile.scope=='shot',QualityProfile.target_id==target_id))
    if item:session.delete(item);session.flush()
    session.commit();return profile(session,'shot',target_id,request.app.state.settings)

@router.get('/capabilities')
def capabilities(request:Request,session:Session=Depends(get_db)):
    from app.services.super_resolution import status
    sr=status(request.app.state.settings)
    seed=seedvr_capabilities(session,request.app.state.settings)
    evidence=[]
    for video in session.scalars(select(m.VideoRevision).order_by(m.VideoRevision.created_at.desc())):
        job=session.get(m.GenerationJob,video.generation_job_id)
        if job and not video.metadata_json.get('quality_render_id') and video.metadata_json.get('playable_video'):
            evidence.append({'video_id':video.id,'model_id':job.parameters_json.get('service_snapshot',{}).get('model_id'),
                'service_id':job.parameters_json.get('service_snapshot',{}).get('service_id'),'service_revision':job.parameters_json.get('service_snapshot',{}).get('service_revision'),
                'workflow':job.workflow_version,'width':video.metadata_json.get('width'),'height':video.metadata_json.get('height'),
                'steps':job.parameters_json.get('steps'),'evidence_type':'existing_decoded_generation_output'})
    return {'plans':[plan(QualitySettings(resolution=r),sr) for r in ['720p','1080p','2k','4k']],
        'ai_plans':[plan(QualitySettings(resolution=r,processing='ai_super_resolution'),sr) for r in ['720p','1080p','2k','4k']],
        'seedvr2_plans':[plan(QualitySettings(resolution=r,processing='seedvr2'),sr,seed) for r in ['720p','1080p','2k','4k']],
        'modes':{name:{**mapping,'verification':'参数映射已接入；须按具体模型、服务和工作流核对实测证据',
            'model_specific_verification':[e for e in evidence if e['steps']==mapping['steps']][:20]} for name,mapping in MODES.items()},
        'native_evidence':evidence[:40],'ai_super_resolution':sr,'seedvr2':seed,'reconstruction':{'state':'not_configured'}}

@router.post('/super-resolution/load-test',response_model=QualityRenderDTO,status_code=202)
def test_super_resolution(payload:QualityRenderInput,request:Request,session:Session=Depends(get_db)):
    if payload.settings.processing!='ai_super_resolution' or payload.settings.resolution!='4k':
        raise AppError(422,'super_resolution_test_scope','此明确实测入口仅用于4K AI超分，不替代普通缩放')
    item=create_render(session,request.app.state.settings,payload.video_id,payload.settings,allow_unverified_ai=True)
    session.commit();return render_dto(item)

@router.post('/renders',response_model=QualityRenderDTO,status_code=202)
def render(payload:QualityRenderInput,request:Request,session:Session=Depends(get_db)):
    item=create_render(session,request.app.state.settings,payload.video_id,payload.settings);session.commit();return render_dto(item)


@router.post('/seedvr2/load-test',response_model=QualityRenderDTO,status_code=202)
async def test_seedvr2(payload:QualityRenderInput,request:Request,session:Session=Depends(get_db)):
    if payload.settings.processing!='seedvr2' or payload.settings.resolution not in {'720p','1080p'}:
        raise AppError(422,'seedvr_test_scope','此入口用于本机 SeedVR2 的 720p／1080p 精确画幅完整视频验证，不会更改保存的画质。')
    video=session.get(m.VideoRevision,payload.video_id)
    if not video:raise AppError(404,'video_not_found','验证源视频不存在')
    from app.services.studio_project_guard import project_guard
    from app.services.studio_pipeline import latest_run,_route,_explicit_route
    async with project_guard(request.app,video.project_id):
        session.expire_all();video=session.get(m.VideoRevision,payload.video_id)
        run=latest_run(session,video.project_id)
        route=_route(run,'video_upscale',video.shot_id) if run else None
        # Reuse an explicitly selected native Seed route, never a cloud fallback.
        if not _explicit_route(route) or (route.get('selected') or {}).get('service_snapshot',{}).get('model_id')!='seedvr2_3b_fp8_e4m3fn.safetensors':
            route=None
        item=create_render(session,request.app.state.settings,payload.video_id,payload.settings,
            route_snapshot=route,allow_unverified_seed=True)
        session.commit();return render_dto(item)

@router.get('/renders/{identifier}',response_model=QualityRenderDTO)
def get_render(identifier:str,session:Session=Depends(get_db)):
    item=session.get(QualityRender,identifier)
    if not item:raise AppError(404,'quality_render_missing','处理记录不存在')
    return render_dto(item)


@router.post('/renders/{identifier}/resume-native',response_model=QualityRenderDTO,status_code=202)
async def resume_seedvr(identifier:str,request:Request,session:Session=Depends(get_db)):
    from app.services.seedvr_recovery import resume_native
    from app.services.studio_project_guard import project_guard
    item=session.get(QualityRender,identifier)
    if not item:raise AppError(404,'quality_render_missing','处理记录不存在')
    source=session.get(m.VideoRevision,item.source_video_id)
    if not source:raise AppError(409,'seedvr_resume_source','原视频不存在')
    async with project_guard(request.app,source.project_id):
        session.expire_all();item=resume_native(session,request.app.state.settings,identifier)
        session.commit();return render_dto(item)
