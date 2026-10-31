"""Isolated project voice preparation contracts; no model calls or production rows."""
import hashlib
import io
from types import SimpleNamespace
import wave

import pytest
from sqlalchemy import select, func

from app import models as m
from app.core.errors import AppError
from app.services import studio_voice
from app.services.studio_voice import assert_voice_preparation


def wav_data():
    out = io.BytesIO()
    with wave.open(out, 'wb') as writer:
        writer.setnchannels(1); writer.setsampwidth(2); writer.setframerate(24000)
        writer.writeframes(b'\x01\x00' * 12000)
    return out.getvalue()


@pytest.fixture()
def voice_project(client, settings):
    with client.app.state.session_factory() as session:
        project = m.Project(name='制作前配音隔离验证')
        other = m.Project(name='另一个隔离项目')
        character = m.Character(slug='voice_subject', display_name='成年值班员')
        second = m.Character(slug='voice_second', display_name='成年核对员')
        session.add_all([project, other, character, second]); session.flush()
        v1 = m.CharacterVersion(character_id=character.id, version=1, approval_state='draft')
        v2 = m.CharacterVersion(character_id=character.id, version=2, approval_state='draft')
        sv = m.CharacterVersion(character_id=second.id, version=1, approval_state='draft')
        session.add_all([v1, v2, sv]); session.flush()
        shots = [m.StoryboardShot(project_id=project.id, order=i+1, scene_title='检查记录', status='approved') for i in range(4)]
        other_shot = m.StoryboardShot(project_id=other.id, order=1, scene_title='另项目核对')
        session.add_all([*shots, other_shot]); session.flush()
        # A retained history shot shares the role/version but is excluded by the
        # current script's explicit membership and must never receive new audio.
        script = m.ScriptRevision(project_id=project.id, revision=2, raw_text='成年值班员检查记录。',
                                  parsed_json={'explicit_shot_ids':[s.id for s in shots[:3]]})
        session.add(script)
        participants = [m.ShotCharacter(shot_id=s.id, character_id=character.id, character_version_id=v1.id,
                                       performance_json={'action':'核对记录','speed':1.05}) for s in shots]
        outside = m.ShotCharacter(shot_id=other_shot.id, character_id=character.id, character_version_id=v1.id)
        other_role = m.ShotCharacter(shot_id=shots[0].id, character_id=second.id, character_version_id=sv.id)
        # The same character's different identity version must remain distinct.
        participants[2].character_version_id = v2.id
        session.add_all([*participants, outside, other_role]); session.flush()
        for p in participants[:3]:
            session.add(m.DialogueLine(shot_id=p.shot_id, participant_id=p.id, order=1, text='记录已经核对完成。'))
        data = wav_data()
        path = settings.media_root/'reference.wav'; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
        asset = m.AudioAsset(project_id=project.id, character_version_id=v1.id, relative_path=path.name,
                             sha256=hashlib.sha256(data).hexdigest(), metadata_json={'actual':{'duration':.5,'full_decode':True,'sample_rate':24000},'source_type':'ai_generated'})
        session.add(asset); session.flush()
        video = m.VideoRevision(project_id=project.id, shot_id=shots[0].id,
                                generation_job_id=None, relative_path='immutable-history.mp4', revision=1, review_status='approved')
        # VideoRevision requires a genuine parent row, even in an isolated test.
        job = m.GenerationJob(project_id=project.id, shot_id=shots[0].id, provider='isolated', status='generated', prompt='隔离契约输入')
        session.add(job); session.flush(); video.generation_job_id=job.id; session.add(video)
        session.commit()
        yield SimpleNamespace(project=project.id, other=other.id, character=character.id, second=second.id,
                              version=v1.id, v2=v2.id, sv=sv.id, participants=[p.id for p in participants],
                              outside=outside.id, other_role=other_role.id, shots=[s.id for s in shots], audio=asset.id,
                              path=path, video=video.id)


def overview(client, fixture):
    response = client.get('/api/studio/projects/'+fixture.project+'/voices')
    assert response.status_code == 200, response.text
    return response.json()


def group(data, fixture):
    return next(g for g in data['groups'] if g['character_id']==fixture.character and g['version_id']==fixture.version)


def save(client, fixture, audio=None, token=None, version=None, **kwargs):
    current = group(overview(client, fixture), fixture)
    return client.put('/api/studio/projects/'+fixture.project+'/voices/'+fixture.character, json={
        'version_id':version or fixture.version,'audio_id':fixture.audio if audio is None else audio,
        'expected_token':token or current['expected_token'],**kwargs})


def local_route(*args, **kwargs):
    return {'selected':{'service_snapshot':{'service_id':'local-tts','name':'CosyVoice3隔离服务','provider':'local_runtime',
            'model_id':'cosyvoice3','settings':{'runtime_family':'cosyvoice3','secret_sentinel':'must-not-return'}}},
            'profile':{'revision':1,'settings':{}},'configured':True}


def test_preparation_lists_current_role_versions_and_requires_explicit_audio_policy(client, voice_project):
    fixture=voice_project; data=overview(client, fixture); target=group(data, fixture)
    assert target['version_state']=='draft' and target['dialogue_count']==2
    assert target['participant_ids']==sorted(fixture.participants[:2])
    assert fixture.participants[3] not in str(data) and fixture.outside not in str(data)
    assert len(data['groups'])==3 and not data['tts_enabled'] and not data['ready_to_start']
    assert data['audio_policy']=='auto' and target['configuration_status']=='unconfigured'
    assert data['blockers'] and target['generation_status']=='unconfigured'
    assert all(not item['enabled'] and item['name']=='沿用视频原声' for item in data['services'])
    preference=client.put('/api/studio/projects/'+fixture.project+'/production-preferences',
        json={'expected_revision':0,'dialogue_audio_policy':'native'})
    assert preference.status_code==200,preference.text
    native=overview(client,fixture)
    assert native['audio_policy']=='native' and native['ready_to_start'] and not native['blockers']
    assert group(native,fixture)['configuration_status']=='native'


def test_selected_local_tts_requires_each_speaking_role_reference_without_generating(client, settings, voice_project, monkeypatch):
    monkeypatch.setattr(studio_voice, 'freeze_route', local_route)
    data=overview(client, voice_project)
    assert not data['ready_to_start'] and data['tts_enabled']
    assert group(data, voice_project)['requires_reference_audio'] and not group(data, voice_project)['ready']
    second=next(g for g in data['groups'] if g['character_id']==voice_project.second)
    assert second['dialogue_count']==0 and not second['requires_reference_audio'] and second['ready']
    assert 'secret_sentinel' not in str(data)
    with client.app.state.session_factory() as session:
        with pytest.raises(AppError) as error:
            assert_voice_preparation(session, settings, voice_project.project)
        assert error.value.code=='studio_voice_preparation_required'
        assert session.scalar(select(func.count()).select_from(m.MediaProcessTask))==0
        assert session.scalar(select(func.count()).select_from(m.StudioRun))==0


def test_save_applies_atomically_only_current_same_role_version_and_preserves_performance(client, voice_project):
    fixture=voice_project; result=save(client, fixture)
    assert result.status_code==200, result.text
    new_group=group(result.json(),fixture)
    assert new_group['audio_id']==fixture.audio and not new_group['mixed']
    with client.app.state.session_factory() as session:
        for identifier in fixture.participants[:2]:
            person=session.get(m.ShotCharacter,identifier)
            assert person.performance_json=={'action':'核对记录','speed':1.05,'reference_audio_asset_id':fixture.audio}
            assert person.character_version_id==fixture.version
        for identifier in [fixture.participants[2],fixture.participants[3],fixture.outside,fixture.other_role]:
            assert not session.get(m.ShotCharacter,identifier).performance_json.get('reference_audio_asset_id')
        assert session.get(m.StoryboardShot,fixture.shots[0]).revision==2
        assert session.get(m.StoryboardShot,fixture.shots[1]).revision==2
        assert session.get(m.StoryboardShot,fixture.shots[2]).revision==1
        assert session.get(m.VideoRevision,fixture.video).review_status=='approved'
        assert session.scalar(select(func.count()).select_from(m.VideoRevision))==1


def test_concurrent_role_edit_or_scope_change_rejects_stale_save(client, voice_project):
    fixture=voice_project; token=group(overview(client,fixture),fixture)['expected_token']
    with client.app.state.session_factory() as session:
        person=session.get(m.ShotCharacter,fixture.participants[1]);person.performance_json={**person.performance_json,'emotion':'专注'};session.commit()
    denied=save(client,fixture,token=token)
    assert denied.status_code==409 and denied.json()['error']['code']=='studio_voice_changed'
    with client.app.state.session_factory() as session:
        assert not any(session.get(m.ShotCharacter,p).performance_json.get('reference_audio_asset_id') for p in fixture.participants[:2])


@pytest.mark.parametrize('corruption',['other_project','other_version','changed_file','wrong_extension','bad_decode','outside_path'])
def test_invalid_reference_is_rejected_before_any_role_update(client, voice_project, corruption):
    fixture=voice_project
    with client.app.state.session_factory() as session:
        asset=session.get(m.AudioAsset,fixture.audio)
        if corruption=='other_project': asset.project_id=fixture.other
        elif corruption=='other_version': asset.character_version_id=fixture.v2
        elif corruption=='changed_file': fixture.path.write_bytes(b'changed')
        elif corruption=='wrong_extension':
            copy=fixture.path.with_suffix('.png');copy.write_bytes(fixture.path.read_bytes());asset.relative_path=copy.name
        elif corruption=='bad_decode': asset.metadata_json={'actual':{'duration':.5,'full_decode':False}}
        elif corruption=='outside_path': asset.relative_path='../reference.wav'
        session.commit()
    denied=save(client,fixture)
    assert denied.status_code==422 and denied.json()['error']['code']=='studio_voice_audio_invalid'
    with client.app.state.session_factory() as session:
        assert all(not session.get(m.ShotCharacter,p).performance_json.get('reference_audio_asset_id') for p in fixture.participants[:2])
        assert all(session.get(m.StoryboardShot,s).revision==1 for s in fixture.shots)


def test_missing_identity_version_is_visible_without_silently_assigning_one(client, voice_project, monkeypatch):
    fixture=voice_project
    with client.app.state.session_factory() as session:
        person=session.get(m.ShotCharacter,fixture.participants[0]);person.character_version_id=None;session.commit()
    monkeypatch.setattr(studio_voice,'freeze_route',local_route)
    data=overview(client,fixture)
    missing=next(g for g in data['groups'] if g['character_id']==fixture.character and g['version_id'] is None)
    assert missing['version_state']=='missing' and not missing['ready']
    assert any('身份版本' in issue for issue in missing['issues'])
    with client.app.state.session_factory() as session:
        assert session.get(m.ShotCharacter,fixture.participants[0]).character_version_id is None


def test_running_task_blocks_group_save_and_noop_preserves_existing_versions(client, voice_project):
    fixture=voice_project
    with client.app.state.session_factory() as session:
        run=m.StudioRun(project_id=fixture.project,status='running',nodes_json=[],snapshot_json={})
        session.add(run);session.commit()
    denied=save(client,fixture)
    assert denied.status_code==409 and denied.json()['error']['code']=='studio_voice_generation_active'
    with client.app.state.session_factory() as session:
        assert all(session.get(m.StoryboardShot,s).revision==1 for s in fixture.shots)
        run=session.scalar(select(m.StudioRun).where(m.StudioRun.project_id==fixture.project));run.status='completed';session.commit()
    first=save(client,fixture); assert first.status_code==200,first.text
    token=group(first.json(),fixture)['expected_token']
    second=save(client,fixture,token=token);assert second.status_code==200,second.text
    assert group(second.json(),fixture)['expected_token']==token
    with client.app.state.session_factory() as session:
        assert session.get(m.StoryboardShot,fixture.shots[0]).revision==2


def test_clear_reference_does_not_change_identity_version_or_delete_audio(client, voice_project):
    fixture=voice_project; first=save(client,fixture);assert first.status_code==200,first.text
    token=group(first.json(),fixture)['expected_token']
    result=client.put('/api/studio/projects/'+fixture.project+'/voices/'+fixture.character,
        json={'version_id':fixture.version,'audio_id':None,'expected_token':token})
    assert result.status_code==200,result.text
    assert group(result.json(),fixture)['audio_id'] is None
    with client.app.state.session_factory() as session:
        assert session.get(m.AudioAsset,fixture.audio) and fixture.path.exists()
        assert all(session.get(m.ShotCharacter,p).character_version_id==fixture.version for p in fixture.participants[:2])


def test_simultaneous_saves_with_one_snapshot_allow_one_winner(client, voice_project):
    from concurrent.futures import ThreadPoolExecutor
    fixture=voice_project; token=group(overview(client,fixture),fixture)['expected_token']
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses=list(executor.map(lambda _:save(client,fixture,token=token),range(2)))
    assert sorted(r.status_code for r in responses)==[200,409]
    denied=next(r for r in responses if r.status_code==409)
    assert denied.json()['error']['code']=='studio_voice_changed'
    with client.app.state.session_factory() as session:
        assert all(session.get(m.ShotCharacter,p).performance_json.get('reference_audio_asset_id')==fixture.audio for p in fixture.participants[:2])
        assert all(session.get(m.StoryboardShot,s).revision==2 for s in fixture.shots[:2])


def test_new_start_requires_local_voice_reference_before_creating_run(client, voice_project, monkeypatch):
    fixture=voice_project
    monkeypatch.setattr(studio_voice,'freeze_route',local_route)
    result=client.post('/api/studio/projects/'+fixture.project+'/start',json={})
    assert result.status_code==409,result.text
    assert result.json()['error']['code']=='studio_voice_preparation_required'
    assert result.json()['error']['details']['blockers']
    with client.app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(m.StudioRun))==0
        assert session.scalar(select(func.count()).select_from(m.MediaProcessTask))==0
        assert all(not session.get(m.ShotCharacter,p).performance_json.get('reference_audio_asset_id') for p in fixture.participants[:2])


def test_repeat_active_start_retains_accepted_run_despite_current_missing_voice_reference(client, voice_project, monkeypatch):
    fixture=voice_project
    monkeypatch.setattr(studio_voice,'freeze_route',local_route)
    assert not overview(client,fixture)['ready_to_start']
    snapshot={'shot_ids':fixture.shots[:3],'options':{}}
    with client.app.state.session_factory() as session:
        run=m.StudioRun(project_id=fixture.project,status='queued',snapshot_json=snapshot,nodes_json=[])
        session.add(run);session.commit();run_id=run.id
    # This request acknowledges an already accepted scope; voice preparation
    # must govern new runs without changing the existing start idempotency.
    result=client.post('/api/studio/projects/'+fixture.project+'/start',json={})
    assert result.status_code==202,result.text
    assert result.json()['run']['id']==run_id
    with client.app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(m.StudioRun))==1
        run=session.get(m.StudioRun,run_id)
        assert run.status=='queued' and run.snapshot_json==snapshot and run.nodes_json==[]
        assert session.scalar(select(func.count()).select_from(m.MediaProcessTask))==0


def test_voice_change_resets_current_decision_and_preserves_historical_approvals_and_snapshots(client, voice_project):
    import json
    fixture=voice_project
    old_file=fixture.path.parent/'immutable-history.mp4'
    old_file.write_bytes(b'Explicit isolated history-byte fixture, not generated model output')
    file_sha=hashlib.sha256(old_file.read_bytes()).hexdigest()
    with client.app.state.session_factory() as session:
        old_video=session.get(m.VideoRevision,fixture.video)
        old_video.review_reason='此前已实际播放并通过'
        old_job=session.get(m.GenerationJob,old_video.generation_job_id)
        old_job.parameters_json={'native_dimensions':{'width':1024,'height':576},'historical_voice':'original'}
        decision=m.StudioDecision(project_id=fixture.project,shot_id=fixture.shots[0],action='keep',video_id=fixture.video,
                                  trim_start=.25,trim_end=.75,position=3,instruction='保留原声音版本',revision=7)
        approval=m.Approval(object_type='video_revision',object_id=old_video.id,revision=old_video.revision,
                            decision='approved',reason='原版本审核记录',operator='local-user')
        old_run=m.StudioRun(project_id=fixture.project,status='completed',nodes_json=[{'kind':'video','video_id':old_video.id,'status':'succeeded'}],
                            snapshot_json={'shot_ids':fixture.shots[:3],'old_voice':'immutable'},result_json={'video_id':old_video.id})
        old_task=m.MediaProcessTask(project_id=fixture.project,shot_id=fixture.shots[0],kind='tts',slot='dialogue_generation',status='succeeded',
                                   snapshot_json={'inputs':{'text':'原版对白'},'voice_reference':'original'},result_json={'old_audio':'immutable'})
        session.add_all([decision,approval,old_run,old_task]);session.commit();session.expire_all()
        protected={
            'video':{c.name:getattr(old_video,c.name) for c in old_video.__table__.columns},
            'approval':{c.name:getattr(approval,c.name) for c in approval.__table__.columns},
            'run':{c.name:getattr(old_run,c.name) for c in old_run.__table__.columns},
            'task':{c.name:getattr(old_task,c.name) for c in old_task.__table__.columns},
            'generation_job':{c.name:getattr(old_job,c.name) for c in old_job.__table__.columns},
        }
        before_sha=hashlib.sha256(json.dumps(protected,sort_keys=True,default=str).encode()).hexdigest()
        ids=(decision.id,approval.id,old_run.id,old_task.id)
    result=save(client,fixture)
    assert result.status_code==200,result.text
    with client.app.state.session_factory() as session:
        decision=session.get(m.StudioDecision,ids[0])
        assert decision.action=='pending' and decision.video_id is None
        assert decision.trim_start==0 and decision.trim_end is None and decision.revision==8
        assert decision.position==3 and decision.instruction=='保留原声音版本'
        old_video=session.get(m.VideoRevision,fixture.video)
        old_job=session.get(m.GenerationJob,old_video.generation_job_id)
        approval=session.get(m.Approval,ids[1]);old_run=session.get(m.StudioRun,ids[2]);old_task=session.get(m.MediaProcessTask,ids[3])
        protected={
            'video':{c.name:getattr(old_video,c.name) for c in old_video.__table__.columns},
            'approval':{c.name:getattr(approval,c.name) for c in approval.__table__.columns},
            'run':{c.name:getattr(old_run,c.name) for c in old_run.__table__.columns},
            'task':{c.name:getattr(old_task,c.name) for c in old_task.__table__.columns},
            'generation_job':{c.name:getattr(old_job,c.name) for c in old_job.__table__.columns},
        }
        after_sha=hashlib.sha256(json.dumps(protected,sort_keys=True,default=str).encode()).hexdigest()
        assert after_sha==before_sha
        assert old_video.review_status=='approved' and old_video.review_reason=='此前已实际播放并通过'
        assert session.get(m.StoryboardShot,fixture.shots[0]).status=='draft'
        assert session.get(m.StoryboardShot,fixture.shots[0]).approved_at is None
        assert session.get(m.StoryboardShot,fixture.shots[0]).revision==2
    assert hashlib.sha256(old_file.read_bytes()).hexdigest()==file_sha
