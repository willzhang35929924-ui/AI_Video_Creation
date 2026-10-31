import hashlib
import io
import math
import os
from pathlib import Path
import struct
import subprocess
import wave

import pytest
from sqlalchemy import select
from app import models as m
from app.core.errors import AppError
from app.services.audio_mix import mix_audio_events, probe_audio
from app.services.screenplay import approved_reference_ids
from test_workspace import people, new_shot, document_bytes, delete


def wav_bytes(seconds=2):
    buffer=io.BytesIO()
    with wave.open(buffer,'wb') as out:
        out.setnchannels(1);out.setsampwidth(2);out.setframerate(48000)
        out.writeframes(b''.join(struct.pack('<h',round(.2*32767*math.sin(2*math.pi*440*i/48000))) for i in range(round(seconds*48000))))
    return buffer.getvalue()


def ffmpeg_settings(settings):
    settings.ffmpeg_path=os.environ.get('FFMPEG_PATH','ffmpeg')
    settings.ffprobe_path=os.environ.get('FFPROBE_PATH','ffprobe')
    return settings


def command(*args):
    return subprocess.run(list(args),capture_output=True,check=True,timeout=30).stdout


def test_real_ffmpeg_audio_timing_volume_and_video_stream_preserved(settings):
    settings=ffmpeg_settings(settings);settings.media_root.mkdir(exist_ok=True)
    video=settings.media_root/'isolated-unit-fixture.mp4'
    command(settings.ffmpeg_path,'-y','-v','error','-f','lavfi','-i','color=c=gray:s=64x64:r=24:d=2','-c:v','libx264','-pix_fmt','yuv420p',str(video))
    before=command(settings.ffmpeg_path,'-v','error','-i',str(video),'-map','0:v','-c','copy','-f','h264','-')
    audio=settings.media_root/'unit-tone.wav';audio.write_bytes(wav_bytes())
    event={'id':'isolated-fixture','relative_path':audio.name,'start_seconds':.5,'end_seconds':1.5,'volume':.25}
    result=mix_audio_events(video,[event],settings)
    after=command(settings.ffmpeg_path,'-v','error','-i',str(video),'-map','0:v','-c','copy','-f','h264','-')
    assert hashlib.sha256(before).digest()==hashlib.sha256(after).digest()
    samples=command(settings.ffmpeg_path,'-v','error','-i',str(video),'-map','0:a','-f','f32le','-ac','1','-ar','48000','-')
    values=struct.unpack('<'+'f'*(len(samples)//4),samples)
    def rms(start,end):
        part=values[round(start*48000):round(end*48000)]
        return math.sqrt(sum(x*x for x in part)/len(part))
    assert rms(.05,.35)<.001 and rms(1.7,1.95)<.001
    assert .03<rms(.7,1.3)<.045
    assert result['events'][0]['volume']==.25
    assert abs(float(probe_audio(video,settings)['format']['duration'])-2)<.05
    original=video.read_bytes()
    with pytest.raises(AppError):mix_audio_events(video,[{**event,'end_seconds':3}],settings)
    assert video.read_bytes()==original


def test_audio_upload_decodes_and_invalid_source_is_tracked_without_changing_event(client,settings):
    ffmpeg_settings(settings);_,shot=new_shot(client)
    audio=client.post('/api/shots/'+shot['id']+'/audio-events',json={'kind':'sfx','description':'单次提示音','start_seconds':.2,'end_seconds':1,'volume':.3}).json()
    valid=client.post('/api/audio-events/'+audio['id']+'/file',files={'file':('unit-tone.wav',wav_bytes())})
    assert valid.status_code==200,valid.text
    path=settings.media_root/valid.json()['relative_path'];assert path.exists()
    invalid=client.post('/api/audio-events/'+audio['id']+'/file',files={'file':('broken.wav',b'not an audio file')})
    assert invalid.status_code==422,invalid.text
    with client.app.state.session_factory() as s:
        assert s.get(m.AudioEvent,audio['id']).relative_path==valid.json()['relative_path']
        failed=s.scalar(select(m.ManagedFile).where(m.ManagedFile.relative_path.contains(hashlib.sha256(b'not an audio file').hexdigest())))
        assert failed and failed.metadata_json['kind']=='failed_audio_upload'
    assert client.patch('/api/content/audio/'+audio['id'],json={'kind':'invalid'}).status_code==422


def test_word_import_can_approve_script_then_shots_and_blocks_unreviewed_identities(client):
    batch=client.post('/api/word-imports',files={'file':('example.docx',document_bytes())}).json()
    result=client.post('/api/word-imports/'+batch['id']+'/confirm',json={'revision':batch['revision'],'duplicate_strategy':'new_project'}).json()
    prefix='/api/projects/'+result['project_id']
    response=client.post(prefix+'/script/approve',json={'operator':'automated-isolated-test'})
    assert response.status_code==200,response.text
    assert client.post(prefix+'/shots/approve-all',json={}).json()['approved_count']==2
    blocked=client.get('/api/shots/'+result['shot_ids'][0]+'/prompt-preview')
    assert blocked.status_code==409 and blocked.json()['error']['code']=='participant_identity_unapproved'


def test_word_style_and_constraints_enter_the_same_multi_character_prompt(client,settings):
    from test_api_flow import create_approved_character
    from PIL import Image
    identifier,version_id=create_approved_character(client)
    # The legacy demo helper supplies placeholders; an explicit isolated PNG
    # fixture is necessary to test compilation without weakening production gates.
    path=settings.media_root/'isolated-reviewed-reference.png'
    path.parent.mkdir(parents=True,exist_ok=True);Image.new('RGB',(64,64),'gray').save(path)
    with client.app.state.session_factory() as s:
        asset=m.CharacterAsset(character_version_id=version_id,asset_type='front_face',relative_path=path.name,mime_type='image/png',sha256=hashlib.sha256(path.read_bytes()).hexdigest(),status='approved',source='isolated_test_fixture',is_primary_reference=True)
        s.add(asset);s.flush();s.get(m.CharacterVersion,version_id).standard_face_asset_id=asset.id;s.commit()
    batch=client.post('/api/word-imports',files={'file':('example.docx',document_bytes())}).json()
    preview=batch['preview_json'];preview['project']['style']='自然窗光，克制的现实摄影'
    preview['project']['constraints']='记录本始终由左侧人物持有'
    # Keep one reviewed identity in this compiler test; all other imported people
    # remain ordinary drafts in separate tests instead of bypassing review gates.
    first=preview['characters'][0];first['decision']='existing';first['match_character_id']=identifier
    preview['shots']=preview['shots'][:1];preview['shots'][0]['participants']=preview['shots'][0]['participants'][:1]
    preview['shots'][0]['dialogues']=preview['shots'][0]['dialogues'][:1]
    batch=client.put('/api/word-imports/'+batch['id']+'/preview',json={'revision':batch['revision'],'preview':preview}).json()
    result=client.post('/api/word-imports/'+batch['id']+'/confirm',json={'revision':batch['revision'],'duplicate_strategy':'new_project'}).json()
    response=client.get('/api/shots/'+result['shot_ids'][0]+'/prompt-preview')
    assert response.status_code==200,response.text
    prompt=response.json()['prompt']
    assert '自然窗光，克制的现实摄影' in prompt and '记录本始终由左侧人物持有' in prompt
    assert '开始核对。' in prompt and '(S1)' in prompt


def test_word_import_reports_unavailable_image_service_and_preserves_imported_records(client,settings):
    settings.h3_demo_mode=False
    with client.app.state.session_factory() as s:
        for service in s.scalars(select(m.AIService).where(m.AIService.module=='character_generation')):service.enabled=False
        s.commit()
    batch=client.post('/api/word-imports',files={'file':('example.docx',document_bytes())}).json()
    response=client.post('/api/word-imports/'+batch['id']+'/confirm',json={'revision':batch['revision'],'duplicate_strategy':'new_project','generate_standard_faces':True})
    assert response.status_code==200,response.text
    result=response.json();assert len(result['asset_requests'])==3
    assert all(item['status']=='blocked' and item['reason'] for item in result['asset_requests'])
    assert len(client.get('/api/projects/'+result['project_id']+'/shots').json())==2
    with client.app.state.session_factory() as s:assert s.scalar(select(m.CharacterGenerationJob.id)) is None


def test_reparse_preserves_old_shots_participation_and_approval(client):
    people(client)
    project=client.post('/api/projects',json={'name':'历史修订测试','source_text':'分镜 1：值班室（5秒）\n画面提示词：三人站在桌旁。'}).json()
    prefix='/api/projects/'+project['id']
    old=client.post(prefix+'/parse').json()['script']
    shot=client.get(prefix+'/shots').json()[0]
    client.post('/api/shots/'+shot['id']+'/participants',json={'character_ids':['person_0']})
    client.post(prefix+'/script/approve',json={});client.post('/api/shots/'+shot['id']+'/approve',json={})
    assert client.patch(prefix+'/script',json={'raw_text':'分镜 1：档案室（5秒）\n画面提示词：查找纸质记录。'}).status_code==200
    revised=client.post(prefix+'/parse').json()['script']
    assert old['id']!=revised['id']
    current=client.get(prefix+'/shots').json();assert len(current)==1 and current[0]['id']!=shot['id']
    assert len(client.get(prefix+'/shots?include_history=true').json())==2
    previous=client.get('/api/shots/'+shot['id']+'/workspace').json()
    assert previous['shot']['status']=='approved' and len(previous['participants'])==1
    assert client.get(prefix+'/shots?script_revision_id='+old['id']).json()[0]['id']==shot['id']


def test_duplicate_keeps_cast_lines_and_independent_inputs(client):
    people(client);_,shot=new_shot(client);url='/api/shots/'+shot['id']
    cast=client.post(url+'/participants',json={'character_ids':['person_0','person_1','person_2']}).json()['participants']
    client.patch('/api/participants/'+cast[1]['id'],json={'performance':{'action':'翻阅记录','position':'中间'}})
    client.post(url+'/dialogues',json={'participant_id':cast[1]['id'],'text':'在这里。','delivery':{'tone':'平静'}})
    copied=client.post(url+'/duplicate');assert copied.status_code==201,copied.text
    copy=client.get('/api/shots/'+copied.json()['id']+'/workspace').json()
    assert len(copy['participants'])==3 and len(copy['dialogues'])==1
    assert {p['id'] for p in copy['participants']}.isdisjoint({p['id'] for p in cast})
    speaker=next(p for p in copy['participants'] if p['character_id']=='person_1')
    assert copy['dialogues'][0]['participant_id']==speaker['id'] and speaker['performance_json']['action']=='翻阅记录'
    client.patch('/api/participants/'+speaker['id'],json={'performance':{'action':'不同动作'}})
    assert client.get(url+'/workspace').json()['participants'][1]['performance_json']['action']=='翻阅记录'


def test_unique_approved_legacy_primary_reused_without_mutating_version(client):
    people(client)
    with client.app.state.session_factory() as s:
        version=s.scalar(select(m.CharacterVersion).where(m.CharacterVersion.character_id=='person_0'))
        version.approval_state='approved'
        asset=m.CharacterAsset(character_version_id=version.id,asset_type='front_face',relative_path='unit/primary.png',mime_type='image/png',source='uploaded',status='approved',is_primary_reference=True,sha256='isolated')
        s.add(asset);s.flush()
        assert approved_reference_ids(s,version)==[asset.id] and version.standard_face_asset_id is None
        second=m.CharacterAsset(character_version_id=version.id,asset_type='front_face',relative_path='unit/second.png',mime_type='image/png',source='uploaded',status='approved',is_primary_reference=True,sha256='isolated')
        s.add(second);s.flush();assert approved_reference_ids(s,version)==[]


def test_generic_participant_delete_does_not_resurrect_legacy_relation(client):
    people(client);_,shot=new_shot(client)
    with client.app.state.session_factory() as s:
        row=s.get(m.StoryboardShot,shot['id']);row.character_id='person_0';s.commit()
    cast=client.get('/api/shots/'+shot['id']+'/workspace').json()['participants']
    _,response=delete(client,'participants',[cast[0]['id']])
    assert response.json()['status']=='completed'
    assert client.get('/api/shots/'+shot['id']+'/workspace').json()['participants']==[]
    assert client.get('/api/characters/person_0').status_code==200
