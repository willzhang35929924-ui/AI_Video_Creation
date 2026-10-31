from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.schemas_product_library import ProductSelection


class Strict(BaseModel):
    # Project name must preserve the exact submitted value, including intentional spaces.
    model_config = ConfigDict(extra='forbid')


class GenerateScreenplay(Strict):
    project_name: str = Field(min_length=1, max_length=200)
    creative_prompt: str = Field(min_length=1, max_length=16000)
    input_mode: Literal['text', 'image', 'video']
    story_text: str = Field(default='', max_length=40000)
    asset_id: str | None = Field(default=None, max_length=64)
    asset_ids:list[str]=Field(default_factory=list,max_length=8)
    product_selections:list[ProductSelection]=Field(default_factory=list,max_length=4)
    product_mode:Literal['single','combined','separate']='single'
    marketing_template:Literal['showcase','usage','problem_solution','comparison','unboxing','lifestyle','sales','brand']='showcase'
    reference_video_ids:list[str]=Field(default_factory=list,max_length=4)
    style_profile_id:str|None=Field(default=None,max_length=64)
    video_creation_mode:Literal['adaptation','style']='adaptation'
    new_theme:str=Field(default='',max_length=16000)
    primary_reference_video_id:str|None=Field(default=None,max_length=64)
    allow_cloud_media_upload:bool=False
    service_id: str | None = Field(default=None, max_length=64)
    model_id: str = Field(default='', max_length=240)
    allow_cloud_costs: bool = False
    allow_paid_fallback: bool = False
    idempotency_key: str = Field(min_length=8, max_length=120)

    @model_validator(mode='after')
    def mode_input(self):
        if not self.project_name.strip() or not self.creative_prompt.strip():
            raise ValueError('项目名称与创作提示词不能是空白')
        if any(ord(c)<32 for c in self.project_name):
            raise ValueError('项目名称不能包含换行或控制字符')
        if self.input_mode == 'text':
            if not self.story_text.strip() or self.asset_id or self.asset_ids or self.product_selections or self.reference_video_ids:
                raise ValueError('文生剧本只接受故事文本，不混入图片或视频')
        elif self.story_text:raise ValueError('图片或视频模式不混入故事文本，请将创作要求填入统一提示词')
        elif self.input_mode=='image':
            if sum(bool(v) for v in (self.asset_id,self.asset_ids,self.product_selections))!=1 or self.reference_video_ids:
                raise ValueError('图片模式请选择单图、多图或产品图库中的一种来源')
            if len(self.product_selections)>1 and self.product_mode=='single':raise ValueError('多产品需明确选择分别生成或组合营销')
        else:
            if bool(self.asset_id)==bool(self.reference_video_ids) or self.asset_ids or self.product_selections:
                raise ValueError('视频模式请选择一份上传视频或参考视频库，不混入其他素材')
            if self.video_creation_mode=='style' and not self.new_theme.strip():raise ValueError('风格创作需要填写新的主题、故事或产品')
        if len(set(self.asset_ids))!=len(self.asset_ids) or len(set(self.reference_video_ids))!=len(self.reference_video_ids):raise ValueError('输入素材不能重复')
        if self.primary_reference_video_id and self.primary_reference_video_id not in self.reference_video_ids:raise ValueError('主参考必须属于本次所选视频')
        if self.input_mode!='video' and (self.style_profile_id or self.primary_reference_video_id or self.new_theme):raise ValueError('视频风格设置只能用于视频模式')
        if self.allow_paid_fallback and not self.allow_cloud_costs:
            raise ValueError('允许付费回退也需要允许本次云端费用')
        return self


class ReferenceTarget(Strict):
    asset_id: str = Field(min_length=1, max_length=64)
    target_kind: Literal['character', 'scene']
    target_key: str = Field(min_length=1, max_length=160)
    purpose: str = Field(default='identity_front', min_length=1, max_length=60)


class ApproveScreenplay(Strict):
    expected_revision: int = Field(ge=1)
    document_id: str = Field(min_length=1, max_length=64)
    approve: bool = False
    create_project: bool = False
    reference_targets: list[ReferenceTarget] = Field(default_factory=list, max_length=20)


class ReviseScreenplay(Strict):
    expected_revision: int = Field(ge=1)
    creative_prompt: str | None = Field(default=None, min_length=1, max_length=16000)
    instruction: str = Field(min_length=1, max_length=16000)
    scope: Literal['all', 'scene', 'shot'] = 'all'
    target_key: str | None = Field(default=None, max_length=160)
    idempotency_key: str = Field(min_length=8, max_length=120)
    allow_cloud_costs: bool = False
    allow_paid_fallback: bool = False

    @model_validator(mode='after')
    def coherent(self):
        if not self.instruction.strip():raise ValueError('请填写具体修改要求')
        if self.scope != 'all' and not self.target_key:raise ValueError('局部修改需要明确场景或分镜编号')
        if self.scope == 'all' and self.target_key:raise ValueError('整份重写不接受局部编号')
        if self.allow_paid_fallback and not self.allow_cloud_costs:raise ValueError('付费回退需要本次云端费用授权')
        return self


class ExportRetry(Strict):
    expected_revision: int = Field(ge=1)


class RetryScreenplay(Strict):
    expected_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=120)
    allow_cloud_costs: bool = False
    allow_paid_fallback: bool = False
    confirm_unknown_retry: bool = False

    @model_validator(mode='after')
    def costs(self):
        if self.allow_paid_fallback and not self.allow_cloud_costs:raise ValueError('付费回退需要本次云端费用授权')
        return self


class CancelScreenplay(Strict):
    expected_revision: int = Field(ge=1)


class ResumeScreenplay(Strict):
    expected_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=120)
    allow_cloud_costs: bool = False
    allow_paid_fallback: bool = False
    confirm_unknown_retry: bool = False

    @model_validator(mode='after')
    def costs(self):
        if self.allow_paid_fallback and not self.allow_cloud_costs:raise ValueError('付费回退需要本次云端费用授权')
        return self


class BatchCancelScreenplay(Strict):
    ids: list[str] = Field(min_length=1, max_length=200)


class BatchDeleteScreenplay(Strict):
    ids: list[str] = Field(min_length=1, max_length=200)


class BatchResumeScreenplay(Strict):
    ids: list[str] = Field(min_length=1, max_length=200)
    allow_cloud_costs: bool = False
    allow_paid_fallback: bool = False
