"""Deterministic audio timing/volume after native video generation."""
import json
import subprocess
from pathlib import Path
from app.core.paths import safe_media_path
from app.core.errors import AppError


def probe_audio(path,settings,require_audio=True):
    try:
        result=subprocess.run([settings.ffprobe_path,'-v','error','-show_streams','-show_format','-of','json',str(path)],capture_output=True,timeout=30,check=True)
        data=json.loads(result.stdout)
    except (OSError,subprocess.SubprocessError,ValueError) as exc:
        raise AppError(422,'audio_decode_failed','音频文件无法由本机FFprobe读取') from exc
    if require_audio and not any(s['codec_type']=='audio' for s in data.get('streams',[])):
        raise AppError(422,'audio_stream_missing','文件没有可解码音轨')
    return data


def mix_audio_events(video_path,events,settings):
    actual=[event for event in events if event.get('relative_path')]
    if not actual:return None
    info=probe_audio(video_path,settings,False)
    duration=float(info['format']['duration'])
    output=video_path.with_name(video_path.stem+'.mixing.mp4')
    command=[settings.ffmpeg_path,'-y','-nostdin','-v','error','-i',str(video_path)]
    filters=[];labels=['[0:a:0]'];manifest=[]
    if not any(s['codec_type']=='audio' for s in info['streams']):
        filters.append(f'anullsrc=r=48000:cl=stereo,atrim=duration={duration}[base]');labels=['[base]']
    for index,event in enumerate(actual,1):
        source=safe_media_path(settings.media_root,event['relative_path'],must_exist=True)
        probe_audio(source,settings)
        start=float(event.get('start_seconds',0));end=float(event['end_seconds']) if event.get('end_seconds') is not None else duration
        volume=float(event.get('volume',1))
        if not 0<=start<end<=duration+0.05 or not 0<=volume<=4:
            raise AppError(422,'audio_event_range','音轨起止时间必须位于镜头时长内，音量必须为0到4')
        command+=['-i',str(source)]
        name='event'+str(index);labels.append('['+name+']')
        filters.append(f'[{index}:a:0]atrim=duration={end-start:.6f},asetpts=PTS-STARTPTS,volume={volume:.6f},adelay={round(start*1000)}:all=1[{name}]')
        manifest.append({'event_id':event.get('id'),'relative_path':event['relative_path'],'start_seconds':start,'end_seconds':end,'volume':volume})
    filters.append(''.join(labels)+f'amix=inputs={len(labels)}:duration=first:normalize=0:dropout_transition=0,alimiter=limit=0.95[mixed]')
    command+=['-filter_complex',';'.join(filters),'-map','0:v:0','-map','[mixed]','-c:v','copy','-c:a','aac','-t',str(duration),'-movflags','+faststart',str(output)]
    try:
        result=subprocess.run(command,capture_output=True,timeout=180)
        if result.returncode:raise AppError(502,'audio_mix_failed','FFmpeg音轨合成失败，原始本次生成文件保留',{'stderr':result.stderr.decode('utf-8','replace')[-2000:]})
        probe_audio(output,settings)
        output.replace(video_path)
    finally:
        if output.exists():output.unlink()
    return {'method':'ffmpeg_post_mix','events':manifest,'video_stream':'copied without re-encoding'}
