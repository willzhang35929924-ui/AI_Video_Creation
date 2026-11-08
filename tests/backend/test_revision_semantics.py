"""Conservative written-prompt checks; no model, frame, or private script data."""
from copy import deepcopy

import pytest

from app.services.revision_semantics import extract_scene_requirements, revision_semantic_findings


INSTRUCTION = 'P02只有水杯大小。第一帧P02由左手掌心托住，手与脸在同一画面；随后把P02抬起贴近脸颊。'


def scale_constraints(*, absolute=True, relative=True):
    item = {'target_key': 'P02', 'target_entity_id': 'green-prop', 'kind': 'physical_scale',
            'source': 'instruction', 'source_text': 'P02' + ('高10cm，宽8cm，' if absolute else '') +
                ('只有水杯大小。' if relative else '尺寸不变。'),
            'measurements': []}
    if absolute:
        item['measurements'] = [
            {'axis': 'height', 'value': '10', 'unit': 'cm'},
            {'axis': 'width', 'value': '8', 'unit': 'cm'},
        ]
    if relative:
        item['relative_scale'] = 'drinking_cup'
    return {'version': 1, 'items': [item]}


def contract(*, instruction=None, absolute=True, relative=True):
    constraints = scale_constraints(absolute=absolute, relative=relative)
    return {
        'version': 1,
        'revision_constraints': constraints,
        'constraint_target_groups': {'P02': 'product-1'},
        'groups': [{'category': 'product', 'group': 'product-1',
                    'appearance_native': ['<Picture 1>', '<Picture 2>']}],
        'scene_requirements': extract_scene_requirements(instruction, constraints),
    }


def prompt(body, *, definitions=None, retention='<Subject 1> fully_preserved: green plush and yellow star.'):
    definitions = definitions or (
        '<Subject 1> is the same green plush tree from <Picture 1> and <Picture 2>.')
    return (f'subject_definitions:\n{definitions}\n\nsummary:\nA small green plush is handled.\n\n'
            f'retention_analysis:\n{retention}\n\ndetailed_description:\n'
            f'The target video uses a photographic live-action style.\n[Shot 1] {body}\n\n'
            'overall_soundscape:\nQuiet room tone.\n\nnon_diegetic_music:\nN/A\n')


def rules(findings):
    assert all(row['severity'] == 'ERROR' for row in findings)
    return {row['rule'] for row in findings}


def test_extracts_only_explicit_single_target_requirements_and_preserves_caller_words():
    constraints = scale_constraints()
    before = deepcopy(constraints)
    extracted = extract_scene_requirements(INSTRUCTION, constraints)
    assert {row['rule'] for row in extracted} == {
        'target_first_frame_palm_support', 'target_hand_face_same_frame', 'target_raise_to_cheek'}
    assert all(row['target_key'] == 'P02' for row in extracted)
    assert all(row['source_text'] and row['source_text'] in INSTRUCTION for row in extracted)
    assert all(row['evidence_text'] and row['evidence_text'] in INSTRUCTION for row in extracted)
    assert constraints == before


def test_actual_failure_shape_rejects_linked_picture_dimensions_scale_retention_and_lost_actions():
    text = prompt(
        'The camera holds static. <Subject 1> sits on the carpet beside the child.',
        definitions=(
            '<Picture 1> shows a green plush with a yellow star.\n'
            '<Picture 2> shows the same plush, 15cm wide and 12.5cm tall.\n'
            '<Subject 1> is the green plush from <Picture 1> and <Picture 2>.'),
        retention=('<Subject 1> fully_preserved. Printed dimensions from <Picture 2> '
                   'are used to establish its physical scale.'))
    value = contract(instruction=INSTRUCTION)
    before = deepcopy(value)
    findings = revision_semantic_findings(text, value)
    assert value == before
    found = rules(findings)
    assert {'revision_target_scale_conflict', 'revision_reference_scale_used',
            'target_first_frame_palm_support', 'target_hand_face_same_frame',
            'target_raise_to_cheek'} <= found
    messages = '\n'.join(row['message'] for row in findings)
    assert '15cm' in messages and '12.5cm' in messages
    assert '<Subject 1>' in messages and '<Picture 2>' in messages
    assert 'carpet' in messages
    assert any(row['source_text'] in messages for row in contract(instruction=INSTRUCTION)['scene_requirements'])


def test_valid_medium_close_palm_face_cheek_and_current_dimensions_are_accepted():
    text = prompt(
        'In a medium close-up, at the first frame <Subject 1> is supported by the open left palm. '
        'The left hand and the child\'s face remain visible in the same frame with <Subject 1>. '
        'The height of <Subject 1> is 10cm and its width is 8cm. '
        'The child then raises <Subject 1> to her cheek. A carpet stays in the background. '
        'The camera holds static with a 35mm lens and 2800K light for 4 seconds.')
    assert revision_semantic_findings(text, contract(instruction=INSTRUCTION)) == []


@pytest.mark.parametrize('opening,framing,lift', [
    ('At the first frame, the target <Subject 1> is supported by the open left palm.',
     'Keep the supporting hand and the face visible in the same frame with the target <Subject 1>.',
     'Then raise the target <Subject 1> to the cheek.'),
    ('At the first frame, <Subject 1> is supported by the open left palm.',
     'The supporting hand, the entire <Subject 1>, and the face are visible in the same frame.',
     'Then, the child raises <Subject 1> to her cheek.'),
    ('第一帧，<Subject 1>由左手掌心托住。',
     '手掌、完整<Subject 1>、人物脸部同时入镜。',
     '随后将<Subject 1>靠近脸颊。'),
])
def test_single_target_introductory_commas_and_framing_lists_preserve_requested_actions(opening, framing, lift):
    text = prompt(' '.join((opening, framing, lift)))
    assert revision_semantic_findings(text, contract(instruction=INSTRUCTION)) == []


def test_introductory_first_frame_comma_does_not_lend_another_subjects_palm_action():
    text = prompt('At the first frame, <Subject 1> remains on the display stand while '
                  '<Subject 2> is supported by the open left palm. '
                  'The supporting hand, <Subject 2>, and the face remain visible in the same frame. '
                  'Then, the child raises <Subject 2> to her cheek.', definitions=(
                      '<Subject 1> is the green plush from <Picture 1> and <Picture 2>.\n'
                      '<Subject 2> is a different product from <Picture 3>.'))
    assert {'target_first_frame_palm_support', 'target_hand_face_same_frame',
            'target_raise_to_cheek'} <= rules(revision_semantic_findings(text, contract(instruction=INSTRUCTION)))


def test_verified_target_subject_excludes_other_object_in_the_same_picture():
    value = contract(instruction=INSTRUCTION)
    value['verified_target_subjects'] = {'P02': ['<Subject1>']}
    text = prompt('At the first frame, <Subject 1> is supported by the open left palm. '
                  'The supporting hand, <Subject 1>, and the face remain visible in the same frame. '
                  'The child raises <Subject 1> to her cheek. '
                  '<Subject 2> rests on the carpet and is 50cm tall and 35cm wide.', definitions=(
                      '<Subject 1> is the green plush from <Picture 1> and <Picture 2>.\n'
                      '<Subject 2> is a cylindrical gift box also visible in <Picture 2>.'))
    assert revision_semantic_findings(text, value) == []


def test_verified_target_subject_does_not_borrow_other_object_actions_from_same_picture():
    value = contract(instruction=INSTRUCTION)
    value['verified_target_subjects'] = {'P02': ['<Subject 1>']}
    text = prompt('<Subject 1> remains beside the child. '
                  'At the first frame, <Subject 2> is supported by the open left palm. '
                  'The supporting hand, <Subject 2>, and the face remain visible in the same frame. '
                  'The child raises <Subject 2> to her cheek.', definitions=(
                      '<Subject 1> is the green plush from <Picture 1> and <Picture 2>.\n'
                      '<Subject 2> is a cylindrical gift box also visible in <Picture 2>.'))
    assert {'target_first_frame_palm_support', 'target_hand_face_same_frame',
            'target_raise_to_cheek'} <= rules(revision_semantic_findings(text, value))


def test_actual_writer_visible_together_in_frame_and_their_cheek_phrasing_is_accepted():
    text = prompt("At the first frame, <Subject 1> is supported by the open left palm. "
                  "A fixed close-up shot captures <Subject 1> being held in the open left palm of 人物甲, "
                  "with the person's face and hand visible together in the frame. "
                  "The person then lifts <Subject 1> to their cheek.")
    assert revision_semantic_findings(text, contract(instruction=INSTRUCTION)) == []


def test_visible_together_in_frame_phrase_does_not_borrow_another_product_action():
    text = prompt('<Subject 1> remains on the display stand while '
                  '<Subject 2> is held in the open left palm, with the person\'s face '
                  'and hand visible together in the frame. '
                  'The person then lifts <Subject 2> to their cheek.', definitions=(
                      '<Subject 1> is the green plush from <Picture 1> and <Picture 2>.\n'
                      '<Subject 2> is a cylindrical gift box also visible in <Picture 2>.'))
    value = contract(instruction=INSTRUCTION)
    value['verified_target_subjects'] = {'P02': ['<Subject 1>']}
    assert {'target_first_frame_palm_support', 'target_hand_face_same_frame',
            'target_raise_to_cheek'} <= rules(revision_semantic_findings(text, value))


def test_actual_writer_opening_present_palm_state_is_first_frame_without_magic_words():
    text = prompt("A fixed close-up shot captures <Subject 1> being held in the open left palm of 人物甲, "
                  "with the person's face and hand visible together in the frame. "
                  "The person then lifts <Subject 1> to their cheek.")
    assert revision_semantic_findings(text, contract(instruction=INSTRUCTION)) == []


@pytest.mark.parametrize('opening', [
    '<Subject 1> will later be held in the open left palm.',
    'The child picks up <Subject 1> from the carpet and holds it in the open left palm.',
    '<Subject 1> rests on the carpet. Later <Subject 1> is held in the open left palm.',
    'The camera holds static. <Subject 1> is held in the open left palm.',
])
def test_future_or_later_palm_support_is_not_an_initial_present_state(opening):
    text = prompt(opening + ' The hand and face are in the same frame with <Subject 1>. '
                  'The person raises <Subject 1> to their cheek.')
    assert 'target_first_frame_palm_support' in rules(revision_semantic_findings(text, contract(instruction=INSTRUCTION)))


def test_actual_writer_initial_palm_holding_and_camera_composition_list_are_accepted():
    text = prompt("The camera holds static, framing the person's face, their left palm holding "
                  "<Subject 1>, and the plush tree's full body. "
                  "The person then lifts <Subject 1> to their cheek.")
    assert revision_semantic_findings(text, contract(instruction=INSTRUCTION)) == []


@pytest.mark.parametrize('composition', [
    "The camera holds static, framing the person's face and a hand. <Subject 1> is beside the chair.",
    "The camera holds static, framing the person's face, their left palm holding <Subject 1>, "
        "but the product's full body is outside the frame.",
    "The camera holds static, framing the person's face, their left palm holding <Subject 1>, "
        "with the full body hidden.",
])
def test_camera_framing_without_all_visible_requested_items_is_not_same_frame(composition):
    assert 'target_hand_face_same_frame' in rules(revision_semantic_findings(prompt(composition), contract(instruction=INSTRUCTION)))


def test_palm_holding_camera_composition_does_not_borrow_other_product_or_future_state():
    text = prompt("The camera holds static, framing the person's face, their left palm holding "
                  "<Subject 2>, and the plush tree's full body. "
                  "<Subject 1> will later be held in the left palm. "
                  "The person then lifts <Subject 2> to their cheek.", definitions=(
                      '<Subject 1> is the green plush from <Picture 1> and <Picture 2>.\n'
                      '<Subject 2> is a cylindrical gift box also visible in <Picture 2>.'))
    value = contract(instruction=INSTRUCTION)
    value['verified_target_subjects'] = {'P02': ['<Subject 1>']}
    assert {'target_first_frame_palm_support', 'target_hand_face_same_frame',
            'target_raise_to_cheek'} <= rules(revision_semantic_findings(text, value))


@pytest.mark.parametrize('owner', ["the product's", 'the product’s', 'the target product’s', '<Subject 1>’s'])
def test_referenced_for_product_physical_dimensions_is_a_scale_conflict(owner):
    text = prompt('<Subject 1> is 10cm tall and 8cm wide.',
        retention=f'<Picture 2> weak_reference - product packaging and green plush tree are referenced for {owner} physical dimensions and appearance.')
    assert 'revision_reference_scale_used' in rules(revision_semantic_findings(text, contract()))


def test_referenced_for_appearance_but_not_physical_dimensions_is_allowed():
    text = prompt('<Subject 1> is 10cm tall and 8cm wide.',
        retention='<Picture 2> weak_reference - the green plush tree is referenced for appearance but not for the product’s physical dimensions.')
    assert revision_semantic_findings(text, contract()) == []


@pytest.mark.parametrize('measurement', [
    '<Subject 1> is 100mm tall and 80mm wide.',
    '<Subject 1> is 0.10m high and 0.08m wide.',
    'The height of <Subject 1> is 10cm and the width of <Subject 1> is 8cm.',
])
def test_equivalent_axis_units_do_not_conflict(measurement):
    assert revision_semantic_findings(prompt(measurement), contract(relative=False)) == []


@pytest.mark.parametrize('measurement, evidence', [
    ('<Subject 1> is 15cm wide and 12.5cm tall.', '15cm'),
    ('The height of <Subject 1> is 125mm.', '125mm'),
    ('The width of <Subject 1> is 0.15m.', '0.15m'),
])
def test_recognized_axis_conflicts_report_the_target_and_exact_evidence(measurement, evidence):
    findings = revision_semantic_findings(prompt(measurement), contract(relative=False))
    assert 'revision_target_scale_conflict' in rules(findings)
    assert any(evidence in row['message'] and '<Subject 1>' in row['message'] for row in findings)


def test_relative_cup_without_absolute_numbers_does_not_reuse_reference_measurements_as_scale():
    text = prompt(
        '<Subject 1> remains the size of a drinking cup.',
        definitions=('<Picture 2> shows the product 15cm wide and 12.5cm tall.\n'
                     '<Subject 1> is the product from <Picture 1> and <Picture 2>.'),
        retention=('<Subject 1> fully_preserved. The printed measurements in <Picture 2> '
                   'are retained as the physical scale of <Subject 1>.'))
    findings = revision_semantic_findings(text, contract(absolute=False))
    assert 'revision_reference_scale_used' in rules(findings)
    assert any('<Picture 2>' in row['message'] for row in findings)


def test_printed_label_can_be_retained_without_using_it_as_physical_scale():
    text = prompt('<Subject 1> is 10cm tall and 8cm wide.',
        definitions=('<Picture 2> has printed text "15cm" on a label.\n'
                     '<Subject 1> is the green plush from <Picture 1> and <Picture 2>.'),
        retention=('<Subject 1> fully_preserved: green color and yellow star. '
                   'The printed dimensions on <Picture 2> are not used for physical scale.'))
    assert revision_semantic_findings(text, contract()) == []


def test_other_product_actions_in_the_same_sentence_do_not_satisfy_target_requirements():
    value = contract(instruction=INSTRUCTION)
    value['groups'].append({'category': 'product', 'group': 'product-2',
                            'appearance_native': ['<Picture 3>']})
    text = prompt(
        'At the first frame <Subject 1> remains on the display stand while '
        '<Subject 2> is supported by the open left palm. '
        'The child places <Subject 1> beside <Subject 2>, then raises <Subject 2> '
        'to her cheek, keeping the supporting hand and face visible in the same frame with <Subject 2>.',
        definitions=('<Subject 1> is the green plush from <Picture 1> and <Picture 2>.\n'
                     '<Subject 2> is a different product from <Picture 3>.'))
    findings = revision_semantic_findings(text, value)
    assert {'target_first_frame_palm_support', 'target_hand_face_same_frame',
            'target_raise_to_cheek'} <= rules(findings)
    assert all('<Subject 1>' in row['message'] for row in findings)


def test_negated_reference_scale_does_not_hide_an_explicit_target_dimension_conflict():
    text = prompt('<Subject 1> is 15cm wide and 12.5cm tall, while printed measurements '
                  'on <Picture 2> are not used for physical scale.')
    findings = revision_semantic_findings(text, contract(relative=False))
    assert 'revision_target_scale_conflict' in rules(findings)
    assert any('15cm' in row['message'] and '<Subject 1>' in row['message'] for row in findings)


def test_nearby_child_height_is_not_assigned_to_the_target_product():
    text = prompt('<Subject 1> is held beside a child who is 100cm tall. '
                  '<Subject 1> is 10cm tall and 8cm wide.')
    assert revision_semantic_findings(text, contract(relative=False)) == []


def test_other_products_floor_placement_and_photography_numbers_are_not_target_conflicts():
    value = contract(instruction=INSTRUCTION)
    value['groups'].append({'category': 'product', 'group': 'product-2',
                            'appearance_native': ['<Picture 3>']})
    text = prompt(
        'At the first frame <Subject 1> is supported by the open left palm. '
        'The left hand and face are in the same frame with <Subject 1>. '
        '<Subject 1> is 10cm tall and 8cm wide. The child raises <Subject 1> to her cheek. '
        '<Subject 2> rests on the rug and is 15cm wide and 12.5cm tall. '
        'The camera holds static with a 35mm lens, 2800K lighting and a 4 second shot.',
        definitions=('<Subject 1> is the green plush from <Picture 1> and <Picture 2>.\n'
                     '<Subject 2> is a different box from <Picture 3>.'))
    assert revision_semantic_findings(text, value) == []


def test_multi_target_constraints_are_checked_in_their_own_picture_groups():
    value = contract(relative=False)
    value['revision_constraints']['items'].append({'target_key': 'P01', 'kind': 'physical_scale', 'measurements': [
        {'axis': 'width', 'value': '50', 'unit': 'cm'}]})
    value['constraint_target_groups']['P01'] = 'product-2'
    value['groups'].append({'category': 'product', 'group': 'product-2',
                            'appearance_native': ['<Picture 3>']})
    text = prompt('<Subject 1> is 12.5cm tall. <Subject 2> is 50cm wide.', definitions=(
        '<Subject 1> is the green plush from <Picture 1> and <Picture 2>.\n'
        '<Subject 2> is the box from <Picture 3>.'))
    findings = revision_semantic_findings(text, value)
    assert 'revision_target_scale_conflict' in rules(findings)
    assert all('<Subject 2>' not in row['message'] for row in findings)
    assert any('<Subject 1>' in row['message'] for row in findings)


def test_split_subjects_from_the_same_group_cannot_hide_conflicting_picture_scale():
    text = prompt('<Subject 1> is 12.5cm tall. <Subject 2> is 15cm wide.', definitions=(
        '<Subject 1> is the green plush from <Picture 1>.\n'
        '<Subject 2> is another view of the same green plush from <Picture 2>.'))
    findings = revision_semantic_findings(text, contract(relative=False))
    assert 'revision_target_scale_conflict' in rules(findings)
    assert any('<Subject 1>' in row['message'] for row in findings)
    assert any('<Subject 2>' in row['message'] for row in findings)


def test_no_scene_requirement_is_invented_for_general_action_or_ambiguous_targets():
    assert extract_scene_requirements('P02只有水杯大小。人物开心地互动。', scale_constraints()) == []
    assert extract_scene_requirements(INSTRUCTION, {'version': 1, 'items': []}) == []
    ambiguous = scale_constraints()
    ambiguous['items'].append({'target_key': 'P01', 'kind': 'physical_scale', 'measurements': [
        {'axis': 'height', 'value': '20', 'unit': 'cm'}]})
    assert extract_scene_requirements('第一帧由左手掌心托住，手与脸在同一画面，随后抬起贴近脸颊。', ambiguous) == []
    assert revision_semantic_findings(prompt('<Subject 1> rests on the carpet.'), contract()) == []


def test_unbound_subject_does_not_borrow_a_target_group_and_checker_never_mutates_inputs():
    text = prompt('<Subject 7> is 15cm wide and 12.5cm tall.',
                  definitions='<Subject 7> is an unspecified object without a reference binding.')
    value = contract(instruction=INSTRUCTION)
    before = deepcopy(value)
    assert revision_semantic_findings(text, value) == []
    assert value == before
    assert text == prompt('<Subject 7> is 15cm wide and 12.5cm tall.',
                          definitions='<Subject 7> is an unspecified object without a reference binding.')


def test_no_revision_contract_leaves_text_untouched_and_unrestricted():
    text = prompt('<Subject 1> sits on the carpet and is 15cm wide and 12.5cm tall.')
    assert revision_semantic_findings(text, {}) == []
