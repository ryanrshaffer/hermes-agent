"""Tests for /delegate and /team Kanban convenience commands."""

from pathlib import Path

import pytest

from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS, resolve_command
from hermes_cli.team_commands import delegate_to_kanban_args, run_team_command


def test_delegate_command_registered_for_cli_and_gateway():
    assert resolve_command("delegate").name == "delegate"
    assert resolve_command("team").name == "team"
    assert "delegate" in GATEWAY_KNOWN_COMMANDS
    assert "team" in GATEWAY_KNOWN_COMMANDS


def test_delegate_to_kanban_args_assigns_specialist_profile():
    args = delegate_to_kanban_args(
        "/delegate researcher Compare three local payroll vendors and summarize risks"
    )

    assert args.startswith("create ")
    assert "--assignee researcher" in args
    assert "--created-by platform-delegate" in args
    assert "Compare three local payroll vendors" in args


def test_delegate_rejects_unknown_profile(tmp_path, monkeypatch):
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    monkeypatch.setattr("hermes_cli.team_commands.get_hermes_home", lambda: tmp_path)

    with pytest.raises(ValueError, match="Unknown profile"):
        delegate_to_kanban_args("/delegate not-a-real-profile do work")


def test_delegate_accepts_profile_directory(tmp_path, monkeypatch):
    (tmp_path / "profiles" / "custom-agent").mkdir(parents=True)
    monkeypatch.setattr("hermes_cli.team_commands.get_hermes_home", lambda: tmp_path)

    args = delegate_to_kanban_args("/delegate custom-agent Write the handoff")

    assert "--assignee custom-agent" in args
    assert "Write the handoff" in args


def test_team_status_uses_read_only_kanban_commands(monkeypatch):
    calls: list[str] = []

    def fake_run_slash(text: str) -> str:
        calls.append(text)
        return f"OUT:{text}"

    monkeypatch.setattr("hermes_cli.kanban.run_slash", fake_run_slash)

    output = run_team_command("/team status")

    assert calls == ["assignees", "stats"]
    assert "Team status" in output
    assert "OUT:assignees" in output
    assert "OUT:stats" in output


def test_team_queue_uses_read_only_list(monkeypatch):
    calls: list[str] = []

    def fake_run_slash(text: str) -> str:
        calls.append(text)
        return "queue"

    monkeypatch.setattr("hermes_cli.kanban.run_slash", fake_run_slash)

    assert run_team_command("/team queue") == "queue"
    assert calls == ["list --sort priority"]
