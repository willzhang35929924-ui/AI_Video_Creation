"""Product-only compiler/API regressions; provider is an explicit contract stub."""
import copy
import hashlib
from types import SimpleNamespace

from PIL import Image
import pytest
from sqlalchemy import func, select
from starlette.requests import Request

from app import models as m
from app.api import routes
from app.core.db import Base, build_engine, build_session_factory
from app.core.errors import AppError
from app.models.product_library import Product, ProductImage, ProjectProductBinding, ShotProductBinding
from app.schemas import GenerationRequest
from app.services.cinematic_quality import generation_inputs_hash, inspect_ir, shot_preflight, visual_inputs
from app.services.cinematic_rules import fingerprint, rules_bundle
from app.services.providers import JobAccepted
from app.services.screenplay import generation_inputs, uses_structured_screenplay


def run_contract(coroutine):
    """Drive the in-process contract without creating a socket/event loop."""
    try:
        coroutine.send(None)
    except StopIteration as completed:
        return completed.value
    finally:
        coroutine.close()
    raise AssertionError('This contract must finish before any external/asynchronous I/O')


class ContractProvider:
    name = 'explicit_contract_stub'

    def __init__(self):
        self.requests = []

    async def submit(self, request):
        self.requests.append(request)
        return JobAccepted('contract-only-no-inference', 'queued')


@pytest.fixture()
def product_shot(settings):
    settings.h3_demo_mode = False
    engine = build_engine(settings.database_url)
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    with factory() as session:
        project = m.Project(name='Product-only isolated compiler fixture')
        session.add(project)
        session.flush()
        rules = rules_bundle()
        script = m.ScriptRevision(project_id=project.id, revision=1, raw_text='Fixture product presentation, no performers', status='approved',
            parsed_json={'versions': rules['versions'], 'rules_snapshot': rules})
        session.add(script)
        session.flush()
        scene = m.Scene(project_id=project.id, script_revision_id=script.id, order=1, name='Studio tabletop',
                        environment_json={'location': 'plain tabletop', 'lighting': 'soft practical light'})
        session.add(scene)
        session.flush()
        shot = m.StoryboardShot(project_id=project.id, order=1, scene_title='Product presentation', status='approved',
            aspect_ratio='16:9', duration_seconds=4, visual_prompt='The supplied product remains visible on a tabletop.',
            action='Product rotates slowly.', camera_move='Fixed camera')
        session.add(shot)
        session.flush()
        detail = m.ShotDetail(shot_id=shot.id, scene_id=scene.id, photography_json={'framing': 'product close-up'}, source_json={'production': {}})
        product = Product(name='Isolated product fixture')
        session.add_all([detail, product])
        session.flush()
        settings.media_root.mkdir(parents=True, exist_ok=True)
        path = settings.media_root / 'explicit_product_fixture.png'
        Image.new('RGB', (64, 64), (20, 100, 80)).save(path)
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        source = m.ScreenplayInputAsset(input_mode='image', filename=path.name, relative_path=path.name, sha256=sha,
                                       metadata_json={'width': 64, 'height': 64, 'bytes': path.stat().st_size})
        session.add(source)
        session.flush()
        picture = ProductImage(product_id=product.id, asset_id=source.id, purpose='main', order=1)
        session.add(picture)
        session.flush()
        project_binding = ProjectProductBinding(project_id=project.id, product_id=product.id,
            snapshot_json={'id': product.id, 'name': product.name, 'images': [{'id': picture.id, 'asset_id': source.id,
                'relative_path': path.name, 'sha256': sha, 'purpose': 'main', 'order': 1}]})
        binding = ShotProductBinding(project_id=project.id, shot_id=shot.id, product_id=product.id,
            image_ids_json=[picture.id], state_json={'visible_state': 'fully visible', 'presentation': 'rotating on tabletop'})
        session.add_all([project_binding, binding])
        session.flush()
        keyframe = m.ProductionVisual(project_id=project.id, shot_id=shot.id, kind='keyframe', status='approved', prompt='Explicit fixture input',
            relative_path=path.name, sha256=sha, snapshot_json={'inputs_hash': fingerprint(visual_inputs(session, shot))})
        session.add(keyframe)
        session.flush()
        detail.source_json = {'production': {'visual_ids': [keyframe.id]}}
        session.add(m.AudioEvent(shot_id=shot.id, kind='voice', description='Optional narration with speaker unspecified; no character identity asserted'))
        session.commit()
        yield SimpleNamespace(session=session, settings=settings, project=project, script=script, scene=scene,
            shot=shot, detail=detail, product=product, binding=binding, project_binding=project_binding,
            picture=picture, source=source, path=path, keyframe=keyframe)
    engine.dispose()


def test_product_only_compiles_real_refs_without_fabricating_a_character(product_shot):
    p = product_shot
    assert uses_structured_screenplay(p.session, p.shot)
    version, refs, prompt, snapshot = generation_inputs(p.session, p.shot, p.settings)
    assert version is None
    assert [r['role'] for r in refs] == ['product', 'composition']
    assert refs[0]['asset_id'] == p.picture.id and refs[0]['product_id'] == p.product.id
    assert [r['picture_index'] for r in refs] == [1, 2]
    assert snapshot['reference_manifest']['actors'] == [] and snapshot['reference_manifest']['speakers'] == []
    assert snapshot['reference_manifest']['references'][0]['sha256'] == p.source.sha256
    assert snapshot['product_appearance_review']['exact_logo_text_verified'] is False
    assert snapshot['reference_manifest']['sha256'] == fingerprint({k: v for k, v in snapshot['reference_manifest'].items() if k != 'sha256'})
    assert 'natural adult skin' not in prompt and 'pores and age cues' not in prompt and 'Costume state' not in prompt
    assert 'product photography' in prompt and 'logo reproduction require visual review' in prompt
    assert 'Optional narration' in prompt
    assert not p.session.scalar(select(func.count()).select_from(m.Character))
    preflight = shot_preflight(p.session, p.settings, p.shot)
    assert preflight['ready'], preflight['findings']
    assert preflight['reference_count'] == 2


def test_product_only_keyframe_prompt_keeps_product_identity_without_performer_language(product_shot, monkeypatch):
    from app.schemas_cinematic import VisualInput
    from app.services import cinematic_visuals
    from app.services.ai_services import service_snapshot

    p = product_shot
    service = m.AIService(module='character_generation', provider='comfyui', name='Contract only; no image execution',
        base_url='http://127.0.0.1:9', default_model='flux-2-klein-4b-fp8.safetensors',
        settings_json={'text_encoder': 'qwen_3_4b.safetensors', 'vae': 'flux2-vae.safetensors'})
    p.session.add(service)
    p.session.flush()
    resolution_calls = []

    def contract_resolution(session, module, service_id=None, project_id=None, model_id=None):
        assert session is p.session and module == 'character_generation'
        assert service_id == service.id and project_id == p.project.id and model_id is None
        resolution_calls.append(service_id)
        return service, service_snapshot(service, model_id)

    async def forbidden_transport(*args, **kwargs):
        raise AssertionError('Prompt compilation must not submit an external or GPU request')

    monkeypatch.setattr(cinematic_visuals, 'resolve_service', contract_resolution)
    monkeypatch.setattr(cinematic_visuals, 'service_request', forbidden_transport)
    original_bytes = p.path.read_bytes()
    visual = cinematic_visuals.build_visual(p.session, p.settings, p.project.id,
        VisualInput(kind='keyframe', shot_id=p.shot.id, service_id=service.id))
    assert resolution_calls == [service.id]
    assert visual.status == 'queued' and visual.relative_path is None
    assert visual.snapshot_json['product_only'] is True
    assert visual.snapshot_json['reference_manifest']['actors'] == []
    assert [reference['role'] for reference in visual.snapshot_json['references']] == ['product']
    reference = visual.snapshot_json['references'][0]
    assert reference['asset_id'] == p.picture.id and reference['product_id'] == p.product.id
    assert reference['sha256'] == p.source.sha256
    assert visual.snapshot_json['product_reference_selection']['actual'] == [p.picture.id]
    assert 'PRODUCT-ONLY' in visual.prompt
    assert 'Absolutely no people, faces, hands' in visual.prompt
    assert f'Reference image 1 defines only product {p.product.id} ({p.product.name})' in visual.prompt
    assert 'Preserve its shape, proportions, color and packaging.' in visual.prompt
    assert 'A believable adult' not in visual.prompt
    assert 'grounded body weight' not in visual.prompt
    assert 'Show only the explicitly referenced performers' not in visual.prompt
    language = visual.snapshot_json['rules_snapshot']['visual_language']
    assert all(language[key] in visual.prompt for key in ('scene', 'camera'))
    assert all(language[key] not in visual.prompt for key in ('identity', 'wardrobe', 'action'))
    assert p.path.read_bytes() == original_bytes
    assert not p.session.scalar(select(func.count()).select_from(m.Character))


def test_product_state_or_selected_image_changes_invalidate_visual_inputs(product_shot):
    p = product_shot
    before = generation_inputs_hash(p.session, p.shot)
    p.binding.state_json = {'visible_state': 'packaging open'}
    p.session.flush()
    assert generation_inputs_hash(p.session, p.shot) != before
    assert any(f['code'] == 'keyframe_stale' for f in shot_preflight(p.session, p.settings, p.shot)['findings'])
    p.binding.image_ids_json = []
    p.session.flush()
    with pytest.raises(AppError, match='有效的产品'):
        generation_inputs(p.session, p.shot, p.settings)
    assert any(f['code'] == 'product_reference_missing' for f in shot_preflight(p.session, p.settings, p.shot, False)['findings'])


def test_missing_product_ref_and_missing_participants_remain_blocked(product_shot):
    p = product_shot
    p.session.delete(p.binding)
    p.session.flush()
    assert not uses_structured_screenplay(p.session, p.shot)
    with pytest.raises(AppError) as error:
        generation_inputs(p.session, p.shot, p.settings)
    assert error.value.code == 'participants_required'
    assert any(f['code'] == 'cast_not_ready' for f in shot_preflight(p.session, p.settings, p.shot, False)['findings'])
    ir = {'characters': [], 'scenes': [], 'shots': [{'key': 'plain', 'participants': [], 'duration_seconds': 4}]}
    assert any(f['code'] == 'cast_unknown' for f in inspect_ir(ir)['findings'])
    assert 'products' not in visual_inputs(p.session, p.shot)


def test_changed_product_bytes_are_rejected_before_model_dispatch(product_shot):
    p = product_shot
    Image.new('RGB', (64, 64), 'red').save(p.path)
    with pytest.raises(AppError) as error:
        generation_inputs(p.session, p.shot, p.settings)
    assert error.value.code == 'product_source_changed'
    assert not shot_preflight(p.session, p.settings, p.shot, False)['ready']


def test_product_binding_does_not_bypass_a_declared_performers_identity_review(product_shot):
    p = product_shot
    character = m.Character(slug='explicit-unapproved-fixture', display_name='Explicit test performer')
    p.session.add(character)
    p.session.flush()
    version = m.CharacterVersion(character_id=character.id, version=1, approval_state='draft', identity_json={})
    p.session.add(version)
    p.session.flush()
    p.session.add(m.ShotCharacter(shot_id=p.shot.id, character_id=character.id, character_version_id=version.id,
                                 reference_asset_ids_json=[], order=1))
    p.session.flush()
    with pytest.raises(AppError) as error:
        generation_inputs(p.session, p.shot, p.settings)
    assert error.value.code == 'participant_identity_unapproved'
    assert any(f['code'] == 'identity_not_ready' for f in shot_preflight(p.session, p.settings, p.shot, False)['findings'])


def test_product_reference_budget_is_the_actual_h3_budget(product_shot):
    p = product_shot
    snapshot = copy.deepcopy(p.project_binding.snapshot_json)
    base = snapshot['images'][0]
    snapshot['images'] = [{**base, 'id': 'fixture-image-' + str(i), 'order': i} for i in range(10)]
    p.project_binding.snapshot_json = snapshot
    p.binding.image_ids_json = [i['id'] for i in snapshot['images']]
    p.session.flush()
    with pytest.raises(AppError) as error:
        generation_inputs(p.session, p.shot, p.settings)
    assert error.value.code == 'reference_limit'
    assert any(f['code'] == 'reference_limit' for f in shot_preflight(p.session, p.settings, p.shot, False)['findings'])


@pytest.mark.parametrize('override', [None, '本地完整输入：' + '景' * 20000], ids=['compiled', 'edited-long'])
def test_generation_entry_persists_null_character_version_and_product_manifest(product_shot, monkeypatch, override):
    from app.services import video_services
    p = product_shot
    provider = ContractProvider()
    requests = provider.requests
    monkeypatch.setattr(routes, 'provider_for', lambda settings: provider)
    monkeypatch.setattr(video_services, 'select_video_service', lambda *args: (None,
        {'settings': {}, 'provider': 'comfyui', 'is_cloud': False}, provider))
    request = Request({'type': 'http', 'app': SimpleNamespace(state=SimpleNamespace(settings=p.settings))})
    job, result = run_contract(routes.generate_for_shot(request, p.session, p.shot,
        GenerationRequest(parameters={'width': 1024, 'height': 576}, prompt_override=override), legacy_production=True))
    assert result is None and job.status == 'queued'
    if override is not None:
        assert requests[0].prompt == override == job.prompt
        assert job.parameters_json['prompt_override'] == override
        assert job.parameters_json['compiled_prompt'] != override
    assert job.parameters_json['character_version_id'] is None
    assert job.reference_assets_json[0]['role'] == 'product'
    assert requests[0].reference_assets[0]['asset_id'] == p.picture.id
    assert requests[0].parameters['reference_manifest']['actors'] == []
    assert not p.session.scalar(select(func.count()).select_from(m.Character))
    shots = routes.list_shots(p.project.id, request, session=p.session)
    assert shots[0]['generation_ready'] is True and shots[0]['participants'] == []


@pytest.mark.parametrize('kind', ['cloud', 'native'])
def test_direct_generation_respects_selected_legacy_profile_without_context_quality(product_shot, monkeypatch, kind):
    from app.services import ai_services, context_production_inputs, video_services
    p = product_shot
    provider = ContractProvider()
    service = m.AIService(module='video_generation', name='Isolated direct ' + kind,
        provider='video_api' if kind == 'cloud' else 'comfyui',
        base_url='https://example.invalid/v1' if kind == 'cloud' else 'http://127.0.0.1:8189',
        default_model='contract-cloud-video' if kind == 'cloud' else 'minimax_h3_ref2va_pruned_w6a8.safetensors',
        settings_json=({'video_contract': 'video-jobs-v1'} if kind == 'cloud' else
                       {'workflow_profile': 'native_h3_r2v', 'h3_context_ir': False}))
    p.session.add(service); p.session.commit()
    selected = []
    resolved = []
    real_resolve = ai_services.resolve_service

    def resolve_profile(session, module, service_id=None, project_id=None, model_id=None):
        resolved.append((module, service_id, project_id, model_id))
        return real_resolve(session, module, service_id, project_id, model_id)

    def select_contract(session, settings, project_id, service_id=None, model_id=None, **kwargs):
        assert session is p.session and settings is p.settings and project_id == p.project.id
        assert service_id == service.id and model_id == service.default_model
        selected.append(kwargs)
        return service, ai_services.service_snapshot(service, model_id), provider

    def forbidden_context(*_args, **_kwargs):
        pytest.fail('A cloud or explicit ordinary native profile must not enter Context-IR quality/input compilation')

    monkeypatch.setattr(ai_services, 'resolve_service', resolve_profile)
    monkeypatch.setattr(video_services, 'select_video_service', select_contract)
    monkeypatch.setattr(routes, 'provider_for', lambda _settings: provider)
    monkeypatch.setattr(context_production_inputs, 'quality_for_shot', forbidden_context)
    monkeypatch.setattr(context_production_inputs, 'collect_context_inputs', forbidden_context)
    monkeypatch.setattr(context_production_inputs, 'restore_context_inputs', forbidden_context)
    complete_prompt = ('Preserve the supplied product and its packaging. ' * 450).strip()
    original_bytes = p.path.read_bytes()
    request = Request({'type': 'http', 'app': SimpleNamespace(state=SimpleNamespace(settings=p.settings))})
    # Deliberately omit legacy_production: direct API selection must dispatch
    # by the saved profile, not depend on an internal orchestration override.
    job, result = run_contract(routes.generate_for_shot(request, p.session, p.shot,
        GenerationRequest(service_id=service.id, model_id=service.default_model, prompt_override=complete_prompt,
                          parameters={'width': 1024, 'height': 576})))
    assert result is None and job.status == 'queued'
    assert resolved == [('video_generation', service.id, p.project.id, service.default_model)]
    assert selected == [{}] and len(provider.requests) == 1
    submitted = provider.requests[0]
    assert len(complete_prompt) > 7000
    assert submitted.prompt == job.prompt == complete_prompt
    assert job.parameters_json['prompt_override'] == complete_prompt
    assert job.parameters_json['compiled_prompt'] != complete_prompt
    assert not {'h3_workflow', 'context_inputs', 'quality_request'} & job.parameters_json.keys()
    assert job.parameters_json['service_snapshot']['service_id'] == service.id
    assert job.parameters_json['service_snapshot']['settings'] == service.settings_json
    assert submitted.parameters['width'] == 1024 and submitted.parameters['height'] == 576
    assert submitted.parameters['reference_manifest']['actors'] == []
    assert job.parameters_json['character_version_id'] is None
    assert submitted.reference_assets[0]['asset_id'] == p.picture.id
    assert job.reference_assets_json[0]['role'] == 'product'
    assert p.path.read_bytes() == original_bytes
    assert not p.session.scalar(select(func.count()).select_from(m.Character))
