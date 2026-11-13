"""StudiOS worker inbox — receives delegated tasks from ENKI-II over the tailnet.

Run with:
    python3 -m studios_inbox --port 8765 --host 0.0.0.0

Or from the StudiOS-1.1 root:
    cd ~/StudiOS-1.1
    PYTHONPATH=src python3 -m studios_inbox --port 8765 --host 0.0.0.0
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel, Field
from uvicorn import run

log = logging.getLogger("studios_inbox")
app = FastAPI(title="StudiOS Worker Inbox")

# ── Storage ──────────────────────────────────────────────────────────────────

INBOX_DIR = Path.home() / ".studios" / "inbox"
INBOX_DIR.mkdir(parents=True, exist_ok=True)

# NAS mount — /Volumes/Dump is the StudiOS shared storage
NAS_MOUNT = Path("/Volumes/Dump")
assert NAS_MOUNT.exists(), f"NAS mount {NAS_MOUNT} not found"

# Thread pool for slow NAS operations
_nas_executor = ThreadPoolExecutor(max_workers=2)


def load_inbox() -> dict[str, dict]:
    """Load all inbox entries as {task_id: entry}."""
    entries = {}
    for p in INBOX_DIR.glob("*.json"):
        try:
            entries[p.stem] = json.loads(p.read_text())
        except Exception:
            pass
    return entries


def save_entry(task_id: str, entry: dict) -> None:
    path = INBOX_DIR / f"{task_id}.json"
    path.write_text(json.dumps(entry, indent=2))
    path.chmod(0o600)


# ── Pydantic models ─────────────────────────────────────────────────────────

class WorkerInboxPayload(BaseModel):
    task: dict
    body: str = ""
    revision: int = 1


class WorkerInboxResponse(BaseModel):
    received: bool
    task_id: str
    received_at: str


# ── Endpoints ───────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"status": "ok", "service": "studios-inbox"}


@app.get("/api/inbox")
def list_inbox():
    """List all received delegated tasks."""
    return {"inbox": list(load_inbox().values())}


@app.get("/api/inbox/{task_id}")
def get_inbox_entry(task_id: str):
    """Get a specific inbox entry."""
    entries = load_inbox()
    if task_id not in entries:
        raise HTTPException(status_code=404, detail="Task not found in inbox")
    return entries[task_id]


@app.post("/api/worker/inbox", response_model=WorkerInboxResponse)
def worker_inbox(
    request: Request,
    payload: WorkerInboxPayload,
    x_task_id: str | None = Header(default=None),
    x_delegation_secret: str | None = Header(default=None),
):
    """
    Receive a delegated task pushed from ENKI-II.

    ENKI-II POSTs with:
      - X-Task-ID header: the task ID
      - X-Delegation-Secret header: optional secret set at registration
      - body: WorkerInboxPayload JSON
    """
    task_id = x_task_id or payload.task.get("id", "unknown")
    now = datetime.now(timezone.utc).isoformat()

    entry = {
        "task_id": task_id,
        "received_at": now,
        "delegation_secret": x_delegation_secret,
        "task": payload.task,
        "body": payload.body,
        "revision": payload.revision,
        "status": "received",
    }

    save_entry(task_id, entry)
    log.info("Received delegated task %s from ENKI-II", task_id)

    return WorkerInboxResponse(
        received=True,
        task_id=task_id,
        received_at=now,
    )


# ── Dispatch endpoint ──────────────────────────────────────────────────────────

class DispatchRequest(BaseModel):
    task_kind: str = "cuda-inference"
    privacy_class: str = "private"
    execution_mode: str = "batch"
    required_backends: list[str] = []
    min_vram_gb: float = 0.0
    objective: str = ""


class DispatchResponse(BaseModel):
    job_id: str
    dispatched: bool
    primary_worker_id: str | None
    fallback_worker_id: str | None
    block_reason: str
    ambiguity_detected: bool
    scores: dict


@app.post("/api/dispatch", response_model=DispatchResponse)
async def dispatch_task(payload: DispatchRequest):
    """
    Score all available workers for a job and return the dispatch decision.

    Any StudiOS node (or external caller) can POST here to get a routing
    decision based on live hardware state, energy constraints, and skill availability.

    Returns the primary worker, fallback, ambiguity flag, and per-worker scores.
    """
    import uuid
    from studios_coordination.prioritization_engine import (
        PrioritizationEngine,
    )
    from studios_coordination.scheduler import JobRequest as SchedulerJobRequest
    from studios_coordination.scheduler import WorkerCapability, refresh_remote_workers

    job_id = f"disp-{uuid.uuid4().hex[:8]}"

    # Build a scheduler JobRequest
    job = SchedulerJobRequest(
        job_id=job_id,
        task_kind=payload.task_kind,
        privacy_class=payload.privacy_class,
        execution_mode=payload.execution_mode,
        required_backends=tuple(payload.required_backends),
        min_vram_gb=payload.min_vram_gb,
    )

    # Gather workers: MacBook (local) + ENKI-II (remote)
    workers = [
        WorkerCapability(
            worker_id=os.environ.get("STUDIOS_WORKER_ID", "macbook-worker"),
            task_kinds=("comfyui-workflow", "cuda-inference", "document-draft",
                        "code-generation", "research"),
            backends=("mps", "cpu", "ollama"),
            vram_gb=0,           # Unified memory — no discrete VRAM
            system_memory_gb=128,
            max_parallel=4,
            active_jobs=0,
            local=True,
            online=True,
        )
    ]
    try:
        import threading
        result = []
        def _fetch():
            try:
                result.extend(refresh_remote_workers())
            except Exception:
                pass
        t = threading.Thread(target=_fetch, daemon=True)
        t.start()
        t.join(timeout=3.0)   # ← max 3 s for ENKI-II poll
        workers.extend(result)
    except Exception:
        pass   # ENKI-II offline — continue with MacBook only

    # Score and rank
    engine = PrioritizationEngine()
    decision = engine.dispatch(job=job, workers=workers)

    return DispatchResponse(
        job_id=decision.job_id,
        dispatched=decision.block_reason == "",
        primary_worker_id=decision.primary_worker_id,
        fallback_worker_id=decision.fallback_worker_id,
        block_reason=decision.block_reason,
        ambiguity_detected=decision.ambiguity_detected,
        scores={
            wid: {
                "total_score": s.total_score,
                "hardware_score": s.hardware_score,
                "load_score": s.load_score,
                "energy_score": s.energy_score,
                "skill_score": s.skill_score,
            }
            for wid, s in decision.scores.items()
        },
    )


# ── Registration helper ──────────────────────────────────────────────────────

def register_with_enkii(enkii_url: str, macbook_url: str, worker_name: str = "thomas-macbook") -> str | None:
    """Register this worker with ENKI-II's StudiOS API. Returns worker_id or None."""
    import urllib.request
    import urllib.error

    body = json.dumps({
        "name": worker_name,
        "url": macbook_url,
        "tag": "tag:thomas-macbook",
    }).encode()

    req = urllib.request.Request(
        f"{enkii_url}/api/workers",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
            worker_id = result.get("worker_id")
            log.info("Registered with ENKI-II as %s", worker_id)
            return worker_id
    except urllib.error.HTTPError as e:
        log.error("Failed to register with ENKI-II: HTTP %s", e.code)
        return None
    except Exception as e:
        log.error("Failed to register with ENKI-II: %s", e)
        return None


# ── NAS file serving ─────────────────────────────────────────────────────────

@app.get("/api/nas/list")
async def list_nas_files():
    """Return a curated subset of NAS files — not a full traversal."""
    def _scan():
        items = []
        count = 0
        for p in sorted(NAS_MOUNT.iterdir()):
            if count >= 50:
                items.append({"name": "...", "size": 0, "is_dir": True})
                break
            try:
                items.append({"name": p.name, "size": p.stat().st_size, "is_dir": p.is_dir()})
                count += 1
            except OSError:
                continue
        return {"dir": str(NAS_MOUNT), "count_limited": count, "items": items}
    try:
        result = await asyncio.to_thread(_scan)
        return result
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/nas/{filepath:path}")
def serve_nas_file(filepath: str):
    """Serve a file from the NAS mount. E.g. /nas/studios_test_input.mp4"""
    safe_path = (NAS_MOUNT / filepath).resolve()
    # Prevent path traversal
    if not str(safe_path).startswith(str(NAS_MOUNT.resolve())):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    if not safe_path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    if safe_path.is_dir():
        return JSONResponse({"error": "directory listing not supported"}, status_code=400)
    return FileResponse(safe_path, filename=safe_path.name)


@app.put("/nas/{filepath:path}")
async def upload_nas_file(filepath: str, request: Request):
    """Upload a file to the NAS mount. E.g. PUT /nas/output.mp4 with binary body."""
    safe_path = (NAS_MOUNT / filepath).resolve()
    if not str(safe_path).startswith(str(NAS_MOUNT.resolve())):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    # Ensure parent directory exists
    safe_path.parent.mkdir(parents=True, exist_ok=True)
    body = await request.body()
    safe_path.write_bytes(body)
    return JSONResponse({"status": "written", "path": str(safe_path), "size": len(body)})


# ── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="StudiOS Worker Inbox Server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host")
    parser.add_argument("--port", type=int, default=8765, help="Bind port")
    parser.add_argument("--enkii-url", default=os.environ.get("STUDIOS_ENKII_URL", "http://127.0.0.1:8765"),
                        help="ENKI-II StudiOS API URL")
    parser.add_argument("--worker-name", default="thomas-macbook-worker")
    parser.add_argument("--register", action="store_true",
                        help="Register with ENKI-II on startup")
    parser.add_argument("--macbook-url",
                        default=os.environ.get("STUDIOS_NAS_URL", "http://127.0.0.1:8765"),
                        help="This worker's public URL for ENKI-II")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    if args.register:
        wid = register_with_enkii(args.enkii_url, args.macbook_url, args.worker_name)
        if wid:
            print(f"Registered as worker_id={wid}")
        else:
            print("WARNING: Registration failed — ENKI-II may not be reachable")

    print(f"Starting StudiOS inbox on {args.host}:{args.port}")
    print(f"Inbox directory: {INBOX_DIR}")
    run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
