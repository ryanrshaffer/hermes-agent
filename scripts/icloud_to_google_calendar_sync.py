#!/usr/bin/env python3
"""Mirror published iCloud calendars into a Google Calendar for Riley/Vapi.

Purpose:
- iCloud published webcal feeds are read-only.
- Google Calendar can be the operational calendar Riley checks and writes.
- This script mirrors iCloud personal/family busy blocks into a target Google
  Calendar so Vapi's Google Calendar tools can check one consolidated calendar.

Secrets/config come from Hermes .env:
  RILEY_PERSONAL_ICAL_URL=webcal://...
  RILEY_FAMILY_ICAL_URL=webcal://...        # optional until provided
  RILEY_GOOGLE_CALENDAR_ID=primary          # or a dedicated calendar ID
  RILEY_SYNC_DAYS=90
  RILEY_GOOGLE_MIRROR_DETAILS=false         # false = summaries become Busy

Google OAuth files expected from the google-workspace skill setup:
  google_token.json
  google_client_secret.json

Stdlib-only. Safe by default: run with --dry-run first.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def load_local_tz():
    name = os.environ.get("RILEY_CALENDAR_TIMEZONE", "America/Los_Angeles")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        # Windows Python often lacks the IANA tzdata package. Fall back to the
        # host's local timezone for runtime safety; install `tzdata` later for
        # exact historical/future DST rules if needed.
        return datetime.now().astimezone().tzinfo or timezone(timedelta(hours=-8))


LOCAL_TZ = load_local_tz()
SOURCE_PROP = "hermesSource"
SOURCE_VAL = "riley_icloud_mirror"
UID_PROP = "hermesSourceUid"
HASH_PROP = "hermesContentHash"


def hermes_homes() -> list[Path]:
    out: list[Path] = []
    if os.environ.get("HERMES_HOME"):
        out.append(Path(os.environ["HERMES_HOME"]))
    out.append(Path.home() / ".hermes")
    # de-dupe while preserving order
    seen = set()
    uniq = []
    for p in out:
        s = str(p).lower()
        if s not in seen:
            uniq.append(p); seen.add(s)
    return uniq


def load_env() -> None:
    for home in hermes_homes():
        path = home / ".env"
        if not path.exists():
            continue
        for line in path.read_text(errors="ignore").splitlines():
            if line and not line.lstrip().startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def find_file(name: str) -> Path | None:
    for home in hermes_homes():
        p = home / name
        if p.exists():
            return p
    return None


def unfold_ics(text: str) -> list[str]:
    out: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw.startswith((" ", "\t")) and out:
            out[-1] += raw[1:]
        else:
            out.append(raw)
    return out


def parse_dt(value: str, params: str = "") -> tuple[datetime | None, bool]:
    value = (value or "").strip()
    if not value:
        return None, False
    all_day = "VALUE=DATE" in params or ("T" not in value)
    tzid = None
    m = re.search(r"TZID=([^;:]+)", params)
    if m:
        tzid = m.group(1)
    try:
        if value.endswith("Z"):
            return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ), all_day
        if "T" in value:
            dt = datetime.strptime(value, "%Y%m%dT%H%M%S")
            zone = ZoneInfo(tzid) if tzid else LOCAL_TZ
            return dt.replace(tzinfo=zone).astimezone(LOCAL_TZ), all_day
        return datetime.strptime(value, "%Y%m%d").replace(tzinfo=LOCAL_TZ), True
    except Exception:
        return None, all_day


def ics_unescape(value: str) -> str:
    return (value or "").replace("\\n", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def fetch_ics(url: str) -> str:
    if url.startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    if re.match(r"https?://", url):
        req = urllib.request.Request(url, headers={"User-Agent": "Hermes-Riley-CalendarSync/1.0"})
        with urllib.request.urlopen(req, timeout=45) as resp:
            return resp.read().decode("utf-8", "replace")
    return Path(url).read_text(encoding="utf-8", errors="replace")


def parse_events(text: str, source_name: str, horizon_start: datetime, horizon_end: datetime, show_details: bool) -> list[dict[str, Any]]:
    lines = unfold_ics(text)
    raw_events: list[dict[str, str]] = []
    cur: dict[str, str] | None = None
    for line in lines:
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT" and cur is not None:
            raw_events.append(cur); cur = None
        elif cur is not None and ":" in line:
            left, val = line.split(":", 1)
            name = left.split(";", 1)[0].upper()
            # Keep first occurrence; enough for exported busy feeds.
            cur.setdefault(name, val)
            cur.setdefault(name + "_PARAMS", left)
    events: list[dict[str, Any]] = []
    for idx, e in enumerate(raw_events):
        start, start_all_day = parse_dt(e.get("DTSTART", ""), e.get("DTSTART_PARAMS", ""))
        end, end_all_day = parse_dt(e.get("DTEND", ""), e.get("DTEND_PARAMS", ""))
        if not start:
            continue
        if not end:
            end = start + timedelta(days=1 if start_all_day else 1/24)
        if end <= horizon_start or start >= horizon_end:
            continue
        uid = e.get("UID") or f"{source_name}:{idx}:{start.isoformat()}"
        recur = e.get("RECURRENCE-ID", "")
        source_uid = f"{source_name}:{uid}:{recur}" if recur else f"{source_name}:{uid}"
        summary = ics_unescape(e.get("SUMMARY", "Busy")).strip() or "Busy"
        gsummary = f"Busy - {source_name}" if not show_details else f"{source_name}: {summary}"
        all_day = start_all_day or end_all_day
        item: dict[str, Any] = {
            "source_uid": source_uid,
            "summary": gsummary,
            "description": "Mirrored iCloud busy block for Riley scheduling. Do not edit this mirrored event directly.",
            "transparency": "opaque",
            "visibility": "private",
            "extendedProperties": {"private": {SOURCE_PROP: SOURCE_VAL, UID_PROP: source_uid}},
        }
        if all_day:
            item["start"] = {"date": start.date().isoformat()}
            item["end"] = {"date": end.date().isoformat()}
        else:
            item["start"] = {"dateTime": start.isoformat(), "timeZone": str(LOCAL_TZ)}
            item["end"] = {"dateTime": end.isoformat(), "timeZone": str(LOCAL_TZ)}
        canonical = json.dumps({k: item[k] for k in ["summary", "description", "start", "end", "visibility", "transparency"]}, sort_keys=True)
        item["extendedProperties"]["private"][HASH_PROP] = hashlib.sha256(canonical.encode()).hexdigest()
        events.append(item)
    return events


def load_google_credentials() -> tuple[dict[str, Any], dict[str, Any], Path]:
    token_path = find_file("google_token.json")
    secret_path = find_file("google_client_secret.json")
    if not token_path or not secret_path:
        raise SystemExit("Google OAuth is not set up. Run the google-workspace setup for Calendar read/write first.")
    token = json.loads(token_path.read_text())
    secret = json.loads(secret_path.read_text())
    return token, secret, token_path


def refresh_access_token() -> str:
    token, secret, token_path = load_google_credentials()
    access = token.get("access_token")
    expiry = token.get("expiry") or token.get("expires_at")
    if access and expiry:
        try:
            # Handles ISO expiry strings used by google-auth.
            exp = datetime.fromisoformat(str(expiry).replace("Z", "+00:00"))
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp > datetime.now(timezone.utc) + timedelta(minutes=2):
                return access
        except Exception:
            pass
    installed = secret.get("installed") or secret.get("web") or {}
    client_id = installed.get("client_id")
    client_secret = installed.get("client_secret")
    refresh = token.get("refresh_token")
    if not (client_id and client_secret and refresh):
        raise SystemExit("Google token cannot be refreshed: missing client_id/client_secret/refresh_token.")
    data = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh,
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        new = json.loads(resp.read().decode())
    token.update(new)
    if "expires_in" in new:
        token["expiry"] = (datetime.now(timezone.utc) + timedelta(seconds=int(new["expires_in"]))).isoformat()
    token_path.write_text(json.dumps(token, indent=2), encoding="utf-8")
    return token["access_token"]


def gcal(method: str, path: str, token: str, payload: dict[str, Any] | None = None, query: dict[str, str] | None = None) -> dict[str, Any]:
    qs = urllib.parse.urlencode(query or {})
    url = "https://www.googleapis.com/calendar/v3" + path + (("?" + qs) if qs else "")
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Hermes-Riley-CalendarSync/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"Google Calendar API HTTP {e.code}: {body}")


def list_mirrored(calendar_id: str, token: str, time_min: datetime, time_max: datetime) -> dict[str, dict[str, Any]]:
    existing: dict[str, dict[str, Any]] = {}
    page_token = None
    while True:
        query = {
            "timeMin": time_min.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "timeMax": time_max.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "singleEvents": "true",
            "privateExtendedProperty": f"{SOURCE_PROP}={SOURCE_VAL}",
            "maxResults": "2500",
        }
        if page_token:
            query["pageToken"] = page_token
        data = gcal("GET", f"/calendars/{urllib.parse.quote(calendar_id, safe='')}/events", token, query=query)
        for ev in data.get("items", []):
            props = ev.get("extendedProperties", {}).get("private", {})
            uid = props.get(UID_PROP)
            if uid:
                existing[uid] = ev
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return existing


def sync(dry_run: bool) -> dict[str, Any]:
    load_env()
    days = int(os.environ.get("RILEY_SYNC_DAYS", "90"))
    calendar_id = os.environ.get("RILEY_GOOGLE_CALENDAR_ID", "primary")
    show_details = os.environ.get("RILEY_GOOGLE_MIRROR_DETAILS", "false").lower() in {"1", "true", "yes"}
    sources = []
    if os.environ.get("RILEY_PERSONAL_ICAL_URL"):
        sources.append(("Personal", os.environ["RILEY_PERSONAL_ICAL_URL"]))
    if os.environ.get("RILEY_FAMILY_ICAL_URL"):
        sources.append(("Family", os.environ["RILEY_FAMILY_ICAL_URL"]))
    if not sources:
        raise SystemExit("No iCloud source URLs configured. Set RILEY_PERSONAL_ICAL_URL and/or RILEY_FAMILY_ICAL_URL.")
    start = datetime.now(LOCAL_TZ) - timedelta(days=1)
    end = datetime.now(LOCAL_TZ) + timedelta(days=days)
    desired: dict[str, dict[str, Any]] = {}
    fetched_counts: dict[str, int] = {}
    for name, url in sources:
        events = parse_events(fetch_ics(url), name, start, end, show_details)
        fetched_counts[name] = len(events)
        for ev in events:
            desired[ev["source_uid"]] = ev
    if dry_run:
        return {"ok": True, "dry_run": True, "calendar_id": calendar_id, "sources": [s[0] for s in sources], "desired_events": len(desired), "source_counts": fetched_counts}
    token = refresh_access_token()
    existing = list_mirrored(calendar_id, token, start, end)
    created = updated = deleted = unchanged = 0
    cal_path = f"/calendars/{urllib.parse.quote(calendar_id, safe='')}/events"
    for uid, ev in desired.items():
        prev = existing.get(uid)
        props = ev["extendedProperties"]["private"]
        if not prev:
            gcal("POST", cal_path, token, payload=ev)
            created += 1
        elif prev.get("extendedProperties", {}).get("private", {}).get(HASH_PROP) != props[HASH_PROP]:
            gcal("PATCH", cal_path + "/" + urllib.parse.quote(prev["id"], safe=""), token, payload=ev)
            updated += 1
        else:
            unchanged += 1
    desired_uids = set(desired)
    for uid, prev in existing.items():
        if uid not in desired_uids:
            gcal("DELETE", cal_path + "/" + urllib.parse.quote(prev["id"], safe=""), token)
            deleted += 1
    return {"ok": True, "dry_run": False, "calendar_id": calendar_id, "sources": [s[0] for s in sources], "created": created, "updated": updated, "deleted": deleted, "unchanged": unchanged, "source_counts": fetched_counts}


def create_booking(args: argparse.Namespace) -> dict[str, Any]:
    load_env()
    calendar_id = args.calendar or os.environ.get("RILEY_GOOGLE_CALENDAR_ID", "primary")
    if not (args.summary and args.start and args.end):
        raise SystemExit("create-booking requires --summary, --start, and --end.")
    token = refresh_access_token()
    payload: dict[str, Any] = {
        "summary": args.summary,
        "start": {"dateTime": args.start, "timeZone": str(LOCAL_TZ)},
        "end": {"dateTime": args.end, "timeZone": str(LOCAL_TZ)},
        "description": args.description or "Booked by Riley/Hermes after caller confirmation.",
    }
    if args.location:
        payload["location"] = args.location
    if args.attendees:
        payload["attendees"] = [{"email": e.strip()} for e in args.attendees.split(",") if e.strip()]
    data = gcal("POST", f"/calendars/{urllib.parse.quote(calendar_id, safe='')}/events", token, payload=payload)
    return {"ok": True, "event_id": data.get("id"), "htmlLink": data.get("htmlLink"), "summary": data.get("summary")}


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("sync")
    s.add_argument("--dry-run", action="store_true")
    c = sub.add_parser("create-booking")
    c.add_argument("--summary")
    c.add_argument("--start")
    c.add_argument("--end")
    c.add_argument("--description", default="")
    c.add_argument("--location", default="")
    c.add_argument("--attendees", default="")
    c.add_argument("--calendar", default="")
    args = ap.parse_args()
    if args.cmd == "create-booking":
        result = create_booking(args)
    else:
        result = sync(dry_run=getattr(args, "dry_run", False))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
