"""Explicit, optimistic commands for a failed dialogue-duration mix."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AudioDurationChoice(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    expected_run_id: str = Field(min_length=1, max_length=64)
    source_fingerprint: str = Field(pattern=r'^[0-9a-f]{64}$')
    action: Literal['extend_video', 'shorten_dialogue', 'adjust_speed']
    client_request_id: str = Field(min_length=1, max_length=100)
    target_duration_seconds: float | None = Field(default=None, gt=0, le=60)
    dialogue_line_id: str | None = Field(default=None, max_length=64)
    text: str | None = Field(default=None, min_length=1, max_length=20000)
    speed: float | None = Field(default=None, gt=0, le=4)
    language: str | None = Field(default=None, max_length=12)


class AudioDurationApply(AudioDurationChoice):
    preview_token: str = Field(pattern=r'^[0-9a-f]{64}$')
