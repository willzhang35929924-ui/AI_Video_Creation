"""Additive task routing and only the previously absent audio/face processing jobs."""
from sqlalchemy import ForeignKey, Integer, JSON, String, Text, UniqueConstraint, Index, text
from sqlalchemy.orm import Mapped, mapped_column
from app.core.db import Base
from .entities import TimestampMixin, new_id


class RoutePolicy(TimestampMixin, Base):
    __tablename__ = 'route_policies'
    __table_args__ = (UniqueConstraint('scope','target_id','slot',name='uq_route_scope_target_slot'),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda:new_id('route'))
    scope: Mapped[str] = mapped_column(String(20))
    target_id: Mapped[str] = mapped_column(String(64))
    slot: Mapped[str] = mapped_column(String(40))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    settings_json: Mapped[dict] = mapped_column(JSON, default=dict)


class MediaProcessTask(TimestampMixin, Base):
    __tablename__ = 'media_process_tasks'
    __table_args__=(Index('uq_media_live_fingerprint','fingerprint',unique=True,
        sqlite_where=text("status IN ('queued','running','unknown','succeeded')"),
        postgresql_where=text("status IN ('queued','running','unknown','succeeded')")),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda:new_id('mediajob'))
    project_id: Mapped[str] = mapped_column(ForeignKey('projects.id'), index=True)
    shot_id: Mapped[str | None] = mapped_column(ForeignKey('storyboard_shots.id'), index=True)
    kind: Mapped[str] = mapped_column(String(32))  # tts/audio_mix/lip_sync, never an alternate video queue
    slot: Mapped[str] = mapped_column(String(40))
    fingerprint: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default='queued', index=True)
    snapshot_json: Mapped[dict] = mapped_column(JSON, default=dict)
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text)


class AudioAsset(TimestampMixin, Base):
    __tablename__ = 'audio_assets'
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda:new_id('audioasset'))
    project_id: Mapped[str] = mapped_column(ForeignKey('projects.id'), index=True)
    shot_id: Mapped[str | None] = mapped_column(ForeignKey('storyboard_shots.id'), index=True)
    participant_id: Mapped[str | None] = mapped_column(ForeignKey('shot_characters.id'))
    dialogue_line_id: Mapped[str | None] = mapped_column(ForeignKey('dialogue_lines.id'))
    character_version_id: Mapped[str | None] = mapped_column(ForeignKey('character_versions.id'))
    task_id: Mapped[str | None] = mapped_column(ForeignKey('media_process_tasks.id'))
    relative_path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
