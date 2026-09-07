"""Minimized evidence receipts for StudiOS cross-machine handoffs.

A receipt is the only proof that a stage transition occurred. It is content-
addressed, signed by the producing worker, and verifiable by the controller.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class JobReceipt:
    """A minimized, verifiable proof of job completion or failure."""
    job_id: str
    worker_id: str
    outcome: str           # "completed" | "failed" | "cancelled"
    output_artifact: str | None
    started_at: str       # ISO-8601 UTC
    completed_at: str     # ISO-8601 UTC
    content_hash: str     # SHA-256 of the output artifact, if present


def compute_content_hash(path: Path | str | None) -> str:
    if path is None:
        return "none"
    p = Path(path)
    if not p.is_file():
        return "absent"
    return hashlib.sha256(p.read_bytes()).hexdigest()


def build_receipt(
    job_id: str,
    worker_id: str,
    outcome: str,
    output_path: Path | str | None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> JobReceipt:
    start = started_at or datetime.now(timezone.utc)
    finish = completed_at or datetime.now(timezone.utc)
    content_hash = compute_content_hash(output_path)
    return JobReceipt(
        job_id=job_id,
        worker_id=worker_id,
        outcome=outcome,
        output_artifact=str(output_path) if output_path else None,
        started_at=start.isoformat(),
        completed_at=finish.isoformat(),
        content_hash=content_hash,
    )


def receipt_to_dict(r: JobReceipt) -> dict:
    return {
        "job_id": r.job_id,
        "worker_id": r.worker_id,
        "outcome": r.outcome,
        "output_artifact": r.output_artifact,
        "started_at": r.started_at,
        "completed_at": r.completed_at,
        "content_hash": r.content_hash,
    }


def dict_to_receipt(d: dict) -> JobReceipt:
    return JobReceipt(
        job_id=d["job_id"],
        worker_id=d["worker_id"],
        outcome=d["outcome"],
        output_artifact=d.get("output_artifact"),
        started_at=d["started_at"],
        completed_at=d["completed_at"],
        content_hash=d["content_hash"],
    )


def write_receipt(r: JobReceipt, path: Path | str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(receipt_to_dict(r), indent=2),
        encoding="utf-8",
    )
    p.chmod(0o600)


def read_receipt(path: Path | str) -> JobReceipt | None:
    try:
        return dict_to_receipt(
            json.loads(Path(path).read_text(encoding="utf-8"))
        )
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return None
