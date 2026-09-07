"""Hard gate policy for StudiOS job routing and execution.

All gates must pass before a job is dispatched. Cost, energy, and speed
only rank candidates that have already cleared every gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .contracts import JobRecord
from .scheduler import WorkerCapability


@dataclass(frozen=True)
class PolicyGates:
    allow_cloud: bool = False          # set True to permit non-local routing
    max_credit_cost_usd: float = 0.0  # 0 = block all cloud routing
    max_energy_wh: float = 0.0        # 0 = no limit
    max_runtime_seconds: float = 0.0  # 0 = no limit
    max_retries: int = 2
    require_local_for_restricted: bool = True  # restricted data stays local


class PolicyError(ValueError):
    """A policy gate blocked the operation."""
    pass


def evaluate_gates(
    job: JobRecord,
    worker: WorkerCapability,
    *,
    gates: PolicyGates,
    estimated_cost_usd: float,
    estimated_energy_wh: float,
    estimated_runtime_seconds: float,
    attempt_count: int,
) -> None:
    """Raise PolicyError with a descriptive gate name if any gate fails.

    All arguments are required. Returns None on success.
    """
    # Gate 1 — restricted data never leaves local workers
    if gates.require_local_for_restricted and job.privacy_class == "restricted":
        if not worker.local:
            raise PolicyError("gate_restricted_requires_local")

    # Gate 2 — restricted data requires loopback-only workers
    if job.privacy_class == "restricted" and not worker.local:
        raise PolicyError("gate_restricted_local_only")

    # Gate 3 — cloud workers require explicit allowlist
    if not worker.local:
        if not gates.allow_cloud:
            raise PolicyError("gate_cloud_not_allowed")
        if gates.max_credit_cost_usd > 0 and estimated_cost_usd > gates.max_credit_cost_usd:
            raise PolicyError(f"gate_cost_limit {estimated_cost_usd:.4f} > {gates.max_credit_cost_usd}")

    # Gate 4 — energy budget
    if gates.max_energy_wh > 0 and estimated_energy_wh > gates.max_energy_wh:
        raise PolicyError(f"gate_energy_limit {estimated_energy_wh:.1f}Wh > {gates.max_energy_wh}Wh")

    # Gate 5 — runtime budget
    if gates.max_runtime_seconds > 0 and estimated_runtime_seconds > gates.max_runtime_seconds:
        raise PolicyError(f"gate_runtime_limit {estimated_runtime_seconds:.0f}s > {gates.max_runtime_seconds}s")

    # Gate 6 — retry exhaustion
    if attempt_count >= gates.max_retries:
        raise PolicyError(f"gate_max_retries {attempt_count} >= {gates.max_retries}")

    # Gate 7 — worker must be online and have capacity
    if not worker.online:
        raise PolicyError("gate_worker_offline")
    if worker.active_jobs >= worker.max_parallel:
        raise PolicyError("gate_worker_at_capacity")

    # Gate 8 — worker must support this task kind
    if job.task_kind not in worker.task_kinds:
        raise PolicyError(f"gate_unsupported_task_kind {job.task_kind}")


def default_gates() -> PolicyGates:
    """Conservative default gates — no cloud, no cost, conservative retries."""
    return PolicyGates(
        allow_cloud=False,
        max_credit_cost_usd=0.0,
        max_energy_wh=0.0,
        max_runtime_seconds=0.0,
        max_retries=2,
        require_local_for_restricted=True,
    )
