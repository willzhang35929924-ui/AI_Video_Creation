from sqlalchemy import select
import pytest
from app.models import AIService,ProjectServiceBinding
from app.services.video_services import select_video_service, existing_provider
from app.core.errors import AppError


def test_video_selection_uses_own_service_and_exact_workflow_model(client,settings):
    from dataclasses import replace
    workflow=__import__('pathlib').Path(__file__).resolve().parents[2]/'workflows/h3_r2v_character_v1.json'
    config=replace(settings,h3_workflow_api_json=workflow)
    with client.app.state.session_factory() as session:
        service=session.scalar(select(AIService).where(AIService.module=='video_generation'))
        service.settings_json={**service.settings_json,'workflow_path':str(workflow)}
        session.flush()
        _,snapshot,provider=select_video_service(session,config,'no-binding')
        assert provider.name=='comfyui_h3' and snapshot['model_id']=='minimax_h3_ref2va_pruned_w6a8.safetensors'
        assert snapshot['workflow_sha256'] and snapshot['service_id']==service.id
        original=snapshot.copy()
        service.default_model='wrong-model'
        with pytest.raises(AppError) as exc:select_video_service(session,config,'no-binding')
        assert exc.value.code=='workflow_model_mismatch'
        assert snapshot==original
        assert existing_provider(session,config,snapshot).settings.comfyui_base_url==snapshot['base_url']


def test_cloud_config_does_not_imply_generation_support(client,settings):
    with client.app.state.session_factory() as session:
        service=AIService(module='video_generation',provider='video_api',name='unit contract only',base_url='https://example.invalid',default_model='unverified')
        session.add(service);session.flush()
        with pytest.raises(AppError) as exc:select_video_service(session,settings,'none',service.id)
        assert exc.value.code=='video_contract_required'
        service.settings_json={'video_contract':'video-jobs-v1'}
        _,snapshot,provider=select_video_service(session,settings,'none',service.id)
        assert provider.name=='cloud_video_jobs_v1' and snapshot['is_cloud']
        assert service.capabilities_json=={}


@pytest.mark.parametrize('base_url,trust', [
    ('http://127.0.0.1:8189', ''),
    ('http://localhost:8189', ''),
    ('http://[::1]:8189', ''),
    ('http://host.docker.internal:8189', 'http://host.docker.internal:8189'),
    ('http://host.docker.internal:8189/comfy', 'http://host.docker.internal:8189'),
])
def test_comfy_selection_and_historical_provider_allow_only_explicit_local_origins(
        client, settings, monkeypatch, base_url, trust):
    from dataclasses import replace
    from pathlib import Path

    monkeypatch.setenv('TRUSTED_LOCAL_SERVICE_ORIGINS', trust)
    workflow = Path(__file__).resolve().parents[2] / 'workflows/h3_r2v_character_v1.json'
    config = replace(settings, h3_workflow_api_json=workflow)
    with client.app.state.session_factory() as session:
        service = session.scalar(select(AIService).where(AIService.module == 'video_generation'))
        service.base_url = base_url
        service.settings_json = {**service.settings_json, 'workflow_path': str(workflow)}
        session.flush()
        _, snapshot, provider = select_video_service(session, config, 'no-binding')
        assert provider.settings.comfyui_base_url == base_url
        assert existing_provider(session, config, snapshot).settings.comfyui_base_url == base_url


@pytest.mark.parametrize('base_url,trust', [
    ('http://host.docker.internal:8189', ''),
    ('http://host.docker.internal:8190', 'http://host.docker.internal:8189'),
    ('https://host.docker.internal:8189', 'http://host.docker.internal:8189'),
    ('http://192.168.1.2:8189', 'http://host.docker.internal:8189'),
    ('https://example.com:8189', 'http://host.docker.internal:8189'),
    ('file://localhost:8189/video', ''),
    ('http://user:password@localhost:8189', ''),
    ('http://@localhost:8189', ''),
    ('http://host.docker.internal:8189?key=x', 'http://host.docker.internal:8189'),
    ('http://localhost:8189?key=x', ''),
    ('http://localhost:8189?', ''),
    ('http://localhost:8189#fragment', ''),
    ('http://localhost:8189#', ''),
    ('http://localhost:badport', ''),
])
def test_comfy_selection_and_historical_provider_reject_unsafe_origins(
        client, settings, monkeypatch, base_url, trust):
    from app.services.ai_services import service_snapshot

    monkeypatch.setenv('TRUSTED_LOCAL_SERVICE_ORIGINS', trust)
    with client.app.state.session_factory() as session:
        service = session.scalar(select(AIService).where(AIService.module == 'video_generation'))
        service.base_url = base_url
        session.flush()
        snapshot = service_snapshot(service)
        with pytest.raises(AppError) as selected:
            select_video_service(session, settings, 'no-binding')
        assert selected.value.code == 'remote_comfy_not_supported'
        with pytest.raises(AppError) as restored:
            existing_provider(session, settings, snapshot)
        assert restored.value.code == 'remote_comfy_not_supported'
