from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from app.core.config import Settings


def inspect_media(path: Path, settings: Settings) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    exists = path.exists() and path.is_file()
    checks.append(
        {
            "check_type": "file_readable",
            "status": "passed" if exists and path.stat().st_size > 0 else "failed",
            "details": {"exists": exists, "size_bytes": path.stat().st_size if exists else 0},
        }
    )
    if not exists:
        checks.append({"check_type": "media_decode", "status": "failed", "details": {"reason": "file_missing"}})
        return checks
    if path.suffix.lower() == ".demo.json":
        checks.extend(
            [
                {
                    "check_type": "media_decode",
                    "status": "not_available",
                    "details": {"reason": "demo_status_replay_is_not_video"},
                },
                {
                    "check_type": "face_consistency",
                    "status": "not_configured",
                    "details": {"reason": "face_similarity_provider_not_configured"},
                },
            ]
        )
        return checks
    executable = shutil.which(settings.ffprobe_path)
    if not executable:
        checks.append(
            {
                "check_type": "media_decode",
                "status": "not_available",
                "details": {"reason": "ffprobe_not_found"},
            }
        )
    else:
        try:
            completed = subprocess.run(
                [
                    executable,
                    "-v",
                    "error",
                    "-count_frames",
                    "-show_streams",
                    "-show_format",
                    "-of",
                    "json",
                    str(path),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                check=False,
                shell=False,
            )
            if completed.returncode == 0:
                details = json.loads(completed.stdout or "{}")
                duration = float(details.get("format", {}).get("duration", 0))
                video = next((s for s in details.get("streams", []) if s.get("codec_type") == "video"), None)
                valid = duration > 0 and video and int(video.get("width", 0)) > 0 and int(video.get("height", 0)) > 0
                decode = shutil.which(settings.ffmpeg_path)
                if valid and decode:
                    decoded = subprocess.run([decode, "-v", "error", "-xerror", "-i", str(path), "-map", "0:v?", "-map", "0:a?", "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, check=False, shell=False)
                    valid = decoded.returncode == 0
                    details["full_decode"] = {"exit_code": decoded.returncode, "stderr": decoded.stderr[-2000:]}
                checks.append({"check_type": "media_decode", "status": "passed" if valid else "failed", "details": details})
            else:
                checks.append(
                    {
                        "check_type": "media_decode",
                        "status": "failed",
                        "details": {"stderr": completed.stderr[-1000:]},
                    }
                )
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, TypeError, ValueError) as exc:
            checks.append(
                {
                    "check_type": "media_decode",
                    "status": "failed",
                    "details": {"reason": type(exc).__name__},
                }
            )
    checks.append(
        {
            "check_type": "face_consistency",
            "status": "not_configured",
            "details": {"reason": "face_similarity_provider_not_configured"},
        }
    )
    return checks

