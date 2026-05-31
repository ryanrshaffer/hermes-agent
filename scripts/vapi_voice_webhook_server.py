#!/usr/bin/env python3
"""Vapi webhook receiver for Ryan's Riley voice assistant.

- Validates X-Vapi-Webhook-Secret.
- Handles transfer-destination-request by returning Ryan's escalation number.
- Logs end-of-call reports to Discord forum (one post per call).
- Writes a durable Obsidian markdown note and raw JSON artifact.

Stdlib-only so it can run under the Hermes Windows install without extra deps.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


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
ESCALATION_NUMBER = os.environ.get("VAPI_ESCALATION_NUMBER", "")
DISCORD_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_FORUM_ID = os.environ.get("DISCORD_VOICE_CALL_FORUM_ID", "")
OBSIDIAN_VAULT = os.environ.get("OBSIDIAN_VAULT_PATH", str(Path.home() / "Documents" / "Obsidian Vault"))
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


def _discord_request(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_BOT_TOKEN missing")
    url = f"https://discord.com/api/v10{path}"
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bot {DISCORD_TOKEN}",
        "Content-Type": "application/json",
        "User-Agent": "Hermes-Vapi-Webhook/1.0",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8", "replace")
        return json.loads(raw) if raw else {}


def _chunk(text: str, n: int = 1800) -> list[str]:
    text = text or ""
    return [text[i:i+n] for i in range(0, len(text), n)] or [""]


def _post_discord(summary: dict[str, str], note_path: Path | None) -> str:
    if not DISCORD_FORUM_ID:
        raise RuntimeError("DISCORD_VOICE_CALL_FORUM_ID missing")
    title_bits = ["Call"]
    if summary["customer_name"] and summary["customer_name"] != "Unknown caller":
        title_bits.append(summary["customer_name"])
    elif summary["customer_number"]:
        title_bits.append(summary["customer_number"][-4:].rjust(len(summary["customer_number"]), "*"))
    title_bits.append(datetime.now().strftime("%Y-%m-%d %H:%M"))
    thread_name = _safe_name(" - ".join(title_bits), "voice-call")[:90]
    content = (
        f"## Voice call log\n"
        f"**Urgency:** {summary['urgency']}\n"
        f"**Caller:** {summary['customer_name']} {summary['customer_number']}\n"
        f"**Call ID:** `{summary['call_id']}`\n"
        f"**Started:** {summary['started']}\n"
        f"**Ended:** {summary['ended']}\n"
        f"**End reason:** {summary['ended_reason']}\n"
        f"**Cost:** {summary['cost']}\n"
        f"**Recording:** {summary['recording'] or 'not provided'}\n"
        f"**Obsidian note:** {note_path if note_path else 'not written'}\n\n"
        f"**Summary**\n{summary['summary'][:900]}"
    )[:1900]
    created = _discord_request("POST", f"/channels/{DISCORD_FORUM_ID}/threads", {
        "name": thread_name,
        "auto_archive_duration": 10080,
        "message": {"content": content},
    })
    thread_id = created.get("id")
    if thread_id and summary["transcript"]:
        for idx, part in enumerate(_chunk(summary["transcript"], 1800), 1):
            _discord_request("POST", f"/channels/{thread_id}/messages", {"content": f"**Transcript part {idx}**\n```\n{part}\n```"[:2000]})
    return str(thread_id or "")


def _write_obsidian(summary: dict[str, str], raw: dict[str, Any]) -> Path:
    vault = Path(OBSIDIAN_VAULT)
    day = datetime.now().strftime("%Y-%m-%d")
    folder = vault / "OwnerOps" / "Voice Calls" / day
    folder.mkdir(parents=True, exist_ok=True)
    call_id = _safe_name(summary["call_id"])
    name = _safe_name(summary["customer_name"], "unknown-caller")
    note = folder / f"{datetime.now().strftime('%H%M%S')}-{name}-{call_id}.md"
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
        return _json({
            "destination": {
                "type": "number",
                "number": ESCALATION_NUMBER,
                "message": "I’m going to try Ryan now. One moment."
            }
        })

    if msg_type == "end-of-call-report":
        summary = _summarize_call(message)
        note_path = None
        discord_thread = ""
        try:
            note_path = _write_obsidian(summary, payload)
        except Exception:
            traceback.print_exc()
        try:
            discord_thread = _post_discord(summary, note_path)
        except Exception:
            traceback.print_exc()
        return _json({"ok": True, "call_id": call_id, "obsidian_note": str(note_path or ""), "discord_thread_id": discord_thread})

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
