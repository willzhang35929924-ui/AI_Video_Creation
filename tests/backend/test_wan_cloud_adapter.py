"""Isolated protocol and safety regressions; no paid cloud generation is run."""
import asyncio
import base64
import hashlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
from PIL import Image
import pytest

from app.core.errors import AppError
from app.services import video_services as service
from app.services.providers import VideoGenerationRequest


@pytest.fixture
def inputs(tmp_path, settings):
    frame=tmp_path/'frame.png'; Image.new('RGB',(1280,720),'navy').save(frame)
    ref={'absolute_path':str(frame),'sha256':hashlib.sha256(frame.read_bytes()).hexdigest(),'asset_id':'isolated_frame'}
    request=VideoGenerationRequest(job_id='test-job',project_id='test-project',shot_id='test-shot',
        prompt='A person walks slowly.',negative_prompt='animation',duration_seconds=5,aspect_ratio='16:9',seed=123,
        parameters={'width':1280,'height':720},reference_assets=[ref],output_dir=tmp_path)
    snapshot={'provider':'video_api','base_url':'https://example.invalid/api/v1','model_id':'wan2.6-i2v-flash',
              'settings':{'video_contract':'wan26-i2v'}}
    return service.Wan26ImageToVideo(settings,snapshot,'local-test-secret'),request


def test_wan_submits_correct_endpoint_header_and_exact_single_frame(inputs,monkeypatch):
    provider,request=inputs; calls=[]
    async def transport(settings,snapshot,method,path,secret='',**kwargs):
        calls.append((method,path,secret,kwargs))
        return httpx.Response(200,json={'output':{'task_id':'task-1','task_status':'PENDING'}})
    monkeypatch.setattr(service,'service_request',transport)
    accepted=asyncio.run(provider.submit(request))
    assert accepted.provider_job_id=='task-1' and accepted.status=='queued'
    method,path,secret,kw=calls[0]
    assert method=='POST' and path=='/services/aigc/video-generation/video-synthesis'
    assert kw['extra_headers']=={'X-DashScope-Async':'enable'} and secret=='local-test-secret'
    assert kw['client_timeout']==120.0
    body=kw['json']; assert body['model']=='wan2.6-i2v-flash' and body['parameters']['resolution']=='720P'
    assert body['parameters']['duration']==5 and body['parameters']['prompt_extend'] is False
    assert base64.b64decode(body['input']['img_url'].split(',',1)[1])==Path(request.reference_assets[0]['absolute_path']).read_bytes()


@pytest.mark.parametrize('mutation,code',[
    ('two_frames','wan_single_frame_required'),('prompt','wan_prompt_length'),('duration','wan_duration_invalid'),
    ('dimensions','wan_resolution_invalid'),('model','wan_model_unsupported'),('base','wan_base_url_invalid'),
    ('hash','reference_hash_changed'),('seed','wan_seed_invalid')])
def test_wan_rejects_unsupported_inputs_before_post(inputs,monkeypatch,mutation,code):
    provider,request=inputs
    async def forbidden(*args,**kwargs):raise AssertionError('unsafe input must not be sent')
    monkeypatch.setattr(service,'service_request',forbidden)
    if mutation=='two_frames':request.reference_assets.append(request.reference_assets[0])
    elif mutation=='prompt':request.prompt='x'*1501
    elif mutation=='duration':request.duration_seconds=16
    elif mutation=='dimensions':request.parameters['width']=640
    elif mutation=='model':provider.snapshot['model_id']='qwen3.8-max'
    elif mutation=='base':provider.snapshot['base_url']='https://example.invalid/compatible-mode/v1'
    elif mutation=='hash':request.reference_assets[0]['sha256']='bad'
    elif mutation=='seed':request.seed=2147483648
    with pytest.raises(AppError) as exc:asyncio.run(provider.submit(request))
    assert exc.value.code==code


@pytest.mark.parametrize('status,expected',[(500,'cloud_submission_unknown'),(408,'cloud_submission_unknown'),(401,'cloud_video_submit_failed')])
def test_wan_uncertain_submission_is_never_retried(inputs,monkeypatch,status,expected):
    provider,request=inputs; calls=[]
    async def transport(*args,**kwargs):calls.append(1);return httpx.Response(status,json={})
    monkeypatch.setattr(service,'service_request',transport)
    with pytest.raises(AppError) as exc:asyncio.run(provider.submit(request))
    assert exc.value.code==expected and len(calls)==1


@pytest.mark.parametrize('state,expected',[('PENDING','queued'),('RUNNING','running'),('SUCCEEDED','generated'),('FAILED','failed'),('CANCELED','cancelled'),('UNKNOWN','unknown')])
def test_wan_task_states_and_signed_url_never_enter_raw(inputs,monkeypatch,state,expected):
    provider,_=inputs; signed='https://example.invalid/result.mp4?signature=private'
    async def transport(settings,snapshot,method,path,secret='',**kwargs):
        assert method=='GET' and path=='/tasks/task-1'
        assert kwargs['client_timeout']==30.0
        return httpx.Response(200,json={'output':{'task_id':'task-1','task_status':state,'video_url':signed}})
    monkeypatch.setattr(service,'service_request',transport)
    result=asyncio.run(provider.get_job('task-1'))
    assert result['status']==expected and 'signature' not in str(result['raw'])
    if state=='SUCCEEDED':assert result['output_url']==signed


def test_wan_submission_timeout_stays_unknown_without_retry(inputs,monkeypatch):
    provider,request=inputs; calls=[]
    async def transport(*args,**kwargs):
        calls.append(kwargs['client_timeout'])
        raise httpx.ReadTimeout('test timeout')
    monkeypatch.setattr(service,'service_request',transport)
    with pytest.raises(AppError) as exc:asyncio.run(provider.submit(request))
    assert exc.value.code=='cloud_submission_unknown' and calls==[120.0]


def test_signed_video_download_preserves_query_without_provider_key(inputs,monkeypatch):
    provider,_=inputs; signed='https://example.invalid/result.mp4?signature=private'
    async def transport(settings,url):
        assert url==signed
        return b'video'
    monkeypatch.setattr(service,'download_public_video',transport)
    assert asyncio.run(provider.download(signed))==b'video'


def test_comfy_origin_allowlist_never_bypasses_url_validation(settings,monkeypatch):
    monkeypatch.setenv('TRUSTED_LOCAL_SERVICE_ORIGINS','http://host.docker.internal:8189')
    assert service.local_settings(settings,{'base_url':'http://host.docker.internal:8189','settings':{}}).comfyui_base_url.endswith(':8189')
    for url in ['http://host.docker.internal:8190','http://localhost:8189?','file://localhost:8189','http://user@localhost:8189']:
        with pytest.raises(AppError):service.local_settings(settings,{'base_url':url,'settings':{}})
