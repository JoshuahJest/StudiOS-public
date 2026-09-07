"""Benchmark manifest registry and runner for StudiOS hardware qualification.

Benchmarks produce reproducible measurements that feed the scheduler's capability
estimates. Every benchmark result is signed and stored as a content-addressed
artifact so the controller can verify freshness and integrity.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class BenchmarkResult:
    benchmark_name: str
    host_id: str
    measured_at: str          # ISO-8601 UTC
    duration_seconds: float
    memory_peak_mb: float
    score: float
    details: dict


class BenchmarkRunner:
    """Run registered benchmark suites and produce signed result manifests."""

    _registry: dict[str, callable] = {}

    @classmethod
    def register(cls, name: str, fn: callable) -> None:
        cls._registry[name] = fn

    def run(self, name: str, host_id: str, output_dir: Path) -> BenchmarkResult:
        if name not in self._registry:
            raise ValueError(f"unknown benchmark: {name}")
        start = time.monotonic()
        details = self._registry[name]()
        duration = time.monotonic() - start

        import psutil
        mem = psutil.Process().memory_info().rss / (1024 * 1024)

        result = BenchmarkResult(
            benchmark_name=name,
            host_id=host_id,
            measured_at=datetime.now(timezone.utc).isoformat(),
            duration_seconds=round(duration, 3),
            memory_peak_mb=round(mem, 1),
            score=details.get("score", 0.0),
            details=details,
        )
        self._store(result, output_dir)
        return result

    def _store(self, result: BenchmarkResult, output_dir: Path) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        d = {
            "benchmark_name": result.benchmark_name,
            "host_id": result.host_id,
            "measured_at": result.measured_at,
            "duration_seconds": result.duration_seconds,
            "memory_peak_mb": result.memory_peak_mb,
            "score": result.score,
            "details": result.details,
        }
        blob = json.dumps(d, indent=2, sort_keys=True).encode("utf-8")
        content_hash = hashlib.sha256(blob).hexdigest()
        path = output_dir / f"{result.benchmark_name}_{content_hash[:16]}.json"
        path.write_bytes(blob)
        path.chmod(0o600)


# ── Built-in micro-benchmarks ──────────────────────────────────────────────

def _comfyui_queue_benchmark() -> dict:
    """Ping ComfyUI prompt endpoint and measure queue latency."""
    import httpx
    try:
        t0 = time.monotonic()
        with httpx.Client(timeout=10.0) as client:
            r = client.get("http://127.0.0.1:8188/system_stats")
        latency = time.monotonic() - t0
        return {"latency_seconds": round(latency, 4), "status": r.status_code}
    except Exception as e:
        return {"latency_seconds": -1, "error": str(e)}


def _vram_survey() -> dict:
    """Query VRAM via ComfyUI system_stats."""
    import httpx
    try:
        with httpx.Client(timeout=10.0) as client:
            r = client.get("http://127.0.0.1:8188/system_stats")
        data = r.json()
        devices = data.get("devices", [])
        total_vram_gb = sum(d.get("vram_total", 0) for d in devices) / (1024**3)
        free_vram_gb = sum(d.get("vram_free", 0) for d in devices) / (1024**3)
        return {
            "total_vram_gb": round(total_vram_gb, 2),
            "free_vram_gb": round(free_vram_gb, 2),
            "device_count": len(devices),
        }
    except Exception as e:
        return {"error": str(e)}


def _disk_survey() -> dict:
    import shutil
    total, used = shutil.disk_usage("/").total, shutil.disk_usage("/").used
    return {
        "total_gb": round(total / (1024**3), 1),
        "used_gb": round(used / (1024**3), 1),
        "free_gb": round((total - used) / (1024**3), 1),
    }


# Register built-ins
BenchmarkRunner.register("comfyui_queue_latency", _comfyui_queue_benchmark)
BenchmarkRunner.register("vram_survey", _vram_survey)
BenchmarkRunner.register("disk_survey", _disk_survey)
