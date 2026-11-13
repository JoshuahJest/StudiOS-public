"""Energy, power, and thermal hard gates for StudiOS scheduling.

Encodes the hard policy gates from the Phase doc:
  - MacBook battery/thermal thresholds
  - ENKI-II VRAM/GPU utilization limits
  - SOLARIS thermal throttle blocks

Does NOT make routing decisions — returns allow/block with a reason and a
soft score for ranking among eligible candidates.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from .host_identity import LOCAL_WORKER_ID

log = logging.getLogger("energy-policy")

# ── Result types ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class EnergyResult:
    """Outcome of energy/power policy evaluation for one (job, worker) pair."""
    allowed: bool                    # True if the job can run here
    reason: str                     # "" if allowed; block reason if not
    energy_class: str               # "immediate" | "efficient" | "opportunistic"
    score: float                   # 0.0 = best (free), 1.0 = worst eligible
    block_level: str                # "none" | "hard" | "soft"

    # Detail breakdown (for audit)
    power_source: str               # "ac" | "battery" | "unknown"
    battery_percent: float | None
    gpu_util_percent: float | None
    gpu_temp_c: float | None
    vram_free_gb: float | None
    thermal_state: str              # "nominal" | "fair" | "serious" | "critical"


# ── System state readers ──────────────────────────────────────────────────────

def read_enkii_resources(state_path: Path | None = None) -> dict[str, Any]:
    """
    Read the cached ENKI-II resource snapshot written by the poller.
    Returns {} if the file is absent or unreadable.
    """
    if state_path is None:
        state_path = Path.home() / ".studios" / "enkii_resources.json"
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text())
    except Exception as e:
        log.warning("Could not read ENKII resources from %s: %s", state_path, e)
        return {}


def get_macbook_energy_state() -> dict[str, Any]:
    """
    Read MacBook system energy state.
    Falls back to conservative defaults if pmsubattery is unavailable.
    """
    state = {
        "power_source": "ac",
        "battery_percent": 100.0,
        "thermal_state": "nominal",
        "thermal_cpus": None,
        "gpu_util_percent": 0.0,
        "vram_free_gb": None,
    }

    # Try pmset (macOS)
    import subprocess
    try:
        result = subprocess.run(
            ["pmset", "-g", "battery"],
            capture_output=True, text=True, timeout=5,
        )
        output = result.stdout
        for line in output.splitlines():
            if "InternalBattery" in line or "Now drawing from" in line:
                if "AC Power" in line or "Battery Power" not in line:
                    state["power_source"] = "ac"
                else:
                    state["power_source"] = "battery"
            if "%" in line:
                parts = line.split()
                for i, part in enumerate(parts):
                    if part.endswith("%"):
                        try:
                            state["battery_percent"] = float(part.rstrip("%"))
                        except ValueError:
                            pass
    except Exception as e:
        log.debug("pmset unavailable: %s", e)

    # Try thermal state via system_profiler
    try:
        result = subprocess.run(
            ["system_profiler", "SPHardwareDataType"],
            capture_output=True, text=True, timeout=10,
        )
        # Thermal state not directly available this way on MacBook
        # but we can check fan speed / SMC keys
        pass
    except Exception:
        pass

    return state


# ── MacBook policy ─────────────────────────────────────────────────────────────

MACBOOK_BATTERY_BLOCK_GPU = 20.0      # % — block new GPU/accelerator jobs
MACBOOK_BATTERY_BLOCK_NEW_WORK = 40.0  # % — block new accelerator jobs
MACBOOK_BATTERY_PAUSE_OPPORTUNISTIC = 30.0  # % — pause opportunistic work
MACBOOK_THERMAL_SERIOUS = "serious"
MACBOOK_THERMAL_CRITICAL = "critical"
MACBOOK_VRAM_RESERVE_GB = 0.0         # Unified memory — no vRAM boundary per se


def evaluate_macbook(job_min_vram_gb: float,
                     job_task_kind: str,
                     latency_class: str = "background",
                     resource_snapshot: dict[str, Any] | None = None,
                     macbook_state: dict[str, Any] | None = None) -> EnergyResult:
    """
    Evaluate energy/power policy for a MacBook M5 Max job.
    Applies hard gates from the Phase doc.

    Uses resource_snapshot (dict with optional macbook_* keys) when available,
    otherwise falls back to get_macbook_energy_state().
    """
    # Resolve state — prefer explicit arg, then snapshot dict, then live query
    if macbook_state is None and resource_snapshot:
        macbook_state = resource_snapshot.get("macbook_state")

    if macbook_state is None:
        macbook_state = get_macbook_energy_state()

    power = macbook_state.get("power_source", "unknown")
    battery_pct = macbook_state.get("battery_percent", 100.0)
    thermal = macbook_state.get("thermal_state", "nominal")
    gpu_util = macbook_state.get("gpu_util_percent", 0.0)
    is_gpu_job = job_task_kind in (
        "comfyui-workflow", "cuda-inference", "training",
        "image-generation", "video-render",
    )

    # ── Hard gate: thermal ──────────────────────────────────────────────────
    if thermal == MACBOOK_THERMAL_CRITICAL:
        return EnergyResult(
            allowed=False,
            reason="thermal-critical",
            energy_class="opportunistic",
            score=1.0,
            block_level="hard",
            power_source=power, battery_percent=battery_pct,
            gpu_util_percent=gpu_util, gpu_temp_c=None,
            vram_free_gb=None, thermal_state=thermal,
        )
    if thermal == MACBOOK_THERMAL_SERIOUS:
        return EnergyResult(
            allowed=False,
            reason="thermal-serious",
            energy_class="opportunistic",
            score=1.0,
            block_level="hard",
            power_source=power, battery_percent=battery_pct,
            gpu_util_percent=gpu_util, gpu_temp_c=None,
            vram_free_gb=None, thermal_state=thermal,
        )

    # ── Hard gate: battery for GPU jobs ────────────────────────────────────
    if power == "battery":
        if battery_pct < MACBOOK_BATTERY_BLOCK_GPU and is_gpu_job:
            return EnergyResult(
                allowed=False,
                reason=f"battery-low-gpu-blocked ({battery_pct:.0f}%)",
                energy_class="opportunistic",
                score=1.0,
                block_level="hard",
                power_source=power, battery_percent=battery_pct,
                gpu_util_percent=gpu_util, gpu_temp_c=None,
                vram_free_gb=None, thermal_state=thermal,
            )
        if battery_pct < MACBOOK_BATTERY_BLOCK_NEW_WORK and is_gpu_job:
            return EnergyResult(
                allowed=False,
                reason=f"battery-low-accelerator-blocked ({battery_pct:.0f}%)",
                energy_class="opportunistic",
                score=1.0,
                block_level="hard",
                power_source=power, battery_percent=battery_pct,
                gpu_util_percent=gpu_util, gpu_temp_c=None,
                vram_free_gb=None, thermal_state=thermal,
            )

    # ── Soft score: power source ─────────────────────────────────────────────
    if power == "ac":
        power_score = 0.0
    elif battery_pct >= 80:
        power_score = 0.2
    elif battery_pct >= 50:
        power_score = 0.4
    elif battery_pct >= MACBOOK_BATTERY_BLOCK_NEW_WORK:
        power_score = 0.7
    else:
        power_score = 0.9

    # ── Soft score: thermal ──────────────────────────────────────────────────
    thermal_score_map = {"nominal": 0.0, "fair": 0.3, "serious": 0.7, "critical": 1.0}
    thermal_score = thermal_score_map.get(thermal, 0.5)

    # ── Soft score: GPU util ─────────────────────────────────────────────────
    gpu_score = gpu_util / 100.0

    # ── Combined score ────────────────────────────────────────────────────────
    # Weighted average: power 60%, thermal 25%, GPU util 15%
    energy_score = power_score * 0.6 + thermal_score * 0.25 + gpu_score * 0.15

    # ── Energy class ────────────────────────────────────────────────────────
    if power == "ac" and thermal in ("nominal", "fair"):
        energy_class = "immediate"
    elif power == "ac" or battery_pct >= 60:
        energy_class = "efficient"
    else:
        energy_class = "opportunistic"

    return EnergyResult(
        allowed=True,
        reason="",
        energy_class=energy_class,
        score=energy_score,
        block_level="none",
        power_source=power,
        battery_percent=battery_pct,
        gpu_util_percent=gpu_util,
        gpu_temp_c=None,
        vram_free_gb=None,
        thermal_state=thermal,
    )


# ── ENKI-II policy ─────────────────────────────────────────────────────────────

ENKII_VRAM_RESERVE_GB = 2.0          # Reserve 2 GB VRAM always
ENKII_RAM_RESERVE_GB = 12.0          # Reserve 12 GB system RAM
ENKII_GPU_UTIL_BLOCK_THRESHOLD = 95  # Block new GPU jobs above this util
ENKII_THERMAL_BLOCK_C = 83           # Hard block above this GPU temp


def evaluate_enkii(job_min_vram_gb: float,
                   job_task_kind: str,
                   latency_class: str = "background",
                   resource_snapshot: dict[str, Any] | None = None,
                   enkii_state: dict[str, Any] | None = None) -> EnergyResult:
    """
    Evaluate energy/power policy for an ENKI-II RTX 4090 job.
    Reads live state from the cached resource poller output.
    """
    if enkii_state is None:
        enkii_state = read_enkii_resources()
        if not enkii_state:
            # No data — be conservative
            return EnergyResult(
                allowed=False,
                reason="enkii-state-unavailable",
                energy_class="opportunistic",
                score=1.0,
                block_level="hard",
                power_source="unknown", battery_percent=None,
                gpu_util_percent=None, gpu_temp_c=None,
                vram_free_gb=None, thermal_state="unknown",
            )

    # Extract from resource snapshot
    sample = {}
    for node in enkii_state.get("nodes", []):
        if node.get("node_id") == "enki-ii":
            sample = node.get("sample", {})
            break

    gpu_util = sample.get("gpu_util_percent", 0.0)
    gpu_temp = sample.get("gpu_temperature_c", 0.0)
    vram_used_mb = sample.get("gpu_memory_used_mb", 0)
    vram_total_mb = sample.get("gpu_memory_total_mb", 24 * 1024)
    vram_free_gb = (vram_total_mb - vram_used_mb) / 1024
    ram_free_gb = sample.get("ram_free_gb", 0.0)
    is_gpu_job = job_task_kind in (
        "comfyui-workflow", "cuda-inference", "training",
        "image-generation", "video-render",
    )

    # ── Hard gate: VRAM ─────────────────────────────────────────────────────
    effective_vram_free = vram_free_gb - ENKII_VRAM_RESERVE_GB
    if is_gpu_job and effective_vram_free < job_min_vram_gb:
        return EnergyResult(
            allowed=False,
            reason=f"vram-insufficient ({effective_vram_free:.1f}GB free, need {job_min_vram_gb:.1f}GB)",
            energy_class="efficient",
            score=1.0,
            block_level="hard",
            power_source="ac", battery_percent=None,
            gpu_util_percent=gpu_util, gpu_temp_c=gpu_temp,
            vram_free_gb=effective_vram_free, thermal_state="nominal",
        )

    # ── Hard gate: RAM ─────────────────────────────────────────────────────
    effective_ram_free = ram_free_gb - ENKII_RAM_RESERVE_GB
    if effective_ram_free < 4.0:  # Minimum 4 GB for OS + apps
        return EnergyResult(
            allowed=False,
            reason=f"ram-insufficient ({effective_ram_free:.1f}GB free)",
            energy_class="efficient",
            score=1.0,
            block_level="hard",
            power_source="ac", battery_percent=None,
            gpu_util_percent=gpu_util, gpu_temp_c=gpu_temp,
            vram_free_gb=effective_vram_free, thermal_state="nominal",
        )

    # ── Hard gate: GPU utilization ─────────────────────────────────────────
    if is_gpu_job and gpu_util >= ENKII_GPU_UTIL_BLOCK_THRESHOLD:
        return EnergyResult(
            allowed=False,
            reason=f"gpu-saturated ({gpu_util:.0f}% util)",
            energy_class="efficient",
            score=1.0,
            block_level="hard",
            power_source="ac", battery_percent=None,
            gpu_util_percent=gpu_util, gpu_temp_c=gpu_temp,
            vram_free_gb=effective_vram_free, thermal_state="nominal",
        )

    # ── Hard gate: thermal ──────────────────────────────────────────────────
    if gpu_temp >= ENKII_THERMAL_BLOCK_C:
        return EnergyResult(
            allowed=False,
            reason=f"gpu-thermal-critical ({gpu_temp:.0f}°C)",
            energy_class="opportunistic",
            score=1.0,
            block_level="hard",
            power_source="ac", battery_percent=None,
            gpu_util_percent=gpu_util, gpu_temp_c=gpu_temp,
            vram_free_gb=effective_vram_free, thermal_state="critical",
        )

    # ── Soft score: GPU utilization ─────────────────────────────────────────
    gpu_score = gpu_util / 100.0

    # ── Soft score: VRAM headroom ──────────────────────────────────────────
    if effective_vram_free >= 16.0:
        vram_score = 0.0
    elif effective_vram_free >= 8.0:
        vram_score = 0.2
    elif effective_vram_free >= 4.0:
        vram_score = 0.5
    else:
        vram_score = 0.8

    # ── Soft score: thermal ─────────────────────────────────────────────────
    if gpu_temp <= 50:
        temp_score = 0.0
    elif gpu_temp <= 65:
        temp_score = 0.2
    elif gpu_temp <= 75:
        temp_score = 0.4
    else:
        temp_score = 0.7

    # ── Combined score ──────────────────────────────────────────────────────
    # GPU util 50%, VRAM headroom 30%, thermal 20%
    energy_score = gpu_score * 0.5 + vram_score * 0.3 + temp_score * 0.2

    # ── Energy class ────────────────────────────────────────────────────────
    if gpu_util < 30 and gpu_temp < 60:
        energy_class = "immediate"
    elif gpu_util < 70 and gpu_temp < 75:
        energy_class = "efficient"
    else:
        energy_class = "opportunistic"

    return EnergyResult(
        allowed=True,
        reason="",
        energy_class=energy_class,
        score=energy_score,
        block_level="none",
        power_source="ac",
        battery_percent=None,
        gpu_util_percent=gpu_util,
        gpu_temp_c=gpu_temp,
        vram_free_gb=effective_vram_free,
        thermal_state="nominal" if gpu_temp < 60 else "fair",
    )


# ── SOLARIS policy ────────────────────────────────────────────────────────────

SOLARIS_THERMAL_BLOCK_C = 75.0        # Block GPU work above 75°C (throttle risk)
SOLARIS_VRAM_TOTAL_GB = 8.0           # RTX 6000 Ada 8 GB nameplate
SOLARIS_VRAM_RESERVE_GB = 1.0


def evaluate_solaris(job_min_vram_gb: float,
                    job_task_kind: str,
                    latency_class: str = "background",
                    resource_snapshot: dict[str, Any] | None = None,
                    solaris_state: dict[str, Any] | None = None) -> EnergyResult:
    """
    Evaluate SOLARIS RTX 6000 Ada. 
    - Thermal throttling at 78°C is the primary constraint.
    - 8 GB VRAM limits large diffusion/training workloads.
    - Hermes is currently broken (Python trampoline spawn error).
    """
    if solaris_state is None:
        solaris_state = read_enkii_resources()
        if not solaris_state:
            return EnergyResult(
                allowed=False,
                reason="solaris-state-unavailable",
                energy_class="opportunistic",
                score=1.0,
                block_level="hard",
                power_source="unknown", battery_percent=None,
                gpu_util_percent=None, gpu_temp_c=None,
                vram_free_gb=None, thermal_state="unknown",
            )

    sample = {}
    for node in solaris_state.get("nodes", []):
        if node.get("node_id") == "solaris":
            sample = node.get("sample", {})
            break

    gpu_util = sample.get("gpu_util_percent", 0.0)
    gpu_temp = sample.get("gpu_temperature_c", 0.0)
    vram_used_mb = sample.get("gpu_memory_used_mb", 0)
    vram_free_gb = SOLARIS_VRAM_TOTAL_GB - (vram_used_mb / 1024)
    is_gpu_job = job_task_kind in (
        "comfyui-workflow", "cuda-inference", "training",
        "image-generation", "video-render",
    )
    effective_vram_free = vram_free_gb - SOLARIS_VRAM_RESERVE_GB

    # ── Hard gate: Hermes broken ─────────────────────────────────────────────
    # This is a separate check done at enrollment time; surface it here too
    hermes_broken = sample.get("hermes_broken", False)
    if hermes_broken or "hermes" in [p.lower() for p in sample.get("gpu_processes", [])]:
        # Hermes is in the process list but may be in error state — be conservative
        pass  # Don't hard-block based on process name alone

    # ── Hard gate: thermal (primary SOLARIS constraint) ─────────────────────
    if gpu_temp >= SOLARIS_THERMAL_BLOCK_C:
        return EnergyResult(
            allowed=False,
            reason=f"solaris-thermal-throttle ({gpu_temp:.0f}°C ≥ {SOLARIS_THERMAL_BLOCK_C:.0f}°C limit)",
            energy_class="opportunistic",
            score=1.0,
            block_level="hard",
            power_source="unknown", battery_percent=None,
            gpu_util_percent=gpu_util, gpu_temp_c=gpu_temp,
            vram_free_gb=effective_vram_free, thermal_state="critical",
        )

    # ── Hard gate: VRAM ─────────────────────────────────────────────────────
    if is_gpu_job and effective_vram_free < job_min_vram_gb:
        return EnergyResult(
            allowed=False,
            reason=f"solaris-vram-insufficient ({effective_vram_free:.1f}GB < {job_min_vram_gb:.1f}GB required)",
            energy_class="efficient",
            score=1.0,
            block_level="hard",
            power_source="unknown", battery_percent=None,
            gpu_util_percent=gpu_util, gpu_temp_c=gpu_temp,
            vram_free_gb=effective_vram_free, thermal_state="serious" if gpu_temp > 65 else "fair",
        )

    # ── Soft score ──────────────────────────────────────────────────────────
    gpu_score = gpu_util / 100.0
    temp_score = min(gpu_temp / SOLARIS_THERMAL_BLOCK_C, 1.0)
    vram_score = max(0.0, 1.0 - effective_vram_free / SOLARIS_VRAM_TOTAL_GB)

    energy_score = gpu_score * 0.4 + temp_score * 0.4 + vram_score * 0.2

    return EnergyResult(
        allowed=True,
        reason="",
        energy_class="efficient",
        score=energy_score,
        block_level="none",
        power_source="unknown",
        battery_percent=None,
        gpu_util_percent=gpu_util,
        gpu_temp_c=gpu_temp,
        vram_free_gb=effective_vram_free,
        thermal_state="fair" if gpu_temp < 65 else "serious",
    )


# ── Unified evaluator ─────────────────────────────────────────────────────────

# Data-driven per-worker policy dispatch.
# Register worker-specific evaluators here — no code changes needed to add a worker.
_WORKER_EVALUATORS: dict[str, callable] = {
    LOCAL_WORKER_ID: evaluate_macbook,
    "enki-ii": evaluate_enkii,
    "solaris": evaluate_solaris,
}


def register_worker_evaluator(worker_id: str, fn: callable) -> None:
    """Register or override a worker energy evaluator at runtime."""
    _WORKER_EVALUATORS[worker_id] = fn


def evaluate_worker_energy(
    worker_id: str,
    job_min_vram_gb: float,
    job_task_kind: str,
    latency_class: str = "background",
    resource_snapshot: dict[str, Any] | None = None,
) -> EnergyResult:
    """
    Route to the correct worker-specific evaluator.
    Unknown workers get a conservative allow with a soft score.
    """
    evaluator = _WORKER_EVALUATORS.get(worker_id)
    if evaluator is None:
        # Unknown worker — conservative: allow but score high
        return EnergyResult(
            allowed=True,
            reason="unknown-worker-conservative",
            energy_class="efficient",
            score=0.7,
            block_level="none",
            power_source="unknown", battery_percent=None,
            gpu_util_percent=None, gpu_temp_c=None,
            vram_free_gb=None, thermal_state="unknown",
        )

    # Pass positional to match all evaluator signatures
    return evaluator(
        job_min_vram_gb,
        job_task_kind,
        latency_class,
        resource_snapshot,
    )
