"""Shared versioned IR; extra source fields are retained, not silently discarded."""
from typing import Any,Literal
from pydantic import BaseModel,ConfigDict,Field,model_validator


class Preserved(BaseModel):
    model_config=ConfigDict(extra='allow')


class Provenance(Preserved):
    state:Literal['explicit','suggested','inherited','reviewed','unknown','conflict']
    source_ids:list[str]=Field(default_factory=list)
    confidence:float=Field(default=0,ge=0,le=1)
    reason:str=''


class Versions(Preserved):
    production_rules_version:str
    screenplay_schema_version:str
    template_version:str
    parser_version:str
    video_prompt_compiler_version:str


class CharacterIR(Preserved):
    key:str
    name:str=Field(min_length=1,max_length=160)
    aliases:list[str]=Field(default_factory=list)
    profile:dict[str,Any]=Field(default_factory=dict)
    source:list[str]=Field(default_factory=list)
    provenance:dict[str,Provenance]=Field(default_factory=dict)
    decision:Literal['new','existing','pending']='new'
    match_character_id:str|None=None


class DesignIR(Preserved):
    key:str
    kind:Literal['wardrobe','prop','scene_state']
    name:str
    character_key:str|None=None
    scene_key:str|None=None
    spec:dict[str,Any]=Field(default_factory=dict)
    source:list[str]=Field(default_factory=list)
    provenance:dict[str,Provenance]=Field(default_factory=dict)


class SceneIR(Preserved):
    key:str
    name:str=Field(min_length=1,max_length=240)
    order:int=Field(ge=1)
    environment:dict[str,Any]=Field(default_factory=dict)
    audio_events:list[dict[str,Any]]=Field(default_factory=list)
    source:list[str]=Field(default_factory=list)
    provenance:dict[str,Provenance]=Field(default_factory=dict)


class ParticipantIR(Preserved):
    character_key:str
    performance:dict[str,str]=Field(default_factory=dict)
    source:list[str]=Field(default_factory=list)
    provenance:dict[str,Provenance]=Field(default_factory=dict)


class LineIR(Preserved):
    character_key:str
    text:str=Field(min_length=1)
    delivery:dict[str,Any]=Field(default_factory=dict)
    source:list[str]=Field(default_factory=list)
    provenance:dict[str,Provenance]=Field(default_factory=dict)


class Beat(Preserved):
    character_key:str
    start_seconds:float=Field(ge=0)
    end_seconds:float=Field(gt=0)
    action:str=Field(min_length=1)
    start_state:str=''
    end_state:str=''
    contact_target:str=''
    source_ids:list[str]=Field(default_factory=list)

    @model_validator(mode='after')
    def time_order(self):
        if self.end_seconds<=self.start_seconds:raise ValueError('动作结束时间须晚于开始')
        return self


class MotionReferenceInput(BaseModel):
    """An explicit, per-shot permission to use a bounded source segment."""
    model_config=ConfigDict(extra='forbid', allow_inf_nan=False)
    video_id:str=Field(min_length=1,max_length=64)
    expected_revision:int=Field(ge=1)
    source_sha256:str=Field(pattern=r'^[0-9a-f]{64}$')
    trim_start_seconds:float=Field(ge=0)
    trim_end_seconds:float=Field(gt=0)
    authorized_for_motion:bool
    reference_role:Literal['structure','action']|None=None
    target_character_id:str|None=Field(default=None,min_length=1,max_length=64)

    @model_validator(mode='after')
    def valid_segment(self):
        if not self.authorized_for_motion:raise ValueError('请明确确认本镜可以使用该视频的动作参考')
        if not 2 <= self.trim_end_seconds-self.trim_start_seconds <= 15:
            raise ValueError('动作参考片段需为2至15秒')
        if self.target_character_id and self.reference_role != 'action':
            raise ValueError('只有动作与姿态参考可以明确绑定人物')
        return self


class ShotProduction(Preserved):
    scene_state_key:str|None=None
    wardrobe_states:dict[str,dict[str,Any]]=Field(default_factory=dict)
    prop_states:list[dict[str,Any]]=Field(default_factory=list)
    beats:list[Beat]=Field(default_factory=list)
    start_state:str=''
    end_state:str=''
    state_change_reason:str=''
    visual_ids:list[str]=Field(default_factory=list,max_length=7)
    require_visual_review:bool=True
    requested_controls:dict[str,Any]=Field(default_factory=dict)
    motion_references:list[MotionReferenceInput]=Field(default_factory=list,max_length=3)

    @model_validator(mode='after')
    def distinct_motion_sources(self):
        ids=[ref.video_id for ref in self.motion_references]
        if len(ids)!=len(set(ids)):raise ValueError('同一动作参考视频不能重复选择')
        return self


class ShotIR(Preserved):
    key:str
    scene_key:str
    title:str=Field(min_length=1,max_length=240)
    visual_prompt:str=''
    action:str=''
    duration_seconds:float=Field(ge=1,le=60)
    camera_move:str=''
    participants:list[ParticipantIR]=Field(default_factory=list,max_length=30)
    dialogues:list[LineIR]=Field(default_factory=list,max_length=300)
    audio_events:list[dict[str,Any]]=Field(default_factory=list)
    photography:dict[str,Any]=Field(default_factory=dict)
    production:ShotProduction=Field(default_factory=ShotProduction)
    source:list[str]=Field(default_factory=list)
    suggested:list[str]=Field(default_factory=list)
    provenance:dict[str,Provenance]=Field(default_factory=dict)


class ScreenplayIR(Preserved):
    versions:Versions
    project:dict[str,Any]
    parser:str
    mode:Literal['standard','general','legacy']='general'
    characters:list[CharacterIR]=Field(default_factory=list,max_length=100)
    scenes:list[SceneIR]=Field(default_factory=list,max_length=300)
    shots:list[ShotIR]=Field(default_factory=list,max_length=1000)
    designs:list[DesignIR]=Field(default_factory=list,max_length=1000)
    unrecognized:list[dict[str,Any]]=Field(default_factory=list)
    parse_warnings:list[dict[str,Any]]=Field(default_factory=list)
    rules_snapshot:dict[str,Any]
    source_versions:dict[str,str]=Field(default_factory=dict)


class DesignInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    kind:Literal['wardrobe','prop','scene_state']
    logical_id:str=Field(min_length=1,max_length=100)
    name:str=Field(min_length=1,max_length=240)
    character_id:str|None=None
    scene_id:str|None=None
    spec:dict[str,Any]=Field(default_factory=dict)


class ReviewInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    status:Literal['approved','rejected','needs_review']
    category:Literal['identity','wardrobe','action','scene','prop','audio','technical']='technical'
    reason:str=Field(min_length=1,max_length=4000)
    operator:str=Field(default='local-user',min_length=1,max_length=100)


class VisualInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    kind:Literal['scene_reference','keyframe','outfit_reference']
    shot_id:str|None=None
    scene_id:str|None=None
    entity_id:str|None=None
    service_id:str|None=None
    model_id:str|None=None
    width:int=Field(default=1024,ge=256,le=4096,multiple_of=8)
    height:int=Field(default=576,ge=256,le=4096,multiple_of=8)
    seed:int=Field(default=20261023,ge=0,le=2_147_483_647)
    instructions:str=''
    confirm_cloud_test:bool=False


class RunInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    shot_ids:list[str]=Field(min_length=1,max_length=100)
    video_service_id:str|None=None
    visual_service_id:str|None=None
    width:int=Field(default=1024,ge=256,le=3840,multiple_of=8)
    height:int=Field(default=576,ge=256,le=3840,multiple_of=8)
    seed:int=Field(default=20261023,ge=0,le=2_147_483_647)
    assembly_resolution_mode:Literal['source','custom']|None=None
    assembly_allow_downscale:bool=False
    assembly_width:int=Field(default=1280,ge=128,le=3840,multiple_of=2)
    assembly_height:int=Field(default=720,ge=128,le=3840,multiple_of=2)
    confirmed:bool=False
