"""Security tests for terminal session snapshots and Windows cache ACLs."""

import os
import subprocess
import time

from tools.environments import local as local_mod
from tools.environments.base import _snapshot_export_command


def test_snapshot_export_excludes_credentials_but_keeps_operational_auth_vars(tmp_path):
    snapshot = tmp_path / "snapshot.sh"
    quoted_path = "'" + str(snapshot).replace("'", "'\\''") + "'"
    secret_value = "terminal-snapshot-test-credential"
    script = "\n".join(
        (
            f"export TEST_API_KEY='{secret_value}'",
            f"export lower_case_password='{secret_value}'",
            f"export DATABASE_URL='{secret_value}'",
            f"declare -rx READ_ONLY_ACCESS_KEY='{secret_value}'",
            f"export GITHUB_PAT='{secret_value}'",
            f"export CLOUD_ACCOUNT_KEY='{secret_value}'",
            f"export AZURE_SUBSCRIPTION_KEY='{secret_value}'",
            f"export ALERT_WEBHOOK_URL='{secret_value}'",
            f"export SENTRY_DSN='{secret_value}'",
            f"export SMTP_URL='{secret_value}'",
            f"export AMQP_URL='{secret_value}'",
            f"export BROKER_URL='{secret_value}'",
            "export USE_LOCAL_OAUTH='true'",
            "export SSH_AUTH_SOCK='/tmp/agent.sock'",
            "export CLAUDE_CODE_OAUTH_SCOPES='profile'",
            "export HERMES_REDACT_SECRETS='true'",
            "export TOKENIZERS_PARALLELISM='false'",
            _snapshot_export_command(quoted_path),
        )
    )

    bash = local_mod._find_bash()
    subprocess.run([bash, "-c", script], check=True, timeout=10)
    persisted = snapshot.read_text(encoding="utf-8")

    assert secret_value not in persisted
    assert "TEST_API_KEY" not in persisted
    assert "lower_case_password" not in persisted
    assert "DATABASE_URL" not in persisted
    assert "READ_ONLY_ACCESS_KEY" not in persisted
    assert "GITHUB_PAT" not in persisted
    assert "CLOUD_ACCOUNT_KEY" not in persisted
    assert "AZURE_SUBSCRIPTION_KEY" not in persisted
    assert "ALERT_WEBHOOK_URL" not in persisted
    assert "SENTRY_DSN" not in persisted
    assert "SMTP_URL" not in persisted
    assert "AMQP_URL" not in persisted
    assert "BROKER_URL" not in persisted
    assert "USE_LOCAL_OAUTH" in persisted
    assert "SSH_AUTH_SOCK" in persisted
    assert "CLAUDE_CODE_OAUTH_SCOPES" in persisted
    assert "HERMES_REDACT_SECRETS" in persisted
    assert "TOKENIZERS_PARALLELISM" in persisted
    assert "PATH" in persisted

    roundtrip_env = os.environ.copy()
    roundtrip_env["TEST_API_KEY"] = secret_value
    roundtrip = subprocess.run(
        [
            bash,
            "-c",
            f"source {quoted_path} >/dev/null 2>&1; "
            'test -n "$TEST_API_KEY" && test "$USE_LOCAL_OAUTH" = true',
        ],
        env=roundtrip_env,
        check=False,
        timeout=10,
    )
    assert roundtrip.returncode == 0


def test_stale_cleanup_is_age_bounded_and_preserves_unrelated_cache(tmp_path):
    now = time.time()
    old_snap = tmp_path / "hermes-snap-0123456789ab.sh"
    old_cwd = tmp_path / "hermes-cwd-abcdef012345.txt"
    recent_snap = tmp_path / "hermes-snap-fedcba987654.sh"
    unrelated = tmp_path / "application-cache.json"
    invalid_name = tmp_path / "hermes-snap-not-a-session.sh"
    result_dir = tmp_path / "hermes-results"
    result_dir.mkdir()
    old_result = result_dir / "call_0123456789abcdef.txt"
    recent_result = result_dir / "toolu_fedcba9876543210.txt"
    unrelated_result = result_dir / "notes.json"
    invalid_result = result_dir / ".txt"
    for path in (old_snap, old_cwd, recent_snap, unrelated, invalid_name):
        path.write_text("test", encoding="utf-8")
    for path in (old_result, recent_result, unrelated_result, invalid_result):
        path.write_text("test", encoding="utf-8")
    old_time = now - local_mod._TERMINAL_ARTIFACT_MAX_AGE_SECONDS - 1
    os.utime(old_snap, (old_time, old_time))
    os.utime(old_cwd, (old_time, old_time))
    old_result_time = now - local_mod._TOOL_RESULT_ARTIFACT_MAX_AGE_SECONDS - 1
    os.utime(old_result, (old_result_time, old_result_time))

    removed = local_mod._cleanup_stale_terminal_artifacts(tmp_path, now=now)

    assert removed == 3
    assert not old_snap.exists()
    assert not old_cwd.exists()
    assert not old_result.exists()
    assert recent_snap.exists()
    assert recent_result.exists()
    assert unrelated.exists()
    assert invalid_name.exists()
    assert unrelated_result.exists()
    assert invalid_result.exists()


def test_windows_acl_is_reset_to_owner_system_and_administrators(monkeypatch, tmp_path):
    current_sid = "S-1-5-21-100-200-300-400"
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[0] == "whoami":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=f'"MACHINE\\user","{current_sid}"\n',
            )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(local_mod, "_IS_WINDOWS", True)
    monkeypatch.setattr(local_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(local_mod, "windows_hide_flags", lambda: 0)

    local_mod._secure_windows_terminal_cache_dir(tmp_path)

    commands = [call[0] for call in calls]
    assert commands[0][:2] == ["whoami", "/user"]
    assert commands[1][-2:] == ["/reset", "/Q"]
    assert commands[2][2] == "/grant:r"
    assert f"*{current_sid}:(OI)(CI)F" in commands[2]
    assert f"*{local_mod._WINDOWS_SYSTEM_SID}:(OI)(CI)F" in commands[2]
    assert f"*{local_mod._WINDOWS_ADMINISTRATORS_SID}:(OI)(CI)F" in commands[2]
    assert commands[3][-2:] == ["/inheritance:r", "/Q"]
