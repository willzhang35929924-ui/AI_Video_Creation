"""Serialize local queue-check/free/upload/submit transactions per server loop."""
import asyncio
import functools
import weakref
import errno
import json
import logging
import os
import time
from pathlib import Path
import httpx
from app.core.config import PROJECT_ROOT
_locks=weakref.WeakKeyDictionary()
_last_health_warning=0


def _connection_refused(error):
    pending=[error];seen=set()
    while pending:
        item=pending.pop()
        if id(item) in seen:continue
        seen.add(id(item))
        if isinstance(item,OSError) and (getattr(item,'errno',None) in {errno.ECONNREFUSED,10061} or getattr(item,'winerror',None)==10061):return True
        pending.extend(child for child in (getattr(item,'__cause__',None),getattr(item,'__context__',None),*getattr(item,'args',())) if isinstance(child,BaseException))
    return False


async def prepare_local_llm_gpu(settings):
    """Conservatively verify Comfy release before local-model work."""
    from app.services.video_quality import confirm_gpu_release
    if os.getenv('VIDEO_FACTORY_CONTAINER') != '1':
        return await confirm_gpu_release(settings)
    if os.getenv('LOCAL_LLM_GPU_INDEPENDENT', '').lower() in {'1','true','yes','on'}:
        return {'state':'independent_gpu_declared'}
    try:
        async with httpx.AsyncClient(timeout=3, trust_env=False, follow_redirects=False) as client:
            response=await client.get(settings.comfyui_base_url.rstrip('/') + '/queue')
            response.raise_for_status()
    except httpx.ConnectError as exc:
        if _connection_refused(exc):
            return {'state':'comfy_connection_refused'}
        raise
    return await confirm_gpu_release(settings)


def _uncertain_model_health(reason):
    global _last_health_warning
    current=time.monotonic()
    if current-_last_health_warning>=30:
        logging.getLogger(__name__).warning('Local GPU dispatch deferred: fixed model health is uncertain (%s); waiting for a fresh check',reason)
        _last_health_warning=current
    return True


def _managed_screenplay_process_state():
    """Return True/False when the workstation process registry proves state.

    The local Qwen adapter is started only through screenplay_runtime, which
    records its PID in the same project-scoped registry as the dev services.
    A missing/unreadable/mismatched registry is deliberately unknown.
    """
    registry_path = Path(PROJECT_ROOT) / 'data' / 'pids' / 'dev-processes.json'
    try:
        document = json.loads(registry_path.read_text(encoding='utf-8-sig'))
        recorded_root = Path(document.get('projectRoot', '')).resolve()
        project_root = Path(PROJECT_ROOT).resolve()
        if (os.path.normcase(str(recorded_root)) != os.path.normcase(str(project_root)) or
                not isinstance(document.get('processes'), list)):
            return None
    except (OSError, ValueError, TypeError):
        return None

    entries = [item for item in document['processes']
               if isinstance(item, dict) and item.get('kind') == 'screenplay_model']
    if not entries:
        return False
    for entry in entries:
        try:
            pid = int(entry['pid'])
            working_directory = Path(entry['workingDirectory']).resolve()
            if pid <= 0 or os.path.normcase(str(working_directory)) != os.path.normcase(str(project_root)):
                return None
        except (KeyError, ValueError, TypeError, OSError):
            return None
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            # A recorded PID which no longer exists is a stale record.
            continue
        except PermissionError:
            # The process exists but cannot be inspected by this account.
            return True
        except OSError as exc:
            if getattr(exc, 'winerror', None) in {87, 1168} or exc.errno == errno.ESRCH:
                continue
            return None
        return True
    return False


async def owned_model_status():
    """Return an explainable idle/busy/unknown reading for the fixed local model."""
    if os.getenv('VIDEO_FACTORY_CONTAINER') == '1':
        from app.services.screenplay_runtime import factory_base_url, factory_health_matches
        try:
            base = factory_base_url(required=False)
        except Exception:
            return {'state':'unknown','reason':'local_factory_configuration_invalid'}
        if not base:
            return {'state':'idle','reason':'no_managed_container_factory'}
        try:
            async with httpx.AsyncClient(timeout=1,trust_env=False,follow_redirects=False) as client:
                response=await client.get(base[:-3]+'/health')
                response.raise_for_status()
                health=response.json()
            if not factory_health_matches(health):
                return {'state':'unknown','reason':'unexpected_runtime_identity_or_health_schema'}
            busy=health['busy'] or health['loaded']
            return {'state':'busy' if busy else 'idle','reason':'health_busy' if busy else 'health_idle'}
        except httpx.ConnectError as exc:
            if _connection_refused(exc):
                return {'state':'idle','reason':'runtime_port_refused'}
            return {'state':'unknown','reason':'health_connect_error'}
        except Exception:
            return {'state':'unknown','reason':'health_unreachable_or_invalid'}
    try:
        async with httpx.AsyncClient(timeout=1,trust_env=False) as client:
            response=await client.get('http://127.0.0.1:8771/health')
            response.raise_for_status()
            health=response.json()
            if (not isinstance(health,dict) or health.get('service')!='video-factory-screenplay-qwen-v1' or
                not isinstance(health.get('workspace'),str) or
                health['workspace'].replace('\\','/').casefold()!=str(PROJECT_ROOT).replace('\\','/').casefold() or
                not isinstance(health.get('busy'),bool) or not isinstance(health.get('loaded'),bool)):
                return {'state':'unknown','reason':'unexpected_runtime_identity_or_health_schema'}
            # The local adapter releases its model after each request, so a
            # loaded model remains a GPU occupant until both flags are false.
            busy=health['busy'] or health['loaded']
            return {'state':'busy' if busy else 'idle','reason':'health_busy' if busy else 'health_idle'}
    except httpx.ConnectError as exc:
        if _connection_refused(exc):
            return {'state':'idle','reason':'runtime_port_refused'}
        reason=type(exc).__name__
    except httpx.TimeoutException as exc:
        reason=type(exc).__name__
    except Exception as exc:
        reason=type(exc).__name__

    process_state = _managed_screenplay_process_state()
    if process_state is False:
        return {'state':'idle','reason':'health_unreachable_and_no_managed_runtime_process'}
    if process_state is True:
        return {'state':'unknown','reason':'health_unreachable_while_managed_runtime_process_is_alive'}
    return {'state':'unknown','reason':'health_unreachable_and_runtime_process_state_unverified'}


async def owned_model_busy():
    """Read only our fixed local runtime; isolated tests replace this boundary."""
    # Unknown remains fail-closed only when the local process registry cannot
    # prove the Qwen worker is absent. This prevents both GPU overlap and an
    # infinite false-busy queue when loopback health probes time out.
    status=await owned_model_status()
    if status['state']=='unknown':return _uncertain_model_health(status['reason'])
    return status['state']=='busy'


def gpu_submission(function):
    @functools.wraps(function)
    async def wrapped(*args,**kwargs):
        loop=asyncio.get_running_loop();lock=_locks.setdefault(loop,asyncio.Lock())
        async with lock:
            if await owned_model_busy():return None
            return await function(*args,**kwargs)
    return wrapped
