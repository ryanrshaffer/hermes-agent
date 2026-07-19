#!/usr/bin/env python3
"""Poll Vapi for ended calls and log new ones locally in Obsidian.

This avoids needing a public webhook tunnel. It is intended to run every few
minutes via Hermes cron or Windows Task Scheduler. Stdlib-only.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from vapi_voice_webhook_server import _summarize_call, _write_obsidian  # noqa: E402


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


def load_processed_state(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Vapi processed-call state is unreadable: {path}: {exc}") from exc
    if not isinstance(data, list) or any(not isinstance(item, str) for item in data):
        raise RuntimeError(f"Vapi processed-call state has an invalid schema: {path}")
    return set(data)


def save_processed_state(path: Path, processed: set[str]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(sorted(processed)[-500:], indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def save_poller_status(path: Path, *, status: str, failures: list[str]) -> None:
    previous: dict[str, Any] = {}
    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Vapi poller status is unreadable: {path}: {exc}") from exc
    if status == "healthy" and not failures and previous.get("status") == "healthy":
        return
    signature = "|".join(sorted(failures))
    repeated = status == "blocked" and previous.get("signature") == signature
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "status": status,
        "signature": signature,
        "first_seen_at": previous.get("first_seen_at") if repeated else now,
        "last_checked_at": now,
        "consecutive_failures": (
            int(previous.get("consecutive_failures", 0)) + 1
            if repeated
            else (1 if failures else 0)
        ),
        "failure_types": failures,
    }
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
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
    status_path = state_dir / "poller_status.json"
    processed = load_processed_state(state_path)
    try:
        calls = vapi_get("/call?limit=25")
    except Exception as exc:
        failure_types = [f"list:{type(exc).__name__}"]
        save_poller_status(status_path, status="blocked", failures=failure_types)
        print("BLOCKED: Vapi call listing failed; no calls were marked processed.")
        return 2
    if not isinstance(calls, list):
        save_poller_status(status_path, status="blocked", failures=["list:invalid_schema"])
        print("BLOCKED: Vapi call listing returned an invalid schema; no calls were marked processed.")
        return 2
    new_count = 0
    failures: list[str] = []
    ended_seen = 0
    for call in calls:
        if not isinstance(call, dict):
            failures.append("call:invalid_schema")
            continue
        call_id = call.get("id")
        if not call_id or call_id in processed:
            continue
        if call.get("status") != "ended":
            continue
        ended_seen += 1
        try:
            # Fetch full details so transcript/analysis/recordings are present.
            full = vapi_get(f"/call/{call_id}")
            if not isinstance(full, dict) or str(full.get("id") or "") != str(call_id):
                raise ValueError("full call payload failed identity/schema acceptance")
            payload = {"message": {"type": "end-of-call-report", **full}}
            summary = _summarize_call(payload["message"])
            if not isinstance(summary, dict) or str(summary.get("call_id") or "") != str(call_id):
                raise ValueError("call summary failed identity/schema acceptance")
            note = Path(_write_obsidian(summary, payload))
            if not note.is_file() or note.stat().st_size <= 0:
                raise RuntimeError("Obsidian note failed persistence acceptance")
            processed.add(str(call_id))
            # Checkpoint each accepted call so a later item cannot cause duplicate
            # local notes for earlier calls on the next poll.
            save_processed_state(state_path, processed)
            new_count += 1
        except Exception as exc:
            failures.append(f"call:{type(exc).__name__}")

    if failures:
        save_poller_status(status_path, status="blocked", failures=failures)
        print(
            f"BLOCKED: Vapi poll incomplete; {len(failures)} acceptance failure(s), "
            f"{ended_seen} ended candidate(s), and {new_count} accepted call(s) checkpointed."
        )
        return 2
    save_poller_status(status_path, status="healthy", failures=[])
    if new_count == 0:
        # Silent no-op for cron/no_agent watchdog pattern.
        return 0
    print(f"logged {new_count} voice call(s) locally")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
