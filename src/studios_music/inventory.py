"""Bounded, read-only inventory of legacy iTunes/Music artifacts."""

from __future__ import annotations

import hashlib
import multiprocessing
import os
import queue as queue_module
import signal
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


@dataclass(frozen=True)
class InventoryBudget:
    max_depth: int = 3
    max_entries: int = 2_000
    max_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.max_depth < 0:
            raise ValueError("max_depth must be non-negative")
        if self.max_entries < 1:
            raise ValueError("max_entries must be positive")
        if self.max_seconds <= 0:
            raise ValueError("max_seconds must be positive")


def _classify(path: Path, is_dir: bool) -> str:
    name = path.name.casefold()
    if is_dir:
        if name.endswith(".musiclibrary"):
            return "music_library_bundle"
        if name.endswith(".sparsebundle"):
            return "disk_image_bundle"
        if name == "previous itunes libraries":
            return "previous_libraries"
        if name == "album artwork":
            return "artwork_directory"
        if name in {"itunes media", "itunes music", "music media"}:
            return "media_directory"
        return "directory"

    if name.endswith(".itl"):
        return "itunes_database"
    if name.endswith(".xml") and "library" in name and (
        "itunes" in name or "music" in name
    ):
        return "itunes_xml"
    if name.endswith(".musiclibrary"):
        return "music_library_bundle"
    return "file"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class _DirectoryListingTimeout(TimeoutError):
    pass


def _scandir_with_timeout(directory: Path, timeout_seconds: float) -> list[os.DirEntry[str]]:
    """List one directory with a hard deadline where SIGALRM is available."""

    if timeout_seconds <= 0:
        raise _DirectoryListingTimeout
    supports_alarm = (
        hasattr(signal, "SIGALRM")
        and hasattr(signal, "setitimer")
        and threading.current_thread() is threading.main_thread()
    )
    if not supports_alarm:
        return sorted(os.scandir(directory), key=lambda item: item.name.casefold())

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)

    def _raise_timeout(_signum: int, _frame: object) -> None:
        raise _DirectoryListingTimeout

    signal.signal(signal.SIGALRM, _raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
    try:
        return sorted(os.scandir(directory), key=lambda item: item.name.casefold())
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def inventory_roots(
    roots: Sequence[str | Path],
    budget: InventoryBudget,
    *,
    hash_file_max_bytes: int = 16 * 1024 * 1024,
) -> dict[str, Any]:
    """Inventory exact roots within depth, entry, and wall-clock budgets.

    Symlinks are recorded but never traversed. Errors are data: a missing or
    unreadable root cannot be confused with a valid empty inventory.
    """

    started = time.monotonic()
    entries: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    truncated = False
    truncation_reason: str | None = None

    for raw_root in roots:
        root = Path(raw_root)
        if not root.exists():
            errors.append({"root": str(root), "error": "root does not exist"})
            continue
        if not root.is_dir():
            errors.append({"root": str(root), "error": "root is not a directory"})
            continue

        queue: deque[tuple[Path, int]] = deque([(root, 0)])
        while queue:
            if time.monotonic() - started >= budget.max_seconds:
                truncated = True
                truncation_reason = "time budget exceeded"
                break

            directory, depth = queue.popleft()
            remaining_seconds = budget.max_seconds - (time.monotonic() - started)
            try:
                children = _scandir_with_timeout(directory, remaining_seconds)
            except _DirectoryListingTimeout:
                truncated = True
                truncation_reason = "time budget exceeded"
                break
            except OSError as exc:
                errors.append(
                    {
                        "root": str(root),
                        "path": str(directory),
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue

            for child in children:
                if len(entries) >= budget.max_entries:
                    truncated = True
                    truncation_reason = "entry budget exceeded"
                    break
                if time.monotonic() - started >= budget.max_seconds:
                    truncated = True
                    truncation_reason = "time budget exceeded"
                    break

                path = Path(child.path)
                relative = path.relative_to(root).as_posix()
                try:
                    is_symlink = child.is_symlink()
                    is_dir = child.is_dir(follow_symlinks=False)
                    stat = child.stat(follow_symlinks=False)
                except OSError as exc:
                    errors.append(
                        {
                            "root": str(root),
                            "path": relative,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    continue

                kind = "symlink" if is_symlink else _classify(path, is_dir)
                entry: dict[str, Any] = {
                    "root": str(root),
                    "relative_path": relative,
                    "entry_type": "symlink" if is_symlink else ("directory" if is_dir else "file"),
                    "kind": kind,
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "sha256": None,
                }
                if (
                    not is_dir
                    and not is_symlink
                    and kind in {"itunes_database", "itunes_xml"}
                    and stat.st_size <= hash_file_max_bytes
                ):
                    try:
                        entry["sha256"] = _sha256(path)
                    except OSError as exc:
                        errors.append(
                            {
                                "root": str(root),
                                "path": relative,
                                "error": f"hash failed: {type(exc).__name__}: {exc}",
                            }
                        )

                entries.append(entry)
                child_depth = depth + 1
                if (
                    is_dir
                    and not is_symlink
                    and kind not in {"music_library_bundle", "disk_image_bundle"}
                    and child_depth < budget.max_depth
                ):
                    queue.append((path, child_depth))

            if truncated:
                break
        if truncated:
            break

    return {
        "roots": [str(Path(root)) for root in roots],
        "budget": {
            "max_depth": budget.max_depth,
            "max_entries": budget.max_entries,
            "max_seconds": budget.max_seconds,
        },
        "entries": entries,
        "errors": errors,
        "truncated": truncated,
        "truncation_reason": truncation_reason,
    }


def _inventory_worker(
    roots: Sequence[str | Path],
    budget: InventoryBudget,
    hash_file_max_bytes: int,
    result_queue: Any,
) -> None:
    try:
        result_queue.put(
            inventory_roots(
                roots,
                budget,
                hash_file_max_bytes=hash_file_max_bytes,
            )
        )
    except BaseException as exc:
        result_queue.put(
            {
                "worker_error": f"{type(exc).__name__}: {exc}",
            }
        )


def inventory_roots_hard_timeout(
    roots: Sequence[str | Path],
    budget: InventoryBudget,
    *,
    hash_file_max_bytes: int = 16 * 1024 * 1024,
) -> dict[str, Any]:
    """Run inventory in a disposable process so blocked SMB calls can be killed."""

    start_methods = multiprocessing.get_all_start_methods()
    method = "fork" if "fork" in start_methods else "spawn"
    context = multiprocessing.get_context(method)
    result_queue = context.Queue(maxsize=1)
    root_strings = [str(Path(root)) for root in roots]
    process_factory: Any = getattr(context, "Process")
    process = process_factory(
        target=_inventory_worker,
        args=(root_strings, budget, hash_file_max_bytes, result_queue),
        daemon=True,
    )
    process.start()
    process.join(budget.max_seconds + 0.25)

    if process.is_alive():
        process.terminate()
        process.join(0.5)
        if process.is_alive() and hasattr(process, "kill"):
            process.kill()
            process.join(0.5)
        return {
            "roots": root_strings,
            "budget": {
                "max_depth": budget.max_depth,
                "max_entries": budget.max_entries,
                "max_seconds": budget.max_seconds,
            },
            "entries": [],
            "errors": [
                {
                    "root": ", ".join(root_strings),
                    "error": "inventory worker was terminated after a blocked directory listing",
                }
            ],
            "truncated": True,
            "truncation_reason": "hard time budget exceeded",
        }

    try:
        result = result_queue.get(timeout=0.5)
    except queue_module.Empty:
        result = {"worker_error": f"worker exited with code {process.exitcode} without a result"}
    finally:
        result_queue.close()

    if "worker_error" in result:
        return {
            "roots": root_strings,
            "budget": {
                "max_depth": budget.max_depth,
                "max_entries": budget.max_entries,
                "max_seconds": budget.max_seconds,
            },
            "entries": [],
            "errors": [{"root": ", ".join(root_strings), "error": result["worker_error"]}],
            "truncated": True,
            "truncation_reason": "inventory worker failed",
        }
    return result
