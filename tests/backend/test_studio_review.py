"""Actual FFmpeg processing of an isolated synthetic fixture; no AI generation."""
import json,shutil,subprocess
from pathlib import Path
import pytest
from app import models as m
from app.services.assembly import run_render
from app.core.config import Settings

def setup_review(client,settings):
    configured=Settings.from_env()
    ffmpeg=shutil.which(configured.ffmpeg_path) or shutil.which('ffmpeg')
    ffprobe=shutil.which(configured.ffprobe_path) or shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:pytest.skip('real FFmpeg and FFprobe not available on this host')
    settings.ffmpeg_path=ffmpeg;settings.ffprobe_path=ffprobe
    # Keep the assembly contract independent of deletable production reports and
    # user footage. These pixels verify trim/subtitles/retention, never AI quality.
    fixture=settings.media_root/'isolated_review_fixture.mp4'
    fixture.parent.mkdir(parents=True,exist_ok=True)
    subprocess.run([settings.ffmpeg_path,'-nostdin','-v','error','-y','-f','lavfi','-i',
        'color=c=gray:s=640x360:r=24:d=2','-f','lavfi','-i','anullsrc=r=48000:cl=stereo',
        '-shortest','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac',str(fixture)],
        check=True,capture_output=True,timeout=30)
    from sqlalchemy import select
    with client.app.state.session_factory() as s:
        service=s.scalar(select(m.AIService).where(m.AIService.provider=='ffmpeg'))
        service.settings_json={**service.settings_json,'ffmpeg_path':settings.ffmpeg_path,'ffprobe_path':settings.ffprobe_path}
        project=m.Project(name='隔离审片与真实后处理测试');s.add(project);s.flush()
        script=m.ScriptRevision(project_id=project.id,revision=1,raw_text='隔离真实文件测试');s.add(script);s.flush()
        scene=m.Scene(project_id=project.id,script_revision_id=script.id,order=1,name='测试场景');s.add(scene);s.flush()
        person=m.Character(slug=project.id,display_name='测试说话人');s.add(person);s.flush();ids=[]
        for i in range(3):
            shot=m.StoryboardShot(project_id=project.id,order=i+1,scene_title='镜头'+str(i+1),duration_seconds=5);s.add(shot);s.flush();s.add(m.ShotDetail(shot_id=shot.id,scene_id=scene.id))
            participant=m.ShotCharacter(shot_id=shot.id,character_id=person.id,order=1);s.add(participant);s.flush();s.add(m.DialogueLine(shot_id=shot.id,participant_id=participant.id,order=1,text='这是镜头字幕。'))
            job=m.GenerationJob(project_id=project.id,shot_id=shot.id,provider='existing_real_file_processing_test',status='generated',prompt='This test does not call a model.');s.add(job);s.flush()
            path=settings.media_root/('input'+str(i)+'.mp4');shutil.copy2(fixture,path)
            video=m.VideoRevision(project_id=project.id,shot_id=shot.id,generation_job_id=job.id,revision=1,relative_path=path.name);s.add(video);s.flush()
            for kind in ['file_readable','media_decode']:s.add(m.QualityCheck(video_revision_id=video.id,check_type=kind,status='passed'))
            ids.append((shot.id,video.id))
        s.add(m.StudioRun(project_id=project.id,status='waiting_review',snapshot_json={},nodes_json=[],result_json={}))
        s.commit();return project.id,ids

def test_one_review_keeps_excludes_reorders_trims_subtitles_and_retains_versions(client,settings):
    project,ids=setup_review(client,settings);base='/api/studio/projects/'+project
    for i,(shot,video) in enumerate(ids):
        result=client.post(base+'/shots/'+shot+'/decision',json={'action':'exclude' if i==1 else 'keep','video_id':video,'trim_start':.25,'trim_end':1})
        assert result.status_code==200,result.text
    moved=client.post(base+'/shots/'+ids[2][0]+'/decision',json={'action':'keep','video_id':ids[2][1],'trim_start':.25,'trim_end':1,'position':1})
    assert moved.status_code==200,moved.text
    overview=client.get(base).json();positions={sh['id']:sh['decision']['position'] for sh in overview['shots']};assert len(set(positions.values()))==3
    result=client.post(base+'/compose',json={'subtitles':True,'transition':'crossfade','transition_duration':.25,'width':640,'height':360})
    assert result.status_code==202,result.text
    with client.app.state.session_factory() as s:
        from sqlalchemy import select
        pipeline=s.scalar(select(m.StudioRun).where(m.StudioRun.project_id==project))
        assert pipeline.status=='rendering' and pipeline.result_json['render_id']==result.json()['render_id']
        render=s.get(m.AssemblyRender,result.json()['render_id']);assert [c['video_id'] for c in render.snapshot_json['clips']]==[ids[2][1],ids[0][1]]
        assert render.snapshot_json['duration']==1.25 and len(render.snapshot_json['subtitle_cues'])==2
    run_render(settings,client.app.state.session_factory,result.json()['render_id'])
    with client.app.state.session_factory() as s:
        render=s.get(m.AssemblyRender,result.json()['render_id']);assert render.status=='generated',render.error_message
        path=settings.media_root/render.relative_path
        probe=json.loads(subprocess.check_output([settings.ffprobe_path,'-v','error','-show_streams','-of','json',str(path)]))
        assert any(stream.get('codec_name')=='mov_text' for stream in probe['streams'])
    second=client.post(base+'/compose',json={'subtitles':False});assert second.status_code==202,second.text
    assert second.json()['render_id']!=result.json()['render_id']
    assert path.is_file() and all((settings.media_root/('input'+str(i)+'.mp4')).exists() for i in range(3))

def test_unreviewed_or_invalid_trim_cannot_compose(client,settings):
    project,ids=setup_review(client,settings);base='/api/studio/projects/'+project
    assert client.post(base+'/compose',json={}).status_code==409
    shot,video=ids[0]
    assert client.post(base+'/shots/'+shot+'/decision',json={'action':'keep','video_id':video,'trim_end':9999}).status_code==422


def test_explicit_frozen_assembly_route_selects_new_render_service(client,settings):
    from sqlalchemy import select
    from app.services.ai_services import service_snapshot
    project,ids=setup_review(client,settings);base='/api/studio/projects/'+project
    with client.app.state.session_factory() as session:
        other=m.AIService(module='final_assembly',provider='ffmpeg',name='Explicit second tool profile',settings_json={
            'ffmpeg_path':settings.ffmpeg_path,'ffprobe_path':settings.ffprobe_path},enabled=True)
        session.add(other);session.flush()
        snap=service_snapshot(other)
        run=session.scalar(select(m.StudioRun).where(m.StudioRun.project_id==project))
        run.snapshot_json={'routes':{'final_assembly':{'slot':'final_assembly','configured':True,'selected':{'service_snapshot':snap},'profile':{'revision':1,'settings':{'parameters':{}}}}}}
        session.commit();selected_id=other.id
    for index,(shot,video) in enumerate(ids):
        assert client.post(base+'/shots/'+shot+'/decision',json={'action':'keep' if index==0 else 'exclude','video_id':video}).status_code==200
    result=client.post(base+'/compose',json={'width':640,'height':360})
    assert result.status_code==202,result.text
    with client.app.state.session_factory() as session:
        render=session.get(m.AssemblyRender,result.json()['render_id'])
        assert render.snapshot_json['service_snapshot']['service_id']==selected_id
        assert render.snapshot_json['task_route_snapshot']['selected']['service_snapshot']['service_id']==selected_id


def test_regeneration_conflict_preserves_kept_video_order_trim_and_decision(client,settings):
    from sqlalchemy import select
    project,ids=setup_review(client,settings);base='/api/studio/projects/'+project
    shot,video=ids[0]
    kept=client.post(base+'/shots/'+shot+'/decision',json={'action':'keep','video_id':video,
        'position':2,'trim_start':.25,'trim_end':1,'instruction':'保留现有结果'})
    assert kept.status_code==200,kept.text
    with client.app.state.session_factory() as db:
        active=m.StudioRun(project_id=project,status='running',snapshot_json={'shot_ids':[ids[1][0]],'options':{}},nodes_json=[])
        db.add(active);db.commit();active_id=active.id
    denied=client.post(base+'/shots/'+shot+'/decision',json={'action':'regenerate','video_id':video,'position':1})
    assert denied.status_code==409,denied.text
    assert denied.json()['error']['code']=='studio_already_active'
    with client.app.state.session_factory() as db:
        decision=db.scalar(select(m.StudioDecision).where(m.StudioDecision.project_id==project,m.StudioDecision.shot_id==shot))
        assert decision.action=='keep' and decision.video_id==video
        assert decision.position==2 and decision.trim_start==.25 and decision.trim_end==1
        assert decision.instruction=='保留现有结果' and decision.revision==kept.json()['revision']
        assert db.get(m.VideoRevision,video).review_status=='approved'
        runs=list(db.scalars(select(m.StudioRun).where(m.StudioRun.project_id==project)))
        assert len(runs)==2 and sum(run.id==active_id for run in runs)==1


def test_invalid_position_does_not_approve_video_or_persist_review(client,settings):
    from sqlalchemy import select
    project,ids=setup_review(client,settings);shot,video=ids[0]
    denied=client.post('/api/studio/projects/'+project+'/shots/'+shot+'/decision',json={
        'action':'keep','video_id':video,'position':len(ids)+1})
    assert denied.status_code==422,denied.text
    assert denied.json()['error']['code']=='studio_position_out_of_range'
    with client.app.state.session_factory() as db:
        assert db.get(m.VideoRevision,video).review_status=='pending'
        assert not db.scalar(select(m.Approval.id).where(m.Approval.object_type=='video_revision',m.Approval.object_id==video))
        decision=db.scalar(select(m.StudioDecision).where(m.StudioDecision.project_id==project,m.StudioDecision.shot_id==shot))
        assert decision is None or decision.action=='pending'


def test_compose_rejects_changed_confirmed_decisions(client,settings):
    project,ids=setup_review(client,settings);base='/api/studio/projects/'+project
    confirmed={}
    for shot,video in ids:
        response=client.post(base+'/shots/'+shot+'/decision',json={'action':'keep','video_id':video})
        assert response.status_code==200,response.text
        confirmed[shot]=response.json()['revision']
    changed=client.post(base+'/shots/'+ids[1][0]+'/decision',json={'action':'exclude','video_id':ids[1][1]})
    assert changed.status_code==200,changed.text
    result=client.post(base+'/compose',json={'expected_decisions':confirmed})
    assert result.status_code==409 and result.json()['error']['code']=='studio_review_changed'
    with client.app.state.session_factory() as db:
        from sqlalchemy import select
        assert not db.scalar(select(m.Assembly.id).where(m.Assembly.project_id==project))


def test_compose_rejects_readable_file_replaced_after_review(client,settings):
    import hashlib
    project,ids=setup_review(client,settings);base='/api/studio/projects/'+project
    for shot,video in ids:
        assert client.post(base+'/shots/'+shot+'/decision',json={'action':'keep','video_id':video}).status_code==200
    with client.app.state.session_factory() as db:
        video=db.get(m.VideoRevision,ids[0][1]);path=settings.media_root/video.relative_path
        video.metadata_json={'sha256':hashlib.sha256(path.read_bytes()).hexdigest()};db.commit()
    with path.open('ab') as stream:stream.write(b'changed-after-review')
    # A trailing change remains readable, but is not the reviewed byte sequence.
    from app.services.assembly import probe
    assert probe(path,settings)['duration']>0
    result=client.post(base+'/compose',json={})
    assert result.status_code==409 and result.json()['error']['code']=='input_file_changed'


def test_keep_decision_calls_real_video_approval_with_request(client,settings):
    project,ids=setup_review(client,settings);shot,video=ids[0]
    response=client.post('/api/studio/projects/'+project+'/shots/'+shot+'/decision',json={'action':'keep','video_id':video})
    assert response.status_code==200,response.text
    with client.app.state.session_factory() as db:
        assert db.get(m.VideoRevision,video).review_status=='approved'
