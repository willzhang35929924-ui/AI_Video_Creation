"""Verify and release the idle Windows native peer before Context-IR inference."""
from __future__ import annotations

import asyncio
import ctypes
from ctypes import wintypes
from pathlib import PureWindowsPath
import sys
import time

import httpx

from app.core.config import PROJECT_ROOT
from app.services.gpu_gate import _connection_refused


NATIVE_ORIGIN = 'http://127.0.0.1:8189'
MIN_COMMIT_HEADROOM = 2 * 1024**3  # Same emergency floor as the running-job guard.
_last_clean_history = None


class _PerformanceInfo(ctypes.Structure):
    _fields_ = [('cb', wintypes.DWORD)] + [(name, ctypes.c_size_t) for name in (
        'CommitTotal', 'CommitLimit', 'CommitPeak', 'PhysicalTotal', 'PhysicalAvailable',
        'SystemCache', 'KernelTotal', 'KernelPaged', 'KernelNonpaged', 'PageSize',
    )] + [(name, wintypes.DWORD) for name in ('HandleCount', 'ProcessCount', 'ThreadCount')]


def _commit_snapshot():
    if sys.platform != 'win32':
        return None
    info = _PerformanceInfo()
    info.cb = ctypes.sizeof(info)
    if not ctypes.windll.psapi.GetPerformanceInfo(ctypes.byref(info), info.cb) or not info.PageSize:
        return None
    used, limit = info.CommitTotal * info.PageSize, info.CommitLimit * info.PageSize
    if not 0 <= used <= limit or not limit:
        return None
    return {'commit_bytes': used, 'commit_limit_bytes': limit, 'headroom_bytes': limit - used}


def _verified_native_device(stats):
    system = stats.get('system') if isinstance(stats, dict) else None
    argv = system.get('argv') if isinstance(system, dict) else None
    if not isinstance(argv, list) or not argv or not all(isinstance(value, str) for value in argv):
        return None
    if PureWindowsPath(argv[0]) != PureWindowsPath('E:/ComfyUI/main.py'):
        return None
    required = {'--disable-async-offload', '--disable-pinned-memory', '--disable-comfy-compiler'}
    if not required.issubset(argv):
        return None
    expected_paths = {
        '--models-directory': 'E:/Models/H3R2V',
        '--input-directory': str(PROJECT_ROOT / 'data/comfyui_runtime/input'),
        '--output-directory': str(PROJECT_ROOT / 'data/comfyui_runtime/output'),
    }
    pairs = list(zip(argv, argv[1:]))
    for key, value in {'--listen': '127.0.0.1', '--port': '8189'}.items():
        if [right for left, right in pairs if left == key] != [value]:
            return None
    for key, value in expected_paths.items():
        if [PureWindowsPath(right) for left, right in pairs if left == key] != [PureWindowsPath(value)]:
            return None
    devices = stats.get('devices')
    if not isinstance(devices, list):
        return None
    device = next((value for value in devices if isinstance(value, dict) and value.get('type') == 'cuda'), {})
    reserved, total = device.get('torch_vram_total'), device.get('vram_total')
    if type(reserved) is not int or type(total) is not int or not 0 <= reserved <= total or not total:
        return None
    return {'torch_reserved_bytes': reserved, 'vram_total_bytes': total,
            'clean': reserved <= max(64 * 1024**2, total * .01)}


async def _get(client, path):
    response = await client.get(NATIVE_ORIGIN + path, timeout=5)
    response.raise_for_status()
    return response.json()


async def _peer_idle(client):
    queue = await _get(client, '/queue')
    if not isinstance(queue, dict) or not all(isinstance(queue.get(key), list) for key in ('queue_running', 'queue_pending')):
        raise ValueError('Unrecognized native queue response')
    return not (queue['queue_running'] or queue['queue_pending'])


def _result(receipt, state, *, eligible=False):
    current = _commit_snapshot()
    receipt.update(state=state, ready=False, commit_after=current)
    if eligible:
        if current is None:
            receipt['state'] = 'commit_unverified'
        elif current['headroom_bytes'] < MIN_COMMIT_HEADROOM:
            receipt['state'] = 'commit_below_guard'
        else:
            receipt['ready'] = True
    return receipt


async def prepare_context_peer_memory(client, legacy_origin):
    """Call under gpu_submission, after confirming the Context-IR queue idle."""
    global _last_clean_history
    receipt = {'ready': False, 'peer': NATIVE_ORIGIN, 'commit_before': _commit_snapshot(),
               'minimum_headroom_bytes': MIN_COMMIT_HEADROOM, 'release_requested': False, 'observations': []}
    if receipt['commit_before'] is None:
        return _result(receipt, 'commit_unverified')
    if (legacy_origin or '').rstrip('/') != NATIVE_ORIGIN:
        return _result(receipt, 'peer_not_configured', eligible=True)
    try:
        idle = await _peer_idle(client)
    except httpx.ConnectError as exc:
        if _connection_refused(exc):
            _last_clean_history = None
            return _result(receipt, 'peer_absent', eligible=True)
        return _result(receipt, 'peer_unverified')
    except (httpx.HTTPError, ValueError):
        return _result(receipt, 'peer_unverified')
    if not idle:
        return _result(receipt, 'peer_busy')
    try:
        device = _verified_native_device(await _get(client, '/system_stats'))
        if device is None:
            return _result(receipt, 'peer_identity_unverified')
        receipt['device_before'] = device
        history = await _get(client, '/history?max_items=1')
        if not isinstance(history, dict) or len(history) > 1:
            return _result(receipt, 'peer_unverified')
        last_prompt = next(iter(history), None)
        if device['clean'] and (last_prompt is None or last_prompt == _last_clean_history):
            if not await _peer_idle(client):
                return _result(receipt, 'peer_busy')
            return _result(receipt, 'peer_clean', eligible=True)
        if not await _peer_idle(client):
            return _result(receipt, 'peer_busy')
        receipt['release_requested'] = True
        response = await client.post(NATIVE_ORIGIN + '/free', json={'unload_models': True, 'free_memory': True}, timeout=5)
        response.raise_for_status()
        deadline = time.monotonic() + 20
        stable = 0
        while time.monotonic() < deadline:
            if not await _peer_idle(client):
                return _result(receipt, 'peer_busy')
            device = _verified_native_device(await _get(client, '/system_stats'))
            if device is None:
                return _result(receipt, 'peer_identity_unverified')
            receipt['observations'].append({'device': device, 'commit': _commit_snapshot()})
            stable = stable + 1 if device['clean'] else 0
            if stable >= 2:
                if not await _peer_idle(client):
                    return _result(receipt, 'peer_busy')
                _last_clean_history = last_prompt
                return _result(receipt, 'peer_released', eligible=True)
            await asyncio.sleep(.5)
    except (httpx.HTTPError, ValueError):
        return _result(receipt, 'peer_release_unverified' if receipt['release_requested'] else 'peer_unverified')
    return _result(receipt, 'peer_release_unverified')
