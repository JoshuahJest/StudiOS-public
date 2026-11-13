"""Host identity for this worker.

The worker id is configuration, not code. It defaults to a generic name so a
public checkout carries no infrastructure identifiers; set STUDIOS_WORKER_ID in
the host's launchd plist (or the environment) to the value the coordination
API knows this machine by.
"""

import os
import socket

DEFAULT_WORKER_ID = "macbook-worker"

LOCAL_WORKER_ID: str = (
    os.environ.get("STUDIOS_WORKER_ID")
    or os.environ.get("STUDIOS_LOCAL_WORKER_ID")
    or DEFAULT_WORKER_ID
)

LOCAL_HOSTNAME: str = socket.gethostname()


def worker_id() -> str:
    """Return the configured worker id for this host."""
    return LOCAL_WORKER_ID
