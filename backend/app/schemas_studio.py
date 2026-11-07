"""Authoritative HTTP boundary for the unified studio; exported to JSON Schema/TS."""
from typing import Annotated,Any,Literal
from pydantic import BaseModel,ConfigDict,Field,model_validator
from app.schemas_quality import QualitySettings
from app.schemas_cinematic import MotionReferenceInput

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
    prompt_overrides:dict[str,Annotated[str,Field(min_length=1)]]=Field(default_factory=dict)
    reference_image_ids:dict[str,Annotated[list[Annotated[str,Field(min_length=1,max_length=64)]],Field(max_length=9)]]=Field(default_factory=dict)
    motion_reference_overrides:dict[str,Annotated[list[MotionReferenceInput],Field(max_length=3)]]=Field(default_factory=dict)
    max_retries:int=Field(default=2,ge=0,le=3)

    @model_validator(mode='after')
    def explicit_inputs(self):
        if any(not prompt.strip() for prompt in self.prompt_overrides.values()):
            raise ValueError('镜头生成要求不能为空白')
        if any(len(ids)!=len(set(ids)) for ids in self.reference_image_ids.values()):
            raise ValueError('同一镜头不能重复选择参考图片')
        return self


class ShotFramingTarget(StudioInput):
    id:str=Field(min_length=1,max_length=64)
    expected_revision:int=Field(ge=1,strict=True)

class ShotFramingUpdate(StudioInput):
    aspect_ratio:Literal['16:9','9:16','1:1']
    shots:list[ShotFramingTarget]=Field(min_length=1,max_length=5000)

    @model_validator(mode='after')
    def unique_shots(self):
        if len({shot.id for shot in self.shots})!=len(self.shots):
            raise ValueError('同一批次不能重复选择镜头')
        return self

class ShotFramingRow(BaseModel):
    id:str
    order:int
    title:str
    aspect_ratio:str
    revision:int
    editable:bool
    block_reason:str|None=None

class ShotFramingOverview(BaseModel):
    project_id:str
    shots:list[ShotFramingRow]
    supported_aspect_ratios:list[Literal['16:9','9:16','1:1']]
    requires_new_run:bool=False

class ShotFramingResult(ShotFramingOverview):
    updated_shot_ids:list[str]=Field(default_factory=list)
    notice:str=''



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
