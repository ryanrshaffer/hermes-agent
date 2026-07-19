"""Regression tests for Feishu's optional SDK loading boundary."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path


def test_importing_feishu_adapter_does_not_import_lark_oapi():
    repo_root = Path(__file__).resolve().parents[2]
    script = """
import sys
assert "lark_oapi" not in sys.modules
import plugins.platforms.feishu.adapter
assert "lark_oapi" not in sys.modules
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr


def test_configured_connect_checks_lazy_requirements(monkeypatch):
    from gateway.config import PlatformConfig
    from plugins.platforms.feishu import adapter as feishu

    platform = feishu.FeishuAdapter(PlatformConfig())
    platform._app_id = "cli_test"
    platform._app_secret = "secret_test"
    platform._connection_mode = "websocket"
    calls = []
    monkeypatch.setattr(
        feishu,
        "check_feishu_requirements",
        lambda: calls.append("requirements") or False,
    )

    assert asyncio.run(platform.connect()) is False
    assert calls == ["requirements"]


def test_unconfigured_connect_does_not_load_lazy_requirements(monkeypatch):
    from gateway.config import PlatformConfig
    from plugins.platforms.feishu import adapter as feishu

    platform = feishu.FeishuAdapter(PlatformConfig())
    platform._app_id = ""
    platform._app_secret = ""
    monkeypatch.setattr(
        feishu,
        "check_feishu_requirements",
        lambda: (_ for _ in ()).throw(AssertionError("SDK load should be skipped")),
    )

    assert asyncio.run(platform.connect()) is False
