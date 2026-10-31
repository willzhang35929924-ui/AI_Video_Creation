"""Versioned production designs, actual visual tasks and gated production runs."""
from sqlalchemy import ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.core.db import Base
from .entities import TimestampMixin,new_id


class ProductionEntity(TimestampMixin,Base):
    __tablename__='production_entities'
    __table_args__=(UniqueConstraint('project_id','logical_id','version'),)
    id:Mapped[str]=mapped_column(String(64),primary_key=True,default=lambda:new_id('design'))
    project_id:Mapped[str]=mapped_column(ForeignKey('projects.id'),index=True)
    character_id:Mapped[str|None]=mapped_column(ForeignKey('characters.id'))
    scene_id:Mapped[str|None]=mapped_column(ForeignKey('scenes.id'))
    logical_id:Mapped[str]=mapped_column(String(100))
    kind:Mapped[str]=mapped_column(String(32))
    version:Mapped[int]=mapped_column(Integer,default=1)
    name:Mapped[str]=mapped_column(String(240))
    status:Mapped[str]=mapped_column(String(32),default='draft')
    spec_json:Mapped[dict]=mapped_column(JSON,default=dict)
    source_json:Mapped[dict]=mapped_column(JSON,default=dict)


class ProductionVisual(TimestampMixin,Base):
    __tablename__='production_visuals'
    id:Mapped[str]=mapped_column(String(64),primary_key=True,default=lambda:new_id('visual'))
    project_id:Mapped[str]=mapped_column(ForeignKey('projects.id'),index=True)
    scene_id:Mapped[str|None]=mapped_column(ForeignKey('scenes.id'))
    shot_id:Mapped[str|None]=mapped_column(ForeignKey('storyboard_shots.id'))
    entity_id:Mapped[str|None]=mapped_column(ForeignKey('production_entities.id'))
    kind:Mapped[str]=mapped_column(String(32))
    status:Mapped[str]=mapped_column(String(32),default='queued',index=True)
    prompt:Mapped[str]=mapped_column(Text)
    snapshot_json:Mapped[dict]=mapped_column(JSON,default=dict)
    provider_job_id:Mapped[str|None]=mapped_column(String(200))
    relative_path:Mapped[str|None]=mapped_column(Text)
    sha256:Mapped[str|None]=mapped_column(String(64))
    review_json:Mapped[dict]=mapped_column(JSON,default=dict)
    error_message:Mapped[str|None]=mapped_column(Text)


class ProductionRun(TimestampMixin,Base):
    __tablename__='production_runs'
    id:Mapped[str]=mapped_column(String(64),primary_key=True,default=lambda:new_id('production'))
    project_id:Mapped[str]=mapped_column(ForeignKey('projects.id'),index=True)
    script_revision_id:Mapped[str]=mapped_column(ForeignKey('script_revisions.id'))
    status:Mapped[str]=mapped_column(String(40),default='waiting_review',index=True)
    snapshot_json:Mapped[dict]=mapped_column(JSON,default=dict)
    steps_json:Mapped[list]=mapped_column(JSON,default=list)
    result_json:Mapped[dict]=mapped_column(JSON,default=dict)
    error_message:Mapped[str|None]=mapped_column(Text)
