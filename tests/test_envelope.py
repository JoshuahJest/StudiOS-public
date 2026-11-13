import unittest
from datetime import datetime, timedelta, timezone

from studios_coordination.envelope import validate_envelope


def valid_envelope(**overrides):
    now = datetime.now(timezone.utc)
    envelope = {
        "schema": "studios.task-envelope/v1",
        "task_id": "nas-canary",
        "run_id": "run-001",
        "attempt_number": 1,
        "origin": "wanda",
        "issuer_id": "enki-ii-controller",
        "issuer_key_id": "enki-key-1",
        "issuer_signature": "proposed-not-verified",
        "intended_host": "macbook-worker",
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "policy_digest": "policy-v1",
        "input_manifest": [{"ref": "fixture://synthetic-v1", "sha256": "a" * 64}],
        "allowed_capabilities": ["read-only-fixture"],
        "allowed_paths": ["~/StudiOS-1.1/fixtures"],
        "required_evidence": ["summary", "cleanup"],
        "idempotency_key": "idem-001",
        "kind": "read-only",
        "model_request": {
            "privacy_class": "internal",
            "execution_mode": "batch",
            "allowed_models": ["local-test-model"],
            "budget_class": "local-only",
            "max_runtime_seconds": 30,
        },
    }
    envelope.update(overrides)
    return envelope


class EnvelopeValidationTests(unittest.TestCase):
    def test_accepts_a_current_envelope_for_its_intended_host(self):
        result = validate_envelope(valid_envelope(), host_id="macbook-worker")

        self.assertTrue(result.accepted)
        self.assertIsNone(result.reason)
        self.assertEqual(len(result.envelope_digest), 64)

    def test_rejects_an_expired_envelope(self):
        result = validate_envelope(
            valid_envelope(expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()),
            host_id="macbook-worker",
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "expired")

    def test_rejects_a_raw_shell_command_payload(self):
        result = validate_envelope(
            valid_envelope(command="rm -rf /"),
            host_id="macbook-worker",
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "raw-command-payload")


if __name__ == "__main__":
    unittest.main()
