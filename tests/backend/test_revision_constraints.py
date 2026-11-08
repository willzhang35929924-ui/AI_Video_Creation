import copy
import json

import pytest

from app.services.revision_constraints import (
    apply_revision_overrides, enforce_revision_constraints, extract_revision_constraints,
    revision_constraints_text,
)


TARGETS = [
    {'id':'prop-1','logical_id':'P01','name':'Box','kind':'prop','spec':{'dimensions':'高20cm，宽30cm'}},
    {'id':'prop-2','logical_id':'P02','name':'Green tree','kind':'prop','spec':{'dimensions':'高约12.5cm，宽约15cm','color':'green'}},
]
BODY = ('subject_definitions:\n<Subject 1> is a box from <Picture 1>.\n'
        '<Subject 2> is the green plush from <Picture 2>.\n\nsummary:\nA child squeezes the plush.\n\n'
        'retention_analysis:\n<Subject 2> fully_preserved.\n\ndetailed_description:\n'
        'A photographic live-action style.\n[Shot 1] The camera holds static. A child squeezes <Subject 2>.\n\n'
        'overall_soundscape:\nSoft room tone.\n\nnon_diegetic_music:\nN/A\n')


@pytest.mark.parametrize(('instruction','height','width','relative'), [
    ('其他内容不变，P02毛绒玩具尺寸高12.5cm，宽 15cm。按压玩具凹陷 3cm 后松开。','12.5','15',None),
    ('其他内容不变，P02毛绒玩具尺寸高3.5cm，宽 5cm。按压玩具凹陷后松开。','3.5','5',None),
    ('P02毛绒玩具尺寸只有人物甲的两个手掌大小。',None,None,'two_palms'),
    ('P02毛绒玩具尺寸只有人物甲的一个手掌大小。',None,None,'one_palm'),
    ('P02毛绒玩具尺寸只有水杯大小。',None,None,'drinking_cup'),
    ('Make P02 height 35mm and width 5cm.','35','5',None),
    ('P02尺寸改为3.5×5cm。','3.5','5',None),
    ('Make P02 the size of two human palms.',None,None,'two_palms'),
    ('P02 is two-palm-sized.',None,None,'two_palms'),
    ('P02 is palm-sized.',None,None,'one_palm'),
])
def test_explicit_product_scale_survives_all_real_revision_forms(instruction,height,width,relative):
    constraints=extract_revision_constraints(instruction,targets=TARGETS)
    assert len(constraints['items'])==1
    item=constraints['items'][0]
    assert item['target_key']=='P02' and item['target_entity_id']=='prop-2'
    values={entry['axis']:entry['value'] for entry in item['measurements']}
    assert values.get('height')==height and values.get('width')==width
    assert item.get('relative_scale')==relative
    updated,receipt=enforce_revision_constraints(BODY,constraints,{'P02':'<Subject 2>'})
    detail=updated.split('detailed_description:\n')[1].split('overall_soundscape:')[0]
    assert receipt['source']=='caller_revision_constraints'
    assert all(clause['text'] in detail for clause in receipt['clauses'])
    assert detail.index('[Shot 1]') < detail.index('Current caller-authored product scale')
    assert updated.endswith('non_diegetic_music:\nN/A\n')
    assert json.loads(json.dumps(constraints))==constraints


@pytest.mark.parametrize('instruction',[
    '使用35mm镜头，2800K灯光，镜头4秒，输出576×1024。',
    'P02保持原尺寸，用两个手掌挤压。',
    'Hold P02 with two palms while the camera pans left.',
    'Camera height 1.6m, 35mm lens, 4 seconds.',
    '对白：“P02尺寸高3.5cm宽5cm。”',
    '台词改为“P02高3.5cm”。镜头固定。',
    'dialogue: "P02 height 3.5cm"',
    'P02加一点反光，其他内容保持。',
    'P02不要高12.5cm，也不要一个手掌大小。',
    'P02 height must be less than 15cm.',
    'P02 should not be the size of one human palm.',
    'P02放在台面，台面高度100cm。',
    'P02 stands on a table height 100cm.',
])
def test_normal_instructions_camera_settings_and_spoken_numbers_are_not_product_scale(instruction):
    assert extract_revision_constraints(instruction,targets=TARGETS)['items']==[]


def test_latest_instruction_overrides_editable_prompt_without_carrying_its_old_scale():
    constraints=extract_revision_constraints('P02只有水杯大小。',prompt_override='P02尺寸高12.5cm，宽15cm。',targets=TARGETS)
    item=constraints['items'][0]
    assert item['measurements']==[] and item['relative_scale']=='drinking_cup'
    assert item['source']=='instruction'
    assert '12.5' not in revision_constraints_text(constraints)


def test_only_targeted_generated_copies_change_and_original_script_stays_intact():
    original=copy.deepcopy(TARGETS)
    action='P01沿边凹陷3cm；P02腹部凹陷3cm后松开。'
    constraints=extract_revision_constraints('P02高3.5cm，宽5cm。按压后回弹。',targets=TARGETS)
    updated,rows,audit=apply_revision_overrides(action,TARGETS,constraints)
    assert rows[0]==TARGETS[0]
    assert rows[1]['spec']['dimensions']=='高 3.5cm，宽 5cm'
    assert rows[1]['spec']['color']=='green'
    assert 'P01沿边凹陷3cm' in updated and 'P02腹部凹陷后松开' in updated
    assert TARGETS==original and action=='P01沿边凹陷3cm；P02腹部凹陷3cm后松开。'
    assert audit['action_changed'] and audit['entity_fields'][0]['before']=='高约12.5cm，宽约15cm'


def test_explicit_requested_deformation_is_retained_even_in_the_following_sentence():
    constraints=extract_revision_constraints('P02高12.5cm，宽15cm。按压玩具凹陷3cm后松开。',targets=TARGETS)
    assert constraints['items'][0]['deformation_depth']=={'value':'3','unit':'cm'}
    updated,_,_=apply_revision_overrides('P02腹部凹陷2cm后松开。',TARGETS,constraints)
    assert '凹陷 3cm' in updated


def test_one_axis_override_keeps_unmodified_axis_and_other_spec_fields():
    constraints=extract_revision_constraints('P02高度3.5cm。',targets=TARGETS)
    _,rows,_=apply_revision_overrides('',TARGETS,constraints)
    assert '宽约15cm' in rows[1]['spec']['dimensions'] and '12.5' not in rows[1]['spec']['dimensions']
    assert rows[0]==TARGETS[0]


def test_relative_scale_replaces_old_direct_numeric_dimensions_only_on_target():
    rows=copy.deepcopy(TARGETS);rows[1]['spec'].update(height_cm=12.5,width_cm=15)
    constraints=extract_revision_constraints('P02一个手掌大小。',targets=rows)
    _,result,_=apply_revision_overrides('',rows,constraints)
    assert 'height_cm' not in result[1]['spec'] and 'width_cm' not in result[1]['spec']
    assert 'one human palm' in result[1]['spec']['dimensions']
    assert rows[1]['spec']['height_cm']==12.5 and result[0]==rows[0]


def test_independent_product_targets_do_not_merge_or_share_a_subject():
    constraints=extract_revision_constraints('P01高20cm，P02高3.5cm宽5cm。',targets=TARGETS)
    assert [i['target_key'] for i in constraints['items']]==['P01','P02']
    with pytest.raises(ValueError,match='cannot share'):
        enforce_revision_constraints(BODY,constraints,{'P01':'<Subject 1>','P02':'<Subject 1>'})
    updated,receipt=enforce_revision_constraints(BODY,constraints,{'P01':'<Subject 1>','P02':'<Subject 2>'})
    assert 'height of <Subject 1> is 20 cm' in updated
    assert 'height of <Subject 2> is 3.5 cm' in updated and len(receipt['clauses'])==2


@pytest.mark.parametrize('subject_map',[{}, {'P02':'anything'}, {'P02':'<Subject 99>'}])
def test_missing_invalid_or_unwritten_subject_binding_blocks_sampling(subject_map):
    constraints=extract_revision_constraints('P02高3.5cm宽5cm。',targets=TARGETS)
    with pytest.raises(ValueError):enforce_revision_constraints(BODY,constraints,subject_map)


def test_enforcement_is_idempotent_and_does_not_change_speech_or_other_sections():
    body=BODY.replace('Soft room tone.','Dialogue: "35cm is the old package size."')
    constraints=extract_revision_constraints('P02高3.5cm宽5cm。',targets=TARGETS)
    updated,_=enforce_revision_constraints(body,constraints,{'P02':'<Subject 2>'})
    twice,_=enforce_revision_constraints(updated,constraints,{'P02':'<Subject 2>'})
    assert twice==updated
    assert updated.split('detailed_description:')[0]==body.split('detailed_description:')[0]
    assert updated.split('overall_soundscape:')[1]==body.split('overall_soundscape:')[1]


def test_no_constraint_no_rewriting():
    constraints=extract_revision_constraints('Keep the camera still.',targets=TARGETS)
    action,rows,audit=apply_revision_overrides('P02凹陷3cm。',TARGETS,constraints)
    assert action=='P02凹陷3cm。' and rows==TARGETS and not audit['action_changed']
    assert enforce_revision_constraints(BODY,constraints,{})[0]==BODY


def test_product_identifiers_are_exact_and_not_prefix_matches():
    constraints=extract_revision_constraints('P020高度3.5cm。',targets=TARGETS)
    assert constraints['items'][0]['target_key']=='P020'
    assert 'target_entity_id' not in constraints['items'][0]
    _,rows,_=apply_revision_overrides('',TARGETS,constraints)
    assert rows==TARGETS


def test_a_previous_constraint_overlay_is_not_silently_accumulated_with_a_new_size():
    old=extract_revision_constraints('P02高12.5cm宽15cm。',targets=TARGETS)
    new=extract_revision_constraints('P02高3.5cm宽5cm。',targets=TARGETS)
    written,_=enforce_revision_constraints(BODY,old,{'P02':'<Subject 2>'})
    with pytest.raises(ValueError,match='fresh revision'):
        enforce_revision_constraints(written,new,{'P02':'<Subject 2>'})
