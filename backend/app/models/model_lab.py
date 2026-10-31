"""Additive model discovery, verified runtime evidence and isolated experiments."""
from sqlalchemy import Boolean, ForeignKey, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.core.db import Base
from .entities import TimestampMixin, new_id


class ModelDirectory(TimestampMixin, Base):
    __tablename__ = 'model_directories'
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id('modeldir'))
    path: Mapped[str] = mapped_column(Text, unique=True)
    label: Mapped[str] = mapped_column(String(160), default='')
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    scan_json: Mapped[dict] = mapped_column(JSON, default=dict)


class LocalModel(TimestampMixin, Base):
    __tablename__ = 'local_models'
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id('localmodel'))
    directory_id: Mapped[str | None] = mapped_column(ForeignKey('model_directories.id'))
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(24), default='unknown', index=True)
    path: Mapped[str] = mapped_column(Text)
    model_id: Mapped[str] = mapped_column(Text)
    service_id: Mapped[str | None] = mapped_column(ForeignKey('ai_services.id'))
    workflow_path: Mapped[str | None] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64))
    scan_json: Mapped[dict] = mapped_column(JSON, default=dict)
    verification_json: Mapped[dict] = mapped_column(JSON, default=dict)


class ModelExperiment(TimestampMixin, Base):
    __tablename__ = 'model_experiments'
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id('experiment'))
    name: Mapped[str] = mapped_column(String(160))
    mode: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(32), default='queued', index=True)
    source_shot_id: Mapped[str] = mapped_column(ForeignKey('storyboard_shots.id'))
    test_project_id: Mapped[str] = mapped_column(ForeignKey('projects.id'))
    test_shot_id: Mapped[str] = mapped_column(ForeignKey('storyboard_shots.id'))
    snapshot_json: Mapped[dict] = mapped_column(JSON, default=dict)
    cases_json: Mapped[list] = mapped_column(JSON, default=list)
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text)


class ModelLabDefault(TimestampMixin, Base):
    __tablename__ = 'model_lab_defaults'
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default='video_generation')
    configuration_json: Mapped[dict] = mapped_column(JSON, default=dict)
