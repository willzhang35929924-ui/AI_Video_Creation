"""Local contract tests only. No provider request or paid image generation."""
import asyncio
import base64
import hashlib
import io
import json
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from app.core.errors import AppError
from app.services.character_factory import qwen_image_body
from app.services import cinematic_visuals


def image_bytes(fmt='PNG'):
    output=io.BytesIO();Image.new('RGB',(32,32),'blue').save(output,fmt);return output.getvalue()


@pytest.mark.parametrize('count',[0,1,2,3])
def test_qwen_contract_preserves_order_and_format(count):
    images=[('a.jpg',image_bytes('JPEG'),'image/png'),('b.png',image_bytes(),'image/jpeg'),('c.png',image_bytes(),'image/png')][:count]
    body=qwen_image_body({'model':'qwen-image-3.0','prompt':'test','size':'1024x1024','n':1},images)
    if not count:assert 'image' not in body;return
    urls=[body['image']] if count==1 else body['image']
    assert len(urls)==count
    for url,(_,data,_) in zip(urls,images):assert base64.b64decode(url.split(',',1)[1])==data
    assert urls[0].startswith('data:image/jpeg;base64,')


def test_qwen_rejects_over_budget_and_non_image():
    image=('one.png',image_bytes(),'image/png')
    for body,files,code in [({'model':'qwen-image-3.0'},[image]*4,'qwen_image_reference_limit'),
                            ({'model':'qwen3.8-max'},[],'qwen_image_model_unsupported'),
                            ({'model':'qwen-image-3.0'},[('bad',b'not image','image/png')],'qwen_image_invalid')]:
        with pytest.raises(AppError) as error:qwen_image_body(body,files)
        assert error.value.code==code
    assert len(qwen_image_body({'model':'qwen-image-2.1-pro'},[image]*10)['image'])==10


@pytest.mark.parametrize('status',[200,500])
def test_real_visual_submission_uses_generations_and_excludes_images_from_log(tmp_path,monkeypatch,status):
    data=image_bytes();(tmp_path/'source.png').write_bytes(data)
    snapshot={'service_id':'fixture','service_revision':1,'model_id':'qwen-image-3.0',
              'settings':{'image_api_format':'qwen_generations'},'provider':'openai_compatible'}
    visual=SimpleNamespace(id='visual-1',snapshot_json={'service_snapshot':snapshot,'parameters':{'width':1024,'height':1024},
        'references':[{'relative_path':'source.png','sha256':hashlib.sha256(data).hexdigest(),'asset_id':'a'}]},prompt='sample',status='queued',provider_job_id=None,sha256=None)
    class Session:
        def commit(self):pass
    calls=[]
    async def transport(settings,snapshot,method,endpoint,secret,**kwargs):
        calls.append((method,endpoint,kwargs));return httpx.Response(status,headers={'x-request-id':'actual-id'},
            json={'data':[{'b64_json':base64.b64encode(data).decode()}]},request=httpx.Request(method,'https://example.invalid'))
    def store(_settings,_session,obj,output):
        assert output==data;obj.sha256=hashlib.sha256(output).hexdigest();obj.snapshot_json['actual_dimensions']=[32,32]
    monkeypatch.setattr(cinematic_visuals,'service_request',transport)
    monkeypatch.setattr(cinematic_visuals,'store_visual',store)
    coro=cinematic_visuals.cloud_visual(SimpleNamespace(media_root=tmp_path),Session(),visual,'sensitive-key',tmp_path)
    if status==500:
        with pytest.raises(httpx.HTTPStatusError):asyncio.run(coro)
        assert visual.snapshot_json['cloud_post_accepted'] is True
    else:asyncio.run(coro)
    assert len(calls)==1 and calls[0][:2]==('POST','/images/generations')
    assert calls[0][2]['json']['image'].startswith('data:image/png;base64,')
    record=json.loads((tmp_path/'cloud_request.json').read_text(encoding='utf8'))
    assert record['endpoint']=='/images/generations' and 'image' not in record['body']
    assert 'sensitive-key' not in json.dumps(record)
