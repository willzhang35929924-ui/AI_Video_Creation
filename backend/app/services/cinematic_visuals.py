"""Actual local and compatible cloud image queue.

Multiple identity references are separate ReferenceLatent conditioning inputs,
never a fabricated exact first-frame or pixel identity guarantee.
"""
import asyncio
import base64
import copy
import hashlib
import io
import json
import time
from types import SimpleNamespace
from PIL import Image
from sqlalchemy import select
from app import models as m
from app.core.config import PROJECT_ROOT
from app.core.errors import AppError
from app.core.secrets import redact
from app.core.paths import safe_media_path,media_relative
from app.services.ai_services import resolve_service,service_request
from app.services.character_factory import native_graph,credential_for,klein_text2image_graph,qwen_image_body
from app.services.cinematic_rules import rules_bundle,fingerprint
from app.services.cinematic_quality import visual_inputs,production_context,physical_directions
from app.services.screenplay import get,row,shot_bundle
from app.services.identity_references import actor_references,reference_manifest,actor_visual_description,english_position,english_action


def native_visual_reference_limit(snapshot, capabilities):
    """Native Flux2 chains latents; four was an application guard, not a node limit.

    Nine is this pipeline's bounded input budget, not a claim that nine-image
    inference or likeness has been verified. Decoded results record actual counts.
    Unknown models/capabilities retain their previous conservative budget.
    """
    capability=(capabilities or {}).get('reference_image') or {}
    if (snapshot.get('provider')=='comfyui'
            and snapshot.get('model_id')=='flux-2-klein-4b-fp8.safetensors'
            and capability.get('state') in {'detected','verified'}
            and capability.get('model_id') in {None,'',snapshot['model_id']}):
        return 9
    return 4


def build_visual(session,settings,project_id,payload,uploaded=False):
    get(session,m.Project,project_id)
    if settings.h3_demo_mode and not uploaded:raise AppError(409,'real_visual_service_required','视觉制作只提交真实任务，不用Demo替代')
    shot=get(session,m.StoryboardShot,payload.shot_id) if payload.shot_id else None
    scene=get(session,m.Scene,payload.scene_id) if payload.scene_id else None
    entity=get(session,m.ProductionEntity,payload.entity_id) if payload.entity_id else None
    if any(o.project_id!=project_id for o in [shot,scene,entity] if o):raise AppError(422,'visual_project_mismatch','视觉任务关联记录不属于同一项目')
    if payload.kind=='keyframe' and not shot:raise AppError(422,'keyframe_shot_required','关键画面必须属于具体镜头')
    if payload.kind=='scene_reference' and not scene:raise AppError(422,'scene_required','场景参考必须关联场景')
    rules=rules_bundle();references=[];context={};definitions=[];performer_names={};actors=[];language_coverage=[];product_reference_selection={}
    if uploaded:snapshot={'provider':'user_upload'}
    else:service,snapshot=resolve_service(session,'character_generation',payload.service_id,project_id=project_id,model_id=payload.model_id)
    native_limit=native_visual_reference_limit(snapshot,service.capabilities_json) if not uploaded and snapshot['provider']=='comfyui' else None
    if shot:
        detail,linked_scene,script,production,entities=production_context(session,shot);scene=scene or linked_scene
        rules=script.parsed_json.get('rules_snapshot') or rules if script else rules
        context=visual_inputs(session,shot)
        if not uploaded:
            participants=shot_bundle(session,shot.id)['participants']
            actors,references=actor_references(session,settings,participants,first_only=True)
        names={a['display_name']:f'performer {a["subject_index"]}' for a in actors}
        for actor in actors:
            index=actor['subject_index'];performer_names[actor['display_name']]=index
            description,coverage=actor_visual_description(actor)
            position,position_covered=english_position(actor['screen_position'])
            action,action_covered=english_action(actor['performance'].get('action',''),names)
            state=production.get('wardrobe_states',{}).get(actor['actor_id'],{})
            entity=next((e for e in entities if e and e.id==state.get('entity_id')),None)
            wardrobe=entity.spec_json if entity else actor['performance'].get('wardrobe') or actor['profile'].get('wardrobes',[])
            definitions.append(f'Reference image {actor["picture_indices"][0]} defines only performer {index}: {description}. Screen position: {position}. Independent action: {action or "no additional action specified"}. Preserve this performer independently from all other references. Their wardrobe only: '+json.dumps(wardrobe,ensure_ascii=False))
            language_coverage.append({'actor_id':actor['actor_id'],'identity':coverage,'position_covered':position_covered,'action_covered':action_covered})
        if scene and not uploaded:
            candidates=session.scalars(select(m.ProductionVisual).where(m.ProductionVisual.scene_id==scene.id,m.ProductionVisual.kind=='scene_reference',m.ProductionVisual.status=='approved').order_by(m.ProductionVisual.created_at.desc()))
            scene_reference=next((v for v in candidates if v.entity_id==production.get('scene_state_id') and v.snapshot_json.get('visual_inputs',{}).get('environment')==scene.environment_json),None)
            if scene_reference:
                references.append({'asset_id':scene_reference.id,'sha256':scene_reference.sha256,'relative_path':scene_reference.relative_path,'production_visual_id':scene_reference.id,'role':'scene','picture_index':len(references)+1})
                definitions.append(f'Reference image {len(references)} defines the room layout, practical light sources and material appearance only.'+(' Keep all performer identities from the earlier person references.' if actors else ' The location remains unoccupied.'))
        from app.services.product_library import shot_references
        product_refs=shot_references(session,settings,shot,stage='keyframe') if not uploaded else []
        if product_refs and not uploaded:
            budget=max(0,native_limit-len(references)) if snapshot['provider']=='comfyui' else len(product_refs)
            ordered=product_refs
            if not budget:raise AppError(422,'product_visual_reference_limit','本镜人物与场景已占满图像模型参考预算，无法实际输入产品图；请减少人物参考或选择支持更多图的已验证服务')
            if len(ordered)>budget:raise AppError(422,'product_visual_reference_limit','产品图超出本镜图像模型剩余预算，请在项目制作设置的产品参考图中明确改选；不会自动截断',{'selected':len(ordered),'remaining_budget':budget,'shot_id':shot.id})
            selected=ordered
            product_reference_selection={'available':[r['id'] for r in ordered],'actual':[r['id'] for r in selected],
                'excluded':[],'selection_policy':'使用本镜明确保存的关键画面参考选择；不自动截断'}
            for product in selected:
                references.append({**product,'asset_id':product['id'],'role':'product','picture_index':len(references)+1})
                definitions.append(f"Reference image {len(references)} defines only product {product['product_id']} ({product['product_name']}). Preserve its shape, proportions, color and packaging. These photos are the appearance authority over conflicting generic descriptions. Follow the shot for actions and open/closed state; do not copy incidental background people or hands. "+("Never transfer product features onto a person's face. " if actors else "Display the product alone, without people or hands. ")+"Logo and packaging text require human checking or compositing from the original image.")
    elif scene:
        script=session.get(m.ScriptRevision,scene.script_revision_id) if scene.script_revision_id else None
        if script:rules=script.parsed_json.get('rules_snapshot') or rules
        context={'scene_id':scene.id,'environment':scene.environment_json,'entity':{'id':entity.id,'version':entity.version,'spec':entity.spec_json} if entity else None}
    elif entity:context={'entity':{'id':entity.id,'version':entity.version,'spec':entity.spec_json}}
    else:raise AppError(422,'visual_context_required','请关联镜头、场景或造型')
    if entity and entity.scene_id and scene and entity.scene_id!=scene.id:raise AppError(422,'visual_scene_mismatch','场景状态不属于此场景')
    if payload.kind=='outfit_reference':
        from app.services.screenplay import approved_reference_ids
        if not entity or entity.kind!='wardrobe' or not entity.character_id:raise AppError(422,'wardrobe_owner_required','造型参考必须有明确的人物和服装版本')
        character=get(session,m.Character,entity.character_id);version=get(session,m.CharacterVersion,character.current_approved_version_id) if character.current_approved_version_id else None
        refs=approved_reference_ids(session,version)
        if not refs:raise AppError(409,'wardrobe_identity_unreviewed','该人物尚无可用正式标准脸')
        asset=get(session,m.CharacterAsset,refs[0]);references=[{'asset_id':asset.id,'sha256':asset.sha256,'relative_path':asset.relative_path,'character_id':character.id,'character_version_id':version.id}]
        definitions=[f'Reference image 1 defines the facial identity of {character.display_name}; change only the separately specified wardrobe.']
        from app.services.character_reference import saved_reference_snapshot
        project_references, reference_revision = saved_reference_snapshot(session, settings, project_id, character.id)
        for source in project_references:
            references.append({**source, 'asset_id': source['id']})
            definitions.append(f"Reference image {len(references)} belongs only to {character.display_name}; its purpose is {source['purpose']}. Preserve the approved face from image 1; use wardrobe references only for clothing and the other saved views for this same person's appearance.")
        context = {**context, 'project_character_reference_revision': reference_revision,
                   'project_character_reference_ids': [r['id'] for r in project_references]}
    if uploaded:snapshot={'provider':'user_upload'}
    else:
        if snapshot['provider']=='comfyui' and (payload.width%32 or payload.height%32 or max(payload.width,payload.height)>1344):raise AppError(422,'local_visual_dimensions','本地Klein适配器宽高需32的倍数且不超过1344；云端按接口实际支持尺寸配置')
        if snapshot['provider']=='comfyui' and len(references)>native_limit:raise AppError(422,'visual_reference_limit',f'当前本地图像模型／工作流的输入预算为{native_limit}张，实际选择{len(references)}张；请调整选择或使用支持更多参考的服务',{'limit':native_limit,'reference_count':len(references)})
        if snapshot['provider']=='openai_compatible':
            if snapshot['settings'].get('image_api_format')=='qwen_generations':
                maximum={'qwen-image-3.0':3,'qwen-image-3.0-pro':3,'qwen-image-2.1-pro':10}.get(snapshot['model_id'])
                if maximum is None:raise AppError(422,'qwen_image_model_unsupported','请选择千问图像模型；语言模型无法生成参考图')
                if len(references)>maximum:raise AppError(422,'qwen_image_reference_limit',f'所选千问模型最多接受 {maximum} 张参考图；请调整选择，系统不会丢弃参考图')
            capability=service.capabilities_json.get('reference_image' if references else 'text_to_image',{})
            verified=capability.get('state')=='verified' and capability.get('model_id')==snapshot['model_id'] and (not references or capability.get('max_reference_images',1)>=len(references))
            if not verified and not payload.confirm_cloud_test:raise AppError(409,'cloud_visual_single_validation_required','云端图像接口或本次多图数量尚未真实验证；请在参考图区明确提交一张验证图，实际解码成功后编排才继续，不会自动批量计费')
            if payload.confirm_cloud_test and any(v.snapshot_json.get('service_snapshot',{}).get('service_id')==service.id for v in session.scalars(select(m.ProductionVisual).where(m.ProductionVisual.status.in_(['queued','submitting','submitted','running','unknown'])))):raise AppError(409,'cloud_visual_probe_active','该云端服务已有未结束或结果未知的视觉任务，请先核对原请求，避免重复计费')
        elif snapshot['provider']!='comfyui':raise AppError(409,'visual_adapter_unavailable','此配置不是已实现的图片适配器')
    from app.services.cinematic_rules import generation_rules
    rules=generation_rules(rules)
    product_only=bool(shot and not actors and any(r.get('role')=='product' for r in references))
    language_keys=['scene','camera'] if payload.kind=='scene_reference' or product_only else ['identity','wardrobe','scene','action','camera']
    role='EMPTY, UNOCCUPIED LOCATION SCOUTING PHOTOGRAPH. Absolutely no people, human silhouettes, faces or hands; show only the usable location. ' if payload.kind=='scene_reference' else 'Show only the explicitly referenced performers in this shot. '
    if product_only:role='PRODUCT-ONLY LIVE-ACTION ADVERTISEMENT. Show the referenced product alone on the specified surface. Absolutely no people, faces, hands, human silhouettes, actors or mannequins. '
    contact=physical_directions(context,performer_names) if payload.kind=='keyframe' else ''
    if shot:
        # Keep the full source/IDs in the snapshot, but render only visual facts
        # for the image encoder; DB metadata is not a scene description.
        facts={k:context[k] for k in ['visual_prompt','camera_move','lighting','photography','environment']}
        if product_only:facts['product_presentation']=[p.get('state',{}) for p in context.get('products',[])]
        else:
            facts['wardrobe_and_materials']=[{'kind':e['kind'],'design':e['logical_id'],'spec':e['spec']} for e in context['entities'] if e['kind']=='wardrobe']
            facts['physical_states']=context['production'].get('wardrobe_states',{})
    else:facts=context
    prompt=role+'A single eye-level photograph from a live-action motion picture. Show the actual location in photographic perspective with real materials and motivated practical light. Never a floorplan, blueprint, schematic, wireframe or collage. '+payload.kind.replace('_',' ')+'. No panels, text or watermark.\n'+contact+'\nExact scene and current shot:\n'+json.dumps(facts,ensure_ascii=False,default=str)+'\n'+'\n'.join(definitions)+'\n'+'\n'.join(rules['visual_language'][k] for k in language_keys)+'\nAdditional reviewed direction: '+payload.instructions+'\n'+contact
    references=[{k:v for k,v in r.items() if k!='absolute_path'} for r in references]
    snapshot_data={'source':'user_upload' if uploaded else 'model_generation','service_snapshot':snapshot,'rules_snapshot':rules,'versions':rules['versions'],'references':references,'reference_manifest':reference_manifest(actors,references,stage='keyframe_image' if shot else payload.kind),'language_coverage':language_coverage,'visual_inputs':context,'inputs_hash':fingerprint(context),'parameters':{'width':payload.width,'height':payload.height,'quality':'production'},'seed':payload.seed,'workflow_path':None if uploaded else 'workflows/character_klein_reference_v1.json','reference_semantics':'appearance and scene guidance; global reference latents have no regional face-lock guarantee','image_mode':'user_upload' if uploaded else 'reference_to_image' if references else 'text_to_image'}
    snapshot_data['product_reference_selection']=product_reference_selection
    if native_limit is not None:
        snapshot_data['reference_budget']={'limit':native_limit,'reference_count':len(references),
            'basis':'native Flux2 ReferenceLatent chain with detected selected model and nodes' if native_limit==9 else 'conservative unverified-model budget',
            'inference_verified':False}
    snapshot_data['product_only']=product_only
    if not uploaded and snapshot['provider']=='openai_compatible':
        snapshot_data['workflow_path']='compatible-images-v1'
        snapshot_data['request_contract_hash']=fingerprint({'model':snapshot['model_id'],'prompt':prompt,'parameters':snapshot_data['parameters'],'references':references,'format':snapshot['settings'].get('image_api_format','multipart')})
    visual=m.ProductionVisual(project_id=project_id,scene_id=scene.id if scene else None,shot_id=shot.id if shot else None,entity_id=entity.id if entity else None,kind=payload.kind,prompt=prompt,snapshot_json=snapshot_data)
    session.add(visual);session.flush();return visual


def visual_graph(visual,uploaded):
    job=SimpleNamespace(id=visual.id,prompt=visual.prompt,negative_prompt='illustration, animation, anime, CGI, 3D render, plastic skin, doll, glamour retouch, watermark, collage',seed=visual.snapshot_json['seed'],snapshot_json=visual.snapshot_json)
    if visual.snapshot_json.get('product_only'):job.negative_prompt+=', person, people, face, human, hand, actor, mannequin'
    if uploaded:graph=native_graph(job,uploaded[0])
    else:graph=klein_text2image_graph(job)
    for index,filename in enumerate(uploaded[1:],1):
        base='film_ref_'+str(index)
        graph[base]={'class_type':'LoadImage','inputs':{'image':filename}}
        graph[base+'_scale']={'class_type':'ImageScaleToTotalPixels','inputs':{'image':[base,0],'upscale_method':'nearest-exact','megapixels':.8,'resolution_steps':1}}
        graph[base+'_encode']={'class_type':'VAEEncode','inputs':{'pixels':[base+'_scale',0],'vae':['72',0]}}
        old_positive=graph['63']['inputs']['positive'];old_negative=graph['63']['inputs']['negative']
        graph[base+'_positive']={'class_type':'ReferenceLatent','inputs':{'conditioning':old_positive,'latent':[base+'_encode',0]}}
        graph[base+'_negative']={'class_type':'ReferenceLatent','inputs':{'conditioning':old_negative,'latent':[base+'_encode',0]}}
        graph['63']['inputs']['positive']=[base+'_positive',0];graph['63']['inputs']['negative']=[base+'_negative',0]
    save=next(n for n in graph.values() if n['class_type']=='SaveImage');save['inputs']['filename_prefix']='cinematic_visuals/'+visual.id
    return graph


def store_visual(session,settings,visual,data):
    if len(data)>80*1024*1024:raise AppError(413,'visual_too_large','图像输出过大')
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.width*image.height>32_000_000:raise ValueError()
            image.load()
            dimensions=[image.width,image.height]
            buffer=io.BytesIO();image.convert('RGB').save(buffer,format='PNG');data=buffer.getvalue()
    except Exception as exc:raise AppError(502,'visual_decode_failed','实际图像无法解码') from exc
    relative='production_visuals/'+visual.project_id+'/'+visual.id+'.png';path=safe_media_path(settings.media_root,relative);path.parent.mkdir(parents=True,exist_ok=True)
    digest=hashlib.sha256(data).hexdigest()
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest()!=digest:raise AppError(409,'visual_file_exists','拒绝覆盖已有视觉版本')
    if not path.exists():path.write_bytes(data)
    visual.relative_path=relative;visual.sha256=digest;visual.status='needs_review'
    visual.snapshot_json={**visual.snapshot_json,'actual_dimensions':dimensions,'output_sha256':digest}
    registry=session.scalar(select(m.ManagedFile).where(m.ManagedFile.relative_path==relative))
    if not registry:registry=m.ManagedFile(relative_path=relative);session.add(registry)
    registry.state='active';registry.ownership_json=[{'table':'production_visuals','id':visual.id}];registry.metadata_json={'verified_owned':True,'sha256':digest,'bytes':len(data),'kind':'production_visual'}
    snapshot=visual.snapshot_json.get('service_snapshot',{})
    if snapshot.get('provider')=='comfyui' and snapshot.get('model_id')=='flux-2-klein-4b-fp8.safetensors':
        service=session.get(m.AIService,snapshot['service_id'])
        if service and service.revision==snapshot['service_revision']:
            count=len(visual.snapshot_json.get('references',[]))
            if count:
                old=service.capabilities_json.get('reference_image',{})
                previous=old.get('max_reference_images',0) if old.get('model_id') in (None,'',snapshot['model_id']) else 0
                service.capabilities_json={**service.capabilities_json,'reference_image':{
                    'state':'verified','source':'actual decoded local native reference image; identity and product fidelity require review',
                    'visual_id':visual.id,'provider_job_id':visual.provider_job_id,'model_id':snapshot['model_id'],
                    'service_revision':service.revision,'max_reference_images':max(count,previous),
                    'workflow_sha256':visual.snapshot_json.get('workflow_sha256'),'output_sha256':digest}}
                visual.snapshot_json={**visual.snapshot_json,'reference_budget':{
                    **visual.snapshot_json.get('reference_budget',{}),'reference_count':count,'inference_verified':True}}
    if snapshot.get('provider')=='openai_compatible':
        service=session.get(m.AIService,snapshot['service_id'])
        if service and service.revision==snapshot['service_revision']:
            count=len(visual.snapshot_json.get('references',[]));kind='reference_image' if count else 'text_to_image';old=service.capabilities_json.get(kind,{})
            service.capabilities_json={**service.capabilities_json,kind:{'state':'verified','source':'actual decoded cloud image, visual identity still requires review','job_id':visual.id,'provider_job_id':visual.provider_job_id,'model_id':snapshot['model_id'],'max_reference_images':max(count,old.get('max_reference_images',0))}}


async def cloud_visual(settings,session,visual,secret,log):
    """Synchronous compatible image contract; no fake provider job IDs or retries."""
    snapshot=visual.snapshot_json['service_snapshot'];parameters=visual.snapshot_json['parameters'];images=[]
    for ref in visual.snapshot_json['references']:
        path=safe_media_path(settings.media_root,ref['relative_path'],must_exist=True);data=path.read_bytes()
        if hashlib.sha256(data).hexdigest()!=ref['sha256']:raise AppError(409,'visual_reference_changed','锁定参考文件发生变化')
        images.append((ref['sha256']+'.png',data,'image/png'))
    body={'model':snapshot['model_id'],'prompt':visual.prompt,'n':1,'size':f"{parameters['width']}x{parameters['height']}"}
    qwen_format=snapshot['settings'].get('image_api_format')=='qwen_generations'
    qwen_body=qwen_image_body(body,images) if qwen_format else None
    (log/'cloud_request.json').write_text(json.dumps({'endpoint':'/images/edits' if images and not qwen_format else '/images/generations','body':body,'reference_ids':[r['asset_id'] for r in visual.snapshot_json['references']],'reference_count':len(images),'service_revision':snapshot['service_revision']},ensure_ascii=False,indent=2),encoding='utf-8')
    visual.status='submitting';session.commit()
    if qwen_format:response=await service_request(settings,snapshot,'POST','/images/generations',secret,json=qwen_body)
    elif not images:response=await service_request(settings,snapshot,'POST','/images/generations',secret,json=body)
    elif snapshot['settings'].get('image_api_format')=='json':response=await service_request(settings,snapshot,'POST','/images/edits',secret,json={**body,'images':[{'image_url':'data:image/png;base64,'+base64.b64encode(v[1]).decode()} for v in images]})
    else:response=await service_request(settings,snapshot,'POST','/images/edits',secret,data={k:str(v) for k,v in body.items()},files=[('image' if len(images)==1 else 'image[]',v) for v in images])
    visual.provider_job_id=response.headers.get('x-request-id')
    if qwen_format and (response.status_code==408 or response.status_code>=500):
        visual.snapshot_json={**visual.snapshot_json,'cloud_post_accepted':True,'cloud_response_status':response.status_code}
        session.commit()  # Acceptance is uncertain: retain request ID and never retry blindly.
    response.raise_for_status()
    visual.snapshot_json={**visual.snapshot_json,'cloud_post_accepted':True,'cloud_response_status':response.status_code}
    session.commit()  # actual ID survives malformed body, download or decode errors
    result=response.json()
    if not isinstance(result.get('data'),list) or len(result['data'])!=1:raise AppError(502,'cloud_image_contract','接口未返回唯一 data 图像，不能冒称成功')
    item=result['data'][0]
    if item.get('b64_json'):data=base64.b64decode(item['b64_json'],validate=True)
    else:
        from urllib.parse import urlsplit
        url=item.get('url','')
        if urlsplit(url).scheme!='https':raise AppError(422,'unsafe_result_url','云端图像结果必须是公开HTTPS地址')
        download=await service_request(settings,{**snapshot,'base_url':url},'GET','');download.raise_for_status();data=download.content
    store_visual(session,settings,visual,data)
    (log/'cloud_receipt.json').write_text(json.dumps({'request_id':visual.provider_job_id,'status_code':response.status_code,'output_sha256':visual.sha256,'actual_dimensions':visual.snapshot_json['actual_dimensions'],'status':'needs_review'},ensure_ascii=False),encoding='utf-8')


from app.services.gpu_gate import gpu_submission

@gpu_submission
async def visual_tick(settings,session_factory,visual_id=None):
    with session_factory() as session:
        active=list(session.scalars(select(m.ProductionVisual).where(m.ProductionVisual.status.in_(['submitting','submitted','running','cancelling'])).order_by(m.ProductionVisual.created_at)))
        for visual in active:
            try:
                snapshot=visual.snapshot_json['service_snapshot'];secret=credential_for(session,snapshot)
                if snapshot['provider']!='comfyui':visual.status='unknown';visual.error_message='云端同步请求在中断时未取得确定结果；先到供应商核对原请求，禁止自动重提';session.commit();continue
                if not visual.provider_job_id:
                    visual.status='unknown';visual.error_message='提交结果未知，未重复提交；需核对ComfyUI记录';session.commit();continue
                response=await service_request(settings,snapshot,'GET','/history/'+visual.provider_job_id,secret);response.raise_for_status();history=response.json().get(visual.provider_job_id)
                if history:
                    status=history.get('status',{})
                    if status.get('status_str')=='error':
                        visual.status='failed';visual.error_message='ComfyUI实际执行失败，查看此视觉任务日志'
                    elif status.get('completed'):
                        outputs=[image for node in history.get('outputs',{}).values() for image in node.get('images',[]) if image.get('type')=='output']
                        if len(outputs)!=1:raise AppError(502,'visual_output_ambiguous','图像任务没有唯一真实输出')
                        data=await service_request(settings,snapshot,'GET','/view',secret,params=outputs[0]);data.raise_for_status();store_visual(session,settings,visual,data.content)
                    (PROJECT_ROOT/'logs/cinematic_visuals'/visual.id/'history.json').write_text(json.dumps(history,ensure_ascii=False),encoding='utf-8')
                else:
                    queue=(await service_request(settings,snapshot,'GET','/queue',secret)).json()
                    running={str(r[1]) for r in queue.get('queue_running',[])};pending={str(r[1]) for r in queue.get('queue_pending',[])}
                    if visual.provider_job_id in running:visual.status='running'
                    elif visual.provider_job_id in pending:visual.status='submitted'
                    else:
                        since=visual.snapshot_json.get('status_missing_since') or time.time()
                        visual.snapshot_json={**visual.snapshot_json,'status_missing_since':since}
                        if time.time()-since>30:visual.status='unknown';visual.error_message='ComfyUI队列/历史连续30秒未找到此任务，不自动重提'
                session.commit()
            except Exception as exc:
                if isinstance(exc,AppError):visual.status='failed';visual.error_message=exc.message
                else:visual.error_message=type(exc).__name__+'：读取实际服务状态失败，可稍后重新查询'
                session.commit()
        if active:return
        queued=select(m.ProductionVisual).where(m.ProductionVisual.status=='queued')
        if visual_id:
            queued=queued.where(m.ProductionVisual.id==visual_id)
        visual=session.scalar(queued.order_by(m.ProductionVisual.created_at).limit(1))
        if not visual:return
        snapshot=visual.snapshot_json['service_snapshot'];log=PROJECT_ROOT/'logs/cinematic_visuals'/visual.id;log.mkdir(parents=True,exist_ok=True)
        prompt_submission_started=False
        try:
            secret=credential_for(session,snapshot)
            if snapshot['provider']=='openai_compatible':
                await cloud_visual(settings,session,visual,secret,log);session.commit();return
            queue=await service_request(settings,snapshot,'GET','/queue',secret);queue.raise_for_status()
            if any(queue.json().get(k) for k in ['queue_running','queue_pending']):return
            visual.status='submitting';session.commit();uploaded=[]
            for ref in visual.snapshot_json['references']:
                path=safe_media_path(settings.media_root,ref['relative_path'],must_exist=True);data=path.read_bytes()
                if hashlib.sha256(data).hexdigest()!=ref['sha256']:raise AppError(409,'visual_reference_changed','锁定参考文件发生变化')
                response=await service_request(settings,snapshot,'POST','/upload/image',secret,files={'image':(ref['sha256']+'.png',data,'image/png')},data={'type':'input','subfolder':'cinematic','overwrite':'false'});response.raise_for_status();entry=response.json();uploaded.append('/'.join(filter(None,[entry.get('subfolder'),entry['name']])))
            graph=visual_graph(visual,uploaded)
            visual.snapshot_json={**visual.snapshot_json,'workflow_sha256':fingerprint(graph)}
            (log/'submitted_prompt.json').write_text(json.dumps({'prompt':graph,'snapshot':visual.snapshot_json},ensure_ascii=False,indent=2),encoding='utf-8')
            # Only a failure from this point can mean ComfyUI accepted the job
            # while its response was lost. Earlier connection/upload failures
            # are definitive pre-submit failures and must not become "unknown".
            prompt_submission_started=True
            response=await service_request(settings,snapshot,'POST','/prompt',secret,json={'prompt':graph,'client_id':visual.id})
            if response.status_code>=400:raise AppError(422,'visual_graph_rejected','ComfyUI拒绝当前实际节点图，未创建假成功')
            visual.provider_job_id=response.json()['prompt_id'];visual.status='submitted';session.commit()
        except AppError as exc:
            accepted=snapshot['provider']=='openai_compatible' and visual.snapshot_json.get('cloud_post_accepted')
            visual.status='unknown' if accepted else 'failed'
            visual.error_message=('云端已受理但结果未确认，先核对原请求，未自动重提：' if accepted else '')+redact(exc.message,secret if 'secret' in locals() else '');session.commit()
        except Exception as exc:
            import httpx
            accepted=snapshot['provider']=='openai_compatible' and visual.snapshot_json.get('cloud_post_accepted')
            if snapshot['provider']=='comfyui' and not prompt_submission_started:
                visual.status='failed'
                visual.error_message=type(exc).__name__+'：ComfyUI在提交生成任务前不可用；未创建服务任务。检查本地服务后可重试。'
            elif snapshot['provider']=='comfyui':
                visual.status='unknown'
                visual.error_message=type(exc).__name__+'：ComfyUI提交响应未确认；先核对原服务队列，未重复提交。'
            else:
                visual.status='failed' if snapshot['provider']=='openai_compatible' and not accepted and isinstance(exc,(httpx.HTTPStatusError,ValueError,KeyError)) else 'unknown'
                visual.error_message=type(exc).__name__+(f' HTTP {exc.response.status_code}' if isinstance(exc,httpx.HTTPStatusError) else '')+'：真实请求未完成，请核对服务端记录；不会自动重复计费'
            session.commit()


async def visual_worker(settings,session_factory):
    while True:
        try:await visual_tick(settings,session_factory)
        except asyncio.CancelledError:raise
        except Exception:
            import logging
            logging.getLogger(__name__).exception('Cinematic visual worker failure')
        await asyncio.sleep(2)
