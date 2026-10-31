import ctypes
import json
import os
from pathlib import Path
import pytest
from sqlalchemy import select, text
from app import models as m
from app.services.content_manager import safe_file
from app.core.errors import AppError
from test_workspace import delete, document_bytes, people


def owned_file(client,settings,name='owned.bin',data=b'actual disk bytes'):
    p=settings.media_root/'exports'/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
    with client.app.state.session_factory() as s:
        f=m.ManagedFile(relative_path='exports/'+name,state='retained',metadata_json={'verified_owned':True});s.add(f);s.commit();identifier=f.id
    return identifier,p


def test_bulk_delete_is_idempotent_and_purge_releases_real_bytes(client,settings):
    first,p1=owned_file(client,settings,'one.bin');second,p2=owned_file(client,settings,'two.bin')
    plan,r=delete(client,'files',[first,second],'trash');assert r.status_code==200,r.text
    task=r.json();assert task['status']=='completed' and task['result']['records_deleted']==2
    assert not p1.exists() and not p2.exists()
    payload={'resource':'files','ids':[first,second],'mode':'trash','cascade':False,'fingerprint':plan['fingerprint'],'confirm':True}
    assert client.post('/api/deletions/execute',json=payload).json()['id']==task['id']
    assert client.post('/api/deletions/'+task['id']+'/purge',json={}).status_code==409
    r=client.post('/api/deletions/'+task['id']+'/purge',json={'confirm_permanent':True})
    assert r.json()['status']=='purged' and r.json()['result']['verified_released_bytes']==2*len(b'actual disk bytes')
    assert not (settings.media_root/'.recycle'/task['id']/'exports/one.bin').exists()
    assert client.post('/api/deletions/'+task['id']+'/restore').status_code==409


@pytest.mark.skipif(os.name!='nt',reason='Real Windows file lock contract')
def test_actual_locked_file_failure_then_retry_after_unlock(client,settings):
    identifier,p=owned_file(client,settings)
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateFileW.restype=ctypes.c_void_p
    kernel.CreateFileW.argtypes=[ctypes.c_wchar_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p]
    kernel.CloseHandle.argtypes=[ctypes.c_void_p]
    handle=kernel.CreateFileW(str(p),0x80000000,0,None,3,0,None)
    assert handle not in (None,ctypes.c_void_p(-1).value)
    try:
        plan,r=delete(client,'files',[identifier],'permanent')
        assert r.status_code==200,r.text
        task=r.json();assert task['status']=='files_failed' and p.exists(),task
        assert task['result']['records_deleted']==1 and task['result']['failed_files']==1
    finally:kernel.CloseHandle(handle)
    retried=client.post('/api/deletions/'+task['id']+'/retry')
    assert retried.json()['status']=='completed' and not p.exists(),retried.text


def test_database_failure_rolls_back_records_before_file_operations(client,settings):
    identifier,p=owned_file(client,settings)
    with client.app.state.session_factory() as s:
        s.execute(text('CREATE TABLE unexpected_reference (id TEXT PRIMARY KEY, file_id TEXT REFERENCES managed_files(id))'))
        s.execute(text('INSERT INTO unexpected_reference VALUES (:id,:file_id)'),{'id':'dependency','file_id':identifier});s.commit()
    _,response=delete(client,'files',[identifier],'permanent')
    assert response.json()['status']=='database_failed',response.text
    assert p.exists()
    with client.app.state.session_factory() as s:assert s.get(m.ManagedFile,identifier) is not None


def test_shared_image_file_survives_deleting_one_asset(client,settings):
    people(client)
    with client.app.state.session_factory() as s:
        v=s.scalar(select(m.CharacterVersion).where(m.CharacterVersion.character_id=='person_0'))
        from PIL import Image
        relative='characters/job_fixture/shared.png';p=settings.media_root/relative;p.parent.mkdir(parents=True,exist_ok=True);Image.new('RGB',(64,64),'gray').save(p)
        records=[]
        for _ in range(2):
            a=m.CharacterAsset(character_version_id=v.id,asset_type='front_face',relative_path=relative,mime_type='image/png',sha256='fixture',source='generated',metadata_json={'generation_job_id':'job_fixture'},status='needs_review');s.add(a);s.flush();records.append(a.id)
        s.commit()
    plan,r=delete(client,'assets',[records[0]],'permanent')
    assert r.json()['status']=='completed' and plan['files'][0]['protected']
    assert plan['files'][0]['shared_references'] and p.exists()
    with client.app.state.session_factory() as s:assert s.get(m.CharacterAsset,records[1])


def test_changed_file_invalidates_preview_and_missing_file_is_reported(client,settings):
    identifier,p=owned_file(client,settings)
    payload={'resource':'files','ids':[identifier],'mode':'permanent'}
    plan=client.post('/api/deletions/preview',json=payload).json()
    p.write_bytes(b'changed actual file')
    r=client.post('/api/deletions/execute',json={**payload,'fingerprint':plan['fingerprint'],'confirm':True,'confirm_permanent':True})
    assert r.status_code==409 and p.exists()
    p.unlink()
    _,r=delete(client,'files',[identifier],'permanent')
    assert r.json()['result']['files']['exports/owned.bin']['status']=='missing'


@pytest.mark.skipif(os.name!='nt',reason='Windows DPAPI secure-store verification')
def test_real_dpapi_preserved_on_edit_and_deleted_without_affecting_other_service(client):
    key='isolated-test-secret-not-a-real-provider-key'
    body={'name':'隔离安全存储测试','module':'character_generation','provider':'openai_compatible','base_url':'https://images.example.com','api_key':key}
    first=client.post('/api/settings/ai-services',json=body);assert first.status_code==201,first.text
    second=client.post('/api/settings/ai-services',json={**body,'name':'另一个隔离配置'}).json()
    identifier=first.json()['id']
    assert key not in first.text
    with client.app.state.session_factory() as s:before=s.get(m.AIService,identifier).encrypted_secret;other=s.get(m.AIService,second['id']).encrypted_secret
    patched=client.patch('/api/settings/ai-services/'+identifier,json={'name':'已编辑名称','default_model':'local-test-model'})
    assert patched.status_code==200 and key not in patched.text
    with client.app.state.session_factory() as s:assert s.get(m.AIService,identifier).encrypted_secret==before
    _,r=delete(client,'services',[identifier],'permanent')
    assert r.json()['status']=='completed' and r.json()['result']['credentials'][0]['status']=='dpapi_record_removed_verified'
    with client.app.state.session_factory() as s:
        assert s.get(m.AIService,identifier) is None
        assert s.get(m.AIService,second['id']).encrypted_secret==other
    assert key not in json.dumps(r.json())


def test_alias_conflict_requires_explicit_identity_resolution(client):
    for i in range(2):
        client.post('/api/character-factory/characters',json={'id':'duplicate_'+str(i),'name':'人物0','profile':{'age':35,'description':'不同身份'+str(i)}})
    source=document_bytes()
    b=client.post('/api/word-imports',files={'file':('same-name.docx',source)}).json()
    first=b['preview_json']['characters'][0];assert first['decision']=='pending' and len(first['candidates'])==2
    r=client.post('/api/word-imports/'+b['id']+'/confirm',json={'revision':b['revision'],'duplicate_strategy':'new_project'})
    assert r.status_code==422
    first['decision']='existing';first['match_character_id']='duplicate_1'
    b=client.put('/api/word-imports/'+b['id']+'/preview',json={'revision':b['revision'],'preview':b['preview_json']}).json()
    r=client.post('/api/word-imports/'+b['id']+'/confirm',json={'revision':b['revision'],'duplicate_strategy':'new_project'})
    assert r.status_code==200,r.text
    bundle=client.get('/api/shots/'+r.json()['shot_ids'][0]+'/workspace').json()
    assert bundle['participants'][0]['character_id']=='duplicate_1'


def test_crud_templates_export_and_no_client_path_delete(client,settings):
    first=client.post('/api/content/templates',json={'name':'模板一','body':'自然语言规则'}).json()
    second=client.post('/api/content/templates',json={'name':'模板二'}).json()
    assert client.patch('/api/content/templates/'+first['id'],json={'body':'修订规则'}).status_code==200
    assert client.get('/api/content/templates?q='+first['id']).json()['items'][0]['data']['body']=='修订规则'
    _,r=delete(client,'templates',[first['id'],second['id']]);assert r.json()['result']['records_deleted']==2
    assert client.post('/api/deletions/preview',json={'resource':'files','ids':['any'],'mode':'permanent','path':'C:/Windows'}).status_code==422
    project=client.post('/api/projects',json={'name':'导出文件独立清理测试'}).json()
    exported=client.post('/api/projects/'+project['id']+'/export').json()
    path=settings.media_root/exported['relative_path'];assert path.exists()
    plan,response=delete(client,'files',[exported['id']],'permanent')
    assert response.json()['status']=='completed' and not path.exists(),response.text
    assert not plan['files'][0]['protected']
    assert client.get('/api/projects/'+project['id']).status_code==200


def test_new_revision_keeps_previous_shots_and_original_version(client):
    source=document_bytes()
    b=client.post('/api/word-imports',files={'file':('source.docx',source)}).json()
    r=client.post('/api/word-imports/'+b['id']+'/confirm',json={'revision':b['revision'],'duplicate_strategy':'new_project'}).json()
    second=client.post('/api/word-imports',files={'file':('source.docx',source)}).json()
    p=second['preview_json']
    for character in p['characters']:character['decision']='new';character['match_character_id']=None
    second=client.put('/api/word-imports/'+second['id']+'/preview',json={'revision':second['revision'],'preview':p}).json()
    r2=client.post('/api/word-imports/'+second['id']+'/confirm',json={'revision':second['revision'],'duplicate_strategy':'new_revision','project_id':r['project_id']})
    assert r2.status_code==200,r2.text
    assert r2.json()['script_id']!=r['script_id']
    assert len(client.get('/api/projects/'+r['project_id']+'/shots?include_history=true').json())==4
    assert len(client.get('/api/projects/'+r['project_id']+'/shots').json())==2
    with client.app.state.session_factory() as s:assert s.get(m.ScriptRevision,r['script_id'])
