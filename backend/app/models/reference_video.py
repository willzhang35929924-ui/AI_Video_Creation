"""Reusable local source videos and durable analysis/style evidence."""
from sqlalchemy import ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.core.db import Base
from .entities import TimestampMixin, new_id


class ReferenceVideo(TimestampMixin, Base):
    __tablename__ = 'reference_videos'
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id('refvideo'))
    name: Mapped[str] = mapped_column(String(240))
    filename: Mapped[str] = mapped_column(String(240))
    category: Mapped[str] = mapped_column(String(100), default='')
    tags_json: Mapped[list] = mapped_column(JSON, default=list)
    notes: Mapped[str] = mapped_column(Text, default='')
    source_note: Mapped[str] = mapped_column(Text, default='')
    usage_scope: Mapped[str] = mapped_column(Text, default='abstract_style_only')
    relative_path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String(24), default='active', index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)


class VideoAnalysis(TimestampMixin, Base):
    __tablename__ = 'video_analyses'
    __table_args__ = (UniqueConstraint('video_id', 'version', name='uq_video_analysis_version'),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id('vanalysis'))
    video_id: Mapped[str] = mapped_column(ForeignKey('reference_videos.id'), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    idempotency_key: Mapped[str] = mapped_column(String(120), unique=True)
    request_sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default='queued', index=True)
    stage: Mapped[str] = mapped_column(String(32), default='queued')
    options_json: Mapped[dict] = mapped_column(JSON, default=dict)
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)
    receipt_json: Mapped[dict] = mapped_column(JSON, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text)
    retry_of_id: Mapped[str | None] = mapped_column(ForeignKey('video_analyses.id'))
    revision: Mapped[int] = mapped_column(Integer, default=1)


class StyleProfile(TimestampMixin, Base):
    __tablename__ = 'style_profiles'
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id('style'))
    name: Mapped[str] = mapped_column(String(240))
    video_ids_json: Mapped[list] = mapped_column(JSON, default=list)
    analysis_ids_json: Mapped[list] = mapped_column(JSON, default=list)
    primary_video_id: Mapped[str | None] = mapped_column(ForeignKey('reference_videos.id'))
    profile_json: Mapped[dict] = mapped_column(JSON, default=dict)
    user_notes: Mapped[str] = mapped_column(Text, default='')
    version: Mapped[int] = mapped_column(Integer, default=1)
