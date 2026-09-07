# StudiOS Local Compute Control Plane

**Implementation status:** local routing tracer and filesystem lease implemented; distributed execution remains disabled.

## Pinokio's role

Pinokio is a **host-local application installer and supervisor**, not the StudiOS scheduler, trust root, security sandbox, or cross-machine transport. Its Electron shell intentionally exposes a terminal-like execution environment and weakens several normal browser isolation controls; the StudiOS boundary therefore assumes a compromised Pinokio app can act with the Pinokio process's OS permissions.

```text
Intent proposal
      │
      ▼
policy + approval + typed JobRequest
      │
      ▼
StudiOS scheduler ── capability/benchmark registry
      │
      ▼
signed, expiring lease for a registered workload
      │
      ▼
independent host-local worker
      ├─ direct local model endpoint
      ├─ ComfyUI REST adapter
      ├─ Phosphene loopback adapter
      └─ approved Pinokio start/stop plan
```

A job never carries a shell command, Git URL, Pinokio script body, credential, or unrestricted filesystem path. Pinokio starts an already-audited runtime; the worker then talks to that runtime's typed loopback API.

`src/studios_coordination/pinokio.py` currently:

- accepts only `start` and `stop`;
- requires an exact 40-character Git revision;
- requires the app to be a direct child of the configured Pinokio `api` directory;
- rejects nested or absolute script paths;
- rejects non-loopback Pinokio and application URLs;
- emits a reviewable Pinokio 8 loopback request and marks it `executed=False`;
- cannot install, update, reset, download, or execute an app.

This is intentionally a planning boundary. A later authorized runner must recheck the on-disk Git revision immediately before issuing the request, then verify the exact loopback health endpoint afterward.

## Routing tracer implemented

`src/studios_coordination/scheduler.py` provides:

- typed worker capabilities, job requests, benchmark estimates, and policies;
- hard filters for online state, slots, task support, backend support, VRAM, runtime, energy, privacy, and cloud-credit limits;
- background routing by measured energy estimate, then runtime;
- interactive routing by runtime, then energy;
- restricted-data denial for cloud workers regardless of cloud policy;
- an atomic, expiring local filesystem lease with idempotent same-worker claims;
- fail-closed behavior when an existing lease is malformed.

This is a tracer bullet, not the final distributed scheduler. The detailed target contract, SQLite state machine, fencing design, benchmark protocol, and staged rollout are in `hardware-aware-background-execution-phase.md`.

## Initial machine responsibilities

### MacBook M5 Max / 128 GB

Primary:

- interactive StudiOS control plane and approvals;
- local intent extraction, transcription, embeddings, RAG, and document transforms;
- review, curation, provenance, and lightweight draft generation;
- Phosphene/MLX character and video experiments when on AC power;
- overnight CPU/Metal work only when measured energy and thermal policy make it preferable.

Reserve at least 24 GiB unified memory for the operator by default. New accelerator jobs should not start on battery. Lid-close sleep pauses work; durable jobs reconcile on wake instead of preventing sleep.

### ENKI-II / RTX 4090 24 GB

Primary once enrolled:

- persistent local instruction-model endpoint;
- interactive quantized H3/image/video drafts;
- CUDA-only ComfyUI workflows that fit verified VRAM headroom;
- batch coding, extraction, and evaluation workers when they do not interfere with interactive GPU work.

Start with one GPU job at a time, a 2 GiB VRAM reserve, and a 12 GiB system-memory reserve. These are policy defaults, not hardware claims.

### RTX 6000 Ada / 48 GB

Primary once its actual host and RAM are verified:

- high-quality H3 and other long video batches;
- larger model inference or training profiles that do not fit the 4090;
- high-resolution refinement/upscaling;
- long CUDA jobs with checkpoint and receipt requirements.

Do not qualify workloads from the GPU model name. Qualification requires a signed capability advertisement plus a public-fixture benchmark at the exact model/runtime/precision revision.

## Energy and credit policy

1. Privacy, authorization, data locality, health, and capability are hard gates.
2. Interactive work chooses the fastest qualified route within budget.
3. Background work chooses the lowest measured/estimated energy route within its deadline.
4. Opportunistic work runs only in declared idle/AC/thermal windows.
5. Unknown energy is conservatively estimated, never treated as zero.
6. Cloud/OAuth models are explicit fallbacks with a per-job credit budget. Restricted inputs never route to them.
7. Repeated model cold starts count against energy and latency; warm endpoint occupancy is benchmarked separately.
8. Quality ladders stop after a failed structural draft instead of spending on upscale/final passes.

## Workload order

Implement adapters in this order:

1. `synthetic.transform/v1` — validates durability, lease expiry, restart, cancellation, and artifact commitment without models.
2. `local.llm.inference/v1` — typed OpenAI-compatible request to a loopback model endpoint.
3. `comfy.workflow/v1` — pinned API-format workflow, exact model/node manifest, seed, budget, and output contract.
4. `phosphene.video/v1` — Mac-local queue adapter after analytics/network controls and model-license review.
5. `minimax.director.compile/v1` — compile/lint a `ShotSpec` without GPU execution.
6. `minimax.h3.render/v1` — render only after a short public-fixture benchmark qualifies the host.

Maestro is excluded from automated adapters under its current non-commercial license. MiniMax H3 Studio is excluded until its software license and transitive revision pins are clarified.

## Definition of the next substantial phase

The next phase is **not more GUI work**. It is the durable single-host worker that proves StudiOS can run background work safely before any second machine is enrolled:

- SQLite job, attempt, lease, event, receipt, and credit tables;
- canonical JSON digests and idempotency keys;
- registered adapters instead of commands;
- restart-safe worker outbox and artifact commit fencing;
- AC/battery/thermal/operator-reserve policy;
- benchmark manifests and public fixtures;
- a 1,000-job or 24-hour synthetic soak;
- only then private worker enrollment over the approved Tailnet.

The Mac-to-ENKI-II path is still staged, not operational. The absence of a live private transport must not be bypassed with Pinokio Home Server, public ComfyUI binding, Dropbox polling that executes documents, or shared Hermes state.
