# StudiOS 1.1 — Mac Coordination Bootstrap

This repository contains **Mac-local, no-write coordination preparation** for the staged StudiOS intermachine architecture.

## Safety boundary

- It does not connect to ENKI-II, run remote commands, manage credentials, or control other agents.
- It does not copy Hermes homes, sessions, auth files, memory databases, or live state.
- It validates a proposed task envelope locally and produces reviewable artifacts only.

## Current contents

- `src/studios_coordination/envelope.py` — local-only host/expiry validation for proposed `studios.task-envelope/v1` envelopes.
- `src/studios_coordination/intent.py` — evidence-preserving synthesis of multimodal, multi-machine interpretation candidates into clarification, proposal, or approval-required states.
- `src/studios_coordination/scheduler.py` — deterministic local-first routing plus an expiring filesystem-lease tracer.
- `src/studios_coordination/pinokio.py` — loopback-only, pinned-app lifecycle planning; it cannot install, update, reset, download, or execute.
- `tests/` — standard-library coverage for envelopes, request-to-intent synthesis, routing, leases, and Pinokio boundaries.
- `docs/coordination/request-to-intent.md` — interpretation/authorization boundary.
- `docs/coordination/local-compute-control-plane.md` — Pinokio's role, machine responsibilities, and workload rollout.
- `docs/coordination/hardware-aware-background-execution-phase.md` — durable worker target design and acceptance criteria.
- `docs/research/2026-09-05-h3-pinokio-security-audit.md` — pinned H3/Pinokio source and license review.

## Run validation

```bash
cd ~/StudiOS-1.1
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

This is only Phase 0/host-preparation work. Remote worker enrollment must wait for ENKI-II's canonical revision-conflict gate and an approved no-write canary.
