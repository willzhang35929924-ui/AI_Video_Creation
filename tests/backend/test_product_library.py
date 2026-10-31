"""Real local image/SQLite/HTTP contracts; these tests do not claim model inference."""
import base64
import copy
import ctypes
import hashlib
from io import BytesIO
import os
from types import SimpleNamespace
import uuid

from PIL import Image
import pytest
from sqlalchemy import select

from app import models as m
from app.core.errors import AppError
from app.schemas_product_library import ProductSelection
from app.services import product_library
from app.services.screenplay_multi_source import prepare_images, source_input


def png(index=0):
    stream = BytesIO()
    Image.new('RGB', (96 + index, 96), (20 + index * 19, 50, 90)).save(stream, 'PNG')
    return stream.getvalue()


def create_product(client, name='Isolated real-image product'):
    response = client.post('/api/product-library', json={'name': name, 'category': 'test',
        'selling_points': ['User-provided unverified fixture fact'], 'evidence_notes': 'Local test input only'})
    assert response.status_code == 201, response.text
    return response.json()


def upload(client, identifier, images, filenames=None):
    names = filenames or [f'angle-{index}.png' for index in range(len(images))]
    response = client.post(f'/api/product-library/{identifier}/images',
        files=[('files', (name, image, 'image/png')) for name, image in zip(names, images)],
        data={'purpose': 'front', 'notes': 'Explicit local fixture image'})
    assert response.status_code == 201, response.text
    return response.json()


def path_for(settings, url):
    assert url.startswith('/api/media/')
    relative = url.removeprefix('/api/media/')
    target = (settings.media_root / relative).resolve()
    assert target.is_relative_to(settings.media_root.resolve())
    return target


def delete_preview(client, resource, identifiers, mode='permanent', cascade=True):
    body = {'resource': resource, 'ids': identifiers, 'mode': mode, 'cascade': cascade}
    response = client.post('/api/deletions/preview', json=body)
    assert response.status_code == 200, response.text
    return body, response.json()


def execute(client, body, plan):
    return client.post('/api/deletions/execute', json={**body, 'fingerprint': plan['fingerprint'],
        'confirm': True, 'confirm_permanent': body['mode'] == 'permanent'})


def bind_product(client, settings, product):
    project = client.post('/api/projects', json={'name': 'Isolated product reference selection'}).json()
    with client.app.state.session_factory() as session:
        shot = m.StoryboardShot(project_id=project['id'], order=1, scene_title='Local fixture product view')
        session.add(shot)
        session.flush()
        frozen = product_library.freeze_products(session, settings,
            [ProductSelection(product_id=product['id'], image_ids=[image['id'] for image in product['images']])])[0]
        project_binding = m.ProjectProductBinding(project_id=project['id'], product_id=product['id'], snapshot_json=frozen)
        shot_binding = m.ShotProductBinding(project_id=project['id'], shot_id=shot.id, product_id=product['id'],
            image_ids_json=[image['id'] for image in frozen['images']], state_json={'visible_state': 'visible'})
        session.add_all([project_binding, shot_binding])
        session.commit()
        return project['id'], shot.id, project_binding.id, frozen


def test_real_batch_upload_metadata_order_cover_reload_and_content_deduplication(client, settings):
    product = create_product(client)
    data = [png(0), png(1), png(2)]
    result = upload(client, product['id'], data, ['same.png', 'same.png', 'third.png'])
    assert len(result['added']) == 3 and not result['errors']
    ids = [image['id'] for image in result['added']]
    assert len(set(ids)) == 3
    duplicate = upload(client, product['id'], [data[0]], ['renamed-content.png'])
    assert not duplicate['added'] and duplicate['duplicates'][0]['id'] == ids[0]
    current = client.get('/api/product-library/' + product['id']).json()
    assert len(current['images']) == 3
    response = client.patch('/api/product-library/' + product['id'] + '/images', json={
        'expected_revision': current['revision'], 'cover_image_id': ids[2],
        'images': [{'id': identifier, 'purpose': purpose, 'order': index + 1, 'notes': f'explicit angle {index}'}
                   for index, (identifier, purpose) in enumerate(zip(reversed(ids), ['packaging', 'side', 'front']))]})
    assert response.status_code == 200, response.text
    refreshed = client.get('/api/product-library/' + product['id']).json()
    assert [image['id'] for image in refreshed['images']] == list(reversed(ids))
    assert refreshed['cover_image_id'] == ids[2]
    assert [image['purpose'] for image in refreshed['images']] == ['packaging', 'side', 'front']
    for image in refreshed['images']:
        original = path_for(settings, image['media_url'])
        thumbnail = path_for(settings, image['thumbnail_url'])
        assert hashlib.sha256(original.read_bytes()).hexdigest() == image['sha256']
        with Image.open(original) as decoded:
            decoded.load()
            assert decoded.size == (image['width'], image['height'])
        with Image.open(thumbnail) as decoded:
            decoded.load()
            assert max(decoded.size) <= 320
        assert client.get(image['media_url']).status_code == 200
    assert any(item['id'] == product['id'] for item in client.get('/api/product-library?q=Isolated').json()['items'])


def test_partial_real_batch_rejects_bad_image_without_inventing_a_saved_record(client, settings):
    product = create_product(client)
    result = upload(client, product['id'], [png(), b'not an actual png'], ['valid.png', 'invalid.png'])
    assert len(result['added']) == 1 and len(result['errors']) == 1
    assert result['errors'][0]['filename'] == 'invalid.png'
    assert result['errors'][0]['code'] == 'screenplay_image_decode'
    assert len(client.get('/api/product-library/' + product['id']).json()['images']) == 1
    with client.app.state.session_factory() as session:
        assert len(list(session.scalars(select(m.ProductImage)))) == 1
        assert len(list(session.scalars(select(m.ScreenplayInputAsset)))) == 1


def test_local_file_reanalysis_reports_real_whole_image_statistics_and_staleness(client, settings):
    product = upload(client, create_product(client)['id'], [png(), png(1)])['product']
    response = client.post('/api/product-library/' + product['id'] + '/reanalyze')
    assert response.status_code == 200, response.text
    analysis = response.json()['analysis']
    assert analysis['status'] == 'succeeded' and analysis['model_calls'] == 0
    assert [image['whole_image_mean_rgb'] for image in analysis['images']] == [[20, 50, 90], [39, 50, 90]]
    assert all(image['status'] == 'verified' for image in analysis['images'])
    assert '不等于产品' in analysis['scope']
    refreshed = client.get('/api/product-library/' + product['id']).json()
    assert refreshed['analysis'] == analysis
    added = upload(client, product['id'], [png(2)])['product']
    assert added['analysis']['status'] == 'stale'
    bad_path = path_for(settings, product['images'][0]['media_url'])
    bad_path.write_bytes(png(3))  # Only a pytest temporary original is altered.
    failed = client.post('/api/product-library/' + product['id'] + '/reanalyze').json()
    assert failed['analysis']['status'] == 'needs_attention' and failed['state'] == 'needs_attention'
    assert failed['analysis']['images'][0]['status'] == 'unavailable'
    assert product['id'] in {item['id'] for item in client.get('/api/product-library').json()['items']}, 'A damaged active library product must remain visible for repair/recheck'
    bad_path.write_bytes(png())
    restored = client.post('/api/product-library/' + product['id'] + '/reanalyze').json()
    assert restored['state'] == 'active' and restored['analysis']['status'] == 'succeeded'
    assert restored['analysis']['version'] > analysis['version']


@pytest.mark.parametrize('damaged', [False, True], ids=['readable-source', 'damaged-source'])
def test_archived_reanalysis_never_restores_without_explicit_restore(client, settings, damaged):
    product = upload(client, create_product(client)['id'], [png()])['product']
    path = path_for(settings, product['images'][0]['media_url'])
    if damaged:
        path.write_bytes(png(3))  # Only the isolated temporary source is changed.
    current_bytes = path.read_bytes()
    archived = client.delete('/api/product-library/' + product['id'])
    assert archived.status_code == 200 and archived.json()['state'] == 'archived'
    checked = client.post('/api/product-library/' + product['id'] + '/reanalyze')
    assert checked.status_code == 200, checked.text
    assert checked.json()['state'] == 'archived'
    assert checked.json()['analysis']['status'] == ('needs_attention' if damaged else 'succeeded')
    assert product['id'] not in {item['id'] for item in client.get('/api/product-library').json()['items']}
    all_items = client.get('/api/product-library?include_archived=true').json()['items']
    assert any(item['id'] == product['id'] and item['state'] == 'archived' for item in all_items)
    rejected = upload(client, product['id'], [png(1)])
    assert not rejected['added'] and rejected['errors'][0]['code'] == 'product_archived'
    restored = client.post('/api/product-library/' + product['id'] + '/restore')
    assert restored.status_code == 200, restored.text
    assert restored.json()['state'] == ('needs_attention' if damaged else 'active')
    assert [image['id'] for image in restored.json()['images']] == [product['images'][0]['id']]
    assert path.read_bytes() == current_bytes
    assert client.post('/api/product-library/' + product['id'] + '/restore').status_code == 409


def test_nine_image_library_restores_all_original_bytes_without_generation_limit_truncation(client, settings):
    originals = [png(index) for index in range(9)]
    expected_shas = [hashlib.sha256(data).hexdigest() for data in originals]
    assert len(set(expected_shas)) == 9
    uploaded = upload(client, create_product(client, 'Nine-image archive restore')['id'], originals)
    assert len(uploaded['added']) == 9 and not uploaded['duplicates'] and not uploaded['errors']
    product = uploaded['product']
    identifiers = [image['id'] for image in product['images']]
    assert [image['sha256'] for image in product['images']] == expected_shas
    paths = {image['id']: path_for(settings, image['media_url']) for image in product['images']}
    for image, original in zip(product['images'], originals):
        assert paths[image['id']].read_bytes() == original
    limits = client.get('/api/product-library').json()['limits']
    assert limits['max_images_per_product'] == 32 and limits['max_generation_images'] == 8

    archived = client.delete('/api/product-library/' + product['id'])
    assert archived.status_code == 200 and archived.json()['state'] == 'archived'
    assert product['id'] not in {item['id'] for item in client.get('/api/product-library').json()['items']}
    restored = client.post('/api/product-library/' + product['id'] + '/restore')
    assert restored.status_code == 200, restored.text
    assert restored.json()['state'] == 'active'
    assert restored.json()['analysis']['status'] == 'succeeded'
    assert restored.json()['analysis']['model_calls'] == 0
    assert len(restored.json()['analysis']['images']) == 9
    assert all(image['status'] == 'verified' for image in restored.json()['analysis']['images'])
    refreshed = client.get('/api/product-library/' + product['id']).json()
    assert [image['id'] for image in refreshed['images']] == identifiers
    assert [image['sha256'] for image in refreshed['images']] == expected_shas
    assert refreshed['cover_image_id'] == product['cover_image_id']
    for image, original in zip(refreshed['images'], originals):
        assert paths[image['id']].read_bytes() == original
        response = client.get(image['media_url'])
        assert response.status_code == 200 and response.content == original
    with client.app.state.session_factory() as session:
        assert len(list(session.scalars(select(m.ProductImage)))) == 9
        assert len(list(session.scalars(select(m.ScreenplayInputAsset)))) == 9


def test_needs_attention_product_accepts_repair_upload_without_silently_dropping_the_old_issue(client, settings):
    product = upload(client, create_product(client)['id'], [png()])['product']
    broken = product['images'][0]
    path_for(settings, broken['media_url']).write_bytes(png(3))
    checked = client.post('/api/product-library/' + product['id'] + '/reanalyze').json()
    assert checked['state'] == 'needs_attention'
    result = upload(client, product['id'], [png(1)], ['explicit-repair-copy.png'])
    assert not result['errors'] and len(result['added']) == 1
    repaired_copy = result['added'][0]
    assert repaired_copy['product_id'] == product['id']
    assert path_for(settings, repaired_copy['media_url']).read_bytes() == png(1)
    current = client.get('/api/product-library/' + product['id']).json()
    assert current['state'] == 'needs_attention'
    assert {image['id'] for image in current['images']} == {broken['id'], repaired_copy['id']}
    assert current['analysis']['status'] == 'stale'
    assert product['id'] in {item['id'] for item in client.get('/api/product-library').json()['items']}
    rechecked = client.post('/api/product-library/' + product['id'] + '/reanalyze').json()
    assert rechecked['state'] == 'needs_attention' and rechecked['analysis']['status'] == 'needs_attention'
    assert rechecked['analysis']['model_calls'] == 0


def test_empty_product_archive_and_explicit_restore_create_no_phantom_images(client):
    product = create_product(client, 'Empty product restore contract')
    checked = client.post('/api/product-library/' + product['id'] + '/reanalyze').json()
    assert checked['state'] == 'active' and checked['images'] == []
    assert client.post('/api/product-library/' + product['id'] + '/restore').status_code == 409
    assert client.delete('/api/product-library/' + product['id']).json()['state'] == 'archived'
    assert client.get('/api/product-library').json()['items'] == []
    archived_check = client.post('/api/product-library/' + product['id'] + '/reanalyze').json()
    assert archived_check['state'] == 'archived' and archived_check['images'] == []
    restored = client.post('/api/product-library/' + product['id'] + '/restore')
    assert restored.status_code == 200, restored.text
    assert restored.json()['state'] == 'active' and restored.json()['images'] == []
    assert restored.json()['cover_image_id'] is None
    assert [item['id'] for item in client.get('/api/product-library').json()['items']] == [product['id']]
    with client.app.state.session_factory() as session:
        assert len(list(session.scalars(select(m.Product)))) == 1
        assert not list(session.scalars(select(m.ProductImage)))
        assert not list(session.scalars(select(m.ScreenplayInputAsset)))


def test_product_metadata_cas_and_image_scope_cannot_overwrite_other_products(client):
    first = upload(client, create_product(client, 'First')['id'], [png()])['product']
    second = upload(client, create_product(client, 'Second')['id'], [png(1)])['product']
    saved = client.patch('/api/product-library/' + first['id'], json={'expected_revision': first['revision'], 'brand': 'Explicit brand'})
    assert saved.status_code == 200, saved.text
    stale = client.patch('/api/product-library/' + first['id'], json={'expected_revision': first['revision'], 'brand': 'Lost update'})
    assert stale.status_code == 409
    assert client.get('/api/product-library/' + first['id']).json()['brand'] == 'Explicit brand'
    cross = client.patch('/api/product-library/' + first['id'] + '/images', json={'expected_revision': saved.json()['revision'],
        'images': [{'id': second['images'][0]['id'], 'order': 1}], 'cover_image_id': second['images'][0]['id']})
    assert cross.status_code == 422
    assert client.get('/api/product-library/' + second['id']).json()['images'] == second['images']


def test_frozen_images_keep_sha_and_explicit_keyframe_three_video_five_selection(client, settings):
    product = upload(client, create_product(client)['id'], [png(index) for index in range(5)])['product']
    project_id, shot_id, _, frozen = bind_product(client, settings, product)
    endpoint = f'/api/product-library/projects/{project_id}/shots/{shot_id}/references'
    initial = client.get(endpoint).json()
    row = initial['products'][0]
    ids = [image['id'] for image in row['images']]
    assert row['keyframe_image_ids'] == row['video_image_ids'] == ids and len(ids) == 5
    assert initial['limits'] == {'keyframe_total_max': 4, 'video_total_max': 9}
    invalid = client.put(endpoint, json={'products': [{'product_id': product['id'], 'expected_revision': row['revision'],
        'keyframe_image_ids': ids, 'video_image_ids': ids}]})
    assert invalid.status_code == 422  # Never silently truncate the five-image request.
    body = {'products': [{'product_id': product['id'], 'expected_revision': row['revision'],
        'keyframe_image_ids': ids[:3], 'video_image_ids': ids}]}
    response = client.put(endpoint, json=body)
    assert response.status_code == 200, response.text
    assert client.put(endpoint, json=body).status_code == 409
    with client.app.state.session_factory() as session:
        shot = session.get(m.StoryboardShot, shot_id)
        keyframes = product_library.shot_references(session, settings, shot, stage='keyframe')
        videos = product_library.shot_references(session, settings, shot, stage='video')
        assert [image['id'] for image in keyframes] == ids[:3]
        assert [image['id'] for image in videos] == ids
        assert all(image['visible_state']['reference_selection_confirmed'] is True for image in videos)
        assert [image['sha256'] for image in videos] == [image['sha256'] for image in frozen['images']]
        content, proof = prepare_images(settings, SimpleNamespace(snapshot_json={'products': [frozen]}))
        actual_images = [part for part in content if part['type'] == 'image_url']
        assert len(actual_images) == len(proof['images']) == 5
        for part, evidence in zip(actual_images, proof['images']):
            actual = base64.b64decode(part['image_url']['url'].split(',', 1)[1])
            with Image.open(BytesIO(actual)) as decoded:
                decoded.load()
                assert decoded.format == 'JPEG'
            assert hashlib.sha256(actual).hexdigest() == evidence['request_image_sha256']


def test_cross_product_freeze_and_changed_frozen_bytes_rejected_before_dispatch(client, settings):
    first = upload(client, create_product(client, 'First')['id'], [png()])['product']
    second = upload(client, create_product(client, 'Second')['id'], [png(1)])['product']
    with client.app.state.session_factory() as session:
        with pytest.raises(AppError) as error:
            product_library.freeze_products(session, settings, [ProductSelection(product_id=first['id'], image_ids=[second['images'][0]['id']])])
        assert error.value.code == 'product_image_scope'
    project_id, shot_id, _, _ = bind_product(client, settings, first)
    original = path_for(settings, first['images'][0]['media_url'])
    original.write_bytes(png(3))  # Isolated pytest temporary file only.
    with client.app.state.session_factory() as session:
        with pytest.raises(AppError) as error:
            product_library.shot_references(session, settings, session.get(m.StoryboardShot, shot_id))
        assert error.value.code == 'product_source_changed'


def test_unlink_only_removes_project_and_shot_bindings_and_retains_originals(client, settings):
    product = upload(client, create_product(client)['id'], [png(), png(1)])['product']
    project_id, shot_id, binding_id, _ = bind_product(client, settings, product)
    before = {path_for(settings, image['media_url']): image['sha256'] for image in product['images']}
    response = client.delete(f'/api/product-library/{product["id"]}/project-bindings/{binding_id}')
    assert response.status_code == 200 and response.json()['original_files_retained'], response.text
    assert client.get(f'/api/product-library/projects/{project_id}/shots/{shot_id}/references').json()['products'] == []
    for path, digest in before.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert len(client.get('/api/product-library/' + product['id']).json()['images']) == 2


def test_shared_source_delete_is_protected_and_single_image_delete_leaves_other_images(client, settings):
    first = upload(client, create_product(client, 'First')['id'], [png(), png(1)])['product']
    second = upload(client, create_product(client, 'Second')['id'], [png()])['product']
    shared, unique = first['images']
    assert shared['asset_id'] == second['images'][0]['asset_id']
    source_body, source_plan = delete_preview(client, 'screenplay_inputs', [shared['asset_id']])
    # ProductImage owns a thumbnail, while its immutable original is protected
    # through actual foreign-key dependencies to the shared input record.
    assert {shared['id'], second['images'][0]['id']} <= {item['id'] for item in source_plan['blockers']}
    rejected = execute(client, source_body, source_plan)
    assert rejected.status_code == 409 and path_for(settings, shared['media_url']).exists()
    saved_bytes = path_for(settings, shared['media_url']).read_bytes()
    body, plan = delete_preview(client, 'product_images', [unique['id']])
    assert not plan['blockers']
    targets = {entry['path'] for entry in plan['files'] if not entry['protected']}
    assert path_for(settings, shared['media_url']).relative_to(settings.media_root).as_posix() not in targets
    response = execute(client, body, plan)
    assert response.status_code == 200 and response.json()['status'] == 'completed', response.text
    assert not path_for(settings, unique['media_url']).exists()
    assert not path_for(settings, unique['thumbnail_url']).exists()
    assert path_for(settings, shared['media_url']).read_bytes() == saved_bytes
    assert path_for(settings, second['images'][0]['thumbnail_url']).exists()
    assert [image['id'] for image in client.get('/api/product-library/' + first['id']).json()['images']] == [shared['id']]
    assert client.get(second['images'][0]['media_url']).status_code == 200


def test_deleting_the_cover_does_not_leave_a_normal_product_pointing_to_a_deleted_image(client):
    product = upload(client, create_product(client)['id'], [png(), png(1)])['product']
    cover, remaining = product['images']
    assert product['cover_image_id'] == cover['id']
    body, plan = delete_preview(client, 'product_images', [cover['id']])
    response = execute(client, body, plan)
    assert response.status_code == 200 and response.json()['status'] == 'completed', response.text
    refreshed = client.get('/api/product-library/' + product['id']).json()
    assert [image['id'] for image in refreshed['images']] == [remaining['id']]
    assert refreshed['cover_image_id'] in (None, remaining['id']), refreshed
    assert refreshed['revision'] > product['revision']


@pytest.mark.skipif(os.name != 'nt', reason='Actual Windows exclusive-handle deletion failure')
def test_product_file_delete_failure_remains_durable_then_actual_retry_succeeds(client, settings):
    product = upload(client, create_product(client)['id'], [png()])['product']
    image = product['images'][0]
    thumbnail = path_for(settings, image['thumbnail_url'])
    body, plan = delete_preview(client, 'product_images', [image['id']])
    assert not plan['blockers']
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.CreateFileW(str(thumbnail), 0x80000000, 0, None, 3, 0, None)
    assert handle not in (None, ctypes.c_void_p(-1).value)
    try:
        response = execute(client, body, plan)
        assert response.status_code == 200, response.text
        task = response.json()
        assert task['status'] == 'files_failed' and thumbnail.exists(), task
        assert task['result']['failed_files'] == 1
        with client.app.state.session_factory() as session:
            persisted = session.get(m.DeletionTask, task['id'])
            assert persisted.status == 'files_failed'
            assert persisted.result_json['files'][thumbnail.relative_to(settings.media_root).as_posix()]['status'] == 'failed'
            assert session.get(m.ProductImage, image['id']) is None
    finally:
        kernel.CloseHandle(handle)
    retried = client.post('/api/deletions/' + task['id'] + '/retry')
    assert retried.status_code == 200 and retried.json()['status'] == 'completed', retried.text
    assert not thumbnail.exists() and not path_for(settings, image['media_url']).exists()


def add_language_service(client, provider='local_llm'):
    with client.app.state.session_factory() as session:
        service = m.AIService(module='language_processing', provider=provider, name='Explicit contract-only registry entry',
            base_url='http://127.0.0.1:9/v1' if provider == 'local_llm' else 'https://example.invalid/v1',
            default_model='contract-only-no-inference', models_json=[{'id': 'contract-only-no-inference'}], enabled=True)
        session.add(service)
        session.commit()
        return service.id


def test_cloud_product_media_requires_separate_authorization_before_creating_any_job(client):
    product = upload(client, create_product(client)['id'], [png()])['product']
    service_id = add_language_service(client, 'openai_compatible')
    response = client.post('/api/screenplay/capability-tests', json={'project_name': 'No cloud request permitted',
        'creative_prompt': 'Local authorization gate contract only', 'input_mode': 'image', 'service_id': service_id,
        'product_selections': [{'product_id': product['id'], 'image_ids': [product['images'][0]['id']]}],
        'allow_cloud_costs': True, 'allow_cloud_media_upload': False, 'idempotency_key': uuid.uuid4().hex})
    assert response.status_code == 403 and response.json()['error']['code'] == 'screenplay_cloud_media_not_authorized', response.text
    with client.app.state.session_factory() as session:
        assert not list(session.scalars(select(m.ScreenplayGenerationJob)))


@pytest.mark.parametrize('source_kind', ['products', 'images'])
def test_multi_source_request_round_trip_retains_every_asset_and_selection(client, source_kind):
    product = upload(client, create_product(client)['id'], [png(), png(1)])['product']
    service_id = add_language_service(client)
    source = {'product_selections': [{'product_id': product['id'], 'image_ids': [image['id'] for image in product['images']]}],
        'product_mode': 'single', 'marketing_template': 'unboxing'} if source_kind == 'products' else {
        'asset_ids': [image['asset_id'] for image in product['images']]}
    response = client.post('/api/screenplay/capability-tests', json={'project_name': 'Source restoration contract',
        'creative_prompt': 'No provider execution; inspect durable request only', 'input_mode': 'image', 'service_id': service_id,
        'idempotency_key': uuid.uuid4().hex, **source})
    assert response.status_code == 201, response.text
    restored = client.get('/api/screenplay/jobs/' + response.json()['id']).json()
    for field, expected in source.items():
        assert restored['source_selection'][field] == expected
    if source_kind == 'images':
        assert [asset['id'] for asset in restored['input_assets']] == source['asset_ids']
    else:
        assert [image['id'] for image in restored['products'][0]['images']] == source['product_selections'][0]['image_ids']
    with client.app.state.session_factory() as session:
        job = session.get(m.ScreenplayGenerationJob, restored['id'])
        assert source_input(job) == restored['source_selection']
        assert job.status == 'queued' and job.receipt_json == {}  # No fake model response.


def test_bind_existing_draft_shots_freezes_selected_real_images_and_is_idempotent(client, settings):
    product=create_product(client)
    images=upload(client, product['id'], [png(0),png(1)])['added']
    project=client.post('/api/projects',json={'name':'Existing draft binding fixture'}).json()
    with client.app.state.session_factory() as session:
        shot=m.StoryboardShot(project_id=project['id'],order=1,scene_title='Draft product fixture',status='draft')
        session.add(shot);session.commit();shot_id=shot.id
    payload={'product_id':product['id'],'image_ids':[i['id'] for i in images],
             'shots':[{'shot_id':shot_id,'image_ids':[images[0]['id']],'visible_state':'package'}]}
    url='/api/product-library/projects/'+project['id']+'/bind'
    first=client.post(url,json=payload);assert first.status_code==200,first.text
    assert first.json()['human_visual_approval'] is False
    second=client.post(url,json=payload);assert second.status_code==200,second.text
    with client.app.state.session_factory() as session:
        assert len(list(session.scalars(select(m.ProjectProductBinding).where(m.ProjectProductBinding.project_id==project['id']))))==1
        bindings=list(session.scalars(select(m.ShotProductBinding).where(m.ShotProductBinding.shot_id==shot_id)))
        assert len(bindings)==1 and bindings[0].image_ids_json==[images[0]['id']]
        assert session.get(m.StoryboardShot,shot_id).status=='draft'
        assert bindings[0].state_json['reference_selection_confirmed'] is False
    changed=copy.deepcopy(payload);changed['shots'][0]['image_ids']=[images[1]['id']]
    assert client.post(url,json=changed).status_code==409


def test_bind_existing_shot_rejects_approved_and_busy_project(client, settings):
    product=create_product(client); image=upload(client,product['id'],[png()])['added'][0]
    project=client.post('/api/projects',json={'name':'Binding safety fixture'}).json()
    with client.app.state.session_factory() as session:
        shot=m.StoryboardShot(project_id=project['id'],order=1,scene_title='Approved',status='approved')
        session.add(shot);session.commit();shot_id=shot.id
    payload={'product_id':product['id'],'image_ids':[image['id']],
             'shots':[{'shot_id':shot_id,'image_ids':[image['id']]}]}
    url='/api/product-library/projects/'+project['id']+'/bind'
    assert client.post(url,json=payload).status_code==409
    with client.app.state.session_factory() as session:
        session.get(m.StoryboardShot,shot_id).status='draft'
        session.add(m.StudioRun(project_id=project['id'],status='running',snapshot_json={},nodes_json=[],result_json={}))
        session.commit()
    assert client.post(url,json=payload).status_code==409
    with client.app.state.session_factory() as session:
        assert not list(session.scalars(select(m.ShotProductBinding).where(m.ShotProductBinding.shot_id==shot_id)))


def test_studio_plan_selects_explicit_images_by_script_entities_and_preserves_existing_video(client, settings):
    from app.services.cinematic_quality import visual_inputs
    from app.services.cinematic_rules import fingerprint
    from app.services.studio_pipeline import create_pipeline
    product=create_product(client,name='MYSTERY DUMPLING')
    uploaded=upload(client,product['id'],[png(0),png(1),png(2)])['added']
    allowed=[uploaded[0]['id'],uploaded[1]['id']]
    project=client.post('/api/projects',json={'name':'Automated product plan fixture'}).json()
    with client.app.state.session_factory() as session:
        entities=[m.ProductionEntity(project_id=project['id'],logical_id=k,kind='prop',name=n,spec_json={}) for k,n in [
            ('box','MYSTERY DUMPLING'),('tray','fixture tray'),('toy','fixture toy'),('other','unrelated prop')]]
        session.add_all(entities);session.flush();box,tray,toy,other=entities
        shots=[]
        for index,items in enumerate(([box,tray,toy],[tray,toy],[other],[box,tray]),1):
            shot=m.StoryboardShot(project_id=project['id'],order=index,scene_title='Fixture',status='approved')
            session.add(shot);session.flush();shots.append(shot)
            session.add(m.ShotDetail(shot_id=shot.id,source_json={'production':{'prop_states':[{'entity_id':x.id} for x in items]}}))
        session.flush()
        job=m.GenerationJob(project_id=project['id'],shot_id=shots[3].id,provider='fixture',status='generated',prompt='fixture')
        session.add(job);session.flush()
        video=m.VideoRevision(project_id=project['id'],shot_id=shots[3].id,generation_job_id=job.id,relative_path='fixture.mp4',revision=1,review_status='approved',metadata_json={'sha256':'fixture'})
        session.add(video);session.commit()
        old_hash=fingerprint(visual_inputs(session,shots[0]))
        plan={'product_id':product['id'],'image_ids':allowed,'selection_reason':'User selected English package and matching tray'}
        run=create_pipeline(session,settings,project['id'],{'product_reference_plan':plan,'auto_start':False})
        session.commit();r=run.snapshot_json['product_plan_result']
        assert {x['shot_id'] for x in r['affected_shots']}=={shots[0].id,shots[1].id}
        assert r['skipped_shots']==[{'shot_id':shots[3].id,'reason':'existing_video_preserved'}]
        assert r['human_visual_approval'] is False and fingerprint(visual_inputs(session,shots[0]))!=old_hash
        assert session.get(m.VideoRevision,video.id).review_status=='approved'
        assert not product_library.shot_product_snapshot(session,shots[2])
        assert not product_library.shot_product_snapshot(session,shots[3])
        for shot in shots[:2]:
            refs=product_library.shot_references(session,settings,shot)
            assert {x['id'] for x in refs}==set(allowed)
            assert uploaded[2]['id'] not in {x['id'] for x in refs}
            assert session.get(m.StoryboardShot,shot.id).status=='approved'
        assert run.snapshot_json['options']['product_reference_plan']==plan
        assert any(n['kind']=='keyframe' and n['target_id']==shots[0].id for n in run.nodes_json)


def test_studio_product_plan_does_not_guess_when_brand_absent(client, settings):
    product=create_product(client,name='MYSTERY DUMPLING')
    image=upload(client,product['id'],[png()])['added'][0]
    project=client.post('/api/projects',json={'name':'No product anchor fixture'}).json()
    with client.app.state.session_factory() as session:
        entity=m.ProductionEntity(project_id=project['id'],logical_id='other',kind='prop',name='Unrelated item',spec_json={})
        shot=m.StoryboardShot(project_id=project['id'],order=1,scene_title='Fixture',status='draft')
        session.add_all([entity,shot]);session.flush()
        session.add(m.ShotDetail(shot_id=shot.id,source_json={'production':{'prop_states':[{'entity_id':entity.id}]}}));session.commit()
        with pytest.raises(AppError) as e:
            product_library.apply_studio_product_plan(session,settings,project['id'],[shot],{'product_id':product['id'],'image_ids':[image['id']],'selection_reason':'User chose this product'})
        assert e.value.code=='product_script_anchor_missing'
        session.rollback()
        assert session.scalar(select(m.ProjectProductBinding.id).where(m.ProjectProductBinding.project_id==project['id'])) is None


def test_studio_product_plan_subset_uses_anchor_from_current_full_script(client, settings):
    product=create_product(client,name='MYSTERY DUMPLING')
    image=upload(client,product['id'],[png()])['added'][0]
    project=client.post('/api/projects',json={'name':'Closeup-only fixture'}).json()
    with client.app.state.session_factory() as session:
        box=m.ProductionEntity(project_id=project['id'],logical_id='box',kind='prop',name='MYSTERY DUMPLING',spec_json={})
        toy=m.ProductionEntity(project_id=project['id'],logical_id='toy',kind='prop',name='toy',spec_json={})
        session.add_all([box,toy]);session.flush()
        shots=[]
        for order,ids in enumerate(([box.id,toy.id],[toy.id]),1):
            shot=m.StoryboardShot(project_id=project['id'],order=order,scene_title='Fixture',status='draft')
            session.add(shot);session.flush();shots.append(shot)
            session.add(m.ShotDetail(shot_id=shot.id,source_json={'production':{'prop_states':[{'entity_id':x} for x in ids]}}))
        session.commit()
        plan={'product_id':product['id'],'image_ids':[image['id']],'selection_reason':'User selected reference'}
        result=product_library.apply_studio_product_plan(session,settings,project['id'],[shots[1]],plan)
        assert [x['shot_id'] for x in result['affected_shots']]==[shots[1].id]
        assert not product_library.shot_product_snapshot(session,shots[0])
        assert product_library.shot_product_snapshot(session,shots[1])
