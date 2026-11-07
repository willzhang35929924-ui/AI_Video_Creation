"""Protect real sampling recipes from generic UI quality-step overrides."""
import copy
import json
from dataclasses import replace

import pytest

from app.core.errors import AppError
from app.services.providers import ComfyUIH3Provider, apply_sampling_steps, validate_sampling_contract


def sampling_graph():
    graph = {
        'model': {'class_type': 'UNETLoader', 'inputs': {'unet_name': 'existing.safetensors', 'weight_dtype': 'default'}},
        'lora': {'class_type': 'LoraLoaderModelOnly', 'inputs': {'model': ['model', 0], 'lora_name': 'matched-recipe.safetensors', 'strength_model': 1.0}},
        'shift': {'class_type': 'MiniMaxH3SigmaShift', 'inputs': {'model': ['lora', 0], 'shift_video': 12.0, 'shift_audio': 3.0}},
        'conditioning': {'class_type': 'MiniMaxH3ReferenceToVideo', 'inputs': {'prompt': 'dynamic scene prompt'}},
        'guider': {'class_type': 'BasicGuider', 'inputs': {'model': ['shift', 0], 'conditioning': ['conditioning', 0]}},
        'select': {'class_type': 'KSamplerSelect', 'inputs': {'sampler_name': 'euler'}},
        'sigmas': {'class_type': 'ManualSigmas', 'inputs': {'sigmas': '1, 0.8, 0.5, 0.2, 0'}},
        'noise': {'class_type': 'RandomNoise', 'inputs': {'noise_seed': 1247}},
        'sample': {'class_type': 'SamplerCustomAdvanced', 'inputs': {
            'noise': ['noise', 0], 'guider': ['guider', 0], 'sampler': ['select', 0],
            'sigmas': ['sigmas', 0], 'latent_image': ['conditioning', 1]}},
        'decode': {'class_type': 'VAEDecode', 'inputs': {'samples': ['sample', 0]}},
        'video': {'class_type': 'CreateVideo', 'inputs': {'images': ['decode', 0], 'fps': 24}},
        'save': {'class_type': 'SaveVideo', 'inputs': {'video': ['video', 0], 'filename_prefix': 'test'}},
    }
    metadata = {'prompt_path': ['conditioning', 'inputs', 'prompt'], 'output_prefix_path': ['save', 'inputs', 'filename_prefix']}
    return graph, metadata


def fixed_recipe():
    graph, metadata = sampling_graph()
    locked = []
    for identifier in ('model', 'lora', 'shift', 'guider', 'select', 'sigmas', 'sample'):
        for field, value in graph[identifier]['inputs'].items():
            if identifier == 'guider' and field == 'conditioning':
                continue
            if identifier == 'sample' and field not in ('sigmas', 'sampler', 'guider'):
                continue
            locked.append({'path': [identifier, 'inputs', field], 'value': copy.deepcopy(value)})
    metadata['sampling_contract'] = {'version': 1, 'steps': 4, 'sampler_node': 'sample', 'locked_inputs': locked}
    return graph, metadata


def test_json_four_steps_is_preserved_when_request_has_no_steps():
    graph = {'schedule': {'class_type': 'BasicScheduler', 'inputs': {'steps': 4}}}
    metadata = {'steps_path': ['schedule', 'inputs', 'steps']}
    apply_sampling_steps(graph, metadata, {})
    assert graph['schedule']['inputs']['steps'] == 4
    assert metadata['sampling_receipt'] == {'policy': 'configurable', 'steps': 4, 'source': 'workflow'}


def test_existing_twenty_step_workflow_stays_configurable():
    graph = {'schedule': {'class_type': 'BasicScheduler', 'inputs': {'steps': 20}}}
    metadata = {'steps_path': ['schedule', 'inputs', 'steps']}
    apply_sampling_steps(graph, metadata, {})
    assert graph['schedule']['inputs']['steps'] == 20
    apply_sampling_steps(graph, metadata, {'steps': 12})
    assert graph['schedule']['inputs']['steps'] == 12
    assert metadata['sampling_receipt']['source'] == 'request'
    assert 'sampling_contract' not in metadata  # Do not silently lock an old Turbo graph.


def test_locked_manual_sigmas_use_four_steps_without_a_fake_steps_mapping():
    graph, metadata = fixed_recipe()
    graph['conditioning']['inputs']['prompt'] = 'different scene, same fixed sampling recipe'
    graph['noise']['inputs']['noise_seed'] = 99
    apply_sampling_steps(graph, metadata, {})
    assert metadata['sampling_receipt'] == {'policy': 'workflow_locked', 'steps': 4, 'contract_version': 1}
    assert graph['sigmas']['inputs']['sigmas'] == '1, 0.8, 0.5, 0.2, 0'


def test_quality_preset_cannot_overwrite_a_fixed_sigma_recipe():
    graph, metadata = fixed_recipe()
    before = copy.deepcopy(graph)
    with pytest.raises(AppError) as error:
        apply_sampling_steps(graph, metadata, {'steps': 20})
    assert error.value.code == 'h3_sampling_steps_locked'
    assert graph == before
    assert 'sampling_receipt' not in metadata


@pytest.mark.parametrize(('node', 'field', 'new_value'), [
    ('lora', 'strength_model', 0.0), ('lora', 'lora_name', 'unrelated-fl2va.safetensors'),
    ('shift', 'shift_audio', 12.0), ('select', 'sampler_name', 'res_multistep'),
    ('guider', 'model', ['model', 0]), ('sigmas', 'sigmas', '1,0.7,0.3,0'),
])
def test_a_changed_lora_shift_sampler_or_sigma_is_rejected(node, field, new_value):
    graph, metadata = fixed_recipe()
    graph[node]['inputs'][field] = new_value
    with pytest.raises(AppError) as error:
        validate_sampling_contract(graph, metadata)
    assert error.value.code in {'h3_sampling_contract_changed', 'h3_sampling_contract_invalid'}


def test_unused_lora_cannot_satisfy_the_executed_sampling_recipe():
    graph, metadata = fixed_recipe()
    graph['unused_lora'] = copy.deepcopy(graph['lora'])
    metadata['sampling_contract']['locked_inputs'].append({
        'path': ['unused_lora', 'inputs', 'lora_name'], 'value': 'matched-recipe.safetensors'})
    with pytest.raises(AppError) as error:
        validate_sampling_contract(graph, metadata)
    assert error.value.code == 'h3_sampling_contract_invalid'


@pytest.mark.parametrize('broken', ['missing_link', 'missing_node', 'not_saved'])
def test_disconnected_or_missing_sigma_and_sampler_output_are_rejected(broken):
    graph, metadata = fixed_recipe()
    if broken == 'missing_link':
        del graph['sample']['inputs']['sigmas']
    elif broken == 'missing_node':
        del graph['sigmas']
    else:
        graph['decode']['inputs']['samples'] = ['conditioning', 1]
    with pytest.raises(AppError) as error:
        validate_sampling_contract(graph, metadata)
    assert error.value.code == 'h3_sampling_contract_invalid'


@pytest.mark.parametrize('values', ['1,0.8,0.2,0', '1,0.8,0.5,0.2,0.1', '1,0.5,0.7,0.2,0', '1,nan,0.5,0.2,0'])
def test_even_a_pinned_but_incomplete_sigma_schedule_is_not_four_step(values):
    graph, metadata = fixed_recipe()
    graph['sigmas']['inputs']['sigmas'] = values
    for entry in metadata['sampling_contract']['locked_inputs']:
        if entry['path'] == ['sigmas', 'inputs', 'sigmas']:
            entry['value'] = values
    with pytest.raises(AppError) as error:
        validate_sampling_contract(graph, metadata)
    assert error.value.code == 'h3_sampling_contract_invalid'


@pytest.mark.parametrize('contract', [None, {}, {'version': 2, 'steps': 4, 'sampler_node': 'sample', 'locked_inputs': []}])
def test_unknown_or_incomplete_contract_is_not_treated_as_unlocked(contract):
    graph, metadata = sampling_graph()
    metadata['sampling_contract'] = contract
    with pytest.raises(AppError) as error:
        validate_sampling_contract(graph, metadata)
    assert error.value.code == 'h3_sampling_contract_invalid'


def test_contract_cannot_leave_an_unpinned_model_or_sigma_input():
    graph, metadata = fixed_recipe()
    metadata['sampling_contract']['locked_inputs'] = [entry for entry in metadata['sampling_contract']['locked_inputs']
                                                    if entry['path'] != ['shift', 'inputs', 'shift_video']]
    with pytest.raises(AppError) as error:
        validate_sampling_contract(graph, metadata)
    assert error.value.code == 'h3_sampling_contract_invalid'


def test_health_loader_rejects_invalid_recipe_before_any_provider_request(settings, tmp_path):
    graph, metadata = fixed_recipe()
    metadata['sampling_contract']['locked_inputs'][0]['path'] = ['missing', 'inputs', 'unet_name']
    path = tmp_path / 'fixed-workflow.json'
    path.write_text(json.dumps({'prompt': graph, '_video_factory': metadata}), encoding='utf-8')
    provider = ComfyUIH3Provider(replace(settings, h3_workflow_api_json=path))
    with pytest.raises(AppError) as error:
        provider._load_workflow()
    assert error.value.code == 'h3_sampling_contract_invalid'
