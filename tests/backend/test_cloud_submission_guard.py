"""Isolated persistence and concurrency guards; no external requests."""
import asyncio
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from app import models as m
from app.core.errors import AppError
from app.services import jobs


@pytest.fixture
def task(client):
    with client.app.state.session_factory() as s:
        p=m.Project(name='isolated guard');s.add(p);s.flush()
        s.add(m.ScriptRevision(project_id=p.id,revision=1,status='approved',raw_text='test'))
        c=m.Character(slug='isolated-guard',display_name='test');s.add(c);s.flush()
        v=m.CharacterVersion(character_id=c.id,version=1,approval_state='approved');s.add(v);s.flush()
        shot=m.StoryboardShot(project_id=p.id,order=1,status='approved',character_id=c.id,character_asset_version_id=v.id)
        s.add(shot);s.flush()
        j=m.GenerationJob(project_id=p.id,shot_id=shot.id,provider='cloud_wan26_i2v',status='submitting',prompt='test',parameters_json={})
        s.add(j);s.commit()
        return SimpleNamespace(project_id=p.id,shot_id=shot.id,version_id=v.id,job_id=j.id)


@pytest.mark.parametrize('state',['submitting','submitted','unknown'])
def test_existing_cloud_task_blocks_another_submit(client,task,monkeypatch,state):
    from app.services import model_lab
    def frozen(session, settings, shot, payload):
        return session.get(m.CharacterVersion,task.version_id), [], 'test', {}
    monkeypatch.setattr(model_lab,'frozen_generation_inputs',frozen)
    with client.app.state.session_factory() as s:
        s.get(m.GenerationJob,task.job_id).status=state;s.commit()
    result=client.post('/api/shots/'+task.shot_id+'/generate',json={})
    assert result.status_code==409 and result.json()['error']['code']=='generation_already_active'
    with client.app.state.session_factory() as s:
        assert len(list(s.scalars(select(m.GenerationJob).where(m.GenerationJob.shot_id==task.shot_id))))==1


@pytest.mark.parametrize('failure',['service_disabled','video_download_timeout','video_download_incomplete','unsafe_video_url'])
def test_unavailable_cloud_service_keeps_job_id_and_blocks_retry(client,settings,task,monkeypatch,failure):
    with client.app.state.session_factory() as s:
        j=s.get(m.GenerationJob,task.job_id);j.status='queued';j.provider_job_id='provider-task';s.commit()
    async def unavailable(*args):raise AppError(409,failure,'service unavailable')
    async def no_composition(*args):pass
    monkeypatch.setattr(jobs,'poll_job',unavailable)
    monkeypatch.setattr(jobs,'process_composition',no_composition)
    asyncio.run(jobs.process_video_tick(settings,client.app.state.session_factory))
    with client.app.state.session_factory() as s:
        j=s.get(m.GenerationJob,task.job_id)
        assert j.status=='unknown' and j.provider_job_id=='provider-task' and j.error_code=='provider_status_unknown'


def test_inflight_cloud_submission_is_unknown_after_restart(client,settings,task):
    from fastapi.testclient import TestClient
    from app.main import create_app
    with TestClient(create_app(settings)) as restarted:
        with restarted.app.state.session_factory() as s:
            j=s.get(m.GenerationJob,task.job_id)
            assert j.status=='unknown' and j.provider_job_id is None
            assert j.error_code=='submission_outcome_unknown'
    assert client.post('/api/jobs/'+task.job_id+'/reconcile').status_code==409


def test_reconcile_known_provider_job_only_queues_status_lookup(client,task,monkeypatch):
    from app.services import video_services
    calls=[]
    monkeypatch.setattr(video_services,'existing_provider',lambda *args:calls.append('validate'))
    with client.app.state.session_factory() as s:
        j=s.get(m.GenerationJob,task.job_id);j.status='unknown';j.provider_job_id='existing-task'
        j.parameters_json={'service_snapshot':{'provider':'video_api'}};s.commit()
    response=client.post('/api/jobs/'+task.job_id+'/reconcile')
    assert response.status_code==202 and response.json()['status']=='queued'
    assert response.json()['provider_job_id']=='existing-task' and calls==['validate']


def test_wan_multi_reference_rejected_before_job_created(client,settings,task,monkeypatch):
    from dataclasses import replace
    from starlette.requests import Request
    from app.api import routes
    from app.schemas import GenerationRequest
    from app.services import model_lab,video_services
    monkeypatch.setattr(model_lab,'frozen_generation_inputs',lambda s,settings,shot,payload:(s.get(m.CharacterVersion,task.version_id),[{'asset_id':'one'},{'asset_id':'two'}],'test',{}))
    monkeypatch.setattr(video_services,'select_video_service',lambda *a:(None,{'settings':{'video_contract':'wan26-i2v'}},None))
    request=Request({'type':'http','app':SimpleNamespace(state=SimpleNamespace(settings=replace(settings,h3_demo_mode=False)))})
    with client.app.state.session_factory() as s:
        s.get(m.GenerationJob,task.job_id).status='generated';s.commit()
        with pytest.raises(AppError) as error:
            asyncio.run(routes.generate_for_shot(request,s,s.get(m.StoryboardShot,task.shot_id),GenerationRequest()))
        assert error.value.code=='wan_single_frame_required'
        assert len(list(s.scalars(select(m.GenerationJob).where(m.GenerationJob.shot_id==task.shot_id))))==1


def test_invalid_paid_output_remains_visible_and_unapprovable(client,settings,task,monkeypatch,tmp_path):
    from app.services import video_services,cinematic_quality,audio_mix
    raw=b'isolated provider bytes'*20
    class Provider:
        async def get_job(self,identifier):return {'status':'generated','output_url':'https://invalid.example/x','raw':{'status':'SUCCEEDED'}}
        async def download(self,url):return raw
    monkeypatch.setattr(video_services,'existing_provider',lambda *a:Provider())
    monkeypatch.setattr(jobs,'PROJECT_ROOT',tmp_path)
    def mix(path,events,settings):path.write_bytes(b'mixed');return {'performed':True}
    monkeypatch.setattr(audio_mix,'mix_audio_events',mix)
    monkeypatch.setattr(jobs,'inspect_media',lambda *a:[{'check_type':'file_readable','status':'passed'}, {'check_type':'media_decode','status':'passed','details':{}}])
    monkeypatch.setattr(cinematic_quality,'inspect_generated',lambda *a:[{'check_type':'cinematic_output_spec','status':'failed','details':{'reason':'size'}}])
    with client.app.state.session_factory() as s:
        j=s.get(m.GenerationJob,task.job_id);j.status='queued';j.provider_job_id='provider-task'
        j.parameters_json={'output_relative_dir':'projects/test/output','service_snapshot':{'provider':'video_api','is_cloud':True},'screenplay_snapshot':{'resolved_audio_events':[{'event':'test'}]}}
        s.commit()
    asyncio.run(jobs.poll_job(settings,client.app.state.session_factory,task.job_id))
    with client.app.state.session_factory() as s:
        j=s.get(m.GenerationJob,task.job_id)
        video=s.scalar(select(m.VideoRevision).where(m.VideoRevision.generation_job_id==j.id))
        assert j.status=='failed' and video.review_status=='needs_regeneration'
        assert (settings.media_root/video.relative_path).read_bytes()==raw
        checks=list(s.scalars(select(m.QualityCheck).where(m.QualityCheck.video_revision_id==video.id)))
        assert any(c.check_type=='cinematic_output_spec' and c.status=='failed' for c in checks)
        video_id=video.id
    response=client.post('/api/videos/'+video_id+'/approve',json={'reason':'test'})
    assert response.status_code==409 and response.json()['error']['code']=='video_needs_regeneration'
    response=client.post('/api/assemblies',json={'project_id':task.project_id,'name':'test','timeline':{'clips':[{'video_id':video_id}]}})
    assert response.status_code==422 and response.json()['error']['code']=='unapproved_input'
