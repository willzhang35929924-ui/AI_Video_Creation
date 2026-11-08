"""Real CPU-only FFmpeg regression for the retained FinalPage export endpoint."""
import asyncio
import hashlib
import subprocess

from sqlalchemy import select

from app import models as m
from app.core.config import Settings
from app.services import composition
from app.services.assembly import probe


def test_legacy_final_export_keeps_audio_frames_and_source_resolution(client,settings,monkeypatch,tmp_path):
    tools=Settings.from_env();settings.ffmpeg_path=tools.ffmpeg_path;settings.ffprobe_path=tools.ffprobe_path
    monkeypatch.setattr(composition,'PROJECT_ROOT',tmp_path)
    settings.media_root.mkdir(parents=True,exist_ok=True)
    originals=[]
    for index,dimensions in enumerate(['320x180','160x128']):
        path=settings.media_root/f'labelled-{index}.mp4'
        command=[settings.ffmpeg_path,'-nostdin','-v','error','-n','-f','lavfi','-i',f'color=c=gray:s={dimensions}:r=30:d=1']
        if index==0:command+=['-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=1']
        command+=['-t','1','-c:v','libx264','-pix_fmt','yuv420p']
        if index==0:command+=['-c:a','aac']
        command+=[str(path)]
        r=subprocess.run(command,capture_output=True,timeout=30,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        assert r.returncode==0,r.stderr
        originals.append((path,hashlib.sha256(path.read_bytes()).hexdigest()))
    with client.app.state.session_factory() as s:
        service=s.scalar(select(m.AIService).where(m.AIService.module=='final_assembly'))
        service.settings_json={**service.settings_json,'ffmpeg_path':settings.ffmpeg_path,'ffprobe_path':settings.ffprobe_path}
        project=m.Project(name='CPU stream-preserving legacy export contract');s.add(project);s.flush()
        ids=[]
        for index,(path,digest) in enumerate(originals):
            shot=m.StoryboardShot(project_id=project.id,order=index+1,status='approved')
            s.add(shot);s.flush()
            job=m.GenerationJob(project_id=project.id,shot_id=shot.id,provider='synthetic_fixture',status='generated',prompt='No inference')
            s.add(job);s.flush()
            video=m.VideoRevision(project_id=project.id,shot_id=shot.id,generation_job_id=job.id,revision=1,
                relative_path=path.name,review_status='approved',metadata_json={'sha256':digest})
            s.add(video);s.flush();ids.append(video.id)
        s.commit();project_id=project.id
    response=client.post(f'/api/projects/{project_id}/compositions',json={'video_ids':ids})
    assert response.status_code==202,response.text
    identifier=response.json()['id']
    asyncio.run(composition.process_composition(settings,client.app.state.session_factory))
    with client.app.state.session_factory() as s:
        item=s.get(m.FinalComposition,identifier)
        assert item.status=='generated',item.error_message
        actual=probe(settings.media_root/item.relative_path,settings)
        assert (actual['width'],actual['height'],actual['fps'])==(320,180,30)
        assert actual['frames']==60 and abs(actual['duration']-2)<.04
        assert actual['has_audio'] is True and item.metadata_json['has_audio'] is True
        assert item.metadata_json['checks'] and any(c['check_type']=='media_decode' and c['status']=='passed' for c in item.metadata_json['checks'])
        assert s.scalar(select(m.AssemblyRender.id)) is None
        assert len(list(s.scalars(select(m.GenerationJob))))==2  # No H3/cloud re-generation
        destination=settings.media_root/item.relative_path
    audio=subprocess.run([settings.ffmpeg_path,'-nostdin','-hide_banner','-i',str(destination),'-vn','-af','volumedetect','-f','null','-'],
        capture_output=True,timeout=30,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    assert audio.returncode==0 and b'mean_volume: -inf' not in audio.stderr
    assert b'mean_volume:' in audio.stderr
    assert all(hashlib.sha256(path.read_bytes()).hexdigest()==digest for path,digest in originals)
