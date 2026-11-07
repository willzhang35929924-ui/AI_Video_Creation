"""Automatic draft uses real FFmpeg, isolated database, and no human approval."""
import hashlib
import json
import subprocess
from pathlib import Path
from sqlalchemy import select
import pytest
from app import models as m
from app.services.assembly import run_render
from app.services.studio_pipeline import create_pipeline, _queue_automatic_draft, _overall
from test_studio_pipeline import studio
from test_assembly_full_streams import binaries


def test_automatic_draft_preserves_pending_videos_and_renders_once(studio, monkeypatch):
    ffmpeg,ffprobe=binaries(); studio.settings.ffmpeg_path=ffmpeg;studio.settings.ffprobe_path=ffprobe
    session=studio.session
    session.add(m.AIService(module='final_assembly',name='FFmpeg',provider='ffmpeg',base_url='',is_default=True,default_model='ffmpeg',settings_json={'ffmpeg_path':ffmpeg,'ffprobe_path':ffprobe},capabilities_json={}))
    media=studio.settings.media_root;media.mkdir(parents=True,exist_ok=True);source=media/'draft-input.mp4'
    subprocess.run([ffmpeg,'-v','error','-y','-f','lavfi','-i','testsrc2=size=160x128:rate=24:duration=1','-f','lavfi','-i','sine=frequency=440:duration=1.1','-c:v','libx264','-c:a','aac','-pix_fmt','yuv420p',str(source)],check=True,capture_output=True,timeout=60)
    digest=hashlib.sha256(source.read_bytes()).hexdigest()
    monkeypatch.setattr('app.services.studio_pipeline.generation_inputs_hash',lambda *a:'expected')
    monkeypatch.setattr('app.services.cinematic_quality.generation_inputs_hash',lambda *a:'expected')
    nodes=[];vids=[]
    for shot in studio.shots:
        job=m.GenerationJob(project_id=studio.project.id,shot_id=shot.id,provider='video_api',status='generated',prompt='test',parameters_json={'screenplay_snapshot':{'cinematic':{'preflight':{'generation_inputs_hash':'expected'}}}})
        session.add(job);session.flush()
        video=m.VideoRevision(project_id=studio.project.id,shot_id=shot.id,generation_job_id=job.id,revision=1,review_status='pending',relative_path=source.name,metadata_json={'sha256':digest})
        session.add(video);session.flush();vids.append(video.id)
        session.add(m.QualityCheck(video_revision_id=video.id,check_type='media_decode',status='passed',details_json={}))
        session.add(m.QualityCheck(video_revision_id=video.id,check_type='cinematic_output_spec',status='warning',details_json={}))
        nodes.append({'id':'video:'+shot.id,'kind':'video','target_id':shot.id,'status':'succeeded','video_id':video.id,'error':None})
    run=create_pipeline(session,studio.settings,studio.project.id,{'automatic_draft_assembly':True});session.commit()
    run.snapshot_json={**run.snapshot_json,'options':{**run.snapshot_json['options'],'width':160,'height':128}}
    _overall(run,nodes);assert run.status=='waiting_review'
    _queue_automatic_draft(session,studio.settings,run,nodes);session.commit()
    identifier=run.result_json['render_id']; assert run.status=='rendering'
    assert all(session.get(m.VideoRevision,v).review_status=='pending' for v in vids)

    assert session.scalar(select(m.Approval.id)) is None
    _queue_automatic_draft(session,studio.settings,run,nodes);session.commit()
    assert run.result_json['render_id']==identifier
    assert len(list(session.scalars(select(m.AssemblyRender))))==1
    run_render(studio.settings,studio.factory,identifier)
    session.expire_all();render=session.get(m.AssemblyRender,identifier)
    assert render.status=='generated',render.error_message
    assert render.snapshot_json['automatic_draft']['human_review_required'] is True
    assert all(c['full_stream_timing']['preserves_full_streams'] for c in render.snapshot_json['clips'])
    path=media/render.relative_path; info=json.loads(subprocess.run([ffprobe,'-v','error','-show_streams','-of','json',str(path)],capture_output=True,check=True,timeout=20).stdout)
    assert {'video','audio'} <= {x['codec_type'] for x in info['streams']}
    assert all(session.get(m.VideoRevision,v).review_status=='pending' for v in vids)

    # A user can remove a clip from the automatic draft and render another
    # explicitly unreviewed version without changing any review decisions.
    from types import SimpleNamespace
    from app.api.assemblies import patch, submit
    from app.schemas_product import AssemblyPatch, AssemblySubmit, TimelineRequest
    assembly=session.get(m.Assembly,render.assembly_id)
    source_timeline=assembly.timeline_json
    reduced=TimelineRequest.model_validate({'clips':[{'video_id':vids[0]}], 'transitions':[],
        'output':source_timeline['output'],'service_id':source_timeline['service_snapshot']['service_id']})
    request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=studio.settings)))
    updated=patch(assembly.id,AssemblyPatch(name=assembly.name,revision=assembly.revision,timeline=reduced),request,session)
    assert updated['timeline_json']['automatic_draft']['human_review_required'] is True
    second=submit(assembly.id,AssemblySubmit(revision=updated['revision']),request,session,'render')
    assert second['version']==2 and second['snapshot_json']['automatic_draft']['human_review_required'] is True
    run_render(studio.settings,studio.factory,second['id'])
    session.expire_all();second_render=session.get(m.AssemblyRender,second['id'])
    assert second_render.status=='generated',second_render.error_message
    assert (media/second_render.relative_path).is_file()
    assert all(session.get(m.VideoRevision,v).review_status=='pending' for v in vids)
    assert session.scalar(select(m.Approval.id)) is None
    # Previously removed draft clips remain recoverable from render history.
    assembly=session.get(m.Assembly,render.assembly_id)
    restored=TimelineRequest.model_validate({'clips':[{'video_id':vids[0]},{'video_id':vids[1]}],
        'transitions':[{'type':'cut'}], 'output':source_timeline['output'],
        'service_id':source_timeline['service_snapshot']['service_id']})
    updated=patch(assembly.id,AssemblyPatch(name=assembly.name,revision=assembly.revision,timeline=restored),request,session)
    assert len(updated['timeline_json']['clips'])==2
    # A pending video never included in this draft cannot silently bypass review.
    other=m.VideoRevision(project_id=studio.project.id,shot_id=studio.shots[0].id,
        generation_job_id=session.get(m.VideoRevision,vids[0]).generation_job_id,
        revision=9,review_status='pending',relative_path=source.name,metadata_json={'sha256':digest})
    session.add(other);session.commit()
    invalid=TimelineRequest.model_validate({'clips':[{'video_id':other.id}],'transitions':[],
        'output':source_timeline['output'],'service_id':source_timeline['service_snapshot']['service_id']})
    with pytest.raises(Exception) as exc:
        patch(assembly.id,AssemblyPatch(name=assembly.name,revision=updated['revision'],timeline=invalid),request,session)
    assert getattr(exc.value,'code',None)=='draft_input_not_in_timeline'


def test_failed_or_incomplete_shots_cannot_queue_draft(studio):
    run=create_pipeline(studio.session,studio.settings,studio.project.id,{'automatic_draft_assembly':True});studio.session.commit()
    with pytest.raises(Exception) as exc:
        _queue_automatic_draft(studio.session,studio.settings,run,[])
    assert getattr(exc.value,'code',None)=='draft_inputs_incomplete'
    assert studio.session.scalar(select(m.AssemblyRender.id)) is None


def test_start_opt_in_is_frozen_for_paused_history(studio):
    # Keep the public native/legacy fixture; Context-IR is independently covered
    # by its explicit production profile tests, not forced onto every service.
    run=create_pipeline(studio.session,studio.settings,studio.project.id,{'automatic_draft_assembly':True})
    run.status='paused'; studio.session.commit()
    frozen=run.snapshot_json['services'].copy()
    studio.session.expire_all()
    assert run.status=='paused' and run.snapshot_json['options']['automatic_draft_assembly'] is True
    assert run.snapshot_json['services']==frozen


def test_duplicate_page_automatic_draft_http_entry_is_retired(client):
    result=client.post('/api/studio/projects/no-project/automatic-draft',json={
        'expected_run_id':'old-run','enabled':True})
    assert result.status_code==404
