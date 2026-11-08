from dataclasses import replace
from pathlib import Path
import array
import hashlib
import json
import math
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from app.core.errors import AppError
from app.services.assembly import command_for, full_clip_timing, probe, validate_timeline
from app.schemas_product import TimelineRequest


def binaries():
    ffmpeg = shutil.which('ffmpeg')
    ffprobe = shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:
        bundled = Path('D:/comfyui/ComfyUI')
        if (bundled / 'ffmpeg.exe').is_file() and (bundled / 'ffprobe.exe').is_file():
            ffmpeg, ffprobe = str(bundled / 'ffmpeg.exe'), str(bundled / 'ffprobe.exe')
    if not ffmpeg or not ffprobe:
        pytest.skip('A real FFmpeg and FFprobe are needed for this integration test')
    return ffmpeg, ffprobe


def run(cmd):
    return subprocess.run(cmd, capture_output=True, timeout=120, check=True)


def test_real_two_clip_hard_cut_keeps_delayed_video_and_audio_tail(settings, tmp_path, monkeypatch):
    ffmpeg, ffprobe = binaries()
    settings = replace(settings, ffmpeg_path=ffmpeg, ffprobe_path=ffprobe, media_root=tmp_path)
    source = tmp_path / 'source.mp4'
    run([ffmpeg, '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc2=size=160x128:rate=24:duration=5.166667',
         '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=5.258344',
         '-filter_complex', '[0:v]setpts=PTS+2/24/TB[v]', '-map', '[v]', '-map', '1:a',
         '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-fps_mode', 'passthrough', '-c:a', 'aac', str(source)])
    metadata = probe(source, settings)
    timing = full_clip_timing(metadata, 24)
    # FFmpeg/MP4 edit-list versions quantize this synthetic fixture's initial
    # offset differently (<1ms). The render assertions below still compare all
    # actual source frames and the complete audio tail, without trimming.
    assert timing['video_offset'] == pytest.approx(2/24, abs=.001)
    assert timing['source_end'] > 5.25
    assert timing['duration'] == pytest.approx(127/24)
    digest=hashlib.sha256(source.read_bytes()).hexdigest()
    video=SimpleNamespace(id='v1',project_id='project',shot_id='shot',generation_job_id='job',revision=1,
                          review_status='approved',relative_path=source.name,metadata_json={'sha256':digest},thumbnail_paths_json=[])
    from app.models import GenerationJob
    job=SimpleNamespace(id='job',project_id='project',shot_id='shot',parameters_json={},status='generated')
    session=SimpleNamespace(get=lambda model, identifier: job if model is GenerationJob else video)
    monkeypatch.setattr('app.services.assembly.resolve_service', lambda *args: (SimpleNamespace(provider='ffmpeg'), {'settings':{}}))
    request=TimelineRequest.model_validate({'clips':[{'video_id':'v1'},{'video_id':'v1'}],
        'transitions':[{'type':'cut','duration':0,'audio':'cut'}],
        'output':{'width':160,'height':128,'fps':24,'audio_policy':'preserve'}})
    snapshot=validate_timeline(session,settings,'project',request)
    assert snapshot['clips'][0]['full_stream_timing']['preserves_full_streams'] is True
    assert snapshot['clips'][0]['duration'] == pytest.approx(127/24)
    result = tmp_path / 'result.mp4'
    run(command_for(settings,snapshot,result))
    def info(path):
        return json.loads(run([ffprobe,'-v','error','-count_frames','-show_streams','-of','json',str(path)]).stdout)['streams']
    source_info, output_info = info(source), info(result)
    assert next(s for s in source_info if s['codec_type']=='video')['nb_read_frames'] == '124'
    assert next(s for s in output_info if s['codec_type']=='video')['nb_read_frames'] == '254'
    assert float(next(s for s in output_info if s['codec_type']=='audio')['duration']) >= 2*5.258344
    def gray(path):
        return run([ffmpeg,'-v','error','-i',str(path),'-map','0:v','-vf','format=gray',
                    '-fps_mode','passthrough','-f','rawvideo','-']).stdout
    original, rendered = gray(source), gray(result)
    frame_size = 160*128
    assert len(original)==124*frame_size and len(rendered)==254*frame_size
    for offset in (2,129):
        for i in range(124):
            expected = original[i*frame_size:(i+1)*frame_size]
            actual = rendered[(offset+i)*frame_size:(offset+i+1)*frame_size]
            assert sum(abs(a-b) for a,b in zip(expected,actual))/frame_size < 3
    for start in (5.20, 127/24+5.20):
        audio = run([ffmpeg,'-v','error','-ss',str(start),'-t','0.05','-i',str(result),
                     '-vn','-f','f32le','-ac','1','-ar','48000','-']).stdout
        samples = array.array('f',audio)
        assert samples and math.sqrt(sum(x*x for x in samples)/len(samples)) > 0.02


def test_full_clip_rejects_missing_timing_and_lower_output_fps():
    meta={'fps':24,'streams':[{'codec_type':'video','start_time':'0','duration':'5'}]}
    with pytest.raises(AppError) as error:
        full_clip_timing(meta, 12)
    assert error.value.code == 'full_clip_fps_too_low'
    meta['streams'][0]['duration'] = 'N/A'
    with pytest.raises(AppError) as error:
        full_clip_timing(meta, 24)
    assert error.value.code == 'full_clip_timing_unavailable'


def test_api_create_preview_resave_render_keeps_full_media(client, settings):
    from sqlalchemy import select
    from app import models as m
    from app.services.assembly import run_render

    ffmpeg, ffprobe = binaries()
    settings.ffmpeg_path, settings.ffprobe_path = ffmpeg, ffprobe
    settings.media_root.mkdir(parents=True,exist_ok=True)
    source = settings.media_root / 'source.mp4'
    run([ffmpeg,'-v','error','-y','-f','lavfi','-i','testsrc2=size=160x128:rate=24:duration=5.166667',
         '-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=5.258344',
         '-filter_complex','[0:v]setpts=PTS+2/24/TB[v]','-map','[v]','-map','1:a',
         '-c:v','libx264','-pix_fmt','yuv420p','-fps_mode','passthrough','-c:a','aac',str(source)])
    with client.app.state.session_factory() as session:
        service=session.scalar(select(m.AIService).where(m.AIService.provider=='ffmpeg'))
        service.settings_json={**service.settings_json,'ffmpeg_path':ffmpeg,'ffprobe_path':ffprobe}
        project=m.Project(name='isolated full media');session.add(project);session.flush()
        shot=m.StoryboardShot(project_id=project.id,order=1,scene_title='fixture',duration_seconds=5);session.add(shot);session.flush()
        job=m.GenerationJob(project_id=project.id,shot_id=shot.id,provider='isolated_fixture',status='generated',prompt='test');session.add(job);session.flush()
        video=m.VideoRevision(project_id=project.id,shot_id=shot.id,generation_job_id=job.id,revision=1,
            relative_path=source.name,review_status='approved',metadata_json={'sha256':hashlib.sha256(source.read_bytes()).hexdigest()})
        session.add(video);session.flush();project_id=project.id;video_id=video.id;session.commit()
    output={'width':160,'height':128,'fps':24,'codec':'libx264','audio_policy':'preserve'}
    timeline={'clips':[{'video_id':video_id},{'video_id':video_id}],
              'transitions':[{'type':'cut','duration':0}], 'output':output}
    create=client.post('/api/assemblies',json={'project_id':project_id,'name':'full media','timeline':timeline})
    assert create.status_code==201,create.text
    row=create.json();identifier=row['id'];assert row['timeline_json']['clips'][0]['trim_end'] is None
    assert row['timeline_json']['clips'][0]['effective_trim_end']==pytest.approx(127/24)
    preview=client.post('/api/assemblies/'+identifier+'/preview',json={'revision':row['revision'],'transition_index':0})
    assert preview.status_code==202,preview.text
    assert preview.json()['snapshot_json']['clips'][0]['full_stream_timing'] is not None
    run_render(settings,client.app.state.session_factory,preview.json()['id'])
    with client.app.state.session_factory() as session:
        rendered=session.get(m.AssemblyRender,preview.json()['id'])
        assert rendered.status=='generated',rendered.error_message
    one={'clips':[{'video_id':video_id}], 'transitions':[], 'output':output}
    patch=client.patch('/api/assemblies/'+identifier,json={'name':'resaved full media','revision':row['revision'],'timeline':one})
    assert patch.status_code==200,patch.text
    row=patch.json();assert row['timeline_json']['clips'][0]['trim_end'] is None
    # Snapshots produced by the previous version stored a derived numeric end;
    # the full-stream marker must recover their original no-trim intent.
    with client.app.state.session_factory() as session:
        assembly=session.get(m.Assembly,identifier);timeline=dict(assembly.timeline_json)
        timeline['clips']=[{**timeline['clips'][0],'trim_end':timeline['clips'][0]['effective_trim_end']}]
        assembly.timeline_json=timeline;session.commit()
    submitted=client.post('/api/assemblies/'+identifier+'/render',json={'revision':row['revision']})
    assert submitted.status_code==202,submitted.text
    frozen=submitted.json()['snapshot_json'];assert frozen['clips'][0]['trim_end'] is None
    assert frozen['clips'][0]['full_stream_timing']['preserves_full_streams']
    run_render(settings,client.app.state.session_factory,submitted.json()['id'])
    with client.app.state.session_factory() as session:
        rendered=session.get(m.AssemblyRender,submitted.json()['id'])
        assert rendered.status=='generated',rendered.error_message
        destination=settings.media_root/rendered.relative_path
    streams=json.loads(run([ffprobe,'-v','error','-count_frames','-show_streams','-of','json',str(destination)]).stdout)['streams']
    assert next(s for s in streams if s['codec_type']=='video')['nb_read_frames']=='127'
    assert float(next(s for s in streams if s['codec_type']=='audio')['duration'])>=5.258344
    tail=run([ffmpeg,'-v','error','-ss','5.20','-t','0.05','-i',str(destination),'-vn','-f','f32le','-ac','1','-ar','48000','-']).stdout
    samples=array.array('f',tail)
    assert samples and math.sqrt(sum(x*x for x in samples)/len(samples))>0.02
