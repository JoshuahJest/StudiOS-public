"""Least-privilege planning for already-audited Pinokio app lifecycles.

The planner never installs, updates, resets, downloads, or executes an app. It
creates a loopback-only Pinokio 8 request that a separately authorized runner
may issue after checking the pinned revision.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from urllib.parse import urlparse


class PinokioPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class PinokioAppSpec:
    app_id: str
    app_dir: Path
    revision: str
    start_script: str
    service_url: str


@dataclass(frozen=True)
class LifecyclePlan:
    app_id: str
    action: str
    cwd: Path
    expected_service_url: str
    revision: str
    request_url: str
    http_method: str
    executed: bool = False


def _require_loopback_http(url: str, reason: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise PinokioPolicyError(reason)


def plan_lifecycle_action(
    spec: PinokioAppSpec,
    action: str,
    pinokio_home: Path,
    *,
    pinokio_base_url: str = "http://127.0.0.1:42000",
) -> LifecyclePlan:
    """Validate and describe start/stop for an existing pinned local app."""
    if action not in {"start", "stop"}:
        raise PinokioPolicyError("action-not-allowlisted")
    if not re.fullmatch(r"[0-9a-f]{40}", spec.revision):
        raise PinokioPolicyError("revision-not-pinned")

    home = pinokio_home.resolve()
    app_dir = spec.app_dir.resolve()
    managed_api = (home / "api").resolve()
    if app_dir.parent != managed_api:
        raise PinokioPolicyError("app-outside-managed-root")

    script = Path(spec.start_script)
    if script.name != spec.start_script or script.suffix != ".js":
        raise PinokioPolicyError("invalid-script-path")
    if not (app_dir / script).is_file():
        raise PinokioPolicyError("script-not-found")

    _require_loopback_http(spec.service_url, "service-not-loopback")
    _require_loopback_http(pinokio_base_url, "control-plane-not-loopback")

    script_path = f"api/{app_dir.name}/{spec.start_script}"
    base = pinokio_base_url.rstrip("/")
    if action == "start":
        request_url = f"{base}/run/{script_path}"
    else:
        request_url = f"{base}/action/stop/{script_path}"

    return LifecyclePlan(
        app_id=spec.app_id,
        action=action,
        cwd=spec.app_dir,
        expected_service_url=spec.service_url,
        revision=spec.revision,
        request_url=request_url,
        http_method="GET",
    )
