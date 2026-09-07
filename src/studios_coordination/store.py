"""SQLite store for StudiOS job, attempt, lease, and audit state."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .contracts import (
    AttemptRecord,
    AttemptState,
    AuditEvent,
    JobRecord,
    JobState,
    LeaseRecord,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Store:
    """SQLite-backed StudiOS state machine.

    Schema
    ------
    jobs:    (job_id, task_kind, privacy_class, execution_mode, created_at,
              updated_at, state, assigned_worker, input_artifact, output_artifact)
    attempts: (attempt_id, job_id, worker_id, created_at, state,
               runtime_seconds, error_msg)
    leases:   (job_id, worker_id, created_at, expires_at, fencing_token)
    audit:    (event_id, occurred_at, job_id, worker_id, event_type, payload)
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS jobs (
        job_id          TEXT PRIMARY KEY,
        task_kind       TEXT NOT NULL,
        privacy_class   TEXT NOT NULL DEFAULT 'private',
        execution_mode  TEXT NOT NULL DEFAULT 'batch',
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL,
        state           TEXT NOT NULL DEFAULT 'proposed',
        assigned_worker TEXT,
        input_artifact  TEXT,
        output_artifact TEXT
    );
    CREATE TABLE IF NOT EXISTS attempts (
        attempt_id      TEXT PRIMARY KEY,
        job_id          TEXT NOT NULL REFERENCES jobs(job_id),
        worker_id       TEXT NOT NULL,
        created_at      TEXT NOT NULL,
        state           TEXT NOT NULL DEFAULT 'pending',
        runtime_seconds REAL,
        error_msg       TEXT
    );
    CREATE TABLE IF NOT EXISTS leases (
        job_id          TEXT PRIMARY KEY,
        worker_id       TEXT NOT NULL,
        created_at      TEXT NOT NULL,
        expires_at      TEXT NOT NULL,
        fencing_token   TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS audit (
        event_id        INTEGER PRIMARY KEY AUTOINCREMENT,
        occurred_at     TEXT NOT NULL,
        job_id          TEXT,
        worker_id       TEXT,
        event_type      TEXT NOT NULL,
        payload         TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS ix_jobs_state      ON jobs(state);
    CREATE INDEX IF NOT EXISTS ix_attempts_job_id ON attempts(job_id);
    CREATE INDEX IF NOT EXISTS ix_audit_job_id   ON audit(job_id);
    """

    def __init__(self, db_path: Path | str = "studios.db"):
        self.db = sqlite3.connect(db_path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")  # safe with WAL, faster
        self.db.row_factory = sqlite3.Row
        self.db.executescript(self.SCHEMA)

    # ── Jobs ────────────────────────────────────────────────────────────────

    def insert_job(self, rec: JobRecord) -> None:
        now = _utcnow().isoformat()
        self.db.execute(
            """
            INSERT INTO jobs
                (job_id, task_kind, privacy_class, execution_mode,
                 created_at, updated_at, state, assigned_worker,
                 input_artifact, output_artifact)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rec.job_id, rec.task_kind, rec.privacy_class,
                rec.execution_mode, now, now,
                rec.state.value, rec.assigned_worker,
                rec.input_artifact, rec.output_artifact,
            ),
        )
        self.db.commit()

    def get_job(self, job_id: str) -> JobRecord | None:
        row = self.db.execute(
            "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return _row_to_job(row) if row else None

    def update_job_state(
        self, job_id: str, state: JobState, worker: str | None = None
    ) -> bool:
        now = _utcnow().isoformat()
        cur = self.db.execute(
            "UPDATE jobs SET state = ?, updated_at = ?, assigned_worker = ? "
            "WHERE job_id = ?",
            (state.value, now, worker, job_id),
        )
        self.db.commit()
        return cur.rowcount > 0

    def list_jobs(self, state: JobState | None = None) -> Iterator[JobRecord]:
        sql = "SELECT * FROM jobs"
        args: list = []
        if state:
            sql += " WHERE state = ?"
            args.append(state.value)
        sql += " ORDER BY created_at DESC"
        for row in self.db.execute(sql, args):
            rec = _row_to_job(row)
            if rec:
                yield rec

    # ── Attempts ──────────────────────────────────────────────────────────

    def insert_attempt(self, rec: AttemptRecord) -> None:
        self.db.execute(
            """
            INSERT INTO attempts
                (attempt_id, job_id, worker_id, created_at, state,
                 runtime_seconds, error_msg)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rec.attempt_id, rec.job_id, rec.worker_id,
                _utcnow().isoformat(), rec.state.value,
                rec.runtime_seconds, rec.error_msg,
            ),
        )
        self.db.commit()

    def update_attempt_state(
        self, attempt_id: str, state: AttemptState,
        runtime_seconds: float | None = None, error_msg: str | None = None
    ) -> bool:
        cur = self.db.execute(
            "UPDATE attempts SET state = ?, runtime_seconds = ?, error_msg = ? "
            "WHERE attempt_id = ?",
            (state.value, runtime_seconds, error_msg, attempt_id),
        )
        self.db.commit()
        return cur.rowcount > 0

    # ── Leases ─────────────────────────────────────────────────────────────

    def acquire_lease(self, rec: LeaseRecord) -> bool:
        try:
            self.db.execute(
                """
                INSERT INTO leases
                    (job_id, worker_id, created_at, expires_at, fencing_token)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    rec.job_id, rec.worker_id,
                    _utcnow().isoformat(),
                    rec.expires_at,
                    rec.fencing_token,
                ),
            )
            self.db.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def get_lease(self, job_id: str) -> LeaseRecord | None:
        row = self.db.execute(
            "SELECT * FROM leases WHERE job_id = ?", (job_id,)
        ).fetchone()
        return _row_to_lease(row) if row else None

    def release_lease(self, job_id: str, worker_id: str) -> bool:
        cur = self.db.execute(
            "DELETE FROM leases WHERE job_id = ? AND worker_id = ?",
            (job_id, worker_id),
        )
        self.db.commit()
        return cur.rowcount > 0

    # ── Audit ──────────────────────────────────────────────────────────────

    def append_audit(
        self,
        event_type: str,
        job_id: str | None = None,
        worker_id: str | None = None,
        **payload_fields: str,
    ) -> None:
        import json
        self.db.execute(
            "INSERT INTO audit (occurred_at, job_id, worker_id, event_type, payload) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                _utcnow().isoformat(), job_id, worker_id,
                event_type, json.dumps(payload_fields),
            ),
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()


# ── Helpers ────────────────────────────────────────────────────────────────

def _row_to_job(row: sqlite3.Row) -> JobRecord | None:
    if row is None:
        return None
    try:
        return JobRecord(
            job_id=row["job_id"],
            task_kind=row["task_kind"],
            privacy_class=row["privacy_class"],
            execution_mode=row["execution_mode"],
            state=JobState(row["state"]),
            assigned_worker=row["assigned_worker"],
            input_artifact=row["input_artifact"],
            output_artifact=row["output_artifact"],
        )
    except (KeyError, ValueError):
        return None


def _row_to_lease(row: sqlite3.Row) -> LeaseRecord | None:
    if row is None:
        return None
    try:
        return LeaseRecord(
            job_id=row["job_id"],
            worker_id=row["worker_id"],
            expires_at=row["expires_at"],
            fencing_token=row["fencing_token"],
        )
    except (KeyError, ValueError):
        return None
