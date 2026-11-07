"""Isolated framing/CAS contracts. No media is generated and no provider is contacted."""
import asyncio
import copy
import hashlib
from types import SimpleNamespace

from fastapi import Request
import httpx
import pytest
from sqlalchemy import func,select,update
from sqlalchemy.orm import Session
from sqlalchemy.sql.dml import Update

from app import models as m
from app.api import studio
from app.models.entities import utcnow
from app.schemas_studio import PipelineStart,ShotFramingUpdate
from app.services import studio_pipeline,studio_voice,video_quality
from app.services.cinematic_quality import generation_inputs_hash
from app.services.studio_project_guard import project_guard


@pytest.fixture(autouse=True)
def no_provider(monkeypatch):
    async def forbidden(*args,**kwargs):raise AssertionError('Framing tests must not request models')
    monkeypatch.setattr(httpx.AsyncClient,'request',forbidden)


@pytest.fixture
def case(client,settings):
    with client.app.state.session_factory() as db:
        project=m.Project(name='Isolated framing edit');db.add(project);db.flush()
        first=m.StoryboardShot(project_id=project.id,order=1,scene_title='First',aspect_ratio='16:9',revision=3,status='approved',approved_at=utcnow())
        second=m.StoryboardShot(project_id=project.id,order=2,scene_title='Second',aspect_ratio='16:9',revision=2,status='draft')
        historical=m.StoryboardShot(project_id=project.id,order=3,scene_title='Old revision',aspect_ratio='16:9',status='draft')
        deleted=m.StoryboardShot(project_id=project.id,order=4,scene_title='Deleted',aspect_ratio='16:9',status='deleted')
        db.add_all([first,second,historical,deleted]);db.flush()
        old=m.ScriptRevision(project_id=project.id,revision=1,parsed_json={'explicit_shot_ids':[historical.id]},raw_text='Isolated old script')
        current=m.ScriptRevision(project_id=project.id,revision=2,parsed_json={'explicit_shot_ids':[first.id,second.id,deleted.id]},raw_text='Isolated current script')
        db.add_all([old,current]);db.flush()
        scene=m.Scene(project_id=project.id,script_revision_id=current.id,order=1,name='Shared unit scene')
        db.add(scene);db.flush()
        db.add_all([m.ShotDetail(shot_id=first.id,scene_id=scene.id),m.ShotDetail(shot_id=second.id,scene_id=scene.id)])
        job=m.GenerationJob(project_id=project.id,shot_id=first.id,provider='unit_fixture',status='generated',prompt='Not an inference result',parameters_json={'frozen':'unchanged'})
        db.add(job);db.flush()
        paths=[];videos=[]
        settings.media_root.mkdir(parents=True,exist_ok=True)
        for index,status in enumerate(['approved','pending','rejected'],1):
            path=settings.media_root/f'unit-original-{index}.mp4';path.write_bytes(b'Explicit metadata fixture; not an actual video.')
            paths.append(path)
            video=m.VideoRevision(project_id=project.id,shot_id=first.id,generation_job_id=job.id,relative_path=path.name,
                revision=index,review_status=status,metadata_json={'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'frozen':'unchanged'})
            db.add(video);db.flush();videos.append(video.id)
        q={'resolution':'1080p','aspect_ratio':'16:9','processing':'lanczos','mode':'high'}
        db.add(m.QualityProfile(scope='project',target_id=project.id,revision=5,settings_json=q));db.commit()
        yield SimpleNamespace(client=client,settings=settings,project_id=project.id,first=first.id,second=second.id,
            old=historical.id,deleted=deleted.id,scene=scene.id,job=job.id,videos=videos,paths=paths,quality=q)


def endpoint(f):return f'/api/studio/projects/{f.project_id}/shot-framing'

def payload(f,ratio='9:16',ids=None):
    revisions={f.first:3,f.second:2,f.old:1,f.deleted:1}
    return {'aspect_ratio':ratio,'shots':[{'id':identifier,'expected_revision':revisions[identifier]} for identifier in (ids or [f.first,f.second])]}

def add_run(f,status):
    with f.client.app.state.session_factory() as db:
        frozen={'options':{'quality':copy.deepcopy(f.quality),'quality_by_shot':{f.first:copy.deepcopy(f.quality)}},
                'h3_context_by_shot':{f.first:{'native_canvas':[1024,576]}},'shot_ids':[f.first,f.second]}
        run=m.StudioRun(project_id=f.project_id,status=status,snapshot_json=frozen,nodes_json=[])
        db.add(run);db.commit();return run.id,copy.deepcopy(frozen)

def assert_unchanged(f):
    with f.client.app.state.session_factory() as db:
        assert (db.get(m.StoryboardShot,f.first).aspect_ratio,db.get(m.StoryboardShot,f.first).revision)==('16:9',3)
        assert (db.get(m.StoryboardShot,f.second).aspect_ratio,db.get(m.StoryboardShot,f.second).revision)==('16:9',2)
        assert db.scalar(select(func.count()).select_from(m.AuditEvent).where(m.AuditEvent.action=='aspect_ratio_changed'))==0


def test_get_only_lists_current_live_members(case):
    result=case.client.get(endpoint(case));assert result.status_code==200,result.text
    body=result.json();assert {s['id'] for s in body['shots']}=={case.first,case.second}
    assert body['supported_aspect_ratios']==['16:9','9:16','1:1'] and body['requires_new_run'] is False
    assert all(s['editable'] and s['block_reason'] is None for s in body['shots'])


def test_batch_change_invalidates_approved_review_but_preserves_files_quality_and_frozen_run(case):
    run_id,frozen=add_run(case,'paused')
    before=[path.read_bytes() for path in case.paths]
    with case.client.app.state.session_factory() as db:
        old_hash=generation_inputs_hash(db,db.get(m.StoryboardShot,case.first))
    result=case.client.put(endpoint(case),json=payload(case));assert result.status_code==200,result.text
    body=result.json();assert set(body['updated_shot_ids'])=={case.first,case.second} and body['requires_new_run'] is True
    with case.client.app.state.session_factory() as db:
        first=db.get(m.StoryboardShot,case.first);second=db.get(m.StoryboardShot,case.second)
        assert (first.aspect_ratio,first.revision,first.status,first.approved_at)==('9:16',4,'draft',None)
        assert (second.aspect_ratio,second.revision)==('9:16',3)
        assert [db.get(m.VideoRevision,id).review_status for id in case.videos]==['needs_regeneration','needs_regeneration','rejected']
        assert generation_inputs_hash(db,first)!=old_hash
        assert db.get(m.GenerationJob,case.job).parameters_json=={'frozen':'unchanged'}
        assert db.get(m.StudioRun,run_id).snapshot_json==frozen and db.get(m.StudioRun,run_id).status=='paused'
        quality=db.scalar(select(m.QualityProfile).where(m.QualityProfile.target_id==case.project_id))
        assert quality.revision==5 and quality.settings_json==case.quality
        audits=list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.action=='aspect_ratio_changed')))
        assert len(audits)==2 and all(a.details_json['run_id']==run_id for a in audits)
        assert db.scalar(select(func.count()).select_from(m.GenerationJob))==1
    assert [path.read_bytes() for path in case.paths]==before
    assert case.client.get(endpoint(case)).json()['requires_new_run'] is True
    blocked=case.client.post(f'/api/studio/projects/{case.project_id}/resume')
    assert blocked.status_code==409 and blocked.json()['error']['code']=='studio_framing_changed'


def test_noop_keeps_revision_timestamp_approval_and_audit(case):
    with case.client.app.state.session_factory() as db:
        shot=db.get(m.StoryboardShot,case.first);stamp=shot.updated_at;approved=shot.approved_at
    result=case.client.put(endpoint(case),json=payload(case,'16:9'));assert result.status_code==200,result.text
    assert result.json()['updated_shot_ids']==[]
    with case.client.app.state.session_factory() as db:
        shot=db.get(m.StoryboardShot,case.first)
        assert (shot.revision,shot.status,shot.updated_at,shot.approved_at)==(3,'approved',stamp,approved)
        assert db.get(m.VideoRevision,case.videos[0]).review_status=='approved'
    assert_unchanged(case)


def test_draft_shot_change_invalidates_kept_approved_video_without_touching_original(case):
    before=[path.read_bytes() for path in case.paths]
    with case.client.app.state.session_factory() as db:
        shot=db.get(m.StoryboardShot,case.first);shot.status='draft';shot.approved_at=None
        db.add_all([
            m.StudioDecision(project_id=case.project_id,shot_id=case.first,action='keep',video_id=case.videos[0],position=1),
            m.StudioDecision(project_id=case.project_id,shot_id=case.second,action='exclude',position=2)])
        db.commit()
    result=case.client.put(endpoint(case),json=payload(case,ids=[case.first]))
    assert result.status_code==200,result.text
    with case.client.app.state.session_factory() as db:
        shot=db.get(m.StoryboardShot,case.first)
        assert (shot.aspect_ratio,shot.revision,shot.status,shot.approved_at)==('9:16',4,'draft',None)
        assert [db.get(m.VideoRevision,id).review_status for id in case.videos]==['needs_regeneration','needs_regeneration','rejected']
        audit=db.scalar(select(m.AuditEvent).where(m.AuditEvent.action=='aspect_ratio_changed'))
        assert audit.details_json['approval_invalidated'] is False
        assert audit.details_json['invalidated_videos']==2
        decision=db.scalar(select(m.StudioDecision).where(m.StudioDecision.shot_id==case.first))
        assert decision.action=='keep' and decision.video_id==case.videos[0]
    approve=case.client.post(f'/api/videos/{case.videos[0]}/approve',json={'reason':'Cannot revive stale framing'})
    assert approve.status_code==409 and approve.json()['error']['code']=='video_needs_regeneration'
    compose=case.client.post('/api/assemblies',json={'project_id':case.project_id,'name':'Stale framing blocked',
        'timeline':{'clips':[{'video_id':case.videos[0]}],'transitions':[],'output':{'resolution_mode':'source'}}})
    assert compose.status_code==422 and compose.json()['error']['code']=='unapproved_input'
    assert [path.read_bytes() for path in case.paths]==before


def readable_check(db,video_id):
    db.add(m.QualityCheck(video_revision_id=video_id,check_type='file_readable',status='passed'))


def test_framing_change_and_change_back_cannot_revive_legacy_rejected_video(case,monkeypatch):
    from app.services import assembly
    # This is an approval/scope contract; no FFprobe or provider is invoked.
    monkeypatch.setattr(assembly,'probe',lambda *_:{'duration':5})
    rejected=case.videos[2]
    with case.client.app.state.session_factory() as db:
        readable_check(db,rejected);db.commit()
    assert case.client.put(endpoint(case),json=payload(case,ids=[case.first])).status_code==200
    restored=case.client.put(endpoint(case),json={'aspect_ratio':'16:9','shots':[{'id':case.first,'expected_revision':4}]})
    assert restored.status_code==200,restored.text
    response=case.client.post(f'/api/videos/{rejected}/approve',json={'reason':'Old framing stays stale'})
    assert response.status_code==409,response.text
    assert response.json()['error']['code']=='video_needs_regeneration'
    assembly=case.client.post('/api/assemblies',json={'project_id':case.project_id,'name':'Rejected source blocked',
        'timeline':{'clips':[{'video_id':rejected}],'transitions':[],'output':{'resolution_mode':'source'}}})
    assert assembly.status_code==422 and assembly.json()['error']['code']=='unapproved_input'
    retired=case.client.post(f'/api/studio/projects/{case.project_id}/shots/{case.first}/decision',
        json={'action':'keep','video_id':rejected})
    assert retired.status_code==404
    with case.client.app.state.session_factory() as db:
        old=db.get(m.VideoRevision,rejected)
        assert old.review_status=='rejected'
        assert old.metadata_json['frozen']=='unchanged'
        assert old.metadata_json['framing_invalidated']=={'shot_revision':5,'before':'9:16','after':'16:9'}
        assert not db.scalar(select(m.Approval.id).where(m.Approval.object_id==rejected))
        decision=db.scalar(select(m.StudioDecision).where(m.StudioDecision.shot_id==case.first))
        assert decision is None or decision.action=='pending'


def test_fresh_video_after_explicit_framing_change_can_be_approved(case):
    assert case.client.put(endpoint(case),json=payload(case,ids=[case.first])).status_code==200
    with case.client.app.state.session_factory() as db:
        fresh=m.VideoRevision(project_id=case.project_id,shot_id=case.first,generation_job_id=case.job,
            relative_path=case.paths[0].name,revision=4,review_status='pending',metadata_json={'unit_fixture':True})
        db.add(fresh);db.flush();fresh_id=fresh.id;readable_check(db,fresh_id);db.commit()
    response=case.client.post(f'/api/videos/{fresh_id}/approve',json={'reason':'Fresh framing fixture'})
    assert response.status_code==200,response.text
    assert response.json()['review_status']=='approved'
    with case.client.app.state.session_factory() as db:
        assert db.get(m.VideoRevision,fresh_id).metadata_json=={'unit_fixture':True}
        assert db.get(m.VideoRevision,case.videos[2]).review_status=='rejected'


@pytest.mark.parametrize('concurrent_change',['audit','marker'])
def test_approval_write_cas_rechecks_framing_after_initial_validation(case,monkeypatch,concurrent_change):
    target=case.videos[1]
    with case.client.app.state.session_factory() as db:
        readable_check(db,target);db.commit()
    original=Session.execute;injected=False
    def racing(self,statement,*args,**kwargs):
        nonlocal injected
        if isinstance(statement,Update) and statement.table.name=='video_revisions' and target in statement.compile().params.values() and not injected:
            injected=True
            if concurrent_change=='audit':
                self.add(m.AuditEvent(object_type='shot',object_id=case.first,action='aspect_ratio_changed',
                    details_json={'project_id':case.project_id,'before':'16:9','after':'9:16'}));self.flush()
            else:
                original(self,update(m.VideoRevision).where(m.VideoRevision.id==target).values(
                    metadata_json={'framing_invalidated':{'shot_revision':4}}).execution_options(synchronize_session=False))
        return original(self,statement,*args,**kwargs)
    monkeypatch.setattr(Session,'execute',racing)
    response=case.client.post(f'/api/videos/{target}/approve',json={'reason':'Stale read cannot overwrite newer framing'})
    assert injected
    assert response.status_code==409,response.text
    assert response.json()['error']['code']=='video_review_changed'
    with case.client.app.state.session_factory() as db:
        assert db.get(m.VideoRevision,target).review_status=='pending'
        assert not db.scalar(select(m.Approval.id).where(m.Approval.object_id==target))


@pytest.mark.parametrize('status',['queued','running','waiting_review'])
def test_active_current_run_requires_pause_before_edit(case,status):
    add_run(case,status)
    body=case.client.get(endpoint(case)).json();assert all(not s['editable'] for s in body['shots'])
    response=case.client.put(endpoint(case),json=payload(case))
    assert response.status_code==409 and response.json()['error']['code']=='shot_framing_blocked'
    assert_unchanged(case)


@pytest.mark.parametrize('status',['paused','exceptions','completed','completed_with_errors','cancelled','failed'])
def test_paused_or_finished_run_can_edit_but_requires_new_run(case,status):
    run_id,frozen=add_run(case,status)
    response=case.client.put(endpoint(case),json=payload(case,'1:1'))
    assert response.status_code==200,response.text
    assert response.json()['requires_new_run'] is True
    with case.client.app.state.session_factory() as db:assert db.get(m.StudioRun,run_id).snapshot_json==frozen


@pytest.mark.parametrize('kind,status',[
    ('generation','queued'),('generation','running'),('generation','submitted'),('generation','submitting'),
    ('generation','unknown'),('generation','reconciling'),('generation','cancelling'),
    ('visual','queued'),('visual','running'),('visual','unknown'),
    ('media','queued'),('media','unknown'),('quality','queued'),('quality','running'),('quality','unknown'),('shared_visual','unknown')])
def test_active_or_unknown_sources_block_the_whole_selected_batch(case,kind,status):
    add_run(case,'paused')
    with case.client.app.state.session_factory() as db:
        if kind=='generation':db.add(m.GenerationJob(project_id=case.project_id,shot_id=case.first,provider='unit_fixture',status=status,prompt='No provider call'))
        elif kind in {'visual','shared_visual'}:db.add(m.ProductionVisual(project_id=case.project_id,shot_id=case.first if kind=='visual' else None,
            scene_id=case.scene,kind='keyframe' if kind=='visual' else 'scene_reference',status=status,prompt='No image inference'))
        elif kind=='media':db.add(m.MediaProcessTask(project_id=case.project_id,shot_id=case.first,kind='audio_mix',slot='audio_mix',status=status))
        else:db.add(m.QualityRender(source_video_id=case.videos[0],status=status))
        db.commit()
    response=case.client.put(endpoint(case),json=payload(case,ids=[case.second,case.first]))
    assert response.status_code==409,response.text
    rows=case.client.get(endpoint(case)).json()['shots']
    assert next(s for s in rows if s['id']==case.first)['editable'] is False
    assert_unchanged(case)


@pytest.mark.parametrize('bad',['historical','deleted','foreign','duplicate','empty','stale','invalid_ratio','bool_revision'])
def test_invalid_scope_or_revision_rejects_without_partial_change(case,bad):
    body=payload(case)
    if bad=='historical':body['shots'][1]={'id':case.old,'expected_revision':1}
    elif bad=='deleted':body['shots'][1]={'id':case.deleted,'expected_revision':1}
    elif bad=='foreign':
        with case.client.app.state.session_factory() as db:
            project=m.Project(name='Other isolated project');db.add(project);db.flush()
            shot=m.StoryboardShot(project_id=project.id,order=1);db.add(shot);db.commit()
            body['shots'][1]={'id':shot.id,'expected_revision':1}
    elif bad=='duplicate':body['shots'][1]=copy.deepcopy(body['shots'][0])
    elif bad=='empty':body['shots']=[]
    elif bad=='stale':body['shots'][1]['expected_revision']=1
    elif bad=='bool_revision':body['shots'][0]['expected_revision']=True
    else:body['aspect_ratio']='4:3'
    response=case.client.put(endpoint(case),json=body)
    assert response.status_code in (409,422),response.text
    assert_unchanged(case)


def test_second_sql_cas_race_rolls_back_first_changed_shot_and_approval(case,monkeypatch):
    original=Session.execute;injected=False
    def racing(self,statement,*args,**kwargs):
        nonlocal injected
        if isinstance(statement,Update) and statement.table.name=='storyboard_shots' and case.second in statement.compile().params.values() and not injected:
            injected=True
            assert original(self,select(m.StoryboardShot.aspect_ratio).where(m.StoryboardShot.id==case.first)).scalar_one()=='9:16'
            original(self,update(m.StoryboardShot).where(m.StoryboardShot.id==case.second).values(revision=m.StoryboardShot.revision+1).execution_options(synchronize_session=False))
        return original(self,statement,*args,**kwargs)
    monkeypatch.setattr(Session,'execute',racing)
    response=case.client.put(endpoint(case),json=payload(case))
    assert response.status_code==409 and response.json()['error']['code']=='shot_framing_conflict'
    assert injected
    assert_unchanged(case)
    with case.client.app.state.session_factory() as db:assert db.get(m.VideoRevision,case.videos[0]).review_status=='approved'


def test_framing_marker_does_not_apply_to_a_new_run(case):
    add_run(case,'paused')
    assert case.client.put(endpoint(case),json=payload(case)).json()['requires_new_run'] is True
    add_run(case,'paused')
    assert case.client.get(endpoint(case)).json()['requires_new_run'] is False


def test_start_and_framing_share_project_guard_and_start_captures_saved_ratio(case,monkeypatch):
    monkeypatch.setattr(studio_voice,'assert_voice_preparation',lambda *a,**k:None)
    async def exercise():
        request=Request({'type':'http','app':case.client.app})
        with case.client.app.state.session_factory() as edit_db,case.client.app.state.session_factory() as start_db:
            async with project_guard(case.client.app,case.project_id):
                editing=asyncio.create_task(studio.save_shot_framing(case.project_id,ShotFramingUpdate.model_validate(payload(case)),request,edit_db))
                starting=asyncio.create_task(studio.start(case.project_id,PipelineStart(),request,start_db))
                await asyncio.sleep(0)
                assert not editing.done() and not starting.done()
            await asyncio.wait_for(editing,5);await asyncio.wait_for(starting,5)
    asyncio.run(exercise())
    with case.client.app.state.session_factory() as db:
        run=studio_pipeline.latest_run(db,case.project_id)
        assert run.snapshot_json['options']['quality_by_shot'][case.first]['aspect_ratio']=='9:16'
        assert db.scalar(select(func.count()).select_from(m.GenerationJob))==1


def test_quality_rejection_explains_actual_shot_orientation(case,monkeypatch):
    monkeypatch.setattr(video_quality,'seedvr_capabilities',lambda *_:{'state':'verified','verified_targets':[
        {'resolution':'1080p','aspect_ratio':'9:16'}]})
    with case.client.app.state.session_factory() as db:
        with pytest.raises(Exception) as caught:
            studio_pipeline.create_pipeline(db,case.settings,case.project_id,{'quality':{
                'resolution':'1080p','aspect_ratio':'9:16','processing':'seedvr2','mode':'high'}})
        error=caught.value
        assert error.code=='quality_method_unverified'
        assert all(text in error.message for text in ('第1镜','16:9','1080p/seedvr2','1920×1080','保存镜头画幅'))
        assert error.details['shot_id']==case.first and error.details['aspect_ratio']=='16:9'
        assert error.details['resolution']=='1080p'
        assert db.scalar(select(func.count()).select_from(m.StudioRun))==0
