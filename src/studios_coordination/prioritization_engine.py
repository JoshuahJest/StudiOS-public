"""Multi-factor prioritization engine for StudiOS dispatch.

Scores every candidate worker across 6 dimensions for a given job:
  1. hardware_score   — required backends available (CUDA/MPS/etc.)
  2. load_score       — current utilization vs capacity
  3. energy_score     — power/thermal/battery constraints
  4. locality_score   — data already local vs needs transfer
  5. skill_score     — task-specific skills available
  6. cost_score       — estimated credit cost

Integrates:
  - skill_registry.py  (skill → worker mapping)
  - energy_policy.py   (hard gates + energy soft scores)
  - live system state  (enkii_resources.json + StudiOS API)

Usage:
    engine = PrioritizationEngine()
    decision = engine.dispatch(job_request)
"""

from __future__ import annotations

import json
import logging
import math
import urllib.request
import urllib.error
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .scheduler import (
    JobRequest,
    WorkerCapability,
    RouteEstimate,
    SchedulingPolicy,
    RouteDecision,
)
from .skill_registry import SkillRegistry, DEFAULT_REGISTRY
from .energy_policy import EnergyResult, evaluate_worker_energy

log = logging.getLogger("prioritization-engine")

# ── Policy weights (can be overridden per invocation) ────────────────────────────

DEFAULT_WEIGHTS = {
    "hardware_score":  0.35,
    "load_score":      0.20,
    "energy_score":    0.15,
    "locality_score":  0.10,
    "skill_score":     0.10,
    "cost_score":      0.10,
}

# Weight sanity bounds — sum must be ~1.0
assert abs(sum(DEFAULT_WEIGHTS.values()) - 1.0) < 1e-9

# ── Result types ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DispatchScore:
    """Per-worker score breakdown."""
    worker_id: str
    total_score: float           # weighted composite — lower is better
    hardware_score: float        # 0=perfect backend, 1=incompatible
    load_score: float           # 0=idle, 1=saturated
    energy_score: float        # 0=free, 1=constrained
    locality_score: float       # 0=local, >0=transfer penalty
    skill_score: float         # 0=full skill match, 1=no match
    cost_score: float          # normalized credit cost
    energy_result: EnergyResult  # full energy evaluation
    backends: tuple[str, ...]   # available on this worker


@dataclass
class DispatchDecision:
    """Outcome of a dispatch decision for a job."""
    job_id: str
    primary_worker_id: str | None    # best-scored worker
    fallback_worker_id: str | None   # second-best
    block_reason: str               # "" if dispatched, else reason code
    scores: dict[str, DispatchScore]  # all workers scored
    estimated_runtime_seconds: float
    estimated_cost_credits: float
    energy_class: str              # "immediate" | "efficient" | "opportunistic"
    ambiguity_detected: bool        # top 2 scores within epsilon
    ambiguity_epsilon: float = 0.05  # redefined below as instance attr


@dataclass
class LiveWorkerState:
    """Live resource state for a worker at dispatch time."""
    worker_id: str
    online: bool
    active_jobs: int
    vram_free_gb: float | None     # None = no discrete GPU
    ram_free_gb: float | None
    gpu_util_percent: float = 0.0
    gpu_temp_c: float = 0.0
    power_source: str = "ac"
    battery_percent: float = 100.0
    thermal_state: str = "nominal"
    data_local_paths: tuple[str, ...] = field(default_factory=tuple)
    models_loaded: tuple[str, ...] = field(default_factory=tuple)


# ── System state readers ───────────────────────────────────────────────────────

def read_enkii_live_state(resource_path: Path | None = None) -> dict[str, LiveWorkerState]:
    """
    Read live state for ENKI-II and SOLARIS from the cached resource poller file.
    Returns {worker_id: LiveWorkerState}.
    """
    if resource_path is None:
        resource_path = Path.home() / ".studios" / "enkii_resources.json"

    states: dict[str, LiveWorkerState] = {}
    if not resource_path.exists():
        return states

    try:
        data = json.loads(resource_path.read_text())
    except Exception:
        return states

    for node in data.get("nodes", []):
        sample = node.get("sample", {})
        sid = node.get("node_id", "")
        vram_total = sample.get("gpu_memory_total_mb", 0)
        vram_used = sample.get("gpu_memory_used_mb", 0)
        states[sid] = LiveWorkerState(
            worker_id=sid,
            online=sample.get("reachable", False),
            active_jobs=sample.get("running_gpu_jobs", 0),
            vram_free_gb=(vram_total - vram_used) / 1024 if vram_total else None,
            ram_free_gb=sample.get("ram_free_gb", 0.0),
            gpu_util_percent=sample.get("gpu_util_percent", 0.0),
            gpu_temp_c=sample.get("gpu_temperature_c", 0.0),
            power_source="ac",  # ENKI-II is always AC
            battery_percent=100.0,
            thermal_state="nominal",
        )

    return states


def read_local_worker_state() -> LiveWorkerState:
    """
    Read current MacBook state for dispatch decisions.
    Conservative defaults if unavailable.
    """
    from .energy_policy import get_macbook_energy_state
    energy = get_macbook_energy_state()

    # Try to read ComfyUI system stats for GPU utilization
    gpu_util = 0.0
    try:
        import httpx
        with httpx.Client(timeout=3.0) as client:
            r = client.get("http://127.0.0.1:8188/system_stats")
            if r.status_code == 200:
                stats = r.json()
                # ComfyUI doesn't expose GPU util directly in system_stats
                # but if it's reachable, ComfyUI is up
                gpu_util = 0.0
    except Exception:
        pass

    return LiveWorkerState(
        worker_id="joshuahs-macbook-pro",
        online=True,
        active_jobs=0,
        vram_free_gb=None,   # Unified memory — no discrete VRAM boundary
        ram_free_gb=None,    # Would need to read from system
        gpu_util_percent=gpu_util,
        power_source=energy.get("power_source", "unknown"),
        battery_percent=energy.get("battery_percent", 100.0),
        thermal_state=energy.get("thermal_state", "nominal"),
    )


# ── Core engine ────────────────────────────────────────────────────────────────

class PrioritizationEngine:
    """
    Multi-factor job scorer for StudiOS dispatch.

    Phase 1: Static capability list + live ENKII poller state.
    Phase 2: Dynamic discovery via StudiOS API /api/resources.
    """

    def __init__(
        self,
        weights: dict[str, float] | None = None,
        skill_registry: SkillRegistry | None = None,
        ambiguity_epsilon: float = 0.05,
    ):
        self.weights = weights if weights is not None else dict(DEFAULT_WEIGHTS)
        self.skill_registry = skill_registry or DEFAULT_REGISTRY
        self.ambiguity_epsilon = ambiguity_epsilon
        self._live_states: dict[str, LiveWorkerState] = {}

    # ── Live state ────────────────────────────────────────────────────────────

    def refresh_live_state(self, resource_path: Path | None = None) -> None:
        """Refresh live state from all sources. Call before dispatch in a live loop."""
        # ENKII/SOLARIS from poller cache
        self._live_states = read_enkii_live_state(resource_path)
        # MacBook live state
        self._live_states["joshuahs-macbook-pro"] = read_local_worker_state()

    def get_live_state(self, worker_id: str) -> LiveWorkerState | None:
        return self._live_states.get(worker_id)

    # ── Per-worker scoring ────────────────────────────────────────────────────

    def _hardware_score(
        self,
        job: JobRequest,
        worker: WorkerCapability,
    ) -> float:
        """0.0 = perfect backend match, 1.0 = no required backends available."""
        if not job.required_backends:
            return 0.0

        worker_backends = set(getattr(worker, "backends", ()))
        if not worker_backends:
            return 1.0

        present = sum(1 for be in job.required_backends if be in worker_backends)
        return 1.0 - (present / len(job.required_backends))

    def _load_score(
        self,
        job: JobRequest,
        worker: WorkerCapability,
        live_state: LiveWorkerState,
    ) -> float:
        """0.0 = idle, 1.0 = saturated."""
        if worker.max_parallel <= 0:
            return 1.0
        active = max(live_state.active_jobs, worker.active_jobs)
        return min(active / worker.max_parallel, 1.0)

    def _locality_score(
        self,
        job: JobRequest,
        worker: WorkerCapability,
        live_state: LiveWorkerState,
    ) -> float:
        """
        0.0 = data already local
        0.1 = NAS (already shared via MacBook proxy)
        0.2 = remote transfer required
        0.3 = cross-machine pull
        """
        # If worker is the same host as the request — local
        # If data is on NAS (any worker can reach via MacBook proxy) — near-local
        # If data is on another machine — transfer penalty
        data_paths = live_state.data_local_paths
        if not data_paths:
            return 0.1  # Assume NAS/shared storage = minimal penalty
        return 0.0

    def _cost_score(
        self,
        job: JobRequest,
        worker: WorkerCapability,
        estimate: RouteEstimate | None,
    ) -> float:
        """Normalized estimated credit cost — 0 = cheapest, 1 = most expensive."""
        if estimate is None or estimate.credit_cost <= 0:
            return 0.5  # Unknown = middle ground
        # Normalize: assume 0–1000 mCr is the realistic range
        return min(estimate.credit_cost / 1000.0, 1.0)

    def _compute_scores(
        self,
        job: JobRequest,
        worker: WorkerCapability,
        live_state: LiveWorkerState,
        estimate: RouteEstimate | None,
    ) -> DispatchScore:
        """Compute all 6 factor scores and the weighted total."""

        hw = self._hardware_score(job, worker)
        load = self._load_score(job, worker, live_state)
        locality = self._locality_score(job, worker, live_state)
        cost = self._cost_score(job, worker, estimate)
        skill = self.skill_registry.skill_score(job.task_kind, worker.worker_id)

        # Energy evaluation (includes hard gates)
        energy_result = evaluate_worker_energy(
            worker_id=worker.worker_id,
            job_min_vram_gb=job.min_vram_gb,
            job_task_kind=job.task_kind,
            latency_class=getattr(job, "latency_class", "background"),
            resource_snapshot=None,  # could pass full resource snapshot
        )

        # If hard block, set energy_score to 1.0 and mark total as worst
        if energy_result.block_level == "hard":
            energy = 1.0
        else:
            energy = energy_result.score

        total = (
            self.weights["hardware_score"]  * hw +
            self.weights["load_score"]       * load +
            self.weights["energy_score"]     * energy +
            self.weights["locality_score"]   * locality +
            self.weights["skill_score"]      * skill +
            self.weights["cost_score"]       * cost +
            0.0  # placeholder for additional factors
        )

        return DispatchScore(
            worker_id=worker.worker_id,
            total_score=total,
            hardware_score=hw,
            load_score=load,
            energy_score=energy,
            locality_score=locality,
            skill_score=skill,
            cost_score=cost,
            energy_result=energy_result,
            backends=worker.backends,
        )

    # ── Ranking ──────────────────────────────────────────────────────────────

    def score_worker(
        self,
        job: JobRequest,
        worker: WorkerCapability,
        live_state: LiveWorkerState | None = None,
        estimate: RouteEstimate | None = None,
    ) -> DispatchScore:
        """Score one worker for one job."""
        if live_state is None:
            live_state = self._live_states.get(
                worker.worker_id,
                LiveWorkerState(worker_id=worker.worker_id, online=False, active_jobs=0,
                                vram_free_gb=None, ram_free_gb=None),
            )
        return self._compute_scores(job, worker, live_state, estimate)

    def rank_workers(
        self,
        job: JobRequest,
        workers: Iterable[WorkerCapability],
        estimates: Iterable[RouteEstimate] = (),
    ) -> list[DispatchScore]:
        """
        Score and rank all workers for a job.
        Returns workers sorted by total_score ascending (lower = better).
        Hard-blocked workers are excluded.
        """
        est_map = {e.worker_id: e for e in estimates}
        ranked: list[DispatchScore] = []

        for worker in workers:
            live_state = self._live_states.get(
                worker.worker_id,
                LiveWorkerState(worker_id=worker.worker_id, online=False, active_jobs=0,
                                vram_free_gb=None, ram_free_gb=None),
            )

            # Skip workers that are marked offline in their capability
            if not worker.online:
                continue

            score = self._compute_scores(job, worker, live_state, est_map.get(worker.worker_id))

            # Skip hard-blocked workers
            if score.energy_result.block_level == "hard":
                log.debug("Worker %s hard-blocked: %s", worker.worker_id, score.energy_result.reason)
                continue

            ranked.append(score)

        ranked.sort(key=lambda s: (s.total_score, s.worker_id))
        return ranked

    # ── Dispatch ─────────────────────────────────────────────────────────────

    def dispatch(
        self,
        job: JobRequest,
        workers: Iterable[WorkerCapability] | None = None,
        estimates: Iterable[RouteEstimate] = (),
    ) -> DispatchDecision:
        """
        Make a dispatch decision for a job.

        Steps:
        1. Rank all workers
        2. Check for ambiguity (top 2 within epsilon)
        3. Return primary + fallback + block_reason

        Args:
            job: The job to dispatch
            workers: Candidate workers (defaults to known fleet)
            estimates: Optional runtime/cost estimates per worker

        Returns:
            DispatchDecision with primary_worker_id, fallback, and scores
        """
        # Default worker list: MacBook + ENKII if online
        if workers is None:
            workers = self._default_workers()

        ranked = self.rank_workers(job, workers, estimates)

        if not ranked:
            return DispatchDecision(
                job_id=job.job_id,
                primary_worker_id=None,
                fallback_worker_id=None,
                block_reason="no-qualified-worker",
                scores={},
                estimated_runtime_seconds=0.0,
                estimated_cost_credits=0.0,
                energy_class="opportunistic",
                ambiguity_detected=False,
            )

        primary = ranked[0]
        fallback = ranked[1] if len(ranked) > 1 else None

        # Ambiguity check: top 2 within epsilon
        ambiguity = (
            fallback is not None
            and abs(primary.total_score - fallback.total_score) < self.ambiguity_epsilon
        )

        # Runtime/cost from primary estimate
        est_map = {e.worker_id: e for e in estimates}
        primary_est = est_map.get(primary.worker_id)

        return DispatchDecision(
            job_id=job.job_id,
            primary_worker_id=primary.worker_id,
            fallback_worker_id=fallback.worker_id if fallback else None,
            block_reason="",  # Dispatched successfully
            scores={s.worker_id: s for s in ranked},
            estimated_runtime_seconds=primary_est.runtime_seconds if primary_est else 0.0,
            estimated_cost_credits=primary_est.credit_cost if primary_est else 0.0,
            energy_class=primary.energy_result.energy_class,
            ambiguity_detected=ambiguity,
        )

    def _default_workers(self) -> list[WorkerCapability]:
        """Return the default candidate worker list."""
        from .scheduler import refresh_remote_workers

        # Get live remote workers from ENKII poll
        try:
            remote = refresh_remote_workers()
        except Exception:
            remote = []

        workers: list[WorkerCapability] = remote.copy()

        # Always include MacBook
        from .scheduler import WorkerCapability
        workers.append(WorkerCapability(
            worker_id="joshuahs-macbook-pro",
            task_kinds=("comfyui-workflow", "document-draft", "code-review",
                       "local-ollama", "nas-file-op"),
            backends=("mps", "cpu", "comfyui"),
            vram_gb=0.0,   # Unified memory — no boundary
            system_memory_gb=128.0,
            max_parallel=3,
            active_jobs=0,
            local=True,
            online=True,
        ))

        return workers

    # ── Audit helpers ────────────────────────────────────────────────────────

    def explain_decision(self, decision: DispatchDecision) -> str:
        """Human-readable explanation of a dispatch decision."""
        lines = [f"Dispatch for job {decision.job_id}:"]
        if decision.block_reason:
            lines.append(f"  BLOCKED: {decision.block_reason}")
            return "\n".join(lines)

        primary_id = decision.primary_worker_id if decision.primary_worker_id else ""
        primary_score = decision.scores[primary_id].total_score
        lines.append(f"  Winner: {primary_id} (score={primary_score:.3f})")
        if decision.ambiguity_detected:
            lines.append(f"  AMBIGUOUS: top 2 within ε={self.ambiguity_epsilon}")
        if decision.fallback_worker_id:
            fb = decision.scores[decision.fallback_worker_id]
            lines.append(f"  Fallback: {decision.fallback_worker_id} (score={fb.total_score:.3f})")

        lines.append("  Scores:")
        for wid, sc in sorted(decision.scores.items(), key=lambda x: x[1].total_score):
            lines.append(f"    {wid}: total={sc.total_score:.3f}  "
                        f"hw={sc.hardware_score:.2f}  "
                        f"load={sc.load_score:.2f}  "
                        f"energy={sc.energy_score:.2f}  "
                        f"locality={sc.locality_score:.2f}  "
                        f"skill={sc.skill_score:.2f}  "
                        f"cost={sc.cost_score:.2f}")
        return "\n".join(lines)


# ── Convenience function ───────────────────────────────────────────────────────

def quick_dispatch(
    job_id: str,
    task_kind: str,
    privacy_class: str = "shared-nonsecret",
    execution_mode: str = "background",
    min_vram_gb: float = 0.0,
    required_backends: tuple[str, ...] = (),
) -> DispatchDecision:
    """
    One-shot dispatch decision without needing to instantiate the engine.
    Suitable for cron jobs and simple scripts.

    Example:
        decision = quick_dispatch(
            job_id="my-job-001",
            task_kind="comfyui-workflow",
            min_vram_gb=4.0,
            required_backends=("mps",),
        )
    """
    engine = PrioritizationEngine()
    engine.refresh_live_state()

    job = JobRequest(
        job_id=job_id,
        task_kind=task_kind,
        privacy_class=privacy_class,
        execution_mode=execution_mode,
        required_backends=required_backends,
        min_vram_gb=min_vram_gb,
    )
    return engine.dispatch(job)
