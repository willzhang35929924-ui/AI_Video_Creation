"""Library API integration uses real PCM decoding; it does not simulate TTS."""
import hashlib
import io
import math
import struct
import wave
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app import models as m
from app.core.config import Settings
from app.core.errors import AppError
from app.models.voice_library import VoiceLibraryAsset
from app.services import studio_voice, voice_library


def pcm_audio(seconds=.7):
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(24000)
        stream.writeframes(b''.join(struct.pack('<h', round(5000 * math.sin(index * 2 * math.pi * 440 / 24000)))
                                   for index in range(round(seconds * 24000))))
    return buffer.getvalue()


@pytest.fixture
def library_client(client, settings, monkeypatch):
    configured = Settings.from_env()
    settings.ffmpeg_path = configured.ffmpeg_path
    settings.ffprobe_path = configured.ffprobe_path
    monkeypatch.setattr(voice_library, 'compatible_services', lambda *args: [])
    VoiceLibraryAsset.__table__.create(client.app.state.engine, checkfirst=True)
    if not any(getattr(route, 'path', '') == '/api/voice-library' for route in client.app.routes):
        from app.api.voice_library import router
        client.app.include_router(router)
    return client


@pytest.fixture
def voice_scope(library_client):
    with library_client.app.state.session_factory() as session:
        project = m.Project(name='声音库隔离项目')
        other = m.Project(name='声音库其他项目')
        character = m.Character(slug='voice_library_subject', display_name='核对员')
        session.add_all([project, other, character]); session.flush()
        version = m.CharacterVersion(character_id=character.id, version=1)
        session.add(version); session.flush()
        shot = m.StoryboardShot(project_id=project.id, order=1, scene_title='核对')
        session.add(shot); session.flush()
        person = m.ShotCharacter(shot_id=shot.id, character_id=character.id, character_version_id=version.id)
        session.add(person); session.flush()
        line = m.DialogueLine(shot_id=shot.id, participant_id=person.id, order=1, text='记录已经核对完成。')
        session.add(line); session.commit()
        return SimpleNamespace(project=project.id, other=other.id, character=character.id,
                               version=version.id, person=person.id, shot=shot.id, line=line.id)


def upload(client, data=None, filename='signal.wav', **fields):
    return client.post('/api/voice-library/uploads', files=[('files', (filename, data or pcm_audio(), 'audio/wav'))], data=fields)


def bind(client, scope, identifier, **changes):
    overview = client.get(f'/api/studio/projects/{scope.project}/voices').json()
    group = next(item for item in overview['groups'] if item['character_id'] == scope.character)
    body = {'project_id': scope.project, 'character_id': scope.character, 'version_id': scope.version,
            'expected_token': group['expected_token'], **changes}
    return client.post('/api/voice-library/' + identifier + '/bind', json=body)


def test_real_decode_preview_original_preserved_and_sha_dedup(library_client, settings):
    data = pcm_audio()
    first = upload(library_client, data, name='清晰测试录音', tags='["普通话","测试"]')
    assert first.status_code == 201, first.text
    body = first.json(); assert not body['failures']
    item = body['items'][0]
    assert item['actual'] == {'duration': .7, 'sample_rate': 24000, 'channels': 1, 'codec': 'pcm_s16le', 'full_decode': True}
    assert item['reference_authorized'] is False
    assert item['capabilities']['playback'] and not item['capabilities']['voice_reference']
    assert not item['capabilities']['tts'] and not item['capabilities']['voice_clone']
    assert (settings.media_root / item['relative_path']).read_bytes() == data
    assert hashlib.sha256(data).hexdigest() == item['sha256']
    assert library_client.get(item['preview_url']).status_code == 200
    repeat = upload(library_client, data, filename='different-name.wav', reference_authorized='true')
    assert repeat.json()['duplicates'] == [item['id']]
    # Reimporting bytes never silently changes existing recording permission.
    assert repeat.json()['items'][0]['reference_authorized'] is False
    listed = library_client.get('/api/voice-library?q=清晰&tag=普通话').json()['items']
    assert [row['id'] for row in listed] == [item['id']]


def test_undecodable_audio_is_rejected_without_a_live_record(library_client, settings):
    result = upload(library_client, b'not an audio file')
    assert result.status_code == 201
    assert not result.json()['items'] and result.json()['failures']
    assert not list(settings.media_root.rglob('original.wav'))
    with library_client.app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(VoiceLibraryAsset)) == 0


def test_local_import_cannot_escape_declared_directory(library_client, settings, monkeypatch):
    recordings=settings.media_root.parent/'imports'/'recordings'
    recordings.mkdir(parents=True)
    monkeypatch.setattr(voice_library,'SOURCE_DIRECTORY',recordings)
    monkeypatch.setenv('LOCAL_RECORDINGS_DIR',str(recordings))
    source = settings.media_root.parent / 'outside.wav'
    source.write_bytes(pcm_audio())
    result = library_client.post('/api/voice-library/imports', json={'paths': [str(source)]})
    assert result.status_code == 201
    assert result.json()['failures'][0]['code'] == 'voice_source_scope'
    assert source.read_bytes() == pcm_audio()


def test_library_selection_uses_stable_role_scope_and_requires_separate_reference_permission(library_client, voice_scope, monkeypatch):
    scope = voice_scope
    item = upload(library_client).json()['items'][0]
    def local_route(*args, **kwargs):
        return {'configured': True, 'selected': {'service_snapshot': {'service_id': 'test-local', 'provider': 'local_runtime',
            'name': 'IndexTTS隔离路由', 'model_id': 'indextts-2.5', 'settings': {'runtime_family': 'indextts25'}}}, 'profile': {'settings': {}}}
    monkeypatch.setattr(studio_voice, 'freeze_route', local_route)
    result = bind(library_client, scope, item['id'])
    assert result.status_code == 200, result.text
    group = result.json()['groups'][0]
    assert group['library_asset_id'] == item['id'] and group['audio_id']
    assert group['configuration_status'] == 'blocked' and not result.json()['ready_to_start']
    assert any('授权' in issue for issue in group['issues'])
    with library_client.app.state.session_factory() as session:
        audio = session.get(m.AudioAsset, group['audio_id'])
        assert audio.project_id == scope.project and audio.character_version_id == scope.version
        assert audio.relative_path == item['relative_path']
        with pytest.raises(AppError) as error:
            voice_library.assert_reference_authorized(session, audio)
        assert error.value.code == 'voice_reference_authorization_required'
    updated = library_client.patch('/api/voice-library/' + item['id'], json={
        'expected_revision': item['revision'], 'reference_authorized': True, 'source_note': '隔离测试录音明确用于本地声音参考'})
    assert updated.status_code == 200, updated.text
    prepared = library_client.get(f'/api/studio/projects/{scope.project}/voices').json()
    assert prepared['ready_to_start'] and prepared['groups'][0]['configuration_status'] == 'ready'
    refs = library_client.get('/api/voice-library/' + item['id'] + '/references').json()
    assert refs['projects'][0]['project_id'] == scope.project and refs['reference_count'] == 1
    clear = library_client.put(f'/api/studio/projects/{scope.project}/voices/{scope.character}', json={
        'version_id': scope.version, 'audio_id': None, 'expected_token': prepared['groups'][0]['expected_token']})
    assert clear.status_code == 200 and clear.json()['groups'][0]['audio_id'] is None
    assert library_client.get(item['media_url']).status_code == 200


def test_stale_bind_or_cross_project_scope_rolls_back_new_audio(library_client, voice_scope):
    scope = voice_scope; item = upload(library_client).json()['items'][0]
    denied = bind(library_client, scope, item['id'], expected_token='0' * 64)
    assert denied.status_code == 409, denied.text
    denied = bind(library_client, scope, item['id'], project_id=scope.other)
    assert denied.status_code == 422, denied.text
    with library_client.app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(m.AudioAsset)) == 0
        assert session.get(m.ShotCharacter, scope.person).performance_json == {}


def test_no_service_is_unconfigured_and_native_requires_explicit_choice(library_client, voice_scope):
    result = library_client.get(f'/api/studio/projects/{voice_scope.project}/voices')
    assert result.status_code == 200, result.text
    assert not result.json()['ready_to_start']
    assert result.json()['groups'][0]['configuration_status'] == 'unconfigured'
    denied = library_client.post(f'/api/studio/projects/{voice_scope.project}/start', json={})
    assert denied.status_code == 409


def test_metadata_revision_and_delete_protect_shared_files(library_client, settings):
    item = upload(library_client).json()['items'][0]
    first = library_client.patch('/api/voice-library/' + item['id'], json={
        'expected_revision': 1, 'name': '修改声音名', 'role_types': ['旁白']})
    assert first.status_code == 200 and first.json()['revision'] == 2
    stale = library_client.patch('/api/voice-library/' + item['id'], json={'expected_revision': 1, 'name': '覆盖'})
    assert stale.status_code == 409
    denied = library_client.delete('/api/voice-library/' + item['id'] + '?mode=delete_files')
    assert denied.status_code == 409 and denied.json()['error']['code'] == 'voice_delete_preview_required'
    assert (settings.media_root / item['relative_path']).is_file()


def test_missing_library_original_is_not_advertised_as_playable_or_reference(library_client, settings):
    item = upload(library_client, reference_authorized='true').json()['items'][0]
    (settings.media_root / item['relative_path']).unlink()
    current = library_client.get('/api/voice-library/' + item['id']).json()
    assert current['integrity_status'] == 'unavailable'
    assert not current['capabilities']['playback'] and not current['capabilities']['voice_reference']


@pytest.mark.parametrize('state', ['submitted', 'cancelling', 'unknown'])
def test_accepted_or_unknown_media_task_prevents_changing_role_reference(library_client, voice_scope, state):
    item = upload(library_client).json()['items'][0]
    with library_client.app.state.session_factory() as session:
        session.add(m.MediaProcessTask(project_id=voice_scope.project, shot_id=voice_scope.shot,
            kind='tts', slot='dialogue_generation', status=state, snapshot_json={}))
        session.commit()
    denied = bind(library_client, voice_scope, item['id'])
    assert denied.status_code == 409 and denied.json()['error']['code'] == 'studio_voice_generation_active'
    with library_client.app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(m.AudioAsset)) == 0
        assert not session.get(m.ShotCharacter, voice_scope.person).performance_json.get('reference_audio_asset_id')
