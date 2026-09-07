"""Loopback transport for StudiOS job coordination.

The LoopbackTransport moves proposals, claims, leases, and receipts through
filesystem paths — no network required. It is the only legal transport for
Stages 0-2 and all deterministic tests.

The transport does not authorize execution. It only moves structured
documents between a controller and a worker on the same host.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from .contracts import JobRecord, JobState


PROPOSALS_DIR = "proposals"   # controller → worker
CLAIMS_DIR    = "claims"      # worker → controller
RECEIPTS_DIR  = "receipts"   # worker → controller


class LoopbackTransport:
    """Filesystem-based coordination transport for single-host or loopback use.

    Directory layout (relative to base_dir):
        proposals/   <job_id>.json   — controller writes, worker reads
        claims/      <job_id>.json   — worker writes, controller reads
        receipts/    <job_id>.json   — worker writes, controller reads
        leases/      <job_id>.json   — atomic lock files, optional

    All files are mode 0600 — owner read/write only.
    """

    def __init__(self, base_dir: Path | str = "studios_transport"):
        self.base = Path(base_dir)
        for sub in (PROPOSALS_DIR, CLAIMS_DIR, RECEIPTS_DIR):
            (self.base / sub).mkdir(parents=True, exist_ok=True, mode=0o700)

    # ── Controller → Worker ──────────────────────────────────────────────

    def submit_proposal(self, job: JobRecord) -> None:
        """Write a job proposal to the proposals directory."""
        path = self.base / PROPOSALS_DIR / f"{job.job_id}.json"
        import json
        path.write_text(
            json.dumps(
                {
                    "job_id": job.job_id,
                    "task_kind": job.task_kind,
                    "privacy_class": job.privacy_class,
                    "execution_mode": job.execution_mode,
                    "input_artifact": job.input_artifact,
                    "proposed_at": datetime.now(timezone.utc).isoformat(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        path.chmod(0o600)

    def list_proposals(self) -> Iterator[tuple[str, JobRecord]]:
        """Yield (job_id, job) for every proposal in the directory."""
        import json
        for p in (self.base / PROPOSALS_DIR).iterdir():
            if p.suffix != ".json":
                continue
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                yield p.stem, JobRecord(
                    job_id=d["job_id"],
                    task_kind=d["task_kind"],
                    privacy_class=d["privacy_class"],
                    execution_mode=d["execution_mode"],
                    input_artifact=d.get("input_artifact"),
                )
            except (json.JSONDecodeError, KeyError, ValueError):
                continue

    def withdraw_proposal(self, job_id: str) -> bool:
        """Remove a proposal. Returns True if it existed."""
        p = self.base / PROPOSALS_DIR / f"{job_id}.json"
        if p.exists():
            p.unlink()
            return True
        return False

    # ── Worker → Controller ──────────────────────────────────────────────

    def write_claim(self, job_id: str, worker_id: str) -> None:
        """Worker claims a job — writes a claim receipt."""
        path = self.base / CLAIMS_DIR / f"{job_id}.json"
        import json
        path.write_text(
            json.dumps(
                {
                    "job_id": job_id,
                    "worker_id": worker_id,
                    "claimed_at": datetime.now(timezone.utc).isoformat(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        path.chmod(0o600)

    def read_claim(self, job_id: str) -> dict | None:
        """Controller reads a claim. Returns None if no claim exists."""
        import json
        p = self.base / CLAIMS_DIR / f"{job_id}.json"
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def write_receipt(
        self,
        job_id: str,
        worker_id: str,
        outcome: str,
        output_artifact: str | None = None,
        error: str | None = None,
    ) -> None:
        """Worker writes a completion/failure receipt."""
        path = self.base / RECEIPTS_DIR / f"{job_id}.json"
        import json
        path.write_text(
            json.dumps(
                {
                    "job_id": job_id,
                    "worker_id": worker_id,
                    "outcome": outcome,   # completed | failed | cancelled
                    "output_artifact": output_artifact,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "error": error,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        path.chmod(0o600)

    def read_receipt(self, job_id: str) -> dict | None:
        """Controller reads a receipt. Returns None if no receipt exists."""
        import json
        p = self.base / RECEIPTS_DIR / f"{job_id}.json"
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def purge(self, job_id: str) -> None:
        """Remove all transport files for a job."""
        for sub in (PROPOSALS_DIR, CLAIMS_DIR, RECEIPTS_DIR):
            p = self.base / sub / f"{job_id}.json"
            if p.exists():
                p.unlink()
