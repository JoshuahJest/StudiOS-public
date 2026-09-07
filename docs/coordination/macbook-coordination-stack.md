---
type: coordination-doc
schema: studios-coordination/v1
host: joshuahs-macbook-pro
updated: 2026-09-06T06:22:00-05:00
---

# MacBook StudiOS Coordination Stack — 2026-09-06

## Overview

MacBook runs as the primary StudiOS controller. It hosts the Hermes agent (Thomas interface), the `studi-worker` profile for background delegation, and the MacBook-side coordination components documented here.

## Components

### `studios_inbox.py` — Worker Inbox Receiver

**Location:** `src/studios_inbox.py`
**Purpose:** Receives push-delegated tasks from ENKI-II over the Tailscale mesh
**Bind:** `0.0.0.0:8765` (via `tailscale serve` → `https://joshuahs-macbook-pro.tail1ba312.ts.net/`)
**Port used:** 443 (TLS, via `tailscale serve`) → 8765 (local)
**Startup:** Background process, should be migrated to launchd service
**Dependencies:** fastapi, uvicorn (Hermes agent venv Python 3.11)
**Endpoints:**
- `POST /api/worker/inbox` — receives delegated tasks from ENKI-II (X-Task-ID header)
- `GET /api/inbox` — list all received inbox entries
- `GET /api/inbox/{task_id}` — get specific inbox entry
- `GET /api/health` — health check

### `remote_transport.py` — Remote Transport

**Location:** `src/studios_coordination/remote_transport.py`
**Purpose:** Cross-machine transport adapter — translates LoopbackTransport-shaped operations to ENKI-II REST API calls
**Endpoints used:**
- `POST /api/tasks` — submit proposal
- `GET /api/tasks/{id}` — read task
- `DELETE /api/tasks/{id}` — withdraw/delete task
- `POST /api/tasks/{id}` — update task (claim, receipt)
- `GET /api/resources` — poll ENKI-II resource advertisements

### `scheduler.py` — Remote Worker Support

**Location:** `src/studios_coordination/scheduler.py`
**New exports:**
- `ENKI_II_WORKER` — static WorkerCapability for ENKI-II (RTX 4090, 24GB VRAM, CUDA backends)
- `refresh_remote_workers(enki_url)` — polls ENKI-II `/api/resources`, returns live WorkerCapability list
- `dispatch_task_to_enkii(job_id, task_kind, ...)` — submits a job directly to ENKI-II's StudiOS API

## Network Path

```
MacBook (Hermes/studi-worker)
  ↓ POST /api/tasks
ENKI-II StudiOS API (100.79.103.70:8765)
  ↓ POST /api/tasks/{id}/delegate
https://joshuahs-macbook-pro.tail1ba312.ts.net (port 443, TLS)
  ↓ tailscale serve proxy
127.0.0.1:8765
  ↓
studios_inbox (FastAPI)
  ↓ stored to ~/.studios/inbox/
MacBook filesystem
```

## ACL Requirements

| Direction | Source | Destination | Port | Purpose |
|---|---|---|---|---|
| MacBook → ENKI-II | tag:thomas-macbook | tag:phase1-control | 8765/TCP | REST task dispatch |
| ENKI-II → MacBook | tag:phase1-control | tag:thomas-macbook | 443/TCP | Push delegation (tailscale serve) |
| MacBook ↔ Fukurokuju | tag:thomas-macbook | tag:livesync-nas | 445/TCP | SMB (pre-existing) |

## Environment

- MacBook Tailscale: `100.121.168.39` / `joshuahs-macbook-pro.tail1ba312.ts.net` / `tag:thomas-macbook`
- ENKI-II Tailscale: `100.79.103.70` / `enki-ii.tail1ba312.ts.net` / `tag:phase1-control`
- Shared tailnet: `tail1ba312.ts.net`

## Startup Commands

```bash
# Start studios_inbox (once, background)
cd /Users/thomas/StudiOS-1.1
PYTHONPATH=src /Users/thomas/.hermes/hermes-agent/venv/bin/python3.11 -c "
import uvicorn, sys; sys.path.insert(0, 'src')
from studios_inbox import app
uvicorn.run(app, host='0.0.0.0', port=8765, log_level='info')
" >> /tmp/studios_inbox.log 2>&1 &

# Start tailscale serve (persistent — survives restarts)
sudo /Applications/Tailscale.app/Contents/MacOS/Tailscale serve --bg 8765

# Verify
curl http://127.0.0.1:8765/api/health
curl https://joshuahs-macbook-pro.tail1ba312.ts.net/api/worker/inbox  # should 405/404 not timeout
```

## To Do

- [ ] Migrate `studios_inbox` to launchd service for resilience
- [ ] Register MacBook as worker via `POST /api/workers` to ENKI-II (currently registered manually)
- [ ] Add MacBook worker to studi-worker cron dispatch loop
- [ ] Implement `/api/peer/message` endpoint if true agent-to-agent DMs are needed
