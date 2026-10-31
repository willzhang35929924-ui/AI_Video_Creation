"""Persistent products and project/shot references over the existing media store."""
from sqlalchemy import ForeignKey,Integer,JSON,String,Text,UniqueConstraint
from sqlalchemy.orm import Mapped,mapped_column
from app.core.db import Base
from .entities import TimestampMixin,new_id


class Product(TimestampMixin,Base):
    __tablename__='products'
    id:Mapped[str]=mapped_column(String(64),primary_key=True,default=lambda:new_id('product'))
    name:Mapped[str]=mapped_column(String(200))
    brand:Mapped[str]=mapped_column(String(200),default='')
    category:Mapped[str]=mapped_column(String(120),default='')
    notes:Mapped[str]=mapped_column(Text,default='')
    selling_points_json:Mapped[list]=mapped_column(JSON,default=list)
    evidence_notes:Mapped[str]=mapped_column(Text,default='')
    target_audience:Mapped[str]=mapped_column(Text,default='')
    cover_image_id:Mapped[str|None]=mapped_column(String(64))
    state:Mapped[str]=mapped_column(String(24),default='active')
    revision:Mapped[int]=mapped_column(Integer,default=1)


class ProductImage(TimestampMixin,Base):
    __tablename__='product_images'
    __table_args__=(UniqueConstraint('product_id','asset_id'),)
    id:Mapped[str]=mapped_column(String(64),primary_key=True,default=lambda:new_id('pimage'))
    product_id:Mapped[str]=mapped_column(ForeignKey('products.id'),index=True)
    asset_id:Mapped[str]=mapped_column(ForeignKey('screenplay_input_assets.id'),index=True)
    purpose:Mapped[str]=mapped_column(String(40),default='other')
    order:Mapped[int]=mapped_column(Integer,default=1)
    notes:Mapped[str]=mapped_column(Text,default='')
    thumbnail_relative_path:Mapped[str|None]=mapped_column(Text)


class ProjectProductBinding(TimestampMixin,Base):
    __tablename__='project_product_bindings'
    __table_args__=(UniqueConstraint('project_id','product_id'),)
    id:Mapped[str]=mapped_column(String(64),primary_key=True,default=lambda:new_id('pproduct'))
    project_id:Mapped[str]=mapped_column(ForeignKey('projects.id'),index=True)
    product_id:Mapped[str]=mapped_column(ForeignKey('products.id'),index=True)
    source_job_id:Mapped[str|None]=mapped_column(ForeignKey('screenplay_generation_jobs.id'))
    snapshot_json:Mapped[dict]=mapped_column(JSON,default=dict)


class ShotProductBinding(TimestampMixin,Base):
    __tablename__='shot_product_bindings'
    __table_args__=(UniqueConstraint('shot_id','product_id'),)
    id:Mapped[str]=mapped_column(String(64),primary_key=True,default=lambda:new_id('sproduct'))
    project_id:Mapped[str]=mapped_column(ForeignKey('projects.id'),index=True)
    shot_id:Mapped[str]=mapped_column(ForeignKey('storyboard_shots.id'),index=True)
    product_id:Mapped[str]=mapped_column(ForeignKey('products.id'),index=True)
    image_ids_json:Mapped[list]=mapped_column(JSON,default=list)
    state_json:Mapped[dict]=mapped_column(JSON,default=dict)
    revision:Mapped[int]=mapped_column(Integer,default=1)


class ProjectProductionPreference(TimestampMixin,Base):
    __tablename__='project_production_preferences'
    id:Mapped[str]=mapped_column(ForeignKey('projects.id'),primary_key=True)
    dialogue_audio_policy:Mapped[str]=mapped_column(String(16),default='auto')
    revision:Mapped[int]=mapped_column(Integer,default=1)
