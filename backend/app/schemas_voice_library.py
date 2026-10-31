"""Local voice-library contracts without implicit synthesis consent."""
from pydantic import BaseModel, ConfigDict, Field


class VoiceLibraryInput(BaseModel):
    model_config = ConfigDict(extra='forbid')


class VoiceLibraryImport(VoiceLibraryInput):
    paths: list[str] = Field(min_length=1, max_length=20)
    name: str | None = Field(default=None, max_length=240)
    language: str = Field(default='und', min_length=1, max_length=40)
    tags: list[str] = Field(default_factory=list, max_length=40)
    source_note: str = Field(default='', max_length=4000)
    reference_authorized: bool = False


class VoiceLibraryPatch(VoiceLibraryInput):
    expected_revision: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=240)
    language: str | None = Field(default=None, min_length=1, max_length=40)
    tags: list[str] | None = Field(default=None, max_length=40)
    source_note: str | None = Field(default=None, max_length=4000)
    voice_description: str | None = Field(default=None, max_length=1000)
    role_types: list[str] | None = Field(default=None, max_length=40)
    reference_authorized: bool | None = None


class VoiceLibraryBind(VoiceLibraryInput):
    project_id: str = Field(min_length=1, max_length=64)
    character_id: str = Field(min_length=1, max_length=64)
    version_id: str = Field(min_length=1, max_length=64)
    expected_token: str = Field(min_length=64, max_length=64)
