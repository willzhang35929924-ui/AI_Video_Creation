"""Real SQLite and synthetic decoded source media; no provider/inference requests."""
import copy
import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import select

from app import models as m
from app.core.db import Base, build_engine, build_session_factory
from app.core.errors import AppError
from app.schemas_quality import QualitySettings
from app.services.ai_services import service_snapshot
from app.services.studio_quality_apply import apply_saved_quality
from app.services import task_routing, video_quality as q


@pytest.fixture
def context(settings):
    engine = build_engine(settings.database_url)
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    with factory() as session:
        project = m.Project(name='Isolated explicit quality application')
        session.add(project); session.flush()
        shot = m.StoryboardShot(project_id=project.id, order=1, scene_title='Synthetic source',
                               aspect_ratio='16:9', duration_seconds=1)
        session.add(shot); session.flush()
        job = m.GenerationJob(project_id=project.id, shot_id=shot.id, status='generated',
            provider='explicit_test_fixture', prompt='Synthetic test source, not model output', seed=1,
            parameters_json={}, reference_assets_json=[])
        session.add(job); session.flush()
        video = m.VideoRevision(project_id=project.id, shot_id=shot.id, generation_job_id=job.id,
                                relative_path='synthetic-source.mp4', revision=1, metadata_json={})
        session.add(video); session.commit()
        yield session, factory, project, shot, video
    engine.dispose()


def source(context, settings):
    ffmpeg = shutil.which('ffmpeg') or 'D:/comfyui/ComfyUI/ffmpeg.exe'
    ffprobe = shutil.which('ffprobe') or 'D:/comfyui/ComfyUI/ffprobe.exe'
    if not Path(ffmpeg).is_file() or not Path(ffprobe).is_file():
        pytest.skip('Actual FFmpeg tools required for isolated decoded-media tests')
    settings.ffmpeg_path = ffmpeg; settings.ffprobe_path = ffprobe
    session, _, _, _, video = context
    settings.media_root.mkdir(parents=True, exist_ok=True)
    path = settings.media_root / video.relative_path
    subprocess.run([ffmpeg, '-v', 'error', '-n', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=24:duration=1',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=1',
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(path)],
                   capture_output=True, check=True, timeout=60)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    video.metadata_json = {'sha256': digest}
    session.commit()
    return path, digest


@pytest.fixture
def prepared(context, settings, monkeypatch):
    session, factory, project, shot, video = context
    path, digest = source(context, settings)
    _, checks = q.measured(path, settings)
    for check in checks:
        session.add(m.QualityCheck(video_revision_id=video.id, check_type=check['check_type'],
                                  status=check['status'], details_json=check['details']))
    service = m.AIService(name='Explicit isolated SeedVR route', module='video_upscale', provider='comfyui',
        base_url='http://127.0.0.1:8189', enabled=True, is_default=False,
        default_model='seedvr2_3b_fp8_e4m3fn.safetensors', settings_json={'runtime_family': 'seedvr2'})
    session.add(service); session.flush()
    route = {'slot': 'video_upscale', 'configured': True,
             'profile': {'revision': 1, 'inherited_from': None, 'settings': {'parameters': {}}},
             'selected': {'service_snapshot': service_snapshot(service)}}
    session.add(m.RoutePolicy(scope='project', target_id=project.id, slot='video_upscale', revision=2,
        settings_json={'mode': 'local_only', 'choices': [{'service_id': service.id, 'model_id': service.default_model}]}))
    old = QualitySettings(resolution='720p', processing='lanczos').model_dump()
    new = QualitySettings(resolution='720p', mode='high', processing='seedvr2').model_dump()
    session.add(m.QualityProfile(scope='project', target_id=project.id, revision=3, settings_json=new))
    job = session.get(m.GenerationJob, video.generation_job_id)
    job.parameters_json = {'accepted_generation': 'must remain untouched', 'width': 320, 'height': 180}
    other_route = {'selected': {'service_snapshot': {'service_id': 'accepted_h3', 'service_revision': 7}},
                   'profile': {'settings': {'parameters': {'steps': 30}}}}
    nodes = [
        {'id': 'video:' + shot.id, 'kind': 'video', 'target_id': shot.id, 'status': 'succeeded',
         'child_id': job.id, 'video_id': video.id, 'attempts': 1, 'history': [], 'dependencies': []},
        {'id': 'quality:' + shot.id, 'kind': 'quality', 'target_id': shot.id, 'status': 'exception',
         'child_id': None, 'attempts': 0, 'history': [], 'code': 'quality_route_processing_mismatch',
         'error': 'previous explicit configuration conflict', 'route': route, 'dependencies': ['video:' + shot.id]},
        {'id': 'quality_check:' + shot.id, 'kind': 'quality_check', 'target_id': shot.id,
         'status': 'waiting_dependencies', 'child_id': None, 'attempts': 0, 'history': [],
         'code': None, 'error': None, 'dependencies': ['quality:' + shot.id]},
    ]
    run = m.StudioRun(project_id=project.id, status='running', nodes_json=nodes,
        snapshot_json={'shot_ids': [shot.id], 'services': {'accepted': {'revision': 11}},
            'options': {'quality': old, 'quality_by_shot': {shot.id: old},
                        'allow_cloud_costs': False, 'allow_paid_fallback': False},
            'routes': {'video_generation': other_route, 'video_upscale': route},
            'routes_by_shot': {shot.id: {'video_generation': other_route, 'video_upscale': route}}},
        result_json={})
    session.add(run); session.commit()
    caps = {'state': 'verified', 'verified_resolutions': ['720p'], 'verified_aspect_ratios': ['16:9'],
            'verified_targets': [{'resolution': '720p', 'aspect_ratio': '16:9', 'width': 1280, 'height': 720}]}
    monkeypatch.setattr(q, 'seedvr_capabilities', lambda *_: caps, raising=False)
    monkeypatch.setattr(task_routing, 'manifest_status', lambda *_: {
        'state': 'verified', 'manifest': {'model_id': service.default_model, 'task_capabilities': ['video_upscale']}})
    return session, project, shot, video, run, service, path, digest, copy.deepcopy(new)


def test_apply_updates_only_undispatched_quality_and_checks_and_is_idempotent(prepared, settings):
    session, project, shot, video, run, _, path, digest, new = prepared
    accepted = copy.deepcopy(run.nodes_json[0]); before = copy.deepcopy(run.snapshot_json)
    parameters = copy.deepcopy(session.get(m.GenerationJob, video.generation_job_id).parameters_json)
    result = apply_saved_quality(session, settings, project.id, expected_profile_revision=3)
    session.commit()
    assert result['applied'] == [shot.id] and result['skipped'] == []
    assert run.nodes_json[0] == accepted
    assert [n['status'] for n in run.nodes_json[1:]] == ['pending', 'pending']
    assert run.nodes_json[1]['history'][0]['previous']['code'] == 'quality_route_processing_mismatch'
    assert run.snapshot_json['options']['quality_by_shot'][shot.id] == new
    assert run.snapshot_json['options']['quality'] == new
    assert run.snapshot_json['routes_by_shot'][shot.id]['video_upscale']['profile']['inherited_from'] == 'project:' + project.id
    assert run.snapshot_json['routes']['video_generation'] == before['routes']['video_generation']
    assert run.snapshot_json['routes_by_shot'][shot.id]['video_generation'] == before['routes_by_shot'][shot.id]['video_generation']
    assert run.snapshot_json['services'] == before['services']
    assert run.snapshot_json['options']['allow_cloud_costs'] is False
    assert run.snapshot_json['options']['allow_paid_fallback'] is False
    assert session.get(m.GenerationJob, video.generation_job_id).parameters_json == parameters
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert not list(session.scalars(select(m.QualityRender)))
    history = copy.deepcopy(run.nodes_json)
    again = apply_saved_quality(session, settings, project.id, expected_profile_revision=3)
    assert again['applied'] == [] and again['skipped'][0]['code'] == 'quality_settings_already_current'
    assert run.nodes_json == history


@pytest.mark.parametrize('status,child', [('running', None), ('unknown', None), ('succeeded', None),
                                          ('cancelled', None), ('exception', 'accepted-quality-child')])
def test_existing_or_uncertain_quality_is_never_replaced(prepared, settings, status, child):
    session, project, _, _, run, *_ = prepared
    nodes = copy.deepcopy(run.nodes_json); nodes[1].update(status=status, child_id=child)
    run.nodes_json = nodes; session.commit(); before = copy.deepcopy(run.snapshot_json)
    result = apply_saved_quality(session, settings, project.id, 3)
    assert not result['applied'] and result['skipped'][0]['code'] == 'quality_apply_child_protected'
    assert run.nodes_json == nodes and run.snapshot_json == before


def test_downstream_result_unknown_or_child_protects_entire_shot(prepared, settings):
    session, project, _, _, run, *_ = prepared
    nodes = copy.deepcopy(run.nodes_json); nodes[2].update(status='unknown', child_id='unknown-check-child')
    run.nodes_json = nodes; session.commit(); before = copy.deepcopy(run.snapshot_json)
    result = apply_saved_quality(session, settings, project.id, 3)
    assert not result['applied'] and result['skipped'][0]['code'] == 'quality_apply_downstream_protected'
    assert run.nodes_json == nodes and run.snapshot_json == before


def test_paused_run_applies_configuration_without_resuming_it(prepared, settings):
    session, project, shot, _, run, *_ = prepared
    run.status = 'paused'; session.commit()
    result = apply_saved_quality(session, settings, project.id, 3)
    assert result['applied'] == [shot.id] and result['run_status'] == run.status == 'paused'


@pytest.mark.parametrize('kind', ['revision', 'products', 'completed', 'rendering'])
def test_stale_or_protected_run_controls_reject_without_changes(prepared, settings, kind):
    session, project, _, _, run, *_ = prepared
    if kind == 'products': run.snapshot_json = {**run.snapshot_json, 'product_references_changed': True}
    if kind in {'completed', 'rendering'}: run.status = kind
    session.commit(); before = copy.deepcopy(run.nodes_json)
    with pytest.raises(AppError):
        apply_saved_quality(session, settings, project.id, 2 if kind == 'revision' else 3)
    assert run.nodes_json == before


def test_shot_override_is_kept_and_explicit_lanczos_removes_only_affected_seed_route(prepared, settings):
    session, project, shot, _, run, *_ = prepared
    override = QualitySettings(resolution='1080p', mode='preview', processing='lanczos').model_dump()
    session.add(m.QualityProfile(scope='shot', target_id=shot.id, revision=1, settings_json=override)); session.commit()
    old_global = copy.deepcopy(run.snapshot_json['routes']['video_upscale'])
    result = apply_saved_quality(session, settings, project.id, 3)
    assert result['applied'] == [shot.id]
    assert run.snapshot_json['options']['quality_by_shot'][shot.id] == override
    route = run.snapshot_json['routes_by_shot'][shot.id]['video_upscale']
    assert route['configured'] is False and route['selected'] is None
    assert not task_routing.route_is_explicit(route)
    assert run.snapshot_json['routes']['video_upscale'] == old_global


def test_aspect_mismatch_does_not_stretch_or_change_shot(prepared, settings):
    session, project, shot, _, run, *_ = prepared
    shot.aspect_ratio = '9:16'; session.commit(); before = copy.deepcopy(run.snapshot_json)
    result = apply_saved_quality(session, settings, project.id, 3)
    assert not result['applied'] and result['skipped'][0]['code'] == 'quality_apply_aspect_mismatch'
    assert shot.aspect_ratio == '9:16' and run.snapshot_json == before


def test_unfinished_video_does_not_change_generation_mapping(prepared, settings):
    session, project, _, video, run, *_ = prepared
    job = session.get(m.GenerationJob, video.generation_job_id); job.status = 'running'; session.commit()
    before = copy.deepcopy(run.snapshot_json)
    result = apply_saved_quality(session, settings, project.id, 3)
    assert not result['applied'] and result['skipped'][0]['code'] == 'quality_apply_source_mismatch'
    assert run.snapshot_json == before


def test_unsupported_cloud_upscale_is_not_replaced_or_authorized(prepared, settings):
    session, project, _, _, run, service, *_ = prepared
    service.provider = 'openai_compatible'; service.base_url = 'https://example.invalid/v1'
    session.commit(); before = copy.deepcopy(run.snapshot_json)
    result = apply_saved_quality(session, settings, project.id, 3)
    assert not result['applied']
    assert result['skipped'][0]['code'] in {'task_route_blocked', 'quality_route_adapter_unavailable'}
    assert run.snapshot_json == before and run.snapshot_json['options']['allow_cloud_costs'] is False


def test_active_audio_transformation_is_preserved(prepared, settings):
    session, project, shot, _, run, *_ = prepared
    run.nodes_json = [*run.nodes_json, {'id': 'audio_mix:' + shot.id, 'kind': 'audio_mix',
        'target_id': shot.id, 'status': 'running', 'child_id': 'accepted-audio-child', 'dependencies': []}]
    session.commit(); before = copy.deepcopy(run.nodes_json)
    result = apply_saved_quality(session, settings, project.id, 3)
    assert not result['applied'] and result['skipped'][0]['code'] == 'quality_apply_media_busy'
    assert run.nodes_json == before


def test_cross_shot_downstream_is_not_reset(prepared, settings):
    session, project, shot, _, run, *_ = prepared
    other = m.StoryboardShot(project_id=project.id, order=2, aspect_ratio='16:9', scene_title='Other synthetic shot')
    session.add(other); session.flush()
    nodes = copy.deepcopy(run.nodes_json); nodes[2]['target_id'] = other.id
    run.nodes_json = nodes; session.commit()
    result = apply_saved_quality(session, settings, project.id, 3)
    assert not result['applied'] and result['skipped'][0]['code'] == 'quality_apply_downstream_scope'
    assert run.nodes_json == nodes


def test_damaged_source_is_rejected_without_snapshot_rewrite(prepared, settings):
    session, project, _, _, run, _, path, *_ = prepared
    before = copy.deepcopy(run.snapshot_json)
    path.write_bytes(b'explicit damage to isolated synthetic test input')
    result = apply_saved_quality(session, settings, project.id, 3)
    assert not result['applied'] and result['skipped'][0]['code'] == 'studio_video_changed'
    assert run.snapshot_json == before


def test_unverified_target_is_not_downgraded_to_lanczos(prepared, settings, monkeypatch):
    session, project, _, _, run, *_ = prepared
    monkeypatch.setattr(q, 'seedvr_capabilities', lambda *_: {'state': 'not_tested'})
    before = copy.deepcopy(run.snapshot_json)
    result = apply_saved_quality(session, settings, project.id, 3)
    assert not result['applied'] and result['skipped'][0]['code'] == 'quality_method_unverified'
    assert run.snapshot_json == before


def test_only_shots_in_the_frozen_run_scope_can_be_applied(prepared, settings):
    session, project, shot, _, run, *_ = prepared
    other = m.StoryboardShot(project_id=project.id, order=2, aspect_ratio='16:9', scene_title='Out of current run')
    session.add(other); session.flush()
    out_of_scope = copy.deepcopy(run.nodes_json[1])
    out_of_scope.update(id='quality:' + other.id, target_id=other.id, dependencies=[])
    run.nodes_json = [*run.nodes_json, out_of_scope]; session.commit()
    result = apply_saved_quality(session, settings, project.id, 3)
    assert result['applied'] == [shot.id]
    assert result['skipped'] == [{'shot_id': other.id, 'code': 'quality_apply_shot_scope',
                                 'reason': '此镜头不在本轮冻结制作范围，未修改'}]
    assert run.nodes_json[-1] == out_of_scope


def test_profile_changed_during_preflight_is_not_applied(prepared, context, settings, monkeypatch):
    session, project, _, _, run, *_ = prepared
    factory = context[1]
    original_plan = q.plan
    changed = []

    def concurrent_save(*args, **kwargs):
        result = original_plan(*args, **kwargs)
        if not changed:
            with factory() as controller:
                profile = controller.scalar(select(m.QualityProfile).where(
                    m.QualityProfile.scope == 'project', m.QualityProfile.target_id == project.id))
                profile.revision += 1
                profile.settings_json = QualitySettings(processing='lanczos').model_dump()
                controller.commit()
            changed.append(True)
        return result

    monkeypatch.setattr(q, 'plan', concurrent_save)
    before = copy.deepcopy(run.nodes_json)
    with pytest.raises(AppError) as error:
        apply_saved_quality(session, settings, project.id, 3)
    assert error.value.code == 'quality_profile_changed'
    assert run.nodes_json == before


@pytest.mark.parametrize('change', ['pause', 'products', 'child'])
def test_run_changed_during_file_preflight_is_never_overwritten(prepared, context, settings, monkeypatch, change):
    session, project, _, video, run, *_ = prepared
    factory = context[1]
    original_plan = q.plan
    original_raw = apply_saved_quality.__globals__['_raw_video']
    committed = []

    def concurrent_control():
        if committed:
            return
        with factory() as controller:
            latest = controller.get(m.StudioRun, run.id)
            if change == 'pause':
                latest.status = 'paused'
            elif change == 'products':
                latest.snapshot_json = {**latest.snapshot_json, 'product_references_changed': True}
            else:
                accepted = m.QualityRender(source_video_id=video.id, status='running',
                    snapshot_json={'fixture': 'isolated accepted-provider boundary; no real inference'}, result_json={})
                controller.add(accepted); controller.flush()
                nodes = copy.deepcopy(latest.nodes_json)
                nodes[1].update(status='running', child_id=accepted.id, attempts=1)
                latest.nodes_json = nodes
            controller.commit()
            committed.append({'status': latest.status, 'snapshot': copy.deepcopy(latest.snapshot_json),
                              'nodes': copy.deepcopy(latest.nodes_json)})

    def plan_boundary(*args, **kwargs):
        result = original_plan(*args, **kwargs)
        concurrent_control()
        return result

    def source_boundary(*args, **kwargs):
        result = original_raw(*args, **kwargs)
        concurrent_control()
        return result

    if change == 'pause':
        monkeypatch.setattr(q, 'plan', plan_boundary)
    else:
        monkeypatch.setitem(apply_saved_quality.__globals__, '_raw_video', source_boundary)
    with pytest.raises(AppError) as error:
        apply_saved_quality(session, settings, project.id, 3)
    assert error.value.code == 'studio_quality_run_changed'
    session.expire_all()
    latest = session.get(m.StudioRun, run.id)
    assert latest.status == committed[0]['status']
    assert latest.snapshot_json == committed[0]['snapshot']
    assert latest.nodes_json == committed[0]['nodes']
    assert not list(session.scalars(select(m.AuditEvent).where(
        m.AuditEvent.object_id == run.id,
        m.AuditEvent.action == 'explicit_quality_applied_to_undispatched_nodes')))


def test_pause_committed_between_final_read_and_cas_is_not_overwritten(prepared, context, settings, monkeypatch):
    from sqlalchemy.sql.dml import Update
    session, project, _, _, run, *_ = prepared
    factory = context[1]
    original_execute = session.execute
    before_nodes = copy.deepcopy(run.nodes_json)
    injected = []

    def before_cas(statement, *args, **kwargs):
        if isinstance(statement, Update) and statement.table.name == 'studio_runs' and not injected:
            with factory() as controller:
                latest = controller.get(m.StudioRun, run.id)
                latest.status = 'paused'
                controller.commit()
            injected.append(True)
        return original_execute(statement, *args, **kwargs)

    monkeypatch.setattr(session, 'execute', before_cas)
    with pytest.raises(AppError) as error:
        apply_saved_quality(session, settings, project.id, 3)
    assert error.value.code == 'studio_quality_run_changed' and injected == [True]
    session.rollback()
    latest = session.get(m.StudioRun, run.id)
    assert latest.status == 'paused' and latest.nodes_json == before_nodes
    assert not list(session.scalars(select(m.AuditEvent).where(
        m.AuditEvent.object_id == run.id,
        m.AuditEvent.action == 'explicit_quality_applied_to_undispatched_nodes')))
