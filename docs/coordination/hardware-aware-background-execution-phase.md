# Phase 1 Design: Hardware-Aware Background Execution

**Status:** Phase 1-2 operational. Cross-machine ENKI-II ↔ MacBook dispatch live.
**Scope:** MacBook M5 Max / 128 GB unified memory, ENKI-II / RTX 4090 24 GB / 64 GB RAM, and one RTX 6000 Ada 48 GB host  
**Primary outcome:** a durable, policy-driven scheduler that can first be proven entirely on one host, then enroll independent workers without sharing agent state or credentials.

## 1. Boundaries and invariants

1. A small StudiOS controller is the sole writer for job, attempt, lease, cancellation, credit, and verification state. It is not a shared Hermes home and holds no model, OAuth, SSH, browser, or user-login credentials.
2. Each host runs an independent worker under a dedicated local OS account. Each agent keeps its own Hermes profile, memory/session database, tools, model credentials, and provider credentials on that host.
3. Workers pull bounded work; the controller does not send arbitrary shell commands. A job names a registered `workload_kind` and version whose local adapter defines allowed inputs, outputs, paths, and side effects.
4. Shared Git contains contracts, policy, benchmark manifests, sanitized fixtures, and receipts—not secrets, live databases, private inputs, model weights, or agent homes.
5. Scheduling is **at least once**. Durable idempotency, monotonic fencing tokens, content-addressed inputs, and an atomic output-commit step prevent duplicate external effects.
6. Privacy, authority, data residency, approvals, and host health are hard gates. Cost, energy, and speed only rank candidates that pass all gates.
7. Capability claims expire and benchmark claims are evidence-backed. Hardware model names alone never qualify a workload.
8. A staged proposal is not accepted, executed, delivered, completed, or verified until the corresponding durable state and receipt exist.
9. Cross-machine work is operational. MacBook reaches ENKI-II at `http://100.79.103.70:8765` over Tailscale tailnet. NAS served via `https://joshuahs-macbook-pro.tail1ba312.ts.net/nas/*`. Both hosts enrolled in shared Tailscale org.

## 2. Deliverable architecture

```text
operator / approved producer
            |
            v
  StudiOS durable controller (SQLite first)
  - job and attempt state machine
  - policy evaluator and scheduler
  - capability/benchmark registry
  - credit ledger
  - append-only audit events
            ^
            | pull / claim / renew / receipt
            | (loopback first; private authenticated transport later)
   +--------+---------+------------------+
   |                  |                  |
Mac worker       ENKI-II worker    RTX 6000 worker
M5 Max/128 GB    4090/24+64 GB     Ada/48 GB
local adapters   local adapters    local adapters
local secrets    local secrets     local secrets
local agent      local agent       local agent
```

The initial implementation is one Python package and one SQLite database, not a service mesh. Keep transport behind an interface:

- `LoopbackTransport`: required for Stages 0–2 and all deterministic tests.
- `FileProposalTransport`: manual, non-secret proposal/receipt exchange only; documents are not execution authority.
- `PrivatePullTransport`: deferred until authenticated private reachability and worker enrollment are verified.

Recommended modules:

```text
src/studios_coordination/
  contracts.py       # typed records, canonical serialization, validation
  store.py           # migrations and transactional state transitions
  policy.py          # hard gates, credit/energy/latency rules
  scheduler.py       # eligibility and deterministic ranking
  worker.py          # pull/claim/renew/checkpoint/complete loop
  adapters.py        # registered workload kinds; never raw commands
  benchmark.py       # manifests, runners, signed result import
  receipts.py        # minimized evidence and artifact verification
```

## 3. Scheduler contracts

Use canonical JSON: UTF-8, sorted keys, no insignificant whitespace, timestamps as UTC RFC 3339, integer byte/duration/credit fields, and SHA-256 digests over the canonical bytes. Reject unknown schema major versions. Preserve identifiers exactly as submitted.

### 3.1 Capability advertisement: `studios.capability/v1`

Required fields:

```yaml
schema: studios.capability/v1
host_id: enki-ii
worker_id: enki-background-01
agent_id: independent-local-agent-id
advertisement_id: UUID
sequence: 42                  # strictly increases per worker identity
observed_at: UTC timestamp
expires_at: UTC timestamp     # at most 120 seconds after observed_at online
hardware:
  cpu_arch: string
  system_memory_bytes: integer
  accelerators:
    - id: local-stable-id
      family: cuda | metal | cpu
      model: string
      memory_bytes: integer | null
      driver_runtime: string | null
resources:
  cpu_threads_total: integer
  cpu_threads_free: integer
  system_memory_free_bytes: integer
  accelerator_memory_free_bytes: {local-stable-id: integer}
  scratch_free_bytes: integer
  active_slots: integer
  slot_limit: integer
power:
  source: ac | battery | unknown
  battery_percent: integer | null
  thermal_state: nominal | fair | serious | critical | unknown
  power_cap_watts: number | null
policy:
  accepted_data_classes: [synthetic, shared-nonsecret, private-local]
  accepted_workload_kinds: [kind@version]
  local_only_paths: [opaque-path-policy-id]
benchmarks:
  benchmark_set_digest: sha256 | null
  qualified_profiles: [profile-id]
health: ready | draining | degraded
key_id: enrolled-worker-key-id
signature: base64 | null
```

Rules:

- The controller uses only non-expired `ready` advertisements with a higher `sequence` than the last accepted record.
- A worker computes free resources locally. The controller never probes filenames, processes, credentials, or private data to infer capacity.
- `private-local` means the job may run only where the referenced input already resides; it does not authorize input transfer.
- Before authenticated enrollment, imports are marked `untrusted_offline` and may support benchmark review but cannot receive executable leases.
- Signature verification is mandatory for network execution. Keys are host-local; the controller stores only public keys, key IDs, revocation state, and rotation metadata.
- A capability omitted from the advertisement is unavailable. `unknown` energy or thermal telemetry is not interpreted as safe or free.

### 3.2 Job specification: `studios.job/v1`

Required fields:

```yaml
schema: studios.job/v1
job_id: UUID
idempotency_key: producer-scoped-stable-string
producer_id: registered-principal
submitted_at: UTC timestamp
not_before: UTC timestamp | null
expires_at: UTC timestamp
data_class: synthetic | shared-nonsecret | private-local
workload:
  kind: registered-kind
  version: exact-version
  parameters: adapter-validated-object
inputs:
  - uri: content-addressed-or-host-local-reference
    sha256: digest
    bytes: integer
    allowed_hosts: [host-id]
requirements:
  qualified_profile: profile-id | null
  accelerator_family: cuda | metal | cpu | any
  accelerator_memory_min_bytes: integer
  system_memory_min_bytes: integer
  scratch_min_bytes: integer
  cpu_threads_min: integer
  allowed_hosts: [host-id]
  forbidden_hosts: [host-id]
policy:
  latency_class: urgent | background | batch | opportunistic
  deadline_at: UTC timestamp | null
  energy_class: immediate | efficient | opportunistic
  max_credits: integer
  approval_id: string | null
  policy_digest: sha256
execution:
  max_attempts: integer
  attempt_timeout_seconds: integer
  lease_seconds: integer
  checkpoint: none | optional | required
  side_effect_class: none | artifact-only | external
outputs:
  artifact_contract: registered-contract-id
  required_evidence: [evidence-id]
```

Validation and authority rules:

- Uniqueness is `(producer_id, idempotency_key)`. Re-submitting identical canonical content returns the original `job_id`; different content returns `409 idempotency-conflict`.
- `expires_at`, positive finite budgets, bounded lease/timeout values, input hashes, policy digest, and an exact registered adapter version are mandatory.
- Reject `command`, `shell`, inline credentials, credential references that are not an approved local alias, traversal paths, unbounded URLs, and parameters unknown to the adapter schema.
- `external` side effects require a valid approval and an adapter-specific output idempotency key. This phase should enable only `none` and `artifact-only` workloads.
- Secrets may be referenced only by a host-local alias declared by the adapter. Aliases never leave the selected host; advertisements and receipts never reveal whether a particular secret exists.

### 3.3 Lease: `studios.lease/v1`

A lease is issued only by a transactional claim:

```yaml
schema: studios.lease/v1
job_id: UUID
attempt_id: UUID
attempt_number: integer
lease_id: UUID
worker_id: enrolled-worker-id
host_id: exact-host-id
fencing_token: integer         # monotonically increases for this job
issued_at: controller timestamp
starts_before: controller timestamp
expires_at: controller timestamp
envelope_digest: sha256
capability_digest: sha256
policy_digest: sha256
reserved_credits: integer
input_digests: [sha256]
nonce: 128-bit random value
```

Lease operations:

- `claim(job_id, worker_id, capability_digest)`: atomic `QUEUED/RETRY_WAIT -> LEASED`; verifies current capability, policy, attempt limit, cancellation, expiry, and credit reservation in one transaction.
- `start(lease_id, fencing_token)`: accepted once and only before `starts_before`; transitions `LEASED -> RUNNING`.
- `renew(lease_id, fencing_token, progress, checkpoint_digest?)`: extends from controller time, never worker time; compare-and-swap on current lease and token.
- `complete(lease_id, fencing_token, receipt_digest)`: records a proposed result. It does not directly mark the job verified.
- `abandon(...)`: releases the lease with a reason and lets retry policy decide.

Default bounds for the first real workload: `starts_before <= issued_at + 30s`, `lease_seconds = 60`, renew every 20 seconds, and `attempt_timeout_seconds <= 30m`. Later adapters may define different bounded profiles.

### 3.4 Receipt: `studios.receipt/v1`

A receipt includes job/attempt/lease IDs, fencing token, envelope/capability/policy/input digests, worker and host IDs, adapter version, model/runtime versions where applicable, controller-correlated start/end times, outcome, checkpoint/output artifact paths as policy-safe logical references, artifact hashes/sizes, required evidence, measured resource/energy summary, charged credits, cleanup state, and unresolved risks. It excludes credentials, prompts/private inputs unless separately authorized, process listings, and unrelated machine metadata.

The worker writes receipts through a durable local outbox before acknowledging completion. The controller verifies the active fencing token, required evidence, output contract, hashes, and credit charge before `SUCCEEDED -> VERIFIED`. Late or invalid receipts become `QUARANTINED_RESULT` audit records and cannot publish outputs.

## 4. Durable state model

### 4.1 SQLite tables

- `jobs`: immutable specification digest plus current state/revision.
- `attempts`: one row per attempt, selected host/profile, timestamps, terminal reason.
- `leases`: lease ID, attempt, fencing token, nonce digest, issue/renew/expiry times.
- `capability_ads`: canonical digest, trust state, expiry, accepted sequence.
- `benchmark_results`: manifest/result digests, environment fingerprint, qualification status.
- `idempotency_keys`: producer/key, request digest, canonical job ID.
- `receipts`: receipt digest, verification status, minimized body.
- `artifacts`: logical ID, digest, size, commit state, owning attempt.
- `credit_ledger`: append-only reserve/charge/release entries and policy digest.
- `job_events`: append-only transition audit with actor, prior/new revision, reason, timestamp.
- `worker_keys`: public keys only, enrollment/rotation/revocation timestamps.

Enable WAL mode, foreign keys, a busy timeout, and migrations. Each mutation uses `BEGIN IMMEDIATE`, checks `expected_revision`, inserts the audit event, and commits atomically. Back up with SQLite's online backup API; never copy an active database file.

### 4.2 Job states

```text
SUBMITTED -> QUEUED -> LEASED -> RUNNING -> SUCCEEDED -> VERIFYING -> VERIFIED
                 |        |         |            |           |
                 |        |         +----------> RETRY_WAIT --+
                 |        +--------------------> RETRY_WAIT
                 +-----------------------------> EXPIRED
any nonterminal ------------------------------> CANCEL_REQUESTED
LEASED/RUNNING + acknowledged cancellation ---> CANCELLED
terminal failures ----------------------------> FAILED | DENIED
invalid late output --------------------------> QUARANTINED_RESULT (event/result, not job success)
```

Terminal job states are `VERIFIED`, `CANCELLED`, `EXPIRED`, `FAILED`, and `DENIED`. `SUCCEEDED` means the worker reported success; it is not completion until verification passes. A job never moves backward. Retries create a new attempt and fencing token rather than reopening an old attempt.

Recovery on controller restart:

1. Reopen SQLite and run integrity/migration checks.
2. Reconcile expired leases using controller time.
3. For a checkpointable attempt, create `RETRY_WAIT` referencing the verified checkpoint; otherwise retry from immutable inputs.
4. Release unused credit reservations by append-only compensating entries.
5. Preserve all prior attempts and receipts.

Recovery on worker restart:

1. Open the host-local durable `seen_envelopes` and receipt outbox stores.
2. Do not resume merely because local work exists; query the controller for the exact lease/token.
3. Resume only an active, matching, checkpoint-qualified lease. Otherwise stop local work and retain/quarantine evidence.
4. Retransmit outbox receipts idempotently until acknowledged.

## 5. Replay protection and output commit

Use four layers:

1. **Submission dedupe:** unique producer/idempotency key plus request digest.
2. **Envelope authenticity:** enrolled key signature, exact host/worker, short expiry, nonce, and canonical digest.
3. **Execution dedupe:** worker-local durable unique constraint on `(envelope_digest, fencing_token)` and a completed-envelope tombstone retained for at least the job retention period.
4. **Commit fencing:** outputs first land in an attempt-scoped temporary location. Only the controller-authorized current fencing token can atomically promote the expected artifact digest to its final logical name.

A lease expiry does not prove computation stopped. Therefore an expired attempt loses commit authority immediately. The next attempt may compute, but the old attempt can only submit a quarantined receipt. Cleanup of temporary artifacts is adapter-owned and receipted.

Clock policy: the controller is authoritative for leases. Workers record monotonic elapsed time and treat local wall-clock skew over 30 seconds as degraded; they refuse new work when skew exceeds 120 seconds. Signature/key rotation supports overlap, explicit revocation, and an audit trail; it is not implemented as a shared secret copied among agents.

## 6. Energy, credit, and latency policy

### 6.1 Hard-gate order

For every scheduling decision, apply in this order:

1. schema, signature/trust, expiry, cancellation, revision, and approval;
2. data class, input locality, allowed/forbidden host, and adapter path policy;
3. advertised and benchmark-qualified capability;
4. free memory/scratch/slot headroom and host health;
5. thermal, battery, operator reserve, maintenance/drain, and quiet-hour policy;
6. deadline feasibility using the qualified profile's observed runtime bound;
7. available job/account/host credits;
8. ranking among remaining candidates.

A lower-cost or faster host never bypasses a hard gate.

### 6.2 Latency classes

| Class | Dispatch objective | Queue behavior | Energy behavior |
|---|---:|---|---|
| `urgent` | claim within 10 s when an eligible idle worker exists | may use reserved capacity; never preempts unsafe/non-checkpointable work | chooses fastest deadline-feasible route within credits |
| `background` | claim within 2 min | fair queue; respects interactive reserve | balances predicted completion, energy, and credits |
| `batch` | claim within 15 min | may coalesce jobs and wait for an efficient host | prefers lowest predicted credits meeting deadline |
| `opportunistic` | no claim SLO | runs only in declared idle windows and may be paused/cancelled | requires energy/thermal eligibility; no deadline promise |

These are controller objectives, not guaranteed end-to-end latency. If no route is feasible, the scheduler records a stable reason such as `NO_QUALIFIED_PROFILE`, `DATA_NOT_LOCAL`, `ENERGY_DEFERRED`, `CREDIT_LIMIT`, or `DEADLINE_INFEASIBLE` rather than silently relaxing policy.

### 6.3 Energy classes and host reserves

- `immediate`: run when safe; energy influences ranking but does not defer a deadline-feasible job.
- `efficient`: defer within the latency/deadline window for a materially more efficient qualified host.
- `opportunistic`: run only when the host advertises AC power, acceptable thermal state, idle capacity, and an operator-approved window.

Initial conservative policy:

- **MacBook:** keep one interactive CPU slot and configurable unified-memory reserve (default 24 GiB). On battery, deny new accelerator jobs and jobs predicted over 10 minutes; permit only explicitly allowed low-power CPU work. Pause checkpointable opportunistic work at 30% battery and reject new work at 40%. `serious` or `critical` thermal state drains the worker.
- **ENKI-II / RTX 4090:** reserve configurable VRAM (default 2 GiB) and system memory (default 12 GiB); enforce a locally configured power/temperature policy. Unknown or stale GPU telemetry makes the worker ineligible for accelerator jobs.
- **RTX 6000 Ada:** reserve configurable VRAM (default 4 GiB) and system memory (default 12 GiB until the host's actual RAM is advertised). Its 48 GB nameplate does not qualify large jobs without measured free-memory headroom and a benchmark profile.

All defaults are versioned configuration, not baked-in hardware facts. A worker can always advertise stricter local limits; the controller cannot weaken them.

### 6.4 Credits

Credits are internal capacity accounting, not currency and not a substitute for measured energy. Store integer milli-credits (`mCr`) only.

For benchmark profile `p` on host `h`:

```text
predicted_charge_mCr =
  ceil(profile_runtime_p90_seconds / 60 * occupancy_rate_mCr_per_minute)
  + ceil(profile_energy_p90_Wh * energy_rate_mCr_per_Wh)
  + fixed_start_mCr
```

- `occupancy_rate` captures scarce CPU/GPU/memory capacity and is policy-configured per qualified profile.
- `energy_rate` may vary by host/time window but is versioned and included in the decision record.
- If energy cannot be measured, use a conservative configured upper bound and mark `energy_estimated`; never assign zero.
- Claim reserves the predicted charge. Completion charges measured/derived usage up to `max_credits`, then releases the remainder. Exceeding the reservation requires policy approval or checkpoint-and-stop; it cannot produce a negative ledger balance.
- Maintain separate pools for urgent reserve and normal/background work. Opportunistic jobs cannot consume urgent reserve.

Initial fairness is weighted deficit round robin across producer queues within each latency class, followed by host ranking. This prevents a stream of small jobs or one producer's large batch from starving others.

### 6.5 Deterministic host ranking

For eligible host/profile pairs, compute normalized terms from the same benchmark set:

```text
score =
  0.50 * predicted_finish_ratio
+ 0.25 * predicted_credit_ratio
+ 0.15 * predicted_energy_ratio
+ 0.10 * locality_penalty
```

Lower is better. `predicted_finish_ratio` includes queue wait plus runtime divided by remaining deadline window; without a deadline it is normalized against eligible candidates. Local input has zero locality penalty; an already-authorized content transfer has a positive measured penalty; prohibited transfer was removed by a hard gate. Tie-break by earlier capability sequence acceptance, then lexical `host_id`, so tests are repeatable. Policy versions may change weights only with replay tests and benchmark evidence.

## 7. Benchmark and qualification design

### 7.1 Principles

- Run the same sanitized, versioned manifest on each host through its local adapter.
- Pin dataset/input digests, adapter version, model digest where applicable, runtime/driver version, precision, context/batch settings, and policy profile.
- Correctness is a gate; throughput does not compensate for wrong output.
- Separate cold start from warm execution. Record failures and out-of-memory outcomes rather than deleting them.
- Do not transfer private benchmark inputs or credentials. Hosts may export only minimized result bundles and signed evidence.

### 7.2 Workload profiles

1. `cpu-transform-small`: deterministic JSONL parse/validate/hash; exercises CPU dispatch and artifact commit.
2. `memory-transform-large`: deterministic synthetic blocks sized at 8, 24, and 48 GiB where safe; proves admission headroom and cancellation without assuming all hosts can run all sizes.
3. `inference-short`: fixed sanitized prompts, fixed output checks, 1-input latency and warm throughput.
4. `inference-long-context`: fixed context/output sizes; measures memory pressure and time-to-first/output token where supported.
5. `accelerator-sustained-10m`: deterministic inference or compute loop; tests thermal/power stability and checkpoint/cancel responsiveness.
6. `checkpoint-recovery`: kill worker after a checkpoint, restart, reacquire/validate a new lease, and verify only the current fencing token can commit.
7. `contention-reserve`: benchmark while an operator-safe synthetic foreground load is present; validates advertised reserve rather than maximizing utilization.

Model-based profiles are parameterized by **measured loaded footprint**, runtime, quantization/precision, and model digest—not labels such as “small” or assumptions that a given GPU can load them.

### 7.3 Measurements

Record:

- queue-to-claim, cold load, time to first result/token, total duration, and throughput;
- peak system memory, peak accelerator memory, scratch bytes, CPU/GPU utilization;
- integrated energy Wh, telemetry source/sample interval, power cap, thermal state, and throttling events;
- exact output digest plus profile-specific correctness score;
- cancellation acknowledgement and stop latency;
- checkpoint size/time and recovery duration;
- runtime, driver, adapter, manifest, and capability digests.

For NVIDIA, integrate available host-local power samples (for example, one-second GPU power telemetry) and label this `gpu_only` unless whole-system energy is actually measured. For macOS, use an approved non-interactive telemetry source if available; otherwise mark energy unavailable and apply the conservative estimate policy. Never require persistent privilege escalation merely to obtain energy telemetry.

### 7.4 Procedure and qualification

- Validate fixtures once, then perform 2 warmups and 10 measured runs per short profile/host; perform 1 warmup and 5 measured runs for sustained/large profiles.
- With fewer than 30 observations, report median, maximum, and empirical p90; do not label the result p95. The live predictor may publish p95 only after 30 comparable successful observations.
- Randomize profile order where safe, restore a declared idle window, and record ambient/background caveats.
- A profile is qualified only if all correctness gates pass, zero unhandled OOMs occur within the declared requirement, cancellation meets its adapter bound, memory reserves are preserved, telemetry is complete or conservatively estimated, and the result bundle verifies against its manifest.
- Qualification is keyed by host hardware fingerprint, adapter/model/runtime tuple, and benchmark digest. Driver/runtime/model/adapter changes invalidate or downgrade it to `needs_requalification`.

The benchmark report must compare routes by correctness, cold/warm latency, sustained throughput, observed/estimated energy, credits, failure rate, and reserve impact. It must not declare one machine globally “best”; qualification and routing are workload-specific.

## 8. Rollout stages

### Stage 0 — contracts and deterministic local simulator

Implement schemas, canonical digests, SQLite migrations, state transitions, policy reasons, scheduler ranking, fake clock, fake capabilities, and failure injection. Extend—not silently replace—the existing local `envelope.py` behavior.

**Exit criteria:** AC-001 through AC-012, AC-020 through AC-026, and AC-030 through AC-034 pass in local automated tests; no network or credential dependency exists.

### Stage 1 — single-host Mac durable worker

Run only registered synthetic `none`/`artifact-only` adapters through loopback. Add local outbox, restart recovery, cancellation, checkpoint test, credit ledger, and conservative Mac energy handling. Start CPU-only; qualify Metal work separately.

**Exit criteria:** 24-hour soak or 1,000 synthetic jobs (whichever occurs first), no duplicate committed artifacts, no lost accepted jobs, restart recovery demonstrated, and AC-040 through AC-048 pass.

### Stage 2 — host-local benchmark kits, still offline

Run the same signed/versioned benchmark manifest locally on each available machine. Import only sanitized result bundles into the controller. An offline/untrusted result may inform review but cannot authorize leases until enrollment verifies the worker key and result provenance.

**Exit criteria:** each intended host has a capability card and result bundle, unsupported profiles are explicitly recorded, and AC-050 through AC-057 pass. A missing host blocks only that host's enrollment, not local development.

### Stage 3 — private reachability and enrollment

Install/authenticate the approved private-network client locally on each host, prove named-host reachability, enroll unique worker public keys, and expose one pull service/port to the exact approved host identities. Keep unrelated SSH, database, Docker, dashboards, subnet routing, exit-node behavior, and public ingress denied.

**Entry blocker now:** Mac-to-ENKI-II reachability is not established and the Tailscale CLI is absent. This stage is pending operator/network setup and cannot be represented as complete.

**Exit criteria:** AC-060 through AC-067 pass, including denial tests and key revocation. Credentials remain local.

### Stage 4 — multi-host no-write canary

Use synthetic, non-secret fixtures only. Exercise accepted, wrong-host, expired, replayed, cancelled, stale-capability, lease-loss, late-receipt, and policy-denied jobs on one worker at a time, then concurrent workers. Manually review receipts.

**Exit criteria:** all outcomes are correctly receipted, stale attempts cannot commit, controller restart preserves authority, and AC-070 through AC-078 pass.

### Stage 5 — bounded background production

Enable allowlisted artifact-only workloads with explicit data locality, credit limits, operator reserve, deadline policy, and rollback. Start at one concurrent job per host. Raise concurrency only after a seven-day error/thermal/latency review.

**Exit criteria:** AC-080 through AC-087 pass for seven consecutive days; no secret leakage, policy bypass, duplicate publication, unresolved credit drift, or unexplained job loss.

### Stage 6 — optional external effects

Out of scope for this phase. Requires a separate threat model, named approval workflow, adapter-specific idempotency/rollback, and operator acceptance. Never unlock it merely because artifact-only work is stable.

## 9. Implementation acceptance criteria

### Contracts and validation

- **AC-001:** Canonically equivalent JSON produces the same SHA-256 digest; changed typed content produces a different digest.
- **AC-002:** Unknown schema majors, missing required fields, unknown adapter parameters, naive/non-UTC timestamps, invalid bounds, and unknown enum values are rejected with stable reason codes.
- **AC-003:** Raw command/shell payloads, inline secrets, traversal paths, and unregistered adapter versions are rejected before persistence or execution.
- **AC-004:** Same producer/idempotency key plus same request digest returns the original job; different digest returns `idempotency-conflict`.
- **AC-005:** Allowed/forbidden host, local data class, input digest, approval, and path-policy rules have positive and negative tests.
- **AC-006:** Capability sequence rollback, expiry, wrong worker/host, revoked key, invalid signature, and stale benchmark digest are rejected.
- **AC-007:** Pre-enrollment capability/benchmark imports are labeled untrusted and cannot become lease-eligible.
- **AC-008:** Receipts never serialize local credential aliases, credential values, private raw inputs, or unrelated host metadata.
- **AC-009:** Every scheduling and denial record includes the exact job, capability, benchmark, and policy digests used.
- **AC-010:** Contract fixtures round-trip without changing identifiers, integer units, digests, or timestamp instants.
- **AC-011:** Existing `envelope.py` wrong-host, invalid-expiry, expired, and raw-command behavior remains covered while the v1 contract is extended.
- **AC-012:** Property/fuzz tests cannot crash contract parsing or create an executable lease from malformed input.

### Durability, leases, and replay

- **AC-020:** Every state mutation uses expected revision and transactionally writes exactly one audit event; a stale concurrent mutation loses without partial changes.
- **AC-021:** Two simultaneous claims for one job yield exactly one active lease and one fencing token winner.
- **AC-022:** Lease renew/start/complete with a wrong lease ID, worker, nonce, or fencing token is rejected without state or artifact mutation.
- **AC-023:** After lease expiry/reassignment, the old worker's success receipt is quarantined and cannot promote its artifact.
- **AC-024:** Worker restart retransmits one durable receipt idempotently; controller restart neither loses accepted jobs nor creates duplicate attempts for a live lease.
- **AC-025:** Cancellation is durable, prevents new claims, reaches a running test adapter within its declared bound, and records cleanup. Race tests cover cancel-versus-complete.
- **AC-026:** Credit reserve, charge, and release reconcile to the ledger after success, failure, cancellation, expiry, and crash; compensations are append-only.

### Scheduler and policy

- **AC-030:** Hard-gate tests prove no score, urgency, or credit setting can bypass privacy, approval, locality, health, or capability requirements.
- **AC-031:** Given identical inputs and policy, ranking is deterministic and follows the documented tie-break.
- **AC-032:** Deadline-infeasible, no-qualified-profile, stale-capability, energy-deferred, data-not-local, memory-reserve, and credit-limit paths return distinct stable reasons.
- **AC-033:** Weighted deficit round robin gives every continuously eligible producer its configured share in a 10,000-job simulation and preserves urgent reserve from opportunistic use.
- **AC-034:** Unknown energy is charged conservatively, never as zero; energy and occupancy rate changes are policy-versioned and replayable.

### Single-host execution

- **AC-040:** A loopback worker executes 1,000 synthetic jobs or a 24-hour soak with zero lost accepted jobs and zero duplicate final artifacts.
- **AC-041:** Forced controller crashes at each state transition recover to a valid state with a complete audit trail.
- **AC-042:** Forced worker crashes before/after checkpoint and receipt-outbox write recover as specified; only the latest token can commit.
- **AC-043:** Output promotion verifies expected digest/size and is atomic; corrupt/missing output yields verification failure, not `VERIFIED`.
- **AC-044:** Mac battery/AC and thermal states are simulated deterministically; policy denies, pauses, or drains exactly at configured thresholds.
- **AC-045:** The worker preserves configured CPU, unified-memory, and scratch reserves under admission stress.
- **AC-046:** Adapter sandbox/path tests prove writes remain inside attempt scratch and authorized artifact destinations.
- **AC-047:** SQLite online backup restores to an integrity-checked database with the same jobs, attempts, ledger balance, and verified artifact metadata.
- **AC-048:** Logs and receipts pass automated secret-pattern tests plus manual review of one complete job lifecycle.

### Benchmarks and qualification

- **AC-050:** A manifest and result bundle have reproducible canonical digests and record all required runtime/input/environment fields.
- **AC-051:** Correctness failure, OOM, reserve breach, missing telemetry without an estimate, or cancellation-bound failure prevents qualification.
- **AC-052:** Benchmark reporting distinguishes cold/warm runs, GPU-only versus whole-system energy, measured versus estimated energy, and empirical p90 versus qualified p95.
- **AC-053:** Runtime, driver, model, adapter, or manifest digest change marks the affected profile `needs_requalification`.
- **AC-054:** Scheduler admission uses measured peak plus configured reserve and rejects a profile that relies only on accelerator nameplate memory.
- **AC-055:** Checkpoint benchmark proves a stale pre-crash attempt cannot publish after recovery.
- **AC-056:** All three hosts use the same sanitized fixture digests; host-specific unsupported profiles remain visible rather than being omitted.
- **AC-057:** Importing an offline result changes no execution authority until worker key enrollment and provenance verification succeed.

### Network enrollment

- **AC-060:** Each host uses a unique worker identity/key and independent local agent home; no test copies credentials, sessions, memory databases, or browser profiles.
- **AC-061:** The named private endpoint is reachable and authenticated only after explicit setup; no test substitutes LAN scan results for identity.
- **AC-062:** The exact approved source/worker can access the one pull endpoint/port; unrelated hosts and ports are denied.
- **AC-063:** Wrong-host, expired, replayed, tampered, unsigned, and revoked-key envelopes are denied at the worker before adapter invocation.
- **AC-064:** Public ingress, Funnel-equivalent exposure, exit-node behavior, subnet routing, broad SSH, raw database, Docker, and dashboard access remain disabled unless separately approved.
- **AC-065:** Key rotation supports bounded overlap; revocation stops new claims and renewals while preserving prior audit verification.
- **AC-066:** A network outage longer than a lease proves the disconnected worker loses commit authority and later submits only a quarantined receipt.
- **AC-067:** Removing network access leaves each agent locally usable with its own credentials and state.

### Multi-host canary and production

- **AC-070:** Canary matrix covers accepted, wrong-host, expired, duplicate-submission, replayed-envelope, cancellation, denial, stale capability, lease loss, and late completion on every enrolled worker.
- **AC-071:** Concurrent workers cannot claim the same attempt, oversubscribe a host's advertised slot/memory reserve, or promote two artifacts for one logical output.
- **AC-072:** Data-local jobs never move inputs to another host; the scheduler records `DATA_NOT_LOCAL` when the local host is unavailable.
- **AC-073:** Capability expiry/drain removes a host from new scheduling within 120 seconds without killing an unrelated local agent session.
- **AC-074:** Scheduler chooses only benchmark-qualified routes and records its alternatives, hard-gate removals, score inputs, and final reason without sensitive data.
- **AC-075:** Receipt verification detects modified artifact, benchmark, capability, policy, and envelope digests.
- **AC-076:** Cancellation and deadline behavior meet each registered adapter's declared bound under cross-host latency and outage injection.
- **AC-077:** Manual shared-folder proposals remain non-executable until imported and approved through the authenticated controller path.
- **AC-078:** Dashboard/status language distinguishes prepared, staged, accepted, running, succeeded, verified, and quarantined states.
- **AC-080:** Seven-day production pilot has no lost accepted jobs, duplicate final artifacts, unauthorized host/data route, policy bypass, or secret-bearing receipt.
- **AC-081:** Per-host success rate, retry rate, queue/claim/runtime percentiles, correctness, energy quality, credits, reserve breaches, thermal deferrals, and cancellation latency are reviewable.
- **AC-082:** Ledger reconciliation is exact daily; any drift drains new scheduling until resolved.
- **AC-083:** A worker can drain and upgrade without losing jobs; changed runtime/profile is requalified before reuse.
- **AC-084:** Concurrency remains one per host until the pilot passes; each increase repeats contention, reserve, cancellation, and thermal tests.
- **AC-085:** Rollback disables new claims, lets policy-approved jobs finish or cancel, preserves receipts/audit data, and leaves independent local agents functional.
- **AC-086:** Artifact retention and cleanup run by explicit policy; cleanup never deletes an unverified current attempt or sole verified artifact.
- **AC-087:** Operator review signs off host-specific routes and unsupported workloads; no report calls cross-machine operation complete while a reachability/enrollment criterion is open.

## 10. Explicit non-goals for this phase

- Sharing or synchronizing Hermes homes, credentials, sessions, browser state, or live memory databases.
- General remote shell, arbitrary code payloads, public APIs, broad peer access, or shared database access.
- Migrating private inputs between hosts merely to improve utilization.
- Automatic financial, publishing, deployment, destructive, or other external effects.
- Declaring the RTX 4090, RTX 6000 Ada, or M5 Max universally preferable without workload-specific qualification.
- Claiming ENKI-II or the RTX 6000 host is enrolled before private reachability, identity, policy, denial, and canary evidence exists.

## 11. First implementation slice

The first mergeable slice should be Stage 0 plus one thin Stage 1 path:

1. versioned Python dataclasses/validators for capability, job, lease, and receipt;
2. SQLite migrations and compare-and-swap state transitions;
3. fake clock, fake worker, and deterministic scheduler;
4. one `cpu-transform-small@1` adapter over synthetic fixtures;
5. durable worker seen-envelope and receipt-outbox stores;
6. atomic content-addressed artifact commit;
7. tests for AC-001–012, AC-020–026, AC-030–034, and AC-040–043.

Do not make network setup, CUDA, Metal, Tailscale, ENKI-II, or the RTX 6000 host a prerequisite for that slice. This produces useful, testable scheduling semantics now while keeping remote enrollment honest and reversible.
