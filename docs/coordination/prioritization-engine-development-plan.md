---
type: development-plan
schema: studiosaos-dev/v1
host: macbook-worker
created: 2026-09-06
updated: 2026-09-06T22:55:00Z
status: in-progress
owner: studi-worker (MacBook Hermes, sonnet/anthropic + minimax-oauth)
context: Shared across StudiOS fleet — all agents read this to understand current work.
---

# Prioritization Engine — Development Plan
**Status:** In Progress (Phase 1 shipping)
**Sprint started:** 2026-09-06
**Owner:** studi-worker (MacBook Hermes, sonnet via anthropic + minimax-oauth)  
**Context:** This plan lives in the shared StudiOS repo so all fleet agents can see current work and intent.

---

## Context

StudiOS currently has a working dispatch layer (`scheduler.py`, `dispatch_task_to_enkii()`, `ComfyUIAdapter`) but no **automatic prioritization engine** that scores incoming requests across all available workers using live system state, active workload, energy constraints, skill availability, and data locality.

Without this engine, every request requires a human (or the top-level agent) to manually decide which machine to use. The engine closes that loop.

---

## ✅ Phase 1 — Core Engine (SHIPPED 2026-09-06)

| Module | File | Status |
|--------|------|--------|
| Skill Registry | `src/studios_coordination/skill_registry.py` | ✅ Done |
| Energy Policy | `src/studios_coordination/energy_policy.py` | ✅ Done |
| Prioritization Engine | `src/studios_coordination/prioritization_engine.py` | ✅ Done |
| Intent-Aware Router | `src/studios_coordination/intent_aware_router.py` | ✅ Done |
| REST Dispatch Endpoint | `POST /api/dispatch` in `studios_inbox.py` | ✅ Done (live) |
| Kanban Poller | `scripts/studios_kanban_poller.py` | ✅ Done |
| Tests | `tests/test_prioritization_engine.py` | ✅ 23/23 passing |

**Verified live:**
- `POST /api/dispatch {"task_kind":"comfyui-workflow"}` → `{"dispatched":true,"primary_worker_id":"macbook-worker","scores":{"macbook-worker":{"total_score":0.16}}}` ✅

---

## 🔄 Phase 2 — Next Steps

- [ ] Wire intent output → dispatch (intent_aware_router already written; needs live signal feed)
- [ ] Add `POST /api/prioritize` to StudiOS API (external callers can submit a raw request and get a dispatch decision)
- [ ] Register kanban poller as launchd service (`com.studios.kanban-poller.plist`)
- [ ] Pull qwen2.5:3b into Ollama for code-specific background tasks
- [ ] Verify ENKI-II StudiOS API reachability and add ENKI-II to the dispatch fleet state

---

## 🔲 Future Phases

- [ ] IntentAwareRouter → IntentSynthesis live feed (signals from iMessage, cron, Dropbox handoff)
- [ ] Multi-agent coordination: Wanda on ENKI-II receives dispatch decisions and executes
- [ ] SOLARIS repair + thermal re-enrollment in fleet
- [ ] Anthropic MCP server: OAuth token flow (requires browser for initial OAuth; session token can be stored in `~/.claude/` after manual auth)

---

## Architecture Overview

```
Natural request (any channel)
        ↓
Intent synthesis (intent.py — IntentSynthesis)
        ↓
PrioritizationEngine (new)
  ├─ Read live system state
  │    ├─ ~/.studios/enkii_resources.json (ENKI-II VRAM, GPU util, RAM)
  │    ├─ StudiOS API /api/tasks (queued tasks)
  │    ├─ ComfyUI API :8188 (loaded models/LoRAs)
  │    ├─ LM Studio API :1234 (ENKI-II loaded models)
  │    └─ MacBook system state (battery, thermal)
  ├─ Score each candidate worker across 6 dimensions
  │    ├─ hardware_score     (0=perfect fit, 1=incompatible)
  │    ├─ load_score         (0=idle, 1=saturated)
  │    ├─ energy_score       (battery/thermal gates)
  │    ├─ locality_score      (0=local, >0=transfer penalty)
  │    ├─ skill_score        (task kind → required skills)
  │    └─ cost_score         (credit estimate)
  └─ Rank + select best worker
        ↓
Dispatch to primary worker (or fallback if primary fails)
        ↓
Receipt stored → engine ready for next request
```

---

## Deliverables

### 1. `skill_registry.py` — Skill-to-Worker Mapper
**What:** Maps `task_kind` → required skill tags → which workers have those skills loaded.

```python
class SkillRegistry:
    def get_required_skills(self, task_kind: str) -> list[str]: ...
    def worker_has_skill(self, worker_id: str, skill_tag: str) -> bool: ...
    def skill_score(self, job: JobRequest, worker: WorkerCapability) -> float: ...
```

**Skill tag taxonomy (Phase 1):**
| task_kind | required skills |
|---|---|
| `comfyui-workflow` | `comfyui`, `mps` or `cuda` |
| `cuda-inference` | `cuda`, `torch` |
| `video-render` | `nvenc`, `ffmpeg` |
| `training` | `cuda`, `torch`, `training` |
| `benchmark` | `benchmark-runner` |
| `document-draft` | `writing`, `notes` |
| `code-review` | `code-analysis` |

**Sources of truth per worker:**
- MacBook: reads `$HERMES_HOME/skills/` directory + `config.yaml` model settings
- ENKI-II: reads LM Studio model list from `http://<enkii-tailnet-ip>:1234/v1/models`
- SOLARIS: read from local config when repaired

---

### 2. `energy_policy.py` — Hard Energy/GPU Gates
**What:** Encodes the hard gates from the Phase doc. Does not score — hard-blocks or allows.

```python
class EnergyPolicy:
    def evaluate(self, job: JobRequest, worker: WorkerCapability, 
                system_state: dict) -> EnergyResult:
        # EnergyResult: {allowed: bool, reason: str, score: float}
```

**Hard gates (Phase 1):**
| Condition | Action |
|---|---|
| MacBook on battery < 20% | Block new GPU/accelerator jobs |
| MacBook battery < 40% | Block new accelerator jobs, pause opportunistic |
| MacBook thermal serious/critical | Drain worker, deny new work |
| ENKI-II GPU util > 90% | Mark GPU slot full |
| ENKI-II VRAM free < job.min_vram_gb | Hard block |
| SOLARIS GPU temperature > 75°C | Hard block (thermal throttle risk) |
| SOLARIS Hermes broken | Hard block all work |

**Soft scoring (for ranking among eligible):**
- AC power → score 0.0; battery → score depends on %
- Thermal nominal → 0.0; fair → 0.3; serious → 0.7; critical → 1.0

---

### 3. `prioritization_engine.py` — Main Scoring + Routing Engine
**What:** Integrates skill_registry + energy_policy + live state into a scored routing decision.

```python
class PrioritizationEngine:
    def __init__(self, policy_weights: dict = DEFAULT_WEIGHTS): ...
    
    def score_worker(self, job: JobRequest, worker: WorkerCapability,
                     system_state: dict) -> DispatchScore: ...
    
    def rank_workers(self, job: JobRequest, 
                     workers: list[WorkerCapability]) -> list[DispatchScore]: ...
    
    def dispatch(self, job: JobRequest) -> DispatchDecision:
        # Returns primary + fallback + block_reason
```

**Default policy weights:**
```python
DEFAULT_WEIGHTS = {
    "hardware_score":  0.35,
    "load_score":      0.20,
    "energy_score":    0.15,
    "locality_score":  0.10,
    "skill_score":    0.10,
    "cost_score":      0.10,
}
```

**DispatchDecision shape:**
```python
@dataclass
class DispatchDecision:
    primary_worker_id: str | None
    fallback_worker_id: str | None
    block_reason: str  # "" if dispatched, else e.g. "NO_QUALIFIED_WORKER"
    scores: dict[str, DispatchScore]  # per-worker breakdown
    estimated_runtime_seconds: float
    estimated_cost_credits: float
    energy_class: str  # immediate / efficient / opportunistic
```

---

### 4. `test_prioritization_engine.py` — Test Suite
**Coverage:**
- ComfyUI job → MacBook preferred (MPS, local ComfyUI running)
- NVENC encode → ENKI-II preferred (CUDA/NVENC available)
- Low-battery MacBook → GPU jobs blocked, CPU jobs still allowed
- SOLARIS thermal throttle → blocked for GPU work
- High-confidence single winner → no ambiguity returned
- Conflicting scores → top 2 within ε → clarification recommended
- ENKI-II offline → falls back to MacBook
- Unknown task_kind → block with "UNKNOWN_TASK_KIND"

---

## Integration Points

### With intent.py
`IntentSynthesis` output → `PrioritizationEngine.dispatch()`:
```python
def dispatch_from_intent(synthesis: IntentSynthesis) -> DispatchDecision:
    job = JobRequest(
        job_id=generate_id(),
        task_kind=synthesis.inferred_task_kind,  # derived from objective
        privacy_class=synthesis.privacy_class,
        execution_mode=synthesis.execution_mode,
    )
    return engine.dispatch(job)
```

### With existing scheduler.py
`PrioritizationEngine` replaces the static qualification filtering in `route_job()` with dynamic scoring:
- `refresh_remote_workers()` continues to poll ENKI-II
- `route_job()` becomes a thin wrapper: calls engine.rank_workers(), returns top

### With StudiOS API
New endpoint: `POST /api/prioritize` — accepts a raw job description, returns a dispatch decision without executing. Allows any agent on any machine to ask "where should this run?"

### With kanban
studi-worker polls `GET /api/tasks?status=waiting` every minute. For each waiting task, calls `engine.dispatch()` and auto-claims if MacBook is the winner.

---

## Ollama Setup for MacBook (Background Work)

**Purpose:** Zero-cost local inference for background agentic work on MacBook's energy-efficient silicon.

```bash
# Install
brew install ollama

# Pull a fast instruction model (3B parameters, ~2GB)
ollama pull llama3.2:3b

# Or a better reasoning model
ollama pull qwen2.5:3b

# Start server (runs on 127.0.0.1:11434)
ollama serve

# Test
curl http://127.0.0.1:11434/v1/models
```

**Hermes configuration:**
```bash
hermes config set providers.ollama.url http://127.0.0.1:11434
hermes config set providers.ollama.model llama3.2:3b
# Set as fallback for non-sensitive background work
hermes config set fallback_providers '{"provider":"ollama","model":"llama3.2:3b"}'
```

**When to use Ollama vs MiniMax M2.7:**
| Workload | Ollama (local) | MiniMax M2.7 |
|---|---|---|
| File renaming, simple transforms | ✅ | ✅ |
| Document drafting, notes | | ✅ |
| Code refactor, simple | | ✅ |
| Architecture / security review | | GPT-5.6-sol Codex |
| Private RAG on local docs | ✅ | |
| Background overnight batch | ✅ | |

---

## Anthropic Integration

**Current state:** No Anthropic API key configured. GPT-5.6-sol Codex is the current cloud reasoning provider.

**Two options:**

### Option A: Anthropic API Key
```bash
hermes auth add anthropic
# Enter API key when prompted
```
- Enables Claude Sonnet 4 as a cloud reasoning fallback
- Useful for: complex architecture decisions, long-context document review, security audits
- Cost: pay-per-token (~$3-5/month for typical usage)

### Option B: Local Claude via LM Studio
- ENKI-II has `claude` in its process list — a local Claude-compatible server may already be running
- Check: `curl http://<enkii-tailnet-ip>:8080/v1/models` (common LM Studio / LocalAI port)
- If available, configure as a custom provider in Hermes with no API cost

**Recommendation:** Add the Anthropic API key. The cost is low and Claude excels at complex reasoning tasks where Codex is rate-limited. Use it as a targeted escalation for difficult architectural decisions, not as a general fallback.

---

## Phase Roadmap

### Phase A: Core Engine (this sprint)
- [x] Plan documented
- [ ] `skill_registry.py` — skill tag → worker mapping
- [ ] `energy_policy.py` — hard gates + soft scoring
- [ ] `prioritization_engine.py` — main scoring loop
- [ ] `test_prioritization_engine.py` — 100% synthetic pass
- [ ] Live dispatch verification (1 real job routed)

### Phase B: Intent Integration
- [ ] `IntentAwareRouter` — `IntentSynthesis` → `JobRequest` conversion
- [ ] Hook into `studi-worker` cron loop for automatic polling

### Phase C: Cross-Machine Adoption
- [ ] ENKI-II Wanda uses prioritization engine to decide if task should stay local or dispatch to MacBook
- [ ] SOLARIS re-enrollment after repair + thermal fix
- [ ] `POST /api/prioritize` endpoint in StudiOS API

### Phase D: Ollama + Local Model Layer
- [ ] Ollama installed on MacBook
- [ ] Hermes configured with `ollama` as zero-cost fallback provider
- [ ] Benchmark: Ollama 3B vs MiniMax M2.7 on document tasks

---

## Open Questions

1. **SOLARIS Hermes repair** — Python trampoline spawn error. Needs investigation before SOLARIS can participate.
2. **ComfyUI on ENKI-II** — Wanda has no ComfyUI. RTX 4090 sits idle for diffusion. Pinokio install is the fastest path.
3. **Anthropic API key** — Does Thomas want to add this? It enables Claude as a reasoning-tier fallback.
4. **RTX 6000 Ada thermal** — SOLARIS at 78°C. Is this a fan/cooling issue or ambient?
5. **Intent taxonomy** — `IntentSynthesis.inferred_task_kind` derivation rules not yet defined. Needs a mapping table.

---

## File Inventory (this sprint)

```
src/studios_coordination/
  skill_registry.py          [NEW] — task_kind → skills → worker capability
  energy_policy.py          [NEW] — hard gates + energy soft scoring
  prioritization_engine.py   [NEW] — main dispatcher
  scheduler.py              [MODIFY] — integrate engine, extend weights
  adapters.py               [UNCHANGED]
  contracts.py              [UNCHANGED]
  intent.py                 [UNCHANGED — Phase B hook]

tests/
  test_prioritization_engine.py  [NEW] — full synthetic test suite

docs/coordination/
  prioritization-engine-development-plan.md  [THIS FILE]
```
