from typing import Literal
from pydantic import BaseModel,ConfigDict,Field,field_validator

Purpose=Literal['main','front','back','side','detail','usage','packaging','dimensions','other']
class Strict(BaseModel):model_config=ConfigDict(extra='forbid')
class ProductCreate(Strict):
    name:str=Field(min_length=1,max_length=200)
    brand:str=Field(default='',max_length=200)
    category:str=Field(default='',max_length=120)
    notes:str=Field(default='',max_length=16000)
    selling_points:list[str]=Field(default_factory=list,max_length=50)
    evidence_notes:str=Field(default='',max_length=16000)
    target_audience:str=Field(default='',max_length=4000)
    @field_validator('name')
    @classmethod
    def nonblank(cls,value):
        if not value.strip():raise ValueError('产品名称不能是空白')
        return value
    @field_validator('selling_points')
    @classmethod
    def bounded_claims(cls,values):
        if any(not value.strip() or len(value)>2000 for value in values):raise ValueError('每条卖点需为1至2000字')
        return values
class ProductEdit(Strict):
    expected_revision:int=Field(ge=1)
    name:str|None=Field(default=None,min_length=1,max_length=200)
    brand:str|None=Field(default=None,max_length=200)
    category:str|None=Field(default=None,max_length=120)
    notes:str|None=Field(default=None,max_length=16000)
    selling_points:list[str]|None=Field(default=None,max_length=50)
    evidence_notes:str|None=Field(default=None,max_length=16000)
    target_audience:str|None=Field(default=None,max_length=4000)
    cover_image_id:str|None=None
class ImageEdit(Strict):
    id:str
    purpose:Purpose='other'
    order:int=Field(ge=1,le=1000)
    notes:str=Field(default='',max_length=4000)
class ProductImagesEdit(Strict):
    expected_revision:int=Field(ge=1)
    images:list[ImageEdit]=Field(max_length=32)
    cover_image_id:str|None=None
class ProductSelection(Strict):
    product_id:str=Field(min_length=1,max_length=64)
    image_ids:list[str]=Field(default_factory=list,max_length=8)
class ProductionPreferenceEdit(Strict):
    expected_revision:int=Field(ge=0)
    dialogue_audio_policy:Literal['auto','native','tts','skip']
class ShotProductReferenceEdit(Strict):
    product_id:str
    expected_revision:int=Field(ge=1)
    video_image_ids:list[str]=Field(min_length=1,max_length=8)
    keyframe_image_ids:list[str]=Field(min_length=1,max_length=4)
class ShotProductReferencesEdit(Strict):
    products:list[ShotProductReferenceEdit]=Field(max_length=4)

class ExistingProjectShotProduct(Strict):
    shot_id:str=Field(min_length=1,max_length=64)
    image_ids:list[str]=Field(min_length=1,max_length=4)
    visible_state:str=Field(default='',max_length=2000)
class ExistingProjectProductBind(Strict):
    product_id:str=Field(min_length=1,max_length=64)
    image_ids:list[str]=Field(min_length=1,max_length=8)
    shots:list[ExistingProjectShotProduct]=Field(min_length=1,max_length=100)

class ProjectProductReferenceEdit(Strict):
    product_id:str=Field(min_length=1,max_length=64)
    expected_token:str=Field(min_length=64,max_length=64)
    image_ids:list[str]=Field(default_factory=list,max_length=4)
    shot_ids:list[str]=Field(default_factory=list,max_length=1000)
