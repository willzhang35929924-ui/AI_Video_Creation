"""Revision recovery contracts; isolated rows and labelled local media, no inference."""
import asyncio
import copy
import hashlib
import io
import subprocess
import wave
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import models as m
from app.core.config import Settings
from app.core.errors import AppError
from app.services import revision_postprocess as rp


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _wav():
    out = io.BytesIO()
    with wave.open(out, 'wb') as audio:
        audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(24000)
        audio.writeframes(b'\x00\x01' * 4800)
    return out.getvalue()


@pytest.fixture()
def source(client, settings):
    settings.media_root.mkdir(parents=True, exist_ok=True)
    with client.app.state.session_factory() as s:
        project = m.Project(name='Synthetic revision recovery contract')
        s.add(project); s.flush()
        shot = m.StoryboardShot(project_id=project.id, order=1, status='approved', duration_seconds=1, aspect_ratio='16:9')
        actor = m.Character(slug=project.id, display_name='Synthetic adult')
        s.add_all([shot, actor]); s.flush()
        version = m.CharacterVersion(character_id=actor.id, version=1, approval_state='approved')
        s.add(version); s.flush()
        participant = m.ShotCharacter(shot_id=shot.id, character_id=actor.id, character_version_id=version.id)
        s.add(participant); s.flush()
        line = m.DialogueLine(shot_id=shot.id, participant_id=participant.id, order=1, text='A sample sentence.')
        s.add(line); s.flush()
        tts = m.MediaProcessTask(project_id=project.id, shot_id=shot.id, kind='tts', slot='dialogue_generation', status='succeeded',
            snapshot_json={'inputs': {'text': line.text}, 'participant_id': participant.id,
                'character_version_id': version.id, 'dialogue_line_id': line.id})
        s.add(tts); s.flush()
        audio = settings.media_root/'synthetic.wav'; audio.write_bytes(_wav())
        asset = m.AudioAsset(project_id=project.id, shot_id=shot.id, participant_id=participant.id,
            character_version_id=version.id, dialogue_line_id=line.id, task_id=tts.id,
            relative_path=audio.name, sha256=_sha(audio.read_bytes()),
            metadata_json={'text_sha256': _sha(line.text.encode())})
        s.add(asset); s.flush(); tts.result_json={'audio_id':asset.id}
        job = m.GenerationJob(project_id=project.id, shot_id=shot.id, provider='synthetic_fixture', status='generated',
            prompt='labelled fixture, no inference', parameters_json={'duration_seconds':1})
        s.add(job); s.flush()
        videos=[]
        for n in range(1,4):
            path=settings.media_root/f'old-{n}.mp4'; path.write_bytes(b'LABELLED CONTRACT FIXTURE '+bytes([n]))
            video=m.VideoRevision(project_id=project.id,shot_id=shot.id,generation_job_id=job.id,revision=n,
                relative_path=path.name,metadata_json={'sha256':_sha(path.read_bytes())})
            s.add(video);s.flush();videos.append(video)
        raw,mixed,quality=videos
        mix=m.MediaProcessTask(project_id=project.id,shot_id=shot.id,kind='audio_mix',slot='dialogue_generation',status='succeeded',
            snapshot_json={'inputs':{'mode':'replace_dialogue'},'sources':{'audio_0':{'id':asset.id,'sha256':asset.sha256}}},
            result_json={'video_id':mixed.id})
        s.add(mix);s.flush()
        mixed.metadata_json={**mixed.metadata_json,'source_video_id':raw.id,'processing_kind':'audio_mix','processing_task_id':mix.id}
        render=m.QualityRender(source_video_id=mixed.id,status='generated',video_id=quality.id,
            snapshot_json={'settings':{'resolution':'1080p','processing':'seedvr2','mode':'fast','aspect_ratio':'16:9'},'route':None})
        s.add(render);s.flush()
        # Quality metadata inherits old processing_task_id. It is not itself the
        # audio mix result and must be traversed through source_video_id.
        quality.metadata_json={**mixed.metadata_json,'source_video_id':mixed.id,'processing_kind':'video_upscale','quality_render_id':render.id}
        retryjob=m.GenerationJob(project_id=project.id,shot_id=shot.id,provider='cloud_lk888_h3_v2',status='generated',prompt='visual revision',
            parameters_json={'duration_seconds':1,'based_on_video_id':quality.id})
        s.add(retryjob);s.flush()
        path=settings.media_root/'prior-revision.mp4';path.write_bytes(b'LABELLED RAW REVISION')
        current=m.VideoRevision(project_id=project.id,shot_id=shot.id,generation_job_id=retryjob.id,revision=4,
            relative_path=path.name,metadata_json={'sha256':_sha(path.read_bytes())})
        s.add(current);s.commit()
        return SimpleNamespace(project=project.id,shot=shot.id,video=current.id,raw=raw.id,mixed=mixed.id,quality=quality.id,
            asset=asset.id,line=line.id,participant=participant.id,version=version.id,tts=tts.id,render=render.id,audio=audio)


def _freeze(session,settings,source,instruction='Only make the product smaller.'):
    return rp.freeze_plan(session,settings,session.get(m.StoryboardShot,source.shot),session.get(m.VideoRevision,source.video),instruction)


def _new_job(session,settings,source):
    plan=_freeze(session,settings,source)
    job=m.GenerationJob(project_id=source.project,shot_id=source.shot,provider='synthetic_fixture',status='running',
        prompt='labelled revision, no inference',parameters_json={rp.KEY:plan})
    session.add(job);session.flush()
    path=settings.media_root/'new-raw.mp4';path.write_bytes(b'LABELLED NEW RAW')
    raw=m.VideoRevision(project_id=source.project,shot_id=source.shot,generation_job_id=job.id,revision=5,
        relative_path=path.name,metadata_json={'sha256':_sha(path.read_bytes())})
    session.add(raw);session.flush();rp.attach_raw(job,raw);session.commit()
    return job.id,raw.id


def test_freezes_ancestor_independent_dialogue_and_exact_seedvr_settings(client,settings,source):
    with client.app.state.session_factory() as s:
        plan=_freeze(s,settings,source)
        assert plan['inputs']['quality']['settings']=={'resolution':'1080p','processing':'seedvr2','mode':'fast','aspect_ratio':'16:9'}
        assert plan['inputs']['quality']['source_render_id']==source.render
        assert plan['inputs']['audio']['sources'][0]['audio_id']==source.asset
        assert plan['inputs']['audio']['sources'][0]['sha256']==_sha(source.audio.read_bytes())
        assert source.quality in plan['inputs']['lineage']
        assert source.mixed in plan['inputs']['lineage']
        assert 'studio_run_id' not in plan and 'studio_node_id' not in plan


@pytest.mark.parametrize('change,code',[
    ('text','revision_dialogue_changed'),('voice','revision_dialogue_source'),
    ('sha','revision_dialogue_file_changed'),('duration','revision_dialogue_timing_changed'),
    ('ownership','revision_dialogue_source'),('task','revision_dialogue_source'),
])
def test_changed_sources_fail_before_video_submission(client,settings,source,change,code):
    with client.app.state.session_factory() as s:
        if change=='text':s.get(m.DialogueLine,source.line).text='New sentence.'
        elif change=='voice':s.get(m.ShotCharacter,source.participant).character_version_id=None
        elif change=='sha':source.audio.write_bytes(b'changed')
        elif change=='duration':s.get(m.StoryboardShot,source.shot).duration_seconds=2
        elif change=='ownership':s.get(m.AudioAsset,source.asset).shot_id=None
        elif change=='task':s.get(m.MediaProcessTask,source.tts).status='unknown'
        with pytest.raises(AppError) as error:_freeze(s,settings,source)
        assert error.value.code==code
        assert len(list(s.scalars(select(m.GenerationJob))))==2


def test_voice_edits_request_normal_script_workflow_instead_of_reusing_old_tts(client,settings,source):
    with client.app.state.session_factory() as s:
        with pytest.raises(AppError) as error:_freeze(s,settings,source,'Change the dialogue to Hello.')
        assert error.value.code=='revision_dialogue_edit_requires_script'
        assert '项目工作台' in error.value.message


@pytest.mark.parametrize('instruction',[
    '原配音不变，仅缩小产品。', '保留原声音，产品只有一个手掌大小。',
    '其他音频保持不变。只改变产品尺寸。',
    'Keep original audio unchanged, only make the product smaller.',
    'Preserve the existing dialogue; 产品缩小。',
])
def test_explicit_preservation_instructions_reuse_audio(client,settings,source,instruction):
    with client.app.state.session_factory() as s:
        assert _freeze(s,settings,source,instruction)['inputs']['audio']['sources'][0]['audio_id']==source.asset


@pytest.mark.parametrize('instruction',[
    '保留原声音，但台词改说你好。', '不要保留原配音，改说你好。',
    'Keep original audio unchanged, but change the dialogue to Hello.',
    'Do not keep original audio. Make her say Hello.', '静音，不说话。', '静音。', 'Mute the shot.',
])
def test_real_speech_changes_are_not_hidden_by_preservation_clause(client,settings,source,instruction):
    with client.app.state.session_factory() as s:
        with pytest.raises(AppError) as error:_freeze(s,settings,source,instruction)
        assert error.value.code=='revision_dialogue_edit_requires_script'


def test_original_mix_timing_and_volume_freeze_before_new_task_fingerprint(client,settings,source):
    with client.app.state.session_factory() as s:
        mixed=s.get(m.VideoRevision,source.mixed);task=s.get(m.MediaProcessTask,mixed.metadata_json['processing_task_id'])
        snapshot=copy.deepcopy(task.snapshot_json)
        snapshot['sources']['audio_0']['metadata']={'start_seconds':.3,'end_seconds':.5,'volume':.4}
        task.snapshot_json=snapshot;s.commit()
        identifier,_=_new_job(s,settings,source)
    asyncio.run(rp.process_tick(settings,client.app.state.session_factory))
    with client.app.state.session_factory() as s:
        plan=s.get(m.GenerationJob,identifier).parameters_json[rp.KEY]
        frozen=plan['inputs']['audio']['sources'][0]['timing']
        assert frozen=={'start_seconds':.3,'end_seconds':.5,'volume':.4}
        child=s.get(m.MediaProcessTask,plan['children']['audio'])
        assert {k:child.snapshot_json['sources']['audio_0']['metadata'][k] for k in frozen}==frozen
        from app.services.media_processing import _fingerprint
        assert child.fingerprint==_fingerprint(child.snapshot_json)
        assert 'start_seconds' not in s.get(m.AudioAsset,source.asset).metadata_json


def test_legacy_raw_resume_is_readonly_until_clicked_and_idempotent(client,settings,source):
    with client.app.state.session_factory() as s:
        s.add(m.QualityCheck(video_revision_id=source.video,check_type='media_decode',status='passed',details_json={'synthetic_contract':True}))
        s.commit()
    response=client.get(f'/api/videos/{source.video}')
    assert response.status_code==200,response.text
    assert response.json()['metadata_json']['revision_processing_recovery']['available'] is True
    with client.app.state.session_factory() as s:
        video=s.get(m.VideoRevision,source.video);job=s.get(m.GenerationJob,video.generation_job_id)
        assert rp.KEY not in job.parameters_json
        count=len(list(s.scalars(select(m.GenerationJob))))
    first=client.post(f'/api/videos/{source.video}/resume-revision-processing')
    assert first.status_code==202,first.text
    assert first.json()['replayed'] is False and first.json()['provider_resubmitted'] is False
    second=client.post(f'/api/videos/{source.video}/resume-revision-processing')
    assert second.status_code==202,second.text
    assert second.json()['replayed'] is True
    assert first.json()['job']['id']==second.json()['job']['id']
    with client.app.state.session_factory() as s:
        assert len(list(s.scalars(select(m.GenerationJob))))==count
        job=s.get(m.GenerationJob,first.json()['job']['id'])
        assert job.status==rp.STATUS and job.parameters_json[rp.KEY]['raw_video_id']==source.video


def test_legacy_raw_with_missing_postprocessing_cannot_be_approved(client,settings,source):
    response=client.post(f'/api/videos/{source.video}/approve',json={'operator':'contract'})
    assert response.status_code==409,response.text
    assert response.json()['error']['code']=='revision_postprocess_required'


def test_legacy_resume_respects_other_active_shot_job(client,settings,source):
    with client.app.state.session_factory() as s:
        s.add(m.QualityCheck(video_revision_id=source.video,check_type='media_decode',status='passed',details_json={}))
        s.add(m.GenerationJob(project_id=source.project,shot_id=source.shot,provider='synthetic_fixture',status='unknown',prompt='unknown outcome'))
        s.commit()
    response=client.post(f'/api/videos/{source.video}/resume-revision-processing')
    assert response.status_code==409,response.text
    assert response.json()['error']['code']=='revision_processing_busy'


def test_resume_refuses_file_changes_and_never_creates_provider_job(client,settings,source):
    with client.app.state.session_factory() as s:
        s.add(m.QualityCheck(video_revision_id=source.video,check_type='media_decode',status='passed',details_json={}))
        path=settings.media_root/s.get(m.VideoRevision,source.video).relative_path
        path.write_bytes(b'changed raw bytes');s.commit()
    response=client.post(f'/api/videos/{source.video}/resume-revision-processing')
    assert response.status_code==409,response.text
    assert response.json()['error']['code']=='revision_processing_raw_changed'
    with client.app.state.session_factory() as s:
        assert len(list(s.scalars(select(m.GenerationJob))))==2
        assert rp.KEY not in s.get(m.GenerationJob,s.get(m.VideoRevision,source.video).generation_job_id).parameters_json


def test_invalid_legacy_lineage_cannot_bypass_required_processing_gate(client,settings,source):
    with client.app.state.session_factory() as s:
        video=s.get(m.VideoRevision,source.video);job=s.get(m.GenerationJob,video.generation_job_id)
        job.parameters_json={**job.parameters_json,'based_on_video_id':'missing-ancestor'};s.commit()
        diagnosis=rp.recovery_status(s,settings,video)
        assert diagnosis['required'] is True and diagnosis['available'] is False
    response=client.post(f'/api/videos/{source.video}/approve',json={})
    assert response.status_code==409,response.text
    assert response.json()['error']['code']=='revision_postprocess_required'


@pytest.mark.parametrize('policy,instruction',[
    ('auto','静音，不说话。'),('skip','Only change the product size.'),
    ('native','Use native model audio.'),
])
def test_prior_audio_intent_boundary_prevents_resurrecting_old_tts(client,settings,source,policy,instruction):
    with client.app.state.session_factory() as s:
        prior=s.get(m.VideoRevision,source.video);old_job=s.get(m.GenerationJob,prior.generation_job_id)
        old_job.parameters_json={**old_job.parameters_json,'instruction':instruction,'context_inputs':{'audio_policy':policy}}
        new_job=m.GenerationJob(project_id=source.project,shot_id=source.shot,provider='synthetic_fixture',status='generated',
            prompt='later visual change',parameters_json={'based_on_video_id':prior.id,'duration_seconds':1,'instruction':'Only make the toy smaller.'})
        s.add(new_job);s.flush()
        later=m.VideoRevision(project_id=source.project,shot_id=source.shot,generation_job_id=new_job.id,
            revision=6,relative_path=prior.relative_path,metadata_json=copy.deepcopy(prior.metadata_json))
        s.add(later);s.flush()
        plan=rp.freeze_plan(s,settings,s.get(m.StoryboardShot,source.shot),later,'保留原声音，只改产品尺寸。')
        assert plan['inputs']['audio'] is None
        assert plan['inputs']['quality']['source_render_id']==source.render


def test_legacy_completion_gate_is_lightweight_and_blocks_already_approved_raw(client,settings,source,monkeypatch):
    def forbidden(*args,**kwargs):raise AssertionError('list/assembly completeness must not hash media')
    monkeypatch.setattr(rp,'_sha',forbidden)
    with client.app.state.session_factory() as s:
        video=s.get(m.VideoRevision,source.video);video.review_status='approved';s.commit()
        with pytest.raises(AppError) as error:rp.assert_complete_revision(s,s.get(m.GenerationJob,video.generation_job_id),video)
        assert error.value.code=='revision_postprocess_required'
    response=client.post('/api/publishing/batches/dry-run',json={'items':[{'video_revision_id':source.video,'account_id':'not-reached'}]})
    assert response.status_code==409,response.text
    assert response.json()['error']['code']=='revision_postprocess_required'


def test_queued_legacy_composition_rechecks_completeness_before_render(client,settings,source,monkeypatch):
    from app.services import composition
    with client.app.state.session_factory() as s:
        s.get(m.VideoRevision,source.video).review_status='approved'
        item=m.FinalComposition(project_id=source.project,video_ids_json=[source.video],status='queued')
        s.add(item);s.commit();identifier=item.id
    def forbidden(*args,**kwargs):raise AssertionError('Incomplete revision must not start a render')
    monkeypatch.setattr(composition,'compose',forbidden)
    asyncio.run(composition.process_composition(settings,client.app.state.session_factory))
    with client.app.state.session_factory() as s:
        assert s.get(m.FinalComposition,identifier).status=='failed'
        assert '恢复配音与画质' in s.get(m.FinalComposition,identifier).error_message


def test_pending_postprocessing_blocks_new_generation_before_any_provider_lookup(client,settings,source):
    with client.app.state.session_factory() as s:
        _,raw=_new_job(s,settings,source)
        s.add(m.ScriptRevision(project_id=source.project,revision=1,status='approved',raw_text='Synthetic fixture',parsed_json={'explicit_shot_ids':[source.shot]}));s.commit()
    response=client.post(f'/api/shots/{source.shot}/generate',json={})
    assert response.status_code==409,response.text
    assert response.json()['error']['code']=='generation_already_active'
    with client.app.state.session_factory() as s:
        assert s.scalar(select(m.GenerationJob.id).where(m.GenerationJob.shot_id==source.shot,m.GenerationJob.status=='postprocessing'))


def test_unified_task_projection_reports_postprocessing_as_running(client,settings,source):
    from app.services.task_projection import get_task
    with client.app.state.session_factory() as s:
        identifier,_=_new_job(s,settings,source)
        result=get_task(s,'video:'+identifier)
        assert result.status=='running' and result.snapshot['source_status']=='postprocessing'


def test_pre_generation_retry_validates_the_frozen_dialogue_source(client,settings,source):
    with client.app.state.session_factory() as s:
        plan=_freeze(s,settings,source)
        shot=s.get(m.StoryboardShot,source.shot)
        rp.validate_generation_plan(s,settings,shot,plan)
        s.get(m.DialogueLine,source.line).text='Now changed.'
        with pytest.raises(AppError) as error:rp.validate_generation_plan(s,settings,shot,plan)
        assert error.value.code=='revision_dialogue_changed'


def test_native_cloud_revision_without_previous_processing_does_not_invent_audio(client,settings,source):
    with client.app.state.session_factory() as s:
        v=s.get(m.VideoRevision,source.video);j=s.get(m.GenerationJob,v.generation_job_id)
        j.parameters_json={'duration_seconds':1}
        assert _freeze(s,settings,source) is None


def test_raw_and_intermediates_cannot_be_approved_as_completed_revision(client,settings,source):
    with client.app.state.session_factory() as s:identifier,raw=_new_job(s,settings,source)
    response=client.post(f'/api/videos/{raw}/approve',json={'operator':'contract','reason':'test'})
    assert response.status_code==409,response.text
    assert response.json()['error']['code']=='revision_postprocess_required'
    with client.app.state.session_factory() as s:
        assert s.get(m.GenerationJob,identifier).status==rp.STATUS
        assert s.get(m.GenerationJob,identifier).provider_job_id is None


def test_ticks_after_restart_reuse_durable_mix_child_and_keep_provider_out(client,settings,source):
    with client.app.state.session_factory() as s:identifier,raw=_new_job(s,settings,source)
    asyncio.run(rp.process_tick(settings,client.app.state.session_factory))
    with client.app.state.session_factory() as s:
        j=s.get(m.GenerationJob,identifier);child=j.parameters_json[rp.KEY]['children']['audio']
        task=s.get(m.MediaProcessTask,child)
        assert task.snapshot_json['sources']['audio_0']['id']==source.asset
        assert task.snapshot_json['sources']['video_path']['id']==raw
        assert task.kind=='audio_mix' and task.status=='queued'
    # Fresh sessions model restarting the orchestrator. No in-memory queue owns
    # the correlation and no second source/model/mix task can be created.
    asyncio.run(rp.process_tick(settings,client.app.state.session_factory))
    with client.app.state.session_factory() as s:
        assert s.get(m.GenerationJob,identifier).parameters_json[rp.KEY]['children']['audio']==child
        assert len(list(s.scalars(select(m.MediaProcessTask).where(m.MediaProcessTask.kind=='audio_mix'))))==2


def test_provider_poll_never_redownloads_or_reimports_preserved_revision_raw(client,settings,source,monkeypatch):
    from app.services.jobs import poll_job
    from app.services import video_services
    with client.app.state.session_factory() as s:identifier,_=_new_job(s,settings,source)
    def forbidden(*args,**kwargs):
        raise AssertionError('Existing raw must never query or submit the video provider again')
    monkeypatch.setattr(video_services,'existing_provider',forbidden)
    asyncio.run(poll_job(settings,client.app.state.session_factory,identifier))
    with client.app.state.session_factory() as s:
        assert len(list(s.scalars(select(m.VideoRevision).where(m.VideoRevision.generation_job_id==identifier))))==1


def test_known_local_mix_interruption_recovers_at_new_path_without_inference(client,settings,source):
    with client.app.state.session_factory() as s:identifier,_=_new_job(s,settings,source)
    asyncio.run(rp.process_tick(settings,client.app.state.session_factory))
    with client.app.state.session_factory() as s:
        old=s.get(m.GenerationJob,identifier).parameters_json[rp.KEY]['children']['audio']
        s.get(m.MediaProcessTask,old).status='unknown';s.commit()
    asyncio.run(rp.process_tick(settings,client.app.state.session_factory))
    asyncio.run(rp.process_tick(settings,client.app.state.session_factory))
    with client.app.state.session_factory() as s:
        plan=s.get(m.GenerationJob,identifier).parameters_json[rp.KEY]
        assert plan['children']['audio']!=old
        assert plan['history']==[{'stage':'audio','child_id':old,'status':'interrupted_local_mix'}]
        assert s.get(m.MediaProcessTask,old).status=='failed'
        assert s.get(m.GenerationJob,identifier).status==rp.STATUS


def test_failure_retry_uses_same_generation_and_never_resubmits_provider(client,settings,source):
    with client.app.state.session_factory() as s:identifier,_=_new_job(s,settings,source)
    asyncio.run(rp.process_tick(settings,client.app.state.session_factory))
    with client.app.state.session_factory() as s:
        old=s.get(m.GenerationJob,identifier).parameters_json[rp.KEY]['children']['audio']
        task=s.get(m.MediaProcessTask,old);task.status='failed';task.error_message='Deliberate local encoder contract failure';s.commit()
    asyncio.run(rp.process_tick(settings,client.app.state.session_factory))
    response=client.post(f'/api/jobs/{identifier}/retry')
    assert response.status_code==202,response.text
    assert response.json()['job']['id']==identifier
    assert response.json()['provider_resubmitted'] is False
    with client.app.state.session_factory() as s:
        j=s.get(m.GenerationJob,identifier)
        assert j.status==rp.STATUS and not j.provider_job_id
        assert 'audio' not in j.parameters_json[rp.KEY]['children']
        assert len(list(s.scalars(select(m.GenerationJob))))==3


def test_native_unknown_quality_cannot_be_retried_as_new_model(client,settings,source):
    with client.app.state.session_factory() as s:
        identifier,raw=_new_job(s,settings,source)
        j=s.get(m.GenerationJob,identifier);plan=copy.deepcopy(j.parameters_json[rp.KEY]);plan['inputs']['audio']=None
        plan['fingerprint']=rp._digest(plan['inputs'])
        child=m.QualityRender(source_video_id=raw,status='unknown',snapshot_json={'native_execution':{'inference_submitted':True}})
        s.add(child);s.flush();plan['children']['quality']=child.id;j.parameters_json={rp.KEY:plan};s.commit()
    asyncio.run(rp.process_tick(settings,client.app.state.session_factory))
    response=client.post(f'/api/jobs/{identifier}/retry')
    assert response.status_code==409,response.text
    assert response.json()['error']['code']=='revision_processing_unknown'


def test_real_ffmpeg_mix_and_quality_finish_once_with_original_audio_and_frames(client,settings,source):
    """Actual local encode/decode, explicitly not an AI model quality test."""
    from app.services import media_processing,video_quality
    environment=Settings.from_env();settings.ffmpeg_path=environment.ffmpeg_path;settings.ffprobe_path=environment.ffprobe_path
    with client.app.state.session_factory() as s:
        identifier,raw=_new_job(s,settings,source)
        j=s.get(m.GenerationJob,identifier);plan=copy.deepcopy(j.parameters_json[rp.KEY])
        plan['inputs']['quality']['settings']={'resolution':'720p','processing':'lanczos','mode':'fast','aspect_ratio':'16:9'}
        plan['fingerprint']=rp._digest(plan['inputs']);j.parameters_json={rp.KEY:plan}
        source_path=settings.media_root/s.get(m.VideoRevision,raw).relative_path
        result=subprocess.run([settings.ffmpeg_path,'-nostdin','-v','error','-y','-f','lavfi','-i','color=black:s=64x36:r=24:d=1',
            '-c:v','libx264','-pix_fmt','yuv420p',str(source_path)],capture_output=True,timeout=30,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        assert result.returncode==0,result.stderr
        v=s.get(m.VideoRevision,raw);v.metadata_json={**v.metadata_json,'sha256':_sha(source_path.read_bytes())};s.commit()
    asyncio.run(rp.process_tick(settings,client.app.state.session_factory))
    with client.app.state.session_factory() as s:mix=s.get(m.GenerationJob,identifier).parameters_json[rp.KEY]['children']['audio']
    asyncio.run(media_processing.process_task(settings,client.app.state.session_factory,mix))
    asyncio.run(rp.process_tick(settings,client.app.state.session_factory))
    with client.app.state.session_factory() as s:
        j=s.get(m.GenerationJob,identifier);q=j.parameters_json[rp.KEY]['children']['quality']
        assert j.status==rp.STATUS
        assert s.get(m.MediaProcessTask,mix).status=='succeeded'
    video_quality.run_render(settings,client.app.state.session_factory,q)
    asyncio.run(rp.process_tick(settings,client.app.state.session_factory))
    asyncio.run(rp.process_tick(settings,client.app.state.session_factory))
    with client.app.state.session_factory() as s:
        j=s.get(m.GenerationJob,identifier);plan=j.parameters_json[rp.KEY]
        assert j.status=='generated' and plan['state']=='ready'
        final=s.get(m.VideoRevision,plan['final_video_id'])
        assert final.metadata_json[rp.KEY]['reused_audio_ids']==[source.asset]
        rp.assert_review_ready(j,final)
        with pytest.raises(AppError):rp.assert_review_ready(j,s.get(m.VideoRevision,raw))
        actual,_=video_quality.measured(settings.media_root/final.relative_path,settings)
        assert actual['frames']==24 and actual['fps']=='24/1'
        assert (actual['width'],actual['height'])==(1280,720)
        assert len(actual['audio'])==1
        assert source.audio.read_bytes()==_wav()
        assert len(list(s.scalars(select(m.QualityRender).where(m.QualityRender.source_video_id!=source.mixed))))==1
        final_id=final.id
    # A later user-requested render peels the completed upscale but retains its
    # mix. Its registered requested source proves it derives from the complete
    # version, despite the encoded input being that version's mixed ancestor.
    response=client.post('/api/quality/renders',json={'video_id':final_id,
        'settings':{'resolution':'1080p','processing':'lanczos','mode':'fast','aspect_ratio':'16:9'}})
    assert response.status_code==202,response.text
    next_render=response.json()['id']
    video_quality.run_render(settings,client.app.state.session_factory,next_render)
    with client.app.state.session_factory() as s:
        render=s.get(m.QualityRender,next_render)
        assert render.status=='generated',render.error_message
        assert render.snapshot_json['requested_source_video_id']==final_id
        assert render.source_video_id!=final_id
        derived=s.get(m.VideoRevision,render.video_id)
        rp.assert_complete_revision(s,s.get(m.GenerationJob,identifier),derived)
        derived_id=derived.id
        actual,_=video_quality.measured(settings.media_root/derived.relative_path,settings)
        assert (actual['width'],actual['height'],actual['frames'])==(1920,1080,24)
        assert len(actual['audio'])==1
    response=client.post(f'/api/videos/{derived_id}/approve',json={'operator':'synthetic contract test'})
    assert response.status_code==200,response.text
    with client.app.state.session_factory() as s:
        from app.services.composition import _sources
        assert _sources(s,SimpleNamespace(project_id=source.project,video_ids_json=[derived_id]))[0].id==derived_id


@pytest.mark.parametrize('change',[None,'raw_input','unfinished','receipt_sha','metadata_only','cycle','wrong_scope'])
def test_complete_derivatives_require_registered_receipts_not_copied_ready_metadata(client,settings,source,change,monkeypatch):
    """Labelled database contracts, no inference and no real media claims."""
    def forbidden(*args,**kwargs):raise AssertionError('completion gates must not hash files')
    with client.app.state.session_factory() as s:
        identifier,raw_id=_new_job(s,settings,source)
        job=s.get(m.GenerationJob,identifier);raw=s.get(m.VideoRevision,raw_id)
        final=m.VideoRevision(project_id=source.project,shot_id=source.shot,generation_job_id=identifier,
            revision=6,relative_path='labelled-complete.mp4',metadata_json={'sha256':'complete-fixture',
                'source_video_id':raw.id,'processing_kind':'video_upscale',rp.KEY:{'state':'ready'}})
        s.add(final);s.flush()
        plan=copy.deepcopy(job.parameters_json[rp.KEY]);plan.update(state='ready',final_video_id=final.id)
        job.parameters_json={rp.KEY:plan};job.status='generated'
        render=m.QualityRender(source_video_id=raw.id,status='generated',relative_path='labelled-derived.mp4',
            snapshot_json={'source_sha256':raw.metadata_json['sha256'],'requested_source_video_id':final.id},
            result_json={'sha256':'derived-fixture'})
        s.add(render);s.flush()
        derived=m.VideoRevision(project_id=source.project,shot_id=source.shot,generation_job_id=identifier,
            revision=7,relative_path=render.relative_path,metadata_json={'sha256':'derived-fixture',
                'source_video_id':raw.id,'processing_kind':'video_upscale','quality_render_id':render.id,rp.KEY:{'state':'ready'}})
        s.add(derived);s.flush();render.video_id=derived.id
        if change=='raw_input':render.snapshot_json={**render.snapshot_json,'requested_source_video_id':raw.id}
        elif change=='unfinished':render.status='running'
        elif change=='receipt_sha':render.result_json={'sha256':'other-output'}
        elif change=='metadata_only':derived.metadata_json={**derived.metadata_json,'quality_render_id':'no-real-receipt'}
        elif change=='cycle':render.snapshot_json={**render.snapshot_json,'requested_source_video_id':derived.id}
        elif change=='wrong_scope':derived.generation_job_id=s.get(m.VideoRevision,source.video).generation_job_id
        s.flush()
        monkeypatch.setattr(rp,'_sha',forbidden)
        if change is None:
            rp.assert_complete_revision(s,job,derived)
            # A separately completed mix of that complete quality derivative is
            # also eligible; copying the ready marker alone is never enough.
            mix=m.MediaProcessTask(project_id=source.project,shot_id=source.shot,kind='audio_mix',
                slot='dialogue_generation',status='succeeded',snapshot_json={'sources':{'video_path':{
                    'id':derived.id,'sha256':'derived-fixture'}}},
                result_json={'sha256':'remixed-fixture','relative_path':'labelled-remixed.mp4'})
            s.add(mix);s.flush()
            remixed=m.VideoRevision(project_id=source.project,shot_id=source.shot,generation_job_id=identifier,
                revision=8,relative_path='labelled-remixed.mp4',metadata_json={'sha256':'remixed-fixture',
                    'source_video_id':derived.id,'processing_kind':'audio_mix','processing_task_id':mix.id})
            s.add(remixed);s.flush();mix.result_json={**mix.result_json,'video_id':remixed.id}
            rp.assert_complete_revision(s,job,remixed)
        else:
            with pytest.raises(AppError):rp.assert_complete_revision(s,job,derived)
        with pytest.raises(AppError):rp.assert_complete_revision(s,job,raw)
