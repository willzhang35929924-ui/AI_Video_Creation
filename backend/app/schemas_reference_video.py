from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ReferenceVideoEdit(StrictModel):
    revision: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=240)
    category: str = Field(default='', max_length=100)
    tags: list[str] = Field(default_factory=list, max_length=30)
    notes: str = Field(default='', max_length=5000)
    source_note: str = Field(default='', max_length=2000)
    usage_scope: str = Field(default='abstract_style_only', max_length=2000)


class AnalyzeReferenceVideo(StrictModel):
    idempotency_key: str = Field(min_length=8, max_length=120)
    use_local_models: bool = True
    max_frames: int = Field(default=8, ge=2, le=24)
    scene_threshold: float = Field(default=.35, ge=.05, le=.9)


class RetryVideoAnalysis(StrictModel):
    revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=120)


class CreateStyleProfile(StrictModel):
    name: str = Field(default='参考视频风格', min_length=1, max_length=240)
    video_ids: list[str] = Field(min_length=1, max_length=10)
    primary_video_id: str | None = None
    user_notes: str = Field(default='', max_length=5000)

    @model_validator(mode='after')
    def valid_sources(self):
        if len(set(self.video_ids)) != len(self.video_ids):
            raise ValueError('参考视频不能重复')
        if self.primary_video_id and self.primary_video_id not in self.video_ids:
            raise ValueError('主参考必须属于选中视频')
        return self


class EditStyleProfile(StrictModel):
    version: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=240)
    user_notes: str = Field(default='', max_length=5000)
    primary_video_id: str | None = None
