"""Explainable instruction preflight; no invented visual quality scores."""
import re
import copy
import json
from sqlalchemy import select
from app import models as m
from app.services.cinematic_rules import rules_bundle,fingerprint


def issue(code,severity,message,shot_key=None,source_ids=None,repair=None):
    return {'code':code,'severity':severity,'message':message,'shot_key':shot_key,'source_ids':source_ids or [],'repair':repair}


def inspect_ir(ir):
    rules=ir.get('rules_snapshot') or rules_bundle();findings=[];previous_props={};previous_looks={}
    people={p['key']:p for p in ir.get('characters',[])};designs={d['key']:d for d in ir.get('designs',[])}
    for warning in ir.get('parse_warnings',[]):
        if warning['kind'] in ('suggested_split','suggested_attribution','unresolved_semantics'):
            findings.append(issue(warning['kind'],'review',warning['message'],source_ids=[warning.get('source','')]))
    for person in people.values():
        if person.get('decision')=='pending':findings.append(issue('identity_ambiguous','blocker',person['name']+'身份存在歧义，需要按稳定ID确认',source_ids=person.get('source',[])))
    for shot in ir.get('shots',[]):
        key=shot['key'];source=shot.get('source',[]);participants={p['character_key']:p for p in shot.get('participants',[])}
        prod=shot.get('production',{});reason=prod.get('state_change_reason','')
        if not participants and not shot.get('products'):findings.append(issue('cast_unknown','review','本镜人物尚未确定，保留原文等待确认',key,source))
        for p in participants.values():
            action=p.get('performance',{}).get('action','');pieces=[v for v in re.split(r'[，；;]|然后|接着|随后|并且|再',action) if v.strip()]
            if len(pieces)>rules['defaults']['max_primary_actions']:
                findings.append(issue('complex_action','review','同一人物包含多个主要动作，建议拆镜或提供动作节拍；原文未自动改写',key,p.get('source',[]),'action'))
            if not action:findings.append(issue('action_unspecified','suggestion','有参与人物尚未写明主动作，可选择静止观察或补充动作',key,p.get('source',[]),'action'))
        for line in shot.get('dialogues',[]):
            if line['character_key'] not in participants:findings.append(issue('speaker_not_cast','blocker','对白说话人不属于本镜人物',key,line.get('source',[])))
        beats={}
        for beat in prod.get('beats',[]):
            if beat['character_key'] not in participants or beat['end_seconds']>shot['duration_seconds']:
                findings.append(issue('beat_out_of_range','blocker','动作节拍的主体或时间不属于本镜',key,beat.get('source_ids',[]),'action'))
            existing=beats.setdefault(beat['character_key'],[])
            if any(beat['start_seconds']<b['end_seconds'] and beat['end_seconds']>b['start_seconds'] for b in existing):findings.append(issue('overlapping_actions','review','同一人物的动作节拍重叠，需要明确是否同步',key,beat.get('source_ids',[]),'action'))
            existing.append(beat)
        for person,state in prod.get('wardrobe_states',{}).items():
            design=designs.get(state.get('wardrobe_key'))
            if design is None:findings.append(issue('wardrobe_undefined','blocker','本镜服装ID没有对应造型记录',key,state.get('source_ids',[]),'wardrobe'))
            elif design.get('character_key') and design['character_key']!=person:findings.append(issue('wardrobe_owner','blocker','服装造型属于另一人物',key,state.get('source_ids',[]),'wardrobe'))
            old=previous_looks.get(person);current={k:v for k,v in state.items() if k!='source_ids'}
            if old and old!=current and not reason:findings.append(issue('wardrobe_state_jump','review','相邻镜头服装/干湿等状态变化未说明原因',key,state.get('source_ids',[]),'wardrobe'))
            previous_looks[person]=current
        for state in prod.get('prop_states',[]):
            identifier=state.get('prop_key');old=previous_props.get(identifier,{})
            if identifier not in designs:findings.append(issue('prop_undefined','blocker','本镜道具ID尚未定义',key,state.get('source_ids',[]),'prop'))
            for field in ('holder','hand','location','open_state','wetness','damage'):
                if field in old and field in state and old[field]!=state[field] and not reason:
                    findings.append(issue('prop_state_jump','review',f'道具 {identifier} 的 {field} 从“{old[field]}”变成“{state[field]}”，未说明衔接',key,state.get('source_ids',[]),'prop'))
            previous_props[identifier]={**old,**{k:v for k,v in state.items() if k!='source_ids'}}
        if not shot.get('photography',{}):findings.append(issue('camera_optional','suggestion','摄影细节未提供，固定机位仅为已标注建议',key,source))
    return {'findings':findings,'counts':{s:sum(f['severity']==s for f in findings) for s in ['blocker','review','suggestion']},'confidence_note':'置信度代表提取依据；没有自动电影质量评分',
            'estimate':{'characters':len(people),'scene_references':len(ir.get('scenes',[])),'keyframes':len(ir.get('shots',[])),'video_jobs':len(ir.get('shots',[])),'requested_video_seconds':sum(s['duration_seconds'] for s in ir.get('shots',[])),'eta_seconds':None,'cloud_cost':None,'note':'本机GPU串行；耗时/费用无经过验证的估算模型'}}


def production_context(session,shot):
    detail=session.scalar(select(m.ShotDetail).where(m.ShotDetail.shot_id==shot.id))
    scene=session.get(m.Scene,detail.scene_id) if detail and detail.scene_id else None
    script=session.get(m.ScriptRevision,scene.script_revision_id) if scene and scene.script_revision_id else None
    production=(detail.source_json or {}).get('production',{}) if detail else {}
    ids=[production.get('scene_state_id')]+[v.get('entity_id') for v in production.get('wardrobe_states',{}).values()]+[v.get('entity_id') for v in production.get('prop_states',[])]
    entities=[session.get(m.ProductionEntity,k) for k in dict.fromkeys(ids) if k]
    return detail,scene,script,production,entities


def database_ir(session,script,shots):
    entities=list(session.scalars(select(m.ProductionEntity).where(m.ProductionEntity.project_id==script.project_id)))
    people={};scenes={};result=[]
    for shot in shots:
        detail,scene,_,prod,_=production_context(session,shot)
        if scene:scenes[scene.id]={'key':scene.id,'name':scene.name,'environment':scene.environment_json,'source':scene.source_json.get('blocks',[])}
        participants=[]
        for p in session.scalars(select(m.ShotCharacter).where(m.ShotCharacter.shot_id==shot.id)):
            c=session.get(m.Character,p.character_id);people[c.id]={'key':c.id,'name':c.display_name,'decision':'existing','profile':{},'source':[]}
            participants.append({'character_key':c.id,'performance':p.performance_json})
        production=copy.deepcopy(prod)
        production['wardrobe_states']={k:{**v,'wardrobe_key':v.get('entity_id')} for k,v in prod.get('wardrobe_states',{}).items()}
        production['prop_states']=[{**v,'prop_key':v.get('entity_id')} for v in prod.get('prop_states',[])]
        production['beats']=[{**v,'character_key':v.get('character_id',v.get('character_key'))} for v in prod.get('beats',[])]
        lines=[]
        for line in session.scalars(select(m.DialogueLine).where(m.DialogueLine.shot_id==shot.id)):
            speaker=session.get(m.ShotCharacter,line.participant_id);lines.append({'character_key':speaker.character_id,'text':line.text})
        from app.services.product_library import shot_product_snapshot
        result.append({'key':shot.id,'title':shot.scene_title,'scene_key':scene.id if scene else '', 'duration_seconds':shot.duration_seconds,'participants':participants,'dialogues':lines,'photography':detail.photography_json if detail else {},'production':production,
                       'products':shot_product_snapshot(session,shot)})
    return {'characters':list(people.values()),'scenes':list(scenes.values()),'shots':result,'designs':[{'key':e.id,'kind':e.kind,'character_key':e.character_id,'spec':e.spec_json} for e in entities],'rules_snapshot':script.parsed_json.get('rules_snapshot') or rules_bundle()}


def visual_inputs(session,shot):
    detail,scene,script,production,entities=production_context(session,shot)
    participants=list(session.scalars(select(m.ShotCharacter).where(m.ShotCharacter.shot_id==shot.id).order_by(m.ShotCharacter.order,m.ShotCharacter.id)))
    from app.services.product_library import shot_product_snapshot
    products=shot_product_snapshot(session,shot)
    motion_state={}
    if production.get('motion_references'):
        from app.services.motion_references import motion_live_state
        motion_state={'motion_reference_state':motion_live_state(session,production['motion_references'])}
    return {'shot_id':shot.id,'visual_prompt':shot.visual_prompt,'action':shot.action,'camera_move':shot.camera_move,'lighting':shot.lighting,
        'photography':detail.photography_json if detail else {},'environment':scene.environment_json if scene else {},
        'production':{k:v for k,v in production.items() if k!='visual_ids'},
        'entities':[{'id':e.id,'logical_id':e.logical_id,'version':e.version,'kind':e.kind,'spec':e.spec_json} for e in entities if e],
        'cast':[{'character_id':p.character_id,'version_id':p.character_version_id,'reference_asset_ids':p.reference_asset_ids_json,'performance':p.performance_json} for p in participants],
        'versions':script.parsed_json.get('versions',{}) if script else {},**({'products':products} if products else {}),
        **motion_state}


def generation_inputs_hash(session,shot):
    detail,scene,_,_,_=production_context(session,shot)
    dialogue=[{k:getattr(line,k) for k in ['participant_id','order','text','delivery_json']} for line in session.scalars(select(m.DialogueLine).where(m.DialogueLine.shot_id==shot.id).order_by(m.DialogueLine.order,m.DialogueLine.id))]
    audio=[{k:getattr(event,k) for k in ['kind','description','start_seconds','end_seconds','volume','relative_path','source_json']} for event in session.scalars(select(m.AudioEvent).where((m.AudioEvent.shot_id==shot.id)|(m.AudioEvent.scene_id==scene.id if scene else m.AudioEvent.id=='__none__')).order_by(m.AudioEvent.id))]
    return fingerprint({'visual':visual_inputs(session,shot),'duration':shot.duration_seconds,'aspect_ratio':shot.aspect_ratio,'dialogue':dialogue,'audio':audio})


def shot_preflight(session,settings,shot,require_visual=True):
    from app.services.screenplay import shot_bundle
    from app.core.paths import safe_media_path
    detail,scene,script,prod,entities=production_context(session,shot);findings=[];bundle=shot_bundle(session,shot.id)
    enabled=bool(script and script.parsed_json.get('versions'))
    if enabled:
        siblings=list(session.scalars(select(m.StoryboardShot).join(m.ShotDetail,m.ShotDetail.shot_id==m.StoryboardShot.id).join(m.Scene,m.Scene.id==m.ShotDetail.scene_id).where(m.Scene.script_revision_id==script.id).order_by(m.StoryboardShot.order,m.StoryboardShot.id)))
        report=inspect_ir(database_ir(session,script,siblings))
        findings.extend(f for f in report['findings'] if f['shot_key'] in [None,shot.id] and f['severity'] in ['blocker','review'])
    if not enabled:findings.append(issue('legacy_rules','review','旧镜头未采用电影规范；须主动建立新修订以应用新规范',shot.id))
    for p in bundle['participants']:
        if p['version_state']!='approved' or not p['reference_asset_ids_json']:findings.append(issue('identity_not_ready','blocker',p['name']+'缺少正式身份或已审核参考',shot.id,repair='identity'))
    if bundle['participants']:
        try:
            from app.services.identity_references import actor_references
            actor_references(session,settings,bundle['participants'])
        except Exception as exc:
            from app.core.errors import AppError
            if isinstance(exc,AppError):findings.append(issue(exc.code,'blocker',exc.message,shot.id,repair='identity'))
            else:raise
    from app.services.product_library import shot_references,shot_product_snapshot
    products=shot_product_snapshot(session,shot);product_references=[]
    if products:
        try:
            product_references=shot_references(session,settings,shot)
            if not product_references:findings.append(issue('product_reference_missing','blocker','产品镜头缺少有效的已绑定参考图片',shot.id))
        except Exception as exc:
            from app.core.errors import AppError
            if isinstance(exc,AppError):findings.append(issue(exc.code,'blocker',exc.message,shot.id,repair='product'))
            else:raise
    if not bundle['participants'] and not products:findings.append(issue('cast_not_ready','blocker','镜头尚无可确认人物或产品绑定',shot.id))
    for e in entities:
        if e is None:findings.append(issue('design_missing','blocker','当前绑定的制作设定已不存在',shot.id))
        elif e.project_id!=shot.project_id:findings.append(issue('design_project_mismatch','blocker','制作设定属于其他项目',shot.id))
        elif e.status!='approved':findings.append(issue('design_unreviewed','review',e.name+' v'+str(e.version)+'尚未审核确认',shot.id,repair='wardrobe' if e.kind=='wardrobe' else 'scene' if e.kind=='scene_state' else 'prop'))
    beats=prod.get('beats',[])
    if prod.get('motion_references'):
        try:
            from app.services.motion_references import resolve_motion_references
            resolve_motion_references(session,settings,shot,prod['motion_references'])
            findings.append(issue('motion_hash_checked_on_submit','suggestion','动作参考文件将在提交时重新核验 SHA；预检只确认素材版本与路径',shot.id,repair='action'))
        except Exception as exc:
            from app.core.errors import AppError
            if isinstance(exc,AppError):findings.append(issue(exc.code,'blocker',exc.message,shot.id,repair='action'))
            else:raise
    for beat in beats:
        if beat.get('end_seconds',0)>shot.duration_seconds:findings.append(issue('beat_out_of_range','blocker','动作节拍超过当前镜头时长',shot.id,repair='action'))
    for control,value in prod.get('requested_controls',{}).items():
        if value and control in ['first_frame','last_frame','exact_lip_sync','voice_clone','aperture','shutter']:
            findings.append(issue('unsupported_control','blocker','当前H3工作流未验证精确控制：'+control,shot.id))
    visuals=[];input_hash=fingerprint(visual_inputs(session,shot))
    if len(prod.get('visual_ids',[]))!=len(set(prod.get('visual_ids',[]))):findings.append(issue('duplicate_visual_reference','blocker','视觉引用包含重复ID，未重复送给模型',shot.id))
    for identifier in prod.get('visual_ids',[]):
        visual=session.get(m.ProductionVisual,identifier)
        if not visual or visual.project_id!=shot.project_id or visual.status!='approved':findings.append(issue('visual_not_approved','blocker','所选视觉参考未批准或不属于此项目',shot.id));continue
        if visual.kind=='scene_reference' and (not scene or visual.scene_id!=scene.id or visual.entity_id!=prod.get('scene_state_id') or visual.snapshot_json.get('visual_inputs',{}).get('environment')!=scene.environment_json):findings.append(issue('scene_reference_mismatch','blocker','场景参考与本镜场景状态或环境不符，请选择对应版本',shot.id));continue
        if visual.shot_id and visual.shot_id!=shot.id:findings.append(issue('keyframe_wrong_shot','blocker','关键画面属于其他镜头',shot.id));continue
        if visual.kind=='keyframe' and visual.snapshot_json.get('inputs_hash')!=input_hash:findings.append(issue('keyframe_stale','blocker','关键画面对应旧版表演/造型/场景，请重新确认或生成当前版本',shot.id));continue
        try:
            import hashlib
            path=safe_media_path(settings.media_root,visual.relative_path,must_exist=True)
            if hashlib.sha256(path.read_bytes()).hexdigest()!=visual.sha256:raise ValueError()
        except Exception:findings.append(issue('visual_file_invalid','blocker','视觉参考文件缺失或哈希变化',shot.id));continue
        visuals.append(visual)
    if enabled and require_visual and not any(v.kind=='keyframe' for v in visuals):findings.append(issue('keyframe_review_required','review','先生成并审核本镜关键画面，再提交电影镜头',shot.id))
    policy=prod.get('requested_controls',{}).get('identity_reference_policy','full_reference')
    if policy not in {'full_reference','independent_faces_scene'}:findings.append(issue('identity_reference_policy_unsupported','blocker','未实现此身份参考路由',shot.id))
    reference_count=sum(len(p['reference_asset_ids_json']) for p in bundle['participants'])+len(product_references)+sum(not(policy=='independent_faces_scene' and v.kind=='keyframe') for v in visuals)
    if reference_count>9:findings.append(issue('reference_limit','blocker','H3实际最多9张参考，本镜超过上限',shot.id))
    acceptance=(detail.source_json or {}).get('studio_acceptance',{}) if detail else {}
    if acceptance.get('actor')=='automation' and acceptance.get('inputs_hash')==input_hash:
        # Studio records these editorial cautions for final video review. This
        # acknowledgement never certifies likeness and never suppresses blockers.
        findings=[{**f,'severity':'suggestion','automation_acknowledged':True,'original_severity':'review'} if f['severity']=='review' and f['code']!='keyframe_review_required' else f for f in findings]
    return {'shot_id':shot.id,'enabled':enabled,'versions':script.parsed_json.get('versions',{}) if script else {},'inputs_hash':input_hash,'generation_inputs_hash':generation_inputs_hash(session,shot),'findings':findings,'counts':{s:sum(f['severity']==s for f in findings) for s in ['blocker','review','suggestion']},'ready':not any(f['severity'] in ['blocker','review'] for f in findings),'reference_count':reference_count,'visual_ids':[v.id for v in visuals]}


def inspect_generated(job,checks):
    cinematic=job.parameters_json.get('screenplay_snapshot',{}).get('cinematic')
    if not cinematic:return []
    decode=next((c['details'] for c in checks if c['check_type']=='media_decode' and c['status']=='passed'),None)
    if not decode:return [{'check_type':'cinematic_output_spec','status':'failed','details':{'reason':'media_decode_not_passed'}}]
    from fractions import Fraction
    from math import isfinite
    stream=next((s for s in decode.get('streams',[]) if s.get('codec_type')=='video'),{});parameters=job.parameters_json
    cloud=parameters.get('service_snapshot',{}).get('provider')=='video_api'
    expected_frames=max(1,round(parameters['duration_seconds']*24))
    if not cloud:expected_frames=max(5,expected_frames);expected_frames+=(5-expected_frames%17)%17
    def number(value, parse):
        try:
            result=parse(value)
            return result if isfinite(result) and result>0 else None
        except (ValueError, TypeError, ZeroDivisionError, OverflowError):
            return None
    counted=number(stream.get('nb_read_frames'),int)
    reported=number(stream.get('nb_frames'),int)
    actual={'width':number(stream.get('width'),int),'height':number(stream.get('height'),int),
            'fps':number(stream.get('avg_frame_rate'),lambda value: float(Fraction(value))),
            'frames':counted if counted is not None else reported,
            'duration':number((decode.get('format') or {}).get('duration'),float)}
    expected={'width':int(parameters['width']),'height':int(parameters['height']),'fps':24,'frames':expected_frames}
    missing=[key for key,value in actual.items() if value is None]
    valid=not missing and all(actual[k]==v for k,v in expected.items()) and abs(actual['duration']-expected_frames/24)<.1 and (not cloud or actual['duration'] >= expected_frames/24)
    # Some video services align dimensions or return a few extra frames.
    # Keep all media and expose the deviation for human review. Never accept
    # short output, wrong orientation, large ratio/size error or missing data.
    def offset(value):
        try:
            result=float(value)
            return result if isfinite(result) and 0 <= result <= .5 else None
        except (ValueError,TypeError,OverflowError):
            return None
    size_delta = {k: abs(actual[k]-expected[k]) for k in ('width','height')} if not missing else None
    ratio_error = abs((actual['width']/actual['height'])/(expected['width']/expected['height'])-1) if not missing else None
    dimensions_ok = (not missing and max(size_delta.values()) <= 16 and ratio_error <= .01
                     and (actual['width'] > actual['height']) == (expected['width'] > expected['height'])
                     and (actual['width'] < actual['height']) == (expected['width'] < expected['height']))
    stream_duration=number(stream.get('duration'),float)
    stream_start=offset(stream.get('start_time'))
    stream_timing_ok=(stream_duration is not None and stream_start is not None and not missing
        and abs(stream_duration-actual['frames']/24) <= .1
        and stream_start+stream_duration <= actual['duration'] + 1/24)
    for audio in (s for s in decode.get('streams',[]) if s.get('codec_type')=='audio'):
        a_duration=number(audio.get('duration'),float); a_start=offset(audio.get('start_time'))
        stream_timing_ok = stream_timing_ok and a_duration is not None and a_start is not None and a_start+a_duration <= actual['duration']+1/24
    # Larger timing tails and any dimension mismatch need verifiable stream
    # timestamps, to avoid mistaking corrupted metadata for normal alignment.
    needs_timing=not missing and (size_delta != {'width':0,'height':0} or actual['duration']>expected_frames/24+.5)
    warning=(cloud and not missing and not valid and dimensions_ok and actual['fps']==expected['fps']
             and expected_frames <= actual['frames'] <= expected_frames + 12
             and expected_frames/24 <= actual['duration'] <= expected_frames/24 + 1
             and (not needs_timing or stream_timing_ok))
    result='passed' if valid else 'warning' if warning else 'failed'
    return [{'check_type':'cinematic_output_spec','status':result,'details':{'actual':actual,'expected':expected,'unavailable_fields':missing,'frame_count_source':'nb_read_frames' if counted is not None else 'nb_frames' if reported is not None else None,'frame_quantization':'cloud contract: requested seconds at 24fps; no H3 quantization' if cloud else '17n+5, native H3 workflow','duration_policy':'complete original; actual size and duration require human review' if warning else None,'alignment':{'pixel_delta':size_delta,'relative_aspect_error':ratio_error,'stream_timing_verified':bool(stream_timing_ok)}}},
        {'check_type':'cinematic_visual_continuity','status':'not_configured','details':{'identity':'manual review required','hands_and_contact':'manual review required','flicker_motion':'manual review required','costume_scene_audio':'manual review required','numeric_film_quality_score':None}}]


def physical_directions(context,performer_names=None):
    """Compile explicit contact constraints rather than expecting JSON to steer them."""
    production=context.get('production',{});entities={e['id']:e for e in context.get('entities',[])};result=[]
    contact_text=' '.join(str(b.get('action',''))+' '+str(b.get('contact_target','')) for b in production.get('beats',[]))+' '+str(context.get('action',''))
    handoff=bool(re.search(r'递|接收|交接|传递|接过|hand.?off|\bpass|\bgive|\breceiv|\btransfer|\btake',contact_text,re.I))
    for state in production.get('prop_states',[]):
        entity=entities.get(state.get('entity_id'),{});name=entity.get('logical_id',state.get('prop_key','prop'))
        holder=state.get('holder','specified holder');index=(performer_names or {}).get(holder)
        if index:holder=f'the performer from reference image {index} ({holder})'
        hand={'右手':'RIGHT hand','左手':'LEFT hand'}.get(state.get('hand'),state.get('hand','unspecified hand'))
        closure={'合上':'CLOSED, covers together, no visible inner pages','关闭':'CLOSED','打开':'OPEN'}.get(state.get('open_state'),state.get('open_state','unspecified'))
        ownership=(f'Initial state: {holder} holds it with the {hand} at {state.get("location","specified position")}. During the explicitly described handoff, the declared receiver may contact and receive this SAME single object; follow the timed giver/contact/release/receiver-hold beats, never create a second copy.' if handoff else f'Only {holder} holds this object, with the {hand}, at {state.get("location","specified position")}. Performers without any declared object assignment keep their hands free; other declared object holders retain their own separately assigned props. Preserve this static state; looking at an object does not mean taking or opening it.')
        result.append(f'CONTACT AND OBJECT COUNT: exactly ONE physical {name}, never duplicated. '+ownership+f' Object state: {closure}. Material and appearance: '+json.dumps(entity.get('spec',{}),ensure_ascii=False))
    return '\n'.join(result)


def compile_context(session,shot,settings):
    detail,scene,script,production,entities=production_context(session,shot)
    if not script or not script.parsed_json.get('versions'):return None
    rules=script.parsed_json.get('rules_snapshot')
    if not rules:return None
    from app.services.cinematic_rules import generation_rules
    rules=generation_rules(rules)
    from app.services.screenplay import shot_bundle
    from app.services.product_library import shot_product_snapshot
    participants=shot_bundle(session,shot.id)['participants']
    product_only=not participants and bool(shot_product_snapshot(session,shot))
    rules_text='\n'.join(rules['visual_language'][key] for key in ('scene','camera','audio')) if product_only else '\n'.join(rules['visual_language'].values())
    if product_only:rules_text+='\nPreserve each referenced product shape, proportions, color, packaging and visible structure. Exact printed text and logo reproduction require visual review or post-production overlays. Do not invent performers or unsupported accessories.'
    names={p['name']:i for i,p in enumerate(participants,1)}
    return {'contact_directions':physical_directions(visual_inputs(session,shot),names),'versions':rules['versions'],'source_versions':script.parsed_json['versions'],'rules_source_sha256':rules['source_sha256'],'rules_text':rules_text,'production':production,'entities':[{'id':e.id,'logical_id':e.logical_id,'kind':e.kind,'version':e.version,'status':e.status,'spec':e.spec_json} for e in entities if e],'preflight':shot_preflight(session,settings,shot)}
