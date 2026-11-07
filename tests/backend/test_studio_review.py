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

def approve(client, video_id):
    result=client.post('/api/videos/'+video_id+'/approve',json={'operator':'acceptance-test','reason':'Isolated file-processing fixture'})
    assert result.status_code==200,result.text
    return result.json()


def timeline(video_ids, *, trims=False, subtitles=False, output=None, service_id=None):
    clips=[{'video_id':identifier,**({'trim_start':.25,'trim_end':1} if trims else {})} for identifier in video_ids]
    result={'clips':clips,'transitions':[{'type':'crossfade' if trims else 'cut','duration':.25 if trims else 0,'audio':'crossfade' if trims else 'cut'} for _ in clips[1:]],
            'output':{'resolution_mode':'source','fps':24,'subtitles':subtitles,**(output or {})}}
    if service_id:result['service_id']=service_id
    return result


def create_assembly(client,project,config):
    return client.post('/api/assemblies',json={'project_id':project,'name':'Retained review and assembly contract','timeline':config})


def submit(client,assembly):
    return client.post('/api/assemblies/'+assembly['id']+'/render',json={'revision':assembly['revision']})


def test_review_keeps_excludes_reorders_trims_subtitles_and_retains_versions(client,settings):
    project,ids=setup_review(client,settings)
    approve(client,ids[0][1]);approve(client,ids[2][1])
    rejected=client.post('/api/videos/'+ids[1][1]+'/reject',json={'operator':'acceptance-test','reason':'Excluded from selected timeline'})
    assert rejected.status_code==200,rejected.text
    # The retained assembly screen owns clip order and trims; no retired StudioDecision API is involved.
    config=timeline([ids[2][1],ids[0][1]],trims=True,subtitles=True)
    created=create_assembly(client,project,config)
    assert created.status_code==201,created.text
    assembly=created.json()
    result=submit(client,assembly)
    assert result.status_code==202,result.text
    first=result.json();render_id=first['id']
    with client.app.state.session_factory() as s:
        render=s.get(m.AssemblyRender,render_id)
        assert [c['video_id'] for c in render.snapshot_json['clips']]==[ids[2][1],ids[0][1]]
        assert render.snapshot_json['duration']==1.25 and len(render.snapshot_json['subtitle_cues'])==2
        assert all(c['trim_start']==.25 and c['trim_end']==1 for c in render.snapshot_json['clips'])
        assert s.get(m.VideoRevision,ids[1][1]).review_status=='rejected'
    run_render(settings,client.app.state.session_factory,render_id)
    with client.app.state.session_factory() as s:
        render=s.get(m.AssemblyRender,render_id);assert render.status=='generated',render.error_message
        path=settings.media_root/render.relative_path
        probe=json.loads(subprocess.check_output([settings.ffprobe_path,'-v','error','-show_streams','-of','json',str(path)]))
        assert any(stream.get('codec_name')=='mov_text' for stream in probe['streams'])
    patched=client.patch('/api/assemblies/'+assembly['id'],json={'name':assembly['name'],'revision':assembly['revision'],
        'timeline':timeline([ids[2][1],ids[0][1]],trims=True,subtitles=False)})
    assert patched.status_code==200,patched.text
    second=submit(client,patched.json());assert second.status_code==202,second.text
    assert second.json()['id']!=render_id and second.json()['version']==2
    assert path.is_file() and all((settings.media_root/('input'+str(i)+'.mp4')).exists() for i in range(3))
    versions=client.get('/api/assemblies/'+assembly['id']+'/versions').json()
    assert {version['id'] for version in versions}=={render_id,second.json()['id']}


def test_unreviewed_or_invalid_trim_cannot_compose(client,settings):
    project,ids=setup_review(client,settings)
    unreviewed=create_assembly(client,project,timeline([ids[0][1]]))
    assert unreviewed.status_code==422 and unreviewed.json()['error']['code']=='unapproved_input'
    approve(client,ids[0][1])
    config=timeline([ids[0][1]]);config['clips'][0]['trim_end']=9999
    invalid=create_assembly(client,project,config)
    assert invalid.status_code==422 and invalid.json()['error']['code']=='invalid_clip_trim'


def test_explicit_assembly_service_is_frozen_and_changed_service_cannot_render(client,settings):
    project,ids=setup_review(client,settings)
    with client.app.state.session_factory() as session:
        other=m.AIService(module='final_assembly',provider='ffmpeg',name='Explicit second tool profile',settings_json={
            'ffmpeg_path':settings.ffmpeg_path,'ffprobe_path':settings.ffprobe_path},enabled=True)
        session.add(other);session.commit();selected_id=other.id
    approve(client,ids[0][1])
    created=create_assembly(client,project,timeline([ids[0][1]],service_id=selected_id))
    assert created.status_code==201,created.text
    result=submit(client,created.json())
    assert result.status_code==202,result.text
    with client.app.state.session_factory() as session:
        render=session.get(m.AssemblyRender,result.json()['id'])
        assert render.snapshot_json['service_snapshot']['service_id']==selected_id
        old_snapshot=dict(render.snapshot_json)
        session.get(m.AIService,selected_id).revision+=1;session.commit()
    stale=submit(client,created.json())
    assert stale.status_code==409 and stale.json()['error']['code']=='assembly_service_changed'
    with client.app.state.session_factory() as session:
        assert session.get(m.AssemblyRender,result.json()['id']).snapshot_json==old_snapshot


def test_retired_regeneration_entry_cannot_change_kept_history_or_active_run(client,settings):
    from sqlalchemy import select
    project,ids=setup_review(client,settings);shot,video=ids[0]
    approve(client,video)
    with client.app.state.session_factory() as db:
        decision=m.StudioDecision(project_id=project,shot_id=shot,action='keep',video_id=video,position=2,
            trim_start=.25,trim_end=1,instruction='Keep existing result')
        active=m.StudioRun(project_id=project,status='running',snapshot_json={'shot_ids':[ids[1][0]],'options':{}},nodes_json=[])
        db.add_all([decision,active]);db.commit();active_id,revision=active.id,decision.revision
    denied=client.post('/api/studio/projects/'+project+'/shots/'+shot+'/decision',json={'action':'regenerate','video_id':video,'position':1})
    assert denied.status_code==404,denied.text
    with client.app.state.session_factory() as db:
        decision=db.scalar(select(m.StudioDecision).where(m.StudioDecision.project_id==project,m.StudioDecision.shot_id==shot))
        assert decision.action=='keep' and decision.video_id==video
        assert decision.position==2 and decision.trim_start==.25 and decision.trim_end==1
        assert decision.instruction=='Keep existing result' and decision.revision==revision
        assert db.get(m.VideoRevision,video).review_status=='approved'
        runs=list(db.scalars(select(m.StudioRun).where(m.StudioRun.project_id==project)))
        assert len(runs)==2 and sum(run.id==active_id for run in runs)==1


def test_retired_decision_and_compose_entries_cannot_approve_or_persist_review(client,settings):
    from sqlalchemy import select
    project,ids=setup_review(client,settings);shot,video=ids[0]
    base='/api/studio/projects/'+project
    denied=client.post(base+'/shots/'+shot+'/decision',json={'action':'keep','video_id':video,'position':len(ids)+1})
    assert denied.status_code==404,denied.text
    assert client.post(base+'/compose',json={}).status_code==404
    assert client.post(base+'/automatic-draft',json={'expected_run_id':'retired','enabled':True}).status_code==404
    with client.app.state.session_factory() as db:
        assert db.get(m.VideoRevision,video).review_status=='pending'
        assert not db.scalar(select(m.Approval.id).where(m.Approval.object_type=='video_revision',m.Approval.object_id==video))
        assert not db.scalar(select(m.StudioDecision.id).where(m.StudioDecision.project_id==project,m.StudioDecision.shot_id==shot))
        assert not db.scalar(select(m.Assembly.id).where(m.Assembly.project_id==project))


def test_compose_rejects_changed_confirmed_timeline_revision(client,settings):
    from sqlalchemy import select
    project,ids=setup_review(client,settings)
    for _,video in ids:approve(client,video)
    created=create_assembly(client,project,timeline([v for _,v in ids]))
    assert created.status_code==201,created.text
    confirmed=created.json()
    changed=client.patch('/api/assemblies/'+confirmed['id'],json={'name':confirmed['name'],'revision':confirmed['revision'],
        'timeline':timeline([ids[0][1],ids[2][1]])})
    assert changed.status_code==200,changed.text
    result=submit(client,confirmed)
    assert result.status_code==409 and result.json()['error']['code']=='timeline_changed'
    with client.app.state.session_factory() as db:
        assert not db.scalar(select(m.AssemblyRender.id).where(m.AssemblyRender.assembly_id==confirmed['id']))
        assert [c['video_id'] for c in db.get(m.Assembly,confirmed['id']).timeline_json['clips']]==[ids[0][1],ids[2][1]]


def test_compose_rejects_readable_file_replaced_after_review(client,settings):
    import hashlib
    project,ids=setup_review(client,settings)
    for _,video in ids:approve(client,video)
    created=create_assembly(client,project,timeline([v for _,v in ids]));assert created.status_code==201,created.text
    with client.app.state.session_factory() as db:
        video=db.get(m.VideoRevision,ids[0][1]);path=settings.media_root/video.relative_path
        video.metadata_json={'sha256':hashlib.sha256(path.read_bytes()).hexdigest()};db.commit()
    with path.open('ab') as stream:stream.write(b'changed-after-review')
    from app.services.assembly import probe
    assert probe(path,settings)['duration']>0
    result=submit(client,created.json())
    assert result.status_code==409 and result.json()['error']['code']=='input_file_changed'


def test_retained_review_entry_persists_real_video_approval(client,settings):
    project,ids=setup_review(client,settings);_,video=ids[0]
    approve(client,video)
    with client.app.state.session_factory() as db:
        assert db.get(m.VideoRevision,video).review_status=='approved'


def test_final_keeps_reviewed_resolution_and_recovers_legacy_smaller_canvas(client,settings):
    project,ids=setup_review(client,settings)
    approve(client,ids[0][1])
    default=create_assembly(client,project,timeline([ids[0][1]]));assert default.status_code==201,default.text
    default_render=submit(client,default.json());assert default_render.status_code==202,default_render.text
    smaller={'resolution_mode':'custom','width':320,'height':180}
    denied=create_assembly(client,project,timeline([ids[0][1]],output=smaller))
    assert denied.status_code==422 and denied.json()['error']['code']=='assembly_downscale_confirmation_required'
    opted_in=create_assembly(client,project,timeline([ids[0][1]],output={**smaller,'allow_downscale':True}))
    assert opted_in.status_code==201,opted_in.text
    historical=submit(client,opted_in.json());assert historical.status_code==202,historical.text
    legacy_id,historical_render_id=opted_in.json()['id'],historical.json()['id']
    with client.app.state.session_factory() as db:
        output=db.get(m.AssemblyRender,default_render.json()['id']).snapshot_json['output']
        assert (output['width'],output['height'])==(640,360)
        saved=db.get(m.Assembly,legacy_id)
        legacy_output={k:v for k,v in saved.timeline_json['output'].items() if k not in {'resolution_mode','allow_downscale'}}
        saved.timeline_json={**saved.timeline_json,'output':legacy_output};revision=saved.revision;db.commit()
    refreshed=client.post('/api/assemblies/'+legacy_id+'/render',json={'revision':revision})
    assert refreshed.status_code==202,refreshed.text
    snapshot=refreshed.json()['snapshot_json']
    assert (snapshot['output']['width'],snapshot['output']['height'])==(640,360)
    assert snapshot['output']['resolution_mode']=='source'
    assert snapshot['clips'][0]['video_id']==ids[0][1]
    with client.app.state.session_factory() as db:
        historical_output=db.get(m.AssemblyRender,historical_render_id).snapshot_json['output']
        assert (historical_output['width'],historical_output['height'])==(320,180)
        assert db.get(m.Assembly,legacy_id).timeline_json['output']==legacy_output
