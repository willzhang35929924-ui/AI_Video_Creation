from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from .entities import TimestampMixin, new_id, utcnow


class AIService(TimestampMixin, Base):
    __tablename__ = 'ai_services'
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id('svc'))
    module: Mapped[str] = mapped_column(String(40), index=True)
    name: Mapped[str] = mapped_column(String(160))
    provider: Mapped[str] = mapped_column(String(40))
    base_url: Mapped[str] = mapped_column(Text, default='')
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    default_model: Mapped[str] = mapped_column(String(240), default='')
    models_json: Mapped[list] = mapped_column(JSON, default=list)
    settings_json: Mapped[dict] = mapped_column(JSON, default=dict)
    capabilities_json: Mapped[dict] = mapped_column(JSON, default=dict)
    last_test_json: Mapped[dict] = mapped_column(JSON, default=dict)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    encrypted_secret: Mapped[bytes | None] = mapped_column(LargeBinary)
    revision: Mapped[int] = mapped_column(Integer, default=1)


class ProjectServiceBinding(TimestampMixin, Base):
    __tablename__ = 'project_service_bindings'
    __table_args__ = (UniqueConstraint('project_id','module',name='uq_project_module'),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id('binding'))
    project_id: Mapped[str] = mapped_column(ForeignKey('projects.id'), index=True)
    module: Mapped[str] = mapped_column(String(40))
    service_id: Mapped[str] = mapped_column(ForeignKey('ai_services.id'))
    model_id: Mapped[str] = mapped_column(String(240), default='')


class CharacterBatch(TimestampMixin, Base):
    __tablename__ = 'character_batches'
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id('batch'))
    character_id: Mapped[str] = mapped_column(ForeignKey('characters.id'), index=True)
    character_version_id: Mapped[str] = mapped_column(ForeignKey('character_versions.id'))
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default='queued')
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    snapshot_json: Mapped[dict] = mapped_column(JSON)
    contact_sheet_path: Mapped[str | None] = mapped_column(Text)


class CharacterGenerationJob(TimestampMixin, Base):
    __tablename__ = 'character_generation_jobs'
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id('imgjob'))
    batch_id: Mapped[str] = mapped_column(ForeignKey('character_batches.id'), index=True)
    status: Mapped[str] = mapped_column(String(32), default='queued', index=True)
    asset_type: Mapped[str] = mapped_column(String(40))
    prompt: Mapped[str] = mapped_column(Text)
    negative_prompt: Mapped[str] = mapped_column(Text, default='')
    seed: Mapped[int] = mapped_column(Integer)
    snapshot_json: Mapped[dict] = mapped_column(JSON)
    provider_job_id: Mapped[str | None] = mapped_column(String(200))
    result_asset_id: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    log_path: Mapped[str | None] = mapped_column(Text)
    retry_of: Mapped[str | None] = mapped_column(String(64))


class Assembly(TimestampMixin, Base):
    __tablename__ = 'assemblies'
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id('assembly'))
    project_id: Mapped[str] = mapped_column(ForeignKey('projects.id'), index=True)
    name: Mapped[str] = mapped_column(String(200))
    timeline_json: Mapped[dict] = mapped_column(JSON, default=dict)
    revision: Mapped[int] = mapped_column(Integer, default=1)


class AssemblyRender(TimestampMixin, Base):
    __tablename__ = 'assembly_renders'
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id('render'))
    assembly_id: Mapped[str] = mapped_column(ForeignKey('assemblies.id'), index=True)
    version: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(20), default='render')
    status: Mapped[str] = mapped_column(String(32), default='queued', index=True)
    snapshot_json: Mapped[dict] = mapped_column(JSON)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    relative_path: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    log_path: Mapped[str | None] = mapped_column(Text)

