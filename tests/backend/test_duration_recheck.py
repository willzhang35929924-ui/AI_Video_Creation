"""Recheck preserves cloud media and audit history, without a second request."""
import hashlib
import pytest
from sqlalchemy import select
from app import models as m
from app.services import jobs, cinematic_quality, studio_pipeline
from test_cloud_submission_guard import task


@pytest.fixture
def failed_video(client,settings,task,monkeypatch):
    data=b'original video content'; rel='projects/test/original.mp4'
    path=settings.media_root/rel;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
    monkeypatch.setattr(cinematic_quality,'generation_inputs_hash',lambda session,shot:'expected-hash')
    decode={'check_type':'media_decode','status':'passed','details':{'streams':[{'codec_type':'video','width':1280,'height':720,'avg_frame_rate':'24/1','nb_read_frames':'124'}],'format':{'duration':'5.258344'},'full_decode':{'exit_code':0}}}
    monkeypatch.setattr(jobs,'inspect_media',lambda *args:[{'check_type':'file_readable','status':'passed','details':{}},decode])
    with client.app.state.session_factory() as s:
        j=s.get(m.GenerationJob,task.job_id);j.status='failed';j.error_code='output_quality_failed';j.provider_job_id='paid-task'
        j.parameters_json={'width':1280,'height':720,'duration_seconds':5,'service_snapshot':{'provider':'video_api'},'screenplay_snapshot':{'cinematic':{'preflight':{'generation_inputs_hash':'expected-hash'}},'resolved_audio_events':[]}}
        v=m.VideoRevision(project_id=task.project_id,shot_id=task.shot_id,generation_job_id=j.id,relative_path=rel,revision=1,review_status='needs_regeneration',metadata_json={'sha256':hashlib.sha256(data).hexdigest(),'quality_status':'failed'})
        s.add(v);s.flush();s.add(m.QualityCheck(video_revision_id=v.id,check_type='cinematic_output_spec',status='failed',details_json={'actual':{'frames':0}}));s.commit()
        return v.id,path


def test_recheck_preserves_history_and_requires_human_review(client,settings,failed_video):
    video_id,path=failed_video;before=path.read_bytes()
    r=client.post('/api/videos/'+video_id+'/recheck-duration')
    assert r.status_code==200
    with client.app.state.session_factory() as s:
        v=s.get(m.VideoRevision,video_id);j=s.get(m.GenerationJob,v.generation_job_id)
        assert v.review_status=='pending' and v.metadata_json['quality_status']=='warning'
        assert j.status=='generated' and path.read_bytes()==before
        rows=list(s.scalars(select(m.QualityCheck).where(m.QualityCheck.video_revision_id==video_id,m.QualityCheck.check_type=='cinematic_output_spec').order_by(m.QualityCheck.created_at)))
        assert [x.status for x in rows]==['failed','warning']
        assert rows[-1].details_json['actual']['frames']==124
        assert s.scalar(select(m.AuditEvent).where(m.AuditEvent.object_id==j.id,m.AuditEvent.action=='duration_rechecked'))
        studio_pipeline._valid_video(s,settings,v)
    assert client.post('/api/videos/'+video_id+'/recheck-duration').status_code==409


def test_recheck_rejects_changed_file_or_stale_inputs(client,failed_video,monkeypatch):
    video_id,path=failed_video
    path.write_bytes(b'changed')
    r=client.post('/api/videos/'+video_id+'/recheck-duration');assert r.status_code==409 and r.json()['error']['code']=='video_file_changed'
    path.write_bytes(b'original video content')
    monkeypatch.setattr(cinematic_quality,'generation_inputs_hash',lambda session,shot:'stale')
    r=client.post('/api/videos/'+video_id+'/recheck-duration');assert r.status_code==409 and r.json()['error']['code']=='video_inputs_stale'


def test_recheck_does_not_skip_other_failed_checks(client,failed_video):
    video_id,_=failed_video
    with client.app.state.session_factory() as s:
        s.add(m.QualityCheck(video_revision_id=video_id,check_type='other_integrity_check',status='failed',details_json={}));s.commit()
    r=client.post('/api/videos/'+video_id+'/recheck-duration')
    assert r.status_code==409 and r.json()['error']['code']=='duration_recheck_other_failure'
    with client.app.state.session_factory() as s:
        assert s.get(m.VideoRevision,video_id).review_status=='needs_regeneration'


def test_recheck_does_not_skip_required_audio_mix(client,failed_video):
    video_id,_=failed_video
    with client.app.state.session_factory() as s:
        v=s.get(m.VideoRevision,video_id);j=s.get(m.GenerationJob,v.generation_job_id);params=dict(j.parameters_json);snap=dict(params['screenplay_snapshot']);snap['resolved_audio_events']=[{'relative_path':'voice/test.wav'}];params['screenplay_snapshot']=snap;j.parameters_json=params;s.commit()
    r=client.post('/api/videos/'+video_id+'/recheck-duration')
    assert r.status_code==409 and r.json()['error']['code']=='duration_recheck_audio_unverified'


def test_studio_reuses_existing_paid_task_without_provider_submit(client,settings,failed_video,monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from app.api import routes
    video_id,_=failed_video
    monkeypatch.setattr(studio_pipeline,'_assert_shot_live',lambda *args:None)
    monkeypatch.setattr(studio_pipeline,'generation_inputs_hash',lambda *args:'expected-hash')
    async def forbidden(*args,**kwargs):raise AssertionError('must not submit paid task again')
    monkeypatch.setattr(routes,'generate_for_shot',forbidden)
    with client.app.state.session_factory() as s:
        v=s.get(m.VideoRevision,video_id);j=s.get(m.GenerationJob,v.generation_job_id)
        entry={'target_id':j.shot_id,'child_id':j.id,'status':'pending','id':'node','attempts':1}
        run=SimpleNamespace(id='run',snapshot_json={'options':{}})
        asyncio.run(studio_pipeline._video(None,s,settings,run,entry,True))
        assert entry['status']=='succeeded' and entry['video_id']==video_id
        assert j.status=='generated' and v.review_status=='pending'


def test_studio_failed_recheck_does_not_resubmit_paid_task(client,settings,failed_video,monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from app.api import routes
    from app.core.errors import AppError
    video_id,path=failed_video;path.write_bytes(b'tampered')
    monkeypatch.setattr(studio_pipeline,'_assert_shot_live',lambda *args:None)
    async def forbidden(*args,**kwargs):raise AssertionError('must not submit paid task again')
    monkeypatch.setattr(routes,'generate_for_shot',forbidden)
    with client.app.state.session_factory() as s:
        v=s.get(m.VideoRevision,video_id);j=s.get(m.GenerationJob,v.generation_job_id)
        entry={'target_id':j.shot_id,'child_id':j.id,'status':'pending','id':'node','attempts':1}
        run=SimpleNamespace(id='run',snapshot_json={'options':{}})
        with pytest.raises(AppError) as error:asyncio.run(studio_pipeline._video(None,s,settings,run,entry,True))
        assert error.value.code=='video_file_changed' and j.status=='failed'


def test_studio_retry_preserves_quality_failed_child_id(client,task,monkeypatch):
    from app.api import studio
    monkeypatch.setattr(studio,'_capture_services',lambda *args:({},{}))
    monkeypatch.setattr(studio,'sync_production_status',lambda *args:None)
    monkeypatch.setattr(studio,'overview',lambda *args:{})
    with client.app.state.session_factory() as s:
        run=m.StudioRun(project_id=task.project_id,status='exception',snapshot_json={'services':{}},nodes_json=[{'id':'video:1','kind':'video','status':'exception','code':'output_quality_failed','child_id':task.job_id,'error':'technical','history':[]}],result_json={})
        s.add(run);s.commit()
        studio.retry(task.project_id,{},s)
        assert run.nodes_json[0]['child_id']==task.job_id and run.nodes_json[0]['status']=='pending'
