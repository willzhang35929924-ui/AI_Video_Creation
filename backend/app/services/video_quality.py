"""Measured full-video processing. Lanczos is ordinary scaling, never AI super-resolution."""
from __future__ import annotations
import asyncio
import copy
import hashlib
import importlib.util
import json
import logging
import subprocess
import time
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from sqlalchemy import select, update, func
from app import models as m
from app.core.errors import AppError
from app.core.paths import safe_media_path
from app.core.config import PROJECT_ROOT
from app.models.quality import QualityProfile, QualityRender
from app.schemas_quality import QualitySettings
from app.services.quality import inspect_media

SIZES = {'720p': (1280,720), '1080p': (1920,1080), '2k': (2560,1440), '4k': (3840,2160)}
MODES = {
    'high': {'steps': 30, 'crf': 16, 'preset': 'slow'},
    'standard': {'steps': 20, 'crf': 18, 'preset': 'medium'},
    'fast': {'steps': 4, 'crf': 23, 'preset': 'fast'},
    # Retained for settings and historical runs written before Context-IR routing.
    'preview': {'steps': 12, 'crf': 23, 'preset': 'fast'},
}
SEEDVR_RUNTIME_WAIT = 'seedvr_runtime_unavailable'

def plan(settings,sr_status=None,seed_status=None):
    q = QualitySettings.model_validate(settings)
    width,height = SIZES[q.resolution]
    if q.aspect_ratio == '9:16': width,height = height,width
    if q.aspect_ratio == '1:1': width=height
    base = {'16:9': (1024,576), '9:16': (576,1024), '1:1': (768,768)}[q.aspect_ratio]
    ai_supported=q.processing=='ai_super_resolution' and q.resolution=='4k' and (sr_status or {}).get('state')=='verified' and q.aspect_ratio in (sr_status or {}).get('verified_aspect_ratios',[])
    from app.services.seedvr_capabilities import target_supported
    seed_supported=q.processing=='seedvr2' and target_supported(q,seed_status)
    supported=q.processing=='lanczos' or ai_supported or seed_supported
    return {'requested': {'resolution':q.resolution,'width':width,'height':height,'aspect_ratio':q.aspect_ratio},
        'generation': {'width':base[0],'height':base[1],'steps':MODES[q.mode]['steps'],'parameter_mapping':'H3 sampler steps; model-specific, not cloud universal'},
        'encoding': {'codec':'libx264','pix_fmt':'yuv420p',**{k:v for k,v in MODES[q.mode].items() if k!='steps'}},
        'method': q.processing, 'supported':supported, 'ai_super_resolution':ai_supported or seed_supported,
        'temporal_model':bool(seed_supported),
        'postprocess_model':{'name':'SeedVR2 3B FP8','steps':1,'seed':20261024,'temporal_chunk_pixel_frames':9,'temporal_overlap_latent_frames':1} if q.processing=='seedvr2' else None,
        'label':q.resolution+' 输出（普通 Lanczos 缩放）' if q.processing=='lanczos' else q.resolution+' 输出（SeedVR2 时序视频超分）' if seed_supported else '4K 输出（Real-ESRGAN 逐帧超分）' if ai_supported else '所选处理方式尚无已验证的工作流',
        'visual_quality':'human_review_required', 'limits':['文件像素尺寸不代表原生细节质量','SeedVR2仅开放完整视频实测通过的精确分辨率与画幅组合','模式参数已接入工作流；各参数实测证据以任务记录为准','时序分块模型仍需检查边界闪烁与细节伪造；Real-ESRGAN为逐帧图像模型']}

def _target(session, scope, target):
    if scope == 'system':
        if target != 'default': raise AppError(422,'quality_scope','系统默认ID必须为default')
    elif scope in {'project','shot'}:
        if not session.get(m.Project if scope=='project' else m.StoryboardShot,target): raise AppError(404,'quality_target_missing','画质配置对象不存在')
    else: raise AppError(422,'quality_scope','不支持的画质配置层级')

def find_profile(session, scope, target):
    return session.scalar(select(QualityProfile).where(QualityProfile.scope==scope,QualityProfile.target_id==target))

def profile(session, scope, target, app_settings=None):
    _target(session,scope,target)
    own=find_profile(session,scope,target); found=own
    if not found and scope=='shot':
        found=find_profile(session,'project',session.get(m.StoryboardShot,target).project_id)
    if not found and scope!='system': found=find_profile(session,'system','default')
    q=QualitySettings.model_validate(found.settings_json if found else {})
    return {'scope':scope,'target_id':target,'settings':q.model_dump(),'revision':own.revision if own else 0,
        'inherited_from':None if own else (found.scope+':'+found.target_id if found else 'built_in'), 'plan':plan(q,_sr_status(app_settings),seedvr_capabilities(session,app_settings) if q.processing=='seedvr2' else None)}

def _sr_status(settings):
    if not settings:return None
    from app.services.super_resolution import status
    return status(settings)

def seedvr_capabilities(session, settings):
    from app.services.seedvr_capabilities import capabilities
    return capabilities(session, settings)


def seedvr_status(settings):
    if not settings:return {'state':'not_configured','reasons':['Application settings unavailable'],'verified_resolutions':[],'verified_aspect_ratios':[],'limits':['仅720p16:9经过完整视频实测后可用']}
    from app.services.runtime_registry import manifest_status
    result=manifest_status('seedvr2',settings)
    actual=result.get('evidence',{}).get('actual',{})
    video=next((stream for stream in actual.get('streams',[]) if stream.get('codec_type')=='video'),{})
    verified=result.get('state')=='verified' and (video.get('width'),video.get('height'))==(1280,720)
    result.update(verified_resolutions=['720p'] if verified else [],verified_aspect_ratios=['16:9'] if verified else [],
                  limits=[*result.get('manifest',{}).get('limitations',[]),'当前生产仅开放720p16:9；不代表Seed4K或人工视觉批准'])
    if result.get('state')=='verified' and not verified:
        result['state']='not_tested';result.setdefault('reasons',[]).append('No matching720p16:9 smoke output')
    return result

def save_profile(session, scope, target, payload, app_settings=None):
    _target(session,scope,target)
    if not plan(payload.settings,_sr_status(app_settings),seedvr_capabilities(session,app_settings) if payload.settings.processing=='seedvr2' else None)['supported']: raise AppError(409,'quality_method_unverified','当前未验证此原生／AI超分／重建流程，请选择已验证处理方式')
    item=find_profile(session,scope,target)
    if payload.expected_revision is not None and payload.expected_revision!=(item.revision if item else 0): raise AppError(409,'quality_profile_changed','画质设置已改变，请刷新后保存')
    if item: item.settings_json=payload.settings.model_dump(); item.revision+=1
    else: session.add(QualityProfile(scope=scope,target_id=target,settings_json=payload.settings.model_dump(),revision=1))
    session.add(m.AuditEvent(object_type='quality_profile',object_id=scope+':'+target,action='explicit_settings_saved',details_json=payload.settings.model_dump()))
    session.flush(); return profile(session,scope,target,app_settings)

def inherited_settings(session, project_id, shot_id=None):
    scopes=([('shot',shot_id)] if shot_id else [])+[('project',project_id),('system','default')]
    for scope,target in scopes:
        found=find_profile(session,scope,target)
        if found:return dict(found.settings_json)
    return None  # old API consumers retain their existing explicit dimensions

def measured(path, settings):
    checks=inspect_media(path,settings)
    decode=next((c for c in checks if c['check_type']=='media_decode'),None)
    if not decode or decode['status']!='passed' or 'full_decode' not in decode['details']:
        raise AppError(422,'quality_full_decode_failed','实际视频完整解码未通过',{'checks':checks})
    raw=decode['details']; video=next(s for s in raw['streams'] if s['codec_type']=='video')
    counted=subprocess.run([settings.ffprobe_path,'-v','error','-count_frames','-show_streams','-of','json',str(path)],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=120,shell=False)
    if counted.returncode:raise AppError(422,'quality_frame_count_failed','完整视频帧计数失败')
    counted_video=next((s for s in json.loads(counted.stdout).get('streams',[]) if s.get('codec_type')=='video'),{})
    frames=int(counted_video.get('nb_read_frames') or 0)
    if not frames:raise AppError(422,'quality_frame_count_failed','没有可验证的完整视频帧数')
    return {'width':int(video['width']),'height':int(video['height']),
        'fps':str(video.get('avg_frame_rate','0/1')),'duration':float(video.get('duration') or raw['format']['duration']),
        'frames':frames,'frame_count_source':'ffprobe -count_frames','start_time':float(video.get('start_time') or 0),'codec':video.get('codec_name'),'pix_fmt':video.get('pix_fmt'),
        'bit_rate':int(raw['format'].get('bit_rate') or 0),'size_bytes':path.stat().st_size,
        'audio':[{'codec':s.get('codec_name'),'sample_rate':s.get('sample_rate'),'channels':s.get('channels'),'duration':s.get('duration'),'start_time':float(s.get('start_time') or 0)} for s in raw['streams'] if s['codec_type']=='audio'],
        'full_decode':raw['full_decode']},checks

def render_dto(item):
    return {'id':item.id,'status':item.status,'source_video_id':item.source_video_id,'video_id':item.video_id,
        'relative_path':item.relative_path,'snapshot':item.snapshot_json,'result':item.result_json,'error_message':item.error_message}

def _upscale_layer(video):
    metadata=video.metadata_json or {}
    kind=metadata.get('processing_kind')
    if kind is not None:return kind in {'video_upscale','quality_upscale'}
    # Legacy metadata may have inherited old quality keys from a protected stage.
    # Without an explicit current kind, preserve audio/lip transformations.
    if metadata.get('audio_mix') or metadata.get('lip_sync'):return False
    return bool(metadata.get('quality_render_id') and metadata.get('quality',{}).get('method') in
                {'ordinary_lanczos_scaling','realesrgan_framewise','seedvr2_temporal','seedvr2_temporal_video'})

def _source_lineage(session,requested):
    current=requested;selected=requested;seen=set();peeling=True;chain=[]
    while True:
        if current.id in seen or len(seen)>32:raise AppError(409,'quality_source_cycle','源视频版本关系异常')
        seen.add(current.id);chain.append(current.id)
        parent_id=(current.metadata_json or {}).get('source_video_id')
        if not parent_id:break
        parent=session.get(m.VideoRevision,parent_id)
        if not parent or (parent.project_id,parent.shot_id,parent.generation_job_id)!=(requested.project_id,requested.shot_id,requested.generation_job_id):
            raise AppError(409,'quality_source_mismatch','后处理源视频不属于同一项目、镜头与生成任务')
        if peeling and _upscale_layer(current):selected=parent
        else:peeling=False
        current=parent
    return selected,current.id,chain

def _route_for_quality(session,route_snapshot,q):
    if route_snapshot is None:return q,None
    from app.services.task_routing import assert_frozen
    _service,selected=assert_frozen(session,route_snapshot)
    if route_snapshot.get('slot')!='video_upscale' or selected.get('module')!='video_upscale':
        raise AppError(422,'quality_route_slot','画质处理只能使用视频超分任务路由')
    if selected.get('model_id')!='seedvr2_3b_fp8_e4m3fn.safetensors' or selected.get('provider') not in {'comfyui','local_runtime'}:
        raise AppError(409,'quality_route_adapter_unavailable','此视频超分服务尚无已验证执行适配；未切换到其他服务')
    base=selected.get('base_url') or 'http://127.0.0.1:8189'
    if base.rstrip('/')!='http://127.0.0.1:8189':
        raise AppError(409,'seedvr_endpoint_unverified','当前SeedVR实测仅对应本机8189端点，未使用其他地址')
    if q.processing!='seedvr2':
        raise AppError(409,'quality_route_processing_mismatch','视频超分路由选择了SeedVR2，请先将画质处理方式明确保存为SeedVR2；未替换当前普通缩放或其他处理方式')
    return q,copy.deepcopy(route_snapshot)

def create_render(session, settings, video_id, quality,allow_unverified_ai=False,route_snapshot=None,allow_unverified_seed=False):
    q=QualitySettings.model_validate(quality);q,route_snapshot=_route_for_quality(session,route_snapshot,q)
    sr=_sr_status(settings);seed=seedvr_capabilities(session,settings) if q.processing=='seedvr2' else None;p=plan(q,sr,seed)
    testing=allow_unverified_ai and q.processing=='ai_super_resolution' and q.resolution=='4k' and sr and sr['state'] in {'detected_not_tested','verified'}
    seed_testing=allow_unverified_seed and q.processing=='seedvr2' and q.resolution in {'720p','1080p'} and seed and seed.get('state')=='verified'
    if not p['supported'] and not (testing or seed_testing):raise AppError(409,'quality_method_unverified','没有已验证的原生／AI超分／重建流程；不能用普通缩放冒充')
    video=session.get(m.VideoRevision,video_id)
    if not video: raise AppError(404,'video_not_found','源视频不存在')
    requested_video_id=video.id;video,root_source_video_id,source_lineage=_source_lineage(session,video)
    path=safe_media_path(settings.media_root,video.relative_path,must_exist=True)
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    if video.metadata_json.get('sha256') and digest!=video.metadata_json['sha256']:raise AppError(409,'quality_source_changed','原始视频哈希已变化')
    # No history overwrite, and no metadata-only approval: the worker decodes the entire source.
    cache_input=[video.id,digest,q.model_dump()]
    if q.processing=='ai_super_resolution':cache_input.append({'model_sha256':sr['model_sha256'],'script_sha256':sr['script_sha256']})
    if q.processing=='seedvr2':
        seed={**seed,'helper_path':str(PROJECT_ROOT/'scripts/deploy_seedvr2.py'),'helper_sha256':_file_sha(PROJECT_ROOT/'scripts/deploy_seedvr2.py')}
        cache_input.append({'manifest_sha256':seed['manifest_sha256'],'helper_sha256':seed['helper_sha256']})
    if route_snapshot:
        selected=route_snapshot['selected']['service_snapshot']
        cache_input.append({key:selected.get(key) for key in ['service_id','service_revision','provider','model_id','base_url','settings']})
    fingerprint=hashlib.sha256(json.dumps(cache_input,sort_keys=True).encode()).hexdigest()
    for old in session.scalars(select(QualityRender).where(QualityRender.source_video_id==video.id).order_by(QualityRender.created_at.desc())):
        if old.snapshot_json.get('fingerprint')==fingerprint and old.status=='unknown':
            raise AppError(409,'quality_dispatch_unknown','同一原生处理任务状态不确定，请先核对原Comfy任务，未重复派发',{'quality_render_id':old.id})
        native=old.snapshot_json.get('native_execution') or {}
        if (old.snapshot_json.get('fingerprint')==fingerprint and old.status=='failed'
                and native.get('inference_submitted') is True and native.get('prompt_id')
                and (native.get('terminal_status') or {}).get('status_str')=='success'):
            return old  # accepted model output still needs collection; no new inference
        if old.snapshot_json.get('fingerprint')==fingerprint and old.status in {'queued','running','generated'}:
            if old.status=='generated':
                prior=safe_media_path(settings.media_root,old.relative_path,must_exist=True)
                if hashlib.sha256(prior.read_bytes()).hexdigest()!=old.result_json.get('sha256'):continue
            return old
    item=QualityRender(source_video_id=video.id,snapshot_json={'settings':q.model_dump(),'plan':p,'source_sha256':digest,
        'source_relative_path':video.relative_path,'project_id':video.project_id,'shot_id':video.shot_id,'generation_job_id':video.generation_job_id,
        'fingerprint':fingerprint,'requested_source_video_id':requested_video_id,'root_source_video_id':root_source_video_id,'source_lineage':source_lineage,
        'method':'seedvr2_temporal_video' if q.processing=='seedvr2' else 'realesrgan_framewise' if q.processing=='ai_super_resolution' else 'ordinary_lanczos_scaling',
        'ai_super_resolution':q.processing in {'ai_super_resolution','seedvr2'},'super_resolution':sr if q.processing=='ai_super_resolution' else None,
        'seedvr2':seed,'route':route_snapshot,
        'verification_run':bool(testing or seed_testing)},status='queued',result_json={})
    session.add(item);session.flush();return item

def _file_sha(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

def _native_unknown(message,details=None):
    error=AppError(409,'seedvr_dispatch_unknown',message,details or {})
    error.native_unknown=True
    return error

def _persist_native(factory,identifier,value):
    with factory() as session:
        item=session.get(QualityRender,identifier)
        item.snapshot_json={**item.snapshot_json,'native_execution':value}
        session.commit()

def _run_seedvr_native(settings,factory,identifier,snapshot,source,destination,original):
    """One real native Comfy submission under the existing shared GPU lock.

    Reuse the deployment helper's upload/frozen graph/history/full-video checks.
    The intermediate is lossless; the existing quality mode encodes the final
    managed version once, with compressed source audio preserved.
    """
    frozen=snapshot['seedvr2'];current=seedvr_status(settings)
    if current.get('state')!='verified' or current.get('manifest_sha256')!=frozen.get('manifest_sha256'):
        raise AppError(409,'seedvr_manifest_changed','SeedVR模型清单或实测证据变化，未派发')
    helper_path=(PROJECT_ROOT/'scripts/deploy_seedvr2.py').resolve()
    if Path(frozen['helper_path']).resolve()!=helper_path or _file_sha(helper_path)!=frozen['helper_sha256']:
        raise AppError(409,'seedvr_helper_changed','SeedVR执行程序变化，未派发')
    dimensions=snapshot['plan']['requested']
    if original['width']*dimensions['height']!=original['height']*dimensions['width']:
        raise AppError(422,'seedvr_source_aspect','SeedVR目标画幅须与源视频一致；未拉伸或裁切原片')
    spec=importlib.util.spec_from_file_location('video_factory_seedvr_'+identifier,helper_path)
    helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
    # Scratch and proof files belong to managed media, not the installed models.
    helper.RUNTIME=(settings.media_root/'seedvr2_runtime'/identifier).resolve()
    manifest=frozen['manifest'];workflow=Path(manifest['workflow_path'])
    if _file_sha(workflow)!=manifest['workflow_sha256']:
        raise AppError(409,'seedvr_workflow_changed','SeedVR工作流变化，未派发')
    paths={name:str(destination.with_suffix('.seed-'+name+'.json')) for name in ['plan','prepared','submission','result']}
    prior=snapshot.get('native_execution',{})
    prepared_exists=Path(paths['prepared']).is_file()
    resume = prior.get('resume_existing_submission') is True
    if Path(paths['result']).exists() or (Path(paths['submission']).exists() and not resume):
        raise _native_unknown('本次SeedVR已有派发证据文件，请先核对，未自动重复派发',{'paths':paths})
    if resume and (not prepared_exists or not Path(paths['submission']).is_file() or not prior.get('prompt_id') or prior.get('inference_submitted') is not True):
        raise _native_unknown('缺少可核对的已提交任务凭据，未重新提交模型', {'paths':paths})
    if prepared_exists and (not prior.get('prepared_sha256') or _file_sha(paths['prepared'])!=prior['prepared_sha256'] or (not resume and prior.get('status') not in {'prepared','deferred'})):
        raise _native_unknown('SeedVR准备文件不匹配冻结记录，未重新派发',{'paths':paths})
    native={**prior,'paths':paths,'status':'prepared' if prepared_exists else 'preparing','prompt_id':prior.get('prompt_id') if resume else None,'inference_submitted':prior.get('inference_submitted') if resume else False}
    _persist_native(factory,identifier,native)
    # Build an internal fixed plan from allowlisted helper constants; production
    # does not depend on audit reports or accept HTTP model/executable paths.
    components={Path(row['path']).resolve():row for row in manifest['components']}
    files=[]
    for name,size,digest in helper.SPECS:
        target=(helper.MODELS/name).resolve();component=components.get(target)
        if not component or component['bytes']!=size or component['sha256']!=digest:
            raise AppError(409,'seedvr_component_changed','SeedVR组件不匹配已验证固定模型，未派发')
        files.append({'repo_path':name,'expected_bytes':size,'expected_sha256':digest,'target':str(target),
            'partial':str(helper.RUNTIME/'temp'/(digest+'.safetensors.partial')),
            'url':f'https://huggingface.co/{helper.REPO}/resolve/{helper.COMMIT}/{name}'})
    base_url='http://127.0.0.1:8189'
    if not prepared_exists:
        helper.write_json(paths['plan'],{'repo':helper.REPO,'commit':helper.COMMIT,'files':files})
        helper.prepare_command(SimpleNamespace(plan=paths['plan'],input=str(source),output=paths['prepared'],workflow=str(workflow),
            ffmpeg=settings.ffmpeg_path,ffprobe=settings.ffprobe_path,width=dimensions['width'],height=dimensions['height'],frames=0,seed=20261024,chunk_frames=9,base_url=base_url))
        prepared=json.loads(Path(paths['prepared']).read_text('utf-8'))
        prepared['prompt']['15']['inputs']['format.codec.encoding.crf']=0.0
        prepared['graph_sha256']=hashlib.sha256(json.dumps(prepared['prompt'],sort_keys=True).encode()).hexdigest()
        prepared['parameters']['native_intermediate_encoding']={'codec':'h264','crf':0,'purpose':'lossless intermediate; final quality-mode encoding follows'}
        helper.write_json(paths['prepared'],prepared)
    else:
        prepared=json.loads(Path(paths['prepared']).read_text('utf-8'))
        if prepared.get('original_source_sha256')!=snapshot['source_sha256'] or _file_sha(prepared['comfy_upload']['actual_path'])!=prepared['input_sha256']:
            raise AppError(409,'seedvr_prepared_input_changed','等待期间SeedVR冻结输入变化，未继续派发')
    native.update(status='prepared',graph_sha256=prepared['graph_sha256'],prepared_sha256=_file_sha(paths['prepared']))
    _persist_native(factory,identifier,native)
    if not resume:
        try:
            helper.submit_command(SimpleNamespace(prepared=paths['prepared'],output=paths['submission'],base_url=base_url,
                scheduler_note='Existing QualityRender worker owns gpu_submission throughout release, native submit, full-video collect and publication'))
        except Exception as error:
            if isinstance(error,helper.LocalGPUQueueBusyError):
                native['status']='deferred';_persist_native(factory,identifier,native)
                deferred=AppError(409,'seedvr_gpu_busy','本机另一Comfy队列忙或状态未知，保留冻结输入等待，未提交')
                deferred.native_deferred=True
                raise deferred from error
            submission=json.loads(Path(paths['submission']).read_text('utf-8')) if Path(paths['submission']).is_file() else {}
            native.update(status=submission.get('status','not_submitted'),prompt_id=submission.get('prompt_id'))
            if submission.get('status') in {'SUBMITTING_NO_AUTORETRY','UNKNOWN_DO_NOT_RESUBMIT','SUBMITTED'}:
                native['inference_submitted']=True if submission.get('status')=='SUBMITTED' else None
            _persist_native(factory,identifier,native)
            if submission.get('status') in {'SUBMITTING_NO_AUTORETRY','UNKNOWN_DO_NOT_RESUBMIT','SUBMITTED'}:
                raise _native_unknown('SeedVR提交结果不确定，请核对已保存的Comfy证据，未自动重试',native) from error
            raise
    submission=json.loads(Path(paths['submission']).read_text('utf-8'));prompt_id=submission['prompt_id']
    if resume and (submission.get('status')!='SUBMITTED' or prompt_id!=prior.get('prompt_id') or submission.get('prepared_sha256')!=prior.get('prepared_sha256')):
        raise _native_unknown('原提交编号或冻结输入凭据不匹配；未新建模型请求',native)
    native.update(status='submitted',prompt_id=prompt_id,inference_submitted=True)
    _persist_native(factory,identifier,native)
    deadline=time.monotonic()+3600
    while time.monotonic()<deadline:
        try:history=helper.api(base_url,'/history/'+prompt_id).get(prompt_id)
        except Exception as error:
            raise _native_unknown('SeedVR已提交但暂不能读取Comfy状态，保留任务证据且不重复派发',native) from error
        if history:
            native['terminal_status']=history.get('status');_persist_native(factory,identifier,native)
            if history.get('status',{}).get('status_str')!='success':
                raise AppError(502,'seedvr_native_failed','真实SeedVR任务未成功，已保留Comfy历史',native)
            break
        time.sleep(3)
    else:raise _native_unknown('SeedVR已提交，等待结果超时；未取消原Comfy任务或重复提交',native)
    delivery=helper.RUNTIME/'outputs'/('quality_'+identifier+'.mp4')
    helper.collect_command(SimpleNamespace(submission=paths['submission'],delivery=str(delivery),output=paths['result']))
    receipt=json.loads(Path(paths['result']).read_text('utf-8'))
    if receipt.get('status')!='PASS_TECHNICAL_FILE' or receipt.get('frames_processed')!=original['frames'] or not all(
        value is True or key=='full_decode_exit_code' and value==0 for key,value in receipt.get('checks',{}).items()):
        raise AppError(502,'seedvr_incomplete','SeedVR完整帧、输入图、音轨或源文件检查未通过',{'receipt_path':paths['result']})
    if receipt.get('source_sha256')!=snapshot['source_sha256'] or _file_sha(receipt['output'])!=receipt.get('output_sha256'):
        raise AppError(409,'seedvr_output_changed','SeedVR源或实际交付SHA不匹配，未发布')
    native.update(status='completed',receipt_path=paths['result'],receipt_sha256=_file_sha(paths['result']),output_sha256=receipt['output_sha256'])
    _persist_native(factory,identifier,native)
    return Path(receipt['output']),receipt

def run_render(settings, factory, identifier,gpu_release_evidence=None):
    with factory() as session:
        claimed=session.execute(update(QualityRender).where(QualityRender.id==identifier,QualityRender.status=='queued').values(status='running',error_message=None));session.commit()
        if not claimed.rowcount:return
        item=session.get(QualityRender,identifier); snapshot=dict(item.snapshot_json)
    started=time.monotonic()
    try:
        if snapshot.get('route'):
            from app.services.task_routing import assert_frozen
            with factory() as session:assert_frozen(session,snapshot['route'])
        source=safe_media_path(settings.media_root,snapshot['source_relative_path'],must_exist=True)
        if hashlib.sha256(source.read_bytes()).hexdigest()!=snapshot['source_sha256']:raise AppError(409,'quality_source_changed','源视频发生变化')
        original,_=measured(source,settings)
        relative=f"quality/{snapshot['project_id']}/{identifier}.mp4"
        destination=safe_media_path(settings.media_root,relative); destination.parent.mkdir(parents=True,exist_ok=True)
        p=snapshot['plan']; target=p['requested']; enc=p['encoding']
        command=[settings.ffmpeg_path,'-hide_banner','-v','error','-xerror','-n','-threads','2','-filter_threads','1','-i',str(source),'-map','0:v:0','-map','0:a?',
            '-vf',f"scale={target['width']}:{target['height']}:force_original_aspect_ratio=decrease:flags=lanczos,pad={target['width']}:{target['height']}:(ow-iw)/2:(oh-ih)/2,setsar=1",
            '-c:v','libx264','-threads','2','-crf',str(enc['crf']),'-preset',enc['preset'],'-pix_fmt','yuv420p','-c:a','aac','-b:a','192k','-movflags','+faststart',str(destination)]
        sr_receipt=None
        is_seed=snapshot.get('settings',{}).get('processing')=='seedvr2'
        if is_seed:
            seed_source,sr_receipt=_run_seedvr_native(settings,factory,identifier,snapshot,source,destination,original)
            command=[settings.ffmpeg_path,'-hide_banner','-v','error','-xerror','-n','-threads','2','-i',str(seed_source),
                '-map','0:v:0','-map','0:a?','-c:v','libx264','-threads','2','-crf',str(enc['crf']),'-preset',enc['preset'],
                '-pix_fmt','yuv420p','-c:a','copy','-movflags','+faststart',str(destination)]
        elif snapshot.get('ai_super_resolution'):
            sr_paths=snapshot['super_resolution']['runtime'];receipt_path=destination.with_suffix('.sr.json')
            if hashlib.sha256(Path(sr_paths['model']).read_bytes()).hexdigest()!=snapshot['super_resolution']['model_sha256']:raise AppError(409,'super_resolution_model_changed','超分模型文件变化，未继续处理')
            if hashlib.sha256(Path(sr_paths['script']).read_bytes()).hexdigest()!=snapshot['super_resolution']['script_sha256']:raise AppError(409,'super_resolution_script_changed','超分执行程序变化，需重新冻结输入')
            command=[sr_paths['python'],sr_paths['script'],'--input',str(source),'--output',str(destination),'--model',sr_paths['model'],
                '--width',str(target['width']),'--height',str(target['height']),'--ffmpeg',settings.ffmpeg_path,'--ffprobe',settings.ffprobe_path,'--receipt',str(receipt_path),
                '--crf',str(enc['crf']),'--preset',enc['preset']]
        process=subprocess.run(command,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=3600,shell=False,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if process.returncode:raise AppError(502,'quality_encoding_failed','真实FFmpeg处理失败：'+process.stderr[-1800:])
        if snapshot.get('ai_super_resolution') and not is_seed:
            sr_receipt=json.loads(receipt_path.read_text('utf-8'))
            if sr_receipt.get('model_sha256')!=snapshot['super_resolution']['model_sha256'] or sr_receipt.get('frames_processed')!=original['frames']:
                raise AppError(502,'super_resolution_incomplete','超分没有处理全部原始帧，或模型SHA不符')
        actual,checks=measured(destination,settings)
        expected_fps=Fraction(original['fps']); tolerance=1/float(expected_fps)+.01
        if (actual['width'],actual['height'])!=(target['width'],target['height']):raise AppError(502,'quality_output_mismatch','实际输出尺寸不符合请求')
        if Fraction(actual['fps'])!=expected_fps or abs(actual['duration']-original['duration'])>tolerance or (original['frames'] and actual['frames']!=original['frames']):raise AppError(502,'quality_timing_mismatch','实际帧率、帧数或时长不符合源视频')
        if len(actual['audio'])!=len(original['audio']):raise AppError(502,'quality_audio_mismatch','实际音轨数量与源视频不一致')
        for audio,before_audio in zip(actual['audio'],original['audio']):
            if audio['duration'] and before_audio['duration'] and abs(float(audio['duration'])-float(before_audio['duration']))>.1:raise AppError(502,'quality_av_sync_mismatch','音轨时长与源视频相差超过0.1秒')
            if abs((audio['start_time']-actual['start_time'])-(before_audio['start_time']-original['start_time']))>.05:raise AppError(502,'quality_audio_offset_mismatch','音轨相对视频的起始偏移发生变化')
            if audio['sample_rate']!=before_audio['sample_rate'] or audio['channels']!=before_audio['channels']:raise AppError(502,'quality_audio_format_mismatch','音轨采样率或声道数与源视频不符')
        if hashlib.sha256(source.read_bytes()).hexdigest()!=snapshot['source_sha256']:raise AppError(409,'quality_source_changed','处理期间源视频发生变化')
        digest=hashlib.sha256(destination.read_bytes()).hexdigest()
        result={'requested_resolution':target,'model_actual_resolution':original,'postprocess_resolution':{'width':actual['width'],'height':actual['height']},
            'final_actual':actual,'method':snapshot['method'],'ai_super_resolution':bool(sr_receipt),'reconstruction':False,
            'temporal_model':bool(is_seed),'native_generation':False,'root_source_video_id':snapshot.get('root_source_video_id',snapshot.get('requested_source_video_id')),
            'super_resolution_receipt':sr_receipt,
            'gpu_release_check':gpu_release_evidence,
            'sha256':digest,'source_preserved':True,'elapsed_seconds':time.monotonic()-started,'command':command,
            'visual_quality':{'status':'human_review_required','score':None,'limits':'分辨率、解码和时序验证不判断脸部身份、动作、闪烁或原生细节质量'}}
        with factory() as session:
            item=session.get(QualityRender,identifier); source_video=session.get(m.VideoRevision,item.source_video_id)
            revision=(session.scalar(select(func.max(m.VideoRevision.revision)).where(m.VideoRevision.shot_id==source_video.shot_id)) or 0)+1
            video=m.VideoRevision(project_id=source_video.project_id,shot_id=source_video.shot_id,generation_job_id=source_video.generation_job_id,
                relative_path=relative,revision=revision,review_status='pending',instruction=source_video.instruction,
                metadata_json={**source_video.metadata_json,'sha256':digest,'width':actual['width'],'height':actual['height'],
                    'duration_seconds':actual['duration'],'frame_rate':actual['fps'],'file_size':actual['size_bytes'],
                    'quality':result,'source_video_id':source_video.id,'quality_render_id':identifier})
            video.metadata_json={**video.metadata_json,'root_source_video_id':snapshot.get('root_source_video_id',source_video.id),'processing_kind':'video_upscale',
                'processing_chain':[*source_video.metadata_json.get('processing_chain',[]),{'kind':'video_upscale','quality_render_id':identifier,
                    'method':snapshot['method'],'input_video_id':source_video.id,'input_sha256':snapshot['source_sha256'],'output_sha256':digest}]}
            session.add(video);session.flush()
            for check in checks:session.add(m.QualityCheck(video_revision_id=video.id,check_type=check['check_type'],status=check['status'],details_json=check['details']))
            item.status='generated';item.relative_path=relative;item.video_id=video.id;item.result_json=result;session.commit()
        if sr_receipt and not is_seed:
            # Verification publication is separate from the already successful media result.
            # An unwritable proof file must not turn an existing valid version into "failed".
            try:
                publish_sr_verification(settings,factory,identifier,snapshot,sr_receipt,relative,digest,actual)
            except Exception as warning:
                logging.getLogger(__name__).exception('SR output generated; verification publication failed')
                try:
                    with factory() as session:
                        item=session.get(QualityRender,identifier);item.result_json={**item.result_json,'verification_publication_warning':type(warning).__name__+': '+str(warning)[:600]};session.commit()
                except Exception:logging.getLogger(__name__).exception('Could not persist optional SR verification warning; generated media state retained')
    except Exception as exc:
        try:
            preserved=safe_media_path(settings.media_root,snapshot['source_relative_path'],must_exist=True)
            preserved=hashlib.sha256(preserved.read_bytes()).hexdigest()==snapshot['source_sha256']
        except Exception:preserved=False
        with factory() as session:
            item=session.get(QualityRender,identifier);item.status='queued' if getattr(exc,'native_deferred',False) else 'unknown' if getattr(exc,'native_unknown',False) else 'failed';item.error_message=str(exc)[:2400]
            item.result_json={'elapsed_seconds':time.monotonic()-started,'source_preserved':preserved,'source_not_modified_by_worker':True,'failure_type':type(exc).__name__};session.commit()

def recover_interrupted_quality(factory):
    # Previous local process termination is a known encoding interruption, never an AI retry.
    with factory() as session:
        for item in session.scalars(select(QualityRender).where(QualityRender.status=='running')):
            native=item.snapshot_json.get('native_execution',{});submission=native.get('paths',{}).get('submission')
            uncertain=False
            if item.snapshot_json.get('settings',{}).get('processing')=='seedvr2' and submission and Path(submission).is_file():
                try:uncertain=json.loads(Path(submission).read_text('utf-8')).get('status') not in {'REJECTED'}
                except (OSError,ValueError):uncertain=True
            item.status='unknown' if uncertain else 'failed'
            item.error_message='原生SeedVR派发进程中断，需先核对Comfy历史；未自动重复派发' if uncertain else '本地后处理进程中断，源视频保留；可显式创建新处理版本'
        session.commit()

async def quality_worker(settings,factory):
    recover_interrupted_quality(factory)
    while True:
        try:
            with factory() as session: pending=[(row.id,bool(row.snapshot_json.get('ai_super_resolution'))) for row in session.scalars(select(QualityRender).where(QualityRender.status=='queued').order_by(QualityRender.created_at).limit(20))]
            for identifier,is_ai in pending:
                if is_ai:
                    if not await run_ai_locked(settings,factory,identifier):continue
                else:await asyncio.to_thread(run_render,settings,factory,identifier)
                break
        except asyncio.CancelledError:raise
        except Exception:logging.getLogger(__name__).exception('Quality worker failed')
        await asyncio.sleep(1)

def publish_sr_verification(settings,factory,identifier,snapshot,sr_receipt,relative,digest,actual):
    proof=Path(snapshot['super_resolution']['runtime']['verification']);proof.parent.mkdir(parents=True,exist_ok=True)
    payload={'model_sha256':sr_receipt['model_sha256'],'script_sha256':snapshot['super_resolution']['script_sha256'],'quality_render_id':identifier,'relative_path':relative,'aspect_ratio':snapshot['settings']['aspect_ratio'],
        'sha256':digest,'full_decode':0,'all_frames_processed':True,'frames':actual['frames'],'method':'RealESRGAN_x4plus framewise','temporal_model':False}
    temporary=proof.with_suffix('.tmp.json');temporary.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8');temporary.replace(proof)
    with factory() as session:
        for candidate in session.scalars(select(m.LocalModel).where(m.LocalModel.kind=='upscale')):
            if Path(candidate.path).resolve()==Path(snapshot['super_resolution']['runtime']['model']).resolve():
                candidate.verification_json={'state':'verified','fingerprint':candidate.fingerprint,'quality_render_id':identifier,'actual_output':actual,'model_sha256':sr_receipt['model_sha256'],'scope':'image model applied to every video frame; no temporal or human visual approval'}
        session.commit()

from app.services.gpu_gate import gpu_submission
@gpu_submission
async def run_ai_locked(settings,factory,identifier):
    with factory() as session:
        render=session.get(QualityRender,identifier)
        if render is None or render.status!='queued':return False
        seed_fixed=getattr(render,'snapshot_json',{}).get('settings',{}).get('processing')=='seedvr2'
        active=['queued','submitting','submitted','running','cancelling']
        # Cloud jobs do not occupy this machine's GPU. Use the frozen provider,
        # then still confirm the actual Comfy queue and VRAM before inference.
        local_jobs=[select(m.GenerationJob.id).where(m.GenerationJob.status.in_(active),m.GenerationJob.provider=='comfyui_h3')]+[
            select(kind.id).where(kind.status.in_(active),kind.snapshot_json['service_snapshot']['provider'].as_string()=='comfyui')
            for kind in [m.CharacterGenerationJob,m.ProductionVisual]]
        if any(session.scalar(statement.limit(1)) for statement in local_jobs):return False
    try:
        # Seed's verified native controller is fixed independently of the H3
        # global setting. Never mutate that setting or free the other endpoint.
        if seed_fixed:
            release=await confirm_gpu_release(replace(settings,comfyui_base_url='http://127.0.0.1:8189'),require_seed_runtime=True)
        else:
            release=await confirm_gpu_release(settings)
        if release is None:return False
    except Exception as exc:
        with factory() as session:
            item=session.get(QualityRender,identifier)
            if item is None or item.status!='queued':return False
            if isinstance(exc,AppError) and exc.code==SEEDVR_RUNTIME_WAIT:
                if item.error_message!=exc.message:
                    item.error_message=exc.message
                    session.commit()
                return False
            item.status='failed';item.error_message='GPU派发前检查失败：'+str(exc)[:1600];item.result_json={'gpu_release_check':getattr(exc,'gpu_release_check',{}),'inference_submitted':False,'source_not_modified_by_worker':True};session.commit()
        return True
    await asyncio.to_thread(run_render,settings,factory,identifier,release)
    return True


async def confirm_gpu_release(settings,*,require_seed_runtime=False):
    """Called only while holding gpu_submission; HTTP200 is not unload completion."""
    import httpx
    release={'ack_is_not_unload_completion':True,'observations':[],'policy':'Comfy allocator <=1% (minimum 64MiB) and at least half device VRAM free, twice consecutively; conservative local scheduling, not universal model VRAM minimum'}
    try:
        async with httpx.AsyncClient(timeout=10,trust_env=False) as client:
            parsed=urlsplit(settings.comfyui_base_url)
            known_local=parsed.hostname in {'127.0.0.1','localhost','::1'} and parsed.port in {8188,8189}
            if known_local:
                # The two known local servers use the same card. A refused peer
                # is not running; timeout/unknown/HTTP errors are not idle proof.
                release['known_local_queues']={}
                for port in (8188,8189):
                    try:
                        peer=await client.get(f'http://127.0.0.1:{port}/queue');peer.raise_for_status();value=peer.json()
                        if not isinstance(value.get('queue_running'),list) or not isinstance(value.get('queue_pending'),list):raise ValueError('Unknown local Comfy queue response')
                    except Exception as error:
                        if port!=parsed.port and _connection_refused(error):
                            release['known_local_queues'][str(port)]={'state':'not_started_connection_refused'};continue
                        if port==parsed.port and require_seed_runtime:
                            raise AppError(503,SEEDVR_RUNTIME_WAIT,'等待本机 SeedVR2（8189）：请启动项目原生 ComfyUI 或检查 logs/comfyui_startup.log；服务就绪后自动接续。') from error
                        return None
                    busy=bool(value['queue_running'] or value['queue_pending'])
                    release['known_local_queues'][str(port)]={'state':'busy' if busy else 'idle','running':len(value['queue_running']),'pending':len(value['queue_pending'])}
                    if busy:return None
            if not (known_local and require_seed_runtime):
                queue=await client.get(settings.comfyui_base_url+'/queue');queue.raise_for_status();payload=queue.json()
                if payload.get('queue_running') or payload.get('queue_pending'):return None
            free=await client.post(settings.comfyui_base_url+'/free',json={'unload_models':True,'free_memory':True});free.raise_for_status()
            stable=0;deadline=time.monotonic()+30
            while time.monotonic()<deadline:
                stats=await client.get(settings.comfyui_base_url+'/system_stats');stats.raise_for_status()
                device=next((d for d in stats.json().get('devices',[]) if d.get('type')=='cuda'),{})
                total=device.get('vram_total',0);reserved=device.get('torch_vram_total');available=device.get('vram_free',0)
                release['observations'].append({'vram_total':total,'comfy_torch_reserved':reserved,'vram_free':available})
                ready=total>0 and reserved is not None and reserved<=max(64*1024**2,total*.01) and available>=total*.5
                stable=stable+1 if ready else 0
                if stable>=2:break
                await asyncio.sleep(.25)
            if stable<2:raise AppError(409,'super_resolution_vram_release_unverified','30秒内未确认Comfy缓存释放及保守显存余量，未启动超分')
    except Exception as exc:
        exc.gpu_release_check=release
        raise
    return release

def _connection_refused(error):
    pending=[error];seen=set()
    while pending:
        item=pending.pop()
        if id(item) in seen:continue
        seen.add(id(item))
        if isinstance(item,ConnectionRefusedError) or getattr(item,'winerror',None)==10061 or getattr(item,'errno',None) in {61,111,10061}:return True
        pending.extend(value for value in [getattr(item,'__cause__',None),getattr(item,'__context__',None),getattr(item,'reason',None)] if isinstance(value,BaseException))
        pending.extend(value for value in getattr(item,'exceptions',()) if isinstance(value,BaseException))
    return False
