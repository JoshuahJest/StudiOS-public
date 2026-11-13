"""CLI entrypoint for the StudiOS MacBook qualification benchmark.

Run as a module so the package root stays importable:

    PYTHONPATH=src python3 -m studios_coordination.benchmark_cli

Writes a JSON qualification record to ``STUDIOS_BENCH_OUT`` (default:
``docs/benchmarks/<host>.json``) and prints a one-line summary.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from .benchmark import BenchmarkRunner

DEFAULT_OUT = Path(__file__).resolve().parent.parent.parent / "docs" / "benchmarks"

BENCHMARKS = ("vram_survey", "comfyui_queue_latency", "disk_survey")


def _sysinfo() -> dict:
    try:
        import psutil

        cpu_cores = psutil.cpu_count()
        mem_total = round(psutil.virtual_memory().total / 1024**3, 1)
        mem_free = round(psutil.virtual_memory().available / 1024**3, 1)
    except Exception:
        cpu_cores = os.cpu_count()
        mem_total = None
        mem_free = None

    disk = shutil.disk_usage("/")
    return {
        "cpu_cores": cpu_cores,
        "mem_total_gb": mem_total,
        "mem_free_gb": mem_free,
        "disk_free_gb": round(disk.free / 1024**3, 1),
    }


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    host_id = os.environ.get("STUDIOS_HOST_ID", "macbook-m5max")

    out_env = os.environ.get("STUDIOS_BENCH_OUT")
    out_path = Path(out_env) if out_env else DEFAULT_OUT / f"{host_id}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    work_dir = Path(os.environ.get("STUDIOS_BENCH_DIR", "/tmp/studios_benchmarks"))
    work_dir.mkdir(parents=True, exist_ok=True)

    runner = BenchmarkRunner()
    results: dict = {}
    for name in BENCHMARKS:
        try:
            r = runner.run(name, host_id, work_dir)
            results[name] = {
                "score": r.score,
                "duration_s": r.duration_seconds,
                "details": r.details,
                "measured_at": r.measured_at,
            }
        except Exception as exc:  # a failed probe must not abort the suite
            results[name] = {"error": f"{type(exc).__name__}: {exc}"}

    results["sysinfo"] = _sysinfo()
    results["qualified_at"] = datetime.now(timezone.utc).isoformat()
    results["host_id"] = host_id

    with open(out_path, "w") as fh:
        json.dump(results, fh, indent=2)

    print(f"qualification complete: {results['qualified_at']} -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
