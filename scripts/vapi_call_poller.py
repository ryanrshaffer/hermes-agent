#!/usr/bin/env python3
"""Poll Vapi for ended calls and log new ones to Discord forum + Obsidian.

This avoids needing a public webhook tunnel. It is intended to run every few
minutes via Hermes cron or Windows Task Scheduler. Stdlib-only.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from vapi_voice_webhook_server import _post_discord, _summarize_call, _write_obsidian  # noqa: E402


def load_env() -> None:
    candidates: list[Path] = []
    if os.environ.get("HERMES_HOME"):
        candidates.append(Path(os.environ["HERMES_HOME"]) / ".env")
    candidates.append(Path.home() / ".hermes" / ".env")
    for path in candidates:
        if path.exists():
            for line in path.read_text(errors="ignore").splitlines():
                if line and not line.lstrip().startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def vapi_get(path: str) -> Any:
    key = os.environ.get("VAPI_API_KEY") or os.environ.get("VAPI_PRIVATE_KEY")
    if not key:
        raise RuntimeError("VAPI_API_KEY missing")
    req = urllib.request.Request("https://api.vapi.ai" + path, headers={
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "User-Agent": "Hermes-Vapi-Poller/1.0",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def main() -> None:
    load_env()
    # State dir must match vapi_voice_webhook_server.LOG_DIR so the poller's
    # processed_calls.json sits next to the per-call payload JSONs the webhook
    # writes. Both honour VAPI_CALL_LOG_DIR; otherwise we derive from
    # HERMES_HOME. No %LOCALAPPDATA% fallback — a subprocess that loses env
    # should fail loudly, not silently recreate state under AppData.
    explicit = os.environ.get("VAPI_CALL_LOG_DIR")
    if explicit:
        state_dir = Path(explicit)
    elif os.environ.get("HERMES_HOME"):
        state_dir = Path(os.environ["HERMES_HOME"]) / "vapi-call-logs"
    else:
        raise RuntimeError("VAPI_CALL_LOG_DIR or HERMES_HOME must be set.")
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "processed_calls.json"
    try:
        processed = set(json.loads(state_path.read_text()) if state_path.exists() else [])
    except Exception:
        processed = set()

    calls = vapi_get("/call?limit=25")
    new_count = 0
    for call in calls if isinstance(calls, list) else []:
        call_id = call.get("id")
        if not call_id or call_id in processed:
            continue
        if call.get("status") != "ended":
            continue
        # Fetch full details so transcript/analysis/recordings are present.
        full = vapi_get(f"/call/{call_id}")
        payload = {"message": {"type": "end-of-call-report", **full}}
        summary = _summarize_call(payload["message"])
        note = _write_obsidian(summary, payload)
        thread_id = _post_discord(summary, note)
        processed.add(call_id)
        new_count += 1
        print(f"logged call {call_id} -> Discord thread {thread_id} -> {note}")

    # Keep state bounded.
    state_path.write_text(json.dumps(sorted(processed)[-500:], indent=2), encoding="utf-8")
    if new_count == 0:
        # Silent no-op for cron/no_agent watchdog pattern.
        return


if __name__ == "__main__":
    main()
