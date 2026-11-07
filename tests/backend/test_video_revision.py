"""Revision HTTP/input contracts using isolated rows; no model inference evidence."""
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import models as m
from app.schemas import RevisionRequest
from app.services.video_revisions import revision_generation_request


@pytest.fixture()
def revision_source(client, settings):
    with client.app.state.session_factory() as session:
        project = m.Project(name='文字修订输入回归测试')
        session.add(project); session.flush()
        script = m.ScriptRevision(project_id=project.id, revision=1, status='approved', raw_text='成年值班员核对记录。')
        person = m.Character(slug=project.id, display_name='测试值班员')
        session.add_all([script, person]); session.flush()
        version = m.CharacterVersion(character_id=person.id, version=1, approval_state='approved')
        session.add(version); session.flush()
        shot = m.StoryboardShot(project_id=project.id, order=1, status='approved', aspect_ratio='16:9',
                                scene_title='核对记录', action='记录本保持合上')
        session.add(shot); session.flush()
        job = m.GenerationJob(project_id=project.id, shot_id=shot.id, provider='comfyui_h3', status='generated',
                              prompt='A fully clothed adult checks a closed notebook.', seed=123,
                              parameters_json={'width':1024, 'height':576, 'steps':30,
                                  'service_snapshot':{'service_id':'source-service', 'model_id':'source-model'},
                                  'studio_run_id':'old-run', 'studio_node_id':'old-node',
                                  'screenplay_snapshot':{'do_not_copy':'old screenplay'},
                                  'reference_manifest':{'do_not_copy':'old reference slots'},
                                  'output_relative_dir':'old/output'})
        session.add(job); session.flush()
        # A derived 4K version still points to the original native GenerationJob.
        # A revision must not feed its postprocessing pixels back to H3.
        path = settings.media_root/'original-version.bin'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'isolated immutable media bytes, not an actual video')
        video = m.VideoRevision(project_id=project.id, shot_id=shot.id, generation_job_id=job.id,
                                relative_path=path.name, revision=2, review_status='approved',
                                metadata_json={'width':3840, 'height':2160, 'quality_render_id':'old-quality-render'})
        session.add(video); session.commit()
        return SimpleNamespace(project_id=project.id, shot_id=shot.id, video_id=video.id,
                               job_id=job.id, version_id=version.id, character_id=person.id, path=path)


def test_text_only_revision_uses_native_job_inputs_not_upscaled_video(client, revision_source):
    source = revision_source
    with client.app.state.session_factory() as session:
        shot, video = session.get(m.StoryboardShot, source.shot_id), session.get(m.VideoRevision, source.video_id)
        request = revision_generation_request(session, shot, video, RevisionRequest(instruction='镜头移动更缓慢。'))
        assert request.parameters == {'width':1024, 'height':576, 'steps':30}
        assert (request.service_id, request.model_id, request.seed) == ('source-service', 'source-model', 123)
        assert video.metadata_json['width'] == 3840


@pytest.mark.parametrize('aspect, dimensions', [('16:9',(1024,576)), ('9:16',(576,1024)), ('1:1',(768,768))])
@pytest.mark.parametrize('old_dimensions', [{}, {'width':640, 'height':384}, {'width':None, 'height':None}])
def test_old_missing_or_incompatible_dimensions_resolve_current_shot_frame(client, revision_source, aspect, dimensions, old_dimensions):
    source = revision_source
    with client.app.state.session_factory() as session:
        shot, video = session.get(m.StoryboardShot, source.shot_id), session.get(m.VideoRevision, source.video_id)
        shot.aspect_ratio = aspect
        session.get(m.GenerationJob, source.job_id).parameters_json = dict(old_dimensions)
        result = revision_generation_request(session, shot, video, RevisionRequest(instruction='动作放慢。'))
        assert (result.parameters['width'], result.parameters['height']) == dimensions


def test_explicit_revision_settings_win_and_new_service_does_not_inherit_unrelated_model(client, revision_source):
    source = revision_source
    with client.app.state.session_factory() as session:
        shot, video = session.get(m.StoryboardShot, source.shot_id), session.get(m.VideoRevision, source.video_id)
        result = revision_generation_request(session, shot, video, RevisionRequest(instruction='动作放慢。',
            seed=456, service_id='new-service', model_id='new-model', parameters={'width':1152,'height':640,'steps':12}))
        assert result.parameters == {'width':1152,'height':640,'steps':12}
        assert (result.service_id, result.model_id, result.seed) == ('new-service','new-model',456)
        switched = revision_generation_request(session, shot, video, RevisionRequest(instruction='动作放慢。', service_id='new-service'))
        assert switched.service_id == 'new-service' and switched.model_id is None


def test_revision_route_passes_text_and_native_inputs_and_retains_original(client, revision_source, monkeypatch):
    from app.api import routes
    captured = {}
    async def capture_generation(request, session, shot, payload, *, instruction=None):
        captured.update(payload=payload, instruction=instruction, shot_id=shot.id)
        job = m.GenerationJob(project_id=shot.project_id, shot_id=shot.id, provider='isolated-contract-test',
                              status='queued', prompt='isolated revised prompt', parameters_json=payload.parameters)
        session.add(job); session.flush()
        return job, None
    monkeypatch.setattr(routes, 'generate_for_shot', capture_generation)
    source = revision_source
    original_bytes = source.path.read_bytes()
    response = client.post('/api/videos/'+source.video_id+'/revise', json={'instruction':'记录本全程保持合上，镜头移动更缓慢。'})
    assert response.status_code == 202, response.text
    assert captured['instruction'] == '记录本全程保持合上，镜头移动更缓慢。'
    assert captured['shot_id'] == source.shot_id
    assert captured['payload'].parameters == {'width':1024,'height':576,'steps':30}
    assert captured['payload'].service_id == 'source-service'
    result = response.json()
    assert result['based_on'] == source.video_id
    with client.app.state.session_factory() as session:
        job = session.get(m.GenerationJob, result['job']['id'])
        assert job.parameters_json['based_on_video_id'] == source.video_id
        assert job.parameters_json['original_prompt'] == session.get(m.GenerationJob, source.job_id).prompt
        old = session.get(m.VideoRevision, source.video_id)
        assert old.review_status == 'approved' and old.revision == 2 and old.relative_path == source.path.name
        assert len(list(session.scalars(select(m.VideoRevision).where(m.VideoRevision.shot_id == source.shot_id)))) == 1
    assert source.path.read_bytes() == original_bytes


def test_explicit_mismatched_dimensions_are_still_rejected_before_provider_submit(client, revision_source, monkeypatch):
    from app.services import model_lab
    source = revision_source
    def structured_inputs(session, settings, shot, payload):
        return (session.get(m.CharacterVersion, source.version_id), [], 'detailed_description:\nAn adult checks a notebook.',
                {'cinematic':{'versions':{}}})
    monkeypatch.setattr(model_lab, 'frozen_generation_inputs', structured_inputs)
    # The actual generate_for_shot validation executes. It must reject these
    # explicitly supplied pixels rather than repairing them or submitting a job.
    response = client.post('/api/videos/'+source.video_id+'/revise', json={
        'instruction':'动作放慢。', 'parameters':{'width':640,'height':384}})
    assert response.status_code == 422, response.text
    assert response.json()['error']['code'] == 'cinematic_aspect_mismatch'
    with client.app.state.session_factory() as session:
        assert len(list(session.scalars(select(m.GenerationJob).where(m.GenerationJob.shot_id == source.shot_id)))) == 1
        assert session.get(m.VideoRevision, source.video_id).review_status == 'approved'


def test_structured_revision_preview_uses_participants_when_legacy_binding_is_empty(client, revision_source, monkeypatch):
    from app.services import screenplay
    source = revision_source
    monkeypatch.setattr(screenplay, 'uses_structured_screenplay', lambda session, shot: True)
    def inputs(session, shot, settings):
        return (session.get(m.CharacterVersion, source.version_id), [{'asset_id':'face-A'},{'asset_id':'face-B'}],
                'detailed_description:\nTwo fully clothed adult colleagues check a notebook.',
                {'model_lab_negative_prompt':'Do not mix identities.'})
    monkeypatch.setattr(screenplay, 'generation_inputs', inputs)
    response = client.post('/api/videos/'+source.video_id+'/revision-preview', json={'instruction':'记录本保持合上。'})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['character_version_id'] == source.version_id and result['character_id'] == source.character_id
    assert result['asset_ids'] == ['face-A','face-B']
    assert result['negative_prompt'] == 'Do not mix identities.'
    assert 'Reviewed repair direction for this shot: 记录本保持合上。' in result['prompt']
    assert result['based_on'] == source.video_id


def test_product_only_revision_preview_allows_missing_character_version(client, revision_source, monkeypatch):
    from app.services import screenplay
    source = revision_source
    monkeypatch.setattr(screenplay, 'uses_structured_screenplay', lambda session, shot: True)

    def product_only_inputs(session, shot, settings):
        return (None, [{'asset_id': 'product-photo-1', 'role': 'product'}],
                'detailed_description:\nA realistic product-only close-up of the approved package.',
                {'model_lab_negative_prompt': 'No invented text.'})

    monkeypatch.setattr(screenplay, 'generation_inputs', product_only_inputs)
    response = client.post('/api/videos/'+source.video_id+'/revision-preview',
                           json={'instruction': '包装保持完整，镜头缓慢推进。'})

    assert response.status_code == 200, response.text
    result = response.json()
    assert result['character_id'] is None
    assert result['character_version_id'] is None
    assert result['asset_ids'] == ['product-photo-1']
    assert result['based_on'] == source.video_id
    assert 'Reviewed repair direction for this shot: 包装保持完整，镜头缓慢推进。' in result['prompt']


@pytest.mark.parametrize('structured', [True, False])
def test_revision_preview_and_generation_keep_exact_edited_prompt(client, revision_source, monkeypatch, structured):
    from app.services import screenplay
    from app.services.video_revisions import editable_generation_prompt
    source = revision_source
    monkeypatch.setattr(screenplay, 'uses_structured_screenplay', lambda session, shot: structured)
    if structured:
        monkeypatch.setattr(screenplay, 'generation_inputs', lambda session, shot, settings:
            (None, [{'asset_id': 'product-photo-1'}], 'Original compiled prompt', {}))
    else:
        with client.app.state.session_factory() as session:
            session.get(m.StoryboardShot, source.shot_id).character_asset_version_id = source.version_id
            session.commit()
    edited = '自定义提示词：' + '景' * 12000
    instruction = '保留完整动作。'
    response = client.post('/api/videos/'+source.video_id+'/revision-preview',
                           json={'instruction': instruction, 'prompt_override': edited})
    assert response.status_code == 200, response.text
    assert response.json()['prompt'] == editable_generation_prompt(
        'Original compiled prompt', instruction, structured=structured, prompt_override=edited)
    assert response.json()['prompt'].startswith(edited)
    assert response.json()['prompt'].count(instruction) == 1
    with client.app.state.session_factory() as session:
        payload = revision_generation_request(session, session.get(m.StoryboardShot, source.shot_id),
            session.get(m.VideoRevision, source.video_id), RevisionRequest(instruction=instruction, prompt_override=edited))
        assert payload.prompt_override == edited


@pytest.mark.parametrize('context_retry', [False, True])
def test_retry_retains_edited_prompt_but_rebuilds_compiled_metadata(client, revision_source, monkeypatch, context_retry):
    from app.api import routes
    source = revision_source
    edited = '保留这个精简后的原文。'
    with client.app.state.session_factory() as session:
        previous = session.get(m.GenerationJob, source.job_id)
        previous.status = 'failed'
        previous.parameters_json = {**previous.parameters_json, 'prompt_override': edited,
            'prompt_override_used': True, 'compiled_prompt': 'Old derived body',
            'instruction': '缓慢推进。', 'provider_submission': {'sent_prompt': 'Old submitted body'}}
        if context_retry:
            previous.parameters_json = {**previous.parameters_json, 'h3_workflow': {'workflow_id':'frozen-context-retry-test'}}
        session.commit()
    captured = {}
    async def generate(request, session, shot, payload, *, instruction=None, retry_count=0,
                       frozen_job=None, frozen_service=None, legacy_production=False):
        captured.update(prompt_override=payload.prompt_override, parameters=payload.parameters,
                        instruction=instruction, retry_count=retry_count,
                        frozen_job_id=frozen_job.id if frozen_job else None,
                        frozen_service=frozen_service, legacy_production=legacy_production)
        return session.get(m.GenerationJob, source.job_id), None
    monkeypatch.setattr(routes, 'generate_for_shot', generate)
    response = client.post('/api/jobs/'+source.job_id+'/retry')
    assert response.status_code == 202, response.text
    assert captured['prompt_override'] == edited
    assert captured['instruction'] == '缓慢推进。'
    assert captured['retry_count'] == 1
    assert captured['frozen_job_id'] == (source.job_id if context_retry else None)
    assert captured['frozen_service'] == (None if context_retry else {'service_id':'source-service','model_id':'source-model'})
    assert captured['legacy_production'] is (not context_retry)
    assert not {'prompt_override', 'prompt_override_used', 'compiled_prompt', 'provider_submission'} & captured['parameters'].keys()
