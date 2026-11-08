"""Final-output consumers reject raw revisions, including older approvals."""
import pytest
from app import models as m
from app.services.assembly import run_render
from test_studio_overview_video_selection import reviewed
from test_studio_review import approve,create_assembly,submit,timeline


@pytest.mark.parametrize('state,final_matches',[('processing',False),('failed',False),('ready',False),('ready',True)])
def test_overview_only_selects_the_exact_complete_revision(reviewed,state,final_matches):
    new_id=reviewed.revise();approve(reviewed.client,new_id)
    with reviewed.client.app.state.session_factory() as session:
        video=session.get(m.VideoRevision,new_id);job=session.get(m.GenerationJob,video.generation_job_id)
        job.parameters_json={**job.parameters_json,'revision_postprocess':{
            'state':state,'raw_video_id':new_id,'final_video_id':new_id if final_matches else 'another-output'}}
        session.commit()
    assert reviewed.current()['video']['id']==(new_id if state=='ready' and final_matches else reviewed.original_id)


def test_already_approved_legacy_raw_cannot_enter_a_new_timeline(reviewed):
    new_id=reviewed.revise();approve(reviewed.client,new_id)
    with reviewed.client.app.state.session_factory() as session:
        old=session.get(m.VideoRevision,reviewed.original_id)
        old.metadata_json={**old.metadata_json,'quality_render_id':'historical-quality-evidence'}
        session.commit()
    result=create_assembly(reviewed.client,reviewed.project_id,timeline([new_id]))
    assert result.status_code==409,result.text
    assert result.json()['error']['code']=='revision_postprocess_required'
    assert reviewed.current()['video']['id']==reviewed.original_id


def test_queued_render_rechecks_processing_state_before_executing_ffmpeg(reviewed):
    new_id=reviewed.revise();approve(reviewed.client,new_id)
    created=create_assembly(reviewed.client,reviewed.project_id,timeline([new_id]))
    assert created.status_code==201,created.text
    result=submit(reviewed.client,created.json());assert result.status_code==202,result.text
    render_id=result.json()['id']
    with reviewed.client.app.state.session_factory() as session:
        video=session.get(m.VideoRevision,new_id);job=session.get(m.GenerationJob,video.generation_job_id)
        job.parameters_json={**job.parameters_json,'revision_postprocess':{'state':'processing','final_video_id':None}}
        session.commit()
    run_render(reviewed.settings,reviewed.client.app.state.session_factory,render_id)
    with reviewed.client.app.state.session_factory() as session:
        render=session.get(m.AssemblyRender,render_id)
        assert render.status=='failed'
        assert '后处理' in render.error_message or '配音' in render.error_message
        assert not render.relative_path
