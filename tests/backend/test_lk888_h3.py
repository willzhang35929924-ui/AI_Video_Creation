"""Isolated lk888 H3 contract tests; no paid provider request is sent."""
import asyncio
import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from dataclasses import asdict

import httpx
import pytest
from PIL import Image

from app.core.errors import AppError
from app.services import lk888_h3
from app.services.providers import VideoGenerationRequest
from app.services.cinematic_rules import rules_bundle


@pytest.fixture
def specimen(settings, tmp_path):
    refs = []
    for index, extension in enumerate(('jpg', 'png')):
        path = tmp_path / ('ref' + str(index) + '.' + extension)
        Image.new('RGB', (360, 640), (10 + index, 20, 30)).save(path)
        refs.append({'asset_id': 'asset-' + str(index), 'absolute_path': str(path),
                     'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                     'character_id': 'character-' + str(index), 'character_version_id': 'v1', 'role': 'character'})
    request = VideoGenerationRequest(job_id='job-1', project_id='project-1', shot_id='shot-1',
        prompt='A person smiles.', negative_prompt='watermark', duration_seconds=5, aspect_ratio='9:16',
        seed=42, parameters={'width': 1080, 'height': 1920}, reference_assets=refs, output_dir=tmp_path)
    snapshot = {'provider': 'video_api', 'base_url': 'https://api.lk888.ai/v1', 'model_id': 'minimax-h3', 'settings': {}}
    return lk888_h3.Lk888H3Video(settings, snapshot, 'test-only-key'), request


def response(status=200, payload=None):
    return httpx.Response(status, json=payload if payload is not None else {},
                          request=httpx.Request('POST', 'https://api.lk888.ai/v2/video_generation'))


def test_submit_uses_official_contract_and_preserves_order_and_actual_mime(specimen, monkeypatch):
    provider, request = specimen
    calls = []
    async def send(settings, snapshot, method, endpoint, secret, **kwargs):
        calls.append((snapshot, method, endpoint, secret, kwargs))
        return response(payload={'task_id': 'task-123'})
    monkeypatch.setattr(lk888_h3, 'service_request', send)
    result = asyncio.run(provider.submit(request))
    snapshot, method, endpoint, key, options = calls[0]
    assert snapshot['base_url'] == 'https://api.lk888.ai'
    assert method == 'POST' and endpoint == '/v2/video_generation' and key == 'test-only-key'
    body = options['json']
    assert body['model'] == 'MiniMax-H3' and body['duration'] == 5
    assert body['resolution'] == '1080P' and body['ratio'] == '9:16'
    assert body['content'][0]['text'] == 'A person smiles.\nAvoid: watermark'
    for index, ref in enumerate(request.reference_assets):
        item = body['content'][index + 1]
        assert item['role'] == 'reference_image'
        assert item['image_url']['url'].startswith('data:image/' + ('jpeg' if index == 0 else 'png') + ';base64,')
        assert base64.b64decode(item['image_url']['url'].split(',', 1)[1]) == Path(ref['absolute_path']).read_bytes()
    assert result.provider_job_id == 'task-123' and result.status == 'queued'
    assert result.metadata['seed_applied'] is False and result.metadata['negative_prompt_applied'] is True
    assert [r['asset_id'] for r in result.metadata['reference_uploads']] == ['asset-0', 'asset-1']
    assert 'base64' not in str(result.metadata) and 'test-only-key' not in str(result.metadata)


@pytest.mark.parametrize('mutation,code', [
    ('base', 'lk888_endpoint_invalid'), ('model', 'lk888_h3_model_invalid'),
    ('too_many', 'lk888_reference_count'),
    ('motion', 'lk888_motion_unsupported'), ('empty_prompt', 'lk888_prompt_invalid'),
    ('prompt', 'lk888_prompt_too_long'), ('duration', 'lk888_duration_invalid'),
    ('ratio', 'lk888_ratio_invalid'), ('dimensions', 'lk888_dimensions_invalid'),
    ('hash', 'reference_hash_changed'), ('image', 'lk888_reference_invalid')])
def test_invalid_input_is_rejected_before_submission(specimen, monkeypatch, tmp_path, mutation, code):
    provider, request = specimen
    async def forbidden(*args, **kwargs):
        raise AssertionError('invalid input must not be sent')
    monkeypatch.setattr(lk888_h3, 'service_request', forbidden)
    if mutation == 'base': provider.snapshot['base_url'] = 'https://api.lk888.ai/v1/other'
    elif mutation == 'model': provider.snapshot['model_id'] = 'unknown'
    elif mutation == 'too_many': request.reference_assets = request.reference_assets * 5
    elif mutation == 'motion': request.motion_references = [{'id': 'motion'}]
    elif mutation == 'empty_prompt': request.prompt = ''
    elif mutation == 'prompt': request.prompt = 'x' * 7001
    elif mutation == 'duration': request.duration_seconds = 3
    elif mutation == 'ratio': request.aspect_ratio = 'unsupported'
    elif mutation == 'dimensions': request.parameters['width'] = 1000
    elif mutation == 'hash': request.reference_assets[0]['sha256'] = 'bad'
    elif mutation == 'image':
        path = tmp_path / 'invalid.png'; path.write_bytes(b'invalid')
        request.reference_assets[0].update(absolute_path=str(path), sha256=hashlib.sha256(b'invalid').hexdigest())
    with pytest.raises(AppError) as error:
        asyncio.run(provider.submit(request))
    assert error.value.code == code


@pytest.mark.parametrize('status,code', [(302, 'cloud_submission_unknown'), (408, 'cloud_submission_unknown'),
                                         (500, 'cloud_submission_unknown'), (401, 'cloud_video_submit_failed')])
def test_submit_error_is_not_retried(specimen, monkeypatch, status, code):
    provider, request = specimen; calls = []
    async def send(*args, **kwargs):
        calls.append(1); return response(status)
    monkeypatch.setattr(lk888_h3, 'service_request', send)
    with pytest.raises(AppError) as error: asyncio.run(provider.submit(request))
    assert error.value.code == code and len(calls) == 1


def test_lost_response_is_unknown(specimen, monkeypatch):
    provider, request = specimen; calls = []
    async def send(*args, **kwargs):
        calls.append(1); raise httpx.ReadTimeout('test')
    monkeypatch.setattr(lk888_h3, 'service_request', send)
    with pytest.raises(AppError) as error: asyncio.run(provider.submit(request))
    assert error.value.code == 'cloud_submission_unknown' and len(calls) == 1


def test_small_or_oversized_image_rejected_before_post(specimen, monkeypatch, tmp_path):
    provider, request = specimen
    async def forbidden(*args, **kwargs): raise AssertionError('invalid image submitted')
    monkeypatch.setattr(lk888_h3, 'service_request', forbidden)
    small = tmp_path / 'small.png'; Image.new('RGB', (299, 400)).save(small)
    request.reference_assets[0].update(absolute_path=str(small), sha256=hashlib.sha256(small.read_bytes()).hexdigest())
    with pytest.raises(AppError) as error: asyncio.run(provider.submit(request))
    assert error.value.code == 'lk888_reference_invalid'
    monkeypatch.setattr(lk888_h3.Lk888H3Video, '_max_image_bytes', 1)
    with pytest.raises(AppError) as error: asyncio.run(provider.submit(request))
    assert error.value.code == 'lk888_reference_too_large'


def test_request_without_motion_field_remains_compatible(specimen, monkeypatch):
    provider, request = specimen
    fields = asdict(request); fields.pop('motion_references')
    async def send(*args, **kwargs): return response(payload={'task_id': 'task-compatible'})
    monkeypatch.setattr(lk888_h3, 'service_request', send)
    result = asyncio.run(provider.submit(SimpleNamespace(**fields)))
    assert result.provider_job_id == 'task-compatible'


def test_text_only_is_supported_by_provider_contract(specimen, monkeypatch):
    provider, request = specimen; request.reference_assets = []
    async def send(*args, **kwargs):
        assert len(kwargs['json']['content']) == 1
        assert kwargs['json']['content'][0]['type'] == 'text'
        return response(payload={'task_id':'task-text-only'})
    monkeypatch.setattr(lk888_h3, 'service_request', send)
    assert asyncio.run(provider.submit(request)).provider_job_id == 'task-text-only'


def _system_block(product_only=False):
    rules = rules_bundle()
    text = ('\n'.join(rules['visual_language'][k] for k in ('scene','camera','audio')) if product_only
            else '\n'.join(rules['visual_language'].values()))
    if product_only:
        text += ('\nPreserve each referenced product shape, proportions, color, packaging and visible structure. '
                 'Exact printed text and logo reproduction require visual review or post-production overlays. '
                 'Do not invent performers or unsupported accessories.')
    return ('Cinematic production rules (version '+rules['versions']['production_rules_version']+'):\n'
            +text+'\nReviewed production designs, timed actions and continuity states: ')


def test_exact_rules_compaction_preserves_script_and_records_actual_payload(specimen, monkeypatch):
    provider, request = specimen
    user_start = '用户剧本/身份/动作/构图/参考映射:' + '人' * 6250
    user_end = '{"production":"服装状态","entities":"道具位置"}\noverall_soundscape:完整声音'
    request.prompt = user_start + '\n' + _system_block() + user_end
    original = request.prompt + '\nAvoid: watermark'
    assert len(original) > 7000
    calls = []
    async def send(*args, **kwargs): calls.append(kwargs['json']); return response(payload={'task_id':'task-compact'})
    monkeypatch.setattr(lk888_h3, 'service_request', send)
    result = asyncio.run(provider.submit(request)); sent = calls[0]['content'][0]['text']
    assert user_start in sent and user_end in sent and sent.endswith('Avoid: watermark')
    assert 'Cinematic production rules (version ' in sent
    assert result.metadata['prompt_adjustment'] == 'system_rules_compacted'
    assert result.metadata['sent_prompt'] == sent
    assert result.metadata['original_prompt_characters'] == len(original)
    assert result.metadata['sent_prompt_characters'] == len(sent) <= 7000
    assert result.metadata['sent_prompt_sha256'] == hashlib.sha256(sent.encode('utf-8')).hexdigest()
    repeated, details = lk888_h3._adapt_prompt(sent, '')
    assert repeated == sent and details['prompt_adjustment'] == 'none'


def test_product_only_system_rules_are_compacted_without_user_edits():
    prefix='商品几何色彩与动作不可删除:' + '物' * 6400
    suffix='{"production":"产品细节","entities":"产品版本"}\noverall_soundscape:真实环境音'
    prompt=prefix+'\n'+_system_block(product_only=True)+suffix
    assert len(prompt)>7000
    result, receipt=lk888_h3._adapt_prompt(prompt, '')
    assert prefix in result and suffix in result and receipt['prompt_adjustment']=='system_rules_compacted'


def _contact_prompt(*, explicit=False, pad=5200):
    from app.services.cinematic_quality import physical_directions
    states = [dict(entity_id='a', prop_key='A'), dict(entity_id='b', prop_key='B')]
    if explicit:
        states[1].update(holder='Alice', hand='右手', location='桌边', open_state='打开')
    data = {'production': {'prop_states': states, 'beats': []}, 'entities': [
        {'id': 'a', 'logical_id': 'A', 'spec': {'color': '红色', 'details': '甲' * 200}},
        {'id': 'b', 'logical_id': 'B', 'spec': {'color': '蓝色', 'details': '乙' * 200}},
    ]}
    canonical = physical_directions(data)
    design = json.dumps(data, ensure_ascii=False)
    prompt = ('subject_definitions:\n人物身份、道具、对白与真实场景\nretention_analysis:\n'
              '<Picture 3> (' + canonical + '\n[Shot 1] composition)\ndetailed_description:\n'
              + '用户剧本' + '人' * pad + '\n' + _system_block() + design
              + '\noverall_soundscape:\n室内自然声音')
    return prompt, data, canonical, design


def test_compaction_preserves_user_text_and_design_but_removes_only_placeholder_contacts():
    prompt, data, block, design = _contact_prompt()
    sent, receipt = lk888_h3._adapt_prompt(prompt, '水印')
    assert receipt['prompt_adjustment'] == 'system_rules_and_placeholder_contacts_compacted'
    assert len(sent) <= 7000 and design in sent
    assert '人物身份、道具、对白与真实场景' in sent and '用户剧本' + '人' * 5200 in sent
    assert 'overall_soundscape:\n室内自然声音' in sent and sent.endswith('Avoid: 水印')
    assert all('exactly ONE physical ' + name in sent for name in ('A', 'B'))
    assert 'Only specified holder holds this object' not in sent
    assert all(json.dumps(e['spec'], ensure_ascii=False) in sent for e in data['entities'])


def test_explicit_contact_constraint_remains_unchanged_while_placeholder_is_compacted():
    prompt, _, block, design = _contact_prompt(explicit=True, pad=5000)
    sent, receipt = lk888_h3._adapt_prompt(prompt, '')
    assert receipt['prompt_adjustment'] == 'system_rules_and_placeholder_contacts_compacted'
    assert block.splitlines()[1] in sent and design in sent
    assert block.splitlines()[0] not in sent


def test_product_reference_compaction_preserves_identity_mapping_and_reversible_design():
    product_id = 'product_' + 'a'*32
    design_id = 'design_' + 'b'*32
    character_id = 'spchar_' + 'c'*24
    image_id = 'pimage_' + 'd'*32
    visibility = {'keyframe_image_ids': [image_id],
                  'automation_product_evidence': {'anchor_entity_ids': [design_id], 'user_selection_reason': '英文盒面'}}
    definition = (f'defines only product {product_id} (原始名称), purpose packaging. '
                  'Preserve product appearance. Declared visibility: ' + json.dumps(visibility, ensure_ascii=False))
    design = {'production': {'wardrobe_states': {character_id: {'entity_id': design_id}},
                             'prop_states': [{'entity_id': design_id}], 'beats': []},
              'entities': [{'id': design_id, 'logical_id': '礼盒',
                            'spec': {'note': design_id, 'material': '纸板'}}]}
    user = '用户原文及对白：~1、英文盒面、不要修改。'
    prompt = ('subject_definitions:\n<Picture 2> ' + definition + '\n<Picture 3> ' + definition
              + '\nsummary:\n' + user + '\nReviewed production designs, timed actions and continuity states: '
              + json.dumps(design, ensure_ascii=False))
    compact, aliases = lk888_h3._compact_repeated_references(prompt)
    assert user in compact and '原始名称' in compact and '<Picture 2>' in compact and '<Picture 3>' in compact
    assert '<Picture 3> defines only the same product, purpose, visibility and constraints as <Picture 2>.' in compact
    assert aliases and not any(alias in prompt for alias in aliases)
    # Both references resolve to the original identifiers; an authored note
    # that happens to resemble an ID remains verbatim.
    assert design_id in compact and '"note":"' + design_id + '"' in compact
    marker = 'Reviewed production designs, timed actions and continuity states: '
    actual = json.loads(compact.split(marker, 1)[1])
    def expand(x):
        if isinstance(x, dict): return {aliases.get(k, k): expand(v) for k, v in x.items()}
        if isinstance(x, list): return [expand(v) for v in x]
        return aliases.get(x, x) if isinstance(x, str) else x
    assert expand(actual) == design
    original_visibility = json.loads(compact.split('Declared visibility: ', 1)[1].splitlines()[0])
    assert expand(original_visibility) == visibility


def test_distinct_product_references_are_not_merged():
    prompt = ('subject_definitions:\n<Picture 2> defines only product A. Declared visibility: {}\n'
              '<Picture 3> defines only product B. Declared visibility: {}\nsummary:\n用户原文')
    compact, _ = lk888_h3._compact_repeated_references(prompt)
    assert '<Picture 2> defines only product A.' in compact
    assert '<Picture 3> defines only product B.' in compact
    assert 'same product' not in compact


@pytest.mark.parametrize('mutation', ['unknown_contact', 'invalid_design', 'duplicated_retention', 'user_text_extra'])
def test_ambiguous_contacts_are_not_rewritten(mutation):
    prompt, _, block, _ = _contact_prompt(pad=5900)
    if mutation == 'unknown_contact':
        prompt = prompt.replace('Only specified holder holds this object', 'Only unspecified person holds this object', 1)
    elif mutation == 'invalid_design':
        prompt = prompt.replace('"entities": [', '"entities": "unknown", "unused": [', 1)
    elif mutation == 'duplicated_retention':
        prompt += '\nretention_analysis:\n用户原文'
    else:
        prompt += '\n用户内容：' + '文' * 3000
    with pytest.raises(AppError) as error:
        lk888_h3._adapt_prompt(prompt, '')
    assert error.value.code == 'lk888_prompt_too_long'


@pytest.mark.parametrize('case', ['unknown', 'duplicated', 'still_long'])
def test_unsafe_or_insufficient_compaction_is_rejected(case):
    if case == 'unknown': prompt='用户输入'+'人'*7200
    elif case == 'duplicated': prompt='人'*6000+_system_block()+'{}'+_system_block()+'{}'
    else: prompt='保留完整用户剧情'+'人'*7100+_system_block()+'{}'
    with pytest.raises(AppError) as error: lk888_h3._adapt_prompt(prompt, '')
    assert error.value.code == 'lk888_prompt_too_long'


@pytest.mark.parametrize('width,height,resolution', [(768,1365,'768P'), (1080,1920,'1080P'),
    (1440,2560,'2K'), (2160,3840,'4K')])
def test_resolution_tiers_are_explicit(specimen, monkeypatch, width, height, resolution):
    provider, request = specimen
    request.parameters = {'width': width, 'height': height}
    async def send(*args, **kwargs):
        assert kwargs['json']['resolution'] == resolution
        return response(payload={'task_id':'task-tier'})
    monkeypatch.setattr(lk888_h3, 'service_request', send)
    assert asyncio.run(provider.submit(request)).metadata['request_resolution'] == resolution


def test_unsupported_target_is_rejected_not_silently_downgraded(specimen, monkeypatch):
    provider, request = specimen
    request.parameters = {'width':720, 'height':1280}
    async def forbidden(*args, **kwargs): raise AssertionError('unsupported resolution submitted')
    monkeypatch.setattr(lk888_h3, 'service_request', forbidden)
    with pytest.raises(AppError) as error: asyncio.run(provider.submit(request))
    assert error.value.code == 'lk888_resolution_unsupported'


@pytest.mark.parametrize('state,expected', [('queued','queued'), ('running','running'), ('succeeded','generated'),
                                            ('failed','failed'), ('cancelled','cancelled')])
def test_poll_task_mapping_does_not_log_signed_output(specimen, monkeypatch, state, expected):
    provider, _ = specimen
    signed = 'https://cdn.example.invalid/out.mp4?signature=private'
    async def send(settings, snapshot, method, endpoint, key, **kwargs):
        assert snapshot['base_url'] == 'https://api.lk888.ai'
        assert method == 'GET' and endpoint == '/v2/query/video_generation/task-123'
        return response(payload={'task': {'id': 'task-123', 'status': state, 'content': {'url': signed}}})
    monkeypatch.setattr(lk888_h3, 'service_request', send)
    result = asyncio.run(provider.get_job('task-123'))
    assert result['status'] == expected and 'signature' not in str(result['raw'])
    assert result['output_url'] == (signed if state == 'succeeded' else None)


def test_succeeded_without_url_is_not_success(specimen, monkeypatch):
    provider, _ = specimen
    async def send(*args, **kwargs): return response(payload={'task': {'id': 'task-123', 'status': 'succeeded'}})
    monkeypatch.setattr(lk888_h3, 'service_request', send)
    with pytest.raises(AppError) as error: asyncio.run(provider.get_job('task-123'))
    assert error.value.code == 'cloud_video_status_unavailable'


def test_download_does_not_forward_provider_credentials(specimen, monkeypatch):
    provider, _ = specimen; signed = 'https://cdn.example.invalid/out.mp4?signature=private'
    async def download(settings, url):
        assert url == signed
        return b'example-video'
    monkeypatch.setattr(lk888_h3, 'download_public_video', download)
    assert asyncio.run(provider.download(signed)) == b'example-video'
