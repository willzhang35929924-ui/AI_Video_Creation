"""Isolated contract regressions. These are NOT real cloud generation evidence."""
import asyncio
import base64
import io
from types import SimpleNamespace
import httpx
import pytest
from PIL import Image
from sqlalchemy import select
from app import models as m
from app.core.errors import AppError
from app.schemas_cinematic import VisualInput,RunInput
from app.services.cinematic_visuals import build_visual,cloud_visual,visual_tick
from app.services.cinematic_quality import inspect_generated
from app.services.production_orchestrator import create_run,advance_run
from test_cinematic import approved_cast,import_case


def cloud_service(session,module='character_generation'):
    service=m.AIService(module=module,provider='openai_compatible' if module=='character_generation' else 'video_api',name='ISOLATED CONTRACT FIXTURE',base_url='https://example.invalid/v1',default_model='fixture-model',settings_json={'width':1024,'height':1024,'image_api_format':'multipart','video_contract':'video-jobs-v1'})
    session.add(service);session.flush();return service


def test_cloud_visual_requires_explicit_single_validation_and_real_decode(client,settings,monkeypatch,tmp_path):
    approved_cast(client,settings);result=import_case(client);settings.h3_demo_mode=False
    with client.app.state.session_factory() as s:
        service=cloud_service(s);scene=s.scalar(select(m.Scene).where(m.Scene.project_id==result['project_id']))
        payload=VisualInput(kind='scene_reference',scene_id=scene.id,service_id=service.id)
        with pytest.raises(AppError) as exc:build_visual(s,settings,result['project_id'],payload)
        assert exc.value.code=='cloud_visual_single_validation_required'
        payload.confirm_cloud_test=True
        visual=build_visual(s,settings,result['project_id'],payload);s.commit()
        assert visual.snapshot_json['workflow_path']=='compatible-images-v1'
        assert service.capabilities_json=={}
        with pytest.raises(AppError) as exc:build_visual(s,settings,result['project_id'],payload)
        assert exc.value.code=='cloud_visual_probe_active'
        buffer=io.BytesIO();Image.new('RGB',(64,64),'gray').save(buffer,format='PNG')
        calls=[]
        async def transport(settings,snapshot,method,endpoint,secret='',**kwargs):
            calls.append((method,endpoint,kwargs));return httpx.Response(200,json={'data':[{'b64_json':base64.b64encode(buffer.getvalue()).decode()}]},request=httpx.Request(method,'https://example.invalid'))
        monkeypatch.setattr('app.services.cinematic_visuals.service_request',transport)
        asyncio.run(cloud_visual(settings,s,visual,'',tmp_path));s.commit()
        assert calls[0][1]=='/images/generations' and calls[0][2]['json']['n']==1
        assert visual.status=='needs_review' and visual.provider_job_id is None
        assert visual.snapshot_json['actual_dimensions']==[64,64]
        assert service.capabilities_json['text_to_image']['state']=='verified'
        # Text-only evidence never verifies the three-reference keyframe edit.
        with pytest.raises(AppError) as exc:build_visual(s,settings,result['project_id'],VisualInput(kind='keyframe',shot_id=result['shot_ids'][0],service_id=service.id))
        assert exc.value.code=='cloud_visual_single_validation_required'
        second=build_visual(s,settings,result['project_id'],VisualInput(kind='scene_reference',scene_id=scene.id,service_id=service.id))
        assert second.status=='queued'


def test_cloud_reference_edits_send_separate_images_and_verify_actual_count(client,settings,monkeypatch,tmp_path):
    approved_cast(client,settings);result=import_case(client);settings.h3_demo_mode=False
    with client.app.state.session_factory() as s:
        service=cloud_service(s)
        visual=build_visual(s,settings,result['project_id'],VisualInput(kind='keyframe',shot_id=result['shot_ids'][0],service_id=service.id,confirm_cloud_test=True))
        assert len(visual.snapshot_json['references'])==2
        buffer=io.BytesIO();Image.new('RGB',(64,64),'gray').save(buffer,format='PNG');calls=[]
        async def transport(settings,snapshot,method,endpoint,secret='',**kwargs):
            calls.append((endpoint,kwargs));return httpx.Response(200,json={'data':[{'b64_json':base64.b64encode(buffer.getvalue()).decode()}]},request=httpx.Request(method,'https://example.invalid'))
        monkeypatch.setattr('app.services.cinematic_visuals.service_request',transport)
        asyncio.run(cloud_visual(settings,s,visual,'',tmp_path))
        assert calls[0][0]=='/images/edits' and [k for k,v in calls[0][1]['files']]==['image[]','image[]']
        assert service.capabilities_json['reference_image']['max_reference_images']==2
        assert visual.status=='needs_review'
        service.capabilities_json={'reference_image':{'state':'verified','model_id':'different-model','max_reference_images':9}}
        with pytest.raises(AppError):build_visual(s,settings,result['project_id'],VisualInput(kind='keyframe',shot_id=result['shot_ids'][0],service_id=service.id))


def test_cloud_interrupted_sync_request_becomes_unknown_without_resubmission(client,settings,monkeypatch):
    approved_cast(client,settings);result=import_case(client);settings.h3_demo_mode=False
    with client.app.state.session_factory() as s:
        service=cloud_service(s);scene=s.scalar(select(m.Scene).where(m.Scene.project_id==result['project_id']))
        visual=build_visual(s,settings,result['project_id'],VisualInput(kind='scene_reference',scene_id=scene.id,service_id=service.id,confirm_cloud_test=True));visual.status='submitting';s.commit();identifier=visual.id
    async def forbidden(*args,**kwargs):raise AssertionError('must not repeat a paid request')
    monkeypatch.setattr('app.services.cinematic_visuals.service_request',forbidden)
    asyncio.run(visual_tick(settings,client.app.state.session_factory))
    asyncio.run(visual_tick(settings,client.app.state.session_factory))
    with client.app.state.session_factory() as s:assert s.get(m.ProductionVisual,identifier).status=='unknown'


@pytest.mark.parametrize('failure_endpoint,expected_status',[
    ('/queue','failed'),('/prompt','unknown'),
])
def test_comfyui_failure_records_whether_generation_submission_could_have_reached_provider(
    client,settings,monkeypatch,tmp_path,failure_endpoint,expected_status
):
    approved_cast(client,settings);result=import_case(client);settings.h3_demo_mode=False
    with client.app.state.session_factory() as s:
        service=m.AIService(module='character_generation',provider='comfyui',name='LOCAL COMFYUI FIXTURE',
            base_url='http://127.0.0.1:8189',default_model='flux-2-klein-4b-fp8.safetensors',
            settings_json={'candidate_model':'flux-2-klein-4b-fp8.safetensors','text_encoder':'qwen.safetensors','vae':'vae.safetensors'})
        s.add(service);s.flush()
        scene=s.scalar(select(m.Scene).where(m.Scene.project_id==result['project_id']))
        visual=build_visual(s,settings,result['project_id'],VisualInput(kind='scene_reference',scene_id=scene.id,service_id=service.id))
        identifier=visual.id;s.commit()

    async def transport(_settings,_snapshot,method,endpoint,_secret='',**_kwargs):
        if endpoint==failure_endpoint:
            raise httpx.ConnectError('ComfyUI is offline')
        return httpx.Response(200,json={'queue_running':[],'queue_pending':[]},request=httpx.Request(method,'http://127.0.0.1:8189'+endpoint))

    monkeypatch.setattr('app.services.cinematic_visuals.PROJECT_ROOT',tmp_path)
    monkeypatch.setattr('app.services.cinematic_visuals.service_request',transport)
    asyncio.run(visual_tick(settings,client.app.state.session_factory,visual_id=identifier))
    with client.app.state.session_factory() as s:
        visual=s.get(m.ProductionVisual,identifier)
        assert visual.status==expected_status
        assert visual.provider_job_id is None
        if failure_endpoint=='/queue':
            assert '未创建服务任务' in visual.error_message
        else:
            assert '先核对原服务队列' in visual.error_message


def test_cloud_run_freezes_services_and_pauses_when_configuration_changes(client,settings):
    approved_cast(client,settings);result=import_case(client);settings.h3_demo_mode=False
    with client.app.state.session_factory() as s:
        image=cloud_service(s);video=cloud_service(s,'video_generation')
        run=create_run(s,settings,result['project_id'],RunInput(shot_ids=result['shot_ids'],confirmed=True,visual_service_id=image.id,video_service_id=video.id,width=1280,height=720));s.commit()
        assert run.snapshot_json['options']['visual_height']==1024
        assert run.snapshot_json['selected_service_snapshots']['video']['model_id']=='fixture-model'
        video.revision+=1;s.commit();asyncio.run(advance_run(client.app,settings,s,run))
        assert run.status=='paused' and not list(s.scalars(select(m.ProductionVisual)))


def test_cloud_video_spec_does_not_apply_local_h3_frame_quantization():
    job=SimpleNamespace(parameters_json={'screenplay_snapshot':{'cinematic':{'versions':{}}},'service_snapshot':{'provider':'video_api'},'width':1280,'height':720,'duration_seconds':5})
    checks=[{'check_type':'media_decode','status':'passed','details':{'streams':[{'codec_type':'video','width':1280,'height':720,'avg_frame_rate':'24/1','nb_frames':'120'}],'format':{'duration':'5.0'}}}]
    assert inspect_generated(job,checks)[0]['status']=='passed'
    checks[0]['details']['streams'][0]['nb_frames']='124'
    result=inspect_generated(job,checks)[0]
    # Retain complete cloud originals for review, as required by the current
    # output policy. A small overrun must not imply local H3 quantization.
    assert result['status']=='warning'
    assert result['details']['actual']['frames']==124
    assert result['details']['expected']['frames']==120
    assert result['details']['duration_policy'].startswith('complete original;')
    checks[0]['details']['streams'][0]['nb_frames']='116'
    assert inspect_generated(job,checks)[0]['status']=='failed'
