import unittest

from studios_coordination.intent import Interpretation, Signal, synthesize_intent


class IntentSynthesisTests(unittest.TestCase):
    def test_combines_corroborating_modalities_across_machines(self):
        signals = [
            Signal(
                signal_id="voice-1",
                source="thomas",
                machine_id="macbook",
                modality="speech",
                content="Let's make the task list easier to use.",
            ),
            Signal(
                signal_id="screen-1",
                source="screen-capture",
                machine_id="enki-ii",
                modality="image",
                content="Priority controls require several clicks.",
            ),
        ]
        interpretations = [
            Interpretation(
                interpretation_id="local-a",
                objective="reduce friction in task prioritization",
                evidence_ids=("voice-1",),
                confidence=0.80,
            ),
            Interpretation(
                interpretation_id="local-b",
                objective="reduce friction in task prioritization",
                evidence_ids=("screen-1",),
                confidence=0.75,
            ),
        ]

        result = synthesize_intent(signals, interpretations)

        self.assertEqual(result.objective, "reduce friction in task prioritization")
        self.assertEqual(result.status, "ready_for_proposal")
        self.assertEqual(result.evidence_ids, ("screen-1", "voice-1"))
        self.assertEqual(result.modalities, ("image", "speech"))
        self.assertEqual(result.machine_ids, ("enki-ii", "macbook"))
        self.assertFalse(result.execution_authorized)

    def test_requests_clarification_when_top_objectives_conflict(self):
        signals = [
            Signal("voice-1", "thomas", "macbook", "speech", "Make the task list easier."),
            Signal("note-1", "meeting-note", "enki-ii", "text", "Automate task ranking."),
        ]
        interpretations = [
            Interpretation("model-a", "redesign the task list", ("voice-1",), 0.80),
            Interpretation("model-b", "automate task ranking", ("note-1",), 0.80),
        ]

        result = synthesize_intent(signals, interpretations)

        self.assertEqual(result.status, "needs_clarification")
        self.assertEqual(result.clarification_reason, "conflicting-objectives")
        self.assertFalse(result.execution_authorized)

    def test_high_risk_intent_requires_approval_even_with_corroboration(self):
        signals = [
            Signal("voice-1", "thomas", "macbook", "speech", "Send the announcement."),
            Signal("draft-1", "staged-draft", "enki-ii", "text", "Announcement is ready."),
        ]
        interpretations = [
            Interpretation("model-a", "send the external announcement", ("voice-1",), 0.90, risk="high"),
            Interpretation("model-b", "send the external announcement", ("draft-1",), 0.90, risk="high"),
        ]

        result = synthesize_intent(signals, interpretations)

        self.assertEqual(result.status, "approval_required")
        self.assertEqual(result.clarification_reason, "high-risk-action")
        self.assertFalse(result.execution_authorized)

    def test_merges_different_phrasings_using_a_shared_intent_key(self):
        signals = [
            Signal("voice-1", "thomas", "macbook", "speech", "It takes too many clicks."),
            Signal("note-1", "observer", "enki-ii", "text", "Task triage is slow."),
        ]
        interpretations = [
            Interpretation(
                "model-a",
                "make prioritizing tasks feel effortless",
                ("voice-1",),
                0.90,
                intent_key="task-priority-friction",
            ),
            Interpretation(
                "model-b",
                "reduce friction in task prioritization",
                ("note-1",),
                0.80,
                intent_key="task-priority-friction",
            ),
        ]

        result = synthesize_intent(signals, interpretations)

        self.assertEqual(result.status, "ready_for_proposal")
        self.assertEqual(result.intent_key, "task-priority-friction")
        self.assertEqual(result.objective, "make prioritizing tasks feel effortless")
        self.assertEqual(result.evidence_ids, ("note-1", "voice-1"))


if __name__ == "__main__":
    unittest.main()
