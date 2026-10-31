"""Destructive lifecycle contracts for every real business resource.

Rows are isolated database fixtures, never claimed as model-generation evidence.
"""
import hashlib
import math
import struct
import subprocess
import wave
from functools import lru_cache
from io import BytesIO

import pytest
from PIL import Image
from app import models as m
from app.core.config import Settings
from app.services.content_manager import RESOURCES
from test_workspace import delete


NEW_ASSET_RESOURCES={'products','product_images','product_project_bindings','product_shot_bindings',
    'reference_videos','video_analyses','style_profiles','voice_library'}


@lru_cache(maxsize=1)
def neutral_video_bytes():
    """Real local two-frame MP4, solely a file-lifecycle fixture, never model evidence."""
    result=subprocess.run([Settings.from_env().ffmpeg_path,'-nostdin','-v','error','-f','lavfi',
        '-i','color=c=gray:s=64x64:r=4:d=0.5','-an','-c:v','libx264','-threads','1',
        '-pix_fmt','yuv420p','-movflags','frag_keyframe+empty_moov','-f','mp4','pipe:1'],
        capture_output=True,timeout=30,shell=False)
    assert result.returncode==0,result.stderr.decode('utf-8','replace')
    assert b'ftyp' in result.stdout[:32]
    return result.stdout


def fixture_row(session,resource,suffix,media_root):
    def add(obj):session.add(obj);session.flush();return obj
    def project():return add(m.Project(name='隔离管理测试 '+suffix))
    def character():return add(m.Character(slug='fixture_'+suffix,display_name='隔离人物 '+suffix))
    def version():return add(m.CharacterVersion(character_id=character().id,version=1))
    def shot():return add(m.StoryboardShot(project_id=project().id,order=1))
    def service():return add(m.AIService(name='隔离服务 '+suffix,module='video_generation',provider='comfyui_h3'))
    def participant():return add(m.ShotCharacter(shot_id=shot().id,character_id=character().id))
    def image_batch():
        v=version();return add(m.CharacterBatch(character_id=v.character_id,character_version_id=v.id,kind='standard_face',status='failed',snapshot_json={'isolated_database_fixture':True}))
    def video_job():
        s=shot();return add(m.GenerationJob(project_id=s.project_id,shot_id=s.id,provider='isolated_database_fixture',status='failed',prompt='非生成结果，仅用于删除接口测试'))
    def assembly():return add(m.Assembly(project_id=project().id,name='隔离时间线 '+suffix))
    def account():return add(m.PublishingAccount(platform='douyin',display_name='隔离账号 '+suffix))
    def screenplay_job():
        return add(m.ScreenplayGenerationJob(idempotency_key='management_fixture_'+suffix,request_sha256='0'*64,
            project_name='隔离剧本管理记录 '+suffix,input_mode='text',creative_prompt='仅数据库管理夹具，不调用模型',
            story_text='非生成结果，仅用于备注与删除接口测试',status='failed',stage='failed',
            snapshot_json={'isolated_database_fixture':True},receipt_json={'model_called':False}))
    def owned_file(owner,relative,data):
        path=media_root/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
        digest=hashlib.sha256(data).hexdigest()
        add(m.ManagedFile(relative_path=relative,state='active',ownership_json=[{'table':owner.__table__.name,'id':owner.id}],
            metadata_json={'verified_owned':True,'sha256':digest,'isolated_lifecycle_fixture':True,'model_called':False}))
        return digest
    def product():return add(m.Product(name='隔离产品 '+suffix,category='管理测试'))
    def product_image(parent=None):
        parent=parent or product();buffer=BytesIO();Image.new('RGB',(32,24),(100,120,140)).save(buffer,format='PNG');data=buffer.getvalue()
        asset=add(m.ScreenplayInputAsset(input_mode='image',filename='neutral_'+suffix+'.png',relative_path='',sha256=hashlib.sha256(data).hexdigest(),
            metadata_json={'library_owner':'product_library','width':32,'height':24,'isolated_lifecycle_fixture':True,'model_called':False},source_note='本地创建的中性管理夹具，非模型输出'))
        asset.relative_path=f'product_library/{parent.id}/{asset.id}/original.png';owned_file(asset,asset.relative_path,data)
        image=add(m.ProductImage(product_id=parent.id,asset_id=asset.id,purpose='main',order=1))
        image.thumbnail_relative_path=f'product_library/{parent.id}/{image.id}/thumbnail.png';owned_file(image,image.thumbnail_relative_path,data)
        parent.cover_image_id=image.id
        return image
    def reference_video():
        data=neutral_video_bytes()
        video=add(m.ReferenceVideo(name='中性参考视频 '+suffix,filename='neutral_'+suffix+'.mp4',relative_path='',sha256=hashlib.sha256(data).hexdigest(),
            source_note='本地FFmpeg灰色双帧管理夹具；没有调用模型',metadata_json={'duration':.5,'width':64,'height':64,'isolated_lifecycle_fixture':True,'model_called':False}))
        video.relative_path=f'reference_videos/{video.id}/original.mp4';owned_file(video,video.relative_path,data)
        return video
    def video_analysis(video=None):
        video=video or reference_video()
        return add(m.VideoAnalysis(video_id=video.id,version=1,idempotency_key='management_analysis_'+suffix,
            request_sha256=hashlib.sha256(suffix.encode()).hexdigest(),status='failed',stage='failed',
            receipt_json={'isolated_database_fixture':True,'model_called':False},error_message='管理生命周期夹具，未执行分析'))
    if resource=='projects':return project()
    if resource=='scripts':return add(m.ScriptRevision(project_id=project().id,revision=1,raw_text='原始隔离测试文本'))
    if resource=='scenes':return add(m.Scene(project_id=project().id,name='隔离场景'))
    if resource=='shots':return shot()
    if resource=='participants':return participant()
    if resource=='dialogues':
        p=participant();return add(m.DialogueLine(shot_id=p.shot_id,participant_id=p.id,text='隔离对白'))
    if resource=='audio':return add(m.AudioEvent(shot_id=shot().id,kind='sfx',description='隔离音频记录'))
    if resource=='characters':return character()
    if resource=='versions':return version()
    if resource=='assets':return add(m.CharacterAsset(character_version_id=version().id,asset_type='front_face',relative_path='fixtures/'+suffix+'.png',mime_type='image/png',sha256='isolated_fixture',source='upload'))
    if resource=='image_batches':return image_batch()
    if resource=='image_jobs':return add(m.CharacterGenerationJob(batch_id=image_batch().id,status='failed',asset_type='front_face',prompt='隔离接口测试',seed=1,snapshot_json={'isolated_database_fixture':True}))
    if resource=='video_jobs':return video_job()
    if resource=='videos':
        j=video_job();return add(m.VideoRevision(project_id=j.project_id,shot_id=j.shot_id,generation_job_id=j.id,relative_path='fixtures/'+suffix+'.mp4',revision=1))
    if resource=='assemblies':return assembly()
    if resource=='renders':return add(m.AssemblyRender(assembly_id=assembly().id,version=1,status='failed',snapshot_json={'isolated_database_fixture':True}))
    if resource=='compositions':return add(m.FinalComposition(project_id=project().id,video_ids_json=[],status='failed'))
    if resource=='imports':return add(m.WordImport(filename='isolated.docx',sha256=suffix,relative_path='fixtures/'+suffix+'.docx',status='failed'))
    if resource=='sources':return add(m.SourceDocument(project_id=project().id,filename='isolated.docx',original_text='原始来源',sha256=suffix))
    if resource=='templates':return add(m.ContentTemplate(name='隔离模板 '+suffix,body='自然语言'))
    if resource=='services':return service()
    if resource=='bindings':return add(m.ProjectServiceBinding(project_id=project().id,module='video_generation',service_id=service().id))
    if resource=='accounts':return account()
    if resource=='presets':return add(m.PublishingPreset(account_id=account().id,name='隔离发布预设 '+suffix))
    if resource=='publish_batches':return add(m.PublishBatch(status='validated',dry_run=True))
    if resource=='files':return add(m.ManagedFile(relative_path='fixtures/'+suffix+'.bin',metadata_json={'verified_owned':True}))
    if resource=='production_entities':return add(m.ProductionEntity(project_id=project().id,logical_id=suffix,kind='prop',name='隔离道具版本'))
    if resource=='production_visuals':return add(m.ProductionVisual(project_id=project().id,kind='scene_reference',status='failed',prompt='隔离数据库夹具，不代表生成输出'))
    if resource=='production_runs':
        p=project();script=add(m.ScriptRevision(project_id=p.id,revision=1,raw_text='隔离数据库夹具'))
        return add(m.ProductionRun(project_id=p.id,script_revision_id=script.id,status='failed'))
    if resource=='studio_runs':return add(m.StudioRun(project_id=project().id,status='failed'))
    if resource=='studio_decisions':
        sh=shot();return add(m.StudioDecision(project_id=sh.project_id,shot_id=sh.id,position=1))
    if resource=='screenplay_jobs':return screenplay_job()
    if resource=='screenplay_documents':
        return add(m.ScreenplayDocument(job_id=screenplay_job().id,version=1,
            relative_path='fixtures/'+suffix+'.docx',sha256='0'*64,screenplay_sha256='0'*64,
            versions_json={'isolated_database_fixture':True},review_state='pending'))
    if resource=='screenplay_inputs':
        return add(m.ScreenplayInputAsset(input_mode='image',filename='isolated_'+suffix+'.png',
            relative_path='fixtures/'+suffix+'.png',sha256='0'*64,
            metadata_json={'isolated_database_fixture':True,'actual_file_created':False},source_note='仅数据库管理夹具'))
    if resource=='character_references':
        return add(m.CharacterReferenceAsset(project_id=project().id,character_id=character().id,
            relative_path='fixtures/'+suffix+'.png',sha256='0'*64,mime_type='image/png',
            width=64,height=64,byte_size=0,purpose='identity_front',order=1,state='draft',
            source_note='仅数据库管理夹具，未上传或生成实际人物图片'))
    if resource=='character_reference_sets':
        return add(m.CharacterReferenceSet(project_id=project().id,character_id=character().id,revision=0))
    if resource=='products':
        parent=product();product_image(parent);return parent
    if resource=='product_images':return product_image()
    if resource=='product_project_bindings':
        return add(m.ProjectProductBinding(project_id=project().id,product_id=product().id,snapshot_json={'isolated_database_fixture':True}))
    if resource=='product_shot_bindings':
        sh=shot();return add(m.ShotProductBinding(project_id=sh.project_id,shot_id=sh.id,product_id=product().id,state_json={'isolated_database_fixture':True}))
    if resource=='reference_videos':return reference_video()
    if resource=='video_analyses':return video_analysis()
    if resource=='style_profiles':
        video=reference_video();analysis=video_analysis(video)
        return add(m.StyleProfile(name='管理风格档案 '+suffix,video_ids_json=[video.id],analysis_ids_json=[analysis.id],primary_video_id=video.id,
            profile_json={'isolated_relational_fixture':True,'analysis_performed':False,'model_called':False}))
    if resource=='voice_library':
        buffer=BytesIO();frequency=200+int(suffix.rsplit('_',1)[-1])
        with wave.open(buffer,'wb') as output:
            output.setnchannels(1);output.setsampwidth(2);output.setframerate(16000)
            output.writeframes(b''.join(struct.pack('<h',round(3000*math.sin(i*2*math.pi*frequency/16000))) for i in range(8000)))
        data=buffer.getvalue();digest=hashlib.sha256(data).hexdigest()
        voice=add(m.VoiceLibraryAsset(name='中性音频管理夹具 '+suffix,language='und',relative_path='',sha256=digest,file_size=len(data),
            metadata_json={'actual':{'duration':.5,'sample_rate':16000,'channels':1,'codec':'pcm_s16le','full_decode':True},
                'reference_authorized':False,'isolated_lifecycle_fixture':True,'signal':'synthetic oscillator; no voice or TTS claim'}))
        voice.relative_path=f'voice_library/{voice.id}/original.wav';owned_file(voice,voice.relative_path,data)
        voice.preview_relative_path=f'voice_library/{voice.id}/preview.wav';owned_file(voice,voice.preview_relative_path,data)
        return voice
    raise AssertionError('Missing fixture '+resource)


@pytest.mark.parametrize('count',[1,2],ids=['single','bulk'])
@pytest.mark.parametrize('resource',list(RESOURCES))
def test_every_business_resource_lists_edits_notes_and_deletes_only_selected(client,settings,resource,count):
    with client.app.state.session_factory() as s:
        rows=[fixture_row(s,resource,resource+'_'+str(i),settings.media_root) for i in range(3)]
        ids=[obj.id for obj in rows];s.commit()
    response=client.patch('/api/content/'+resource+'/'+ids[0],json={'notes':'可持久化的管理备注'})
    assert response.status_code==200,response.text
    listed=client.get('/api/content/'+resource,params={'q':ids[0]}).json()
    assert listed['total']==1 and listed['items'][0]['note']['notes']=='可持久化的管理备注'
    if resource in NEW_ASSET_RESOURCES:
        editable={'products':('name','修改产品名称'),'product_images':('purpose','detail'),
            'reference_videos':('name','修改参考视频名称'),'style_profiles':('name','修改风格档案名称'),
            'voice_library':('name','修改音频名称')}
        if resource in editable:
            field,value=editable[resource]
            updated=client.patch('/api/content/'+resource+'/'+ids[0],json={field:value})
            assert updated.status_code==200 and updated.json()[field]==value,updated.text
        else:
            # Bindings and accepted analysis identities remain immutable in the
            # generic editor; their human notes are editable and persisted above.
            protected=client.patch('/api/content/'+resource+'/'+ids[0],json={'id':'different-id'})
            assert protected.status_code==422,protected.text
    original_files={path.relative_to(settings.media_root).as_posix():hashlib.sha256(path.read_bytes()).hexdigest()
        for path in settings.media_root.rglob('*') if path.is_file()}
    mode=['keep','trash','permanent'][list(RESOURCES).index(resource)%3]
    plan,response=delete(client,resource,ids[:count],mode,cascade=resource in NEW_ASSET_RESOURCES)
    assert response.status_code==200 and response.json()['status']=='completed',response.text
    assert response.json()['result']['records_deleted']>=count
    with client.app.state.session_factory() as s:
        model=RESOURCES[resource][0]
        assert all(s.get(model,key) is None for key in ids[:count])
        assert s.get(model,ids[2]) is not None
    assert client.get('/api/content/'+resource,params={'q':ids[0]}).json()['total']==0
    removed={item['path'] for item in plan['files'] if not item['protected'] and mode!='keep'}
    for relative,digest in original_files.items():
        path=settings.media_root/relative
        if relative in removed:
            assert not path.exists(),relative
        else:
            assert path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest()==digest,relative
