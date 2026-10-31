import copy
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app import models as m
from app.core.db import Base, build_engine, build_session_factory
from app.schemas_screenplay import ImportConfirm
from app.services.cinematic_quality import inspect_ir, shot_preflight
from app.services.screenplay_production_bindings import bind_production_designs
from app.services.word_screenplay import commit_import


def sample():
    return {'characters': [{'key': 'A'}, {'key': 'B'}], 'designs': [],
            'shots': [{'key': 'S1', 'scene_key': 'room',
                       'participants': [{'character_key': 'A'}, {'character_key': 'B'}],
                       'production': {'wardrobe_states': {'A': {'description': '深蓝工作服', 'wetness': '湿'}},
                                      'prop_states': [{'name': '记录本', 'state': '合上', 'holder': 'A'}]}}]}


def wardrobe(key='W1', owner='A', description='深蓝工作服'):
    return {'key': key, 'kind': 'wardrobe', 'name': description,
            'character_key': owner, 'spec': {'description': description}}


def test_missing_records_are_derived_from_original_words_and_reused_stably():
    original = sample()
    original['shots'].append(copy.deepcopy(original['shots'][0]))
    original['shots'][1]['key'] = 'S2'
    original['shots'][1]['production']['prop_states'][0]['state'] = '打开'
    before = copy.deepcopy(original)
    result = bind_production_designs(original, 'lineage-one')
    assert original == before
    assert len(result['designs']) == 2
    first, second = [shot['production'] for shot in result['shots']]
    assert first['prop_states'][0]['prop_key'] == second['prop_states'][0]['prop_key']
    assert first['prop_states'][0]['state'] == '合上'
    assert second['prop_states'][0]['state'] == '打开'
    assert first['wardrobe_states']['A']['wetness'] == '湿'
    assert all(set(design['spec']) == {'description'} for design in result['designs'])
    assert all(design['scene_key'] == 'room' for design in result['designs'])
    assert all(design['provenance']['description']['state'] == 'suggested' for design in result['designs'])
    assert all(design['provenance']['description']['confidence'] == .5 for design in result['designs'])
    assert all(design['source'] == ['generation:lineage-one'] for design in result['designs'])
    assert bind_production_designs(result, 'lineage-one') == result
    assert bind_production_designs(original, 'lineage-one') == result
    assert result['characters'] == original['characters']


def test_unique_owned_wardrobe_and_exactly_matching_prop_are_linked():
    source = sample()
    source['designs'] = [wardrobe(description='既有工作服'),
                         {'key': 'P1', 'kind': 'prop', 'name': '记录本', 'spec': {}}]
    result = bind_production_designs(source, 'one')
    assert result['designs'] == source['designs']
    assert result['shots'][0]['production']['wardrobe_states']['A']['wardrobe_key'] == 'W1'
    assert result['shots'][0]['production']['wardrobe_states']['A']['description'] == '深蓝工作服'
    assert result['shots'][0]['production']['prop_states'][0]['prop_key'] == 'P1'


def test_multiple_owned_wardrobes_require_a_unique_exact_text_match():
    source = sample()
    source['designs'] = [wardrobe(), wardrobe('W2', description='白色衬衫')]
    result = bind_production_designs(source, 'one')
    assert result['shots'][0]['production']['wardrobe_states']['A']['wardrobe_key'] == 'W1'
    source['shots'][0]['production']['wardrobe_states']['A']['description'] = '其他服装'
    with pytest.raises(ValueError, match='无法唯一对应'):
        bind_production_designs(source, 'one')


@pytest.mark.parametrize('design,key,message', [
    (wardrobe(), 'missing', '不存在'),
    ({'key': 'W1', 'kind': 'prop', 'name': '记录本'}, 'W1', '类型'),
    (wardrobe(owner='B'), 'W1', '另一人物'),
])
def test_explicit_wardrobe_references_never_guess_or_cross_roles(design, key, message):
    source = sample()
    source['designs'] = [design]
    source['shots'][0]['production']['wardrobe_states']['A']['wardrobe_key'] = key
    original = copy.deepcopy(source)
    with pytest.raises(ValueError, match=message):
        bind_production_designs(source, 'one')
    assert source == original


@pytest.mark.parametrize('key,kind', [('missing', 'prop'), ('P1', 'wardrobe')])
def test_explicit_prop_key_must_exist_and_be_a_prop(key, kind):
    source = sample()
    source['designs'] = [{'key': 'P1', 'kind': kind, 'name': '记录本'}]
    source['shots'][0]['production']['prop_states'][0]['prop_key'] = key
    with pytest.raises(ValueError, match='不存在|类型'):
        bind_production_designs(source, 'one')


def test_ambiguous_same_named_prop_is_not_silently_selected():
    source = sample()
    source['designs'] = [{'key': key, 'kind': 'prop', 'name': '记录本'} for key in ('P1', 'P2')]
    with pytest.raises(ValueError, match='对应多个'):
        bind_production_designs(source, 'one')


def test_explicit_prop_character_ownership_is_validated():
    source = sample()
    source['designs'] = [{'key': 'P1', 'kind': 'prop', 'name': '记录本', 'character_key': 'B'}]
    source['shots'][0]['production']['prop_states'][0].update(prop_key='P1', character_key='A')
    with pytest.raises(ValueError, match='道具设定属于另一人物'):
        bind_production_designs(source, 'one')


@pytest.mark.parametrize('kind,field', [('wardrobe', 'wardrobe_key'), ('prop', 'prop_key')])
def test_explicit_design_for_another_scene_is_rejected(kind, field):
    source = sample()
    source['designs'] = [{'key': 'D1', 'kind': kind, 'name': '原文',
                          'character_key': 'A' if kind == 'wardrobe' else None,
                          'scene_key': 'another_room'}]
    production = source['shots'][0]['production']
    target = production['wardrobe_states']['A'] if kind == 'wardrobe' else production['prop_states'][0]
    target[field] = 'D1'
    original = copy.deepcopy(source)
    with pytest.raises(ValueError, match='属于另一场景'):
        bind_production_designs(source, 'one')
    assert source == original


def test_auto_binding_does_not_use_or_modify_designs_scoped_to_other_scene():
    source = sample()
    source['designs'] = [wardrobe(), {'key': 'P1', 'kind': 'prop', 'name': '记录本'}]
    for design in source['designs']:
        design['scene_key'] = 'another_room'
    source['shots'][0]['source'] = ['file:unverified.docx', 'human:reviewed']
    source['shots'][0]['production']['prop_states'][0]['source_ids'] = ['approved:invented']
    original = copy.deepcopy(source)
    result = bind_production_designs(source, 'one')
    assert result['designs'][:2] == original['designs']
    assert len(result['designs']) == 4
    assert all(design['scene_key'] == 'room' for design in result['designs'][2:])
    assert all(design['source'] == ['generation:one'] for design in result['designs'][2:])
    assert result['shots'][0]['production']['prop_states'][0]['state'] == '合上'
    source['shots'][0]['production']['wardrobe_states']['A'] = {}
    with pytest.raises(ValueError, match='缺少明确名称或描述'):
        bind_production_designs(source, 'one')


def test_explicit_scene_state_for_another_scene_is_rejected():
    source = sample()
    source['designs'] = [{'key': 'E1', 'kind': 'scene_state', 'name': '夜间', 'scene_key': 'other'}]
    source['shots'][0]['production']['scene_state_key'] = 'E1'
    with pytest.raises(ValueError, match='另一场景'):
        bind_production_designs(source, 'one')


def test_duplicate_design_keys_and_ambiguous_generic_wardrobes_are_visible_errors():
    source = sample()
    source['designs'] = [wardrobe(), wardrobe()]
    with pytest.raises(ValueError, match='编号重复'):
        bind_production_designs(source, 'one')
    source['designs'] = [wardrobe('W1', owner=None), wardrobe('W2', owner=None)]
    with pytest.raises(ValueError, match='无法唯一对应'):
        bind_production_designs(source, 'one')


def test_empty_prose_and_nonparticipant_wardrobe_are_rejected():
    source = sample()
    source['shots'][0]['production']['prop_states'] = [{}]
    with pytest.raises(ValueError, match='缺少明确名称或描述'):
        bind_production_designs(source, 'one')
    source = sample()
    source['shots'][0]['participants'] = [{'character_key': 'B'}]
    with pytest.raises(ValueError, match='未在本镜出场'):
        bind_production_designs(source, 'one')


def test_partial_revision_keeps_other_shots_and_existing_designs_exactly():
    source = sample()
    source['shots'].append(copy.deepcopy(source['shots'][0]))
    source['shots'][1]['key'] = 'S2'
    source['designs'] = [wardrobe()]
    original = copy.deepcopy(source)
    result = bind_production_designs(source, 'one', shot_keys={'S2'})
    assert result['shots'][0] == original['shots'][0]
    assert result['designs'][0] == original['designs'][0]
    assert len(result['designs']) == 2
    assert source == original
    assert bind_production_designs(source, 'one', shot_keys=set()) == source
    with pytest.raises(ValueError, match='不存在的分镜'):
        bind_production_designs(source, 'one', shot_keys={'absent'})


def test_saved_actual_text_ir_imports_without_missing_design_blockers(settings):
    artifact = Path(__file__).resolve().parents[2] / 'reports/screenplay_incremental/real_generation.json'
    if not artifact.exists():
        pytest.skip('Real local generation artifact is not part of this checkout')
    report = json.loads(artifact.read_text(encoding='utf-8'))
    original = report['cases']['text_main_retry']['job']['screenplay']
    assert any(f['code'] == 'wardrobe_undefined' for f in inspect_ir(original)['findings'])
    repaired = bind_production_designs(original, 'actual-text-main-retry')
    assert repaired['designs'][:len(original['designs'])] == original['designs']
    # The saved real IR incorrectly scoped one character's wardrobe to each
    # scene while both characters occur in both. Do not rewrite that evidence:
    # create new applicable drafts only from each shot's explicit description.
    assert len(repaired['designs']) > len(original['designs'])
    designs = {design['key']: design for design in repaired['designs']}
    for shot in repaired['shots']:
        states = shot['production']['wardrobe_states'].values()
        assert all(designs[state['wardrobe_key']].get('scene_key') in {None, shot['scene_key']} for state in states)
        assert all(designs[state['prop_key']].get('scene_key') in {None, shot['scene_key']}
                   for state in shot['production']['prop_states'])
    assert not {f['code'] for f in inspect_ir(repaired)['findings']} & {'wardrobe_undefined', 'prop_undefined'}
    engine = build_engine(settings.database_url)
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    with factory() as session:
        batch = m.WordImport(filename='actual-model-output.docx', sha256='a' * 64,
                             status='pending_review', relative_path='test/not-used.docx',
                             blocks_json=[], preview_json=repaired, revision=1)
        session.add(batch)
        session.flush()
        imported = commit_import(session, batch, ImportConfirm(
            revision=1, duplicate_strategy='new_project', reuse_approved=False))
        session.flush()
        shots = list(session.scalars(select(m.StoryboardShot).where(
            m.StoryboardShot.project_id == imported['project_id'])))
        assert len(shots) == 2
        for shot in shots:
            preflight = shot_preflight(session, settings, shot, require_visual=False)
            assert not {f['code'] for f in preflight['findings']} & {
                'wardrobe_undefined', 'prop_undefined', 'design_missing'}
            detail = session.scalar(select(m.ShotDetail).where(m.ShotDetail.shot_id == shot.id))
            production = detail.source_json['production']
            assert all(state['entity_id'] for state in production['wardrobe_states'].values())
            assert all(state['entity_id'] for state in production['prop_states'])
        session.rollback()
