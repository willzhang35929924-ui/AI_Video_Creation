"""Project-scoped optional source photos, distinct from approved identity assets."""
from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.core.db import Base
from .entities import TimestampMixin, new_id


class CharacterReferenceSet(TimestampMixin, Base):
    __tablename__ = 'character_reference_sets'
    __table_args__ = (UniqueConstraint('project_id', 'character_id', name='uq_character_reference_set'),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id('charrefset'))
    project_id: Mapped[str] = mapped_column(ForeignKey('projects.id'), index=True)
    character_id: Mapped[str] = mapped_column(ForeignKey('characters.id'), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)


class CharacterReferenceAsset(TimestampMixin, Base):
    __tablename__ = 'character_reference_assets'
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id('charref'))
    project_id: Mapped[str] = mapped_column(ForeignKey('projects.id'), index=True)
    character_id: Mapped[str] = mapped_column(ForeignKey('characters.id'), index=True)
    relative_path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    mime_type: Mapped[str] = mapped_column(String(100))
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    byte_size: Mapped[int] = mapped_column(Integer)
    purpose: Mapped[str] = mapped_column(String(32), default='identity_front')
    order: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String(24), default='draft', index=True)
    source_note: Mapped[str] = mapped_column(Text, default='')
