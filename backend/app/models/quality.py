from sqlalchemy import ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.core.db import Base
from .entities import TimestampMixin, new_id

class QualityProfile(TimestampMixin, Base):
    __tablename__ = 'quality_profiles'
    __table_args__ = (UniqueConstraint('scope', 'target_id', name='uq_quality_scope_target'),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id('qprofile'))
    scope: Mapped[str] = mapped_column(String(20))
    target_id: Mapped[str] = mapped_column(String(64))
    settings_json: Mapped[dict] = mapped_column(JSON, default=dict)
    revision: Mapped[int] = mapped_column(Integer, default=1)

class QualityRender(TimestampMixin, Base):
    __tablename__ = 'quality_renders'
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id('qrender'))
    source_video_id: Mapped[str] = mapped_column(ForeignKey('video_revisions.id'), index=True)
    video_id: Mapped[str | None] = mapped_column(ForeignKey('video_revisions.id'))
    status: Mapped[str] = mapped_column(String(32), default='queued', index=True)
    snapshot_json: Mapped[dict] = mapped_column(JSON, default=dict)
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)
    relative_path: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)

