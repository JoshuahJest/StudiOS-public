# StudiOS 1.1 — Mac Coordination Bootstrap

This repository contains **Mac-local, no-write coordination preparation** for the staged StudiOS intermachine architecture.

## Safety boundary

- It does not connect to ENKI-II, run remote commands, manage credentials, or control other agents.
- It does not copy Hermes homes, sessions, auth files, memory databases, or live state.
- It validates a proposed task envelope locally and produces reviewable artifacts only.

## Current contents

- `src/studios_coordination/envelope.py` — local-only host/expiry validation for proposed `studios.task-envelope/v1` envelopes.
- `tests/` — standard-library test coverage for accepted and expired envelopes.
- `docs/coordination/` — Mac capability report and receipt template.

## Run validation

```bash
cd /Users/thomas/StudiOS-1.1
PYTHONPATH=src python3 -m unittest tests.test_envelope -v
```

This is only Phase 0/host-preparation work. Remote worker enrollment must wait for ENKI-II's canonical revision-conflict gate and an approved no-write canary.
