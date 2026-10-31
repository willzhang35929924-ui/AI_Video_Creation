"""Controlled parser/DAG fixtures; NOT evidence of actual model video generation."""
import asyncio
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
import yaml
from PIL import Image
from sqlalchemy import select,func
from app import models as m
from app.schemas_cinematic import ScreenplayIR,RunInput
from app.services import cinematic_rules
from app.services.cinematic_import import build_ir
from app.services.cinematic_quality import inspect_ir,generation_inputs_hash,visual_inputs,shot_preflight,inspect_generated
from app.services.cinematic_visuals import visual_graph,store_visual
from app.services.production_orchestrator import advance_run
from app.services.word_screenplay import read_blocks,parse_blocks

ROOT=Path(__file__).resolve().parents[2]
FIXTURES=ROOT/'tests/fixtures/screenplays'


def parse(name):return build_ir(read_blocks((FIXTURES/name).read_bytes(),5_000_000),parse_blocks)


@pytest.mark.parametrize('name',[p.name for p in FIXTURES.glob('*.docx')])
def test_real_docx_formats_share_schema_sources_and_preserve_unknowns(name):
    ir=parse(name);ScreenplayIR.model_validate(ir)
    assert ir['versions']==cinematic_rules.rules_bundle()['versions']
    assert ir['shots'] and all(s['source'] for s in ir['shots'])
    assert all('state' in p for s in ir['shots'] for p in s['provenance'].values())
    assert ir['rules_snapshot']['source_sha256']
    if name=='incomplete.docx':
        assert ir['characters']==[] and ir['unrecognized'] and ir['parse_warnings'][0]['kind']=='unresolved_semantics'
        assert any(f['code']=='cast_unknown' for f in inspect_ir(ir)['findings'])
    if name=='cinematic_real_case.docx':
        assert len(ir['shots'])==2 and len(ir['scenes'])==2 and len(ir['characters'])==2
        assert len(ir['shots'][0]['participants'])==2 and sum(len(s['dialogues']) for s in ir['shots'])==2
        assert not inspect_ir(ir)['findings']
        assert '固定眼平双人中近景' in ir['shots'][1]['visual_prompt']
        assert '两人固定身份' in ir['project']['constraints']
    if name=='multi_dialogue.docx':assert len(ir['shots'])==1 and len(ir['shots'][0]['participants'])==2 and len(ir['shots'][0]['dialogues'])==3
    if name=='aliases.docx':assert len(ir['characters'])==1 and ir['shots'][0]['dialogues'][0]['character_key']==ir['characters'][0]['key']
    if name=='table_layout.docx':assert len(ir['shots'])==2 and all(s['dialogues'] for s in ir['shots'])
    if name=='wardrobe_change.docx':assert any(f['code']=='wardrobe_state_jump' for f in inspect_ir(ir)['findings'])
    if name=='contradiction.docx':assert sum(f['code']=='prop_state_jump' for f in inspect_ir(ir)['findings'])==3


def test_generated_schema_templates_versions_and_drift_detection(monkeypatch,tmp_path):
    from docx import Document
    assert cinematic_rules.check_generated_contract()['status']=='passed'
    schema=json.loads((ROOT/'schemas/screenplay-ir.schema.json').read_text(encoding='utf-8'))
    assert schema['$defs']['Provenance']['properties']['state']['enum']==cinematic_rules.rules_bundle()['defaults']['provenance_states']
    for name in ['剧本导入模板.docx','剧本导入示例.docx']:
        doc=Document(ROOT/'docs/templates'/name);text='\n'.join(p.text for p in doc.paragraphs)
        assert all(k+': '+v in text for k,v in cinematic_rules.rules_bundle()['versions'].items())
        assert cinematic_rules.rules_bundle()['source_sha256'] in doc.core_properties.comments
    changed=copy.deepcopy(cinematic_rules.rules_bundle());changed['defaults']['duration_seconds']=9
    path=tmp_path/'modified-rules.yaml';path.write_text(yaml.safe_dump(changed,allow_unicode=True),encoding='utf-8');monkeypatch.setattr(cinematic_rules,'RULE_PATH',path)
    assert cinematic_rules.check_generated_contract()['status']=='failed'
    ir=parse('standard.docx');assert ir['shots'][0]['duration_seconds']==9


def test_unmarked_and_old_templates_compatibility_keeps_source_version():
    blocks=read_blocks((FIXTURES/'standard.docx').read_bytes(),5_000_000)
    blocks.insert(0,{'id':'paragraph:version','kind':'paragraph','text':'template_version: cinematic-word-0.1','style':'Normal'})
    ir=build_ir(blocks,parse_blocks)
    assert ir['mode']=='general' and ir['source_versions']['template_version']=='cinematic-word-0.1'
    assert any(w['kind']=='template_compatibility' for w in ir['parse_warnings'])


def approved_cast(client,settings):
    """Gray images are explicit isolated input fixtures; no generated identity claim."""
    with client.app.state.session_factory() as s:
        for i,(identifier,name) in enumerate([('character_example_a','角色甲'),('character_example_b','角色乙')]):
            c=m.Character(id=identifier,slug=identifier,display_name=name);s.add(c);s.flush()
            v=m.CharacterVersion(character_id=c.id,version=1,approval_state='approved',identity_json={'profile':{'age':34 if i==0 else 28,'description':'隔离测试成年角色'}});s.add(v);s.flush()
            path=settings.media_root/(identifier+'.png');path.parent.mkdir(parents=True,exist_ok=True);Image.new('RGB',(64,64),(60+i*60,80,90)).save(path)
            a=m.CharacterAsset(character_version_id=v.id,asset_type='front_face',relative_path=path.name,mime_type='image/png',sha256=hashlib.sha256(path.read_bytes()).hexdigest(),status='approved',is_primary_reference=True,source='isolated_test_fixture')
            s.add(a);s.flush();v.standard_face_asset_id=a.id;c.current_approved_version_id=v.id
        s.commit()


def import_case(client):
    batch=client.post('/api/word-imports',files={'file':('cinematic_real_case.docx',(FIXTURES/'cinematic_real_case.docx').read_bytes())}).json()
    result=client.post('/api/word-imports/'+batch['id']+'/confirm',json={'revision':batch['revision'],'duplicate_strategy':'new_project'})
    assert result.status_code==200,result.text
    return result.json()


def test_design_versions_and_shot_state_ownership_are_real_and_immutable(client,settings):
    approved_cast(client,settings);result=import_case(client)
    overview=client.get('/api/projects/'+result['project_id']+'/production').json()
    design=next(d for d in overview['designs'] if d['logical_id']=='W_WEN')
    body={'kind':design['kind'],'logical_id':design['logical_id'],'name':design['name'],'character_id':design['character_id'],'scene_id':design['scene_id'],'spec':{'material':'真实棉布'}}
    assert client.patch('/api/production-designs/'+design['id'],json=body).status_code==200
    reviewed=client.post('/api/production-designs/'+design['id']+'/review',json={'status':'approved','reason':'隔离API输入测试，不代表实际图片审核'})
    assert reviewed.status_code==200
    assert client.patch('/api/production-designs/'+design['id'],json=body).status_code==409
    clone=client.post('/api/production-designs/'+design['id']+'/version').json();assert clone['version']==2 and clone['status']=='draft'
    first=overview['shots'][0];bad=copy.deepcopy(first['production'])
    bad['wardrobe_states']['character_example_b']['entity_id']=design['id']
    assert client.put('/api/shots/'+first['shot']['id']+'/production',json=bad).status_code==422


def test_same_named_declarations_remain_distinct_and_ambiguous_speaker_requires_review():
    from app.services.word_screenplay import match_characters
    text=['人物设定','人物 张宁','人物ID：staff_a','年龄：28','人物 张宁','人物ID：staff_b','年龄：44','正文剧情','场景 1 控制室','分镜 1 对话','对白：staff_a：第一人。','对白：张宁：无法确定哪一人。']
    blocks=[{'id':'paragraph:'+str(i),'kind':'paragraph','style':'Normal','text':line} for i,line in enumerate(text)]
    ir=build_ir(blocks,parse_blocks)
    assert len([p for p in ir['characters'] if p['name']=='张宁'])==2
    assert {p['profile'].get('character_id') for p in ir['characters'] if p['name']=='张宁'}=={'staff_a','staff_b'}
    ambiguous=next(p for p in ir['characters'] if p.get('unresolved_name'));assert ambiguous['decision']=='pending'
    first=next(p for p in ir['characters'] if p['profile'].get('character_id')=='staff_a')
    assert ir['shots'][0]['dialogues'][0]['character_key']==first['key']
    assert ir['shots'][0]['dialogues'][1]['character_key']==ambiguous['key']


def test_continuity_conflicts_block_actual_shot_readiness_until_explicit_transition(client,settings):
    approved_cast(client,settings);result=import_case(client)
    with client.app.state.session_factory() as s:
        for e in s.scalars(select(m.ProductionEntity)):e.status='approved'
        second=s.get(m.StoryboardShot,result['shot_ids'][1]);detail=s.scalar(select(m.ShotDetail).where(m.ShotDetail.shot_id==second.id))
        source=copy.deepcopy(detail.source_json);source['production']['state_change_reason']='';source['production']['prop_states'][0]['hand']='左手';detail.source_json=source;s.commit()
        pre=shot_preflight(s,settings,second,False)
        assert not pre['ready'] and any(f['code']=='prop_state_jump' for f in pre['findings'])
        source=copy.deepcopy(detail.source_json);source['production']['state_change_reason']='本镜明确拍摄换到左手；其余物理状态延续。';detail.source_json=source;s.commit()
        assert not any(f['code']=='prop_state_jump' for f in shot_preflight(s,settings,second,False)['findings'])


def test_uploaded_visual_is_real_copied_file_reviewed_separately_and_never_a_model_job(client,settings):
    import io
    approved_cast(client,settings);result=import_case(client)
    image=io.BytesIO();Image.new('RGB',(96,64),'gray').save(image,format='PNG');data=image.getvalue()
    response=client.post('/api/projects/'+result['project_id']+'/production-visuals/upload',data={'payload':json.dumps({'kind':'keyframe','shot_id':result['shot_ids'][0]})},files={'file':('isolated-input.png',data,'image/png')})
    assert response.status_code==201,response.text
    visual=response.json();assert visual['status']=='needs_review' and visual['provider_job_id'] is None and visual['snapshot_json']['source']=='user_upload'
    path=settings.media_root/visual['relative_path'];assert path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest()==visual['sha256']
    assert visual['snapshot_json']['source_sha256']==hashlib.sha256(data).hexdigest()
    with client.app.state.session_factory() as s:assert not list(s.scalars(select(m.GenerationJob)))
    assert client.post('/api/production-visuals/'+visual['id']+'/review',json={'status':'approved','reason':'仅隔离上传与审核协议测试，不是实际生成验收'}).status_code==200
    invalid=client.post('/api/projects/'+result['project_id']+'/production-visuals/upload',data={'payload':json.dumps({'kind':'keyframe','shot_id':result['shot_ids'][0]})},files={'file':('bad.png',b'not an image','image/png')})
    assert invalid.status_code==502


def test_dag_persists_both_scene_ids_and_all_keyframes_without_premature_video(client,settings):
    approved_cast(client,settings);result=import_case(client);settings.h3_demo_mode=False
    with client.app.state.session_factory() as s:
        for e in s.scalars(select(m.ProductionEntity).where(m.ProductionEntity.project_id==result['project_id'])):e.status='approved'
        s.commit()
    response=client.post('/api/projects/'+result['project_id']+'/productions',json={'shot_ids':result['shot_ids'],'confirmed':True});assert response.status_code==201,response.text
    identifier=response.json()['id']
    def advance():
        with client.app.state.session_factory() as s:asyncio.run(advance_run(client.app,settings,s,s.get(m.ProductionRun,identifier)))
    advance();advance()
    with client.app.state.session_factory() as s:
        run=s.get(m.ProductionRun,identifier);assert len(run.result_json['scene_steps'])==2
        scenes=list(s.scalars(select(m.ProductionVisual)));assert len(scenes)==2
        assert not list(s.scalars(select(m.GenerationJob)))
        for v in scenes:
            import io
            b=io.BytesIO();Image.new('RGB',(64,64),'gray').save(b,format='PNG');store_visual(s,settings,v,b.getvalue());v.status='approved'
        s.commit()
    advance();advance()
    with client.app.state.session_factory() as s:
        run=s.get(m.ProductionRun,identifier)
        assert len({step['keyframe_id'] for step in run.steps_json})==2
        assert s.scalar(select(func.count()).select_from(m.ProductionVisual))==4
        assert run.status=='waiting_visual_review' and not list(s.scalars(select(m.GenerationJob)))
    assert client.post('/api/productions/'+identifier+'/approve-inputs',json={'operator':'isolated-test'}).status_code==409
    # Controlled durable-state regression: image retries must not consume the
    # video retry budget or discard a previously approved keyframe.
    with client.app.state.session_factory() as s:
        run=s.get(m.ProductionRun,identifier);steps=copy.deepcopy(run.steps_json);step=steps[0];keyframe=step['keyframe_id']
        run.result_json={**run.result_json,'attempt_history':[{**step,'category':'prop','repair_phase':'keyframe'} for _ in range(2)]}
        step['video_job_id']='isolated-history-marker-not-a-provider-job';step['stage']='quality_failed';run.steps_json=steps;run.status='quality_failed';s.commit()
    repaired=client.post('/api/productions/'+identifier+'/repair',json={'shot_id':result['shot_ids'][0],'category':'prop','reason':'隔离回归：仅修复视频道具状态，保留关键画面'})
    assert repaired.status_code==200,repaired.text
    assert repaired.json()['steps_json'][0]['keyframe_id']==keyframe and repaired.json()['steps_json'][0]['video_job_id'] is None
    assert repaired.json()['result_json']['attempt_history'][-1]['repair_phase']=='video'


def test_dialogue_only_edit_reuses_visual_geometry_but_invalidates_video_input_hash(client,settings):
    approved_cast(client,settings);result=import_case(client)
    with client.app.state.session_factory() as s:
        shot=s.get(m.StoryboardShot,result['shot_ids'][0]);visual_before=cinematic_rules.fingerprint(visual_inputs(s,shot));video_before=generation_inputs_hash(s,shot)
        line=s.scalar(select(m.DialogueLine).where(m.DialogueLine.shot_id==shot.id));identifier=line.id;participant=line.participant_id
    response=client.patch('/api/dialogues/'+identifier,json={'participant_id':participant,'text':'另一句审核后的台词。','order':1})
    assert response.status_code==200,response.text
    with client.app.state.session_factory() as s:
        shot=s.get(m.StoryboardShot,result['shot_ids'][0]);assert cinematic_rules.fingerprint(visual_inputs(s,shot))==visual_before
        assert generation_inputs_hash(s,shot)!=video_before


def test_actual_native_graph_has_multiple_distinct_reference_latents():
    snapshot={'seed':3,'service_snapshot':{'model_id':'explicit-isolated-model','settings':{'text_encoder':'isolated-encoder','vae':'isolated-vae'}},'parameters':{'width':1024,'height':576,'quality':'production'}}
    visual=SimpleNamespace(id='isolated-graph-test',prompt='Two distinct referenced adults.',snapshot_json=snapshot)
    graph=visual_graph(visual,['one.png','two.png','location.png'])
    assert graph['1000']['inputs']['image']=='one.png'
    assert graph['film_ref_1']['inputs']['image']=='two.png' and graph['film_ref_2']['inputs']['image']=='location.png'
    assert graph['film_ref_2_positive']['inputs']['conditioning']==['film_ref_1_positive',0]
    empty=visual_graph(visual,[]);assert not any(n['class_type']=='LoadImage' for n in empty.values())
    assert empty['63']['inputs']['positive']==['74',0]


def test_post_generation_spec_checks_have_no_fake_visual_score():
    job=SimpleNamespace(parameters_json={'screenplay_snapshot':{'cinematic':{'versions':{}}},'width':1024,'height':576,'duration_seconds':5})
    checks=[{'check_type':'media_decode','status':'passed','details':{'streams':[{'codec_type':'video','width':1024,'height':576,'avg_frame_rate':'24/1','nb_frames':'124'}],'format':{'duration':'5.166667'}}}]
    result=inspect_generated(job,checks);assert result[0]['status']=='passed' and result[1]['status']=='not_configured'
    assert result[1]['details']['numeric_film_quality_score'] is None
    checks[0]['details']['streams'][0]['width']=640;assert inspect_generated(job,checks)[0]['status']=='failed'
