import json

import pytest

from app.core.errors import AppError
from app.services import h3_workflow_router
from app.services.h3_context_provider import context_compiler_intent, reference_layout
from app.services.h3_workflow_router import FAST, QUALITY, build_graph, freeze_workflow


PICTURE = {'kind': 'picture', 'label': 'actor', 'role': 'subject', 'file': 'jobs/test/face.png [input]'}
VIDEO = {'kind': 'video', 'label': 'move', 'role': 'structure', 'file': 'jobs/test/move.mp4 [input]', 'soundtrack': 'off'}


def build(snapshot, tray, **overrides):
    inputs = dict(tray=tray, intent='One short daylight shot.', duration_seconds=5.0, aspect_ratio='16:9',
                  seed=20261104, width=672, height=384, reference_groups='', required_references='',
                  motion_targets='', output_prefix='projects/project-1/shot-2/job-3')
    return build_graph(snapshot, **{**inputs, **overrides})


@pytest.mark.parametrize(('mode', 'workflow_id', 'steps'), [('high', QUALITY, 30), ('standard', QUALITY, 20), ('fast', FAST, 4)])
@pytest.mark.parametrize('tray', [[PICTURE, VIDEO], [PICTURE], [VIDEO], []], ids=['mixed', 'images', 'videos', 'text'])
def test_every_mode_keeps_one_guarded_compiler_and_the_actual_sampler(mode, workflow_id, steps, tray):
    snapshot = freeze_workflow(mode)
    assert snapshot['workflow_id'] == workflow_id
    assert snapshot['workflow_filename'] == f'{workflow_id}.api.json'
    assert snapshot['actual_steps'] == steps and snapshot['context_ir_enabled'] is True
    graph = build(snapshot, tray)
    assert json.loads(graph['2']['inputs']['tray']) == tray
    assert graph['4']['class_type'] == 'VideoFactoryLocalH3IRCompile'
    assert graph['4']['inputs']['setup'] == ['16', 0] and graph['4']['inputs']['media'] == ['2', 0]
    assert '17' not in graph and graph['16']['class_type'] == 'VideoFactoryLocalVLMGuard'
    assert graph['1']['inputs']['reference_model'] == snapshot['model_name']
    assert graph['4']['inputs']['seed'] == graph['5']['inputs']['noise_seed'] == 20261104
    assert graph['8']['inputs']['conditioning'] == ['4', 1] and graph['9']['inputs']['latent_image'] == ['4', 2]
    assert graph['13']['inputs']['filename_prefix'] == 'projects/project-1/shot-2/job-3'
    if mode == 'fast':
        assert graph['6']['inputs']['sampler_name'] == 'euler'
        assert graph['7']['class_type'] == 'ManualSigmas'
        assert [float(s) for s in graph['7']['inputs']['sigmas'].split(',')] == [1, 0.9677419355, 0.9, 0.6666666667, 0]
        assert graph['18']['inputs']['lora_name'] == 'SMD-MiniMax-H3-Ref2VA-4step.safetensors'
        assert graph['18']['inputs']['strength_model'] == 1
        assert graph['19']['inputs']['model'] == ['18', 0]
        assert graph['20']['inputs'] == {'model': ['19', 0], 'shift_video': 12, 'shift_audio': 3}
        assert graph['8']['inputs']['model'] == ['20', 0]
    else:
        assert graph['6']['inputs']['sampler_name'] == 'res_multistep'
        assert graph['7']['class_type'] == 'BasicScheduler' and graph['7']['inputs']['steps'] == steps
        assert graph['8']['inputs']['model'] == ['4', 0]
        assert all(node['class_type'] != 'LoraLoaderModelOnly' for node in graph.values())


def test_legacy_preview_is_normalized_only_at_new_selection_and_snapshot_cannot_change():
    assert freeze_workflow('preview') == freeze_workflow('fast')
    snapshot = freeze_workflow('standard')
    with pytest.raises(AppError) as exc:
        build({**snapshot, 'actual_steps': 30}, [])
    assert exc.value.code == 'h3_workflow_snapshot_changed'
    assert build(snapshot, [])['7']['inputs']['steps'] == 20


@pytest.mark.parametrize(('width', 'height', 'aspect'), [(1024, 576, '16:9'), (576, 1024, '9:16'), (768, 768, '1:1')])
def test_project_canvas_presets_map_to_native_dimensions(width, height, aspect):
    graph = build(freeze_workflow('high'), [], width=width, height=height, aspect_ratio=aspect)
    assert graph['4']['inputs']['megapixels'] == round(width * height / 1_000_000, 2)
    assert graph['4']['inputs']['aspect'] == aspect


def test_rejects_mismatched_geometry_and_unstaged_media():
    snapshot = freeze_workflow('standard')
    with pytest.raises(AppError) as exc:
        build(snapshot, [], width=640, height=384)
    assert exc.value.code == 'h3_context_canvas' and exc.value.details['actual_canvas'] == [672, 384]
    with pytest.raises(AppError) as exc:
        build(snapshot, [{**PICTURE, 'file': '../private/face.png'}])
    assert exc.value.code == 'h3_context_media_file'
    with pytest.raises(AppError) as exc:
        build(snapshot, [{**PICTURE, 'label': f'face-{number}'} for number in range(10)])
    assert exc.value.code == 'h3_context_media_limit'


def test_missing_or_changed_template_never_falls_back_to_a_different_graph(tmp_path, monkeypatch):
    monkeypatch.setattr(h3_workflow_router, 'PROJECT_ROOT', tmp_path)
    with pytest.raises(AppError) as exc:
        freeze_workflow('fast')
    assert exc.value.code == 'h3_workflow_missing'
    workflows = tmp_path / 'workflows'
    workflows.mkdir()
    (workflows / f'{FAST}.api.json').write_text('{"prompt": {}}', encoding='utf-8')
    with pytest.raises(AppError) as exc:
        freeze_workflow('fast')
    assert exc.value.code == 'h3_workflow_changed'


def test_footage_uses_camera_structure_by_default_and_requires_explicit_identity_target():
    image = {'role': 'identity', 'character_id': 'one'}
    tray, declarations, directions = reference_layout([image], [{'reference_role': 'structure'}])
    assert tray[1]['role'] == 'structure' and tray[1]['soundtrack'] == 'off'
    assert declarations['required_references'] == 'picture1, video1' and declarations['motion_targets'] == ''
    assert any('standalone' in line and 'weak_reference' in line for line in directions)
    tray, declarations, _ = reference_layout([image], [{'reference_role': 'action', 'target_character_id': 'one'}])
    assert tray[1]['role'] == 'subject' and declarations['motion_targets'] == 'character-1: video1'
    with pytest.raises(AppError, match='本镜已有身份图片'):
        reference_layout([image], [{'reference_role': 'action', 'target_character_id': 'other'}])


def test_context_keeps_explicit_camera_terms_in_the_shot_without_overriding_new_direction():
    camera = '拿起水壶\n机位与运镜：手持手机微晃跟随，轻微向前推进'
    _, _, directions = reference_layout([{'role': 'product', 'product_id': 'bottle'}], [])
    intent = context_compiler_intent(camera, '', directions)
    assert 'this exact camera sentence: The camera shakes slightly and pushes in.' in intent
    assert 'exact subject tag from that definition in the visible action or placement' in intent
    assert 'use the literal <Subject 1> at the first mention of the pictured product' in intent
    assert 'Do not add plain-key entries like location, lighting, lens' in intent
    _, _, multi_product = reference_layout([{'role': 'product', 'product_id': name} for name in ('one', 'two')], [])
    assert all('Keep <Subject 1>' not in direction for direction in multi_product)
    assert 'this exact camera sentence' not in context_compiler_intent(
        '固定机位\n首段是本次明确的镜头制作要求。\n' + camera, '', directions)
    assert 'this exact camera sentence' not in context_compiler_intent(
        '机位与运镜：不要微晃，避免向前推进', '', directions)


def test_handheld_approach_uses_the_native_camera_verbs_as_a_complete_sentence():
    intent = context_compiler_intent('大口直饮补水\n机位与运镜：手持手机微晃，仰角缓慢靠近', '', [])
    assert 'this exact camera sentence: The camera shakes slightly and pushes in at slow speed.' in intent
def test_named_product_compiler_layout_includes_stationary_reference_at_declared_position():
    from app.services.h3_context_provider import _declared_prop_states, _frozen_product_staging
    prompt = ('道具镜头状态：[{"道具":"粉色杯","holder":"右手"},'
              '{"道具":"绿杯","holder":"左手"},{"道具":"花纹杯","holder":"展台","state":"弹盖关闭，静置于展台"}]')
    pictures = [
        {'role': 'product', 'source': 'screenplay_prop_reference',
         'product_id': f'product-{i}', 'product_name': name}
        for i, name in enumerate(('粉色杯', '绿杯', '花纹杯'), 1)
    ]
    props = _declared_prop_states(prompt)
    tray, groups, directions = reference_layout(pictures, [], prop_states=props)
    assert len(tray) == 3
    assert groups['required_references'] == 'picture1, picture2, picture3'
    assert any('Define <Subject 3> from <Picture 3> only' in d and
               '展台' in d and 'stationary product on the display' in d for d in directions)
    assert any('every exact tag: <Subject 1>, <Subject 2>, <Subject 3>' in d for d in directions)
    staging = _frozen_product_staging(pictures, [], props)
    assert '<Subject 3> (from <Picture 3>) stays on the display stand' in staging
    assert "<Subject 1> (from <Picture 1>) is the product handled by the protagonist's right hand" in staging
    assert "<Subject 2> (from <Picture 2>) is the product handled by the protagonist's left hand" in staging
    assert context_compiler_intent(prompt, '', directions, staging=staging).startswith(staging + '\n' + prompt)
