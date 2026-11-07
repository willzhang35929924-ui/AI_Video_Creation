"""Isolated SQLite orchestration contracts; no model/service requests are made."""
import copy
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import models as m
from app.core.errors import AppError
from app.services import studio_pipeline as p
from app.services.task_routing import SLOTS
from test_studio_pipeline import studio


def frozen(slot, service=None, *, configured=False, parameters=None):
    snapshot = {'service_id':service.id,'service_revision':service.revision,'provider':service.provider,
                'module':service.module,'model_id':service.default_model,'settings':service.settings_json,
                'is_cloud':False} if service else None
    return {'slot':slot,'configured':configured,'profile':{'revision':1 if configured else 0,
            'settings':{'parameters':parameters or {},'max_retries':0,'allow_fallback':False}},
            'selected':{'service_snapshot':snapshot} if snapshot else None,'choices':[],
            'allow_cloud_costs':False,'allow_paid_fallback':False}


def prepare(studio, monkeypatch, *, dialogue=False, lip=False, selected=True, quality=False, context_mode=False):
    shot=studio.shots[1];session=studio.session
    person=session.scalar(select(m.ShotCharacter).where(m.ShotCharacter.shot_id==shot.id))
    if dialogue:
        session.add(m.DialogueLine(shot_id=shot.id,participant_id=person.id,order=1,text='中性对白。'))
    if lip:
        detail=session.scalar(select(m.ShotDetail).where(m.ShotDetail.shot_id==shot.id))
        detail.source_json={'production':{'requested_controls':{'require_lip_sync':True}}}
    service=session.scalar(select(m.AIService).where(m.AIService.module=='video_generation'))
    if context_mode:
        # Public services opt into Context-IR explicitly on every OS. Leave the
        # default fixture native/legacy so its existing route coverage remains.
        service.default_model='MiniMax_H3_w6a8.safetensors'
        service.settings_json={**service.settings_json,'workflow_profile':'context_ir'}
    import app.services.task_routing as routing
    def freeze(session,settings,project_id,shot_id,slot,*args):
        active=slot in {'dialogue_generation','lip_sync'} and dialogue
        return frozen(slot,service if active and selected else None,configured=active)
    monkeypatch.setattr(routing,'freeze_route',freeze)
    session.flush()
    options={'shot_ids':[shot.id]}
    if quality:options['quality']={'resolution':'720p','aspect_ratio':'16:9','mode':'standard','processing':'lanczos'}
    run=p.create_pipeline(session,studio.settings,studio.project.id,options);session.commit()
    return run,shot,person


def find(run,kind):
    return next(n for n in run.nodes_json if n['kind']==kind)


@pytest.mark.parametrize('context_mode',[False,True])
def test_every_slot_and_shot_snapshot_is_frozen_without_dispatch(studio,monkeypatch,context_mode):
    run,shot,_=prepare(studio,monkeypatch,dialogue=True,lip=True,quality=True,context_mode=context_mode)
    assert set(run.snapshot_json['routes'])==set(SLOTS)
    assert set(run.snapshot_json['routes_by_shot'][shot.id])==set(SLOTS)
    assert run.snapshot_json['context_production'] is context_mode
    if context_mode:
        assert run.snapshot_json['h3_context_by_shot'][shot.id]['service']['h3_workflow']['actual_steps']==20
        assert not any(n['kind']=='keyframe' for n in run.nodes_json)
    for kind in (*([] if context_mode else ['keyframe']),'video','tts','lip_sync','quality','quality_check'):
        assert 'route' in find(run,kind)
    assert find(run,'audio_mix')['dependencies']==['video:'+shot.id,find(run,'tts')['id']]
    assert find(run,'lip_sync')['dependencies']==['audio_mix:'+shot.id]
    assert find(run,'quality')['dependencies']==['lip_sync:'+shot.id]
    assert find(run,'quality_check')['dependencies']==['quality:'+shot.id]
    assert studio.session.scalar(select(m.MediaProcessTask.id)) is None
    assert studio.session.scalar(select(m.GenerationJob.id)) is None


def test_unconfigured_tts_is_optional_and_preserves_legacy_video_chain(studio,monkeypatch):
    run,shot,_=prepare(studio,monkeypatch)
    optional=find(run,'tts_optional')
    assert optional['status']=='skipped' and optional['optional']
    assert not any(n['kind'] in p.MEDIA_KINDS for n in run.nodes_json)
    assert find(run,'quality_check')['dependencies']==['video:'+shot.id]
    assert p._leaf(run.nodes_json,shot.id)['kind']=='quality_check'


def test_explicit_unavailable_tts_is_not_silently_optional(studio,monkeypatch):
    run,_,_=prepare(studio,monkeypatch,dialogue=True,selected=False)
    assert find(run,'tts')['status']=='pending'
    assert not any(n['kind']=='tts_optional' for n in run.nodes_json)
    assert find(run,'audio_mix')['audio_policy']=='replace_dialogue'
    assert 'raw video' in find(run,'audio_mix')['disclosure']


def test_required_lip_without_model_blocks_before_any_media_submit(studio,monkeypatch):
    run,_,_=prepare(studio,monkeypatch,lip=True)
    def forbidden(*args,**kwargs):raise AssertionError('must not create a task')
    monkeypatch.setattr('app.services.media_processing.create_task',forbidden)
    with pytest.raises(AppError) as failure:p._media(studio.session,studio.settings,run,find(run,'lip_sync'),True)
    assert failure.value.code=='task_route_blocked'


def test_tts_passes_role_and_only_explicit_role_reference(studio,monkeypatch):
    run,shot,person=prepare(studio,monkeypatch,dialogue=True)
    person.performance_json={'reference_audio_asset_id':'owned-reference','voice_profile':'explicit-provider-voice'}
    calls=[]
    def create(session,settings,payload,kind,route):
        calls.append((payload,kind,route));task=m.MediaProcessTask(project_id=payload.project_id,shot_id=payload.shot_id,kind=kind,slot=payload.slot,status='queued')
        session.add(task);session.flush();return task
    monkeypatch.setattr('app.services.media_processing.create_task',create)
    entry=find(run,'tts');p._media(studio.session,studio.settings,run,entry,True)
    payload,kind,route=calls[0]
    assert kind=='tts' and payload.inputs=={'dialogue_line_id':entry['target_id'],'participant_id':person.id,'reference_audio_asset_id':'owned-reference'}
    assert payload.shot_id==shot.id and route==run.snapshot_json['routes_by_shot'][shot.id]['dialogue_generation']
    assert entry['child_id'] and entry['status']=='running'
    assert not payload.allow_paid_fallback


@pytest.mark.parametrize('status,expected',[('unknown','unknown'),('blocked','exception'),('failed','exception'),('running','running'),('queued','running')])
def test_media_child_reconciliation_never_resubmits_unknown(studio,monkeypatch,status,expected):
    run,shot,_=prepare(studio,monkeypatch,dialogue=True)
    task=m.MediaProcessTask(project_id=studio.project.id,shot_id=shot.id,kind='tts',slot='dialogue_generation',status=status,
                            result_json={'attempt_history':[{'index':1,'status':status}]},error_message='controlled result')
    studio.session.add(task);studio.session.flush()
    entry=find(run,'tts');entry['child_id']=task.id
    monkeypatch.setattr('app.services.media_processing.create_task',lambda *a,**k:pytest.fail('known child must not be recreated'))
    p._media(studio.session,studio.settings,run,entry,True)
    assert entry['status']==expected and entry['child_id']==task.id and entry['attempt_history'][0]['status']==status


def test_mixing_then_lip_use_direct_predecessor_video_and_whole_mix_audio(studio,monkeypatch):
    run,shot,person=prepare(studio,monkeypatch,dialogue=True,lip=True)
    find(run,'video').update(status='succeeded',video_id='raw-video')
    find(run,'tts').update(status='succeeded',audio_id='one-line-audio')
    calls=[]
    def create(session,settings,payload,kind,route):
        calls.append((copy.deepcopy(payload.inputs),kind,route));task=m.MediaProcessTask(project_id=payload.project_id,shot_id=payload.shot_id,kind=kind,slot=payload.slot,status='queued')
        session.add(task);session.flush();return task
    monkeypatch.setattr('app.services.media_processing.create_task',create)
    mix=find(run,'audio_mix');p._media(studio.session,studio.settings,run,mix,True)
    assert calls[0][:2]==({'video_id':'raw-video','operation':'audio_mix','audio_ids':['one-line-audio'],'mode':'replace_dialogue'},'audio_mix')
    assert calls[0][2] is None
    mix.update(status='succeeded',video_id='mixed-video',audio_id='complete-mixed-track')
    p._media(studio.session,studio.settings,run,find(run,'lip_sync'),True)
    assert calls[1][0]=={'video_id':'mixed-video','audio_id':'complete-mixed-track','participant_id':person.id}
    assert find(run,'lip_sync')['source_video_id']=='mixed-video'


def test_lip_refuses_one_tts_clip_when_full_mix_is_missing(studio,monkeypatch):
    run,_,_=prepare(studio,monkeypatch,dialogue=True,lip=True)
    find(run,'tts').update(status='succeeded',audio_id='one-line')
    find(run,'audio_mix').update(status='succeeded',video_id='mixed-video')
    with pytest.raises(AppError) as failure:p._media(studio.session,studio.settings,run,find(run,'lip_sync'),True)
    assert failure.value.code=='studio_lip_audio_required'


def test_quality_consumes_lip_output_and_keeps_frozen_upscale_route(studio,monkeypatch):
    run,shot,_=prepare(studio,monkeypatch,dialogue=True,lip=True,quality=True)
    find(run,'video').update(status='succeeded',video_id='raw')
    find(run,'lip_sync').update(status='succeeded',video_id='lip-derived')
    route=frozen('video_upscale',configured=True)
    run.snapshot_json['routes_by_shot'][shot.id]['video_upscale']=route
    calls=[]
    def create(session,settings,source_id,quality,route_snapshot):
        calls.append((source_id,route_snapshot));return SimpleNamespace(id='quality-child')
    monkeypatch.setattr('app.services.video_quality.create_render',create)
    entry=find(run,'quality');p._quality(studio.session,studio.settings,run,entry)
    assert calls==[('lip-derived',route)] and entry['source_video_id']=='lip-derived'


def test_unknown_upscale_never_becomes_running_again(studio,monkeypatch):
    run,shot,_=prepare(studio,monkeypatch,quality=True)
    find(run,'video')['video_id']='raw-preserved'
    job=m.GenerationJob(project_id=studio.project.id,shot_id=shot.id,provider='controlled_contract',prompt='no inference')
    studio.session.add(job);studio.session.flush()
    video=m.VideoRevision(project_id=studio.project.id,shot_id=shot.id,generation_job_id=job.id,revision=1,relative_path='unused.mp4')
    studio.session.add(video);studio.session.flush()
    render=m.QualityRender(source_video_id=video.id,status='unknown',snapshot_json={})
    studio.session.add(render);studio.session.flush()
    entry=find(run,'quality');entry['child_id']=render.id
    p._quality(studio.session,studio.settings,run,entry)
    assert entry['status']=='unknown' and entry['child_id']==render.id


def test_review_uses_last_derived_leaf_and_waits_for_technical_check(studio,monkeypatch):
    run,shot,_=prepare(studio,monkeypatch,dialogue=True,lip=True,quality=True)
    for entry in run.nodes_json:entry.update(status='succeeded')
    find(run,'video')['video_id']='raw'
    find(run,'lip_sync')['video_id']='lip-derived'
    find(run,'quality')['video_id']='upscaled-lip'
    check=find(run,'quality_check');check['status']='pending'
    p._overall(run,run.nodes_json);assert run.status=='running' and run.result_json['videos']=={}
    check.update(status='succeeded',video_id='upscaled-lip')
    p._overall(run,run.nodes_json)
    assert run.status=='waiting_review' and run.result_json['videos']=={shot.id:'upscaled-lip'}


def test_deleted_shot_cannot_dispatch_a_media_child(studio,monkeypatch):
    run,shot,_=prepare(studio,monkeypatch,dialogue=True)
    shot.status='deleted';studio.session.flush()
    monkeypatch.setattr('app.services.media_processing.create_task',lambda *a,**k:pytest.fail('deleted shot'))
    with pytest.raises(AppError) as failure:p._media(studio.session,studio.settings,run,find(run,'tts'),True)
    assert failure.value.code=='studio_shot_deleted'


def test_frozen_image_route_does_not_follow_later_service_revision(studio,monkeypatch):
    run,shot,_=prepare(studio,monkeypatch)
    service=studio.session.scalar(select(m.AIService).where(m.AIService.module=='character_generation'))
    run.snapshot_json['routes_by_shot'][shot.id]['keyframe_image']=frozen('keyframe_image',service,configured=True)
    service.revision+=1;studio.session.flush()
    with pytest.raises(AppError) as failure:p._service(studio.session,run,'image','keyframe_image',shot.id)
    assert failure.value.code=='route_service_changed'


def test_tts_output_cannot_attach_after_role_version_changes(studio,monkeypatch):
    run,shot,person=prepare(studio,monkeypatch,dialogue=True)
    entry=find(run,'tts')
    old=person.character_version_id
    other=m.CharacterVersion(character_id=person.character_id,version=2,identity_json={})
    studio.session.add(other);studio.session.flush();person.character_version_id=other.id
    asset=m.AudioAsset(project_id=studio.project.id,shot_id=shot.id,participant_id=person.id,
                       character_version_id=old,relative_path='not-consumed.wav',sha256='0'*64)
    studio.session.add(asset);studio.session.flush()
    task=m.MediaProcessTask(project_id=studio.project.id,shot_id=shot.id,kind='tts',slot='dialogue_generation',status='succeeded',result_json={'audio_id':asset.id})
    studio.session.add(task);studio.session.flush();entry['child_id']=task.id
    with pytest.raises(AppError) as failure:p._media(studio.session,studio.settings,run,entry,True)
    assert failure.value.code=='studio_audio_output_ownership'


def test_pending_processing_does_not_expose_latest_raw_as_review_output(studio,monkeypatch):
    run,shot,_=prepare(studio,monkeypatch,dialogue=True)
    job=m.GenerationJob(project_id=studio.project.id,shot_id=shot.id,provider='controlled_contract',prompt='no inference')
    studio.session.add(job);studio.session.flush()
    raw=m.VideoRevision(project_id=studio.project.id,shot_id=shot.id,generation_job_id=job.id,revision=1,relative_path='raw.mp4')
    studio.session.add(raw);studio.session.flush()
    find(run,'video').update(status='succeeded',video_id=raw.id)
    dto=p.overview(studio.session,studio.project.id)
    view=next(s for s in dto['shots'] if s['id']==shot.id)
    assert view['video'] is None and view['status']=='pending'
    assert studio.session.get(m.VideoRevision,raw.id) is raw


def test_quality_override_is_the_actual_route_cache_contract(studio,monkeypatch):
    run,shot,_=prepare(studio,monkeypatch,quality=True)
    service=studio.session.scalar(select(m.AIService).where(m.AIService.module=='video_generation'))
    route=frozen('video_generation',service,configured=True,parameters={'steps':12,'width':768,'height':448})
    run.snapshot_json['routes_by_shot'][shot.id]['video_generation']=route
    actual=p.generation_parameters(run,shot)
    assert actual['steps']==20 and (actual['width'],actual['height'])==(1024,576)
    snapshot={'service_snapshot':route['selected']['service_snapshot']}
    assert p._matching_route_output(run,'video_generation',shot.id,snapshot,actual,actual)
    changed={**actual,'steps':12}
    assert not p._matching_route_output(run,'video_generation',shot.id,snapshot,changed,actual)
