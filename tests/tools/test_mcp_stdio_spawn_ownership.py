"""Concurrent stdio MCP starts must retain exact subprocess ownership."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import tools.mcp_tool as mcp


def test_stdio_process_identity_requires_resolved_command_and_argument_prefix() -> None:
    processes = {
        101: (
            [r"C:\Program Files\nodejs\node.exe", "server.js", "--stdio"],
            1.0,
        ),
        202: (
            [
                r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
                "-File",
                r"C:\other\script.ps1",
            ],
            2.0,
        ),
    }

    class FakeProcess:
        def __init__(self, pid: int):
            self.command_line, self.started_at = processes[pid]

        def cmdline(self):
            return list(self.command_line)

        def create_time(self):
            return self.started_at

    fake_psutil = SimpleNamespace(Process=FakeProcess)
    with patch.dict(sys.modules, {"psutil": fake_psutil}):
        assert mcp._inspect_stdio_process(
            101,
            r"C:\Program Files\nodejs\node.exe",
            ["server.js", "--stdio"],
        ) == 1.0
        assert mcp._inspect_stdio_process(
            202,
            r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            ["-File", r"C:\expected\script.ps1"],
        ) is None


def test_release_stdio_ownership_preserves_replacement_generation() -> None:
    with mcp._lock:
        mcp._stdio_pids.clear()
        mcp._stdio_pids[101] = "replacement-owner"
        mcp._stdio_pid_start_times.clear()
        mcp._stdio_pid_start_times[101] = 9.99

    released = mcp._release_stdio_ownership(
        "old-owner",
        {101},
        {101: 1.01},
    )

    assert not released
    with mcp._lock:
        assert mcp._stdio_pids[101] == "replacement-owner"
        assert mcp._stdio_pid_start_times[101] == 9.99
        mcp._stdio_pids.clear()
        mcp._stdio_pid_start_times.clear()


def test_parallel_stdio_starts_do_not_claim_each_others_children() -> None:
    live_pids: set[int] = set()
    releases: dict[str, asyncio.Event]
    pid_for_command = {"server-a": 101, "server-b": 202}

    @asynccontextmanager
    async def fake_stdio_client(params, *, errlog):
        del errlog
        pid = pid_for_command[params.command]
        live_pids.add(pid)
        if pid == 202:
            live_pids.add(303)  # unrelated gateway child inside the window
        await asyncio.sleep(0)
        try:
            yield MagicMock(), MagicMock()
        finally:
            live_pids.discard(pid)
            live_pids.discard(303)

    class FakeClientSession:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def initialize(self):
            return None

    async def fake_discover_tools(self):
        self._tools = []

    async def fake_wait_for_lifecycle_event(self):
        await releases[self.name].wait()
        return "shutdown"

    async def run_test() -> None:
        nonlocal releases
        releases = {"server-a": asyncio.Event(), "server-b": asyncio.Event()}
        server_a = mcp.MCPServerTask("server-a")
        server_b = mcp.MCPServerTask("server-b")
        with mcp._lock:
            mcp._stdio_pids.clear()
            mcp._stdio_pid_start_times.clear()
            mcp._stdio_descendant_start_times.clear()
            mcp._stdio_pgids.clear()
            mcp._orphan_stdio_pids.clear()

        with patch.object(mcp, "_MCP_AVAILABLE", True), \
                patch.object(mcp, "_build_safe_env", return_value={}), \
                patch.object(
                    mcp,
                    "_resolve_stdio_command",
                    side_effect=lambda command, env: (command, env),
                ), \
                patch(
                    "tools.osv_check.check_package_for_malware",
                    return_value=None,
                ), \
                patch.object(mcp, "stdio_client", fake_stdio_client), \
                patch.object(mcp, "ClientSession", FakeClientSession), \
                patch.object(
                    mcp,
                    "_snapshot_child_pids",
                    side_effect=lambda: set(live_pids),
                ), \
                patch.object(
                    mcp,
                    "_inspect_stdio_process",
                    side_effect=lambda pid, _command, _args: (
                        float(pid) if pid in {101, 202} else None
                    ),
                ), \
                patch.object(mcp, "_capture_stdio_descendants"), \
                patch.object(mcp, "_cleanup_windows_stdio_processes"), \
                patch.object(mcp, "_write_stderr_log_header"), \
                patch.object(mcp, "_get_mcp_stderr_log", return_value=None), \
                patch.object(
                    mcp.MCPServerTask,
                    "_discover_tools",
                    fake_discover_tools,
                ), \
                patch.object(
                    mcp.MCPServerTask,
                    "_wait_for_lifecycle_event",
                    fake_wait_for_lifecycle_event,
                ):
            task_a = asyncio.create_task(
                server_a._run_stdio({"command": "server-a"})
            )
            task_b = asyncio.create_task(
                server_b._run_stdio({"command": "server-b"})
            )
            await asyncio.wait_for(server_a._ready.wait(), timeout=1)
            await asyncio.wait_for(server_b._ready.wait(), timeout=1)

            with mcp._lock:
                assert mcp._stdio_pids == {101: "server-a", 202: "server-b"}
                # Simulate numeric PID reuse/claim by a newer active owner
                # before server-a's old finally block completes.
                mcp._stdio_pids[101] = "replacement-owner"
                mcp._stdio_pid_start_times[101] = 999.0
                mcp._stdio_descendant_start_times[101] = {909: 9.09}

            releases["server-a"].set()
            await asyncio.wait_for(task_a, timeout=1)
            assert not task_b.done()
            with mcp._lock:
                assert mcp._stdio_pids == {
                    101: "replacement-owner",
                    202: "server-b",
                }
                assert mcp._stdio_pid_start_times[101] == 999.0
                assert mcp._stdio_descendant_start_times[101] == {909: 9.09}
                assert not mcp._orphan_stdio_pids

            with patch.object(mcp.os, "kill") as kill:
                mcp._kill_orphaned_mcp_children()
            kill.assert_not_called()
            assert not task_b.done()

            with mcp._lock:
                mcp._stdio_pids.pop(101, None)
                mcp._stdio_pid_start_times.pop(101, None)
                mcp._stdio_descendant_start_times.pop(101, None)
            releases["server-b"].set()
            await asyncio.wait_for(task_b, timeout=1)

        with mcp._lock:
            assert not mcp._stdio_pids
            mcp._stdio_pid_start_times.clear()
            mcp._stdio_descendant_start_times.clear()
            assert not mcp._orphan_stdio_pids

    asyncio.run(run_test())


def test_stdio_entry_failure_clears_exact_spawn_ownership() -> None:
    live_pids: set[int] = set()

    class FailingTransport:
        async def __aenter__(self):
            live_pids.add(404)
            raise RuntimeError("transport entry failed")

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def run_test() -> None:
        server = mcp.MCPServerTask("entry-failure")
        with mcp._lock:
            mcp._stdio_pids.clear()
            mcp._stdio_pid_start_times.clear()
            mcp._stdio_descendant_start_times.clear()
            mcp._orphan_stdio_pids.clear()

        with patch.object(mcp, "_MCP_AVAILABLE", True), \
                patch.object(mcp, "_build_safe_env", return_value={}), \
                patch.object(
                    mcp,
                    "_resolve_stdio_command",
                    side_effect=lambda command, env: (command, env),
                ), \
                patch(
                    "tools.osv_check.check_package_for_malware",
                    return_value=None,
                ), \
                patch.object(mcp, "stdio_client", return_value=FailingTransport()), \
                patch.object(
                    mcp,
                    "_snapshot_child_pids",
                    side_effect=lambda: set(live_pids),
                ), \
                patch.object(
                    mcp,
                    "_inspect_stdio_process",
                    return_value=4.04,
                ), \
                patch.object(mcp, "_write_stderr_log_header"), \
                patch.object(mcp, "_get_mcp_stderr_log", return_value=None), \
                patch.object(mcp, "_cleanup_windows_stdio_processes") as cleanup:
            try:
                await server._run_stdio({"command": "entry-failure"})
            except RuntimeError as exc:
                assert str(exc) == "transport entry failed"
            else:
                raise AssertionError("transport entry failure did not propagate")

        cleanup.assert_called_once_with({404})
        with mcp._lock:
            assert not mcp._stdio_pids
            assert not mcp._stdio_pid_start_times
            assert not mcp._stdio_descendant_start_times

    asyncio.run(run_test())


def test_stdio_entry_cancellation_reaps_exact_spawn() -> None:
    live_pids: set[int] = set()
    entered: asyncio.Event

    class CancelledTransport:
        async def __aenter__(self):
            live_pids.add(505)
            entered.set()
            await asyncio.Event().wait()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def run_test() -> None:
        nonlocal entered
        entered = asyncio.Event()
        server = mcp.MCPServerTask("entry-cancel")
        with mcp._lock:
            mcp._stdio_pids.clear()
            mcp._stdio_pid_start_times.clear()
            mcp._stdio_descendant_start_times.clear()
            mcp._orphan_stdio_pids.clear()

        with patch.object(mcp, "_MCP_AVAILABLE", True), \
                patch.object(mcp, "_build_safe_env", return_value={}), \
                patch.object(
                    mcp,
                    "_resolve_stdio_command",
                    side_effect=lambda command, env: (command, env),
                ), \
                patch(
                    "tools.osv_check.check_package_for_malware",
                    return_value=None,
                ), \
                patch.object(
                    mcp,
                    "stdio_client",
                    return_value=CancelledTransport(),
                ), \
                patch.object(
                    mcp,
                    "_snapshot_child_pids",
                    side_effect=lambda: set(live_pids),
                ), \
                patch.object(mcp, "_inspect_stdio_process", return_value=5.05), \
                patch.object(mcp, "_write_stderr_log_header"), \
                patch.object(mcp, "_get_mcp_stderr_log", return_value=None), \
                patch.object(mcp, "_cleanup_windows_stdio_processes") as cleanup:
            task = asyncio.create_task(
                server._run_stdio({"command": "entry-cancel"})
            )
            await asyncio.wait_for(entered.wait(), timeout=1)
            task.cancel("gateway stopping")
            try:
                await task
            except asyncio.CancelledError as exc:
                assert exc.args == ("gateway stopping",)
            else:
                raise AssertionError("transport entry cancellation did not propagate")

        cleanup.assert_called_once_with({505})
        with mcp._lock:
            assert not mcp._stdio_pids
            assert not mcp._stdio_pid_start_times
            assert not mcp._stdio_descendant_start_times

    asyncio.run(run_test())


def test_client_initialization_failure_captures_descendant_before_transport_exit() -> None:
    live_pids: set[int] = set()
    captured_before_cleanup = False

    @asynccontextmanager
    async def fake_stdio_client(params, *, errlog):
        del params, errlog
        live_pids.add(606)
        try:
            yield MagicMock(), MagicMock()
        finally:
            live_pids.discard(606)

    class FailingClientSession:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def initialize(self):
            raise RuntimeError("initialize failed")

    def capture_descendants(root_pids: set) -> None:
        assert root_pids == {606}
        with mcp._lock:
            mcp._stdio_descendant_start_times[606] = {707: 7.07}

    def cleanup(root_pids: set) -> set:
        nonlocal captured_before_cleanup
        with mcp._lock:
            captured_before_cleanup = (
                root_pids == {606}
                and mcp._stdio_descendant_start_times.get(606) == {707: 7.07}
            )
        return set()

    async def run_test() -> None:
        server = mcp.MCPServerTask("initialize-failure")
        with mcp._lock:
            mcp._stdio_pids.clear()
            mcp._stdio_pid_start_times.clear()
            mcp._stdio_descendant_start_times.clear()
            mcp._orphan_stdio_pids.clear()

        with patch.object(mcp, "_MCP_AVAILABLE", True), \
                patch.object(mcp, "_build_safe_env", return_value={}), \
                patch.object(
                    mcp,
                    "_resolve_stdio_command",
                    side_effect=lambda command, env: (command, env),
                ), \
                patch(
                    "tools.osv_check.check_package_for_malware",
                    return_value=None,
                ), \
                patch.object(mcp, "stdio_client", fake_stdio_client), \
                patch.object(mcp, "ClientSession", FailingClientSession), \
                patch.object(
                    mcp,
                    "_snapshot_child_pids",
                    side_effect=lambda: set(live_pids),
                ), \
                patch.object(mcp, "_inspect_stdio_process", return_value=6.06), \
                patch.object(mcp, "_write_stderr_log_header"), \
                patch.object(mcp, "_get_mcp_stderr_log", return_value=None), \
                patch.object(
                    mcp,
                    "_capture_stdio_descendants",
                    side_effect=capture_descendants,
                ), \
                patch.object(
                    mcp,
                    "_cleanup_windows_stdio_processes",
                    side_effect=cleanup,
                ):
            try:
                await server._run_stdio({"command": "initialize-failure"})
            except RuntimeError as exc:
                assert str(exc) == "initialize failed"
            else:
                raise AssertionError("initialize failure did not propagate")

    asyncio.run(run_test())
    assert captured_before_cleanup


def test_live_wrapper_descendants_survive_wrapper_exit_until_cleanup() -> None:
    live_pids: set[int] = set()
    capture_states: list[bool] = []
    cleanup_descendants: dict[int, float] = {}

    @asynccontextmanager
    async def fake_stdio_client(params, *, errlog):
        del params, errlog
        live_pids.add(808)
        yield MagicMock(), MagicMock()

    class FakeClientSession:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def initialize(self):
            return None

    async def fake_discover_tools(self):
        self._tools = []

    async def fake_wait_for_lifecycle_event(self):
        assert capture_states == [True]
        live_pids.discard(808)
        return "shutdown"

    def capture_descendants(root_pids: set) -> None:
        assert root_pids == {808}
        wrapper_is_live = 808 in live_pids
        capture_states.append(wrapper_is_live)
        if wrapper_is_live:
            with mcp._lock:
                mcp._stdio_descendant_start_times[808] = {909: 9.09}

    def cleanup(root_pids: set) -> set:
        assert root_pids == {808}
        with mcp._lock:
            cleanup_descendants.update(
                mcp._stdio_descendant_start_times.get(808, {})
            )
        return set()

    async def run_test() -> None:
        server = mcp.MCPServerTask("wrapper-exit")
        with mcp._lock:
            mcp._stdio_pids.clear()
            mcp._stdio_pid_start_times.clear()
            mcp._stdio_descendant_start_times.clear()
            mcp._stdio_pgids.clear()
            mcp._orphan_stdio_pids.clear()

        with patch.object(mcp.os, "name", "nt"), \
                patch.object(mcp, "_MCP_AVAILABLE", True), \
                patch.object(mcp, "_build_safe_env", return_value={}), \
                patch.object(
                    mcp,
                    "_resolve_stdio_command",
                    side_effect=lambda command, env: (command, env),
                ), \
                patch(
                    "tools.osv_check.check_package_for_malware",
                    return_value=None,
                ), \
                patch.object(mcp, "stdio_client", fake_stdio_client), \
                patch.object(mcp, "ClientSession", FakeClientSession), \
                patch.object(
                    mcp,
                    "_snapshot_child_pids",
                    side_effect=lambda: set(live_pids),
                ), \
                patch.object(mcp, "_inspect_stdio_process", return_value=8.08), \
                patch.object(mcp, "_write_stderr_log_header"), \
                patch.object(mcp, "_get_mcp_stderr_log", return_value=None), \
                patch.object(
                    mcp,
                    "_capture_stdio_descendants",
                    side_effect=capture_descendants,
                ), \
                patch.object(
                    mcp,
                    "_cleanup_windows_stdio_processes",
                    side_effect=cleanup,
                ), \
                patch.object(
                    mcp.MCPServerTask,
                    "_discover_tools",
                    fake_discover_tools,
                ), \
                patch.object(
                    mcp.MCPServerTask,
                    "_wait_for_lifecycle_event",
                    fake_wait_for_lifecycle_event,
                ):
            await server._run_stdio({"command": "wrapper-exit"})

        assert capture_states == [True, False]
        assert cleanup_descendants == {909: 9.09}
        with mcp._lock:
            assert not mcp._stdio_pids
            assert not mcp._stdio_pid_start_times
            assert not mcp._stdio_descendant_start_times
            assert not mcp._orphan_stdio_pids

    asyncio.run(run_test())


def test_windows_orphan_cleanup_targets_only_owned_wrapper_tree() -> None:
    with mcp._lock:
        mcp._stdio_pids.clear()
        mcp._stdio_pids[202] = "active-server"
        mcp._stdio_pid_start_times.clear()
        mcp._stdio_pid_start_times[101] = 1.0
        mcp._stdio_descendant_start_times.clear()
        mcp._orphan_stdio_pids.clear()
        mcp._orphan_stdio_pids.add(101)
        mcp._stdio_pgids.clear()

    match_calls = 0

    def matches_until_forced(_pid: int, _started_at: float) -> bool:
        nonlocal match_calls
        match_calls += 1
        return match_calls < 3

    with patch.object(mcp.os, "name", "nt"), \
            patch.object(mcp.time, "sleep"), \
            patch.object(
                mcp,
                "_process_matches_start_time",
                side_effect=matches_until_forced,
            ), \
            patch.object(subprocess, "run") as run:
        run.return_value = subprocess.CompletedProcess([], 0)
        mcp._kill_orphaned_mcp_children()

    commands = [call.args[0] for call in run.call_args_list]
    assert commands == [
        ["taskkill", "/PID", "101", "/T"],
        ["taskkill", "/PID", "101", "/T", "/F"],
    ]
    assert all("202" not in command for command in commands)

    with mcp._lock:
        assert mcp._stdio_pids == {202: "active-server"}
        assert not mcp._orphan_stdio_pids
        mcp._stdio_pids.clear()
        mcp._stdio_pid_start_times.clear()
        mcp._stdio_descendant_start_times.clear()


def test_windows_cleanup_uses_captured_descendant_when_wrapper_is_gone() -> None:
    with mcp._lock:
        mcp._stdio_pids.clear()
        mcp._stdio_pid_start_times.clear()
        mcp._stdio_pid_start_times[101] = 1.0
        mcp._stdio_descendant_start_times.clear()
        mcp._stdio_descendant_start_times[101] = {303: 3.0}

    def matches(pid: int, started_at: float) -> bool:
        return pid == 303 and started_at == 3.0

    with patch.object(mcp, "_process_matches_start_time", side_effect=matches), \
            patch.object(mcp.time, "sleep"), \
            patch.object(subprocess, "run") as run:
        run.return_value = subprocess.CompletedProcess([], 0)
        mcp._cleanup_windows_stdio_processes({101})

    commands = [call.args[0] for call in run.call_args_list]
    assert commands == [
        ["taskkill", "/PID", "303", "/T"],
        ["taskkill", "/PID", "303", "/T", "/F"],
    ]


def test_windows_cleanup_escalates_surviving_verified_descendant() -> None:
    with mcp._lock:
        mcp._stdio_pids.clear()
        mcp._stdio_pid_start_times.clear()
        mcp._stdio_pid_start_times[101] = 1.0
        mcp._stdio_descendant_start_times.clear()
        mcp._stdio_descendant_start_times[101] = {303: 3.0}

    checks = {101: 0, 303: 0}

    def matches(pid: int, started_at: float) -> bool:
        del started_at
        checks[pid] += 1
        return pid == 303 or checks[pid] == 1

    with patch.object(mcp, "_process_matches_start_time", side_effect=matches), \
            patch.object(mcp.time, "sleep"), \
            patch.object(subprocess, "run") as run:
        run.return_value = subprocess.CompletedProcess([], 0)
        mcp._cleanup_windows_stdio_processes({101})

    commands = [call.args[0] for call in run.call_args_list]
    assert commands == [
        ["taskkill", "/PID", "101", "/T"],
        ["taskkill", "/PID", "303", "/T"],
        ["taskkill", "/PID", "303", "/T", "/F"],
    ]


def test_reused_or_mismatched_orphan_pid_is_never_targeted() -> None:
    with mcp._lock:
        mcp._stdio_pids.clear()
        mcp._stdio_pids[101] = "new-active-owner"
        mcp._orphan_stdio_pids.clear()
        mcp._orphan_stdio_pids.add(101)
        mcp._stdio_pid_start_times.clear()
        mcp._stdio_pid_start_times[101] = 1.0
        mcp._stdio_descendant_start_times.clear()

    with patch.object(mcp.os, "name", "nt"), \
            patch.object(subprocess, "run") as run:
        mcp._kill_orphaned_mcp_children()
    run.assert_not_called()

    with mcp._lock:
        mcp._stdio_pids.clear()
        mcp._orphan_stdio_pids.add(101)
    with patch.object(mcp.os, "name", "nt"), \
            patch.object(mcp, "_process_matches_start_time", return_value=False), \
            patch.object(subprocess, "run") as run:
        mcp._kill_orphaned_mcp_children()
    run.assert_not_called()
