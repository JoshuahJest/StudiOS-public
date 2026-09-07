# Request-to-intent inference layer

StudiOS needs an interpretation layer between Thomas's natural requests and its narrow tool contracts. The layer must help articulate intent without silently converting inference into authority.

## Pipeline

```text
speech / text / images / documents / device context
                         |
              machine-local capture adapters
                         |
       immutable Signals with source + machine identity
                         |
       one or more modality-specific interpreters
                         |
    evidence-citing Interpretation candidates
                         |
          deterministic intent synthesis
                         |
       clarify | proposal | approval required
                         |
            policy-gated tool contracts
```

## Implemented tracer bullet

`src/studios_coordination/intent.py` implements the safe synthesis boundary:

- accepts signals from different modalities and machines;
- requires interpretations to cite known signal IDs;
- combines paraphrases through a shared semantic `intent_key`;
- preserves evidence, modality, and machine provenance;
- requests clarification for equally supported conflicting objectives;
- marks high-risk requests as approval-required;
- never authorizes or executes an action.

The model-facing portion is deliberately an adapter boundary. Models propose `Interpretation` records; deterministic code decides whether the evidence is sufficient to formulate a proposal.

## Recommended interpreter ensemble

Use specialized components instead of one model trying to infer everything:

1. **Speech capture:** local speech-to-text on the originating machine, retaining the audio reference and transcript confidence.
2. **Visual interpretation:** a local vision-language model summarizes selected screenshots or camera frames; no ambient capture by default.
3. **Document/context retrieval:** retrieval selects small, cited context packets from approved StudiOS records.
4. **Fast intent drafter:** a small local instruction model emits an interpretation and semantic intent key.
5. **Independent critic:** a second model checks evidence coverage, contradictions, missing scope, and risk.
6. **Deterministic synthesizer:** `synthesize_intent` combines only grounded candidates.
7. **Human checkpoint:** ambiguity becomes a concise clarification; high-risk intent becomes a proposal awaiting approval.

A strong cloud model may be used as an escalated critic for difficult requests, but raw private inputs should remain local unless their data policy explicitly permits transfer.

## Multi-machine rules

- Capture and reduce inputs on the machine where they originate.
- Exchange signed, expiring envelopes containing hashes and minimal derived text—not broad filesystem or Hermes-state access.
- Preserve `machine_id`, source identity, modality, and evidence IDs end to end.
- Do not treat agreement between two models reading the same source as independent corroboration.
- Do not let a remote interpretation grant capabilities; the receiving host applies its own policy.
- Keep audio, images, and private documents out of Git.

## Next slices

1. JSON Schema serialization for Signals, Interpretations, and IntentSynthesis.
2. Signed transport in `studios.task-envelope/v1`.
3. Local speech adapter with explicit recording consent.
4. Model adapter returning schema-constrained interpretations.
5. Critic pass and calibrated confidence.
6. Intent-to-tool proposal compiler, still without execution authority.
