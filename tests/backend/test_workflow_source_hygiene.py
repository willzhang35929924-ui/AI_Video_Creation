"""Public workflow source hygiene only; never loads models or generates media."""
import json
import re
from pathlib import Path

import pytest


WORKFLOWS = Path(__file__).resolve().parents[2] / 'workflows'
PRIVATE_PATH = re.compile(r'(?<![A-Za-z0-9])[A-Za-z]:[\\/]')
PRIVATE_ID = re.compile(r'\b(?:prj|job|shot|spjob|run|prod)_[0-9a-f]{16,}\b', re.IGNORECASE)
HISTORICAL_KEYS = {'export_acceptance', 'smd_comparison', 'preview_approval', 'user_reply',
                   'provider_job_id', 'prompt_id', 'single_prompt_id', 'paired_prompt_id'}


def walk(value, location='$'):
    if isinstance(value, dict):
        for key, child in value.items():
            yield location, key
            yield from walk(child, location + '/' + key)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk(child, location + '/' + str(index))
    elif isinstance(value, str):
        yield location, value


@pytest.mark.parametrize('path', sorted(WORKFLOWS.glob('*.json')), ids=lambda path: path.name)
def test_published_workflows_have_no_private_paths_task_records_or_user_approval(path):
    document = json.loads(path.read_text('utf-8-sig'))
    for location, value in walk(document):
        assert not PRIVATE_PATH.search(value), f'{path.name}: private drive path at {location}'
        assert not PRIVATE_ID.search(value), f'{path.name}: private project/task ID at {location}'
        assert value not in HISTORICAL_KEYS, f'{path.name}: historical acceptance at {location}'
        assert 'user-preview-approval' not in value, f'{path.name}: private approval receipt at {location}'


def test_fast_gui_is_an_empty_unvalidated_template_with_public_provenance():
    document = json.loads((WORKFLOWS / 'H3_R2V_OpenH3IR_SMD4_Fast.json').read_text('utf-8'))
    metadata = document['extra']['video_factory']
    assert metadata['generation_status'] == 'NOT_GENERATED'
    assert metadata['template_requires_user_media'] is True
    assert metadata['sampling']['runtime_status'] == 'NOT_VERIFIED'
    assert not {'assets', 'export_acceptance', 'smd_comparison', 'reference_interpretation'} & metadata.keys()
    assert metadata['upstream'].startswith('https://github.com/')
    assert metadata['pack_commit'] and metadata['license']
    lora = metadata['sampling']['lora_source']
    assert lora['repository'] and lora['revision'] and lora['license']
    assert re.fullmatch(r'[0-9a-f]{64}', lora['sha256'])
    for node in document['nodes']:
        values = node.get('widgets_values', [])
        if node['type'] == 'OpenH3IRMedia':
            assert json.loads(values[0]) == []
        elif node['type'] == 'OpenH3IRDirector':
            assert json.loads(values[0]) == {} and values[1] == ''
        elif node['type'] in {'VideoFactoryLocalH3IRCompile', 'OpenH3IRCompile'}:
            assert values[0] == ''
            assert all(value == '' for value in values[12:])


def test_gui_sanitization_preserves_the_production_four_step_recipe():
    gui = json.loads((WORKFLOWS / 'H3_R2V_OpenH3IR_SMD4_Fast.json').read_text('utf-8'))
    api = json.loads((WORKFLOWS / 'H3_R2V_OpenH3IR_SMD4_Fast.api.json').read_text('utf-8'))['prompt']
    nodes = {str(node['id']): node for node in gui['nodes']}
    assert gui['extra']['video_factory']['sampling']['steps'] == 4
    assert nodes['6']['widgets_values'][0] == api['6']['inputs']['sampler_name']
    assert nodes['7']['widgets_values'][0] == api['7']['inputs']['sigmas']
    assert nodes['18']['widgets_values'] == [api['18']['inputs']['lora_name'], api['18']['inputs']['strength_model']]
    assert nodes['20']['widgets_values'] == [api['20']['inputs']['shift_video'], api['20']['inputs']['shift_audio']]
