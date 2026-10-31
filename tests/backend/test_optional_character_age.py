"""Ordinary character production must not require a declared adult age.

These tests compile/persist real API and pipeline inputs in isolated databases;
they never contact a provider or submit inference to the workstation GPU.
"""
import copy
import hashlib
from io import BytesIO
import re
from types import SimpleNamespace

from docx import Document
from PIL import Image
import pytest
from sqlalchemy import select

from app import models as m
from app.core.db import Base, build_engine, build_session_factory
from app.schemas_product import BatchRequest, IdentityProfile
from app.services.cinematic_rules import rules_bundle, generation_rules
from app.services.cinematic_quality import inspect_ir
from app.services.character_factory import candidate_language_adapter
from app.services.screenplay_generation import normalize_ir
from app.services.screenplay import generation_inputs
from app.services.studio_pipeline import _character, create_pipeline
from app.services.word_screenplay import canonical_profile
from app.services.identity_profiles import resolve_profile


CASES = [
    ({}, None),
    ({'description': '街头提供者', 'hair': '短黑发'}, None),
    ({'age': 0, 'description': '在监护人怀中休息的婴儿', 'original_adult': False}, 0),
    ({'age': 12, 'description': '学生在公园安静阅读', 'original_adult': False}, 12),
    ({'age': 150, 'description': '原创奇幻人物阅读地图', 'original_adult': False}, 150),
]


def image_service(client):
    with client.app.state.session_factory() as session:
        service = session.scalar(select(m.AIService).where(m.AIService.module == 'character_generation'))
        assert service is not None
        service.settings_json = {**service.settings_json, 'candidate_model': 'sd_xl_base_1.0.safetensors'}
        service.capabilities_json = {'text_to_image': {'state': 'detected', 'source': 'isolated contract fixture'}}
        session.commit()


def create_character(client, profile, identifier='optional-age-role'):
    response = client.post('/api/character-factory/characters', json={
        'id': identifier, 'name': '提供者', 'profile': profile,
    })
    assert response.status_code == 201, response.text
    return response.json()


def assert_unfabricated_age(prompt, age):
    assert 'None years old' not in prompt and 'null years old' not in prompt
    if age is None:
        assert not re.search(r'\b\d+ years old\b', prompt)
    else:
        assert f'{age} years old' in prompt
    if age is None or age < 18:
        assert not re.search(r'\badult\b', prompt, flags=re.I)


@pytest.mark.parametrize('profile,age', CASES)
def test_factory_create_and_candidate_preview_accept_optional_or_nonadult_ages(client, profile, age):
    created = create_character(client, profile)
    image_service(client)
    version = created['version']
    response = client.post('/api/characters/optional-age-role/batch-preview', json={
        'version_id': version['id'], 'kind': 'candidates', 'candidate_count': 1,
    })
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan['profile']['age'] == age
    prompt = plan['jobs'][0]['prompt']
    assert_unfabricated_age(prompt, age)
    assert not re.search(r'\b(child|teenager)\b', plan['jobs'][0]['negative_prompt'], re.I)
    for field in ('description', 'hair'):
        if profile.get(field):
            assert profile[field] in prompt
    with client.app.state.session_factory() as session:
        assert session.scalar(select(m.CharacterGenerationJob.id)) is None
        assert session.scalar(select(m.CharacterBatch.id)) is None
        assert session.get(m.Character, created['character']['id']).current_approved_version_id is None


@pytest.mark.parametrize('age', [None, 0, 12, 150])
def test_profile_update_retains_supplied_traits_without_adult_confirmation(client, age):
    created = create_character(client, {'age': 36, 'description': '原始资料'})
    profile = {'age': age, 'original_adult': False, 'description': '', 'eyes': '深褐色杏眼'}
    response = client.patch('/api/characters/optional-age-role/profile', json={
        'version_id': created['version']['id'], 'profile': profile,
    })
    assert response.status_code == 200, response.text
    value = response.json()['identity_json']['profile']
    assert value['age'] == age and value['eyes'] == '深褐色杏眼'
    assert value['description'] == '' and value.get('original_adult') is False


def test_profile_relaxation_does_not_unlock_published_identity(client):
    created = create_character(client, {})
    with client.app.state.session_factory() as session:
        version = session.get(m.CharacterVersion, created['version']['id'])
        version.approval_state = 'approved'
        session.commit()
    response = client.patch('/api/characters/optional-age-role/profile', json={
        'version_id': created['version']['id'], 'profile': {'age': 12, 'description': '其他身份'},
    })
    assert response.status_code == 409
    assert response.json()['error']['code'] == 'profile_locked'


def test_schema_has_no_default_age_or_adult_confirmation_and_keeps_custom_negative():
    profile = IdentityProfile()
    assert profile.age is None and profile.description == ''
    assert not profile.original_adult
    request = BatchRequest(version_id='draft', kind='candidates')
    assert not re.search(r'\b(child|teenager)\b', request.negative_prompt, re.I)
    custom = BatchRequest(version_id='draft', kind='candidates', negative_prompt='user-authored specific exclusion')
    assert custom.negative_prompt == 'user-authored specific exclusion'


@pytest.mark.parametrize('age', [None, 0, 12, 150])
def test_candidate_language_adapter_does_not_invent_an_adult_age(age):
    profile = IdentityProfile(age=age, description='在街头阅读的人物', hair='短黑发')
    prompt, receipt, warnings = candidate_language_adapter(profile, '深蓝棉布外套')
    assert_unfabricated_age(prompt, age)
    assert receipt['gender'] == 'unspecified'
    assert isinstance(warnings, list)


@pytest.mark.parametrize('age', [None, 0, 12, 150])
def test_word_profile_conversion_preserves_nonage_identity_fields(age):
    raw = {'description': '街头提供者', 'eyes': '深褐色杏眼', 'hair': '短黑发',
           'eyebrows': '平直眉', 'marks': '左脸小痣', 'immutable_traits': '短黑发；左脸小痣'}
    if age is not None:
        raw['age'] = age
    original = copy.deepcopy(raw)
    result = canonical_profile({'name': '提供者', 'profile': raw})
    assert result is not None and result['age'] == age
    assert result['description'] == raw['description'] and result['eyes'] == raw['eyes']
    assert '平直眉' in result['facial_features'] and result['identifying_marks'] == '左脸小痣'
    assert result['fixed_traits'] == ['短黑发', '左脸小痣']
    assert raw == original


def word_bytes(age):
    document = Document()
    lines = ['项目基本信息', '项目名称：年龄可选导入回归', '画幅：16:9', '人物设定',
             '人物 提供者', '人物ID：word_optional_age', '身份：街头提供者', '眼睛：深褐色杏眼',
             '外观：自然脸部轮廓', '固定身份特征：短黑发；左脸小痣']
    if age is not None:
        lines.append('年龄：' + str(age))
    lines += ['正文剧情', '场景 1 广场', '地点：广场', '分镜 1 安静阅读', '出场人物：提供者',
              '画面：提供者在长椅上安静阅读一本合上的书', '时长：5秒', '运镜：固定',
              '表演：提供者｜动作=看向书本；位置=中间；情绪=专注']
    for line in lines:
        document.add_paragraph(line)
    output = BytesIO()
    document.save(output)
    return output.getvalue()


@pytest.mark.parametrize('age', [None, 0, 12, 150])
def test_word_import_without_adult_age_keeps_a_usable_profile(client, age):
    response = client.post('/api/studio/import', data={'auto_start': 'false'},
        files={'file': ('optional-age.docx', word_bytes(age), 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['status'] == 'imported'
    with client.app.state.session_factory() as session:
        version = session.scalar(select(m.CharacterVersion).where(m.CharacterVersion.character_id == 'word_optional_age'))
        assert version is not None
        profile = version.identity_json['profile']
        assert profile['age'] == age and profile['eyes'] == '深褐色杏眼'
        assert not any('成年年龄' in value for value in version.identity_json.get('needs_confirmation', []))
        assert session.scalar(select(m.StudioRun.id)) is None


def minimal_ir(profile):
    return {'project': {'synopsis': '人物安静阅读。', 'style': '真实实拍', 'aspect_ratio': '16:9'},
        'characters': [{'key': 'person', 'name': '提供者', 'profile': profile}],
        'scenes': [{'key': 'park', 'name': '公园', 'order': 1, 'environment': {'lighting': '日光'}}],
        'shots': [{'key': 'shot', 'scene_key': 'park', 'title': '阅读', 'duration_seconds': 5,
            'visual_prompt': '人物坐在长椅上阅读一本书，衣物与环境材质真实自然。', 'action': '阅读',
            'participants': [{'character_key': 'person', 'performance': {'action': '阅读', 'position': 'center'}}],
            'dialogues': [], 'photography': {'shot_size': '中景'}, 'production': {}}]}


@pytest.mark.parametrize('age', [None, 0, 12, 150])
def test_provider_screenplay_normalization_accepts_optional_age_without_fabrication(client, age):
    profile = {'description': '广场提供者', 'hair': '短黑发'}
    if age is not None:
        profile['age'] = age
    raw = minimal_ir(profile)
    original = copy.deepcopy(raw)
    job = SimpleNamespace(id='spjob_optional_age', project_name='年龄可选', input_mode='text', scope='all',
        snapshot_json={'rules_snapshot': rules_bundle()})
    with client.app.state.session_factory() as session:
        ir = normalize_ir(session, job, raw, {})
    actual = ir['characters'][0]['profile']
    assert actual.get('age') == age and actual['hair'] == profile['hair']
    assert raw == original


def test_legacy_null_profile_recovers_source_traits_and_builds_only_new_snapshot(settings):
    """No TestClient/background worker is started, so queued jobs cannot infer."""
    settings.h3_demo_mode = False
    engine = build_engine(settings.database_url)
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    try:
        with factory() as session:
            service = m.AIService(module='character_generation', name='isolated input compiler', provider='comfyui',
                base_url='http://127.0.0.1:8189', is_default=True, default_model='sd_xl_base_1.0.safetensors',
                settings_json={'candidate_model': 'sd_xl_base_1.0.safetensors', 'concurrency': 1},
                capabilities_json={'text_to_image': {'state': 'detected'}})
            project = m.Project(name='旧资料兼容回归')
            character = m.Character(slug='legacy-null-profile', display_name='提供者', description='街头提供者')
            session.add_all([service, project, character]);session.flush()
            source = {'description': '街头提供者', 'eyes': '深褐色杏眼', 'hair': '短黑发', 'marks': '左脸小痣'}
            old_identity = {'profile': None, 'source_profile': source, 'import_id': 'legacy-import',
                'needs_confirmation': ['明确成年年龄及结构化身份档案']}
            version = m.CharacterVersion(character_id=character.id, version=1, identity_json=copy.deepcopy(old_identity))
            shot = m.StoryboardShot(project_id=project.id, order=1, scene_title='阅读', aspect_ratio='16:9')
            session.add_all([version, shot]);session.flush()
            session.add(m.ShotCharacter(shot_id=shot.id, character_id=character.id,
                character_version_id=version.id, order=1, performance_json={'action': '安静阅读'}))
            session.commit()
            run = create_pipeline(session, settings, project.id, {'auto_start': False})
            entry = next(item for item in run.nodes_json if item['kind'] == 'character')
            _character(session, settings, run, entry, dispatch=True)
            session.flush()
            assert entry['child_id'] and entry['status'] == 'running'
            batch = session.get(m.CharacterBatch, entry['child_id'])
            new_version = session.get(m.CharacterVersion, batch.character_version_id)
            assert new_version.id != version.id
            profile = batch.snapshot_json['profile']
            assert profile['age'] is None and profile['eyes'] == source['eyes']
            assert profile['hair'] == source['hair'] and profile['identifying_marks'] == source['marks']
            assert new_version.identity_json['source_profile'] == source
            assert new_version.identity_json['profile']['age'] is None
            session.refresh(version)
            assert version.identity_json == old_identity
            job = session.scalar(select(m.CharacterGenerationJob).where(m.CharacterGenerationJob.batch_id == batch.id))
            assert job.status == 'queued' and job.provider_job_id is None
            assert_unfabricated_age(job.prompt, None)
            assert source['hair'] in job.prompt and source['eyes'] in job.prompt
    finally:
        engine.dispose()


@pytest.mark.parametrize('age', [None, 12])
def test_video_prompt_preserves_unspecified_or_nonadult_age_without_adult_bias(client, settings, age):
    created=create_character(client, {'age':age,'description':'在广场安静阅读的人物','hair':'短黑发'})
    with client.app.state.session_factory() as session:
        character=session.get(m.Character,created['character']['id'])
        version=session.get(m.CharacterVersion,created['version']['id'])
        picture=BytesIO();Image.new('RGB',(64,64),'#607080').save(picture,'PNG');data=picture.getvalue()
        settings.media_root.mkdir(exist_ok=True,parents=True)
        path=settings.media_root/'optional-age-face.png';path.write_bytes(data)
        asset=m.CharacterAsset(character_version_id=version.id,asset_type='front_face',relative_path=path.name,
            mime_type='image/png',sha256=hashlib.sha256(data).hexdigest(),width=64,height=64,
            status='approved',is_primary_reference=True,source='uploaded')
        project=m.Project(name='普通全龄人物输入编译回归')
        session.add_all([asset,project]);session.flush()
        version.approval_state='approved';version.standard_face_asset_id=asset.id
        character.current_approved_version_id=version.id
        shot=m.StoryboardShot(project_id=project.id,order=1,scene_title='广场阅读',
            visual_prompt='人物在长椅上安静阅读。',action='quietly reads a book',aspect_ratio='16:9')
        session.add(shot);session.flush()
        session.add(m.ShotCharacter(shot_id=shot.id,character_id=character.id,character_version_id=version.id,
            order=1,reference_asset_ids_json=[asset.id],performance_json={'action':'quietly reads a book','position':'center'}))
        session.commit()
        _,refs,prompt,snapshot=generation_inputs(session,shot,settings)
        assert [ref['asset_id'] for ref in refs]==[asset.id]
        assert snapshot['reference_manifest']['actors'][0]['profile']['age']==age
        assert not re.search(r'\badult\b',prompt,re.I)
        assert 'None-year-old' not in prompt and 'None years old' not in prompt
        if age is not None:assert f'{age}-year-old' in prompt


def test_cached_stock_adult_rule_is_migrated_only_in_future_input_copy():
    current=rules_bundle()
    snapshot=copy.deepcopy(current)
    snapshot['source_sha256']='original-script-source-sha'
    snapshot['versions']['production_rules_version']='cinematic-1.0.0'
    snapshot['versions']['video_prompt_compiler_version']='cinematic-compiler-1.0.0'
    snapshot['visual_language']['identity']='A believable adult with the referenced facial geometry, natural asymmetry, pores and age cues. Preserve identity across costume and emotional changes. Do not beautify, remodel facial proportions, invent markings, or smooth all skin texture.'
    snapshot['visual_language']['camera']='User-authored camera instruction kept exactly.'
    original=copy.deepcopy(snapshot)
    effective=generation_rules(snapshot)
    assert snapshot==original
    assert effective['visual_language']['identity']==current['visual_language']['identity']
    assert not re.search(r'\badult\b',effective['visual_language']['identity'],re.I)
    assert effective['visual_language']['camera']==snapshot['visual_language']['camera']
    assert effective['legacy_rule_migration']['original_source_sha256']=='original-script-source-sha'
    assert effective['versions']['production_rules_version']==current['versions']['production_rules_version']


def test_user_authored_age_direction_is_not_removed_as_a_stock_rule():
    snapshot=copy.deepcopy(rules_bundle())
    snapshot['visual_language']['identity']='User-approved adult ensemble in an ordinary workplace scene.'
    original=copy.deepcopy(snapshot)
    effective=generation_rules(snapshot)
    assert effective==original and snapshot==original
    assert 'legacy_rule_migration' not in effective


def test_unspecified_age_is_not_a_required_review_finding():
    ir=minimal_ir({'description':'在公园阅读的人物'})
    ir['characters'][0]['decision']='new'
    result=inspect_ir(ir)
    assert not any(finding['code']=='age_unknown' for finding in result['findings'])


def test_empty_legacy_profile_recovers_source_traits_without_inventing_age():
    identity={'profile':{},'source_profile':{'eyes':'深褐色杏眼','hair':'短黑发'},'source':['original-word']}
    original=copy.deepcopy(identity)
    profile=resolve_profile(identity,name='提供者')
    assert profile.age is None
    assert profile.hair=='短黑发' and profile.eyes=='深褐色杏眼'
    assert identity==original


def test_generic_legacy_profile_with_textual_unknown_age_remains_usable_and_unchanged():
    identity={'profile':{'age':'未知','description':'街头提供者','hair':'短黑发','eyes':'深褐色杏眼'},
        'provenance':{'source':'legacy-versions-api'}}
    original=copy.deepcopy(identity)
    profile=resolve_profile(identity,name='提供者')
    assert profile.age is None and profile.hair=='短黑发' and profile.eyes=='深褐色杏眼'
    assert '未知' in profile.description
    assert identity==original


def test_legacy_word_false_profile_ready_reaches_real_preview_without_age_gate(client, settings, monkeypatch):
    from app.core.errors import AppError
    from app.services import character_factory
    created=create_character(client,{'description':'街头提供者'})
    image_service(client)
    source={'description':'街头提供者','hair':'短黑发','eyes':'深褐色杏眼'}
    with client.app.state.session_factory() as session:
        version=session.get(m.CharacterVersion,created['version']['id'])
        version.identity_json={'profile':None,'source_profile':source,'needs_confirmation':['明确成年年龄及结构化身份档案']}
        record=m.WordImport(filename='old-completed-import.docx',relative_path='not-read-by-idempotent-confirm.docx',
            sha256='a'*64,status='completed',result_json={'status':'imported','new_characters':[
                {'character_id':created['character']['id'],'version_id':version.id,'profile_ready':False}]})
        session.add(record);session.commit();record_id=record.id;revision=record.revision
    real_build=character_factory.build_batch_plan
    plans=[]
    def preview_only(*args,**kwargs):
        plans.append(real_build(*args,**kwargs))
        # Deliberately stop after compiling the actual plan, before any queue
        # entry or inference request can be created by the confirm endpoint.
        raise AppError(409,'isolated_preview_only','isolated real input preview reached; no inference submitted')
    monkeypatch.setattr(character_factory,'build_batch_plan',preview_only)
    monkeypatch.setattr(settings,'h3_demo_mode',False)
    response=client.post(f'/api/word-imports/{record_id}/confirm',json={
        'revision':revision,'duplicate_strategy':'new_project','generate_standard_faces':True})
    assert response.status_code==200,response.text
    requests=response.json()['asset_requests']
    assert len(plans)==1 and requests[0]['reason']=='isolated real input preview reached; no inference submitted'
    assert plans[0]['profile']['age'] is None and plans[0]['profile']['hair']==source['hair']
    assert_unfabricated_age(plans[0]['jobs'][0]['prompt'],None)
    with client.app.state.session_factory() as session:
        assert session.scalar(select(m.CharacterGenerationJob.id)) is None
