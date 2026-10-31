"""Isolated database/API contracts. These are not real model-generation evidence."""
import asyncio
from io import BytesIO

from docx import Document
from sqlalchemy import func, select

from app import models as m
from app.services.studio_import import content_fingerprint, language_extract
from app.services.word_screenplay import read_blocks


def screenplay(*, same_scene=False, second_weather='雨', extra_line='', name='赵舟', stable='studio_zhaozhou'):
    doc=Document();doc.add_heading('自动化三人剧本',0)
    lines=['项目基本信息','项目名称：自动化夜班','画幅：16:9','人物设定']
    for n,person in enumerate([name,'周青','罗宁']):
        lines += ['人物 '+person,'人物ID：'+(stable if n==0 else ['','studio_zhouqing','studio_luoning'][n]),
            '年龄：'+str(32+n),'身份：海岸站成年值班工程师','外观：普通成人脸，真实皮肤纹理',
            '眼睛：深褐色杏眼','稳定识别特征：左眼下小痣','体态：肩背端正','眉毛：平直眉',
            '鼻部：鼻梁平直','发际线：自然发际线','固定身份特征：左眼下小痣；深褐色杏眼',
            '声音：低沉清晰','服装：深蓝色棉质工作服']
    lines += ['正文剧情']
    for n in range(2):
        room='控制室' if same_scene or n==0 else '门口雨棚'
        lines += [f'场景 {n+1} {room}','地点：'+room,'时间：夜','天气：'+('雨' if n==0 else second_weather),
            '灯光：低亮度顶灯','环境声：雨水击打金属棚顶',f'分镜 {n+1} 三人核对',
            '出场人物：'+name+'、周青、罗宁','画面：三位成年人围在桌旁，记录本始终合上',
            '时长：5秒','运镜：固定','表演：'+name+'｜动作=手扶合上的记录本；位置=左侧；情绪=克制',
            '表演：周青｜动作=指向桌上记录本；位置=中间；情绪=专注',
            '表演：罗宁｜动作=看向周青并点头；位置=右侧；情绪=认真',
            '对白：'+name+'：记录在这里。','对白：周青：我来核对。','对白：罗宁：收到。']
    if extra_line:lines.append(extra_line)
    for line in lines:doc.add_paragraph(line)
    output=BytesIO();doc.save(output);return output.getvalue()


def upload(client, data=None, **form):
    return client.post('/api/studio/import',files={'file':('自动化.docx',data or screenplay())},data={
        'auto_start':'false',**{k:str(v).lower() if isinstance(v,bool) else str(v) for k,v in form.items()}})


def test_one_upload_creates_graph_and_every_explicit_identity_trait(client):
    result=upload(client);assert result.status_code==200,result.text
    result=result.json();assert result['status']=='imported'
    assert len(result['character_ids'])==3 and len(result['scene_ids'])==2 and len(result['shot_ids'])==2
    assert result['parser']['language_model_called'] is False
    with client.app.state.session_factory() as db:
        version=db.scalar(select(m.CharacterVersion).where(m.CharacterVersion.character_id=='studio_zhaozhou'))
        profile=version.identity_json['profile']
        assert profile['eyes']=='深褐色杏眼' and profile['identifying_marks']=='左眼下小痣'
        assert profile['body']=='肩背端正' and '平直眉' in profile['facial_features']
        assert profile['fixed_traits']==['左眼下小痣','深褐色杏眼']
        assert version.identity_json['source_profile']['voice_profile']=='低沉清晰'
        for shot_id in result['shot_ids']:
            assert db.scalar(select(func.count()).select_from(m.ShotCharacter).where(m.ShotCharacter.shot_id==shot_id))==3
            assert db.scalar(select(func.count()).select_from(m.DialogueLine).where(m.DialogueLine.shot_id==shot_id))==3
            detail=db.scalar(select(m.ShotDetail).where(m.ShotDetail.shot_id==shot_id))
            assert detail.scene_id in result['scene_ids']
            for line in db.scalars(select(m.DialogueLine).where(m.DialogueLine.shot_id==shot_id)):
                assert db.get(m.ShotCharacter,line.participant_id).shot_id==shot_id


def test_import_defaults_to_preparation_without_creating_a_pipeline(client):
    response=client.post('/api/studio/import',files={'file':('prepare.docx',screenplay())})
    assert response.status_code==200,response.text
    assert response.json()['status']=='imported' and not response.json().get('pipeline')
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(m.StudioRun))==0
        assert db.scalar(select(func.count()).select_from(m.GenerationJob))==0
        assert db.scalar(select(func.count()).select_from(m.MediaProcessTask))==0


def test_explicit_import_auto_start_checks_voice_preparation_before_pipeline(client,monkeypatch):
    from app.core.errors import AppError
    from app.services import studio_voice
    captured=[]
    def missing_reference(session,settings,project_id,*permissions):
        captured.append(project_id)
        raise AppError(409,'studio_voice_preparation_required','请先配置角色配音')
    monkeypatch.setattr(studio_voice,'assert_voice_preparation',missing_reference)
    response=upload(client,auto_start=True)
    assert response.status_code==409,response.text
    assert response.json()['error']['code']=='studio_voice_preparation_required' and len(captured)==1
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(m.StudioRun))==0
        assert db.scalar(select(func.count()).select_from(m.GenerationJob))==0
        assert db.scalar(select(func.count()).select_from(m.MediaProcessTask))==0


def test_same_doc_and_same_word_content_reuses_project_without_new_work(client):
    data=screenplay();first=upload(client,data).json();same=upload(client,data).json()
    assert same['reused'] is True and same['project_id']==first['project_id']
    doc=Document(BytesIO(data));doc.core_properties.comments='Changed container metadata only'
    output=BytesIO();doc.save(output)
    assert output.getvalue()!=data
    changed_zip=upload(client,output.getvalue()).json()
    assert changed_zip['reused'] is True and changed_zip['script_id']==first['script_id']
    assert len(client.get('/api/projects').json())==1


def test_explicit_new_revision_preserves_old_graph_and_reuses_characters(client):
    first=upload(client).json()
    next_version=upload(client,screenplay(second_weather='晴'),project_id=first['project_id'],new_revision=True)
    assert next_version.status_code==200,next_version.text
    second=next_version.json();assert second['script_id']!=first['script_id']
    assert second['project_id']==first['project_id'] and second['new_characters']==[]
    assert second['character_ids']==first['character_ids']
    with client.app.state.session_factory() as db:
        assert db.get(m.ScriptRevision,first['script_id']) and db.get(m.StoryboardShot,first['shot_ids'][0])
        assert db.get(m.ScriptRevision,second['script_id']).revision==2
    denied=upload(client,screenplay(second_weather='雪'),project_id=first['project_id'])
    assert denied.status_code==409 and denied.json()['error']['code']=='new_revision_required'


def test_identical_repeated_scene_reuses_scene_and_design_but_weather_change_does_not(client):
    first=upload(client,screenplay(same_scene=True)).json()
    assert first['status']=='imported' and len(first['scene_ids'])==1
    with client.app.state.session_factory() as db:
        details=[db.scalar(select(m.ShotDetail).where(m.ShotDetail.shot_id==shot)) for shot in first['shot_ids']]
        assert details[0].scene_id==details[1].scene_id
        assert details[0].source_json['production']['scene_state_id']==details[1].source_json['production']['scene_state_id']
    second=upload(client,screenplay(same_scene=True,second_weather='晴')).json()
    assert second['status']=='imported' and len(second['scene_ids'])==2


def test_unrecognized_prose_is_actionable_exception_not_hidden_success(client):
    result=upload(client,screenplay(extra_line='他低头之后，另外一个人说了一句不知归属的话。')).json()
    assert result['status']=='pending_review' and result['issues'] and result['preview']
    assert result['parser']['language_model_called'] is False
    assert len(client.get('/api/projects').json())==0
    assert client.get(result['preview_url']).status_code==200


def test_same_name_existing_characters_is_not_silently_merged(client):
    for identifier in ['same_one','same_two']:
        r=client.post('/api/character-factory/characters',json={'id':identifier,'name':'同名角色',
            'profile':{'age':32,'description':'成年工程师'}})
        assert r.status_code==201
    # No explicit stable ID means the shared display name cannot be resolved.
    doc=Document(BytesIO(screenplay(name='同名角色')))
    for paragraph in doc.paragraphs:
        if paragraph.text=='人物ID：studio_zhaozhou':paragraph.text=''
    output=BytesIO();doc.save(output)
    result=upload(client,output.getvalue()).json()
    assert result['status']=='pending_review'
    assert len(result['preview']['characters'][0]['candidates'])==2
    assert len(client.get('/api/projects').json())==0


def test_existing_approved_character_identity_version_is_reused(client,settings):
    client.post('/api/character-factory/characters',json={'id':'studio_zhaozhou','name':'赵舟',
        'profile':{'age':32,'description':'成年工程师'}})
    with client.app.state.session_factory() as db:
        version=db.scalar(select(m.CharacterVersion).where(m.CharacterVersion.character_id=='studio_zhaozhou'))
        character=db.get(m.Character,'studio_zhaozhou');character.current_approved_version_id=version.id
        version.approval_state='approved'
        # Existing approved asset metadata is a contract fixture, not evidence
        # that this test called a portrait model.
        asset=m.CharacterAsset(character_version_id=version.id,asset_type='front_face',relative_path='unit-fixture.png',
            mime_type='image/png',sha256='a'*64,status='approved',is_primary_reference=True,source='unit_fixture')
        db.add(asset);db.flush();version.standard_face_asset_id=asset.id
        db.commit();approved_id=version.id;asset_id=asset.id
    result=upload(client).json();assert result['status']=='imported'
    assert len(result['new_characters'])==2
    with client.app.state.session_factory() as db:
        for shot in result['shot_ids']:
            participant=db.scalar(select(m.ShotCharacter).where(m.ShotCharacter.shot_id==shot,m.ShotCharacter.character_id=='studio_zhaozhou'))
            assert participant.character_version_id==approved_id
            assert participant.reference_asset_ids_json==[asset_id]
        assert db.get(m.Character,'studio_zhaozhou').current_approved_version_id==approved_id


def test_explicit_auto_start_saves_real_pipeline_record_without_model_call(client):
    project=client.post('/api/projects',json={'name':'明确使用视频原声的自动启动项目'}).json()
    preference=client.put('/api/studio/projects/'+project['id']+'/production-preferences',
        json={'expected_revision':0,'dialogue_audio_policy':'native'})
    assert preference.status_code==200,preference.text
    response=client.post('/api/studio/import',files={'file':('自动化.docx',screenplay())},
        data={'project_id':project['id'],'new_revision':'true','auto_start':'true'})
    assert response.status_code==200,response.text
    result=response.json();assert result['status']=='imported' and result['pipeline_run_id']
    with client.app.state.session_factory() as db:
        run=db.get(m.StudioRun,result['pipeline_run_id'])
        assert run and run.project_id==result['project_id']
        assert run.snapshot_json['options']['dialogue_audio_policy']=='native'
        voice_nodes=[node for node in run.nodes_json if node['kind']=='tts_optional']
        assert voice_nodes and all(node['status']=='skipped' and node['code']=='native_audio' for node in voice_nodes)
    repeated=client.post('/api/studio/import',files={'file':('再次.docx',screenplay())}).json()
    assert repeated['pipeline_run_id']==result['pipeline_run_id'] and repeated['reused']


def test_cloud_language_cost_gate_sends_no_document(client,settings,monkeypatch):
    from app.services import ai_services
    with client.app.state.session_factory() as db:
        service=m.AIService(module='language_processing',provider='openai_compatible',name='Contract only',
            base_url='https://example.invalid/v1',default_model='unverified-model',is_default=True)
        db.add(service);db.commit()
        def should_never_call(*args,**kwargs):raise AssertionError('cloud request forbidden')
        monkeypatch.setattr(ai_services,'service_request',should_never_call)
        extracted,receipt=asyncio.run(language_extract(db,settings,None,read_blocks(screenplay(),settings.import_max_bytes),False))
        assert extracted is None and receipt['language_model_called'] is False


def test_language_contract_rejects_invented_source_evidence(client,settings,monkeypatch):
    """Controlled HTTP contract response, explicitly not a live language test."""
    from app.services import ai_services
    import httpx
    with client.app.state.session_factory() as db:
        db.add(m.AIService(module='language_processing',provider='openai_compatible',name='Contract only',
            base_url='https://example.invalid/v1',default_model='unverified-model',is_default=True));db.commit()
        async def contract_response(*args,**kwargs):
            assert args[3]=='/chat/completions'
            content='{"lines":[{"text":"人物 张三","source_ids":["paragraph:0"],"quote":"原文没有这个人物"}],"uncertainties":[]}'
            return httpx.Response(200,request=httpx.Request('POST','https://example.invalid/v1/chat/completions'),json={
                'choices':[{'message':{'content':content}}]})
        monkeypatch.setattr(ai_services,'service_request',contract_response)
        extracted,receipt=asyncio.run(language_extract(db,settings,None,read_blocks(screenplay(),settings.import_max_bytes),True))
        assert extracted is None and receipt['status']=='failed' and receipt['language_model_called'] is True
        assert '原文证据' in receipt['error']


def test_docx_signature_and_size_are_checked_before_persistence(client):
    response=client.post('/api/studio/import',files={'file':('bad.docx',b'not a real Word file')})
    assert response.status_code in (400,415,422)
    response=client.post('/api/studio/import',files={'file':('old.doc',b'old binary')})
    assert response.status_code==415
    assert len(client.get('/api/projects').json())==0


def test_editing_one_shot_reuses_other_shot_and_preserves_video_record(client):
    """Metadata regression only; no fabricated clip is used as inference evidence."""
    first=upload(client).json();unchanged_id=first['shot_ids'][0]
    with client.app.state.session_factory() as db:
        job=m.GenerationJob(project_id=first['project_id'],shot_id=unchanged_id,provider='unit-contract-only',
            status='generated',prompt='database preservation contract')
        db.add(job);db.flush()
        video=m.VideoRevision(project_id=first['project_id'],shot_id=unchanged_id,generation_job_id=job.id,
            relative_path='unit-contract-only/not-an-inference-result.mp4',revision=1,review_status='approved')
        db.add(video);db.commit();video_id=video.id
    doc=Document(BytesIO(screenplay()))
    matches=[p for p in doc.paragraphs if p.text=='对白：周青：我来核对。']
    matches[-1].text='对白：周青：现在可以核对。'
    output=BytesIO();doc.save(output)
    second=upload(client,output.getvalue(),project_id=first['project_id'],new_revision=True).json()
    assert second['status']=='imported'
    assert second['shot_ids'][0]==unchanged_id and second['shot_ids'][1]!=first['shot_ids'][1]
    assert second['reused_shot_ids']==[unchanged_id] and second['changed_shot_ids']==[second['shot_ids'][1]]
    current=client.get('/api/projects/'+first['project_id']+'/shots').json()
    assert [s['id'] for s in current]==second['shot_ids']
    history=client.get('/api/projects/'+first['project_id']+'/shots?include_history=true').json()
    assert len(history)==3
    with client.app.state.session_factory() as db:
        assert db.get(m.VideoRevision,video_id).shot_id==unchanged_id
        assert db.get(m.VideoRevision,video_id).review_status=='approved'
        original_scene=db.get(m.Scene,first['scene_ids'][0])
        assert original_scene.script_revision_id==first['script_id']
        assert db.get(m.ScriptRevision,second['script_id']).parsed_json['explicit_shot_ids']==second['shot_ids']


def test_unchanged_new_revision_reuses_all_members_and_editor_changes_are_not_reused(client):
    first=upload(client).json()
    second=upload(client,project_id=first['project_id'],new_revision=True).json()
    assert second['shot_ids']==first['shot_ids'] and second['scene_ids']==first['scene_ids']
    assert second['changed_shot_ids']==[]
    assert [s['id'] for s in client.get('/api/projects/'+first['project_id']+'/shots').json()]==first['shot_ids']
    with client.app.state.session_factory() as db:
        edited=db.get(m.StoryboardShot,first['shot_ids'][0]);edited.visual_prompt='手工修改过画面，不得静默套回旧原文'
        db.commit()
    third=upload(client,project_id=first['project_id'],new_revision=True).json()
    assert third['shot_ids'][0]!=first['shot_ids'][0] and third['shot_ids'][1]==first['shot_ids'][1]


def test_advanced_add_duplicate_and_delete_maintain_imported_revision_members(client):
    imported=upload(client).json();pid=imported['project_id']
    added=client.post('/api/projects/'+pid+'/shots',json={'order':3,'scene_title':'手工补充的镜头','duration_seconds':5})
    assert added.status_code==201,added.text
    copied=client.post('/api/shots/'+imported['shot_ids'][0]+'/duplicate')
    assert copied.status_code==201,copied.text
    expected=set([*imported['shot_ids'],added.json()['id'],copied.json()['id']])
    assert {s['id'] for s in client.get('/api/studio/projects/'+pid).json()['shots']}==expected
    removed=client.delete('/api/shots/'+added.json()['id'])
    assert removed.status_code==200,removed.text
    expected.remove(added.json()['id'])
    assert {s['id'] for s in client.get('/api/studio/projects/'+pid).json()['shots']}==expected
    with client.app.state.session_factory() as db:
        from app.api.routes import project_shot_statement
        assert added.json()['id'] in {s.id for s in db.scalars(project_shot_statement(db,pid,include_history=True))}
        assert db.get(m.StoryboardShot,added.json()['id']).status=='deleted'


def test_legacy_manual_add_remains_visible_and_deleted_import_shot_is_not_reused(client):
    legacy=client.post('/api/projects',json={'name':'旧版手工项目'}).json()
    manual=client.post('/api/projects/'+legacy['id']+'/shots',json={'order':1,'scene_title':'旧版入口'}).json()
    assert [s['id'] for s in client.get('/api/studio/projects/'+legacy['id']).json()['shots']]==[manual['id']]
    imported=upload(client).json();old=imported['shot_ids'][0]
    assert client.delete('/api/shots/'+old).status_code==200
    revised=upload(client,project_id=imported['project_id'],new_revision=True).json()
    assert revised['shot_ids'][0]!=old and revised['shot_ids'][1]==imported['shot_ids'][1]
    assert len(client.get('/api/studio/projects/'+imported['project_id']).json()['shots'])==2


def rearranged_screenplay(*, sequence=(0,1), insert=False, duplicate_scene=False, changed_weather=False):
    """A real DOCX with numbering changes only; identities/content stay stable."""
    original=Document(BytesIO(screenplay()))
    texts=[p.text for p in original.paragraphs]
    starts=[i for i,text in enumerate(texts) if text.startswith('场景 ')]
    prefix=texts[1:starts[0]]
    blocks=[texts[starts[n]:starts[n+1] if n+1<len(starts) else len(texts)] for n in range(len(starts))]
    if duplicate_scene:
        blocks[1]=[line.replace('门口雨棚','控制室') for line in blocks[1]]
    if changed_weather:blocks[0]=['天气：晴' if line=='天气：雨' else line for line in blocks[0]]
    selected=[list(blocks[n]) for n in sequence]
    if insert:
        extra=[line.replace('控制室','海堤步道').replace('三人核对','步道核对') for line in blocks[0]]
        selected.insert(0,extra)
    result=Document();result.add_heading(texts[0],0)
    for line in prefix:result.add_paragraph(line)
    import re
    for n,block in enumerate(selected,1):
        for line in block:
            line=re.sub(r'^场景\s+\d+',f'场景 {n}',line)
            line=re.sub(r'^分镜\s+\d+',f'分镜 {n}',line)
            result.add_paragraph(line)
    output=BytesIO();result.save(output);return output.getvalue()


def test_inserting_independent_scene_reuses_shifted_original_shots(client):
    first=upload(client).json()
    second=upload(client,rearranged_screenplay(insert=True),project_id=first['project_id'],new_revision=True).json()
    assert second['status']=='imported',second
    assert second['shot_ids'][1:]==first['shot_ids']
    assert second['reused_shot_ids']==first['shot_ids'] and len(second['changed_shot_ids'])==1
    assert second['revision_diff']['order_changed'] is True
    overview=client.get('/api/studio/projects/'+first['project_id']).json()
    assert [s['id'] for s in overview['shots']]==second['shot_ids']
    assert [s['order'] for s in overview['shots']]==[1,2,3]
    assert [s['id'] for s in client.get('/api/projects/'+first['project_id']+'/shots').json()]==second['shot_ids']


def test_deleting_independent_scene_reuses_surviving_shifted_shot(client):
    first=upload(client).json()
    second=upload(client,rearranged_screenplay(sequence=(1,)),project_id=first['project_id'],new_revision=True).json()
    assert second['status']=='imported',second
    assert second['shot_ids']==[first['shot_ids'][1]] and second['changed_shot_ids']==[]
    assert second['revision_diff']['removed_members']==[first['shot_ids'][0]]
    overview=client.get('/api/studio/projects/'+first['project_id']).json()
    assert [s['order'] for s in overview['shots']]==[1]
    with client.app.state.session_factory() as db:
        assert db.get(m.StoryboardShot,first['shot_ids'][0])


def test_word_reordering_preserves_review_and_records_new_default_timeline(client):
    first=upload(client).json();original_snapshot={'order':first['shot_ids'],'source':'unit database snapshot, not a render'}
    with client.app.state.session_factory() as db:
        for n,identifier in enumerate(first['shot_ids'],1):
            db.add(m.StudioDecision(project_id=first['project_id'],shot_id=identifier,action='exclude',position=n,
                trim_start=.1,trim_end=1.2,instruction='保留已有审片意见'))
        assembly=m.Assembly(project_id=first['project_id'],name='Historical unit timeline',timeline_json=original_snapshot)
        db.add(assembly);db.flush()
        render=m.AssemblyRender(assembly_id=assembly.id,version=1,kind='render',status='failed',snapshot_json=original_snapshot)
        db.add(render);db.commit();render_id=render.id
    second=upload(client,rearranged_screenplay(sequence=(1,0)),project_id=first['project_id'],new_revision=True).json()
    assert second['status']=='imported',second
    assert second['shot_ids']==list(reversed(first['shot_ids'])) and second['changed_shot_ids']==[]
    assert second['revision_diff']['order_changed'] and second['revision_diff']['timeline_reordered']
    overview=client.get('/api/studio/projects/'+first['project_id']).json()
    assert [s['id'] for s in overview['shots']]==second['shot_ids']
    assert [s['order'] for s in overview['shots']]==[1,2]
    advanced=client.get('/api/projects/'+first['project_id']+'/shots').json()
    assert [s['order'] for s in advanced]==[1,2] and [s['source_order'] for s in advanced]==[2,1]
    assert [s['decision']['position'] for s in overview['shots']]==[1,2]
    for shot in overview['shots']:
        assert shot['decision']['action']=='exclude' and shot['decision']['revision']==2
        assert shot['decision']['trim_start']==.1 and shot['decision']['trim_end']==1.2
    with client.app.state.session_factory() as db:
        audits=list(db.scalars(select(m.AuditEvent).where(m.AuditEvent.action=='word_revision_order_applied')))
        assert len(audits)==2
        assert db.get(m.AssemblyRender,render_id).snapshot_json==original_snapshot
        assert db.get(m.StoryboardShot,first['shot_ids'][0]).order==1


def test_identical_semantic_shots_are_not_ambiguously_matched(client):
    source=rearranged_screenplay(duplicate_scene=True)
    first=upload(client,source).json()
    second=upload(client,source,project_id=first['project_id'],new_revision=True).json()
    assert first['status']=='imported' and second['status']=='imported'
    assert not set(first['shot_ids'])&set(second['shot_ids'])
    assert second['reused_shot_ids']==[]


def test_independent_scene_environment_change_does_not_invalidate_other_scene(client):
    first=upload(client).json()
    second=upload(client,rearranged_screenplay(changed_weather=True),project_id=first['project_id'],new_revision=True).json()
    assert second['status']=='imported',second
    assert second['shot_ids'][0]!=first['shot_ids'][0] and second['shot_ids'][1]==first['shot_ids'][1]
    assert second['revision_diff']['order_changed'] is False


def test_include_deleted_flag_preserves_legacy_and_explicit_revision_members(client):
    imported=upload(client).json();pid=imported['project_id'];deleted=imported['shot_ids'][0]
    assert client.delete('/api/shots/'+deleted).status_code==200
    assert deleted not in {s['id'] for s in client.get('/api/projects/'+pid+'/shots').json()}
    assert deleted in {s['id'] for s in client.get('/api/projects/'+pid+'/shots?include_deleted=true').json()}
    assert deleted not in {s['id'] for s in client.get('/api/studio/projects/'+pid).json()['shots']}
