"""Portable profile freezing and native-runtime evidence boundaries (CPU only)."""
import asyncio
import base64
from dataclasses import replace
import hashlib
import json

import httpx
import pytest

from app.core.errors import AppError
from app.services import h3_context_provider as context
from app.services.video_services import context_service_origins, existing_provider, select_video_service


def runtime_receipt():
    return {'schema_version': 1, 'service': 'video-factory-native-context-runtime-v1',
            'profile_id': 'local-h3ir-qwen4b-v3', 'ready': True,
            'memory_policy': 'runtime_owned', 'watcher_running': True,
            'source_baseline': {'ok': True},
            'qwen_health': {'service': 'video-factory-h3ir-qwen4b-v1', 'profile': 'h3ir-qwen4b-32k',
                            'model_id': 'Qwen/Qwen3-VL-4B-Instruct', 'offline': True,
                            'max_model_len': 32768, 'busy': False, 'loaded': False,
                            'script_sha256': 'a' * 64, 'inference_phase': 'idle',
                            'gpu_release_status': 'not_used', 'last_gpu_cleanup': None}}


def test_profile_freezes_selected_node_and_polling_does_not_follow_new_global(settings, monkeypatch):
    from app.services import video_services
    from types import SimpleNamespace
    profile = {'h3_context_ir': True, 'workflow_profile': 'context_ir',
               'local_service_origin': 'http://192.168.8.42:8788'}
    snapshot = {'base_url': 'http://192.168.8.42:8788', 'settings': profile,
                'model_id': 'minimax_h3_ref2va_pruned_w6a8.safetensors'}
    service = SimpleNamespace(provider='comfyui', encrypted_secret=None)
    monkeypatch.setattr(video_services, 'resolve_service', lambda *a: (service, dict(snapshot)))
    config = replace(settings, container_mode=True, h3_context_comfyui_url='http://127.0.0.1:9999')
    _, frozen, provider = select_video_service(None, config, 'project', quality_mode='fast')
    assert frozen['base_url'] == snapshot['base_url']
    assert frozen['legacy_comfyui_base_url'] is None
    assert frozen['h3_workflow']['actual_steps'] == 4
    restored = existing_provider(None, replace(config, h3_context_comfyui_url='http://127.0.0.1:2222'),
                                 {**frozen, 'provider': 'comfyui'})
    assert restored.settings.comfyui_base_url == provider.settings.comfyui_base_url == snapshot['base_url']
    assert restored.profile_settings == profile


def test_container_legacy_port_requires_explicit_context_profile(settings):
    with pytest.raises(AppError) as exc:
        context_service_origins(replace(settings, container_mode=True),
                                {'base_url': 'http://127.0.0.1:8189', 'settings': {}})
    assert exc.value.code == 'context_ir_profile_required'


@pytest.mark.parametrize('change,code', [
    ({'memory_policy': 'unverified'}, 'context_ir_runtime_required'),
    ({'watcher_running': False}, 'context_ir_runtime_required'),
    ({'source_baseline': {'ok': False}}, 'context_ir_runtime_required'),
    ({'ready': False}, 'context_ir_runtime_not_ready'),
    ({'qwen_health': {**runtime_receipt()['qwen_health'], 'max_model_len': 8192}}, 'context_ir_qwen_identity'),
    ({'qwen_health': {**runtime_receipt()['qwen_health'], 'loaded': True}}, 'context_ir_qwen_starting'),
])
def test_container_never_treats_reachable_unverified_runtime_as_ready(change, code):
    calls = []
    def reply(request):
        calls.append((request.method, str(request.url)))
        return httpx.Response(200, json={**runtime_receipt(), **change})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            with pytest.raises(AppError) as exc:
                await context.require_context_qwen(client, 'http://192.168.8.42:8788', runtime_required=True)
            assert exc.value.code == code
    asyncio.run(run())
    assert calls == [('GET', 'http://192.168.8.42:8788/video_factory/runtime')]


@pytest.mark.parametrize('watcher,code',[(True,None),(False,'context_ir_runtime_required')])
def test_cold_start_preflight_preserves_remote_runtime_ownership_requirement(watcher,code):
    calls=[]
    def reply(request):
        calls.append((request.method,request.url.path))
        if request.url.path=='/queue':return httpx.Response(200,json={'queue_running':[],'queue_pending':[]})
        if request.url.path=='/object_info':return httpx.Response(200,json={'VideoFactoryLocalH3IRCompile':{
            'input':{'optional':{'production_contract':['STRING',{'default':''}],
                                 'caller_contract_policy':['STRING',{'default':'revision_semantics_v2'}]}}}})
        assert request.url.path=='/video_factory/runtime'
        return httpx.Response(200,json={**runtime_receipt(),'watcher_running':watcher})
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            if code:
                with pytest.raises(AppError) as error:
                    await context.require_context_ready(client,'http://192.168.8.42:8788',
                        required_nodes={'VideoFactoryLocalH3IRCompile'},runtime_required=True)
                assert error.value.code==code
            else:
                await context.require_context_ready(client,'http://192.168.8.42:8788',
                    required_nodes={'VideoFactoryLocalH3IRCompile'},runtime_required=True)
    asyncio.run(exercise())
    assert calls==[('GET','/queue'),('GET','/object_info'),('GET','/video_factory/runtime')]


def test_native_receipts_preserve_actual_bytes_and_refuse_hash_corruption(tmp_path, monkeypatch):
    calls = []
    corrupt = False
    def reply(request):
        path = request.url.params['path']
        calls.append(path)
        document = {'status': 'PASS', 'source': 'written', 'fallback_reason': None}
        raw = json.dumps(document, indent=3).encode()
        return httpx.Response(200, json={'raw_base64': base64.b64encode(raw).decode(),
            'bytes': len(raw), 'sha256': '0'*64 if corrupt else hashlib.sha256(raw).hexdigest()})
    monkeypatch.setattr(context, 'local_service_client',
                        lambda *a, **kw: httpx.AsyncClient(transport=httpx.MockTransport(reply)))
    evidence = {'compiler_receipt': 'C:/node/output/h3_local_backend_profile_v3/scope/profile.json',
                'smd_sampling_receipt': 'C:/node/output/h3_smd_sampling_audit/scope/audit.json'}
    result = asyncio.run(context.preserve_context_receipts('http://192.168.8.42:8788', evidence, tmp_path))
    assert set(result) == {'compiler', 'sampling', 'source_gate', 'reference_binding'}
    raw = (tmp_path/'native-receipts/source_gate.json').read_bytes()
    assert hashlib.sha256(raw).hexdigest() == result['source_gate']['sha256']
    corrupt = True
    with pytest.raises(AppError) as exc:
        asyncio.run(context.preserve_context_receipts('http://192.168.8.42:8788', evidence, tmp_path))
    assert exc.value.code == 'context_ir_receipt_invalid'
