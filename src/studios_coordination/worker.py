"""Worker loop for StudiOS jobs.

The worker polls the transport for proposals, acquires leases, executes workloads
via registered adapters, writes receipts, and updates the store. It is intentionally
single-threaded and loop-based — no async, no threading, no subprocess pools.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .adapters import get_adapter, AdapterContext, AdapterError
from .contracts import AttemptRecord, AttemptState, JobRecord, JobState, LeaseRecord
from .policy import PolicyGates, evaluate_gates, PolicyError
from .receipts import build_receipt, write_receipt
from .scheduler import WorkerCapability
from .store import Store
from .transport import LoopbackTransport

log = logging.getLogger(__name__)


class WorkerLoop:
    """Single-threaded StudiOS worker loop."""

    def __init__(
        self,
        worker_id: str,
        store: Store,
        transport: LoopbackTransport,
        host_id: str,
        capabilities: WorkerCapability,
        policy_gates: PolicyGates,
        output_dir: Path | str = "studios_output",
        poll_interval: float = 5.0,
    ):
        self.worker_id = worker_id
        self.store = store
        self.transport = transport
        self.host_id = host_id
        self.capabilities = capabilities
        self.policy_gates = policy_gates
        self.output_dir = Path(output_dir)
        self.poll_interval = poll_interval
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._running = False

    def start(self) -> None:
        """Run the worker loop. Blocks until stop() is called."""
        self._running = True
        log.info("Worker %s starting on host %s", self.worker_id, self.host_id)
        while self._running:
            self._tick()
            time.sleep(self.poll_interval)

    def stop(self) -> None:
        self._running = False

    def _tick(self) -> None:
        """One iteration: scan proposals, evaluate gates, execute, receipt."""
        for job_id, proposal in self.transport.list_proposals():
            self._process_proposal(job_id, proposal)

    def _process_proposal(self, job_id: str, proposal: JobRecord) -> None:
        # Skip jobs we already own
        lease = self.store.get_lease(job_id)
        if lease is not None and lease.worker_id == self.worker_id:
            return

        try:
            evaluate_gates(
                job=proposal,
                worker=self.capabilities,
                gates=self.policy_gates,
                estimated_cost_usd=0.0,   # TODO: wire real estimates
                estimated_energy_wh=0.0,
                estimated_runtime_seconds=0.0,
                attempt_count=0,
            )
        except PolicyError as e:
            log.debug("Job %s blocked by policy: %s", job_id, e)
            return

        # Attempt to acquire the lease
        fencing_token = str(uuid.uuid4())
        expires_at = datetime.now(timezone.utc).isoformat()
        acquired = self.store.acquire_lease(
            LeaseRecord(
                job_id=job_id,
                worker_id=self.worker_id,
                expires_at=expires_at,
                fencing_token=fencing_token,
            )
        )
        if not acquired:
            log.debug("Job %s: lease contention", job_id)
            return

        self.transport.write_claim(job_id, self.worker_id)
        self.store.update_job_state(job_id, JobState.RUNNING, self.worker_id)
        self.store.append_audit("job_started", job_id, self.worker_id)

        attempt_id = str(uuid.uuid4())
        self.store.insert_attempt(
            AttemptRecord(
                attempt_id=attempt_id,
                job_id=job_id,
                worker_id=self.worker_id,
                state=AttemptState.ACQUIRED,
            )
        )

        self._execute_and_receipt(job_id, proposal, attempt_id)

    def _execute_and_receipt(
        self, job_id: str, job: JobRecord, attempt_id: str
    ) -> None:
        started_at = datetime.now(timezone.utc)
        try:
            adapter = get_adapter(job.task_kind)
            output_path = adapter.run(
                ctx=AdapterContext(
                    job_id=job_id,
                    input_artifact=Path(job.input_artifact) if job.input_artifact else None,
                    output_dir=self.output_dir,
                    host_id=self.host_id,
                    workdir=self.output_dir,
                ),
                params={},
            )
            outcome = "completed"
            error = None
        except AdapterError as e:
            outcome = "failed"
            error = str(e)
            output_path = None
            log.exception("Job %s adapter error", job_id)
        except Exception as e:
            outcome = "failed"
            error = str(e)
            output_path = None
            log.exception("Job %s unexpected error", job_id)

        completed_at = datetime.now(timezone.utc)
        receipt = build_receipt(
            job_id=job_id,
            worker_id=self.worker_id,
            outcome=outcome,
            output_path=output_path,
            started_at=started_at,
            completed_at=completed_at,
        )
        receipt_path = self.output_dir / "receipts" / f"{job_id}.json"
        write_receipt(receipt, receipt_path)

        # Write transport receipt for controller pickup
        self.transport.write_receipt(
            job_id=job_id,
            worker_id=self.worker_id,
            outcome=outcome,
            output_artifact=str(output_path) if output_path else None,
            error=error,
        )

        # Update state
        final_state = JobState.COMPLETED if outcome == "completed" else JobState.FAILED
        self.store.update_job_state(job_id, final_state, self.worker_id)
        self.store.update_attempt_state(
            attempt_id,
            AttemptState.COMPLETED if outcome == "completed" else AttemptState.ABORTED,
        )
        self.store.append_audit("job_completed", job_id, self.worker_id, outcome=outcome)
        self.store.release_lease(job_id, self.worker_id)

        # Withdraw proposal so we don't re-process
        self.transport.withdraw_proposal(job_id)
