"""Canonical typed records for StudiOS coordination."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


# ── Enums ──────────────────────────────────────────────────────────────────

class JobState(str, Enum):
    PROPOSED   = "proposed"
    SCHEDULED  = "scheduled"
    RUNNING    = "running"
    COMPLETED  = "completed"
    FAILED     = "failed"
    CANCELLED  = "cancelled"


class AttemptState(str, Enum):
    PENDING    = "pending"
    ACQUIRED   = "acquired"
    COMPLETED  = "completed"
    ABORTED    = "aborted"
    RETRYING   = "retrying"


# ── Records ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class JobRecord:
    job_id: str
    task_kind: str
    privacy_class: str = "private"          # private | restricted | public
    execution_mode: str = "batch"            # batch | interactive
    state: JobState = JobState.PROPOSED
    assigned_worker: str | None = None
    input_artifact: str | None = None
    output_artifact: str | None = None


@dataclass(frozen=True)
class AttemptRecord:
    attempt_id: str
    job_id: str
    worker_id: str
    state: AttemptState = AttemptState.PENDING
    runtime_seconds: float | None = None
    error_msg: str | None = None


@dataclass(frozen=True)
class LeaseRecord:
    job_id: str
    worker_id: str
    expires_at: str          # ISO-8601 UTC
    fencing_token: str       # monotonic, per-job unique


@dataclass(frozen=True)
class AuditEvent:
    event_type: str
    occurred_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    job_id: str | None = None
    worker_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
