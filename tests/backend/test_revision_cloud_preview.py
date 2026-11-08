"""Preview/submit contracts with real app compilation and transport stubs only."""
import hashlib

import httpx
import pytest
from sqlalchemy import select

from app import models as m
from app.services.ai_services import service_snapshot
from app.services import video_services
from test_product_library import create_product, upload, png, bind_product


@pytest.mark.parametrize('selection', ['original', 'explicit'])
def test_cloud_product_name_scale_preview_equals_actual_submit_body(client,settings,monkeypatch,selection):
    product=create_product(client,'圣诞树玩具')
    upload(client,product['id'],[png()])
    product=client.get('/api/product-library/'+product['id']).json()
    project,shot_id,_,_=bind_product(client,settings,product)
    with client.app.state.session_factory() as s:
        shot=s.get(m.StoryboardShot,shot_id)
        shot.status='approved';shot.duration_seconds=4;shot.aspect_ratio='16:9'
        shot.visual_prompt='Realistic green toy on a desk.';shot.action='The product turns slowly.';shot.camera_move='固定机位'
        script=m.ScriptRevision(project_id=project,revision=1,status='approved',raw_text='Synthetic product contract',
            parsed_json={'explicit_shot_ids':[shot_id]})
        cloud=m.AIService(module='video_generation',provider='video_api',name='ISOLATED CLOUD CONTRACT',
            base_url='https://example.invalid/v1',default_model='fixture-video',
            settings_json={'video_contract':'video-jobs-v1','width':1024,'height':576})
        s.add_all([script,cloud]);s.flush()
        snapshot=service_snapshot(cloud)
        old=m.GenerationJob(project_id=project,shot_id=shot_id,provider='cloud_video_jobs_v1',status='generated',
            prompt='Synthetic source without inference',parameters_json={'service_snapshot':snapshot,'width':1024,'height':576,'steps':20,'duration_seconds':4})
        s.add(old);s.flush()
        path=settings.media_root/'source-contract.mp4';path.write_bytes(b'LABELLED CONTRACT SOURCE, NOT VIDEO INFERENCE')
        video=m.VideoRevision(project_id=project,shot_id=shot_id,generation_job_id=old.id,revision=1,
            relative_path=path.name,metadata_json={'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
        s.add(video);s.commit();video_id=video.id;service_id=cloud.id
    # Start-up stayed in isolated demo mode, so no worker/model can run. Only
    # these two synchronous requests exercise the actual real adapter path.
    settings.h3_demo_mode=False
    calls=[]
    async def transport(config,snapshot,method,path,secret='',**kwargs):
        calls.append({'method':method,'path':path,'body':kwargs.get('json')})
        assert method=='POST' and path=='/video/jobs'
        return httpx.Response(200,json={'id':'isolated-provider-task','status':'queued'})
    monkeypatch.setattr(video_services,'service_request',transport)
    payload={'instruction':'圣诞树玩具只有人物甲的两个手掌大小，其他画面保持不变。'}
    if selection=='explicit':payload.update(service_id=service_id,model_id='fixture-video')
    preview=client.post(f'/api/videos/{video_id}/revision-preview',json=payload)
    assert preview.status_code==200,preview.text
    assert preview.json()['preview_stage']=='provider_request'
    assert preview.json()['provider']=='video_api'
    assert '圣诞树玩具 is the size of two human palms' in preview.json()['prompt']
    assert '__unresolved_product__' not in preview.json()['prompt']
    assert calls==[]
    with client.app.state.session_factory() as s:
        s.add(m.ProjectServiceBinding(project_id=project,module='video_generation',service_id=service_id));s.commit()
    local_only={'instruction':payload['instruction'],'use_project_video_service':True}
    blocked_preview=client.post(f'/api/videos/{video_id}/revision-preview',json=local_only)
    blocked_submit=client.post(f'/api/videos/{video_id}/revise',json=local_only)
    assert blocked_preview.status_code==blocked_submit.status_code==409
    assert blocked_preview.json()['error']['code']==blocked_submit.json()['error']['code']=='context_ir_service_required'
    assert calls==[]  # a local-only checkbox must never authorize paid cloud generation
    result=client.post(f'/api/videos/{video_id}/revise',json=payload)
    assert result.status_code==202,result.text
    assert len(calls)==1 and calls[0]['body']['prompt']==preview.json()['prompt']
    assert calls[0]['body']['model']=='fixture-video'
    assert len(calls[0]['body']['reference_images'])==1
    with client.app.state.session_factory() as s:
        job=s.get(m.GenerationJob,result.json()['job']['id'])
        assert job.provider=='cloud_video_jobs_v1'
        assert 'h3_workflow' not in job.parameters_json and 'context_inputs' not in job.parameters_json
        assert len(list(s.scalars(select(m.GenerationJob).where(m.GenerationJob.shot_id==shot_id))))==2
