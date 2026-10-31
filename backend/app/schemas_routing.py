"""Canonical task-slot, routing and isolated-runtime boundaries.

Old AIService modules remain valid. Slots select only adapters and capabilities
that have real evidence; cloud drafts are never promoted by connectivity alone.
"""
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

TaskSlot = Literal['script_planning','character_image','keyframe_image','video_generation',
    'motion_driving','dialogue_generation','lip_sync','video_upscale','quality_check','final_assembly',
    'screenplay_from_text','screenplay_from_image','screenplay_from_video']
ServiceModule = Literal['language_processing','character_generation','video_generation','final_assembly',
    'dialogue_generation','lip_sync','video_upscale','quality_check']
ServiceProvider = Literal['comfyui','openai_compatible','video_api','ffmpeg','local_runtime','local_llm','openai_tts','cloud_draft']
RouteMode = Literal['local_only','cloud_only','local_first','cloud_first','manual','auto']
RouteScope = Literal['system','project','shot']
TaskState = Literal['queued','running','succeeded','failed','unknown','cancelled','blocked','skipped']


def reject_credentials(value,depth=0):
    if depth>12:raise ValueError('任务参数嵌套层级过深')
    if isinstance(value,dict):
        for key,item in value.items():
            normalized=''.join(ch for ch in str(key).casefold() if ch.isalnum())
            if normalized in {'apikey','xapikey','authorization','accesstoken','token','secret','password','headers'}:
                raise ValueError('认证信息只能保存在服务的专用安全字段')
            reject_credentials(item,depth+1)
    elif isinstance(value,list):
        for item in value:reject_credentials(item,depth+1)


class RoutingInput(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)


class RouteChoice(RoutingInput):
    service_id:str=Field(min_length=1,max_length=64)
    model_id:str=Field(default='',max_length=240)


class RouteSettings(RoutingInput):
    mode:RouteMode='local_only'
    choices:list[RouteChoice]=Field(default_factory=list,max_length=20)
    parameters:dict[str,Any]=Field(default_factory=dict)
    quality_mode:Literal['preview','standard','high']='standard'
    allow_fallback:bool=False
    allow_paid_fallback:bool=False
    max_retries:int=Field(default=0,ge=0,le=2)

    @model_validator(mode='after')
    def coherent_fallback(self):
        reject_credentials(self.parameters)
        if self.allow_paid_fallback and not self.allow_fallback:
            raise ValueError('付费回退还需显式开启回退策略')
        keys=[(c.service_id,c.model_id) for c in self.choices]
        if len(set(keys))!=len(keys):raise ValueError('路由不能重复同一服务与模型')
        return self


class RouteSave(RoutingInput):
    settings:RouteSettings
    expected_revision:int=Field(ge=0)


class RouteProfile(BaseModel):
    scope:RouteScope
    target_id:str
    slot:TaskSlot
    revision:int
    inherited_from:str|None=None
    settings:RouteSettings


class TaskCandidate(BaseModel):
    service_id:str
    model_id:str
    name:str
    provider:ServiceProvider
    module:ServiceModule
    is_cloud:bool
    enabled:bool
    eligible:bool=False
    verification:Literal['not_tested','verified','failed','unavailable']='not_tested'
    reasons:list[str]=Field(default_factory=list)
    evidence:dict[str,Any]=Field(default_factory=dict)


class SlotOverview(BaseModel):
    slot:TaskSlot
    name:str
    allowed_modules:list[ServiceModule]
    default_route:RouteProfile
    candidates:list[TaskCandidate]=Field(default_factory=list)


class ModelServicesOverview(BaseModel):
    slots:list[SlotOverview]
    limitations:list[str]=Field(default_factory=list)


class TaskSubmit(RoutingInput):
    slot:TaskSlot
    project_id:str
    shot_id:str|None=None
    inputs:dict[str,Any]=Field(default_factory=dict)
    parameters:dict[str,Any]=Field(default_factory=dict)
    allow_cloud_costs:bool=False
    allow_paid_fallback:bool=False

    @model_validator(mode='after')
    def private_auth_is_not_task_input(self):
        reject_credentials(self.inputs);reject_credentials(self.parameters)
        return self


class UnifiedTask(BaseModel):
    id:str
    source_type:str
    source_id:str
    project_id:str|None=None
    shot_id:str|None=None
    slot:TaskSlot
    status:TaskState
    provider:str=''
    model_id:str=''
    snapshot:dict[str,Any]=Field(default_factory=dict)
    output:dict[str,Any]=Field(default_factory=dict)
    error:dict[str,Any]|None=None
    created_at:str|None=None
    started_at:str|None=None
    finished_at:str|None=None


class ServiceGenerationTest(RoutingInput):
    """One explicitly requested adapter verification, independent of production routes."""
    slot:TaskSlot
    project_id:str
    shot_id:str|None=None
    model_id:str=''
    inputs:dict[str,Any]=Field(default_factory=dict)
    parameters:dict[str,Any]=Field(default_factory=dict)
    allow_cloud_costs:bool=False

    @model_validator(mode='after')
    def no_credentials(self):
        reject_credentials(self.inputs);reject_credentials(self.parameters)
        return self


class RuntimeJob(RoutingInput):
    """JSON input to fixed, allowlisted CLI adapters; never shell code.

    Public HTTP inputs resolve media IDs to paths before constructing this job.
    Helpers emit a receipt, but the backend independently validates output bytes.
    """
    protocol:Literal['video-factory-runtime-1']='video-factory-runtime-1'
    task_id:str=Field(min_length=1,max_length=100)
    slot:TaskSlot
    model_id:str
    inputs:dict[str,Any]=Field(default_factory=dict)
    parameters:dict[str,Any]=Field(default_factory=dict)
    output_path:str
    receipt_path:str


class AudioAssetDTO(BaseModel):
    id:str
    project_id:str
    shot_id:str|None=None
    participant_id:str|None=None
    character_version_id:str|None=None
    relative_path:str
    sha256:str
    metadata:dict[str,Any]=Field(default_factory=dict)
    media_url:str


class RuntimeReceipt(BaseModel):
    protocol:Literal['video-factory-runtime-1']='video-factory-runtime-1'
    task_id:str
    slot:TaskSlot
    model_id:str
    status:Literal['succeeded','failed']
    output_path:str|None=None
    output_sha256:str|None=None
    inputs_sha256:dict[str,str]=Field(default_factory=dict)
    actual:dict[str,Any]=Field(default_factory=dict)
    runtime:dict[str,Any]=Field(default_factory=dict)
    elapsed_seconds:float|None=None
    error:str|None=None


class RuntimeComponent(RoutingInput):
    path:str
    role:str
    bytes:int=Field(ge=0)
    sha256:str=Field(pattern=r'^[a-f0-9]{64}$')


class RuntimeManifest(RoutingInput):
    protocol:Literal['video-factory-deployment-1']='video-factory-deployment-1'
    family:str=Field(pattern=r'^[a-z0-9_]+$')
    model_id:str
    name:str
    version:str
    task_capabilities:list[TaskSlot]
    python:str=''
    repo:str=''
    model_dir:str=''
    helper:str=''
    helper_sha256:str=''
    workflow_path:str=''
    workflow_sha256:str=''
    components:list[RuntimeComponent]=Field(default_factory=list)
    dependency_status:Literal['not_tested','installed','failed','blocked']='not_tested'
    load_status:Literal['not_tested','verified','failed']='not_tested'
    smoke_test:dict[str,Any]=Field(default_factory=lambda:{'status':'NOT RUN'})
    sources:list[dict[str,Any]]=Field(default_factory=list)
    licenses:list[dict[str,str]]=Field(default_factory=list)
    limitations:list[str]=Field(default_factory=list)
    last_tested_at:str|None=None
    failure_reason:str|None=None
