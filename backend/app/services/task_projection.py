"""Read-only projection of real typed child records; never dispatches work."""
from sqlalchemy import select
from app import models as m
from app.core.errors import AppError
from app.core.secrets import redact
from app.schemas_routing import UnifiedTask

TYPES={'video':m.GenerationJob,'image':m.CharacterGenerationJob,'visual':m.ProductionVisual,
       'quality':m.QualityRender,'assembly':m.AssemblyRender,'media':m.MediaProcessTask}

def state(value):
    if value in {'generated','approved','revised','needs_review','succeeded','completed'}:return 'succeeded'
    if value in {'failed','rejected','exception'}:return 'failed'
    if value in {'unknown'}:return 'unknown'
    if value in {'cancelled'}:return 'cancelled'
    if value in {'blocked','studio_paused','paused'}:return 'blocked'
    if value=='skipped':return 'skipped'
    return 'running' if value in {'running','submitting','submitted','dispatching','cancelling'} else 'queued'

def dto(session,kind,row):
    project=getattr(row,'project_id',None);shot=getattr(row,'shot_id',None)
    snapshot=dict(getattr(row,'snapshot_json',{}) or getattr(row,'parameters_json',{}) or {})
    output=dict(getattr(row,'result_json',{}) or getattr(row,'metadata_json',{}) or {})
    if kind=='video':
        slot='video_generation';output={'videos':[{'id':v.id,'relative_path':v.relative_path,'sha256':v.metadata_json.get('sha256')} for v in session.scalars(select(m.VideoRevision).where(m.VideoRevision.generation_job_id==row.id))]}
    elif kind=='image':
        slot='character_image';batch=session.get(m.CharacterBatch,row.batch_id)
        if batch:
            scope=batch.snapshot_json.get('studio_scope',{});run=session.get(m.StudioRun,scope.get('run_id')) if scope.get('run_id') else None
            project=run.project_id if run else snapshot.get('project_id')
        output={'asset_id':row.result_asset_id}
    elif kind=='visual':slot='keyframe_image' if row.kind=='keyframe' else 'character_image';output={'relative_path':row.relative_path,'sha256':row.sha256}
    elif kind=='quality':
        slot='video_upscale';source=session.get(m.VideoRevision,row.source_video_id)
        if source:project=source.project_id;shot=source.shot_id
    elif kind=='assembly':
        slot='final_assembly';assembly=session.get(m.Assembly,row.assembly_id)
        if assembly:project=assembly.project_id
    else:slot=row.slot
    selected=(snapshot.get('active_route') or snapshot.get('route') or snapshot.get('task_route_snapshot') or {}).get('selected') or {}
    service=output.get('actual_service_snapshot') or snapshot.get('service_snapshot') or selected.get('service_snapshot',{})
    output={**output,**({'relative_path':row.relative_path} if getattr(row,'relative_path',None) else {})}
    error=getattr(row,'error_message',None)
    return UnifiedTask(id=kind+':'+row.id,source_type=kind,source_id=row.id,project_id=project,shot_id=shot,slot=slot,status=state(row.status),
        provider=getattr(row,'provider','') or service.get('provider',''),model_id=service.get('model_id',''),
        snapshot=redact({**snapshot,'source_status':row.status}),output=redact(output),
        error={'message':redact(error),'code':getattr(row,'error_code',None)} if error else None,
        created_at=row.created_at.isoformat() if getattr(row,'created_at',None) else None,
        started_at=row.started_at.isoformat() if getattr(row,'started_at',None) else None,
        finished_at=row.finished_at.isoformat() if getattr(row,'finished_at',None) else None)

def get_task(session,identifier):
    if ':' not in identifier:kind='media';key=identifier
    else:kind,key=identifier.split(':',1)
    row=session.get(TYPES[kind],key) if kind in TYPES else None
    if not row:raise AppError(404,'task_missing','任务不存在')
    return dto(session,kind,row)

def tasks(session,project_id=None,shot_id=None):
    result=[]
    for kind,model in TYPES.items():
        for row in session.scalars(select(model).order_by(model.created_at.desc())):
            item=dto(session,kind,row)
            if project_id and item.project_id!=project_id or shot_id and item.shot_id!=shot_id:continue
            result.append(item)
    return sorted(result,key=lambda t:t.created_at or '',reverse=True)[:500]
