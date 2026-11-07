from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.schemas_routing import ServiceModule as Module, ServiceProvider as Provider


class ProductModel(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class ServiceCreate(ProductModel):
    module: Module
    provider: Provider
    name: str = Field(min_length=1, max_length=160)
    base_url: str = Field(default='', max_length=2048)
    enabled: bool = True
    default_model: str = Field(default='', max_length=240)
    models: list[str] = Field(default_factory=list, max_length=100)
    settings: dict[str, Any] = Field(default_factory=dict)
    api_key: SecretStr | None = None


class ServicePatch(ProductModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    base_url: str | None = Field(default=None, max_length=2048)
    enabled: bool | None = None
    default_model: str | None = Field(default=None, max_length=240)
    models: list[str] | None = Field(default=None, max_length=100)
    settings: dict[str, Any] | None = None
    api_key: SecretStr | None = None
    clear_api_key: bool = False
    confirm_clear: bool = False


class BindingRequest(ProductModel):
    service_id: str
    model_id: str = ''


class IdentityProfile(ProductModel):
    age: int | None = Field(default=None, ge=0)
    original_adult: bool | None = None  # Legacy field; no confirmation gate.
    description: str = Field(default='', max_length=4000)
    facial_features: str = Field(default='', max_length=2000)
    hair: str = Field(default='', max_length=1000)
    eyes: str = Field(default='', max_length=1000)
    skin: str = Field(default='', max_length=1000)
    identifying_marks: str = Field(default='', max_length=1000)
    body: str = Field(default='', max_length=1000)
    style: str = Field(default='Photorealistic cinematic photography, natural skin texture.', max_length=2000)
    fixed_traits: list[str] = Field(default_factory=list, max_length=30)
    variable_attributes: list[str] = Field(default_factory=list, max_length=30)
    scene_constraints: list[str] = Field(default_factory=list, max_length=30)
    wardrobes: list[dict[str, str]] = Field(default_factory=list, max_length=20)


class ProfileUpdate(ProductModel):
    version_id: str
    profile: IdentityProfile


class BatchRequest(ProductModel):
    version_id: str
    project_id: str | None = Field(default=None, min_length=1, max_length=64)
    kind: Literal['candidates', 'references'] = 'references'
    service_id: str | None = None
    model_id: str | None = None
    types: list[str] = Field(default_factory=lambda: ['left_45', 'half_body', 'smile'], min_length=1, max_length=16)
    count_per_type: int = Field(default=1, ge=1, le=4)
    candidate_count: int = Field(default=2, ge=1, le=3)
    width: int = Field(default=832, ge=256, le=1536)
    height: int = Field(default=1024, ge=256, le=1536)
    seed: int = Field(default=202610210, ge=0, le=2_000_000_000)
    seed_strategy: Literal['increment', 'random'] = 'increment'
    quality: Literal['draft', 'standard'] = 'standard'
    prompt_template: str = Field(default='', max_length=8000)
    negative_prompt: str = Field(default='anime, cartoon, text, watermark, multiple people, malformed anatomy, plastic skin', max_length=4000)
    wardrobe_id: str = Field(default='default', max_length=100)
    wardrobe_description: str = Field(default='', max_length=2000)
    concurrency: int = Field(default=1, ge=1, le=4)
    max_tasks: int = Field(default=12, ge=1, le=12)
    max_retries: int = Field(default=0, ge=0, le=2)
    confirm: bool = False
    preview_hash: str = ''


class AnchorRequest(ProductModel):
    asset_id: str
    confirm_anchor_lock: bool
    reason: str = Field(min_length=1, max_length=2000)
    operator: str = Field(default='local-user', max_length=100)


class ProductAssetReview(ProductModel):
    status: Literal['approved', 'rejected', 'needs_review']
    reason: str = Field(min_length=1, max_length=2000)
    operator: str = Field(default='local-user', max_length=100)
    checks: list[str] = Field(default_factory=list, max_length=30)


class BatchReview(ProductModel):
    asset_ids: list[str] = Field(min_length=1, max_length=12)
    review: ProductAssetReview


class FactoryCharacterCreate(ProductModel):
    id: str = Field(pattern=r'^[a-z0-9][a-z0-9_-]{0,63}$')
    name: str = Field(min_length=1, max_length=160)
    profile: IdentityProfile


class TimelineClip(ProductModel):
    video_id: str
    trim_start: float = Field(default=0,ge=0)
    trim_end: float | None = Field(default=None,gt=0)


class TimelineTransition(ProductModel):
    type: Literal['cut', 'crossfade', 'fadeblack', 'fadewhite', 'dissolve', 'slide', 'push', 'wipe'] = 'cut'
    duration: float = Field(default=0.5, ge=0, le=10)
    direction: Literal['left', 'right', 'up', 'down'] = 'left'
    audio: Literal['cut', 'crossfade'] = 'crossfade'


class AssemblyOutput(ProductModel):
    resolution_mode: Literal['source', 'custom'] | None = None
    allow_downscale: bool = False
    width: int = Field(default=640, ge=128, le=3840)
    height: int = Field(default=384, ge=128, le=3840)
    fps: int = Field(default=24, ge=12, le=60)
    codec: Literal['libx264', 'libx265'] = 'libx264'
    audio_policy: Literal['preserve', 'fade', 'mute'] = 'preserve'
    fade_duration: float = Field(default=0.2, ge=0, le=2)
    subtitles: bool = False


class TimelineRequest(ProductModel):
    clips: list[TimelineClip] = Field(min_length=1, max_length=20)
    transitions: list[TimelineTransition] = Field(default_factory=list, max_length=19)
    output: AssemblyOutput = Field(default_factory=AssemblyOutput)
    service_id: str | None = None


class AssemblyCreate(ProductModel):
    project_id: str
    name: str = Field(min_length=1, max_length=200)
    timeline: TimelineRequest


class AssemblyPatch(ProductModel):
    name: str = Field(min_length=1, max_length=200)
    revision: int = Field(ge=1)
    timeline: TimelineRequest


class AssemblySubmit(ProductModel):
    revision: int = Field(ge=1)
    transition_index: int | None = Field(default=None, ge=0, le=18)
