"""Database-only dispatch regression tests. These do not run model inference."""
import hashlib
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image
from sqlalchemy import select
from starlette.requests import Request

from app import models as m
from app.core.db import Base, build_engine, build_session_factory
from app.core.errors import AppError
from app.schemas_studio import PipelineStart
from app.services.ai_services import seed_local_services
from app.services.context_production_inputs import collect_context_inputs, restore_context_inputs
from app.services.studio_pipeline import create_pipeline, generation_dimensions


def add_shot(session, name):
    project = m.Project(name=name)
    session.add(project); session.flush()
    session.add(m.ScriptRevision(project_id=project.id, revision=1, raw_text='Ocean waves.', status='approved'))
    shot = m.StoryboardShot(project_id=project.id, order=1, visual_prompt='Early morning ocean waves.', aspect_ratio='16:9', status='approved')
    session.add(shot); session.flush()
    return project, shot


@pytest.fixture
def session(settings):
    settings.h3_demo_mode = False
    engine=build_engine(settings.database_url)
    Base.metadata.create_all(engine)
    try:
        with build_session_factory(engine)() as session:
            seed_local_services(session,settings)
            # Context-specific tests opt in explicitly on every platform; the
            # old implicit loopback mapping is Windows-only compatibility.
            service = session.scalar(select(m.AIService).where(m.AIService.module == 'video_generation'))
            service.settings_json = {**service.settings_json, 'workflow_profile': 'context_ir', 'h3_context_ir': True}
            session.commit()
            yield session
    finally:
        engine.dispose()


def test_context_run_freezes_text_and_mode_without_intermediate_image(session, settings):
        project, shot = add_shot(session, 'Isolated Context-IR text input')
        session.add(m.QualityProfile(scope='project', target_id=project.id, settings_json={'mode':'standard'}))
        session.add(m.QualityProfile(scope='shot', target_id=shot.id, settings_json={'mode':'fast'}))
        session.flush()
        run = create_pipeline(session, settings, project.id,
            {'shot_ids':[shot.id], 'prompt_overrides':{shot.id:'Waves wash over smooth stones.'}})
        selected = run.snapshot_json['h3_context_by_shot'][shot.id]
        assert selected['service']['h3_workflow']['actual_steps'] == 4
        assert selected['service']['h3_workflow']['quality_mode'] == 'fast'
        assert selected['inputs']['references'] == [] and selected['inputs']['motion_references'] == []
        assert selected['inputs']['prompt'].startswith('Waves wash over smooth stones.')
        assert not any(n['kind'] in {'scene','keyframe','character'} for n in run.nodes_json)
        assert next(n for n in run.nodes_json if n['kind']=='video')['dependencies'] == []
        assert restore_context_inputs(session, settings, shot, selected['inputs']) == ([], [])


def test_explicit_native_canvas_is_frozen_and_local_text_has_no_app_character_cap(session, settings):
    project, shot = add_shot(session, 'Explicit local native canvas')
    long_prompt = 'A calm ocean. ' * 2500
    options = PipelineStart(width=672, height=384, shot_ids=[shot.id], prompt_overrides={shot.id: long_prompt})
    run = create_pipeline(session, settings, project.id, options)
    session.commit()
    assert generation_dimensions(run, shot) == (672, 384)
    assert run.snapshot_json['h3_context_by_shot'][shot.id]['inputs']['prompt'].startswith(long_prompt.strip())
    with pytest.raises(AppError, match='锁定生成尺寸'):
        create_pipeline(session, settings, project.id, PipelineStart(width=1024, height=576, shot_ids=[shot.id]))


def test_explicit_images_are_scoped_and_frozen(session, settings):
        project, shot = add_shot(session, 'Current project')
        foreign, other = add_shot(session, 'Other project')
        output=BytesIO(); Image.new('RGB',(32,32),'red').save(output,'PNG')
        data=output.getvalue(); settings.media_root.mkdir(parents=True,exist_ok=True)
        image=settings.media_root/'input.png'; image.write_bytes(data)
        pictures = [m.ProductionVisual(project_id=owner.id, shot_id=owner_shot.id, kind='keyframe',
            status='approved', relative_path=image.name, sha256=hashlib.sha256(data).hexdigest(), prompt='',
            snapshot_json={'source':'user_upload'}) for owner,owner_shot in ((project,shot),(foreign,other))]
        session.add_all(pictures); session.flush()
        with pytest.raises(AppError) as error:
            collect_context_inputs(session, settings, shot, image_ids=[pictures[1].id])
        assert error.value.details['findings'][0]['code']=='visual_not_approved'
        snapshot = collect_context_inputs(session, settings, shot, image_ids=[pictures[0].id])
        assert [ref['asset_id'] for ref in restore_context_inputs(session, settings, shot, snapshot)[0]] == [pictures[0].id]
        assert collect_context_inputs(session,settings,shot,image_ids=[])['references'] == []
        image.write_bytes(b'changed')
        with pytest.raises(AppError) as error:
            restore_context_inputs(session, settings, shot, snapshot)
        assert error.value.code=='context_input_snapshot_stale'


@pytest.fixture
def imported_props(session, settings):
    project, shot = add_shot(session, 'Imported multi-product shot')
    batch = m.WordImport(filename='script.docx', relative_path='script.docx', sha256='a' * 64)
    product = m.Product(name='Bottles')
    session.add_all([batch, product]); session.flush()
    settings.media_root.mkdir(parents=True, exist_ok=True)
    assets = []
    for color in ('red', 'pink', 'green'):
        path = settings.media_root / f'{color}.png'
        Image.new('RGB', (32, 32), color).save(path)
        asset = m.ScreenplayInputAsset(input_mode='image', filename=path.name, relative_path=path.name,
                                      sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        assets.append(asset)
    session.add_all(assets); session.flush()
    blanket, pink, green = assets
    image = m.ProductImage(product_id=product.id, asset_id=blanket.id)
    session.add(image); session.flush()
    frozen = [{key: getattr(asset, key) for key in ('id', 'sha256', 'relative_path')} for asset in assets]
    job = m.ScreenplayGenerationJob(project_id=project.id, word_import_id=batch.id, idempotency_key='multi-props',
        request_sha256='b' * 64, project_name=project.name, input_mode='image', creative_prompt='Bottles',
        snapshot_json={'input_assets': frozen})
    session.add_all([
        job,
        m.ProjectProductBinding(project_id=project.id, product_id=product.id, snapshot_json={'name': product.name,
            'images': [{**frozen[0], 'id': image.id, 'purpose': 'main', 'order': 1}]}),
        m.ShotProductBinding(project_id=project.id, shot_id=shot.id, product_id=product.id, image_ids_json=[image.id],
            state_json={'source': 'project_product_preparation', 'reference_selection_confirmed': True}),
    ])
    props = [m.ProductionEntity(project_id=project.id, logical_id=logical_id, kind='prop', name=name,
              status='approved', spec_json={'source_image_id': asset.id}, source_json={'import_id': batch.id})
             for logical_id, name, asset in (('P_PINK', 'Pink bow bottle', pink), ('P_GREEN', 'Green teddy bottle', green))]
    session.add_all(props); session.flush()
    states = [{'entity_id': entity.id, 'prop_key': entity.logical_id, 'holder': holder, 'state': 'closed'}
              for entity, holder in zip(props, ('right hand', 'left hand'))]
    detail = m.ShotDetail(shot_id=shot.id, source_json={'import_id': batch.id, 'production': {'prop_states': states}})
    session.add(detail); session.flush()
    return SimpleNamespace(shot=shot, job=job, detail=detail, image=image, assets=assets, props=props)


def test_context_resolves_explicit_imported_prop_sources_and_freezes_holder_state(session, settings, imported_props):
    data = imported_props
    snapshot = collect_context_inputs(session, settings, data.shot)
    refs = snapshot['references']
    assert [ref['asset_id'] for ref in refs] == [asset.id for asset in data.assets[1:]]
    assert [ref['product_id'] for ref in refs] == [prop.id for prop in data.props]
    assert all(ref['source'] == 'screenplay_prop_reference' for ref in refs)
    assert 'Pink bow bottle", "图片": "<Picture 1>' in snapshot['prompt']
    assert 'Green teddy bottle", "holder": "left hand", "state": "closed"' in snapshot['prompt']
    restored, _ = restore_context_inputs(session, settings, data.shot, snapshot)
    assert [ref['asset_id'] for ref in restored] == [asset.id for asset in data.assets[1:]]

    data.job.snapshot_json = {'input_assets': data.job.snapshot_json['input_assets'][:-1]}
    session.flush()
    with pytest.raises(AppError) as error:
        restore_context_inputs(session, settings, data.shot, snapshot)
    assert error.value.code == 'context_input_snapshot_stale'
    assert [ref['asset_id'] for ref in collect_context_inputs(session, settings, data.shot)['references']] == [data.image.id]


def test_context_prefers_manual_shot_selection_over_imported_prop_sources(session, settings, imported_props):
    data = imported_props
    session.add(m.AuditEvent(object_type='storyboard_shot', object_id=data.shot.id, action='product_reference_selection_saved'))
    session.flush()
    snapshot = collect_context_inputs(session, settings, data.shot)
    assert [ref['asset_id'] for ref in snapshot['references']] == [data.image.id]


def test_failed_job_retry_keeps_frozen_route_when_project_mode_changes(session, settings, monkeypatch):
    from app.api.routes import generate_for_shot, retry_job
    from app.schemas import GenerationRequest
    from app.services.h3_context_provider import ContextIRH3Provider

    project, shot = add_shot(session,'Frozen failure retry')
    profile=m.QualityProfile(scope='project',target_id=project.id,settings_json={'mode':'fast'})
    session.add(profile);session.commit()
    request=Request({'type':'http','app':SimpleNamespace(state=SimpleNamespace(settings=settings))})
    calls=[]
    async def reject(self, item):
        calls.append((item.seed,item.prompt,item.parameters['h3_workflow']))
        raise AppError(422,'isolated_known_rejection','Isolated test failure before any ComfyUI submission')
    monkeypatch.setattr(ContextIRH3Provider,'submit',reject)
    def finish_without_io(coroutine):
        try:
            return coroutine.send(None)
        finally:
            coroutine.close()

    with pytest.raises(AppError,match='Isolated test failure'):
        finish_without_io(generate_for_shot(request,session,shot,GenerationRequest(seed=17,prompt_override='A calm ocean.')))
    job=session.scalar(select(m.GenerationJob).where(m.GenerationJob.shot_id==shot.id))
    assert job.status=='failed' and job.parameters_json['h3_workflow']['actual_steps']==4
    profile.settings_json={'mode':'standard'};session.commit()
    with pytest.raises(AppError,match='Isolated test failure'):
        finish_without_io(retry_job(request,job.id,session))
    assert len(calls)==2 and calls[0]==calls[1]


def test_context_revision_requires_explicit_migration_and_shares_preview_inputs(session, settings, monkeypatch):
    from app.api.diagnostics import revision_preview
    from app.api.routes import revise_video
    from app.schemas import RevisionRequest
    from app.services.h3_context_provider import ContextIRH3Provider, context_compiler_intent

    project, shot = add_shot(session, 'Historical cloud source without a character')
    cloud = m.AIService(module='video_generation', name='Historical cloud provider', provider='video_api',
                        default_model='cloud-old-model', base_url='https://example.invalid')
    session.add(cloud); session.flush()
    old = m.GenerationJob(project_id=project.id, shot_id=shot.id, provider='cloud_video_jobs_v1',
                          prompt='Historical prompt', status='generated', seed=743,
                          parameters_json={'width':1920, 'height':1080, 'steps':60,
                              'service_snapshot':{'service_id':cloud.id, 'model_id':cloud.default_model}})
    session.add(old); session.flush()
    video = m.VideoRevision(project_id=project.id, shot_id=shot.id, generation_job_id=old.id,
                            relative_path='historical.mp4', revision=1, review_status='approved')
    session.add_all([video, m.QualityProfile(scope='shot', target_id=shot.id, settings_json={'mode':'fast'})]); session.commit()
    request = Request({'type':'http', 'app':SimpleNamespace(state=SimpleNamespace(settings=settings))})
    payload = RevisionRequest(instruction='Hold the camera still.', prompt_override='Waves over smooth stones.')
    with pytest.raises(AppError) as rejected:
        revision_preview(video.id, payload, request, session)
    assert rejected.value.code == 'context_ir_service_required'


    payload.use_project_video_service = True
    preview = revision_preview(video.id, payload, request, session)
    assert 'Hold the camera still.' in preview['prompt']
    assert preview['character_id'] is None and preview['asset_ids'] == []
    assert preview['h3_workflow']['actual_steps'] == 4
    calls = []

    async def reject(self, item):
        calls.append(item)
        raise AppError(422, 'isolated_known_rejection', 'Stopped before any provider I/O')

    monkeypatch.setattr(ContextIRH3Provider, 'submit', reject)
    coroutine = revise_video(request, video.id, payload, session)
    with pytest.raises(AppError, match='Stopped before any provider I/O'):
        try:
            coroutine.send(None)
        finally:
            coroutine.close()
    assert len(calls) == 1
    submitted = calls[0]
    assert (submitted.seed, submitted.parameters['width'], submitted.parameters['height']) == (743, 1024, 576)
    assert submitted.parameters['steps'] == 4 and submitted.parameters['h3_workflow'] == preview['h3_workflow']
    assert submitted.reference_assets == []
    assert context_compiler_intent(submitted.prompt, submitted.negative_prompt, [],
                                  silent=submitted.parameters['context_silent']) == preview['prompt']
    assert old.status == 'generated' and old.parameters_json['width'] == 1920

    session.add(m.ProjectServiceBinding(project_id=project.id, module='video_generation', service_id=cloud.id))
    session.commit()
    with pytest.raises(AppError) as rejected:
        revision_preview(video.id, payload, request, session)
    assert rejected.value.code == 'context_ir_service_required'


def test_context_routing_evidence_matches_only_its_service_recipe_and_model(session, settings):
    from app.services.task_routing import candidate
    from app.services.video_services import select_video_service

    project, shot = add_shot(session, 'Candidate evidence contract')
    service, snapshot, _ = select_video_service(session, settings, project.id, quality_mode='fast')
    workflow = snapshot['h3_workflow']
    settings.media_root.mkdir(parents=True, exist_ok=True)
    media = settings.media_root/'candidate.bin'
    media.write_bytes(b'isolated candidate metadata test, no media inference')
    sha = hashlib.sha256(media.read_bytes()).hexdigest()
    digest = 'a'*64
    receipt = {'workflow':workflow, 'compiler_receipt':'fixture', 'smd_sampling_receipt':'fixture', 'compiled_prompt_sha256':digest}
    job = m.GenerationJob(project_id=project.id, shot_id=shot.id, provider='comfyui_h3',
        provider_job_id='fixture-comfy-id', status='generated', workflow_version=workflow['workflow_id'], prompt='fixture',
        parameters_json={'service_snapshot':snapshot, 'h3_workflow':workflow, 'steps':4,
                         'context_ir_execution':receipt, 'actual_workflow_sha256':digest})
    session.add(job); session.flush()
    video = m.VideoRevision(project_id=project.id, shot_id=shot.id, generation_job_id=job.id, revision=1,
        relative_path=media.name, metadata_json={'playable_video':True, 'sha256':sha, 'h3_workflow':workflow,
            'context_ir_execution':receipt, 'actual_workflow_sha256':digest, 'comfyui_prompt_id':job.provider_job_id})
    session.add(video); session.flush()
    session.add(m.QualityCheck(video_revision_id=video.id, check_type='media_decode', status='passed')); session.flush()

    service.settings_json = {**service.settings_json, 'workflow_path':str(media.parent/'missing-legacy-workflow.json')}
    evidence = candidate(session, settings, service, 'video_generation')
    assert evidence.eligible and evidence.evidence['kind'] == 'decoded_context_video'
    assert evidence.evidence['actual_steps'] == 4 and evidence.evidence['workflow_id'] == workflow['workflow_id']
    assert snapshot['requested_model_id'] != snapshot['model_id']
    assert not candidate(session, settings, service, 'video_generation', snapshot['model_id']).eligible

    # Even if an altered Context job happens to match the old graph checksum
    # and alias, it cannot attest legacy workflow output.
    legacy_workflow = media.parent/'legacy-workflow.json'
    legacy_workflow.write_bytes(b'legacy graph fixture')
    service.settings_json = {**service.settings_json, 'workflow_path':str(legacy_workflow)}
    job.parameters_json = {**job.parameters_json, 'service_snapshot':{**snapshot, 'model_id':service.default_model,
        'workflow_sha256':hashlib.sha256(legacy_workflow.read_bytes()).hexdigest()}}
    assert not candidate(session, settings, service, 'video_generation').eligible
