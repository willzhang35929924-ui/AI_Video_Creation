"""Explicit, bounded video references for H3; source files are never modified."""
from __future__ import annotations

import hashlib
import json
import math
import subprocess
import tempfile
from pathlib import Path

from sqlalchemy import select

from app import models as m
from app.core.errors import AppError
from app.core.paths import safe_media_path
from app.schemas_cinematic import MotionReferenceInput
from app.services.reference_video_analysis import file_digest


def _grid_frames(frames):
    return 5 + ((frames - 5) // 17) * 17 if frames >= 5 else 0


def resolve_motion_references(session, settings, shot, values=None):
    """Freeze source identity and bounds; refuse stale or unauthorised media."""
    if values is None:
        detail=session.scalar(select(m.ShotDetail).where(m.ShotDetail.shot_id==shot.id))
        values=((detail.source_json or {}).get('production',{}) if detail else {}).get('motion_references',[])
    if not isinstance(values,list) or len(values)>3:
        raise AppError(422,'motion_reference_limit','动作参考最多选择3条视频')
    result=[]; seen=set()
    for raw in values:
        try: ref=MotionReferenceInput.model_validate(raw)
        except Exception as exc: raise AppError(422,'motion_reference_invalid','动作参考配置无效或未明确授权') from exc
        if ref.video_id in seen:raise AppError(422,'motion_reference_duplicate','动作参考视频不能重复选择')
        seen.add(ref.video_id)
        video=session.get(m.ReferenceVideo,ref.video_id)
        if not video or video.state!='active':raise AppError(409,'motion_reference_missing','动作参考不存在或已移出图库')
        if video.revision!=ref.expected_revision or video.sha256!=ref.source_sha256:
            raise AppError(409,'motion_reference_stale','动作参考版本或哈希已变化，请重新选择并确认')
        duration=(video.metadata_json or {}).get('duration_seconds')
        if not isinstance(duration,(int,float)) or not math.isfinite(duration) or ref.trim_end_seconds>duration+1e-6:
            raise AppError(422,'motion_reference_range','所选片段超出原视频时长')
        source=safe_media_path(settings.media_root,video.relative_path,must_exist=True)
        if not source.is_file():raise AppError(409,'motion_reference_changed','动作参考原视频缺失，未提交生成')
        wanted=math.floor((ref.trim_end_seconds-ref.trim_start_seconds)*24+1e-6)
        output=round(shot.duration_seconds*24);output=output+(5-output%17)%17
        frames=_grid_frames(min(wanted,output))
        if frames<48:raise AppError(422,'motion_reference_too_short','H3对齐后不足2秒；请扩大参考片段或镜头时长')
        result.append({**ref.model_dump(exclude_none=True),'relative_path':video.relative_path,'absolute_path':str(source),
                       'effective_frames':frames,'effective_duration_seconds':frames/24,
                       'source_duration_seconds':duration,'fps':24,'audio_included':False})
    return result


def verify_motion_sources(references):
    """Hash in a worker thread when called from asynchronous request handlers."""
    for ref in references:
        source=Path(ref['absolute_path'])
        if not source.is_file() or file_digest(source)!=ref['source_sha256']:
            raise AppError(409,'motion_reference_changed','动作参考原视频缺失或哈希变化，未提交生成')


def motion_live_state(session, values):
    """Cheap fingerprint data changes whenever selected source state changes."""
    result=[]
    for ref in values or []:
        identifier=ref.get('video_id') if isinstance(ref,dict) else None
        item=session.get(m.ReferenceVideo,identifier) if identifier else None
        result.append({'video_id':identifier,'revision':item.revision if item else None,
                       'sha256':item.sha256 if item else None,'state':item.state if item else 'missing'})
    return result


def active_motion_jobs(session, video_id):
    active={'submitting','queued','submitted','running','postprocessing','unknown','cancelling','reconciling'}
    return [job.id for job in session.scalars(select(m.GenerationJob).where(m.GenerationJob.status.in_(active)))
            if any(isinstance(ref,dict) and ref.get('video_id')==video_id for ref in (job.parameters_json or {}).get('motion_references',[]))]


def motion_bound_shots(session, video_id):
    return [detail.shot_id for detail in session.scalars(select(m.ShotDetail))
            if any(isinstance(ref,dict) and ref.get('video_id')==video_id for ref in
                   ((detail.source_json or {}).get('production') or {}).get('motion_references',[]))]


def prepare_motion_reference(settings, ref):
    """Make a temporary, mute 24fps segment and verify it before transfer."""
    source=Path(ref['absolute_path'])
    if not source.is_file() or file_digest(source)!=ref['source_sha256']:
        raise AppError(409,'motion_reference_changed','动作参考原视频已变化，未提交生成')
    root=Path(settings.media_root)/'tmp';root.mkdir(parents=True,exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix='motion-reference-',dir=root) as directory:
            target=Path(directory)/'reference.mp4'
            cmd=[settings.ffmpeg_path,'-nostdin','-v','error','-ss',str(ref['trim_start_seconds']),'-i',str(source),
                 '-an','-vf',"fps=24,scale='min(640,iw)':-2",'-frames:v',str(ref['effective_frames']),
                 '-c:v','libx264','-preset','veryfast','-pix_fmt','yuv420p','-movflags','+faststart',str(target)]
            run=subprocess.run(cmd,capture_output=True,timeout=600,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            if run.returncode:raise AppError(422,'motion_reference_transcode','动作参考转码失败，未提交生成')
            probe=subprocess.run([settings.ffprobe_path,'-v','error','-count_frames','-show_streams','-of','json',str(target)],capture_output=True,timeout=60,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            if probe.returncode:raise AppError(422,'motion_reference_probe','动作参考转码后无法核验，未提交生成')
            streams=json.loads(probe.stdout).get('streams',[])
            if len(streams)!=1 or streams[0].get('codec_type')!='video' or int(streams[0].get('nb_read_frames',0))!=ref['effective_frames'] or streams[0].get('avg_frame_rate')!='24/1':
                raise AppError(422,'motion_reference_output_invalid','动作参考帧数、帧率或音轨不符合要求，未提交生成')
            if file_digest(source)!=ref['source_sha256']:raise AppError(409,'motion_reference_changed','动作参考处理期间原视频变化，未提交生成')
            data=target.read_bytes()
            if not data or len(data)>64*1024**2:raise AppError(422,'motion_reference_output_size','动作参考转换文件为空或超过64MiB')
            return data,{'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data),'frames':ref['effective_frames'],
                         'fps':24,'audio_included':False,'width':streams[0].get('width'),'height':streams[0].get('height')}
    except (OSError,subprocess.TimeoutExpired,ValueError,KeyError,TypeError) as exc:
        raise AppError(422,'motion_reference_processing_failed','动作参考处理失败，未提交生成') from exc
