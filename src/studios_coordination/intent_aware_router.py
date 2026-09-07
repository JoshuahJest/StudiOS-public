"""Intent-aware router — bridges IntentSynthesis output to dispatch decisions.

Takes an IntentSynthesis (from intent.py) and converts it to a DispatchDecision
(prioritization_engine.py) that can be executed directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from studios_coordination.intent import IntentSynthesis
from studios_coordination.prioritization_engine import (
    DispatchDecision,
    PrioritizationEngine,
    WorkerCapability,
)
from studios_coordination.scheduler import JobRequest


# ── Task-kind inference from intent ────────────────────────────────────────────

# Map intent_key prefixes to task_kind values understood by the engine.
# Extend this table as new intent_keys are defined.
_INTENT_KEY_TASK_MAP: dict[str, str] = {
    "image": "comfyui-workflow",
    "video": "video-render",
    "render": "video-render",
    "cuda": "cuda-inference",
    "llm": "cuda-inference",
    "inference": "cuda-inference",
    "training": "training",
    "benchmark": "benchmark",
    "document": "document-draft",
    "note": "document-draft",
    "code": "code-generation",
    "research": "research",
}


def _infer_task_kind(intent: IntentSynthesis) -> str:
    """Infer a task_kind from intent_key or objective text."""
    key = (intent.intent_key or "").lower()
    for prefix, kind in _INTENT_KEY_TASK_MAP.items():
        if key.startswith(prefix):
            return kind
    obj = intent.objective.lower()
    for prefix, kind in _INTENT_KEY_TASK_MAP.items():
        if prefix in obj:
            return kind
    return "cuda-inference"


# ── Privacy class inference ─────────────────────────────────────────────────────

_PRIVACY_KEYWORDS = {"private", "confidential", "internal", "restricted", "personal"}
_CLOUD_KEYWORDS = {"public", "synthetic", "test", "sample"}


def _infer_privacy_class(intent: IntentSynthesis) -> str:
    """Infer a privacy class from intent objective text."""
    obj = (intent.objective + " " + intent.intent_key).lower()
    if any(kw in obj for kw in _PRIVACY_KEYWORDS):
        return "restricted"
    if any(kw in obj for kw in _CLOUD_KEYWORDS):
        return "public"
    return "private"


# ── Energy class inference ─────────────────────────────────────────────────────

_ENERGY_CLASS_MAP: dict[str, str] = {
    "immediate": "immediate",
    "efficient": "efficient",
    "background": "background",
    "opportunistic": "opportunistic",
    "approval_required": "background",
    "needs_clarification": "background",
    "ready_for_proposal": "efficient",
}


def _infer_energy_class(intent: IntentSynthesis) -> str:
    """Infer energy class from intent status."""
    return _ENERGY_CLASS_MAP.get(intent.status, "background")


# ── IntentAwareRouter ─────────────────────────────────────────────────────────


@dataclass
class IntentAwareRouter:
    """Bridges IntentSynthesis output to a DispatchDecision.

    Given an IntentSynthesis and a list of workers, returns a ranked dispatch
    plan with confidence, ambiguity detection, and human-readable explanation.
    """

    engine: PrioritizationEngine = field(default_factory=PrioritizationEngine)

    def route(
        self,
        intent: IntentSynthesis,
        workers: list[WorkerCapability],
    ) -> DispatchDecision:
        """Convert an IntentSynthesis into a DispatchDecision."""
        if intent.status in ("needs_clarification", "approval_required"):
            return DispatchDecision(
                job_id=f"intent-{intent.intent_key or 'unknown'}",
                primary_worker_id=None,
                fallback_worker_id=None,
                block_reason=(
                    f"intent-status={intent.status} "
                    f"(clarification: {intent.clarification_reason})"
                ),
                scores={},
                estimated_runtime_seconds=0.0,
                estimated_cost_credits=0.0,
                energy_class="background",
                ambiguity_detected=False,
            )

        task_kind = _infer_task_kind(intent)
        privacy_class = _infer_privacy_class(intent)
        energy_class = _infer_energy_class(intent)

        job = JobRequest(
            job_id=f"intent-{intent.intent_key or 'synthesis'}",
            task_kind=task_kind,
            privacy_class=privacy_class,
            execution_mode="batch",
            required_backends=(),
            min_vram_gb=0.0,
            metadata={
                "intent_objective": intent.objective,
                "intent_key": intent.intent_key,
                "intent_status": intent.status,
                "modalities": list(intent.modalities),
                "machine_ids": list(intent.machine_ids),
            },
        )

        return self.engine.dispatch(job=job, workers=workers)

    def explain(self, decision: DispatchDecision) -> str:
        """Human-readable explanation of the routing decision."""
        return self.engine.explain_decision(decision)


# ── Convenience function ──────────────────────────────────────────────────────


def route_intent_to_dispatch(
    intent: IntentSynthesis,
    workers: list[WorkerCapability],
) -> DispatchDecision:
    """One-shot: infer job from intent and return dispatch decision."""
    router = IntentAwareRouter(engine=PrioritizationEngine())
    return router.route(intent, workers)
