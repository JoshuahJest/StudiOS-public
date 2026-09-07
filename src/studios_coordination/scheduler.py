"""Deterministic, local-first routing for reviewable StudiOS jobs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import json
import os
from pathlib import Path
from typing import Iterable, Optional, Tuple


@dataclass(frozen=True)
class WorkerCapability:
    worker_id: str
    task_kinds: Tuple[str, ...]
    backends: Tuple[str, ...]
    vram_gb: float
    system_memory_gb: float
    max_parallel: int
    active_jobs: int
    local: bool
    online: bool


@dataclass(frozen=True)
class JobRequest:
    job_id: str
    task_kind: str
    privacy_class: str
    execution_mode: str
    required_backends: Tuple[str, ...] = ()
    min_vram_gb: float = 0


@dataclass(frozen=True)
class RouteEstimate:
    worker_id: str
    task_kind: str
    runtime_seconds: float
    energy_wh: float
    credit_cost: float


@dataclass(frozen=True)
class SchedulingPolicy:
    allow_cloud: bool = False
    max_credit_cost: float = 0
    max_runtime_seconds: float = 0
    max_energy_wh: float = 0


@dataclass(frozen=True)
class RouteDecision:
    worker_id: Optional[str]
    reason: str
    runtime_seconds: float = 0
    energy_wh: float = 0
    credit_cost: float = 0


@dataclass(frozen=True)
class LeaseResult:
    acquired: bool
    reason: str


def route_job(
    job: JobRequest,
    workers: Iterable[WorkerCapability],
    estimates: Iterable[RouteEstimate],
    policy: SchedulingPolicy,
) -> RouteDecision:
    """Choose the lowest-energy qualified worker without executing the job."""
    worker_by_id = {worker.worker_id: worker for worker in workers}
    qualified = []
    for estimate in estimates:
        worker = worker_by_id.get(estimate.worker_id)
        if worker is None or estimate.task_kind != job.task_kind:
            continue
        if not worker.online or worker.active_jobs >= worker.max_parallel:
            continue
        if job.task_kind not in worker.task_kinds:
            continue
        if worker.vram_gb < job.min_vram_gb:
            continue
        if not set(job.required_backends).issubset(worker.backends):
            continue
        if policy.max_runtime_seconds and estimate.runtime_seconds > policy.max_runtime_seconds:
            continue
        if policy.max_energy_wh and estimate.energy_wh > policy.max_energy_wh:
            continue
        if not worker.local:
            if job.privacy_class == "restricted":
                continue
            if not policy.allow_cloud or estimate.credit_cost > policy.max_credit_cost:
                continue
        qualified.append(estimate)
    if not qualified:
        return RouteDecision(None, "no-qualified-worker")
    if job.execution_mode == "interactive":
        selected = min(qualified, key=lambda item: (item.runtime_seconds, item.energy_wh, item.worker_id))
    else:
        selected = min(qualified, key=lambda item: (item.energy_wh, item.runtime_seconds, item.worker_id))
    return RouteDecision(
        selected.worker_id,
        "lowest-policy-cost",
        selected.runtime_seconds,
        selected.energy_wh,
        selected.credit_cost,
    )


def claim_job(
    lease_path: Path,
    *,
    job_id: str,
    worker_id: str,
    now,
    ttl_seconds: int,
) -> LeaseResult:
    """Acquire a small filesystem lease without executing the claimed job."""
    lease_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = lease_path.with_suffix(lease_path.suffix + ".lock")
    try:
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return LeaseResult(False, "lease-update-contention")
    try:
        os.close(lock_fd)
        current = None
        try:
            current = json.loads(lease_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass
        except (json.JSONDecodeError, OSError):
            return LeaseResult(False, "malformed-existing-lease")

        if current:
            expires_at = now.fromisoformat(current["expires_at"])
            if expires_at > now:
                if current.get("job_id") == job_id and current.get("worker_id") == worker_id:
                    return LeaseResult(True, "already-owned")
                return LeaseResult(False, "leased-by-other-worker")

        document = {
            "schema": "studios.worker-lease/v1",
            "job_id": job_id,
            "worker_id": worker_id,
            "claimed_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(),
        }
        temporary = lease_path.with_suffix(lease_path.suffix + f".{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, lease_path)
        return LeaseResult(True, "acquired")
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


# ── Remote worker registry ───────────────────────────────────────────────────

import urllib.request
import urllib.error

ENKI_II_WORKER: WorkerCapability = WorkerCapability(
    worker_id="enki-ii",
    task_kinds=("comfyui-workflow", "cuda-inference", "video-render", "training", "benchmark"),
    backends=("cuda", "nvenc", "torch"),
    vram_gb=24.0,          # RTX 4090 24GB
    system_memory_gb=63.7,
    max_parallel=2,         # one GPU job + one CPU job
    active_jobs=0,
    local=False,            # remote worker
    online=False,           # set true by refresh_remote_workers()
)


def refresh_remote_workers(
    enki_url: str = "http://100.79.103.70:8765",
) -> list[WorkerCapability]:
    """Poll ENKI-II's /api/resources and return updated WorkerCapability list."""
    import json as _json

    workers = [ENKI_II_WORKER]
    try:
        req = urllib.request.Request(f"{enki_url}/api/resources")
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            data = _json.loads(resp.read().decode())
        nodes = data.get("nodes", [])
        if nodes:
            sample = nodes[0].get("sample", {})
            # Update ENKI-II active job count from live resource ad
            enki = workers[0]
            active_gpu = sample.get("running_gpu_jobs", 0)
            workers[0] = WorkerCapability(
                worker_id=enki.worker_id,
                task_kinds=enki.task_kinds,
                backends=enki.backends,
                vram_gb=enki.vram_gb,
                system_memory_gb=sample.get("ram_free_gb", 0) + sample.get("ram_used_gb", 0),
                max_parallel=enki.max_parallel,
                active_jobs=active_gpu,
                local=enki.local,
                online=True,
            )
    except Exception:
        # Network failure — mark ENKI-II offline
        workers[0] = WorkerCapability(
            worker_id=ENKI_II_WORKER.worker_id,
            task_kinds=ENKI_II_WORKER.task_kinds,
            backends=ENKI_II_WORKER.backends,
            vram_gb=ENKI_II_WORKER.vram_gb,
            system_memory_gb=ENKI_II_WORKER.system_memory_gb,
            max_parallel=ENKI_II_WORKER.max_parallel,
            active_jobs=0,
            local=False,
            online=False,
        )
    return workers


def dispatch_task_to_enkii(
    job_id: str,
    task_kind: str,
    privacy_class: str = "private",
    execution_mode: str = "batch",
    input_artifact: str | None = None,
    enki_url: str = "http://100.79.103.70:8765",
) -> dict:
    """Submit a job directly to ENKI-II's StudiOS API. Returns API response."""
    import json as _json

    task_body = {
        "task": {
            "id": job_id,
            "title": f"[PROPOSAL] {job_id}",
            "kind": "task",
            "status": "waiting",
            "category": "Business",
            "domain": "JestWorks",
            "goal": f"task_kind={task_kind} privacy={privacy_class} mode={execution_mode} input={input_artifact}",
            "priority": 3,
            "delegable": True,
            "resources": ["tools"],
            "arm": "Creative + AV",
            "created": "2026-09-06",
            "updated": "2026-09-06",
        }
    }
    url = f"{enki_url}/api/tasks"
    data = _json.dumps(task_body).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15.0) as resp:
        return _json.loads(resp.read().decode())
