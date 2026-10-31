"""Additive durable automation and final-shot decisions; legacy rows stay intact."""
from sqlalchemy import Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.core.db import Base
from .entities import TimestampMixin, new_id


class StudioRun(TimestampMixin, Base):
    __tablename__ = 'studio_runs'
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id('studio'))
    project_id: Mapped[str] = mapped_column(ForeignKey('projects.id'), index=True)
    script_revision_id: Mapped[str | None] = mapped_column(ForeignKey('script_revisions.id'))
    status: Mapped[str] = mapped_column(String(40), default='queued', index=True)
    snapshot_json: Mapped[dict] = mapped_column(JSON, default=dict)
    nodes_json: Mapped[list] = mapped_column(JSON, default=list)
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text)


class StudioDecision(TimestampMixin, Base):
    __tablename__ = 'studio_decisions'
    __table_args__ = (UniqueConstraint('project_id', 'shot_id', name='uq_studio_project_shot'),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id('decision'))
    project_id: Mapped[str] = mapped_column(ForeignKey('projects.id'), index=True)
    shot_id: Mapped[str] = mapped_column(ForeignKey('storyboard_shots.id'), index=True)
    video_id: Mapped[str | None] = mapped_column(ForeignKey('video_revisions.id'))
    action: Mapped[str] = mapped_column(String(24), default='pending')
    position: Mapped[int] = mapped_column(Integer, default=1)
    trim_start: Mapped[float] = mapped_column(Float, default=0)
    trim_end: Mapped[float | None] = mapped_column(Float)
    instruction: Mapped[str] = mapped_column(Text, default='')
    revision: Mapped[int] = mapped_column(Integer, default=1)
