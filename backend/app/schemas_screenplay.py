from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class SceneInput(Strict):
    script_revision_id: str | None = None
    name: str = Field(min_length=1, max_length=240)
    order: int = Field(default=1, ge=1)
    environment: dict[str, Any] = Field(default_factory=dict)


class ParticipantAdd(Strict):
    character_ids: list[str] = Field(min_length=1, max_length=30)


class ParticipantPatch(Strict):
    character_version_id: str | None = None
    order: int | None = Field(default=None, ge=1)
    reference_asset_ids: list[str] | None = Field(default=None, max_length=9)
    performance: dict[str, str] | None = None
    revision: int | None = None


class LineInput(Strict):
    participant_id: str
    text: str = Field(min_length=1)
    order: int = Field(default=1, ge=1)
    delivery: dict[str, Any] = Field(default_factory=dict)


class AudioInput(Strict):
    kind: Literal['ambience','sfx','music','voice'] = 'ambience'
    description: str = ''
    start_seconds: float = Field(default=0, ge=0)
    end_seconds: float | None = Field(default=None, ge=0)
    volume: float = Field(default=1, ge=0, le=4)


class ShotDetailInput(Strict):
    scene_id: str | None = None
    photography: dict[str, str] = Field(default_factory=dict)


class ImportPreview(Strict):
    revision: int = Field(ge=1)
    preview: dict[str, Any]


class ImportConfirm(Strict):
    revision: int = Field(ge=1)
    duplicate_strategy: Literal['new_project','new_revision','compare']
    project_id: str | None = None
    generate_standard_faces: bool = False
    reuse_approved: bool = True


class DeletePreview(Strict):
    resource: str
    ids: list[str] = Field(min_length=1, max_length=100)
    mode: Literal['keep','trash','permanent'] = 'keep'
    cascade: bool = False


class DeleteExecute(DeletePreview):
    fingerprint: str
    confirm: bool
    confirm_permanent: bool = False
