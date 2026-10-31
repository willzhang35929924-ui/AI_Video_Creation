"""Durable TTS/mix/lip children of Studio; existing video/image queues stay authoritative."""
import asyncio
import copy
import hashlib
import json
import logging
import math
import os
import subprocess
from pathlib import Path
from sqlalchemy import select, update, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app import models as m
from app.core.config import PROJECT_ROOT
from app.core.errors import AppError
from app.core.paths import safe_media_path
from app.core.secrets import reveal, redact
from app.schemas_routing import RuntimeJob, RuntimeReceipt
from app.services.task_routing import freeze_route, assert_frozen, validate_parameters
from app.services.runtime_registry import manifest_status, file_sha, REFERENCE_TTS_FAMILIES
from app.services.gpu_gate import gpu_submission

ACTIVE=['queued','submitting','submitted','running','cancelling']
TTS_PARAMETERS={
    'cosyvoice3':{'device','fp16','text_frontend','speed','mode','seed'},
    'indextts25':{'device','use_bf16','speed','language','seed'},
}


def _tts_parameters(provider, family):
    return {'voice','speed','instructions'} if provider=='openai_tts' else TTS_PARAMETERS.get(family,TTS_PARAMETERS['cosyvoice3'])


def _index_parameters(parameters):
    """The fixed helper maps common speed to IndexTTS's inverse duration factor."""
    if 'device' in parameters and parameters['device'] not in {'cuda','cpu'}:
        raise AppError(422,'tts_device','IndexTTS 2.5 设备仅支持cuda或cpu')
    if 'use_bf16' in parameters and not isinstance(parameters['use_bf16'],bool):
        raise AppError(422,'tts_precision','IndexTTS 2.5 use_bf16必须为布尔值')
    if parameters.get('device','cuda')=='cpu' and parameters.get('use_bf16') is True:
        raise AppError(422,'tts_precision','IndexTTS 2.5 CPU运行需关闭use_bf16，使用FP32')
    if 'language' in parameters and parameters['language'] not in {'ZH','EN','JA','ES','AR'}:
        raise AppError(422,'tts_language','IndexTTS 2.5 语言需为ZH、EN、JA、ES或AR')
    if 'seed' in parameters and (isinstance(parameters['seed'],bool) or not isinstance(parameters['seed'],int) or not 0<=parameters['seed']<=2**32-1):
        raise AppError(422,'tts_seed','IndexTTS 2.5 种子需为32位非负整数')

def _owned(session,model,identifier,project_id,shot_id=None):
    row=session.get(model,identifier)
    if not row or getattr(row,'project_id',project_id)!=project_id or shot_id and getattr(row,'shot_id',shot_id)!=shot_id:
        raise AppError(422,'media_input_ownership','媒体/人物/对白不属于本次项目和镜头')
    return row

def _audio(session,settings,identifier,project_id):
    row=session.get(m.AudioAsset,identifier)
    if row:
        if row.project_id!=project_id:raise AppError(422,'audio_input_ownership','音频素材不属于项目')
        path=safe_media_path(settings.media_root,row.relative_path,must_exist=True)
        if file_sha(path)!=row.sha256:raise AppError(409,'audio_input_changed','音频文件SHA发生变化')
        return {'id':row.id,'path':str(path),'sha256':row.sha256,'metadata':row.metadata_json,
            'character_version_id':row.character_version_id,'participant_id':row.participant_id,'kind':'audio_asset'}
    event=session.get(m.AudioEvent,identifier)
    owner=session.get(m.StoryboardShot,event.shot_id) if event and event.shot_id else session.get(m.Scene,event.scene_id) if event and event.scene_id else None
    if not event or not owner or owner.project_id!=project_id or not event.relative_path:raise AppError(422,'audio_input_ownership','请选择当前项目内已上传的音频ID')
    path=safe_media_path(settings.media_root,event.relative_path,must_exist=True)
    return {'id':event.id,'path':str(path),'sha256':file_sha(path),'kind':'audio_event',
        'metadata':{'start_seconds':event.start_seconds,'end_seconds':event.end_seconds,'volume':event.volume}}


def _fingerprint(frozen):
    """Inference identity excludes capture clocks, probe output and proof IDs."""
    selected=(frozen.get('route',{}).get('selected') or {}).get('service_snapshot',{})
    stable={key:frozen.get(key) for key in ('kind','project_id','shot_id','participant_id','character_version_id','dialogue_line_id','parameters','inputs')}
    stable['service']={key:selected.get(key) for key in ('service_id','service_revision','provider','model_id','settings')}
    stable['sources']={key:{'id':value.get('id'),'sha256':value.get('sha256'),
        'timing':{k:value.get('metadata',{}).get(k) for k in ('start_seconds','end_seconds','volume')}} for key,value in frozen['sources'].items()}
    return hashlib.sha256(json.dumps(stable,ensure_ascii=False,sort_keys=True,allow_nan=False).encode()).hexdigest()

def _video(session,settings,identifier,project_id,shot_id):
    row=_owned(session,m.VideoRevision,identifier,project_id,shot_id)
    path=safe_media_path(settings.media_root,row.relative_path,must_exist=True);sha=file_sha(path)
    if row.metadata_json.get('sha256') and row.metadata_json['sha256']!=sha:raise AppError(409,'media_input_changed','输入视频SHA发生变化')
    return row,{'id':row.id,'path':str(path),'sha256':sha}

def create_task(session,settings,payload,kind=None,route=None):
    _owned(session,m.Project,payload.project_id,payload.project_id)
    shot=_owned(session,m.StoryboardShot,payload.shot_id,payload.project_id) if payload.shot_id else None
    if shot and shot.status=='deleted':raise AppError(409,'shot_deleted','已删除镜头不再派发任务')
    inputs=dict(payload.inputs);parameters=dict(payload.parameters);validate_parameters(parameters)
    if any(key.lower().endswith('_path') or key.lower() in {'python','helper','repo','model_dir','headers','api_key','token','secret'} for key in inputs):raise AppError(422,'media_paths_forbidden','公开输入只接受素材ID和文本，不接受本地路径/密钥/命令')
    kind=kind or ('audio_mix' if inputs.get('operation')=='audio_mix' else 'tts' if payload.slot=='dialogue_generation' else 'lip_sync')
    if kind not in {'tts','audio_mix','lip_sync'}:raise AppError(422,'media_operation','未实现的媒体处理操作')
    if payload.slot!=('lip_sync' if kind=='lip_sync' else 'dialogue_generation'):raise AppError(422,'media_slot_mismatch','处理操作与任务槽不匹配')
    if kind!='tts' and not shot:raise AppError(422,'shot_required','视频后处理需要明确镜头')
    sources={};compiled={};participant=None;line=None
    if kind=='tts':
        if inputs.get('dialogue_line_id'):
            line=session.get(m.DialogueLine,inputs['dialogue_line_id'])
            if not line or not shot or line.shot_id!=shot.id:raise AppError(422,'dialogue_ownership','对白不属于当前镜头')
            participant=session.get(m.ShotCharacter,line.participant_id);compiled['text']=line.text
        else:
            compiled['text']=inputs.get('text','')
            if inputs.get('participant_id'):
                participant=session.get(m.ShotCharacter,inputs['participant_id'])
                if not participant or not shot or participant.shot_id!=shot.id:raise AppError(422,'participant_ownership','人物不属于当前镜头')
        if not isinstance(compiled['text'],str) or not 1<=len(compiled['text'].strip())<=20000:raise AppError(422,'tts_text','对白文本长度必须是1到20000')
        reference=inputs.get('reference_audio_asset_id') or inputs.get('reference_audio_id') or inputs.get('audio_event_id')
        if reference:
            from app.services.voice_library import assert_reference_authorized
            assert_reference_authorized(session,session.get(m.AudioAsset,reference))
            sources['reference_audio_path']=_audio(session,settings,reference,payload.project_id)
            if participant:
                asset=session.get(m.AudioAsset,reference)
                if not asset or asset.character_version_id!=participant.character_version_id:raise AppError(422,'voice_reference_ownership','对白参考音频必须明确属于当前角色版本')
            compiled['reference_audio_path']=sources['reference_audio_path']['path']
        if inputs.get('prompt_text'):compiled['prompt_text']=str(inputs['prompt_text'])[:20000]
    else:
        video,sources['video_path']=_video(session,settings,inputs.get('video_id'),payload.project_id,shot.id)
        compiled['video_path']=sources['video_path']['path']
        audio_ids=inputs.get('audio_ids',[]) if kind=='audio_mix' else [inputs.get('audio_id') or inputs.get('audio_event_id')]
        if not isinstance(audio_ids,list) or not audio_ids or len(audio_ids)>100 or any(not isinstance(i,str) for i in audio_ids):raise AppError(422,'audio_input_required','请选择明确的已生成/已上传音频ID')
        for index,identifier in enumerate(audio_ids):sources['audio_path' if kind=='lip_sync' else 'audio_'+str(index)]=_audio(session,settings,identifier,payload.project_id)
        if kind=='lip_sync':
            participants=list(session.scalars(select(m.ShotCharacter).where(m.ShotCharacter.shot_id==shot.id)))
            identifier=inputs.get('participant_id') or parameters.get('target_participant_id')
            if len(participants)!=1 or not identifier or participants[0].id!=identifier:raise AppError(409,'lip_sync_single_target','当前口型适配器仅支持明确指定且画面只有一个参与人物的镜头')
            participant=participants[0]
            voice=sources['audio_path']
            if voice.get('kind')!='audio_asset' or voice.get('character_version_id')!=participant.character_version_id or voice.get('participant_id') not in (None,participant.id):
                raise AppError(422,'lip_audio_voice_ownership','口型音频必须明确属于此人物版本；不使用无说话人归属的音频事件')
            compiled['audio_path']=voice['path']
            compiled.update(target_participant_id=participant.id,participant_ids=[participant.id])
        else:
            compiled['mode']=inputs.get('mode','replace_dialogue')
            people=list(session.scalars(select(m.ShotCharacter).where(m.ShotCharacter.shot_id==shot.id)))
            voice_sources=[value for key,value in sources.items() if key.startswith('audio_')]
            if len(people)==1 and people[0].character_version_id and all(value.get('kind')=='audio_asset' and value.get('character_version_id')==people[0].character_version_id and value.get('participant_id') in (None,people[0].id) for value in voice_sources):
                participant=people[0]
    snapshot_route=route or (freeze_route(session,settings,payload.project_id,payload.shot_id,payload.slot,payload.allow_cloud_costs,payload.allow_paid_fallback) if kind!='audio_mix' else {'slot':'dialogue_generation','selected':None,'internal':'ffmpeg_audio_mix'})
    if participant and not participant.character_version_id:raise AppError(422,'voice_version_required','角色配音与口型需要明确的身份版本')
    if participant and participant.performance_json.get('voice_profile'):
        compiled['cloud_voice_id']=str(participant.performance_json['voice_profile']).strip()
    config=snapshot_route.get('profile',{}).get('settings',{});parameters={**config.get('parameters',{}),**parameters}
    validate_parameters(parameters)
    selected=(snapshot_route.get('selected') or {}).get('service_snapshot',{})
    provider=selected.get('provider');family=selected.get('settings',{}).get('runtime_family')
    if kind=='tts':
        maximum=4096 if provider=='openai_tts' else 2000
        if len(compiled['text'])>maximum:raise AppError(422,'tts_provider_text_limit',f'此配音适配器每次最多{maximum}字符')
        if provider=='local_runtime' and family=='cosyvoice3':
            parameters.setdefault('mode','zero_shot' if compiled.get('prompt_text') else 'cross_lingual')
            parameters.setdefault('fp16',False);parameters.setdefault('text_frontend',False)
        if provider=='local_runtime' and family=='indextts25':
            if compiled.get('prompt_text'):raise AppError(422,'tts_parameter_unsupported','IndexTTS 2.5 参考克隆不接收CosyVoice的参考转写prompt_text')
            parameters.setdefault('use_bf16',parameters.get('device','cuda')!='cpu')
            _index_parameters(parameters)
        if provider=='openai_tts' and participant:
            role_voice=parameters.get('voice') or participant.performance_json.get('voice_profile')
            if not isinstance(role_voice,str) or not role_voice.strip():
                raise AppError(422,'role_cloud_voice_required','云端配音角色需要明确的供应商音色ID；参考WAV不会被此适配器用于克隆')
            parameters['voice']=role_voice.strip();compiled['reference_mode']='cloud_voice_id_only'
        allowed=_tts_parameters(provider,family)
        if set(parameters)-allowed:raise AppError(422,'tts_parameter_unsupported','此配音适配器不支持所填参数',{'fields':sorted(set(parameters)-allowed)})
        if 'speed' in parameters:
            speed=parameters['speed'];low,high=(.25,4) if provider=='openai_tts' else (.5,2)
            if isinstance(speed,bool) or not isinstance(speed,(int,float)) or not math.isfinite(speed) or not low<=speed<=high:raise AppError(422,'tts_speed','配音语速超出此适配器范围')
    elif kind=='lip_sync':
        parameters.pop('target_participant_id',None)
        if set(parameters)-{'seed','inference_steps','guidance_scale','duration_policy'}:raise AppError(422,'lip_parameter_unsupported','口型参数不在固定适配器范围')
    frozen={'kind':kind,'route':snapshot_route,'parameters':parameters,'inputs':compiled,'sources':sources,
        'participant_id':participant.id if participant else None,'character_version_id':participant.character_version_id if participant else None,
        'dialogue_line_id':line.id if line else None,'project_id':payload.project_id,'shot_id':payload.shot_id}
    fingerprint=_fingerprint(frozen)
    for old in session.scalars(select(m.MediaProcessTask).where(m.MediaProcessTask.project_id==payload.project_id,m.MediaProcessTask.kind==kind).order_by(m.MediaProcessTask.created_at.desc())):
        if old.snapshot_json.get('fingerprint')==fingerprint and old.status in {'queued','running','succeeded','unknown'}:return old
    frozen['fingerprint']=fingerprint
    task=m.MediaProcessTask(project_id=payload.project_id,shot_id=payload.shot_id,kind=kind,slot=payload.slot,fingerprint=fingerprint,snapshot_json=frozen)
    if kind!='audio_mix' and not snapshot_route.get('selected'):
        task.status='blocked';task.error_message='此任务槽无已验证且已授权服务；没有派发推理'
    elif kind=='tts' and provider=='local_runtime' and not reference:
        task.status='blocked';task.error_message='对白角色缺少明确归属的参考音频，未使用统一smoke声线代替'
    try:
        with session.begin_nested():session.add(task);session.flush()
    except IntegrityError:
        # The DB unique partial index is authoritative across threads/processes.
        # A separate reader sees the committed winner without discarding this
        # caller's surrounding Studio transaction.
        with Session(session.get_bind()) as reader:
            existing=reader.scalar(select(m.MediaProcessTask).where(m.MediaProcessTask.fingerprint==fingerprint,m.MediaProcessTask.status.in_(['queued','running','unknown','succeeded'])))
            if not existing:raise
            reader.expunge(existing);return existing
    return task

def _probe(path,settings,audio=False):
    from app.services.audio_mix import probe_audio
    if not audio:
        from app.services.video_quality import measured
        return measured(path,settings)[0]
    info=probe_audio(path,settings)
    result=subprocess.run([settings.ffmpeg_path,'-nostdin','-v','error','-xerror','-i',str(path),'-f','null','-'],capture_output=True,timeout=180,shell=False,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if result.returncode:raise AppError(422,'audio_full_decode','完整音频解码失败')
    stream=next(s for s in info['streams'] if s['codec_type']=='audio')
    return {'duration':float(info['format']['duration']),'sample_rate':int(stream['sample_rate']),'channels':stream['channels'],'codec':stream['codec_name'],'full_decode':True}

def _run_cli(command,timeout,cwd):
    environment={**os.environ,'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'}
    process=subprocess.Popen(command,cwd=cwd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),shell=False,env=environment)
    try:
        stdout,stderr=process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name=='nt':subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True,shell=False,timeout=15)
        else:process.kill()
        process.communicate(timeout=15);raise
    if process.returncode:raise AppError(502,'runtime_failed','固定运行时执行失败：'+stderr.decode('utf-8','replace')[-1400:])


def normalize_reference(settings,source,destination):
    """New PCM derivative; original uploaded/reference bytes remain immutable."""
    original=Path(source['path'])
    if file_sha(original)!=source['sha256']:raise AppError(409,'reference_source_changed','原参考音频已变化')
    actual=_probe(original,settings,True)
    if not .1<=actual['duration']<=30:raise AppError(422,'voice_reference_duration','此参考音频适配器接受0.1至30秒，不会静默截断')
    command=[settings.ffmpeg_path,'-nostdin','-v','error','-xerror','-n','-i',str(original),'-vn','-ar','24000','-ac','1','-c:a','pcm_s16le',str(destination)]
    result=subprocess.run(command,capture_output=True,timeout=120,shell=False)
    if result.returncode:raise AppError(422,'voice_normalization_failed','参考音频规范化失败；源文件保留')
    normalized=_probe(destination,settings,True)
    if abs(normalized['duration']-actual['duration'])>.05:raise AppError(422,'voice_normalization_timing','参考音频规范化改变了时长')
    return {'source_id':source['id'],'source_path':str(original),'source_sha256':source['sha256'],
        'output_path':str(destination),'output_sha256':file_sha(destination),'actual':normalized,
        'method':'explicit_pcm_s16le_24000hz_mono_derivative','source_preserved':file_sha(original)==source['sha256']}

def _mix(settings,snapshot,destination):
    source=snapshot['sources']['video_path'];before=_probe(Path(source['path']),settings)
    command=[settings.ffmpeg_path,'-v','error','-xerror','-n','-i',source['path']];filters=[];labels=[]
    mode=snapshot['inputs']['mode']
    if mode not in {'replace_dialogue','mix'}:raise AppError(422,'audio_mix_mode','请选择替换对白或混合音轨')
    if mode=='mix' and before['audio']:labels.append('[0:a:0]')
    cursor=0
    for index,item in enumerate(v for key,v in snapshot['sources'].items() if key.startswith('audio_')):
        command+=['-i',item['path']];meta=item.get('metadata',{});start=float(meta.get('start_seconds',cursor))
        if start<0 or start>=before['duration']:raise AppError(422,'audio_timing','对白音频开始时间超出镜头')
        duration=float(_probe(Path(item['path']),settings,True)['duration'])
        end=float(meta['end_seconds']) if meta.get('end_seconds') is not None else start+duration
        volume=float(meta.get('volume',1))
        if not all(math.isfinite(v) for v in (start,end,volume)) or end<=start or not 0<=volume<=4:
            raise AppError(422,'audio_event_range','音频事件起止与音量无效')
        if end-start>duration+.05:raise AppError(422,'audio_event_longer_than_source','音频事件要求的时长超过源音频，未循环或伪造音频')
        cursor=end
        if cursor>before['duration']+.05:raise AppError(422,'audio_longer_than_video','对白音频超过原片长度，未静默截断')
        filters.append(f'[{index+1}:a:0]atrim=duration={end-start:.8f},asetpts=PTS-STARTPTS,volume={volume:.8f},adelay={round(start*1000)}:all=1[a{index}]');labels.append(f'[a{index}]')
    filters.append(''.join(labels)+f'amix=inputs={len(labels)}:duration=longest:normalize=0,apad,atrim=duration={before["duration"]},alimiter=limit=0.95[mixed]')
    command+=['-filter_complex',';'.join(filters),'-map','0:v:0','-map','[mixed]','-c:v','copy','-c:a','aac','-t',str(before['duration']),'-movflags','+faststart',str(destination)]
    result=subprocess.run(command,capture_output=True,timeout=300,shell=False)
    if result.returncode:raise AppError(502,'audio_mix_failed','真实音频混合失败，原片保留')

def _publish(session,settings,task,snapshot,destination,actual):
    relative=destination.relative_to(settings.media_root).as_posix();sha=file_sha(destination)
    result={'relative_path':relative,'sha256':sha,'actual':actual,'source_preserved':True,'method':task.kind,'visual_review':'human_review_required','dialogue_semantics_verification':'NOT RUN','lip_sync_visual_verification':'NOT RUN'}
    if task.kind=='audio_mix':result['audio_policy']='replace_complete_native_track; no dialogue/ambience stem separation' if snapshot['inputs']['mode']=='replace_dialogue' else 'explicit native-track mix; overlapping original speech may remain'
    if task.kind=='tts':
        asset=m.AudioAsset(project_id=task.project_id,shot_id=task.shot_id,participant_id=snapshot.get('participant_id'),
            dialogue_line_id=snapshot.get('dialogue_line_id'),character_version_id=snapshot.get('character_version_id'),task_id=task.id,
            relative_path=relative,sha256=sha,metadata_json={'actual':actual,'text_sha256':hashlib.sha256(snapshot['inputs']['text'].encode()).hexdigest(),'task_id':task.id,
                'source_type':'ai_generated','human_approved':False,'voice_identity_verification':'NOT RUN'})
        session.add(asset);session.flush();result['audio_id']=asset.id;owner={'table':'audio_assets','id':asset.id}
    else:
        source=session.get(m.VideoRevision,snapshot['sources']['video_path']['id'])
        revision=(session.scalar(select(func.max(m.VideoRevision.revision)).where(m.VideoRevision.shot_id==source.shot_id)) or 0)+1
        metadata={**source.metadata_json,'sha256':sha,'width':actual['width'],'height':actual['height'],'duration_seconds':actual['duration'],
            'frame_rate':actual['fps'],'file_size':destination.stat().st_size,'playable_video':True,'source_video_id':source.id,
            'root_source_video_id':source.metadata_json.get('root_source_video_id',source.id),'processing_task_id':task.id,'processing_kind':task.kind,
            'processing_chain':[*source.metadata_json.get('processing_chain',[]),{'task_id':task.id,'kind':task.kind,'input_video_id':source.id,'input_sha256':snapshot['sources']['video_path']['sha256'],'output_sha256':sha}]}
        metadata.pop('quality_render_id',None)
        video=m.VideoRevision(project_id=source.project_id,shot_id=source.shot_id,generation_job_id=source.generation_job_id,
            revision=revision,relative_path=relative,metadata_json=metadata,review_status='pending',instruction=source.instruction)
        session.add(video);session.flush();result['video_id']=video.id;owner={'table':'video_revisions','id':video.id}
        if task.kind=='audio_mix':
            audio_path=destination.with_suffix('.mixed_audio.wav')
            process=subprocess.run([settings.ffmpeg_path,'-nostdin','-v','error','-xerror','-n','-i',str(destination),'-map','0:a:0','-vn','-c:a','pcm_s16le',str(audio_path)],capture_output=True,timeout=120,shell=False)
            if process.returncode:raise AppError(502,'mixed_audio_extraction_failed','完整混音轨提取失败，视频源保留')
            audio_actual=_probe(audio_path,settings,True);audio_relative=audio_path.relative_to(settings.media_root).as_posix();audio_sha=file_sha(audio_path)
            audio=m.AudioAsset(project_id=task.project_id,shot_id=task.shot_id,participant_id=snapshot.get('participant_id'),
                character_version_id=snapshot.get('character_version_id'),task_id=task.id,relative_path=audio_relative,sha256=audio_sha,
                metadata_json={'actual':audio_actual,'human_approved':False,'source_type':'derived_mix','source_video_id':video.id,
                    'source_audio_ids':[item['id'] for key,item in snapshot['sources'].items() if key.startswith('audio_')],'voice_identity_verification':'NOT RUN'})
            session.add(audio);session.flush();result.update(audio_id=audio.id,audio_relative_path=audio_relative,audio_sha256=audio_sha)
            session.add(m.ManagedFile(relative_path=audio_relative,state='active',ownership_json=[{'table':'audio_assets','id':audio.id},{'table':'media_process_tasks','id':task.id}],metadata_json={'verified_owned':True,'sha256':audio_sha,'kind':'mixed_audio'}))
        for check in __import__('app.services.quality',fromlist=['inspect_media']).inspect_media(destination,settings):
            session.add(m.QualityCheck(video_revision_id=video.id,check_type=check['check_type'],status=check['status'],details_json=check['details']))
    session.add(m.ManagedFile(relative_path=relative,state='active',ownership_json=[owner,{'table':'media_process_tasks','id':task.id}],metadata_json={'verified_owned':True,'sha256':sha,'kind':task.kind}))
    task.status='succeeded';task.result_json=result;task.error_message=None

async def _process_attempt(settings,factory,identifier):
    with factory() as session:
        task=session.get(m.MediaProcessTask,identifier)
        if not task or task.status!='queued':return
        snapshot=dict(task.snapshot_json)
        active_route=snapshot.get('active_route') or snapshot['route']
        snapshot['route']=active_route
        attempt_index=int(task.snapshot_json.get('attempt_index',0))
        prior_attempts=list(task.result_json.get('attempt_history',[]))
        if task.shot_id and session.get(m.StoryboardShot,task.shot_id).status=='deleted':
            task.status='blocked';task.error_message='镜头已删除，未派发';session.commit();return
        if task.kind!='audio_mix':
            try:service,service_snap=assert_frozen(session,snapshot['route'])
            except AppError as exc:task.status='blocked';task.error_message=exc.message;session.commit();return
        else:service=None;service_snap={'provider':'ffmpeg','model_id':''}
        task.status='running';session.commit()
    relative=f'media_processing/{snapshot["project_id"]}/{identifier}'+(f'.attempt{attempt_index}' if attempt_index else '')+('.wav' if snapshot['kind']=='tts' else '.mp4')
    destination=safe_media_path(settings.media_root,relative);destination.parent.mkdir(parents=True,exist_ok=True)
    cloud_accepted=False;secret='';runtime_receipt=None;derivations=[]
    try:
        for source in snapshot['sources'].values():
            if file_sha(source['path'])!=source['sha256']:raise AppError(409,'processing_source_changed','输入文件SHA发生变化')
        if snapshot['kind']=='audio_mix':await asyncio.to_thread(_mix,settings,snapshot,destination)
        elif service_snap['provider']=='openai_tts':
            from app.services.ai_services import service_request
            with factory() as session:secret=reveal(session.get(m.AIService,service_snap['service_id']).encrypted_secret)
            body={'model':service_snap['model_id'],'input':snapshot['inputs']['text'],'voice':snapshot['parameters'].get('voice') or snapshot['inputs'].get('cloud_voice_id') or service_snap['settings'].get('voice','alloy'),'response_format':'wav'}
            body.update({key:snapshot['parameters'][key] for key in ('speed','instructions') if key in snapshot['parameters']})
            try:response=await service_request(settings,service_snap,'POST','/audio/speech',secret,json=body)
            except __import__('httpx').TransportError as exc:raise AppError(502,'cloud_submission_unknown','语音请求可能已受理；未重发或回退') from exc
            if response.status_code>=500 or response.status_code==408:raise AppError(502,'cloud_submission_unknown',f'语音服务 HTTP{response.status_code}，无法确认是否已受理；未重发')
            if response.status_code>=300:raise AppError(502,'tts_http_rejected',f'语音服务拒绝请求 HTTP{response.status_code}',{'http_status':response.status_code})
            cloud_accepted=True
            if not 44<=len(response.content)<=100*1024*1024:raise AppError(502,'cloud_submission_unknown','语音请求已受理但输出无效；未重发')
            with destination.open('xb') as stream:stream.write(response.content)
        else:
            family=service_snap['settings'].get('runtime_family','');runtime=manifest_status(family,settings)
            if runtime['state']!='verified':raise AppError(409,'runtime_unverified','运行时组件或真实输出证据不再有效')
            frozen_runtime=snapshot['route']['selected']['candidate']['evidence'].get('runtime',{})
            if runtime.get('manifest_sha256')!=frozen_runtime.get('manifest_sha256'):raise AppError(409,'runtime_manifest_changed','运行时manifest改变；未派发')
            manifest=runtime['manifest'];receipt_path=destination.with_suffix('.receipt.json');job_path=destination.with_suffix('.job.json')
            allowed={'seed','inference_steps','guidance_scale','duration_policy'} if family=='latentsync15' else _tts_parameters('local_runtime',family)
            runtime_inputs=dict(snapshot['inputs'])
            if family in REFERENCE_TTS_FAMILIES:
                normalized=await asyncio.to_thread(normalize_reference,settings,snapshot['sources']['reference_audio_path'],destination.with_suffix('.reference.wav'))
                derivations.append(normalized);runtime_inputs['reference_audio_path']=normalized['output_path']
            cli_params={k:v for k,v in snapshot['parameters'].items() if k in allowed}
            if family=='cosyvoice3':
                cli_params.setdefault('mode','zero_shot' if runtime_inputs.get('prompt_text') else 'cross_lingual')
                cli_params.setdefault('fp16',False);cli_params.setdefault('text_frontend',False)
            elif family=='indextts25':
                if runtime_inputs.get('prompt_text'):raise AppError(422,'tts_parameter_unsupported','IndexTTS 2.5 参考克隆不接收CosyVoice的参考转写prompt_text')
                cli_params.setdefault('device','cuda');cli_params.setdefault('use_bf16',cli_params['device']!='cpu')
                cli_params.setdefault('speed',1.0);cli_params.setdefault('language','ZH');cli_params.setdefault('seed',1247)
                _index_parameters(cli_params)
            job=RuntimeJob(task_id=identifier,slot=task.slot,model_id=service_snap['model_id'],inputs=runtime_inputs,parameters=cli_params,output_path=str(destination),receipt_path=str(receipt_path))
            with job_path.open('x',encoding='utf-8') as stream:stream.write(job.model_dump_json(indent=2))
            command=[manifest['python'],manifest['helper'],'--job',str(job_path),'--model-dir',manifest['model_dir'],'--repo',manifest['repo']]
            if family=='latentsync15':command+=['--ffmpeg',settings.ffmpeg_path,'--ffprobe',settings.ffprobe_path]
            try:
                await asyncio.to_thread(_run_cli,command,3600,manifest['repo'])
            except AppError as exc:
                if exc.code=='runtime_failed' and receipt_path.is_file():
                    try:
                        failed=RuntimeReceipt.model_validate(json.loads(receipt_path.read_text('utf-8-sig')))
                        if (failed.status=='failed' and failed.task_id==job.task_id and failed.slot==job.slot
                                and failed.model_id==job.model_id
                                and (failed.output_path is None or Path(failed.output_path).resolve()==destination.resolve()) and failed.error):
                            runtime_receipt=failed.model_dump()
                            raise AppError(502,'runtime_failed','固定运行时执行失败：'+failed.error[:1400]) from exc
                    except (ValueError,OSError):pass
                raise
            receipt=RuntimeReceipt.model_validate(json.loads(receipt_path.read_text('utf-8-sig')))
            if receipt.status!='succeeded' or receipt.task_id!=identifier or receipt.slot!=task.slot or receipt.model_id!=job.model_id or not receipt.output_path or Path(receipt.output_path).resolve()!=destination.resolve() or receipt.output_sha256!=file_sha(destination):raise AppError(502,'runtime_receipt_mismatch','运行时回执身份、输出路径、状态或SHA不符')
            expected_inputs={'reference_audio':file_sha(runtime_inputs['reference_audio_path']),'text_utf8':hashlib.sha256(runtime_inputs['text'].strip().encode()).hexdigest()} if family in REFERENCE_TTS_FAMILIES else {key:file_sha(runtime_inputs[key]) for key in ('video_path','audio_path')}
            if any(receipt.inputs_sha256.get(key)!=value for key,value in expected_inputs.items()):raise AppError(502,'runtime_input_receipt_mismatch','运行时回执的实际输入SHA不符')
            runtime_receipt=receipt.model_dump()
        actual=await asyncio.to_thread(_probe,destination,settings,snapshot['kind']=='tts')
        for source in snapshot['sources'].values():
            if file_sha(source['path'])!=source['sha256']:raise AppError(409,'processing_source_changed','处理期间输入文件发生变化')
        if snapshot['kind']!='tts':
            before=await asyncio.to_thread(_probe,Path(snapshot['sources']['video_path']['path']),settings)
            if actual['frames']!=before['frames'] or actual['fps']!=before['fps'] or abs(actual['duration']-before['duration'])>.05:raise AppError(502,'processing_timing_changed','派生视频帧数、帧率或时长变化')
            if (actual['width'],actual['height'])!=(before['width'],before['height']):raise AppError(502,'processing_dimensions_changed','此处理不应改变视频尺寸')
        with factory() as session:
            finished=session.get(m.MediaProcessTask,identifier);_publish(session,settings,finished,snapshot,destination,actual)
            finished.result_json={**finished.result_json,'runtime_receipt':runtime_receipt,'runtime_input_derivations':derivations,
                'actual_service_snapshot':service_snap,'attempt_history':[*prior_attempts,{'index':attempt_index,'service_id':service_snap.get('service_id'),'model_id':service_snap.get('model_id'),'provider':service_snap.get('provider'),'status':'succeeded','relative_path':relative}]};session.commit()
    except Exception as exc:
        unknown=getattr(exc,'code',None)=='cloud_submission_unknown' or cloud_accepted
        with factory() as session:
            task=session.get(m.MediaProcessTask,identifier);task.status='unknown' if unknown else 'failed';task.error_message=redact(str(exc),secret)[:1800]
            task.result_json={'inference_submitted':None if unknown else cloud_accepted,'submission_unknown':unknown,'output_preserved':destination.exists(),'relative_path':relative if destination.exists() else None,
                'runtime_receipt':runtime_receipt,
                'error_code':getattr(exc,'code',type(exc).__name__),'actual_service_snapshot':service_snap,
                'attempt_history':[*prior_attempts,{'index':attempt_index,'service_id':service_snap.get('service_id'),'model_id':service_snap.get('model_id'),'provider':service_snap.get('provider'),'status':'unknown' if unknown else 'failed','error_code':getattr(exc,'code',type(exc).__name__),'relative_path':relative}]}
            session.commit()


def _schedule_determined_retry(session,task):
    """Only known exit/rejection can advance the already frozen candidate list."""
    if task.status!='failed' or task.kind=='audio_mix' or task.result_json.get('error_code') not in {'runtime_failed','tts_http_rejected'}:return False
    snapshot=dict(task.snapshot_json);original=snapshot['route'];active=snapshot.get('active_route') or original
    config=original.get('profile',{}).get('settings',{});selected=active.get('selected') or {};current=selected.get('service_snapshot',{})
    history=task.result_json.get('attempt_history',[])
    if len(history)>=max(1,len(original.get('choices',[])))*(int(config.get('max_retries',0))+1):return False
    same=sum(1 for item in history if item.get('service_id')==current.get('service_id') and item.get('model_id')==current.get('model_id'))
    next_choice=None
    if same<=int(config.get('max_retries',0)):next_choice=selected
    elif config.get('allow_fallback'):
        choices=original.get('choices',[])
        position=next((i for i,c in enumerate(choices) if c.get('service_snapshot',{}).get('service_id')==current.get('service_id') and c.get('service_snapshot',{}).get('model_id')==current.get('model_id')),len(choices))
        for choice in choices[position+1:]:
            candidate=choice.get('candidate',{});service=choice.get('service_snapshot',{})
            if not candidate.get('eligible'):continue
            if service.get('is_cloud') and not (config.get('allow_paid_fallback') and original.get('allow_paid_fallback') and original.get('allow_cloud_costs')):continue
            if service.get('provider')=='local_runtime' and service.get('settings',{}).get('runtime_family') in REFERENCE_TTS_FAMILIES and not snapshot['sources'].get('reference_audio_path'):continue
            if service.get('provider')=='local_runtime' and service.get('settings',{}).get('runtime_family')=='indextts25' and snapshot['inputs'].get('prompt_text'):continue
            if service.get('provider')=='openai_tts' and snapshot.get('participant_id') and not (snapshot['inputs'].get('cloud_voice_id') or snapshot['parameters'].get('voice')):continue
            next_choice=choice;break
    if not next_choice:return False
    snapshot['active_route']={**copy.deepcopy(original),'selected':copy.deepcopy(next_choice)}
    snapshot['attempt_index']=int(snapshot.get('attempt_index',0))+1
    task.snapshot_json=snapshot;task.status='queued';task.error_message=None
    task.result_json={**task.result_json,'retry_pending':True,'retry_reason':'determined process exit or explicit HTTP rejection only'}
    session.flush();return True


async def process_task(settings,factory,identifier):
    await _process_attempt(settings,factory,identifier)
    with factory() as session:
        task=session.get(m.MediaProcessTask,identifier)
        if task:_schedule_determined_retry(session,task);session.commit()

@gpu_submission
async def media_tick(settings,factory):
    with factory() as session:
        task=session.scalar(select(m.MediaProcessTask).where(m.MediaProcessTask.status=='queued').order_by(m.MediaProcessTask.created_at))
        if not task:return
        identifier=task.id;selected=(task.snapshot_json.get('active_route') or task.snapshot_json.get('route',{})).get('selected') or {}
        gpu=task.kind!='audio_mix' and not selected.get('service_snapshot',{}).get('is_cloud',False)
        if gpu:
            active=bool(session.scalar(select(m.GenerationJob.id).where(m.GenerationJob.provider=='comfyui_h3',m.GenerationJob.status.in_(ACTIVE)).limit(1)))
            active=active or any(session.scalar(select(kind.id).where(kind.status.in_(ACTIVE),kind.snapshot_json['service_snapshot']['provider'].as_string()=='comfyui').limit(1)) for kind in [m.CharacterGenerationJob,m.ProductionVisual])
            active=active or session.scalar(select(m.QualityRender.id).where(m.QualityRender.status=='running',m.QualityRender.snapshot_json['ai_super_resolution'].as_boolean()==True).limit(1))
            if active:return
    if gpu:
        # Use the same queue/free/confirmed-release transaction as existing AI quality.
        from app.services.video_quality import confirm_gpu_release
        try:
            if await confirm_gpu_release(settings) is None:return
        except Exception as exc:
            with factory() as session:
                row=session.get(m.MediaProcessTask,identifier);row.status='failed';row.error_message='GPU派发前检查失败：'+str(exc)[:1200];row.result_json={'inference_submitted':False};session.commit()
            return
    await process_task(settings,factory,identifier)

async def media_worker(settings,factory):
    with factory() as session:
        for task in session.scalars(select(m.MediaProcessTask).where(m.MediaProcessTask.status=='running')):
            selected=task.snapshot_json.get('route',{}).get('selected') or {}
            task.status='unknown'
            task.error_message='处理进程中断；原输入与任务保留，结果未知的云请求不自动重提'
        session.commit()
    while True:
        try:await media_tick(settings,factory)
        except asyncio.CancelledError:raise
        except Exception:logging.getLogger(__name__).exception('Media processing worker failed; no automatic resubmission')
        await asyncio.sleep(1)
