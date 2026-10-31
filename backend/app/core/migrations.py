"""Versioned startup upgrades, with an online backup before changing SQLite."""
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from sqlalchemy import inspect, text
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from app.core.config import PROJECT_ROOT
from app.core.errors import AppError


def upgrade_database(settings,engine):
    if settings.app_env=='test':return
    config=Config(str(PROJECT_ROOT/'backend/alembic.ini'))
    head=ScriptDirectory.from_config(config).get_current_head()
    tables=inspect(engine).get_table_names()
    with engine.connect() as connection:
        version=connection.scalar(text('SELECT version_num FROM alembic_version')) if 'alembic_version' in tables else None
    if version==head:return
    if tables and version is None:
        raise AppError(503,'unversioned_database','数据库存在历史表但没有迁移版本，请先备份并确认旧结构，不会自动重建或清空')
    if engine.dialect.name=='sqlite' and engine.url.database and Path(engine.url.database).exists():
        db=Path(engine.url.database);out=db.parent/'schema_backups';out.mkdir(exist_ok=True)
        target=out/('before_'+head+'_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')+'.db')
        with sqlite3.connect(db) as src,sqlite3.connect(target) as dst:src.backup(dst)
    config.attributes['database_url']=settings.database_url
    with engine.begin() as connection:
        config.attributes['connection']=connection
        command.upgrade(config,'head')
