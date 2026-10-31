"""Reusable voice recordings; playback and reference permission are separate."""
from sqlalchemy import Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.core.db import Base
from .entities import TimestampMixin, new_id


class VoiceLibraryAsset(TimestampMixin, Base):
    __tablename__ = 'voice_library_assets'
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id('voice'))
    name: Mapped[str] = mapped_column(String(240))
    language: Mapped[str] = mapped_column(String(40), default='und')
    tags_json: Mapped[list] = mapped_column(JSON, default=list)
    source_type: Mapped[str] = mapped_column(String(40), default='uploaded_recording')
    relative_path: Mapped[str] = mapped_column(Text)
    preview_relative_path: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    file_size: Mapped[int] = mapped_column(Integer)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String(24), default='active', index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
