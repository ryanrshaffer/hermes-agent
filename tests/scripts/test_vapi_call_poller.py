from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class VapiCallPollerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        env = {
            "HERMES_HOME": str(self.root / "hermes-home"),
            "VAPI_CALL_LOG_DIR": str(self.root / "call-logs"),
        }
        with mock.patch.dict(os.environ, env), mock.patch.object(Path, "home", return_value=self.root):
            self.webhook = load_module(
                "vapi_voice_webhook_server", SCRIPTS / "vapi_voice_webhook_server.py"
            )
            self.poller = load_module("vapi_call_poller_under_test", SCRIPTS / "vapi_call_poller.py")
        self.vault = self.root / "vault"
        (self.vault / ".obsidian").mkdir(parents=True)
        (self.vault / ".obsidian" / "core-plugins.json").write_text(
            '{"sync": true}\n', encoding="utf-8"
        )
        (self.vault / "00 - Home").mkdir()
        (self.vault / "99 - System").mkdir()
        self.webhook.CANONICAL_OBSIDIAN_VAULT = self.vault
        self.webhook.OBSIDIAN_VAULT = str(self.vault)

    def tearDown(self):
        sys.modules.pop("vapi_call_poller_under_test", None)
        sys.modules.pop("vapi_voice_webhook_server", None)
        self.tempdir.cleanup()

    def test_obsidian_note_path_is_stable_for_call_id(self):
        summary = {
            "call_id": "call/test-123",
            "customer_name": "First Name",
            "customer_number": "",
            "urgency": "routine",
            "started": "2026-07-10T22:30:00Z",
            "ended": "2026-07-10T22:35:00Z",
            "ended_reason": "customer-ended-call",
            "cost": "0.10",
            "recording": "https://recording.example/private-call",
            "summary": "First attempt",
            "transcript": "Hello",
        }
        vault = self.root / "vault"
        with mock.patch.object(self.webhook, "OBSIDIAN_VAULT", str(vault)):
            first_path = self.webhook._write_obsidian(summary, {"attempt": 1})
            summary["customer_name"] = "Updated Name"
            summary["summary"] = "Retry"
            second_path = self.webhook._write_obsidian(
                summary,
                {
                    "attempt": 2,
                    "transcript": "Hello",
                    "recordingUrl": "https://recording.example/private-call",
                },
            )

        self.assertEqual(first_path, second_path)
        self.assertEqual(first_path.name, "calltest-123.md")
        self.assertEqual(first_path.parent.name, "2026-07-10")
        self.assertEqual(len(list(vault.rglob("*.md"))), 1)
        self.assertEqual(len(list(first_path.parent.glob("*.json"))), 1)
        raw = json.loads(first_path.with_suffix(".json").read_text())
        self.assertEqual(raw["attempt"], 2)
        self.assertEqual(raw["transcript"], "Hello")
        self.assertEqual(raw["recordingUrl"], "https://recording.example/private-call")
        self.assertIn("https://recording.example/private-call", first_path.read_text())
        self.assertIn("Hello", first_path.read_text())

    def test_obsidian_write_rejects_noncanonical_vault_before_mkdir(self):
        alternate = self.root / "OneDrive" / "AI Workspace"
        summary = {
            "call_id": "call-123",
            "customer_name": "Caller",
            "started": "2026-07-10T22:30:00Z",
        }

        with mock.patch.object(self.webhook, "OBSIDIAN_VAULT", str(alternate)):
            with self.assertRaisesRegex(RuntimeError, "not canonical"):
                self.webhook._write_obsidian(summary, {})

        self.assertFalse(alternate.exists())

    def test_webhook_has_no_external_delivery_implementation(self):
        source = (SCRIPTS / "vapi_voice_webhook_server.py").read_text(encoding="utf-8")

        self.assertFalse(hasattr(self.webhook, "_post_discord"))
        self.assertNotIn("discord.com/api", source)
        self.assertNotIn("DISCORD_VOICE_CALL_FORUM_ID", source)
        self.assertNotIn("VAPI_ESCALATION_NUMBER", source)

    def test_transfer_request_fails_closed_without_disclosing_a_number(self):
        status, body, _headers = self.webhook.handle_payload(
            {"message": {"type": "transfer-destination-request", "id": "call-123"}}
        )

        self.assertEqual(status, 409)
        response = json.loads(body)
        self.assertEqual(
            response,
            {
                "error": "external_communication_requires_explicit_approval",
                "transfer_authorized": False,
            },
        )
        self.assertNotIn("number", response)

    def test_webhook_stays_local_when_obsidian_write_fails(self):
        payload = {
            "message": {
                "type": "end-of-call-report",
                "id": "call-123",
                "summary": "Call summary",
                "transcript": "PRIVATE FULL TRANSCRIPT",
                "recordingUrl": "https://recording.example/private-call",
            }
        }

        with (
            mock.patch.object(self.webhook, "_write_obsidian", side_effect=OSError("disk full")),
            mock.patch.object(self.webhook.traceback, "print_exc"),
        ):
            status, body, _headers = self.webhook.handle_payload(payload)

        self.assertEqual(status, 200)
        response = json.loads(body)
        self.assertFalse(response["obsidian_note_written"])
        self.assertEqual(response["external_delivery"], "not_attempted_approval_required")
        self.assertNotIn(str(self.root), response)

    def test_webhook_response_exposes_only_local_status(self):
        payload = {
            "message": {
                "type": "end-of-call-report",
                "id": "private-call-id",
                "summary": "Call summary",
                "transcript": "PRIVATE FULL TRANSCRIPT",
                "recordingUrl": "https://recording.example/private-call",
            }
        }
        note = self.root / "vault" / "OwnerOps" / "Voice Calls" / "call.md"

        with mock.patch.object(self.webhook, "_write_obsidian", return_value=note):
            status, body, _headers = self.webhook.handle_payload(payload)

        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body),
            {
                "ok": True,
                "obsidian_note_written": True,
                "external_delivery": "not_attempted_approval_required",
            },
        )

    def test_no_new_calls_does_not_rewrite_processed_state(self):
        state_dir = self.root / "state"
        state_dir.mkdir()
        (state_dir / "processed_calls.json").write_text('["existing-call"]', encoding="utf-8")

        with (
            mock.patch.dict(self.poller.os.environ, {"VAPI_CALL_LOG_DIR": str(state_dir)}),
            mock.patch.object(self.poller, "load_env"),
            mock.patch.object(self.poller, "vapi_get", return_value=[]),
            mock.patch.object(self.poller, "save_processed_state") as save_state,
        ):
            self.poller.main()

        save_state.assert_not_called()

    def test_corrupt_processed_state_fails_closed(self):
        state = self.root / "processed_calls.json"
        state.write_text("{broken", encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "state is unreadable"):
            self.poller.load_processed_state(state)

        self.assertEqual(state.read_text(encoding="utf-8"), "{broken")

    def test_new_call_persists_processed_state_without_external_post(self):
        state_dir = self.root / "state"
        calls = [{"id": "new-call", "status": "ended"}]
        note = self.root / "call.md"
        note.write_text("accepted note", encoding="utf-8")

        def vapi_get(path: str):
            return calls if path == "/call?limit=25" else {"id": "new-call", "status": "ended"}

        stdout = io.StringIO()
        with (
            mock.patch.dict(self.poller.os.environ, {"VAPI_CALL_LOG_DIR": str(state_dir)}),
            mock.patch.object(self.poller, "load_env"),
            mock.patch.object(self.poller, "vapi_get", side_effect=vapi_get),
            mock.patch.object(self.poller, "_summarize_call", return_value={"call_id": "new-call"}),
            mock.patch.object(self.poller, "_write_obsidian", return_value=note),
            redirect_stdout(stdout),
        ):
            self.poller.main()

        state = state_dir / "processed_calls.json"
        self.assertEqual(json.loads(state.read_text(encoding="utf-8")), ["new-call"])
        self.assertFalse(list(state_dir.glob("*.tmp")))
        self.assertEqual(stdout.getvalue().strip(), "logged 1 voice call(s) locally")
        self.assertNotIn("new-call", stdout.getvalue())
        self.assertNotIn(str(self.root), stdout.getvalue())
        self.assertNotIn("_post_discord", (SCRIPTS / "vapi_call_poller.py").read_text())

    def test_invalid_list_schema_is_persistent_failure(self):
        state_dir = self.root / "state"
        stdout = io.StringIO()
        with (
            mock.patch.dict(self.poller.os.environ, {"VAPI_CALL_LOG_DIR": str(state_dir)}),
            mock.patch.object(self.poller, "load_env"),
            mock.patch.object(self.poller, "vapi_get", return_value={"unexpected": []}),
            redirect_stdout(stdout),
        ):
            result = self.poller.main()

        self.assertEqual(result, 2)
        self.assertIn("invalid schema", stdout.getvalue())
        status = json.loads((state_dir / "poller_status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["status"], "blocked")

    def test_later_call_failure_does_not_lose_prior_checkpoint(self):
        state_dir = self.root / "state"
        calls = [
            {"id": "first-call", "status": "ended"},
            {"id": "second-call", "status": "ended"},
        ]
        note = self.root / "call.md"
        note.write_text("accepted note", encoding="utf-8")

        def vapi_get(path: str):
            if path == "/call?limit=25":
                return calls
            call_id = path.rsplit("/", 1)[-1]
            return {"id": call_id, "status": "ended"}

        def summarize(message):
            if message["id"] == "second-call":
                raise RuntimeError("controlled failure")
            return {"call_id": message["id"]}

        with (
            mock.patch.dict(self.poller.os.environ, {"VAPI_CALL_LOG_DIR": str(state_dir)}),
            mock.patch.object(self.poller, "load_env"),
            mock.patch.object(self.poller, "vapi_get", side_effect=vapi_get),
            mock.patch.object(self.poller, "_summarize_call", side_effect=summarize),
            mock.patch.object(self.poller, "_write_obsidian", return_value=note),
        ):
            result = self.poller.main()

        self.assertEqual(result, 2)
        self.assertEqual(
            json.loads((state_dir / "processed_calls.json").read_text(encoding="utf-8")),
            ["first-call"],
        )


if __name__ == "__main__":
    unittest.main()
