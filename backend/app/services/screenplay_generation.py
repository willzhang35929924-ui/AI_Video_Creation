"""Durable, source-aware screenplay authoring over the existing service registry."""
import asyncio
import copy
import hashlib
import json
import logging
import re
import time
from pathlib import Path
import httpx
from sqlalchemy import select, func, update
from sqlalchemy.exc import IntegrityError
from app import models as m
from app.core.errors import AppError
from app.core.paths import safe_media_path
from app.core.secrets import reveal, redact
from app.models.entities import new_id, utcnow
from app.models.screenplay_generation import ScreenplayGenerationJob as Job, ScreenplayDocument as Document, ScreenplayInputAsset as Asset, ScreenplayProjectNameClaim as NameClaim
from app.schemas_cinematic import ScreenplayIR
from app.schemas_screenplay import ImportConfirm
from app.schemas_screenplay_generation import GenerateScreenplay
from app.services.ai_services import service_snapshot, service_request, public_service
from app.services.cinematic_rules import rules_bundle
from app.services.runtime_registry import file_sha
from app.services.screenplay_generation_media import LIMITS, prepare_source, asset_dto, sha
from app.services.screenplay_document import export_screenplay
from app.services.word_screenplay import validate_preview, summary, read_blocks, commit_import
from app.services.gpu_gate import gpu_submission
from app.services.screenplay_review_warnings import review_warnings
from app.services.screenplay_production_bindings import bind_production_designs
from app.services.screenplay_multi_source import frozen_sources,prepare_images,source_input,cleanup_temporary_source

SLOTS={mode:'screenplay_from_'+mode for mode in ('text','image','video')}
MODE_NAMES={'text':'文生剧本','image':'图片生剧本','video':'视频生剧本'}
ACTIVE={'queued','running'}
SCREENPLAY_CONTINUATION_CONTEXT_CHARS=8192
def canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
def content_sha(value):return sha(canonical(value).encode('utf-8'))

def safe_metadata(value,secret='',depth=0):
    if depth>12:return '[metadata depth limit]'
    if isinstance(value,dict):
        return {key:('***' if re.sub('[^a-z0-9]','',str(key).casefold()) in {'apikey','authorization','accesstoken','token','secret','password','headers'} else safe_metadata(item,secret,depth+1)) for key,item in value.items()}
    if isinstance(value,list):return [safe_metadata(item,secret,depth+1) for item in value[:1000]]
    if isinstance(value,str):return redact(value,secret)
    return value

def get_job(session,identifier):
    item=session.get(Job,identifier)
    if not item:raise AppError(404,'screenplay_job_missing','剧本任务不存在')
    return item

def documents(session,job_id):
    return list(session.scalars(select(Document).where(Document.job_id==job_id).order_by(Document.version)))

def dto(session,job):
    docs=documents(session,job.id)
    asset=session.get(Asset,job.input_asset_id) if job.input_asset_id else None
    return {'id':job.id,'project_name':job.project_name,'input_mode':job.input_mode,'creative_prompt':job.creative_prompt,
        'story_text':job.story_text,'input_asset':asset_dto(asset) if asset else None,'status':job.status,'stage':job.stage,
        'revision':job.revision,'service_snapshot':job.receipt_json.get('actual_service_snapshot') or job.snapshot_json.get('service_snapshot',{}),
        'selected_service_snapshot':job.snapshot_json.get('service_snapshot',{}),'screenplay':job.screenplay_json or None,
        'counts':summary(job.screenplay_json) if job.screenplay_json else {'characters':0,'scenes':0,'shots':0,'dialogues':0,'suggestions':0,'unrecognized':0},
        'documents':[{'id':d.id,'version':d.version,'download_url':f'/api/screenplay/documents/{d.id}/download','sha256':d.sha256,
                      'review_state':d.review_state,'created_at':d.created_at.isoformat()} for d in docs],
        'latest_document_id':docs[-1].id if docs else None,'review_state':job.review_state,'project_id':job.project_id,
        'error':job.error_message,'parent_job_id':job.parent_job_id,'scope':job.scope,'target_key':job.target_key,
        'uncertainties':list(dict.fromkeys(job.screenplay_json.get('uncertainties',[])+review_warnings(job.screenplay_json,job.input_mode))) if job.screenplay_json else [],
        'receipt':job.receipt_json,'input_request':source_input(job),'source_selection':source_input(job),
        'input_assets':[asset_dto(row) for item in job.snapshot_json.get('input_assets',[]) if (row:=session.get(Asset,item['id']))],
        'products':job.snapshot_json.get('products',[]),
        'reference_context':job.snapshot_json.get('reference_context'),'capability_test':job.capability_test,'created_at':job.created_at.isoformat(),'updated_at':job.updated_at.isoformat()}

def screenplay_evidence(session,settings,service,model,slot):
    mode=next((mode for mode,value in SLOTS.items() if value==slot),None)
    if not mode:return None
    for job in session.scalars(select(Job).where(Job.input_mode==mode,Job.status.in_(['awaiting_review','approved'])).order_by(Job.created_at.desc())):
        snapshot=job.receipt_json.get('actual_service_snapshot') or job.snapshot_json.get('service_snapshot',{})
        if (snapshot.get('service_id'),snapshot.get('service_revision'),snapshot.get('model_id'))!=(service.id,service.revision,model):continue
        receipt=job.receipt_json
        if not receipt.get('provider_response_received') or not receipt.get('provider_output_sha256') or receipt.get('validated_ir_sha256')!=content_sha(job.screenplay_json):continue
        if mode=='image' and not receipt.get('source',{}).get('image_request_sha256'):continue
        if mode=='video' and len(receipt.get('source',{}).get('frames',[]))<2 and not (receipt.get('source',{}).get('source_kind')=='analysed_reference_library' and receipt.get('reference_analysis',{}).get('state')=='ready'):continue
        for document in reversed(documents(session,job.id)):
            try:
                path=safe_media_path(settings.media_root,document.relative_path,must_exist=True)
                if document.screenplay_sha256==content_sha(job.screenplay_json) and file_sha(path)==document.sha256:
                    return {'kind':'actual_screenplay_generation','job_id':job.id,'document_id':document.id,'input_mode':mode,
                        'sha256':document.sha256,'ir_sha256':document.screenplay_sha256,'provider_output_sha256':receipt['provider_output_sha256'],
                        'scope':'real model request, media input and validated IR/DOCX; creative quality requires human review'}
            except (OSError,AppError):continue
    return None

def options(session,settings):
    from app.services.task_routing import candidate,route_profile
    services=list(session.scalars(select(m.AIService).where(m.AIService.module=='language_processing').order_by(m.AIService.created_at)))
    modes=[]
    for mode,slot in SLOTS.items():
        profile=route_profile(session,'system','default',slot)
        choices=profile.settings.choices
        available=[candidate(session,settings,s,slot,model.get('id') or s.default_model).model_dump()
            for s in services if s.enabled and s.provider in {'local_llm','openai_compatible'}
            for model in (s.models_json or [{'id':s.default_model}])]
        # Manually added model lists can omit the default; keep it selectable once.
        for s in services:
            if s.enabled and s.provider in {'local_llm','openai_compatible'} and s.default_model and not any(c['service_id']==s.id and c['model_id']==s.default_model for c in available):
                available.append(candidate(session,settings,s,slot,s.default_model).model_dump())
        default_service=next((s for s in services if choices and s.id==choices[0].service_id),None)
        modes.append({'input_mode':mode,'slot':slot,'name':MODE_NAMES[mode],'candidates':available,
            'default_service_id':choices[0].service_id if choices else None,
            'default_model_id':(choices[0].model_id or (default_service.default_model if default_service else '')) if choices else None,'limits':LIMITS[mode],
            'reason':None if available else '尚未配置语言/多模态服务，请在服务档案登记并进行对应模式测试'})
    return {'modes':modes,'services':[public_service(s) for s in services],
        'limitations':['图片模式真实发送图片；视频模式抽取带时间戳的8帧。','有声音轨会提取；已配置的本机ASR可自动转写，原话与说话人仍需核对，未配置ASR时标受限。','测试结果仅证明对应输入链路可执行，剧情与感知质量仍需人工审核。']}

def create_generation(session,settings,payload,*,capability_test=False,parent=None,scope='all',target_key=None,instruction='',retry_of_job_id=None):
    data=payload.model_dump();request_hash=content_sha({**data,'capability_test':capability_test,'parent_job_id':parent.id if parent else None,'scope':scope,'target_key':target_key,'instruction':instruction,
        **({'retry_of_job_id':retry_of_job_id} if retry_of_job_id else {})})
    existing=session.scalar(select(Job).where(Job.idempotency_key==payload.idempotency_key))
    if existing:
        if existing.request_sha256!=request_hash:raise AppError(409,'screenplay_idempotency_conflict','此提交标识已经用于其他内容，请创建新的生成请求')
        return existing
    first_asset_id=payload.asset_id or (payload.asset_ids[0] if payload.asset_ids else None)
    asset=session.get(Asset,first_asset_id) if first_asset_id else None
    if payload.input_mode!='text' and not (payload.product_selections or payload.reference_video_ids) and (not asset or asset.input_mode!=payload.input_mode):raise AppError(422,'screenplay_asset_mode','素材不存在或不属于所选输入模式')
    from app.services.task_routing import freeze_route,candidate
    slot=SLOTS[payload.input_mode]
    route=None
    if payload.service_id:
        service=session.get(m.AIService,payload.service_id)
        if not service or not service.enabled or service.module!='language_processing' or service.provider not in {'local_llm','openai_compatible'}:
            raise AppError(409,'screenplay_service_unavailable','请选择已启用的语言模型服务，图片/视频生成模型不能替代剧本模型')
        model=payload.model_id or service.default_model
        known={entry.get('id') for entry in service.models_json}|{service.default_model}
        if not model or model not in known:raise AppError(422,'screenplay_model_not_registered','模型不在当前服务已登记的模型列表中')
        if not capability_test and not candidate(session,settings,service,slot,model).eligible:
            raise AppError(409,'screenplay_capability_unverified','此服务及模型尚未通过该模式的真实测试，请先执行能力测试')
        snapshot=service_snapshot(service,model)
    else:
        if capability_test:raise AppError(422,'screenplay_test_service','能力测试需要明确选择服务')
        route=freeze_route(session,settings,None,None,slot,payload.allow_cloud_costs,payload.allow_paid_fallback)
        if not route.get('selected'):raise AppError(409,'screenplay_service_unavailable','此模式尚无满足能力验证与费用授权的默认服务',{'rejections':route.get('rejections',[])})
        snapshot=route['selected']['service_snapshot'];service=session.get(m.AIService,snapshot['service_id'])
    if snapshot['is_cloud'] and not payload.allow_cloud_costs:raise AppError(403,'screenplay_cloud_not_authorized','本次未允许付费云端调用，未发送请求')
    if snapshot['is_cloud'] and payload.input_mode!='text' and not payload.allow_cloud_media_upload:raise AppError(403,'screenplay_cloud_media_not_authorized','本次未明确允许向云端发送素材或素材分析，未发送请求')
    # Fail before creating a durable queued job if the current worker identity
    # cannot read a configured credential. Empty credentials remain valid for
    # compatible endpoints that do not require authentication.
    if service.encrypted_secret:
        reveal(service.encrypted_secret)
    extras=frozen_sources(session,settings,payload)
    job=Job(id=new_id('spjob'),idempotency_key=payload.idempotency_key,request_sha256=request_hash,project_name=payload.project_name,
        input_mode=payload.input_mode,creative_prompt=payload.creative_prompt,story_text=payload.story_text,input_asset_id=asset.id if asset else None,
        parent_job_id=parent.id if parent else None,scope=scope,target_key=target_key,instruction=instruction,capability_test=capability_test,
        snapshot_json={**extras,'service_snapshot':snapshot,'route':route,'request_service_id':payload.service_id,'request_model_id':payload.model_id,
            'allow_cloud_costs':payload.allow_cloud_costs,'allow_paid_fallback':payload.allow_paid_fallback,
            'source_asset_sha256':asset.sha256 if asset else None,'rules_snapshot':rules_bundle(),
            'base_ir':copy.deepcopy(parent.screenplay_json) if parent else None,'lineage_job_id':(parent.snapshot_json.get('lineage_job_id') or parent.id) if parent else None,
            **({'retry_of_job_id':retry_of_job_id} if retry_of_job_id else {})})
    session.add(job)
    try:session.commit()
    except IntegrityError:
        session.rollback();existing=session.scalar(select(Job).where(Job.idempotency_key==payload.idempotency_key))
        if not existing or existing.request_sha256!=request_hash:raise AppError(409,'screenplay_idempotency_conflict','重复提交的内容发生冲突')
        return existing
    return job

def revise(session,settings,identifier,payload):
    parent=get_job(session,identifier)
    if parent.revision!=payload.expected_revision:raise AppError(409,'screenplay_stale','剧本已变化，请刷新后修改')
    if not parent.screenplay_json or parent.status not in {'awaiting_review','approved','export_failed'}:
        raise AppError(409,'screenplay_revision_unavailable','请等待生成出可修改的结构化剧本')
    if payload.scope!='all':
        collection='scenes' if payload.scope=='scene' else 'shots'
        if not any(item['key']==payload.target_key for item in parent.screenplay_json[collection]):raise AppError(422,'screenplay_revision_target','局部修改目标不属于当前剧本')
    service=parent.snapshot_json['service_snapshot']
    request=GenerateScreenplay(project_name=parent.project_name,creative_prompt=payload.creative_prompt or parent.creative_prompt,
        input_mode=parent.input_mode,story_text=parent.story_text,**source_input(parent),service_id=service['service_id'],model_id=service['model_id'],
        allow_cloud_costs=payload.allow_cloud_costs,allow_paid_fallback=payload.allow_paid_fallback,idempotency_key=payload.idempotency_key)
    return create_generation(session,settings,request,parent=parent,scope=payload.scope,target_key=payload.target_key,instruction=payload.instruction)

def cancel_generation(session,identifier,payload):
    job=get_job(session,identifier)
    if job.revision!=payload.expected_revision:raise AppError(409,'screenplay_stale','任务已变化，请刷新后重试')
    if job.status not in {'queued','running'}:
        raise AppError(409,'screenplay_cancel_unavailable','只有等待执行或正在处理的任务可以中止；结果未知必须通过明确确认新建重试')
    receipt=job.receipt_json or {}
    if job.status=='running' and receipt.get('provider_request_submitted'):
        set_stage(session,job,'unknown','unknown','模型请求已经提交；本地中止无法停止供应商执行，结果未知。核对后可明确新建重试')
        return job
    set_stage(session,job,'cancelled','cancelled','用户在模型请求提交前手动中止')
    return job

def resume_generation(session,settings,identifier,payload):
    job=get_job(session,identifier)
    if job.revision!=payload.expected_revision:raise AppError(409,'screenplay_stale','任务已变化，请刷新后重试')
    if job.status!='cancelled':raise AppError(409,'screenplay_resume_unavailable','只有已中止的任务可以继续执行')
    if job.receipt_json.get('provider_request_submitted') and not payload.confirm_unknown_retry:
        raise AppError(409,'screenplay_resume_unknown','原任务已经提交模型请求，继续会创建新的独立请求；请核对供应商记录或明确接受可能重复生成、计费')
    service_snapshot=job.snapshot_json.get('service_snapshot',{})
    service_id=job.snapshot_json.get('request_service_id') or service_snapshot.get('service_id')
    model_id=''
    if service_id:
        row=session.get(m.AIService,service_id)
        if not row or not row.enabled:raise AppError(409,'screenplay_resume_service_unavailable','原选择的服务当前不可用，请在设置修复后重试或新建剧本')
        model_id=row.default_model
    from app.schemas_screenplay_generation import GenerateScreenplay
    request=GenerateScreenplay(project_name=job.project_name,creative_prompt=job.creative_prompt,
        input_mode=job.input_mode,story_text=job.story_text,**source_input(job),
        service_id=service_id,model_id=model_id,
        allow_cloud_costs=payload.allow_cloud_costs,allow_paid_fallback=payload.allow_paid_fallback,
        idempotency_key=payload.idempotency_key)
    parent=session.get(Job,job.parent_job_id) if job.parent_job_id else None
    result=create_generation(session,settings,request,capability_test=job.capability_test,parent=parent,
        scope=job.scope,target_key=job.target_key,instruction=job.instruction,retry_of_job_id=job.id)
    return result

def retry_generation(session,settings,identifier,payload):
    previous=get_job(session,identifier)
    if previous.revision!=payload.expected_revision:raise AppError(409,'screenplay_stale','原任务已变化，请刷新后重试')
    if previous.status=='unknown' and not payload.confirm_unknown_retry:
        raise AppError(409,'screenplay_retry_unknown','原请求结果未知；核对供应商记录或明确接受可能重复生成、计费后，才能新建重试')
    if previous.status=='export_failed':raise AppError(409,'screenplay_retry_export_only','剧本已经保存，请使用Word导出重试，无需再次调用模型')
    if previous.status not in {'failed','blocked','unknown'} or previous.screenplay_json:
        raise AppError(409,'screenplay_retry_unavailable','仅失败、受阻或已明确确认结果未知且尚无已校验剧本的任务可以新建重试')
    # Default routes are re-resolved now. An explicitly selected service remains
    # explicit, but uses its current registered model/configuration revision.
    service_id=previous.snapshot_json.get('request_service_id')
    if previous.capability_test or (not previous.snapshot_json.get('route') and not service_id):
        service_id=previous.snapshot_json.get('service_snapshot',{}).get('service_id')
    model_id=''
    if service_id:
        row=session.get(m.AIService,service_id)
        if not row or not row.enabled:raise AppError(409,'screenplay_retry_service_unavailable','原选择的服务当前不可用，请在设置修复后重试或新建剧本选择其他服务')
        model_id=row.default_model
    request=GenerateScreenplay(project_name=previous.project_name,creative_prompt=previous.creative_prompt,input_mode=previous.input_mode,
        story_text=previous.story_text,**source_input(previous),service_id=service_id,model_id=model_id,
        allow_cloud_costs=payload.allow_cloud_costs,allow_paid_fallback=payload.allow_paid_fallback,idempotency_key=payload.idempotency_key)
    parent=session.get(Job,previous.parent_job_id) if previous.parent_job_id else None
    result=create_generation(session,settings,request,capability_test=previous.capability_test,parent=parent,
        scope=previous.scope,target_key=previous.target_key,instruction=previous.instruction,retry_of_job_id=previous.id)
    return result

def prompt_messages(job,media_parts,source):
    products_for_model=[{key:product.get(key) for key in ('id','name','brand','category','notes','selling_points','evidence_notes','target_audience')}|{'images':[{key:picture.get(key) for key in ('id','purpose','notes')} for picture in product.get('images',[])]} for product in job.snapshot_json.get('products',[])]
    system='''你是现实电影编剧与结构化制片助手。只返回一个合法JSON对象，不要Markdown。
创作内容和素材可证实事实必须区分；原图/视频不确定的姓名、关系、背景与不可听到的对白必须标为创作建议或待确认。
人物年龄按用户提供的设定保留，年龄可以省略或为null，不要求成年确认，不凭空补成年年龄。真实实拍观感、自然肤质、可信材质与实景光、克制动作。每镜一个主要动作，完整独立人物动作与说话人。
必须用以下JSON字段（不要输出versions/rules_snapshot，由系统绑定真实版本）：
project:{genre,style,target_duration,aspect_ratio,synopsis,story_body,continuity};
characters:[{key,name,aliases:[],profile:{age:可选非负整数或null,description:身份与面容,hair,eyes,skin,immutable_traits,wardrobe,voice,relationships}}];
scenes:[{key,name,order:整数,environment:{location,time,weather,lighting,layout,description,materials,continuity},audio_events:[{kind:ambience|sfx|music,description}]}];
shots:[{key,scene_key,title,visual_prompt,action,duration_seconds:1至60数字,camera_move,participants:[{character_key,performance:{action,position,expression,emotion,eyeline,wardrobe,continuity}}],dialogues:[{character_key,text,delivery:{}}],audio_events:[{kind:ambience|sfx|music,description}],photography:{shot_size,camera_position,angle,composition,lens},production:{start_state,end_state,state_change_reason,wardrobe_states:{},prop_states:[],beats:[{character_key,start_seconds:数字,end_seconds:数字,action,start_state,end_state}]}}];
designs:[{key,kind:wardrobe|prop|scene_state,name,character_key:可选人物key,scene_key:可选场景key,spec:{}}];
observations:[素材中真实可见的线索],uncertainties:[需要用户确认的信息]。
所有人物/场景/分镜key唯一，引用必须存在；对白的说话人必须出现在本镜participants。各人物profile中只用文字和整数，不用嵌套对象；衣服与固定身份分开。wardrobe_states的每个人物值必须是对象，例如 {"chen_ning":{"description":"深蓝工作服"}}；prop_states每项必须是对象，例如 [{"name":"记录本","state":"合上"}]，不能仅写字符串。每个分镜必须有画面和摄影，声音单独记录。禁止虚构原视频对白；创作对白应标为建议。
输出长度要适合用户目标时长，不要把剧情写成不可解析的长段落。'''
    system+='\n制作资料关联：每项服装状态需填写 wardrobe_key，指向 designs 中同一人物的 kind=wardrobe 记录；每项道具状态需填写 prop_key，指向 kind=prop 记录。所有引用编号必须存在、唯一，不能跨人物混用。跨场景共用的服装和可携道具设计必须设 scene_key=null，场景专属设计只能由对应场景镜头引用。状态、名称和描述分开保存；不确定的道具状态标待确认，不要补造动作。target_duration 数值的单位为秒。'
    if job.snapshot_json.get('products'):
        system+='\n本次为产品营销剧本。可无人出镜，characters可以为空，shots.participants/dialogues也可为空；不要为满足格式凭空添加演员。每个镜头必须增加products:[{product_id:实际所选完整ID,visible_state:可见状态,display_features:应展示的已知特征,on_screen_text:屏幕文字,logo_review_required:true}]。顶层marketing:{hook,target_audience,display_sequence,call_to_action,evidence_sources}。旁白建议用audio_events kind=voice描述，不把无人旁白强行归给画面人物。所有商品名称、产品ID、卖点证据来自给定资料；价格、功效、参数、认证不得猜测。形状、比例、颜色、包装保持参考，Logo/包装字样需原图后期图层或人工核对，不能保证生成准确复现。模板：'+job.snapshot_json['input_request']['marketing_template']+'\n所选真实产品资料（只含创作需要的语义字段，校验值仍由任务保存）：'+canonical(products_for_model)
        # A syntactically valid exemplar is necessary for small local models:
        # prose-only pseudo schemas caused complete collections to be nested
        # inside project.continuity. This is prompt input, never model output.
        exemplar={'project':{'genre':'产品展示','style':'现实实拍','target_duration':3,'aspect_ratio':'16:9','synopsis':'按所选产品资料创作','story_body':'按用户目标编写','continuity':'保持产品外形与颜色'},
            'characters':[],'scenes':[{'key':'S1','name':'展示环境','order':1,'environment':{'location':'展示台','lighting':'柔和真实照明','description':'按创作要求设置实景'},'audio_events':[]}],
            'shots':[{'key':'T1','scene_key':'S1','title':'展示','duration_seconds':3,'visual_prompt':'按真实参考图与用户要求描述画面','action':'保持静置','camera_move':'固定',
                'participants':[],'dialogues':[],'audio_events':[],'photography':{'shot_size':'产品近景','camera_position':'眼平','angle':'平视','composition':'居中'},
                'production':{'start_state':'静置','end_state':'静置','wardrobe_states':{},'prop_states':[],'beats':[]},
                'products':[{'product_id':products_for_model[0]['id'],'visible_state':'完整可见','display_features':'只描述参考可证实特征','on_screen_text':'','logo_review_required':True}]}],
            'designs':[],'observations':[],'uncertainties':[],'marketing':{'hook':'按用户要求创作','target_audience':'待确认','display_sequence':'按分镜顺序','call_to_action':'','evidence_sources':['用户资料及原图']}}
        product=products_for_model[0]
        exemplar['project'].update(synopsis=product['name']+'实景展示',story_body=product['name']+'静置在中性灰展示台上，展示原图所见外观。')
        exemplar['shots'][0]['visual_prompt']='现实实拍产品近景：'+product['name']+'静置在中性灰桌面，柔和侧光呈现真实材质；保持原图结构、配色与比例，不新增部件，固定机位。'
        exemplar['shots'][0]['products'][0]['display_features']='；'.join(product.get('selling_points') or [product['name']])
        exemplar['marketing'].update(hook=product['name']+'的外观细节',target_audience=product.get('target_audience') or '待确认')
        system+='\n下面是合法JSON的层级示例。请按用户要求改写内容/数量/时长，保留键的层级；不要把任何最外层列表移入project或continuity：\n'+canonical(exemplar)
    if job.snapshot_json.get('reference_context'):
        system+='\n本次使用多个参考视频的已保存本地分析，只采用抽象风格/节奏/构图/声音特征，分析≠训练。不要复制原片完整对白、人物身份、品牌或独创表达。'+('根据新主题重新创作：'+job.snapshot_json['input_request'].get('new_theme','') if job.snapshot_json['input_request'].get('video_creation_mode')=='style' else '内容改编：结合用户要求提炼新故事，不复述原片完整台词。')
    if not job.snapshot_json.get('products'):
        exemplar={'project':{'genre':'剧情','style':'现实实拍','target_duration':3,'aspect_ratio':'16:9','synopsis':'依照用户主题创作','story_body':'依照用户主题创作','continuity':'角色身份保持一致'},
            'characters':[{'key':'A','name':'人物示例','aliases':[],'profile':{'age':32,'description':'成年人物','hair':'黑色短发','wardrobe':'日常衣服'}}],
            'scenes':[{'key':'S1','name':'场景示例','order':1,'environment':{'location':'室内','description':'真实实景','lighting':'窗户自然光'},'audio_events':[]}],
            'shots':[{'key':'T1','scene_key':'S1','title':'镜头示例','duration_seconds':3,'visual_prompt':'按用户要求描写','action':'停稳','camera_move':'固定','participants':[{'character_key':'A','performance':{'action':'停稳','position':'中央'}}],'dialogues':[],'audio_events':[],
                'photography':{'shot_size':'中景','camera_position':'眼平'},'production':{'start_state':'站立','end_state':'站立','wardrobe_states':{},'prop_states':[],'beats':[]}}],
            'designs':[],'observations':[],'uncertainties':[]}
        system+='\n合法JSON层级示例（仅示范结构，按用户主题重新创作实际人物/镜头数量/时长/内容，不照抄人物示例）：\n'+canonical(exemplar)
    system+='\nJSON层级必须正确：project对象在continuity字段后立即闭合。characters、scenes、shots、designs、observations、uncertainties、marketing都位于最外层，与project并列；不能放进project里面。designs/observations/uncertainties不能放进shots里面。无人产品镜头的participants、dialogues、beats均为[]，不要生成character_key为空字符串的虚构人物动作。每镜的products必须直接放在该shot上，与production并列，不要藏在designs.spec里。'
    rules=job.snapshot_json.get('rules_snapshot',{})
    instructions=['\n'.join([section.get('title',''),*section.get('instructions',[])]) for section in rules.get('sections',{}).values()]
    system+='\n当前项目统一制作规范（各模式相同，不能把字数当时长）：\n'+'\n'.join(instructions)
    model_source={k:v for k,v in source.items() if k not in {'frames','products','images','reference_videos','transcription_receipt'}}
    if source.get('images'):model_source['images']=[{k:picture.get(k) for k in ('id','product_id','purpose','notes')} for picture in source['images']]
    user=[{'type':'text','text':'项目名称必须原样保持：'+job.project_name+'\n统一创作提示词：'+job.creative_prompt+
        '\n输入方式：'+job.input_mode+'\n故事素材：'+job.story_text+'\n来源处理：'+canonical(model_source)}]+media_parts
    if source.get('audio_transcription')=='not_configured':
        user.append({'type':'text','text':'音轨已提取但未配置语音转写。你不能听到音轨，禁止声称恢复原对白；原对白内容与说话人必须标为待确认。'})
    elif source.get('audio_transcription')=='transcribed_unverified':
        user.append({'type':'text','text':'以下为真实ASR自动转写，仍需人工核对。所有说话人目前未知，严禁凭空指派到某个角色或声称已验证口型；应把原话保留为来源与待确认项。若另写创作对白，请明确是建议。\n'+canonical(source.get('transcript',{}))})
    base=job.snapshot_json.get('base_ir')
    if base:
        if job.scope=='all':payload=base;instruction='重新生成整份，保存新的独立版本。'
        elif job.scope=='scene':
            payload={'scene':next(s for s in base['scenes'] if s['key']==job.target_key),
                'shots':[s for s in base['shots'] if s['scene_key']==job.target_key],'characters':base['characters'],'designs':base.get('designs',[])}
            instruction='仅返回JSON {scene:完整同key场景,shots:本场景完整镜头列表}；不得增加人物，不得修改其他场景。'
        else:
            payload={'shot':next(s for s in base['shots'] if s['key']==job.target_key),'characters':base['characters'],'scenes':base['scenes'],'designs':base.get('designs',[])}
            instruction='仅返回JSON {shot:完整同key分镜}；不得增加人物或修改其他镜头。'
        user.append({'type':'text','text':instruction+'\n修改要求：'+job.instruction+'\n已保存的原内容：'+canonical(payload)})
    return [{'role':'system','content':system},{'role':'user','content':user}]

def _continuation_context_tail(chunks):
    """Keep continuation requests bounded while retaining the exact recent output tail."""
    remaining=SCREENPLAY_CONTINUATION_CONTEXT_CHARS
    tail=[]
    for chunk in reversed(chunks):
        if not remaining:break
        tail.append(chunk[-remaining:])
        remaining-=len(tail[-1])
    return ''.join(reversed(tail))

def _continuation_messages(base_messages,context_tail):
    """Continue a saved, definitively truncated answer without growing context forever."""
    return [*copy.deepcopy(base_messages),
        {'role':'assistant','content':context_tail},
        {'role':'user','content':'此前的生成内容已持久保存；下面是其末尾上下文。请从这个上下文最后一个字符之后继续，只输出尚未生成的JSON文本；不要重写、重复或补充前文，不要使用Markdown代码围栏。继续保持原有JSON结构与字段要求。'}]

def _read_saved_response(settings,relative,digest,*,parse=True):
    try:
        path=safe_media_path(settings.media_root,relative,must_exist=True)
        data=path.read_bytes()
        if sha(data)!=digest:raise ValueError('响应摘要不匹配')
        return json.loads(data.decode('utf-8')) if parse else None
    except (OSError,UnicodeDecodeError,ValueError,TypeError,AppError) as exc:
        raise AppError(502,'screenplay_saved_response_corrupt','已保存的模型响应校验或解析失败；保留任务与原始响应，不会重新提交') from exc

def _load_saved_generation_result(settings,receipt):
    """Verify and join saved length-limited response chunks after a worker restart."""
    history=copy.deepcopy(receipt.get('attempt_history') or [])
    current_status=int(receipt.get('provider_http_status') or 200)
    current_path=receipt.get('provider_response_relative_path')
    current_sha=receipt.get('provider_response_sha256')
    if current_status>=300:
        if not current_path or not current_sha:
            raise AppError(502,'screenplay_saved_response_corrupt','已收到模型响应但缺少可校验的响应归档；不会重新提交')
        _read_saved_response(settings,current_path,current_sha,parse=False)
        if current_status>=500 or current_status==408 or current_status<400:
            return {},history,[],current_status

    saved=[]
    current_index=receipt.get('provider_current_attempt_index')
    for attempt in history:
        status=int(attempt.get('http_status') or 200)
        relative=attempt.get('response_relative_path')
        digest=attempt.get('response_sha256')
        if (not relative or not digest) and current_path and current_sha and attempt.get('attempt_index')==current_index:
            relative=current_path;digest=current_sha
        if not relative or not digest or status>=300:continue
        result=_read_saved_response(settings,relative,digest)
        if not isinstance(result,dict) or not isinstance(result.get('choices'),list) or not result['choices']:
            if attempt is history[-1]:return result,history,[],status
            continue
        choice=result['choices'][0]
        content=(choice.get('message') or {}).get('content')
        if not isinstance(content,str):
            raise AppError(502,'screenplay_saved_response_corrupt','已保存的模型响应不含可续接的文字内容；不会重新提交')
        saved.append((attempt,result,choice,content))

    if not saved and current_status>=400:
        return {},history,[],current_status
    if not saved and current_path and current_sha:
        result=_read_saved_response(settings,current_path,current_sha)
        return result,history,[],current_status
    if not saved:
        raise AppError(502,'screenplay_saved_response_corrupt','已保存的模型响应记录缺失；不会重新提交')

    chunks=[];joined='';usage={}
    previous_reason=None
    for index,(_attempt,result,choice,content) in enumerate(saved):
        reason=choice.get('finish_reason')
        if index and previous_reason!='length':
            raise AppError(502,'screenplay_saved_response_corrupt','模型响应续接记录顺序不一致；不会重新提交')
        if chunks and len(content.strip())>=64 and content.strip()==chunks[-1].strip():
            raise AppError(502,'screenplay_saved_response_corrupt','模型续接响应与上一段重复；不会自动拼接')
        chunks.append(content);joined+=content;previous_reason=reason
        for key,value in (result.get('usage') or {}).items():
            if isinstance(value,int) and not isinstance(value,bool):usage[key]=usage.get(key,0)+value

    if current_status>=400:
        rejected={}
        if usage:rejected['usage']=usage
        return rejected,history,chunks,current_status

    final=copy.deepcopy(saved[-1][1])
    final['choices'][0]['message']['content']=joined
    if usage:final['usage']=usage
    return final,history,chunks,current_status

def _unreviewed_model_content(value,job_id):
    """Provider prose never supplies trusted approval, IDs or provenance.

    Run before merging a partial response, so existing source records from the
    parent IR are kept while only newly generated content loses forged trust.
    """
    origin='generation:'+job_id
    if isinstance(value,list):return [_unreviewed_model_content(item,job_id) for item in value]
    if not isinstance(value,dict):return copy.deepcopy(value)
    result={key:_unreviewed_model_content(item,job_id) for key,item in value.items()
        if key not in {'source','source_ids','provenance','reviewed_edit','review_state','approved','human_identity_approved','is_approved','decision','match_character_id','character_id','entity_id','visual_ids'}}
    record=any(key in value for key in ('key','character_key','kind'))
    if record or any(key in value for key in ('source','source_ids')):result['source']=[origin]
    if record or isinstance(value.get('provenance'),dict):
        fields=value.get('provenance') if isinstance(value.get('provenance'),dict) and value['provenance'] else {'content':None}
        result['provenance']={key:{'state':'suggested','source_ids':[origin],'confidence':.5,'reason':'模型生成内容，尚未人工审核'} for key in fields}
    if any(key in value for key in ('reviewed_edit','human_identity_approved','is_approved','approved','review_state')):result['reviewed_edit']=False
    return result


def normalize_ir(session,job,raw,source):
    if not isinstance(raw,dict):raise ValueError('模型未返回JSON对象')
    raw=_unreviewed_model_content(raw,job.id)
    base=job.snapshot_json.get('base_ir')
    if base and job.scope=='shot':
        replacement=raw.get('shot')
        # Some instruction models keep the full-document wrapper even for a
        # partial response. Accept exactly one explicitly matching target;
        # ignore all other returned sections and merge only into the base IR.
        if replacement is None and isinstance(raw.get('shots'),list):
            matching=[item for item in raw['shots'] if isinstance(item,dict) and item.get('key')==job.target_key]
            if len(matching)==1:replacement=matching[0]
        if not isinstance(replacement,dict) or replacement.get('key')!=job.target_key:raise ValueError('局部改写没有返回同编号分镜')
        ir=copy.deepcopy(base);ir['shots']=[replacement if s['key']==job.target_key else s for s in ir['shots']]
    elif base and job.scope=='scene':
        scene=raw.get('scene');shots=raw.get('shots')
        if not isinstance(scene,dict) or scene.get('key')!=job.target_key or not isinstance(shots,list) or not shots or any(s.get('scene_key')!=job.target_key for s in shots):
            raise ValueError('局部改写没有返回同编号场景与对应镜头')
        ir=copy.deepcopy(base);ir['scenes']=[scene if s['key']==job.target_key else s for s in ir['scenes']]
        positions=[i for i,s in enumerate(ir['shots']) if s['scene_key']==job.target_key]
        first=positions[0];ir['shots']=[s for s in ir['shots'] if s['scene_key']!=job.target_key];ir['shots'][first:first]=shots
    else:ir=copy.deepcopy(raw)
    rules=job.snapshot_json['rules_snapshot'];ir['versions']=rules['versions'];ir['rules_snapshot']=rules
    ir['parser']='screenplay_generation_v1';ir['mode']='standard';ir.setdefault('source_versions',rules['versions'])
    if not isinstance(ir.get('project'),dict):raise ValueError('缺少项目概述')
    ir['project']['name']=job.project_name
    if not ir['project'].get('synopsis') or not ir['project'].get('style'):raise ValueError('缺少梗概或视觉风格')
    ir.setdefault('designs',[]);ir.setdefault('unrecognized',[]);ir.setdefault('parse_warnings',[])
    ir.setdefault('observations',[]);ir.setdefault('uncertainties',[])
    for key in ('observations','uncertainties'):
        if isinstance(ir[key],str):ir[key]=[ir[key]] if ir[key].strip() else []
        if not isinstance(ir[key],list):raise ValueError('观察与待确认内容必须是文字或文字列表')
    if any(not isinstance(v,str) for v in ir['observations']+ir['uncertainties']):raise ValueError('观察与待确认内容需为文字列表')
    if job.input_mode=='text':
        # A text-only request has no photographed or filmed observation source.
        ir['creative_suggestions']=ir.get('creative_suggestions',[])+ir['observations']
        ir['observations']=[]
        ir['uncertainties']=[value+'（本次仅有文字输入，未读取任何原视频）' if '原视频' in value else value for value in ir['uncertainties']]
    if job.snapshot_json.get('products'):ir.setdefault('characters',[])
    if (not ir.get('characters') and not job.snapshot_json.get('products')) or not ir.get('scenes') or not ir.get('shots'):raise ValueError('人物、场景或分镜为空（只有明确绑定产品的无人营销镜头可不设人物）')
    namespace=job.snapshot_json.get('lineage_job_id') or job.id
    for person in ir['characters']:
        if not base or job.scope=='all':person['decision']='new';person['match_character_id']=None
        person.setdefault('aliases',[])
        profile=person.get('profile',{})
        if not isinstance(profile,dict):raise ValueError('人物资料只能包含独立文字字段和年龄')
        # Providers sometimes return a list of traits/relationships despite the
        # scalar prompt. Preserve only plain text lists without inventing a
        # relationship or flattening nested objects into misleading prose.
        profile={key:('；'.join(value) if isinstance(value,list) and all(isinstance(item,str) for item in value) else value) for key,value in profile.items()}
        if any((not isinstance(value,(str,int)) or isinstance(value,bool)) and not (key=='age' and value is None) and not (key=='original_adult' and isinstance(value,bool)) for key,value in profile.items()):raise ValueError('人物资料只能包含独立文字字段和可选年龄')
        if profile.get('age') not in (None,'') and (not isinstance(profile['age'],int) or isinstance(profile['age'],bool) or profile['age']<0):raise ValueError('年龄可省略；填写时需为非负整数')
        if profile.get('age')=='':profile.pop('age')
        profile['character_id']='spchar_'+sha((namespace+':'+person['key']).encode())[:24]
        person['profile']=profile
        person.setdefault('source',['generation:'+job.id])
        person.setdefault('provenance',{'profile':{'state':'suggested','source_ids':['generation:'+job.id],'confidence':.5,'reason':'模型创作设定，待用户审核'}})
    for collection in ('scenes','shots'):
        keys=[item.get('key') for item in ir[collection]]
        if len(set(keys))!=len(keys) or any(not isinstance(k,str) or not 1<=len(k)<=160 for k in keys):raise ValueError('场景或分镜编号无效或重复')
    if len({s.get('order') for s in ir['scenes']})!=len(ir['scenes']):raise ValueError('场景顺序重复')
    people={p['key'] for p in ir['characters']}
    scenes={s['key'] for s in ir['scenes']}
    if any(not isinstance(k,str) or not 1<=len(k)<=160 for k in people):raise ValueError('人物编号无效')
    for owner in ir['scenes']+ir['shots']:
        for event in owner.get('audio_events',[]):
            if not isinstance(event,dict) or event.get('kind') not in {'ambience','sfx','music','voice'} or not isinstance(event.get('description'),str) or not event['description'].strip():
                raise ValueError('声音记录需要明确类型与文字描述')
            if any(k in event for k in ('relative_path','path','url')):raise ValueError('模型声音记录不能引用未经上传的文件')
    for shot in ir['shots']:
        shot.setdefault('dialogues',[]);shot.setdefault('audio_events',[]);shot.setdefault('photography',{});shot.setdefault('production',{})
        if not shot.get('visual_prompt') or not shot.get('photography'):raise ValueError('分镜缺少独立画面或摄影字段')
        placeholders={'按真实参考图与用户要求描述画面','按用户要求描写','按所选产品资料创作','按用户目标编写','只描述参考可证实特征'}
        if str(shot['visual_prompt']).strip() in placeholders or any(str(p.get('display_features','')).strip() in placeholders for p in shot.get('products',[]) if isinstance(p,dict)):
            raise ValueError('模型照抄模板占位提示，未生成可执行画面描述；原响应保留，需明确修改或重试')
        # Retain unambiguously misplaced production fields from a provider.
        # Conflicting duplicate values must remain a visible failure rather
        # than silently selecting the container that happens to be read first.
        for key in ('wardrobe_states','prop_states','beats'):
            if key in shot:
                if key in shot['production'] and shot['production'][key]!=shot[key]:raise ValueError('分镜制作字段重复且冲突：'+key)
                shot['production'][key]=shot.pop(key)
        # Normalize only unambiguous containers in a real provider result. Keep
        # the original wording; never infer a missing wardrobe/prop state.
        wardrobe=shot['production'].get('wardrobe_states',{})
        if isinstance(wardrobe,dict):
            shot['production']['wardrobe_states']={key:({'description':value} if isinstance(value,str) else value) for key,value in wardrobe.items()}
        props=shot['production'].get('prop_states',[])
        if isinstance(props,list):
            shot['production']['prop_states']=[{'description':value} if isinstance(value,str) else value for value in props]
        shot['production']['visual_ids']=[];shot['production']['require_visual_review']=True
        shot['reviewed_edit']=False
        for beat in shot['production'].get('beats',[]):
            if beat.get('character_key') not in people or beat.get('end_seconds',0)>shot.get('duration_seconds',0):raise ValueError('动作节拍人物或时长不属于当前镜头')
        if set(shot['production'].get('wardrobe_states',{}))-people:raise ValueError('造型状态引用未定义人物')
    for design in ir['designs']:
        if not isinstance(design.get('key'),str) or not 1<=len(design['key'])<=100:raise ValueError('制作设定编号无效')
        if design.get('character_key') and design['character_key'] not in people:raise ValueError('服装引用未定义人物')
        if design.get('scene_key') and design['scene_key'] not in scenes:raise ValueError('设计引用未定义场景')
        if job.snapshot_json.get('products') and not people and design.get('kind')=='wardrobe':
            product_ids={product['id'] for product in job.snapshot_json['products']}
            if not design.get('character_key') and design.get('spec',{}).get('product_id') in product_ids:
                # A known library ID proves this record is the selected object,
                # not clothing. Correct only its unambiguous classification;
                # preserve the provider's original label and all source prose.
                design['model_original_kind']='wardrobe';design['kind']='prop'
                ir['parse_warnings'].append({'kind':'product_design_classification','design_key':design['key'],'message':'模型将已选产品标为服装；根据真实product_id归为道具，原类型保留供审核。'})
            else:raise ValueError('无人产品片不能把不明内容当作服装设计；请在产品字段或道具中描述')
    if source.get('audio_transcription')=='not_configured':
        message='原视频音轨已提取但尚未转写；原对白、说话人与声音内容必须人工确认，新增台词仅为创作建议。'
        if message not in ir['uncertainties']:ir['uncertainties'].append(message)
        ir['parse_warnings'].append({'kind':'untranscribed_source_audio','message':message})
    total=round(sum(float(shot['duration_seconds']) for shot in ir['shots']),3)
    ir['project']['shot_total_duration_seconds']=total
    declared=ir['project'].get('target_duration',ir['project'].get('target_duration_seconds'))
    if declared is not None:
        match=re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*(秒|s|sec|seconds?|分钟|min|minutes?)?\s*',str(declared),re.I)
        if match:
            seconds=float(match[1])*(60 if (match[2] or '').lower() in {'分钟','min','minute','minutes'} else 1)
            explicit_seconds=(isinstance(declared,(int,float)) and not isinstance(declared,bool)) or ('target_duration' not in ir['project'] and 'target_duration_seconds' in ir['project'])
            if (not match[2] and not explicit_seconds) or abs(seconds-total)>.1:
                message=f'模型给出的目标时长“{declared}”需核对，当前分镜合计{total:g}秒；字数不代表片长。'
                ir['project']['duration_conflict']=True
                if message not in ir['uncertainties']:ir['uncertainties'].append(message)
                ir['parse_warnings'].append({'kind':'screenplay_duration_conflict','message':message})
    if source.get('audio_transcription')=='transcribed_unverified':
        message='原音轨已有自动转写，识别结果未经人工确认，说话人未知；请核对原话、时间段与说话人归属。'
        ir['source_transcript']=copy.deepcopy(source.get('transcript',{}))
        if message not in ir['uncertainties']:ir['uncertainties'].append(message)
        ir['parse_warnings'].append({'kind':'unverified_source_transcript','message':message})
    products=job.snapshot_json.get('products',[])
    if products:
        allowed={p['id'] for p in products};ir['products']=copy.deepcopy(products)
        for shot in ir['shots']:
            visible=shot.get('products',[])
            if not visible and len(products)==1:
                visible=[{'product_id':products[0]['id'],'visible_state':'模型未单列产品可见状态，需审核','logo_review_required':True}]
                ir['uncertainties'].append('镜头'+shot['key']+'缺少产品可见状态，已保留唯一所选产品引用供审核。')
            if not isinstance(visible,list) or any(not isinstance(p,dict) or p.get('product_id') not in allowed for p in visible):raise ValueError('每镜产品必须引用本次图库中所选的真实product_id')
            shot['products']=visible
        ir['uncertainties'].append('产品卖点为用户提供/模型建议，未核实价格、功效、认证；Logo、包装文字和外观连续性须人工核对。')
    ir=ScreenplayIR.model_validate(ir).model_dump()
    selected=None
    if base and job.scope=='shot':selected={job.target_key}
    elif base and job.scope=='scene':selected={shot['key'] for shot in ir['shots'] if shot['scene_key']==job.target_key}
    ir=ScreenplayIR.model_validate(bind_production_designs(ir,namespace,shot_keys=selected)).model_dump()
    issues=validate_preview(session,ir)
    if issues:raise ValueError('；'.join(issues))
    ir['uncertainties']=list(dict.fromkeys(ir.get('uncertainties',[])+review_warnings(ir,job.input_mode)))
    return ir

def set_stage(session,job,status,stage,error=None):
    job.status=status;job.stage=stage;job.error_message=error;job.revision+=1;session.commit()

def export_job(session,settings,job):
    if not job.screenplay_json:raise AppError(409,'screenplay_no_ir','没有可用于导出的已校验剧本')
    parent=session.get(Job,job.parent_job_id) if job.parent_job_id else None
    versions=[d.version for d in documents(session,job.id)]
    if parent:versions.extend(d.version for d in documents(session,parent.id))
    version=max(versions,default=0)+1
    document=Document(id=new_id('spdoc'),job_id=job.id,version=version,relative_path='',sha256='',screenplay_sha256=content_sha(job.screenplay_json),versions_json=job.screenplay_json['versions'])
    document.relative_path=f'screenplay_jobs/{job.id}/documents/{document.id}.docx'
    actual_service=job.receipt_json.get('actual_service_snapshot') or job.snapshot_json.get('service_snapshot',{})
    payload=export_screenplay(job.screenplay_json,job_id=job.id,document_id=document.id,version=version,
        input_source={'输入方式':MODE_NAMES[job.input_mode],'源素材':job.receipt_json.get('source',{}).get('filename','产品图库所选图片' if job.snapshot_json.get('products') else '多条参考视频的已保存分析' if job.snapshot_json.get('reference_context') else '用户故事文本'),
            '所选产品ID':[p['id'] for p in job.snapshot_json.get('products',[])],
            '参考视频ID':job.snapshot_json.get('reference_context',{}).get('reference_video_ids',[]),
            '参考分析版本':job.snapshot_json.get('reference_context',{}).get('analysis_ids',[]),
            '风格档案ID':job.receipt_json.get('source',{}).get('style_profile_id',''),
            '源素材SHA256':job.receipt_json.get('source',{}).get('source_sha256',''),'素材说明':job.receipt_json.get('source',{}).get('source_note',''),
            '音轨处理':job.receipt_json.get('source',{}).get('audio_transcription','不适用'),
            '实际生成模型':actual_service.get('model_id',''),'模型服务档案修订':actual_service.get('service_revision',''),
            '原音轨自动转写':job.receipt_json.get('source',{}).get('transcript',{}).get('text',''),
            '原音轨说话人':'未知，需人工核对' if job.receipt_json.get('source',{}).get('transcript') else ''})
    destination=safe_media_path(settings.media_root,document.relative_path);destination.parent.mkdir(parents=True,exist_ok=True)
    with destination.open('xb') as stream:stream.write(payload)
    # Opening the output with our existing DOCX parser verifies it is a normal readable document.
    read_blocks(payload,settings.import_max_bytes)
    document.sha256=sha(payload);session.add(document);session.flush()
    session.add(m.ManagedFile(relative_path=document.relative_path,state='active',ownership_json=[{'table':'screenplay_documents','id':document.id}],metadata_json={'verified_owned':True,'sha256':document.sha256}))
    job.status='awaiting_review';job.stage='awaiting_review';job.review_state='pending';job.error_message=None;job.revision+=1;session.commit()
    return document

def _next_fallback(session,job,current):
    route=job.snapshot_json.get('route')
    if job.capability_test or not route:return None
    config=route.get('profile',{}).get('settings',{})
    if not config.get('allow_fallback'):return None
    choices=route.get('choices',[])
    position=next((n for n,c in enumerate(choices) if c.get('service_snapshot',{}).get('service_id')==current['service_id'] and c.get('service_snapshot',{}).get('model_id')==current['model_id']),len(choices))
    for choice in choices[position+1:]:
        snapshot=choice.get('service_snapshot',{});candidate=choice.get('candidate',{})
        if not candidate.get('eligible'):continue
        if snapshot.get('is_cloud') and not (config.get('allow_paid_fallback') and job.snapshot_json.get('allow_paid_fallback') and job.snapshot_json.get('allow_cloud_costs')):continue
        if snapshot.get('is_cloud') and job.input_mode!='text' and not job.snapshot_json.get('allow_cloud_media_upload'):continue
        row=session.get(m.AIService,snapshot.get('service_id'))
        if row and row.enabled and row.revision==snapshot.get('service_revision') and row.provider in {'local_llm','openai_compatible'}:
            return snapshot
    return None

async def _call_provider(settings,session,job,media_parts,source,*,snapshot=None,history=None,accumulated_chunks=None,continuation_count=0,last_chunk='',prior_usage=None):
    snapshot=copy.deepcopy(snapshot or job.snapshot_json['service_snapshot']);history=copy.deepcopy(history or [])
    base_messages=None
    output_chunks=list(accumulated_chunks or [])
    usage_totals={key:value for key,value in (prior_usage or {}).items() if isinstance(value,int) and not isinstance(value,bool)}
    while True:
        row=session.get(m.AIService,snapshot['service_id'])
        if not row or not row.enabled or row.revision!=snapshot['service_revision']:
            raise AppError(409,'screenplay_service_changed','冻结的模型档案发生变化，未派发或改用任意服务')
        secret=reveal(row.encrypted_secret) if row.encrypted_secret else ''
        if snapshot['provider']=='local_llm':
            from app.services.screenplay_runtime import ensure_local_service
            await ensure_local_service(snapshot)
            if source.get('audio_relative_path') and source.get('audio_transcription')=='not_configured':
                from app.services.screenplay_runtime import maybe_transcribe_audio
                enriched=await maybe_transcribe_audio(snapshot,settings,source)
                if enriched is not source:source.update(enriched)
                job.receipt_json={**job.receipt_json,'source':copy.deepcopy(source)};session.commit()
        if base_messages is None:base_messages=prompt_messages(job,media_parts,source)
        output_limit=int(snapshot['settings'].get('max_tokens',8192))
        if not snapshot.get('is_cloud'):output_limit=min(output_limit,3072)
        request_messages=base_messages if not output_chunks else _continuation_messages(base_messages,_continuation_context_tail(output_chunks))
        body={'model':snapshot['model_id'],'messages':request_messages,
            'temperature':.35,'max_tokens':output_limit,'stream':False}
        timeout=httpx.Timeout(None)
        attempt_index=len(history)+1
        attempt={'attempt_index':attempt_index,'service_id':snapshot['service_id'],'service_revision':snapshot['service_revision'],'model_id':snapshot['model_id'],
            'is_cloud':snapshot['is_cloud'],'request_sha256':content_sha(body),'started_at':utcnow().isoformat(),
            'timeout_seconds':None,'total_timeout_seconds':None,'continuation_index':continuation_count,
            'continuation_of_attempt':history[-1].get('attempt_index') if continuation_count and history else None,'status':'submitted'}
        history.append(attempt)
        job.receipt_json={**job.receipt_json,'provider_request_submitted':True,'request_sha256':attempt['request_sha256'],
            'actual_service_snapshot':snapshot,'attempt_history':copy.deepcopy(history),
            'provider_current_attempt_index':attempt_index,'provider_response_received':False,'provider_response_saved':False,
            'provider_response_relative_path':None,'provider_response_sha256':None,'provider_continuation_count':continuation_count,
            'paid_fallback':any(a.get('is_cloud') and a.get('service_id')!=history[0].get('service_id') for a in history)};session.commit()
        try:response=await service_request(settings,snapshot,'POST','/chat/completions',secret,
            client_timeout=timeout,json=body)
        except (httpx.TransportError,asyncio.TimeoutError) as exc:
            detail=redact(str(exc),secret).strip()
            error_type=type(exc).__name__
            attempt.update(status='unknown',transport_error_type=error_type,
                transport_error=detail[:600] if detail else None)
            job.receipt_json={**job.receipt_json,'attempt_history':copy.deepcopy(history)};session.commit()
            explanation=f'模型请求结果未知，未重发或回退（{error_type}）'
            if detail:explanation+='：'+detail[:600]
            raise AppError(502,'screenplay_provider_unknown',explanation) from exc
        # Persist every actual response before parsing or deciding whether to continue.
        attempt.update(status='response_received',http_status=response.status_code)
        job.receipt_json={**job.receipt_json,'attempt_history':copy.deepcopy(history),
            'provider_response_received':True,'provider_http_status':response.status_code};session.commit()
        raw=response.text
        safe_raw=redact(raw,secret).encode('utf-8')
        relative=f'screenplay_jobs/{job.id}/attempt_{len(history)}_response.json'
        response_path=safe_media_path(settings.media_root,relative);response_path.parent.mkdir(parents=True,exist_ok=True)
        with response_path.open('xb') as stream:stream.write(safe_raw)
        response_sha=sha(safe_raw)
        attempt.update(status='response_saved',response_relative_path=relative,response_sha256=response_sha,
            response_credentials_redacted=safe_raw!=raw.encode('utf-8'),http_status=response.status_code)
        session.add(m.ManagedFile(relative_path=relative,state='active',ownership_json=[{'table':'screenplay_generation_jobs','id':job.id}],
            metadata_json={'sha256':response_sha,'verified_owned':True,'kind':'provider_response'}))
        job.receipt_json={**job.receipt_json,'attempt_history':copy.deepcopy(history),'provider_response_saved':True,
            'provider_response_relative_path':relative,'provider_response_sha256':response_sha};session.commit()
        if response.status_code>=500 or response.status_code==408:
            attempt.update(status='unknown',http_status=response.status_code)
            job.receipt_json={**job.receipt_json,'attempt_history':copy.deepcopy(history)};session.commit()
            raise AppError(502,'screenplay_provider_unknown',f'模型返回HTTP{response.status_code}，原请求可能已执行，未重试或回退')
        if response.status_code>=300:
            attempt.update(status='rejected',http_status=response.status_code)
            job.receipt_json={**job.receipt_json,'attempt_history':copy.deepcopy(history)};session.commit()
            # Only an explicit 4xx rejection can use a configured, previously
            # verified fallback. Redirects, unknown and successful malformed
            # content never trigger automatic model resubmission.
            next_service=_next_fallback(session,job,snapshot) if 400<=response.status_code<500 else None
            if next_service:snapshot=next_service;continue
            raise AppError(502,'screenplay_provider_rejected',f'模型明确拒绝请求HTTP{response.status_code}；没有允许的备用服务')
        try:result=response.json()
        except ValueError as exc:
            attempt.update(status='invalid_response',http_status=response.status_code)
            job.receipt_json={**job.receipt_json,'attempt_history':copy.deepcopy(history),'provider_response_received':True};session.commit()
            raise AppError(502,'screenplay_provider_invalid','模型已返回结果但不是JSON响应；没有自动再次生成') from exc
        try:
            choice=result['choices'][0];output=choice['message']['content'];finish_reason=choice.get('finish_reason')
            if not isinstance(output,str):raise TypeError('模型响应不是文本')
        except (KeyError,IndexError,TypeError) as exc:
            attempt.update(status='invalid_response',http_status=response.status_code)
            job.receipt_json={**job.receipt_json,'attempt_history':copy.deepcopy(history),'provider_response_received':True};session.commit()
            raise AppError(502,'screenplay_provider_invalid','模型响应缺少可校验的文本内容；响应已保存且不会自动重发') from exc
        safe_chunk=redact(output,secret)
        attempt.update(status='responded',http_status=response.status_code,finish_reason=finish_reason,
            content_sha256=sha(safe_chunk.encode('utf-8')),provider_request_id=safe_metadata(result.get('id'),secret),
            usage=safe_metadata(result.get('usage',{}),secret))
        for key,value in (result.get('usage') or {}).items():
            if isinstance(value,int) and not isinstance(value,bool):usage_totals[key]=usage_totals.get(key,0)+value
        job.receipt_json={**job.receipt_json,'attempt_history':copy.deepcopy(history),'provider_response_received':True,
            'provider_continuation_count':continuation_count,'finish_reason':finish_reason};session.commit()

        if finish_reason=='length':
            if not safe_chunk.strip():
                raise AppError(502,'screenplay_output_truncated','模型因输出长度限制中止且没有返回可续接文本；响应已保存，不会重发完整请求')
            if last_chunk and len(safe_chunk.strip())>=64 and safe_chunk.strip()==last_chunk.strip():
                raise AppError(502,'screenplay_output_truncated','模型续接内容与上一段重复；已保存响应但停止自动续接以防生成循环')
            output_chunks.append(safe_chunk)
            last_chunk=safe_chunk;continuation_count+=1
            continue
        if finish_reason not in {None,'stop'}:
            reason=str(finish_reason)[:80]
            raise AppError(502,'screenplay_provider_finish_reason',f'模型以未完成状态 {reason} 结束；响应已归档，未自动续接或重发')

        result=copy.deepcopy(result)
        result['choices'][0]['message']['content']=''.join((*output_chunks,safe_chunk))
        if usage_totals:result['usage']=usage_totals
        job.receipt_json={**job.receipt_json,'provider_continuation_count':continuation_count,
            'provider_response_received':True,'provider_response_saved':True};session.commit()
        return result,snapshot,secret,response.status_code

async def _process_job(settings,factory,identifier):
    started=time.monotonic();secret='';response_received=False
    with factory() as session:
        job=get_job(session,identifier)
        if job.status!='queued':return
        receipt=job.receipt_json or {}
        saved_response=bool(receipt.get('provider_response_saved') and receipt.get('provider_response_relative_path') and receipt.get('provider_response_sha256'))
        snapshot=receipt.get('actual_service_snapshot') or job.snapshot_json['service_snapshot'];service=session.get(m.AIService,snapshot['service_id'])
        if not saved_response and (not service or not service.enabled or service.revision!=snapshot['service_revision']):
            set_stage(session,job,'blocked','blocked','模型档案修订已变化，未发送请求；请创建新生成任务');return
        if not saved_response and snapshot['is_cloud'] and not job.snapshot_json.get('allow_cloud_costs'):
            set_stage(session,job,'blocked','blocked','未允许云端费用，未发送请求');return
        secret=reveal(service.encrypted_secret) if service and service.encrypted_secret else ''
        asset=session.get(Asset,job.input_asset_id) if job.input_asset_id else None
        if job.snapshot_json.get('reference_context') and not saved_response:
            from app.services.reference_video_analysis import resolve_reference_context
            try:
                resolution=resolve_reference_context(session,job.snapshot_json['reference_context'],profile_name=job.project_name+' · 参考风格')
            except (AppError,KeyError,TypeError,ValueError) as exc:
                set_stage(session,job,'blocked','blocked','冻结参考视频分析不可用，未发送模型请求：'+str(exc)[:1000]);return
            job.receipt_json={**job.receipt_json,'reference_analysis':resolution};session.commit()
            if resolution['state']=='waiting':return
            if resolution['state']!='ready':set_stage(session,job,'blocked','blocked',resolution.get('reason','参考视频分析尚未可用'));return
        set_stage(session,job,'running','preprocessing')
        try:
            if saved_response:
                source=copy.deepcopy(receipt.get('source') or {})
                if not source:raise AppError(502,'screenplay_saved_response_source_missing','模型响应已保存，但原素材处理记录缺失；保留响应且不会重新提交')
                media_parts=[]
                if job.input_mode=='image' and (job.snapshot_json.get('products') or job.snapshot_json.get('input_request',{}).get('asset_ids')):
                    media_parts,_=await asyncio.to_thread(prepare_images,settings,job)
                elif job.input_mode in {'image','video'} and asset:
                    media_parts,_=await asyncio.to_thread(prepare_source,settings,job,asset)
            elif job.snapshot_json.get('reference_context'):
                source={'input_mode':'video','reference_videos':job.snapshot_json['reference_context'],'style_profile_id':resolution['style_profile_id'],'style_context':resolution['context'],'frames':[],'source_kind':'analysed_reference_library','four_stage_status':{'analysis':'succeeded','style_summary':'succeeded','screenplay':'generating','new_video':'not_started'}}
                media_parts=[]
            elif job.input_mode=='image' and (job.snapshot_json.get('products') or job.snapshot_json.get('input_request',{}).get('asset_ids')):
                media_parts,source=await asyncio.to_thread(prepare_images,settings,job)
            else:media_parts,source=await asyncio.to_thread(prepare_source,settings,job,asset)
            job.receipt_json={**job.receipt_json,'source':source}
            if saved_response:
                result,history,chunks,http_status=_load_saved_generation_result(settings,receipt)
                job.receipt_json={**job.receipt_json,'provider_response_received':True,'provider_response_saved':True,
                    'provider_request_submitted':True,'local_response_recovery_attempted':True}
                set_stage(session,job,'running','generating')
                if http_status>=500 or http_status==408:
                    raise AppError(502,'screenplay_provider_unknown',f'模型返回HTTP{http_status}，原请求可能已执行；本地恢复不会重复提交')
                if http_status>=300:
                    next_service=_next_fallback(session,job,snapshot) if 400<=http_status<500 else None
                    if not next_service:
                        raise AppError(502,'screenplay_provider_rejected',f'已保存的模型响应为HTTP{http_status}；没有已授权的备用服务')
                    result,snapshot,secret,http_status=await _call_provider(settings,session,job,media_parts,source,
                        snapshot=next_service,history=history,accumulated_chunks=chunks,
                        continuation_count=len(chunks),last_chunk=chunks[-1] if chunks else '',prior_usage=result.get('usage',{}))
                finish_reason=(result.get('choices') or [{}])[0].get('finish_reason')
                if finish_reason=='length':
                    if not chunks or not chunks[-1].strip():
                        raise AppError(502,'screenplay_output_truncated','已保存响应因输出长度限制中止，但没有可续接文本；不会重新提交完整请求')
                    result,snapshot,secret,http_status=await _call_provider(settings,session,job,media_parts,source,
                        snapshot=snapshot,history=history,accumulated_chunks=chunks,
                        continuation_count=len(chunks),last_chunk=chunks[-1],prior_usage=result.get('usage',{}))
                elif finish_reason not in {None,'stop'}:
                    raise AppError(502,'screenplay_provider_finish_reason',f'已保存模型响应以未完成状态 {str(finish_reason)[:80]} 结束；不会自动续接或重新提交')
            else:
                job.receipt_json={**job.receipt_json,'source':source,'provider_request_submitted':False}
                set_stage(session,job,'running','generating')
                result,snapshot,secret,http_status=await _call_provider(settings,session,job,media_parts,source)
            response_received=True
            output=result['choices'][0]['message']['content']
            if not isinstance(output,str):raise ValueError('模型输出不是文字JSON')
            safe_output=redact(output,secret)
            job.receipt_json={**job.receipt_json,'provider_response_received':True,'provider_http_status':http_status,
                'provider_output_sha256':sha(output.encode()),'usage':safe_metadata(result.get('usage',{}),secret),'provider_request_id':safe_metadata(result.get('id'),secret),
                'runtime':safe_metadata(result.get('runtime',{}),secret),'factory_receipt':safe_metadata(result.get('factory_receipt',{}),secret),
                'model_output_credentials_redacted':safe_output!=output,'finish_reason':result['choices'][0].get('finish_reason'),
                'elapsed_seconds':round(time.monotonic()-started,3)}
            set_stage(session,job,'running','validating')
            if result['choices'][0].get('finish_reason') not in {None,'stop'}:
                raise ValueError('模型输出因长度、时限或服务策略中止；已保留原响应，未伪造完整剧本或自动重发')
            text=safe_output.strip()
            if text.startswith('```'):text=re.sub(r'^```(?:json)?\s*|\s*```$','',text)
            raw=json.loads(text)
            ir=normalize_ir(session,job,raw,source)
            job.screenplay_json=ir;job.receipt_json={**job.receipt_json,'validated_ir_sha256':content_sha(ir)}
            set_stage(session,job,'running','exporting')
            try:await asyncio.to_thread(export_job,session,settings,job)
            except Exception as exc:
                session.rollback();job=get_job(session,identifier)
                set_stage(session,job,'export_failed','failed','Word生成失败，已校验剧本保留；可重试导出而不再调用模型：'+redact(str(exc),secret)[:1000])
        except Exception as exc:
            session.rollback();job=get_job(session,identifier)
            current_receipt=job.receipt_json or {}
            unknown=(isinstance(exc,httpx.TransportError) or getattr(exc,'code','') in {'screenplay_provider_unknown','screenplay_saved_response_corrupt'} or
                current_receipt.get('provider_response_received') and not current_receipt.get('provider_response_saved')) and current_receipt.get('provider_request_submitted') and not response_received
            job.receipt_json={**job.receipt_json,'elapsed_seconds':round(time.monotonic()-started,3),'automatic_retry':False,
                'paid_fallback':job.receipt_json.get('paid_fallback',False)}
            set_stage(session,job,'unknown' if unknown else 'failed','unknown' if unknown else 'failed',redact(str(exc),secret)[:1800])
        finally:
            cleaned=cleanup_temporary_source(settings,job,job.receipt_json.get('source',{}))
            if cleaned.get('four_stage_status'):
                cleaned['four_stage_status']={**cleaned['four_stage_status'],'screenplay':'succeeded' if job.screenplay_json else job.status}
            job.receipt_json={**job.receipt_json,'source':cleaned};session.commit()


def _record_unhandled_worker_failure(factory,identifier,exc):
    """Terminalize exceptions escaping the normal processing boundary.

    In particular, credential decryption happens before the provider-call
    boundary. Without this guard a bad Windows DPAPI context leaves a job
    queued forever and the worker logs the same exception every second.
    """
    with factory() as session:
        job=session.get(Job,identifier)
        if not job or job.status not in {'queued','running'}:return
        receipt=job.receipt_json or {}
        request_submitted=(receipt.get('provider_request_submitted') is True or
            receipt.get('provider_response_received') is True)
        if request_submitted:
            status='unknown'
            if receipt.get('provider_response_saved'):
                message='模型响应已保存，但本地处理发生异常；原请求不会自动重发。请核对响应后再明确决定是否新建重试。'
            else:
                message='模型请求可能已提交，但后台处理发生异常，结果未知；不会自动重发。请核对供应商记录后再明确决定是否新建重试。'
        elif getattr(exc,'code','') in {'secure_store_failed','secure_store_unavailable','secret_migration_required'}:
            status='blocked'
            message='模型服务密钥无法从当前 Windows 用户的安全存储中读取，未发送模型请求。请使用保存该密钥的 Windows 用户上下文运行服务，或在服务设置中重新保存密钥。'
        else:
            status='failed'
            message='剧本任务在模型请求提交前发生后台异常，未发送模型请求。修复错误后可创建新的重试任务。'
        job.receipt_json={**receipt,
            'provider_request_submitted':request_submitted,
            'automatic_retry':False,
            'worker_failure':{'phase':job.stage or 'queued','error_type':type(exc).__name__,
                'error_code':getattr(exc,'code',None)}}
        message+=f'（内部错误类型：{type(exc).__name__}）'
        set_stage(session,job,status,status,message[:1800])


async def process_job(settings,factory,identifier):
    try:
        await _process_job(settings,factory,identifier)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        try:
            _record_unhandled_worker_failure(factory,identifier,exc)
        except Exception as persist_exc:
            logging.getLogger(__name__).error(
                'Could not persist screenplay worker failure for %s (%s)',
                identifier,type(persist_exc).__name__)

def _check_document(session,settings,job,identifier):
    document=session.get(Document,identifier)
    if not document or document.job_id!=job.id:raise AppError(422,'screenplay_document_ownership','文档不属于当前剧本任务')
    path=safe_media_path(settings.media_root,document.relative_path,must_exist=True)
    if file_sha(path)!=document.sha256 or document.screenplay_sha256!=content_sha(job.screenplay_json):raise AppError(409,'screenplay_document_changed','Word或对应剧本发生变化，请重新核对')
    return document,path

def approve(session,settings,identifier,payload):
    job=get_job(session,identifier)
    # A completed create request is safe to repeat even when its old revision is retried.
    if job.project_id and payload.approve and payload.create_project:
        checked,_=_check_document(session,settings,job,payload.document_id)
        if [target.model_dump() for target in payload.reference_targets]!=job.receipt_json.get('project_reference_targets',[]):
            raise AppError(409,'screenplay_project_already_created','项目已经创建；新参考图请到项目工作台明确保存，不会重复建项或静默修改')
        if checked.review_state=='approved':return job
    if job.revision!=payload.expected_revision:raise AppError(409,'screenplay_stale','审核对象已变化，请刷新后重新确认')
    if not payload.approve:raise AppError(422,'screenplay_review_required','请先明确勾选剧本内容审核通过')
    if job.status not in {'awaiting_review','approved'}:raise AppError(409,'screenplay_review_unavailable','当前任务尚未生成可审核Word')
    document,path=_check_document(session,settings,job,payload.document_id)
    if payload.reference_targets and not payload.create_project:raise AppError(422,'screenplay_reference_needs_project','创建项目后才能绑定素材参考')
    try:
        changed=session.execute(update(Job).where(Job.id==job.id,Job.revision==payload.expected_revision).values(revision=Job.revision+1))
        if changed.rowcount!=1:raise AppError(409,'screenplay_stale','其他窗口已审核本任务')
        if payload.create_project and not job.project_id:
            existing=session.scalar(select(m.Project.id).where(m.Project.name==job.project_name).limit(1))
            if existing:raise AppError(409,'screenplay_project_name_conflict','同名项目已存在。请取消建项，或返回使用新的项目名称生成；不会覆盖已有项目',{'project_id':existing})
            claim=NameClaim(name_key=sha(job.project_name.casefold().encode()),project_name=job.project_name,job_id=job.id)
            session.add(claim);session.flush()
            batch=m.WordImport(id=new_id('import'),filename=job.project_name[:180]+'.docx',sha256=document.sha256,status='pending_review',
                relative_path=document.relative_path,blocks_json=read_blocks(path.read_bytes(),settings.import_max_bytes),
                preview_json=copy.deepcopy(job.screenplay_json),revision=1)
            session.add(batch);session.flush()
            result=commit_import(session,batch,ImportConfirm(revision=1,duplicate_strategy='new_project',reuse_approved=False))
            job.project_id=result['project_id'];job.word_import_id=batch.id;claim.project_id=job.project_id
            from app.services.product_library import bind_created_project
            bind_created_project(session,job,result)
            if payload.reference_targets:_adopt_references(session,settings,job,result,payload.reference_targets)
            job.receipt_json={**job.receipt_json,'project_reference_targets':[target.model_dump() for target in payload.reference_targets],
                'project_created_from_document_id':document.id,'project_id':job.project_id,'production_auto_started':False}
            project=session.get(m.Project,job.project_id)
            if project.name!=job.project_name:raise ValueError('Project title differs from exact reviewed value')
            project.source_uri='screenplay_job:'+job.id
            session.add(m.AuditEvent(object_type='project',object_id=project.id,action='screenplay_review_created_project',details_json={
                'job_id':job.id,'document_id':document.id,'ir_sha256':document.screenplay_sha256,'service_snapshot':job.snapshot_json['service_snapshot'],
                'source_asset_id':job.input_asset_id,'rules_versions':job.screenplay_json['versions'],'production_auto_started':False}))
        document.review_state='approved';job.review_state='approved';job.status='approved';job.stage='approved';job.error_message=None
        session.commit();session.refresh(job);return job
    except IntegrityError as exc:
        session.rollback();current=get_job(session,identifier)
        if current.project_id:return current
        raise AppError(409,'screenplay_project_name_conflict','项目名称或建项状态已被其他窗口占用，已保留剧本与Word') from exc
    except Exception:
        session.rollback();raise

def _adopt_references(session,settings,job,result,targets):
    asset=session.get(Asset,job.input_asset_id) if job.input_asset_id else None
    if not asset or asset.input_mode!='image' or any(t.asset_id!=asset.id for t in targets):raise AppError(422,'screenplay_reference_source','只能绑定本次剧本已审核的原始图片')
    if file_sha(safe_media_path(settings.media_root,asset.relative_path,must_exist=True))!=asset.sha256:raise AppError(409,'screenplay_reference_source_changed','创作原图在审核前发生变化，未绑定或创建项目')
    script=session.get(m.ScriptRevision,result['script_id']);bindings=script.parsed_json['identity_bindings']
    scene_map=script.parsed_json.get('scene_bindings') or result.get('scene_bindings') or {}
    if not scene_map:
        # Legacy imports may merge repeated scene declarations. Resolve via the
        # preserved shot keys rather than zipping a deduplicated scene table.
        for source,shot_id in zip(job.screenplay_json['shots'],script.parsed_json.get('explicit_shot_ids',[])):
            detail=session.scalar(select(m.ShotDetail).where(m.ShotDetail.shot_id==shot_id))
            if detail and detail.scene_id:scene_map[source['scene_key']]=detail.scene_id
    seen=set()
    for target in targets:
        key=(target.target_kind,target.target_key,target.purpose)
        if key in seen:raise AppError(422,'screenplay_reference_duplicate','不能重复绑定同一用途的参考素材')
        seen.add(key)
        if target.target_kind=='character':
            if target.target_key not in bindings:raise AppError(422,'screenplay_reference_target','参考人物不属于当前剧本')
            # One supplied face must never silently become every role's identity.
            if len({t.target_key for t in targets if t.target_kind=='character'})>1:raise AppError(422,'screenplay_reference_identity_isolation','同一人物参考图不能自动绑定多个不同角色，请只选择对应人物')
            from app.services.character_reference import adopt_source_reference
            adopt_source_reference(session,settings,job.project_id,bindings[target.target_key]['character_id'],asset.relative_path,asset.sha256,
                purpose=target.purpose,source_note='用户审核剧本并选入创作原图；job='+job.id+'; source_asset='+asset.id)
        else:
            if target.target_key not in scene_map:raise AppError(422,'screenplay_reference_target','参考场景不属于当前剧本')
            session.add(m.ProductionVisual(project_id=job.project_id,scene_id=scene_map[target.target_key],kind='scene_reference',status='approved',
                prompt='用户审核选入的原始创作素材，仅作为场景视觉参考',relative_path=asset.relative_path,sha256=asset.sha256,
                snapshot_json={'source':'user_upload','screenplay_job_id':job.id,'source_asset_id':asset.id,'image_mode':'user_upload','references':[]},
                review_json={'status':'approved','reason':'用户在剧本审核建项时明确勾选原图作为此场景参考','operator':'local-user'}))

def retry_export(session,settings,identifier,revision):
    job=get_job(session,identifier)
    if job.revision!=revision:raise AppError(409,'screenplay_stale','任务已变化，请刷新')
    if job.status!='export_failed':raise AppError(409,'screenplay_export_retry_unavailable','只有Word导出失败可重试，无需重复模型生成')
    set_stage(session,job,'running','exporting')
    try:export_job(session,settings,job)
    except Exception as exc:
        session.rollback();job=get_job(session,identifier);set_stage(session,job,'export_failed','failed','Word导出仍失败：'+str(exc)[:1000])
    return job

def export_new_version(session,settings,identifier,revision):
    job=get_job(session,identifier)
    if job.revision!=revision:raise AppError(409,'screenplay_stale','剧本已变化，请刷新后重新导出')
    if job.status not in {'awaiting_review','approved','export_failed'} or not job.screenplay_json:
        raise AppError(409,'screenplay_export_unavailable','当前没有可重新导出的已校验剧本')
    # This changes only a readable document version. The existing project and
    # its original WordImport/ScriptRevision remain bound to their old source.
    set_stage(session,job,'running','exporting')
    try:export_job(session,settings,job)
    except Exception as exc:
        session.rollback();job=get_job(session,identifier)
        set_stage(session,job,'export_failed','failed','Word新版本导出失败，旧文档及其审核记录保留：'+str(exc)[:1000])
    return job

async def screenplay_tick(settings,factory):
    with factory() as session:
        local_identifier=cloud_identifier=None
        candidates=session.scalars(select(Job).where(Job.status=='queued').order_by(Job.created_at,Job.id).execution_options(yield_per=64))
        for candidate in candidates:
            frozen=candidate.snapshot_json.get('reference_context')
            analysis_ids=frozen.get('analysis_ids') if isinstance(frozen,dict) else None
            if isinstance(analysis_ids,list) and analysis_ids and all(isinstance(identifier,str) for identifier in analysis_ids):
                analyses=list(session.scalars(select(m.VideoAnalysis).where(m.VideoAnalysis.id.in_(analysis_ids))))
                # Only known, incomplete analyses are a wait. A missing or
                # rejected source must reach process_job's explicit block.
                if (len(analyses)==len(set(analysis_ids)) and
                    not any(a.status in {'failed','blocked','unknown'} for a in analyses) and
                    any(a.status!='succeeded' for a in analyses)):
                    continue
            if candidate.snapshot_json['service_snapshot']['is_cloud']:
                if cloud_identifier is None:cloud_identifier=candidate.id
            elif local_identifier is None:
                local_identifier=candidate.id
            if local_identifier and cloud_identifier:break
    # Cloud authoring does not consume the local GPU. Let it proceed when an
    # older local job is waiting on the local runtime or the shared GPU.
    if local_identifier:
        handled=await _screenplay_local_tick(settings,factory,local_identifier)
        if handled:return
    if cloud_identifier:
        await process_job(settings,factory,cloud_identifier)

@gpu_submission
async def _screenplay_local_tick(settings,factory,identifier):
    with factory() as session:
        job=session.get(Job,identifier)
        if not job or job.status!='queued' or job.snapshot_json['service_snapshot']['is_cloud']:return False
        active=['queued','submitting','submitted','running','cancelling']
        if session.scalar(select(m.GenerationJob.id).where(m.GenerationJob.provider=='comfyui_h3',m.GenerationJob.status.in_(active)).limit(1)):return False
        if any(session.scalar(select(kind.id).where(kind.status.in_(active)).limit(1)) for kind in [m.CharacterGenerationJob,m.ProductionVisual,m.MediaProcessTask,m.QualityRender]):return False
    from app.services.gpu_gate import prepare_local_llm_gpu
    try:
        if await prepare_local_llm_gpu(settings) is None:return False
    except Exception as exc:
        with factory() as session:set_stage(session,get_job(session,identifier),'blocked','blocked','GPU释放前检查未通过'+str(exc)[:1000])
        return False
    await process_job(settings,factory,identifier)
    return True

def recover_interrupted_jobs(factory):
    with factory() as session:
        for job in session.scalars(select(Job).where(Job.status=='running')):
            receipt=job.receipt_json or {}
            if job.screenplay_json and job.stage=='exporting':
                job.status='export_failed';job.stage='failed';job.error_message='Word导出进程中断，已保留剧本；可显式重试导出'
            elif (receipt.get('provider_response_saved') and receipt.get('provider_response_relative_path') and receipt.get('provider_response_sha256')):
                job.status='queued';job.stage='queued';job.error_message='模型响应已安全保存；已重新排队进行本地校验，不会再次调用模型'
            elif receipt.get('provider_request_submitted') is False or (receipt.get('provider_request_submitted') is None and job.stage in {'queued','preprocessing'}):
                job.status='queued';job.stage='queued';job.error_message='服务中断发生在模型请求提交前；已安全重新排队'
            else:
                job.status='unknown';job.stage='unknown'
                if receipt.get('provider_response_received') and receipt.get('provider_response_relative_path'):
                    job.error_message='模型响应已收到并保存，但本地校验流程中断；不会重复提交。可核对请求记录后明确新建重试'
                else:
                    job.error_message='模型请求期间服务中断，结果未知；没有自动重发或付费回退。可核对请求记录后明确新建重试'
            job.revision+=1
        session.commit()


async def screenplay_worker(settings,factory):
    recover_interrupted_jobs(factory)
    while True:
        try:await screenplay_tick(settings,factory)
        except asyncio.CancelledError:raise
        except Exception:logging.getLogger(__name__).exception('Screenplay worker failed; no automatic model resubmission')
        await asyncio.sleep(1)


def batch_cancel(session, settings, payload):
    from app.schemas_screenplay_generation import BatchCancelScreenplay
    from app.models.screenplay_generation import ScreenplayGenerationJob as Job, ScreenplayDocument as Document
    results = []
    for job_id in payload.ids:
        job = session.get(Job, job_id)
        if job is None:
            results.append({'id': job_id, 'ok': False, 'error': 'job_not_found'})
            continue
        if job.status=='unknown':
            results.append({'id':job_id,'ok':False,'error':'unknown_outcome_requires_explicit_retry'})
            continue
        if job.status not in {'queued','running'}:
            results.append({'id':job_id,'ok':False,'error':f'status_{job.status}_not_cancellable'})
            continue
        if job.status=='running' and (job.receipt_json or {}).get('provider_request_submitted'):
            set_stage(session,job,'unknown','unknown','模型请求已经提交；批量中止无法停止供应商执行，结果未知。核对后可明确新建重试')
            results.append({'id':job_id,'ok':False,'error':'provider_request_already_submitted_outcome_unknown'})
            continue
        set_stage(session, job, 'cancelled', 'cancelled', '用户在模型请求提交前批量中止')
        results.append({'id': job_id, 'ok': True})
    return results


def batch_delete(session, settings, payload):
    import shutil
    from sqlalchemy import update as sql_update
    from app.models.screenplay_generation import (ScreenplayGenerationJob as Job,
        ScreenplayDocument as Document, ScreenplayInputAsset as Asset,
        ScreenplayProjectNameClaim as NameClaim)
    from app.core.paths import safe_media_path
    results = []
    ids_to_delete = list(payload.ids)
    for job_id in ids_to_delete:
        job = session.get(Job, job_id)
        if job is None:
            results.append({'id': job_id, 'ok': False, 'error': 'job_not_found'})
            continue
        try:
            # 1. Nullify self-referential parent_job_id on child jobs that point here
            #    (only if parent is not also in the delete batch — those will be deleted too)
            children = session.scalars(select(Job).where(Job.parent_job_id == job_id)).all()
            for child in children:
                if child.id not in ids_to_delete:
                    child.parent_job_id = None
            session.flush()

            # 2. Nullify project_product_bindings.source_job_id FK
            try:
                from app.models import project_product_bindings as ppb_model
                session.execute(
                    sql_update(ppb_model.ProjectProductBinding)
                    .where(ppb_model.ProjectProductBinding.source_job_id == job_id)
                    .values(source_job_id=None)
                )
            except Exception:
                # model path may differ; fall back to raw SQL
                from sqlalchemy import text
                session.execute(
                    text('UPDATE project_product_bindings SET source_job_id = NULL WHERE source_job_id = :jid'),
                    {'jid': job_id}
                )
            session.flush()

            # 3. Delete on-disk documents and job directory
            docs = session.scalars(select(Document).where(Document.job_id == job_id)).all()
            try:
                job_dir = settings.media_root / 'screenplay_jobs' / job_id
                if job_dir.exists():
                    shutil.rmtree(job_dir, ignore_errors=True)
            except Exception:
                pass
            for doc in docs:
                if doc.relative_path:
                    try:
                        p = safe_media_path(settings.media_root, doc.relative_path)
                        if p.exists():
                            p.unlink(missing_ok=True)
                    except Exception:
                        pass

            # 4. Remove orphaned input asset file
            if job.input_asset_id:
                asset = session.get(Asset, job.input_asset_id)
                if asset:
                    sibling_count = session.scalar(
                        select(func.count(Job.id)).where(
                            Job.input_asset_id == asset.id,
                            Job.id != job_id
                        )
                    )
                    if sibling_count == 0:
                        if asset.relative_path:
                            try:
                                p = safe_media_path(settings.media_root, asset.relative_path)
                                if p.exists():
                                    p.unlink(missing_ok=True)
                            except Exception:
                                pass
                        session.delete(asset)
                        session.flush()

            # 5. Delete DB rows: documents → name claim → job
            for doc in docs:
                session.delete(doc)
            session.flush()
            claim = session.scalar(select(NameClaim).where(NameClaim.job_id == job_id))
            if claim:
                session.delete(claim)
                session.flush()
            session.delete(job)
            session.commit()
            results.append({'id': job_id, 'ok': True})
        except Exception as exc:
            session.rollback()
            results.append({'id': job_id, 'ok': False, 'error': str(exc)[:400]})
    return results


def batch_resume(session, settings, payload):
    from app.schemas_screenplay_generation import BatchResumeScreenplay
    from app.models.screenplay_generation import ScreenplayGenerationJob as Job
    results = []
    for job_id in payload.ids:
        job = session.get(Job, job_id)
        if job is None:
            results.append({'id': job_id, 'ok': False, 'error': 'job_not_found'})
            continue
        if job.status != 'cancelled':
            results.append({'id': job_id, 'ok': False, 'error': f'status_{job.status}_not_resumable'})
            continue
        try:
            from app.schemas_screenplay_generation import ResumeScreenplay
            import uuid
            resume_payload = ResumeScreenplay(
                expected_revision=job.revision,
                idempotency_key=f'batchresume_{job_id}_{uuid.uuid4().hex[:16]}',
                allow_cloud_costs=payload.allow_cloud_costs,
                allow_paid_fallback=payload.allow_paid_fallback,
            )
            new_job = resume_generation(session, settings, job_id, resume_payload)
            results.append({'id': job_id, 'ok': True, 'new_id': new_job.id})
        except AppError as exc:
            session.rollback()
            results.append({'id':job_id,'ok':False,'error':exc.code})
        except Exception as exc:
            session.rollback()
            results.append({'id': job_id, 'ok': False, 'error': str(exc)[:400]})
    return results
