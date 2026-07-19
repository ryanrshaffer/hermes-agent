"""Connection cancellation must retain ownership until MCP teardown finishes."""

from __future__ import annotations

import asyncio

import pytest

from tools.mcp_tool import MCPServerTask, _connect_server


@pytest.mark.asyncio
async def test_connect_cancellation_preserves_error_when_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    start_entered = asyncio.Event()

    async def _start(self: MCPServerTask, config: dict) -> None:
        start_entered.set()
        await asyncio.Event().wait()

    async def _shutdown(self: MCPServerTask) -> None:
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(MCPServerTask, "start", _start)
    monkeypatch.setattr(MCPServerTask, "shutdown", _shutdown)

    connect_task = asyncio.create_task(
        _connect_server("cleanup-error-test", {"command": "fake"})
    )
    await start_entered.wait()
    connect_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await connect_task
    assert "cleanup after failed connect failed" in caplog.text


@pytest.mark.asyncio
async def test_connect_waits_for_cleanup_through_second_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_entered = asyncio.Event()
    shutdown_entered = asyncio.Event()
    release_shutdown = asyncio.Event()
    shutdown_complete = asyncio.Event()

    async def _start(self: MCPServerTask, config: dict) -> None:
        start_entered.set()
        await asyncio.Event().wait()

    async def _shutdown(self: MCPServerTask) -> None:
        shutdown_entered.set()
        await release_shutdown.wait()
        shutdown_complete.set()

    monkeypatch.setattr(MCPServerTask, "start", _start)
    monkeypatch.setattr(MCPServerTask, "shutdown", _shutdown)

    connect_task = asyncio.create_task(
        _connect_server("double-cancel-test", {"command": "fake"})
    )
    await start_entered.wait()
    connect_task.cancel()
    await shutdown_entered.wait()
    connect_task.cancel()
    await asyncio.sleep(0)

    assert not connect_task.done()
    release_shutdown.set()
    with pytest.raises(asyncio.CancelledError):
        await connect_task
    assert shutdown_complete.is_set()


@pytest.mark.asyncio
async def test_connect_failure_defers_cancellation_until_cleanup_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shutdown_entered = asyncio.Event()
    release_shutdown = asyncio.Event()
    shutdown_complete = asyncio.Event()

    async def _start(self: MCPServerTask, config: dict) -> None:
        raise RuntimeError("handshake failed")

    async def _shutdown(self: MCPServerTask) -> None:
        shutdown_entered.set()
        await release_shutdown.wait()
        shutdown_complete.set()

    monkeypatch.setattr(MCPServerTask, "start", _start)
    monkeypatch.setattr(MCPServerTask, "shutdown", _shutdown)

    connect_task = asyncio.create_task(
        _connect_server("failure-cancel-race-test", {"command": "fake"})
    )
    await shutdown_entered.wait()
    connect_task.cancel("gateway stopping")
    await asyncio.sleep(0)

    assert not connect_task.done()
    release_shutdown.set()
    with pytest.raises(asyncio.CancelledError):
        await connect_task
    assert shutdown_complete.is_set()


@pytest.mark.asyncio
async def test_cancelled_cleanup_preserves_original_connect_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    start_entered = asyncio.Event()

    async def _start(self: MCPServerTask, config: dict) -> None:
        start_entered.set()
        await asyncio.Event().wait()

    async def _shutdown(self: MCPServerTask) -> None:
        raise asyncio.CancelledError("cleanup cancel")

    monkeypatch.setattr(MCPServerTask, "start", _start)
    monkeypatch.setattr(MCPServerTask, "shutdown", _shutdown)

    connect_task = asyncio.create_task(
        _connect_server("cancelled-cleanup-test", {"command": "fake"})
    )
    await start_entered.wait()
    connect_task.cancel("original cancel")

    with pytest.raises(asyncio.CancelledError) as exc_info:
        await connect_task
    assert exc_info.value.args == ("original cancel",)
    assert "shutdown task was cancelled" in caplog.text


@pytest.mark.asyncio
async def test_failed_connect_preserves_error_when_cleanup_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def _start(self: MCPServerTask, config: dict) -> None:
        raise RuntimeError("handshake failed")

    async def _shutdown(self: MCPServerTask) -> None:
        raise asyncio.CancelledError("cleanup cancel")

    monkeypatch.setattr(MCPServerTask, "start", _start)
    monkeypatch.setattr(MCPServerTask, "shutdown", _shutdown)

    with pytest.raises(RuntimeError, match="handshake failed"):
        await _connect_server("failed-connect-cleanup-cancel-test", {"command": "fake"})
    assert "shutdown task was cancelled" in caplog.text


@pytest.mark.asyncio
async def test_caller_cancellation_wins_when_cleanup_is_also_cancelled(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    shutdown_entered = asyncio.Event()
    shutdown_task_holder: list[asyncio.Task] = []

    async def _start(self: MCPServerTask, config: dict) -> None:
        raise RuntimeError("handshake failed")

    async def _shutdown(self: MCPServerTask) -> None:
        shutdown_task_holder.append(asyncio.current_task())
        shutdown_entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(MCPServerTask, "start", _start)
    monkeypatch.setattr(MCPServerTask, "shutdown", _shutdown)

    connect_task = asyncio.create_task(
        _connect_server("combined-cancel-test", {"command": "fake"})
    )
    await shutdown_entered.wait()
    shutdown_task_holder[0].cancel("cleanup cancel")
    connect_task.cancel("outer cancel")

    with pytest.raises(asyncio.CancelledError) as exc_info:
        await connect_task
    assert exc_info.value.args == ("outer cancel",)
    assert "shutdown task was cancelled" in caplog.text
