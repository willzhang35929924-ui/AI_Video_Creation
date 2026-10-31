"""HTTP review/transaction regressions with isolated SQL rows, not AI footage."""
import pytest
from sqlalchemy import select
from app import models as m


@pytest.fixture
def review_video(client):
    with client.app.state.session_factory() as session:
        project=m.Project(name='Isolated review transaction')
        session.add(project);session.flush()
        shot=m.StoryboardShot(project_id=project.id,order=1,action='Stationary test object')
        session.add(shot);session.flush()
        job=m.GenerationJob(project_id=project.id,shot_id=shot.id,provider='comfyui_h3',status='generated',prompt='Isolated protocol test',seed=1,parameters_json={})
        session.add(job);session.flush()
        video=m.VideoRevision(project_id=project.id,shot_id=shot.id,generation_job_id=job.id,relative_path='unit-only.bin',revision=1,review_status='needs_review',metadata_json={})
        session.add(video);session.flush()
        session.add(m.QualityCheck(video_revision_id=video.id,check_type='file_readable',status='passed',details_json={'scope':'isolated review gate fixture'}))
        session.commit()
        return video.id


def test_workbench_review_uses_the_same_approval_gate_as_video_api(client,review_video):
    response=client.post(f'/api/production-videos/{review_video}/review',json={'status':'approved','category':'technical','reason':'Unit review transaction','operator':'unit-test'})
    assert response.status_code==200,response.text
    assert response.json()['review_status']=='approved'
    with client.app.state.session_factory() as session:
        assert session.get(m.VideoRevision,review_video).review_status=='approved'
        events=list(session.scalars(select(m.AuditEvent).where(m.AuditEvent.object_type=='cinematic_video_review',m.AuditEvent.object_id==review_video)))
        assert len(events)==1 and events[0].action=='approved'


def test_failed_workbench_review_does_not_record_a_successful_decision(client,review_video):
    with client.app.state.session_factory() as session:
        session.get(m.VideoRevision,review_video).review_status='needs_regeneration';session.commit()
    response=client.post(f'/api/production-videos/{review_video}/review',json={'status':'approved','category':'technical','reason':'Must be rejected','operator':'unit-test'})
    assert response.status_code==409,response.text
    with client.app.state.session_factory() as session:
        assert session.get(m.VideoRevision,review_video).review_status=='needs_regeneration'
        assert session.scalar(select(m.AuditEvent).where(m.AuditEvent.object_type=='cinematic_video_review',m.AuditEvent.object_id==review_video)) is None
