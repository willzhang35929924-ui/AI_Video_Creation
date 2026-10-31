"""Additive screenplay authoring records; existing project tables stay authoritative."""
from sqlalchemy import ForeignKey, Integer, JSON, String, Text, Boolean, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.core.db import Base
from .entities import TimestampMixin, new_id


class ScreenplayInputAsset(TimestampMixin, Base):
    __tablename__ = 'screenplay_input_assets'
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda:new_id('spinput'))
    input_mode: Mapped[str] = mapped_column(String(12))
    filename: Mapped[str] = mapped_column(String(240))
    relative_path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    source_note: Mapped[str] = mapped_column(Text, default='')


class ScreenplayGenerationJob(TimestampMixin, Base):
    __tablename__ = 'screenplay_generation_jobs'
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda:new_id('spjob'))
    idempotency_key: Mapped[str] = mapped_column(String(120), unique=True)
    request_sha256: Mapped[str] = mapped_column(String(64))
    project_name: Mapped[str] = mapped_column(String(200))
    input_mode: Mapped[str] = mapped_column(String(12))
    creative_prompt: Mapped[str] = mapped_column(Text)
    story_text: Mapped[str] = mapped_column(Text, default='')
    input_asset_id: Mapped[str | None] = mapped_column(ForeignKey('screenplay_input_assets.id'))
    parent_job_id: Mapped[str | None] = mapped_column(ForeignKey('screenplay_generation_jobs.id'))
    scope: Mapped[str] = mapped_column(String(12), default='all')
    target_key: Mapped[str | None] = mapped_column(String(160))
    instruction: Mapped[str] = mapped_column(Text, default='')
    status: Mapped[str] = mapped_column(String(32), default='queued', index=True)
    stage: Mapped[str] = mapped_column(String(32), default='queued')
    revision: Mapped[int] = mapped_column(Integer, default=1)
    capability_test: Mapped[bool] = mapped_column(Boolean, default=False)
    snapshot_json: Mapped[dict] = mapped_column(JSON, default=dict)
    screenplay_json: Mapped[dict] = mapped_column(JSON, default=dict)
    receipt_json: Mapped[dict] = mapped_column(JSON, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text)
    review_state: Mapped[str] = mapped_column(String(24), default='pending')
    project_id: Mapped[str | None] = mapped_column(ForeignKey('projects.id'), unique=True)
    word_import_id: Mapped[str | None] = mapped_column(ForeignKey('word_imports.id'), unique=True)


class ScreenplayDocument(TimestampMixin, Base):
    __tablename__ = 'screenplay_documents'
    __table_args__ = (UniqueConstraint('job_id', 'version'),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda:new_id('spdoc'))
    job_id: Mapped[str] = mapped_column(ForeignKey('screenplay_generation_jobs.id'), index=True)
    version: Mapped[int] = mapped_column(Integer)
    relative_path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    screenplay_sha256: Mapped[str] = mapped_column(String(64))
    versions_json: Mapped[dict] = mapped_column(JSON, default=dict)
    review_state: Mapped[str] = mapped_column(String(24), default='pending')


class ScreenplayProjectNameClaim(TimestampMixin, Base):
    __tablename__ = 'screenplay_project_name_claims'
    name_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_name: Mapped[str] = mapped_column(String(200))
    job_id: Mapped[str] = mapped_column(ForeignKey('screenplay_generation_jobs.id'), unique=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey('projects.id'), unique=True)

    @property
    def id(self):
        # Existing generic content ownership uses obj.id for any FK child.
        return self.name_key
