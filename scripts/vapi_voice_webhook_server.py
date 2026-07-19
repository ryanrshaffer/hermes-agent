#!/usr/bin/env python3
"""Local-only Vapi webhook receiver for Ryan's Riley voice assistant.

- Validates X-Vapi-Webhook-Secret.
- Writes a durable Obsidian markdown note and raw JSON artifact.
- Fails closed for call transfers and external delivery pending explicit approval.

Stdlib-only so it can run under the Hermes Windows install without extra deps.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from obsidian_vault_guard import (  # noqa: E402
    CANONICAL_OBSIDIAN_VAULT,
    require_canonical_obsidian_vault,
)


def _load_env_file() -> None:
    candidates = []
    if os.environ.get("HERMES_HOME"):
        candidates.append(Path(os.environ["HERMES_HOME"]) / ".env")
    candidates.append(Path.home() / ".hermes" / ".env")
    for path in candidates:
        if not path.exists():
            continue
        for line in path.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env_file()

PORT = int(os.environ.get("VAPI_WEBHOOK_PORT", "8787"))
WEBHOOK_SECRET = os.environ.get("VAPI_WEBHOOK_SECRET", "")
OBSIDIAN_VAULT = os.environ.get("OBSIDIAN_VAULT_PATH") or str(CANONICAL_OBSIDIAN_VAULT)


def _resolve_log_dir() -> Path:
    explicit = os.environ.get("VAPI_CALL_LOG_DIR")
    if explicit:
        return Path(explicit)
    hermes_home = os.environ.get("HERMES_HOME")
    if hermes_home:
        return Path(hermes_home) / "vapi-call-logs"
    raise RuntimeError("VAPI_CALL_LOG_DIR or HERMES_HOME must be set.")


LOG_DIR = _resolve_log_dir()
LOG_DIR.mkdir(parents=True, exist_ok=True)


def _safe_name(value: str, fallback: str = "call") -> str:
    value = re.sub(r"[^A-Za-z0-9._ -]+", "", value or "").strip().replace(" ", "-")
    return value[:80] or fallback


def _json(obj: Any, status: int = 200) -> tuple[int, bytes, dict[str, str]]:
    return status, json.dumps(obj).encode("utf-8"), {"Content-Type": "application/json"}


def _pick(d: dict[str, Any], *paths: str, default: Any = "") -> Any:
    for path in paths:
        cur: Any = d
        ok = True
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and cur not in (None, ""):
            return cur
    return default


def _call_id(message: dict[str, Any]) -> str:
    return str(_pick(message, "call.id", "callId", "id", default=f"call-{int(time.time())}"))


def _markdown_escape(s: Any) -> str:
    return str(s or "").replace("\r", "").strip()


def _summarize_call(message: dict[str, Any]) -> dict[str, str]:
    call_id = _call_id(message)
    customer_name = _pick(message, "customer.name", "call.customer.name", default="Unknown caller")
    customer_number = _pick(message, "customer.number", "call.customer.number", default="")
    status = _pick(message, "call.status", "status", default="")
    ended_reason = _pick(message, "endedReason", "call.endedReason", default="")
    started = _pick(message, "startedAt", "call.startedAt", default="")
    ended = _pick(message, "endedAt", "call.endedAt", default="")
    summary = _pick(message, "summary", "analysis.summary", "call.analysis.summary", default="")
    transcript = _pick(message, "transcript", "artifact.transcript", "call.artifact.transcript", default="")
    recording = _pick(message, "recordingUrl", "artifact.recordingUrl", "call.artifact.recordingUrl", "artifact.stereoRecordingUrl", "call.artifact.stereoRecordingUrl", default="")
    cost = _pick(message, "cost", "call.cost", default="")
    urgency = "routine"
    haystack = f"{summary}\n{transcript}".lower()
    if any(w in haystack for w in ["urgent", "emergency", "deadline", "legal", "contract", "money", "payment", "property", "franchise", "acquisition", "seller", "broker", "family"]):
        urgency = "urgent/important"
    return {
        "call_id": call_id,
        "customer_name": str(customer_name or "Unknown caller"),
        "customer_number": str(customer_number or ""),
        "status": str(status or ""),
        "ended_reason": str(ended_reason or ""),
        "started": str(started or ""),
        "ended": str(ended or ""),
        "summary": str(summary or "No summary returned by Vapi."),
        "transcript": str(transcript or ""),
        "recording": str(recording or ""),
        "cost": str(cost or ""),
        "urgency": urgency,
    }


def _write_obsidian(summary: dict[str, str], raw: dict[str, Any]) -> Path:
    vault = require_canonical_obsidian_vault(
        OBSIDIAN_VAULT, canonical=CANONICAL_OBSIDIAN_VAULT
    )
    call_timestamp = summary.get("started") or summary.get("ended") or ""
    day_match = re.search(r"\d{4}-\d{2}-\d{2}", call_timestamp)
    day = day_match.group(0) if day_match else "undated"
    folder = vault / "OwnerOps" / "Voice Calls" / day
    folder.mkdir(parents=True, exist_ok=True)
    call_id = _safe_name(summary["call_id"])
    note = folder / f"{call_id}.md"
    raw_path = folder / f"{note.stem}.json"
    raw_path.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
    md = f"""# Voice Call — {summary['customer_name']}

- Call ID: `{summary['call_id']}`
- Urgency: {summary['urgency']}
- Caller: {summary['customer_name']} {summary['customer_number']}
- Started: {summary['started']}
- Ended: {summary['ended']}
- End reason: {summary['ended_reason']}
- Cost: {summary['cost']}
- Recording: {summary['recording'] or 'not provided'}
- Raw JSON: [[{raw_path.stem}]]

## Summary

{summary['summary']}

## Transcript

```text
{summary['transcript'] or 'No transcript returned.'}
```
"""
    note.write_text(md, encoding="utf-8")
    return note


def handle_payload(payload: dict[str, Any]) -> tuple[int, bytes, dict[str, str]]:
    message = payload.get("message") if isinstance(payload.get("message"), dict) else payload
    msg_type = message.get("type") or payload.get("type")
    call_id = _call_id(message)
    (LOG_DIR / f"{_safe_name(call_id)}-{int(time.time())}.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    if msg_type == "transfer-destination-request":
        return _json(
            {
                "error": "external_communication_requires_explicit_approval",
                "transfer_authorized": False,
            },
            409,
        )

    if msg_type == "end-of-call-report":
        summary = _summarize_call(message)
        note_path = None
        try:
            note_path = _write_obsidian(summary, payload)
        except Exception:
            traceback.print_exc()
        return _json({
            "ok": True,
            "obsidian_note_written": bool(note_path),
            "external_delivery": "not_attempted_approval_required",
        })

    return _json({"ok": True, "ignored_type": msg_type or "unknown", "call_id": call_id})


class Handler(BaseHTTPRequestHandler):
    server_version = "HermesVapiWebhook/1.0"

    def _send(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/health", "/vapi/health"):
            self._send(*_json({"status": "ok", "service": "vapi-voice-webhook"}))
        else:
            self._send(404, b"not found", {"Content-Type": "text/plain"})

    def do_POST(self) -> None:  # noqa: N802
        if not self.path.startswith("/vapi/webhook"):
            self._send(404, b"not found", {"Content-Type": "text/plain"})
            return
        if WEBHOOK_SECRET and self.headers.get("X-Vapi-Webhook-Secret") != WEBHOOK_SECRET:
            self._send(*_json({"error": "unauthorized"}, 401))
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            self._send(*handle_payload(payload))
        except Exception as exc:
            traceback.print_exc()
            self._send(*_json({"error": str(exc)}, 500))

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.log_date_time_string(), fmt % args))


def main() -> None:
    print(f"Starting Vapi webhook server on 127.0.0.1:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
