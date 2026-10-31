from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

ReferencePurpose = Literal['identity_front', 'identity_side', 'full_body', 'wardrobe']


class ReferenceSelection(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    asset_id: str = Field(min_length=1, max_length=64)
    purpose: ReferencePurpose = 'identity_front'
    order: int = Field(ge=1, le=4)


class CharacterReferenceSave(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=0)
    references: list[ReferenceSelection] = Field(default_factory=list, max_length=4)

    @model_validator(mode='after')
    def unique_selections(self):
        if len({r.asset_id for r in self.references}) != len(self.references):
            raise ValueError('参考图片不能重复选择')
        if len({r.order for r in self.references}) != len(self.references):
            raise ValueError('参考图片顺序不能重复')
        return self
