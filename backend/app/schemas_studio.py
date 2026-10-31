"""Authoritative HTTP boundary for the unified studio; exported to JSON Schema/TS."""
from typing import Any,Literal
from pydantic import BaseModel,ConfigDict,Field,model_validator
from app.schemas_quality import QualitySettings

class StudioInput(BaseModel):
    model_config=ConfigDict(extra='forbid')

class StudioProductReferencePlan(StudioInput):
    product_id:str=Field(min_length=1,max_length=64)
    image_ids:list[str]=Field(min_length=1,max_length=4)
    selection_reason:str=Field(min_length=1,max_length=2000)

class PipelineStart(StudioInput):
    dialogue_audio_policy:Literal['auto','native','tts','skip']|None=None
    automatic_draft_assembly:bool=False
    product_reference_plan:StudioProductReferencePlan|None=None
    quality:QualitySettings|None=None
    allow_cloud_costs:bool=False
    allow_paid_fallback:bool=False
    width:int=Field(default=1024,ge=256,le=3840,multiple_of=8)
    height:int=Field(default=576,ge=256,le=3840,multiple_of=8)
    shot_ids:list[str]|None=None
    max_retries:int=Field(default=2,ge=0,le=3)

class DraftAssemblyEdit(StudioInput):
    expected_run_id:str=Field(min_length=1,max_length=64)
    enabled:bool

class DecisionInput(StudioInput):
    action:Literal['keep','exclude','pending','regenerate','modify']
    video_id:str|None=None
    trim_start:float=Field(default=0,ge=0)
    trim_end:float|None=Field(default=None,gt=0)
    position:int|None=Field(default=None,ge=1)
    instruction:str=''
    prompt_override:str|None=None
    expected_revision:int|None=None
    operator:Literal['local-user','acceptance-test']='local-user'

    @model_validator(mode='after')
    def validate_trim(self):
        if self.trim_end is not None and self.trim_end<=self.trim_start:raise ValueError('裁剪结束时间必须大于开始时间')
        if self.action=='modify' and not self.instruction.strip():raise ValueError('修改镜头需要填写具体要求')
        if self.prompt_override is not None and not self.prompt_override.strip():raise ValueError('自定义提示词不能为空')
        return self

class ComposeInput(StudioInput):
    expected_decisions:dict[str,int]|None=None
    transition:Literal['cut','crossfade']='cut'
    transition_duration:float=Field(default=.5,ge=0,le=3)
    width:int=Field(default=1280,ge=128,le=3840,multiple_of=2)
    height:int=Field(default=720,ge=128,le=3840,multiple_of=2)
    fps:Literal[24,25,30]=24
    audio_policy:Literal['preserve','mute']='preserve'
    subtitles:bool=False

class StudioProject(BaseModel):
    id:str
    name:str
    status:str='pending'
    shot_count:int=0
    video_count:int=0
    error_count:int=0
    run_id:str|None=None
    script_revision_id:str|None=None

class StudioDecision(BaseModel):
    action:Literal['pending','keep','exclude']='pending'
    video_id:str|None=None
    trim_start:float=0
    trim_end:float|None=None
    position:int=1
    instruction:str=''
    revision:int=1

class StudioShot(BaseModel):
    id:str
    order:int
    title:str
    scene_id:str|None=None
    scene_name:str=''
    cast:list[dict[str,Any]]=Field(default_factory=list)
    duration_seconds:float
    status:str
    video:dict[str,Any]|None=None
    decision:StudioDecision
    action:str=''
    dialogues:list[dict[str,Any]]=Field(default_factory=list)

class StudioOverview(BaseModel):
    project:dict[str,Any]
    script:dict[str,Any]|None=None
    characters:list[dict[str,Any]]=Field(default_factory=list)
    scenes:list[dict[str,Any]]=Field(default_factory=list)
    shots:list[StudioShot]=Field(default_factory=list)
    run:dict[str,Any]|None=None
    exceptions:list[dict[str,Any]]=Field(default_factory=list)
    renders:list[dict[str,Any]]=Field(default_factory=list)

class ComposeResult(BaseModel):
    assembly_id:str
    render_id:str
    status:str
