"""Durable automation over the real factory/image/video services.

Internal acceptance means only that a file exists, decodes and retains its input
lineage. It is recorded as automation, never as a user's identity/visual review.
Video decisions and assembly remain separate, explicit user actions.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import re
import time
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.orm.attributes import flag_modified
from starlette.requests import Request
from app import models as m
from app.models.studio import StudioRun, StudioDecision
from app.models.entities import utcnow
from app.core.errors import AppError
from app.core.paths import safe_media_path
from app.core.secrets import redact
from app.schemas import GenerationRequest
from app.schemas_product import BatchRequest, IdentityProfile
from app.schemas_cinematic import VisualInput
from app.schemas_studio import PipelineStart
from app.services.ai_services import resolve_service
from app.services.cinematic_rules import fingerprint
from app.services.cinematic_quality import production_context, generation_inputs_hash, visual_inputs, shot_preflight
from app.services.screenplay import get, row, approved_reference_ids, shot_bundle
from app.services.screenplay_production_status import sync_production_status

TERMINAL = {'succeeded', 'failed', 'exception', 'unknown', 'cancelled', 'skipped'}
RUN_TERMINAL = {'waiting_review', 'completed_with_errors', 'completed', 'cancelled'}
ACTIVE_CHILD = {'queued', 'submitting', 'submitted', 'running', 'cancelling'}
ACCEPTANCE = {'actor': 'automation', 'scope': 'technical acceptance; human visual review pending',
              'identity_verified': False, 'performance_verified': False, 'dialogue_verified': False}
MEDIA_KINDS = {'tts', 'audio_mix', 'lip_sync'}
VIDEO_KINDS = {'video', 'audio_mix', 'lip_sync', 'quality', 'quality_check'}


def node(kind, target, dependencies=()):
    return {'id': kind+':'+target, 'kind': kind, 'target_id': target,
            'dependencies': list(dependencies), 'status': 'pending', 'attempts': 0,
            'child_id': None, 'history': [], 'error': None, 'code': None}


def latest_run(session, project_id):
    return session.scalar(select(StudioRun).where(StudioRun.project_id == project_id)
                          .order_by(StudioRun.created_at.desc(), StudioRun.id.desc()).limit(1))


def _shots(session, project_id, shot_ids=None):
    from app.api.routes import project_shot_statement
    query = project_shot_statement(session, project_id).order_by(m.StoryboardShot.order, m.StoryboardShot.id)
    shots = list(session.scalars(query))
    if shot_ids is not None:
        scope = set(shot_ids)
        if not scope or scope-set(s.id for s in shots):
            raise AppError(422, 'studio_shot_scope', '镜头必须属于项目的当前剧本版本，且至少选择一个镜头')
        shots = [s for s in shots if s.id in scope]
    return shots


def _capture_services(session, project_id):
    snapshots, errors = {}, []
    for kind, module in [('image', 'character_generation'), ('video', 'video_generation')]:
        try:
            _, snapshots[kind] = resolve_service(session, module, project_id=project_id)
        except AppError as exc:
            errors.append({'kind': kind, 'code': exc.code, 'message': exc.message})
    return snapshots, errors


def _route(run, slot, shot_id=None):
    return (run.snapshot_json.get('routes_by_shot', {}).get(shot_id, {}).get(slot)
            or run.snapshot_json.get('routes', {}).get(slot))


def _explicit_route(route):
    from app.services.task_routing import route_is_explicit
    return route_is_explicit(route)


def _route_parameters(run, slot, shot_id=None):
    route = _route(run, slot, shot_id)
    return copy.deepcopy(route.get('profile', {}).get('settings', {}).get('parameters', {})) if _explicit_route(route) else {}


def _matching_route_output(run,slot,shot_id,snapshot,parameters=None,requested_parameters=None):
    route=_route(run,slot,shot_id)
    if not _explicit_route(route):return True
    selected=(route.get('selected') or {}).get('service_snapshot')
    if not selected:return False
    actual=snapshot.get('service_snapshot',{})
    if any(actual.get(k)!=selected.get(k) for k in ('service_id','service_revision','model_id')):return False
    requested=_route_parameters(run,slot,shot_id) if requested_parameters is None else requested_parameters
    return all((parameters or snapshot.get('parameters',{})).get(k)==v for k,v in requested.items())


def _leaf(nodes, shot_id):
    # The last derived video owns review. Never silently expose raw H3 while a
    # requested voice/lip/quality child is still pending or blocked.
    return next((n for kind in ('quality_check', 'quality', 'lip_sync', 'audio_mix', 'video')
                 for n in nodes if n['kind'] == kind and n['target_id'] == shot_id), None)


def _predecessor(nodes, entry):
    return next((n for identifier in reversed(entry['dependencies']) for n in nodes
                 if n['id'] == identifier and n.get('video_id')), None)


def _append_media_nodes(session, shot, nodes, routes, quality, audio_policy='auto'):
    route = routes['dialogue_generation']
    lines = [line for line in session.scalars(select(m.DialogueLine).where(m.DialogueLine.shot_id == shot.id)
                                .order_by(m.DialogueLine.order, m.DialogueLine.id)) if line.text.strip()]
    previous = 'video:' + shot.id
    if lines and audio_policy not in {'native','skip'} and (_explicit_route(route) or route.get('selected')):
        tts_ids = []
        for line in lines:
            entry = node('tts', line.id, ['keyframe:' + shot.id])
            entry.update(shot_id=shot.id, participant_id=line.participant_id, route=copy.deepcopy(route))
            nodes.append(entry); tts_ids.append(entry['id'])
        entry = node('audio_mix', shot.id, [previous, *tts_ids])
        entry.update(audio_policy='replace_dialogue', disclosure='Replace original H3 audio with explicitly generated dialogue; raw video and its original audio remain available.')
        nodes.append(entry); previous = entry['id']
    else:
        entry = node('tts_optional', shot.id)
        if not lines:entry.update(status='skipped',optional=True,code='no_dialogue',error='本镜没有对白，不需要独立配音')
        elif audio_policy in {'native','skip'}:
            entry.update(status='skipped',optional=True,code='native_audio' if audio_policy=='native' else 'user_skipped',error='用户明确沿用视频原声' if audio_policy=='native' else '用户明确跳过独立配音')
        else:entry.update(status='exception',optional=False,required=True,code='tts_not_configured',error='本镜有对白，但未配置可用配音服务；请选择角色声音和服务，或明确选择视频原声/跳过独立配音')
        entry['route']=copy.deepcopy(route)
        nodes.append(entry)
    controls = production_context(session, shot)[3].get('requested_controls', {})
    if controls.get('require_lip_sync') is True:
        entry = node('lip_sync', shot.id, [previous])
        entry.update(route=copy.deepcopy(routes['lip_sync']), required=True)
        nodes.append(entry); previous = entry['id']
    if quality:
        entry = node('quality', shot.id, [previous])
        entry.update(route=copy.deepcopy(routes['video_upscale']))
        nodes.append(entry); previous = entry['id']
    entry = node('quality_check', shot.id, [previous])
    entry.update(route=copy.deepcopy(routes['quality_check']), check_scope='technical decode and recorded quality checks only; no likeness, ASR or visual-quality approval')
    nodes.append(entry)


def create_pipeline(session, settings, project_id, options):
    """Persist the DAG in the caller's transaction; this function never calls AI."""
    get(session, m.Project, project_id)
    raw = options.model_dump() if hasattr(options, 'model_dump') else dict(options or {})
    validated = PipelineStart.model_validate({k: raw[k] for k in PipelineStart.model_fields if k in raw})
    raw = {**raw, **validated.model_dump()}
    shots = _shots(session, project_id, raw.get('shot_ids'))
    if not shots: raise AppError(409, 'studio_no_shots', '先导入或创建至少一个有效分镜')
    selected = [s.id for s in shots]
    from app.services.video_quality import inherited_settings, plan as quality_plan
    from app.services.super_resolution import status as sr_status
    from app.schemas_quality import QualitySettings
    quality_by_shot={}
    project_quality=raw.get('quality') or inherited_settings(session,project_id)
    for shot in shots:
        from app.services.video_quality import find_profile
        override=find_profile(session,'shot',shot.id)
        requested=override.settings_json if override else project_quality
        if requested:
            config=QualitySettings.model_validate({**requested,'aspect_ratio':shot.aspect_ratio}).model_dump()
            from app.services.video_quality import seedvr_capabilities
            proposal = quality_plan(config, None, seedvr_capabilities(session,settings)) if config['processing']=='seedvr2' else quality_plan(config,sr_status(settings))
            if not proposal['supported']:raise AppError(409,'quality_method_unverified','所选画质处理没有已验证工作流，未派发模型')
            quality_by_shot[shot.id]=config
    raw['quality_by_shot']=quality_by_shot
    old = latest_run(session, project_id)
    if old and old.status not in RUN_TERMINAL | {'paused', 'exceptions'}:
        if old.snapshot_json.get('shot_ids') == selected and not raw.get('force_shot_ids'):
            if raw.get('product_reference_plan') and raw.get('product_reference_plan') != old.snapshot_json.get('options',{}).get('product_reference_plan'):
                raise AppError(409,'studio_active_product_plan_conflict','运行中的制作使用不同产品配置，请先暂停')
            return old  # double-click/repeated idempotent import cannot duplicate paid jobs
        raise AppError(409, 'studio_already_active', '项目已有运行中的制作，请先暂停它', {'run_id': old.id})
    product_plan_result = None
    if raw.get('product_reference_plan'):
        from app.services.product_library import apply_studio_product_plan
        product_plan_result=apply_studio_product_plan(session,settings,project_id,shots,raw['product_reference_plan'])
    scripts = {production_context(session, s)[2].id for s in shots if production_context(session, s)[2]}
    revision = raw.get('input_revision_id') or (next(iter(scripts)) if len(scripts) == 1 else None)
    if revision:
        script = get(session, m.ScriptRevision, revision)
        if script.project_id != project_id: raise AppError(422, 'studio_revision_scope', '剧本版本不属于本项目')
    else:
        script = session.scalar(select(m.ScriptRevision).where(m.ScriptRevision.project_id == project_id).order_by(m.ScriptRevision.revision.desc()).limit(1))
    from app.services.task_routing import SLOTS, freeze_route
    def freeze(shot_id=None):
        return {slot: freeze_route(session, settings, project_id, shot_id, slot,
                    raw['allow_cloud_costs'], raw['allow_paid_fallback']) for slot in SLOTS}
    from app.services.studio_audio_policy import get_audio_policy
    raw['dialogue_audio_policy']=raw.get('dialogue_audio_policy') or get_audio_policy(session,project_id)
    routes = freeze()
    routes_by_shot = {shot.id: freeze(shot.id) for shot in shots}
    nodes, known, scene_ids = [], set(), {}
    for shot in shots:
        for participant in session.scalars(select(m.ShotCharacter).where(m.ShotCharacter.shot_id == shot.id)):
            if participant.character_id not in known:
                entry = node('character', participant.character_id)
                entry.update(route_shot_id=shot.id, route=copy.deepcopy(routes_by_shot[shot.id]['character_image']))
                nodes.append(entry); known.add(participant.character_id)
        _, scene, _, prod, _ = production_context(session, shot)
        if scene:
            key = scene.id+':'+str(prod.get('scene_state_id') or 'default')
            scene_ids[shot.id] = key
            if not any(n['id'] == 'scene:'+key for n in nodes):
                entry = node('scene', key); entry.update(scene_id=scene.id, entity_id=prod.get('scene_state_id'), route_shot_id=shot.id,
                    route=copy.deepcopy(routes_by_shot[shot.id]['keyframe_image'])); nodes.append(entry)
    for shot in shots:
        dependencies = ['character:'+p.character_id for p in session.scalars(select(m.ShotCharacter).where(m.ShotCharacter.shot_id == shot.id))]
        if shot.id in scene_ids: dependencies.append('scene:'+scene_ids[shot.id])
        entry = node('keyframe', shot.id, dependencies); entry.update(route=copy.deepcopy(routes_by_shot[shot.id]['keyframe_image'])); nodes.append(entry)
        entry = node('video', shot.id, ['keyframe:'+shot.id]); entry.update(route=copy.deepcopy(routes_by_shot[shot.id]['video_generation'])); nodes.append(entry)
        _append_media_nodes(session, shot, nodes, routes_by_shot[shot.id], shot.id in quality_by_shot,raw['dialogue_audio_policy'])
    snapshots, errors = _capture_services(session, project_id)
    run = StudioRun(project_id=project_id, script_revision_id=script.id if script else None,
                    status='queued' if raw.get('auto_start', True) else 'paused', nodes_json=nodes,
                    snapshot_json={'options': raw, 'shot_ids': selected, 'services': snapshots, 'product_plan_result': product_plan_result,
                                   'service_errors': errors, 'routes': routes, 'routes_by_shot': routes_by_shot,
                                   'created_by': 'word automation' if raw.get('source') else 'studio start',
                                   'review_policy': ACCEPTANCE, 'resource_policy': {'local_gpu': 1, 'cloud': 1, 'note': 'conservative one actual task at a time; no throughput claim'}},
                    result_json={'quality_limits': ['Automated checks do not verify likeness, lip sync, speaker identity, acting or cinematic quality.']})
    session.add(run); session.flush()
    sync_production_status(session, run)
    session.add(m.AuditEvent(object_type='studio_run', object_id=run.id, action='scope_created',
                            details_json={'shot_ids': selected, 'allow_cloud_costs': raw['allow_cloud_costs'], 'policy': ACCEPTANCE}))
    return run


def _persist(session, run, nodes):
    run.nodes_json = copy.deepcopy(nodes)
    # JSON has no MutableList instrumentation. A dispatch updates nested dicts
    # between commits, so explicitly mark it dirty before the external POST.
    flag_modified(run, 'nodes_json')
    sync_production_status(session, run)
    session.commit()


def _accept(session, kind, identifier, revision=1):
    session.add(m.Approval(object_type=kind, object_id=identifier, revision=revision, decision='approved',
                           reason='自动技术采纳，供本次制作使用；人物相似度、表演及对白仍等待最终镜头人工审片', operator='automation'))
    session.add(m.AuditEvent(object_type=kind, object_id=identifier, action='automation_technical_acceptance', details_json=ACCEPTANCE))


def _verify_image(settings, asset):
    path = safe_media_path(settings.media_root, asset.relative_path, must_exist=True)
    if hashlib.sha256(path.read_bytes()).hexdigest() != asset.sha256:
        raise AppError(409, 'studio_image_changed', '参考素材文件哈希变化，请恢复原文件或创建新版本')
    try:
        with Image.open(path) as image: image.verify()
    except Exception as exc:
        raise AppError(422, 'studio_image_corrupt', '参考图片实际解码失败') from exc


def _valid_video(session, settings, video):
    path = safe_media_path(settings.media_root, video.relative_path, must_exist=True)
    digest = video.metadata_json.get('sha256')
    if digest and hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise AppError(409, 'studio_video_changed', '视频文件哈希已变化')
    history = list(session.scalars(select(m.QualityCheck).where(m.QualityCheck.video_revision_id == video.id).order_by(m.QualityCheck.created_at, m.QualityCheck.id)))
    # Rechecks append evidence; only the latest result per kind controls use.
    checks = list({c.check_type: c for c in history}.values())
    if not any(c.check_type == 'media_decode' and c.status == 'passed' for c in checks) or any(c.status == 'failed' for c in checks):
        raise AppError(422, 'studio_video_quality_failed', '真实视频缺少通过的解码检查，或已有技术检查失败')


def _service(session, run, kind, slot=None, shot_id=None):
    route = _route(run, slot, shot_id) if slot else None
    if _explicit_route(route):
        from app.services.task_routing import assert_frozen
        return assert_frozen(session, route)
    snapshot = run.snapshot_json.get('services', {}).get(kind)
    if not snapshot:
        raise AppError(409, 'service_not_configured', ('图片' if kind == 'image' else '视频')+'服务未配置，请在系统设置中配置后重试异常')
    current = session.get(m.AIService, snapshot['service_id'])
    if not current or not current.enabled or current.revision != snapshot['service_revision']:
        raise AppError(409, 'studio_service_changed', '服务配置已变化，请核对后明确重试异常以使用新设置')
    if snapshot['is_cloud'] and not run.snapshot_json['options']['allow_cloud_costs']:
        raise AppError(409, 'studio_cloud_cost_confirmation', '本轮未授权云端费用；请确认本轮范围后开启云端制作')
    if kind == 'video' and snapshot['provider'] == 'video_api' and snapshot['settings'].get('video_contract') not in {'video-jobs-v1','wan26-i2v','lk888-h3-v2'}:
        raise AppError(409, 'video_contract_required', '请选择已实现的视频任务协议；聊天接口不能生成视频')
    return current, snapshot


def _shared_route_scope(session, run, entry, slot):
    if entry['kind'] == 'character':
        shot_ids = list(session.scalars(select(m.ShotCharacter.shot_id).where(
            m.ShotCharacter.character_id == entry['target_id'], m.ShotCharacter.shot_id.in_(run.snapshot_json['shot_ids']))))
    else:
        shot_ids = list(session.scalars(select(m.ShotDetail.shot_id).where(
            m.ShotDetail.scene_id == entry.get('scene_id'), m.ShotDetail.shot_id.in_(run.snapshot_json['shot_ids']))))
    signatures = set()
    for shot_id in shot_ids:
        route = _route(run, slot, shot_id)
        if _explicit_route(route):
            signatures.add(fingerprint({'settings':route.get('profile', {}).get('settings'),
                                        'selected':(route.get('selected') or {}).get('service_snapshot', {}).get('service_id'),
                                        'model':(route.get('selected') or {}).get('service_snapshot', {}).get('model_id')}))
        else:
            signatures.add('legacy')
    if len(signatures) > 1:
        raise AppError(409, 'studio_shared_route_conflict', '共享人物或场景的镜头配置了不同图片槽路由；请统一共享素材路由或分别制作，不会任选其一')
    return entry.get('route_shot_id') or next(iter(shot_ids), None)


def _failure(entry, code, message, *, unknown=False, transient=False, max_retries=2):
    entry.update(error=redact(str(message))[:2400], code=code)
    if unknown:
        entry['status'] = 'unknown'
    elif transient and entry['attempts'] <= max_retries:
        entry.update(status='retrying', retry_after=time.time()+min(60, 3*2**entry['attempts']))
        entry['history'] = [*entry['history'], {'child_id': entry.get('child_id'), 'code': code, 'error': entry['error'], 'seed': entry.get('seed'), 'attempt': entry['attempts']}]
        entry['child_id'] = None
    else:
        entry['status'] = 'failed' if transient else 'exception'


def _temporary_failure(message):
    """Only a definite reported failure can reach here; unknown POSTs never do."""
    return bool(re.search(r'\b(?:429|502|503|504)\b|temporar|unavailable|out of memory|显存|暂时|稍后', str(message), re.I))


def _assert_shot_live(session, shot):
    # Read the current persisted status as well as the identity-map value. A
    # user can delete a shot through the advanced UI while a provider is busy.
    persisted = session.scalar(select(m.StoryboardShot.status).where(m.StoryboardShot.id == shot.id))
    if shot.status == 'deleted' or persisted in (None, 'deleted'):
        raise AppError(409, 'studio_shot_deleted', '镜头已删除；原始任务与文件保留，不再自动生成、采纳或恢复此镜头')


def _fixed_binding(session, settings, participant, fill_anchor=False):
    version=session.get(m.CharacterVersion,participant.character_version_id) if participant.character_version_id else None
    if version and version.character_id!=participant.character_id:
        raise AppError(409,'studio_identity_binding_mismatch','参与人物绑定了另一角色的身份版本，请修正明确绑定后重试')
    if not version or version.approval_state!='approved':return None
    refs=list(participant.reference_asset_ids_json or [])
    if not refs and fill_anchor:refs=approved_reference_ids(session,version)
    if not refs:return None
    for identifier in refs:
        asset=session.get(m.CharacterAsset,identifier)
        if not asset or asset.character_version_id!=version.id or asset.status!='approved' or not asset.mime_type.startswith('image/'):return None
        try:_verify_image(settings,asset)
        except (AppError,OSError,TypeError):return None
    return version,refs


def _bind_character(session, settings, run, character_id, version, refs, participant_ids=None):
    """Fill explicit gaps only. Existing valid shot identities are immutable here."""
    for p in session.scalars(select(m.ShotCharacter).where(m.ShotCharacter.character_id == character_id,
                                                          m.ShotCharacter.shot_id.in_(run.snapshot_json['shot_ids']))):
        if participant_ids is not None and p.id not in participant_ids:continue
        if _fixed_binding(session,settings,p):continue
        existing=session.get(m.CharacterVersion,p.character_version_id) if p.character_version_id else None
        if existing and existing.approval_state=='approved' and existing.id!=version.id:
            raise AppError(409,'studio_explicit_reference_missing','明确批准的身份版本缺少有效参考，请修复该版本素材；不会改用库中另一个身份')
        p.character_version_id, p.reference_asset_ids_json = version.id, refs
        p.source_json = {**p.source_json, 'studio_binding': {'run_id': run.id, **ACCEPTANCE}}
        shot = session.get(m.StoryboardShot, p.shot_id)
        if shot.character_id in (None, character_id):
            shot.character_id, shot.character_asset_version_id, shot.reference_asset_ids_json = character_id, version.id, refs


def _character(session, settings, run, entry, dispatch):
    character = get(session, m.Character, entry['target_id'])
    participants=list(session.scalars(select(m.ShotCharacter).where(m.ShotCharacter.character_id==character.id,
                    m.ShotCharacter.shot_id.in_(run.snapshot_json['shot_ids'])).order_by(m.ShotCharacter.order,m.ShotCharacter.id)))
    # A published library pointer is only a default for a genuinely unbound
    # participant. It cannot replace an explicit approved or draft shot version.
    for p in participants:
        fixed=_fixed_binding(session,settings,p,True)
        if fixed and not p.reference_asset_ids_json:
            _bind_character(session,settings,run,character.id,*fixed,participant_ids=[p.id])
    missing=[p for p in participants if not _fixed_binding(session,settings,p)]
    if missing:
        formal=session.get(m.CharacterVersion,character.current_approved_version_id) if character.current_approved_version_id else None
        refs=approved_reference_ids(session,formal)
        if formal and formal.character_id==character.id and refs:
            try:
                for identifier in refs:_verify_image(settings,get(session,m.CharacterAsset,identifier))
            except (AppError,OSError,TypeError):refs=[]
            unbound=[p.id for p in missing if not p.character_version_id]
            if refs and unbound:_bind_character(session,settings,run,character.id,formal,refs,participant_ids=unbound)
    missing=[p for p in participants if not _fixed_binding(session,settings,p)]
    if not missing and not entry.get('child_id'):
        bindings=[{'participant_id':p.id,'shot_id':p.shot_id,'version_id':p.character_version_id,'asset_ids':list(p.reference_asset_ids_json)} for p in participants]
        versions=list(dict.fromkeys(p.character_version_id for p in participants))
        entry.update(status='succeeded',version_id=versions[0] if len(versions)==1 else None,
                     asset_ids=list(dict.fromkeys(a for p in participants for a in p.reference_asset_ids_json)),bindings=bindings,reused=True)
        return
    if entry.get('child_id'):
        batch = get(session, m.CharacterBatch, entry['child_id'])
        jobs = list(session.scalars(select(m.CharacterGenerationJob).where(m.CharacterGenerationJob.batch_id == batch.id)))
        if any(j.status in ACTIVE_CHILD for j in jobs): entry['status'] = 'running'; return
        if any(j.status == 'unknown' for j in jobs):
            _failure(entry, 'character_result_unknown', '人物请求结果未知；请先核对供应商记录，未重复提交', unknown=True); return
        if not missing:
            entry.update(status='succeeded',bindings=[{'participant_id':p.id,'shot_id':p.shot_id,'version_id':p.character_version_id,'asset_ids':list(p.reference_asset_ids_json)} for p in participants],reused=True)
            return  # completed candidate remains unreviewed; nobody needs it now
        asset = next((session.get(m.CharacterAsset, j.result_asset_id) for j in jobs if j.result_asset_id and j.status in {'needs_review', 'completed'}), None)
        if not asset:
            message = next((j.error_message for j in jobs if j.error_message), '人物生成失败')
            _failure(entry, 'character_generation_failed', message, transient=_temporary_failure(message), max_retries=run.snapshot_json['options']['max_retries']); return
        _verify_image(settings, asset)
        version = get(session, m.CharacterVersion, batch.character_version_id)
        targets=[p for p in missing if p.id in entry.get('participant_ids',[p.id for p in missing])]
        expected=entry.get('source_version_ids',{})
        if any(p.id in expected and p.character_version_id!=expected[p.id] for p in targets):
            raise AppError(409,'studio_identity_intent_changed','候选生成期间明确身份绑定已更改，旧候选保留；不会覆盖新的绑定意图')
        asset.status = 'approved'; asset.reviewed_by = 'automation'
        asset.review_reason = ACCEPTANCE['scope']; asset.is_primary_reference = True
        asset.metadata_json = {**asset.metadata_json, 'studio_acceptance': {'run_id': run.id, **ACCEPTANCE}}
        version.standard_face_asset_id, version.approval_state = asset.id, 'approved'
        version.anchor_locked_at, version.approved_at = utcnow(), utcnow()
        version.identity_json = {**version.identity_json, 'studio_acceptance': {'run_id': run.id, **ACCEPTANCE}, 'human_identity_approved': False}
        # A new provisional identity is usable only by its explicit shot bindings;
        # never replace the library's formally approved identity pointer.
        _bind_character(session, settings, run, character.id, version, [asset.id],participant_ids=[p.id for p in targets])
        _accept(session, 'character_asset', asset.id); _accept(session, 'character_version', version.id, version.version)
        entry.update(status='succeeded', version_id=version.id, asset_ids=[asset.id], reused=False); return
    explicit=[session.get(m.CharacterVersion,p.character_version_id) for p in missing if p.character_version_id]
    if any(v and v.approval_state=='approved' for v in explicit):
        raise AppError(409,'studio_explicit_reference_missing','明确批准身份的引用无效或缺失，请修复原版本；不会换用全库默认人物身份')
    if any(p.character_version_id and session.get(m.CharacterVersion,p.character_version_id) is None for p in missing):
        raise AppError(409,'studio_explicit_version_missing','明确指定的身份版本不存在，未自动替换')
    sources=[v for v in explicit if v]
    if sources and len({fingerprint(v.identity_json.get('profile') or {}) for v in sources})>1:
        raise AppError(409,'studio_identity_draft_conflict','同角色缺口明确绑定了不同的草稿身份资料；请分别制作或确认绑定，不会合并为同一张脸')
    if not dispatch: return
    if settings.h3_demo_mode: raise AppError(409, 'studio_real_service_required', '自动制作要求真实服务，Demo 模式未生成任何素材')
    route_shot_id = _shared_route_scope(session, run, entry, 'character_image')
    service, snapshot = _service(session, run, 'image', 'character_image', route_shot_id)
    parameters = _route_parameters(run, 'character_image', route_shot_id)
    if set(parameters)-{'width','height','seed','quality'}:
        raise AppError(422, 'studio_character_parameter_unsupported', '人物候选槽当前只支持width、height、seed、quality参数，未忽略其他参数')
    latest=session.scalar(select(m.CharacterVersion).where(m.CharacterVersion.character_id==character.id).order_by(m.CharacterVersion.version.desc()).limit(1))
    current=sources[0] if sources else latest
    try:
        from app.services.identity_profiles import resolve_profile
        profile = resolve_profile(current.identity_json if current else {},name=character.display_name,description=character.description)
    except ValueError as exc: raise AppError(409, 'studio_character_profile_invalid', character.display_name+'的人物资料格式无效，请核对资料；年龄可以留空，无需成年确认') from exc
    version = m.CharacterVersion(character_id=character.id, version=(latest.version if latest else 0)+1,
             identity_json={**(current.identity_json if current else {}), 'profile':profile.model_dump(), 'studio_run_id': run.id, 'human_identity_approved': False,
                            'source_version_ids':list(dict.fromkeys(v.id for v in sources))})
    session.add(version); session.flush()
    from app.services.character_factory import build_batch_plan
    width, height = int(parameters.get('width',snapshot['settings'].get('width', 832))), int(parameters.get('height',snapshot['settings'].get('height', 1024)))
    explicit = _explicit_route(_route(run,'character_image',route_shot_id))
    payload = BatchRequest(version_id=version.id, project_id=run.project_id, kind='candidates', candidate_count=1, service_id=service.id,
               model_id=snapshot['model_id'] if explicit else snapshot['settings'].get('candidate_model') if snapshot['provider'] == 'comfyui' else snapshot['model_id'],
               width=width, height=height, max_tasks=1, max_retries=0,
               seed=parameters.get('seed',202610210), quality=parameters.get('quality','standard'),
               prompt_template='A real camera photograph on a physical set. Natural skin texture matching the supplied character facts, individual hair strands, woven fabric and motivated practical light. '+character.description)
    plan = build_batch_plan(session, settings, character.id, payload)
    batch = m.CharacterBatch(character_id=character.id, character_version_id=version.id, kind='candidates', snapshot_json={**plan, 'studio_route':copy.deepcopy(_route(run,'character_image',route_shot_id)), 'studio_scope': {'run_id': run.id, **ACCEPTANCE}})
    session.add(batch); session.flush()
    for task in plan['jobs']:
        session.add(m.CharacterGenerationJob(batch_id=batch.id, asset_type=task['asset_type'], prompt=task['prompt'], negative_prompt=task['negative_prompt'], seed=task['seed'], snapshot_json={k: v for k, v in plan.items() if k not in {'jobs', 'preview_hash'}} | {'type': task['type'], 'attempt': 0}))
    entry.update(child_id=batch.id, status='running', attempts=entry['attempts']+1,
                 participant_ids=[p.id for p in missing],source_version_ids={p.id:p.character_version_id for p in missing})


def _visual(session, settings, run, entry, dispatch):
    from app.services.cinematic_visuals import build_visual
    options = run.snapshot_json['options']
    shot = get(session, m.StoryboardShot, entry['target_id']) if entry['kind'] == 'keyframe' else None
    if shot: _assert_shot_live(session, shot)
    instruction=str(options.get('instructions',{}).get(shot.id,'') or '').strip() if shot else ''
    instruction_hash=fingerprint({'instruction':instruction})
    def matching_instruction(candidate):
        provenance=candidate.snapshot_json.get('studio_modification')
        return provenance.get('instruction_sha256')==instruction_hash if provenance else not instruction
    scene = session.get(m.Scene, entry.get('scene_id')) if entry['kind'] == 'scene' else production_context(session, shot)[1]
    if not shot and not session.scalar(select(m.StoryboardShot.id).join(m.ShotDetail, m.ShotDetail.shot_id == m.StoryboardShot.id)
            .where(m.StoryboardShot.id.in_(run.snapshot_json['shot_ids']), m.StoryboardShot.status != 'deleted', m.ShotDetail.scene_id == scene.id).limit(1)):
        raise AppError(409, 'studio_scene_unused', '此场景对应的本轮镜头均已删除，不再提交新的场景生成任务')
    visual = session.get(m.ProductionVisual, entry['child_id']) if entry.get('child_id') else None
    if not visual:
        expected = fingerprint(visual_inputs(session, shot)) if shot else None
        candidates = session.scalars(select(m.ProductionVisual).where(m.ProductionVisual.shot_id == shot.id if shot else m.ProductionVisual.scene_id == scene.id,
              m.ProductionVisual.kind == ('keyframe' if shot else 'scene_reference'), m.ProductionVisual.status.in_(['approved', 'needs_review', 'queued', 'submitting', 'submitted', 'running', 'unknown'])).order_by(m.ProductionVisual.created_at.desc()))
        scope=shot.id if shot else entry.get('route_shot_id')
        visual = next((v for v in candidates if _matching_route_output(run,'keyframe_image',scope,v.snapshot_json) and (v.snapshot_json.get('inputs_hash') == expected and matching_instruction(v) if shot else v.entity_id == entry.get('entity_id') and v.snapshot_json.get('visual_inputs', {}).get('environment') == scene.environment_json)), None)
        if not visual and not shot and scene.source_json.get('reference_scene_id'):
            # Import revisions may point at the same physical scene without
            # rewriting any old scene's script lineage. Reuse its actual bytes
            # only when environment and the versioned design are equivalent.
            source_id = scene.source_json['reference_scene_id']
            current_entity = session.get(m.ProductionEntity, entry['entity_id']) if entry.get('entity_id') else None
            previous = session.scalars(select(m.ProductionVisual).where(m.ProductionVisual.scene_id == source_id,
                        m.ProductionVisual.kind == 'scene_reference', m.ProductionVisual.status == 'approved').order_by(m.ProductionVisual.created_at.desc()))
            for old in previous:
                if not _matching_route_output(run,'keyframe_image',entry.get('route_shot_id'),old.snapshot_json):continue
                old_entity = session.get(m.ProductionEntity, old.entity_id) if old.entity_id else None
                if old.snapshot_json.get('visual_inputs', {}).get('environment') != scene.environment_json: continue
                if bool(old_entity) != bool(current_entity) or old_entity and old_entity.spec_json != current_entity.spec_json: continue
                _verify_image(settings, old)
                context = {'scene_id': scene.id, 'environment': scene.environment_json,
                           'entity': {'id': current_entity.id, 'version': current_entity.version, 'spec': current_entity.spec_json} if current_entity else None}
                visual = m.ProductionVisual(project_id=run.project_id, scene_id=scene.id, entity_id=entry.get('entity_id'),
                    kind='scene_reference', status='approved', prompt=old.prompt, relative_path=old.relative_path, sha256=old.sha256,
                    snapshot_json={**old.snapshot_json, 'source': 'reused_existing_scene', 'reused_from_visual_id': old.id,
                                   'visual_inputs': context, 'inputs_hash': fingerprint(context)},
                    review_json={**ACCEPTANCE, 'reused_from_visual_id': old.id, 'source_review': old.review_json})
                session.add(visual); session.flush()
                registry = session.scalar(select(m.ManagedFile).where(m.ManagedFile.relative_path == old.relative_path))
                if registry: registry.ownership_json = [*registry.ownership_json, {'table': 'production_visuals', 'id': visual.id}]
                session.add(m.AuditEvent(object_type='production_visual', object_id=visual.id, action='same_scene_bytes_reused', details_json={'source_visual_id': old.id, 'source_scene_id': source_id, 'sha256': old.sha256, **ACCEPTANCE}))
                break
        if visual: entry.update(child_id=visual.id, reused=True)
    if visual:
        if shot and not matching_instruction(visual):
            raise AppError(409,'studio_visual_modification_stale','此关键画面未包含当前镜头修改要求；原图保留，请只重试本镜关键画面')
        if visual.status in ACTIVE_CHILD: entry['status'] = 'running'; return
        if visual.status in {'failed', 'rejected', 'cancelled', 'unknown'}:
            _failure(entry, 'visual_'+visual.status, visual.error_message or '参考图生成失败', unknown=visual.status == 'unknown',
                     transient=visual.status == 'failed' and _temporary_failure(visual.error_message), max_retries=options['max_retries']); return
        if visual.status not in {'approved', 'needs_review'}: raise AppError(409, 'studio_visual_state', '参考图状态不可自动采纳')
        if shot and visual.snapshot_json.get('inputs_hash') != fingerprint(visual_inputs(session, shot)):
            raise AppError(409, 'studio_visual_stale', '镜头在图片生成期间已修改；已保留旧文件，请只重试受影响的关键画面')
        if not shot and (visual.entity_id != entry.get('entity_id') or visual.snapshot_json.get('visual_inputs', {}).get('environment') != scene.environment_json):
            raise AppError(409, 'studio_scene_stale', '场景设定在图片生成期间已修改；旧参考保留，请只重试此场景')
        _verify_image(settings, visual)
        if visual.status != 'approved':
            visual.status = 'approved'; visual.review_json = {'run_id': run.id, **ACCEPTANCE}
            _accept(session, 'production_visual', visual.id)
        entry.update(status='succeeded', visual_id=visual.id)
        if shot:
            detail, _, _, prod, _ = production_context(session, shot)
            references = [n['visual_id'] for n in run.nodes_json if n['id'] in entry['dependencies'] and n.get('visual_id')]
            bound = list(dict.fromkeys([*references, visual.id]))
            detail.source_json = {**detail.source_json, 'production': {**prod, 'visual_ids': bound}}
        return
    if not dispatch: return
    if settings.h3_demo_mode: raise AppError(409, 'studio_real_service_required', '自动制作要求真实服务，Demo 模式未生成任何素材')
    route_shot_id = shot.id if shot else _shared_route_scope(session,run,entry,'keyframe_image')
    _, snapshot = _service(session, run, 'image', 'keyframe_image', route_shot_id)
    parameters = _route_parameters(run,'keyframe_image',route_shot_id)
    if set(parameters)-{'width','height','seed'}:
        raise AppError(422,'studio_visual_parameter_unsupported','场景／关键帧槽当前只支持width、height、seed参数；未忽略其他参数')
    if snapshot['provider'] == 'comfyui':
        width, height = generation_dimensions(run,shot)
    else:
        width, height = int(snapshot['settings'].get('width', 1024)), int(snapshot['settings'].get('height', 1024))
    width,height=parameters.get('width',width),parameters.get('height',height)
    seed=parameters.get('seed',video_seed('keyframe:'+run.id,shot.id,entry['attempts']+1) if shot else 202610230+entry['attempts'])
    direction=('CURRENT CHANGE FOR THIS SHOT ONLY: '+instruction+'. Apply this correction to the new image even when earlier descriptive references show the prior state. Preserve each referenced identity, the standard photographic production rules, and all unrelated elements.') if instruction else ''
    explicit = _explicit_route(_route(run,'keyframe_image',route_shot_id))
    model_kwargs = {'model_id':snapshot['model_id']} if explicit and 'model_id' in VisualInput.model_fields else {}
    if explicit and not model_kwargs and snapshot['model_id'] != session.get(m.AIService,snapshot['service_id']).default_model:
        raise AppError(409,'studio_visual_model_override_unsupported','当前图片适配器尚未接入显式模型；未改用服务默认模型')
    visual = build_visual(session, settings, run.project_id, VisualInput(kind='keyframe' if shot else 'scene_reference',
               shot_id=shot.id if shot else None, scene_id=scene.id if not shot else None,
               entity_id=entry.get('entity_id'), service_id=snapshot['service_id'], width=width, height=height,
               seed=seed, **model_kwargs,
               instructions=direction,
               confirm_cloud_test=bool(options['allow_cloud_costs'] and snapshot['is_cloud'])))
    visual.snapshot_json = {**visual.snapshot_json, 'studio_route':copy.deepcopy(_route(run,'keyframe_image',route_shot_id)), 'studio_scope': {'run_id': run.id, **ACCEPTANCE}}
    if shot:
        visual.snapshot_json={**visual.snapshot_json,'studio_modification':{'instruction':instruction,'instruction_sha256':instruction_hash,
                'source':'StudioRun.snapshot_json.options.instructions','run_id':run.id,'shot_id':shot.id,
                'scope':'this shot keyframe and video only; source screenplay, shared characters and scene records preserved'}}
    entry.update(child_id=visual.id, status='running', attempts=entry['attempts']+1,seed=seed)


def _accept_inputs(session, settings, run, shot):
    _assert_shot_live(session, shot)
    detail, _, script, _, entities = production_context(session, shot)
    if not script or not detail: raise AppError(409, 'studio_structured_script_required', '镜头缺少结构化剧本及场景关联，请先导入 Word')
    for entity in entities:
        if entity and entity.status != 'approved':
            entity.status = 'approved'; entity.source_json = {**entity.source_json, 'studio_acceptance': {'run_id': run.id, **ACCEPTANCE}}
            _accept(session, 'production_entity', entity.id, entity.version)
    session.flush()
    pre = shot_preflight(session, settings, shot)
    blockers = [f for f in pre['findings'] if f['severity'] == 'blocker']
    if blockers: raise AppError(409, 'studio_input_exception', '；'.join(f['message'] for f in blockers), {'findings': blockers})
    detail.source_json = {**detail.source_json, 'studio_acceptance': {'run_id': run.id, **ACCEPTANCE,
                            'inputs_hash': pre['inputs_hash'], 'findings': pre['findings']}}
    if script.status != 'approved':
        script.status, script.approved_by, script.approved_at = 'approved', 'automation', utcnow()
        _accept(session, 'script_revision', script.id, script.revision)
    if shot.status != 'approved':
        shot.status, shot.approved_at = 'approved', utcnow()
        _accept(session, 'shot', shot.id, shot.revision)
    session.flush()


def video_seed(run_id, shot_id, attempt):
    """Stable per durable dispatch, distinct across new runs and retry attempts."""
    material=json.dumps([run_id,shot_id,attempt],ensure_ascii=False,separators=(',',':')).encode()
    return int.from_bytes(hashlib.sha256(material).digest()[:8],'big') & 0x7fffffff

def generation_dimensions(run,shot):
    config=run.snapshot_json['options'].get('quality_by_shot',{}).get(shot.id) if shot else None
    if config:
        from app.services.video_quality import plan
        actual=plan(config)['generation'];return actual['width'],actual['height']
    options=run.snapshot_json['options'];return options['width'],options['height']

def generation_quality_parameters(run,shot):
    config=run.snapshot_json['options'].get('quality_by_shot',{}).get(shot.id)
    if not config:return {}
    from app.services.video_quality import plan
    route=_route(run,'video_generation',shot.id)
    snapshot=(route.get('selected') or {}).get('service_snapshot',{}) if _explicit_route(route) else run.snapshot_json.get('services',{}).get('video',{})
    result={'quality_request':config}
    if snapshot.get('provider')=='comfyui':result['steps']=plan(config)['generation']['steps']
    return result


def generation_parameters(run,shot):
    result=_route_parameters(run,'video_generation',shot.id)
    if 'seed' in result:
        raise AppError(422,'studio_video_seed_managed','Studio重生成的seed由运行ID、镜头和次数持久化计算；请移除视频槽参数中的seed')
    width,height=generation_dimensions(run,shot)
    if shot.id not in run.snapshot_json['options'].get('quality_by_shot',{}):
        width,height=result.get('width',width),result.get('height',height)
    return {**result,**generation_quality_parameters(run,shot),'width':width,'height':height}

def _quality(session,settings,run,entry):
    from app.services.video_quality import create_render
    raw=_predecessor(run.nodes_json,entry)
    if not raw:raise AppError(409,'studio_processing_source_missing','前一道处理没有已持久化视频，未绕过处理链使用原片')
    item=session.get(m.QualityRender,entry['child_id']) if entry.get('child_id') else None
    if not item:
        route=_route(run,'video_upscale',entry['target_id'])
        item=create_render(session,settings,raw['video_id'],run.snapshot_json['options']['quality_by_shot'][entry['target_id']],
                           route_snapshot=route if _explicit_route(route) else None)
        entry.update(child_id=item.id,status='running',attempts=entry['attempts']+1,source_video_id=raw['video_id'])
    elif item.status=='generated':
        video=get(session,m.VideoRevision,item.video_id);_valid_video(session,settings,video)
        entry.update(status='succeeded',video_id=video.id)
    elif item.status in {'failed','cancelled'}:_failure(entry,'quality_processing_failed',item.error_message or '视频后处理失败，原视频保留')
    elif item.status=='unknown':_failure(entry,'quality_processing_unknown',item.error_message or '画质处理提交结果未知；未重新派发',unknown=True)
    else:entry['status']='running'


def _quality_check(session,settings,run,entry):
    route=_route(run,'quality_check',entry['target_id'])
    actual_service=None
    if _explicit_route(route):
        from app.services.task_routing import assert_frozen
        _,snapshot=assert_frozen(session,route)
        if snapshot['provider']!='ffmpeg':
            raise AppError(409,'studio_quality_adapter_unavailable','此质量检查槽尚无已实现执行桥；没有用FFmpeg冒充所选模型')
        if _route_parameters(run,'quality_check',entry['target_id']):
            raise AppError(422,'studio_quality_parameter_unsupported','技术检查尚不接受额外模型参数，未静默忽略')
        actual_service=snapshot
    source=_predecessor(run.nodes_json,entry)
    if not source:raise AppError(409,'studio_processing_source_missing','质量检查前没有已持久化的最终派生视频')
    video=get(session,m.VideoRevision,source['video_id']);_valid_video(session,settings,video)
    if actual_service:
        from dataclasses import replace
        from app.services.quality import inspect_media
        options=actual_service['settings']
        tools=replace(settings,ffmpeg_path=options.get('ffmpeg_path') or settings.ffmpeg_path,ffprobe_path=options.get('ffprobe_path') or settings.ffprobe_path)
        results=inspect_media(safe_media_path(settings.media_root,video.relative_path,must_exist=True),tools)
        if any(item['status']=='failed' for item in results):raise AppError(422,'studio_selected_quality_failed','所选检测服务的真实媒体检查未通过',{'checks':results})
        for item in results:session.add(m.QualityCheck(video_revision_id=video.id,check_type=item['check_type'],status=item['status'],details_json=item['details']))
        session.flush()
    entry.update(status='succeeded',video_id=video.id,source_video_id=video.id,
                 evidence={'quality_check_ids':list(session.scalars(select(m.QualityCheck.id).where(m.QualityCheck.video_revision_id==video.id))),
                           'actual_service_snapshot':actual_service,'human_review':'pending','visual_identity':'NOT RUN','dialogue_asr':'NOT RUN','lip_sync_quality':'NOT RUN'})


def _media(session,settings,run,entry,dispatch):
    from app.services.media_processing import create_task
    from app.schemas_routing import TaskSubmit
    shot_id=entry.get('shot_id') or entry['target_id']
    shot=get(session,m.StoryboardShot,shot_id);_assert_shot_live(session,shot)
    task=session.get(m.MediaProcessTask,entry['child_id']) if entry.get('child_id') else None
    if task:
        entry['attempt_history']=copy.deepcopy(task.result_json.get('attempt_history',[]))
        if task.status in {'queued','running'}:entry['status']='running';return
        if task.status=='unknown':_failure(entry,'media_submission_unknown',task.error_message or '音频／口型结果未知；没有重新提交',unknown=True);return
        if task.status in {'failed','blocked','cancelled'}:
            _failure(entry,task.result_json.get('error_code') or 'media_'+task.status,task.error_message or '媒体处理未完成；原始视频保留');return
        if task.status!='succeeded':raise AppError(409,'studio_media_state','无法识别媒体子任务状态，未猜测成功')
        result=task.result_json
        if entry['kind']=='tts':
            asset=get(session,m.AudioAsset,result.get('audio_id'))
            participant=get(session,m.ShotCharacter,entry['participant_id'])
            line=get(session,m.DialogueLine,entry['target_id'])
            if task.snapshot_json.get('inputs',{}).get('text') not in (None,line.text):
                raise AppError(409,'studio_dialogue_changed','配音期间对白文本已变化；旧音频保留，不用于当前镜头')
            if asset.project_id!=run.project_id or asset.shot_id!=shot_id or asset.participant_id!=participant.id or asset.character_version_id!=participant.character_version_id:
                raise AppError(409,'studio_audio_output_ownership','配音输出没有绑定本镜头与说话人')
            path=safe_media_path(settings.media_root,asset.relative_path,must_exist=True)
            if hashlib.sha256(path.read_bytes()).hexdigest()!=asset.sha256:raise AppError(409,'studio_audio_changed','配音文件SHA变化')
            entry.update(status='succeeded',audio_id=asset.id)
        else:
            video=get(session,m.VideoRevision,result.get('video_id'))
            if video.project_id!=run.project_id or video.shot_id!=shot_id:raise AppError(409,'studio_media_output_ownership','派生视频不属于当前镜头')
            _valid_video(session,settings,video)
            entry.update(status='succeeded',video_id=video.id)
            if result.get('audio_id'):entry['audio_id']=result['audio_id']
        return
    if not dispatch:return
    options=run.snapshot_json['options'];kind=entry['kind'];slot='lip_sync' if kind=='lip_sync' else 'dialogue_generation'
    route=_route(run,slot,shot_id)
    if kind=='lip_sync' and (not route or not route.get('selected')):
        raise AppError(409,'task_route_blocked','必须执行的口型槽没有已验证且已授权模型；未把此步骤当作成功或可选跳过')
    inputs={}
    if kind=='tts':
        participant=get(session,m.ShotCharacter,entry['participant_id'])
        inputs={'dialogue_line_id':entry['target_id'],'participant_id':participant.id}
        reference=participant.performance_json.get('reference_audio_asset_id')
        if reference:inputs['reference_audio_asset_id']=reference
    else:
        source=_predecessor(run.nodes_json,entry)
        if not source:raise AppError(409,'studio_processing_source_missing','媒体处理前缺少已完成视频，不会回退到旧原片')
        inputs={'video_id':source['video_id']}
        entry['source_video_id']=source['video_id']
        if kind=='audio_mix':
            audio_ids=[n['audio_id'] for identifier in entry['dependencies'] for n in run.nodes_json if n['id']==identifier and n.get('audio_id')]
            inputs.update(operation='audio_mix',audio_ids=audio_ids,mode=entry.get('audio_policy','replace_dialogue'))
            route=None  # FFmpeg mixing is a local deterministic child, not a TTS reroute.
        else:
            participants=list(session.scalars(select(m.ShotCharacter).where(m.ShotCharacter.shot_id==shot_id)))
            if len(participants)!=1:raise AppError(409,'lip_sync_single_target','口型仅允许一个明确参与人物；多人镜头未交给原生第一张脸处理')
            if not source.get('audio_id'):raise AppError(409,'studio_lip_audio_required','口型需要前一道完整混音音轨；未拿某一句配音替换全片声音')
            inputs.update(audio_id=source['audio_id'],participant_id=participants[0].id)
    parameters=_route_parameters(run,slot,shot_id) if kind!='audio_mix' else {}
    task=create_task(session,settings,TaskSubmit(slot=slot,project_id=run.project_id,shot_id=shot_id,inputs=inputs,parameters=parameters,
                     allow_cloud_costs=options['allow_cloud_costs'],allow_paid_fallback=options.get('allow_paid_fallback',False)),kind=kind,route=route)
    entry.update(child_id=task.id,status='running',attempts=entry['attempts']+1)
    # Persist the actual typed child before any worker can dispatch it. Unknown
    # children remain attached to this node across requests/restarts.
    session.flush()


async def _video(app, session, settings, run, entry, dispatch):
    shot = get(session, m.StoryboardShot, entry['target_id']); options = run.snapshot_json['options']
    _assert_shot_live(session, shot)
    if entry['status'] == 'dispatching' and not entry.get('child_id'):
        # A crash can happen after provider POST and before local ID persistence.
        recovered = next((j for j in session.scalars(select(m.GenerationJob).where(m.GenerationJob.shot_id == shot.id)) if j.parameters_json.get('studio_node_id') == entry['id'] and j.parameters_json.get('studio_run_id') == run.id), None)
        if recovered: entry['child_id'] = recovered.id
        else: _failure(entry, 'studio_dispatch_unknown', '服务在提交时中断，供应商可能已接收请求；未自动重复提交，请先核对原任务', unknown=True); return
    job = session.get(m.GenerationJob, entry['child_id']) if entry.get('child_id') else None
    if job:
        if job.status in ACTIVE_CHILD: entry['status'] = 'running'; return
        if job.status == 'failed' and job.error_code == 'output_quality_failed' and job.provider_job_id:
            # Reinspect the saved provider output; never create a new paid task.
            # The recheck rejects stale inputs, changed media and other failures.
            preserved=session.scalar(select(m.VideoRevision).where(m.VideoRevision.generation_job_id==job.id).order_by(m.VideoRevision.revision.desc()))
            if not preserved: raise AppError(409,'studio_video_output_missing','云端任务有结果但未找到本地原片；不能重复付费提交')
            from app.services.jobs import recheck_cloud_duration_warning
            recheck_cloud_duration_warning(session,settings,preserved)
        if job.status in {'failed', 'cancelled', 'unknown', 'closed_unknown'}:
            retryable = job.status == 'failed' and (
                job.error_code == 'provider_unavailable' or
                job.error_code in {'comfyui_execution_failed', 'cloud_video_submit_failed'} and _temporary_failure(job.error_message))
            _failure(entry, job.error_code or 'video_'+job.status, job.error_message or '视频生成失败', unknown=job.status in {'unknown','closed_unknown'}, transient=retryable, max_retries=options['max_retries']); return
        video = session.scalar(select(m.VideoRevision).where(m.VideoRevision.generation_job_id == job.id))
        if not video: raise AppError(409, 'studio_video_output_missing', '任务已结束但没有真实视频记录')
        expected_hash = job.parameters_json.get('screenplay_snapshot', {}).get('cinematic', {}).get('preflight', {}).get('generation_inputs_hash')
        if expected_hash and expected_hash != generation_inputs_hash(session, shot):
            raise AppError(409, 'studio_video_stale', '生成期间镜头内容已变化；真实旧视频已保留，请仅重生成当前镜头')
        _valid_video(session, settings, video)
        entry.update(status='succeeded', video_id=video.id, inputs_hash=generation_inputs_hash(session, shot))
        video.metadata_json = {**video.metadata_json, 'studio_scope': {'run_id': run.id, **ACCEPTANCE}}
        return
    _accept_inputs(session, settings, run, shot)
    current_hash = generation_inputs_hash(session, shot)
    if shot.id not in options.get('force_shot_ids', []):
        for video in session.scalars(select(m.VideoRevision).where(m.VideoRevision.shot_id == shot.id, m.VideoRevision.review_status != 'rejected').order_by(m.VideoRevision.revision.desc())):
            if video.metadata_json.get('quality_render_id') or video.metadata_json.get('processing_task_id'):continue
            previous = session.get(m.GenerationJob, video.generation_job_id)
            if not previous:continue
            if not _matching_route_output(run,'video_generation',shot.id,{'service_snapshot':previous.parameters_json.get('service_snapshot',{})},previous.parameters_json,generation_parameters(run,shot)):continue
            actual = previous.parameters_json.get('screenplay_snapshot', {}).get('cinematic', {}).get('preflight', {}).get('generation_inputs_hash')
            instruction=str(options.get('instructions',{}).get(shot.id,'') or '').strip()
            if previous.parameters_json.get('prompt_override') != options.get('prompt_overrides',{}).get(shot.id):continue
            if shot.id in options.get('quality_by_shot',{}):
                width,height=generation_dimensions(run,shot);requested_params=generation_quality_parameters(run,shot)
                if previous.parameters_json.get('width')!=width or previous.parameters_json.get('height')!=height or ('steps' in requested_params and previous.parameters_json.get('steps')!=requested_params['steps']):continue
            if actual == current_hash and str(previous.parameters_json.get('instruction') or '').strip()==instruction:
                if previous.status=='failed' and previous.error_code=='output_quality_failed' and previous.provider_job_id:
                    from app.services.jobs import recheck_cloud_duration_warning
                    recheck_cloud_duration_warning(session,settings,video)
                _valid_video(session, settings, video)
                entry.update(status='succeeded', child_id=previous.id, video_id=video.id, reused=True, inputs_hash=current_hash); return
    if not dispatch: return
    if settings.h3_demo_mode: raise AppError(409, 'studio_real_service_required', '自动制作要求真实视频服务，未使用 Demo 替代')
    _, snapshot = _service(session, run, 'video', 'video_generation', shot.id)
    route_parameters=generation_parameters(run,shot)
    width,height=route_parameters['width'],route_parameters['height']
    if snapshot['provider'] == 'comfyui' and (width % 32 or height % 32 or max(width, height) > 1344):
        raise AppError(422, 'local_video_dimensions', '本地 H3 宽高必须为32的倍数且不超过1344')
    attempt=entry['attempts']+1
    entry.update(status='dispatching', attempts=attempt, inputs_hash=current_hash, seed=video_seed(run.id,shot.id,attempt))
    _persist(session, run, run.nodes_json)  # node is the same dict as in current durable list
    from app.api.routes import generate_for_shot
    try:
        job, _ = await generate_for_shot(Request({'type': 'http', 'app': app}), session, shot,
            GenerationRequest(service_id=snapshot['service_id'], model_id=snapshot['model_id'], seed=entry['seed'], prompt_override=options.get('prompt_overrides',{}).get(shot.id),
            parameters={**route_parameters,'width': width, 'height': height, 'requested_mode': 'real', **generation_quality_parameters(run,shot),
                        'studio_run_id': run.id, 'studio_node_id': entry['id'],
                        'studio_route':copy.deepcopy(_route(run,'video_generation',shot.id)),
                        'studio_instruction_sha256':fingerprint({'instruction':str(options.get('instructions',{}).get(shot.id,'') or '').strip()}),
                        'studio_instruction_source':'StudioRun.snapshot_json.options.instructions; this shot only'}), instruction=options.get('instructions', {}).get(shot.id))
    except Exception:
        candidates = list(session.scalars(select(m.GenerationJob).where(m.GenerationJob.shot_id == shot.id).order_by(m.GenerationJob.created_at.desc())))
        found = next((j for j in candidates if j.parameters_json.get('studio_run_id') == run.id and j.parameters_json.get('studio_node_id') == entry['id']), None)
        if found: entry['child_id'] = found.id
        raise
    entry.update(child_id=job.id, status='running')


def _overall(run, nodes):
    videos = [leaf for shot_id in run.snapshot_json['shot_ids'] if (leaf:=_leaf(nodes,shot_id))]
    errors = [n for n in nodes if n['status'] in {'failed', 'exception', 'unknown'}]
    if videos and all(n['status'] == 'succeeded' for n in videos) and all(
            n['status'] == 'succeeded' or n['status'] == 'skipped' and n.get('optional') for n in nodes):
        run.status = 'waiting_review'
    # A failed node is not completion. Keep the run durable and active until
    # every requested output succeeds (or the user explicitly pauses/cancels).
    # This leaves dependent work available to the repair/retry controls rather
    # than making a transient prerequisite failure terminal for the whole DAG.
    else: run.status = 'running'
    run.error_message = '；'.join(n['error'] for n in errors if n.get('error'))[:3000] or None
    run.result_json = {**run.result_json, 'videos': {n['target_id']: n['video_id'] for n in videos if n.get('video_id')},
                       'succeeded_nodes': sum(n['status'] == 'succeeded' for n in nodes), 'total_nodes': len(nodes)}


def _resource_busy(session, nodes):
    """A paused run's finished child must not lock every other project forever."""
    for n in nodes:
        if n['status'] == 'dispatching' and not n.get('child_id'): return True
        if n['status'] not in {'running', 'dispatching'} or not n.get('child_id'): continue
        if n['kind'] == 'character':
            batch = session.get(m.CharacterBatch, n['child_id'])
            statuses = list(ACTIVE_CHILD-({'queued'} if batch and batch.paused else set()))
            if session.scalar(select(m.CharacterGenerationJob.id).where(m.CharacterGenerationJob.batch_id == n['child_id'],
                          m.CharacterGenerationJob.status.in_(statuses)).limit(1)): return True
        elif n['kind']=='quality':
            render=session.get(m.QualityRender,n['child_id'])
            if render and render.status=='running' and render.snapshot_json.get('ai_super_resolution'):return True
            continue
        elif n['kind'] in MEDIA_KINDS:
            child=session.get(m.MediaProcessTask,n['child_id'])
            if child and child.status in {'queued','running'}:return True
        else:
            child = session.get(m.GenerationJob if n['kind'] == 'video' else m.ProductionVisual, n['child_id'])
            if child and child.status in ACTIVE_CHILD: return True
    return False


def _queue_automatic_draft(session, settings, run, nodes):
    """Atomically persist a technical draft without granting human approval."""
    from app.schemas_product import TimelineRequest
    from app.services.assembly import validate_timeline
    from app.services.task_routing import assert_frozen
    if run.result_json.get('render_id'):
        return
    shot_ids=run.snapshot_json.get('shot_ids', [])
    clips=[]
    for shot_id in shot_ids:
        leaf=_leaf(nodes,shot_id)
        if not leaf or leaf.get('status')!='succeeded' or not leaf.get('video_id'):
            raise AppError(409,'draft_inputs_incomplete','所有镜头须完成真实视频及技术检查后才能创建自动草稿')
        video=get(session,m.VideoRevision,leaf['video_id'])
        if video.project_id!=run.project_id or video.shot_id!=shot_id or video.review_status not in {'pending','approved'}:
            raise AppError(409,'draft_video_not_eligible','草稿视频已驳回、过期或不属于本镜头')
        job=get(session,m.GenerationJob,video.generation_job_id)
        shot=get(session,m.StoryboardShot,shot_id)
        original=(job.parameters_json.get('screenplay_snapshot',{}).get('cinematic',{}).get('preflight',{}).get('generation_inputs_hash'))
        if not original or original!=generation_inputs_hash(session,shot):
            raise AppError(409,'draft_video_stale','镜头输入与视频快照不一致，不创建草稿')
        _valid_video(session,settings,video)
        clips.append({'video_id':video.id,'trim_start':0,'trim_end':None})
    if not clips: raise AppError(409,'draft_inputs_incomplete','没有视频可合成')
    route=_route(run,'final_assembly')
    service_id=None
    if _explicit_route(route):
        service,snapshot=assert_frozen(session,route)
        if snapshot.get('provider')!='ffmpeg' or _route_parameters(run,'final_assembly'):
            raise AppError(409,'draft_assembly_route','自动草稿需要已验证的本地 FFmpeg 且不能静默覆盖输出参数')
        service_id=service.id
    options=run.snapshot_json.get('options',{})
    width,height=options.get('width',1024),options.get('height',576)
    quality=options.get('quality')
    if quality:
        from app.services.video_quality import plan
        requested=plan(quality)['requested']; width,height=requested['width'],requested['height']
    config=TimelineRequest.model_validate({'clips':clips,'transitions':[{'type':'cut','duration':0,'audio':'cut'} for _ in clips[1:]],
        'output':{'width':width,'height':height,'fps':24,'codec':'libx264','audio_policy':'preserve','subtitles':False},'service_id':service_id})
    timeline=validate_timeline(session,settings,run.project_id,config,allow_unreviewed_draft=True)
    timeline['automatic_draft']={'studio_run_id':run.id,'human_review_required':True,'transition':'cut','full_streams':True}
    assembly=m.Assembly(project_id=run.project_id,name='自动生成草稿（待人工审核）',timeline_json=timeline)
    session.add(assembly);session.flush()
    snapshot={**timeline,'project_id':run.project_id,'assembly_revision':assembly.revision,'assembly_name':assembly.name}
    if _explicit_route(route):snapshot['task_route_snapshot']=copy.deepcopy(route)
    render=m.AssemblyRender(assembly_id=assembly.id,version=1,kind='render',snapshot_json=snapshot)
    session.add(render);session.flush()
    run.status='rendering'
    run.result_json={**run.result_json,'assembly_id':assembly.id,'render_id':render.id,
        'automatic_draft':{'human_review_required':True,'status':'queued','video_ids':[c['video_id'] for c in clips],
                           'transition':'cut','preserve_full_streams':True}}
    session.add(m.AuditEvent(object_type='studio_run',object_id=run.id,action='automatic_draft_queued',details_json={
        'assembly_id':assembly.id,'render_id':render.id,'video_ids':[c['video_id'] for c in clips],
        'human_review_required':True,'no_human_approval_recorded':True}))


async def advance_pipeline(app, settings, session, run):
    from app.services.studio_project_guard import project_guard
    async with project_guard(app, run.project_id) as nested:
        if not nested and not session.dirty and not session.new:
            # The scheduler may have loaded this row before waiting for repair
            # or advancing another project. Reload the durable graph and children.
            identifier = run.id
            session.expire_all()
            run = session.get(StudioRun, identifier)
        return await _advance_pipeline(app, settings, session, run)


async def _advance_pipeline(app, settings, session, run):
    if run.snapshot_json.get('product_references_changed'):
        run.status='paused'
        sync_production_status(session,run)
        session.commit()
        return
    if run.status=='rendering' and run.result_json.get('render_id'):
        render=session.get(m.AssemblyRender,run.result_json['render_id'])
        if not render:
            run.status='completed_with_errors';run.error_message='本次成片渲染记录不存在，请核对历史版本'
        elif render.status=='generated':
            is_draft=bool(render.snapshot_json.get('automatic_draft'))
            run.status='waiting_review' if is_draft else 'completed';run.error_message=None
            run.result_json={**run.result_json,'relative_path':render.relative_path,
                **({'automatic_draft':{**run.result_json.get('automatic_draft',{}),'status':'generated','human_review_required':True}} if is_draft else {})}
        elif render.status in {'failed','cancelled'}:
            run.status='completed_with_errors';run.error_message=render.error_message or '本次合成'+render.status
        sync_production_status(session, run)
        session.commit()
        return  # actual render status owns this phase; never revert to shot review
    if run.status in RUN_TERMINAL | {'paused', 'cancelled'}: return
    nodes = copy.deepcopy(run.nodes_json)
    # Keep the same mutable list for durable dispatch correlation.
    run.nodes_json = nodes
    local_busy = _resource_busy(session, nodes)
    # Across projects, only one Studio child occupies the shared GPU/cloud slot.
    other_busy = any(_resource_busy(session, other.nodes_json) for other in session.scalars(select(StudioRun).where(StudioRun.id != run.id, StudioRun.status.not_in(list(RUN_TERMINAL)))))
    for entry in nodes:
        run.nodes_json = nodes
        if entry['status'] in TERMINAL: continue
        if entry['kind'] in {'keyframe', 'video','quality','quality_check',*MEDIA_KINDS}:
            try: _assert_shot_live(session, get(session, m.StoryboardShot, entry.get('shot_id') or entry['target_id']))
            except AppError as exc:
                _failure(entry, exc.code, exc.message)
                _persist(session, run, nodes)
                continue
        dependencies = [n for n in nodes if n['id'] in entry['dependencies']]
        if any(n['status'] != 'succeeded' for n in dependencies):
            entry['status'] = 'waiting_dependencies'; continue
        if entry['status']=='waiting_dependencies': entry['status']='pending'
        if entry['status'] == 'retrying' and entry.get('retry_after', 0) > time.time(): continue
        dispatch = not local_busy and not other_busy
        try:
            if entry['kind'] == 'character': _character(session, settings, run, entry, dispatch)
            elif entry['kind'] in {'scene', 'keyframe'}: _visual(session, settings, run, entry, dispatch)
            elif entry['kind']=='quality':_quality(session,settings,run,entry)
            elif entry['kind']=='quality_check':_quality_check(session,settings,run,entry)
            elif entry['kind'] in MEDIA_KINDS:_media(session,settings,run,entry,dispatch)
            else: await _video(app, session, settings, run, entry, dispatch)
        except AppError as exc:
            # Service/input errors are exceptions, never guessed model successes.
            unknown = exc.code in {'cloud_submission_unknown', 'cloud_video_invalid_response', 'studio_dispatch_unknown'}
            if entry['kind']=='video' and entry.get('child_id') and not unknown:
                # generate_for_shot persists definitive provider failures itself;
                # reconcile that exact row on the next tick before retry policy.
                entry.update(status='running', error=exc.message, code=exc.code)
            else: _failure(entry, exc.code, exc.message, unknown=unknown)
        except Exception as exc:
            logging.getLogger(__name__).exception('Studio node failed: %s', entry['id'])
            if entry['status'] == 'dispatching':
                _failure(entry, 'studio_dispatch_unknown', '提交时发生意外，供应商结果未知；未自动重试', unknown=True)
            else: _failure(entry, 'studio_internal_error', type(exc).__name__+'：请检查后台日志后重试异常')
        session.flush()
        local_busy = _resource_busy(session, nodes)
        # Pause is requested through another HTTP transaction while an actual
        # provider POST may be in flight. Respect that persisted control before
        # dispatching the next node, while preserving the accepted child ID.
        session.refresh(run, ['status'])
        run.nodes_json = nodes
        _persist(session, run, nodes)
        if run.status=='paused': return
    _overall(run, nodes)
    if run.status=='waiting_review' and run.snapshot_json.get('options',{}).get('automatic_draft_assembly'):
        try:
            _queue_automatic_draft(session, settings, run, nodes)
        except AppError as exc:
            run.result_json={**run.result_json,'automatic_draft':{'status':'blocked','code':exc.code,'human_review_required':True}}
            run.error_message='自动草稿未创建：'+exc.message
    _persist(session, run, nodes)


async def studio_worker(app, settings, session_factory):
    while True:
        try:
            with session_factory() as session:
                runs = list(session.scalars(select(StudioRun).where(StudioRun.status.not_in(list(RUN_TERMINAL | {'paused', 'cancelled'}))).order_by(StudioRun.created_at)))
                for run in runs: await advance_pipeline(app, settings, session, run)
        except asyncio.CancelledError: raise
        except Exception: logging.getLogger(__name__).exception('Studio worker iteration failed; durable state retained')
        await asyncio.sleep(2)


def overview(session, project_id):
    project = get(session, m.Project, project_id)
    script = session.scalar(select(m.ScriptRevision).where(m.ScriptRevision.project_id == project_id).order_by(m.ScriptRevision.revision.desc()).limit(1))
    run = latest_run(session, project_id); nodes = run.nodes_json if run else []
    characters, scenes, shots = {}, {}, []
    for display_order,shot in enumerate(_shots(session, project_id),1):
        bundle = shot_bundle(session, shot.id)
        scene = bundle['scene']
        if scene: scenes[scene['id']] = scene
        for participant in bundle['participants']:
            character = session.get(m.Character, participant['character_id'])
            characters[character.id] = {**row(character), 'version_id': participant['character_version_id'],
                'assets': participant['assets'], 'version_state': participant['version_state'],
                'acceptance': (session.get(m.CharacterVersion, participant['character_version_id']).identity_json.get('studio_acceptance') if participant['character_version_id'] else None)}
        video_node = _leaf(nodes,shot.id)
        decision = session.scalar(select(StudioDecision).where(StudioDecision.project_id == project_id, StudioDecision.shot_id == shot.id))
        video = session.get(m.VideoRevision, decision.video_id) if decision and decision.video_id else session.get(m.VideoRevision, video_node['video_id']) if video_node and video_node.get('video_id') else None
        if not video and not video_node: video = session.scalar(select(m.VideoRevision).where(m.VideoRevision.shot_id == shot.id).order_by(m.VideoRevision.revision.desc()).limit(1))
        shots.append({'id': shot.id, 'order': display_order, 'title': shot.scene_title,
          'scene_id': scene['id'] if scene else None, 'scene_name': scene['name'] if scene else shot.location,
          'cast': bundle['participants'], 'duration_seconds': shot.duration_seconds,
          'status': video_node['status'] if video_node else 'generated' if video else 'pending',
          'video': {**row(video), 'media_url': '/api/media/'+video.relative_path} if video else None,
          'decision': row(decision) if decision else {'action': 'pending', 'position': display_order},
          'action': shot.action, 'dialogues': bundle['dialogues']})
    all_visuals = list(session.scalars(select(m.ProductionVisual).where(m.ProductionVisual.project_id == project_id)))
    for scene in scenes.values(): scene['visuals'] = [{**row(v), 'media_url': '/api/media/'+v.relative_path if v.relative_path else None} for v in all_visuals if v.scene_id == scene['id']]
    exceptions = [{'node_id': n['id'], 'kind': n['kind'], 'target_id': n['target_id'], 'code': n.get('code'), 'message': n.get('error'), 'status': n['status']} for n in nodes if n['status'] in {'failed', 'exception', 'unknown', 'retrying'}]
    renders = list(session.scalars(select(m.AssemblyRender).join(m.Assembly).where(m.Assembly.project_id == project_id).order_by(m.AssemblyRender.created_at.desc())))
    return {'project': row(project), 'script': row(script) if script else None, 'characters': list(characters.values()),
            'scenes': list(scenes.values()), 'shots': shots, 'run': {**row(run), 'steps_json': nodes} if run else None,
            'exceptions': exceptions, 'renders': [{**row(r), 'media_url': '/api/media/'+r.relative_path if r.relative_path else None} for r in renders]}


def project_list(session):
    result = []
    for project in session.scalars(select(m.Project).where(m.Project.status != 'deleted',m.Project.source_type!='model_lab').order_by(m.Project.updated_at.desc())):
        shots = _shots(session, project.id); run = latest_run(session, project.id)
        video_count = session.scalar(select(func.count(func.distinct(m.VideoRevision.shot_id))).where(m.VideoRevision.shot_id.in_([s.id for s in shots]))) if shots else 0
        result.append({'id': project.id, 'name': project.name, 'status': run.status if run else project.status,
                       'shot_count': len(shots), 'video_count': video_count, 'run_id': run.id if run else None,
                       'error_count': sum(n['status'] in {'failed', 'exception', 'unknown'} for n in run.nodes_json) if run else 0,
                       'script_revision_id': run.script_revision_id if run else None})
    return result
