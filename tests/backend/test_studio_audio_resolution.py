"""Isolated repair orchestration tests, not evidence of AI inference."""
import copy
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import models as m
from app.core.errors import AppError
from app.models.studio import StudioRun
from app.schemas_studio_audio_resolution import AudioDurationApply, AudioDurationChoice
from app.services import studio_audio_resolution as repair
from app.services import studio_pipeline as pipeline
from app.services.runtime_registry import file_sha
from app.services.cinematic_quality import generation_inputs_hash
from test_studio_pipeline import studio


@pytest.fixture
def failed(studio, monkeypatch):
    s = studio.session
    shot = studio.shots[1]
    person = s.scalar(select(m.ShotCharacter).where(m.ShotCharacter.shot_id == shot.id))
    line = m.DialogueLine(shot_id=shot.id, participant_id=person.id, order=1,
                          text='Mommy! Look what Santa left in a mini dim sum basket!')
    s.add(line); s.flush()
    job = m.GenerationJob(project_id=shot.project_id, shot_id=shot.id, status='generated',
        provider='isolated_fixture', prompt='Synthetic fixture, not AI output', seed=1,
        parameters_json={'screenplay_snapshot':{'cinematic':{'preflight':{
            'generation_inputs_hash':generation_inputs_hash(s,shot)}}}}, reference_assets_json=[])
    s.add(job); s.flush()
    root = studio.settings.media_root
    root.mkdir(parents=True, exist_ok=True)
    (root / 'video.mp4').write_bytes(b'isolated-video-fixture')
    (root / 'tts.wav').write_bytes(b'isolated-audio-fixture')
    video_sha, audio_sha = file_sha(root / 'video.mp4'), file_sha(root / 'tts.wav')
    video = m.VideoRevision(project_id=shot.project_id, shot_id=shot.id, generation_job_id=job.id,
        relative_path='video.mp4', revision=1, metadata_json={'sha256':video_sha})
    s.add(video); s.flush()
    route = {'slot':'dialogue_generation', 'configured':True, 'selected':{'service_snapshot':{
        'provider':'local_runtime', 'settings':{'runtime_family':'indextts25'}}}}
    task = m.MediaProcessTask(project_id=shot.project_id, shot_id=shot.id, kind='tts',
        slot='dialogue_generation', status='succeeded', snapshot_json={
            'inputs':{'text':line.text}, 'parameters':{'use_bf16':True}, 'route':route},
        result_json={'runtime_receipt':{'actual':{'language':'ZH','speed':1.0}}})
    s.add(task); s.flush()
    audio = m.AudioAsset(project_id=shot.project_id, shot_id=shot.id, participant_id=person.id,
        character_version_id=person.character_version_id, dialogue_line_id=line.id,
        task_id=task.id, relative_path='tts.wav', sha256=audio_sha)
    s.add(audio); s.flush()
    task.result_json = {**task.result_json, 'audio_id':audio.id}
    mix = m.MediaProcessTask(project_id=shot.project_id, shot_id=shot.id, kind='audio_mix',
        slot='dialogue_generation', status='failed', snapshot_json={'sources':{
            'video_path':{'id':video.id,'sha256':video_sha},
            'audio_0':{'id':audio.id,'sha256':audio_sha,'metadata':{}}}},
        result_json={'error_code':'audio_longer_than_video'})
    s.add(mix); s.flush()
    nodes = [pipeline.node('video',shot.id), pipeline.node('tts',line.id),
        pipeline.node('audio_mix',shot.id,['video:'+shot.id,'tts:'+line.id]),
        pipeline.node('quality',shot.id,['audio_mix:'+shot.id]),
        pipeline.node('quality_check',shot.id,['quality:'+shot.id]),
        pipeline.node('video',studio.shots[2].id)]
    nodes[0].update(status='succeeded',child_id=job.id,video_id=video.id)
    nodes[1].update(status='succeeded',child_id=task.id,audio_id=audio.id,
                    shot_id=shot.id,participant_id=person.id)
    nodes[2].update(status='exception',child_id=mix.id,code='audio_longer_than_video',error='超长')
    nodes[3].update(status='waiting_dependencies')
    nodes[4].update(status='waiting_dependencies')
    nodes[5].update(status='succeeded',video_id='other-video')
    run = StudioRun(project_id=shot.project_id, script_revision_id=studio.script.id, status='running',
        nodes_json=nodes, snapshot_json={'shot_ids':[shot.id,studio.shots[2].id],
            'options':{'allow_cloud_costs':False}, 'routes':{'dialogue_generation':route}}, result_json={})
    s.add(run); s.commit()
    monkeypatch.setattr('app.services.media_processing._probe',
        lambda path,settings,audio=False:{'duration':5.189659863945578 if audio else 3.041688})
    return SimpleNamespace(**vars(studio),shot=shot,line=line,person=person,run=run,mix=mix,
        video=video,audio=audio,tts=task,node_id=nodes[2]['id'])


def choice(ctx, action='adjust_speed', **values):
    source = repair.inspect(ctx.session,ctx.settings,ctx.project.id,ctx.node_id)
    return AudioDurationChoice(expected_run_id=ctx.run.id,source_fingerprint=source['source_fingerprint'],
        action=action,client_request_id=values.pop('client_request_id','isolated-command-1'),**values)


def execute(ctx, payload):
    plan = repair.preview(ctx.session,ctx.settings,ctx.project.id,ctx.node_id,payload)
    command = AudioDurationApply(**payload.model_dump(),preview_token=plan['preview_token'])
    result = repair.apply(ctx.session,ctx.settings,ctx.project.id,ctx.node_id,command)
    ctx.session.commit()
    return result,command,plan


def test_inspect_reports_measured_source_and_actual_wrong_language(failed):
    source = repair.inspect(failed.session,failed.settings,failed.project.id,failed.node_id)
    assert source['video']['duration_seconds'] == 3.041688
    assert source['audio_duration_seconds'] == 5.189659863945578
    assert source['dialogues'][0]['language']=='ZH'
    cap=source['capabilities']['adjust_speed']
    assert cap['recommended_speed']==1.9 and cap['min']==.5 and cap['max']==2
    assert cap['languages']==['ZH','EN','JA','ES','AR']


def test_preview_is_read_only_and_does_not_dispatch(failed):
    before=copy.deepcopy(failed.run.nodes_json)
    payload=choice(failed,speed=1.9,dialogue_line_id=failed.line.id,language='EN')
    plan=repair.preview(failed.session,failed.settings,failed.project.id,failed.node_id,payload)
    assert plan['parameters']=={'dialogue_line_id':failed.line.id,'speed':1.9,'language':'EN'}
    assert failed.run.nodes_json==before
    assert len(list(failed.session.scalars(select(m.MediaProcessTask))))==2


def test_speed_requeues_only_this_tts_and_descendants_preserving_sources(failed):
    original_nodes=copy.deepcopy(failed.run.nodes_json)
    result,_,_=execute(failed,choice(failed,speed=1.9,dialogue_line_id=failed.line.id,language='EN'))
    assert result['status']=='queued' and len(result['affected_nodes'])==4
    assert failed.run.nodes_json[0]==original_nodes[0] and failed.run.nodes_json[-1]==original_nodes[-1]
    tts=failed.run.nodes_json[1]
    assert tts['tts_parameters_override']=={'speed':1.9,'language':'EN'}
    assert tts['child_id'] is None and tts['history'][0]['previous']['child_id']==failed.tts.id
    assert failed.tts.status=='succeeded' and failed.mix.status=='failed'
    assert failed.session.get(m.AudioAsset,failed.audio.id)
    assert failed.line.text==tts['audio_resolution_lineage']['original_text']


def test_shorten_is_per_run_text_override_with_immutable_screenplay(failed):
    original=failed.line.text
    execute(failed,choice(failed,'shorten_dialogue',text='Look, a gift!',dialogue_line_id=failed.line.id,language='EN'))
    assert failed.line.text==original
    assert failed.run.nodes_json[1]['tts_text_override']=='Look, a gift!'
    assert failed.run.nodes_json[1]['tts_parameters_override']=={'language':'EN'}
    assert failed.run.nodes_json[0]['video_id']==failed.video.id
    assert failed.run.result_json['audio_duration_resolutions']['isolated-command-1']['change']['effective_text']=='Look, a gift!'


def test_extend_updates_revision_requeues_video_and_preserves_tts(failed,monkeypatch):
    before=copy.deepcopy(failed.run.nodes_json[1]);revision=failed.shot.revision
    failed.run.snapshot_json={**failed.run.snapshot_json,'h3_context_by_shot':{
        failed.shot.id:{'inputs':{'explicit_visual_ids':['image-a'],'audio_policy':'tts'},'service':{'actual':'frozen'}}}}
    captured=[]
    def collect(session,settings,shot,**kwargs):
        captured.append((shot.duration_seconds,shot.revision,kwargs))
        return {'duration_seconds':shot.duration_seconds,'shot_revision':shot.revision,'fingerprint':'new-context'}
    monkeypatch.setattr('app.services.context_production_inputs.collect_context_inputs',collect)
    monkeypatch.setattr('app.services.context_production_inputs.restore_context_inputs',lambda *a:([],[]))
    result,_,_=execute(failed,choice(failed,'extend_video',target_duration_seconds=6))
    assert failed.shot.duration_seconds==6 and failed.shot.revision==revision+1
    assert failed.run.nodes_json[1]==before
    assert failed.run.nodes_json[0]['child_id'] is None
    assert 'tts:'+failed.line.id not in result['affected_nodes']
    assert captured[0][2]['image_ids']==['image-a']
    assert failed.run.snapshot_json['h3_context_by_shot'][failed.shot.id]['inputs']['duration_seconds']==6
    assert failed.shot.id in failed.run.snapshot_json['options']['force_shot_ids']


def test_repeated_apply_replays_durable_receipt_without_second_retry(failed):
    result,command,_=execute(failed,choice(failed,speed=1.9,dialogue_line_id=failed.line.id))
    before=copy.deepcopy(failed.run.nodes_json)
    again=repair.apply(failed.session,failed.settings,failed.project.id,failed.node_id,command)
    assert again=={**result,'replayed':True} and failed.run.nodes_json==before
    audit=list(failed.session.scalars(select(m.AuditEvent).where(m.AuditEvent.action=='audio_duration_resolution_applied')))
    assert len(audit)==1


def test_reused_request_id_cannot_apply_other_choices(failed):
    _,command,_=execute(failed,choice(failed,speed=1.9,dialogue_line_id=failed.line.id))
    bad=command.model_copy(update={'speed':2.0})
    with pytest.raises(AppError,match='请求编号'):
        repair.apply(failed.session,failed.settings,failed.project.id,failed.node_id,bad)


@pytest.mark.parametrize('field,value',[('source_fingerprint','0'*64),('expected_run_id','other-run')])
def test_stale_run_or_source_is_rejected(failed,field,value):
    payload=choice(failed,speed=1.9,dialogue_line_id=failed.line.id).model_copy(update={field:value})
    with pytest.raises(AppError):
        repair.preview(failed.session,failed.settings,failed.project.id,failed.node_id,payload)


def test_source_file_change_is_rejected(failed):
    (failed.settings.media_root/'tts.wav').write_bytes(b'changed')
    with pytest.raises(AppError) as exc:
        repair.inspect(failed.session,failed.settings,failed.project.id,failed.node_id)
    assert exc.value.code=='audio_resolution_source_changed'


@pytest.mark.parametrize('status',['unknown','running','queued'])
def test_existing_active_or_unknown_tts_never_requeues(failed,status):
    payload=choice(failed,speed=1.9,dialogue_line_id=failed.line.id)
    failed.tts.status=status;failed.session.flush()
    with pytest.raises(AppError):
        repair.preview(failed.session,failed.settings,failed.project.id,failed.node_id,payload)
    assert failed.run.nodes_json[1]['child_id']==failed.tts.id


@pytest.mark.parametrize('values',[
    {'action':'extend_video','target_duration_seconds':4},
    {'action':'shorten_dialogue','text':'   '},
    {'action':'adjust_speed','speed':2.1},
    {'action':'adjust_speed','speed':1.9,'language':'RU'},
])
def test_invalid_choices_do_not_mutate_dag(failed,values):
    before=copy.deepcopy(failed.run.nodes_json)
    action=values.pop('action')
    payload=choice(failed,action,dialogue_line_id=failed.line.id,**values)
    with pytest.raises(AppError):
        repair.preview(failed.session,failed.settings,failed.project.id,failed.node_id,payload)
    assert failed.run.nodes_json==before


def test_adapter_without_declared_speed_cannot_accept_speed(failed):
    failed.tts.snapshot_json={**failed.tts.snapshot_json,'route':{'selected':{'service_snapshot':{
        'provider':'custom_unknown','settings':{}}}}}
    payload=choice(failed,speed=1.9,dialogue_line_id=failed.line.id)
    with pytest.raises(AppError) as exc:
        repair.preview(failed.session,failed.settings,failed.project.id,failed.node_id,payload)
    assert exc.value.code=='audio_resolution_speed_unsupported'


def test_current_language_kept_without_explicit_user_choice(failed):
    _,_,plan=execute(failed,choice(failed,speed=1.9,dialogue_line_id=failed.line.id))
    assert 'language' not in failed.run.nodes_json[1]['tts_parameters_override']
    assert any('EN' in warning for warning in plan['warnings'])


def test_pipeline_dispatch_uses_per_line_text_and_parameters(failed,monkeypatch):
    execute(failed,choice(failed,'shorten_dialogue',text='Mommy! A surprise!',
                         dialogue_line_id=failed.line.id,language='EN'))
    calls=[]
    def create(session,settings,payload,kind,route,**kwargs):
        calls.append((payload,kwargs))
        task=m.MediaProcessTask(project_id=payload.project_id,shot_id=payload.shot_id,
            kind=kind,slot=payload.slot,status='queued')
        session.add(task);session.flush();return task
    monkeypatch.setattr('app.services.media_processing.create_task',create)
    pipeline._media(failed.session,failed.settings,failed.run,failed.run.nodes_json[1],True)
    payload,kwargs=calls[0]
    assert payload.inputs['dialogue_line_id']==failed.line.id
    assert payload.parameters=={'language':'EN'}
    assert kwargs=={'dialogue_text_override':'Mommy! A surprise!'}
    assert failed.run.nodes_json[1]['child_id']!=failed.tts.id


def test_real_task_creation_freezes_spoken_override_and_keeps_source_line(failed):
    from app.schemas_routing import TaskSubmit
    from app.services.media_processing import create_task
    route={'selected':{'service_snapshot':{'provider':'openai_tts','settings':{}}},
           'profile':{'settings':{'parameters':{'voice':'alloy'}}}}
    task=create_task(failed.session,failed.settings,TaskSubmit(slot='dialogue_generation',
        project_id=failed.project.id,shot_id=failed.shot.id,
        inputs={'dialogue_line_id':failed.line.id},parameters={'speed':1.9}),
        kind='tts',route=route,dialogue_text_override='Mommy! A surprise!')
    assert task.status=='queued'
    assert task.snapshot_json['dialogue_line_id']==failed.line.id
    assert task.snapshot_json['inputs']['text']=='Mommy! A surprise!'
    assert task.snapshot_json['dialogue_text_override']['original_text']==failed.line.text
    assert task.snapshot_json['parameters']['speed']==1.9
    assert task.id!=failed.tts.id


def test_pipeline_accepts_effective_text_and_preserves_stale_text_guard(failed):
    execute(failed,choice(failed,'shorten_dialogue',text='Look, a gift!',dialogue_line_id=failed.line.id))
    node=failed.run.nodes_json[1]
    failed.tts.snapshot_json={**failed.tts.snapshot_json,'inputs':{'text':'Look, a gift!'}}
    node['child_id']=failed.tts.id
    pipeline._media(failed.session,failed.settings,failed.run,node,False)
    assert node['status']=='succeeded' and node['audio_id']==failed.audio.id
    failed.tts.snapshot_json={**failed.tts.snapshot_json,'inputs':{'text':'Unrelated text'}}
    with pytest.raises(AppError) as exc:
        pipeline._media(failed.session,failed.settings,failed.run,node,False)
    assert exc.value.code=='studio_dialogue_changed'


def test_repeated_duration_failure_can_be_resolved_again(failed):
    execute(failed,choice(failed,'shorten_dialogue',text='Mommy! A surprise!',dialogue_line_id=failed.line.id))
    # The next real mix failed after a new audio was generated; emulate only
    # durable worker reconciliation here, not model inference.
    nodes=copy.deepcopy(failed.run.nodes_json)
    nodes[1].update(status='succeeded',child_id=failed.tts.id,audio_id=failed.audio.id)
    nodes[2].update(status='exception',child_id=failed.mix.id,code='audio_longer_than_video')
    failed.tts.snapshot_json={**failed.tts.snapshot_json,'inputs':{'text':'Mommy! A surprise!'},
        'parameters':{'speed':1.5,'language':'EN'}}
    failed.tts.result_json={**failed.tts.result_json,'runtime_receipt':{'actual':{'speed':1.5,'language':'EN'}}}
    failed.run.nodes_json=nodes;failed.session.commit()
    current=repair.inspect(failed.session,failed.settings,failed.project.id,failed.node_id)
    assert current['dialogues'][0]['text']=='Mommy! A surprise!'
    assert current['dialogues'][0]['speed']==1.5 and current['dialogues'][0]['language']=='EN'
    execute(failed,choice(failed,'adjust_speed',speed=2,language='EN',dialogue_line_id=failed.line.id,
                         client_request_id='second-distinct-choice'))
    assert failed.run.nodes_json[1]['tts_text_override']=='Mommy! A surprise!'
    assert failed.run.nodes_json[1]['tts_parameters_override']=={'speed':2,'language':'EN'}
    assert len(failed.run.result_json['audio_duration_resolutions'])==2


def test_unknown_other_task_on_same_shot_blocks_without_discarding_it(failed):
    rogue=m.MediaProcessTask(project_id=failed.project.id,shot_id=failed.shot.id,kind='tts',
        slot='dialogue_generation',status='unknown')
    failed.session.add(rogue);failed.session.commit()
    payload=choice(failed,speed=1.9,dialogue_line_id=failed.line.id)
    with pytest.raises(AppError) as exc:
        repair.preview(failed.session,failed.settings,failed.project.id,failed.node_id,payload)
    assert exc.value.code=='audio_resolution_busy' and rogue.status=='unknown'


def test_api_get_preview_apply_and_network_retry_are_consistent(failed):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.studio_audio_resolution import router
    from app.core.db import get_db
    from app.core.errors import app_error_handler
    app=FastAPI();app.state.settings=failed.settings;app.include_router(router)
    app.add_exception_handler(AppError,app_error_handler)
    def db():
        with failed.factory() as session:
            yield session
    app.dependency_overrides[get_db]=db
    path=f'/api/studio/projects/{failed.project.id}/audio-duration/{failed.node_id}'
    with TestClient(app) as client:
        source=client.get(path)
        assert source.status_code==200 and source.json()['dialogues'][0]['language']=='ZH'
        payload=choice(failed,speed=1.9,dialogue_line_id=failed.line.id,language='EN').model_dump()
        preview=client.post(path+'/preview',json=payload)
        assert preview.status_code==200
        command={**payload,'preview_token':preview.json()['preview_token']}
        response=client.post(path+'/apply',json=command)
        assert response.status_code==200 and response.json()['replayed'] is False
        again=client.post(path+'/apply',json=command)
        assert again.status_code==200 and again.json()['replayed'] is True
        failed.session.expire_all()
        assert failed.session.get(StudioRun,failed.run.id).nodes_json[1]['tts_parameters_override']=={'speed':1.9,'language':'EN'}


@pytest.mark.parametrize('kind',['dialogue','frozen_context','frozen_routes','tts_parameters'])
def test_cas_covers_live_subdocuments_context_routes_and_parameters(failed,kind):
    payload=choice(failed,speed=1.9,dialogue_line_id=failed.line.id)
    if kind=='dialogue':
        failed.line.delivery_json={'emotion':'new edited delivery'}
    elif kind=='frozen_context':
        failed.run.snapshot_json={**failed.run.snapshot_json,'h3_context_by_shot':{failed.shot.id:{'inputs':{'changed':True}}}}
    elif kind=='frozen_routes':
        failed.run.snapshot_json={**failed.run.snapshot_json,'routes':{'dialogue_generation':{'selected':{'changed':True}}}}
    else:
        failed.tts.snapshot_json={**failed.tts.snapshot_json,'parameters':{'speed':1.4}}
    failed.session.flush()
    with pytest.raises(AppError) as exc:
        repair.preview(failed.session,failed.settings,failed.project.id,failed.node_id,payload)
    assert exc.value.code=='audio_resolution_stale'


def test_existing_stale_source_requires_new_production(failed):
    failed.line.delivery_json={'changed_before_opening_repair':True};failed.session.flush()
    payload=choice(failed,speed=1.9,dialogue_line_id=failed.line.id)
    with pytest.raises(AppError) as exc:
        repair.preview(failed.session,failed.settings,failed.project.id,failed.node_id,payload)
    assert exc.value.code=='audio_resolution_source_stale'


def test_extend_validates_original_frozen_inputs_before_collecting(failed,monkeypatch):
    failed.run.snapshot_json={**failed.run.snapshot_json,'h3_context_by_shot':{
        failed.shot.id:{'inputs':{'frozen':'old-approved-sku'}}}}
    original_duration=failed.shot.duration_seconds
    def stale(*args):
        raise AppError(409,'context_input_snapshot_stale','Approved product reference changed')
    def must_not_collect(*args,**kwargs):
        raise AssertionError('Cannot silently collect changed media')
    monkeypatch.setattr('app.services.context_production_inputs.restore_context_inputs',stale)
    monkeypatch.setattr('app.services.context_production_inputs.collect_context_inputs',must_not_collect)
    payload=choice(failed,'extend_video',target_duration_seconds=6)
    with pytest.raises(AppError) as exc:
        repair.preview(failed.session,failed.settings,failed.project.id,failed.node_id,payload)
    assert exc.value.code=='context_input_snapshot_stale'
    assert failed.shot.duration_seconds==original_duration


def test_kept_decision_is_invalidated_only_for_repaired_shot(failed):
    from app.api.assemblies import submit
    from app.schemas_product import AssemblySubmit
    from starlette.requests import Request
    for shot in failed.shots:
        failed.session.add(m.StudioDecision(project_id=failed.project.id,shot_id=shot.id,
            action='keep',video_id=failed.video.id if shot.id==failed.shot.id else None,
            position=shot.order,trim_start=.1,trim_end=2,revision=7))
    failed.video.review_status='approved';failed.session.commit()
    execute(failed,choice(failed,'shorten_dialogue',text='Look, a gift!',dialogue_line_id=failed.line.id))
    decisions=list(failed.session.scalars(select(m.StudioDecision).where(m.StudioDecision.project_id==failed.project.id)))
    current=next(d for d in decisions if d.shot_id==failed.shot.id)
    assert current.action=='pending' and current.video_id is None and current.revision==8
    assert current.position==failed.shot.order and current.trim_start==0 and current.trim_end is None
    assert all(d.revision==7 for d in decisions if d.shot_id!=failed.shot.id)
    assert failed.video.review_status=='approved'
    kept=failed.run.result_json['audio_duration_resolutions']['isolated-command-1']['previous_decision']
    assert kept['action']=='keep' and kept['video_id']==failed.video.id
    request=Request({'type':'http','app':failed.app})
    assembly=m.Assembly(project_id=failed.project.id,name='Existing timeline CAS fixture',revision=2,
                        timeline_json={'source_history_preserved':True})
    failed.session.add(assembly);failed.session.commit()
    with pytest.raises(AppError) as exc:
        submit(assembly.id,AssemblySubmit(revision=1),request,failed.session,'render')
    assert exc.value.code=='timeline_changed'
    # A finished replacement leaf, rather than the old kept decision, owns the
    # overview. This fixture intentionally uses the same bytes; no inference.
    new_video=m.VideoRevision(project_id=failed.project.id,shot_id=failed.shot.id,
        generation_job_id=failed.video.generation_job_id,relative_path='new-leaf.mp4',revision=2)
    failed.session.add(new_video);failed.session.flush()
    nodes=copy.deepcopy(failed.run.nodes_json)
    next(n for n in nodes if n['kind']=='quality_check' and n['target_id']==failed.shot.id).update(
        status='succeeded',video_id=new_video.id)
    failed.run.nodes_json=nodes;failed.session.commit()
    view=pipeline.overview(failed.session,failed.project.id)
    assert next(s for s in view['shots'] if s['id']==failed.shot.id)['video']['id']==new_video.id


def test_actual_fallback_adapter_capability_and_route_are_preserved(failed,monkeypatch):
    actual={'selected':{'service_snapshot':{'provider':'local_runtime','settings':{'runtime_family':'indextts25'},
                                          'service_id':'actual-fallback'}},'profile':{'settings':{'parameters':{}}}}
    failed.tts.snapshot_json={**failed.tts.snapshot_json,'active_route':actual,
        'route':{'selected':{'service_snapshot':{'provider':'unsupported_primary','settings':{}}}}}
    context=repair.inspect(failed.session,failed.settings,failed.project.id,failed.node_id)
    assert context['dialogues'][0]['capabilities']['adjust_speed']['max']==2
    assert context['dialogues'][0]['provider']=='local_runtime'
    execute(failed,choice(failed,speed=2,language='EN',dialogue_line_id=failed.line.id))
    assert failed.run.nodes_json[1]['tts_route_override']==actual
    captured=[]
    def create(session,settings,payload,kind,route,**kwargs):
        captured.append(route)
        task=m.MediaProcessTask(project_id=payload.project_id,shot_id=payload.shot_id,
            kind=kind,slot=payload.slot,status='queued')
        session.add(task);session.flush();return task
    monkeypatch.setattr('app.services.media_processing.create_task',create)
    pipeline._media(failed.session,failed.settings,failed.run,failed.run.nodes_json[1],True)
    assert captured==[actual]


def test_rewording_accepts_more_characters_but_rejects_same_effective_text(failed):
    # Written length is not speech duration: expanded abbreviations or numeral
    # spelling can change pronunciation independently of character count.
    expanded='New delivery instruction with rewritten words: '+failed.line.text
    assert len(expanded)>len(failed.line.text)
    payload=choice(failed,'shorten_dialogue',text=expanded,dialogue_line_id=failed.line.id)
    plan=repair.preview(failed.session,failed.settings,failed.project.id,failed.node_id,payload)
    assert plan['parameters']['text']==expanded
    assert any('实测' in warning for warning in plan['warnings'])
    same=payload.model_copy(update={'text':'  '+failed.line.text+'  '})
    with pytest.raises(AppError) as exc:
        repair.preview(failed.session,failed.settings,failed.project.id,failed.node_id,same)
    assert exc.value.code=='audio_resolution_shorter_text'
