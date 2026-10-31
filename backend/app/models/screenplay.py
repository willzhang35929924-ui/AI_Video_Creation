"""Additive production relationships; legacy shot columns remain compatible."""
from sqlalchemy import ForeignKey, Integer, JSON, String, Text, Float, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.core.db import Base
from .entities import TimestampMixin, new_id


class Scene(TimestampMixin, Base):
    __tablename__ = 'scenes'
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id('scene'))
    project_id: Mapped[str] = mapped_column(ForeignKey('projects.id'), index=True)
    script_revision_id: Mapped[str | None] = mapped_column(ForeignKey('script_revisions.id'))
    order: Mapped[int] = mapped_column(Integer, default=1)
    name: Mapped[str] = mapped_column(String(240))
    environment_json: Mapped[dict] = mapped_column(JSON, default=dict)
    source_json: Mapped[dict] = mapped_column(JSON, default=dict)


class ShotDetail(TimestampMixin, Base):
    __tablename__ = 'shot_details'
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id('detail'))
    shot_id: Mapped[str] = mapped_column(ForeignKey('storyboard_shots.id'), unique=True)
    scene_id: Mapped[str | None] = mapped_column(ForeignKey('scenes.id'), index=True)
    photography_json: Mapped[dict] = mapped_column(JSON, default=dict)
    source_json: Mapped[dict] = mapped_column(JSON, default=dict)


class ShotCharacter(TimestampMixin, Base):
    __tablename__ = 'shot_characters'
    __table_args__ = (UniqueConstraint('shot_id', 'character_id'),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id('participation'))
    shot_id: Mapped[str] = mapped_column(ForeignKey('storyboard_shots.id'), index=True)
    character_id: Mapped[str] = mapped_column(ForeignKey('characters.id'), index=True)
    character_version_id: Mapped[str | None] = mapped_column(ForeignKey('character_versions.id'))
    order: Mapped[int] = mapped_column(Integer, default=1)
    reference_asset_ids_json: Mapped[list] = mapped_column(JSON, default=list)
    performance_json: Mapped[dict] = mapped_column(JSON, default=dict)
    source_json: Mapped[dict] = mapped_column(JSON, default=dict)


class DialogueLine(TimestampMixin, Base):
    __tablename__ = 'dialogue_lines'
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id('line'))
    shot_id: Mapped[str] = mapped_column(ForeignKey('storyboard_shots.id'), index=True)
    participant_id: Mapped[str] = mapped_column(ForeignKey('shot_characters.id'), index=True)
    order: Mapped[int] = mapped_column(Integer, default=1)
    text: Mapped[str] = mapped_column(Text, default='')
    delivery_json: Mapped[dict] = mapped_column(JSON, default=dict)
    source_json: Mapped[dict] = mapped_column(JSON, default=dict)


class AudioEvent(TimestampMixin, Base):
    __tablename__ = 'audio_events'
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id('audio'))
    scene_id: Mapped[str | None] = mapped_column(ForeignKey('scenes.id'), index=True)
    shot_id: Mapped[str | None] = mapped_column(ForeignKey('storyboard_shots.id'), index=True)
    kind: Mapped[str] = mapped_column(String(30), default='ambience')
    description: Mapped[str] = mapped_column(Text, default='')
    start_seconds: Mapped[float] = mapped_column(Float, default=0)
    end_seconds: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float, default=1)
    relative_path: Mapped[str | None] = mapped_column(Text)
    source_json: Mapped[dict] = mapped_column(JSON, default=dict)


class CharacterAlias(TimestampMixin, Base):
    __tablename__ = 'character_aliases'
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id('alias'))
    character_id: Mapped[str] = mapped_column(ForeignKey('characters.id'), index=True)
    name: Mapped[str] = mapped_column(String(160), index=True)


class WordImport(TimestampMixin, Base):
    __tablename__ = 'word_imports'
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id('import'))
    filename: Mapped[str] = mapped_column(String(240))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default='parsing', index=True)
    relative_path: Mapped[str] = mapped_column(Text)
    blocks_json: Mapped[list] = mapped_column(JSON, default=list)
    preview_json: Mapped[dict] = mapped_column(JSON, default=dict)
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer, default=1)


class ContentTemplate(TimestampMixin, Base):
    __tablename__ = 'content_templates'
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id('template'))
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(40), default='screenplay')
    body: Mapped[str] = mapped_column(Text, default='')


class ManagedFile(TimestampMixin, Base):
    __tablename__ = 'managed_files'
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id('file'))
    relative_path: Mapped[str] = mapped_column(Text, unique=True)
    state: Mapped[str] = mapped_column(String(32), default='active')
    ownership_json: Mapped[list] = mapped_column(JSON, default=list)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class DeletionTask(TimestampMixin, Base):
    __tablename__ = 'deletion_tasks'
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id('delete'))
    status: Mapped[str] = mapped_column(String(40), default='planned', index=True)
    mode: Mapped[str] = mapped_column(String(20))
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    plan_json: Mapped[dict] = mapped_column(JSON, default=dict)
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text)


class ContentNote(TimestampMixin, Base):
    __tablename__ = 'content_notes'
    __table_args__ = (UniqueConstraint('resource', 'record_id'),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id('note'))
    resource: Mapped[str] = mapped_column(String(64))
    record_id: Mapped[str] = mapped_column(String(64))
    label: Mapped[str] = mapped_column(String(240), default='')
    notes: Mapped[str] = mapped_column(Text, default='')
