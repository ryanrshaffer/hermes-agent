"""Sidecar lifecycle tests: orphan reaping and parent-death wiring.

A hard gateway exit used to leave the detached Node sidecar squatting the
loopback port with a token the next gateway run doesn't know — every
replacement spawn then died on EADDRINUSE. These tests cover the startup
reaper (`_reap_stale_sidecar`) and the stdin-pipe lifetime binding, without
spawning Node or binding ports.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from typing import Any, Dict, List, Tuple

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.photon import adapter as photon_adapter
from plugins.platforms.photon.adapter import PhotonAdapter


def _make_adapter(monkeypatch: pytest.MonkeyPatch) -> PhotonAdapter:
    monkeypatch.setenv("PHOTON_PROJECT_ID", "test-project-id")
    monkeypatch.setenv("PHOTON_PROJECT_SECRET", "test-project-secret")
    cfg = PlatformConfig(enabled=True, token="", extra={})
    return PhotonAdapter(cfg)


class _ProbeClient:
    """Fake httpx.AsyncClient whose /healthz probe behavior is injectable."""

    connects = True

    def __init__(self, *a: Any, **k: Any) -> None:
        pass

    async def __aenter__(self) -> "_ProbeClient":
        return self

    async def __aexit__(self, *a: Any) -> bool:
        return False

    async def post(self, *a: Any, **k: Any) -> Any:
        if not self.connects:
            raise photon_adapter.httpx.ConnectError("connection refused")

        class _Resp:
            status_code = 401  # orphan with a different token

        return _Resp()


class _ConnectClient:
    """Minimal connect-time client that records deterministic closure."""

    instances: List["_ConnectClient"] = []

    def __init__(self, *a: Any, **k: Any) -> None:
        self.closed = False
        self.instances.append(self)

    async def aclose(self) -> None:
        self.closed = True


class _LifecycleClient:
    """Fake sidecar client used to verify fatal-path cleanup completion."""

    def __init__(self) -> None:
        self.closed = False
        self.shutdown_requested = False

    async def post(self, *args: Any, **kwargs: Any) -> None:
        self.shutdown_requested = True

    async def aclose(self) -> None:
        self.closed = True


# _reap_stale_sidecar returns immediately on win32 (it shells out to lsof/ps
# and sends POSIX signals; orphaning itself only happens with
# start_new_session, a POSIX-only spawn mode), so the reap flow these tests
# drive is unreachable there — and signal.SIGKILL doesn't exist on Windows.
_posix_reap_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="sidecar reaping is a POSIX-only code path (no-op on win32)",
)


def _capture_kills(monkeypatch: pytest.MonkeyPatch) -> List[Tuple[int, int]]:
    kills: List[Tuple[int, int]] = []

    def _fake_kill(pid: int, sig: int) -> None:
        kills.append((pid, sig))

    monkeypatch.setattr(photon_adapter.os, "kill", _fake_kill)
    return kills


@pytest.mark.asyncio
async def test_reap_noop_when_port_free(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _make_adapter(monkeypatch)

    class _Refused(_ProbeClient):
        connects = False

    monkeypatch.setattr(photon_adapter.httpx, "AsyncClient", _Refused)
    kills = _capture_kills(monkeypatch)

    await adapter._reap_stale_sidecar()

    assert kills == []


@_posix_reap_only
@pytest.mark.asyncio
async def test_reap_kills_verified_orphan(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _make_adapter(monkeypatch)
    monkeypatch.setattr(photon_adapter.httpx, "AsyncClient", _ProbeClient)
    monkeypatch.setattr(adapter, "_find_listener_pids", lambda port: [4242])
    monkeypatch.setattr(adapter, "_pid_is_sidecar", lambda pid: True)
    # Dies promptly on SIGTERM — no escalation expected.
    monkeypatch.setattr(adapter, "_pid_alive", lambda pid: False)
    kills = _capture_kills(monkeypatch)

    await adapter._reap_stale_sidecar()

    assert kills == [(4242, photon_adapter.signal.SIGTERM)]


@_posix_reap_only
@pytest.mark.asyncio
async def test_reap_escalates_to_sigkill(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _make_adapter(monkeypatch)
    monkeypatch.setattr(photon_adapter.httpx, "AsyncClient", _ProbeClient)
    monkeypatch.setattr(adapter, "_find_listener_pids", lambda port: [4242])
    monkeypatch.setattr(adapter, "_pid_is_sidecar", lambda pid: True)
    monkeypatch.setattr(adapter, "_pid_alive", lambda pid: True)  # ignores TERM
    # No clock fakery (logging also calls time.time, which makes a fake clock
    # fragile) — this test rides out the real 3s SIGTERM grace window.
    kills = _capture_kills(monkeypatch)

    await adapter._reap_stale_sidecar()

    assert (4242, photon_adapter.signal.SIGTERM) in kills
    assert (4242, photon_adapter.signal.SIGKILL) in kills


@_posix_reap_only
@pytest.mark.asyncio
async def test_reap_raises_for_foreign_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never signal a process whose command line isn't our sidecar."""
    adapter = _make_adapter(monkeypatch)
    monkeypatch.setattr(photon_adapter.httpx, "AsyncClient", _ProbeClient)
    monkeypatch.setattr(adapter, "_find_listener_pids", lambda port: [777])
    monkeypatch.setattr(adapter, "_pid_is_sidecar", lambda pid: False)
    kills = _capture_kills(monkeypatch)

    with pytest.raises(RuntimeError, match="in use by another process"):
        await adapter._reap_stale_sidecar()

    assert kills == []


@pytest.mark.asyncio
async def test_start_sidecar_spawns_with_stdin_pipe(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The spawn must hold a stdin pipe and enable the sidecar's EOF watch."""
    adapter = _make_adapter(monkeypatch)

    async def _no_reap() -> None:
        pass

    monkeypatch.setattr(adapter, "_reap_stale_sidecar", _no_reap)
    (tmp_path / "node_modules").mkdir()
    monkeypatch.setattr(photon_adapter, "_SIDECAR_DIR", tmp_path)

    spawned: Dict[str, Any] = {}

    class _FakeProc:
        pid = 999
        stdout = None
        stdin = None

        @staticmethod
        def poll() -> None:
            return None

    def _fake_popen(cmd: List[str], **kwargs: Any) -> _FakeProc:
        spawned["cmd"] = cmd
        spawned["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr(photon_adapter.subprocess, "Popen", _fake_popen)

    thread_calls: List[Tuple[Any, Tuple[Any, ...], Dict[str, Any]]] = []

    async def _to_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
        thread_calls.append((func, args, kwargs))
        return subprocess.CompletedProcess(args[0], 0, "", "")

    monkeypatch.setattr(photon_adapter.asyncio, "to_thread", _to_thread)

    clock = {"now": 0.0}
    real_sleep = photon_adapter.asyncio.sleep

    async def _advance_clock(delay: float) -> None:
        clock["now"] += delay
        await real_sleep(0)

    monkeypatch.setattr(photon_adapter.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(photon_adapter.asyncio, "sleep", _advance_clock)

    class _HealthyClient(_ProbeClient):
        async def post(self, *a: Any, **k: Any) -> Any:
            if clock["now"] < 16.0:
                raise photon_adapter.httpx.ConnectError("connection refused")

            class _Resp:
                status_code = 200

            return _Resp()

    monkeypatch.setattr(photon_adapter.httpx, "AsyncClient", _HealthyClient)

    await adapter._start_sidecar()

    kwargs = spawned["kwargs"]
    assert thread_calls[0][0] is subprocess.run
    assert kwargs["stdin"] is subprocess.PIPE
    assert kwargs["env"]["PHOTON_SIDECAR_WATCH_STDIN"] == "1"
    assert 16.0 <= clock["now"] < photon_adapter._SIDECAR_STARTUP_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_connect_cleans_up_sidecar_after_startup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _make_adapter(monkeypatch)
    _ConnectClient.instances.clear()
    monkeypatch.setattr(photon_adapter.httpx, "AsyncClient", _ConnectClient)

    async def _failed_start() -> None:
        adapter._sidecar_proc = object()  # type: ignore[assignment]
        raise RuntimeError("readiness timed out")

    stopped = False

    async def _stop() -> None:
        nonlocal stopped
        stopped = True
        adapter._sidecar_proc = None

    monkeypatch.setattr(adapter, "_start_sidecar", _failed_start)
    monkeypatch.setattr(adapter, "_stop_sidecar", _stop)

    assert await adapter.connect() is False
    assert stopped is True
    assert adapter._sidecar_proc is None
    assert adapter._http_client is None
    assert _ConnectClient.instances[0].closed is True


@pytest.mark.asyncio
async def test_connect_cleans_up_sidecar_when_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _make_adapter(monkeypatch)
    _ConnectClient.instances.clear()
    monkeypatch.setattr(photon_adapter.httpx, "AsyncClient", _ConnectClient)
    start_entered = asyncio.Event()

    async def _cancelled_start() -> None:
        adapter._sidecar_proc = object()  # type: ignore[assignment]
        start_entered.set()
        await asyncio.Event().wait()

    stopped = False

    async def _stop() -> None:
        nonlocal stopped
        stopped = True
        adapter._sidecar_proc = None

    monkeypatch.setattr(adapter, "_start_sidecar", _cancelled_start)
    monkeypatch.setattr(adapter, "_stop_sidecar", _stop)

    connect_task = asyncio.create_task(adapter.connect())
    await start_entered.wait()
    connect_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await connect_task

    assert stopped is True
    assert adapter._sidecar_proc is None
    assert adapter._http_client is None
    assert _ConnectClient.instances[0].closed is True


@pytest.mark.asyncio
async def test_supervisor_fatal_disconnect_completes_without_self_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crashed sidecar supervisor must finish cleanup and return to retry logic."""
    adapter = _make_adapter(monkeypatch)
    adapter._inbound_running = True
    client = _LifecycleClient()
    adapter._http_client = client  # type: ignore[assignment]

    class _EmptyStdout:
        @staticmethod
        def readline() -> bytes:
            return b""

    class _ExitedProc:
        pid = 999
        stdin = None
        stdout = _EmptyStdout()

        @staticmethod
        def poll() -> int:
            return 17

        @staticmethod
        def wait(timeout: float) -> int:
            return 17

    proc = _ExitedProc()
    adapter._sidecar_proc = proc  # type: ignore[assignment]
    handler_completed = False

    async def _fatal_handler(failed_adapter: PhotonAdapter) -> None:
        nonlocal handler_completed
        assert failed_adapter is adapter
        await failed_adapter.disconnect()
        handler_completed = True

    adapter.set_fatal_error_handler(_fatal_handler)
    supervisor = asyncio.create_task(
        adapter._supervise_sidecar(proc)  # type: ignore[arg-type]
    )
    adapter._sidecar_supervisor_task = supervisor

    await supervisor

    assert handler_completed is True
    assert adapter.fatal_error_code == "SIDECAR_CRASHED"
    assert adapter.fatal_error_retryable is True
    assert adapter._sidecar_proc is None
    assert adapter._sidecar_supervisor_task is None
    assert adapter._http_client is None
    assert client.shutdown_requested is True
    assert client.closed is True
