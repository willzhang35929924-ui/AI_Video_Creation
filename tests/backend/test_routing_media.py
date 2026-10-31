"""Isolated protocol tests. Synthetic byte fixtures are not model output evidence."""
import asyncio
import copy
import hashlib
import io
import math
from pathlib import Path
import struct
import subprocess
import wave
import httpx
import pytest
from sqlalchemy import select,func
from app import models as m
from app.core.config import Settings
from app.core.errors import AppError
from app.schemas_routing import TaskSubmit
from app.services import media_processing as media


def wav_bytes():
    output=io.BytesIO()
    with wave.open(output,'wb') as writer:
        writer.setnchannels(1);writer.setsampwidth(2);writer.setframerate(24000)
        writer.writeframes(b''.join(struct.pack('<h',int(5000*math.sin(2*math.pi*440*n/24000))) for n in range(12000)))
    return output.getvalue()


def project(client):return client.post('/api/projects',json={'name':'Isolated media protocol'}).json()['id']


def test_inherited_empty_route_is_an_explicit_block_not_legacy_fallback(client):
    from app.services.task_routing import freeze_route,route_is_explicit,assert_frozen
    p=project(client)
    response=client.put('/api/model-services/routes/system/default/final_assembly',json={
        'expected_revision':0,'settings':{'mode':'local_only','choices':[]}})
    assert response.status_code==200,response.text
    with client.app.state.session_factory() as session:
        route=freeze_route(session,client.app.state.settings,p,None,'final_assembly')
        assert route['profile']['revision']==0 and route['profile']['inherited_from']=='system:default'
        assert route['configured'] and route_is_explicit(route) and route['selected'] is None
        with pytest.raises(AppError) as error:assert_frozen(session,route)
        assert error.value.code=='task_route_blocked'


def cloud(client):
    response=client.post('/api/settings/ai-services',json={'module':'dialogue_generation','provider':'openai_tts','name':'Protocol fixture only',
        'base_url':'https://api.openai.com/v1','default_model':'gpt-4o-mini-tts','api_key':'synthetic-not-a-real-credential','settings':{'voice':'alloy'}})
    assert response.status_code==201,response.text
    return response.json()['id']


def verify(client,service,project_id,**overrides):
    return client.post(f'/api/model-services/services/{service}/generation-test',json={
        'slot':'dialogue_generation','project_id':project_id,'inputs':{'text':'记录已核对。'},'allow_cloud_costs':True,**overrides})


def test_bootstrap_requires_explicit_costs_and_does_not_change_production_routes(client):
    p,s=project(client),cloud(client)
    assert verify(client,s,p,allow_cloud_costs=False).status_code==409
    result=verify(client,s,p);assert result.status_code==202,result.text
    with client.app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(m.RoutePolicy))==0
        task=session.get(m.MediaProcessTask,result.json()['source_id'])
        assert task.status=='queued' and task.snapshot_json['route']['verification_run']
        assert task.snapshot_json['route']['profile']['settings']['max_retries']==0
        assert not task.snapshot_json['route']['profile']['settings']['allow_fallback']


def test_repeated_unknown_request_reuses_task_despite_new_capture_clock(client):
    p,s=project(client),cloud(client);first=verify(client,s,p).json()
    with client.app.state.session_factory() as session:
        task=session.get(m.MediaProcessTask,first['source_id']);task.status='unknown';session.commit()
    second=verify(client,s,p)
    assert second.status_code==202 and second.json()['source_id']==first['source_id'] and second.json()['status']=='unknown'


def test_provider_limits_and_credential_parameter_names_are_rejected_before_dispatch(client):
    p,s=project(client),cloud(client)
    assert verify(client,s,p,inputs={'text':'x'*4097}).status_code==422
    assert verify(client,s,p,parameters={'Authorization ':'private-sentinel'}).status_code==422
    assert verify(client,s,p,parameters={'command':'do-not-execute'}).status_code==422
    assert verify(client,s,p,model_id='unconfigured-model').status_code==422


def test_cloud_draft_cannot_use_bootstrap_entry(client):
    p=project(client)
    item=client.post('/api/settings/ai-services',json={'module':'dialogue_generation','provider':'cloud_draft','name':'Unimplemented supplier'}).json()
    assert verify(client,item['id'],p).status_code==422


def test_actual_fixture_decode_promotes_only_a_successful_task(client,settings,monkeypatch):
    # A deterministic HTTP byte fixture verifies plumbing, not a paid API/model.
    settings.ffmpeg_path=Settings.from_env().ffmpeg_path;settings.ffprobe_path=Settings.from_env().ffprobe_path
    p,s=project(client),cloud(client);item=verify(client,s,p).json()
    async def response(*args,**kwargs):return httpx.Response(200,content=wav_bytes())
    monkeypatch.setattr('app.services.ai_services.service_request',response)
    asyncio.run(media.process_task(settings,client.app.state.session_factory,item['source_id']))
    result=client.get('/api/model-services/tasks/'+item['id']).json()
    assert result['status']=='succeeded' and result['output']['actual']['full_decode']
    assert result['output']['actual']['sample_rate']==24000
    with client.app.state.session_factory() as session:
        from app.services.task_routing import candidate
        service=session.get(m.AIService,s)
        assert candidate(session,settings,service,'dialogue_generation').eligible


def test_unknown_cloud_transport_never_retries_or_claims_not_submitted(client,settings,monkeypatch):
    p,s=project(client),cloud(client);item=verify(client,s,p).json();calls=[]
    async def uncertain(*args,**kwargs):calls.append(True);raise httpx.ReadTimeout('fixture transport outcome unknown')
    monkeypatch.setattr('app.services.ai_services.service_request',uncertain)
    asyncio.run(media.process_task(settings,client.app.state.session_factory,item['source_id']))
    asyncio.run(media.process_task(settings,client.app.state.session_factory,item['source_id']))
    result=client.get('/api/model-services/tasks/'+item['id']).json()
    assert len(calls)==1 and result['status']=='unknown' and result['output']['inference_submitted'] is None


def test_determined_retry_uses_one_row_and_unknown_never_advances(client):
    p,s=project(client),cloud(client);item=verify(client,s,p).json()
    with client.app.state.session_factory() as session:
        task=session.get(m.MediaProcessTask,item['source_id']);snapshot=copy.deepcopy(task.snapshot_json)
        snapshot['route']['profile']['settings']['max_retries']=1;task.snapshot_json=snapshot
        task.status='failed';task.result_json={'error_code':'runtime_failed','attempt_history':[{'service_id':s,'model_id':'gpt-4o-mini-tts','status':'failed'}]}
        assert media._schedule_determined_retry(session,task) and task.status=='queued' and task.snapshot_json['attempt_index']==1
        task.status='unknown';assert not media._schedule_determined_retry(session,task)


def test_paid_fallback_requires_route_and_invocation_approval(client):
    p,s=project(client),cloud(client);item=verify(client,s,p).json()
    with client.app.state.session_factory() as session:
        task=session.get(m.MediaProcessTask,item['source_id']);snapshot=copy.deepcopy(task.snapshot_json)
        first=snapshot['route']['selected'];other=copy.deepcopy(first);other['service_snapshot']['service_id']='second';other['candidate'].update(eligible=True,is_cloud=True)
        snapshot['route']['choices']=[first,other];snapshot['route']['profile']['settings'].update(allow_fallback=True,allow_paid_fallback=True)
        task.snapshot_json=snapshot;task.status='failed';task.result_json={'error_code':'tts_http_rejected','attempt_history':[{'service_id':s,'model_id':'gpt-4o-mini-tts'}]}
        assert not media._schedule_determined_retry(session,task)
        snapshot['route']['allow_paid_fallback']=True;task.snapshot_json=snapshot
        assert media._schedule_determined_retry(session,task)
        assert task.snapshot_json['active_route']['selected']['service_snapshot']['service_id']=='second'


def role_fixture(client,settings):
    p=project(client);factory=client.app.state.session_factory
    with factory() as session:
        shot=m.StoryboardShot(project_id=p,order=1,scene_title='Single adult test');session.add(shot)
        a=m.Character(slug='role-a',display_name='Test A');b=m.Character(slug='role-b',display_name='Test B');session.add_all([a,b]);session.flush()
        va=m.CharacterVersion(character_id=a.id,version=1);vb=m.CharacterVersion(character_id=b.id,version=1);session.add_all([va,vb]);session.flush()
        participant=m.ShotCharacter(shot_id=shot.id,character_id=a.id,character_version_id=va.id);session.add(participant);session.flush()
        job=m.GenerationJob(project_id=p,shot_id=shot.id,provider='synthetic_fixture',prompt='',status='generated');session.add(job);session.flush()
        settings.media_root.mkdir(parents=True,exist_ok=True);source=settings.media_root/'fixture.mp4';source.write_bytes(b'fixture-not-inference')
        video=m.VideoRevision(project_id=p,shot_id=shot.id,generation_job_id=job.id,revision=1,relative_path='fixture.mp4',metadata_json={'sha256':hashlib.sha256(source.read_bytes()).hexdigest()});session.add(video)
        audio=settings.media_root/'voice.wav';audio.write_bytes(wav_bytes());asset=m.AudioAsset(project_id=p,character_version_id=vb.id,relative_path='voice.wav',sha256=hashlib.sha256(audio.read_bytes()).hexdigest());session.add(asset);session.commit()
        return p,shot.id,participant.id,va.id,vb.id,video.id,asset.id


def test_lip_audio_cannot_be_taken_from_another_role_in_same_project(client,settings):
    p,shot,participant,_,_,video,audio=role_fixture(client,settings)
    with client.app.state.session_factory() as session:
        with pytest.raises(AppError,match='明确属于此人物版本'):
            media.create_task(session,settings,TaskSubmit(project_id=p,shot_id=shot,slot='lip_sync',inputs={'video_id':video,'audio_id':audio,'participant_id':participant}))


def test_voice_upload_keeps_original_and_bad_audio_leaves_no_orphan(client,settings):
    p,_,_,va,_,_,_=role_fixture(client,settings)
    settings.ffmpeg_path=Settings.from_env().ffmpeg_path;settings.ffprobe_path=Settings.from_env().ffprobe_path
    body={'project_id':p,'character_version_id':va,'source_type':'ai_generated','declaration':'Synthetic protocol fixture; not a person recording'}
    bad=client.post('/api/model-services/audio-assets',data=body,files={'file':('bad.wav',b'not audio','audio/wav')})
    assert bad.status_code==422 and not list((settings.media_root/'audio_assets').rglob('*.wav'))
    good=client.post('/api/model-services/audio-assets',data=body,files={'file':('good.wav',wav_bytes(),'audio/wav')})
    assert good.status_code==201,good.text
    assert good.json()['metadata']['human_approved'] is False
    assert (settings.media_root/good.json()['relative_path']).read_bytes()==wav_bytes()


def test_reference_normalization_preserves_bytes_and_records_new_sha(settings):
    settings.ffmpeg_path=Settings.from_env().ffmpeg_path;settings.ffprobe_path=Settings.from_env().ffprobe_path
    settings.media_root.mkdir(parents=True,exist_ok=True);source=settings.media_root/'original.wav';source.write_bytes(wav_bytes())
    sha=hashlib.sha256(source.read_bytes()).hexdigest();result=media.normalize_reference(settings,{'id':'reference','path':str(source),'sha256':sha},settings.media_root/'normalized.wav')
    assert result['source_preserved'] and result['source_sha256']==sha and result['actual']['sample_rate']==24000
    assert result['actual']['channels']==1 and result['actual']['codec']=='pcm_s16le'


def test_real_ffmpeg_mix_honors_mute_and_publishes_matching_full_audio_asset(client,settings):
    p,shot,participant,va,_,video,audio=role_fixture(client,settings)
    settings.ffmpeg_path=Settings.from_env().ffmpeg_path;settings.ffprobe_path=Settings.from_env().ffprobe_path
    source=settings.media_root/'fixture.mp4'
    result=subprocess.run([settings.ffmpeg_path,'-nostdin','-v','error','-y','-f','lavfi','-i','color=black:s=64x64:r=24:d=1',
        '-f','lavfi','-i','anullsrc=r=24000:cl=mono','-t','1','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac',str(source)],capture_output=True)
    assert result.returncode==0
    with client.app.state.session_factory() as session:
        v=session.get(m.VideoRevision,video);v.metadata_json={'sha256':hashlib.sha256(source.read_bytes()).hexdigest()}
        a=session.get(m.AudioAsset,audio);a.character_version_id=va;a.participant_id=participant;a.metadata_json={'start_seconds':.2,'end_seconds':.3,'volume':0}
        task=media.create_task(session,settings,TaskSubmit(slot='dialogue_generation',project_id=p,shot_id=shot,
            inputs={'operation':'audio_mix','video_id':video,'audio_ids':[audio],'mode':'replace_dialogue'}))
        session.commit();identifier=task.id
    asyncio.run(media.process_task(settings,client.app.state.session_factory,identifier))
    with client.app.state.session_factory() as session:
        task=session.get(m.MediaProcessTask,identifier);assert task.status=='succeeded',task.error_message
        full=session.get(m.AudioAsset,task.result_json['audio_id'])
        assert full.character_version_id==va and full.participant_id==participant
        assert abs(full.metadata_json['actual']['duration']-1)<.05
        with wave.open(str(settings.media_root/full.relative_path),'rb') as reader:
            values=struct.unpack('<'+'h'*reader.getnframes()*reader.getnchannels(),reader.readframes(reader.getnframes()))
        assert max(abs(value) for value in values)<=2  # The source fixture was audible; volume zero must mute it.
        derived=session.get(m.VideoRevision,task.result_json['video_id'])
        assert derived.metadata_json['source_video_id']==video
        assert hashlib.sha256(source.read_bytes()).hexdigest()==derived.metadata_json['processing_chain'][0]['input_sha256']


def test_lip_sync_accepts_explicit_matching_role_audio_without_dispatch(client,settings):
    p,shot,participant,va,_,video,audio=role_fixture(client,settings)
    with client.app.state.session_factory() as session:
        asset=session.get(m.AudioAsset,audio);asset.character_version_id=va;asset.participant_id=participant
        task=media.create_task(session,settings,TaskSubmit(project_id=p,shot_id=shot,slot='lip_sync',
            inputs={'video_id':video,'audio_id':audio,'participant_id':participant}))
        assert task.status=='blocked'  # No verified route in this isolated fixture; never submits a model.
        assert task.snapshot_json['inputs']['participant_ids']==[participant]


def test_default_alias_cannot_duplicate_an_explicit_model_in_route(client):
    p,s=project(client),cloud(client)
    response=client.put(f'/api/model-services/routes/project/{p}/dialogue_generation',json={'expected_revision':0,'settings':{
        'mode':'manual','choices':[{'service_id':s,'model_id':''},{'service_id':s,'model_id':'gpt-4o-mini-tts'}]}})
    assert response.status_code==422 and response.json()['error']['code']=='route_duplicate_resolved_model'


def test_concurrent_verification_cannot_create_two_billable_children(client,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    p,s=project(client),cloud(client);barrier=threading.Barrier(2);original=media._fingerprint
    def simultaneous(value):
        result=original(value);barrier.wait(timeout=10);return result
    monkeypatch.setattr(media,'_fingerprint',simultaneous)
    with ThreadPoolExecutor(max_workers=2) as executor:
        replies=list(executor.map(lambda _:verify(client,s,p),range(2)))
    assert all(reply.status_code==202 for reply in replies),[reply.text for reply in replies]
    assert replies[0].json()['source_id']==replies[1].json()['source_id']
    with client.app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(m.MediaProcessTask).where(m.MediaProcessTask.project_id==p))==1


def test_fallback_evidence_belongs_to_actual_successful_service(client,settings):
    from app.services.task_routing import candidate
    p,a=project(client),cloud(client);b=cloud(client);result=verify(client,a,p).json()
    settings.media_root.mkdir(parents=True,exist_ok=True);path=settings.media_root/'fallback.wav';path.write_bytes(wav_bytes())
    from app.services.ai_services import service_snapshot
    with client.app.state.session_factory() as session:
        task=session.get(m.MediaProcessTask,result['source_id']);task.status='succeeded'
        task.result_json={'relative_path':'fallback.wav','sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
            'actual_service_snapshot':service_snapshot(session.get(m.AIService,b),'gpt-4o-mini-tts')}
        session.flush()
        assert not candidate(session,settings,session.get(m.AIService,a),'dialogue_generation').eligible
        assert candidate(session,settings,session.get(m.AIService,b),'dialogue_generation').eligible
