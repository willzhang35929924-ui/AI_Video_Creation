"""Caller face-height comparisons reach the real scale overlay; no inference."""
from copy import deepcopy
from pathlib import Path

import pytest

from app.services.revision_constraints import (
    apply_revision_overrides, enforce_revision_constraints, extract_revision_constraints,
    revision_constraints_text,
)
from app.services.revision_semantics import extract_scene_requirements, revision_semantic_findings


TARGETS=[
    {'id':'green','logical_id':'P02','name':'Green tree','kind':'prop',
     'spec':{'dimensions':'高12.5cm，宽15cm','height_cm':12.5,'width_cm':15,'color':'green'}},
    {'id':'other','logical_id':'P03','name':'Other toy','kind':'prop',
     'spec':{'dimensions':'高30cm，宽20cm','height_cm':30,'width_cm':20}},
]
PROMPT=('subject_definitions:\n<Subject 1> is the green plush tree from <Picture 1>.\n'
        '<Subject 2> is another toy from <Picture 2>.\n\nsummary:\nA product demonstration.\n\n'
        'retention_analysis:\n<Subject 1>: fully_preserved - appearance only.\n\n'
        'detailed_description:\nThe target video uses a photographic live-action style.\n'
        '[Shot 1] The camera holds static. At the first frame <Subject 1> rests in the open left palm, '
        'with the hand and the person\'s face visible in the same frame. '
        'Then the person raises <Subject 1> to the cheek.\n\n'
        'overall_soundscape:\nRoom tone.\n\nnon_diegetic_music:\nN/A\n')


@pytest.mark.parametrize('instruction',[
    'P02整体高度约为脸部高度的一半。',
    'P02的总高度为人物甲脸部高度的二分之一。',
    'P02整体高度为脸部高度的1/2。',
    'P02高度是人脸高度的0.5倍。',
    "P02's total height is half the height of the person's face.",
    "P02 height equals one half of the person's face height.",
    'P02 overall height is 0.5 of human face height.',
    "P02 is half the height of the person's face.",
])
def test_explicit_half_face_height_is_independent_of_cup_and_preserves_the_exact_caller_clause(instruction):
    result=extract_revision_constraints(instruction,targets=TARGETS)
    assert len(result['items'])==1
    item=result['items'][0]
    assert item['target_key']=='P02' and item['target_entity_id']=='green'
    assert item['face_height_fraction']=='0.5' and item['measurements']==[]
    assert 'relative_scale' not in item
    assert item['face_height_fraction_source']=='instruction'
    assert item['face_height_fraction_source_text'] in instruction
    assert 'half the height of the person\'s face' in revision_constraints_text(result)


@pytest.mark.parametrize('instruction',[
    'P02和P03整体高度为脸部高度的一半。',
    'P02、P03整体高度为脸部高度的一半。',
    "P02 and P03 total height is half the height of the person's face.",
    'P02整体高度不要为脸部高度的一半。',
    'P02整体高度约为脸部高度的一半或者三分之一。',
    "P02 total height is not half the height of the person's face.",
    'P02手掌高度为脸部高度的一半。',
    "P02's palm height is half the height of the person's face.",
    'P02脸部高度为人物甲脸部高度的一半。',
    'P02旁边人物高度为脸部高度的一半。',
    'P02总高度为手掌高度的一半。',
    'P02整体高度为头部高度的一半。',
    'P02高度为画面高度的一半。',
    'P02占画面高度50%。',
    '对白：“P02整体高度约为脸部高度的一半。”',
])
def test_ambiguous_negative_body_part_or_framing_numbers_do_not_become_product_face_comparisons(instruction):
    assert extract_revision_constraints(instruction,targets=TARGETS)['items']==[]


def test_cup_does_not_imply_a_face_fraction_and_independent_targets_do_not_borrow_it():
    cup=extract_revision_constraints('P02只有水杯大小。',targets=TARGETS)['items'][0]
    assert cup['relative_scale']=='drinking_cup' and 'face_height_fraction' not in cup
    result=extract_revision_constraints('P02高10cm，P03整体高度为脸部高度的一半。',targets=TARGETS)
    p02,p03=result['items']
    assert p02['target_key']=='P02' and 'face_height_fraction' not in p02
    assert p03['target_key']=='P03' and p03['face_height_fraction']=='0.5'
    assert p03['measurements']==[]


def test_unnamed_product_with_multiple_candidates_remains_unresolved_instead_of_resizing_all():
    value=extract_revision_constraints('产品整体高度为脸部高度的一半。',targets=TARGETS)
    assert len(value['items'])==1
    assert value['items'][0]['target_key']=='__unresolved_product__'
    assert 'target_entity_id' not in value['items'][0]
    with pytest.raises(ValueError,match='explicit Subject binding'):
        enforce_revision_constraints(PROMPT,value,{})


def test_current_same_product_cm_cup_and_face_requirements_all_reach_the_model_body():
    text='P02整体尺寸高10cm、宽8cm，只有普通水杯大小。P02整体高度约为脸部高度的一半。'
    value=extract_revision_constraints(text,targets=TARGETS)
    item=value['items'][0]
    assert item['face_height_fraction']=='0.5' and item['relative_scale']=='drinking_cup'
    assert item['measurements']==[{'axis':'height','value':'10','unit':'cm'},
                                  {'axis':'width','value':'8','unit':'cm'}]
    updated,receipt=enforce_revision_constraints(PROMPT,value,{'P02':'<Subject 1>'})
    detail=updated.split('detailed_description:\n')[1].split('overall_soundscape:')[0]
    assert 'physical height of <Subject 1> is 10 cm' in detail
    assert 'physical width of <Subject 1> is 8 cm' in detail
    assert '<Subject 1> is the size of a drinking cup' in detail
    assert "total height of <Subject 1> is half the height of the person's face" in detail
    assert receipt['clauses'][0]['text'] in detail
    assert enforce_revision_constraints(updated,value,{'P02':'<Subject 1>'})[0]==updated


def test_comparison_only_overrides_old_dimensions_on_generation_copies_and_preserves_source_audit():
    before=deepcopy(TARGETS)
    action='P02高12.5cm，宽15cm，腹部凹陷3cm后松开；P03高30cm，宽20cm。'
    value=extract_revision_constraints('P02整体高度为脸部高度的一半。',targets=TARGETS)
    changed,rows,audit=apply_revision_overrides(action,TARGETS,value)
    assert TARGETS==before and rows[1]==before[1]
    green=rows[0]['spec']
    assert '12.5' not in green['dimensions'] and '15' not in green['dimensions']
    assert 'height_cm' not in green and 'width_cm' not in green
    assert 'half the height' in green['physical_scale_override']
    assert green['color']=='green'
    assert 'P03高30cm，宽20cm' in changed and '12.5' not in changed and '15cm' not in changed
    assert audit['action_before']==action and audit['action_changed']
    assert any(row['before']=='高12.5cm，宽15cm' for row in audit['entity_fields'])


def test_new_comparison_supersedes_an_older_prompt_scale_but_not_other_targets():
    value=extract_revision_constraints('P02整体高度为脸部高度的一半。',
        prompt_override='P02高12.5cm，宽15cm，两个手掌大小；P03高30cm。',targets=TARGETS)
    p02,p03=value['items']
    assert p02['measurements']==[] and p02['face_height_fraction']=='0.5'
    assert 'relative_scale' not in p02
    assert p03['measurements']==[{'axis':'height','value':'30','unit':'cm'}]


def test_comparison_only_stays_in_scene_contract_and_actual_overlay_is_idempotent():
    instruction='P02整体高度为脸部高度的一半。第一帧P02由左手掌心托住，手与脸在同一画面；随后把P02抬起贴近脸颊。'
    value=extract_revision_constraints(instruction,targets=TARGETS)
    contract={'version':1,'revision_constraints':value,'constraint_target_groups':{'P02':'product-1'},
        'groups':[{'category':'product','group':'product-1','appearance_native':['<Picture 1>']}],
        'verified_target_subjects':{'P02':['<Subject 1>']},
        'scene_requirements':extract_scene_requirements(instruction,value)}
    assert len(contract['scene_requirements'])==3
    before=deepcopy((value,contract))
    updated,_=enforce_revision_constraints(PROMPT,value,{'P02':'<Subject 1>'})
    assert revision_semantic_findings(updated,contract)==[]
    assert (value,contract)==before
    assert enforce_revision_constraints(updated,value,{'P02':'<Subject 1>'})[0]==updated


def test_public_node_and_backend_scale_rules_have_identical_bytes():
    root=Path(__file__).resolve().parents[2]
    bundle=root/'scripts/local_inference/bundle/comfy_nodes/VideoFactoryLocalVLMGuard'
    if not bundle.is_dir():bundle=root/'scripts/comfy_nodes/VideoFactoryLocalVLMGuard'
    assert (root/'backend/app/services/revision_constraints.py').read_bytes()==(bundle/'revision_constraints.py').read_bytes()
    assert (root/'backend/app/services/revision_semantics.py').read_bytes()==(bundle/'revision_semantics.py').read_bytes()
