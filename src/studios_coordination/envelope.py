"""Local-only validation for proposed StudiOS task envelopes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any


@dataclass(frozen=True)
class ValidationResult:
    accepted: bool
    reason: str | None
    envelope_digest: str


def validate_envelope(envelope: dict[str, Any], *, host_id: str) -> ValidationResult:
    """Validate host targeting without executing, persisting, or networking."""
    canonical = json.dumps(envelope, sort_keys=True, separators=(",", ":"))
    digest = sha256(canonical.encode("utf-8")).hexdigest()
    if "command" in envelope:
        return ValidationResult(False, "raw-command-payload", digest)
    if envelope.get("intended_host") != host_id:
        return ValidationResult(False, "wrong-intended-host", digest)
    try:
        expires_at = datetime.fromisoformat(envelope["expires_at"].replace("Z", "+00:00"))
    except (KeyError, AttributeError, ValueError):
        return ValidationResult(False, "invalid-expiry", digest)
    if expires_at.tzinfo is None or expires_at <= datetime.now(timezone.utc):
        return ValidationResult(False, "expired", digest)
    return ValidationResult(True, None, digest)
