#!/bin/bash
# StudiOS local worker — background job runner
# Runs the WorkerLoop for the macbook-local worker.
# This script is invoked by the launchd plist.

set -e

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
STUDIOS_HOME="${STUDIOS_HOME:-$HOME/StudiOS-1.1}"
TRANSPORT_DIR="${TRANSPORT_DIR:-$HOME/studios_transport}"
OUTPUT_DIR="${OUTPUT_DIR:-$HOME/studios_output}"
LOG_DIR="${LOG_DIR:-$HOME/.hermes/logs/studios-worker}"
PYTHON="${STUDIOS_WORKER_PYTHON:-$HOME/AI/ComfyUI/.venv/bin/python3}"

mkdir -p "$TRANSPORT_DIR" "$OUTPUT_DIR" "$LOG_DIR"

exec >> "$LOG_DIR/worker.log" 2>&1

echo "=== StudiOS worker starting at $(date) ==="

STUDIOS_HOME="$STUDIOS_HOME" \
TRANSPORT_DIR="$TRANSPORT_DIR" \
OUTPUT_DIR="$OUTPUT_DIR" \
exec "$PYTHON" - <<'PYEOF'
import sys, os, logging
from pathlib import Path

sys.path.insert(0, str(Path(os.environ["STUDIOS_HOME"]) / "src"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

from studios_coordination import (
    WorkerLoop,
    WorkerCapability,
    Store,
    LoopbackTransport,
    default_gates,
)
from studios_coordination.host_identity import LOCAL_WORKER_ID

HOME_DIR = Path(os.environ["STUDIOS_HOME"]).parent

store = Store(str(HOME_DIR / "studios.db"))
transport = LoopbackTransport(Path(os.environ["TRANSPORT_DIR"]))

capabilities = WorkerCapability(
    worker_id=LOCAL_WORKER_ID,
    task_kinds=("benchmark", "comfyui-workflow"),
    backends=("cpu", "mps"),
    vram_gb=0,       # unified memory — no discrete VRAM
    system_memory_gb=128,
    max_parallel=4,
    active_jobs=0,
    local=True,
    online=True,
)

worker = WorkerLoop(
    worker_id=LOCAL_WORKER_ID,
    store=store,
    transport=transport,
    host_id=LOCAL_WORKER_ID,
    capabilities=capabilities,
    policy_gates=default_gates(),
    output_dir=Path(os.environ["OUTPUT_DIR"]),
    poll_interval=5.0,
)

logging.info("Worker loop starting as %s", LOCAL_WORKER_ID)
worker.start()
PYEOF
