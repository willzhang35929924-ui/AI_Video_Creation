"""Recover a known accepted native output; never create another inference."""
import json
from pathlib import Path
from app import models as m
from app.core.config import PROJECT_ROOT
from app.core.errors import AppError
from app.core.paths import safe_media_path


def resume_native(session,settings,identifier):
    from app.services.video_quality import _file_sha,seedvr_status
    item=session.get(m.QualityRender,identifier)
    if not item:raise AppError(404,'quality_render_missing','处理记录不存在')
    snapshot=item.snapshot_json or {};native=snapshot.get('native_execution') or {}
    if snapshot.get('settings',{}).get('processing')!='seedvr2':
        raise AppError(409,'seedvr_resume_scope','只能取回原生 SeedVR2 已提交任务')
    if item.status=='generated':return item
    if item.status in {'queued','running'} and native.get('resume_existing_submission'):return item
    if item.status not in {'failed','unknown'} or native.get('inference_submitted') is not True or not native.get('prompt_id'):
        raise AppError(409,'seedvr_resume_unknown','缺少明确的已接收任务编号，未重复提交模型')
    if (native.get('terminal_status') or {}).get('status_str') in {'error','failed','cancelled'}:
        raise AppError(409,'seedvr_native_failed','原服务任务已明确失败，没有可取回的成功输出')
    frozen=snapshot.get('seedvr2') or {};current=seedvr_status(settings)
    if current.get('state')!='verified' or frozen.get('manifest_sha256')!=current.get('manifest_sha256') or frozen.get('helper_sha256')!=_file_sha(PROJECT_ROOT/'scripts/deploy_seedvr2.py'):
        raise AppError(409,'seedvr_resume_configuration_changed','模型或执行程序与原提交快照不一致，保留原任务')
    if snapshot.get('route'):
        from app.services.task_routing import assert_frozen
        assert_frozen(session,snapshot['route'])
    source=session.get(m.VideoRevision,item.source_video_id)
    if not source or (source.project_id,source.shot_id)!=(snapshot.get('project_id'),snapshot.get('shot_id')):
        raise AppError(409,'seedvr_resume_source','原视频与任务归属不匹配')
    source_path=safe_media_path(settings.media_root,snapshot['source_relative_path'],must_exist=True)
    if _file_sha(source_path)!=snapshot.get('source_sha256'):
        raise AppError(409,'seedvr_resume_source_changed','原视频 SHA 已变化，未继续处理')
    destination=safe_media_path(settings.media_root,f"quality/{source.project_id}/{item.id}.mp4")
    paths={name:destination.with_suffix('.seed-'+name+'.json') for name in ('prepared','submission','result')}
    try:
        if any(Path(native['paths'][name]).resolve()!=path.resolve() for name,path in paths.items()):raise ValueError('path')
        if destination.exists() or paths['result'].exists():raise ValueError('existing delivery requires separate validation')
        prepared=json.loads(paths['prepared'].read_text('utf-8'));submission=json.loads(paths['submission'].read_text('utf-8'))
        digest=_file_sha(paths['prepared'])
        if (digest!=native.get('prepared_sha256') or digest!=submission.get('prepared_sha256')
                or Path(submission['prepared']).resolve()!=paths['prepared'].resolve()
                or submission.get('status')!='SUBMITTED' or submission.get('prompt_id')!=native['prompt_id']
                or submission.get('base_url','').rstrip('/')!='http://127.0.0.1:8189'
                or prepared.get('original_source_sha256')!=snapshot['source_sha256']
                or prepared.get('graph_sha256')!=native.get('graph_sha256')
                or prepared.get('sample_only') is not False or not prepared.get('expected_frames')):
            raise ValueError('frozen submission')
    except (OSError,ValueError,KeyError,TypeError) as error:
        raise AppError(409,'seedvr_resume_evidence','已提交任务的原始凭据不完整或已有交付文件，保留原件且不新建推理') from error
    item.snapshot_json={**snapshot,'native_execution':{**native,'resume_existing_submission':True}}
    item.result_json={**item.result_json,'recovery_history':[*item.result_json.get('recovery_history',[]),{'status':item.status,'error':item.error_message,'action':'collect_same_native_prompt','prompt_id':native['prompt_id']}]}
    item.status='queued';item.error_message=None
    session.add(m.AuditEvent(object_type='quality_render',object_id=item.id,action='resume_same_native_output',details_json={'prompt_id':native['prompt_id'],'new_inference':False}))
    session.flush();return item
