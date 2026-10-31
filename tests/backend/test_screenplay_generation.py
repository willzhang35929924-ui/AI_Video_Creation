import asyncio
import copy
from io import BytesIO
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import uuid
import httpx
from PIL import Image
from docx import Document
import pytest
from sqlalchemy import select,func

from app import models as m
from app.core.db import Base,build_engine,build_session_factory
from app.core.errors import AppError
from app.models.screenplay_generation import ScreenplayGenerationJob as Job, ScreenplayDocument as Doc
from app.schemas_screenplay_generation import GenerateScreenplay,ApproveScreenplay,ReviseScreenplay,RetryScreenplay,CancelScreenplay,ResumeScreenplay
from app.services import screenplay_generation as service
from app.services.screenplay_generation_media import upload_asset


@pytest.fixture()
def factory(settings):
    engine=build_engine(settings.database_url);Base.metadata.create_all(engine)
    return build_session_factory(engine)

@pytest.fixture()
def model(factory):
    with factory() as session:
        row=m.AIService(id='svc_screenplay_test',module='language_processing',provider='local_llm',name='Local test model',
            base_url='http://127.0.0.1:8771/v1',default_model='Qwen/Qwen3-VL-2B-Instruct',enabled=True,
            models_json=[{'id':'Qwen/Qwen3-VL-2B-Instruct'}],settings_json={'timeout':600,'max_tokens':8192})
        session.add(row);session.commit();return row.id

def payload(model,**changes):
    return GenerateScreenplay.model_validate({'project_name':'  保留名称空格  ','creative_prompt':'原创成年人，现实电影观感，两人核对记录。',
        'input_mode':'text','story_text':'两名值班工程师核对设备记录。','service_id':model,'idempotency_key':'unit-'+uuid.uuid4().hex,**changes})

def raw_ir():
    return {'project':{'synopsis':'两名工程师依次确认记录。','style':'真人实拍，自然肤质和工作灯。','genre':'职业短片','aspect_ratio':'16:9','target_duration':'10秒','story_body':'核对后交接。'},
        'characters':[{'key':key,'name':name,'profile':{'age':32+i,'description':name+'，成年值班工程师','hair':'短黑发','wardrobe':'深色工作服'}} for i,(key,name) in enumerate([('A','周宁'),('B','方舟')])],
        'scenes':[{'key':'S1','name':'控制室','order':1,'environment':{'location':'监测站','lighting':'顶部工作灯','layout':'两人隔桌站立'},'audio_events':[{'kind':'ambience','description':'设备低鸣'}]}],
        'shots':[{'key':f'T{i}','scene_key':'S1','title':title,'duration_seconds':5,'visual_prompt':'两名成年人在磨损桌旁核对合上的记录本。','action':'核对记录','camera_move':'固定',
            'participants':[{'character_key':'A','performance':{'action':'手扶合上的记录本','position':'左侧'}},{'character_key':'B','performance':{'action':'轻轻点头','position':'右侧'}}],
            'dialogues':[{'character_key':'A','text':line},{'character_key':'B','text':'收到。'}],
            'photography':{'shot_size':'双人中景','camera_position':'眼平'},'production':{'start_state':'本子合上','end_state':'本子仍合上','beats':[{'character_key':'A','start_seconds':0,'end_seconds':2,'action':'看向记录本'}]},
            'audio_events':[{'kind':'sfx','description':'轻微衣料摩擦'}]} for i,title,line in [(1,'核对','原始记录已经核对完成。'),(2,'确认','按顺序继续。')]],
        'designs':[{'key':'W1','kind':'wardrobe','name':'周宁工作服','character_key':'A','spec':{'材质':'哑光棉布'}}],
        'observations':[],'uncertainties':['设备读数尚未提供。']}

def make_ready(factory,settings,model,*,name='  保留名称空格  ',capability_test=False):
    with factory() as session:
        job=service.create_generation(session,settings,payload(model,project_name=name),capability_test=True)
        job.capability_test=capability_test
        job.screenplay_json=service.normalize_ir(session,job,raw_ir(),{})
        job.receipt_json={'provider_response_received':True,'provider_output_sha256':'a'*64,'validated_ir_sha256':service.content_sha(job.screenplay_json),'source':{}}
        session.commit();service.export_job(session,settings,job)
        return job.id


def test_provider_plain_wardrobe_and_prop_descriptions_keep_their_words(factory,settings,model):
    with factory() as session:
        job=service.create_generation(session,settings,payload(model),capability_test=True)
        raw=raw_ir();raw['shots'][0]['production']['wardrobe_states']={'A':'深蓝工作服'}
        raw['shots'][0]['production']['prop_states']=['合上的记录本']
        result=service.normalize_ir(session,job,raw,{})
        state=result['shots'][0]['production']['wardrobe_states']['A']
        assert state['description']=='深蓝工作服' and state['wardrobe_key']=='W1'
        prop=result['shots'][0]['production']['prop_states'][0]
        assert prop['description']=='合上的记录本' and prop['prop_key']
        assert raw['shots'][0]['production']['prop_states']==['合上的记录本']

@pytest.mark.parametrize('placeholder',['按真实参考图与用户要求描述画面','按用户要求描写'])
def test_model_template_placeholder_is_not_a_production_ready_shot(factory,settings,model,placeholder):
    with factory() as session:
        job=service.create_generation(session,settings,payload(model),capability_test=True)
        raw=raw_ir();raw['shots'][0]['visual_prompt']=placeholder
        with pytest.raises(ValueError,match='照抄模板占位'):service.normalize_ir(session,job,raw,{})

def test_product_design_kind_is_corrected_only_for_the_exact_frozen_product(factory,settings,model):
    with factory() as session:
        job=service.create_generation(session,settings,payload(model),capability_test=True)
        job.snapshot_json={**job.snapshot_json,'products':[{'id':'product_explicit','name':'Known product','images':[]}]}
        raw=raw_ir();raw['characters']=[]
        for shot in raw['shots']:
            shot['participants']=[];shot['dialogues']=[];shot['production']['beats']=[]
            shot['products']=[{'product_id':'product_explicit','visible_state':'fully visible'}]
        raw['designs']=[{'key':'P1','kind':'wardrobe','name':'Known product','spec':{'product_id':'product_explicit','description':'original provider prose'}}]
        result=service.normalize_ir(session,job,raw,{})
        design=result['designs'][0]
        assert design['kind']=='prop' and design['model_original_kind']=='wardrobe'
        assert design['spec']['description']=='original provider prose'
        assert raw['designs'][0]['kind']=='wardrobe'
        assert any(w['kind']=='product_design_classification' for w in result['parse_warnings'])
        raw['designs'][0]['spec']['product_id']='unselected_product'
        with pytest.raises(ValueError,match='不明内容'):service.normalize_ir(session,job,raw,{})


def test_provider_plain_profile_lists_and_misplaced_production_fields_are_preserved(factory,settings,model):
    with factory() as session:
        job=service.create_generation(session,settings,payload(model),capability_test=True)
        raw=raw_ir();raw['characters'][0]['profile'].update(relationships=[],immutable_traits=['短黑发','深棕眼睛'])
        raw['shots'][0]['wardrobe_states']={'A':{'description':'深蓝工作服'}}
        raw['shots'][0]['prop_states']=[{'name':'记录本','state':'合上'}]
        result=service.normalize_ir(session,job,raw,{})
        assert result['characters'][0]['profile']['relationships']==''
        assert result['characters'][0]['profile']['immutable_traits']=='短黑发；深棕眼睛'
        assert result['shots'][0]['production']['prop_states'][0]['name']=='记录本'
        assert result['shots'][0]['production']['prop_states'][0]['state']=='合上'
        assert result['shots'][0]['production']['wardrobe_states']['A']['description']=='深蓝工作服'
        assert 'prop_states' not in result['shots'][0]
        raw['characters'][0]['profile']['relationships']=[{'person':'B'}]
        with pytest.raises(ValueError,match='人物资料只能'):service.normalize_ir(session,job,raw,{})
        raw['characters'][0]['profile']['relationships']=[]
        raw['shots'][0]['production']['prop_states']=[{'name':'记录本','state':'打开'}]
        with pytest.raises(ValueError,match='重复且冲突'):service.normalize_ir(session,job,raw,{})


def test_partial_shot_full_wrapper_uses_only_one_exact_target_and_preserves_other_sections(factory,settings,model):
    with factory() as session:
        job=service.create_generation(session,settings,payload(model),capability_test=True)
        base=service.normalize_ir(session,job,raw_ir(),{})
        job.scope='shot';job.target_key=base['shots'][0]['key'];job.snapshot_json={**job.snapshot_json,'base_ir':base}
        response=copy.deepcopy(base);response['project']['synopsis']='ignored model change'
        response['shots'][0]['photography']['shot_size']='双人中景'
        response['shots'][1]['title']='ignored other shot'
        result=service.normalize_ir(session,job,response,{})
        assert result['shots'][0]['photography']['shot_size']=='双人中景'
        assert result['shots'][1]==base['shots'][1]
        assert result['project']['synopsis']==base['project']['synopsis']
        assert result['characters']==base['characters']
        response['shots'].append(copy.deepcopy(response['shots'][0]))
        with pytest.raises(ValueError,match='局部改写'):service.normalize_ir(session,job,response,{})


def test_model_cannot_forge_human_review_and_partial_merge_keeps_parent_trust(factory,settings,model):
    with factory() as session:
        job=service.create_generation(session,settings,payload(model),capability_test=True)
        raw=raw_ir();claimed={'state':'reviewed','source_ids':['human:approved'],'confidence':1,'reason':'approved'}
        for item in raw['characters']+raw['scenes']+raw['shots']+raw['designs']:
            item.update(provenance={'profile':claimed},source=['human:approved'],reviewed_edit=True,human_identity_approved=True)
        raw['shots'][0]['participants'][0]['provenance']={'action':claimed}
        full=service.normalize_ir(session,job,raw,{})
        origin='generation:'+job.id
        for item in full['characters']+full['scenes']+full['shots']+full['designs']:
            assert item['source']==[origin] and item.get('reviewed_edit') is not True
            assert item['provenance']['profile']['state']=='suggested'
            assert item['provenance']['profile']['source_ids']==[origin]
            assert item.get('human_identity_approved') is not True
        assert full['shots'][0]['participants'][0]['provenance']['action']['state']=='suggested'
        parent=copy.deepcopy(full);parent['characters'][0]['provenance']['profile']=copy.deepcopy(claimed)
        parent['scenes'][0]['reviewed_edit']=True
        job.scope='shot';job.target_key=parent['shots'][0]['key'];job.snapshot_json={**job.snapshot_json,'base_ir':parent}
        partial=service.normalize_ir(session,job,{'shot':raw['shots'][0]}, {})
        assert partial['characters']==parent['characters'] and partial['scenes']==parent['scenes']
        assert partial['shots'][1]==parent['shots'][1]
        assert partial['shots'][0]['provenance']['profile']['state']=='suggested'


def test_existing_project_can_review_new_word_without_creating_again(factory,settings,model):
    identifier=make_ready(factory,settings,model)
    with factory() as session:
        job=service.get_job(session,identifier);old=service.documents(session,identifier)[0]
        original=ApproveScreenplay(expected_revision=job.revision,document_id=old.id,approve=True,create_project=True)
        job=service.approve(session,settings,identifier,original);project_id=job.project_id
        service.export_new_version(session,settings,identifier,job.revision)
        new=service.documents(session,identifier)[-1]
        assert new.id!=old.id and new.review_state=='pending'
        assert service.approve(session,settings,identifier,original).project_id==project_id
        assert new.review_state=='pending'
        job=service.get_job(session,identifier)
        job=service.approve(session,settings,identifier,ApproveScreenplay(expected_revision=job.revision,document_id=new.id,approve=True,create_project=True))
        assert job.project_id==project_id and job.status=='approved' and new.review_state=='approved'
        assert session.scalar(select(func.count()).select_from(m.Project))==1


def test_running_screenplay_blocks_service_deletion(factory,settings,model):
    from app.services.content_manager import build_plan
    with factory() as session:
        job=service.create_generation(session,settings,payload(model),capability_test=True)
        plan=build_plan(session,settings,'services',[model],'keep',cascade=True)
        assert any(item.get('id')==job.id and '运行任务' in item['reason'] for item in plan['blockers'])


def test_created_project_deletion_preview_handles_name_claim_and_keeps_source_word(factory,settings,model):
    from app.services.content_manager import build_plan
    identifier=make_ready(factory,settings,model,name='仅预览删除的中性项目')
    with factory() as session:
        job=service.get_job(session,identifier);document=service.documents(session,identifier)[0]
        job=service.approve(session,settings,identifier,ApproveScreenplay(expected_revision=job.revision,document_id=document.id,approve=True,create_project=True))
        plan=build_plan(session,settings,'projects',[job.project_id],'keep',cascade=True)
        assert any(item['table']=='screenplay_project_name_claims' for item in plan['records'])
        assert any(item['table']=='screenplay_generation_jobs' for item in plan['records'])
        assert (settings.media_root/document.relative_path).is_file()


def test_text_only_has_no_visual_observation_and_duration_conflict_is_visible(factory,settings,model):
    with factory() as session:
        job=service.create_generation(session,settings,payload(model),capability_test=True)
        raw=raw_ir();raw['project']['target_duration']='1500';raw['observations']=['画面中出现两位人物']
        raw['uncertainties']=['原视频里的人物姓名需核对']
        ir=service.normalize_ir(session,job,raw,{})
        assert ir['observations']==[] and ir['creative_suggestions']==raw['observations']
        assert ir['project']['shot_total_duration_seconds']==10 and ir['project']['duration_conflict']
        assert any('仅有文字输入' in item for item in ir['uncertainties'])


def test_source_image_format_is_rejected_before_late_reference_adoption(factory,settings):
    data=BytesIO();Image.new('RGB',(64,64),'gray').save(data,format='JPEG')
    with factory() as session:
        with pytest.raises(AppError) as error:upload_asset(session,settings,'image','wrong.png',data.getvalue())
        assert error.value.code=='screenplay_image_decode'


def test_adopt_scene_reference_uses_source_key_after_identical_scenes_merge(factory,settings,model):
    from app.schemas_screenplay_generation import ReferenceTarget
    data=BytesIO();Image.new('RGB',(64,64),'gray').save(data,format='PNG')
    with factory() as session:
        asset=upload_asset(session,settings,'image','source.png',data.getvalue())
        job=service.create_generation(session,settings,payload(model,input_mode='image',story_text='',asset_id=asset.id),capability_test=True)
        raw=raw_ir();second=copy.deepcopy(raw['scenes'][0]);second.update(key='S2',order=2)
        third=copy.deepcopy(raw['scenes'][0]);third.update(key='S3',order=3,name='门口');third['environment']['location']='室外雨棚'
        raw['scenes']=[raw['scenes'][0],second,third];raw['shots'][1]['scene_key']='S2'
        last=copy.deepcopy(raw['shots'][1]);last.update(key='T3',scene_key='S3');raw['shots'].append(last)
        job.screenplay_json=service.normalize_ir(session,job,raw,{})
        job.receipt_json={'source':{}};session.commit();service.export_job(session,settings,job)
        document=service.documents(session,job.id)[0]
        job=service.approve(session,settings,job.id,ApproveScreenplay(expected_revision=job.revision,document_id=document.id,
            approve=True,create_project=True,reference_targets=[ReferenceTarget(asset_id=asset.id,target_kind='scene',target_key='S2',purpose='scene_reference')]))
        batch=session.get(m.WordImport,job.word_import_id);mapping=batch.result_json['scene_bindings']
        assert mapping['S1']==mapping['S2'] and mapping['S2']!=mapping['S3']
        visual=session.scalar(select(m.ProductionVisual).where(m.ProductionVisual.project_id==job.project_id))
        assert visual.scene_id==mapping['S2']

def test_exact_name_mutually_exclusive_input_and_no_hidden_fallback(model):
    request=payload(model)
    assert request.project_name=='  保留名称空格  '
    for changes in [{'project_name':'  '},{'creative_prompt':'\n'},{'asset_id':'input_fake'},
                    {'input_mode':'image','asset_id':'img','story_text':'mixed'},
                    {'input_mode':'video','story_text':''},{'allow_paid_fallback':True},{'project_name':'bad\nname'}]:
        with pytest.raises(ValueError):payload(model,**changes)

def test_capability_bootstrap_is_distinct_and_idempotent(factory,settings,model):
    with factory() as session:
        request=payload(model)
        with pytest.raises(AppError) as blocked:service.create_generation(session,settings,request)
        assert blocked.value.code=='screenplay_capability_unverified'
        first=service.create_generation(session,settings,request,capability_test=True)
        same=service.create_generation(session,settings,request,capability_test=True)
        assert first.id==same.id and first.status=='queued'
        assert session.scalar(select(func.count()).select_from(m.Project))==0
        with pytest.raises(AppError):service.create_generation(session,settings,payload(model,idempotency_key=request.idempotency_key,story_text='different'),capability_test=True)

def test_real_docx_preview_and_review_without_project_then_idempotent_create(factory,settings,model):
    identifier=make_ready(factory,settings,model)
    with factory() as session:
        job=service.get_job(session,identifier);document=service.documents(session,identifier)[0]
        actual=Document(settings.media_root/document.relative_path)
        text='\n'.join(p.text for p in actual.paragraphs)
        assert actual.paragraphs[0].text==job.project_name
        assert '对白：周宁：原始记录已经核对完成。' in text
        assert '表演：方舟｜动作=轻轻点头' in text and '引用场景编号：S1' in text
        original=job.revision
        result=service.approve(session,settings,identifier,ApproveScreenplay(expected_revision=original,document_id=document.id,approve=True))
        assert result.status=='approved' and result.project_id is None
        assert session.scalar(select(func.count()).select_from(m.Project))==0
        request=ApproveScreenplay(expected_revision=result.revision,document_id=document.id,approve=True,create_project=True)
        created=service.approve(session,settings,identifier,request)
        assert session.get(m.Project,created.project_id).name=='  保留名称空格  '
        assert session.scalar(select(func.count()).select_from(m.ShotCharacter))==4
        assert session.scalar(select(func.count()).select_from(m.DialogueLine))==4
        assert service.approve(session,settings,identifier,request).project_id==created.project_id
        assert session.scalar(select(func.count()).select_from(m.Project))==1

def test_conflicting_project_name_rolls_back_without_losing_word(factory,settings,model):
    identifier=make_ready(factory,settings,model,name='重名项目')
    with factory() as session:
        session.add(m.Project(name='重名项目'));session.commit()
        job=service.get_job(session,identifier);doc=service.documents(session,identifier)[0];old=job.revision
        with pytest.raises(AppError) as error:service.approve(session,settings,identifier,ApproveScreenplay(expected_revision=old,document_id=doc.id,approve=True,create_project=True))
        assert error.value.code=='screenplay_project_name_conflict'
        job=service.get_job(session,identifier)
        assert job.project_id is None and job.revision==old and job.screenplay_json
        assert (settings.media_root/doc.relative_path).is_file()
        assert session.scalar(select(func.count()).select_from(m.Project))==1

def test_capability_test_only_creates_project_after_explicit_document_review_and_ownership(factory,settings,model):
    a=make_ready(factory,settings,model,capability_test=True);b=make_ready(factory,settings,model)
    with factory() as session:
        job=service.get_job(session,a);own=service.documents(session,a)[0];foreign=service.documents(session,b)[0]
        assert job.project_id is None and session.scalar(select(func.count()).select_from(m.Project))==0
        with pytest.raises(AppError) as error:service.approve(session,settings,a,ApproveScreenplay(expected_revision=job.revision,document_id=own.id,approve=False,create_project=True))
        assert error.value.code=='screenplay_review_required'
        with pytest.raises(AppError) as error:service.approve(session,settings,a,ApproveScreenplay(expected_revision=job.revision,document_id=foreign.id,approve=True))
        assert error.value.code=='screenplay_document_ownership'
        reviewed=service.approve(session,settings,a,ApproveScreenplay(expected_revision=job.revision,document_id=own.id,approve=True,create_project=True))
        assert reviewed.project_id and reviewed.capability_test is True
        assert session.scalar(select(func.count()).select_from(m.Project))==1

def test_ir_rejects_unowned_dialogue_duplicate_shot_and_out_of_range_beat(factory,settings,model):
    with factory() as session:
        job=service.create_generation(session,settings,payload(model),capability_test=True)
        for change in ['speaker','duplicate','beat','audio','foreign_identity']:
            raw=raw_ir()
            if change=='speaker':raw['shots'][0]['dialogues'][0]['character_key']='unknown'
            elif change=='duplicate':raw['shots'][1]['key']=raw['shots'][0]['key']
            elif change=='beat':raw['shots'][0]['production']['beats'][0]['end_seconds']=20
            elif change=='audio':raw['shots'][0]['audio_events'][0]['relative_path']='elsewhere.wav'
            else:
                raw['characters'][0]['decision']='existing';raw['characters'][0]['match_character_id']='user_existing'
                raw['characters'][0]['profile']['character_id']='user_existing'
                normalized=service.normalize_ir(session,job,raw,{})
                assert normalized['characters'][0]['decision']=='new'
                assert normalized['characters'][0]['profile']['character_id'].startswith('spchar_')
                continue
            with pytest.raises(ValueError):service.normalize_ir(session,job,raw,{})

def test_shot_revision_preserves_all_other_content_and_stable_character_ids(factory,settings,model):
    identifier=make_ready(factory,settings,model)
    with factory() as session:
        parent=service.get_job(session,identifier)
        changed=service.revise(session,settings,identifier,ReviseScreenplay(expected_revision=parent.revision,instruction='只调整第一镜的一个动作',scope='shot',target_key='T1',idempotency_key='revise-'+uuid.uuid4().hex))
        replacement=copy.deepcopy(parent.screenplay_json['shots'][0]);replacement['action']='抬眼看向同事'
        ir=service.normalize_ir(session,changed,{'shot':replacement},{})
        assert ir['shots'][1]==parent.screenplay_json['shots'][1]
        assert ir['characters']==parent.screenplay_json['characters']
        assert ir['scenes']==parent.screenplay_json['scenes']
        assert ir['shots'][0]['action']=='抬眼看向同事'
        assert parent.screenplay_json['shots'][0]['action']=='核对记录'

def test_source_image_signature_mode_ownership_and_draft_is_persistent(factory,settings,model):
    with factory() as session:
        image=Image.new('RGB',(128,160),'gray');out=BytesIO();image.save(out,format='PNG')
        asset=upload_asset(session,settings,'image','reference.png',out.getvalue(),'Original test image')
        assert asset.metadata_json['width']==128 and (settings.media_root/asset.relative_path).is_file()
        with pytest.raises(AppError):upload_asset(session,settings,'image','wrong.png',b'not a PNG')
        with pytest.raises(AppError):upload_asset(session,settings,'image','movie.mp4',out.getvalue())
        with pytest.raises(AppError):service.create_generation(session,settings,payload(model,input_mode='video',story_text='',asset_id=asset.id),capability_test=True)
        assert session.scalar(select(func.count()).select_from(m.Project))==0

def test_evidence_requires_actual_receipt_same_revision_mode_ir_and_output_sha(factory,settings,model):
    identifier=make_ready(factory,settings,model,capability_test=True)
    with factory() as session:
        row=session.get(m.AIService,model);job=service.get_job(session,identifier)
        assert service.screenplay_evidence(session,settings,row,row.default_model,'screenplay_from_text')
        assert service.screenplay_evidence(session,settings,row,row.default_model,'screenplay_from_image') is None
        row.revision+=1
        assert service.screenplay_evidence(session,settings,row,row.default_model,'screenplay_from_text') is None
        row.revision-=1
        doc=service.documents(session,identifier)[0];(settings.media_root/doc.relative_path).write_bytes(b'changed')
        assert service.screenplay_evidence(session,settings,row,row.default_model,'screenplay_from_text') is None

def test_untranscribed_audio_is_explicitly_pending_confirmation(factory,settings,model):
    with factory() as session:
        job=service.create_generation(session,settings,payload(model),capability_test=True)
        ir=service.normalize_ir(session,job,raw_ir(),{'audio_transcription':'not_configured'})
        assert any('尚未转写' in value for value in ir['uncertainties'])
        assert any(w['kind']=='untranscribed_source_audio' for w in ir['parse_warnings'])

def test_real_export_failure_retries_saved_ir_without_calling_model(factory,settings,model,monkeypatch):
    identifier=make_ready(factory,settings,model)
    with factory() as session:
        job=service.get_job(session,identifier);job.status='export_failed';job.stage='failed';session.commit()
        before=job.receipt_json.copy()
        async def never(*args,**kwargs):raise AssertionError('Retry must not call a model')
        monkeypatch.setattr(service,'service_request',never)
        result=service.retry_export(session,settings,identifier,job.revision)
        assert result.status=='awaiting_review' and result.receipt_json==before
        assert len(service.documents(session,identifier))==2

def test_worker_calls_actual_adapter_contract_and_exports_docx(factory,settings,model,monkeypatch):
    seen=[]
    async def ensure(snapshot):seen.append(('ensure',snapshot['model_id']))
    monkeypatch.setitem(sys.modules,'app.services.screenplay_runtime',SimpleNamespace(ensure_local_service=ensure))
    async def call(settings,snapshot,method,endpoint,secret='',**kwargs):
        seen.append((method,endpoint,kwargs['json']))
        return httpx.Response(200,json={'id':'unit-response','choices':[{'message':{'content':json.dumps(raw_ir(),ensure_ascii=False)}}],
            'usage':{'completion_tokens':100}},request=httpx.Request('POST','http://127.0.0.1:8771/v1/chat/completions'))
    monkeypatch.setattr(service,'service_request',call)
    with factory() as session:job=service.create_generation(session,settings,payload(model),capability_test=True);identifier=job.id
    asyncio.run(service.process_job(settings,factory,identifier))
    with factory() as session:
        job=service.get_job(session,identifier)
        assert job.status=='awaiting_review' and len(service.documents(session,identifier))==1
        assert job.receipt_json['provider_response_received'] and job.receipt_json['validated_ir_sha256']
        response_file=settings.media_root/job.receipt_json['provider_response_relative_path']
        assert service.sha(response_file.read_bytes())==job.receipt_json['provider_response_sha256']
        assert json.loads(response_file.read_text('utf-8'))['id']=='unit-response'
        assert session.scalar(select(func.count()).select_from(m.Project))==0
        assert seen[1][0:2]==('POST','/chat/completions')
        assert seen[1][2]['model']=='Qwen/Qwen3-VL-2B-Instruct'


def test_provider_length_response_continues_exactly_once_and_keeps_multimodal_context(factory,settings,model,monkeypatch):
    async def ensure(snapshot):return None
    monkeypatch.setitem(sys.modules,'app.services.screenplay_runtime',SimpleNamespace(ensure_local_service=ensure))
    original_messages=[{'role':'system','content':'Return JSON.'},
        {'role':'user','content':[{'type':'text','text':'Create the screenplay.'},
            {'type':'image_url','image_url':{'url':'data:image/png;base64,c2FtcGxl'}}]}]
    monkeypatch.setattr(service,'prompt_messages',lambda job,media_parts,source:copy.deepcopy(original_messages))
    screenplay=json.dumps(raw_ir(),ensure_ascii=False,separators=(',',':'))
    split=len(screenplay)//2
    responses=[
        httpx.Response(200,json={'id':'first','choices':[{'message':{'content':screenplay[:split]},'finish_reason':'length'}],
            'usage':{'prompt_tokens':10,'completion_tokens':20,'total_tokens':30}},request=httpx.Request('POST','http://127.0.0.1/v1/chat/completions')),
        httpx.Response(200,json={'id':'second','choices':[{'message':{'content':screenplay[split:]},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':15,'completion_tokens':12,'total_tokens':27}},request=httpx.Request('POST','http://127.0.0.1/v1/chat/completions')),
    ]
    requests=[]
    async def call(settings,snapshot,method,endpoint,secret='',**kwargs):
        requests.append(kwargs['json'])
        return responses.pop(0)
    monkeypatch.setattr(service,'service_request',call)
    with factory() as session:job=service.create_generation(session,settings,payload(model),capability_test=True);identifier=job.id

    asyncio.run(service.process_job(settings,factory,identifier))

    with factory() as session:
        job=service.get_job(session,identifier)
        history=job.receipt_json['attempt_history']
        assert job.status=='awaiting_review' and job.screenplay_json, job.error_message
        assert len(requests)==2 and len(history)==2
        assert [attempt['finish_reason'] for attempt in history]==['length','stop']
        assert [attempt['continuation_index'] for attempt in history]==[0,1]
        assert all(attempt['response_relative_path'] and attempt['response_sha256'] for attempt in history)
        continued=requests[1]['messages']
        assert continued[:2]==original_messages
        assert continued[-2]=={'role':'assistant','content':screenplay[:split]}
        assert '只输出尚未生成的JSON文本' in continued[-1]['content']
        assert job.receipt_json['provider_continuation_count']==1
        assert job.receipt_json['usage']['total_tokens']==57


def test_truncated_saved_response_is_continued_after_worker_restart(factory,settings,model,monkeypatch):
    async def ensure(snapshot):return None
    monkeypatch.setitem(sys.modules,'app.services.screenplay_runtime',SimpleNamespace(ensure_local_service=ensure))
    screenplay=json.dumps(raw_ir(),ensure_ascii=False,separators=(',',':'))
    split=len(screenplay)//2
    response={'id':'saved-truncated','choices':[{'message':{'content':screenplay[:split]},'finish_reason':'length'}],
        'usage':{'total_tokens':30}}
    calls=[]
    async def continue_call(settings,snapshot,*args,**kwargs):
        calls.append(kwargs['json'])
        return httpx.Response(200,json={'id':'continued','choices':[{'message':{'content':screenplay[split:]},'finish_reason':'stop'}]},
            request=httpx.Request('POST','http://127.0.0.1/v1/chat/completions'))
    monkeypatch.setattr(service,'service_request',continue_call)
    with factory() as session:
        job=service.create_generation(session,settings,payload(model),capability_test=True)
        relative=f'screenplay_jobs/{job.id}/attempt_1_response.json'
        path=settings.media_root/relative;path.parent.mkdir(parents=True,exist_ok=True)
        data=json.dumps(response,ensure_ascii=False).encode('utf-8');path.write_bytes(data)
        job.status='running';job.stage='validating'
        job.receipt_json={'provider_request_submitted':True,'provider_response_received':True,'provider_response_saved':True,
            'provider_http_status':200,'provider_current_attempt_index':1,'provider_response_relative_path':relative,
            'provider_response_sha256':service.sha(data),'actual_service_snapshot':job.snapshot_json['service_snapshot'],
            'source':{'input_mode':'text','four_stage_status':{'screenplay':'generating'}},
            'attempt_history':[{'attempt_index':1,'status':'response_saved','http_status':200,'response_relative_path':relative,
                'response_sha256':service.sha(data),'finish_reason':'length','continuation_index':0}]}
        identifier=job.id;session.commit()

    service.recover_interrupted_jobs(factory)
    asyncio.run(service.process_job(settings,factory,identifier))

    with factory() as session:
        job=service.get_job(session,identifier)
        assert job.status=='awaiting_review' and job.screenplay_json, job.error_message
        assert len(calls)==1 and job.receipt_json['provider_continuation_count']==1
        assert calls[0]['messages'][-2]=={'role':'assistant','content':screenplay[:split]}
        assert job.receipt_json['attempt_history'][1]['continuation_index']==1
        assert len(job.receipt_json['attempt_history'])==2


def test_unknown_continuation_result_is_never_replayed(factory,settings,model,monkeypatch):
    async def ensure(snapshot):return None
    monkeypatch.setitem(sys.modules,'app.services.screenplay_runtime',SimpleNamespace(ensure_local_service=ensure))
    screenplay=json.dumps(raw_ir(),ensure_ascii=False,separators=(',',':'))
    calls=[]
    async def timeout_on_continuation(settings,snapshot,*args,**kwargs):
        calls.append(kwargs['json'])
        if len(calls)==1:
            return httpx.Response(200,json={'choices':[{'message':{'content':screenplay[:len(screenplay)//2]},'finish_reason':'length'}]},
                request=httpx.Request('POST','http://127.0.0.1/v1/chat/completions'))
        raise httpx.ReadTimeout('result not received')
    monkeypatch.setattr(service,'service_request',timeout_on_continuation)
    with factory() as session:job=service.create_generation(session,settings,payload(model),capability_test=True);identifier=job.id

    asyncio.run(service.process_job(settings,factory,identifier))
    service.recover_interrupted_jobs(factory)
    asyncio.run(service.process_job(settings,factory,identifier))

    with factory() as session:
        job=service.get_job(session,identifier)
        assert job.status=='unknown' and len(calls)==2
        assert [attempt['status'] for attempt in job.receipt_json['attempt_history']]==['responded','unknown']
        assert job.receipt_json['attempt_history'][0]['response_relative_path']
        assert job.receipt_json['automatic_retry'] is False


def test_screenplay_continues_without_total_length_or_attempt_limit(factory,settings,model,monkeypatch):
    async def ensure(snapshot):return None
    monkeypatch.setitem(sys.modules,'app.services.screenplay_runtime',SimpleNamespace(ensure_local_service=ensure))
    long_ir=raw_ir();long_ir['long_note']=''.join(chr(97+(index%26)) for index in range(1_000_050))
    screenplay=json.dumps(long_ir,ensure_ascii=False,separators=(',',':'))
    chunk_size=(len(screenplay)+4)//5
    chunks=[screenplay[index:index+chunk_size] for index in range(0,len(screenplay),chunk_size)]
    calls=[]
    async def keep_truncating(settings,snapshot,*args,**kwargs):
        body=kwargs['json'];calls.append(body)
        index=len(calls)-1
        reason='length' if index<len(chunks)-1 else 'stop'
        return httpx.Response(200,json={'choices':[{'message':{'content':chunks[index]},'finish_reason':reason}]},
            request=httpx.Request('POST','http://127.0.0.1/v1/chat/completions'))
    monkeypatch.setattr(service,'service_request',keep_truncating)
    with factory() as session:job=service.create_generation(session,settings,payload(model),capability_test=True);identifier=job.id

    asyncio.run(service.process_job(settings,factory,identifier))

    with factory() as session:
        job=service.get_job(session,identifier)
        assert job.status=='awaiting_review' and job.screenplay_json, job.error_message
        assert len(calls)==len(chunks)>4 and len(job.receipt_json['attempt_history'])==len(chunks)
        assert all(attempt['response_relative_path'] and attempt['response_sha256'] for attempt in job.receipt_json['attempt_history'])
        assert job.receipt_json['finish_reason']=='stop'
        assert job.receipt_json['provider_continuation_count']==len(chunks)-1
        assert job.screenplay_json['long_note']==long_ir['long_note']
        assert all(len(body['messages'][-2]['content'])<=service.SCREENPLAY_CONTINUATION_CONTEXT_CHARS for body in calls[1:])
        assert len(service.documents(session,identifier))==1


@pytest.mark.parametrize(('finish_reason','expected_calls'),[('content_filter',1),('stop_sequence',1)])
def test_non_length_finish_reason_is_not_continued(factory,settings,model,monkeypatch,finish_reason,expected_calls):
    async def ensure(snapshot):return None
    monkeypatch.setitem(sys.modules,'app.services.screenplay_runtime',SimpleNamespace(ensure_local_service=ensure))
    calls=[]
    async def call(settings,snapshot,*args,**kwargs):
        calls.append(1)
        return httpx.Response(200,json={'choices':[{'message':{'content':'{}'},'finish_reason':finish_reason}]},
            request=httpx.Request('POST','http://127.0.0.1/v1/chat/completions'))
    monkeypatch.setattr(service,'service_request',call)
    with factory() as session:job=service.create_generation(session,settings,payload(model),capability_test=True);identifier=job.id

    asyncio.run(service.process_job(settings,factory,identifier))

    with factory() as session:
        job=service.get_job(session,identifier)
        assert job.status=='failed' and len(calls)==expected_calls
        assert len(job.receipt_json['attempt_history'])==1


def test_create_generation_rejects_unreadable_saved_credentials_before_enqueue(factory,settings,model,monkeypatch):
    with factory() as session:
        row=session.get(m.AIService,model);row.encrypted_secret=b'unreadable-ciphertext';session.commit()

    def unavailable(_value):
        raise AppError(503,'secure_store_failed','DPAPI is unavailable')

    monkeypatch.setattr(service,'reveal',unavailable)
    with factory() as session:
        with pytest.raises(AppError) as failure:
            service.create_generation(session,settings,payload(model),capability_test=True)
        assert failure.value.code=='secure_store_failed'
        assert session.scalar(select(func.count()).select_from(Job))==0


@pytest.mark.parametrize(('error_code','expected_status'),[
    ('secure_store_failed','blocked'),
    ('unexpected_preflight_error','failed'),
])
def test_unhandled_preflight_error_ends_queued_job(factory,settings,model,monkeypatch,error_code,expected_status):
    with factory() as session:
        job=service.create_generation(session,settings,payload(model),capability_test=True)
        identifier=job.id
        row=session.get(m.AIService,model);row.encrypted_secret=b'unreadable-ciphertext';session.commit()

    def fail_before_provider(_value):
        raise AppError(503,error_code,'preflight failed before model submission opaque-secret-sentinel')

    monkeypatch.setattr(service,'reveal',fail_before_provider)
    calls=[]
    async def provider(*args,**kwargs):
        calls.append((args,kwargs))
        raise AssertionError('preflight failure must not call the model')
    monkeypatch.setattr(service,'service_request',provider)

    asyncio.run(service.process_job(settings,factory,identifier))

    with factory() as session:
        job=service.get_job(session,identifier)
        assert (job.status,job.stage)==(expected_status,expected_status)
        assert job.receipt_json['worker_failure']['error_code']==error_code
        assert '未发送模型请求' in job.error_message
        assert 'opaque-secret-sentinel' not in job.error_message
        assert 'opaque-secret-sentinel' not in str(job.receipt_json)
        assert job.receipt_json['provider_request_submitted'] is False
        assert calls==[]


def test_unhandled_preflight_error_after_submission_preserves_unknown_state(factory,settings,model,monkeypatch):
    with factory() as session:
        job=service.create_generation(session,settings,payload(model),capability_test=True)
        identifier=job.id
        job.receipt_json={'provider_request_submitted':True}
        row=session.get(m.AIService,model);row.encrypted_secret=b'unreadable-ciphertext';session.commit()

    def fail_before_provider(_value):
        raise AppError(503,'secure_store_failed','DPAPI is unavailable')

    monkeypatch.setattr(service,'reveal',fail_before_provider)

    asyncio.run(service.process_job(settings,factory,identifier))

    with factory() as session:
        job=service.get_job(session,identifier)
        assert (job.status,job.stage)==('unknown','unknown')
        assert job.receipt_json['automatic_retry'] is False
        assert '可能已提交' in job.error_message

def test_unknown_provider_result_never_auto_retries_or_pays_fallback(factory,settings,model,monkeypatch):
    calls=[];timeouts=[]
    async def ensure(snapshot):return None
    monkeypatch.setitem(sys.modules,'app.services.screenplay_runtime',SimpleNamespace(ensure_local_service=ensure))
    async def fail(*args,**kwargs):
        calls.append(1);timeouts.append(kwargs['client_timeout']);raise httpx.ReadTimeout('response lost')
    monkeypatch.setattr(service,'service_request',fail)
    with factory() as session:job=service.create_generation(session,settings,payload(model),capability_test=True);identifier=job.id
    asyncio.run(service.process_job(settings,factory,identifier))
    asyncio.run(service.process_job(settings,factory,identifier))
    with factory() as session:
        job=service.get_job(session,identifier)
        assert job.status=='unknown' and len(calls)==1
        assert job.receipt_json['automatic_retry'] is False and job.receipt_json['paid_fallback'] is False
        attempt=job.receipt_json['attempt_history'][0]
        assert attempt['timeout_seconds'] is None and attempt['transport_error_type']=='ReadTimeout'
        assert attempt['total_timeout_seconds'] is None
        assert 'ReadTimeout' in job.error_message
        assert timeouts[0].read is None and timeouts[0].connect is None


def test_slow_provider_response_is_not_cancelled_by_an_application_deadline(factory,settings,model,monkeypatch):
    async def ensure(snapshot):return None
    monkeypatch.setitem(sys.modules,'app.services.screenplay_runtime',SimpleNamespace(ensure_local_service=ensure))
    completed=[]
    async def slow_stream(*args,**kwargs):
        await asyncio.sleep(0.05)
        completed.append(kwargs['client_timeout'])
        raise httpx.ReadTimeout('controlled provider timeout')
    monkeypatch.setattr(service,'service_request',slow_stream)
    with factory() as session:job=service.create_generation(session,settings,payload(model),capability_test=True);identifier=job.id
    asyncio.run(service.process_job(settings,factory,identifier))
    with factory() as session:
        job=service.get_job(session,identifier);attempt=job.receipt_json['attempt_history'][0]
        assert job.status=='unknown' and attempt['status']=='unknown'
        assert attempt['transport_error_type']=='ReadTimeout' and attempt['total_timeout_seconds'] is None
        assert len(completed)==1 and completed[0].read is None and completed[0].connect is None
        assert job.receipt_json['automatic_retry'] is False


def test_worker_restart_only_requeues_jobs_known_not_to_have_been_submitted(factory,settings,model):
    with factory() as session:
        before_request=service.create_generation(session,settings,payload(model,idempotency_key='restart-before-'+uuid.uuid4().hex),capability_test=True)
        before_request.status='running';before_request.stage='preprocessing';before_request.receipt_json={}
        generation_not_submitted=service.create_generation(session,settings,payload(model,idempotency_key='restart-generating-'+uuid.uuid4().hex),capability_test=True)
        generation_not_submitted.status='running';generation_not_submitted.stage='generating'
        generation_not_submitted.receipt_json={'provider_request_submitted':False}
        submitted=service.create_generation(session,settings,payload(model,idempotency_key='restart-submitted-'+uuid.uuid4().hex),capability_test=True)
        submitted.status='running';submitted.stage='generating'
        submitted.receipt_json={'provider_request_submitted':True}
        identifiers=(before_request.id,generation_not_submitted.id,submitted.id)
        session.commit()

    service.recover_interrupted_jobs(factory)

    with factory() as session:
        assert [(session.get(Job,identifier).status,session.get(Job,identifier).stage) for identifier in identifiers]==[
            ('queued','queued'),('queued','queued'),('unknown','unknown')]
        assert '已安全重新排队' in session.get(Job,identifiers[0]).error_message
        assert '结果未知' in session.get(Job,identifiers[2]).error_message


def test_saved_provider_response_is_revalidated_locally_after_worker_restart(factory,settings,model,monkeypatch):
    async def ensure(snapshot):return None
    monkeypatch.setitem(sys.modules,'app.services.screenplay_runtime',SimpleNamespace(ensure_local_service=ensure))
    async def forbidden(*args,**kwargs):raise AssertionError('a saved provider response must never be resubmitted')
    monkeypatch.setattr(service,'service_request',forbidden)
    with factory() as session:
        job=service.create_generation(session,settings,payload(model),capability_test=True)
        response={'id':'saved-provider-response','usage':{'total_tokens':20},'choices':[{'message':{'content':json.dumps(raw_ir(),ensure_ascii=False)},'finish_reason':'stop'}]}
        relative=f'screenplay_jobs/{job.id}/attempt_1_response.json'
        path=settings.media_root/relative;path.parent.mkdir(parents=True,exist_ok=True)
        data=json.dumps(response,ensure_ascii=False).encode('utf-8');path.write_bytes(data)
        job.status='running';job.stage='validating'
        job.receipt_json={'provider_request_submitted':True,'provider_response_received':True,'provider_response_saved':True,
            'provider_http_status':200,'provider_current_attempt_index':1,'provider_response_relative_path':relative,
            'provider_response_sha256':service.sha(data),'actual_service_snapshot':job.snapshot_json['service_snapshot'],
            'source':{'input_mode':'text','four_stage_status':{'screenplay':'generating'}},
            'attempt_history':[{'attempt_index':1,'status':'response_saved','response_relative_path':relative,'response_sha256':service.sha(data)}]}
        identifier=job.id;session.commit()

    service.recover_interrupted_jobs(factory)
    with factory() as session:
        recovered=service.get_job(session,identifier)
        assert recovered.status=='queued' and recovered.receipt_json['provider_response_saved'] is True

    asyncio.run(service.process_job(settings,factory,identifier))
    with factory() as session:
        recovered=service.get_job(session,identifier)
        assert recovered.status=='awaiting_review' and recovered.screenplay_json
        assert recovered.receipt_json['local_response_recovery_attempted'] is True
        assert len(recovered.receipt_json['attempt_history'])==1
        assert len(service.documents(session,identifier))==1

def test_cloud_requires_explicit_per_request_authorization(factory,settings,model):
    with factory() as session:
        row=session.get(m.AIService,model);row.provider='openai_compatible';row.base_url='https://example.com/v1';session.commit()
        with pytest.raises(AppError) as blocked:service.create_generation(session,settings,payload(model),capability_test=True)
        assert blocked.value.code=='screenplay_cloud_not_authorized'

def fallback_job(factory,settings,model):
    with factory() as session:
        second=m.AIService(id='svc_second',module='language_processing',provider='local_llm',name='Second',base_url='http://127.0.0.1:8772/v1',default_model='second-model',settings_json={'timeout':30})
        session.add(second);session.commit()
        job=service.create_generation(session,settings,payload(model),capability_test=True);job.capability_test=False
        first=job.snapshot_json['service_snapshot'];backup=service.service_snapshot(second)
        job.snapshot_json={**job.snapshot_json,'route':{'profile':{'settings':{'allow_fallback':True,'allow_paid_fallback':False}},
            'choices':[{'service_snapshot':first,'candidate':{'eligible':True}},{'service_snapshot':backup,'candidate':{'eligible':True}}]}}
        session.commit();return job.id

def test_configured_verified_fallback_only_after_definite_http_rejection(factory,settings,model,monkeypatch):
    identifier=fallback_job(factory,settings,model);calls=[]
    async def ensure(snapshot):return None
    monkeypatch.setitem(sys.modules,'app.services.screenplay_runtime',SimpleNamespace(ensure_local_service=ensure))
    async def call(settings,snapshot,*args,**kwargs):
        calls.append(snapshot['service_id']);status=400 if len(calls)==1 else 200
        return httpx.Response(status,json={'error':'definite rejection'} if status==400 else {'choices':[{'message':{'content':json.dumps(raw_ir(),ensure_ascii=False)}}]},request=httpx.Request('POST','http://127.0.0.1/test'))
    monkeypatch.setattr(service,'service_request',call)
    asyncio.run(service.process_job(settings,factory,identifier))
    with factory() as session:
        job=service.get_job(session,identifier)
        assert job.status=='awaiting_review' and calls==[model,'svc_second']
        assert [a['status'] for a in job.receipt_json['attempt_history']]==['rejected','responded']
        assert service.dto(session,job)['service_snapshot']['service_id']=='svc_second'
        second=session.get(m.AIService,'svc_second')
        assert service.screenplay_evidence(session,settings,second,'second-model','screenplay_from_text')


def test_saved_continuation_rejection_resumes_only_through_configured_fallback(factory,settings,model,monkeypatch):
    identifier=fallback_job(factory,settings,model)
    screenplay=json.dumps(raw_ir(),ensure_ascii=False,separators=(',',':'));split=len(screenplay)//2
    partial=screenplay[:split]
    continuation_rejection=b'{"error":"continuation explicitly rejected"}'
    calls=[]
    async def fallback(settings,snapshot,method,endpoint,secret='',**kwargs):
        calls.append((snapshot['service_id'],kwargs['json']))
        return httpx.Response(200,json={'id':'fallback-complete','choices':[{'message':{'content':screenplay[split:]},'finish_reason':'stop'}]},
            request=httpx.Request('POST','http://127.0.0.1/test'))
    async def ensure(snapshot):return None
    monkeypatch.setitem(sys.modules,'app.services.screenplay_runtime',SimpleNamespace(ensure_local_service=ensure))
    monkeypatch.setattr(service,'service_request',fallback)
    with factory() as session:
        job=service.get_job(session,identifier);first=job.snapshot_json['service_snapshot']
        artifacts=[]
        for index,response in [(1,{'choices':[{'message':{'content':partial},'finish_reason':'length'}]}),(2,continuation_rejection)]:
            relative=f'screenplay_jobs/{job.id}/attempt_{index}_response.json'
            path=settings.media_root/relative;path.parent.mkdir(parents=True,exist_ok=True)
            data=(json.dumps(response,ensure_ascii=False).encode('utf-8') if isinstance(response,dict) else response);path.write_bytes(data)
            artifacts.append((relative,data))
        job.status='running';job.stage='generating'
        job.receipt_json={'provider_request_submitted':True,'provider_response_received':True,'provider_response_saved':True,
            'provider_http_status':400,'provider_current_attempt_index':2,'provider_response_relative_path':artifacts[1][0],
            'provider_response_sha256':service.sha(artifacts[1][1]),'actual_service_snapshot':first,
            'source':{'input_mode':'text','four_stage_status':{'screenplay':'generating'}},
            'attempt_history':[
                {'attempt_index':1,'service_id':model,'service_revision':first['service_revision'],'model_id':first['model_id'],'is_cloud':False,
                    'http_status':200,'response_relative_path':artifacts[0][0],'response_sha256':service.sha(artifacts[0][1]),'finish_reason':'length','continuation_index':0},
                {'attempt_index':2,'service_id':model,'service_revision':first['service_revision'],'model_id':first['model_id'],'is_cloud':False,
                    'http_status':400,'response_relative_path':artifacts[1][0],'response_sha256':service.sha(artifacts[1][1]),'continuation_index':1},
            ]}
        session.commit()

    service.recover_interrupted_jobs(factory)
    asyncio.run(service.process_job(settings,factory,identifier))

    with factory() as session:
        job=service.get_job(session,identifier)
        assert job.status=='awaiting_review' and job.screenplay_json
        assert len(calls)==1 and calls[0][0]=='svc_second'
        assert calls[0][1]['messages'][-2]=={'role':'assistant','content':partial}
        assert job.receipt_json['paid_fallback'] is False
        assert len(job.receipt_json['attempt_history'])==3


@pytest.mark.parametrize(('input_mode','allow_cloud_costs','allow_media'),[('text',False,True),('image',True,False)])
def test_saved_rejection_never_uses_cloud_fallback_without_cost_or_media_authorization(factory,settings,model,monkeypatch,input_mode,allow_cloud_costs,allow_media):
    identifier=fallback_job(factory,settings,model);calls=[]
    async def forbidden(*args,**kwargs):calls.append(args);raise AssertionError('unauthorized cloud fallback must not be called')
    monkeypatch.setattr(service,'service_request',forbidden)
    with factory() as session:
        job=service.get_job(session,identifier);row=session.get(m.AIService,'svc_second')
        row.provider='openai_compatible';row.base_url='https://example.com/v1';row.revision+=1
        snapshot=copy.deepcopy(job.snapshot_json);route=copy.deepcopy(snapshot['route'])
        route['profile']['settings'].update(allow_fallback=True,allow_paid_fallback=True)
        route['choices'][1]['service_snapshot']=service.service_snapshot(row)
        snapshot.update(route=route,allow_paid_fallback=True,allow_cloud_costs=allow_cloud_costs,allow_cloud_media_upload=allow_media)
        job.snapshot_json=snapshot;job.input_mode=input_mode
        raw=b'{"error":"definite rejection"}';relative=f'screenplay_jobs/{job.id}/attempt_1_response.json'
        path=settings.media_root/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
        first=snapshot['service_snapshot'];job.status='running';job.stage='generating'
        job.receipt_json={'provider_request_submitted':True,'provider_response_received':True,'provider_response_saved':True,
            'provider_http_status':400,'provider_current_attempt_index':1,'provider_response_relative_path':relative,
            'provider_response_sha256':service.sha(raw),'actual_service_snapshot':first,
            'source':{'input_mode':input_mode,'four_stage_status':{'screenplay':'generating'}},
            'attempt_history':[{'attempt_index':1,'service_id':model,'service_revision':first['service_revision'],
                'model_id':first['model_id'],'is_cloud':False,'http_status':400,'response_relative_path':relative,
                'response_sha256':service.sha(raw)}]}
        session.commit()

    service.recover_interrupted_jobs(factory)
    asyncio.run(service.process_job(settings,factory,identifier))

    with factory() as session:
        job=service.get_job(session,identifier)
        assert job.status=='failed' and calls==[]
        assert '没有已授权的备用服务' in job.error_message

@pytest.mark.parametrize('status',[408,500,503])
def test_unknown_http_cannot_trigger_configured_fallback(factory,settings,model,monkeypatch,status):
    identifier=fallback_job(factory,settings,model);calls=[]
    async def ensure(snapshot):return None
    monkeypatch.setitem(sys.modules,'app.services.screenplay_runtime',SimpleNamespace(ensure_local_service=ensure))
    async def call(settings,snapshot,*args,**kwargs):
        calls.append(snapshot['service_id']);return httpx.Response(status,request=httpx.Request('POST','http://127.0.0.1/test'))
    monkeypatch.setattr(service,'service_request',call)
    asyncio.run(service.process_job(settings,factory,identifier))
    with factory() as session:
        assert service.get_job(session,identifier).status=='unknown' and calls==[model]

def test_paid_fallback_needs_route_and_invocation_cloud_authorization(factory,settings,model):
    identifier=fallback_job(factory,settings,model)
    with factory() as session:
        job=service.get_job(session,identifier);row=session.get(m.AIService,'svc_second');row.provider='openai_compatible';row.base_url='https://example.com/v1';session.commit()
        snapshot=copy.deepcopy(job.snapshot_json);snapshot['route']['choices'][1]['service_snapshot']=service.service_snapshot(row)
        snapshot['route']['profile']['settings']['allow_paid_fallback']=True;job.snapshot_json=snapshot
        first=snapshot['service_snapshot']
        assert service._next_fallback(session,job,first) is None
        snapshot=copy.deepcopy(job.snapshot_json);snapshot.update(allow_paid_fallback=True,allow_cloud_costs=True);job.snapshot_json=snapshot
        assert service._next_fallback(session,job,first)['service_id']=='svc_second'
        job.snapshot_json={**snapshot,'route':None}
        assert service._next_fallback(session,job,first) is None

def test_generated_word_roundtrips_standard_importer_without_another_model(factory,settings,model):
    from app.services.word_screenplay import parse_blocks
    from app.services.cinematic_import import build_ir
    identifier=make_ready(factory,settings,model)
    with factory() as session:
        document=service.documents(session,identifier)[0]
        blocks=service.read_blocks((settings.media_root/document.relative_path).read_bytes(),settings.import_max_bytes)
        parsed=build_ir(blocks,parse_blocks)
        assert len(parsed['characters'])==2 and len(parsed['scenes'])==1 and len(parsed['shots'])==2
        assert sum(len(shot['dialogues']) for shot in parsed['shots'])==4
        # The legacy importer trims source paragraphs; direct review/create uses
        # the same original IR and preserves the exact project name separately.
        assert parsed['project']['name']=='保留名称空格'
        assert service.validate_preview(session,parsed)==[]

def test_export_new_word_version_preserves_reviewed_document_and_existing_project(factory,settings,model,monkeypatch):
    identifier=make_ready(factory,settings,model,name='重导出保护测试')
    with factory() as session:
        job=service.get_job(session,identifier);old=service.documents(session,identifier)[0]
        created=service.approve(session,settings,identifier,ApproveScreenplay(expected_revision=job.revision,document_id=old.id,approve=True,create_project=True))
        project_id=created.project_id;word_import_id=created.word_import_id
        original_hash=service.file_sha(settings.media_root/old.relative_path)
        async def never(*args,**kwargs):raise AssertionError('Document export must not call a model')
        monkeypatch.setattr(service,'service_request',never)
        result=service.export_new_version(session,settings,identifier,created.revision)
        docs=service.documents(session,identifier)
        assert len(docs)==2 and docs[0].review_state=='approved' and docs[1].review_state=='pending'
        assert docs[0].version==1 and docs[1].version==2 and docs[0].relative_path!=docs[1].relative_path
        assert result.project_id==project_id and result.word_import_id==word_import_id
        assert result.status=='awaiting_review' and result.review_state=='pending'
        assert service.file_sha(settings.media_root/old.relative_path)==original_hash
        assert session.get(m.WordImport,word_import_id).relative_path==old.relative_path
        assert session.scalar(select(func.count()).select_from(m.Project))==1

def test_invalid_model_output_keeps_actual_provider_response_for_diagnosis(factory,settings,model,monkeypatch):
    async def ensure(snapshot):return None
    monkeypatch.setitem(sys.modules,'app.services.screenplay_runtime',SimpleNamespace(ensure_local_service=ensure))
    async def malformed(*args,**kwargs):
        return httpx.Response(200,json={'choices':[{'message':{'content':'This is not a JSON screenplay.'}}]},request=httpx.Request('POST','http://127.0.0.1/test'))
    monkeypatch.setattr(service,'service_request',malformed)
    with factory() as session:job=service.create_generation(session,settings,payload(model),capability_test=True);identifier=job.id
    asyncio.run(service.process_job(settings,factory,identifier))
    with factory() as session:
        job=service.get_job(session,identifier)
        assert job.status=='failed' and not job.screenplay_json
        path=settings.media_root/job.receipt_json['provider_response_relative_path']
        assert path.is_file() and 'This is not a JSON screenplay.' in path.read_text('utf-8')
        assert service.sha(path.read_bytes())==job.receipt_json['provider_response_sha256']

def test_configured_source_transcription_is_sent_to_model_and_retained_as_unverified(factory,settings,model,monkeypatch):
    calls=[]
    async def ensure(snapshot):return None
    async def transcribe(snapshot,settings,source):
        calls.append('asr')
        return {**source,'audio_transcription':'transcribed_unverified',
            'transcript':{'text':'先核对原始记录。','segments':[{'start':0,'end':2,'text':'先核对原始记录。','speaker':'unknown'}],'speaker':'unknown','language':'zh'},
            'transcription_receipt':{'actual_model':'whisper-base','source_sha256':'test-audio-sha'}}
    monkeypatch.setitem(sys.modules,'app.services.screenplay_runtime',SimpleNamespace(ensure_local_service=ensure,maybe_transcribe_audio=transcribe))
    monkeypatch.setattr(service,'prepare_source',lambda *args:([],{'audio_relative_path':'owned_test_audio.wav','audio_transcription':'not_configured'}))
    async def call(settings,snapshot,method,endpoint,secret='',**kwargs):
        text='\n'.join(p.get('text','') for p in kwargs['json']['messages'][1]['content'])
        assert '先核对原始记录。' in text and '说话人目前未知' in text
        assert '音轨已提取但未配置语音转写' not in text
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(raw_ir(),ensure_ascii=False)}}]},request=httpx.Request('POST','http://127.0.0.1/test'))
    monkeypatch.setattr(service,'service_request',call)
    with factory() as session:job=service.create_generation(session,settings,payload(model),capability_test=True);identifier=job.id
    asyncio.run(service.process_job(settings,factory,identifier))
    with factory() as session:
        job=service.get_job(session,identifier);document=service.documents(session,identifier)[0]
        assert job.status=='awaiting_review' and calls==['asr']
        assert job.receipt_json['source']['transcription_receipt']['actual_model']=='whisper-base'
        assert job.screenplay_json['source_transcript']['speaker']=='unknown'
        assert any('识别结果未经人工确认' in s for s in job.screenplay_json['uncertainties'])
        word='\n'.join(p.text for p in Document(settings.media_root/document.relative_path).paragraphs)
        assert '原音轨自动转写：先核对原始记录。' in word and '原音轨说话人：未知' in word

def test_explicit_retry_creates_new_idempotent_job_using_current_config_without_mutating_failure(factory,settings,model):
    with factory() as session:
        original=service.create_generation(session,settings,payload(model),capability_test=True)
        original.status='failed';original.stage='failed';original.error_message='definite prior failure';original.revision+=1;session.commit()
        row=session.get(m.AIService,model);row.revision+=1;row.default_model='updated-local-model';row.models_json=[{'id':'updated-local-model'}];session.commit()
        request=RetryScreenplay(expected_revision=original.revision,idempotency_key='explicit-retry-'+uuid.uuid4().hex)
        retry=service.retry_generation(session,settings,original.id,request)
        assert retry.id!=original.id and retry.status=='queued' and retry.capability_test is True
        assert retry.snapshot_json['retry_of_job_id']==original.id
        assert retry.snapshot_json['service_snapshot']['model_id']=='updated-local-model'
        assert retry.snapshot_json['service_snapshot']['service_revision']==row.revision
        assert retry.snapshot_json['allow_cloud_costs'] is False and retry.snapshot_json['allow_paid_fallback'] is False
        assert original.status=='failed' and original.error_message=='definite prior failure'
        assert service.retry_generation(session,settings,original.id,request).id==retry.id

@pytest.mark.parametrize('status,expected',[('unknown','screenplay_retry_unknown'),('export_failed','screenplay_retry_export_only'),('queued','screenplay_retry_unavailable')])
def test_retry_does_not_resend_unknown_exported_or_running_work(factory,settings,model,status,expected):
    with factory() as session:
        job=service.create_generation(session,settings,payload(model),capability_test=True)
        job.status=status;session.commit()
        with pytest.raises(AppError) as error:service.retry_generation(session,settings,job.id,RetryScreenplay(expected_revision=job.revision,idempotency_key='retry-'+uuid.uuid4().hex))
        assert error.value.code==expected
        assert session.scalar(select(func.count()).select_from(Job))==1


def test_unknown_retry_requires_acknowledgement_and_creates_linked_task(factory,settings,model):
    with factory() as session:
        original=service.create_generation(session,settings,payload(model),capability_test=True)
        original.status='unknown';original.stage='unknown'
        original.receipt_json={'provider_request_submitted':True}
        original.error_message='ReadTimeout';session.commit()
        request=RetryScreenplay(expected_revision=original.revision,idempotency_key='acknowledged-retry-'+uuid.uuid4().hex,
            confirm_unknown_retry=True)

        retry=service.retry_generation(session,settings,original.id,request)

        assert retry.id!=original.id and retry.status=='queued'
        assert retry.snapshot_json['retry_of_job_id']==original.id
        assert original.status=='unknown' and original.error_message=='ReadTimeout'
        assert service.retry_generation(session,settings,original.id,request).id==retry.id


def test_unknown_outcome_cannot_be_cancelled_or_hidden_by_batch_resume(factory,settings,model):
    with factory() as session:
        unknown=service.create_generation(session,settings,payload(model),capability_test=True)
        unknown.status='unknown';unknown.stage='unknown';unknown.receipt_json={'provider_request_submitted':True};session.commit()
        identifier=unknown.id;revision=unknown.revision
        with pytest.raises(AppError) as error:service.cancel_generation(session,identifier,CancelScreenplay(expected_revision=revision))
        assert error.value.code=='screenplay_cancel_unavailable'
        result=service.batch_cancel(session,settings,SimpleNamespace(ids=[identifier]))
        assert result==[{'id':identifier,'ok':False,'error':'unknown_outcome_requires_explicit_retry'}]
        assert service.get_job(session,identifier).status=='unknown'
        assert session.scalar(select(func.count()).select_from(Job))==1


def test_cancelling_submitted_request_is_unknown_and_resume_requires_acknowledgement(factory,settings,model):
    with factory() as session:
        job=service.create_generation(session,settings,payload(model),capability_test=True)
        job.status='running';job.stage='generating';job.receipt_json={'provider_request_submitted':True};session.commit()
        result=service.cancel_generation(session,job.id,CancelScreenplay(expected_revision=job.revision))
        assert result.status=='unknown' and '无法停止供应商执行' in result.error_message

        result.status='cancelled';result.stage='cancelled';session.commit()
        with pytest.raises(AppError) as error:
            service.resume_generation(session,settings,result.id,ResumeScreenplay(expected_revision=result.revision,idempotency_key='resume-unconfirmed-'+uuid.uuid4().hex))
        assert error.value.code=='screenplay_resume_unknown'
        batch=service.batch_resume(session,settings,SimpleNamespace(ids=[result.id],allow_cloud_costs=False,allow_paid_fallback=False))
        assert batch==[{'id':result.id,'ok':False,'error':'screenplay_resume_unknown'}]
        assert session.scalar(select(func.count()).select_from(Job))==1

        confirmed=service.resume_generation(session,settings,result.id,ResumeScreenplay(expected_revision=result.revision,
            idempotency_key='resume-confirmed-'+uuid.uuid4().hex,confirm_unknown_retry=True))
        assert confirmed.status=='queued' and confirmed.snapshot_json['retry_of_job_id']==result.id
