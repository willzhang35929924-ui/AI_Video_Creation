import io
import copy
import asyncio
import hashlib
import json
from types import SimpleNamespace
from pathlib import Path
import pytest
import httpx
from PIL import Image
from sqlalchemy import select
from app.models import AIService, CharacterGenerationJob, CharacterBatch
from app.schemas_product import IdentityProfile
from app.services.character_factory import candidate_language_adapter,native_graph,store_image,process_character_jobs
from app.core.errors import AppError


PROFILE = {'age': 34, 'original_adult': True, 'description': 'Original adult male casting character', 'facial_features': 'rectangular jaw', 'hair': 'short dark brown hair', 'eyes': 'brown eyes', 'fixed_traits': ['face', 'hairline'], 'wardrobes': [{'id': 'default', 'description': 'navy overshirt and white top'}]}


def role(client):
    client.post('/api/characters', json={'id': 'factory_contract', 'slug': 'factory_contract', 'display_name': 'Factory contract'})
    version = client.post('/api/characters/factory_contract/versions', json={'identity': {'profile': PROFILE}}).json()
    return version


def test_automated_identity_is_not_reported_as_formally_approved(client):
    from app.models import Character,CharacterVersion
    version=role(client)
    with client.app.state.session_factory() as session:
        item=session.get(CharacterVersion,version['id']);item.approval_state='approved'
        item.identity_json={**item.identity_json,'studio_acceptance':{'actor':'automation'},'human_identity_approved':False}
        session.commit()
    row=next(c for c in client.get('/api/character-factory/characters').json() if c['id']=='factory_contract')
    assert row['factory_status']=='technical_accepted' and row['current_approved_version_id'] is None
    with client.app.state.session_factory() as session:
        session.get(Character,'factory_contract').current_approved_version_id=version['id'];session.commit()
    row=next(c for c in client.get('/api/character-factory/characters').json() if c['id']=='factory_contract')
    assert row['factory_status']=='approved'


def fixture_image(client, version):
    buffer = io.BytesIO(); Image.new('RGB', (64, 64), '#888888').save(buffer, 'PNG')
    response = client.post('/api/characters/factory_contract/assets', data={'version_id': version['id'], 'asset_type': 'front_face', 'is_primary_reference': 'false'}, files={'file': ('unit-contract.png', buffer.getvalue(), 'image/png')})
    return response.json()


def test_reference_batch_requires_explicit_approved_anchor(client):
    version = role(client)
    result = client.post('/api/characters/factory_contract/batch-preview', json={'version_id': version['id']})
    assert result.status_code == 409
    assert result.json()['error']['code'] == 'approved_anchor_required'
    asset = fixture_image(client, version)
    refused = client.post('/api/characters/factory_contract/standard-face', json={'asset_id': asset['id'], 'confirm_anchor_lock': True, 'reason': 'unit validation'})
    assert refused.status_code == 409
    assert client.post('/api/character-assets/' + asset['id'] + '/review', json={'status': 'approved', 'reason': 'Isolated image contract fixture, not generation evidence'}).status_code == 200
    anchor = client.post('/api/characters/factory_contract/standard-face', json={'asset_id': asset['id'], 'confirm_anchor_lock': True, 'reason': 'Explicit unit anchor selection'}).json()
    assert anchor['standard_face_asset_id'] == asset['id']
    assert client.patch('/api/characters/factory_contract/versions/' + version['id'], json={'identity': {'changed': True}}).status_code == 409
    assert client.post('/api/character-assets/' + asset['id'] + '/review', json={'status': 'rejected', 'reason': 'Cannot mutate anchor'}).status_code == 409


def test_preview_confirmation_and_job_snapshot_are_persisted(client):
    version = role(client)
    # Isolated validation fixture: this does not claim a live model/capability test.
    with client.app.state.session_factory() as session:
        service = session.scalar(select(AIService).where(AIService.module == 'character_generation'))
        service.settings_json={**service.settings_json,'candidate_model':'sd_xl_base_1.0.safetensors'}
        service.capabilities_json = {'text_to_image': {'state': 'detected', 'source': 'isolated contract fixture'}}
        session.commit()
    payload = {'version_id': version['id'], 'kind': 'candidates', 'candidate_count': 2}
    preview = client.post('/api/characters/factory_contract/batch-preview', json=payload).json()
    assert len(preview['jobs']) == 2 and preview['reference'] is None
    assert client.post('/api/characters/factory_contract/batch-generate', json=payload).status_code == 409
    created = client.post('/api/characters/factory_contract/batch-generate', json={**payload, 'confirm': True, 'preview_hash': preview['preview_hash']})
    assert created.status_code == 202, created.text
    batch = created.json()
    assert all(job['status'] == 'queued' and job['result_asset_id'] is None for job in batch['jobs'])
    assert batch['snapshot_json']['service_snapshot']['model_id'] == 'sd_xl_base_1.0.safetensors'
    paused = client.post('/api/character-generation-jobs/' + batch['id'] + '/pause').json()
    assert paused['paused'] and all(job['status'] == 'queued' for job in paused['jobs'])
    assert client.post('/api/character-generation-jobs/' + batch['id'] + '/resume').json()['paused'] is False
    cancelled = client.post('/api/character-generation-jobs/' + batch['id'] + '/cancel').json()
    assert all(job['status'] == 'cancelled' for job in cancelled['batch']['jobs'])
    retried = client.post('/api/character-generation-jobs/' + batch['id'] + '/retry').json()
    assert len(retried['jobs']) == 4
    assert sum(job['status'] == 'queued' for job in retried['jobs']) == 2
    assert all('encrypted_secret' not in str(job['snapshot_json']) for job in retried['jobs'])


@pytest.mark.parametrize('features,gender',[('原创东亚成年男性，方脸，短黑发，深棕眼睛，略宽鼻梁，自然毛孔和轻微胡茬，正常成年比例','male'),('原创东亚成年女性，黑色齐肩发，椭圆脸，自然肤质','female')])
def test_explicit_chinese_identity_gets_early_english_conditioning(features,gender):
    profile=IdentityProfile(age=38,description='原创成年设备工程师',facial_features=features)
    hint,receipt,warnings=candidate_language_adapter(profile,'橄榄绿棉质工作夹克、深灰长裤')
    assert hint.startswith('Unretouched live-action casting photograph of one character of '+gender+' gender, 38 years old.')
    assert 'East Asian' in hint and 'olive green cotton work jacket' in hint and 'dark gray trousers' in hint
    assert receipt['gender']==gender and receipt['full_translation'] is False
    assert any(v['field']=='facial_features' and v['value']==gender for v in receipt['gender_sources'])
    assert warnings and receipt['unmapped_chinese']  # profession is retained, not fabricated as translated
    if gender=='male':
        assert all(part in hint for part in ['square face','short black hair','dark brown eyes','slight facial stubble','visible natural skin pores'])


def test_candidate_gender_unspecified_is_not_inferred_from_name_profession_or_clothes():
    profile=IdentityProfile(age=38,description='角色丙，工程师',hair='短黑发',body='正常成年比例')
    hint,receipt,warnings=candidate_language_adapter(profile,'男士橄榄绿棉质工作夹克')
    assert receipt['gender']=='unspecified' and not receipt['gender_sources']
    assert 'adult male' not in hint and 'adult female' not in hint
    assert any('未明确性别' in warning for warning in warnings)


def test_conflicting_explicit_identity_stops_candidates_and_keeps_source_evidence():
    profile=IdentityProfile(age=38,description='原创成年男性',facial_features='成年女性，短黑发')
    with pytest.raises(AppError) as error:candidate_language_adapter(profile,'工作夹克')
    assert error.value.code=='candidate_identity_gender_conflict'
    assert {source['value'] for source in error.value.details['sources']}=={'male','female'}


def test_negated_gender_is_not_a_second_identity_and_unmapped_chinese_is_reported():
    profile=IdentityProfile(age=38,description='原创成年男性，不是女性',facial_features='方脸，特殊的无法翻译设定')
    hint,receipt,warnings=candidate_language_adapter(profile,'工作夹克')
    assert receipt['gender']=='male' and 'adult female' not in hint
    assert any('特殊' in u['unmapped_chinese'] for u in receipt['unmapped_chinese'])
    assert any('不代表SDXL已理解' in v for v in warnings)


def test_all_sdxl_candidate_prompts_include_explicit_male_and_preserve_original_profile(client):
    original={**copy.deepcopy(PROFILE),'age':38,'description':'原创成年设备工程师','facial_features':'原创东亚成年男性，方脸，短黑发，深棕眼睛，轻微胡茬','hair':'','eyes':'',
              'wardrobes':[{'id':'default','description':'橄榄绿棉质工作夹克、深灰长裤'}]}
    client.post('/api/characters',json={'id':'chinese_male_candidate','slug':'chinese_male_candidate','display_name':'不按名字推断'})
    version=client.post('/api/characters/chinese_male_candidate/versions',json={'identity':{'profile':original}}).json()
    with client.app.state.session_factory() as s:
        service=s.scalar(select(AIService).where(AIService.module=='character_generation'))
        service.settings_json={**service.settings_json,'candidate_model':'sd_xl_base_1.0.safetensors'}
        service.capabilities_json={'text_to_image':{'state':'detected','source':'isolated contract fixture'}};s.commit()
    result=client.post('/api/characters/chinese_male_candidate/batch-preview',json={'version_id':version['id'],'kind':'candidates','candidate_count':3})
    assert result.status_code==200,result.text
    plan=result.json()
    assert len(plan['jobs'])==3 and all(j['prompt'].startswith('Unretouched live-action casting photograph of one character of male gender, 38 years old.') for j in plan['jobs'])
    assert all(original['facial_features'] in j['prompt'] for j in plan['jobs'])
    assert plan['profile']['facial_features']==original['facial_features'] and plan['language_adapter']['gender']=='male'
    assert plan['warnings'] and plan['preview_hash']


def graph_job(model='flux-2-klein-4b-fp8.safetensors'):
    return SimpleNamespace(id='isolated-graph-only',prompt='One explicitly adult male, photographic casting portrait.',negative_prompt='must not become Klein negative text',seed=417,
        snapshot_json={'seed':417,'reference':None,'service_snapshot':{'provider':'comfyui','model_id':model,'settings':{'text_encoder':'explicit-test-te.safetensors','vae':'explicit-test-vae.safetensors'}},'parameters':{'width':768,'height':1024,'quality':'standard'}})


def test_klein_no_reference_candidates_use_valid_native_flux_graph():
    job=graph_job();graph=native_graph(job,None)
    assert not any(n['class_type'] in {'LoadImage','ReferenceLatent','CheckpointLoaderSimple','KSampler'} for n in graph.values())
    assert graph['70']['inputs']['unet_name']==job.snapshot_json['service_snapshot']['model_id']
    assert graph['71']['inputs']['clip_name']=='explicit-test-te.safetensors' and graph['72']['inputs']['vae_name']=='explicit-test-vae.safetensors'
    assert graph['74']['inputs']['text']==job.prompt and graph['73']['inputs']['noise_seed']==417
    assert graph['63']['inputs']['positive']==['74',0] and graph['63']['inputs']['negative']==['82',0]
    assert graph['82']['class_type']=='ConditioningZeroOut'
    assert all(graph[k]['inputs']['width']==768 and graph[k]['inputs']['height']==1024 for k in ['66','62'])
    for node in graph.values():
        for value in node['inputs'].values():
            if isinstance(value,list):assert value[0] in graph,('dangling graph reference',value)
    assert job.negative_prompt not in json.dumps(graph)


def test_scene_and_character_klein_text_graph_share_native_implementation():
    from app.services.cinematic_visuals import visual_graph
    job=graph_job();candidate=native_graph(job,None)
    visual=SimpleNamespace(id=job.id,prompt=job.prompt,snapshot_json=job.snapshot_json)
    scene=visual_graph(visual,[])
    candidate['1001']['inputs']['filename_prefix']=scene['1001']['inputs']['filename_prefix']
    assert scene==candidate


def test_sdxl_candidate_graph_remains_sdxl_and_applies_actual_negative_prompt():
    job=graph_job('sd_xl_base_1.0.safetensors');graph=native_graph(job,None)
    assert graph['1']['class_type']=='CheckpointLoaderSimple' and graph['1']['inputs']['ckpt_name']=='sd_xl_base_1.0.safetensors'
    assert graph['2']['inputs']['text']==job.prompt and graph['3']['inputs']['text']==job.negative_prompt
    assert graph['5']['class_type']=='KSampler' and graph['5']['inputs']['seed']==417
    assert graph['4']['inputs']['width']==768 and graph['4']['inputs']['height']==1024


def test_klein_candidate_plan_and_submitted_graph_metadata_are_truthful(client,settings,monkeypatch):
    from app.services import character_factory
    version=role(client)
    with client.app.state.session_factory() as s:
        service=s.scalar(select(AIService).where(AIService.module=='character_generation'))
        service.settings_json={**service.settings_json,'candidate_model':'flux-2-klein-4b-fp8.safetensors'}
        service.capabilities_json={'text_to_image':{'state':'detected'},'reference_image':{'state':'detected'}};s.commit()
    payload={'version_id':version['id'],'kind':'candidates','candidate_count':1}
    plan=client.post('/api/characters/factory_contract/batch-preview',json=payload).json()
    assert plan['jobs'][0]['negative_prompt']=='' and plan['service_snapshot']['model_id']=='flux-2-klein-4b-fp8.safetensors'
    batch=client.post('/api/characters/factory_contract/batch-generate',json={**payload,'confirm':True,'preview_hash':plan['preview_hash']}).json()
    submitted=[]
    async def transport(settings,snapshot,method,endpoint,secret='',**kwargs):
        # Contract-only transport. This must never reach the production GPU.
        if endpoint=='/queue':data={'queue_running':[],'queue_pending':[]}
        elif endpoint=='/prompt':submitted.append(kwargs['json']['prompt']);data={'prompt_id':'unit-contract-provider-id'}
        else:data={}
        return httpx.Response(200,json=data,request=httpx.Request(method,'http://127.0.0.1/isolated'))
    monkeypatch.setattr(character_factory,'service_request',transport)
    asyncio.run(process_character_jobs(settings,client.app.state.session_factory))
    with client.app.state.session_factory() as s:
        job=s.scalar(select(CharacterGenerationJob).where(CharacterGenerationJob.batch_id==batch['id']))
        assert job.status=='submitted' and len(submitted)==1
        expected=hashlib.sha256(json.dumps(submitted[0],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        assert job.snapshot_json['actual_workflow_sha256']==expected and job.snapshot_json['actual_workflow_version']=='character-klein-text2image-v1'
        assert job.snapshot_json['negative_prompt_applied'] is False
        data=io.BytesIO();Image.new('RGB',(64,64),'gray').save(data,'PNG');store_image(settings,s,job,data.getvalue());s.flush()
        from app.models import CharacterAsset
        asset=s.get(CharacterAsset,job.result_asset_id)
        assert asset.metadata_json['workflow_version']=='character-klein-text2image-v1' and asset.metadata_json['workflow_sha256']==expected
