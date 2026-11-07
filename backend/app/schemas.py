from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProjectCreate(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    source_type: str = Field(default="text", max_length=32)
    source_uri: str | None = Field(default=None, max_length=2048)
    source_filename: str | None = Field(default=None, max_length=255)
    source_text: str = Field(default="", max_length=5_000_000)


class ProjectPatch(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)


class TextImportRequest(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=5_000_000)
    source_type: Literal["text", "paste", "markdown"] = "text"


class UrlImportRequest(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    url: HttpUrl


class ScriptPatch(StrictModel):
    raw_text: str = Field(min_length=1, max_length=5_000_000)


class ApprovalRequest(StrictModel):
    reason: str | None = Field(default=None, max_length=1000)
    operator: str = Field(default="local-user", min_length=1, max_length=100)


class ResolveUnknownLocalGeneration(StrictModel):
    confirmed_inspection: Literal[True]
    accept_duplicate_risk: Literal[True]
    evidence_note: str = Field(min_length=10, max_length=2000)
    operator: str = Field(default="local-user", min_length=1, max_length=100)


class ShotBase(StrictModel):
    order: int = Field(ge=1, le=10_000)
    scene_title: str = Field(default="未命名分镜", max_length=240)
    narration: str = Field(default="")
    dialogue: str = Field(default="")
    visual_prompt: str = Field(default="")
    negative_prompt: str = Field(default="")
    action: str = Field(default="")
    camera_move: str = Field(default="", max_length=200)
    location: str = Field(default="", max_length=200)
    lighting: str = Field(default="", max_length=200)
    audio_direction: str = Field(default="")
    duration_seconds: float = Field(default=5, ge=1, le=60)
    aspect_ratio: Literal["9:16", "16:9", "1:1"] = "9:16"


class ShotCreate(ShotBase):
    pass


class ShotPatch(StrictModel):
    order: int | None = Field(default=None, ge=1, le=10_000)
    scene_title: str | None = Field(default=None, min_length=1, max_length=240)
    narration: str | None = Field(default=None)
    dialogue: str | None = Field(default=None)
    visual_prompt: str | None = Field(default=None)
    negative_prompt: str | None = Field(default=None)
    action: str | None = Field(default=None)
    camera_move: str | None = Field(default=None, max_length=200)
    location: str | None = Field(default=None, max_length=200)
    lighting: str | None = Field(default=None, max_length=200)
    audio_direction: str | None = Field(default=None)
    duration_seconds: float | None = Field(default=None, ge=1, le=60)
    aspect_ratio: Literal["9:16", "16:9", "1:1"] | None = None


class CharacterAssignment(StrictModel):
    character_id: str = Field(min_length=1, max_length=64)
    character_version_id: str = Field(min_length=1, max_length=64)
    asset_ids: list[str] | None = Field(default=None, min_length=1, max_length=9)


class AssetReviewRequest(StrictModel):
    status: Literal["approved", "needs_review", "rejected"]
    is_primary_reference: bool = False
    reason: str = Field(min_length=1, max_length=2000)


class CompositionRequest(StrictModel):
    video_ids: list[str] = Field(min_length=1, max_length=100)


class FinalDryRunRequest(StrictModel):
    title: str = Field(min_length=1, max_length=240)
    description: str = Field(default="", max_length=10000)
    hashtags: list[str] = Field(default_factory=list, max_length=50)


class CharacterCreate(StrictModel):
    id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,99}$")
    display_name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=20_000)


class CharacterVersionCreate(StrictModel):
    identity: dict[str, Any] = Field(default_factory=dict)


class GenerationRequest(StrictModel):
    prompt_override: str | None = None
    seed: int | None = Field(default=None, ge=0, le=2_147_483_647)
    parameters: dict[str, Any] = Field(default_factory=dict)
    service_id: str | None = None
    model_id: str | None = None


class RevisionRequest(GenerationRequest):
    instruction: str = Field(min_length=1)
    use_project_video_service: bool = False


class ReviewRequest(StrictModel):
    reason: str | None = Field(default=None, max_length=2000)
    operator: str = Field(default="local-user", min_length=1, max_length=100)


SUPPORTED_PLATFORMS = {"douyin", "kuaishou", "xiaohongshu", "wechat_channels"}


class PublishingAccountCreate(StrictModel):
    platform: str = Field(max_length=40)
    display_name: str = Field(min_length=1, max_length=160)
    auth_status: Literal["not_configured", "placeholder"] = "placeholder"
    account_metadata: dict[str, Any] = Field(default_factory=dict)
    credential_reference: str | None = Field(default=None, max_length=240)
    token_expiry: datetime | None = None

    @field_validator("platform")
    @classmethod
    def validate_platform(cls, value: str) -> str:
        if value not in SUPPORTED_PLATFORMS:
            raise ValueError(f"unsupported platform: {value}")
        return value


class PublishingAccountPatch(StrictModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=160)
    auth_status: Literal["not_configured", "placeholder", "expired"] | None = None
    account_metadata: dict[str, Any] | None = None
    credential_reference: str | None = Field(default=None, max_length=240)
    token_expiry: datetime | None = None


class PublishingPresetCreate(StrictModel):
    account_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=160)
    title_template: str = Field(default="", max_length=240)
    caption_template: str = Field(default="", max_length=10_000)
    hashtags: list[str] = Field(default_factory=list, max_length=50)
    visibility: Literal["private", "public", "followers", "platform_default"] = "private"
    default_schedule: str | None = Field(default=None, max_length=100)
    extra: dict[str, Any] = Field(default_factory=dict)


class DryRunItem(StrictModel):
    video_revision_id: str = Field(min_length=1, max_length=64)
    account_id: str = Field(min_length=1, max_length=64)
    preset_id: str | None = Field(default=None, max_length=64)
    title: str | None = Field(default=None, max_length=240)
    caption: str | None = Field(default=None, max_length=10_000)
    scheduled_at: datetime | None = None


class DryRunRequest(StrictModel):
    items: list[DryRunItem] = Field(min_length=1, max_length=100)


class ApiMessage(BaseModel):
    message: str

