"""Signal-free process-health contracts; no model/service is started or stopped."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.services import gpu_gate
from test_gpu_gate_health import registry, TimeoutClient


def windows_api(*, handle=123, waited=0x102, closed=True):
    return SimpleNamespace(OpenProcess=Mock(return_value=handle),
        WaitForSingleObject=Mock(return_value=waited), CloseHandle=Mock(return_value=closed))


@pytest.mark.parametrize('waited,expected', [(0x102, True), (0, False), (0xffffffff, None)])
def test_windows_wait_queries_alive_exited_or_failed_without_terminate_rights(waited, expected):
    api = windows_api(waited=waited)
    assert gpu_gate._windows_process_state(12345, native_api=api, last_error=lambda: 0) is expected
    api.OpenProcess.assert_called_once_with(0x00100000, False, 12345)
    api.WaitForSingleObject.assert_called_once_with(123, 0)
    api.CloseHandle.assert_called_once_with(123)
    assert not api.OpenProcess.call_args.args[0] & 0x0001  # PROCESS_TERMINATE never requested


@pytest.mark.parametrize('error,expected', [(87, False), (1168, False), (5, None), (6, None)])
def test_windows_missing_access_denied_and_other_open_errors_are_not_guessed(error, expected):
    api = windows_api(handle=0)
    assert gpu_gate._windows_process_state(12345, native_api=api, last_error=lambda: error) is expected
    api.WaitForSingleObject.assert_not_called()
    api.CloseHandle.assert_not_called()


def test_windows_api_exception_still_closes_handle_and_stays_unknown():
    api = windows_api()
    api.WaitForSingleObject.side_effect = OSError('isolated API failure')
    assert gpu_gate._windows_process_state(12345, native_api=api, last_error=lambda: 0) is None
    api.CloseHandle.assert_called_once_with(123)


def test_windows_close_failure_stays_conservative():
    api = windows_api(closed=False)
    assert gpu_gate._windows_process_state(12345, native_api=api, last_error=lambda: 0) is None


def test_windows_library_failure_is_unknown_without_signalling(monkeypatch):
    import ctypes
    def unavailable(*args, **kwargs):
        raise OSError('isolated loader failure')
    monkeypatch.setattr(ctypes, 'WinDLL', unavailable, raising=False)
    assert gpu_gate._windows_process_state(12345) is None


@pytest.mark.parametrize('pid', [0, -1, True, 1.5, '123', 2**32])
def test_invalid_pid_never_opens_a_handle_or_calls_kill(pid, monkeypatch):
    api = windows_api()
    assert gpu_gate._windows_process_state(pid, native_api=api, last_error=lambda: 0) is None
    api.OpenProcess.assert_not_called()
    forbidden = Mock(side_effect=AssertionError('must never signal a PID during health checking'))
    monkeypatch.setattr(gpu_gate, 'os', SimpleNamespace(name='nt', kill=forbidden))
    assert gpu_gate._process_state(pid) is None
    forbidden.assert_not_called()


def test_windows_dispatch_never_uses_posix_zero_signal(monkeypatch):
    forbidden = Mock(side_effect=AssertionError('Windows zero would emit CTRL_C_EVENT'))
    monkeypatch.setattr(gpu_gate, 'os', SimpleNamespace(name='nt', kill=forbidden))
    native = Mock(return_value=True)
    monkeypatch.setattr(gpu_gate, '_windows_process_state', native)
    assert gpu_gate._process_state(12345) is True
    native.assert_called_once_with(12345)
    forbidden.assert_not_called()


@pytest.mark.parametrize('pid', [0, -1, True, 1.5, 'not-a-pid', 2**32])
def test_invalid_registry_pid_is_unknown_without_a_probe(tmp_path, monkeypatch, pid):
    registry(tmp_path, [{'kind':'screenplay_model','pid':pid,'workingDirectory':str(tmp_path)}])
    monkeypatch.setattr(gpu_gate, 'PROJECT_ROOT', tmp_path)
    probe = Mock(side_effect=AssertionError('invalid registration cannot trigger any process API'))
    monkeypatch.setattr(gpu_gate, '_process_state', probe)
    assert gpu_gate._managed_screenplay_process_state() is None
    probe.assert_not_called()


def test_access_denied_or_unverified_registry_remains_gpu_blocked(tmp_path, monkeypatch, isolate_owned_model_health):
    registry(tmp_path, [{'kind':'screenplay_model','pid':12345,'workingDirectory':str(tmp_path)}])
    monkeypatch.setattr(gpu_gate, 'PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(gpu_gate, '_process_state', lambda _: None)
    monkeypatch.setattr(gpu_gate.httpx, 'AsyncClient', TimeoutClient)
    status = asyncio.run(gpu_gate.owned_model_status())
    assert status == {'state':'unknown','reason':'health_unreachable_and_runtime_process_state_unverified'}
    # The session's default fixture isolates GPU health. Call its returned real
    # policy against the above fully mocked boundary to verify fail-closed.
    assert asyncio.run(isolate_owned_model_health()) is True


def test_posix_permission_error_is_unknown_instead_of_absent(monkeypatch):
    probe = Mock(side_effect=PermissionError('isolated permission denial'))
    monkeypatch.setattr(gpu_gate, 'os', SimpleNamespace(name='posix', kill=probe))
    assert gpu_gate._process_state(12345) is None
    probe.assert_called_once_with(12345, 0)
