from __future__ import annotations

from pathlib import Path, PurePosixPath

from .errors import AppError


def safe_media_path(media_root: Path, relative_path: str, *, must_exist: bool = False) -> Path:
    normalized = PurePosixPath(relative_path.replace("\\", "/"))
    if normalized.is_absolute() or ".." in normalized.parts or not normalized.parts:
        raise AppError(400, "invalid_media_path", "媒体路径必须是 MEDIA_ROOT 内的相对路径")
    root = media_root.resolve()
    candidate = root.joinpath(*normalized.parts).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise AppError(400, "invalid_media_path", "媒体路径越界") from exc
    if must_exist and (not candidate.exists() or not candidate.is_file()):
        raise AppError(404, "media_not_found", "媒体文件不存在")
    return candidate


def media_relative(media_root: Path, absolute_path: Path) -> str:
    root = media_root.resolve()
    resolved = absolute_path.resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise AppError(400, "invalid_media_path", "媒体文件不在 MEDIA_ROOT 内") from exc

