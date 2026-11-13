#!/usr/bin/env python3
"""Create a bounded, read-only DAAP metadata snapshot; dry-run by default."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile
from typing import Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from studios_music.daap import DaapClient, DaapError, HttpResponse, SnapshotLimits, snapshot_daap


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="Private-LAN DAAP origin, e.g. http://192.168.50.140:3689")
    parser.add_argument("--output", required=True, type=Path, help="Atomic JSON receipt path")
    parser.add_argument("--database-id", type=int, default=1)
    parser.add_argument("--container-id", type=int, action="append", default=[])
    parser.add_argument("--request-timeout-seconds", type=float, default=5.0)
    parser.add_argument("--total-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--max-response-bytes", type=int, default=2 * 1024 * 1024)
    parser.add_argument("--max-containers", type=int, default=100)
    parser.add_argument("--max-items-per-container", type=int, default=100)
    parser.add_argument("--max-frames", type=int, default=50_000)
    parser.add_argument("--max-depth", type=int, default=12)
    parser.add_argument("--execute", action="store_true", help="Perform allowlisted metadata GETs")
    return parser


def bounded_http_get(url: str, timeout: float, max_bytes: int) -> HttpResponse:
    request = Request(url, method="GET", headers={"User-Agent": "StudiOS-DAAP-Snapshot/1"})
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise OSError("DAAP response exceeded configured byte limit")
            return HttpResponse(
                body=body,
                headers={key: value for key, value in response.headers.items()},
                status=int(response.status),
            )
    except HTTPError as error:
        return HttpResponse(body=b"", headers=dict(error.headers.items()), status=int(error.code))
    except URLError as error:
        raise OSError(f"DAAP request failed: {error.reason}") from error


def atomic_write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    limits = SnapshotLimits(
        request_timeout_seconds=arguments.request_timeout_seconds,
        total_timeout_seconds=arguments.total_timeout_seconds,
        max_response_bytes=arguments.max_response_bytes,
        max_containers=arguments.max_containers,
        max_items_per_container=arguments.max_items_per_container,
        max_frames=arguments.max_frames,
        max_depth=arguments.max_depth,
    )
    client = DaapClient(arguments.base_url, limits=limits, transport=bounded_http_get)
    if not arguments.execute:
        plan = {
            "dry_run": True,
            "network_accessed": False,
            "base_url": client.base_url,
            "database_id": arguments.database_id,
            "container_ids": arguments.container_id,
            "output": str(arguments.output),
            "warning": "Execution uses unauthenticated, unencrypted HTTP only on the explicit private-LAN endpoint.",
            "next_step": "Review this plan, then add --execute to perform allowlisted read-only GETs.",
        }
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0

    try:
        result = snapshot_daap(
            client,
            database_id=arguments.database_id,
            container_ids=tuple(arguments.container_id),
        )
    except DaapError as error:
        result = {
            "schema_version": 1,
            "status": "error",
            "complete": False,
            "errors": [{"stage": "negotiation", "type": type(error).__name__, "message": str(error)}],
            "provenance": {
                "base_url": client.base_url,
                "method": "GET",
                "audio_fetched": False,
                "write_endpoints_used": False,
            },
        }
    atomic_write_json(arguments.output, result)
    print(json.dumps({"output": str(arguments.output), "status": result["status"]}, sort_keys=True))
    return 0 if result.get("complete") else 3


if __name__ == "__main__":
    raise SystemExit(main())
