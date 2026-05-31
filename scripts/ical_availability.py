#!/usr/bin/env python3
"""Check availability from an iCalendar (.ics/webcal) feed.

Reads ICAL_URL from env unless passed as --url. Stdlib-only parser for common VEVENT
DTSTART/DTEND forms. Prints JSON with busy blocks and suggested free slots.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def load_local_tz():
    name = os.environ.get("ICAL_TIMEZONE", os.environ.get("RILEY_CALENDAR_TIMEZONE", "America/Los_Angeles"))
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return datetime.now().astimezone().tzinfo or timezone(timedelta(hours=-8))


LOCAL_TZ = load_local_tz()


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


def unfold_ics(text: str) -> list[str]:
    out: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw.startswith((" ", "\t")) and out:
            out[-1] += raw[1:]
        else:
            out.append(raw)
    return out


def parse_dt(value: str, params: str = "") -> datetime | None:
    value = value.strip()
    tzid = None
    m = re.search(r"TZID=([^;:]+)", params)
    if m:
        tzid = m.group(1)
    try:
        if value.endswith("Z"):
            return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ)
        if "T" in value:
            dt = datetime.strptime(value, "%Y%m%dT%H%M%S")
            zone = ZoneInfo(tzid) if tzid else LOCAL_TZ
            return dt.replace(tzinfo=zone).astimezone(LOCAL_TZ)
        # all-day events
        return datetime.strptime(value, "%Y%m%d").replace(tzinfo=LOCAL_TZ)
    except Exception:
        return None


def fetch_ics(url: str) -> str:
    if url.startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    if re.match(r"https?://", url):
        req = urllib.request.Request(url, headers={"User-Agent": "Hermes-iCalendar/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", "replace")
    return Path(url).read_text(encoding="utf-8", errors="replace")


def parse_events(text: str) -> list[dict]:
    lines = unfold_ics(text)
    events = []
    cur: dict[str, str] | None = None
    for line in lines:
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT" and cur is not None:
            events.append(cur); cur = None
        elif cur is not None and ":" in line:
            left, val = line.split(":", 1)
            name = left.split(";", 1)[0].upper()
            cur[name] = val
            cur[name + "_PARAMS"] = left
    return events


def free_slots(busy: list[tuple[datetime, datetime, str]], start: datetime, end: datetime, minutes: int) -> list[dict]:
    slots = []
    day = start.date()
    cur_day = datetime.combine(day, datetime.min.time(), tzinfo=LOCAL_TZ)
    while cur_day < end:
        window_start = max(cur_day.replace(hour=9, minute=0), start)
        window_end = min(cur_day.replace(hour=17, minute=0), end)
        cursor = window_start
        for b0, b1, _ in sorted([b for b in busy if b[1] > window_start and b[0] < window_end]):
            if cursor + timedelta(minutes=minutes) <= b0:
                slots.append({"start": cursor.isoformat(), "end": (cursor + timedelta(minutes=minutes)).isoformat()})
            cursor = max(cursor, b1)
        if cursor + timedelta(minutes=minutes) <= window_end:
            slots.append({"start": cursor.isoformat(), "end": (cursor + timedelta(minutes=minutes)).isoformat()})
        cur_day += timedelta(days=1)
    return slots[:10]


def main() -> None:
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("ICAL_URL", ""))
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--duration", type=int, default=30)
    args = ap.parse_args()
    if not args.url:
        raise SystemExit("ICAL_URL missing. Set it in Hermes .env or pass --url.")
    now = datetime.now(LOCAL_TZ)
    horizon = now + timedelta(days=args.days)
    events = parse_events(fetch_ics(args.url))
    busy = []
    for e in events:
        start = parse_dt(e.get("DTSTART", ""), e.get("DTSTART_PARAMS", ""))
        end = parse_dt(e.get("DTEND", ""), e.get("DTEND_PARAMS", ""))
        if start and end and end > now and start < horizon:
            busy.append((max(start, now), min(end, horizon), e.get("SUMMARY", "Busy")))
    print(json.dumps({
        "timezone": str(LOCAL_TZ),
        "range_start": now.isoformat(),
        "range_end": horizon.isoformat(),
        "busy_count": len(busy),
        "busy": [{"start": a.isoformat(), "end": b.isoformat(), "summary": s} for a,b,s in busy[:50]],
        "suggested_free_slots": free_slots(busy, now, horizon, args.duration),
    }, indent=2))


if __name__ == "__main__":
    main()
