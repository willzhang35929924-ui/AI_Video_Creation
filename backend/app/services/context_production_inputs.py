"""Freeze the approved, project-scoped inputs to a local Context-IR job."""
from __future__ import annotations

import copy
import json
import re

from sqlalchemy import select
from app import models as m
from app.core.errors import AppError
from app.services.cinematic_quality import generation_inputs_hash, production_context, shot_preflight
from app.services.cinematic_rules import fingerprint
from app.services.identity_references import actor_references, reference_manifest, verified_image
from app.services.motion_references import resolve_motion_references
from app.services.product_library import shot_product_snapshot, shot_references
from app.services.screenplay import shot_bundle


def quality_for_shot(session, shot, *, frozen=None, project=None):
    from app.schemas_quality import QualitySettings
    from app.services.video_quality import find_profile, inherited_settings

    if frozen is not None:
        return QualitySettings.model_validate(frozen).model_dump()
    override = find_profile(session, 'shot', shot.id)
    requested = (override.settings_json if override else project) or inherited_settings(session, shot.project_id) or {'mode': 'high'}
    return QualitySettings.model_validate({**requested, 'aspect_ratio': shot.aspect_ratio}).model_dump()


def _without_paths(references):
    return [{key: value for key, value in ref.items() if key != 'absolute_path'} for ref in references]


def _screenplay_prop_references(session, shot, detail, scene, production, entities, products, library_refs):
    """Resolve distinct imported prop photos for a blanket workbench reference."""
    if len(products) != 1 or len(library_refs) != 1:
        return []
    product_state = products[0]['state']
    if (product_state.get('source') != 'project_product_preparation'
            or product_state.get('visible_state') or product_state.get('automation_product_evidence')):
        return []
    if session.scalar(select(m.AuditEvent.id).where(
        m.AuditEvent.object_type == 'storyboard_shot', m.AuditEvent.object_id == shot.id,
        m.AuditEvent.action == 'product_reference_selection_saved',
    ).limit(1)):
        return []  # An explicit per-shot selection outranks the workbench default.
    import_id = (detail.source_json or {}).get('import_id') if detail else None
    if not import_id:
        return []
    job = session.scalar(select(m.ScreenplayGenerationJob).where(
        m.ScreenplayGenerationJob.project_id == shot.project_id,
        m.ScreenplayGenerationJob.word_import_id == import_id,
    ))
    if not job:
        return []
    frozen = {item['id']: item for item in (job.snapshot_json or {}).get('input_assets', [])}
    # The blanket reference itself must come from this import. Do not displace
    # an independent product library image with screenplay authoring assets.
    if not any(item.get('sha256') == library_refs[0]['sha256'] for item in frozen.values()):
        return []
    by_id = {entity.id: entity for entity in entities if entity}
    refs, seen_entities, seen_hashes = [], set(), set()
    for state in production.get('prop_states', []):
        entity = by_id.get(state.get('entity_id'))
        if (not entity or entity.kind != 'prop' or entity.status != 'approved'
                or entity.project_id != shot.project_id or (entity.source_json or {}).get('import_id') != import_id
                or (entity.scene_id and (not scene or scene.id != entity.scene_id))
                or state.get('prop_key') not in (entity.logical_id, entity.id) or entity.id in seen_entities):
            continue
        asset_id = (entity.spec_json or {}).get('source_image_id')
        if not asset_id:
            continue
        item = frozen.get(asset_id)
        asset = session.get(m.ScreenplayInputAsset, asset_id) if item else None
        if (not asset or asset.input_mode != 'image' or asset.sha256 in seen_hashes
                or any(getattr(asset, key) != item.get(key) for key in ('relative_path', 'sha256'))):
            return []  # The claimed image is ambiguous or outside this import.
        seen_entities.add(entity.id)
        seen_hashes.add(asset.sha256)
        refs.append({
            'asset_id': asset.id, 'asset_type': 'product_image', 'source': 'screenplay_prop_reference',
            'role': 'product', 'product_id': entity.id, 'product_name': entity.name,
            'product_logical_id': entity.logical_id, 'screenplay_job_id': job.id,
            'relative_path': asset.relative_path, 'sha256': asset.sha256,
            'image_notes': f'{entity.name}；仅参考商品外观，实际开合与摆放按本镜要求',
        })
    return refs if len(refs) > 1 else []


def collect_context_inputs(session, settings, shot, *, prompt_override=None, instruction=None,
                           image_ids=None, motion_values=None, audio_policy=None):
    """Gather inputs once at the app boundary. The ComfyUI node writes Context-IR."""
    from app.api.routes import script_for_shot
    bundle = shot_bundle(session, shot.id)
    detail, scene, _, production, entities = production_context(session, shot)
    script = script_for_shot(session, shot)
    if (scene and scene.project_id != shot.project_id) or (script and script.project_id != shot.project_id):
        raise AppError(409, 'context_project_scope', '剧本或场景不属于本项目，未使用其他项目素材')
    preflight = shot_preflight(session, settings, shot, require_visual=False, context_inputs=True,
                               explicit_visual_ids=image_ids, explicit_motion=motion_values)
    blockers = [finding for finding in preflight['findings'] if finding['severity'] == 'blocker']
    if blockers:
        raise AppError(409, 'context_input_invalid', '；'.join(finding['message'] for finding in blockers), {'findings': blockers})

    participants = bundle['participants']
    actors, references = actor_references(session, settings, [p for p in participants if p.get('reference_asset_ids_json')])
    actor_names = {p['id']: p['name'] for p in participants}
    from app.services.character_reference import saved_reference_snapshot
    wardrobe_sources = []
    for participant in participants:
        if participant.get('reference_asset_ids_json'):
            continue  # Explicit per-shot identity pictures outrank project bindings.
        saved, _ = saved_reference_snapshot(session,settings,shot.project_id,participant['character_id'])
        for ref in saved:
            wardrobe = ref.get('purpose') == 'wardrobe'
            references.append({**ref, 'role': 'composition' if wardrobe else 'identity',
                               'asset_type': 'project_character_reference'})
            if wardrobe:
                wardrobe_sources.append((len(references),participant['name']))
    products = shot_product_snapshot(session, shot)
    library_refs = shot_references(session, settings, shot, include_image_notes=True)
    prop_refs = _screenplay_prop_references(session, shot, detail, scene, production, entities, products, library_refs)
    references.extend(prop_refs or [
        {**source, 'asset_id': source['id'], 'asset_type': 'product_image', 'role': 'product'} for source in library_refs
    ])
    for identifier in preflight['visual_ids']:
        visual = session.get(m.ProductionVisual, identifier)
        role = 'scene' if visual.kind == 'scene_reference' else 'composition'
        references.append({'asset_id': visual.id, 'asset_type': visual.kind, 'role': role,
                           'relative_path': visual.relative_path, 'sha256': visual.sha256, 'production_visual_id': visual.id})
    if len(references) > 9:
        raise AppError(422, 'context_reference_limit', '人物、商品、场景和临时图片最多合计 9 张')
    for index, ref in enumerate(references, 1):
        ref['picture_index'] = index
        if 'absolute_path' not in ref:
            ref['absolute_path'] = str(verified_image(settings, ref))
    motion = resolve_motion_references(session, settings, shot, motion_values)

    user_prompt = prompt_override if prompt_override is not None else shot.visual_prompt
    user_prompt = str(user_prompt or '').strip()
    if prompt_override is not None and not user_prompt:
        raise AppError(422, 'prompt_override_empty', '自定义提示词不能为空')
    parts = [user_prompt] if user_prompt else []
    if prompt_override is not None:
        parts.append('首段是本次明确的镜头制作要求。与下列原分镜的一般描述发生冲突时，以首段为准，并保留独立绑定素材的角色和使用范围。')
    for label, value in (
        ('镜头', shot.scene_title), ('叙述', shot.narration), ('动作', shot.action), ('机位与运镜', shot.camera_move),
        ('地点', shot.location), ('光线', shot.lighting), ('声音', shot.audio_direction),
        ('本次修订指令', instruction),
    ):
        if value and str(value).strip():
            parts.append(f'{label}：{value}')
    people = []
    for p in participants:
        version = session.get(m.CharacterVersion, p.get('character_version_id'))
        identity = version.identity_json if version and version.approval_state == 'approved' else {}
        facts = {key: identity[key] for key in ('fixed_identity', 'description', 'default_clothing', 'profile') if key in identity}
        performance = {k: v for k, v in (p.get('performance_json') or {}).items() if k != 'reference_audio_asset_id'}
        people.append({'名称': p['name'], '人物描述': facts, '表演': performance})
    product_names = {ref['product_id']: ref['product_name'] for ref in library_refs}
    product_directions = [{'商品': ref['product_name'], '图片': f"<Picture {ref['picture_index']}>"}
                          for ref in prop_refs] or [
        {'商品': product_names.get(product['product_id'], '本镜已绑定商品'),
         '可见状态': product['state'].get('visible_state')} for product in products
    ]
    prop_entities = {entity.id: entity.name for entity in entities if entity and entity.kind == 'prop'}
    prop_states = [
        {'道具': prop_entities[state['entity_id']], **{key: state[key] for key in (
            'holder', 'hand', 'location', 'state', 'visible_state', 'open_state', 'wetness', 'damage',
        ) if key in state}}
        for state in production.get('prop_states', []) if state.get('entity_id') in prop_entities
    ]
    lines = [{'说话人': actor_names.get(line['participant_id']), '台词': line['text'], '表达': line['delivery_json']}
             for line in bundle['dialogues'] if line['text'].strip()]
    if not lines and shot.dialogue:
        lines = [{'台词': shot.dialogue}]
    audio_events = bundle['audio_events']
    if scene:
        from app.services.screenplay import row
        audio_events = [row(event) for event in session.scalars(select(m.AudioEvent).where(m.AudioEvent.scene_id==scene.id))] + audio_events
    spoken_language = 'Chinese' if any(re.search(r'[\u3400-\u9fff]', line['台词']) for line in lines) else 'English'
    for label, value in (
        ('场景与材质', scene.environment_json if scene else None),
        ('摄影', detail.photography_json if detail else None), ('人物', people), ('商品', product_directions),
        ('道具镜头状态', prop_states),
        ('对白', lines if audio_policy != 'skip' else None),
        ('声音事件', [{key: event.get(key) for key in ('kind','description','start_seconds','end_seconds','volume')}
                      for event in audio_events]),
        ('已绑定制作设定', [{'名称': e.name, '类别': e.kind, '内容': e.spec_json} for e in entities if e]),
        ('动作节拍', production.get('beats')),
    ):
        if value:
            parts.append(f'{label}：{json.dumps(value, ensure_ascii=False, default=str)}')
    if not any((user_prompt, shot.action, shot.narration, shot.location, lines, references, motion)):
        raise AppError(422, 'context_prompt_required', '没有参考素材时，请填写本镜的生成要求')
    if motion and not any(ref.get('role') == 'identity' for ref in references):
        parts.append('动作参考仅提供动作、节奏和运镜。以明确绑定的人物文字设定为准，原片出镜者不自动关联为项目人物。')
    for index, name in wardrobe_sources:
        parts.append(f'<Picture {index}> 为{name}明确保存的服装参考，仅使用其中的服装信息。')

    snapshot = {
        'version': 1, 'project_id': shot.project_id, 'shot_id': shot.id, 'shot_revision': shot.revision,
        'script_revision_id': script.id if script else None,
        'duration_seconds': shot.duration_seconds, 'aspect_ratio': shot.aspect_ratio,
        'generation_inputs_hash': generation_inputs_hash(session, shot), 'prompt_override': prompt_override,
        'instruction': instruction, 'prompt': '\n'.join(parts), 'negative_prompt': shot.negative_prompt or '',
        'explicit_visual_ids': image_ids, 'explicit_motion': motion_values, 'audio_policy': audio_policy,
        'silent': audio_policy == 'skip', 'spoken_language': spoken_language,
        'references': _without_paths(references), 'motion_references': _without_paths(motion),
        'screenplay_snapshot': {**bundle, 'resolved_audio_events': audio_events,
                                'reference_manifest': reference_manifest(actors, references, stage='video'),
                                'cinematic': {'versions': preflight['versions'], 'preflight': preflight}},
    }
    # The bundle contains ORM date values. Only the production data required by
    # downstream media inspection and review belongs in this durable snapshot.
    from fastapi.encoders import jsonable_encoder
    snapshot = jsonable_encoder(snapshot)
    snapshot['fingerprint'] = fingerprint(snapshot)
    return snapshot


def restore_context_inputs(session, settings, shot, snapshot):
    """Reject changed media/shot state without reinterpreting a frozen prompt."""
    data = copy.deepcopy(snapshot)
    digest = data.pop('fingerprint', None)
    if (data.get('version') != 1 or data.get('project_id') != shot.project_id or data.get('shot_id') != shot.id
            or not digest or fingerprint(data) != digest):
        raise AppError(409, 'context_input_snapshot_invalid', 'Context-IR 输入快照不属于本镜或已变化')
    if data['shot_revision'] != shot.revision or data['generation_inputs_hash'] != generation_inputs_hash(session, shot):
        raise AppError(409, 'context_input_snapshot_stale', '镜头已变化，原任务仍保留。请明确新建制作以使用修改后的输入')
    from app.api.routes import script_for_shot
    script = script_for_shot(session, shot)
    if data.get('script_revision_id') != (script.id if script else None):
        raise AppError(409, 'context_input_snapshot_stale', '镜头所属剧本已变化，未使用原任务以外的剧本')
    preflight = shot_preflight(session, settings, shot, require_visual=False, context_inputs=True,
                               explicit_visual_ids=data['explicit_visual_ids'], explicit_motion=data['explicit_motion'])
    blockers = [finding for finding in preflight['findings'] if finding['severity'] == 'blocker']
    if blockers:
        raise AppError(409, 'context_input_snapshot_stale', '原参考的权限、状态或来源已变化，未派发', {'findings': blockers})
    frozen_visuals = [ref['asset_id'] for ref in data['references'] if ref.get('production_visual_id')]
    if frozen_visuals != preflight['visual_ids']:
        raise AppError(409, 'context_input_snapshot_stale', '镜头的视觉参考选择已变化，未替换原任务素材')
    frozen_props = [{key: value for key, value in ref.items() if key != 'picture_index'} for ref in data['references']
                    if ref.get('source') == 'screenplay_prop_reference']
    if frozen_props:
        detail, scene, _, production, entities = production_context(session, shot)
        current_props = _screenplay_prop_references(
            session, shot, detail, scene, production, entities,
            shot_product_snapshot(session, shot), shot_references(session, settings, shot),
        )
        if current_props != frozen_props:
            raise AppError(409, 'context_input_snapshot_stale', '剧本道具图片的来源或批准状态已变化，未派发原任务')
    for ref in data['references']:
        if ref.get('source') != 'project_character_reference':
            continue
        from app.models.character_reference import CharacterReferenceAsset
        current = session.get(CharacterReferenceAsset,ref['asset_id'])
        if (not current or current.state != 'saved' or current.project_id != shot.project_id
                or any(getattr(current,key) != ref[key] for key in ('relative_path','sha256','purpose','character_id'))):
            raise AppError(409,'context_input_snapshot_stale','已保存的人物参考已移除或变更，未派发原任务')
    references = [{**ref, 'absolute_path': str(verified_image(settings, ref))} for ref in data['references']]
    motion = resolve_motion_references(session, settings, shot, data['explicit_motion'])
    if _without_paths(motion) != data['motion_references']:
        raise AppError(409, 'context_input_snapshot_stale', '动作参考已变化，未派发原任务')
    return references, motion
