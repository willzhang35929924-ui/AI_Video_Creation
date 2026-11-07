import asyncio
import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from app import models as m
from app.core.errors import AppError
from app.schemas_cinematic import MotionReferenceInput
from app.services.motion_references import resolve_motion_references, motion_live_state,verify_motion_sources
from app.services.providers import ComfyUIH3Provider, VideoGenerationRequest


def test_motion_requires_explicit_permission():
    with pytest.raises(ValidationError):
        MotionReferenceInput(video_id='video', expected_revision=1, source_sha256='a'*64,
                             trim_start_seconds=0, trim_end_seconds=3, authorized_for_motion=False)


def test_input_video_preview_is_not_a_generated_video():
    from app.services.jobs import video_outputs
    history={'outputs':{'loader':{'images':[{'filename':'reference.mp4','type':'input'}]},
                        'save':{'images':[{'filename':'generated.mp4','type':'output'}]}}}
    assert video_outputs(history)==[{'filename':'generated.mp4','type':'output'}]


def test_motion_source_revision_hash_and_bounds_are_checked(client, settings):
    media=Path(settings.media_root); media.mkdir(parents=True,exist_ok=True)
    source=media/'original.mp4'; source.write_bytes(b'source-video')
    sha=hashlib.sha256(source.read_bytes()).hexdigest()
    ref={'video_id':'refvideo_test','expected_revision':1,'source_sha256':sha,
         'trim_start_seconds':0,'trim_end_seconds':3,'authorized_for_motion':True}
    shot=SimpleNamespace(duration_seconds=5)
    with client.app.state.session_factory() as session:
        video=m.ReferenceVideo(id='refvideo_test',name='reference',filename='original.mp4',relative_path='original.mp4',
            sha256=sha,metadata_json={'duration_seconds':6},state='active',revision=1)
        session.add(video);session.flush()
        resolved=resolve_motion_references(session,settings,shot,[ref])
        assert resolved[0]['effective_frames']==56 and resolved[0]['audio_included'] is False
        assert motion_live_state(session,[ref])[0]['revision']==1
        video.revision=2;session.flush()
        with pytest.raises(AppError,match='变化'):resolve_motion_references(session,settings,shot,[ref])
        video.revision=1;session.flush();source.write_bytes(b'changed')
        with pytest.raises(AppError,match='哈希'):verify_motion_sources(resolve_motion_references(session,settings,shot,[ref]))


def test_in_use_motion_video_cannot_be_edited_or_archived(client,settings):
    media=Path(settings.media_root);media.mkdir(parents=True,exist_ok=True)
    source=media/'original.mp4';source.write_bytes(b'unchanged')
    sha=hashlib.sha256(source.read_bytes()).hexdigest()
    with client.app.state.session_factory() as session:
        session.add(m.Project(id='project',name='isolated test'));session.flush()
        session.add(m.StoryboardShot(id='shot',project_id='project',order=1));session.flush()
        session.add(m.ReferenceVideo(id='reference',name='reference',filename='original.mp4',relative_path='original.mp4',
            sha256=sha,metadata_json={'duration_seconds':6},state='active',revision=1));session.flush()
        session.add(m.GenerationJob(id='job',project_id='project',shot_id='shot',provider='comfyui_h3',
            status='running',prompt='test',parameters_json={'motion_references':[{'video_id':'reference'}]}));session.commit()
    body={'revision':1,'name':'renamed','category':'','tags':[],'notes':'','source_note':'','usage_scope':'abstract_style_only'}
    assert client.patch('/api/reference-videos/reference',json=body).status_code==409
    assert client.delete('/api/reference-videos/reference?revision=1').status_code==409
    assert source.read_bytes()==b'unchanged'


def test_shot_binding_saves_explicit_scope_and_changed_source_blocks_preflight(client,settings):
    from test_cinematic import approved_cast, import_case
    approved_cast(client,settings);result=import_case(client)
    source=Path(settings.media_root)/'motion-source.mp4';source.write_bytes(b'isolated source')
    sha=hashlib.sha256(source.read_bytes()).hexdigest()
    with client.app.state.session_factory() as session:
        session.add(m.ReferenceVideo(id='motion-video',name='video',filename='motion-source.mp4',relative_path=source.name,
            sha256=sha,metadata_json={'duration_seconds':6},state='active',revision=1));session.commit()
    overview=client.get('/api/projects/'+result['project_id']+'/production').json()
    item=overview['shots'][0]
    ref={'video_id':'motion-video','expected_revision':1,'source_sha256':sha,'trim_start_seconds':0,
         'trim_end_seconds':3,'authorized_for_motion':True}
    production={**item['production'],'motion_references':[ref], 'expected_input_hash':item['preflight']['generation_inputs_hash']}
    response=client.put('/api/shots/'+item['shot']['id']+'/production',json=production)
    assert response.status_code==200,response.text
    current=client.get('/api/projects/'+result['project_id']+'/production').json()
    updated=next(s for s in current['shots'] if s['shot']['id']==item['shot']['id'])
    legacy={k:v for k,v in updated['production'].items() if k!='motion_references'}
    legacy['expected_input_hash']=updated['preflight']['generation_inputs_hash']
    response=client.put('/api/shots/'+item['shot']['id']+'/production',json=legacy)
    assert response.status_code==200,response.text
    # New role/target bindings are optional. A legacy edit must retain every
    # supplied motion field and leave the two new bindings explicitly unset.
    assert response.json()['source_json']['production']['motion_references']==[
        {**ref, 'reference_role': None, 'target_character_id': None}]
    assert client.delete('/api/reference-videos/motion-video?revision=1').status_code==409
    with client.app.state.session_factory() as session:
        video=session.get(m.ReferenceVideo,'motion-video');video.revision=2;session.commit()
    overview=client.get('/api/projects/'+result['project_id']+'/production').json()
    item=next(s for s in overview['shots'] if s['shot']['id']==item['shot']['id'])
    assert any(f['code']=='motion_reference_stale' and f['severity']=='blocker' for f in item['preflight']['findings'])


def test_submitting_job_blocks_direct_edit_delete_and_binding_edit(client,settings):
    from test_cinematic import approved_cast, import_case
    approved_cast(client,settings);result=import_case(client)
    shot_id=result['shot_ids'][0]
    overview=client.get('/api/projects/'+result['project_id']+'/production').json()
    item=next(s for s in overview['shots'] if s['shot']['id']==shot_id)
    with client.app.state.session_factory() as session:
        session.add(m.GenerationJob(id='pending',project_id=result['project_id'],shot_id=shot_id,provider='comfyui_h3',
            status='submitting',prompt='test',parameters_json={}));session.commit()
    assert client.patch('/api/shots/'+shot_id,json={'duration_seconds':3}).status_code==409
    assert client.delete('/api/shots/'+shot_id).status_code==409
    assert client.put('/api/shots/'+shot_id+'/production',json=item['production']).status_code==409


def test_unknown_local_job_requires_two_confirmations_and_records_human_review(client):
    with client.app.state.session_factory() as session:
        session.add(m.Project(id='unknown-project',name='isolated'));session.flush()
        session.add(m.StoryboardShot(id='unknown-shot',project_id='unknown-project',order=1));session.flush()
        session.add(m.GenerationJob(id='unknown-job',project_id='unknown-project',shot_id='unknown-shot',
            provider='comfyui_h3',status='unknown',prompt='test',parameters_json={}));session.commit()
    path='/api/jobs/unknown-job/resolve-unknown'
    body={'confirmed_inspection':True,'accept_duplicate_risk':True,'evidence_note':'已人工检查本地队列与历史记录','operator':'local-user'}
    assert client.post(path,json={**body,'accept_duplicate_risk':False}).status_code==422
    assert client.post(path,json={**body,'evidence_note':'short'}).status_code==422
    response=client.post(path,json=body)
    assert response.status_code==200,response.text
    assert response.json()['status']=='closed_unknown' and response.json()['provider_job_id'] is None
    assert client.post(path,json=body).status_code==409
    with client.app.state.session_factory() as session:
        job=session.get(m.GenerationJob,'unknown-job')
        events=list(session.query(m.AuditEvent).filter_by(object_id='unknown-job',action='unknown_closed_after_manual_review'))
        assert job.status=='closed_unknown' and len(events)==1
        assert events[0].details_json['resubmitted'] is False and events[0].details_json['original_outcome']=='unknown'


def test_stale_motion_video_cannot_be_approved(client):
    with client.app.state.session_factory() as session:
        session.add(m.Project(id='stale-project',name='isolated'));session.flush()
        session.add(m.StoryboardShot(id='stale-shot',project_id='stale-project',order=1));session.flush()
        session.add(m.GenerationJob(id='stale-job',project_id='stale-project',shot_id='stale-shot',provider='comfyui_h3',
            status='generated',prompt='test',parameters_json={'motion_references':[{'video_id':'stale'}], 'motion_inputs_hash':'old-hash'}));session.flush()
        session.add(m.VideoRevision(id='stale-video',project_id='stale-project',shot_id='stale-shot',generation_job_id='stale-job',
            relative_path='isolated.mp4',revision=1,review_status='pending'));session.flush()
        session.add(m.QualityCheck(video_revision_id='stale-video',check_type='file_readable',status='passed',details_json={}));session.commit()
    response=client.post('/api/videos/stale-video/approve',json={'reason':'isolated human check'})
    assert response.status_code==409,response.text
    assert response.json()['error']['code']=='motion_video_stale'


def test_startup_marks_interrupted_submitting_unknown_without_retry(client,settings):
    from fastapi.testclient import TestClient
    from app.main import create_app
    with client.app.state.session_factory() as session:
        session.add(m.Project(id='restart-project',name='isolated'));session.flush()
        session.add(m.StoryboardShot(id='restart-shot',project_id='restart-project',order=1));session.flush()
        session.add(m.GenerationJob(id='restart-job',project_id='restart-project',shot_id='restart-shot',provider='comfyui_h3',
            status='submitting',prompt='test',parameters_json={'motion_references':[{'video_id':'reference'}]}));session.commit()
    with TestClient(create_app(settings)) as restarted:
        job=restarted.get('/api/jobs/restart-job').json()
        assert job['status']=='unknown' and job['provider_job_id'] is None
        assert job['error_code']=='submission_outcome_unknown'


def test_studio_closed_unknown_child_stays_blocked_without_resubmission(client,settings):
    from app.services.studio_pipeline import _video
    with client.app.state.session_factory() as session:
        session.add(m.Project(id='studio-project',name='isolated'));session.flush()
        session.add(m.StoryboardShot(id='studio-shot',project_id='studio-project',order=1));session.flush()
        session.add(m.GenerationJob(id='studio-job',project_id='studio-project',shot_id='studio-shot',provider='comfyui_h3',
            status='closed_unknown',error_code='operator_closed_unknown',error_message='原结果未知',prompt='test',parameters_json={}));session.flush()
        entry={'id':'video:studio-shot','target_id':'studio-shot','status':'running','child_id':'studio-job','attempts':1,'history':[]}
        run=SimpleNamespace(id='studio-run',snapshot_json={'options':{'max_retries':2}})
        asyncio.run(_video(None,session,settings,run,entry,True))
        assert entry['status']=='unknown' and entry['child_id']=='studio-job'
        assert entry['code']=='operator_closed_unknown' and '原结果未知' in entry['error']
        assert session.query(m.GenerationJob).filter_by(shot_id='studio-shot').count()==1


@pytest.mark.parametrize('prompt_status',[200,408])
def test_motion_native_graph_uploads_reference_and_never_uploads_audio(client, settings, tmp_path, monkeypatch,prompt_status):
    import app.services.providers as providers
    import app.services.motion_references as motion
    workflow=Path(__file__).resolve().parents[2]/'workflows/h3_r2v_character_v1.json'
    config=replace(settings,h3_demo_mode=False,h3_workflow_api_json=workflow,comfyui_base_url='http://localhost:8189')
    image=tmp_path/'reference.png';image.write_bytes(b'image-bytes')
    video=b'processed-mute-video'; video_sha=hashlib.sha256(video).hexdigest()
    ref={'video_id':'refvideo_test','source_sha256':'a'*64,'expected_revision':1,'trim_start_seconds':0,
         'trim_end_seconds':3,'effective_frames':56,'effective_duration_seconds':56/24}
    monkeypatch.setattr(motion,'prepare_motion_reference',lambda _settings,_ref:(video,{'sha256':video_sha,'bytes':len(video),'frames':56,'fps':24,'audio_included':False}))
    monkeypatch.setattr(providers,'PROJECT_ROOT',tmp_path)
    sent={}
    def handler(req):
        if req.url.path=='/object_info':
            return httpx.Response(200,json={'LoadVideo':{'input':{'required':{'file':['COMBO']}},'output':['VIDEO']},
                'GetVideoComponents':{'input':{'required':{'video':['VIDEO']}},'output':['IMAGE','AUDIO']},
                'MiniMaxH3ReferenceToVideo':{'input':{'optional':{'ref_videos':['COMFY_AUTOGROW_V3',{'template':{'input':{'required':{'ref_video':['IMAGE']}},'prefix':'ref_video_','max':3}}]}}}})
        if req.url.path=='/upload/image':
            filename=re.search(b'filename="([^"]+)"',req.content).group(1).decode()
            return httpx.Response(200,json={'name':filename,'subfolder':'','type':'input'})
        if req.url.path=='/view':return httpx.Response(200,content=video)
        if req.url.path=='/prompt':
            sent['graph']=json.loads(req.content)['prompt'];sent['count']=sent.get('count',0)+1
            return httpx.Response(prompt_status,json={'prompt_id':'actual-provider-id'} if prompt_status==200 else {'error':'timeout'})
        return httpx.Response(404)
    original=httpx.AsyncClient;transport=httpx.MockTransport(handler)
    client_origins=[]
    def transport_client(origins, **kwargs):
        client_origins.append(origins)
        return original(transport=transport, trust_env=False, follow_redirects=False, **kwargs)
    # Adapter-only fixture. The real pinned transport has its own safety tests.
    monkeypatch.setattr(providers,'local_service_client',transport_client)
    request=VideoGenerationRequest(job_id='test-job',project_id='test-project',shot_id='test-shot',prompt='use <Video 1>',
        negative_prompt='',duration_seconds=5,aspect_ratio='16:9',seed=42,parameters={},
        reference_assets=[{'absolute_path':str(image),'sha256':hashlib.sha256(image.read_bytes()).hexdigest(),'asset_id':'image'}],
        output_dir=tmp_path/'output',motion_references=[ref])
    if prompt_status==408:
        with pytest.raises(AppError) as error:asyncio.run(ComfyUIH3Provider(config).submit(request))
        assert error.value.code=='motion_submission_unknown' and sent['count']==1
    else:
        accepted=asyncio.run(ComfyUIH3Provider(config).submit(request))
        assert accepted.provider_job_id=='actual-provider-id'
    graph=sent['graph'];target=graph['136']['inputs']
    assert target['ref_videos.ref_video_0']==['factory_motion_components_0',0]
    assert graph['factory_motion_components_0']['inputs']['video']==['factory_motion_load_0',0]
    assert graph['factory_motion_load_0']['class_type']=='LoadVideo'
    assert not any(key.startswith('ref_video_audios.') for key in target)
    manifest=json.loads((tmp_path/'logs/jobs/test-job/reference_manifest.json').read_text('utf-8'))
    assert client_origins and all(origins == [config.comfyui_base_url] for origins in client_origins)
    assert manifest['motion_reference_uploads'][0]['processed']['sha256']==video_sha
    assert manifest['motion_reference_uploads'][0]['audio_included'] is False
