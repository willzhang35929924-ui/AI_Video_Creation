"""Isolated proof/queue contracts; fixtures are not real SeedVR2 model evidence.

The scanner receives real temporary receipt/output files with frozen digests.
No decoder, native model, Comfy queue, or external HTTP request is executed.
"""
import copy
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select

from app import models as m
from app.core.config import PROJECT_ROOT
from app.schemas_quality import QualitySettings
from app.services import seedvr_capabilities as seed
from app.services import video_quality as quality


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(autouse=True)
def forbid_provider_requests(monkeypatch):
    async def forbidden(*_args, **_kwargs):
        raise AssertionError('These isolated contracts must never request provider inference')

    def forbidden_native(*_args, **_kwargs):
        raise AssertionError('These isolated contracts must never run the native model')

    monkeypatch.setattr(httpx.AsyncClient, 'request', forbidden)
    monkeypatch.setattr(quality, '_run_seedvr_native', forbidden_native)
    monkeypatch.setattr(quality, '_sr_status', lambda *_: None)


@pytest.fixture
def native_proof(client, settings, monkeypatch):
    base = {'state': 'verified', 'manifest_sha256': 'isolated-current-manifest',
            'verified_resolutions': ['720p'], 'verified_aspect_ratios': ['16:9']}
    monkeypatch.setattr(quality, 'seedvr_status', lambda *_: copy.deepcopy(base))
    with client.app.state.session_factory() as session:
        project = m.Project(name='Isolated Seed target proof')
        session.add(project); session.flush()
        shot = m.StoryboardShot(project_id=project.id, order=1, scene_title='Synthetic reference',
                               aspect_ratio='9:16', duration_seconds=107 / 24)
        session.add(shot); session.flush()
        job = m.GenerationJob(project_id=project.id, shot_id=shot.id, status='generated',
            provider='explicit_test_fixture', prompt='Fixture only; never real model evidence', seed=1,
            parameters_json={}, reference_assets_json=[])
        session.add(job); session.flush()
        settings.media_root.mkdir(parents=True, exist_ok=True)
        source = settings.media_root / 'isolated-source.mp4'
        source.write_bytes(b'explicit isolated source bytes, not a model-generated video')
        raw = m.VideoRevision(project_id=project.id, shot_id=shot.id, generation_job_id=job.id,
            revision=1, relative_path=source.name, metadata_json={'sha256': digest(source)})
        session.add(raw); session.flush()
        relative = f'quality/{project.id}/isolated-seed-result.mp4'
        output = settings.media_root / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b'explicit isolated completed-file fixture, not an actual SeedVR2 output')
        processed = m.VideoRevision(project_id=project.id, shot_id=shot.id, generation_job_id=job.id,
            revision=2, relative_path=relative, metadata_json={'sha256': digest(output), 'processing_kind': 'video_upscale'})
        session.add(processed); session.flush()
        receipt_path = output.with_suffix('.seed-result.json')
        receipt = {'status': 'PASS_TECHNICAL_FILE', 'sample_only': False,
            'prompt_id': 'isolated-native-receipt-not-a-real-prompt',
            'comfy_execution_status': {'status_str': 'success', 'completed': True},
            'source_sha256': digest(source), 'frames_processed': 107, 'source_frames': 107,
            'checks': {'frames_equal': True, 'target_dimensions_equal': True, 'fps_equal': True,
                'source_unchanged': True, 'audio_packets_equal': True, 'actual_graph_matches_frozen': True,
                'full_decode_exit_code': 0}}
        receipt_path.write_text(json.dumps(receipt), encoding='utf-8')
        render = m.QualityRender(source_video_id=raw.id, video_id=processed.id, relative_path=relative,
            status='generated', snapshot_json={
                'settings': QualitySettings(resolution='1080p', aspect_ratio='9:16', processing='seedvr2').model_dump(),
                'project_id': project.id, 'shot_id': shot.id, 'generation_job_id': job.id,
                'source_sha256': digest(source),
                'seedvr2': {'manifest_sha256': base['manifest_sha256'],
                            'helper_sha256': digest(PROJECT_ROOT / 'scripts/deploy_seedvr2.py')},
                'native_execution': {'status': 'completed', 'inference_submitted': True,
                    'terminal_status': {'status_str': 'success', 'completed': True},
                    'prompt_id': receipt['prompt_id'], 'receipt_path': str(receipt_path),
                    'receipt_sha256': digest(receipt_path)}},
            result_json={'final_actual': {'width': 1080, 'height': 1920, 'frames': 107,
                'fps': '24/1', 'duration': 107 / 24, 'full_decode': {'exit_code': 0}}, 'sha256': digest(output)})
        session.add(render); session.commit()
        yield SimpleNamespace(session=session, client=client, settings=settings, project=project,
            shot=shot, raw=raw, processed=processed, render=render, source=source,
            output=output, receipt_path=receipt_path, receipt=receipt, base=base)


def target_pairs(capabilities):
    return {(target['resolution'], target['aspect_ratio']) for target in capabilities['verified_targets']}


def test_complete_target_proof_adds_only_its_exact_pair(native_proof):
    f = native_proof
    capabilities = seed.capabilities(f.session, f.settings)
    assert target_pairs(capabilities) == {('720p', '16:9'), ('1080p', '9:16')}
    assert seed.target_supported(QualitySettings(resolution='1080p', aspect_ratio='9:16'), capabilities)
    assert not seed.target_supported(QualitySettings(resolution='1080p', aspect_ratio='16:9'), capabilities)
    assert not seed.target_supported(QualitySettings(resolution='720p', aspect_ratio='9:16'), capabilities)
    assert not seed.target_supported(QualitySettings(resolution='4k', aspect_ratio='9:16'), capabilities)
    target = next(target for target in capabilities['verified_targets'] if target['resolution'] == '1080p')
    assert target['render_id'] == f.render.id and target['frames'] == 107
    assert target['output_sha256'] == digest(f.output)


def test_word_import_accepts_saved_complete_target_proof_beyond_legacy_manifest(native_proof,monkeypatch):
    """Real DOCX/API import; native proof is explicitly an isolated receipt fixture."""
    from app.services import super_resolution
    from test_studio_import import screenplay
    f=native_proof
    monkeypatch.setattr(super_resolution,'status',lambda *_:None)
    requested=QualitySettings(resolution='1080p',aspect_ratio='9:16',mode='standard',processing='seedvr2')
    legacy=quality.seedvr_status(f.settings)
    capabilities=seed.capabilities(f.session,f.settings)
    assert not quality.plan(requested,None,legacy)['supported']
    assert quality.plan(requested,None,capabilities)['supported']
    saved=f.client.put('/api/quality/profiles/system/default',json={
        'settings':requested.model_dump(),'expected_revision':0})
    assert saved.status_code==200,saved.text
    assert saved.json()['plan']['supported'] is True
    tracked=(m.Project,m.StudioRun,m.GenerationJob,m.QualityRender,m.MediaProcessTask)
    before={model: f.session.scalar(select(func.count()).select_from(model)) for model in tracked}
    source_hash=digest(f.source);output_hash=digest(f.output)
    response=f.client.post('/api/studio/import',files={'file':('verified-portrait.docx',screenplay(aspect_ratio='9:16'))},
        data={'auto_start':'false','quality_json':json.dumps(saved.json()['settings'])})
    assert response.status_code==200,response.text
    result=response.json()
    assert result['status']=='imported' and result['parser']['language_model_called'] is False
    assert not result.get('pipeline_run_id') and not result.get('pipeline')
    with f.client.app.state.session_factory() as db:
        profile=db.scalar(select(m.QualityProfile).where(m.QualityProfile.scope=='project',m.QualityProfile.target_id==result['project_id']))
        assert profile and profile.settings_json==requested.model_dump() and profile.revision==1
        assert db.scalar(select(func.count()).select_from(m.Project))==before[m.Project]+1
        for model in tracked[1:]:
            assert db.scalar(select(func.count()).select_from(model))==before[model]
        assert db.scalar(select(func.count()).select_from(m.GenerationJob).where(m.GenerationJob.project_id==result['project_id']))==0
        assert all(db.get(m.StoryboardShot,shot_id).aspect_ratio=='9:16' for shot_id in result['shot_ids'])
    assert digest(f.source)==source_hash and digest(f.output)==output_hash


@pytest.mark.parametrize('resolution,aspect_ratio',[('1080p','16:9'),('720p','9:16')])
@pytest.mark.parametrize('auto_start',[False,True])
def test_word_import_still_rejects_unverified_exact_target_before_any_persistence(native_proof,monkeypatch,resolution,aspect_ratio,auto_start):
    from app.services import super_resolution
    from test_studio_import import screenplay
    f=native_proof
    monkeypatch.setattr(super_resolution,'status',lambda *_:None)
    capabilities=seed.capabilities(f.session,f.settings)
    assert resolution in capabilities['verified_resolutions']
    assert aspect_ratio in capabilities['verified_aspect_ratios']
    requested=QualitySettings(resolution=resolution,aspect_ratio=aspect_ratio,processing='seedvr2')
    assert not quality.plan(requested,None,capabilities)['supported']
    tracked=(m.Project,m.WordImport,m.StudioRun,m.GenerationJob,m.QualityRender,m.MediaProcessTask)
    before={model:f.session.scalar(select(func.count()).select_from(model)) for model in tracked}
    response=f.client.post('/api/studio/import',files={'file':('unverified-pair.docx',screenplay(aspect_ratio=aspect_ratio))},
        data={'auto_start':str(auto_start).lower(),'quality_json':json.dumps(requested.model_dump())})
    assert response.status_code==409,response.text
    assert response.json()['error']['code']=='quality_method_unverified'
    with f.client.app.state.session_factory() as db:
        for model in tracked:
            assert db.scalar(select(func.count()).select_from(model))==before[model]


@pytest.mark.parametrize('damage', [
    'missing_receipt', 'malformed_receipt', 'receipt_hash', 'wrong_receipt_path',
    'missing_output', 'corrupted_output', 'graph', 'dropped_source_frame', 'processed_frame_count',
    'sample_only', 'failed_comfy', 'source_hash', 'wrong_prompt', 'non_integer_decode',
    'manifest', 'helper', 'native_unknown', 'inference_not_submitted', 'wrong_dimensions',
])
def test_invalid_proof_never_expands_verified_targets(native_proof, damage):
    f = native_proof
    receipt = copy.deepcopy(f.receipt)
    snapshot = copy.deepcopy(f.render.snapshot_json)
    result = copy.deepcopy(f.render.result_json)
    if damage == 'missing_receipt': f.receipt_path.unlink()
    elif damage == 'malformed_receipt':
        f.receipt_path.write_text('{not-json', encoding='utf-8')
        snapshot['native_execution']['receipt_sha256'] = digest(f.receipt_path)
    elif damage == 'receipt_hash': snapshot['native_execution']['receipt_sha256'] = '0' * 64
    elif damage == 'wrong_receipt_path': snapshot['native_execution']['receipt_path'] = str(f.source)
    elif damage == 'missing_output': f.output.unlink()
    elif damage == 'corrupted_output': f.output.write_bytes(b'changed isolated output')
    elif damage == 'graph': receipt['checks']['actual_graph_matches_frozen'] = False
    elif damage == 'dropped_source_frame': receipt['source_frames'] = 106
    elif damage == 'processed_frame_count': receipt['frames_processed'] = 106
    elif damage == 'sample_only': receipt['sample_only'] = True
    elif damage == 'failed_comfy': receipt['comfy_execution_status']['status_str'] = 'error'
    elif damage == 'source_hash': receipt['source_sha256'] = '0' * 64
    elif damage == 'wrong_prompt': receipt['prompt_id'] = 'another-isolated-prompt'
    elif damage == 'non_integer_decode': receipt['checks']['full_decode_exit_code'] = False
    elif damage == 'manifest': snapshot['seedvr2']['manifest_sha256'] = 'stale-manifest'
    elif damage == 'helper': snapshot['seedvr2']['helper_sha256'] = '0' * 64
    elif damage == 'native_unknown': snapshot['native_execution']['status'] = 'unknown'
    elif damage == 'inference_not_submitted': snapshot['native_execution']['inference_submitted'] = False
    elif damage == 'wrong_dimensions': result['final_actual'].update(width=1920, height=1080)
    # JSON types matter: Python dict equality otherwise equates False and 0.
    if json.dumps(receipt, sort_keys=True) != json.dumps(f.receipt, sort_keys=True):
        f.receipt_path.write_text(json.dumps(receipt), encoding='utf-8')
        snapshot['native_execution']['receipt_sha256'] = digest(f.receipt_path)
    f.render.snapshot_json = snapshot; f.render.result_json = result; f.session.commit()
    assert target_pairs(seed.capabilities(f.session, f.settings)) == {('720p', '16:9')}


def test_unavailable_current_runtime_cannot_use_old_success(native_proof):
    f = native_proof
    f.base['state'] = 'unavailable'
    result = seed.capabilities(f.session, f.settings)
    assert result['state'] == 'unavailable' and target_pairs(result) == set()


def test_legacy_arrays_do_not_imply_a_cartesian_product():
    legacy = {'state': 'verified', 'verified_resolutions': ['720p', '1080p'],
              'verified_aspect_ratios': ['16:9', '9:16']}
    assert seed.target_supported(QualitySettings(), legacy)
    assert not seed.target_supported(QualitySettings(resolution='1080p', aspect_ratio='9:16'), legacy)
    assert not seed.target_supported(QualitySettings(resolution='720p', aspect_ratio='9:16'), legacy)


@pytest.mark.parametrize('processing,resolution', [('lanczos', '1080p'), ('seedvr2', '2k'), ('seedvr2', '4k')])
def test_load_test_rejects_unsupported_test_scope(native_proof, processing, resolution):
    f = native_proof
    response = f.client.post('/api/quality/seedvr2/load-test', json={'video_id': f.raw.id,
        'settings': {'resolution': resolution, 'aspect_ratio': '9:16', 'processing': processing}})
    assert response.status_code == 422 and response.json()['error']['code'] == 'seedvr_test_scope'


def test_ordinary_render_cannot_bypass_an_unverified_target(native_proof):
    f = native_proof
    f.render.status = 'failed'; f.session.commit()
    payload = {'video_id': f.raw.id, 'settings': {'resolution': '1080p', 'aspect_ratio': '9:16', 'processing': 'seedvr2'}}
    response = f.client.post('/api/quality/renders', json=payload)
    assert response.status_code == 409 and response.json()['error']['code'] == 'quality_method_unverified'
    response = f.client.post('/api/quality/renders', json={**payload, 'allow_unverified_seed': True})
    assert response.status_code == 422


def test_load_test_does_not_bypass_a_missing_verified_runtime(native_proof):
    f = native_proof
    f.base['state'] = 'unavailable'
    response = f.client.post('/api/quality/seedvr2/load-test', json={'video_id': f.raw.id,
        'settings': {'resolution': '1080p', 'aspect_ratio': '9:16', 'processing': 'seedvr2'}})
    assert response.status_code == 409 and response.json()['error']['code'] == 'quality_method_unverified'


def test_managed_load_test_preserves_profile_and_reuses_queue_without_retrying_unknown(native_proof):
    f = native_proof
    f.render.status = 'failed'
    saved = QualitySettings(resolution='1080p', aspect_ratio='9:16', processing='lanczos').model_dump()
    profile = m.QualityProfile(scope='project', target_id=f.project.id, revision=4, settings_json=saved)
    f.session.add(profile); f.session.commit()
    payload = {'video_id': f.raw.id, 'settings': {**saved, 'processing': 'seedvr2'}}
    first = f.client.post('/api/quality/seedvr2/load-test', json=payload)
    assert first.status_code == 202, first.text
    body = first.json()
    assert body['status'] == 'queued' and body['snapshot']['verification_run'] is True
    assert body['snapshot']['plan']['supported'] is False
    assert body['snapshot']['plan']['requested'] == {'resolution': '1080p', 'width': 1080, 'height': 1920, 'aspect_ratio': '9:16'}
    second = f.client.post('/api/quality/seedvr2/load-test', json=payload)
    assert second.status_code == 202 and second.json()['id'] == body['id']
    f.session.expire_all()
    assert profile.revision == 4 and profile.settings_json == saved
    queued = f.session.get(m.QualityRender, body['id'])
    queued.status = 'unknown'; f.session.commit()
    third = f.client.post('/api/quality/seedvr2/load-test', json=payload)
    assert third.status_code == 409 and third.json()['error']['code'] == 'quality_dispatch_unknown'
    assert len(list(f.session.scalars(select(m.QualityRender)))) == 2
