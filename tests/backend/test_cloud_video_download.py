"""Isolated stream tests, no third-party service or paid task."""
import asyncio
import httpx
import pytest

from app.core.errors import AppError
from app.services import ai_services


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):self.chunks=chunks;self.reads=0;self.closed=False
    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads+=1
            yield chunk
    async def aclose(self):self.closed=True


def test_stream_pins_public_host_and_keeps_signed_query_without_authorization(settings,monkeypatch):
    url='https://cdn.example.invalid/result.mp4?sig=local-test'
    seen=[]; stream=Chunks([b'first',b'second']); real_client=httpx.AsyncClient
    def pin(value,settings):
        assert value==url
        assert settings.doc_import_allowed_hosts==('cdn.example.invalid',)
        return 'https://8.8.8.8/result.mp4?sig=local-test','cdn.example.invalid','cdn.example.invalid'
    async def server(request):
        seen.append(request)
        return httpx.Response(200,stream=stream)
    def client(**kwargs):
        assert kwargs['follow_redirects'] is False and kwargs['trust_env'] is False
        return real_client(transport=httpx.MockTransport(server),**kwargs)
    monkeypatch.setattr(ai_services,'pin_public_url',pin)
    monkeypatch.setattr(ai_services.httpx,'AsyncClient',client)
    result=asyncio.run(ai_services.download_public_video(settings,url,max_bytes=20))
    assert result==b'firstsecond' and stream.closed
    request=seen[0]
    assert request.url.query==b'sig=local-test' and request.headers['host']=='cdn.example.invalid'
    assert request.headers['accept-encoding']=='identity' and 'authorization' not in request.headers
    assert request.extensions['sni_hostname']=='cdn.example.invalid'


@pytest.mark.parametrize('headers,chunks',[
    ({'content-length':'11'},[b'abcdefghijk']),
    ({},[b'a'*65536,b'b'*65536,b'c']),
])
def test_stream_stops_when_content_exceeds_limit(settings,monkeypatch,headers,chunks):
    stream=Chunks(chunks); real_client=httpx.AsyncClient
    monkeypatch.setattr(ai_services,'pin_public_url',lambda *args:('https://8.8.8.8/v','cdn.example.invalid','cdn.example.invalid'))
    async def server(request):return httpx.Response(200,headers=headers,stream=stream)
    monkeypatch.setattr(ai_services.httpx,'AsyncClient',lambda **kwargs:real_client(transport=httpx.MockTransport(server),**kwargs))
    limit=10 if headers else 65536
    with pytest.raises(AppError) as exc:asyncio.run(ai_services.download_public_video(settings,'https://cdn.example.invalid/v',max_bytes=limit))
    assert exc.value.code=='video_too_large' and stream.closed
    assert stream.reads==0 if headers else stream.reads==2


@pytest.mark.parametrize('url',['http://example.com/v','https://localhost/v','https://user@public.example/v','https://public.example/v#fragment'])
def test_download_rejects_unsafe_urls_without_request(settings,monkeypatch,url):
    def pin(value,settings):
        if 'localhost' in value:raise AppError(422,'unsafe_url','private host')
        raise AssertionError('unexpected DNS call')
    monkeypatch.setattr(ai_services,'pin_public_url',pin)
    with pytest.raises(AppError):asyncio.run(ai_services.download_public_video(settings,url))


def test_download_does_not_follow_redirect(settings,monkeypatch):
    seen=[];real_client=httpx.AsyncClient
    monkeypatch.setattr(ai_services,'pin_public_url',lambda *args:('https://8.8.8.8/v','cdn.example.invalid','cdn.example.invalid'))
    async def server(request):seen.append(request);return httpx.Response(302,headers={'location':'https://private.example/'})
    monkeypatch.setattr(ai_services.httpx,'AsyncClient',lambda **kwargs:real_client(transport=httpx.MockTransport(server),**kwargs))
    with pytest.raises(httpx.HTTPStatusError):asyncio.run(ai_services.download_public_video(settings,'https://cdn.example.invalid/v'))
    assert len(seen)==1 and 'authorization' not in seen[0].headers
