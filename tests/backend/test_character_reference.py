"""Isolated reference contracts. Mock provider payloads do not prove GPU quality."""
import asyncio
import base64
import copy
import hashlib
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
from pathlib import Path
import threading
import uuid

import httpx
from PIL import Image
import pytest
from sqlalchemy import func, select

from app import models as m
from app.core.errors import AppError
from app.schemas_character_reference import CharacterReferenceSave
from app.schemas_product import BatchRequest
from app.schemas_screenplay_generation import ApproveScreenplay, GenerateScreenplay
from app.services import character_factory as factory_service
from app.services import character_reference as references
from app.services import screenplay_generation as screenplay
from app.services.screenplay_generation_media import upload_asset


PROFILE = {'age': 32, 'original_adult': True, 'description': 'Original fictional adult woman, practical engineer',
           'facial_features': 'oval face', 'hair': 'dark shoulder-length hair', 'eyes': 'brown',
           'fixed_traits': ['face'], 'wardrobes': [{'id': 'default', 'description': 'navy cotton work jacket'}]}


def picture(color):
    output = BytesIO()
    Image.new('RGB', (80, 96), color).save(output, 'PNG')
    return output.getvalue()


@pytest.fixture()
def project(client):
    with client.app.state.session_factory() as session:
        project = m.Project(id='ref_project', name='Isolated optional reference test')
        session.add(project)
        versions = {}
        for key in ('a', 'b'):
            role = m.Character(id='ref_role_'+key, slug='ref_role_'+key, display_name='Role '+key.upper())
            session.add(role);session.flush()
            version = m.CharacterVersion(character_id=role.id, version=1, identity_json={'profile': copy.deepcopy(PROFILE)})
            session.add(version);session.flush();versions[key] = version.id
        shot = m.StoryboardShot(project_id=project.id, order=1, scene_title='Neutral control room')
        session.add(shot);session.flush()
        for index,key in enumerate(('a', 'b')):
            session.add(m.ShotCharacter(shot_id=shot.id, character_id='ref_role_'+key,
                character_version_id=versions[key], order=index+1))
        image_service = session.scalar(select(m.AIService).where(m.AIService.module=='character_generation'))
        image_service.capabilities_json = {'text_to_image': {'state': 'detected', 'source': 'isolated unit fixture'},
                                         'reference_image': {'state': 'detected', 'source': 'isolated unit fixture'}}
        session.commit()
        return {'id': project.id, 'versions': versions, 'service_id': image_service.id}


def prefix(project, role='a'):
    return f'/api/studio/projects/{project["id"]}/character-references/ref_role_{role}'


def upload(client, project, color, *, role='a', purpose='identity_front'):
    result = client.post(prefix(project,role)+'/uploads',data={'purpose':purpose,'source_note':'Isolated generated solid-color fixture; no likeness approval'},
                         files={'file':('reference.png',picture(color),'image/png')})
    assert result.status_code==201,result.text
    return result.json()


def save(client, project, selections, revision=0, role='a'):
    return client.put(prefix(project,role),json={'expected_revision':revision,'references':selections})


def choice(asset, order, purpose=None):
    return {'asset_id':asset['id'],'purpose':purpose or asset['purpose'],'order':order}


def plan(client, project, role='a', **overrides):
    data={'version_id':project['versions'][role],'project_id':project['id'],'kind':'candidates',
          'candidate_count':1,'max_tasks':1,'width':512,'height':640,**overrides}
    result=client.post('/api/characters/ref_role_'+role+'/batch-preview',json=data)
    assert result.status_code==200,result.text
    return data,result.json()


def test_drafts_are_optional_and_four_saved_images_keep_explicit_order_and_role(client,project):
    assets=[upload(client,project,color,purpose=purpose) for color,purpose in
            [('red','identity_front'),('green','identity_side'),('blue','full_body'),('yellow','wardrobe')]]
    _,draft_plan=plan(client,project)
    assert draft_plan['references']==[] and draft_plan['project_references']==[]
    order=[assets[2],assets[0],assets[3],assets[1]]
    saved=save(client,project,[choice(asset,index+1) for index,asset in enumerate(order)])
    assert saved.status_code==200,saved.text
    _,selected=plan(client,project)
    assert [item['id'] for item in selected['references']]==[asset['id'] for asset in order]
    assert [item['purpose'] for item in selected['references']]==[asset['purpose'] for asset in order]
    assert selected['project_reference_revision']==1
    _,unprovided=plan(client,project,role='b')
    assert unprovided['reference'] is None and unprovided['references']==[]
    assert 'Use the saved project' not in unprovided['jobs'][0]['prompt']
    assert client.get('/api/studio/projects/'+project['id']+'/character-references').json()['characters'][0]['revision']==1


def test_stale_save_replacement_and_unbind_keep_files_and_frozen_plan(client,settings,project):
    first=upload(client,project,'red');second=upload(client,project,'green')
    assert save(client,project,[choice(first,1)]).status_code==200
    _,old_plan=plan(client,project)
    stale=save(client,project,[choice(second,1)],revision=0)
    assert stale.status_code==409 and stale.json()['error']['code']=='character_reference_revision_changed'
    assert save(client,project,[choice(second,1)],revision=1).status_code==200
    assert old_plan['project_references'][0]['id']==first['id']
    assert save(client,project,[],revision=2).status_code==200
    with client.app.state.session_factory() as session:
        for asset in (first,second):
            row=session.get(m.CharacterReferenceAsset,asset['id'])
            assert row.state=='unbound' and (settings.media_root/row.relative_path).is_file()
            assert hashlib.sha256((settings.media_root/row.relative_path).read_bytes()).hexdigest()==asset['sha256']
    _,cleared=plan(client,project)
    assert cleared['references']==[]


def test_wrong_role_and_same_content_are_rejected_without_changing_bindings(client,project):
    first=upload(client,project,'red')
    second=upload(client,project,'red',role='b')
    wrong=save(client,project,[choice(first,1)],role='b')
    assert wrong.status_code==422 and wrong.json()['error']['code']=='character_reference_ownership'
    assert save(client,project,[choice(first,1)]).status_code==200
    duplicate=save(client,project,[choice(second,1)],role='b')
    assert duplicate.status_code==409 and duplicate.json()['error']['code']=='character_reference_identity_conflict'
    overview=client.get('/api/studio/projects/'+project['id']+'/character-references').json()
    assert [len(item['references']) for item in overview['characters']]==[1,0]


def test_concurrent_saves_cannot_apply_one_photo_to_two_different_roles(client,project):
    a=upload(client,project,'magenta');b=upload(client,project,'magenta',role='b')
    barrier=threading.Barrier(2)
    def submit(role,asset):
        barrier.wait(timeout=10)
        return save(client,project,[choice(asset,1)],role=role).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        one=pool.submit(submit,'a',a);two=pool.submit(submit,'b',b)
        assert sorted([one.result(timeout=20),two.result(timeout=20)])==[200,409]
    overview=client.get('/api/studio/projects/'+project['id']+'/character-references').json()
    assert sum(len(item['references']) for item in overview['characters'])==1


def test_concurrent_same_role_saves_use_revision_compare_and_swap(client,project):
    original=upload(client,project,'red');one=upload(client,project,'green');two=upload(client,project,'blue')
    assert save(client,project,[choice(original,1)]).status_code==200
    barrier=threading.Barrier(2)
    def submit(asset):
        barrier.wait(timeout=10)
        result=save(client,project,[choice(asset,1)],revision=1)
        return result.status_code,asset['id']
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(submit,one);second=pool.submit(submit,two)
        outcomes=[first.result(timeout=20),second.result(timeout=20)]
    assert sorted(status for status,_ in outcomes)==[200,409]
    winner=next(identifier for status,identifier in outcomes if status==200)
    overview=client.get('/api/studio/projects/'+project['id']+'/character-references').json()
    role=next(item for item in overview['characters'] if item['character_id']=='ref_role_a')
    assert role['revision']==2 and [asset['id'] for asset in role['references']]==[winner]


def test_reference_content_change_is_rejected_before_dispatch(client,settings,project):
    asset=upload(client,project,'red')
    assert save(client,project,[choice(asset,1)]).status_code==200
    with client.app.state.session_factory() as session:
        relative=session.get(m.CharacterReferenceAsset,asset['id']).relative_path
    (settings.media_root/relative).write_bytes(picture('green'))
    result=client.post('/api/characters/ref_role_a/batch-preview',json={'project_id':project['id'],
        'version_id':project['versions']['a'],'kind':'candidates','candidate_count':1})
    assert result.status_code==409 and result.json()['error']['code']=='character_reference_changed'


@pytest.mark.parametrize('provider_format',['comfyui','comfyui_four','json','multipart'])
def test_anchor_plus_saved_references_reach_every_actual_request_input(client,settings,project,monkeypatch,tmp_path,provider_format):
    assets=[upload(client,project,'green',purpose='identity_side'),upload(client,project,'blue',purpose='wardrobe')]
    ordering=[assets[1],assets[0]]
    if provider_format=='comfyui_four':
        assets.append(upload(client,project,'orange',purpose='full_body'));ordering.append(assets[2])
    assert save(client,project,[choice(asset,index+1) for index,asset in enumerate(ordering)]).status_code==200
    anchor_data=picture('red')
    with client.app.state.session_factory() as session:
        relative='unit_anchor/approved.png';path=settings.media_root/relative;path.parent.mkdir(parents=True);path.write_bytes(anchor_data)
        anchor=m.CharacterAsset(character_version_id=project['versions']['a'],asset_type='front_face',relative_path=relative,
            mime_type='image/png',width=80,height=96,sha256=hashlib.sha256(anchor_data).hexdigest(),status='approved',source='unit_contract')
        session.add(anchor);session.flush();session.get(m.CharacterVersion,project['versions']['a']).standard_face_asset_id=anchor.id
        service=session.get(m.AIService,project['service_id'])
        if not provider_format.startswith('comfyui'):
            service.provider='openai_compatible';service.base_url='https://isolated.invalid/v1';service.default_model='unit-image-editor'
            service.settings_json={**service.settings_json,'image_api_format':provider_format}
            service.capabilities_json={'reference_image':{'state':'verified','model_id':service.default_model,'max_reference_images':4,'source':'isolated unit fixture'}}
        session.commit();anchor_id=anchor.id
    data,preview=plan(client,project,kind='references',types=['half_body'],count_per_type=1)
    expected=[anchor_id]+[asset['id'] for asset in ordering]
    assert [item['id'] for item in preview['references']]==expected
    created=client.post('/api/characters/ref_role_a/batch-generate',json={**data,'confirm':True,'preview_hash':preview['preview_hash']})
    assert created.status_code==202,created.text
    batch=created.json()
    fake_root=tmp_path/'isolated_runtime';(fake_root/'workflows').mkdir(parents=True)
    real_root=Path(__file__).resolve().parents[2]
    (fake_root/'workflows/character_klein_reference_v1.json').write_bytes((real_root/'workflows/character_klein_reference_v1.json').read_bytes())
    monkeypatch.setattr(factory_service,'PROJECT_ROOT',fake_root)
    uploads=[];submitted=[];edits=[]
    async def transport(settings,snapshot,method,endpoint,secret='',**kwargs):
        if endpoint=='/queue':body={'queue_running':[],'queue_pending':[]}
        elif endpoint=='/upload/image':
            uploads.append(kwargs['files']['image']);body={'name':f'unit_{len(uploads)}.png','subfolder':'unit_contract'}
        elif endpoint=='/prompt':submitted.append(kwargs['json']['prompt']);body={'prompt_id':'isolated-provider-job'}
        elif endpoint=='/images/edits':
            edits.append(kwargs);body={'data':[{'b64_json':base64.b64encode(picture('gray')).decode()}]}
        elif endpoint=='/free':body={}
        else:raise AssertionError('Unmocked provider endpoint '+endpoint)
        return httpx.Response(200,json=body,request=httpx.Request(method,'https://isolated.invalid'+endpoint))
    monkeypatch.setattr(factory_service,'service_request',transport)
    asyncio.run(factory_service.process_character_jobs(settings,client.app.state.session_factory))
    with client.app.state.session_factory() as session:
        job=session.scalar(select(m.CharacterGenerationJob).where(m.CharacterGenerationJob.batch_id==batch['id']))
        assert [item['id'] for item in job.snapshot_json['actual_reference_inputs']]==expected
        assert [item['purpose'] for item in job.snapshot_json['actual_reference_inputs']]==['approved_identity']+[asset['purpose'] for asset in ordering]
        if provider_format.startswith('comfyui'):
            assert job.status=='submitted' and len(uploads)==len(expected) and len(submitted)==1
            actual_images=[node['inputs']['image'] for node in submitted[0].values() if node['class_type']=='LoadImage']
            assert actual_images==[f'unit_contract/unit_{index+1}.png' for index in range(len(expected))]
            assert [hashlib.sha256(item[1]).hexdigest() for item in uploads]==[item['sha256'] for item in preview['references']]
        else:
            assert job.status=='needs_review' and len(edits)==1
            if provider_format=='json':
                actual=edits[0]['json']['images'];assert len(actual)==3
                assert [hashlib.sha256(base64.b64decode(item['image_url'].split(',',1)[1])).hexdigest() for item in actual]==[item['sha256'] for item in preview['references']]
            else:
                actual=edits[0]['files'];assert [name for name,_ in actual]==['image[]']*3
                assert [hashlib.sha256(item[1]).hexdigest() for _,item in actual]==[item['sha256'] for item in preview['references']]


def _screenplay_raw():
    return {'project':{'synopsis':'Two adults verify a closed record book.','style':'Live action, practical lighting.','aspect_ratio':'16:9'},
        'characters':[{'key':key,'name':'New '+key,'profile':{'age':32,'description':'Original fictional adult engineer','wardrobe':'navy jacket'}} for key in ('A','B')],
        'scenes':[{'key':'S','name':'Control room','order':1,'environment':{'location':'Interior station'}}],
        'shots':[{'key':'T','scene_key':'S','title':'Check record','visual_prompt':'Two adults beside a closed record book.',
                  'duration_seconds':5,'participants':[{'character_key':key,'performance':{'action':'looks at closed record'}} for key in ('A','B')],
                  'dialogues':[{'character_key':'A','text':'Record checked.'},{'character_key':'B','text':'Confirmed.'}],
                  'photography':{'shot_size':'medium two shot'}}]}


def test_image_screenplay_review_adopts_source_for_only_explicit_role(client,settings,project):
    with client.app.state.session_factory() as session:
        service=m.AIService(module='language_processing',provider='local_llm',name='Unit language fixture',
            base_url='http://127.0.0.1:8771/v1',default_model='unit-language',models_json=[{'id':'unit-language'}])
        session.add(service);session.commit()
        asset=upload_asset(session,settings,'image','neutral.png',picture('cyan'),'Isolated source fixture')
        request=GenerateScreenplay(project_name='Image adoption '+uuid.uuid4().hex,creative_prompt='Two original adult engineers.',
            input_mode='image',asset_id=asset.id,service_id=service.id,idempotency_key='test-'+uuid.uuid4().hex)
        job=screenplay.create_generation(session,settings,request,capability_test=True)
        job.screenplay_json=screenplay.normalize_ir(session,job,_screenplay_raw(),{})
        job.receipt_json={'source':{'source_sha256':asset.sha256}};session.commit()
        document=screenplay.export_job(session,settings,job)
        result=screenplay.approve(session,settings,job.id,ApproveScreenplay(expected_revision=job.revision,document_id=document.id,
            approve=True,create_project=True,reference_targets=[{'asset_id':asset.id,'target_kind':'character','target_key':'A','purpose':'identity_front'}]))
        script=session.scalar(select(m.ScriptRevision).where(m.ScriptRevision.project_id==result.project_id))
        bindings=script.parsed_json['identity_bindings']
        selected,_=references.saved_reference_snapshot(session,settings,result.project_id,bindings['A']['character_id'])
        omitted,_=references.saved_reference_snapshot(session,settings,result.project_id,bindings['B']['character_id'])
        assert len(selected)==1 and selected[0]['sha256']==asset.sha256 and omitted==[]
        assert selected[0]['relative_path']!=asset.relative_path
        assert hashlib.sha256((settings.media_root/asset.relative_path).read_bytes()).hexdigest()==asset.sha256
        assert not session.scalars(select(m.CharacterReferenceAsset).where(m.CharacterReferenceAsset.project_id==project['id'])).all()


def test_adoption_rejects_changed_source_and_foreign_target(client,settings,project):
    with client.app.state.session_factory() as session:
        asset=upload_asset(session,settings,'image','source.png',picture('cyan'),'isolated source')
        with pytest.raises(AppError) as wrong:references.adopt_source_reference(session,settings,project['id'],'foreign_role',asset.relative_path,asset.sha256)
        assert wrong.value.code=='character_reference_ownership'
        with pytest.raises(AppError) as changed:references.adopt_source_reference(session,settings,project['id'],'ref_role_a',asset.relative_path,'0'*64)
        assert changed.value.code=='character_reference_changed'
        session.rollback()
