"""Retained Review HTTP contracts over isolated synthetic files; no model calls."""
import copy
import hashlib
import shutil
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app import models as m
from app.services.cinematic_quality import generation_inputs_hash
from app.services import studio_pipeline as pipeline
from test_studio_review import setup_review, approve


@pytest.fixture
def reviewed(client, settings, monkeypatch):
    project_id, shots = setup_review(client, settings)
    shot_id, original_id = shots[0]
    approve(client, original_id)
    with client.app.state.session_factory() as session:
        shot = session.get(m.StoryboardShot, shot_id)
        original = session.get(m.VideoRevision, original_id)
        job = session.get(m.GenerationJob, original.generation_job_id)
        job.parameters_json = {'screenplay_snapshot': {'cinematic': {'preflight': {
            'generation_inputs_hash': generation_inputs_hash(session, shot)}}}}
        run = session.scalar(select(m.StudioRun).where(m.StudioRun.project_id == project_id))
        leaf = pipeline.node('quality_check', shot_id, ['video:' + shot_id])
        leaf.update(status='succeeded', video_id=original_id)
        video_node = pipeline.node('video', shot_id)
        video_node.update(status='succeeded', video_id=original_id, child_id=job.id)
        run.nodes_json = [video_node, leaf]
        session.add(m.StudioDecision(project_id=project_id, shot_id=shot_id, action='keep',
            video_id=original_id, position=1, revision=4))
        session.commit()

    async def isolated_generation(request, session, shot, payload, *, instruction=None, **kwargs):
        # Only the model transport is replaced. Revision/approval handlers and
        # their persisted ownership/snapshot contracts are the real HTTP path.
        revision = session.scalar(select(func.max(m.VideoRevision.revision)).where(
            m.VideoRevision.shot_id == shot.id)) + 1
        job = m.GenerationJob(project_id=shot.project_id, shot_id=shot.id,
            provider='isolated_no_model_contract', status='generated', prompt=instruction or 'Synthetic fixture',
            parameters_json={'screenplay_snapshot': {'cinematic': {'preflight': {
                'generation_inputs_hash': generation_inputs_hash(session, shot)}}}})
        session.add(job); session.flush()
        relative = 'review-revision-' + str(revision) + '.mp4'
        source = settings.media_root / 'isolated_review_fixture.mp4'
        shutil.copy2(source, settings.media_root / relative)
        digest = hashlib.sha256((settings.media_root / relative).read_bytes()).hexdigest()
        video = m.VideoRevision(project_id=shot.project_id, shot_id=shot.id,
            generation_job_id=job.id, revision=revision, relative_path=relative,
            review_status='pending', metadata_json={'sha256': digest})
        session.add(video); session.flush()
        for check in ('file_readable', 'media_decode'):
            session.add(m.QualityCheck(video_revision_id=video.id, check_type=check, status='passed'))
        session.flush()
        return job, video

    monkeypatch.setattr('app.api.routes.generate_for_shot', isolated_generation)
    def revise():
        result = client.post('/api/videos/' + original_id + '/revise',
            json={'instruction': 'New explicitly reviewed take; isolated transport fixture'})
        assert result.status_code == 202, result.text
        return result.json()['video_revision']['id']
    def current():
        response = client.get('/api/studio/projects/' + project_id)
        assert response.status_code == 200, response.text
        return next(row for row in response.json()['shots'] if row['id'] == shot_id)
    return SimpleNamespace(client=client, settings=settings, project_id=project_id,
        shot_id=shot_id, original_id=original_id, revise=revise, current=current)


def test_retained_revision_approval_supersedes_old_kept_decision(reviewed):
    new_id = reviewed.revise()
    # A pending take is not silently treated as the newly approved output.
    assert reviewed.current()['video']['id'] == reviewed.original_id
    approve(reviewed.client, new_id)
    view = reviewed.current()
    assert view['video']['id'] == new_id and view['video']['review_status'] == 'approved'
    assert view['decision']['video_id'] == reviewed.original_id  # history only
    with reviewed.client.app.state.session_factory() as session:
        decision = session.scalar(select(m.StudioDecision).where(
            m.StudioDecision.project_id == reviewed.project_id,
            m.StudioDecision.shot_id == reviewed.shot_id))
        assert decision.action == 'keep' and decision.revision == 4


@pytest.mark.parametrize('status', ['pending', 'running', 'waiting_dependencies', 'exception'])
def test_required_unfinished_leaf_never_falls_back_even_to_new_approved_take(reviewed, status):
    new_id = reviewed.revise(); approve(reviewed.client, new_id)
    with reviewed.client.app.state.session_factory() as session:
        run = session.scalar(select(m.StudioRun).where(m.StudioRun.project_id == reviewed.project_id))
        nodes = copy.deepcopy(run.nodes_json)
        nodes[-1].update(status=status)  # an old persisted video_id must not bypass this guard
        run.nodes_json = nodes; session.commit()
    view = reviewed.current()
    assert view['video'] is None and view['status'] == status


def test_rejected_old_leaf_cannot_hide_new_approved_revision(reviewed):
    new_id = reviewed.revise(); approve(reviewed.client, new_id)
    result = reviewed.client.post('/api/videos/' + reviewed.original_id + '/reject',
        json={'operator':'acceptance-test','reason':'Superseded old take'})
    assert result.status_code == 200, result.text
    assert reviewed.current()['video']['id'] == new_id
    result = reviewed.client.post('/api/videos/' + new_id + '/reject',
        json={'operator':'acceptance-test','reason':'Rejected replacement'})
    assert result.status_code == 200, result.text
    assert reviewed.current()['video'] is None


@pytest.mark.parametrize('changed', ['stale_hash', 'missing_hash', 'foreign_job_project', 'foreign_job_shot', 'needs_regeneration'])
def test_ineligible_approved_take_cannot_replace_completed_leaf(reviewed, changed):
    new_id = reviewed.revise(); approve(reviewed.client, new_id)
    with reviewed.client.app.state.session_factory() as session:
        video = session.get(m.VideoRevision, new_id)
        job = session.get(m.GenerationJob, video.generation_job_id)
        if changed == 'stale_hash':
            job.parameters_json = {'screenplay_snapshot': {'cinematic': {'preflight': {'generation_inputs_hash': 'not-current'}}}}
        elif changed == 'missing_hash':
            job.parameters_json = {}
        elif changed == 'foreign_job_project':
            other = m.Project(name='Foreign isolated owner');session.add(other);session.flush();job.project_id = other.id
        elif changed == 'foreign_job_shot':
            other = session.scalar(select(m.StoryboardShot).where(
                m.StoryboardShot.project_id == reviewed.project_id, m.StoryboardShot.id != reviewed.shot_id))
            job.shot_id = other.id
        else:
            video.review_status = 'needs_regeneration'
        session.commit()
    assert reviewed.current()['video']['id'] == reviewed.original_id


def test_independent_current_input_take_can_be_explicitly_approved(reviewed):
    new_id = reviewed.revise()
    with reviewed.client.app.state.session_factory() as session:
        video = session.get(m.VideoRevision, new_id);job = session.get(m.GenerationJob, video.generation_job_id)
        job.parameters_json = {k:v for k,v in job.parameters_json.items() if k != 'based_on_video_id'}
        session.commit()
    approve(reviewed.client, new_id)
    assert reviewed.current()['video']['id'] == new_id


def test_without_current_dag_latest_pending_stays_pending_and_rejected_is_ignored(reviewed):
    new_id = reviewed.revise()
    with reviewed.client.app.state.session_factory() as session:
        run = session.scalar(select(m.StudioRun).where(m.StudioRun.project_id == reviewed.project_id))
        run.nodes_json = [];session.commit()
    view = reviewed.current()
    assert view['video']['id'] == new_id and view['video']['review_status'] == 'pending'
    rejected = reviewed.client.post('/api/videos/' + new_id + '/reject',
        json={'operator':'acceptance-test','reason':'Excluded pending take'})
    assert rejected.status_code == 200, rejected.text
    assert reviewed.current()['video']['id'] == reviewed.original_id
