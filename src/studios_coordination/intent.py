"""Evidence-preserving request-to-intent synthesis.

This module converts model-produced interpretations into a reviewable intent
proposal. It does not call models, execute tools, or authorize actions.
"""

from dataclasses import dataclass
from typing import Dict, Iterable, Set, Tuple


@dataclass(frozen=True)
class Signal:
    signal_id: str
    source: str
    machine_id: str
    modality: str
    content: str


@dataclass(frozen=True)
class Interpretation:
    interpretation_id: str
    objective: str
    evidence_ids: Tuple[str, ...]
    confidence: float
    risk: str = "low"
    intent_key: str = ""


@dataclass(frozen=True)
class IntentSynthesis:
    objective: str
    status: str
    evidence_ids: Tuple[str, ...]
    modalities: Tuple[str, ...]
    machine_ids: Tuple[str, ...]
    execution_authorized: bool = False
    clarification_reason: str = ""
    intent_key: str = ""


def synthesize_intent(
    signals: Iterable[Signal], interpretations: Iterable[Interpretation]
) -> IntentSynthesis:
    """Combine corroborating interpretations while preserving their evidence."""
    signal_by_id = {signal.signal_id: signal for signal in signals}
    candidates = list(interpretations)
    if not candidates:
        return IntentSynthesis("", "needs_clarification", (), (), (), False, "no-interpretations")

    scores: Dict[str, float] = {}
    evidence_by_objective: Dict[str, Set[str]] = {}
    for candidate in candidates:
        valid_evidence = tuple(
            evidence_id
            for evidence_id in candidate.evidence_ids
            if evidence_id in signal_by_id
        )
        if not valid_evidence:
            continue
        group_key = candidate.intent_key or candidate.objective
        scores[group_key] = scores.get(group_key, 0.0) + candidate.confidence
        evidence_by_objective.setdefault(group_key, set()).update(valid_evidence)

    if not scores:
        return IntentSynthesis("", "needs_clarification", (), (), (), False, "no-valid-evidence")

    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    if len(ranked) > 1 and abs(ranked[0][1] - ranked[1][1]) < 1e-9:
        conflicting_evidence = tuple(
            sorted(evidence_by_objective[ranked[0][0]] | evidence_by_objective[ranked[1][0]])
        )
        selected = [signal_by_id[evidence_id] for evidence_id in conflicting_evidence]
        return IntentSynthesis(
            "",
            "needs_clarification",
            conflicting_evidence,
            tuple(sorted({signal.modality for signal in selected})),
            tuple(sorted({signal.machine_id for signal in selected})),
            False,
            "conflicting-objectives",
        )

    intent_key = ranked[0][0]
    selected_interpretations = [
        candidate
        for candidate in candidates
        if (candidate.intent_key or candidate.objective) == intent_key
    ]
    objective = max(
        selected_interpretations,
        key=lambda candidate: candidate.confidence,
    ).objective
    evidence_ids = tuple(sorted(evidence_by_objective[intent_key]))
    selected_signals = [signal_by_id[evidence_id] for evidence_id in evidence_ids]
    modalities = tuple(sorted({signal.modality for signal in selected_signals}))
    machine_ids = tuple(sorted({signal.machine_id for signal in selected_signals}))
    if any(candidate.risk == "high" for candidate in selected_interpretations):
        status = "approval_required"
        reason = "high-risk-action"
    elif len(evidence_ids) >= 2:
        status = "ready_for_proposal"
        reason = ""
    else:
        status = "needs_clarification"
        reason = "insufficient-corroboration"
    return IntentSynthesis(
        objective,
        status,
        evidence_ids,
        modalities,
        machine_ids,
        False,
        reason,
        intent_key,
    )
