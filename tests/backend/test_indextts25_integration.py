"""Isolated IndexTTS adapter contracts; fixture WAVs are not model inference evidence."""
import asyncio
import copy
import hashlib
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import wave

import pytest
from sqlalchemy import select

from app import models as m
from app.core.config import Settings
from app.core.errors import AppError
from app.schemas_routing import RuntimeJob, RuntimeManifest, RuntimeReceipt, TaskSubmit
from app.services import media_processing as media, runtime_registry as registry, studio_voice
from app.services.ai_services import service_snapshot


def wav_bytes(rate=24000):
    output=io.BytesIO()
    with wave.open(output,'wb') as writer:
        writer.setnchannels(1);writer.setsampwidth(2);writer.setframerate(rate)
        writer.writeframes(b'\x01\x00'*(rate//2))
    return output.getvalue()


def route(snapshot, runtime=None):
    selected={'service_snapshot':snapshot,'candidate':{'eligible':True,'evidence':{'runtime':runtime or {}}}}
    return {'slot':'dialogue_generation','configured':True,'selected':selected,'choices':[selected],
            'profile':{'revision':1,'settings':{'parameters':{},'max_retries':0,'allow_fallback':False}},
            'allow_cloud_costs':False,'allow_paid_fallback':False}


@pytest.fixture()
def subject(client,settings):
    configured=Settings.from_env();settings.ffmpeg_path=configured.ffmpeg_path;settings.ffprobe_path=configured.ffprobe_path
    with client.app.state.session_factory() as session:
        project=m.Project(name='IndexTTS isolated neutral reference test')
        person=m.Character(slug='index-fixture-person',display_name='成年值班员')
        session.add_all([project,person]);session.flush()
        version=m.CharacterVersion(character_id=person.id,version=1)
        session.add(version);session.flush()
        shot=m.StoryboardShot(project_id=project.id,order=1,scene_title='核对记录')
        session.add(shot);session.flush()
        participant=m.ShotCharacter(shot_id=shot.id,character_id=person.id,character_version_id=version.id)
        session.add(participant);session.flush()
        line=m.DialogueLine(shot_id=shot.id,participant_id=participant.id,order=1,text='原始记录已经核对完成。')
        session.add(line)
        settings.media_root.mkdir(parents=True,exist_ok=True)
        path=settings.media_root/'isolated-reference.wav';path.write_bytes(wav_bytes())
        audio=m.AudioAsset(project_id=project.id,character_version_id=version.id,relative_path=path.name,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),metadata_json={'actual':{'full_decode':True,'duration':.5}})
        index=m.AIService(module='dialogue_generation',provider='local_runtime',name='Index fixture',
            default_model='indextts-2.5',settings_json={'runtime_family':'indextts25'},enabled=True)
        cosy=m.AIService(module='dialogue_generation',provider='local_runtime',name='Cosy fixture',
            default_model='cosyvoice3-0.5b-2512',settings_json={'runtime_family':'cosyvoice3'},enabled=True)
        session.add_all([audio,index,cosy]);session.commit()
        return SimpleNamespace(project=project.id,person=person.id,version=version.id,shot=shot.id,
            participant=participant.id,line=line.id,audio=audio.id,path=path,index=index.id,cosy=cosy.id)


def submit(session,settings,subject,service=None,parameters=None,reference=True,inputs=None,runtime=None):
    snapshot=service_snapshot(session.get(m.AIService,service or subject.index))
    data={'dialogue_line_id':subject.line,'participant_id':subject.participant}
    if reference:data['reference_audio_asset_id']=subject.audio
    data.update(inputs or {})
    return media.create_task(session,settings,TaskSubmit(slot='dialogue_generation',project_id=subject.project,
        shot_id=subject.shot,inputs=data,parameters=parameters or {}),route=route(snapshot,runtime))


def test_fixed_service_registration_accepts_index_without_paths_and_rejects_wrong_module(client):
    payload={'module':'dialogue_generation','provider':'local_runtime','name':'Index configuration only',
        'default_model':'indextts-2.5','settings':{'runtime_family':'indextts25'}}
    created=client.post('/api/settings/ai-services',json=payload)
    assert created.status_code==201,created.text
    assert created.json()['settings']['runtime_family']=='indextts25'
    assert client.post('/api/settings/ai-services',json={**payload,'base_url':'https://example.invalid'}).status_code==422
    assert client.post('/api/settings/ai-services',json={**payload,'settings':{'runtime_family':'indextts25','helper':'arbitrary.py'}}).status_code==422
    assert client.post('/api/settings/ai-services',json={**payload,'module':'lip_sync'}).status_code==422


def test_index_parameters_remain_separate_from_cosy_and_speed_retains_public_meaning(client,settings,subject):
    with client.app.state.session_factory() as session:
        task=submit(session,settings,subject,parameters={'device':'cuda','use_bf16':True,'speed':1.5,'language':'ZH','seed':123})
        assert task.status=='queued'
        assert task.snapshot_json['parameters']=={'device':'cuda','use_bf16':True,'speed':1.5,'language':'ZH','seed':123}
        assert not {'mode','text_frontend','fp16'} & set(task.snapshot_json['parameters'])
        assert task.snapshot_json['character_version_id']==subject.version
        assert task.snapshot_json['sources']['reference_audio_path']['id']==subject.audio


@pytest.mark.parametrize('parameters',[
    {'mode':'cross_lingual'},{'text_frontend':False},{'fp16':True},{'use_bf16':'true'},
    {'speed':0},{'speed':True},{'language':'AUTO'},{'seed':True},{'seed':2**32},{'device':'mps'},
    {'device':'cpu','use_bf16':True},
])
def test_index_rejects_unimplemented_or_invalid_options_before_dispatch(client,settings,subject,parameters):
    with client.app.state.session_factory() as session:
        with pytest.raises(AppError) as error:submit(session,settings,subject,parameters=parameters)
        assert error.value.status_code==422
        assert not list(session.scalars(select(m.MediaProcessTask)))


def test_index_does_not_accept_cosy_reference_transcript(client,settings,subject):
    with client.app.state.session_factory() as session:
        with pytest.raises(AppError) as error:submit(session,settings,subject,inputs={'prompt_text':'reference transcript'})
        assert error.value.code=='tts_parameter_unsupported'


def test_index_requires_owned_reference_and_unknown_reentry_is_idempotent(client,settings,subject):
    with client.app.state.session_factory() as session:
        missing=submit(session,settings,subject,reference=False)
        assert missing.status=='blocked' and '参考音频' in missing.error_message
        valid=submit(session,settings,subject);valid.status='unknown';session.commit()
        duplicate=submit(session,settings,subject)
        assert duplicate.id==valid.id and duplicate.status=='unknown'
        original=session.get(m.AudioAsset,subject.audio)
        wrong=m.CharacterVersion(character_id=subject.person,version=2);session.add(wrong);session.flush()
        original.character_version_id=wrong.id;session.flush()
        with pytest.raises(AppError) as error:submit(session,settings,subject)
        assert error.value.code=='voice_reference_ownership'


def test_preparation_checks_index_reference_before_start(client,settings,subject,monkeypatch):
    with client.app.state.session_factory() as session:
        snapshot=service_snapshot(session.get(m.AIService,subject.index))
    monkeypatch.setattr(studio_voice,'freeze_route',lambda *args,**kwargs:route(snapshot))
    with client.app.state.session_factory() as session:
        state=studio_voice.voice_preparation(session,settings,subject.project)
        assert state['tts_enabled'] and not state['ready_to_start']
        assert state['groups'][0]['requires_reference_audio']
        person=session.get(m.ShotCharacter,subject.participant)
        person.performance_json={'reference_audio_asset_id':subject.audio};session.flush()
        ready=studio_voice.voice_preparation(session,settings,subject.project)
        assert ready['ready_to_start'] and ready['groups'][0]['audio_id']==subject.audio


@pytest.mark.parametrize('fallback',[False,True])
@pytest.mark.parametrize('device',['cuda','cpu'])
def test_runtime_contract_normalizes_reference_and_attributes_active_service(client,settings,subject,monkeypatch,fallback,device):
    runtime={'state':'verified','manifest_sha256':'index-fixture-proof',
        'manifest':{'python':'fixed-isolated-python','helper':'fixed-isolated-index-helper',
                    'repo':str(settings.media_root),'model_dir':str(settings.media_root)}}
    monkeypatch.setattr(media,'manifest_status',lambda family,settings:runtime)
    capture={}
    def fake_fixed_cli(command,timeout,cwd):
        job=RuntimeJob.model_validate_json(Path(command[command.index('--job')+1]).read_text('utf-8'))
        capture['job']=job
        assert job.model_id=='indextts-2.5' and job.parameters['speed']==1.5
        assert job.parameters['device']==device
        assert job.parameters['use_bf16'] is (device!='cpu') and job.parameters['language']=='ZH'
        assert not {'mode','text_frontend','fp16'} & set(job.parameters)
        reference=Path(job.inputs['reference_audio_path'])
        assert reference!=subject.path and reference.is_file()
        output=Path(job.output_path);output.write_bytes(wav_bytes(22050))
        receipt=RuntimeReceipt(task_id=job.task_id,slot=job.slot,model_id=job.model_id,status='succeeded',
            output_path=str(output),output_sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
            inputs_sha256={'reference_audio':hashlib.sha256(reference.read_bytes()).hexdigest(),
                           'text_utf8':hashlib.sha256(job.inputs['text'].strip().encode()).hexdigest()},
            runtime={'fixture':'isolated CLI contract; no model execution'})
        Path(job.receipt_path).write_text(receipt.model_dump_json(),'utf-8')
    monkeypatch.setattr(media,'_run_cli',fake_fixed_cli)
    original_sha=hashlib.sha256(subject.path.read_bytes()).hexdigest()
    with client.app.state.session_factory() as session:
        task=submit(session,settings,subject,service=subject.cosy if fallback else subject.index,
                    parameters={'speed':1.5,'device':device},runtime=runtime)
        if not fallback:assert task.snapshot_json['parameters']['use_bf16'] is (device!='cpu')
        if fallback:
            snapshot=copy.deepcopy(task.snapshot_json)
            snapshot['active_route']=route(service_snapshot(session.get(m.AIService,subject.index)),runtime)
            snapshot['attempt_index']=1;task.snapshot_json=snapshot
            task.result_json={'attempt_history':[{'service_id':subject.cosy,'model_id':'cosyvoice3-0.5b-2512','status':'failed'}]}
        session.commit();identifier=task.id
    asyncio.run(media.process_task(settings,client.app.state.session_factory,identifier))
    with client.app.state.session_factory() as session:
        task=session.get(m.MediaProcessTask,identifier)
        assert task.status=='succeeded',task.error_message
        assert task.result_json['actual_service_snapshot']['service_id']==subject.index
        assert task.result_json['actual']['sample_rate']==22050
        assert task.result_json['runtime_input_derivations'][0]['source_preserved']
        asset=session.get(m.AudioAsset,task.result_json['audio_id'])
        assert asset.character_version_id==subject.version and asset.participant_id==subject.participant
    assert hashlib.sha256(subject.path.read_bytes()).hexdigest()==original_sha
    assert capture['job'].parameters['speed']==1.5


@pytest.mark.parametrize('reference,prompt_text',[(False,None),(True,'参考音频里的原始台词。'),(True,None)])
def test_fallback_only_enters_index_with_reference_and_without_cosy_transcript(client,settings,subject,reference,prompt_text):
    with client.app.state.session_factory() as session:
        task=submit(session,settings,subject,service=subject.cosy,reference=reference,
            inputs={'prompt_text':prompt_text} if prompt_text else None)
        frozen=copy.deepcopy(task.snapshot_json)
        frozen['route']['profile']['settings'].update(allow_fallback=True,max_retries=0)
        alternative=route(service_snapshot(session.get(m.AIService,subject.index)))['selected']
        frozen['route']['choices'].append(alternative)
        task.snapshot_json=frozen;task.status='failed'
        task.result_json={'error_code':'runtime_failed','attempt_history':[{'service_id':subject.cosy,'model_id':'cosyvoice3-0.5b-2512','status':'failed'}]}
        assert media._schedule_determined_retry(session,task) is (reference and not prompt_text)
        if prompt_text:
            assert task.status=='failed' and task.snapshot_json['inputs']['prompt_text']==prompt_text
            assert 'active_route' not in task.snapshot_json


def test_registry_accepts_only_fixed_index_helper_and_audio_smoke(settings,tmp_path,monkeypatch):
    configured=Settings.from_env();settings.ffmpeg_path=configured.ffmpeg_path;settings.ffprobe_path=configured.ffprobe_path
    root=tmp_path/'contract-project';helper=root/'scripts/run_indextts25.py';helper.parent.mkdir(parents=True)
    helper.write_text('# isolated helper-identity fixture; not executable model evidence\n','utf-8')
    repo=tmp_path/'repo';model=tmp_path/'model';repo.mkdir();model.mkdir()
    weight=model/'isolated-component.bin';weight.write_bytes(b'isolated model component identity fixture')
    output=tmp_path/'isolated-smoke.wav';output.write_bytes(wav_bytes(22050))
    sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
    receipt=tmp_path/'receipt.json'
    receipt.write_text(RuntimeReceipt(task_id='isolated',slot='dialogue_generation',model_id='indextts-2.5',
        status='succeeded',output_path=str(output),output_sha256=sha(output)).model_dump_json(),'utf-8')
    manifest=RuntimeManifest(family='indextts25',model_id='indextts-2.5',name='Isolated Index manifest contract',version='2.5',
        task_capabilities=['dialogue_generation'],python=sys.executable,repo=str(repo),model_dir=str(model),helper=str(helper),
        helper_sha256=sha(helper),components=[{'path':str(weight),'role':'fixture','bytes':weight.stat().st_size,'sha256':sha(weight)}],
        dependency_status='installed',load_status='verified',smoke_test={'status':'PASS','receipt_path':str(receipt),
        'output_path':str(output),'output_sha256':sha(output)})
    manifest_path=tmp_path/'manifests';manifest_path.mkdir();(manifest_path/'indextts25.json').write_text(manifest.model_dump_json(),'utf-8')
    monkeypatch.setattr(registry,'PROJECT_ROOT',root);monkeypatch.setattr(registry,'MANIFEST_ROOT',manifest_path)
    status=registry.manifest_status('indextts25',settings)
    assert status['state']=='verified',status
    assert status['evidence']['actual']['streams'][0]['codec_type']=='audio'
    helper.write_text('# changed helper\n','utf-8')
    assert registry.manifest_status('indextts25',settings)['state']=='unavailable'
