"""ENKI-II resource poller — runs as a launchd service on MacBook.

Polls ENKI-II's /api/resources every 60s and writes state to a local JSON file.
Other StudiOS components read this to make routing decisions.

Run with:
    python3 -m studios_enkii_resource_poller

Or via launchd: com.studios.enkii-resource-poller.plist
"""

import os
import json
import logging
import time
from pathlib import Path
from threading import Event

import httpx

log = logging.getLogger("enkii-resource-poller")

ENKII_HOST = os.environ.get("STUDIOS_ENKII_HOST", "127.0.0.1")
ENKII_PORT = 8765
ENKII_URL = f"http://{ENKII_HOST}:{ENKII_PORT}"
POLL_INTERVAL = 60  # seconds
STATE_FILE = Path.home() / ".studios" / "enkii_resources.json"


def poll_enkii_resources() -> dict | None:
    """Fetch resource snapshot from ENKI-II. Returns None on failure."""
    try:
        with httpx.Client(timeout=10.0) as client:
            r = client.get(f"{ENKII_URL}/api/resources")
            r.raise_for_status()
            data = r.json()

        # Add metadata
        data["_polled_at"] = time.time()
        data["_reachable"] = True
        log.debug("Polled ENKI-II resources: GPU util=%s, RAM free=%s",
                  data.get("gpu_util"), data.get("ram_free_gb"))
        return data
    except Exception as e:
        log.warning("ENKI-II poll failed: %s", e)
        return {"_reachable": False, "_polled_at": time.time(), "_error": str(e)}


def write_state(data: dict | None):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: temp + rename
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.rename(STATE_FILE)


def is_enkii_available() -> bool:
    """Quick check — is ENKI-II reachable and have GPU free?"""
    state = read_state()
    if not state or not state.get("_reachable"):
        return False
    gpu_util = state.get("gpu_util", 100)
    return gpu_util < 90  # Available if GPU util below 90%


def read_state() -> dict | None:
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return None


def get_enkii_resources() -> dict | None:
    """Read the latest cached resource state. Always returns cached data if fresh."""
    return read_state()


def run_loop():
    """Main polling loop — writes state every POLL_INTERVAL seconds."""
    log.info("Starting ENKI-II resource poller (interval=%ds, target=%s:%s)",
             POLL_INTERVAL, ENKII_HOST, ENKII_PORT)

    # Prime the state immediately
    data = poll_enkii_resources()
    write_state(data)

    # Then loop
    shutdown = Event()
    while not shutdown.wait(POLL_INTERVAL):
        data = poll_enkii_resources()
        write_state(data)

    log.info("ENKI-II resource poller shutting down")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    run_loop()
