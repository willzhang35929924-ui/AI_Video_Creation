"""CPU application startup checks; external model verification stays explicit."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import select, text
from sqlalchemy.engine import make_url

from app.core.config import PROJECT_ROOT
from app.core.errors import AppError
from app.core.secrets import ensure_container_secret_key, _container_cipher
from app.core.trusted_service_origins import configured_local_origins


def _write_probe(directory):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    try:
        with tempfile.TemporaryFile(dir=directory) as probe:
            probe.write(b'factory-startup-check');probe.flush()
    except OSError as error:
        raise AppError(503,'container_storage_unwritable','应用持久化目录不可写，请检查Docker数据卷所有者/权限；未清空数据') from error


def _tool_version(executable):
    actual=shutil.which(executable)
    if not actual:raise AppError(503,'container_media_tool_missing','FFmpeg/FFprobe缺失，请重新构建官方后端镜像或核对工具路径')
    try:
        result=subprocess.run([actual,'-version'],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=8,shell=False)
        if result.returncode:raise ValueError('media executable returned nonzero')
    except (OSError,ValueError,subprocess.SubprocessError) as error:
        raise AppError(503,'container_media_tool_failed','FFmpeg/FFprobe启动检查失败；不会将不可执行工具标记可用') from error
    return result.stdout.splitlines()[0] if result.stdout else 'version command succeeded'


def validate_container_startup(settings):
    if not settings.container_mode:return {'mode':'native','container_checks':'not_required'}
    if os.name=='posix' and os.geteuid()==0:
        raise AppError(503,'container_root_user','后端容器必须以非root用户运行；请使用默认UID10001')
    if settings.seed_demo_data or settings.h3_demo_mode:
        raise AppError(503,'container_demo_disabled','纯净容器禁止初始化演示业务记录或使用Demo供应商，请设置SEED_DEMO_DATA=false和H3_DEMO_MODE=false')
    url=make_url(settings.database_url)
    if url.get_backend_name()!='sqlite' or not url.database or url.database==':memory:':
        raise AppError(503,'container_database_unsupported','此部署使用单实例持久化SQLite；不能改成内存库或未经迁移验证的其他数据库')
    database=Path(url.database)
    if not database.is_absolute():raise AppError(503,'container_database_path','SQLite必须位于已挂载的绝对数据路径')
    marker=database.parent/'RESTORE_INCOMPLETE'
    if marker.exists() or marker.is_symlink():
        raise AppError(503,'container_restore_incomplete','数据卷恢复尚未完成；请保留原始备份，按恢复教程在空数据卷重新恢复，禁止启动或迁移部分数据')
    for directory in [database.parent,settings.media_root,PROJECT_ROOT/'logs',PROJECT_ROOT/'reports']:_write_probe(directory)
    # Keep temporary uploads and application home on the writable data volume.
    for variable in ('TMPDIR', 'HOME'):
        directory = os.environ.get(variable)
        if directory:
            _write_probe(directory)
    origins=configured_local_origins()
    secret=ensure_container_secret_key(settings.database_url)
    return {'mode':'container','database':'persistent_sqlite','queue':'in_process_single_uvicorn_worker',
            'private_key':secret,'trusted_local_origins':sorted(origins),
            'tools':{'ffmpeg':_tool_version(settings.ffmpeg_path),'ffprobe':_tool_version(settings.ffprobe_path)}}


def runtime_health(application):
    """Application liveness/readiness, not a fabricated AI inference smoke test."""
    from app.models import AIService
    settings=application.state.settings;fatal=[]
    result={'status':'ok','application_ready':True,'deployment_mode':'container' if settings.container_mode else 'native',
            'queue':{'implementation':'SQLite durable jobs + in-process asyncio workers','processes_required':1},
            'checks':{},'external_services':{'scope':'configuration only; no inference or provider request is issued by health checks'}}
    checks=result['checks']
    try:
        with application.state.session_factory() as session:
            session.execute(text('SELECT 1'))
            checks['database']='connected'
            if settings.app_env=='test':checks['schema']='isolated_test_database'
            else:
                expected=ScriptDirectory.from_config(Config(str(PROJECT_ROOT/'backend/alembic.ini'))).get_current_head()
                actual=session.scalar(text('SELECT version_num FROM alembic_version'))
                checks['schema']={'actual':actual,'expected':expected}
                if actual!=expected:fatal.append('schema_version_mismatch')
            services=list(session.scalars(select(AIService).where(AIService.enabled.is_(True))))
            modules={service.module for service in services}
            required={'language_processing','character_generation','video_generation'}
            result['external_services'].update(configured_modules=sorted(modules),
                missing_for_ai_generation=sorted(required-modules),capability_status='not_probed_by_application_health')
    except Exception:
        checks['database']='unavailable';fatal.append('database_or_schema_unavailable')
    worker_tasks=getattr(application.state,'worker_tasks',{})
    checks['workers']={name:'cancelled' if task.cancelled() else 'stopped' if task.done() else 'running' for name,task in worker_tasks.items()}
    result['queue']['enabled']=not settings.h3_demo_mode
    if not settings.h3_demo_mode and (not worker_tasks or any(task.done() for task in worker_tasks.values())):fatal.append('application_worker_stopped')
    directories={'media':settings.media_root,'logs':PROJECT_ROOT/'logs','reports':PROJECT_ROOT/'reports'}
    checks['storage']={name:bool(Path(path).is_dir() and os.access(path,os.W_OK)) for name,path in directories.items()}
    if settings.container_mode and not all(checks['storage'].values()):fatal.append('persistent_storage_unwritable')
    checks['tools']={name:bool(shutil.which(value)) for name,value in [('ffmpeg',settings.ffmpeg_path),('ffprobe',settings.ffprobe_path)]}
    if settings.container_mode:
        if not all(checks['tools'].values()):fatal.append('media_tools_unavailable')
        try:_container_cipher();checks['secret_store']='encrypted_private_key_available'
        except AppError as error:checks['secret_store']=error.code;fatal.append(error.code)
    else:checks['secret_store']='platform_specific; credential test occurs on explicit configuration'
    missing=result['external_services'].get('missing_for_ai_generation',[])
    result.update(status='unhealthy' if fatal else 'degraded' if missing or settings.h3_demo_mode else 'ok',
                  application_ready=not fatal,errors=fatal)
    return result
