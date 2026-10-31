"""Canonical model discovery and isolated video benchmark HTTP contract."""
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class LabInput(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ModelDirectoryInput(LabInput):
    path: str = Field(min_length=1, max_length=2048)
    label: str = Field(default='', max_length=160)


class ModelScanInput(LabInput):
    directory_ids: list[str] | None = None


class ModelDirectoryView(BaseModel):
    id: str
    path: str
    label: str
    enabled: bool
    scan: dict[str, Any] = Field(default_factory=dict)


class LocalModelView(BaseModel):
    id: str
    name: str
    kind: Literal['language', 'image', 'video', 'encoder', 'vae', 'lora', 'upscale', 'unknown']
    path: str
    model_id: str
    service_id: str | None = None
    workflow_path: str | None = None
    fingerprint: str
    component_status: str
    load_status: Literal['not_tested', 'verified', 'failed']
    eligible_for_comparison: bool
    diagnostics: list[str] = Field(default_factory=list)
    components: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    evidence: dict[str, Any] = Field(default_factory=dict)


class ModelCaseInput(LabInput):
    model_id: str
    label: str = Field(default='', max_length=160)
    parameters: dict[str, Any] = Field(default_factory=dict)


class ModelExperimentInput(LabInput):
    name: str = Field(default='视频模型对照实验', max_length=160)
    shot_id: str
    prompt: str | None = Field(default=None, min_length=1)
    mode: Literal['cross_model', 'parameters'] = 'cross_model'
    seed: int = Field(default=20261020, ge=0, le=2147483647)
    width: int = Field(default=1024, ge=256, le=3840, multiple_of=8)
    height: int = Field(default=576, ge=256, le=3840, multiple_of=8)
    allow_cloud_costs: bool = False
    cases: list[ModelCaseInput] = Field(min_length=2, max_length=6)


class ModelLoadTestInput(LabInput):
    shot_id: str
    prompt: str | None = Field(default=None, min_length=1)
    seed: int = Field(default=20261020, ge=0, le=2147483647)
    width: int = Field(default=1024, ge=256, le=3840, multiple_of=8)
    height: int = Field(default=576, ge=256, le=3840, multiple_of=8)
    steps: int = Field(default=20, ge=1, le=60)
    allow_cloud_costs: bool = False


class ModelCaseReviewInput(LabInput):
    notes: str = Field(default='', max_length=4000)
    scores: dict[str, float] = Field(default_factory=dict)
    best: bool = False

    @model_validator(mode='after')
    def scores_are_human(self):
        if set(self.scores) - {'identity', 'motion', 'environment', 'camera', 'overall'}:
            raise ValueError('评分字段只支持 identity/motion/environment/camera/overall')
        if any(not 0 <= value <= 5 for value in self.scores.values()):
            raise ValueError('人工评分范围为0至5，不是自动电影质量评分')
        return self


class ModelDefaultInput(LabInput):
    confirm: Literal[True]


class ModelExperimentView(BaseModel):
    id: str
    name: str
    mode: Literal['load_test', 'cross_model', 'parameters']
    status: str
    source_shot_id: str
    test_project_id: str
    test_shot_id: str
    snapshot: dict[str, Any] = Field(default_factory=dict)
    cases: list[dict[str, Any]] = Field(default_factory=list)
    results: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    created_at: str


class ModelLabOverview(BaseModel):
    directories: list[ModelDirectoryView] = Field(default_factory=list)
    models: list[LocalModelView] = Field(default_factory=list)
    experiments: list[ModelExperimentView] = Field(default_factory=list)
    default: dict[str, Any] | None = None
    limitations: list[str] = Field(default_factory=list)
