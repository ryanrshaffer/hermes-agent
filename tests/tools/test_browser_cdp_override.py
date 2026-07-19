from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

import pytest


HOST = "example-host"
PORT = 9223
WS_URL = f"ws://{HOST}:{PORT}/devtools/browser/abc123"
HTTP_URL = f"http://{HOST}:{PORT}"
VERSION_URL = f"{HTTP_URL}/json/version"


class TestResolveCdpOverride:
    def test_keeps_full_devtools_websocket_url(self):
        from tools.browser_tool import _resolve_cdp_override

        assert _resolve_cdp_override(WS_URL) == WS_URL

    def test_resolves_http_discovery_endpoint_to_websocket(self):
        from tools.browser_tool import _resolve_cdp_override

        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"webSocketDebuggerUrl": WS_URL}

        with patch("tools.browser_tool.requests.get", return_value=response) as mock_get:
            resolved = _resolve_cdp_override(HTTP_URL)

        assert resolved == WS_URL
        mock_get.assert_called_once_with(VERSION_URL, timeout=10)

    def test_resolves_bare_ws_hostport_to_discovery_websocket(self):
        from tools.browser_tool import _resolve_cdp_override

        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"webSocketDebuggerUrl": WS_URL}

        with patch("tools.browser_tool.requests.get", return_value=response) as mock_get:
            resolved = _resolve_cdp_override(f"ws://{HOST}:{PORT}")

        assert resolved == WS_URL
        mock_get.assert_called_once_with(VERSION_URL, timeout=10)

    def test_falls_back_to_raw_url_when_discovery_fails(self):
        from tools.browser_tool import _resolve_cdp_override

        with patch("tools.browser_tool.requests.get", side_effect=RuntimeError("boom")):
            assert _resolve_cdp_override(HTTP_URL) == HTTP_URL

    def test_normalizes_provider_returned_http_cdp_url_when_creating_session(self, monkeypatch):
        import tools.browser_tool as browser_tool

        provider = Mock()
        provider.create_session.return_value = {
            "session_name": "cloud-session",
            "bb_session_id": "bu_123",
            "cdp_url": "https://cdp.browser-use.example/session",
            "features": {"browser_use": True},
        }

        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"webSocketDebuggerUrl": WS_URL}

        monkeypatch.setattr(browser_tool, "_active_sessions", {})
        monkeypatch.setattr(browser_tool, "_session_last_activity", {})
        monkeypatch.setattr(browser_tool, "_start_browser_cleanup_thread", lambda: None)
        monkeypatch.setattr(browser_tool, "_update_session_activity", lambda task_id: None)
        monkeypatch.setattr(
            browser_tool, "_get_task_cdp_override", lambda _task_id: ""
        )
        monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: provider)

        with patch("tools.browser_tool.requests.get", return_value=response) as mock_get:
            session_info = browser_tool._get_session_info("task-browser-use")

        assert session_info["cdp_url"] == WS_URL
        provider.create_session.assert_called_once_with("task-browser-use")
        mock_get.assert_called_once_with(
            "https://cdp.browser-use.example/session/json/version",
            timeout=10,
        )


class TestGetCdpOverride:
    def test_prefers_env_var_over_config(self, monkeypatch):
        import tools.browser_tool as browser_tool

        monkeypatch.setenv("BROWSER_CDP_URL", HTTP_URL)
        monkeypatch.setattr(
            browser_tool,
            "read_raw_config",
            lambda: {"browser": {"cdp_url": "http://config-host:9222"}},
            raising=False,
        )

        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"webSocketDebuggerUrl": WS_URL}

        with patch("tools.browser_tool.requests.get", return_value=response) as mock_get:
            resolved = browser_tool._get_cdp_override()

        assert resolved == WS_URL
        mock_get.assert_called_once_with(VERSION_URL, timeout=10)

    def test_uses_config_browser_cdp_url_when_env_missing(self, monkeypatch):
        import tools.browser_tool as browser_tool

        monkeypatch.delenv("BROWSER_CDP_URL", raising=False)

        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"webSocketDebuggerUrl": WS_URL}

        with patch(
            "hermes_cli.config.read_raw_config",
            return_value={"browser": {"cdp_url": HTTP_URL}},
        ), patch("tools.browser_tool.requests.get", return_value=response) as mock_get:
            resolved = browser_tool._get_cdp_override()

        assert resolved == WS_URL
        mock_get.assert_called_once_with(VERSION_URL, timeout=10)


class TestManagedTaskCdpOverride:
    LOCAL_RAW = "http://127.0.0.1:9223"

    @staticmethod
    def _clear_cache(browser_tool):
        with browser_tool._task_cdp_override_lock:
            browser_tool._task_cdp_override_cache.clear()

    def test_requirement_presence_check_performs_no_network_io(self, monkeypatch):
        import tools.browser_tool as browser_tool

        monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
        with patch(
            "hermes_cli.config.read_raw_config",
            return_value={"browser": {"cdp_url": self.LOCAL_RAW}},
        ), patch.object(browser_tool.requests, "get") as get, patch.object(
            browser_tool, "_is_camofox_mode", return_value=False
        ):
            assert browser_tool.check_browser_requirements() is True
        get.assert_not_called()

    def test_session_ensure_and_resolution_are_cached_per_task(self, monkeypatch):
        import tools.browser_tool as browser_tool

        self._clear_cache(browser_tool)
        events = []
        monkeypatch.setattr(
            browser_tool,
            "_read_cdp_override",
            lambda: (self.LOCAL_RAW, "config"),
        )
        monkeypatch.setattr(browser_tool, "_managed_local_cdp_port", lambda *_: 9223)
        monkeypatch.setattr(
            browser_tool,
            "_ensure_managed_local_cdp",
            lambda port: events.append(("ensure", port)),
        )
        monkeypatch.setattr(
            browser_tool,
            "_resolve_cdp_override",
            lambda raw: events.append(("resolve", raw)) or WS_URL,
        )

        assert browser_tool._get_task_cdp_override("task-1") == WS_URL
        assert browser_tool._get_task_cdp_override("task-1") == WS_URL
        assert events == [("ensure", 9223), ("resolve", self.LOCAL_RAW)]

    @pytest.mark.parametrize(
        ("raw", "source"),
        [
            ("http://remote.example:9223", "config"),
            ("http://127.0.0.1:9223", "env"),
        ],
    )
    def test_remote_and_environment_overrides_never_start_local_task(
        self, monkeypatch, raw, source
    ):
        import tools.browser_tool as browser_tool

        self._clear_cache(browser_tool)
        monkeypatch.setattr(
            browser_tool, "_read_cdp_override", lambda: (raw, source)
        )
        ensure = Mock()
        monkeypatch.setattr(browser_tool, "_ensure_managed_local_cdp", ensure)
        monkeypatch.setattr(browser_tool, "_resolve_cdp_override", lambda _raw: WS_URL)

        assert browser_tool._get_task_cdp_override("task-remote") == WS_URL
        ensure.assert_not_called()

    def test_ensure_failure_is_explicit_and_not_cached(self, monkeypatch):
        import tools.browser_tool as browser_tool

        self._clear_cache(browser_tool)
        monkeypatch.setattr(
            browser_tool,
            "_read_cdp_override",
            lambda: (self.LOCAL_RAW, "config"),
        )
        monkeypatch.setattr(browser_tool, "_managed_local_cdp_port", lambda *_: 9223)
        ensure = Mock(side_effect=RuntimeError("managed CDP unavailable"))
        resolve = Mock(return_value=WS_URL)
        monkeypatch.setattr(browser_tool, "_ensure_managed_local_cdp", ensure)
        monkeypatch.setattr(browser_tool, "_resolve_cdp_override", resolve)

        for _attempt in range(2):
            with pytest.raises(RuntimeError, match="managed CDP unavailable"):
                browser_tool._get_task_cdp_override("task-failure")
        assert ensure.call_count == 2
        resolve.assert_not_called()

    def test_concurrent_same_task_ensures_once(self, monkeypatch):
        import tools.browser_tool as browser_tool

        self._clear_cache(browser_tool)
        monkeypatch.setattr(
            browser_tool,
            "_read_cdp_override",
            lambda: (self.LOCAL_RAW, "config"),
        )
        monkeypatch.setattr(browser_tool, "_managed_local_cdp_port", lambda *_: 9223)
        ensure = Mock()
        resolve = Mock(return_value=WS_URL)
        monkeypatch.setattr(browser_tool, "_ensure_managed_local_cdp", ensure)
        monkeypatch.setattr(browser_tool, "_resolve_cdp_override", resolve)

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(
                pool.map(
                    browser_tool._get_task_cdp_override,
                    ["task-shared"] * 4,
                )
            )
        assert results == [WS_URL] * 4
        ensure.assert_called_once_with(9223)
        resolve.assert_called_once_with(self.LOCAL_RAW)

    def test_cleanup_invalidates_task_resolution(self, monkeypatch):
        import tools.browser_tool as browser_tool

        self._clear_cache(browser_tool)
        monkeypatch.setattr(
            browser_tool,
            "_read_cdp_override",
            lambda: (self.LOCAL_RAW, "config"),
        )
        monkeypatch.setattr(browser_tool, "_managed_local_cdp_port", lambda *_: 9223)
        ensure = Mock()
        monkeypatch.setattr(browser_tool, "_ensure_managed_local_cdp", ensure)
        monkeypatch.setattr(browser_tool, "_resolve_cdp_override", lambda _raw: WS_URL)
        monkeypatch.setattr(browser_tool, "_cleanup_single_browser_session", lambda _task: None)

        browser_tool._get_task_cdp_override("task-cleanup")
        browser_tool.cleanup_browser("task-cleanup")
        browser_tool._get_task_cdp_override("task-cleanup")
        assert ensure.call_count == 2
