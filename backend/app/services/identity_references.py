"""Source-backed actor/reference contracts; not biometric identity verification."""
import copy
import hashlib
import io
from PIL import Image
from app import models as m
from app.core.errors import AppError
from app.core.paths import safe_media_path
from app.services.cinematic_rules import fingerprint


def verified_image(settings, reference):
    path=safe_media_path(settings.media_root,reference['relative_path'],must_exist=True)
    data=path.read_bytes()
    if hashlib.sha256(data).hexdigest()!=reference['sha256']:
        raise AppError(409,'identity_reference_changed','身份或场景参考文件的实际SHA已变化，拒绝混用旧快照')
    try:
        with Image.open(io.BytesIO(data)) as image:image.verify()
    except Exception as exc:raise AppError(422,'identity_reference_corrupt','所选身份参考图片无法实际解码') from exc
    return path


def actor_references(session,settings,participants,first_only=False):
    """Validate every explicit selection before choosing a stage's reference set."""
    actors=[];refs=[];owners={};seen_characters=set()
    for index,p in enumerate(participants,1):
        character=session.get(m.Character,p['character_id'])
        version=session.get(m.CharacterVersion,p.get('character_version_id')) if p.get('character_version_id') else None
        if not character or not version or version.character_id!=character.id:
            raise AppError(409,'identity_version_owner_mismatch','人物参与关系的身份版本不属于该角色；未交给生成模型猜测修正')
        if character.id in seen_characters:raise AppError(409,'identity_participant_duplicate','同一角色在本镜重复参与，请先修复稳定ID关联')
        seen_characters.add(character.id)
        if version.approval_state!='approved':raise AppError(409,'participant_identity_unapproved',character.display_name+'的明确身份版本尚未可用于制作')
        ids=p.get('reference_asset_ids_json') or []
        if not ids:raise AppError(409,'participant_reference_missing',character.display_name+'缺少明确选定的身份参考')
        selected=[];own_hashes=set()
        for identifier in ids:
            asset=session.get(m.CharacterAsset,identifier)
            if not asset or asset.character_version_id!=version.id or asset.status!='approved' or not asset.mime_type.startswith('image/'):
                raise AppError(409,'participant_reference_invalid',character.display_name+'的参考素材不属于该明确批准身份版本')
            if asset.source in {'demo','placeholder'}:raise AppError(409,'identity_reference_placeholder','占位图不能作为真实身份参考')
            if asset.sha256 in own_hashes:raise AppError(409,'identity_reference_duplicate','同一人物重复选择了相同图片内容，请明确保留一份参考')
            if asset.sha256 in owners and owners[asset.sha256]!=character.id:
                raise AppError(409,'identity_reference_shared_across_actors','不同角色选用了相同内容的身份图；请修复角色素材关联后重试')
            own_hashes.add(asset.sha256);owners[asset.sha256]=character.id
            ref={'asset_id':asset.id,'asset_type':asset.asset_type,'relative_path':asset.relative_path,'sha256':asset.sha256,
                 'is_primary_reference':asset.is_primary_reference,'participant_id':p['id'],'character_id':character.id,
                 'character_version_id':version.id,'role':'identity','subject_index':index,'subject_tag':f'<Subject {index}>'}
            ref['absolute_path']=str(verified_image(settings,ref));selected.append(ref)
        # Image editing has a separate explicit budget. Preserve the user's
        # selected set in the receipt, and prefer its actual standard face.
        chosen=selected
        if first_only:
            preferred=next((r for r in selected if r['asset_id']==version.standard_face_asset_id),None)
            preferred=preferred or next((r for r in selected if r['is_primary_reference']),None) or selected[0]
            chosen=[preferred]
        pictures=[]
        for ref in chosen:
            ref['picture_index']=len(refs)+1;pictures.append(ref['picture_index']);refs.append(ref)
        actors.append({'actor_id':character.id,'participant_id':p['id'],'version_id':version.id,
                       'subject_index':index,'subject_tag':f'<Subject {index}>','picture_indices':pictures,
                       'screen_position':p.get('performance_json',{}).get('position',''),
                       'display_name':character.display_name,'profile':copy.deepcopy(version.identity_json.get('profile') or {}),
                       'performance':copy.deepcopy(p.get('performance_json') or {}),
                       'selected_asset_ids':list(ids),'stage_asset_ids':[r['asset_id'] for r in chosen],
                       'omitted_by_image_stage_budget':[r['asset_id'] for r in selected if r not in chosen]})
    return actors,refs


def reference_manifest(actors,references,*,stage,policy='full_reference',excluded=None):
    ordered=[]
    for index,ref in enumerate(references,1):
        ordered.append({k:copy.deepcopy(v) for k,v in {**ref,'picture_index':index}.items() if k!='absolute_path'})
    data={'version':'actor-reference-manifest-1','stage':stage,'policy':policy,'actors':copy.deepcopy(actors),
          'references':ordered,'excluded_references':excluded or [],
          'identity_verification':'association, ownership, file SHA and decoding only; no biometric or regional face-lock guarantee'}
    data['sha256']=fingerprint(data)
    return data


def english_position(value):
    known={'左':'screen left','左侧':'screen left','画面左侧':'screen left','左边':'screen left',
           '右':'screen right','右侧':'screen right','画面右侧':'screen right','右边':'screen right',
           '中':'screen center','中央':'screen center','中间':'screen center','居中':'screen center','前景':'foreground','后景':'background'}
    value=str(value or '').strip()
    if value in known:return known[value],True
    if value and not any('\u3400'<=c<='\u9fff' for c in value):return value,True
    return 'position retained in source; not translated by the verified lexical adapter',False


def english_action(value,names):
    value=str(value or '').strip()
    known={'静止观察':'stands still and observes','安静观察':'quietly observes','保持站稳':'stands steadily',
           '轻微点头':'gives one small nod','轻微点头后保持站稳':'gives one small nod, then stands steadily',
           '静止站立':'stands still','安静听':'listens quietly','安静倾听':'listens quietly'}
    if value in known:return known[value],True
    for prefix,translated in [('安静看向','quietly looks toward '),('看向','looks toward '),('看','looks toward ')]:
        if value.startswith(prefix) and value[len(prefix):] in names:return translated+names[value[len(prefix):]],True
    if value and not any('\u3400'<=c<='\u9fff' for c in value):return value,True
    return value,not value  # preserve unhandled source text, report limited coverage


def actor_visual_description(actor):
    """Finite factual mapping, explicitly not a complete profile translation."""
    from app.schemas_product import IdentityProfile
    from app.services.character_factory import candidate_language_adapter
    profile=actor['profile']
    try:
        parsed=IdentityProfile.model_validate(profile)
        _,receipt,warnings=candidate_language_adapter(parsed,'')
    except (ValueError,AppError):
        return 'the referenced person with facial identity defined by the assigned photograph',{'complete':False,'warnings':['Identity profile is preserved in snapshot; lexical English adaptation unavailable.']}
    person={'male':'male person','female':'female person'}.get(receipt['gender'],'person')
    facts=list(dict.fromkeys(v['english'] for v in receipt['mapped_visual_facts'] if v['field'] not in {'style','wardrobe'}))
    description=(f'the {parsed.age}-year-old {person}' if parsed.age is not None else f'the referenced {person}')+(' with '+', '.join(facts) if facts else '')
    return description,{'complete':not receipt['unmapped_chinese'],'warnings':warnings,'source_receipt':receipt}
