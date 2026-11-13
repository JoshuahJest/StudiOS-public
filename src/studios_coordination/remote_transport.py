"""Remote REST-based transport for StudiOS cross-machine coordination.

The RemoteTransport submits proposals to and reads receipts from a remote
StudiOS API over the tailnet.  It adapts the LoopbackTransport document
shapes to the StudiOS REST API task envelope.

All network calls raise TransportError on failure.
"""

from __future__ import annotations

import json
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .contracts import JobRecord, JobState


class TransportError(RuntimeError):
    """Raised when a remote transport call fails."""


class RemoteTransport:
    """Filesystem-shaped transport that delegates to a remote StudiOS REST API.

    Directory layout (mirrors LoopbackTransport but network-backed):

        proposals/  <job_id>.json   — MacBook controller writes, ENKI-II reads
        claims/     <job_id>.json   — ENKI-II worker writes, MacBook reads
        receipts/   <job_id>.json   — ENKI-II worker writes, MacBook reads

    The remote StudiOS API at base_url owns the authoritative task store.
    This transport only moves structured coordination documents (proposals,
    claims, receipts) — not the full task objects.
    """

    PROPOSALS_DIR = "proposals"
    CLAIMS_DIR    = "claims"
    RECEIPTS_DIR  = "receipts"

    def __init__(
        self,
        base_url: str,          # e.g. "http://<enkii-tailnet-ip>:8765"
        worker_id: str = "enki-ii",
        timeout: float = 15.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.worker_id = worker_id
        self.timeout = timeout
        self._proposals_cache: dict[str, dict] = {}  # job_id → proposal dict

    # ── internal HTTP helpers ─────────────────────────────────────────────────

    def _get(self, path: str) -> dict:
        url = f"{self.base_url}{path}"
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else ""
            raise TransportError(f"GET {url} → HTTP {e.code}: {body}") from e
        except urllib.error.URLError as e:
            raise TransportError(f"GET {url} → {e.reason}") from e

    def _post(self, path: str, body: dict) -> dict:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode()
        try:
            req = urllib.request.Request(
                url, data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else ""
            raise TransportError(f"POST {url} → HTTP {e.code}: {body}") from e
        except urllib.error.URLError as e:
            raise TransportError(f"POST {url} → {e.reason}") from e

    def _delete(self, path: str) -> bool:
        url = f"{self.base_url}{path}"
        try:
            req = urllib.request.Request(url, method="DELETE")
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status == 200
        except urllib.error.HTTPError as e:
            return e.code == 404
        except urllib.error.URLError as e:
            raise TransportError(f"DELETE {url} → {e.reason}") from e

    # ── Controller → Worker ─────────────────────────────────────────────────

    def submit_proposal(self, job: JobRecord) -> None:
        """Submit a job proposal to ENKI-II's StudiOS vault via POST /api/tasks."""
        # Translate JobRecord to StudiOS task shape
        task_body = {
            "task": {
                "id": job.job_id,
                "title": f"[PROPOSAL] {job.job_id}",
                "kind": "task",
                "status": "waiting",
                "category": "Business",
                "domain": "JestWorks",
                "goal": f"StudiOS proposal from MacBook controller. "
                        f"task_kind={job.task_kind}, "
                        f"privacy={job.privacy_class}, "
                        f"mode={job.execution_mode}. "
                        f"Input: {job.input_artifact}",
                "priority": 3,
                "delegable": True,
                "resources": ["tools"],
                "arm": "Creative + AV",
                "created": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            }
        }
        try:
            result = self._post("/api/tasks", task_body)
            # Cache the proposal for list_proposals
            self._proposals_cache[job.job_id] = {
                "job_id": job.job_id,
                "task_kind": job.task_kind,
                "privacy_class": job.privacy_class,
                "execution_mode": job.execution_mode,
                "input_artifact": job.input_artifact,
                "proposed_at": datetime.now(timezone.utc).isoformat(),
                "remote_task": result.get("task", {}),
            }
        except TransportError:
            # If the task already exists (idempotent retry), that's fine
            raise

    def list_proposals(self) -> Iterator[tuple[str, JobRecord]]:
        """Yield active proposals from ENKI-II's StudiOS vault.

        Uses GET /api/tasks filtered to waiting tasks that look like proposals.
        Falls back to cached proposals if the API call fails.
        """
        try:
            tasks_payload = self._get("/api/tasks")
            # API returns {"tasks": [...]} or a list
            tasks = tasks_payload.get("tasks", tasks_payload) if isinstance(tasks_payload, dict) else tasks_payload
            for task in tasks:
                if not isinstance(task, dict):
                    continue
                task_id = task.get("id", "")
                # Only yield tasks that are proposal-shaped (title starts with [PROPOSAL])
                if task.get("title", "").startswith("[PROPOSAL]"):
                    job = JobRecord(
                        job_id=task_id,
                        task_kind=task.get("task_kind", "unknown"),
                        privacy_class=task.get("privacy_class", "private"),
                        execution_mode=task.get("execution_mode", "batch"),
                        input_artifact=task.get("input_artifact"),
                    )
                    yield task_id, job
        except TransportError:
            # Fall back to cache
            for job_id, proposal in self._proposals_cache.items():
                yield job_id, JobRecord(
                    job_id=proposal["job_id"],
                    task_kind=proposal["task_kind"],
                    privacy_class=proposal["privacy_class"],
                    execution_mode=proposal["execution_mode"],
                    input_artifact=proposal.get("input_artifact"),
                )

    def withdraw_proposal(self, job_id: str) -> bool:
        """Delete the proposal task from ENKI-II's vault."""
        result = self._delete(f"/api/tasks/{job_id}")
        self._proposals_cache.pop(job_id, None)
        return result

    # ── Worker → Controller ─────────────────────────────────────────────────

    def write_claim(self, job_id: str, worker_id: str) -> None:
        """Worker claims a job — recorded in ENKI-II's StudiOS task via PATCH."""
        claim_body = {
            "task": {
                "status": "active",
                "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "title": f"[CLAIMED by {worker_id}] {job_id}",
            }
        }
        try:
            self._post(f"/api/tasks/{job_id}", claim_body)
        except TransportError:
            raise

    def read_claim(self, job_id: str) -> dict | None:
        """Read a claim from ENKI-II's task."""
        try:
            result = self._get(f"/api/tasks/{job_id}")
            task = result.get("task", result)
            if "[CLAIMED" in task.get("title", ""):
                return {"job_id": job_id, "worker_id": self.worker_id, "claimed_at": task.get("updated")}
            return None
        except TransportError:
            return None

    def write_receipt(
        self,
        job_id: str,
        worker_id: str,
        outcome: str,
        output_artifact: str | None = None,
        error: str | None = None,
    ) -> None:
        """Worker writes a completion/failure receipt to ENKI-II's StudiOS task."""
        receipt_body = {
            "task": {
                "status": "done" if outcome == "completed" else "waiting",
                "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "title": f"[RECEIPT:{outcome}] {job_id}",
                "goal": f"outcome={outcome}, output={output_artifact}, error={error}",
            }
        }
        try:
            self._post(f"/api/tasks/{job_id}", receipt_body)
        except TransportError:
            raise

    def read_receipt(self, job_id: str) -> dict | None:
        """Read a completion receipt from ENKI-II's task."""
        try:
            result = self._get(f"/api/tasks/{job_id}")
            task = result.get("task", result)
            if task.get("title", "").startswith("[RECEIPT:"):
                return {
                    "job_id": job_id,
                    "worker_id": self.worker_id,
                    "outcome": task["title"].split(":")[1].split("]")[0],
                    "output_artifact": task.get("goal", ""),
                }
            return None
        except TransportError:
            return None

    def purge(self, job_id: str) -> None:
        """Remove all coordination state for a job (proposal + claim + receipt)."""
        self._delete(f"/api/tasks/{job_id}")
        self._proposals_cache.pop(job_id, None)

    # ── Remote health check ─────────────────────────────────────────────────

    def ping(self) -> bool:
        """True if the remote StudiOS API is reachable."""
        try:
            self._get("/api/health")
            return True
        except TransportError:
            return False


# ── convenience factory ──────────────────────────────────────────────────────

def remote_transport(
    url: str = "http://<enkii-tailnet-ip>:8765",
    worker_id: str = "enki-ii",
) -> RemoteTransport:
    """Create a RemoteTransport pointing at ENKI-II's StudiOS API."""
    return RemoteTransport(base_url=url, worker_id=worker_id)
