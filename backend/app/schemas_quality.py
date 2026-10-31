"""Canonical quality HTTP contract. Resolution is an output target, not a detail score."""
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field

class QualitySettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    resolution: Literal['720p', '1080p', '2k', '4k'] = '720p'
    mode: Literal['preview', 'standard', 'high'] = 'standard'
    aspect_ratio: Literal['16:9', '9:16', '1:1'] = '16:9'
    processing: Literal['lanczos', 'native', 'ai_super_resolution', 'seedvr2', 'reconstruction'] = 'lanczos'

class QualityProfileInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    settings: QualitySettings
    expected_revision: int | None = Field(default=None, ge=0)

class QualityProfileDTO(BaseModel):
    scope: Literal['system', 'project', 'shot']
    target_id: str
    settings: QualitySettings
    revision: int
    inherited_from: str | None = None
    plan: dict[str, Any]

class QualityRenderInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    video_id: str
    settings: QualitySettings

class QualityRenderDTO(BaseModel):
    id: str
    status: str
    source_video_id: str
    video_id: str | None = None
    relative_path: str | None = None
    snapshot: dict[str, Any]
    result: dict[str, Any]
    error_message: str | None = None

