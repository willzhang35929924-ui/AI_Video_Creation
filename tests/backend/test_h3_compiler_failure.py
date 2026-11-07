import json
from datetime import datetime, timezone
from pathlib import PureWindowsPath

import pytest

from app.services import h3_context_provider
from app.services.jobs import _h3_source_gate_message


@pytest.fixture
def native_failure(tmp_path, monkeypatch):
    root = tmp_path / 'h3_local_backend_profile_v3'
    evidence = root / '20000101-000000-500000Z-00000001' / 'compiler-001'
    evidence.mkdir(parents=True)
    receipt = evidence / 'source-gate.json'
    receipt.write_text(json.dumps({
        'profile': 'local-h3ir-qwen4b-v3', 'status': 'BLOCKED',
        'source': 'draft', 'required_source': 'written',
        'fallback_reason': 'the written brief still failed after 2 correction pass(es): P5-camera-no-motion-type',
    }), encoding='utf-8')
    monkeypatch.setattr(h3_context_provider, 'WINDOWS_SCOPE_ROOT', root)
    prompt_id = '00000000-0000-4000-8000-000000000001'
    job_id = 'job_00000000000000000000000000000001'
    start = int(datetime(2000, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    graph = {'4': {'class_type': 'VideoFactoryLocalH3IRCompile', 'inputs': {}}}
    failure = {
        'prompt_id': prompt_id, 'timestamp': start + 1000,
        'node_id': '4', 'node_type': 'VideoFactoryLocalH3IRCompile',
        'exception_message': "Compiler source gate BLOCKED 'draft'; requires actual source=written without fallback; "
                             f'evidence: {PureWindowsPath(evidence)}\n',
    }
    result = {
        'provider_job_id': prompt_id,
        'error': 'Node 4 (VideoFactoryLocalH3IRCompile): LocalProfileBlocked: ' + failure['exception_message'],
        'raw': {
            'prompt': [1, prompt_id, graph, {'client_id': job_id}],
            'status': {'messages': [
                ['execution_start', {'prompt_id': prompt_id, 'timestamp': start}],
                ['execution_error', failure],
            ]},
        },
    }
    args = (result, job_id, prompt_id, h3_context_provider.graph_digest(graph), 'local-h3ir-qwen4b-v3')
    return args, failure, receipt


def test_h3_camera_failure_surfaces_specific_rule_without_exposing_receipt_path(native_failure):
    args, failure, receipt = native_failure
    message = _h3_source_gate_message(*args, read_local_receipt=True)
    assert 'P5-camera-no-motion-type' in message
    assert 'Push In' in message and 'Shake Slightly' in message
    assert 'source-gate.json' not in message and 'compiler-001' not in message
    assert str(PureWindowsPath(receipt.parent)) in failure['exception_message']


def test_native_correction_rounds_report_multiple_semicolon_separated_rules(native_failure):
    args, _, receipt = native_failure
    content = json.loads(receipt.read_text(encoding='utf-8'))
    content['fallback_reason'] = ('the written brief still failed after 2 correction pass(es): '
                                  'P1-no-style-opening; T1-no-shot')
    receipt.write_text(json.dumps(content), encoding='utf-8')
    message = _h3_source_gate_message(*args, read_local_receipt=True)
    assert '风格说明' in message
    assert 'P1-no-style-opening; T1-no-shot' in message


@pytest.mark.parametrize('untrusted', ['client', 'graph', 'scope', 'time', 'reason', 'provider'])
def test_h3_failure_does_not_use_unbound_or_untrusted_receipt(native_failure, untrusted):
    args, failure, receipt = native_failure
    result = args[0]
    if untrusted == 'client':
        result['raw']['prompt'][3]['client_id'] = 'another-job'
    elif untrusted == 'graph':
        result['raw']['prompt'][2]['4']['inputs']['seed'] = 1
    elif untrusted == 'scope':
        failure['exception_message'] = failure['exception_message'].replace('compiler-001', r'..\compiler-001')
    elif untrusted == 'time':
        failure['timestamp'] -= 999
    elif untrusted == 'reason':
        raw = json.loads(receipt.read_text(encoding='utf-8'))
        raw['fallback_reason'] += r' C:\private\token.txt'
        receipt.write_text(json.dumps(raw), encoding='utf-8')
    message = _h3_source_gate_message(*args, read_local_receipt=(untrusted != 'provider'))
    assert '本镜 Context-IR 编译未通过' in message
    assert 'P5-camera-no-motion-type' not in message
    assert 'private' not in message and 'compiler-001' not in message
