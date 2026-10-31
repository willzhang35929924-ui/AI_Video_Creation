"""Project preparation contracts; reference audio is never a generated voice claim."""
from pydantic import BaseModel, ConfigDict, Field


class VoiceInput(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ProjectVoiceSave(VoiceInput):
    version_id: str = Field(min_length=1, max_length=64)
    audio_id: str | None = Field(default=None, max_length=64)
    expected_token: str = Field(min_length=64, max_length=64)


class ProjectVoiceGroup(BaseModel):
    character_id: str
    name: str
    version_id: str | None
    version_number: int | None
    version_state: str
    participant_ids: list[str]
    shot_ids: list[str]
    dialogue_count: int
    audio_id: str | None
    mixed: bool
    expected_token: str
    tts_enabled: bool
    requires_reference_audio: bool
    ready: bool
    issues: list[str]
    library_asset_id: str | None = None
    configuration_status: str = 'unconfigured'
    generation_status: str = 'pending'


class ProjectVoiceService(BaseModel):
    shot_id: str
    enabled: bool
    service_id: str | None
    name: str
    provider: str | None
    model_id: str | None
    requires_reference_audio: bool
    issues: list[str]


class ProjectVoicePreparation(BaseModel):
    project_id: str
    groups: list[ProjectVoiceGroup]
    services: list[ProjectVoiceService]
    tts_enabled: bool
    ready_to_start: bool
    blockers: list[str]
    summary: str
    audio_policy: str = 'auto'
