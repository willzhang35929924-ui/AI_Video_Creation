"""Isolated recovery proofs and managed HTTP idempotency; never model inference."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select

from app import models as m
from app.core.config import PROJECT_ROOT
from app.schemas_quality import QualitySettings
from app.services import video_quality as quality
from app.services.studio_pipeline import create_pipeline
from test_video_quality import context, source


@pytest.fixture(autouse=True)
def forbid_provider_requests(monkeypatch):
    async def forbidden(*_args, **_kwargs):
        raise AssertionError('Recovery contracts must never contact a model provider')

    def forbidden_native(*_args, **_kwargs):
        raise AssertionError('Recovery contracts must never launch native inference')

    monkeypatch.setattr(httpx.AsyncClient, 'request', forbidden)
    monkeypatch.setattr(quality, '_run_seedvr_native', forbidden_native)


@pytest.fixture
def recovery_case(context, client, settings, monkeypatch):
    session, factory, project, shot, raw = context
    path, source_sha = source(context, settings)
    caps = {'state': 'verified', 'manifest_sha256': 'isolated-frozen-model-manifest',
            'verified_resolutions': ['720p'], 'verified_aspect_ratios': ['16:9']}
    monkeypatch.setattr(quality, 'seedvr_status', lambda *_: copy.deepcopy(caps))
    item = m.QualityRender(source_video_id=raw.id, status='failed',
        error_message='Controlled collection failure after a known accepted prompt',
        snapshot_json={}, result_json={'source_preserved': True})
    session.add(item); session.flush()
    destination = settings.media_root / f'quality/{project.id}/{item.id}.mp4'
    destination.parent.mkdir(parents=True, exist_ok=True)
    paths = {name: destination.with_suffix('.seed-' + name + '.json')
             for name in ('prepared', 'submission', 'result')}
    prepared = {'original_source_sha256': source_sha, 'graph_sha256': 'isolated-frozen-graph',
                'sample_only': False, 'expected_frames': 24}
    paths['prepared'].write_text(json.dumps(prepared), encoding='utf-8')
    submission = {'status': 'SUBMITTED', 'prompt_id': 'isolated-original-prompt-not-real',
                  'base_url': 'http://127.0.0.1:8189', 'prepared': str(paths['prepared']),
                  'prepared_sha256': quality._file_sha(paths['prepared'])}
    paths['submission'].write_text(json.dumps(submission), encoding='utf-8')
    item.snapshot_json = {
        'settings': QualitySettings(processing='seedvr2').model_dump(),
        'project_id': project.id, 'shot_id': shot.id, 'source_relative_path': raw.relative_path,
        'source_sha256': source_sha,
        'seedvr2': {'manifest_sha256': caps['manifest_sha256'],
                    'helper_sha256': quality._file_sha(PROJECT_ROOT / 'scripts/deploy_seedvr2.py')},
        'native_execution': {'status': 'submitted', 'inference_submitted': True,
            'prompt_id': submission['prompt_id'], 'terminal_status': {'status_str': 'success', 'completed': True},
            'graph_sha256': prepared['graph_sha256'], 'prepared_sha256': submission['prepared_sha256'],
            'paths': {name: str(value) for name, value in paths.items()}}, 'route': None}
    session.commit()
    return SimpleNamespace(session=session, factory=factory, client=client, settings=settings,
        project=project, shot=shot, raw=raw, item=item, source=path, source_sha=source_sha,
        destination=destination, paths=paths, caps=caps, prepared=prepared, submission=submission)


def endpoint(fixture):
    return f'/api/quality/renders/{fixture.item.id}/resume-native'


def audit_count(fixture):
    return fixture.session.scalar(select(func.count()).select_from(m.AuditEvent).where(
        m.AuditEvent.object_id == fixture.item.id, m.AuditEvent.action == 'resume_same_native_output'))


@pytest.mark.parametrize('status', ['failed', 'unknown'])
def test_known_accepted_prompt_is_requeued_once_without_new_inference(recovery_case, status):
    f = recovery_case
    f.item.status = status; f.session.commit()
    before_files = {name: quality._file_sha(f.paths[name]) for name in ('prepared', 'submission')}
    first = f.client.post(endpoint(f), json={})
    assert first.status_code == 202, first.text
    body = first.json()
    assert body['id'] == f.item.id and body['status'] == 'queued'
    native = body['snapshot']['native_execution']
    assert native['resume_existing_submission'] is True
    assert native['prompt_id'] == f.submission['prompt_id']
    assert body['result']['recovery_history'] == [{
        'status': status, 'error': 'Controlled collection failure after a known accepted prompt',
        'action': 'collect_same_native_prompt', 'prompt_id': f.submission['prompt_id']}]
    second = f.client.post(endpoint(f), json={})
    assert second.status_code == 202 and second.json()['id'] == f.item.id
    f.session.expire_all()
    assert audit_count(f) == 1
    assert f.session.scalar(select(func.count()).select_from(m.QualityRender)) == 1
    assert f.session.scalar(select(func.count()).select_from(m.VideoRevision)) == 1
    assert len(f.item.result_json['recovery_history']) == 1
    assert not f.destination.exists() and not f.paths['result'].exists()
    assert {name: quality._file_sha(f.paths[name]) for name in before_files} == before_files
    assert quality._file_sha(f.source) == f.source_sha


@pytest.mark.parametrize('damage,code', [
    ('prepared_missing', 'seedvr_resume_evidence'),
    ('prepared_hash', 'seedvr_resume_evidence'),
    ('submission_missing', 'seedvr_resume_evidence'),
    ('native_paths', 'seedvr_resume_evidence'),
    ('submission_prepared_path', 'seedvr_resume_evidence'),
    ('submission_prepared_hash', 'seedvr_resume_evidence'),
    ('submission_prompt', 'seedvr_resume_evidence'),
    ('submission_endpoint', 'seedvr_resume_evidence'),
    ('submission_status', 'seedvr_resume_evidence'),
    ('prepared_source_hash', 'seedvr_resume_evidence'),
    ('prepared_graph_hash', 'seedvr_resume_evidence'),
    ('sample_only', 'seedvr_resume_evidence'),
    ('zero_frames', 'seedvr_resume_evidence'),
    ('canonical_output', 'seedvr_resume_evidence'),
    ('existing_result', 'seedvr_resume_evidence'),
    ('manifest', 'seedvr_resume_configuration_changed'),
    ('helper', 'seedvr_resume_configuration_changed'),
    ('runtime_unavailable', 'seedvr_resume_configuration_changed'),
    ('source_changed', 'seedvr_resume_source_changed'),
    ('source_owner', 'seedvr_resume_source'),
    ('inference_uncertain', 'seedvr_resume_unknown'),
    ('missing_prompt', 'seedvr_resume_unknown'),
    ('native_failed', 'seedvr_native_failed'),
])
def test_incomplete_or_changed_submission_is_not_resumed(recovery_case, damage, code):
    f = recovery_case
    snapshot = copy.deepcopy(f.item.snapshot_json)
    native = snapshot['native_execution']
    prepared = copy.deepcopy(f.prepared)
    submission = copy.deepcopy(f.submission)
    if damage == 'prepared_missing': f.paths['prepared'].unlink()
    elif damage == 'prepared_hash': f.paths['prepared'].write_text('changed isolated receipt', encoding='utf-8')
    elif damage == 'submission_missing': f.paths['submission'].unlink()
    elif damage == 'native_paths': native['paths']['prepared'] = str(f.source)
    elif damage == 'submission_prepared_path': submission['prepared'] = str(f.source)
    elif damage == 'submission_prepared_hash': submission['prepared_sha256'] = '0' * 64
    elif damage == 'submission_prompt': submission['prompt_id'] = 'another-isolated-original-prompt'
    elif damage == 'submission_endpoint': submission['base_url'] = 'https://example.invalid'
    elif damage == 'submission_status': submission['status'] = 'UNKNOWN_DO_NOT_RESUBMIT'
    elif damage == 'prepared_source_hash': prepared['original_source_sha256'] = '0' * 64
    elif damage == 'prepared_graph_hash': prepared['graph_sha256'] = 'changed-isolated-graph'
    elif damage == 'sample_only': prepared['sample_only'] = True
    elif damage == 'zero_frames': prepared['expected_frames'] = 0
    elif damage == 'canonical_output': f.destination.write_bytes(b'Existing isolated delivery must not be overwritten')
    elif damage == 'existing_result': f.paths['result'].write_text('{}', encoding='utf-8')
    elif damage == 'manifest': snapshot['seedvr2']['manifest_sha256'] = 'changed-isolated-model-manifest'
    elif damage == 'helper': snapshot['seedvr2']['helper_sha256'] = '0' * 64
    elif damage == 'runtime_unavailable': f.caps['state'] = 'unavailable'
    elif damage == 'source_changed': f.source.write_bytes(b'Explicitly changed isolated source')
    elif damage == 'source_owner': snapshot['project_id'] = 'another-isolated-project'
    elif damage == 'inference_uncertain': native['inference_submitted'] = None
    elif damage == 'missing_prompt': native['prompt_id'] = None
    elif damage == 'native_failed': native['terminal_status'] = {'status_str': 'error', 'completed': False}
    if prepared != f.prepared:
        f.paths['prepared'].write_text(json.dumps(prepared), encoding='utf-8')
        native['prepared_sha256'] = quality._file_sha(f.paths['prepared'])
        submission['prepared_sha256'] = native['prepared_sha256']
    if submission != f.submission:
        f.paths['submission'].write_text(json.dumps(submission), encoding='utf-8')
    f.item.snapshot_json = snapshot; f.session.commit()
    before = copy.deepcopy(f.item.snapshot_json)
    before_result = copy.deepcopy(f.item.result_json)
    before_files = {str(path): quality._file_sha(path)
                    for path in [f.source, f.destination, *f.paths.values()] if path.is_file()}
    response = f.client.post(endpoint(f), json={})
    assert response.status_code == 409, response.text
    assert response.json()['error']['code'] == code
    f.session.expire_all()
    assert f.item.status == 'failed' and f.item.snapshot_json == before and f.item.result_json == before_result
    assert audit_count(f) == 0
    assert f.session.scalar(select(func.count()).select_from(m.QualityRender)) == 1
    assert {path: quality._file_sha(path) for path in before_files} == before_files


def test_missing_render_returns_clear_not_found(client):
    response = client.post('/api/quality/renders/not-an-existing-isolated-render/resume-native', json={})
    assert response.status_code == 404 and response.json()['error']['code'] == 'quality_render_missing'


def test_pipeline_uses_exact_target_capabilities_instead_of_legacy_720_status(context, settings, monkeypatch):
    session, _, project, shot, _ = context
    shot.aspect_ratio = '9:16'
    requested = QualitySettings(resolution='1080p', aspect_ratio='9:16', processing='seedvr2').model_dump()
    session.add(m.QualityProfile(scope='project', target_id=project.id, revision=1, settings_json=requested))
    session.commit()
    legacy = {'state': 'verified', 'verified_resolutions': ['720p'], 'verified_aspect_ratios': ['16:9']}
    exact = {**legacy, 'verified_targets': [
        {'resolution': '720p', 'aspect_ratio': '16:9', 'width': 1280, 'height': 720},
        {'resolution': '1080p', 'aspect_ratio': '9:16', 'width': 1080, 'height': 1920}]}
    seen = []

    def target_capabilities(current_session, current_settings):
        seen.append((current_session, current_settings))
        return copy.deepcopy(exact)

    monkeypatch.setattr(quality, 'seedvr_status', lambda *_: copy.deepcopy(legacy))
    monkeypatch.setattr(quality, 'seedvr_capabilities', target_capabilities)
    assert quality.plan(requested, None, legacy)['supported'] is False
    run = create_pipeline(session, settings, project.id, {'auto_start': False})
    assert run.status == 'paused'
    assert run.snapshot_json['options']['quality_by_shot'][shot.id] == requested
    assert any(node['kind'] == 'quality' and node['status'] == 'pending' for node in run.nodes_json)
    assert seen == [(session, settings)]
    assert not list(session.scalars(select(m.QualityRender)))
