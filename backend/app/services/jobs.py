from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath

import httpx
from sqlalchemy import func, select

from app.core.config import PROJECT_ROOT, Settings
from app.core.errors import AppError
from app.core.paths import media_relative, safe_media_path
from app.models import AIService, AuditEvent, GenerationJob, QualityCheck, VideoRevision, ManagedFile
from app.services.providers import ComfyUIH3Provider
from app.services.quality import inspect_media
from app.services.composition import process_composition

logger = logging.getLogger(__name__)
_wan_poll_after: dict[str, float] = {}

_SOURCE_GATE_ERROR = re.compile(
    r"Compiler source gate BLOCKED (?:'[a-z_]+'|None); "
    r"requires actual source=written without fallback; evidence: ([^\r\n]+)"
)
_REFERENCE_GATE_ERROR = 'Declared reference projection BLOCKED before H3 loading: '
_REFERENCE_GATE_MESSAGE = ('本镜 Context-IR 参考素材绑定未通过，视频尚未开始生成。'
                           '请核对已选商品或人物素材，在镜头动作中明确引用对应的主体标签，再重试此镜。')
_COMPILER_SCOPE = re.compile(r'([0-9]{8}-[0-9]{6}-[0-9]{6}Z)-[0-9a-f]{8}')
_VALIDATION_RULE = r'[A-Z][0-9]{1,2}[a-z]?-[a-z0-9]+(?:-[a-z0-9]+)*'
_VALIDATION_REASON = re.compile(
    rf'the written brief still failed after [0-9]{{1,2}} correction pass\(es\): '
    rf'({_VALIDATION_RULE}(?:(?:; |, ){_VALIDATION_RULE}){{0,19}})'
)


def _h3_source_gate_message(result, job_id, prompt_id, graph_sha, profile, *, read_local_receipt):
    """Explain a bound native error without displaying provider paths or arbitrary prose."""
    from app.services.h3_context_provider import WINDOWS_SCOPE_ROOT, graph_digest

    generic = '本镜 Context-IR 编译未通过，视频尚未开始生成。请检查本镜的运镜说明与参考素材，再重试此镜。'
    summary = result.get('error')
    fallback = None
    if isinstance(summary, str) and summary.startswith('Node 4 (VideoFactoryLocalH3IRCompile): '):
        if _SOURCE_GATE_ERROR.search(summary):
            fallback = generic
        elif _REFERENCE_GATE_ERROR in summary:
            fallback = _REFERENCE_GATE_MESSAGE
    if result.get('provider_job_id') != prompt_id or not graph_sha:
        return fallback
    try:
        history = result['raw']
        _, recorded_id, graph, extra, *_ = history['prompt']
        if (recorded_id != prompt_id or extra.get('client_id') != job_id or graph_digest(graph) != graph_sha
                or graph['4']['class_type'] != 'VideoFactoryLocalH3IRCompile'):
            return fallback
        events = {event: data for event, data in history['status']['messages'] if data.get('prompt_id') == prompt_id}
        failure = events['execution_error']
        if failure.get('node_id') != '4' or failure.get('node_type') != 'VideoFactoryLocalH3IRCompile':
            return fallback
        native_error = failure['exception_message'].strip()
        if native_error.startswith(_REFERENCE_GATE_ERROR):
            return _REFERENCE_GATE_MESSAGE
        error = _SOURCE_GATE_ERROR.fullmatch(native_error)
    except (AttributeError, KeyError, TypeError, ValueError):
        return fallback
    if not error:
        return fallback

    if not read_local_receipt:
        return generic
    try:
        evidence = PureWindowsPath(error[1])
        scope = _COMPILER_SCOPE.fullmatch(evidence.parent.name)
        if not scope or evidence.name != 'compiler-001' or evidence.parent.parent != PureWindowsPath(WINDOWS_SCOPE_ROOT):
            return generic
        scope_ms = datetime.strptime(scope[1], '%Y%m%d-%H%M%S-%fZ').replace(tzinfo=timezone.utc).timestamp() * 1000
        if not events['execution_start']['timestamp'] <= scope_ms <= failure['timestamp']:
            return generic
        receipt_path = WINDOWS_SCOPE_ROOT / evidence.parent.name / 'compiler-001' / 'source-gate.json'
        if receipt_path.resolve(strict=True) != receipt_path:
            return generic
        with receipt_path.open('rb') as file:
            receipt = json.loads(file.read(8192))
        if (receipt.get('profile') != profile or receipt.get('source') != 'draft'
                or receipt.get('required_source') != 'written' or receipt.get('status') != 'BLOCKED'):
            return generic
        reason = _VALIDATION_REASON.fullmatch(receipt.get('fallback_reason') or '')
    except (AttributeError, KeyError, OSError, TypeError, ValueError):
        return generic
    if not reason:
        return generic
    rules = re.split(r'[;,] ', reason[1])
    if 'P5-camera-no-motion-type' in rules:
        return ('本镜 Context-IR 编译缺少可识别的运镜类型，视频尚未开始生成。'
                '请在本镜说明中明确运镜，例如轻推（Push In）、手持微晃（Shake Slightly）或固定机位（Static Shot），再重试此镜。'
                f'校验项为 {reason[1]}。')
    if 'P1-no-style-opening' in rules:
        return ('本地模型未按 Context-IR 要求把风格说明写在编号镜头之前，视频尚未开始生成。'
                '镜头的原始输入和参考图已保留，可定向重试此镜。'
                f'校验项为 {reason[1]}。')
    return f'{generic}校验项为 {reason[1]}。'


def video_outputs(history):
    found = []
    for output in history.get("outputs", {}).values():
        for values in output.values():
            if isinstance(values, list):
                found.extend(item for item in values if isinstance(item, dict) and str(item.get("filename", "")).lower().endswith(".mp4") and item.get("type") == "output")
    return found


async def poll_job(settings, session_factory, job_id):
    with session_factory() as session:
        job = session.get(GenerationJob, job_id)
        if (job.parameters_json.get('revision_postprocess') or {}).get('raw_video_id'):
            return  # local postprocessing owns this preserved raw; never redownload/reimport it
        provider_id = job.provider_job_id
        snapshot = job.parameters_json.get('service_snapshot')
        workflow = job.parameters_json.get('h3_workflow')
        submitted_graph_sha = job.parameters_json.get('actual_workflow_sha256')
        from app.services.video_services import existing_provider
        provider = existing_provider(session,settings,snapshot) if snapshot else ComfyUIH3Provider(settings)
    result = await provider.get_job(provider_id)
    logdir = PROJECT_ROOT / "logs/jobs" / job_id
    logdir.mkdir(parents=True, exist_ok=True)
    # Provider output URLs may contain bearer-like signed query parameters.
    # Keep them only in memory for immediate download, never persist in logs.
    safe_result={key:value for key,value in result.items() if key != 'output_url'}
    (logdir / "latest_status.json").write_text(json.dumps(safe_result, ensure_ascii=False, indent=2), encoding="utf-8")
    missing_file=logdir/'missing_status.json'
    if result['status']=='reconciling':
        since=json.loads(missing_file.read_text())['since'] if missing_file.exists() else time.time()
        missing_file.write_text(json.dumps({'since':since,'checked_at':time.time()}))
        if time.time()-since<30:
            return
        result.update(status='failed',error='连续 30 秒未在 ComfyUI 队列或历史中找到任务；原任务 ID 保留，可核对服务端记录')
    elif missing_file.exists():
        missing_file.unlink()
    data = context_evidence = None
    if result["status"] == "generated":
        if snapshot and snapshot['provider'] == 'video_api':
            data = await provider.download(result.get('output_url'))
        else:
            if workflow:
                from app.services.h3_context_provider import context_history
                context_evidence = context_history(result['raw'], workflow, submitted_graph_sha)
                profile = snapshot.get('settings') or {}
                if settings.container_mode or profile.get('h3_context_ir') is True or profile.get('workflow_profile') == 'context_ir':
                    from app.services.h3_context_provider import preserve_context_receipts
                    context_evidence['native_receipts'] = await preserve_context_receipts(snapshot['base_url'], context_evidence, logdir)
                (logdir / 'context-ir-result.json').write_text(json.dumps(context_evidence, ensure_ascii=False, indent=2), encoding='utf-8')
            outputs = video_outputs(result["raw"])
            if len(outputs) != 1:
                raise AppError(502, "comfyui_mp4_missing", "ComfyUI 成功记录没有唯一真实 MP4", {"outputs": outputs})
            base_url=snapshot['base_url'] if snapshot else settings.comfyui_base_url
            from app.core.trusted_service_origins import local_service_client
            async with local_service_client([base_url], timeout=None) as client:
                response = await client.get(f"{base_url}/view", params=outputs[0])
                response.raise_for_status()
                data = response.content
        if not data or (len(data) < 128 and not (snapshot and snapshot.get('provider')=='video_api')):
            raise AppError(502, "empty_video", "ComfyUI 返回的视频为空或不完整")
    with session_factory() as session:
        job = session.get(GenerationJob, job_id)
        if job.status == "cancelled":
            return
        previous = job.status
        job.status = result["status"]
        job.log_path = str(logdir.relative_to(PROJECT_ROOT)).replace("\\", "/")
        if data is not None:
            job.status='running'  # local decode/mixing is still unfinished
            output_dir = safe_media_path(settings.media_root, job.parameters_json["output_relative_dir"])
            output_dir.mkdir(parents=True, exist_ok=True)
            dest = output_dir / f"{job.id}.mp4"
            raw_data = data
            raw_dest = dest
            audio_events = job.parameters_json.get('screenplay_snapshot',{}).get('resolved_audio_events',[])
            is_cloud_video=bool(snapshot and snapshot.get('provider')=='video_api')
            raw_digest=hashlib.sha256(raw_data).digest()
            if is_cloud_video and audio_events:
                raw_dest = output_dir / f'{job.id}-provider-raw.mp4'
                if raw_dest.exists() and hashlib.sha256(raw_dest.read_bytes()).digest() != raw_digest:
                    raise AppError(409, 'output_already_exists', '已有云端原始文件不同，拒绝覆盖')
            if dest.exists() and hashlib.sha256(dest.read_bytes()).digest() != raw_digest:
                # A previous mixing step may have finished before its DB commit.
                # Only recover if the separately saved original matches the
                # provider download; never treat the existing file as trusted or
                # overwrite it. Rebuild into a fresh, exclusively-created path.
                if raw_dest == dest or not raw_dest.exists():
                    raise AppError(409, 'output_already_exists', '已有文件不同；拒绝覆盖历史视频')
                while True:
                    candidate = output_dir / f'{job.id}-recovered-{uuid.uuid4().hex}.mp4'
                    try:
                        with candidate.open('xb') as output:
                            output.write(raw_data)
                        dest = candidate
                        break
                    except FileExistsError:
                        continue
            elif not dest.exists():
                with dest.open('xb') as output:
                    output.write(raw_data)
            if is_cloud_video and audio_events:
                if not raw_dest.exists():
                    with raw_dest.open('xb') as output:
                        output.write(raw_data)
                raw_relative=media_relative(settings.media_root,raw_dest)
                raw_registry=session.scalar(select(ManagedFile).where(ManagedFile.relative_path==raw_relative))
                if not raw_registry:
                    session.add(ManagedFile(relative_path=raw_relative,state='active',ownership_json=[{'table':'generation_jobs','id':job.id}],metadata_json={'verified_owned':True,'kind':'provider_original','sha256':hashlib.sha256(raw_data).hexdigest()}))
            registry=session.scalar(select(ManagedFile).where(ManagedFile.relative_path==media_relative(settings.media_root,dest)))
            if not registry:
                registry=ManagedFile(relative_path=media_relative(settings.media_root,dest),state='active',ownership_json=[{'table':'generation_jobs','id':job.id}],metadata_json={'verified_owned':True,'kind':'generated_video'})
                session.add(registry);session.commit()
            from app.services.audio_mix import mix_audio_events
            # Decode cloud output before any mixing: invalid paid output must
            # remain visible even if FFmpeg could not process audio events.
            raw_checks=await asyncio.to_thread(inspect_media,raw_dest,settings) if is_cloud_video else None
            raw_decoded=bool(raw_checks and any(c['check_type']=='media_decode' and c['status']=='passed' for c in raw_checks))
            if is_cloud_video and not raw_decoded:
                mixing=None;checks=raw_checks
            else:
                mixing=await asyncio.to_thread(mix_audio_events,dest,audio_events,settings)
                if mixing:
                    data=dest.read_bytes()
                    (logdir/'audio_mix.json').write_text(json.dumps(mixing,ensure_ascii=False,indent=2),encoding='utf-8')
                checks = await asyncio.to_thread(inspect_media, dest, settings)
            from app.services.cinematic_quality import inspect_generated
            checks.extend(inspect_generated(job,checks))
            (logdir / "media_validation.json").write_text(json.dumps(checks, ensure_ascii=False, indent=2), encoding="utf-8")
            if any(x["status"] == "failed" for x in checks) or not any(x["check_type"] == "media_decode" and x["status"] == "passed" for x in checks):
                if not is_cloud_video:
                    raise AppError(502, "output_decode_failed", "真实 MP4 解码验证未通过", {"checks": checks})
                # Preserve the paid provider output as an explicitly unusable
                # revision. It remains inspectable/downloadable, but the normal
                # approval path rejects needs_regeneration before composition.
                revision = (session.scalar(select(func.max(VideoRevision.revision)).where(VideoRevision.shot_id == job.shot_id)) or 0) + 1
                decoded = next((c for c in checks if c['check_type'] == 'media_decode' and c['status'] == 'passed'), None)
                video = VideoRevision(project_id=job.project_id, shot_id=job.shot_id, generation_job_id=job.id,
                    relative_path=media_relative(settings.media_root,raw_dest), revision=revision,
                    instruction=job.parameters_json.get('instruction'), review_status='needs_regeneration',
                    review_reason='云端原始视频已保存；技术检测不合格，不能批准或合成',
                    metadata_json={'provider':job.provider,'mode':'cloud','provider_job_id':provider_id,
                        'service_snapshot':snapshot,'sha256':hashlib.sha256(raw_data).hexdigest(),
                        'file_size':len(raw_data),'playable_video':raw_decoded,'quality_status':'failed'})
                session.add(video);session.flush()
                if raw_dest == dest:
                    registry.ownership_json=[{'table':'video_revisions','id':video.id}]
                    registry.metadata_json={**registry.metadata_json,'sha256':hashlib.sha256(raw_data).hexdigest(),'bytes':len(raw_data),'quality_status':'failed'}
                else:
                    raw_registry=session.scalar(select(ManagedFile).where(ManagedFile.relative_path==media_relative(settings.media_root,raw_dest)))
                    raw_registry.ownership_json=[{'table':'video_revisions','id':video.id}]
                for check in checks:
                    session.add(QualityCheck(video_revision_id=video.id,check_type=check['check_type'],status=check['status'],details_json=check.get('details',{})))
                job.status='failed';job.error_code='output_quality_failed' if decoded else 'output_decode_failed'
                job.error_message='云端原始视频已保存，但技术检测不合格；请查看视频版本和检测详情'
                job.finished_at=datetime.now(timezone.utc)
                session.add(AuditEvent(object_type='generation_job',object_id=job.id,action='quality_failed',details_json={'provider_job_id':provider_id,'video_revision_id':video.id}))
                _wan_poll_after.pop(job.id,None);session.commit();return
            revision = (session.scalar(select(func.max(VideoRevision.revision)).where(VideoRevision.shot_id == job.shot_id)) or 0) + 1
            probe = next(c["details"] for c in checks if c["check_type"] == "media_decode")
            stream = next(s for s in probe["streams"] if s["codec_type"] == "video")
            provenance={'provider_job_id':provider_id}
            if job.provider=='comfyui_h3':provenance['comfyui_prompt_id']=provider_id
            video = VideoRevision(project_id=job.project_id, shot_id=job.shot_id, generation_job_id=job.id, relative_path=media_relative(settings.media_root, dest), metadata_json={**provenance, "sha256": hashlib.sha256(data).hexdigest(), "workflow_version": job.workflow_version, "provider": job.provider, "mode": "cloud" if snapshot and snapshot.get('is_cloud',False) else "local", "character_version_id": job.parameters_json.get("character_version_id"), "duration_seconds": float(probe["format"]["duration"]), "width": stream["width"], "height": stream["height"], "frame_rate": stream.get("avg_frame_rate"), "has_audio": any(s["codec_type"] == "audio" for s in probe["streams"]), "file_size": len(data), "playable_video": True}, revision=revision, instruction=job.parameters_json.get("instruction"), review_status="pending")
            session.add(video)
            warning_checks=[c for c in checks if c['status']=='warning']
            if warning_checks:
                video.review_reason='云端视频尺寸或时长与请求存在轻微偏差；请核对画面、声音和实际规格后人工审核'
                video.metadata_json={**video.metadata_json,'quality_status':'warning','quality_warnings':[c['check_type'] for c in warning_checks]}
            video.metadata_json={**video.metadata_json,'provider':job.provider,'mode':'cloud' if snapshot and snapshot.get('is_cloud',False) else 'local','service_snapshot':snapshot,
                                 'provider_raw_relative_path':media_relative(settings.media_root,raw_dest) if raw_dest != dest else None,
                                 'reference_manifest':job.parameters_json.get('reference_manifest'),
                                 'actual_reference_uploads':job.parameters_json.get('actual_reference_uploads',[]),
                                 'actual_workflow_sha256':job.parameters_json.get('actual_workflow_sha256')}
            if context_evidence:
                receipt = {key: value for key, value in context_evidence.items() if key not in {'compiled_prompt', 'media_report'}}
                job.parameters_json = {**job.parameters_json, 'context_ir_execution': receipt}
                video.metadata_json = {**video.metadata_json, 'h3_workflow': workflow, 'context_ir_execution': receipt}
            if snapshot and not context_evidence:
                service=session.get(AIService,snapshot['service_id'])
                if service and service.revision==snapshot['service_revision']:
                    capability='first_frame_to_video' if snapshot.get('settings',{}).get('video_contract')=='wan26-i2v' else 'reference_to_video'
                    service.capabilities_json={**service.capabilities_json,capability:{'state':'verified','source':job.id,'model_id':snapshot['model_id']}}
            session.flush()
            registry.ownership_json=[{'table':'video_revisions','id':video.id}]
            registry.metadata_json={**registry.metadata_json,'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data),'audio_mix':mixing}
            for check in checks:
                session.add(QualityCheck(video_revision_id=video.id, check_type=check["check_type"], status=check["status"], details_json=check.get("details", {})))
            from app.services.revision_postprocess import attach_raw
            if not attach_raw(job, video):
                job.status='generated'
                job.progress = 1.0
                job.finished_at = datetime.now(timezone.utc)
        elif job.status == "failed":
            job.error_code = "comfyui_execution_failed" if job.provider=='comfyui_h3' else 'cloud_video_execution_failed'
            job.error_message = result.get("error") or ('ComfyUI 任务失败' if job.provider=='comfyui_h3' else '云端视频任务失败')
            if workflow and job.provider == 'comfyui_h3':
                message = _h3_source_gate_message(
                    result, job.id, provider_id, submitted_graph_sha, workflow.get('context_ir_version'),
                    read_local_receipt=bool(snapshot and snapshot.get('base_url') == 'http://127.0.0.1:8188'),
                )
                if message:
                    job.error_code = 'h3_context_ir_validation_failed'
                    job.error_message = message
            if workflow and any(message[0] == 'execution_interrupted' for message in result.get('raw', {}).get('status', {}).get('messages', []) if message):
                try:
                    guard = json.loads((logdir / 'memory-interrupt-intent.json').read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    guard = {}
                if guard.get('prompt_id') == provider_id and guard.get('status') == 'ALARM_LOW_COMMIT':
                    job.error_code = 'h3_windows_commit_low'
                    job.error_message = '本机 Windows 提交内存余量低于 2 GiB，已保护性中止 H3。请释放内存或检查分页文件容量后重试。'
            job.finished_at = datetime.now(timezone.utc)
        elif job.status == 'unknown':
            job.error_code='provider_status_unknown'
            job.error_message=result.get('error') or '供应商任务状态未知；保留原任务 ID，不自动重复提交'
        if previous != job.status:
            session.add(AuditEvent(object_type="generation_job", object_id=job.id, action=job.status, details_json={"provider_job_id": provider_id, "source": "ComfyUI queue/history" if job.provider=='comfyui_h3' else 'cloud provider task status'}))
        if job.status in {'generated','failed','cancelled','unknown'}:_wan_poll_after.pop(job.id,None)
        session.commit()


def recheck_cloud_duration_warning(session, settings, video):
    """Reinspect one preserved paid original; never submit or alter media.

    Only the small, non-short cloud duration discrepancy can be recovered.
    Historical checks remain immutable and a new audited check is appended.
    """
    from app.models import StoryboardShot
    from app.services.cinematic_quality import generation_inputs_hash, inspect_generated
    job=session.get(GenerationJob,video.generation_job_id)
    if (not job or job.project_id!=video.project_id or job.shot_id!=video.shot_id
        or job.status!='failed' or job.error_code!='output_quality_failed'
        or video.review_status!='needs_regeneration' or video.metadata_json.get('quality_status')!='failed'
        or job.parameters_json.get('service_snapshot',{}).get('provider')!='video_api'):
        raise AppError(409,'duration_recheck_unavailable','仅可重新检测原始云端视频规格偏差，未重发任务')
    shot=session.get(StoryboardShot,video.shot_id)
    cinematic=job.parameters_json.get('screenplay_snapshot',{}).get('cinematic',{})
    expected_hash=cinematic.get('preflight',{}).get('generation_inputs_hash')
    if not shot or not expected_hash or generation_inputs_hash(session,shot)!=expected_hash:
        raise AppError(409,'video_inputs_stale','镜头、人物或参考素材已变化，不能恢复旧视频')
    audio_events=job.parameters_json.get('screenplay_snapshot',{}).get('resolved_audio_events',[])
    if any(e.get('relative_path') for e in audio_events):
        raise AppError(409,'duration_recheck_audio_unverified','原片有额外音轨需求，不能跳过音频处理恢复')
    path=safe_media_path(settings.media_root,video.relative_path,must_exist=True)
    digest=video.metadata_json.get('sha256')
    if not digest or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
        raise AppError(409,'video_file_changed','原始视频文件哈希不匹配，不能恢复')
    existing=list(session.scalars(select(QualityCheck).where(QualityCheck.video_revision_id==video.id).order_by(QualityCheck.created_at,QualityCheck.id)))
    latest={check.check_type:check for check in existing}
    prior_spec=latest.get('cinematic_output_spec')
    if not prior_spec or prior_spec.status!='failed' or any(c.status=='failed' for k,c in latest.items() if k not in {'file_readable','media_decode','cinematic_output_spec'}):
        raise AppError(409,'duration_recheck_other_failure','存在其他技术失败，不能按规格偏差恢复')
    checks=inspect_media(path,settings)
    decode=next((c for c in checks if c['check_type']=='media_decode'),None)
    if not decode or decode['status']!='passed' or decode.get('details',{}).get('full_decode',{}).get('exit_code')!=0:
        raise AppError(409,'duration_recheck_decode','原片必须完整解码通过，不能恢复')
    checks.extend(inspect_generated(job,checks))
    spec=next((c for c in checks if c['check_type']=='cinematic_output_spec'),None)
    if not spec or spec['status']!='warning' or any(c['status']=='failed' for c in checks):
        raise AppError(409,'duration_recheck_not_eligible','原片不属于可人工复核的轻微规格偏差，不能恢复')
    for c in checks:
        session.add(QualityCheck(video_revision_id=video.id,check_type=c['check_type'],status=c['status'],details_json=c.get('details',{})))
    video.review_status='pending'
    video.review_reason='云端原片保留全部内容；尺寸或时长与请求有轻微偏差，请人工审核画面和声音'
    video.metadata_json={**video.metadata_json,'quality_status':'warning','quality_warnings':['cinematic_output_spec'],
        'playable_video':True,'duration_seconds':spec['details']['actual']['duration'],
        'width':spec['details']['actual']['width'],'height':spec['details']['actual']['height']}
    job.status='generated';job.progress=1.;job.error_code=None;job.error_message=None
    session.add(AuditEvent(object_type='generation_job',object_id=job.id,action='duration_rechecked',details_json={
        'video_revision_id':video.id,'old_quality_check_id':prior_spec.id,'actual':spec['details']['actual'],
        'expected':spec['details']['expected'],'sha256':digest,'provider_resubmitted':False,'human_review_required':True}))
    session.commit()
    return video


async def worker(settings: Settings, session_factory):
    while True:
        try:
            await process_video_tick(settings,session_factory)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning('Video queue tick: %s',type(exc).__name__)
        await asyncio.sleep(2)


async def process_video_tick(settings: Settings, session_factory):
    with session_factory() as session:
        records = list(session.execute(select(GenerationJob.id,GenerationJob.provider).where(GenerationJob.provider.in_(["comfyui_h3","cloud_video_jobs_v1","cloud_wan26_i2v","cloud_lk888_h3_v2"]), GenerationJob.status.in_(["queued", "running"]), GenerationJob.provider_job_id.is_not(None)).order_by(GenerationJob.created_at)))
    for job_id, provider_name in records:
        if provider_name in {'cloud_wan26_i2v','cloud_lk888_h3_v2'}:
            now=time.monotonic()
            if now < _wan_poll_after.get(job_id,0):continue
            _wan_poll_after[job_id]=now+(15 if provider_name=='cloud_wan26_i2v' else 10)
        try:
            await poll_job(settings, session_factory, job_id)
        except asyncio.CancelledError:
            raise
        except (AppError, httpx.HTTPError, OSError, ValueError) as error:
            logger.warning("Job polling failure: %s (%s)", job_id,type(error).__name__)
            with session_factory() as session:
                job = session.get(GenerationJob, job_id)
                if not job:continue
                # Once a provider ID exists, any status/download/local error is
                # not a provider-declared failure. Retain the ID and block a
                # second billable POST until the original task is reconciled.
                cannot_check = job.provider in {'cloud_video_jobs_v1','cloud_wan26_i2v','cloud_lk888_h3_v2'} and bool(job.provider_job_id)
                if cannot_check:
                    job.status='unknown';job.error_code='provider_status_unknown'
                    job.error_message='无法确认或取回已提交的云端任务；请恢复服务配置并核对供应商原任务，未重复提交'
                    session.commit()
                elif isinstance(error, AppError) and error.code not in {'comfyui_status_failed'}:
                    job.status='failed';job.error_code=error.code;job.error_message=error.message
                    job.finished_at=datetime.now(timezone.utc);session.commit()
    from app.services.revision_postprocess import process_tick as revision_processing_tick
    await revision_processing_tick(settings, session_factory)
    await process_composition(settings, session_factory)
