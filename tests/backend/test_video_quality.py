"""Quality contract and actual FFmpeg conformance on explicitly synthetic test media, not AI inference."""
import hashlib
import shutil
import subprocess
from pathlib import Path
import pytest
from sqlalchemy import select
from app import models as m
from app.core.db import Base,build_engine,build_session_factory
from app.core.errors import AppError
from app.schemas_quality import QualitySettings,QualityProfileInput
from app.services import video_quality as q
from app.services.studio_pipeline import create_pipeline,_overall

@pytest.fixture
def context(settings):
    engine=build_engine(settings.database_url);Base.metadata.create_all(engine);factory=build_session_factory(engine)
    with factory() as session:
        project=m.Project(name='Isolated quality conformance');session.add(project);session.flush()
        shot=m.StoryboardShot(project_id=project.id,order=1,scene_title='Fixture',aspect_ratio='16:9',duration_seconds=1)
        script=m.ScriptRevision(project_id=project.id,revision=1,raw_text='Fixture',parsed_json={});session.add_all([shot,script]);session.flush()
        job=m.GenerationJob(project_id=project.id,shot_id=shot.id,status='generated',provider='explicit_test_fixture',prompt='synthetic media, not model output',parameters_json={},reference_assets_json=[],seed=1)
        session.add(job);session.flush()
        video=m.VideoRevision(project_id=project.id,shot_id=shot.id,generation_job_id=job.id,relative_path='source.mp4',revision=1,metadata_json={})
        session.add(video);session.commit()
        yield session,factory,project,shot,video
    engine.dispose()

@pytest.mark.parametrize('resolution,width,height',[('720p',1280,720),('1080p',1920,1080),('2k',2560,1440),('4k',3840,2160)])
def test_explicit_output_sizes_never_claim_native_or_ai(resolution,width,height):
    output=q.plan(QualitySettings(resolution=resolution));assert output['requested']['width']==width and output['requested']['height']==height
    assert output['generation']['width']==1024 and output['method']=='lanczos' and output['ai_super_resolution'] is False
    assert '普通' in output['label']
    vertical=q.plan(QualitySettings(resolution=resolution,aspect_ratio='9:16'));assert (vertical['requested']['width'],vertical['requested']['height'])==(height,width)
    square=q.plan(QualitySettings(resolution=resolution,aspect_ratio='1:1'));assert square['requested']['width']==square['requested']['height']==height

def test_real_mode_parameters_and_unsupported_methods():
    configs=[q.plan(QualitySettings(mode=mode)) for mode in ['preview','standard','high']]
    assert [p['generation']['steps'] for p in configs]==[12,20,30]
    assert [p['encoding']['crf'] for p in configs]==[23,18,16]
    for method in ['native','ai_super_resolution','reconstruction']:assert not q.plan(QualitySettings(processing=method))['supported']

def test_inheritance_revision_scope_and_pre_dispatch_guard(context,settings):
    session,_,project,shot,_=context
    q.save_profile(session,'system','default',QualityProfileInput(settings=QualitySettings(resolution='1080p'),expected_revision=0))
    assert q.profile(session,'shot',shot.id)['inherited_from']=='system:default'
    q.save_profile(session,'project',project.id,QualityProfileInput(settings=QualitySettings(resolution='2k',mode='high'),expected_revision=0))
    assert q.profile(session,'shot',shot.id)['settings']['resolution']=='2k'
    with pytest.raises(AppError,match='画质设置已改变'):q.save_profile(session,'project',project.id,QualityProfileInput(settings=QualitySettings(),expected_revision=0))
    run=create_pipeline(session,settings,project.id,{})
    assert run.snapshot_json['options']['quality_by_shot'][shot.id]['resolution']=='2k'
    assert any(n['kind']=='quality' for n in run.nodes_json)
    for n in run.nodes_json:
        if n['kind']!='quality':n['status']='succeeded'
    _overall(run,run.nodes_json);assert run.status!='waiting_review'
    with pytest.raises(AppError,match='未验证'):q.save_profile(session,'shot',shot.id,QualityProfileInput(settings=QualitySettings(processing='native')))

def tools(settings):
    ffmpeg=shutil.which('ffmpeg') or 'D:/comfyui/ComfyUI/ffmpeg.exe'
    ffprobe=shutil.which('ffprobe') or 'D:/comfyui/ComfyUI/ffprobe.exe'
    if not Path(ffmpeg).is_file() or not Path(ffprobe).is_file():pytest.skip('Actual FFmpeg not installed')
    settings.ffmpeg_path=ffmpeg;settings.ffprobe_path=ffprobe

def source(context,settings):
    tools(settings);session,_,_,_,video=context
    settings.media_root.mkdir(exist_ok=True);path=settings.media_root/video.relative_path
    subprocess.run([settings.ffmpeg_path,'-v','error','-n','-f','lavfi','-i','testsrc2=size=320x180:rate=24:duration=1','-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=1','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac','-shortest',str(path)],check=True,capture_output=True,timeout=60)
    digest=hashlib.sha256(path.read_bytes()).hexdigest();video.metadata_json={'sha256':digest};session.commit();return path,digest

def test_actual_full_video_encoding_preserves_all_frames_audio_and_original(context,settings):
    session,factory,_,_,video=context;path,digest=source(context,settings)
    item=q.create_render(session,settings,video.id,QualitySettings(resolution='1080p'));session.commit();identifier=item.id
    q.run_render(settings,factory,identifier);session.expire_all();item=session.get(m.QualityRender,identifier)
    assert item.status=='generated',item.error_message
    actual=item.result_json['final_actual'];assert (actual['width'],actual['height'],actual['frames'])==(1920,1080,24)
    assert actual['fps']=='24/1' and len(actual['audio'])==1 and actual['full_decode']['exit_code']==0
    assert hashlib.sha256(path.read_bytes()).hexdigest()==digest and item.result_json['source_preserved']
    assert item.result_json['model_actual_resolution']['width']==320 and not item.result_json['ai_super_resolution']
    processed=session.get(m.VideoRevision,item.video_id);assert processed.revision==2 and processed.review_status=='pending'
    assert len(list(session.scalars(select(m.VideoRevision))))==2
    # Reprocessing always returns to the raw generated source, not a second scaled source.
    again=q.create_render(session,settings,processed.id,QualitySettings(resolution='4k'));assert again.source_video_id==video.id
    assert q.create_render(session,settings,video.id,QualitySettings(resolution='1080p')).id==identifier

def test_source_replacement_blocks_processing_without_overwriting(context,settings):
    session,factory,_,_,video=context;path,_=source(context,settings)
    item=q.create_render(session,settings,video.id,QualitySettings());session.commit();identifier=item.id
    path.write_bytes(b'explicit corruption in isolated test only')
    q.run_render(settings,factory,identifier);session.expire_all();item=session.get(m.QualityRender,identifier)
    assert item.status=='failed' and item.video_id is None and not item.result_json['source_preserved']
    assert len(list(session.scalars(select(m.VideoRevision))))==1

def test_profile_api_rejects_scope_confusion_and_unverified_super_resolution(client):
    response=client.get('/api/quality/profiles/system/default');assert response.status_code==200
    response=client.put('/api/quality/profiles/system/default',json={'settings':{'resolution':'4k','processing':'ai_super_resolution'}})
    assert response.status_code==409 and response.json()['error']['code']=='quality_method_unverified'
    assert client.get('/api/quality/profiles/project/not-real').status_code==404

def test_ai_backend_checks_real_frozen_model_path_before_any_child_process(context,settings):
    """Negative branch, no AI inference: changed model must fail before launch, not NameError."""
    session,factory,_,_,video=context;path,digest=source(context,settings)
    model=settings.media_root/'explicit_test_model.bin';model.write_bytes(b'changed test bytes')
    entry=m.QualityRender(source_video_id=video.id,status='queued',snapshot_json={
        'source_relative_path':video.relative_path,'source_sha256':digest,'project_id':video.project_id,
        'plan':q.plan(QualitySettings(resolution='4k')),'ai_super_resolution':True,
        'super_resolution':{'runtime':{'model':str(model),'script':'unused.py'},'model_sha256':'not-the-file-sha'},
        'settings':QualitySettings(resolution='4k',processing='ai_super_resolution').model_dump()},result_json={})
    session.add(entry);session.commit();identifier=entry.id
    q.run_render(settings,factory,identifier);session.expire_all();entry=session.get(m.QualityRender,identifier)
    assert entry.status=='failed' and '模型文件变化' in entry.error_message
    assert entry.result_json['failure_type']=='AppError' and entry.result_json['source_preserved']
    assert entry.video_id is None and hashlib.sha256(path.read_bytes()).hexdigest()==digest
