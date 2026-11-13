#!/usr/bin/env python3
"""StudiOS Kanban poller — auto-claims queued StudiOS tasks.

Run as a launchd agent (per memory rule):
    com.studios.kanban-poller.plist in ~/Library/LaunchAgents/

Polls the StudiOS API for waiting tasks, scores each against live worker
state using the prioritization engine, and claims eligible tasks by
POSTing a status update.

No live session needed — runs autonomously in the background.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

# Add src to path for imports
SRC = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(SRC))

from studios_coordination.prioritization_engine import (
    PrioritizationEngine,
)
from studios_coordination.host_identity import LOCAL_WORKER_ID
from studios_coordination.scheduler import (
    JobRequest,
    WorkerCapability,
    refresh_remote_workers,
)

log = logging.getLogger("kanban-poller")

# ── Config ────────────────────────────────────────────────────────────────────

STUDIOS_API = os.environ.get(
    "STUDIOS_API", "http://127.0.0.1:8765"
)
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "60"))
KANBAN_BOARD = os.environ.get("KANBAN_BOARD", "studi")
DRY_RUN = os.environ.get("DRY_RUN", "1") == "1"


# ── Helpers ───────────────────────────────────────────────────────────────────

def api_get(path: str) -> dict | None:
    url = f"{STUDIOS_API}{path}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:
        log.warning("API GET %s failed: %s", url, exc)
        return None


def api_post(path: str, body: dict) -> bool:
    url = f"{STUDIOS_API}{path}"
    try:
        data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status in (200, 201)
    except Exception as exc:
        log.warning("API POST %s failed: %s", url, exc)
        return False


# ── Task kind detection ────────────────────────────────────────────────────────

def task_kind_from_task(task: dict) -> str:
    """Infer a dispatchable task_kind from a kanban task dict."""
    title = task.get("title", "").lower()
    goal = task.get("goal", "").lower()
    category = task.get("category", "").lower()
    text = f"{title} {goal} {category}"

    mapping = {
        "comfyui": "comfyui-workflow",
        "image gen": "comfyui-workflow",
        "render": "video-render",
        "video": "video-render",
        "nvenc": "video-render",
        "cuda": "cuda-inference",
        "llm": "cuda-inference",
        "inference": "cuda-inference",
        "training": "training",
        "benchmark": "benchmark",
        "document": "document-draft",
        "note": "document-draft",
        "code": "code-generation",
        "research": "research",
    }

    for kw, kind in mapping.items():
        if kw in text:
            return kind
    return "cuda-inference"


# ── Worker gathering ──────────────────────────────────────────────────────────

def get_fleet() -> list[WorkerCapability]:
    """Build the current fleet list for dispatch."""
    workers = [
        WorkerCapability(
            worker_id=LOCAL_WORKER_ID,
            task_kinds=(
                "comfyui-workflow", "cuda-inference", "document-draft",
                "code-generation", "research",
            ),
            backends=("mps", "cpu", "ollama"),
            vram_gb=0,
            system_memory_gb=128,
            max_parallel=4,
            active_jobs=0,
            local=True,
            online=True,
        )
    ]
    try:
        workers.extend(refresh_remote_workers())
    except Exception as exc:
        log.debug("ENKI-II refresh failed (will retry next poll): %s", exc)
    return workers


# ── Core loop ─────────────────────────────────────────────────────────────────

def process_task(task: dict, workers: list[WorkerCapability], engine: PrioritizationEngine) -> bool:
    """Score a task and claim it if a qualified worker is available."""
    task_id = task.get("id") or task.get("task_id")
    if not task_id:
        return False

    task_kind = task_kind_from_task(task)
    priority = task.get("priority", 3)

    job = JobRequest(
        job_id=task_id,
        task_kind=task_kind,
        privacy_class="private",
        execution_mode="batch",
        required_backends=(),
        min_vram_gb=0.0,
    )

    decision = engine.dispatch(job=job, workers=workers)

    if decision.block_reason:
        log.info(
            "  [%s] task=%s kind=%s → BLOCKED (%s)",
            task_id, task.get("title", "")[:40], task_kind, decision.block_reason,
        )
        return False

    target = decision.primary_worker_id or "unknown"

    if DRY_RUN:
        log.info(
            "  [DRY-RUN] [%s] task=%s kind=%s → %s (score=%.3f, fallback=%s, ambiguity=%s)",
            task_id,
            task.get("title", "")[:40],
            task_kind,
            target,
            decision.scores[target].total_score if target in decision.scores else 0,
            decision.fallback_worker_id,
            decision.ambiguity_detected,
        )
        return True

    # Claim: update task status to "active" on the kanban board
    claimed = api_post(
        f"/api/tasks/{task_id}/claim",
        {
            "status": "active",
            "worker_id": target,
            "claimed_at": datetime.now(timezone.utc).isoformat(),
            "dispatch_decision": {
                "primary": target,
                "fallback": decision.fallback_worker_id,
                "ambiguity": decision.ambiguity_detected,
                "scores": {
                    wid: round(s.total_score, 4)
                    for wid, s in decision.scores.items()
                },
            },
        },
    )

    if claimed:
        log.info(
            "  [%s] task=%s kind=%s → claimed by %s",
            task_id, task.get("title", "")[:40], task_kind, target,
        )
    else:
        log.warning(
            "  [%s] scored %s but claim POST failed — will retry next poll",
            task_id, target,
        )

    return claimed


def poll_once() -> int:
    """Poll for waiting tasks and claim eligible ones. Returns tasks processed."""
    log.debug("Polling StudiOS API for waiting tasks on board=%s", KANBAN_BOARD)

    data = api_get(f"/api/tasks?status=waiting&board={KANBAN_BOARD}&limit=20")
    if data is None:
        return 0

    tasks = data.get("tasks", []) or data.get("result", [])
    if not tasks:
        log.debug("No waiting tasks found.")
        return 0

    log.info("Found %d waiting task(s)", len(tasks))
    workers = get_fleet()
    engine = PrioritizationEngine()

    claimed = 0
    for task in tasks:
        try:
            if process_task(task, workers, engine):
                claimed += 1
        except Exception as exc:
            log.error("Error processing task %s: %s", task.get("id"), exc)

    log.info("Poll complete — %d/%d tasks claimed", claimed, len(tasks))
    return claimed


# ── Main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s kanban-poller %(levelname)s %(message)s",
    )
    log.info("StudiOS Kanban poller starting — dry_run=%s", DRY_RUN)
    log.info("Target: %s/api/tasks", STUDIOS_API)

    consecutive_errors = 0
    max_consecutive_errors = 5

    while True:
        try:
            poll_once()
            consecutive_errors = 0
        except Exception as exc:
            consecutive_errors += 1
            log.error("Poll error (consecutive=%d): %s", consecutive_errors, exc)
            if consecutive_errors >= max_consecutive_errors:
                log.critical("Max consecutive errors reached — exiting")
                sys.exit(1)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
