import hashlib
import json
import asyncio
from pathlib import Path
from fastapi import APIRouter, Depends, Request, UploadFile, File
from fastapi.responses import FileResponse
from sqlalchemy import select, func, Integer, Float, JSON
from sqlalchemy.orm import Session
from pydantic import ValidationError
from app.core.config import PROJECT_ROOT
from app.core.db import get_db
from app.core.errors import AppError
from app import models as m
from app.schemas_screenplay import (SceneInput, ParticipantAdd, ParticipantPatch, LineInput, AudioInput,
    ShotDetailInput, ImportPreview, ImportConfirm, DeletePreview, DeleteExecute)
from app.services.screenplay import (row, get, shot_bundle, touch_shot, add_participants,
    validate_participant, remove_participant, PERFORMANCE_FIELDS)
from app.services.word_screenplay import (read_blocks, parse_blocks, match_characters, validate_preview,
    summary, commit_import)
from app.services.content_manager import (RESOURCES, public_record, build_plan, public_plan, execute_delete,
    task_public, file_outbox, restore_task, safe_file, label)

router=APIRouter(prefix='/api')


@router.get('/projects/{project_id}/scenes')
def scenes(project_id:str,session:Session=Depends(get_db)):
    get(session,m.Project,project_id)
    return [row(x) for x in session.scalars(select(m.Scene).where(m.Scene.project_id==project_id).order_by(m.Scene.order))]


@router.post('/projects/{project_id}/scenes',status_code=201)
def create_scene(project_id:str,payload:SceneInput,session:Session=Depends(get_db)):
    get(session,m.Project,project_id)
    from app.api.routes import latest_script
    script=get(session,m.ScriptRevision,payload.script_revision_id) if payload.script_revision_id else latest_script(session,project_id)
    if script and script.project_id!=project_id:raise AppError(422,'script_mismatch','剧本版本不属于此项目')
    item=m.Scene(project_id=project_id,script_revision_id=script.id if script else None,name=payload.name,order=payload.order,environment_json=payload.environment)
    session.add(item);session.commit();return row(item)


@router.patch('/scenes/{scene_id}')
def patch_scene(scene_id:str,payload:SceneInput,session:Session=Depends(get_db)):
    item=get(session,m.Scene,scene_id)
    for detail in session.scalars(select(m.ShotDetail).where(m.ShotDetail.scene_id==scene_id)):touch_shot(session,detail.shot_id)
    item.name,item.order,item.environment_json=payload.name,payload.order,payload.environment
    session.commit();return row(item)


@router.get('/shots/{shot_id}/workspace')
def workspace(shot_id:str,session:Session=Depends(get_db)):
    result=shot_bundle(session,shot_id);session.commit();return result


@router.put('/shots/{shot_id}/details')
def details(shot_id:str,payload:ShotDetailInput,session:Session=Depends(get_db)):
    shot=touch_shot(session,shot_id)
    if payload.scene_id and get(session,m.Scene,payload.scene_id).project_id!=shot.project_id:raise AppError(422,'scene_mismatch','场景不属于当前项目')
    item=session.scalar(select(m.ShotDetail).where(m.ShotDetail.shot_id==shot_id))
    if not item:item=m.ShotDetail(shot_id=shot_id);session.add(item)
    previous=session.get(m.Scene,item.scene_id) if item.scene_id else None
    target=session.get(m.Scene,payload.scene_id) if payload.scene_id else None
    if previous and target and previous.script_revision_id and target.script_revision_id and previous.script_revision_id!=target.script_revision_id:
        raise AppError(422,'scene_revision_mismatch','场景属于另一剧本版本，请为当前修订创建场景副本')
    item.scene_id,item.photography_json=payload.scene_id,payload.photography
    session.commit();return row(item)


@router.post('/shots/{shot_id}/participants')
def add_people(shot_id:str,payload:ParticipantAdd,session:Session=Depends(get_db)):
    result=add_participants(session,shot_id,payload.character_ids);session.commit();return result


@router.post('/shots/{shot_id}/duplicate',status_code=201)
def duplicate_shot(shot_id:str,session:Session=Depends(get_db)):
    old=get(session,m.StoryboardShot,shot_id)
    values={k:v for k,v in row(old).items() if k not in ('id','created_at','updated_at','status','approved_at','rejection_reason','revision')}
    values.update(order=(session.scalar(select(func.max(m.StoryboardShot.order)).where(m.StoryboardShot.project_id==old.project_id)) or 0)+1,scene_title=old.scene_title+'（副本）')
    shot=m.StoryboardShot(**values);session.add(shot);session.flush()
    old_detail=session.scalar(select(m.ShotDetail).where(m.ShotDetail.shot_id==old.id))
    if old_detail:session.add(m.ShotDetail(shot_id=shot.id,scene_id=old_detail.scene_id,photography_json=old_detail.photography_json,source_json={'copied_from':old.id}))
    mapping={}
    for p in session.scalars(select(m.ShotCharacter).where(m.ShotCharacter.shot_id==old.id)):
        item=m.ShotCharacter(shot_id=shot.id,character_id=p.character_id,character_version_id=p.character_version_id,order=p.order,reference_asset_ids_json=p.reference_asset_ids_json,performance_json=p.performance_json,source_json={'copied_from':p.id})
        session.add(item);session.flush();mapping[p.id]=item.id
    for line in session.scalars(select(m.DialogueLine).where(m.DialogueLine.shot_id==old.id)):
        session.add(m.DialogueLine(shot_id=shot.id,participant_id=mapping[line.participant_id],order=line.order,text=line.text,delivery_json=line.delivery_json,source_json={'copied_from':line.id}))
    for audio in session.scalars(select(m.AudioEvent).where(m.AudioEvent.shot_id==old.id)):
        session.add(m.AudioEvent(shot_id=shot.id,kind=audio.kind,description=audio.description,start_seconds=audio.start_seconds,end_seconds=audio.end_seconds,volume=audio.volume,relative_path=audio.relative_path,source_json={'copied_from':audio.id}))
    from app.api.routes import add_shot_to_current_revision
    add_shot_to_current_revision(session,shot)
    session.commit();return row(shot)


@router.patch('/participants/{participant_id}')
def patch_person(participant_id:str,payload:ParticipantPatch,session:Session=Depends(get_db)):
    p=get(session,m.ShotCharacter,participant_id)
    shot=get(session,m.StoryboardShot,p.shot_id)
    if payload.revision is not None and shot.revision!=payload.revision:raise AppError(409,'edit_conflict','分镜已被其他操作修改，请刷新后保存')
    fields=payload.model_fields_set
    version=payload.character_version_id if 'character_version_id' in fields else p.character_version_id
    refs=payload.reference_asset_ids if payload.reference_asset_ids is not None else list(p.reference_asset_ids_json)
    validate_participant(session,p,version,refs)
    if payload.performance is not None and set(payload.performance)-PERFORMANCE_FIELDS:raise AppError(422,'performance_fields','包含不支持的表演字段')
    voice=payload.performance.get('reference_audio_asset_id') if payload.performance else None
    if voice:
        asset=session.get(m.AudioAsset,voice)
        if not asset or asset.project_id!=shot.project_id or asset.character_version_id!=version:raise AppError(422,'voice_reference_ownership','声音参考必须属于当前项目与明确角色版本')
    touch_shot(session,p.shot_id)
    p.character_version_id,p.reference_asset_ids_json=version,refs
    if payload.performance is not None:p.performance_json={**p.performance_json,**payload.performance}
    if payload.order is not None:p.order=payload.order
    if shot.character_id==p.character_id:
        shot.character_asset_version_id=version;shot.reference_asset_ids_json=refs
    session.commit();return row(p)


@router.delete('/participants/{participant_id}')
def remove_person(participant_id:str,remove_dialogues:bool=False,session:Session=Depends(get_db)):
    p=get(session,m.ShotCharacter,participant_id);remove_participant(session,p,remove_dialogues);session.commit();return {'removed':participant_id,'character_preserved':True}


def check_line(session,shot_id,payload):
    p=get(session,m.ShotCharacter,payload.participant_id)
    if p.shot_id!=shot_id:raise AppError(422,'speaker_not_participant','对白说话人必须属于当前分镜')
    for key in ('start_seconds','end_seconds','pause_seconds'):
        if key in payload.delivery:
            if payload.delivery[key] == '':
                payload.delivery.pop(key)
                continue
            try: value=float(payload.delivery[key])
            except (ValueError,TypeError):raise AppError(422,'dialogue_timing','对白时间必须是数字')
            if value<0:raise AppError(422,'dialogue_timing','对白时间不能为负数')
    if 'start_seconds' in payload.delivery and 'end_seconds' in payload.delivery and float(payload.delivery['end_seconds'])<float(payload.delivery['start_seconds']):raise AppError(422,'dialogue_timing','对白时间顺序无效')
    touch_shot(session,shot_id)


@router.post('/shots/{shot_id}/dialogues',status_code=201)
def add_line(shot_id:str,payload:LineInput,session:Session=Depends(get_db)):
    check_line(session,shot_id,payload)
    line=m.DialogueLine(shot_id=shot_id,participant_id=payload.participant_id,text=payload.text,order=payload.order,delivery_json=payload.delivery)
    session.add(line);session.commit();return row(line)


@router.patch('/dialogues/{line_id}')
def patch_line(line_id:str,payload:LineInput,session:Session=Depends(get_db)):
    line=get(session,m.DialogueLine,line_id);check_line(session,line.shot_id,payload)
    line.participant_id,line.text,line.order,line.delivery_json=payload.participant_id,payload.text,payload.order,payload.delivery
    session.commit();return row(line)


@router.delete('/dialogues/{line_id}')
def delete_line(line_id:str,session:Session=Depends(get_db)):
    line=get(session,m.DialogueLine,line_id);touch_shot(session,line.shot_id);session.delete(line);session.commit();return {'deleted':line_id}


def validate_audio(payload):
    if payload.end_seconds is not None and payload.end_seconds<payload.start_seconds:raise AppError(422,'audio_timing','音频结束时间不得早于开始')


@router.get('/scenes/{scene_id}/audio-events')
def scene_audio(scene_id:str,session:Session=Depends(get_db)):
    get(session,m.Scene,scene_id)
    return [row(x) for x in session.scalars(select(m.AudioEvent).where(m.AudioEvent.scene_id==scene_id))]


@router.post('/{scope}/{scope_id}/audio-events',status_code=201)
def add_audio(scope:str,scope_id:str,payload:AudioInput,session:Session=Depends(get_db)):
    validate_audio(payload)
    if scope=='shots':touch_shot(session,scope_id);kwargs={'shot_id':scope_id}
    elif scope=='scenes':
        get(session,m.Scene,scope_id);kwargs={'scene_id':scope_id}
        for detail in session.scalars(select(m.ShotDetail).where(m.ShotDetail.scene_id==scope_id)):touch_shot(session,detail.shot_id)
    else:raise AppError(404,'scope_unknown','音频只支持场景或分镜')
    audio=m.AudioEvent(**kwargs,**payload.model_dump());session.add(audio);session.commit();return row(audio)


@router.patch('/audio-events/{audio_id}')
def patch_audio(audio_id:str,payload:AudioInput,session:Session=Depends(get_db)):
    item=get(session,m.AudioEvent,audio_id);validate_audio(payload)
    if item.shot_id:touch_shot(session,item.shot_id)
    elif item.scene_id:
        for detail in session.scalars(select(m.ShotDetail).where(m.ShotDetail.scene_id==item.scene_id)):touch_shot(session,detail.shot_id)
    for k,v in payload.model_dump().items():setattr(item,k,v)
    session.commit();return row(item)


@router.post('/audio-events/{audio_id}/file')
async def upload_audio(audio_id:str,request:Request,file:UploadFile=File(...),session:Session=Depends(get_db)):
    item=get(session,m.AudioEvent,audio_id)
    extension=Path(file.filename or '').suffix.lower()
    if extension not in ('.wav','.mp3','.m4a','.aac','.ogg'):raise AppError(415,'audio_type','支持 WAV MP3 M4A AAC OGG')
    data=await file.read(request.app.state.settings.import_max_bytes+1)
    if not data or len(data)>request.app.state.settings.import_max_bytes:raise AppError(413,'audio_size','音频为空或过大')
    if item.shot_id:touch_shot(session,item.shot_id)
    elif item.scene_id:
        for detail in session.scalars(select(m.ShotDetail).where(m.ShotDetail.scene_id==item.scene_id)):touch_shot(session,detail.shot_id)
    relative='audio_uploads/'+item.id+'/'+hashlib.sha256(data).hexdigest()+extension
    path=safe_file(request.app.state.settings.media_root,relative);path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest()!=hashlib.sha256(data).hexdigest():raise AppError(409,'audio_path_conflict','已有文件不同，拒绝覆盖')
    if not path.exists():path.write_bytes(data)
    from app.services.audio_mix import probe_audio
    try:probe_audio(path,request.app.state.settings)
    except AppError:
        session.rollback()
        registry=m.ManagedFile(relative_path=relative,state='retained',metadata_json={'verified_owned':False,'kind':'failed_audio_upload','reason':'原始上传解码失败，保留用于诊断'})
        if not session.scalar(select(m.ManagedFile.id).where(m.ManagedFile.relative_path==relative)):session.add(registry)
        session.commit();raise
    registry=session.scalar(select(m.ManagedFile).where(m.ManagedFile.relative_path==relative))
    if not registry:registry=m.ManagedFile(relative_path=relative);session.add(registry)
    registry.ownership_json=[{'table':'audio_events','id':item.id}];registry.metadata_json={'verified_owned':False,'kind':'original_audio_upload'}
    item.relative_path=relative;session.commit();return row(item)


@router.get('/word-imports/templates/{kind}')
def download_template(kind:str):
    names={'blank':'剧本模板.docx','example':'剧本导入示例.docx'}
    if kind not in names:raise AppError(404,'template_missing','模板不存在')
    p=PROJECT_ROOT/'docs/templates'/names[kind]
    if not p.exists():raise AppError(503,'template_unavailable','模板尚未安装')
    return FileResponse(p,filename=names[kind])


def import_public(batch,session):
    from app.services.cinematic_quality import inspect_ir
    try:production_report=inspect_ir(batch.preview_json) if batch.preview_json else None
    except (KeyError,TypeError,AttributeError):production_report={'findings':[{'severity':'blocker','message':'预览结构需修正'}]}
    return {**row(batch),'summary':summary(batch.preview_json),'production_report':production_report,'issues':validate_preview(session,batch.preview_json) if batch.preview_json else [],
            'duplicates':[{'id':x.id,'status':x.status,'result':x.result_json} for x in session.scalars(select(m.WordImport).where(m.WordImport.sha256==batch.sha256,m.WordImport.id!=batch.id))]}


def parse_import(session,settings,batch):
    batch.status='parsing';batch.error_message=None;session.commit()
    try:
        if Path(batch.filename).suffix.lower()!='.docx':raise AppError(415,'legacy_doc','仅支持 .docx；旧版 .doc 请用 Word 或 LibreOffice 另存为 .docx')
        data=safe_file(settings.media_root,batch.relative_path).read_bytes()
        batch.blocks_json=read_blocks(data,settings.import_max_bytes)
        from app.services.cinematic_import import build_ir
        batch.preview_json=match_characters(session,build_ir(batch.blocks_json,parse_blocks));batch.status='pending_review';batch.revision+=1
    except (AppError,OSError,ValueError) as exc:
        batch.status='failed';batch.error_message=exc.message if isinstance(exc,AppError) else type(exc).__name__+'：文档读取失败'
    session.commit();return import_public(batch,session)


@router.post('/word-imports',status_code=201)
async def upload_word(request:Request,file:UploadFile=File(...),session:Session=Depends(get_db)):
    limit=request.app.state.settings.import_max_bytes
    data=await file.read(limit+1)
    if len(data)>limit:raise AppError(413,'document_too_large','文档超过大小限制')
    batch=m.WordImport(filename=Path((file.filename or 'document.docx').replace('\\','/')).name[:240],sha256=hashlib.sha256(data).hexdigest(),relative_path='')
    session.add(batch);session.flush()
    batch.relative_path='word_imports/'+batch.id+'/original'+Path(batch.filename).suffix.lower()
    path=safe_file(request.app.state.settings.media_root,batch.relative_path);path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data);session.commit()
    return await asyncio.to_thread(parse_import,session,request.app.state.settings,batch)


@router.get('/word-imports')
def imports(session:Session=Depends(get_db)):
    return [import_public(x,session) for x in session.scalars(select(m.WordImport).order_by(m.WordImport.created_at.desc()).limit(100))]


@router.get('/word-imports/{import_id}')
def import_detail(import_id:str,session:Session=Depends(get_db)):
    return import_public(get(session,m.WordImport,import_id),session)


@router.post('/word-imports/{import_id}/retry')
def retry_import(import_id:str,request:Request,session:Session=Depends(get_db)):
    batch=get(session,m.WordImport,import_id)
    if batch.status not in ('failed','pending_review'):raise AppError(409,'import_not_retryable','已导入记录不可重新解析覆盖，请新建导入批次')
    return parse_import(session,request.app.state.settings,batch)


@router.put('/word-imports/{import_id}/preview')
def update_preview(import_id:str,payload:ImportPreview,session:Session=Depends(get_db)):
    batch=get(session,m.WordImport,import_id)
    if batch.status!='pending_review' or batch.revision!=payload.revision:raise AppError(409,'import_stale','预览已变化或已导入')
    if len(json.dumps(payload.preview))>2_000_000:raise AppError(413,'preview_too_large','预览内容过大')
    try:issues=validate_preview(session,payload.preview)
    except (KeyError,TypeError,AttributeError):raise AppError(422,'preview_invalid','预览数据结构无效')
    for collection in ('characters','scenes','shots'):
        old={item['key']:item for item in batch.preview_json.get(collection,[])}
        for item in payload.preview.get(collection,[]):
            if item!=old.get(item['key']):item['reviewed_edit']=True
    batch.preview_json=payload.preview;batch.revision+=1;session.commit();return import_public(batch,session)


@router.post('/word-imports/{import_id}/confirm')
def confirm_import(import_id:str,payload:ImportConfirm,request:Request,session:Session=Depends(get_db)):
    batch=get(session,m.WordImport,import_id)
    result=commit_import(session,batch,payload);session.commit()
    if result.get('status')=='comparison':return result
    if payload.generate_standard_faces and not result.get('asset_requests'):
        from app.api.character_factory import submit_batch
        from app.services.character_factory import build_batch_plan
        from app.schemas_product import BatchRequest
        requests=[]
        for person in result['new_characters']:
            try:
                if request.app.state.settings.h3_demo_mode:raise AppError(409,'real_service_required','当前演示环境不提交真实人物素材生成')
                batch_request=BatchRequest(version_id=person['version_id'],kind='candidates',candidate_count=1,
                    prompt_template='Unretouched live-action casting photograph, natural facial anatomy matching the supplied character, practical clothing and soft window daylight.')
                preview=build_batch_plan(session,request.app.state.settings,person['character_id'],batch_request)
                batch_request.confirm=True;batch_request.preview_hash=preview['preview_hash']
                generated=submit_batch(request,person['character_id'],batch_request,session)
                requests.append({**person,'status':'queued','batch_id':generated['id']})
            except AppError as exc:requests.append({**person,'status':'blocked','reason':exc.message})
        batch.result_json={**result,'asset_requests':requests};session.commit()
    return batch.result_json


@router.get('/content/resources')
def resources():
    return [{'key':key,'name':name,'editable_fields':fields,'delete_supported':True,'history_immutable':not bool(fields)} for key,(_,name,fields) in RESOURCES.items()]


@router.post('/projects/{project_id}/export',status_code=201)
def export_project(project_id:str,request:Request,session:Session=Depends(get_db)):
    project=get(session,m.Project,project_id)
    shots=list(session.scalars(select(m.StoryboardShot).where(m.StoryboardShot.project_id==project_id)))
    content={'project':public_record(project),'scenes':[public_record(x) for x in session.scalars(select(m.Scene).where(m.Scene.project_id==project_id))],
        'shots':[shot_bundle(session,x.id) for x in shots]}
    data=json.dumps(content,ensure_ascii=False,indent=2,default=lambda x:x.isoformat()).encode('utf-8')
    relative='exports/'+project_id+'/'+hashlib.sha256(data).hexdigest()+'.json'
    path=safe_file(request.app.state.settings.media_root,relative);path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
    file=session.scalar(select(m.ManagedFile).where(m.ManagedFile.relative_path==relative))
    if not file:file=m.ManagedFile(relative_path=relative);session.add(file)
    file.state='active';file.ownership_json=[{'table':'projects','id':project_id}];file.metadata_json={'verified_owned':True,'kind':'project_export','sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data)}
    session.commit();return {**public_record(file),'download_url':'/api/media/'+relative}


@router.get('/content/{resource}')
def content_list(resource:str,q:str='',status:str='',project_id:str='',offset:int=0,limit:int=50,session:Session=Depends(get_db)):
    if resource not in RESOURCES:raise AppError(404,'resource_unknown','资源不存在')
    cls=RESOURCES[resource][0]
    items=[]
    for item in session.scalars(select(cls)):
        data=public_record(item);name=label(item)
        if q and q.casefold() not in (name+' '+item.id).casefold():continue
        if status and status not in [str(data.get(f,'')) for f in ['status','review_status','approval_state','state']]:continue
        if project_id and data.get('project_id')!=project_id:continue
        note=session.scalar(select(m.ContentNote).where(m.ContentNote.resource==resource,m.ContentNote.record_id==item.id))
        items.append({'id':item.id,'label':name,'status':next((str(data[k]) for k in ('status','review_status','approval_state','state') if k in data),''),'data':data,'note':public_record(note) if note else None})
    return {'total':len(items),'items':items[max(0,offset):max(0,offset)+min(200,max(1,limit))]}


@router.patch('/content/{resource}/{record_id}')
def content_patch(resource:str,record_id:str,payload:dict,session:Session=Depends(get_db)):
    if resource not in RESOURCES:raise AppError(404,'resource_unknown','资源不存在')
    cls,_,allowed=RESOURCES[resource];item=get(session,cls,record_id)
    if resource=='audio':
        values={k:payload.get(k,getattr(item,k)) for k in ('kind','description','start_seconds','end_seconds','volume')}
        try:validate_audio(AudioInput.model_validate(values))
        except ValidationError:raise AppError(422,'audio_fields_invalid','音频类型、时间或音量无效')
    if resource=='services':
        from app.api.ai_settings import patch_service
        from app.schemas_product import ServicePatch
        service_fields={k:v for k,v in payload.items() if k not in ('label','notes')}
        if service_fields:
            try:parsed=ServicePatch.model_validate(service_fields)
            except ValidationError:raise AppError(422,'service_fields_invalid','服务配置字段无效')
            patch_service(record_id,parsed,session)
        if 'label' in payload or 'notes' in payload:
            note=session.scalar(select(m.ContentNote).where(m.ContentNote.resource==resource,m.ContentNote.record_id==record_id))
            if not note:note=m.ContentNote(resource=resource,record_id=record_id);session.add(note)
            for key in ('label','notes'):
                if key in payload:
                    if not isinstance(payload[key],str) or len(payload[key])>10000:raise AppError(422,'note_invalid','备注无效')
                    setattr(note,key,payload[key])
            session.commit()
        return public_record(item)
    changes={k:v for k,v in payload.items() if k not in ('label','notes')}
    if set(changes)-set(allowed):raise AppError(422,'protected_fields','不能修改系统状态、路径、历史任务快照或身份版本')
    if resource=='shots':
        from app.api.routes import patch_shot
        from app.schemas import ShotPatch
        patch_shot(record_id,ShotPatch.model_validate(changes),session)
    else:
        if resource=='scripts' and item.status=='approved' and changes:raise AppError(409,'approved_script','已批准剧本请新建修订，不覆盖历史版本')
        if changes and resource in ('participants','dialogues'):touch_shot(session,item.shot_id)
        if changes and resource=='audio':
            if item.shot_id:touch_shot(session,item.shot_id)
            elif item.scene_id:
                for detail in session.scalars(select(m.ShotDetail).where(m.ShotDetail.scene_id==item.scene_id)):touch_shot(session,detail.shot_id)
        if changes and resource=='scenes':
            for detail in session.scalars(select(m.ShotDetail).where(m.ShotDetail.scene_id==item.id)):touch_shot(session,detail.shot_id)
        for k,v in changes.items():
            column=item.__table__.columns[k]
            if isinstance(column.type,JSON):
                expected=list if k.endswith('ids_json') or k in ('tags_json','hashtags_json') else dict
                if not isinstance(v,expected):raise AppError(422,'field_type','结构字段类型不正确')
            elif isinstance(column.type,(Integer,Float)):
                if not isinstance(v,(int,float)):raise AppError(422,'field_type','数字字段类型不正确')
            elif v is not None and (not isinstance(v,str) or len(v)>100000):raise AppError(422,'field_type','文本字段无效或过长')
            setattr(item,k,v)
    if 'label' in payload or 'notes' in payload:
        note=session.scalar(select(m.ContentNote).where(m.ContentNote.resource==resource,m.ContentNote.record_id==record_id))
        if not note:note=m.ContentNote(resource=resource,record_id=record_id);session.add(note)
        for k in ('label','notes'):
            if k in payload:
                if not isinstance(payload[k],str) or len(payload[k])>10000:raise AppError(422,'note_invalid','备注无效')
                setattr(note,k,payload[k])
    session.commit();return public_record(item)


@router.post('/content/templates',status_code=201)
def new_template(payload:dict,session:Session=Depends(get_db)):
    if not payload.get('name'):raise AppError(422,'name_required','模板名称不能为空')
    item=m.ContentTemplate(name=str(payload['name'])[:200],kind=str(payload.get('kind','screenplay'))[:40],body=str(payload.get('body',''))[:100000])
    session.add(item);session.commit();return public_record(item)


@router.post('/deletions/preview')
def deletion_preview(payload:DeletePreview,request:Request,session:Session=Depends(get_db)):
    return public_plan(build_plan(session,request.app.state.settings,payload.resource,payload.ids,payload.mode,payload.cascade))


@router.post('/deletions/execute')
def deletion_execute(payload:DeleteExecute,request:Request,session:Session=Depends(get_db)):
    return execute_delete(session,request.app.state.settings,payload)


@router.get('/deletions')
def deletion_list(session:Session=Depends(get_db)):
    return [task_public(x) for x in session.scalars(select(m.DeletionTask).order_by(m.DeletionTask.created_at.desc()).limit(100))]


@router.post('/deletions/{task_id}/retry')
def deletion_retry(task_id:str,request:Request,session:Session=Depends(get_db)):
    task=get(session,m.DeletionTask,task_id)
    if task.status not in ('files_failed','files_pending'):raise AppError(409,'retry_unavailable','该任务不处于可重试文件阶段，请重新预览或查看已完成结果')
    return file_outbox(session,request.app.state.settings,task)


@router.post('/deletions/{task_id}/restore')
def deletion_restore(task_id:str,request:Request,session:Session=Depends(get_db)):
    return restore_task(session,request.app.state.settings,get(session,m.DeletionTask,task_id))


@router.post('/deletions/{task_id}/purge')
def purge(task_id:str,payload:dict,request:Request,session:Session=Depends(get_db)):
    task=get(session,m.DeletionTask,task_id)
    if payload.get('confirm_permanent') is not True:raise AppError(409,'purge_confirmation','清空回收站需要明确永久删除确认')
    if task.mode!='trash' or task.status not in ('completed','purge_failed'):raise AppError(409,'purge_unavailable','仅已完成回收站任务可清空')
    result=dict(task.result_json);files=dict(result.get('files',{}));released=result.get('verified_released_bytes',0);failed=0
    for relative,state in list(files.items()):
        if state['status'] not in ('trashed','purge_failed'):continue
        try:
            p=safe_file(request.app.state.settings.media_root,'.recycle/'+task.id+'/'+relative)
            size=p.stat().st_size if p.exists() else 0
            if p.exists():p.unlink()
            if p.exists():raise OSError('file_still_exists')
            files[relative]={'status':'purged','bytes':size};released+=size
        except (OSError,AppError) as exc:files[relative]={'status':'purge_failed','error':str(exc)};failed+=1
    result.update(files=files,verified_released_bytes=released);task.result_json=result;task.status='purge_failed' if failed else 'purged';session.commit();return task_public(task)
