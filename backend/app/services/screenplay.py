import json
from sqlalchemy import select, func
from app.core.errors import AppError
from app.models import (GenerationJob, Scene, ShotDetail, ShotCharacter, DialogueLine, AudioEvent, Character,
                        CharacterVersion, CharacterAsset, StoryboardShot, VideoRevision, AuditEvent)


PERFORMANCE_FIELDS = {'scene_name','action','action_order','posture','route','interaction','expression','emotion','emotion_change','eyeline','position','orientation','depth','voice_profile','speed','pitch','vocal_state','wardrobe','hair','makeup','props','continuity'}
from app.services.cinematic_rules import rules_bundle
PERFORMANCE_FIELDS.update(rules_bundle()['fields']['action'])
PERFORMANCE_FIELDS.update(rules_bundle()['performance_state_fields'])
PERFORMANCE_FIELDS.add('reference_audio_asset_id')


def row(row):
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}


def get(session, model, identifier):
    value = session.get(model, identifier)
    if value is None:
        raise AppError(404, 'record_not_found', '记录不存在或已删除')
    return value


def touch_shot(session, shot_id):
    shot = get(session, StoryboardShot, shot_id)
    active = session.scalar(select(GenerationJob).where(GenerationJob.shot_id == shot_id, GenerationJob.status.in_(['submitting','queued','submitted','running','cancelling','reconciling','unknown'])))
    if active:
        raise AppError(409, 'generation_active', '请先取消运行中的分镜任务，再修改其输入')
    if shot.status == 'deleted':
        raise AppError(409, 'shot_deleted', '已删除分镜不可编辑')
    shot.revision += 1
    shot.status, shot.approved_at = 'draft', None
    detail=session.scalar(select(ShotDetail).where(ShotDetail.shot_id==shot_id))
    if not detail:detail=ShotDetail(shot_id=shot_id);session.add(detail)
    detail.source_json={**(detail.source_json or {}),'structured_edit':True}
    for video in session.scalars(select(VideoRevision).where(VideoRevision.shot_id == shot_id)):
        if video.review_status == 'approved':
            video.review_status = 'needs_review'
    session.add(AuditEvent(object_type='shot',object_id=shot_id,action='structured_content_changed',details_json={'revision':shot.revision}))
    return shot


def sync_legacy(session, shot, force=False):
    """Old one-person API remains functional without erasing new participants."""
    if not shot.character_id:
        return
    p = session.scalar(select(ShotCharacter).where(ShotCharacter.shot_id == shot.id, ShotCharacter.character_id == shot.character_id))
    if not p:
        p = ShotCharacter(shot_id=shot.id, character_id=shot.character_id, order=1,
                          performance_json={'action':shot.action}, source_json={'legacy':True})
        session.add(p)
        force=True
    if force:
        p.character_version_id = shot.character_asset_version_id
        p.reference_asset_ids_json = list(shot.reference_asset_ids_json or [])
    session.flush()


def shot_bundle(session, shot_id):
    shot = get(session, StoryboardShot, shot_id)
    sync_legacy(session, shot)
    detail = session.scalar(select(ShotDetail).where(ShotDetail.shot_id == shot_id))
    participants = list(session.scalars(select(ShotCharacter).where(ShotCharacter.shot_id == shot_id).order_by(ShotCharacter.order,ShotCharacter.id)))
    result = []
    for p in participants:
        c = session.get(Character,p.character_id)
        v = session.get(CharacterVersion,p.character_version_id) if p.character_version_id else None
        assets = list(session.scalars(select(CharacterAsset).where(CharacterAsset.character_version_id == p.character_version_id,CharacterAsset.status == 'approved'))) if v else []
        warnings = []
        if not v or v.approval_state != 'approved': warnings.append('身份版本待审核')
        if not p.reference_asset_ids_json: warnings.append('待补齐已审核参考素材')
        valid = {a.id for a in assets}
        if set(p.reference_asset_ids_json or []) - valid: warnings.append('引用素材缺失或不属于该正式版本')
        if p.performance_json.get('wardrobe') and not any(a.id in p.reference_asset_ids_json and a.asset_type=='outfit' for a in assets):warnings.append('当前服装待补齐已审核造型参考')
        result.append({**row(p), 'name':c.display_name if c else '人物缺失', 'slug':c.slug if c else '',
                       'version_state':v.approval_state if v else 'missing', 'warnings':warnings,
                       'assets':[{**row(a),'media_url':'/api/media/'+a.relative_path} for a in assets]})
    return {'shot':row(shot), 'detail':row(detail) if detail else None,
            'scene':row(session.get(Scene,detail.scene_id)) if detail and detail.scene_id else None,
            'participants':result,
            'dialogues':[row(x) for x in session.scalars(select(DialogueLine).where(DialogueLine.shot_id==shot_id).order_by(DialogueLine.order,DialogueLine.id))],
            'audio_events':[row(x) for x in session.scalars(select(AudioEvent).where(AudioEvent.shot_id==shot_id))]}


def validate_participant(session, p, version_id, asset_ids):
    if version_id:
        v = get(session,CharacterVersion,version_id)
        if v.character_id != p.character_id:
            raise AppError(422,'version_mismatch','身份版本不属于该人物')
    elif asset_ids:
        raise AppError(422,'version_required','先选择身份版本')
    for asset_id in asset_ids:
        a = get(session,CharacterAsset,asset_id)
        if a.character_version_id != version_id or a.status != 'approved':
            raise AppError(422,'asset_mismatch','只能绑定此人物版本已审核通过的素材')


def approved_reference_ids(session,version):
    if not version or version.approval_state!='approved':return []
    if version.standard_face_asset_id:
        asset=session.get(CharacterAsset,version.standard_face_asset_id)
        return [asset.id] if asset and asset.status=='approved' and asset.character_version_id==version.id else []
    # Before the factory introduced standard_face_asset_id, approval used the
    # primary flag. Read that exact unique legacy reference without mutating v1.
    assets=list(session.scalars(select(CharacterAsset).where(CharacterAsset.character_version_id==version.id,CharacterAsset.status=='approved',CharacterAsset.is_primary_reference.is_(True))))
    assets=[a for a in assets if a.source not in ('placeholder','demo') and 'placeholder' not in (a.tags_json or [])]
    return [assets[0].id] if len(assets)==1 else []


def add_participants(session, shot_id, ids):
    get(session,StoryboardShot,shot_id)
    for identifier in set(ids): get(session,Character,identifier)
    existing = list(session.scalars(select(ShotCharacter).where(ShotCharacter.shot_id==shot_id)))
    seen = {p.character_id for p in existing}
    added = False
    for identifier in dict.fromkeys(ids):
        if identifier in seen: continue
        c = session.get(Character,identifier)
        version = c.current_approved_version_id or session.scalar(select(CharacterVersion.id).where(CharacterVersion.character_id==identifier).order_by(CharacterVersion.version.desc()).limit(1))
        v = session.get(CharacterVersion,version) if version else None
        refs = approved_reference_ids(session,v)
        session.add(ShotCharacter(shot_id=shot_id,character_id=identifier,character_version_id=version,order=len(seen)+1,reference_asset_ids_json=refs))
        seen.add(identifier); added = True
    if added: touch_shot(session,shot_id)
    session.flush()
    return shot_bundle(session,shot_id)


def remove_participant(session, p, remove_dialogue=False):
    lines = list(session.scalars(select(DialogueLine).where(DialogueLine.participant_id==p.id)))
    if lines and not remove_dialogue:
        raise AppError(409,'dialogue_dependency','此人物仍有对白，请先改说话人，或明确选择同时移除该人物对白',{'dialogue_count':len(lines)})
    shot = touch_shot(session,p.shot_id)
    for line in lines: session.delete(line)
    session.flush(); session.delete(p); session.flush()
    if shot.character_id == p.character_id:
        other = session.scalar(select(ShotCharacter).where(ShotCharacter.shot_id==shot.id).order_by(ShotCharacter.order))
        shot.character_id = other.character_id if other else None
        shot.character_asset_version_id = other.character_version_id if other else None
        shot.reference_asset_ids_json = list(other.reference_asset_ids_json) if other else []
        shot.dialogue = ''


def context_for_generation(session, shot):
    bundle = shot_bundle(session,shot.id)
    return bundle


def uses_structured_screenplay(session,shot):
    participants=list(session.scalars(select(ShotCharacter).where(ShotCharacter.shot_id==shot.id)))
    detail=session.scalar(select(ShotDetail).where(ShotDetail.shot_id==shot.id))
    from app.services.product_library import shot_product_snapshot
    return bool(shot_product_snapshot(session,shot)) or bool(participants) and (len(participants)>1 or not shot.character_id or bool(detail and detail.source_json.get('structured_edit')) or any(not p.source_json.get('legacy') and not p.source_json.get('migration') for p in participants))


def generation_inputs(session, shot, settings):
    """Compile all participants, ordered speakers, scene and photography to R2V."""
    from app.core.paths import safe_media_path
    bundle=shot_bundle(session,shot.id)
    from app.services.product_library import shot_references,shot_product_snapshot
    product_references=shot_references(session,settings,shot)
    if not bundle['participants'] and not product_references:raise AppError(409,'participants_required','分镜尚未添加人物或有效的产品参考')
    product_only=not bundle['participants']
    from app.services.identity_references import actor_references,reference_manifest,actor_visual_description,english_position,english_action
    from app.services.cinematic_quality import production_context
    _,_,_,production,entities=production_context(session,shot)
    policy=production.get('requested_controls',{}).get('identity_reference_policy','full_reference')
    if policy not in {'full_reference','independent_faces_scene'}:raise AppError(422,'identity_reference_policy_unsupported','未实现此身份参考路由，不会静默更改输入')
    actors,references=actor_references(session,settings,bundle['participants'])
    definitions=[];retention=[];performances=[];subjects={};versions=[];language_coverage=[];excluded=[]
    names={a['display_name']:a['subject_tag'] for a in actors}
    for actor,p in zip(actors,bundle['participants']):
        v=get(session,CharacterVersion,actor['version_id']);versions.append(v)
        subject=actor['subject_tag'];subjects[p['id']]=subject
        labels=[f'<Picture {i}>' for i in actor['picture_indices']]
        description,coverage=actor_visual_description(actor)
        position,position_covered=english_position(actor['screen_position'])
        action,action_covered=english_action(actor['performance'].get('action',''),names)
        state=production.get('wardrobe_states',{}).get(actor['actor_id'],{})
        entity=next((e for e in entities if e and e.id==state.get('entity_id')),None)
        profile=actor['profile'];perf=actor['performance']
        wardrobe=entity.spec_json if entity else perf.get('wardrobe') or next((x.get('description','') for x in profile.get('wardrobes',[]) if x.get('id')=='default'),'') or v.identity_json.get('default_clothing','')
        voice=perf.get('voice_profile') or v.identity_json.get('source_profile',{}).get('voice_profile','')
        definitions.append(f'{subject} is {description}, independently defined by {", ".join(labels)}. This photograph supplies this subject only; it does not define any other performer.')
        retention.append(f'{subject} (appears in [Shot 1]): fully_preserved - retain the assigned facial appearance and hairstyle independently; do not transfer another subject\'s identity.')
        spoken_perf={k:v for k,v in perf.items() if k!='reference_audio_asset_id'}
        performances.append(f'{subject}, {description}, is at {position}. Current action: {action or "no additional movement specified"}. This subject\'s wardrobe only: '+json.dumps(wardrobe,ensure_ascii=False)+'. Independent performance/voice source details: '+json.dumps({**spoken_perf,'voice_profile':voice},ensure_ascii=False))
        language_coverage.append({'actor_id':actor['actor_id'],'identity':coverage,'position_covered':position_covered,'action_covered':action_covered})
    for source in product_references:
        reference={**source,'asset_id':source['id'],'asset_type':'product_image','role':'product','picture_index':len(references)+1}
        references.append(reference)
        definitions.append(f'<Picture {len(references)}> defines only product '+source['product_id']+' ('+source.get('product_name','')+'), purpose '+source.get('purpose','other')+'. Preserve its evidenced shape, proportions, colors, packaging and structural details. These photos are the appearance authority over conflicting generic descriptions. Follow the shot for actions and open/closed state. Do not copy incidental background people or hands, add unsupported accessories, or transfer its appearance to another product. Declared visibility: '+json.dumps(source.get('visible_state',{}),ensure_ascii=False))
        retention.append(f'<Picture {len(references)}> ([Shot 1] product): preserve the supplied product appearance; exact printed text, trademarks and logo reproduction require visual review or a post-production overlay.')
    if product_references:
        bundle['products']=shot_product_snapshot(session,shot)
        bundle['product_appearance_review']={'status':'needs_review','exact_logo_text_verified':False,
            'limitations':['产品参考图已进入实际输入；外观连续性、包装文字与Logo复现仍需人工审核，必要时后期叠加。']}
    from app.services.cinematic_quality import compile_context
    cinematic=compile_context(session,shot,settings)
    if cinematic:
        bundle['cinematic']=cinematic
        from app.models import ProductionVisual
        for identifier in cinematic['preflight']['visual_ids']:
            visual=session.get(ProductionVisual,identifier)
            if policy=='independent_faces_scene' and visual.kind=='keyframe':
                excluded.append({'asset_id':visual.id,'sha256':visual.sha256,'role':'composition','reason':'experimental identity-isolation route excludes synthesized group faces; source layout/positions remain text, not a pixel-first-frame constraint'})
                continue
            role='scene' if visual.kind=='scene_reference' else 'composition'
            references.append({'asset_id':visual.id,'asset_type':visual.kind,'relative_path':visual.relative_path,'absolute_path':str(safe_media_path(settings.media_root,visual.relative_path,must_exist=True)),'sha256':visual.sha256,'picture_index':len(references)+1,'production_visual_id':visual.id,'role':role})
            if role=='scene':
                environment_subject=f'<Subject {len(actors)+1}>'
                definitions.append(f'{environment_subject} is the unoccupied location in <Picture {len(references)}>, supplying only room geometry, lighting and real materials, never performer identity.')
                retention.append(f'{environment_subject} (appears in [Shot 1]): fully_preserved - retain the location separately from each referenced '+('product.' if product_only else 'person.'))
            else:
                definitions.append(f'<Picture {len(references)}> is the composition planning reference for [Shot 1]. '+('Show the supplied products only, without adding performers. ' if product_only else 'Its synthesized faces are not new identities; each performer follows their separate identity Subject reference. ')+'This is guidance, not a guaranteed exact first frame.')
                retention.append(f'<Picture {len(references)}> ([Shot 1] composition): partially_preserved - viewpoint and placement guide the shot while '+('each product retains its own supplied appearance.' if product_only else 'each Subject keeps their independently referenced face.'))
    if len(references)>9:raise AppError(422,'reference_limit','当前H3适配器最多传入9张人物/产品/场景/关键画面参考图，请减少所选数量')
    audio=list(bundle['audio_events'])
    if bundle['scene']:audio=[row(x) for x in session.scalars(select(AudioEvent).where(AudioEvent.scene_id==bundle['scene']['id']))]+audio
    bundle['resolved_audio_events']=audio
    dialogue=[];speakers={}
    for line in bundle['dialogues']:
        subject=subjects.get(line['participant_id'])
        if not subject:raise AppError(422,'speaker_not_participant','对白说话人已失去人物参与关系')
        if line['participant_id'] not in speakers:speakers[line['participant_id']]=len(speakers)+1
        speaker=speakers[line['participant_id']]
        dialogue.append(f"{subject} (S{speaker}) speaks with delivery {json.dumps(line['delivery_json'],ensure_ascii=False)}: <d>[Chinese] {line['text']}</d>")
    environment={**(bundle['scene']['environment_json'] if bundle['scene'] else {'location':shot.location}), 'shot_lighting':shot.lighting}
    photography=bundle['detail']['photography_json'] if bundle['detail'] else {}
    from app.models import ScriptRevision, WordImport
    script=session.get(ScriptRevision,bundle['scene']['script_revision_id']) if bundle['scene'] and bundle['scene']['script_revision_id'] else None
    project_context=script.parsed_json.get('project',{}) if script else {}
    if not project_context and script and script.parsed_json.get('word_import_id'):
        imported=session.get(WordImport,script.parsed_json['word_import_id'])
        project_context=imported.preview_json.get('project',{}) if imported else {}
    bundle['project_context']=project_context
    prompt='\n'.join(['subject_definitions:',*definitions,'summary:',
        '[reference generation] '+('A product presentation shot showing the defined products. Do not add people or hands.' if product_only else 'A live-action dramatic shot with the defined characters. Reference roles apply to their respective subjects only.'),
        'retention_analysis:',*retention,'detailed_description:',
        'The target video uses believable live-action '+('product photography, practical lighting and physically grounded product movement.' if product_only else 'photography with natural skin texture matching each referenced character, practical lighting and physically grounded movement.'),
        'Reviewed screenplay style, synopsis and continuity constraints: '+json.dumps(project_context,ensure_ascii=False),
        '[Shot 1] '+shot.visual_prompt, 'Shot-level action: '+shot.action, 'Environment: '+json.dumps(environment,ensure_ascii=False),
        'Camera: '+shot.camera_move+' '+json.dumps(photography,ensure_ascii=False),
        'Product presentation and sound directions:' if product_only else 'Performer actions, positions, wardrobe and voice:',*performances,*dialogue,
        'overall_soundscape:', '\n'.join(json.dumps({k:x.get(k) for k in ('kind','description','start_seconds','end_seconds','volume')},ensure_ascii=False) for x in audio if x['kind']!='music' and not x.get('relative_path')) or shot.audio_direction or 'Quiet room tone accompanies the visible actions.',
        'non_diegetic_music:', '\n'.join(x['description'] for x in audio if x['kind']=='music' and not x.get('relative_path')) or 'N/A'])
    if cinematic:
        prompt=prompt.replace('[Shot 1] ',cinematic['contact_directions']+'\n[Shot 1] ',1)
        text='Cinematic production rules (version '+cinematic['versions']['production_rules_version']+'):\n'+cinematic['rules_text']+'\nReviewed production designs, timed actions and continuity states: '+json.dumps({k:cinematic[k] for k in ['production','entities']},ensure_ascii=False)
        prompt=prompt.replace('overall_soundscape:',text+'\noverall_soundscape:')
    bundle['reference_manifest']=reference_manifest(actors,references,stage='video',policy=policy,excluded=excluded)
    bundle['reference_manifest']['speakers']=[{'participant_id':identifier,'subject_tag':subjects[identifier],'speaker_tag':f'S{number}'} for identifier,number in speakers.items()]
    from app.services.cinematic_rules import fingerprint
    bundle['reference_manifest']['sha256']=fingerprint({k:v for k,v in bundle['reference_manifest'].items() if k!='sha256'})
    bundle['language_coverage']={'adapter':'finite source-backed visual lexicon; original unhandled details preserved, not full semantic translation','actors':language_coverage}
    return (versions[0] if versions else None),references,prompt,json.loads(json.dumps(bundle,ensure_ascii=False,default=lambda x:x.isoformat()))
