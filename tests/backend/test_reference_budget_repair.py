"""Real compiler/media/SQLite checks; isolated repair transport never runs a model."""
import asyncio
import copy
import hashlib
from io import BytesIO
from types import SimpleNamespace

from PIL import Image
import pytest
from sqlalchemy import func, select

from app import models as m
from app.core.errors import AppError
from app.schemas_cinematic import VisualInput
from app.services import cinematic_visuals, gpu_gate, studio_pipeline
from app.services.ai_services import service_snapshot
from app.services.cinematic_rules import rules_bundle


def png(index):
    stream=BytesIO()
    Image.new('RGB',(96,96),(20+index*13,40+index*7,80+index*5)).save(stream,'PNG')
    return stream.getvalue()


def source_file(settings,index):
    settings.media_root.mkdir(parents=True,exist_ok=True)
    path=settings.media_root/f'budget-source-{index}.png'
    path.write_bytes(png(index))
    return path,hashlib.sha256(path.read_bytes()).hexdigest()


def setup_shot(client,settings,actor_count=3,*,model_id=None,node_status='exception',node_code='product_visual_reference_limit',child_id=None,run_status='completed_with_errors'):
    """One current shot, unique real actor photos, one scene and one product."""
    settings.h3_demo_mode=False
    with client.app.state.session_factory() as session:
        project=m.Project(name='Isolated reference-budget repair')
        session.add(project);session.flush()
        rules=rules_bundle()
        script=m.ScriptRevision(project_id=project.id,revision=1,status='approved',raw_text='Isolated ordinary group product shot',
            parsed_json={'versions':rules['versions'],'rules_snapshot':rules})
        session.add(script);session.flush()
        scene=m.Scene(project_id=project.id,script_revision_id=script.id,order=1,name='Budget reference room',
            environment_json={'location':'ordinary room','lighting':'window light'})
        session.add(scene);session.flush()
        shot=m.StoryboardShot(project_id=project.id,order=1,status='approved',scene_title='Group holds fixed product',
            aspect_ratio='16:9',duration_seconds=5,visual_prompt='The independently referenced people stand beside the fixed product.',
            action='Everyone remains still.')
        session.add(shot);session.flush()
        script.parsed_json={**script.parsed_json,'explicit_shot_ids':[shot.id]}
        detail=m.ShotDetail(shot_id=shot.id,scene_id=scene.id,source_json={'production':{}})
        session.add(detail)
        paths=[];assets=[]
        for index in range(actor_count):
            character=m.Character(slug=f'budget-person-{index}',display_name=f'Performer {index}')
            session.add(character);session.flush()
            version=m.CharacterVersion(character_id=character.id,version=1,approval_state='approved',
                identity_json={'profile':{'age':30+index,'description':f'Distinct performer {index}'}})
            session.add(version);session.flush()
            path,digest=source_file(settings,index)
            face=m.CharacterAsset(character_version_id=version.id,asset_type='front_face',relative_path=path.name,
                mime_type='image/png',sha256=digest,width=96,height=96,status='approved',is_primary_reference=True)
            session.add(face);session.flush()
            version.standard_face_asset_id=face.id;character.current_approved_version_id=version.id
            session.add(m.ShotCharacter(shot_id=shot.id,character_id=character.id,character_version_id=version.id,
                order=index+1,reference_asset_ids_json=[face.id],performance_json={'position':'center','action':'stands still'}))
            paths.append(path);assets.append(face.id)
        scene_path,scene_digest=source_file(settings,actor_count)
        scene_visual=m.ProductionVisual(project_id=project.id,scene_id=scene.id,kind='scene_reference',status='approved',
            prompt='An ordinary room reference',relative_path=scene_path.name,sha256=scene_digest,
            snapshot_json={'visual_inputs':{'environment':scene.environment_json}})
        session.add(scene_visual);session.flush()
        product=m.Product(name='Fixed real product')
        session.add(product);session.flush()
        product_path,product_digest=source_file(settings,actor_count+1)
        source=m.ScreenplayInputAsset(input_mode='image',filename=product_path.name,relative_path=product_path.name,
            sha256=product_digest,metadata_json={'width':96,'height':96,'bytes':product_path.stat().st_size})
        session.add(source);session.flush()
        picture=m.ProductImage(product_id=product.id,asset_id=source.id,purpose='main',order=1)
        session.add(picture);session.flush()
        session.add(m.ProjectProductBinding(project_id=project.id,product_id=product.id,snapshot_json={
            'id':product.id,'name':product.name,'images':[{'id':picture.id,'asset_id':source.id,'relative_path':product_path.name,
                'sha256':product_digest,'purpose':'main','order':1}]}))
        session.add(m.ShotProductBinding(project_id=project.id,shot_id=shot.id,product_id=product.id,
            image_ids_json=[picture.id],state_json={'keyframe_image_ids':[picture.id],'visible_state':'closed box on table'}))
        service=session.scalar(select(m.AIService).where(m.AIService.module=='character_generation'))
        assert service is not None
        service.provider='comfyui';service.enabled=True;service.default_model='flux-2-klein-4b-fp8.safetensors'
        service.settings_json={**service.settings_json,'text_encoder':'qwen_3_4b.safetensors','vae':'flux2-vae.safetensors'}
        service.capabilities_json={'reference_image':{'state':'detected','source':'native ReferenceLatent + installed selected models'},
            'text_to_image':{'state':'detected','source':'installed selected models'}}
        session.flush();captured=service_snapshot(service,model_id or service.default_model)
        route={'slot':'keyframe_image','configured':True,'profile':{'scope':'project','target_id':project.id,'slot':'keyframe_image',
            'revision':1,'settings':{'parameters':{}}},'selected':{'service_snapshot':captured},'allow_cloud_costs':False,'allow_paid_fallback':False}
        entry={'id':'keyframe:'+shot.id,'kind':'keyframe','target_id':shot.id,'dependencies':[],
            'status':node_status,'child_id':child_id,'attempts':1,'history':[],
            'code':node_code,'error':'Old fixed four-image budget','route':route}
        run=m.StudioRun(project_id=project.id,script_revision_id=script.id,status=run_status,nodes_json=[entry],
            snapshot_json={'shot_ids':[shot.id],'services':{'image':captured},'routes_by_shot':{shot.id:{'keyframe_image':route}},
                'options':{'width':1024,'height':576,'allow_cloud_costs':False,'allow_paid_fallback':False,'max_retries':0,'instructions':{}}})
        session.add(run);session.commit()
        return SimpleNamespace(project_id=project.id,shot_id=shot.id,scene_id=scene.id,scene_visual_id=scene_visual.id,
            service_id=service.id,run_id=run.id,actor_asset_ids=assets,actor_paths=paths,product_path=product_path,
            scene_path=scene_path,product_image_id=picture.id)


def compile_visual(client,settings,fixture,*,model_id=None):
    with client.app.state.session_factory() as session:
        transaction=session.begin_nested()
        visual=cinematic_visuals.build_visual(session,settings,fixture.project_id,
            VisualInput(kind='keyframe',shot_id=fixture.shot_id,service_id=fixture.service_id,model_id=model_id))
        snapshot=copy.deepcopy(visual.snapshot_json)
        graph=cinematic_visuals.visual_graph(visual,[f'photo-{index}.png' for index in range(len(snapshot['references']))])
        transaction.rollback()
        return snapshot,graph


def patched_repair(monkeypatch,*,gpu='idle'):
    calls=[]
    async def state():return {'state':gpu,'reason':'isolated_'+gpu}
    async def advance(_app,_settings,_session,run):
        calls.append({'status':run.status,'nodes':copy.deepcopy(run.nodes_json)})
    async def forbidden(*args,**kwargs):raise AssertionError('Isolated repair must never submit inference')
    monkeypatch.setattr(gpu_gate,'owned_model_status',state)
    monkeypatch.setattr(studio_pipeline,'advance_pipeline',advance)
    monkeypatch.setattr(cinematic_visuals,'service_request',forbidden)
    return calls


def test_native_five_reference_keyframe_preserves_every_source_and_both_conditioning_chains(client,settings):
    f=setup_shot(client,settings)
    snapshot,graph=compile_visual(client,settings,f)
    refs=snapshot['references']
    assert snapshot['reference_budget']['inference_verified'] is False
    assert [ref['role'] for ref in refs]==['identity','identity','identity','scene','product']
    assert [ref['picture_index'] for ref in refs]==list(range(1,6))
    assert [ref['asset_id'] for ref in refs[:3]]==f.actor_asset_ids
    assert refs[-1]['asset_id']==f.product_image_id
    assert snapshot['product_reference_selection']['actual']==[f.product_image_id]
    assert snapshot['product_reference_selection']['excluded']==[]
    assert len([node for node in graph.values() if node['class_type']=='LoadImage'])==5
    for polarity in ('positive','negative'):
        link=graph['63']['inputs'][polarity];chain=[]
        while graph[link[0]]['class_type']=='ReferenceLatent':
            current=graph[link[0]];chain.append(current['inputs']['latent'][0]);link=current['inputs']['conditioning']
        assert len(chain)==5 and len(set(chain))==5


def test_native_reference_budget_still_rejects_over_nine_without_dropping_photos(client,settings):
    f=setup_shot(client,settings,actor_count=8)
    with pytest.raises(AppError) as error:compile_visual(client,settings,f)
    assert error.value.code in {'product_visual_reference_limit','visual_reference_limit'}
    with client.app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(m.ProductionVisual))==1


def test_native_reference_budget_accepts_exactly_nine_without_dropping_photos(client,settings):
    f=setup_shot(client,settings,actor_count=7)
    snapshot,graph=compile_visual(client,settings,f)
    assert len(snapshot['references'])==9
    assert len([node for node in graph.values() if node['class_type']=='LoadImage'])==9
    assert snapshot['reference_budget']['limit']==9
    assert snapshot['reference_budget']['reference_count']==9


def test_unknown_local_model_does_not_inherit_klein_expanded_budget(client,settings):
    f=setup_shot(client,settings)
    with pytest.raises(AppError):compile_visual(client,settings,f,model_id='unverified-local-model.safetensors')


def test_decoded_native_output_records_only_five_proven_references_not_the_adapter_ceiling(client,settings):
    f=setup_shot(client,settings)
    with client.app.state.session_factory() as session:
        visual=cinematic_visuals.build_visual(session,settings,f.project_id,
            VisualInput(kind='keyframe',shot_id=f.shot_id,service_id=f.service_id))
        session.flush();visual.provider_job_id='isolated-decoded-provider-receipt'
        cinematic_visuals.store_visual(session,settings,visual,png(17));session.flush()
        service=session.get(m.AIService,f.service_id)
        proof=service.capabilities_json['reference_image']
        assert proof['state']=='verified' and proof['max_reference_images']==5
        assert proof['model_id']=='flux-2-klein-4b-fp8.safetensors'
        assert proof['visual_id']==visual.id and proof['provider_job_id']==visual.provider_job_id
        assert visual.snapshot_json['reference_budget']['inference_verified'] is True
        assert visual.status=='needs_review'


@pytest.mark.parametrize('status',['running','completed_with_errors'])
@pytest.mark.parametrize('code',['product_visual_reference_limit','visual_reference_limit'])
@pytest.mark.parametrize('node_status',['exception','failed'])
def test_one_click_repair_dry_compiles_then_requeues_only_the_failed_keyframe(client,settings,monkeypatch,status,code,node_status):
    calls=patched_repair(monkeypatch)
    f=setup_shot(client,settings,run_status=status,node_code=code,node_status=node_status)
    response=client.post(f'/api/studio/projects/{f.project_id}/repair',json={})
    assert response.status_code==200,response.text
    result=response.json()
    assert calls and calls[0]['nodes'][0]['status']=='pending'
    assert calls[0]['nodes'][0]['child_id'] is None
    assert calls[0]['nodes'][0]['attempts']==1 and calls[0]['nodes'][0]['code'] is None
    assert calls[0]['nodes'][0]['history'][-1]['code']==code
    action=next(action for action in result['actions'] if action['code']=='keyframe_reference_nodes_requeued')
    assert action['status']=='applied' and action['node_ids']==['keyframe:'+f.shot_id]
    diagnostic=action['references'][0]
    assert diagnostic['node_id']=='keyframe:'+f.shot_id
    assert diagnostic['reference_count']==5 and len(diagnostic['reference_ids'])==5
    assert diagnostic['model_id']=='flux-2-klein-4b-fp8.safetensors'
    assert diagnostic['service_id']==f.service_id
    assert not any(blocker['code']=='product_visual_reference_limit' for blocker in result['blockers'])
    with client.app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(m.ProductionVisual))==1,'Preview must roll back its temporary queued visual'
        run=session.get(m.StudioRun,f.run_id)
        assert run.nodes_json[0]['status']=='pending'
        assert run.snapshot_json['options']['allow_cloud_costs'] is False


@pytest.mark.parametrize('gpu',['busy','unknown'])
def test_repair_keeps_budget_failure_when_gpu_not_confirmed_idle(client,settings,monkeypatch,gpu):
    calls=patched_repair(monkeypatch,gpu=gpu)
    f=setup_shot(client,settings)
    response=client.post(f'/api/studio/projects/{f.project_id}/repair',json={})
    assert response.status_code==200,response.text
    assert not calls
    with client.app.state.session_factory() as session:assert session.get(m.StudioRun,f.run_id).nodes_json[0]['status']=='exception'


@pytest.mark.parametrize('status,child',[('unknown',None),('submitted',None),('running',None),('exception','original-submission')])
def test_repair_never_resubmits_unknown_or_existing_child_budget_requests(client,settings,monkeypatch,status,child):
    calls=patched_repair(monkeypatch)
    f=setup_shot(client,settings,node_status=status,child_id=child)
    response=client.post(f'/api/studio/projects/{f.project_id}/repair',json={})
    assert response.status_code==200,response.text
    assert not calls
    with client.app.state.session_factory() as session:
        node=session.get(m.StudioRun,f.run_id).nodes_json[0]
        assert node['status']==status and node['child_id']==child


@pytest.mark.parametrize('status',['paused','completed','cancelled','waiting_review'])
def test_repair_respects_paused_and_finished_run_controls(client,settings,monkeypatch,status):
    calls=patched_repair(monkeypatch)
    f=setup_shot(client,settings,run_status=status)
    response=client.post(f'/api/studio/projects/{f.project_id}/repair',json={})
    assert response.status_code==200,response.text
    assert not calls
    with client.app.state.session_factory() as session:assert session.get(m.StudioRun,f.run_id).status==status


@pytest.mark.parametrize('mutation',['revision','model','disabled','capability_missing','capability_wrong_model'])
def test_repair_refuses_stale_or_unsupported_current_service(client,settings,monkeypatch,mutation):
    calls=patched_repair(monkeypatch)
    f=setup_shot(client,settings,model_id='unverified-local-model.safetensors' if mutation=='model' else None)
    if mutation!='model':
        with client.app.state.session_factory() as session:
            service=session.get(m.AIService,f.service_id)
            if mutation=='revision':service.revision+=1
            elif mutation=='disabled':service.enabled=False
            elif mutation=='capability_missing':service.capabilities_json={}
            else:service.capabilities_json={'reference_image':{'state':'verified','model_id':'another-model.safetensors','max_reference_images':9}}
            session.commit()
    response=client.post(f'/api/studio/projects/{f.project_id}/repair',json={})
    assert response.status_code==200,response.text
    assert not calls
    with client.app.state.session_factory() as session:assert session.get(m.StudioRun,f.run_id).nodes_json[0]['status']=='exception'


@pytest.mark.parametrize('source',['actor','product','scene'])
@pytest.mark.parametrize('damage',['corrupt','missing'])
def test_repair_does_not_requeue_when_any_reference_source_is_unusable(client,settings,monkeypatch,source,damage):
    calls=patched_repair(monkeypatch)
    f=setup_shot(client,settings)
    path={'actor':f.actor_paths[0],'product':f.product_path,'scene':f.scene_path}[source]
    if damage=='missing':path.unlink()  # Only the isolated pytest source is removed.
    else:path.write_bytes(b'not an image')
    response=client.post(f'/api/studio/projects/{f.project_id}/repair',json={})
    assert response.status_code==200,response.text
    assert not calls
    with client.app.state.session_factory() as session:
        assert session.get(m.StudioRun,f.run_id).nodes_json[0]['status']=='exception'
        assert session.scalar(select(func.count()).select_from(m.ProductionVisual))==1


def gpu_wait_commits_concurrent_update(client,monkeypatch,fixture,update):
    """The API has loaded its run when its awaited GPU probe yields to this write."""
    updates=[]
    async def concurrent_gpu_probe():
        await asyncio.sleep(0)
        with client.app.state.session_factory() as background:
            run=background.get(m.StudioRun,fixture.run_id)
            update(background,run)
            background.commit()
            updates.append(True)
        return {'state':'idle','reason':'isolated_idle_after_concurrent_commit'}
    monkeypatch.setattr(gpu_gate,'owned_model_status',concurrent_gpu_probe)
    return updates


def test_repair_preserves_other_node_submission_committed_during_gpu_wait(client,settings,monkeypatch):
    calls=patched_repair(monkeypatch)
    f=setup_shot(client,settings)
    other_id='scene:'+f.scene_id
    child_id='visual_concurrently_submitted_scene'
    with client.app.state.session_factory() as session:
        run=session.get(m.StudioRun,f.run_id)
        run.nodes_json=[*run.nodes_json,{'id':other_id,'kind':'scene','target_id':f.scene_id,'scene_id':f.scene_id,
            'status':'pending','child_id':None,'attempts':0,'dependencies':[],'history':[],'code':None,'error':None}]
        session.commit()
    def submit_other(session,run):
        session.add(m.ProductionVisual(id=child_id,project_id=f.project_id,scene_id=f.scene_id,
            kind='scene_reference',status='submitted',prompt='Concurrent durable scene request',
            provider_job_id='existing-provider-scene-receipt',snapshot_json={'service_snapshot':{'provider':'comfyui','is_cloud':False}}))
        nodes=copy.deepcopy(run.nodes_json)
        nodes[1].update(status='running',child_id=child_id,attempts=1)
        run.nodes_json=nodes
    updates=gpu_wait_commits_concurrent_update(client,monkeypatch,f,submit_other)
    response=client.post(f'/api/studio/projects/{f.project_id}/repair',json={})
    assert response.status_code==200,response.text
    assert updates==[True] and calls
    advanced={node['id']:node for node in calls[0]['nodes']}
    assert advanced['keyframe:'+f.shot_id]['status']=='pending'
    assert advanced[other_id]['status']=='running' and advanced[other_id]['child_id']==child_id
    with client.app.state.session_factory() as session:
        nodes={node['id']:node for node in session.get(m.StudioRun,f.run_id).nodes_json}
        assert nodes[other_id]['child_id']==child_id and nodes[other_id]['attempts']==1
        assert session.get(m.ProductionVisual,child_id).status=='submitted'
        assert session.scalar(select(func.count()).select_from(m.ProductionVisual))==2


def test_repair_respects_pause_committed_during_gpu_wait(client,settings,monkeypatch):
    calls=patched_repair(monkeypatch)
    f=setup_shot(client,settings)
    def pause(_session,run):run.status='paused'
    updates=gpu_wait_commits_concurrent_update(client,monkeypatch,f,pause)
    response=client.post(f'/api/studio/projects/{f.project_id}/repair',json={})
    assert response.status_code==200,response.text
    assert updates==[True] and not calls
    assert response.json()['run_status']=='paused'
    assert not any(action['status']=='applied' for action in response.json()['actions'])
    with client.app.state.session_factory() as session:
        run=session.get(m.StudioRun,f.run_id)
        assert run.status=='paused'
        assert run.nodes_json[0]['status']=='exception' and run.nodes_json[0]['child_id'] is None


def test_repair_rechecks_product_reference_change_committed_during_gpu_wait(client,settings,monkeypatch):
    calls=patched_repair(monkeypatch)
    f=setup_shot(client,settings)
    def changed(_session,run):run.snapshot_json={**run.snapshot_json,'product_references_changed':True}
    updates=gpu_wait_commits_concurrent_update(client,monkeypatch,f,changed)
    response=client.post(f'/api/studio/projects/{f.project_id}/repair',json={})
    assert response.status_code==409,response.text
    assert 'studio_product_inputs_changed' in response.text
    assert updates==[True] and not calls
    with client.app.state.session_factory() as session:
        run=session.get(m.StudioRun,f.run_id)
        assert run.snapshot_json['product_references_changed'] is True
        assert run.nodes_json[0]['status']=='exception' and run.nodes_json[0]['child_id'] is None
        assert session.scalar(select(func.count()).select_from(m.ProductionVisual))==1


def test_repair_preserves_its_own_unknown_child_committed_during_gpu_wait(client,settings,monkeypatch):
    calls=patched_repair(monkeypatch)
    f=setup_shot(client,settings)
    child_id='visual_concurrent_unknown_keyframe'
    def submission_became_unknown(session,run):
        session.add(m.ProductionVisual(id=child_id,project_id=f.project_id,shot_id=f.shot_id,kind='keyframe',
            status='unknown',prompt='Durable request awaiting provider reconciliation',provider_job_id='original-provider-receipt'))
        nodes=copy.deepcopy(run.nodes_json)
        nodes[0].update(status='unknown',child_id=child_id,code='visual_unknown',error='Original submission outcome unknown')
        run.nodes_json=nodes
    updates=gpu_wait_commits_concurrent_update(client,monkeypatch,f,submission_became_unknown)
    response=client.post(f'/api/studio/projects/{f.project_id}/repair',json={})
    assert response.status_code==200,response.text
    assert updates==[True] and not calls
    assert not any(action['status']=='applied' for action in response.json()['actions'])
    with client.app.state.session_factory() as session:
        node=session.get(m.StudioRun,f.run_id).nodes_json[0]
        assert node['status']=='unknown' and node['child_id']==child_id and node['code']=='visual_unknown'
        assert node['error']=='Original submission outcome unknown' and node['history']==[]
        assert session.get(m.ProductionVisual,child_id).provider_job_id=='original-provider-receipt'
