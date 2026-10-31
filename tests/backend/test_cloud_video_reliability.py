"""Local contract/persistence tests; never send a billable provider request."""
import asyncio
import base64
import hashlib
from io import BytesIO
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image
from sqlalchemy import select

from app import models as m
from app.core.errors import AppError
from app.services import jobs, video_services
from app.services.providers import VideoGenerationRequest
from test_cloud_submission_guard import task


@pytest.fixture
def cloud_request(tmp_path):
    def make(format='PNG'):
        raw=BytesIO();Image.new('RGB',(32,32),(50,100,150)).save(raw,format=format)
        data=raw.getvalue();path=tmp_path/(format+'.bin');path.write_bytes(data)
        ref={'absolute_path':str(path),'sha256':hashlib.sha256(data).hexdigest(),'asset_id':'reference'}
        req=VideoGenerationRequest(job_id='isolated',project_id='project',shot_id='shot',prompt='test',negative_prompt='',
            duration_seconds=5,aspect_ratio='16:9',seed=42,parameters={'width':640,'height':360},
            reference_assets=[ref],output_dir=tmp_path)
        return req,data
    return make


@pytest.mark.parametrize(('format','mime'), [('PNG','image/png'),('JPEG','image/jpeg'),('WEBP','image/webp')])
def test_reference_content_is_decoded_and_real_mime_is_sent(cloud_request,monkeypatch,format,mime):
    request,data=cloud_request(format);captured=[]
    async def transport(settings,snapshot,method,path,secret='',**kwargs):
        captured.append(kwargs['json']['reference_images'][0]['image_url'])
        return httpx.Response(200,json={'id':'provider-task'})
    monkeypatch.setattr(video_services,'service_request',transport)
    provider=video_services.CloudVideoJobs(SimpleNamespace(),{'model_id':'model'},'secret')
    result=asyncio.run(provider.submit(request))
    assert result.provider_job_id=='provider-task'
    assert captured[0].startswith('data:'+mime+';base64,')
    assert base64.b64decode(captured[0].split(',',1)[1])==data


def test_invalid_image_is_rejected_without_contacting_provider(cloud_request,monkeypatch):
    request,_=cloud_request();path=request.reference_assets[0]['absolute_path']
    from pathlib import Path
    Path(path).write_bytes(b'invalid-image')
    request.reference_assets[0]['sha256']=hashlib.sha256(b'invalid-image').hexdigest()
    async def forbidden(*args,**kwargs):raise AssertionError('invalid input must not be sent')
    monkeypatch.setattr(video_services,'service_request',forbidden)
    with pytest.raises(AppError) as error:
        asyncio.run(video_services.CloudVideoJobs(SimpleNamespace(),{'model_id':'model'},'secret').submit(request))
    assert error.value.code=='reference_invalid'


@pytest.mark.parametrize(('status','expected'),[(408,'cloud_submission_unknown'),(500,'cloud_submission_unknown'),
    (503,'cloud_submission_unknown'),(401,'cloud_video_submit_failed'),(429,'cloud_video_submit_failed')])
def test_response_status_preserves_uncertain_submission(cloud_request,monkeypatch,status,expected):
    request,_=cloud_request();calls=[]
    async def transport(*args,**kwargs):calls.append(1);return httpx.Response(status,json={})
    monkeypatch.setattr(video_services,'service_request',transport)
    with pytest.raises(AppError) as error:
        asyncio.run(video_services.CloudVideoJobs(SimpleNamespace(),{'model_id':'model'},'secret').submit(request))
    assert error.value.code==expected and len(calls)==1


def test_transport_timeout_is_unknown(cloud_request,monkeypatch):
    request,_=cloud_request()
    async def timeout(*args,**kwargs):raise httpx.ReadTimeout('timeout')
    monkeypatch.setattr(video_services,'service_request',timeout)
    with pytest.raises(AppError) as error:
        asyncio.run(video_services.CloudVideoJobs(SimpleNamespace(),{'model_id':'model'},'secret').submit(request))
    assert error.value.code=='cloud_submission_unknown'


@pytest.mark.parametrize('provider_name',['cloud_video_jobs_v1','cloud_lk888_h3_v2'])
def test_custom_cloud_invalid_output_is_preserved_and_cannot_be_approved(client,settings,task,monkeypatch,tmp_path,provider_name):
    from app.services import cinematic_quality,audio_mix
    raw=b'isolated provider bytes'*20
    class Provider:
        async def get_job(self,identifier):return {'status':'generated','output_url':'https://invalid.example/x','raw':{'status':'succeeded'}}
        async def download(self,url):return raw
    monkeypatch.setattr(video_services,'existing_provider',lambda *a:Provider())
    monkeypatch.setattr(jobs,'PROJECT_ROOT',tmp_path)
    def mix(path,events,settings):path.write_bytes(b'mixed');return {'performed':True}
    monkeypatch.setattr(audio_mix,'mix_audio_events',mix)
    monkeypatch.setattr(jobs,'inspect_media',lambda *a:[{'check_type':'media_decode','status':'passed','details':{}}])
    monkeypatch.setattr(cinematic_quality,'inspect_generated',lambda *a:[{'check_type':'cinematic_output_spec','status':'failed','details':{'reason':'size'}}])
    with client.app.state.session_factory() as s:
        job=s.get(m.GenerationJob,task.job_id);job.provider=provider_name;job.status='queued';job.provider_job_id='provider-task'
        job.parameters_json={'output_relative_dir':'projects/test/output','service_snapshot':{'provider':'video_api','is_cloud':True},
                             'screenplay_snapshot':{'resolved_audio_events':[{'event':'test'}]}}
        s.commit()
    asyncio.run(jobs.poll_job(settings,client.app.state.session_factory,task.job_id))
    with client.app.state.session_factory() as s:
        job=s.get(m.GenerationJob,task.job_id)
        video=s.scalar(select(m.VideoRevision).where(m.VideoRevision.generation_job_id==job.id))
        assert job.status=='failed' and job.error_code=='output_quality_failed' and video.review_status=='needs_regeneration'
        assert video.metadata_json['playable_video'] is True
        assert (settings.media_root/video.relative_path).read_bytes()==raw
        assert any(c.status=='failed' for c in s.scalars(select(m.QualityCheck).where(m.QualityCheck.video_revision_id==video.id)))
        video_id=video.id
    result=client.post('/api/videos/'+video_id+'/approve',json={'reason':'test'})
    assert result.status_code==409 and result.json()['error']['code']=='video_needs_regeneration'


def test_truncated_cloud_output_is_preserved_without_attempting_audio_mix(client,settings,task,monkeypatch,tmp_path):
    from app.services import cinematic_quality,audio_mix
    raw=b'bad-but-returned'
    class Provider:
        async def get_job(self,identifier):return {'status':'generated','output_url':'https://invalid.example/x','raw':{'status':'succeeded'}}
        async def download(self,url):return raw
    monkeypatch.setattr(video_services,'existing_provider',lambda *a:Provider())
    monkeypatch.setattr(jobs,'PROJECT_ROOT',tmp_path)
    def forbidden_mix(*args):raise AssertionError('an undecodable video must not be mixed')
    monkeypatch.setattr(audio_mix,'mix_audio_events',forbidden_mix)
    monkeypatch.setattr(jobs,'inspect_media',lambda *a:[{'check_type':'media_decode','status':'failed','details':{'reason':'invalid'}}])
    monkeypatch.setattr(cinematic_quality,'inspect_generated',lambda *a:[])
    with client.app.state.session_factory() as s:
        job=s.get(m.GenerationJob,task.job_id);job.provider='cloud_video_jobs_v1';job.status='queued';job.provider_job_id='provider-task'
        job.parameters_json={'output_relative_dir':'projects/test/truncated','service_snapshot':{'provider':'video_api','is_cloud':True},
                             'screenplay_snapshot':{'resolved_audio_events':[{'event':'test'}]}}
        s.commit()
    asyncio.run(jobs.poll_job(settings,client.app.state.session_factory,task.job_id))
    with client.app.state.session_factory() as s:
        job=s.get(m.GenerationJob,task.job_id)
        video=s.scalar(select(m.VideoRevision).where(m.VideoRevision.generation_job_id==job.id))
        assert job.status=='failed' and video.review_status=='needs_regeneration'
        assert video.metadata_json['playable_video'] is False
        assert (settings.media_root/video.relative_path).read_bytes()==raw


def test_lk_cloud_poll_failure_preserves_id_and_blocks_repeated_submission(client,settings,task,monkeypatch):
    with client.app.state.session_factory() as s:
        job=s.get(m.GenerationJob,task.job_id);job.provider='cloud_lk888_h3_v2';job.status='queued';job.provider_job_id='existing-id';s.commit()
    async def unavailable(*args):raise AppError(503,'video_download_timeout','download unavailable')
    async def no_composition(*args):pass
    monkeypatch.setattr(jobs,'poll_job',unavailable)
    monkeypatch.setattr(jobs,'process_composition',no_composition)
    asyncio.run(jobs.process_video_tick(settings,client.app.state.session_factory))
    with client.app.state.session_factory() as s:
        job=s.get(m.GenerationJob,task.job_id)
        assert job.status=='unknown' and job.provider_job_id=='existing-id' and job.error_code=='provider_status_unknown'


def test_mixed_decode_failure_still_marks_playable_raw_file(client,settings,task,monkeypatch,tmp_path):
    from app.services import cinematic_quality,audio_mix
    raw=b'valid raw video'
    class Provider:
        async def get_job(self,identifier):return {'status':'generated','output_url':'https://invalid.example/x'}
        async def download(self,url):return raw
    monkeypatch.setattr(video_services,'existing_provider',lambda *a:Provider())
    monkeypatch.setattr(jobs,'PROJECT_ROOT',tmp_path)
    monkeypatch.setattr(audio_mix,'mix_audio_events',lambda path,*a:(path.write_bytes(b'bad mixed') and {'performed':True}))
    def inspect(path,*a):
        return [{'check_type':'media_decode','status':'passed' if path.read_bytes()==raw else 'failed','details':{}}]
    monkeypatch.setattr(jobs,'inspect_media',inspect)
    monkeypatch.setattr(cinematic_quality,'inspect_generated',lambda *a:[])
    with client.app.state.session_factory() as s:
        job=s.get(m.GenerationJob,task.job_id);job.provider='cloud_video_jobs_v1';job.status='queued';job.provider_job_id='provider-task'
        job.parameters_json={'output_relative_dir':'projects/test/mixed','service_snapshot':{'provider':'video_api','is_cloud':True},
                             'screenplay_snapshot':{'resolved_audio_events':[{'event':'test'}]}}
        s.commit()
    asyncio.run(jobs.poll_job(settings,client.app.state.session_factory,task.job_id))
    with client.app.state.session_factory() as s:
        job=s.get(m.GenerationJob,task.job_id)
        video=s.scalar(select(m.VideoRevision).where(m.VideoRevision.generation_job_id==job.id))
        assert job.error_code=='output_decode_failed' and video.metadata_json['playable_video'] is True
        assert (settings.media_root/video.relative_path).read_bytes()==raw


def test_mixed_file_recovery_uses_verified_raw_and_never_overwrites_existing(client,settings,task,monkeypatch,tmp_path):
    from app.services import cinematic_quality,audio_mix
    raw=b'provider bytes';existing=b'unknown file contents'
    class Provider:
        async def get_job(self,identifier):return {'status':'generated','output_url':'https://invalid.example/x'}
        async def download(self,url):return raw
    monkeypatch.setattr(video_services,'existing_provider',lambda *a:Provider())
    monkeypatch.setattr(jobs,'PROJECT_ROOT',tmp_path)
    paths=[]
    def mix(path,*a):
        paths.append(path);assert path.read_bytes()==raw;path.write_bytes(b'new mixed file');return {'performed':True}
    monkeypatch.setattr(audio_mix,'mix_audio_events',mix)
    monkeypatch.setattr(jobs,'inspect_media',lambda *a:[{'check_type':'media_decode','status':'passed','details':{}}])
    monkeypatch.setattr(cinematic_quality,'inspect_generated',lambda *a:[{'check_type':'output_spec','status':'failed','details':{}}])
    relative='projects/test/recovery';directory=settings.media_root/relative;directory.mkdir(parents=True,exist_ok=True)
    dest=directory/(task.job_id+'.mp4');dest.write_bytes(existing)
    original=directory/(task.job_id+'-provider-raw.mp4');original.write_bytes(raw)
    with client.app.state.session_factory() as s:
        job=s.get(m.GenerationJob,task.job_id);job.provider='cloud_video_jobs_v1';job.status='queued';job.provider_job_id='provider-task'
        job.parameters_json={'output_relative_dir':relative,'service_snapshot':{'provider':'video_api','is_cloud':True},
                             'screenplay_snapshot':{'resolved_audio_events':[{'event':'test'}]}}
        s.commit()
    asyncio.run(jobs.poll_job(settings,client.app.state.session_factory,task.job_id))
    assert dest.read_bytes()==existing and original.read_bytes()==raw
    assert len(paths)==1 and paths[0]!=dest and paths[0].read_bytes()==b'new mixed file'
    with client.app.state.session_factory() as s:
        job=s.get(m.GenerationJob,task.job_id)
        video=s.scalar(select(m.VideoRevision).where(m.VideoRevision.generation_job_id==job.id))
        assert job.error_code=='output_quality_failed' and video.metadata_json['playable_video'] is True


def test_recovery_rejects_unverified_existing_file_without_raw_copy(client,settings,task,monkeypatch,tmp_path):
    raw=b'provider bytes';existing=b'unknown file contents'
    class Provider:
        async def get_job(self,identifier):return {'status':'generated','output_url':'https://invalid.example/x'}
        async def download(self,url):return raw
    monkeypatch.setattr(video_services,'existing_provider',lambda *a:Provider())
    monkeypatch.setattr(jobs,'PROJECT_ROOT',tmp_path)
    relative='projects/test/unverified';directory=settings.media_root/relative;directory.mkdir(parents=True,exist_ok=True)
    dest=directory/(task.job_id+'.mp4');dest.write_bytes(existing)
    with client.app.state.session_factory() as s:
        job=s.get(m.GenerationJob,task.job_id);job.provider='cloud_video_jobs_v1';job.status='queued';job.provider_job_id='provider-task'
        job.parameters_json={'output_relative_dir':relative,'service_snapshot':{'provider':'video_api','is_cloud':True},
                             'screenplay_snapshot':{'resolved_audio_events':[{'event':'test'}]}}
        s.commit()
    with pytest.raises(AppError) as error:
        asyncio.run(jobs.poll_job(settings,client.app.state.session_factory,task.job_id))
    assert error.value.code=='output_already_exists' and dest.read_bytes()==existing
