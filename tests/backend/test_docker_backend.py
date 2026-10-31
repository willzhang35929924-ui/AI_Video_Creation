"""Isolated deployment/error-path regressions; real container E2E is separate."""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from app import models as m
from app.core import secrets as store
from app.core import deployment
from app.core.config import Settings
from app.core.errors import AppError
from app.core.trusted_service_origins import configured_local_origins, is_trusted_local_service
from app.services import ai_services


@pytest.fixture
def container_key(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet
    path=tmp_path/'private'/'master.key';path.parent.mkdir(mode=0o700)
    path.write_bytes(Fernet.generate_key()+b'\n');path.chmod(0o600)
    monkeypatch.setenv(store.KEY_FILE_ENV,str(path));monkeypatch.setenv('VIDEO_FACTORY_CONTAINER','1')
    return path


def test_container_ciphertext_roundtrip_and_no_plaintext_fallback(container_key,monkeypatch):
    monkeypatch.setattr(store,'_crypt',lambda *args,**kwargs:pytest.fail('Container encryption must not fall back to DPAPI/plaintext'))
    plain='unit-only-private-api-value'
    encrypted=store.protect(plain)
    assert encrypted.startswith(store.FERNET_PREFIX) and plain.encode() not in encrypted
    assert store.reveal(encrypted)==plain
    assert 'Container Fernet' in store.storage_label(encrypted)


def test_replaced_or_missing_master_key_has_actionable_failure(container_key):
    from cryptography.fernet import Fernet
    encrypted=store.protect('isolated-test-value')
    container_key.write_bytes(Fernet.generate_key()+b'\n')
    with pytest.raises(AppError) as mismatch:store.reveal(encrypted)
    assert mismatch.value.code=='secret_master_key_mismatch'
    container_key.unlink()
    with pytest.raises(AppError) as missing:store.reveal(encrypted)
    assert missing.value.code=='secret_master_key_missing' and not container_key.exists()


def test_key_is_created_only_for_an_empty_credential_database(tmp_path,monkeypatch):
    path=tmp_path/'private'/'master.key';db=tmp_path/'fresh.db'
    monkeypatch.setenv(store.KEY_FILE_ENV,str(path));monkeypatch.setenv('VIDEO_FACTORY_CONTAINER','1')
    result=store.ensure_container_secret_key('sqlite:///'+db.as_posix())
    assert path.is_file() and not db.exists() and result['existing_credentials_validated']==0
    if os.name=='posix':assert path.stat().st_mode & 0o777==0o600
    ciphertext=store.protect('unit-test-value')
    with sqlite3.connect(db) as connection:
        connection.execute('CREATE TABLE ai_services (encrypted_secret BLOB)')
        connection.execute('INSERT INTO ai_services VALUES (?)',(ciphertext,))
    original=path.read_bytes();assert store.ensure_container_secret_key('sqlite:///'+db.as_posix())['existing_credentials_validated']==1
    assert path.read_bytes()==original
    path.unlink()
    with pytest.raises(AppError) as missing:store.ensure_container_secret_key('sqlite:///'+db.as_posix())
    assert missing.value.code=='secret_master_key_missing' and not path.exists()


def test_copied_platform_ciphertext_is_not_treated_as_container_plaintext(container_key,tmp_path):
    db=tmp_path/'copied.db'
    with sqlite3.connect(db) as connection:
        connection.execute('CREATE TABLE ai_services (encrypted_secret BLOB)')
        connection.execute('INSERT INTO ai_services VALUES (?)',(b'old-platform-encrypted-blob',))
    with pytest.raises(AppError) as error:store.ensure_container_secret_key('sqlite:///'+db.as_posix())
    assert error.value.code=='secret_migration_required'


def test_changed_key_cannot_start_against_existing_container_credentials(container_key,tmp_path):
    from cryptography.fernet import Fernet
    db=tmp_path/'credentials.db';ciphertext=store.protect('unit-only-secret')
    with sqlite3.connect(db) as connection:
        connection.execute('CREATE TABLE ai_services (encrypted_secret BLOB)')
        connection.execute('INSERT INTO ai_services VALUES (?)',(ciphertext,))
    container_key.write_bytes(Fernet.generate_key())
    with pytest.raises(AppError) as error:store.ensure_container_secret_key('sqlite:///'+db.as_posix())
    assert error.value.code=='secret_master_key_mismatch'


def test_key_symlinks_are_rejected(container_key,monkeypatch):
    monkeypatch.setattr(Path,'is_symlink',lambda self:True)
    with pytest.raises(AppError) as error:store.protect('unit-value')
    assert error.value.code=='secret_master_key_permissions'


def test_key_permissions_are_rejected_on_posix(container_key):
    if os.name!='posix':pytest.skip('POSIX permissions are verified by actual Linux container checks')
    container_key.chmod(0o644)
    with pytest.raises(AppError) as error:store.protect('unit-value')
    assert error.value.code=='secret_master_key_permissions'


def test_container_key_is_encrypted_and_masked_through_real_api(container_key,client):
    value='isolated-api-test-no-real-key'
    response=client.post('/api/settings/ai-services',json={'module':'language_processing','provider':'openai_compatible','name':'Isolated encrypted service','base_url':'https://example.com/v1','default_model':'unit-model','api_key':value})
    assert response.status_code==201,response.text
    public=response.json();assert public['has_api_key'] and value not in response.text
    assert 'Container Fernet' in public['key_storage']
    with client.app.state.session_factory() as session:
        row=session.get(m.AIService,public['id']);assert store.reveal(row.encrypted_secret)==value
        assert value.encode() not in row.encrypted_secret


@pytest.mark.parametrize('origin',['http://host.docker.internal:8189','http://192.168.1.10:8189','http://comfyui:8188','https://mini.local:8443','http://[fd00::2]:8188'])
def test_explicit_private_origins_are_exact_and_remain_local(monkeypatch,origin):
    monkeypatch.setenv('TRUSTED_LOCAL_SERVICE_ORIGINS',origin)
    assert is_trusted_local_service(origin+'/v1')
    ai_services.validate_configuration('video_generation','comfyui',origin,{})
    service=m.AIService(id='isolated',module='video_generation',provider='comfyui',name='test',base_url=origin,default_model='test',settings_json={})
    assert ai_services.service_snapshot(service)['is_cloud'] is False
    other=origin.rsplit(':',1)[0]+':9999'
    assert not is_trusted_local_service(other)


@pytest.mark.parametrize('origin',['http://169.254.169.254','http://[fd00:ec2::254]','https://api.openai.com','https://8.8.8.8','http://host.docker.internal:8189/v1','http://user:pass@host.docker.internal:8189','http://host.docker.internal:8189?key=hidden'])
def test_metadata_public_or_nonorigin_trust_is_rejected(monkeypatch,origin):
    monkeypatch.setenv('TRUSTED_LOCAL_SERVICE_ORIGINS',origin)
    with pytest.raises(AppError):configured_local_origins()


def test_unlisted_docker_host_is_not_implicitly_trusted(monkeypatch):
    monkeypatch.delenv('TRUSTED_LOCAL_SERVICE_ORIGINS',raising=False)
    with pytest.raises(AppError):ai_services.validate_configuration('video_generation','comfyui','http://host.docker.internal:8189',{})
    with pytest.raises(AppError):ai_services.validate_configuration('language_processing','local_llm','http://host.docker.internal:8771/v1',{})


def test_trust_does_not_turn_cloud_provider_into_unmetered_local(monkeypatch):
    monkeypatch.setenv('TRUSTED_LOCAL_SERVICE_ORIGINS','https://mini.local:8443')
    service=m.AIService(id='isolated',module='language_processing',provider='openai_compatible',name='test',base_url='https://mini.local:8443',default_model='test',settings_json={})
    assert ai_services.service_snapshot(service)['is_cloud'] is True


def test_only_trusted_adapter_request_bypasses_public_dns_pin(monkeypatch,settings):
    monkeypatch.setenv('TRUSTED_LOCAL_SERVICE_ORIGINS','http://host.docker.internal:8189')
    seen=[]
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def request(self,method,url,**kwargs):seen.append((method,url,kwargs));return SimpleNamespace(status_code=200)
    def forbidden(*args):pytest.fail('Explicit trusted local adapter must not be sent to the public-URL importer')
    monkeypatch.setattr(ai_services.httpx,'AsyncClient',Client);monkeypatch.setattr(ai_services,'pin_public_url',forbidden)
    asyncio.run(ai_services.service_request(settings,{'provider':'comfyui','base_url':'http://host.docker.internal:8189','settings':{}},'GET','/queue'))
    assert seen[0][1]=='http://host.docker.internal:8189/queue'


def test_clean_defaults_do_not_seed_demo_business_records(monkeypatch):
    monkeypatch.delenv('SEED_DEMO_DATA',raising=False)
    assert Settings.from_env().seed_demo_data is False


def test_container_seed_only_creates_real_ffmpeg_configuration(settings,client):
    settings.seed_local_model_services=False
    with client.app.state.session_factory() as session:
        for service in session.query(m.AIService).all():session.delete(service)
        session.commit();ai_services.seed_local_services(session,settings)
        services=session.query(m.AIService).all()
        assert len(services)==1 and services[0].provider=='ffmpeg' and not services[0].capabilities_json
        assert session.query(m.Project).count()==0


def test_runtime_health_reports_degraded_without_model_claims(client):
    response=client.get('/api/health/runtime');assert response.status_code==200
    body=response.json();assert body['application_ready'] and body['status']=='degraded'
    assert body['external_services']['capability_status']=='not_probed_by_application_health'
    assert body['queue']['implementation'].startswith('SQLite') and body['queue']['processes_required']==1


def test_runtime_health_fails_if_a_required_worker_stopped(client,settings):
    settings.h3_demo_mode=False
    client.app.state.worker_tasks={'failed':SimpleNamespace(cancelled=lambda:False,done=lambda:True)}
    response=client.get('/api/health/runtime');assert response.status_code==503
    assert 'application_worker_stopped' in response.json()['errors']
    settings.h3_demo_mode=True  # Existing isolated lifespan started no real workers.


@pytest.mark.parametrize('seed,demo',[(True,False),(False,True)])
def test_container_rejects_demo_or_seed_mode_without_touching_data(settings,seed,demo):
    settings.container_mode=True;settings.seed_demo_data=seed;settings.h3_demo_mode=demo
    with pytest.raises(AppError) as error:deployment.validate_container_startup(settings)
    assert error.value.code in {'container_demo_disabled','container_root_user'}


def test_container_rejects_nonpersistent_database(settings):
    settings.container_mode=True;settings.h3_demo_mode=False;settings.database_url='sqlite:///:memory:'
    with pytest.raises(AppError) as error:deployment.validate_container_startup(settings)
    assert error.value.code in {'container_database_unsupported','container_root_user'}


def test_partial_restore_refuses_startup_before_writing_or_migrating(settings,tmp_path,monkeypatch):
    settings.container_mode=True;settings.h3_demo_mode=False
    settings.database_url='sqlite:///'+(tmp_path/'video_factory.db').as_posix()
    marker=tmp_path/'RESTORE_INCOMPLETE';marker.write_text('interrupted restore',encoding='utf-8')
    monkeypatch.setattr(deployment.os,'geteuid',lambda:10001,raising=False)
    monkeypatch.setattr(deployment,'_write_probe',lambda *args:pytest.fail('Partial restore must be rejected before writing to any volume'))
    with pytest.raises(AppError) as error:deployment.validate_container_startup(settings)
    assert error.value.code=='container_restore_incomplete'
    assert marker.read_text(encoding='utf-8')=='interrupted restore'
    assert not (tmp_path/'video_factory.db').exists()
