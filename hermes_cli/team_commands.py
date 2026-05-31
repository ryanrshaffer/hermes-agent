"""Convenience slash-command helpers for team/Kanban delegation.

These helpers keep the messaging-platform `/delegate` and `/team` commands
as thin, deterministic wrappers over the durable Kanban board.  They do not
introduce a separate task system; every delegated item still becomes a normal
Kanban card assigned to a specialist profile.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from hermes_constants import get_hermes_home


SPECIALIST_PROFILES = (
    "hermes-prime",
    "codex-builder",
    "claude-reviewer",
    "ops-agent",
    "automation-agent",
    "inbox-agent",
    "memory-librarian",
    "researcher",
)


def _known_profiles() -> set[str]:
    profiles = set(SPECIALIST_PROFILES)
    profiles_dir = get_hermes_home() / "profiles"
    try:
        if profiles_dir.exists():
            profiles.update(p.name for p in profiles_dir.iterdir() if p.is_dir())
    except Exception:
        pass
    return profiles


def delegate_usage() -> str:
    profiles = ", ".join(SPECIALIST_PROFILES)
    return (
        "Usage: /delegate <profile> <task>\n"
        f"Profiles: {profiles}\n"
        "Example: /delegate researcher Compare three local payroll vendors and summarize risks"
    )


def team_usage() -> str:
    return (
        "Usage: /team <status|queue|assignees|stats>\n"
        "- /team status: profile roster plus board stats\n"
        "- /team queue: current non-archived Kanban queue\n"
        "- /team assignees: known specialist profiles and task counts\n"
        "- /team stats: board status counts"
    )


def delegate_to_kanban_args(text: str) -> str:
    """Translate `/delegate <profile> <task>` into `kanban create ...` args."""
    raw = (text or "").strip()
    if raw.startswith("/"):
        raw = raw[1:]
    if raw.lower().startswith("delegate"):
        raw = raw[len("delegate"):].strip()
    if not raw:
        raise ValueError(delegate_usage())

    parts = shlex.split(raw)
    if len(parts) < 2:
        raise ValueError(delegate_usage())

    profile = parts[0].strip()
    task = raw[len(parts[0]):].strip()
    if not task:
        raise ValueError(delegate_usage())

    known = _known_profiles()
    if profile not in known:
        raise ValueError(
            f"Unknown profile `{profile}`.\n" + delegate_usage()
        )

    first_line = task.splitlines()[0].strip()
    title = first_line[:96].rstrip() or f"Delegated task for {profile}"
    body = (
        "Delegated from platform /delegate command.\n\n"
        f"Assigned specialist profile: {profile}\n\n"
        "Task:\n"
        f"{task}"
    )
    return (
        "create "
        f"{shlex.quote(title)} "
        f"--body {shlex.quote(body)} "
        f"--assignee {shlex.quote(profile)} "
        "--created-by platform-delegate"
    )


def run_team_command(text: str) -> str:
    """Run `/team ...` by delegating to Kanban CLI read-only commands."""
    from hermes_cli.kanban import run_slash

    raw = (text or "").strip()
    if raw.startswith("/"):
        raw = raw[1:]
    if raw.lower().startswith("team"):
        raw = raw[len("team"):].strip()
    sub = (raw.split(None, 1)[0].lower() if raw else "status")

    if sub in {"help", "-h", "--help"}:
        return team_usage()
    if sub in {"status", "overview"}:
        assignees = run_slash("assignees")
        stats = run_slash("stats")
        return f"Team status\n\n{assignees}\n\n{stats}".strip()
    if sub in {"assignees", "profiles", "profile"}:
        return run_slash("assignees")
    if sub in {"stats", "counts"}:
        return run_slash("stats")
    if sub in {"queue", "tasks", "list"}:
        return run_slash("list --sort priority")
    raise ValueError(team_usage())
