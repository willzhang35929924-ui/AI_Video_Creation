from __future__ import annotations

import asyncio
import hashlib
import json
import math
import queue
import subprocess
import threading
import time
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

from sqlalchemy import select, update

from app.core.config import PROJECT_ROOT
from app.core.errors import AppError
from app.core.paths import safe_media_path
from app.models import AIService, AssemblyRender, VideoRevision, GenerationJob, CharacterGenerationJob
from app.schemas_product import TimelineRequest
from app.services.ai_services import resolve_service
from app.services.quality import inspect_media

TRANSITIONS = {'cut': '直接切换', 'crossfade': '淡入淡出', 'fadeblack': '黑场过渡', 'fadewhite': '白场过渡', 'dissolve': '溶解', 'slide': '滑动', 'push': '推入', 'wipe': '擦除'}


def display_dimensions(video_stream):
    width, height = int(video_stream['width']), int(video_stream['height'])
    rotation = next((side['rotation'] for side in video_stream.get('side_data_list', []) if 'rotation' in side), 0)
    if abs(float(rotation)) % 180 == 90:
        return height, width
    return width, height


def probe(path, settings):
    result = subprocess.run([settings.ffprobe_path, '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)], capture_output=True, encoding='utf-8', errors='replace', timeout=30, shell=False)
    if result.returncode:
        raise AppError(422, 'input_decode_failed', '输入视频无法读取，请检查真实文件和 FFprobe 日志')
    payload = json.loads(result.stdout)
    video = next((stream for stream in payload.get('streams', []) if stream.get('codec_type') == 'video'), None)
    if not video:
        raise AppError(422, 'video_stream_missing', '输入文件没有视频流')
    duration = float(video.get('duration') or payload.get('format', {}).get('duration') or 0)
    if duration <= 0 or not math.isfinite(duration):
        raise AppError(422, 'invalid_media_duration', '输入视频时长无效')
    try:
        frames = int(video.get('nb_frames') or 0)
    except (TypeError, ValueError):
        frames = 0
    width, height = display_dimensions(video)
    return {'duration': duration, 'width': width, 'height': height, 'fps': float(Fraction(video.get('avg_frame_rate', '0/1'))), 'frames': frames, 'has_audio': any(stream.get('codec_type') == 'audio' for stream in payload['streams']), 'streams': payload['streams']}


def full_clip_timing(metadata, fps):
    """Retain stream offsets and all media when no edit was requested."""
    streams = metadata['streams']
    video = next(s for s in streams if s.get('codec_type') == 'video')
    audio = next((s for s in streams if s.get('codec_type') == 'audio'), None)
    def value(stream, key):
        try:
            n = float(stream[key])
        except (KeyError, TypeError, ValueError):
            raise AppError(422, 'full_clip_timing_unavailable', '无法确定原始音视频时序；不能保证无裁剪合成')
        if not math.isfinite(n) or (key == 'duration' and n <= 0):
            raise AppError(422, 'full_clip_timing_unavailable', '原始音视频时序无效；不能保证无裁剪合成')
        return n
    vstart, vduration = value(video, 'start_time'), value(video, 'duration')
    astart = value(audio, 'start_time') if audio else None
    aduration = value(audio, 'duration') if audio else None
    origin = min(vstart, astart) if audio else vstart
    video_offset = vstart - origin
    audio_offset = astart - origin if audio else None
    full_end = max(video_offset + vduration, (audio_offset + aduration) if audio else 0)
    if metadata['fps'] > fps + 1e-6:
        raise AppError(422, 'full_clip_fps_too_low', '输出帧率低于原视频，无法保留全部原始画面；请提高输出帧率')
    length = math.ceil(full_end * fps - 1e-6) / fps
    return {'duration': length, 'source_end': full_end, 'video_offset': video_offset,
            'video_duration': vduration, 'audio_offset': audio_offset, 'audio_duration': aduration,
            'video_head_padding': video_offset, 'video_tail_padding': max(0, length-video_offset-vduration),
            'audio_head_silence': audio_offset, 'audio_tail_silence': max(0,length-audio_offset-aduration) if audio else length,
            'preserves_full_streams': True, 'frame_rate_conversion': metadata['fps'] != fps}


def resolution_loses_detail(width, height, source):
    # Aspect-preserving fit is limited by the smaller of the two scale factors.
    return width < source['width'] or height < source['height']


def resolve_output_resolution(output, sources):
    dimensions = output.model_dump()
    mode = output.resolution_mode or ('custom' if {'width', 'height'} & output.model_fields_set else 'source')
    dimensions['resolution_mode'] = mode
    if mode == 'source':
        dimensions['allow_downscale'] = False
        for axis in ('width', 'height'):
            largest = max(source[axis] for source in sources)
            dimensions[axis] = max(128, (largest + 1) // 2 * 2)
        if max(dimensions['width'], dimensions['height']) > 3840:
            raise AppError(422, 'assembly_source_resolution_unsupported', '源视频尺寸超过 3840 像素的输出上限，请选择自定义尺寸并确认缩小')
    width, height = dimensions['width'], dimensions['height']
    if width % 2 or height % 2:
        raise AppError(422, 'invalid_output_dimensions', 'H.264/H.265 输出宽高必须为偶数')
    reduced = [source for source in sources if resolution_loses_detail(width, height, source)]
    if reduced and not dimensions['allow_downscale']:
        raise AppError(422, 'assembly_downscale_confirmation_required',
                       '输出尺寸会缩小已选视频，请选择「跟随源片」或明确确认允许降低分辨率',
                       {'width': width, 'height': height, 'affected_clips': len(reduced)})
    return dimensions


def recover_legacy_output(timeline):
    """Treat a previously saved, unconfirmed smaller canvas as a source default."""
    output = dict(timeline['output'])
    if output.get('resolution_mode') is None:
        for clip in timeline['clips']:
            source = clip.get('source') or {}
            stream = next((stream for stream in source.get('streams', []) if stream.get('codec_type') == 'video'), None)
            width, height = display_dimensions(stream) if stream else (source.get('width'), source.get('height'))
            if width and height and resolution_loses_detail(output['width'], output['height'], {'width': width, 'height': height}):
                output.update(resolution_mode='source', allow_downscale=False)
                break
    return output


def validate_timeline(session, settings, project_id, payload: TimelineRequest, *, allow_unreviewed_draft=False):
    service, snapshot = resolve_service(session, 'final_assembly', payload.service_id, project_id)
    if service.provider != 'ffmpeg':
        raise AppError(409, 'assembly_adapter_unavailable', '当前合成器只启用已实现的本地 FFmpeg 适配器')
    tools = replace(settings, ffmpeg_path=snapshot['settings'].get('ffmpeg_path') or settings.ffmpeg_path, ffprobe_path=snapshot['settings'].get('ffprobe_path') or settings.ffprobe_path)
    if len(payload.transitions) != len(payload.clips) - 1:
        raise AppError(422, 'transition_count_mismatch', '每两个相邻片段必须恰好有一个转场配置')
    sources = []
    for item in payload.clips:
        video = session.get(VideoRevision, item.video_id)
        if not video or video.project_id != project_id or (video.review_status != 'approved' and not (allow_unreviewed_draft and video.review_status == 'pending')):
            raise AppError(422, 'unapproved_input', '请选择当前项目内已批准的真实视频版本')
        path = safe_media_path(settings.media_root, video.relative_path, must_exist=True)
        source_hash=hashlib.sha256(path.read_bytes()).hexdigest()
        if video.metadata_json.get('sha256') and video.metadata_json['sha256']!=source_hash:
            raise AppError(409,'input_file_changed','已审核视频文件的哈希已改变，请恢复原文件或生成并审核新版本')
        metadata = probe(path, tools)
        sources.append((item, video, path, source_hash, metadata))
    output = resolve_output_resolution(payload.output, [metadata for _, _, _, _, metadata in sources])
    clips = []
    for item, video, path, source_hash, metadata in sources:
        decoded = subprocess.run([tools.ffmpeg_path, '-v', 'error', '-xerror', '-i', str(path), '-f', 'null', '-'], capture_output=True, timeout=120, shell=False)
        if decoded.returncode:
            raise AppError(422, 'input_decode_failed', '输入视频完整解码失败，不能提交合成')
        preserve_full = item.trim_start == 0 and item.trim_end is None
        timing = full_clip_timing(metadata, output['fps']) if preserve_full else None
        source_end=metadata['duration'] if item.trim_end is None else item.trim_end
        if not preserve_full and (source_end>metadata['duration']+.02 or item.trim_start>=source_end):raise AppError(422,'invalid_clip_trim','裁剪范围超出实际视频，或没有保留帧')
        trim_start=round(item.trim_start*output['fps'])/output['fps']
        trim_end=timing['duration'] if preserve_full else math.floor(source_end*output['fps']+.01)/output['fps']
        normalized_duration=trim_end-trim_start
        if normalized_duration<1/output['fps']:raise AppError(422,'empty_trim','裁剪后至少保留一帧')
        quality=video.metadata_json.get('quality',{})
        source_provenance={'source_video_id':video.metadata_json.get('source_video_id'),'quality_render_id':video.metadata_json.get('quality_render_id'),
            'method':quality.get('method','original_model_output'),'ai_super_resolution':bool(quality.get('ai_super_resolution')),
            'model_actual_resolution':quality.get('model_actual_resolution') or {'width':metadata['width'],'height':metadata['height']}}
        clips.append({'video_id': video.id, 'revision': video.revision, 'shot_id': video.shot_id, 'generation_job_id': video.generation_job_id, 'relative_path': video.relative_path, 'sha256': source_hash, 'duration': normalized_duration,'trim_start':trim_start,'trim_end':None if preserve_full else trim_end, 'effective_trim_end':trim_end, 'source': metadata, 'full_stream_timing':timing, 'source_provenance':source_provenance,'thumbnail_path': next(iter(video.thumbnail_paths_json), None)})
    transitions = []
    for index, item in enumerate(payload.transitions):
        edge = item.model_dump()
        edge['duration'] = 0 if item.type == 'cut' else round(item.duration * output['fps']) / output['fps']
        if item.type != 'cut' and edge['duration'] < 2 / output['fps']:
            raise AppError(422, 'transition_too_short', '转场至少需要两个输出帧')
        if edge['duration'] >= min(clips[index]['duration'], clips[index+1]['duration']) - 1 / output['fps']:
            raise AppError(422, 'transition_too_long', '转场需要重叠帧，其时长必须小于相邻片段的可用时长')
        transitions.append(edge)
    for index, clip in enumerate(clips):
        overlaps = (transitions[index-1]['duration'] if index else 0) + (transitions[index]['duration'] if index < len(transitions) else 0)
        if overlaps >= clip['duration'] - 1 / output['fps']:
            raise AppError(422, 'overlap_exhausts_clip', '片段两侧转场重叠后没有足够帧，请缩短转场')
    duration = sum(clip['duration'] for clip in clips) - sum(edge['duration'] for edge in transitions)
    cues=[];offset=0
    if output.get('subtitles'):
        from app.models import DialogueLine,ShotCharacter,Character
        for index,clip in enumerate(clips):
            lines=list(session.scalars(select(DialogueLine).where(DialogueLine.shot_id==clip['shot_id']).order_by(DialogueLine.order)))
            available=clip['duration']-(transitions[index]['duration'] if index<len(transitions) else 0)
            for n,line in enumerate(lines):
                performer=session.get(ShotCharacter,line.participant_id);person=session.get(Character,performer.character_id)
                cues.append({'start':offset+n*available/len(lines),'end':offset+(n+1)*available/len(lines),'text':person.display_name+'：'+line.text,'source_dialogue_id':line.id})
            offset+=available
        if not cues:raise AppError(422,'subtitle_dialogue_missing','所选镜头没有对白文本，无法创建字幕')
    return {'clips': clips, 'transitions': transitions, 'output': output,'subtitle_cues':cues,'subtitle_timing':'shot segments, not speech alignment' if cues else None, 'service_snapshot': snapshot, 'duration': round(duration, 8), 'expected_frames': round(duration * output['fps']), 'output_directory': f'assemblies/{project_id}/'}


def preview_snapshot(snapshot, index):
    if index is None or index >= len(snapshot['transitions']):
        raise AppError(422, 'transition_selection_required', '请选择一个相邻转场生成短预览')
    result = json.loads(json.dumps(snapshot))
    first_length=min(snapshot['clips'][index]['duration'],snapshot['transitions'][index]['duration']+1.)
    preview_start=sum(c['duration'] for c in snapshot['clips'][:index])-sum(t['duration'] for t in snapshot['transitions'][:index])+snapshot['clips'][index]['duration']-first_length
    edge = result['transitions'][index]
    result['clips'] = result['clips'][index:index+2]
    for offset, clip in enumerate(result['clips']):
        length = min(clip['duration'], edge['duration'] + 1.0)
        clip['trim_start'] = clip.get('trim_start',0)+(clip['duration'] - length if offset == 0 else 0)
        clip['duration'] = length
    result['transitions'] = [edge]
    result['output'].update(width=384, height=216, codec='libx264', resolution_mode='custom', allow_downscale=True)
    result['duration'] = sum(clip['duration'] for clip in result['clips']) - edge['duration']
    result['subtitle_cues']=[{**cue,'start':max(0,cue['start']-preview_start),'end':min(result['duration'],cue['end']-preview_start)} for cue in snapshot.get('subtitle_cues',[]) if cue['end']>preview_start and cue['start']<preview_start+result['duration']]
    if result['output'].get('subtitles') and not result['subtitle_cues']:result['output']['subtitles']=False
    result['expected_frames'] = round(result['duration'] * result['output']['fps'])
    result['preview_transition_index'] = index
    return result


def effect(edge):
    if edge['type'] == 'crossfade':
        return 'fade'
    if edge['type'] in ('slide', 'wipe'):
        return edge['type'] + edge['direction']
    if edge['type'] == 'push':
        # FFmpeg smooth* performs the spatial push with a smooth displacement.
        return 'smooth' + edge['direction']
    return edge['type']


def command_for(settings, snapshot, destination):
    output, clips = snapshot['output'], snapshot['clips']
    command = [snapshot['service_snapshot']['settings'].get('ffmpeg_path') or settings.ffmpeg_path, '-hide_banner', '-n', '-filter_complex_threads', '1']
    filters = []
    fps = output['fps']
    for index, clip in enumerate(clips):
        path = safe_media_path(settings.media_root, clip['relative_path'], must_exist=True)
        if hashlib.sha256(path.read_bytes()).hexdigest() != clip['sha256']:
            raise AppError(409, 'input_file_changed', '输入文件哈希已改变，历史渲染快照保持不变；请重新保存时间线')
        command += ['-i', str(path)]
        start, length = clip.get('trim_start', 0), clip['duration']
        timing = clip.get('full_stream_timing')
        if timing:
            head = timing['video_offset']
            full_length = timing['duration']
            # A transition preview is a segment of the already padded full
            # timeline, so retain offsets before selecting that segment.
            filters.append(f'[{index}:v:0]setpts=PTS-STARTPTS,tpad=start_mode=clone:start_duration={head:.8f}:stop_mode=clone:stop_duration={full_length:.8f},scale={output["width"]}:{output["height"]}:force_original_aspect_ratio=decrease:flags=lanczos,pad={output["width"]}:{output["height"]}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},trim=start={start:.8f}:duration={length:.8f},setpts=PTS-STARTPTS,format=yuv420p,settb=AVTB[v{index}]')
        else:
            filters.append(f'[{index}:v:0]trim=start={start:.8f}:duration={length:.8f},setpts=PTS-STARTPTS,scale={output["width"]}:{output["height"]}:force_original_aspect_ratio=decrease:flags=lanczos,pad={output["width"]}:{output["height"]}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p,settb=AVTB[v{index}]')
        if output['audio_policy'] != 'mute':
            if clip['source']['has_audio']:
                if timing:
                    delay = round(timing['audio_offset'] * 48000)
                    filters.append(f'[{index}:a:0]asetpts=PTS-STARTPTS,aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,adelay={delay}S:all=1,apad,atrim=start={start:.8f}:duration={length:.8f},asetpts=PTS-STARTPTS[a{index}]')
                else:
                    filters.append(f'[{index}:a:0]atrim=start={start:.8f}:duration={length:.8f},asetpts=PTS-STARTPTS,aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,apad,atrim=duration={length:.8f}[a{index}]')
            else:
                filters.append(f'anullsrc=r=48000:cl=stereo,atrim=duration={length:.8f},asetpts=PTS-STARTPTS[a{index}]')
    v, a, elapsed = 'v0', 'a0', clips[0]['duration']
    for index, edge in enumerate(snapshot['transitions']):
        next_v, next_a, overlap = f'vm{index}', f'am{index}', edge['duration']
        if overlap == 0:
            filters.append(f'[{v}][v{index+1}]concat=n=2:v=1:a=0,settb=AVTB[{next_v}]')
        else:
            filters.append(f'[{v}][v{index+1}]xfade=transition={effect(edge)}:duration={overlap:.8f}:offset={elapsed-overlap:.8f}[{next_v}]')
        if output['audio_policy'] != 'mute':
            if overlap and edge['audio'] == 'crossfade':
                filters.append(f'[{a}][a{index+1}]acrossfade=d={overlap:.8f}:c1=tri:c2=tri[{next_a}]')
            elif overlap:
                filters.append(f'[{a}]atrim=duration={elapsed-overlap/2:.8f}[al{index}]')
                filters.append(f'[a{index+1}]atrim=start={overlap/2:.8f},asetpts=PTS-STARTPTS[ar{index}]')
                filters.append(f'[al{index}][ar{index}]concat=n=2:v=0:a=1[{next_a}]')
            else:
                filters.append(f'[{a}][a{index+1}]concat=n=2:v=0:a=1[{next_a}]')
        v, a = next_v, next_a
        elapsed += clips[index+1]['duration'] - overlap
    if output['audio_policy'] == 'fade' and output['fade_duration']:
        fade = output['fade_duration']
        filters.append(f'[{a}]afade=t=in:d={fade}:st=0,afade=t=out:d={fade}:st={max(0,elapsed-fade):.8f}[outa]')
        a = 'outa'
    if output.get('subtitles'):
        subtitle_file=PROJECT_ROOT/'logs/assemblies'/(destination.stem+'.srt');subtitle_file.parent.mkdir(parents=True,exist_ok=True)
        def timestamp(seconds):
            millis=round(seconds*1000);hours,millis=divmod(millis,3600000);minutes,millis=divmod(millis,60000);seconds,millis=divmod(millis,1000)
            return f'{hours:02}:{minutes:02}:{seconds:02},{millis:03}'
        cues=snapshot.get('subtitle_cues',[])
        subtitle_file.write_text('\n\n'.join(str(i+1)+'\n'+timestamp(c['start'])+' --> '+timestamp(c['end'])+'\n'+c['text'].replace('\r',' ').replace('\n',' ') for i,c in enumerate(cues))+'\n',encoding='utf-8')
        command+=['-i',str(subtitle_file),'-map',f'{len(clips)}:s:0','-c:s','mov_text','-metadata:s:s:0','language=zho']
    command += ['-filter_complex', ';'.join(filters), '-map', f'[{v}]']
    command += ['-an'] if output['audio_policy'] == 'mute' else ['-map', f'[{a}]', '-c:a', 'aac', '-b:a', '192k', '-ar', '48000']
    command += ['-c:v', output['codec'], '-crf', '18', '-preset', 'fast', '-pix_fmt', 'yuv420p', '-r', str(fps), '-t', f'{snapshot["duration"]:.8f}', '-movflags', '+faststart', '-progress', 'pipe:1', '-nostats', str(destination)]
    return command


def run_render(settings, session_factory, identifier):
    with session_factory() as session:
        claimed = session.execute(update(AssemblyRender).where(AssemblyRender.id==identifier,AssemblyRender.status=='queued').values(status='running'))
        session.commit()
        if not claimed.rowcount:
            return
        row = session.get(AssemblyRender, identifier)
        snapshot = row.snapshot_json
        relative = f'assemblies/{snapshot["project_id"]}/{row.assembly_id}/{identifier}.mp4'
        dest = safe_media_path(settings.media_root, relative)
        dest.parent.mkdir(parents=True, exist_ok=True)
        row.log_path = str(PROJECT_ROOT / f'logs/assemblies/{identifier}.log')
        logfile = Path(row.log_path)
        logfile.parent.mkdir(parents=True, exist_ok=True)
        row.status = 'running'
        row.metadata_json = {'progress': 0, 'processed_seconds': 0, 'expected_seconds': snapshot['duration']}
        session.commit()
    process = None
    try:
        if snapshot.get('automatic_draft'):
            from app.services.studio_pipeline import _valid_video
            from app.services.cinematic_quality import generation_inputs_hash
            from app.models import StoryboardShot
            with session_factory() as session:
                for clip in snapshot['clips']:
                    video=session.get(VideoRevision,clip['video_id'])
                    if not video or video.review_status not in {'pending','approved'} or video.shot_id!=clip['shot_id'] or video.project_id!=snapshot['project_id']:
                        raise AppError(409,'draft_video_changed','草稿输入已驳回或失效，不能继续渲染')
                    if video.metadata_json.get('sha256')!=clip['sha256']:
                        raise AppError(409,'draft_video_changed','草稿输入文件哈希记录已变化')
                    job=session.get(GenerationJob,video.generation_job_id);shot=session.get(StoryboardShot,video.shot_id)
                    expected=(job.parameters_json.get('screenplay_snapshot',{}).get('cinematic',{}).get('preflight',{}).get('generation_inputs_hash')) if job else None
                    if not shot or not expected or expected!=generation_inputs_hash(session,shot):
                        raise AppError(409,'draft_video_changed','草稿镜头输入已变化，不能继续渲染')
                    _valid_video(session,settings,video)
        command = command_for(settings, snapshot, dest)
        lines = queue.Queue()
        with logfile.open('w', encoding='utf-8') as log:
            log.write(json.dumps({'command': command, 'snapshot': snapshot}, ensure_ascii=False)+'\n')
            log.flush()
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=log, encoding='utf-8', errors='replace', shell=False, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            def read_progress():
                for line in process.stdout:
                    lines.put(line.strip())
            reader = threading.Thread(target=read_progress, daemon=True)
            reader.start()
            started, updated, processed = time.monotonic(), 0, 0
            cancelled = False
            while process.poll() is None:
                while not lines.empty():
                    line = lines.get_nowait()
                    if line.startswith('out_time_us='):
                        try:
                            processed = max(processed, int(line.split('=', 1)[1]) / 1_000_000)
                        except ValueError:
                            pass
                if time.monotonic() - updated > 0.4:
                    with session_factory() as session:
                        row = session.get(AssemblyRender, identifier)
                        if row.cancel_requested:
                            process.terminate()
                            cancelled = True
                        row.metadata_json = {'progress': min(99, round(processed / snapshot['duration'] * 100, 1)), 'processed_seconds': processed, 'expected_seconds': snapshot['duration'], 'progress_source': 'actual FFmpeg -progress out_time_us'}
                        session.commit()
                    updated = time.monotonic()
                if time.monotonic() - started > 1800:
                    process.kill()
                    raise RuntimeError('FFmpeg 超过 30 分钟上限；任务已终止')
                time.sleep(0.1)
            process.wait(timeout=10)
            reader.join(timeout=2)
            if cancelled:
                with session_factory() as session:
                    row = session.get(AssemblyRender, identifier)
                    row.status = 'cancelled'
                    row.error_message = '已确认当前 FFmpeg 子进程停止；不返回部分文件为成片'
                    session.commit()
                return
            if process.returncode:
                raise RuntimeError(f'FFmpeg exit={process.returncode}；查看 {logfile}')
        tools = replace(settings, ffmpeg_path=snapshot['service_snapshot']['settings'].get('ffmpeg_path') or settings.ffmpeg_path, ffprobe_path=snapshot['service_snapshot']['settings'].get('ffprobe_path') or settings.ffprobe_path)
        checks = inspect_media(dest, tools)
        if any(check['status'] == 'failed' for check in checks):
            raise RuntimeError('成片完整解码检查失败')
        actual = probe(dest, tools)
        if (actual['width'],actual['height'])!=(snapshot['output']['width'],snapshot['output']['height']) or abs(actual['fps']-snapshot['output']['fps'])>.001:
            raise RuntimeError('实际成片分辨率或帧率与请求输出不符')
        if snapshot['output']['audio_policy']=='mute' and actual['has_audio']:
            raise RuntimeError('请求静音但实际输出包含音轨')
        if snapshot['output']['audio_policy']!='mute' and not actual['has_audio']:
            raise RuntimeError('请求保留音轨但实际输出缺少音轨')
        if abs(actual['duration'] - snapshot['duration']) > 1.1 / snapshot['output']['fps'] or abs(actual['frames'] - snapshot['expected_frames']) > 1:
            raise RuntimeError(f'输出时长/帧数不符合时间线：actual={actual["duration"]}/{actual["frames"]}, expected={snapshot["duration"]}/{snapshot["expected_frames"]}')
        cover = dest.with_suffix('.jpg')
        subprocess.run([tools.ffmpeg_path, '-v', 'error', '-n', '-i', str(dest), '-frames:v', '1', str(cover)], capture_output=True, timeout=30, check=True, shell=False)
        with session_factory() as session:
            row = session.get(AssemblyRender, identifier)
            row.status = 'generated'
            row.relative_path = relative
            row.metadata_json = {'progress':100, 'actual':actual, 'checks':checks, 'size_bytes':dest.stat().st_size, 'sha256':hashlib.sha256(dest.read_bytes()).hexdigest(), 'cover_path':cover.relative_to(settings.media_root).as_posix(), 'progress_source':'actual FFmpeg output, then full decode / duration / frame checks',
                'quality':{'requested_resolution':{'width':snapshot['output']['width'],'height':snapshot['output']['height']},
                    'input_actual_resolutions':[{'video_id':c['video_id'],'width':c['source']['width'],'height':c['source']['height'],'provenance':c.get('source_provenance',{})} for c in snapshot['clips']],
                    'method':'ordinary_lanczos_scaling','ai_super_resolution':False,'some_inputs_ai_super_resolution':any(c.get('source_provenance',{}).get('ai_super_resolution') for c in snapshot['clips']),
                    'native_generation':False,'visual_review':'human_review_required'}}
            service = session.get(AIService, snapshot['service_snapshot']['service_id'])
            if service and service.revision == snapshot['service_snapshot']['service_revision']:
                previous_effects = service.capabilities_json.get('transitions',{}).get('effects',[])
                service.capabilities_json = {**service.capabilities_json, 'transitions':{'state':'verified','source':identifier,'effects':list(dict.fromkeys(previous_effects+[edge['type'] for edge in snapshot['transitions']]))}}
            session.commit()
    except Exception as exc:
        if process and process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        with session_factory() as session:
            row = session.get(AssemblyRender, identifier)
            row.status = 'failed'
            row.error_message = exc.message if isinstance(exc, AppError) else str(exc)
            session.commit()


async def assembly_worker(settings, session_factory):
    # A restarted process cannot prove its old child outcome; do not replay or fake success.
    with session_factory() as session:
        for row in session.scalars(select(AssemblyRender).where(AssemblyRender.status.in_(['running','cancelling']))):
            row.status = 'failed'
            row.error_message = '服务重启中断监控，旧任务结果未确认；请新建渲染版本重试'
        session.commit()
    while True:
        try:
            with session_factory() as session:
                gpu_video=session.scalar(select(GenerationJob.id).where(GenerationJob.provider=='comfyui_h3',GenerationJob.status.in_(['queued','running'])).limit(1))
                gpu_image=session.scalar(select(CharacterGenerationJob.id).where(CharacterGenerationJob.status.in_(['submitting','submitted','running','cancelling']),CharacterGenerationJob.snapshot_json['service_snapshot']['provider'].as_string()=='comfyui').limit(1))
                row = None if gpu_video or gpu_image else session.scalar(select(AssemblyRender).where(AssemblyRender.status == 'queued').order_by(AssemblyRender.created_at).limit(1))
                identifier = row.id if row else None
            if identifier:
                await asyncio.to_thread(run_render, settings, session_factory, identifier)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning('Assembly queue tick: %s',type(exc).__name__)
        await asyncio.sleep(0.5)
