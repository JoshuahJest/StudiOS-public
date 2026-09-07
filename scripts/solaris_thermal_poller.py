#!/usr/bin/env python3
"""
Solaris Thermal Poller — runs on MacBook, polls Solaris thermal endpoint.
Thomas sets this up after Solaris has the thermal_reporter running.

Usage:
    python3 scripts/solaris_thermal_poller.py

Run as launchd: com.studios.solaris-thermal-poller.plist (polling interval: 60s)
"""
from __future__ import annotations
import json, logging, os, time
from datetime import datetime

import requests

SOLARIS_HOST = "http://192.168.50.244:8766/api/health/thermal"
OUT_DIR = os.path.expanduser("~/StudiOS/logs/solaris-thermal")
POLL_INTERVAL = 60  # seconds
LOG_FILE = os.path.join(OUT_DIR, f"solaris-thermal-{datetime.now():%Y%m%d}.jsonl")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("solaris-thermal")


def ensure_outdir():
    os.makedirs(OUT_DIR, exist_ok=True)


def poll() -> dict | None:
    try:
        r = requests.get(SOLARIS_HOST, timeout=5)
        r.raise_for_status()
        data = r.json()
        data["_polled_at"] = datetime.now().isoformat()
        return data
    except requests.RequestException as e:
        logger.warning("Poll failed: %s", e)
        return None


def append(data: dict):
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(data) + "\n")


def summarize(log_file: str):
    """Print a quick summary of today's observations."""
    temps = []
    with open(log_file) as f:
        for line in f:
            try:
                d = json.loads(line)
                if "gpu_temp_c" in d and d["gpu_temp_c"]:
                    temps.append(d["gpu_temp_c"])
            except json.JSONDecodeError:
                continue
    if not temps:
        print("No data yet.")
        return
    print(f"Readings: {len(temps)}")
    print(f"Min: {min(temps):.1f}°C  Max: {max(temps):.1f}°C  Avg: {sum(temps)/len(temps):.1f}°C")


def main():
    ensure_outdir()
    logger.info("Starting Solaris thermal poller, interval=%ds", POLL_INTERVAL)

    while True:
        data = poll()
        if data:
            append(data)
            logger.debug("Logged: GPU=%s°C CPU=%s°C",
                         data.get("gpu_temp_c"), data.get("cpu_temp_c"))

            # Daily summary every 100 polls (≈100 min)
            n = len(open(LOG_FILE).readlines())
            if n % 100 == 0:
                summarize(LOG_FILE)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
