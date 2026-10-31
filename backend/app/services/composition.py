from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from sqlalchemy import select

from app.core.config import PROJECT_ROOT
from app.core.paths import safe_media_path
from app.models import AuditEvent, FinalComposition, VideoRevision
from app.services.quality import inspect_media


def compose(settings, paths: list[Path], destination: Path, log_path: Path):
    command = [settings.ffmpeg_path, "-hide_banner", "-n"]
    for path in paths:
        command.extend(["-i", str(path)])
    filters = [f"[{index}:v:0]scale=640:384:force_original_aspect_ratio=decrease,pad=640:384:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24,format=yuv420p,setpts=PTS-STARTPTS[v{index}]" for index in range(len(paths))]
    filters.append("".join(f"[v{i}]" for i in range(len(paths))) + f"concat=n={len(paths)}:v=1:a=0[outv]")
    command.extend(["-filter_complex", ";".join(filters), "-map", "[outv]", "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-movflags", "+faststart", str(destination)])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        log.write(json.dumps(command, ensure_ascii=False) + "\n")
        log.flush()
        result = subprocess.run(command, stdout=log, stderr=log, timeout=600, shell=False)
    if result.returncode:
        raise RuntimeError(f"FFmpeg exit={result.returncode}; {log_path}")
    checks = inspect_media(destination, settings)
    if any(c["status"] == "failed" for c in checks):
        raise RuntimeError(f"Composition decode failed; {log_path}")
    cover = destination.with_suffix(".png")
    with log_path.open("a", encoding="utf-8") as log:
        cover_command = [settings.ffmpeg_path, "-v", "error", "-n", "-i", str(destination), "-frames:v", "1", str(cover)]
        log.write(json.dumps(cover_command, ensure_ascii=False) + "\n")
        result = subprocess.run(cover_command, stdout=log, stderr=log, timeout=60, shell=False)
        if result.returncode:
            raise RuntimeError("Final cover extraction failed")
    return {"command": command, "checks": checks, "has_audio": False, "audio_note": "本次合成输出无音轨；片段原始音轨保留在各版本文件。", "log_path": str(log_path), "cover_relative_path": str(cover.relative_to(settings.media_root)).replace("\\", "/")}


async def process_composition(settings, session_factory):
    with session_factory() as session:
        item = session.scalar(select(FinalComposition).where(FinalComposition.status == "queued").order_by(FinalComposition.created_at).limit(1))
        if item is None:
            return
        item.status = "running"
        identifier = item.id
        paths = []
        for video_id in item.video_ids_json:
            video = session.get(VideoRevision, video_id)
            if video.review_status != "approved":
                item.status = "failed"
                item.error_message = "片段审核状态已改变，请重新选择批准版本"
                session.commit()
                return
            paths.append(safe_media_path(settings.media_root, video.relative_path, must_exist=True))
        relative = f"compositions/{item.project_id}/{identifier}.mp4"
        dest = safe_media_path(settings.media_root, relative)
        dest.parent.mkdir(parents=True, exist_ok=True)
        session.commit()
    try:
        metadata = await asyncio.to_thread(compose, settings, paths, dest, PROJECT_ROOT / f"logs/compositions/{identifier}.log")
        with session_factory() as session:
            item = session.get(FinalComposition, identifier)
            item.status = "generated"
            item.relative_path = relative
            item.metadata_json = metadata
            session.add(AuditEvent(object_type="final_composition", object_id=identifier, action="composed", details_json=metadata))
            session.commit()
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        with session_factory() as session:
            item = session.get(FinalComposition, identifier)
            item.status = "failed"
            item.error_message = str(error)
            session.commit()
