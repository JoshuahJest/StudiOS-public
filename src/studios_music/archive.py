"""Verified, non-overwriting archives for legacy music-library sources."""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


_SAFE_SSH_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DEFAULT_MAX_ENTRIES = 1_000_000
_DEFAULT_MAX_FILE_BYTES = 1 << 40
_DEFAULT_MAX_TOTAL_BYTES = 2 << 40
_DEFAULT_MAX_MANIFEST_BYTES = 64 << 20
_DEFAULT_MAX_STDERR_BYTES = 64 << 10
_HARD_MAX_ENTRIES = 10_000_000
_HARD_MAX_FILE_BYTES = 8 << 50
_HARD_MAX_TOTAL_BYTES = 16 << 50
_HARD_MAX_MANIFEST_BYTES = 512 << 20

# This script deliberately uses Python 2.7 syntax and APIs. It traverses by held
# directory descriptors and O_NOFOLLOW, so a child swapped to a symlink is never
# followed. Output is bounded NDJSON and is parsed strictly by the receiver.
_REMOTE_MANIFEST_SCRIPT = r'''
import hashlib
import json
import os
import stat
import sys

root = os.path.abspath(sys.argv[1])
max_entries = int(sys.argv[2])
max_file_bytes = int(sys.argv[3])
max_total_bytes = int(sys.argv[4])
O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
if not O_DIRECTORY or not O_NOFOLLOW:
    raise RuntimeError("remote Python lacks required no-follow directory flags")
entries = []
file_count = 0
total_bytes = 0


def fingerprint(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size,
            int(value.st_mtime))


def fail(message):
    raise RuntimeError(message)


def add_entry(value):
    global file_count, total_bytes
    entries.append(value)
    if len(entries) > max_entries:
        fail("source entry count exceeds limit")
    if value["type"] == "file":
        file_count += 1
        if value["size"] > max_file_bytes:
            fail("source file size exceeds limit: " + value["relative_path"])
        total_bytes += value["size"]
        if total_bytes > max_total_bytes:
            fail("source total bytes exceeds limit")


def hash_fd(fd, relative):
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode):
        fail("non-regular file is not permitted: " + relative)
    digest = hashlib.sha256()
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    after = os.fstat(fd)
    if fingerprint(before) != fingerprint(after):
        fail("source changed while hashing: " + relative)
    return before, digest.hexdigest()


def scan_directory(directory_fd, relative_prefix):
    os.fchdir(directory_fd)
    names = os.listdir(u".")
    names.sort(key=lambda value: (value.lower(), value))
    for name in names:
        if name in (u".", u"..") or u"/" in name or u"\x00" in name:
            fail("unsafe source entry name")
        relative = name if not relative_prefix else relative_prefix + u"/" + name
        before = os.lstat(name)
        if stat.S_ISLNK(before.st_mode):
            fail("symlink is not permitted: " + relative)
        if stat.S_ISDIR(before.st_mode):
            child_fd = os.open(name, os.O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
            try:
                opened = os.fstat(child_fd)
                if before.st_dev != opened.st_dev or before.st_ino != opened.st_ino:
                    fail("source directory changed while opening: " + relative)
                add_entry({"type": "directory", "relative_path": relative,
                           "mode": stat.S_IMODE(opened.st_mode),
                           "mtime_seconds": int(opened.st_mtime)})
                scan_directory(child_fd, relative)
            finally:
                os.close(child_fd)
                os.fchdir(directory_fd)
            after = os.lstat(name)
            if after.st_dev != before.st_dev or after.st_ino != before.st_ino or not stat.S_ISDIR(after.st_mode):
                fail("source directory changed during scan: " + relative)
        elif stat.S_ISREG(before.st_mode):
            file_fd = os.open(name, os.O_RDONLY | O_NOFOLLOW)
            try:
                opened = os.fstat(file_fd)
                if before.st_dev != opened.st_dev or before.st_ino != opened.st_ino:
                    fail("source file changed while opening: " + relative)
                value, digest = hash_fd(file_fd, relative)
            finally:
                os.close(file_fd)
            after = os.lstat(name)
            if after.st_dev != before.st_dev or after.st_ino != before.st_ino or not stat.S_ISREG(after.st_mode):
                fail("source file changed during scan: " + relative)
            add_entry({"type": "file", "relative_path": relative,
                       "size": value.st_size, "sha256": digest,
                       "mode": stat.S_IMODE(value.st_mode),
                       "mtime_seconds": int(value.st_mtime)})
        else:
            fail("non-regular source entry is not permitted: " + relative)


root_before = os.lstat(root)
if stat.S_ISLNK(root_before.st_mode) or not stat.S_ISDIR(root_before.st_mode):
    fail("source is not a non-symlink directory: " + root)
root_fd = os.open(root, os.O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
try:
    root_opened = os.fstat(root_fd)
    if root_before.st_dev != root_opened.st_dev or root_before.st_ino != root_opened.st_ino:
        fail("source root changed while opening")
    scan_directory(root_fd, u"")
finally:
    os.close(root_fd)

entries.sort(key=lambda value: (value["relative_path"].lower(), value["relative_path"]))
frames = [{"type": "header", "schema_version": 1}]
frames.extend(entries)
frames.append({"type": "complete", "entry_count": len(entries),
               "file_count": file_count, "total_bytes": total_bytes})
for frame in frames:
    sys.stdout.write(json.dumps(frame, sort_keys=True, separators=(",", ":")) + "\n")
'''


class ArchiveExistsError(FileExistsError):
    """Raised when a source archive ID already exists."""


class UnsafeSourceError(ValueError):
    """Raised when traversal or archive storage could escape safely held roots."""


class ArchiveTimeoutError(TimeoutError):
    """Raised when SSH or rsync exceeds its bounded execution time."""


class ArchiveCleanupError(OSError):
    """Raised when failed staging cannot be removed completely."""


class ArchiveTransportError(IOError):
    """Raised when the SSH/rsync transport itself failed.

    A transport failure (connection reset, broken pipe, timeout) says nothing
    about source integrity, so the staged partial copy is preserved for the
    next attempt instead of being deleted. Deleting it would force every
    retry to re-transfer the entire source from byte zero.
    """


# Substrings that identify a transport-level interruption rather than a
# source-integrity or archive-safety violation.
_TRANSPORT_FAILURE_MARKERS = (
    "connection reset by peer",
    "broken pipe",
    "connection closed by remote host",
    "connection timed out",
    "unexpected end of file",
    "lost connection",
    "host is down",
    "no route to host",
    "network is unreachable",
    "connection refused",
    "kex_exchange_identification",
)


@dataclass(frozen=True)
class ArchiveLimits:
    """Hard bounds applied by both remote manifest generation and parsing."""

    max_entries: int = _DEFAULT_MAX_ENTRIES
    max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES
    max_total_bytes: int = _DEFAULT_MAX_TOTAL_BYTES
    max_manifest_bytes: int = _DEFAULT_MAX_MANIFEST_BYTES

    def __post_init__(self) -> None:
        for name in (
            "max_entries",
            "max_file_bytes",
            "max_total_bytes",
            "max_manifest_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        hard_maximums = {
            "max_entries": _HARD_MAX_ENTRIES,
            "max_file_bytes": _HARD_MAX_FILE_BYTES,
            "max_total_bytes": _HARD_MAX_TOTAL_BYTES,
            "max_manifest_bytes": _HARD_MAX_MANIFEST_BYTES,
        }
        for name, maximum in hard_maximums.items():
            if getattr(self, name) > maximum:
                raise ValueError(f"{name} exceeds the hard maximum")
        if self.max_file_bytes > self.max_total_bytes:
            raise ValueError("max_file_bytes cannot exceed max_total_bytes")
        if self.max_manifest_bytes < 128:
            raise ValueError("max_manifest_bytes is too small")


@dataclass(frozen=True)
class _FilesystemIdentity:
    filesystem_type: str
    filesystem_id: tuple[int, int]
    mounted_from: str
    mounted_on: str


if sys.platform == "darwin":
    class _DarwinFsid(ctypes.Structure):
        _fields_ = [("value", ctypes.c_int32 * 2)]


    class _DarwinStatfs(ctypes.Structure):
        _fields_ = [
            ("block_size", ctypes.c_uint32),
            ("io_size", ctypes.c_int32),
            ("blocks", ctypes.c_uint64),
            ("blocks_free", ctypes.c_uint64),
            ("blocks_available", ctypes.c_uint64),
            ("files", ctypes.c_uint64),
            ("files_free", ctypes.c_uint64),
            ("filesystem_id", _DarwinFsid),
            ("owner", ctypes.c_uint32),
            ("filesystem_type_number", ctypes.c_uint32),
            ("flags", ctypes.c_uint32),
            ("filesystem_subtype", ctypes.c_uint32),
            ("filesystem_type_name", ctypes.c_char * 16),
            ("mounted_on", ctypes.c_char * 1024),
            ("mounted_from", ctypes.c_char * 1024),
            ("reserved", ctypes.c_uint32 * 8),
        ]


def _filesystem_identity(directory_fd: int) -> _FilesystemIdentity:
    """Read a mount identity from an already-open directory descriptor."""

    if sys.platform != "darwin":  # pragma: no cover - production is macOS/APFS
        raise OSError(errno.ENOTSUP, "descriptor-based filesystem identity requires macOS")
    value = _DarwinStatfs()
    libc = ctypes.CDLL(None, use_errno=True)
    function = libc.fstatfs
    function.argtypes = [ctypes.c_int, ctypes.POINTER(_DarwinStatfs)]
    function.restype = ctypes.c_int
    if function(directory_fd, ctypes.byref(value)) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))

    def decode(raw: bytes) -> str:
        return raw.split(b"\0", 1)[0].decode("utf-8", errors="strict")

    return _FilesystemIdentity(
        filesystem_type=decode(bytes(value.filesystem_type_name)).casefold(),
        filesystem_id=(
            int(value.filesystem_id.value[0]),
            int(value.filesystem_id.value[1]),
        ),
        mounted_from=decode(bytes(value.mounted_from)),
        mounted_on=decode(bytes(value.mounted_on)),
    )


def _assert_filesystem_identity(
    directory_fd: int, expected: _FilesystemIdentity
) -> None:
    if _filesystem_identity(directory_fd) != expected:
        raise UnsafeSourceError("archive root mount identity changed")


@dataclass(frozen=True)
class _CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool


class _BoundedReader(threading.Thread):
    def __init__(self, handle: Any, limit: int) -> None:
        super().__init__(daemon=True)
        self.handle = handle
        self.limit = limit
        self.data = bytearray()
        self.truncated = False
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            while True:
                chunk = self.handle.read(64 * 1024)
                if not chunk:
                    return
                remaining = self.limit - len(self.data)
                if remaining > 0:
                    self.data.extend(chunk[:remaining])
                if len(chunk) > max(remaining, 0):
                    self.truncated = True
        except BaseException as error:  # pragma: no cover - OS pipe failure
            self.error = error
        finally:
            self.handle.close()


def _positive_timeout(
    name: str, value: int | float, *, maximum: float = 7 * 24 * 60 * 60
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} timeout must be a positive number")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} timeout must be a positive finite number")
    if result > maximum:
        raise ValueError(f"{name} timeout exceeds the hard maximum")
    return result


def _positive_int(name: str, value: int, *, maximum: int = 1 << 63) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    if value > maximum:
        raise ValueError(f"{name} exceeds the hard maximum")
    return value


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=2)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    process.wait()


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    """Kill descendants that may still hold inherited output pipes open."""

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _run_bounded(
    command: list[str],
    *,
    timeout_seconds: float,
    stdout_limit: int,
    stderr_limit: int,
    discard_stdout: bool = False,
    working_directory_fd: int | None = None,
) -> _CommandResult:
    deadline = time.monotonic() + timeout_seconds
    stdout_target: Any = subprocess.DEVNULL if discard_stdout else subprocess.PIPE
    def enter_working_directory() -> None:
        if working_directory_fd is not None:
            os.fchdir(working_directory_fd)

    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=stdout_target,
        stderr=subprocess.PIPE,
        start_new_session=True,
        pass_fds=(() if working_directory_fd is None else (working_directory_fd,)),
        preexec_fn=(None if working_directory_fd is None else enter_working_directory),
    )
    stdout_reader = (
        None
        if discard_stdout
        else _BoundedReader(process.stdout, stdout_limit)  # type: ignore[arg-type]
    )
    stderr_reader = _BoundedReader(process.stderr, stderr_limit)
    if stdout_reader is not None:
        stdout_reader.start()
    stderr_reader.start()
    try:
        process.wait(timeout=max(0.001, deadline - time.monotonic()))
    except subprocess.TimeoutExpired as error:
        _terminate_process_group(process)
        if stdout_reader is not None:
            stdout_reader.join(timeout=2)
        stderr_reader.join(timeout=2)
        raise ArchiveTimeoutError(
            f"command timed out after {timeout_seconds:g} seconds: {command[0]}"
        ) from error
    readers = [reader for reader in (stdout_reader, stderr_reader) if reader is not None]
    for reader in readers:
        reader.join(timeout=max(0.0, deadline - time.monotonic()))
    if any(reader.is_alive() for reader in readers):
        _kill_process_group(process)
        for reader in readers:
            reader.join(timeout=1)
        raise ArchiveTimeoutError(
            f"command output drain timed out after {timeout_seconds:g} seconds: {command[0]}"
        )
    if any(reader.error is not None for reader in readers):
        raise IOError("failed while reading bounded command output")
    return _CommandResult(
        returncode=process.returncode,
        stdout=b"" if stdout_reader is None else bytes(stdout_reader.data),
        stderr=bytes(stderr_reader.data),
        stdout_truncated=False if stdout_reader is None else stdout_reader.truncated,
        stderr_truncated=stderr_reader.truncated,
    )


def _error_detail(result: _CommandResult) -> str:
    text = result.stderr.decode("utf-8", errors="replace").strip()
    if result.stderr_truncated:
        text = f"{text} [stderr truncated]" if text else "[stderr truncated]"
    return f": {text}" if text else ""


def _is_transport_failure(detail: str) -> bool:
    """Report whether a failure detail describes a transport interruption."""

    lowered = detail.casefold()
    return any(marker in lowered for marker in _TRANSPORT_FAILURE_MARKERS)


# rsync writes an in-progress transfer to a temporary name inside the
# destination directory and renames it into place when the write completes.
# The observed shapes are ".<name>.<random>" and "..<name>.<random>", where
# the random tail is 8 to 16 alphanumerics. A transfer interrupted mid-write
# leaves that temporary behind, so the staged payload can legitimately contain
# a file that is not in the source. These names are rsync's own working files,
# not source content, so they are removed before the payload is published.
_RSYNC_TEMP_SUFFIX_PATTERN = re.compile(r"^\.{1,2}[^/]*\.[A-Za-z0-9]{8,16}$")


def _is_rsync_temporary_name(name: str) -> bool:
    """Return True for an rsync in-progress temporary file name."""

    return bool(_RSYNC_TEMP_SUFFIX_PATTERN.match(name))


def _remove_rsync_temporaries(
    payload_fd: int, source_paths: frozenset[str] | set[str] | None = None
) -> int:
    """Delete rsync in-progress temporaries left in the staged payload.

    An interrupted transfer can leave a temporary file inside the payload.
    Those are rsync working files, never source content, so they are removed
    before the payload is published so the published tree matches its
    manifest exactly.

    ``source_paths`` is the set of relative paths present in the source
    manifest. A staged file whose name looks like an rsync temporary is only
    removed when that name is NOT source content, so a genuine source file
    that happens to match the naming shape is never deleted.
    """

    removed = 0
    known = source_paths if source_paths is not None else None

    def visit(directory_fd: int, prefix: str) -> None:
        nonlocal removed
        for name in os.listdir(directory_fd):
            relative = name if not prefix else f"{prefix}/{name}"
            if _is_rsync_temporary_name(name):
                if known is not None and relative in known:
                    # A real source file that matches the temporary shape.
                    continue
                os.unlink(name, dir_fd=directory_fd)
                removed += 1
                continue
            try:
                child_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
            except OSError:
                continue
            try:
                visit(child_fd, relative)
            finally:
                os.close(child_fd)

    visit(payload_fd, "")
    os.fsync(payload_fd)
    return removed


def _apply_directory_times(
    payload_fd: int, source_entries: list[dict[str, Any]]
) -> int:
    """Restore staged directory mtimes to match the source manifest.

    Writing files into a directory, and removing rsync temporaries from it,
    changes that directory's mtime. rsync only restores directory times on a
    complete transfer, so an interrupted-and-resumed transfer leaves them
    wrong. Applying the source manifest's directory times makes the staged
    tree match the source exactly, and it is done deepest-first so that
    setting a parent's time cannot be disturbed afterwards.
    """

    directories = [
        entry
        for entry in source_entries
        if entry.get("type") == "directory" and "mtime_seconds" in entry
    ]
    directories.sort(key=lambda entry: entry["relative_path"], reverse=True)
    applied = 0
    for entry in directories:
        relative = entry["relative_path"]
        parts = relative.split("/")
        try:
            if len(parts) == 1:
                child_fd = os.dup(payload_fd)
            else:
                child_fd = os.open(
                    "/".join(parts[:-1]),
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=payload_fd,
                )
        except OSError:
            continue
        try:
            name = parts[-1]
            try:
                os.utime(
                    name,
                    (entry["mtime_seconds"], entry["mtime_seconds"]),
                    dir_fd=child_fd,
                )
                applied += 1
            except OSError:
                continue
        finally:
            os.close(child_fd)
    os.fsync(payload_fd)
    return applied


def _validate_source_id(source_id: str) -> None:
    if (
        not isinstance(source_id, str)
        or not source_id
        or Path(source_id).name != source_id
        or source_id in {".", ".."}
        or "\x00" in source_id
    ):
        raise UnsafeSourceError("source_id must be one safe path component")


def _validated_regular_file(value: str | Path, label: str) -> Path:
    path = Path(value).expanduser().absolute()
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise UnsafeSourceError(f"{label} is not a file: {path}") from error
    if stat.S_ISLNK(info.st_mode):
        raise UnsafeSourceError(f"{label} must not be a symlink: {path}")
    if not stat.S_ISREG(info.st_mode):
        raise UnsafeSourceError(f"{label} is not a regular file: {path}")
    return path


def validate_ssh_source(
    *,
    source_host: str,
    source_user: str,
    source_path: str,
    identity_file: str | Path,
    known_hosts_file: str | Path,
) -> tuple[Path, Path]:
    """Validate SSH coordinates and explicit trust files without connecting."""

    if not isinstance(source_host, str) or not _SAFE_SSH_NAME.fullmatch(source_host):
        raise UnsafeSourceError("source_host contains unsafe characters")
    if not isinstance(source_user, str) or not _SAFE_SSH_NAME.fullmatch(source_user):
        raise UnsafeSourceError("source_user contains unsafe characters")
    if (
        not isinstance(source_path, str)
        or not source_path.startswith("/")
        or "\x00" in source_path
        or "\n" in source_path
        or "\r" in source_path
    ):
        raise UnsafeSourceError("source_path must be an absolute remote path without controls")
    identity = _validated_regular_file(identity_file, "SSH identity")
    known_hosts = _validated_regular_file(known_hosts_file, "SSH known_hosts file")
    return identity, known_hosts


def _validate_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise IOError("manifest relative_path must be non-empty text")
    if len(value.encode("utf-8")) > 16 * 1024:
        raise IOError("manifest relative_path is too long")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
        or "\\" in value
        or "\x00" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise IOError(f"unsafe manifest relative_path: {value!r}")
    return value


def _strict_int(frame: dict[str, Any], name: str, *, maximum: int) -> int:
    value = frame.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise IOError(f"manifest {name} must be an integer")
    if value < 0 or value > maximum:
        raise IOError(f"manifest {name} is outside allowed bounds")
    return value


def _parse_remote_manifest(data: bytes, limits: ArchiveLimits) -> dict[str, Any]:
    """Parse the complete bounded NDJSON protocol, rejecting ambiguity."""

    if len(data) > limits.max_manifest_bytes:
        raise IOError("remote manifest exceeds byte limit")
    if not data.endswith(b"\n"):
        raise IOError("remote manifest is incomplete or has trailing data")
    raw_lines = data.split(b"\n")
    raw_lines.pop()
    if not raw_lines:
        raise IOError("remote manifest is empty")

    frames: list[dict[str, Any]] = []
    for raw_line in raw_lines:
        if not raw_line:
            raise IOError("remote manifest contains an empty frame")
        try:
            value = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise IOError("remote manifest contains invalid JSON") from error
        if not isinstance(value, dict):
            raise IOError("remote manifest frame must be an object")
        frames.append(value)

    if frames[0] != {"type": "header", "schema_version": 1}:
        raise IOError("invalid remote manifest header")

    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    seen_folded: dict[str, str] = {}
    complete: dict[str, Any] | None = None
    previous_key: tuple[str, str] | None = None
    for index, frame in enumerate(frames[1:], start=1):
        frame_type = frame.get("type")
        if complete is not None:
            raise IOError("remote manifest contains trailing frames")
        if frame_type == "complete":
            if index != len(frames) - 1:
                raise IOError("remote manifest contains trailing frames")
            complete = frame
            continue
        if frame_type not in {"directory", "file"}:
            raise IOError(f"unknown remote manifest frame type: {frame_type!r}")
        expected_keys = {"type", "relative_path", "mode", "mtime_seconds"}
        if frame_type == "file":
            expected_keys.update({"size", "sha256"})
        if set(frame) != expected_keys:
            raise IOError("remote manifest entry has unexpected or missing fields")
        relative_path = _validate_relative_path(frame["relative_path"])
        if relative_path in seen:
            raise IOError(f"duplicate remote manifest path: {relative_path}")
        folded = relative_path.casefold()
        if folded in seen_folded:
            raise IOError(
                f"case-folding collision in remote manifest: {seen_folded[folded]} and {relative_path}"
            )
        key = (folded, relative_path)
        if previous_key is not None and key <= previous_key:
            raise IOError("remote manifest entries are not deterministically sorted")
        previous_key = key
        seen.add(relative_path)
        seen_folded[folded] = relative_path
        mode = _strict_int(frame, "mode", maximum=0o7777)
        mtime_seconds = _strict_int(
            frame, "mtime_seconds", maximum=(1 << 63) - 1
        )
        normalized: dict[str, Any] = {
            "type": frame_type,
            "relative_path": relative_path,
            "mode": mode,
            "mtime_seconds": mtime_seconds,
        }
        if frame_type == "file":
            size = _strict_int(frame, "size", maximum=limits.max_total_bytes)
            if size > limits.max_file_bytes:
                raise IOError(f"manifest file size limit exceeded: {relative_path}")
            digest = frame["sha256"]
            if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                raise IOError(f"invalid SHA-256 in remote manifest: {relative_path}")
            normalized.update(size=size, sha256=digest)
        entries.append(normalized)
        if len(entries) > limits.max_entries:
            raise IOError("remote manifest entry count exceeds limit")

    if complete is None:
        raise IOError("remote manifest has no completion frame")
    if set(complete) != {"type", "entry_count", "file_count", "total_bytes"}:
        raise IOError("remote manifest completion frame has invalid fields")
    entry_count = _strict_int(complete, "entry_count", maximum=limits.max_entries)
    file_count = _strict_int(complete, "file_count", maximum=limits.max_entries)
    total_bytes = _strict_int(
        complete, "total_bytes", maximum=limits.max_total_bytes
    )
    actual_files = [entry for entry in entries if entry["type"] == "file"]
    actual_total = sum(entry["size"] for entry in actual_files)
    if entry_count != len(entries) or file_count != len(actual_files) or total_bytes != actual_total:
        raise IOError("remote manifest completion totals do not match entries")

    files = {entry["relative_path"] for entry in actual_files}
    for entry in entries:
        parts = PurePosixPath(entry["relative_path"]).parts
        for offset in range(1, len(parts)):
            if "/".join(parts[:offset]) in files:
                raise IOError("remote manifest places an entry below a file")
    return {
        "entries": entries,
        "entry_count": entry_count,
        "file_count": file_count,
        "total_bytes": total_bytes,
    }


def _ssh_arguments(
    *,
    ssh_executable: str,
    identity: Path,
    known_hosts: Path,
    connect_timeout_seconds: int,
    io_timeout_seconds: int,
) -> list[str]:
    connect_timeout = _positive_int(
        "connect timeout", connect_timeout_seconds, maximum=86_400
    )
    io_timeout = _positive_int("I/O timeout", io_timeout_seconds, maximum=86_400)
    alive_interval = max(1, min(io_timeout, 30))
    return [
        ssh_executable,
        "-F",
        "/dev/null",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        f"ConnectTimeout={connect_timeout}",
        "-o",
        f"ServerAliveInterval={alive_interval}",
        "-o",
        "ServerAliveCountMax=3",
        "-i",
        str(identity),
    ]


def _run_remote_manifest(
    *,
    ssh_args: list[str],
    source_user: str,
    source_host: str,
    source_path: str,
    limits: ArchiveLimits,
    timeout_seconds: float,
    max_stderr_bytes: int,
) -> dict[str, Any]:
    remote_command = "{} -dimsu {} -c {} {} {} {} {}".format(
        shlex.quote("/usr/bin/caffeinate"),
        shlex.quote("/usr/bin/python"),
        shlex.quote(_REMOTE_MANIFEST_SCRIPT),
        shlex.quote(source_path),
        limits.max_entries,
        limits.max_file_bytes,
        limits.max_total_bytes,
    )
    command = [*ssh_args, "--", f"{source_user}@{source_host}", remote_command]
    result = _run_bounded(
        command,
        timeout_seconds=timeout_seconds,
        stdout_limit=limits.max_manifest_bytes,
        stderr_limit=max_stderr_bytes,
    )
    detail = _error_detail(result)
    if result.returncode != 0:
        unsafe_markers = (
            "symlink is not permitted",
            "source is not a non-symlink directory",
            "non-regular source entry is not permitted",
            "source directory changed",
            "source file changed",
            "source root changed",
        )
        if any(marker in detail for marker in unsafe_markers):
            raise UnsafeSourceError(f"remote source rejected{detail}")
        if _is_transport_failure(detail):
            raise ArchiveTransportError(f"remote manifest transport failed{detail}")
        raise IOError(f"remote manifest failed with exit {result.returncode}{detail}")
    if result.stdout_truncated:
        raise IOError("remote manifest exceeds byte limit")
    return _parse_remote_manifest(result.stdout, limits)


def _open_directory_path_no_symlinks(
    path: str | Path, *, create_missing: bool = True
) -> tuple[Path, int]:
    absolute = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
    parts = absolute.parts
    current_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in parts[1:]:
            try:
                info = os.stat(component, dir_fd=current_fd, follow_symlinks=False)
            except FileNotFoundError as error:
                if not create_missing:
                    raise UnsafeSourceError(
                        f"archive root and every ancestor must already exist: {absolute}"
                    ) from error
                os.mkdir(component, 0o755, dir_fd=current_fd)
                info = os.stat(component, dir_fd=current_fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise UnsafeSourceError(f"archive root contains a symlink: {absolute}")
            if not stat.S_ISDIR(info.st_mode):
                raise UnsafeSourceError(f"archive root component is not a directory: {absolute}")
            try:
                next_fd = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=current_fd,
                )
            except OSError as error:
                if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise UnsafeSourceError(
                        f"archive root contains a symlink or non-directory: {absolute}"
                    ) from error
                raise
            os.close(current_fd)
            current_fd = next_fd
        _assert_absolute_path_fd(absolute, current_fd, "archive root")
        return absolute, current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _assert_absolute_path_fd(path: Path, descriptor: int, label: str) -> None:
    try:
        named = path.lstat()
    except FileNotFoundError as error:
        raise UnsafeSourceError(f"{label} disappeared") from error
    opened = os.fstat(descriptor)
    if (
        stat.S_ISLNK(named.st_mode)
        or not stat.S_ISDIR(named.st_mode)
        or named.st_dev != opened.st_dev
        or named.st_ino != opened.st_ino
    ):
        raise UnsafeSourceError(f"{label} was replaced or redirected")


def _assert_named_directory_fd(
    parent_fd: int, name: str, descriptor: int, label: str
) -> None:
    try:
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError as error:
        raise UnsafeSourceError(f"{label} disappeared") from error
    opened = os.fstat(descriptor)
    if (
        stat.S_ISLNK(named.st_mode)
        or not stat.S_ISDIR(named.st_mode)
        or named.st_dev != opened.st_dev
        or named.st_ino != opened.st_ino
    ):
        raise UnsafeSourceError(f"{label} was replaced or redirected")


def _exists_at(parent_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def _rename_no_replace(
    source_fd: int, source_name: str, destination_fd: int, destination_name: str
) -> None:
    """Atomically rename while refusing an existing destination."""

    source_bytes = os.fsencode(source_name)
    destination_bytes = os.fsencode(destination_name)
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        function = libc.renameatx_np
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        function.restype = ctypes.c_int
        result = function(
            source_fd,
            source_bytes,
            destination_fd,
            destination_bytes,
            0x00000004,  # RENAME_EXCL
        )
    elif hasattr(libc, "renameat2"):
        function = libc.renameat2
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        function.restype = ctypes.c_int
        result = function(
            source_fd, source_bytes, destination_fd, destination_bytes, 1
        )  # RENAME_NOREPLACE
    else:  # pragma: no cover - fail closed on unsupported platforms
        raise OSError(errno.ENOTSUP, "atomic no-replace rename is unavailable")
    if result != 0:
        error_number = ctypes.get_errno()
        if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
            raise FileExistsError(error_number, os.strerror(error_number), destination_name)
        raise OSError(error_number, os.strerror(error_number), destination_name)


def _verify_archive_filesystem(root_fd: int) -> None:
    """Probe no-replace and directory durability on the target filesystem."""

    token = uuid.uuid4().hex
    source = f".archive-probe-source-{token}"
    existing = f".archive-probe-existing-{token}"
    moved = f".archive-probe-moved-{token}"
    created: set[str] = set()
    try:
        os.mkdir(source, 0o700, dir_fd=root_fd)
        created.add(source)
        os.mkdir(existing, 0o700, dir_fd=root_fd)
        created.add(existing)
        source_info = os.stat(source, dir_fd=root_fd, follow_symlinks=False)
        existing_info = os.stat(existing, dir_fd=root_fd, follow_symlinks=False)
        try:
            _rename_no_replace(root_fd, source, root_fd, existing)
        except FileExistsError:
            pass
        else:
            raise OSError(errno.ENOTSUP, "filesystem clobbered a no-replace probe")
        source_after = os.stat(source, dir_fd=root_fd, follow_symlinks=False)
        existing_after = os.stat(existing, dir_fd=root_fd, follow_symlinks=False)
        if (source_info.st_dev, source_info.st_ino) != (
            source_after.st_dev,
            source_after.st_ino,
        ) or (existing_info.st_dev, existing_info.st_ino) != (
            existing_after.st_dev,
            existing_after.st_ino,
        ):
            raise OSError(errno.ENOTSUP, "filesystem no-replace probe was not stable")
        _rename_no_replace(root_fd, source, root_fd, moved)
        created.discard(source)
        created.add(moved)
        os.fsync(root_fd)
    finally:
        for name in tuple(created):
            try:
                os.rmdir(name, dir_fd=root_fd)
            except FileNotFoundError:
                pass
        os.fsync(root_fd)


def _scan_local_tree(root_fd: int, limits: ArchiveLimits) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []

    def visit(directory_fd: int, prefix: str) -> None:
        try:
            names = os.listdir(directory_fd)
        except OSError as error:
            raise UnsafeSourceError(f"cannot list staged directory: {prefix or '.'}") from error
        names.sort(key=lambda value: (value.casefold(), value))
        for name in names:
            relative = name if not prefix else f"{prefix}/{name}"
            _validate_relative_path(relative)
            before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISLNK(before.st_mode):
                raise UnsafeSourceError(f"symlink is not permitted in staged payload: {relative}")
            if stat.S_ISDIR(before.st_mode):
                child_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
                try:
                    opened = os.fstat(child_fd)
                    if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                        raise UnsafeSourceError(f"staged directory changed while opening: {relative}")
                    entries.append(
                        {
                            "type": "directory",
                            "relative_path": relative,
                            "mode": stat.S_IMODE(opened.st_mode),
                            "mtime_seconds": int(opened.st_mtime),
                        }
                    )
                    visit(child_fd, relative)
                finally:
                    os.close(child_fd)
                after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if (
                    not stat.S_ISDIR(after.st_mode)
                    or (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
                    or stat.S_IMODE(after.st_mode) != stat.S_IMODE(opened.st_mode)
                    or int(after.st_mtime) != int(opened.st_mtime)
                ):
                    raise UnsafeSourceError(
                        f"staged directory changed during rewalk: {relative}"
                    )
            elif stat.S_ISREG(before.st_mode):
                file_fd = os.open(
                    name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd
                )
                try:
                    opened = os.fstat(file_fd)
                    if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                        raise UnsafeSourceError(f"staged file changed while opening: {relative}")
                    if opened.st_size > limits.max_file_bytes:
                        raise IOError(f"staged file size limit exceeded: {relative}")
                    digest = hashlib.sha256()
                    while True:
                        chunk = os.read(file_fd, 1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                    after = os.fstat(file_fd)
                    if (
                        opened.st_dev,
                        opened.st_ino,
                        opened.st_mode,
                        opened.st_size,
                        int(opened.st_mtime),
                    ) != (
                        after.st_dev,
                        after.st_ino,
                        after.st_mode,
                        after.st_size,
                        int(after.st_mtime),
                    ):
                        raise IOError(f"staged file changed while hashing: {relative}")
                finally:
                    os.close(file_fd)
                named_after = os.stat(
                    name, dir_fd=directory_fd, follow_symlinks=False
                )
                if (
                    not stat.S_ISREG(named_after.st_mode)
                    or (named_after.st_dev, named_after.st_ino)
                    != (opened.st_dev, opened.st_ino)
                    or named_after.st_mode != opened.st_mode
                    or named_after.st_size != opened.st_size
                    or int(named_after.st_mtime) != int(opened.st_mtime)
                ):
                    raise UnsafeSourceError(
                        f"staged file changed during rewalk: {relative}"
                    )
                entries.append(
                    {
                        "type": "file",
                        "relative_path": relative,
                        "mode": stat.S_IMODE(opened.st_mode),
                        "mtime_seconds": int(opened.st_mtime),
                        "size": opened.st_size,
                        "sha256": digest.hexdigest(),
                    }
                )
            else:
                raise UnsafeSourceError(
                    f"non-regular entry is not permitted in staged payload: {relative}"
                )
            if len(entries) > limits.max_entries:
                raise IOError("staged entry count exceeds limit")

    visit(root_fd, "")
    entries.sort(key=lambda value: (value["relative_path"].casefold(), value["relative_path"]))
    folded: dict[str, str] = {}
    for entry in entries:
        key = entry["relative_path"].casefold()
        if key in folded:
            raise UnsafeSourceError(
                f"case-folding collision in staged payload: {folded[key]} and {entry['relative_path']}"
            )
        folded[key] = entry["relative_path"]
    files = [entry for entry in entries if entry["type"] == "file"]
    total_bytes = sum(entry["size"] for entry in files)
    if total_bytes > limits.max_total_bytes:
        raise IOError("staged total bytes exceeds limit")
    return {
        "entries": entries,
        "entry_count": len(entries),
        "file_count": len(files),
        "total_bytes": total_bytes,
    }


def _fsync_tree(directory_fd: int) -> None:
    for name in os.listdir(directory_fd):
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            raise UnsafeSourceError("symlink appeared while syncing staged payload")
        if stat.S_ISDIR(info.st_mode):
            child_fd = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            try:
                _fsync_tree(child_fd)
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(info.st_mode):
            file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
            try:
                os.fsync(file_fd)
            finally:
                os.close(file_fd)
        else:
            raise UnsafeSourceError("non-regular entry appeared while syncing staged payload")
    os.fsync(directory_fd)


def _write_manifest(staging_fd: int, manifest: dict[str, Any]) -> str:
    payload = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(
        "manifest.json",
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=staging_fd,
    )
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return hashlib.sha256(payload).hexdigest()


def _content_manifest_sha256(
    directories: list[dict[str, Any]], files: list[dict[str, Any]]
) -> str:
    payload = json.dumps(
        {"directories": directories, "files": files},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _clear_directory_descriptor(directory_fd: int) -> None:
    """Remove only entries reached through a held, verified directory descriptor."""

    for name in os.listdir(directory_fd):
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(before.st_mode) and not stat.S_ISLNK(before.st_mode):
            child_fd = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            try:
                opened = os.fstat(child_fd)
                if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                    raise UnsafeSourceError("cleanup directory changed while opening")
                _clear_directory_descriptor(child_fd)
                _assert_named_directory_fd(
                    directory_fd, name, child_fd, "cleanup directory"
                )
                os.rmdir(name, dir_fd=directory_fd)
            finally:
                os.close(child_fd)
        else:
            after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if (before.st_dev, before.st_ino, before.st_mode) != (
                after.st_dev,
                after.st_ino,
                after.st_mode,
            ):
                raise UnsafeSourceError("cleanup entry was replaced")
            os.unlink(name, dir_fd=directory_fd)
    os.fsync(directory_fd)


def _cleanup_staging(
    root_fd: int, name: str, descriptor: int | None = None
) -> bool:
    """Clean a held archive only while its root name still names that inode."""

    if not _exists_at(root_fd, name):
        return False
    if descriptor is not None:
        try:
            _assert_named_directory_fd(root_fd, name, descriptor, "cleanup archive")
        except UnsafeSourceError:
            return False
        _clear_directory_descriptor(descriptor)
        try:
            _assert_named_directory_fd(root_fd, name, descriptor, "cleanup archive")
        except UnsafeSourceError:
            return False
        os.rmdir(name, dir_fd=root_fd)
        os.fsync(root_fd)
        return True
    info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        os.unlink(name, dir_fd=root_fd)
    else:
        shutil.rmtree(name, dir_fd=root_fd)
    os.fsync(root_fd)
    return True


def _same_manifest(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        left["entries"] == right["entries"]
        and left["entry_count"] == right["entry_count"]
        and left["file_count"] == right["file_count"]
        and left["total_bytes"] == right["total_bytes"]
    )


def _resume_state_path(staging_fd: int) -> str:
    return "resume_state.json"


def _write_resume_state(
    staging_fd: int,
    *,
    source_id: str,
    source_host: str,
    source_user: str,
    source_path: str,
    attempt: int,
) -> None:
    """Record which source a staging tree belongs to, for resumable attempts."""

    payload = (
        json.dumps(
            {
                "schema_version": 1,
                "source_id": source_id,
                "source_host": source_host,
                "source_user": source_user,
                "source_path": source_path,
                "attempt": attempt,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    descriptor = os.open(
        _resume_state_path(staging_fd),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
        0o600,
        dir_fd=staging_fd,
    )
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_resume_state(staging_fd: int) -> dict[str, Any]:
    """Read the staging tree's resume state, refusing anything unexpected."""

    try:
        info = os.stat(_resume_state_path(staging_fd), dir_fd=staging_fd, follow_symlinks=False)
    except FileNotFoundError as error:
        raise UnsafeSourceError(
            "existing staging directory has no resume state; refusing to reuse it"
        ) from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise UnsafeSourceError("existing staging resume state is not a regular file")
    descriptor = os.open(
        _resume_state_path(staging_fd),
        os.O_RDONLY | os.O_NOFOLLOW,
        dir_fd=staging_fd,
    )
    try:
        raw = b""
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            raw += chunk
    finally:
        os.close(descriptor)
    try:
        state = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise UnsafeSourceError("existing staging resume state is not valid JSON") from error
    if not isinstance(state, dict) or state.get("schema_version") != 1:
        raise UnsafeSourceError("existing staging resume state has an unsupported schema")
    return state


def _open_existing_staging(
    root_fd: int,
    staging_name: str,
    source_id: str,
    source_host: str,
    source_user: str,
    source_path: str,
) -> int:
    """Reopen a preserved staging tree and prove it belongs to this source."""

    info = os.stat(staging_name, dir_fd=root_fd, follow_symlinks=False)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise UnsafeSourceError("existing staging path is not a directory")
    staging_fd = os.open(
        staging_name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        dir_fd=root_fd,
    )
    try:
        state = _read_resume_state(staging_fd)
    except BaseException:
        os.close(staging_fd)
        raise
    expected = {
        "source_id": source_id,
        "source_host": source_host,
        "source_user": source_user,
        "source_path": source_path,
    }
    for key, value in expected.items():
        if state.get(key) != value:
            os.close(staging_fd)
            raise UnsafeSourceError(
                "existing staging directory belongs to a different source; "
                f"expected {key}={value!r}, found {state.get(key)!r}"
            )
    attempt = state.get("attempt")
    if isinstance(attempt, int) and not isinstance(attempt, bool):
        _write_resume_state(
            staging_fd,
            source_id=source_id,
            source_host=source_host,
            source_user=source_user,
            source_path=source_path,
            attempt=attempt + 1,
        )
    return staging_fd


def _remove_resume_state(staging_fd: int) -> None:
    """Drop the resume state so a published archive carries only its manifest."""

    try:
        os.unlink(_resume_state_path(staging_fd), dir_fd=staging_fd)
    except FileNotFoundError:
        return
    os.fsync(staging_fd)


def _open_staging_payload(staging_fd: int) -> int:
    """Open the staging payload directory, refusing a redirected payload."""

    info = os.stat("payload", dir_fd=staging_fd, follow_symlinks=False)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise UnsafeSourceError("existing staging payload is not a directory")
    payload_fd = os.open(
        "payload",
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        dir_fd=staging_fd,
    )
    try:
        opened = os.fstat(payload_fd)
        if (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
            raise UnsafeSourceError("existing staging payload changed while opening")
    except BaseException:
        os.close(payload_fd)
        raise
    return payload_fd


def archive_ssh_source(
    archive_root: str | Path,
    *,
    source_id: str,
    source_host: str,
    source_user: str,
    source_path: str,
    identity_file: str | Path,
    known_hosts_file: str | Path,
    expected_filesystem_type: str,
    ssh_executable: str = "ssh",
    rsync_executable: str = "/usr/bin/rsync",
    limits: ArchiveLimits | None = None,
    connect_timeout_seconds: int = 15,
    io_timeout_seconds: int = 300,
    operation_timeout_seconds: int | float = 172_800,
    max_stderr_bytes: int = _DEFAULT_MAX_STDERR_BYTES,
) -> dict[str, Any]:
    """Archive an SSH tree through rsync 2.6.9 with fail-closed verification."""

    _validate_source_id(source_id)
    identity, known_hosts = validate_ssh_source(
        source_host=source_host,
        source_user=source_user,
        source_path=source_path,
        identity_file=identity_file,
        known_hosts_file=known_hosts_file,
    )
    active_limits = limits or ArchiveLimits()
    if not isinstance(active_limits, ArchiveLimits):
        raise ValueError("limits must be an ArchiveLimits instance")
    operation_timeout = _positive_timeout("operation", operation_timeout_seconds)
    max_stderr = _positive_int(
        "max_stderr_bytes", max_stderr_bytes, maximum=1 << 20
    )
    ssh_args = _ssh_arguments(
        ssh_executable=ssh_executable,
        identity=identity,
        known_hosts=known_hosts,
        connect_timeout_seconds=connect_timeout_seconds,
        io_timeout_seconds=io_timeout_seconds,
    )

    if not isinstance(expected_filesystem_type, str):
        raise ValueError("expected filesystem type must be apfs")
    expected_type = expected_filesystem_type.casefold()
    if expected_type != "apfs":
        raise UnsafeSourceError(
            "SSH archive filesystem type must be explicitly set to apfs; SMB is rejected"
        )
    root_path, root_fd = _open_directory_path_no_symlinks(
        archive_root, create_missing=False
    )
    # A stable staging name lets a later attempt resume the same partial copy
    # instead of re-transferring the whole source from byte zero.
    staging_name = f".{source_id}.partial"
    staging_fd: int | None = None
    payload_fd: int | None = None
    published_name: str | None = None
    primary_error: BaseException | None = None
    preserve_staging = False
    try:
        mount_identity = _filesystem_identity(root_fd)
        if mount_identity.filesystem_type != expected_type:
            raise UnsafeSourceError(
                "archive root filesystem type mismatch: "
                f"expected {expected_type}, found {mount_identity.filesystem_type}"
            )
        try:
            fcntl.flock(root_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise UnsafeSourceError(
                "archive root is locked by another cooperative writer"
            ) from error
        _verify_archive_filesystem(root_fd)
        _assert_filesystem_identity(root_fd, mount_identity)
        if _exists_at(root_fd, source_id):
            raise ArchiveExistsError(f"archive already exists: {root_path / source_id}")
        resuming = _exists_at(root_fd, staging_name)
        if resuming:
            staging_fd = _open_existing_staging(
                root_fd, staging_name, source_id, source_host, source_user, source_path
            )
            payload_fd = _open_staging_payload(staging_fd)
        else:
            os.mkdir(staging_name, 0o700, dir_fd=root_fd)
            staging_fd = os.open(
                staging_name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=root_fd,
            )
            os.mkdir("payload", 0o700, dir_fd=staging_fd)
            payload_fd = os.open(
                "payload",
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=staging_fd,
            )
            _write_resume_state(
                staging_fd,
                source_id=source_id,
                source_host=source_host,
                source_user=source_user,
                source_path=source_path,
                attempt=1,
            )

        source_before = _run_remote_manifest(
            ssh_args=ssh_args,
            source_user=source_user,
            source_host=source_host,
            source_path=source_path,
            limits=active_limits,
            timeout_seconds=operation_timeout,
            max_stderr_bytes=max_stderr,
        )
        _assert_absolute_path_fd(root_path, root_fd, "archive root")
        _assert_named_directory_fd(root_fd, staging_name, staging_fd, "staging directory")
        _assert_named_directory_fd(staging_fd, "payload", payload_fd, "staging payload")
        _assert_filesystem_identity(root_fd, mount_identity)

        remote_shell = " ".join(shlex.quote(value) for value in ssh_args)
        remote_source = (
            f"{source_user}@{source_host}:"
            + shlex.quote(source_path.rstrip("/") + "/")
        )
        rsync_command = [
            rsync_executable,
            "-aE",
            "--partial",
            "--no-owner",
            "--no-group",
            "--rsync-path=/usr/bin/caffeinate -dimsu /usr/bin/rsync",
            f"--timeout={_positive_int('I/O timeout', io_timeout_seconds, maximum=86_400)}",
            "-e",
            remote_shell,
            "--",
            remote_source,
            ".",
        ]
        rsync_result = _run_bounded(
            rsync_command,
            timeout_seconds=operation_timeout,
            stdout_limit=1,
            stderr_limit=max_stderr,
            discard_stdout=True,
            working_directory_fd=payload_fd,
        )
        if rsync_result.returncode != 0:
            detail = _error_detail(rsync_result)
            if _is_transport_failure(detail):
                raise ArchiveTransportError(
                    f"rsync archive transfer was interrupted{detail}"
                )
            raise IOError(
                f"rsync archive transfer failed with exit {rsync_result.returncode}{detail}"
            )

        source_after = _run_remote_manifest(
            ssh_args=ssh_args,
            source_user=source_user,
            source_host=source_host,
            source_path=source_path,
            limits=active_limits,
            timeout_seconds=operation_timeout,
            max_stderr_bytes=max_stderr,
        )
        if not _same_manifest(source_before, source_after):
            raise IOError("source changed between pre-copy and final rewalk")

        _assert_absolute_path_fd(root_path, root_fd, "archive root")
        _assert_named_directory_fd(root_fd, staging_name, staging_fd, "staging directory")
        _assert_named_directory_fd(staging_fd, "payload", payload_fd, "staging payload")
        # An interrupted transfer can leave rsync's own in-progress temporary
        # files inside the payload. Remove them so the published tree
        # contains exactly the source content.
        _remove_rsync_temporaries(
            payload_fd,
            {entry["relative_path"] for entry in source_before["entries"]},
        )
        # Writing files and removing temporaries both change directory mtimes,
        # and rsync only restores them on a complete transfer, so a resumed
        # transfer must reapply the source directory times itself.
        _apply_directory_times(payload_fd, source_before["entries"])
        destination_manifest = _scan_local_tree(payload_fd, active_limits)
        if not _same_manifest(source_before, destination_manifest):
            raise IOError("staged destination manifest does not match stable source")

        files = []
        directories = []
        for entry in source_before["entries"]:
            if entry["type"] == "file":
                files.append(
                    {
                        "relative_path": entry["relative_path"],
                        "size": entry["size"],
                        "mode": entry["mode"],
                        "mtime_seconds": entry["mtime_seconds"],
                        "source_sha256": entry["sha256"],
                        "archive_sha256": entry["sha256"],
                    }
                )
            else:
                directories.append(
                    {
                        "relative_path": entry["relative_path"],
                        "mode": entry["mode"],
                        "mtime_seconds": entry["mtime_seconds"],
                    }
                )
        content_manifest_sha256 = _content_manifest_sha256(directories, files)
        manifest = {
            "schema_version": 4,
            "source_id": source_id,
            "source_kind": "ssh",
            "source_host": source_host,
            "source_user": source_user,
            "source_path": source_path,
            "provenance": {
                "transport": "rsync-over-ssh",
                "host": source_host,
                "user": source_user,
                "path": source_path,
                "known_hosts_file": str(known_hosts),
            },
            "destination_filesystem": {
                "type": mount_identity.filesystem_type,
                "filesystem_id": list(mount_identity.filesystem_id),
                "mounted_from": mount_identity.mounted_from,
                "mounted_on": mount_identity.mounted_on,
            },
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "file_count": source_before["file_count"],
            "directory_count": source_before["entry_count"] - source_before["file_count"],
            "total_bytes": source_before["total_bytes"],
            "metadata_verified": [
                "hierarchy",
                "type",
                "size",
                "data_fork_sha256",
                "posix_mode",
                "mtime_seconds",
            ],
            "metadata_best_effort_unverified": [
                "extended_attributes",
                "resource_forks",
                "access_control_lists",
            ],
            "content_manifest_sha256": content_manifest_sha256,
            "verified": True,
            "directories": directories,
            "files": files,
        }
        manifest_sha256 = _write_manifest(staging_fd, manifest)
        _remove_resume_state(staging_fd)
        _fsync_tree(payload_fd)
        os.fsync(staging_fd)
        os.fsync(root_fd)
        _assert_absolute_path_fd(root_path, root_fd, "archive root")
        _assert_named_directory_fd(root_fd, staging_name, staging_fd, "staging directory")
        _assert_filesystem_identity(root_fd, mount_identity)

        try:
            _rename_no_replace(root_fd, staging_name, root_fd, source_id)
        except FileExistsError as error:
            raise ArchiveExistsError(
                f"archive already exists: {root_path / source_id}"
            ) from error
        published_name = source_id
        os.fsync(root_fd)
        _assert_named_directory_fd(root_fd, source_id, staging_fd, "published archive")
        final_manifest = _scan_local_tree(payload_fd, active_limits)
        if not _same_manifest(source_before, final_manifest):
            raise IOError("published destination verification does not match source")
        os.fsync(root_fd)
        _assert_absolute_path_fd(root_path, root_fd, "archive root")
        _assert_filesystem_identity(root_fd, mount_identity)
        result = {
            "archive_path": str(root_path / source_id),
            "file_count": source_before["file_count"],
            "total_bytes": source_before["total_bytes"],
            "content_manifest_sha256": content_manifest_sha256,
            "manifest_sha256": manifest_sha256,
            "verified": True,
        }
        _assert_named_directory_fd(root_fd, source_id, staging_fd, "published archive")
        return result
    except BaseException as error:
        primary_error = error
        # A transport interruption says nothing about source integrity, so the
        # staged partial copy is kept for the next attempt to resume. Any
        # other failure (unsafe source, changed source, verification mismatch)
        # still removes the staging tree.
        preserve_staging = isinstance(error, (ArchiveTransportError, ArchiveTimeoutError))
        raise
    finally:
        cleanup_name = published_name or staging_name
        cleanup_error: BaseException | None = None
        if primary_error is not None and staging_fd is not None and not preserve_staging:
            try:
                _cleanup_staging(root_fd, cleanup_name, staging_fd)
            except BaseException as error:
                cleanup_error = error
        if payload_fd is not None:
            os.close(payload_fd)
        if staging_fd is not None:
            os.close(staging_fd)
        os.close(root_fd)
        if cleanup_error is not None:
            raise ArchiveCleanupError(
                f"archive failed and cleanup also failed: {cleanup_error}"
            ) from primary_error


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_files(source: Path) -> list[Path]:
    files: list[Path] = []
    for directory, directory_names, file_names in os.walk(source, followlinks=False):
        current = Path(directory)
        for name in directory_names:
            path = current / name
            if path.is_symlink():
                raise UnsafeSourceError(f"symlink is not permitted: {path}")
        for name in file_names:
            path = current / name
            if path.is_symlink():
                raise UnsafeSourceError(f"symlink is not permitted: {path}")
            if not path.is_file():
                raise UnsafeSourceError(f"non-regular file is not permitted: {path}")
            files.append(path)
    return sorted(
        files,
        key=lambda path: (
            path.relative_to(source).as_posix().casefold(),
            path.relative_to(source).as_posix(),
        ),
    )


def archive_source(
    source: str | Path,
    archive_root: str | Path,
    *,
    source_id: str,
) -> dict[str, Any]:
    """Copy one local source tree, verify hashes, and publish without clobbering."""

    _validate_source_id(source_id)
    source_path = Path(source).resolve()
    root_path = Path(archive_root).absolute()
    root_resolved = root_path.resolve()
    try:
        root_resolved.relative_to(source_path)
    except ValueError:
        pass
    else:
        raise UnsafeSourceError("archive destination cannot be inside the source tree")
    if not source_path.is_dir():
        raise UnsafeSourceError(f"source is not a directory: {source_path}")
    source_files = _source_files(source_path)

    root_path, root_fd = _open_directory_path_no_symlinks(root_path)
    staging_name = f".{source_id}.partial-{uuid.uuid4().hex}"
    staging = root_path / staging_name
    published = False
    try:
        _verify_archive_filesystem(root_fd)
        if _exists_at(root_fd, source_id):
            raise ArchiveExistsError(f"archive already exists: {root_path / source_id}")
        os.mkdir(staging_name, 0o700, dir_fd=root_fd)
        staging_payload = staging / "payload"
        staging_payload.mkdir()
        manifest_files: list[dict[str, Any]] = []
        for source_file in source_files:
            relative = source_file.relative_to(source_path)
            archive_file = staging_payload / relative
            archive_file.parent.mkdir(parents=True, exist_ok=True)
            source_hash = _sha256(source_file)
            shutil.copy2(source_file, archive_file)
            archive_hash = _sha256(archive_file)
            if source_hash != archive_hash:
                raise IOError(f"checksum mismatch after copy: {relative.as_posix()}")
            manifest_files.append(
                {
                    "relative_path": relative.as_posix(),
                    "size": source_file.stat().st_size,
                    "source_sha256": source_hash,
                    "archive_sha256": archive_hash,
                }
            )
        manifest = {
            "schema_version": 1,
            "source_id": source_id,
            "source_path": str(source_path),
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "file_count": len(manifest_files),
            "total_bytes": sum(entry["size"] for entry in manifest_files),
            "verified": True,
            "files": manifest_files,
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        try:
            _rename_no_replace(root_fd, staging_name, root_fd, source_id)
        except FileExistsError as error:
            raise ArchiveExistsError(
                f"archive already exists: {root_path / source_id}"
            ) from error
        published = True
        os.fsync(root_fd)
        return {
            "archive_path": str(root_path / source_id),
            "file_count": len(manifest_files),
            "total_bytes": sum(entry["size"] for entry in manifest_files),
            "verified": True,
        }
    finally:
        if not published and _exists_at(root_fd, staging_name):
            _cleanup_staging(root_fd, staging_name)
        os.close(root_fd)
