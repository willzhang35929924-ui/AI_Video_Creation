"""Isolated SQL/file/adapter-contract tests; NOT evidence of actual AI inference."""
import asyncio
import copy
import hashlib
import io
import base64
from types import SimpleNamespace
import pytest
import httpx
from PIL import Image
from sqlalchemy import select
from app import models as m
from app.models.studio import StudioRun, StudioDecision
from app.core.db import Base, build_engine, build_session_factory
from app.core.errors import AppError
from app.services.cinematic_rules import rules_bundle
from app.services.cinematic_visuals import store_visual
from app.services.character_factory import store_image
from app.services.studio_pipeline import (create_pipeline, advance_pipeline, overview, project_list,
    _failure, _valid_video, _accept_inputs, _temporary_failure, _resource_busy, video_seed, _visual, _overall)


def png(color='#777777'):
    buffer=io.BytesIO();Image.new('RGB',(64,64),color).save(buffer,'PNG');return buffer.getvalue()


@pytest.fixture()
def studio(settings):
    settings.h3_demo_mode=False
    engine=build_engine(settings.database_url);Base.metadata.create_all(engine)
    factory=build_session_factory(engine)
    with factory() as session:
        project=m.Project(name='隔离控制流测试');session.add(project);session.flush()
        rules=rules_bundle()
        script=m.ScriptRevision(project_id=project.id,revision=1,raw_text='隔离 SQL 测试',parsed_json={'versions':rules['versions'],'rules_snapshot':rules})
        session.add(script);session.flush()
        for module in ['character_generation','video_generation']:
            session.add(m.AIService(module=module,name='隔离已探测配置',provider='comfyui',base_url='http://127.0.0.1:8189',is_default=True,
                default_model='flux-2-klein-4b-fp8.safetensors',settings_json={'candidate_model':'sd_xl_base_1.0.safetensors','width':832,'height':1024,'concurrency':1},
                capabilities_json={'text_to_image':{'state':'detected'},'reference_image':{'state':'detected'}}))
        cast=[]
        for i in range(3):
            c=m.Character(slug='isolated_'+str(i),display_name='测试人物'+str(i));session.add(c);session.flush()
            v=m.CharacterVersion(character_id=c.id,version=1,identity_json={'profile':{'age':30+i,'description':'明确成年原创人物，真实皮肤；隔离测试资料'}})
            session.add(v);session.flush();cast.append((c,v))
        scenes=[];shots=[]
        for i in range(2):
            scene=m.Scene(project_id=project.id,script_revision_id=script.id,order=i+1,name='测试空间'+str(i),environment_json={'location':'隔离空间','lighting':'自然光'})
            session.add(scene);session.flush();scenes.append(scene)
        for i in range(3):
            shot=m.StoryboardShot(project_id=project.id,order=i+1,scene_title='控制镜头'+str(i),aspect_ratio='16:9',action='安静观察',visual_prompt='成年人在真实房间静止观察')
            session.add(shot);session.flush();shots.append(shot)
            session.add(m.ShotDetail(shot_id=shot.id,scene_id=scenes[i//2].id,photography_json={'framing':'中景'},source_json={'production':{}}))
            for j,(c,v) in enumerate(cast if i==0 else cast[:1]):
                session.add(m.ShotCharacter(shot_id=shot.id,character_id=c.id,character_version_id=v.id,order=j+1,performance_json={'action':'静止观察'}))
        session.commit()
        yield SimpleNamespace(session=session,factory=factory,settings=settings,project=project,script=script,cast=cast,scenes=scenes,shots=shots,app=SimpleNamespace(state=SimpleNamespace(settings=settings)))
    engine.dispose()


def tick(studio,run):
    asyncio.run(advance_pipeline(studio.app,studio.settings,studio.session,run))


def seed_approved(studio):
    for index,(c,v) in enumerate(studio.cast):
        path=studio.settings.media_root/(c.id+'.png');path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(png((60+index*60,80,90)))
        asset=m.CharacterAsset(character_version_id=v.id,asset_type='front_face',relative_path=path.name,mime_type='image/png',sha256=hashlib.sha256(path.read_bytes()).hexdigest(),status='approved',source='explicit_test_fixture',is_primary_reference=True)
        studio.session.add(asset);studio.session.flush();v.standard_face_asset_id=asset.id;v.approval_state='approved';c.current_approved_version_id=v.id
    studio.session.commit()


def test_dag_stable_scope_double_start_does_not_duplicate_jobs(studio):
    s=studio.session;r=create_pipeline(s,studio.settings,studio.project.id,{});s.commit()
    assert len([n for n in r.nodes_json if n['kind']=='character'])==3
    assert len([n for n in r.nodes_json if n['kind']=='scene'])==2
    first=next(n for n in r.nodes_json if n['id']=='keyframe:'+studio.shots[0].id)
    assert len(first['dependencies'])==4
    assert create_pipeline(s,studio.settings,studio.project.id,{}).id==r.id
    assert s.scalar(select(m.CharacterBatch.id)) is None
    dto=overview(s,studio.project.id)
    assert len(dto['characters'])==3 and len(dto['scenes'])==2 and len(dto['shots'][0]['cast'])==3
    assert dto['run']['steps_json']==dto['run']['nodes_json']
    assert project_list(s)[0]['shot_count']==3


def test_missing_image_service_blocks_dependent_shots_but_video_plan_persists(studio):
    s=studio.session
    service=s.scalar(select(m.AIService).where(m.AIService.module=='character_generation'));service.enabled=False;s.commit()
    r=create_pipeline(s,studio.settings,studio.project.id,{});s.commit();tick(studio,r)
    assert len(overview(s,studio.project.id)['exceptions'])==5
    assert all(n['status']=='waiting_dependencies' for n in r.nodes_json if n['kind'] in {'keyframe','video'})
    assert not s.scalar(select(m.CharacterBatch.id)) and not s.scalar(select(m.ProductionVisual.id))
    # Failed roots keep their downstream DAG alive instead of marking the
    # entire production run finished while dependent shots are still waiting.
    assert r.status=='running'


def test_failed_nodes_never_auto_finish_run_before_requested_outputs_complete():
    run=SimpleNamespace(snapshot_json={'shot_ids':['shot_pending']},result_json={})
    nodes=[
        {'id':'character:c1','kind':'character','target_id':'c1','status':'exception','error':'capability unavailable'},
        {'id':'keyframe:s1','kind':'keyframe','target_id':'shot_pending','status':'waiting_dependencies','error':None},
        {'id':'video:s1','kind':'video','target_id':'shot_pending','status':'waiting_dependencies','error':None},
    ]
    _overall(run,nodes)
    assert run.status=='running'
    # Even when all currently attempted nodes fail, only successful completion
    # or an explicit user pause/cancel may stop automatic execution.
    nodes[1]['status']='failed';nodes[2]['status']='failed'
    _overall(run,nodes)
    assert run.status=='running'


def test_cloud_cost_scope_required_before_any_real_submission(studio):
    s=studio.session
    svc=s.scalar(select(m.AIService).where(m.AIService.module=='character_generation'))
    svc.provider='openai_compatible';svc.base_url='https://example.invalid/v1';s.commit()
    r=create_pipeline(s,studio.settings,studio.project.id,{'allow_cloud_costs':False});s.commit();tick(studio,r)
    assert all(n['code']=='studio_cloud_cost_confirmation' for n in r.nodes_json if n['kind'] in {'character','scene'})
    assert not s.scalar(select(m.CharacterBatch.id)) and not s.scalar(select(m.ProductionVisual.id))


def test_candidate_file_technical_adoption_is_not_formal_library_approval(studio):
    s=studio.session;r=create_pipeline(s,studio.settings,studio.project.id,{});s.commit();tick(studio,r)
    first=next(n for n in r.nodes_json if n['kind']=='character' and n.get('child_id'))
    batch=s.get(m.CharacterBatch,first['child_id']);job=s.scalar(select(m.CharacterGenerationJob).where(m.CharacterGenerationJob.batch_id==batch.id))
    store_image(studio.settings,s,job,png());s.commit();tick(studio,r)
    entry=next(n for n in r.nodes_json if n['id']==first['id']);assert entry['status']=='succeeded'
    c=s.get(m.Character,entry['target_id']);v=s.get(m.CharacterVersion,entry['version_id'])
    assert c.current_approved_version_id is None and v.identity_json['human_identity_approved'] is False
    asset=s.get(m.CharacterAsset,entry['asset_ids'][0]);assert asset.reviewed_by=='automation'
    assert s.scalar(select(m.Approval).where(m.Approval.object_id==v.id)).operator=='automation'
    assert all(p.character_version_id==v.id for p in s.scalars(select(m.ShotCharacter).where(m.ShotCharacter.character_id==c.id)))


def test_existing_formal_identities_reused_and_scene_dependencies_adopt_real_png(studio):
    seed_approved(studio);s=studio.session
    pointers=[c.current_approved_version_id for c,_ in studio.cast]
    r=create_pipeline(s,studio.settings,studio.project.id,{});s.commit();tick(studio,r)
    assert all(n['reused'] for n in r.nodes_json if n['kind']=='character')
    assert not s.scalar(select(m.CharacterBatch.id))
    first=next(n for n in r.nodes_json if n['kind']=='scene' and n.get('child_id'))
    visual=s.get(m.ProductionVisual,first['child_id']);store_visual(s,studio.settings,visual,png());s.commit();tick(studio,r)
    assert visual.status=='approved' and visual.review_json['actor']=='automation'
    assert [c.current_approved_version_id for c,_ in studio.cast]==pointers
    assert len([n for n in r.nodes_json if n['kind']=='scene' and n['status']=='running'])<=1


def test_unknown_interrupted_video_is_never_reposted_and_independent_nodes_continue(studio):
    seed_approved(studio);s=studio.session
    r=create_pipeline(s,studio.settings,studio.project.id,{});s.commit()
    nodes=copy.deepcopy(r.nodes_json)
    for n in nodes:n['status']='succeeded'
    target=next(n for n in nodes if n['kind']=='video');target['status']='dispatching';target['child_id']=None
    r.nodes_json=nodes;s.commit();tick(studio,r)
    node=next(n for n in r.nodes_json if n['id']==target['id'])
    assert node['status']=='unknown' and node['code']=='studio_dispatch_unknown'
    assert not s.scalar(select(m.GenerationJob.id))
    assert all(n['status']=='succeeded' for n in r.nodes_json if n['id']!=target['id'])
    before=copy.deepcopy(r.nodes_json);tick(studio,r);assert r.nodes_json==before


def test_bounded_retry_only_for_definite_temporary_failures():
    entry={'id':'x','attempts':1,'child_id':'actual_job','history':[]}
    _failure(entry,'temporary','HTTP503',transient=True,max_retries=2)
    assert entry['status']=='retrying' and entry['child_id'] is None and entry['history'][0]['child_id']=='actual_job'
    entry.update(attempts=3,child_id='last');_failure(entry,'temporary','HTTP503',transient=True,max_retries=2)
    assert entry['status']=='failed'
    _failure(entry,'unknown','no response',unknown=True,transient=True,max_retries=2);assert entry['status']=='unknown'
    assert _temporary_failure('HTTP 503') and not _temporary_failure('HTTP 401 invalid API key')


def test_corrupt_file_not_adopted_and_unrelated_scene_can_run(studio):
    seed_approved(studio);s=studio.session
    r=create_pipeline(s,studio.settings,studio.project.id,{});s.commit();tick(studio,r)
    first=next(n for n in r.nodes_json if n['kind']=='scene' and n.get('child_id'))
    visual=s.get(m.ProductionVisual,first['child_id']);store_visual(s,studio.settings,visual,png());s.commit()
    (studio.settings.media_root/visual.relative_path).write_bytes(b'corrupt actual file')
    tick(studio,r)
    assert next(n for n in r.nodes_json if n['id']==first['id'])['code']=='studio_image_changed'
    assert any(n['kind']=='scene' and n['status']=='running' and n['id']!=first['id'] for n in r.nodes_json)
    assert visual.status=='needs_review'


def test_final_decisions_and_prior_renders_survive_pipeline_creation(studio):
    s=studio.session;shot=studio.shots[0]
    d=StudioDecision(project_id=studio.project.id,shot_id=shot.id,action='exclude',position=3);s.add(d)
    assembly=m.Assembly(project_id=studio.project.id,name='保留历史版本');s.add(assembly);s.flush()
    render=m.AssemblyRender(assembly_id=assembly.id,version=1,status='failed',snapshot_json={'actual_attempt':True},metadata_json={'exit_code':1},error_message='隔离合成失败记录');s.add(render);s.commit()
    r=create_pipeline(s,studio.settings,studio.project.id,{'shot_ids':[studio.shots[1].id],'force_shot_ids':[studio.shots[1].id]});s.commit()
    dto=overview(s,studio.project.id)
    assert dto['shots'][0]['decision']['action']=='exclude'
    assert dto['renders'][0]['id']==render.id and dto['renders'][0]['metadata_json']['exit_code']==1
    assert r.snapshot_json['shot_ids']==[studio.shots[1].id]
    assert [n['target_id'] for n in r.nodes_json if n['kind']=='video']==[studio.shots[1].id]


def test_video_requires_persisted_decode_check_and_unchanged_bytes(studio):
    s=studio.session;shot=studio.shots[0]
    job=m.GenerationJob(project_id=studio.project.id,shot_id=shot.id,provider='controlled_test_contract',prompt='not actual inference',parameters_json={});s.add(job);s.flush()
    path=studio.settings.media_root/'bad.mp4';path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'not a real video')
    video=m.VideoRevision(project_id=studio.project.id,shot_id=shot.id,generation_job_id=job.id,relative_path='bad.mp4',revision=1,metadata_json={});s.add(video);s.flush()
    with pytest.raises(AppError,match='缺少通过的解码'): _valid_video(s,studio.settings,video)


def test_demo_mode_cannot_generate_even_when_project_is_complete(studio):
    studio.settings.h3_demo_mode=True
    s=studio.session;r=create_pipeline(s,studio.settings,studio.project.id,{});s.commit();tick(studio,r)
    assert all(n['code']=='studio_real_service_required' for n in r.nodes_json if n['kind'] in {'character','scene'})
    assert not s.scalar(select(m.CharacterBatch.id))


@pytest.mark.parametrize('override', [None, '本地完整视频提示词：' + '景' * 16000], ids=['compiled', 'edited-long'])
def test_pipeline_reaches_real_generation_input_boundary_with_durable_dispatch(studio,monkeypatch,override):
    """Provider transport is an explicit contract fixture, not generation proof."""
    from app.services import video_services
    from app.services.ai_services import resolve_service
    from app.services.providers import JobAccepted
    from app.services.cinematic_quality import generation_inputs_hash
    seed_approved(studio);s=studio.session;submitted=[]
    class ContractProvider:
        name='explicit_unit_test_transport'
        async def submit(self,request):
            with studio.factory() as persisted:
                stored=persisted.get(StudioRun,request.parameters['studio_run_id'])
                actual=next(n for n in stored.nodes_json if n['id']==request.parameters['studio_node_id'])
                assert actual['status']=='dispatching' and actual['attempts']==len(submitted)+1
                assert actual['seed']==request.seed==video_seed(stored.id,request.shot_id,actual['attempts'])
            assert request.reference_assets
            if override is None:assert 'subject_definitions:' in request.prompt
            else:assert request.prompt == override
            assert request.parameters['screenplay_snapshot'] if 'screenplay_snapshot' in request.parameters else True
            submitted.append(request)
            return JobAccepted('explicit-contract-fixture-'+request.job_id,'queued',metadata={'workflow':'unit-contract-only'})
    def select_contract(session,settings,project_id,service_id=None,model_id=None):
        service,snapshot=resolve_service(session,'video_generation',service_id,project_id)
        return service,snapshot,ContractProvider()
    monkeypatch.setattr(video_services,'select_video_service',select_contract)
    r=create_pipeline(s,studio.settings,studio.project.id,{'shot_ids':[studio.shots[0].id], 'prompt_overrides':{studio.shots[0].id:override} if override is not None else {}});s.commit()
    for _ in range(6):
        tick(studio,r)
        for visual in s.scalars(select(m.ProductionVisual).where(m.ProductionVisual.status=='queued')):
            store_visual(s,studio.settings,visual,png())
        s.commit()
        if submitted:break
    assert len(submitted)==1
    video_node=next(n for n in r.nodes_json if n['kind']=='video');job=s.get(m.GenerationJob,video_node['child_id'])
    assert job.parameters_json['studio_run_id']==r.id and len(job.reference_assets_json)==5
    assert job.parameters_json['screenplay_snapshot']['cinematic']['preflight']['ready']
    assert job.parameters_json['screenplay_snapshot']['cinematic']['preflight']['generation_inputs_hash']==generation_inputs_hash(s,studio.shots[0])
    assert job.status=='queued' and r.status=='running'
    original_id,original_seed=job.id,job.seed
    assert original_seed==video_seed(r.id,studio.shots[0].id,1)
    tick(studio,r);assert len(submitted)==1
    # A definitive temporary failure creates a new attempt and new seed. The
    # original persisted job keeps its actual seed and remains reproducible.
    job.status='failed';job.error_code='provider_unavailable';job.error_message='HTTP503 definite failure';s.commit()
    tick(studio,r)
    nodes=copy.deepcopy(r.nodes_json);retry=next(n for n in nodes if n['kind']=='video')
    assert retry['status']=='retrying' and retry['history'][-1]['seed']==original_seed
    retry['retry_after']=0;r.nodes_json=nodes;s.commit();tick(studio,r)
    assert len(submitted)==2
    current=next(n for n in r.nodes_json if n['kind']=='video');second=s.get(m.GenerationJob,current['child_id'])
    assert second.id!=original_id and second.seed==video_seed(r.id,studio.shots[0].id,2) and second.seed!=original_seed
    assert s.get(m.GenerationJob,original_id).seed==original_seed
    assert len([n for n in r.nodes_json if n['kind']=='video'])==1  # scoped run never dispatches unrelated shots


def test_video_seed_is_stable_per_run_shot_attempt_and_changes_for_regeneration():
    first=video_seed('studio_first','shot_same',1)
    assert first==video_seed('studio_first','shot_same',1)
    assert first!=video_seed('studio_second','shot_same',1)
    assert first!=video_seed('studio_first','shot_same',2)
    assert first!=video_seed('studio_first','shot_other',1)
    assert all(0<=video_seed('run'+str(i),'same',1)<=2_147_483_647 for i in range(20))


@pytest.mark.parametrize('content',[b'not JSON',b'{"id":null}',b'[]'])
def test_cloud_video_accepted_without_id_is_unknown_not_retryable(studio,monkeypatch,content):
    from app.services import video_services
    calls=[]
    async def transport(*args,**kwargs):
        calls.append(1)
        return httpx.Response(200,content=content,request=httpx.Request('POST','https://example.invalid/video/jobs'))
    monkeypatch.setattr(video_services,'service_request',transport)
    provider=video_services.CloudVideoJobs(studio.settings,{'model_id':'unit-test-contract'},'')
    request=SimpleNamespace(reference_assets=[],job_id='local-correlated-id',prompt='test only',duration_seconds=5,parameters={'width':1024,'height':576},seed=1)
    with pytest.raises(AppError) as error:asyncio.run(provider.submit(request))
    assert error.value.code=='cloud_submission_unknown' and len(calls)==1


@pytest.mark.parametrize('body',[{'data':[]},{'data':[{'b64_json':'invalid base64'}]},{'data':[{'b64_json':base64.b64encode(b'corrupt image bytes').decode()}]}])
def test_cloud_visual_accepted_bad_result_retains_request_id_and_never_reposts(studio,monkeypatch,body):
    from app.services import cinematic_visuals
    seed_approved(studio);s=studio.session
    service=s.scalar(select(m.AIService).where(m.AIService.module=='character_generation'))
    service.provider='openai_compatible';service.base_url='https://example.invalid';s.commit()
    r=create_pipeline(s,studio.settings,studio.project.id,{'allow_cloud_costs':True});s.commit();tick(studio,r)
    calls=[]
    async def transport(*args,**kwargs):
        calls.append(1)
        return httpx.Response(200,json=body,headers={'x-request-id':'actual-contract-request-id'},request=httpx.Request('POST','https://example.invalid/images/generations'))
    monkeypatch.setattr(cinematic_visuals,'service_request',transport)
    asyncio.run(cinematic_visuals.visual_tick(studio.settings,studio.factory))
    asyncio.run(cinematic_visuals.visual_tick(studio.settings,studio.factory))
    s.expire_all()
    visual=s.scalar(select(m.ProductionVisual))
    assert visual.status=='unknown' and visual.provider_job_id=='actual-contract-request-id'
    assert visual.snapshot_json['cloud_post_accepted'] is True and len(calls)==1


def test_cloud_character_accepted_bad_result_is_unknown_and_does_not_retry(studio,monkeypatch):
    from app.services import character_factory
    s=studio.session
    service=s.scalar(select(m.AIService).where(m.AIService.module=='character_generation'))
    service.provider='openai_compatible';service.base_url='https://example.invalid';s.commit()
    r=create_pipeline(s,studio.settings,studio.project.id,{'allow_cloud_costs':True});s.commit();tick(studio,r)
    calls=[]
    async def transport(*args,**kwargs):
        calls.append(1)
        return httpx.Response(200,json={'data':[]},headers={'x-request-id':'actual-character-request'},request=httpx.Request('POST','https://example.invalid/images/generations'))
    monkeypatch.setattr(character_factory,'service_request',transport)
    asyncio.run(character_factory.process_character_jobs(studio.settings,studio.factory))
    asyncio.run(character_factory.process_character_jobs(studio.settings,studio.factory))
    s.expire_all();job=s.scalar(select(m.CharacterGenerationJob))
    assert job.status=='unknown' and job.provider_job_id=='actual-character-request' and len(calls)==1
    tick(studio,r)
    assert next(n for n in r.nodes_json if n.get('child_id')==job.batch_id)['status']=='unknown'


def test_incremental_scene_reuses_same_verified_bytes_without_rewriting_history(studio):
    from app.schemas_cinematic import VisualInput
    from app.services.cinematic_visuals import build_visual
    seed_approved(studio);s=studio.session;old_scene=studio.scenes[0]
    visual=build_visual(s,studio.settings,studio.project.id,VisualInput(kind='scene_reference',scene_id=old_scene.id))
    store_visual(s,studio.settings,visual,png());visual.status='approved';visual.review_json={'operator':'test-human','reason':'isolated fixture'};s.commit()
    original=copy.deepcopy(visual.snapshot_json)
    revised=m.Scene(project_id=studio.project.id,script_revision_id=studio.script.id,order=3,name=old_scene.name,
                    environment_json=copy.deepcopy(old_scene.environment_json),source_json={'reference_scene_id':old_scene.id})
    s.add(revised);s.flush()
    detail=s.scalar(select(m.ShotDetail).where(m.ShotDetail.shot_id==studio.shots[0].id));detail.scene_id=revised.id;s.commit()
    r=create_pipeline(s,studio.settings,studio.project.id,{'shot_ids':[studio.shots[0].id]});s.commit();tick(studio,r)
    scene_node=next(n for n in r.nodes_json if n['kind']=='scene');reused=s.get(m.ProductionVisual,scene_node['child_id'])
    assert scene_node['status']=='succeeded' and reused.id!=visual.id
    assert reused.relative_path==visual.relative_path and reused.sha256==visual.sha256
    assert reused.snapshot_json['reused_from_visual_id']==visual.id and reused.review_json['actor']=='automation'
    assert visual.snapshot_json==original and visual.scene_id==old_scene.id
    registry=s.scalar(select(m.ManagedFile).where(m.ManagedFile.relative_path==visual.relative_path))
    assert {'table':'production_visuals','id':reused.id} in registry.ownership_json


def test_paused_pending_or_finished_child_does_not_deadlock_other_projects(studio):
    s=studio.session;r=create_pipeline(s,studio.settings,studio.project.id,{});s.commit();tick(studio,r)
    entry=next(n for n in r.nodes_json if n['kind']=='character' and n.get('child_id'))
    batch=s.get(m.CharacterBatch,entry['child_id']);job=s.scalar(select(m.CharacterGenerationJob).where(m.CharacterGenerationJob.batch_id==batch.id))
    assert _resource_busy(s,[entry])
    batch.paused=True;s.flush();assert not _resource_busy(s,[entry])
    job.status='running';s.flush();assert _resource_busy(s,[entry])
    job.status='needs_review';s.flush();assert not _resource_busy(s,[entry])


def test_deleted_shot_is_not_revived_by_automation_input_acceptance(studio):
    s=studio.session;shot=studio.shots[0];shot.status='deleted';s.commit()
    r=create_pipeline(s,studio.settings,studio.project.id,{});s.commit()
    with pytest.raises(AppError) as exc:_accept_inputs(s,studio.settings,r,shot)
    assert exc.value.code=='studio_shot_deleted' and shot.status=='deleted'
    assert studio.script.status=='draft' and not s.scalar(select(m.Approval.id))


def test_deleted_during_keyframe_only_blocks_related_nodes_other_shots_continue(studio):
    seed_approved(studio);s=studio.session
    r=create_pipeline(s,studio.settings,studio.project.id,{});s.commit()
    target=studio.shots[0];target.status='deleted';s.commit();tick(studio,r)
    for kind in ['keyframe','video']:
        entry=next(n for n in r.nodes_json if n['id']==kind+':'+target.id)
        assert entry['status']=='exception' and entry['code']=='studio_shot_deleted' and not entry['child_id']
    for _ in range(4):
        for visual in s.scalars(select(m.ProductionVisual).where(m.ProductionVisual.status=='queued')):
            store_visual(s,studio.settings,visual,png())
        s.commit();tick(studio,r)
        if any(n['kind']=='keyframe' and n['target_id']!=target.id and n.get('child_id') for n in r.nodes_json):break
    assert any(n['kind']=='keyframe' and n['target_id']!=target.id and n.get('child_id') for n in r.nodes_json)
    assert target.status=='deleted' and not s.scalar(select(m.GenerationJob.id).where(m.GenerationJob.shot_id==target.id))
    assert not s.scalar(select(m.ProductionVisual.id).where(m.ProductionVisual.shot_id==target.id))


def test_deleted_in_other_transaction_is_detected_before_identity_map_can_revive(studio):
    s=studio.session;shot=studio.shots[0];r=create_pipeline(s,studio.settings,studio.project.id,{});s.commit()
    with studio.factory() as other:
        removed=other.get(m.StoryboardShot,shot.id);removed.status='deleted';other.commit()
    assert shot.status=='draft'  # deliberately stale ORM identity map
    with pytest.raises(AppError) as exc:_accept_inputs(s,studio.settings,r,shot)
    assert exc.value.code=='studio_shot_deleted'
    s.refresh(shot);assert shot.status=='deleted'


@pytest.mark.parametrize('render_status,expected',[('queued','rendering'),('running','rendering'),('generated','completed'),('failed','completed_with_errors'),('cancelled','completed_with_errors')])
def test_composition_tracks_actual_render_without_reverting_to_shot_review(studio,render_status,expected):
    s=studio.session;r=create_pipeline(s,studio.settings,studio.project.id,{});s.commit()
    assembly=m.Assembly(project_id=studio.project.id,name='isolated render tracking');s.add(assembly);s.flush()
    render=m.AssemblyRender(assembly_id=assembly.id,version=1,status=render_status,snapshot_json={},relative_path='real-output-relative-path.mp4' if render_status=='generated' else None,error_message='actual isolated failure receipt' if render_status in {'failed','cancelled'} else None)
    s.add(render);s.flush();r.result_json={'assembly_id':assembly.id,'render_id':render.id};r.status='rendering';s.commit()
    original=copy.deepcopy(r.nodes_json);tick(studio,r)
    assert r.status==expected and r.nodes_json==original
    if render_status=='generated':assert r.result_json['relative_path']==render.relative_path
    if render_status in {'failed','cancelled'}:assert r.error_message==render.error_message


def extra_identity(studio,character,number,*,approved=True,description='Explicit isolated identity variant'):
    s=studio.session;v=m.CharacterVersion(character_id=character.id,version=number,approval_state='approved' if approved else 'draft',
        identity_json={'profile':{'age':38,'description':description,'facial_features':'原创成年男性，方脸'}})
    s.add(v);s.flush()
    if not approved:return v,None
    path=studio.settings.media_root/(v.id+'.png');path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(png())
    a=m.CharacterAsset(character_version_id=v.id,asset_type='front_face',relative_path=path.name,mime_type='image/png',sha256=hashlib.sha256(path.read_bytes()).hexdigest(),status='approved',is_primary_reference=True,source='isolated_explicit_version_fixture')
    s.add(a);s.flush();v.standard_face_asset_id=a.id
    return v,a


def primary_participants(studio):
    return [studio.session.scalar(select(m.ShotCharacter).where(m.ShotCharacter.shot_id==shot.id,m.ShotCharacter.character_id==studio.cast[0][0].id)) for shot in studio.shots]


def test_global_formal_pointer_change_never_replaces_explicit_shot_identity(studio):
    seed_approved(studio);s=studio.session;c,original=studio.cast[0]
    people=primary_participants(studio)
    for p in people:p.reference_asset_ids_json=[original.standard_face_asset_id]
    later,asset=extra_identity(studio,c,2);c.current_approved_version_id=later.id;s.commit()
    before=[(p.id,p.character_version_id,list(p.reference_asset_ids_json),copy.deepcopy(p.source_json)) for p in people]
    r=create_pipeline(s,studio.settings,studio.project.id,{'shot_ids':[studio.shots[0].id],'force_shot_ids':[studio.shots[0].id]});s.commit();tick(studio,r)
    assert [(p.id,p.character_version_id,list(p.reference_asset_ids_json),p.source_json) for p in people]==before
    entry=next(n for n in r.nodes_json if n['id']=='character:'+c.id)
    assert entry['reused'] and entry['version_id']==original.id
    assert not s.scalar(select(m.CharacterBatch.id))


def test_same_character_explicit_different_versions_remain_separate(studio):
    seed_approved(studio);s=studio.session;c,first=studio.cast[0];second,asset=extra_identity(studio,c,2)
    people=primary_participants(studio)
    people[0].reference_asset_ids_json=[first.standard_face_asset_id]
    for p in people[1:]:p.character_version_id=second.id;p.reference_asset_ids_json=[asset.id]
    s.commit();before=[(p.character_version_id,list(p.reference_asset_ids_json)) for p in people]
    r=create_pipeline(s,studio.settings,studio.project.id,{});s.commit();tick(studio,r)
    assert [(p.character_version_id,list(p.reference_asset_ids_json)) for p in people]==before
    entry=next(n for n in r.nodes_json if n['id']=='character:'+c.id)
    assert entry['status']=='succeeded' and entry['version_id'] is None
    assert {b['version_id'] for b in entry['bindings']}=={first.id,second.id}


def test_partial_reference_gaps_fill_only_their_own_identity_or_unbound_default(studio):
    seed_approved(studio);s=studio.session;c,first=studio.cast[0];second,asset=extra_identity(studio,c,2)
    c.current_approved_version_id=second.id;people=primary_participants(studio)
    people[0].reference_asset_ids_json=[first.standard_face_asset_id]
    people[1].reference_asset_ids_json=[]  # explicit approved v1: only fill v1 anchor
    people[2].character_version_id=None;people[2].reference_asset_ids_json=[]
    s.commit();r=create_pipeline(s,studio.settings,studio.project.id,{});s.commit();tick(studio,r)
    assert people[0].character_version_id==first.id and people[0].reference_asset_ids_json==[first.standard_face_asset_id]
    assert people[1].character_version_id==first.id and people[1].reference_asset_ids_json==[first.standard_face_asset_id]
    assert people[2].character_version_id==second.id and people[2].reference_asset_ids_json==[asset.id]
    assert not s.scalar(select(m.CharacterBatch.id))


def test_explicit_draft_profile_generates_only_its_gap_preserving_other_versions(studio):
    seed_approved(studio);s=studio.session;c,first=studio.cast[0];second,asset=extra_identity(studio,c,2)
    draft,_=extra_identity(studio,c,3,approved=False,description='Explicit selected draft appearance; never global latest override')
    unrelated,_=extra_identity(studio,c,4,approved=False,description='Unrelated later library draft must not be selected')
    c.current_approved_version_id=second.id;people=primary_participants(studio)
    people[0].reference_asset_ids_json=[first.standard_face_asset_id]
    people[1].character_version_id=draft.id;people[1].reference_asset_ids_json=[]
    people[2].character_version_id=second.id;people[2].reference_asset_ids_json=[asset.id]
    s.commit();r=create_pipeline(s,studio.settings,studio.project.id,{});s.commit();tick(studio,r)
    batch=s.scalar(select(m.CharacterBatch));assert batch and batch.snapshot_json['profile']['description']==draft.identity_json['profile']['description']
    assert people[0].character_version_id==first.id and people[2].character_version_id==second.id
    job=s.scalar(select(m.CharacterGenerationJob).where(m.CharacterGenerationJob.batch_id==batch.id))
    store_image(studio.settings,s,job,png());s.commit();tick(studio,r)
    assert people[1].character_version_id==batch.character_version_id
    assert people[0].character_version_id==first.id and people[0].reference_asset_ids_json==[first.standard_face_asset_id]
    assert people[2].character_version_id==second.id and people[2].reference_asset_ids_json==[asset.id]
    assert c.current_approved_version_id==second.id


def test_conflicting_explicit_drafts_stop_without_merging_or_overwriting_valid_binding(studio):
    seed_approved(studio);s=studio.session;c,first=studio.cast[0]
    draft_a,_=extra_identity(studio,c,2,approved=False,description='First explicitly selected draft')
    draft_b,_=extra_identity(studio,c,3,approved=False,description='Different explicitly selected draft')
    people=primary_participants(studio);people[0].reference_asset_ids_json=[first.standard_face_asset_id]
    people[1].character_version_id=draft_a.id;people[2].character_version_id=draft_b.id
    people[1].reference_asset_ids_json=[];people[2].reference_asset_ids_json=[]
    s.commit();r=create_pipeline(s,studio.settings,studio.project.id,{});s.commit();tick(studio,r)
    entry=next(n for n in r.nodes_json if n['id']=='character:'+c.id)
    assert entry['status']=='exception' and entry['code']=='studio_identity_draft_conflict'
    assert people[0].character_version_id==first.id and people[1].character_version_id==draft_a.id and people[2].character_version_id==draft_b.id
    assert not s.scalar(select(m.CharacterBatch.id))


def existing_visuals(studio):
    """Actual PNG records as explicit isolated cache inputs, not AI outputs."""
    from app.schemas_cinematic import VisualInput
    from app.services.cinematic_visuals import build_visual
    seed_approved(studio);s=studio.session
    for p in s.scalars(select(m.ShotCharacter)):
        p.reference_asset_ids_json=[s.get(m.CharacterVersion,p.character_version_id).standard_face_asset_id]
    s.flush()
    scene=build_visual(s,studio.settings,studio.project.id,VisualInput(kind='scene_reference',scene_id=studio.scenes[0].id))
    store_visual(s,studio.settings,scene,png());scene.status='approved';s.flush()
    frames=[]
    for shot in studio.shots[:2]:
        visual=build_visual(s,studio.settings,studio.project.id,VisualInput(kind='keyframe',shot_id=shot.id))
        store_visual(s,studio.settings,visual,png());visual.status='approved';frames.append(visual)
    s.commit();return scene,frames


def test_modify_instruction_reaches_actual_keyframe_prompt_without_reusing_plain_frame(studio):
    from app.services.cinematic_rules import fingerprint
    s=studio.session;scene,frames=existing_visuals(studio);old=frames[0];other=frames[1]
    before=(copy.deepcopy(old.snapshot_json),old.prompt,old.sha256,copy.deepcopy(other.snapshot_json),other.prompt,other.sha256,copy.deepcopy(studio.scenes[0].environment_json))
    instruction='只把工作夹克改为深蓝色棉质，并把桌上的记录本保持合上'
    r=create_pipeline(s,studio.settings,studio.project.id,{'shot_ids':[studio.shots[0].id],'force_shot_ids':[studio.shots[0].id],
        'instructions':{studio.shots[0].id:instruction}});s.commit()
    entry=next(n for n in r.nodes_json if n['kind']=='keyframe')
    _visual(s,studio.settings,r,entry,True);s.flush()
    changed=s.get(m.ProductionVisual,entry['child_id'])
    assert changed.id!=old.id and changed.status=='queued'
    assert instruction in changed.prompt and 'CURRENT CHANGE FOR THIS SHOT ONLY' in changed.prompt
    assert 'standard photographic production rules' in changed.prompt and 'live-action motion picture' in changed.prompt
    assert len(changed.snapshot_json['references'])==4  # three identities plus original approved scene
    assert changed.snapshot_json['studio_modification']['instruction_sha256']==fingerprint({'instruction':instruction})
    assert changed.snapshot_json['studio_modification']['run_id']==r.id
    assert changed.snapshot_json['seed']==entry['seed']==video_seed('keyframe:'+r.id,studio.shots[0].id,1)
    assert (old.snapshot_json,old.prompt,old.sha256,other.snapshot_json,other.prompt,other.sha256,studio.scenes[0].environment_json)==before
    assert [v.id for v in s.scalars(select(m.ProductionVisual).where(m.ProductionVisual.kind=='scene_reference'))]==[scene.id]
    assert not s.scalar(select(m.CharacterBatch.id))
    assert len([n for n in r.nodes_json if n['kind']=='video'])==1


def test_empty_regenerate_instruction_reuses_original_frame_and_ignores_modified_cache(studio):
    from app.services.cinematic_rules import fingerprint
    s=studio.session;scene,frames=existing_visuals(studio);plain=frames[0]
    # A newer modified frame must not replace the unmodified cache variant.
    modified=m.ProductionVisual(project_id=plain.project_id,scene_id=plain.scene_id,shot_id=plain.shot_id,kind='keyframe',
        status='approved',prompt=plain.prompt+' changed coat',relative_path=plain.relative_path,sha256=plain.sha256,
        snapshot_json={**plain.snapshot_json,'studio_modification':{'instruction':'change coat','instruction_sha256':fingerprint({'instruction':'change coat'})}})
    s.add(modified);s.commit()
    count=s.scalar(select(__import__('sqlalchemy').func.count()).select_from(m.ProductionVisual))
    r=create_pipeline(s,studio.settings,studio.project.id,{'shot_ids':[studio.shots[0].id],'force_shot_ids':[studio.shots[0].id],'instructions':{studio.shots[0].id:''}});s.commit()
    entry=next(n for n in r.nodes_json if n['kind']=='keyframe');_visual(s,studio.settings,r,entry,True);s.flush()
    assert entry['child_id']==plain.id and entry['reused'] and entry['status']=='succeeded'
    assert s.scalar(select(__import__('sqlalchemy').func.count()).select_from(m.ProductionVisual))==count
    assert not s.scalar(select(m.CharacterBatch.id))


@pytest.mark.parametrize('previous_override, requested_override, reused', [
    ('edited prompt', None, False), (None, 'edited prompt', False),
    ('edited prompt', 'edited prompt', True), (None, None, True)])
def test_video_reuse_requires_the_same_edited_prompt(studio, monkeypatch, previous_override, requested_override, reused):
    from app.services import studio_pipeline as pipeline
    shot = studio.shots[0]
    session = studio.session
    job = m.GenerationJob(project_id=studio.project.id, shot_id=shot.id, provider='contract-fixture',
        status='generated', prompt='fixture body', parameters_json={'prompt_override':previous_override,
            'screenplay_snapshot':{'cinematic':{'preflight':{'generation_inputs_hash':'same-inputs'}}}})
    session.add(job);session.flush()
    video = m.VideoRevision(project_id=studio.project.id, shot_id=shot.id, generation_job_id=job.id,
        relative_path='not-read-in-this-state-test.mp4', revision=1)
    session.add(video);session.flush()
    run = SimpleNamespace(id='run-test', snapshot_json={'options':{
        'prompt_overrides':{shot.id:requested_override} if requested_override is not None else {}}})
    entry = {'id':'video:'+shot.id, 'target_id':shot.id, 'status':'ready', 'attempts':0}
    monkeypatch.setattr(pipeline, '_assert_shot_live', lambda *args: None)
    monkeypatch.setattr(pipeline, '_accept_inputs', lambda *args: None)
    monkeypatch.setattr(pipeline, 'generation_inputs_hash', lambda *args: 'same-inputs')
    monkeypatch.setattr(pipeline, '_matching_route_output', lambda *args: True)
    monkeypatch.setattr(pipeline, 'generation_parameters', lambda *args: {})
    monkeypatch.setattr(pipeline, '_valid_video', lambda *args: None)
    asyncio.run(pipeline._video(studio.app, session, studio.settings, run, entry, False))
    assert entry.get('reused', False) is reused
    assert (entry.get('child_id') == job.id) is reused
