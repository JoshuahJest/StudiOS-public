"""Bounded, read-only DAAP/DMAP metadata parsing and snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import ipaddress
import re
import struct
import time
from typing import Callable, Final, Mapping, Sequence
from urllib.parse import urlencode, urlsplit


class DaapError(Exception):
    """Base error for a DAAP operation that cannot be trusted as complete."""


class DmapProtocolError(DaapError):
    """The server response is malformed or exceeds a declared parser limit."""


class DaapTimeoutError(DaapError):
    """A request or the overall snapshot exceeded its deadline."""


_CONTAINER_TAGS: Final[frozenset[str]] = frozenset(
    {
        "msrv",  # server info response
        "mlog",  # login response
        "avdb",  # databases response
        "aply",  # containers response
        "apso",  # container items response
        "mlcl",  # listing container
        "mlit",  # listing item
    }
)


@dataclass(frozen=True)
class DmapLimits:
    """Hard parser budgets for untrusted DMAP responses."""

    max_response_bytes: int = 2 * 1024 * 1024
    max_value_bytes: int = 512 * 1024
    max_frames: int = 50_000
    max_depth: int = 12

    def __post_init__(self) -> None:
        for name in ("max_response_bytes", "max_value_bytes", "max_frames", "max_depth"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class DmapFrame:
    """One DMAP TLV frame; containers expose children, leaves expose bytes."""

    tag: str
    value: bytes | None = None
    children: tuple["DmapFrame", ...] = ()


def parse_dmap(data: bytes, limits: DmapLimits | None = None) -> tuple[DmapFrame, ...]:
    """Parse a complete DMAP TLV response, rejecting truncation and budget overflow."""

    active_limits = limits or DmapLimits()
    if len(data) > active_limits.max_response_bytes:
        raise DmapProtocolError("DMAP response byte limit exceeded")
    frame_count = [0]
    return _parse_frames(data, active_limits, depth=0, frame_count=frame_count)


def _parse_frames(
    data: bytes,
    limits: DmapLimits,
    *,
    depth: int,
    frame_count: list[int],
) -> tuple[DmapFrame, ...]:
    if depth > limits.max_depth:
        raise DmapProtocolError("DMAP depth limit exceeded")
    frames: list[DmapFrame] = []
    offset = 0
    while offset < len(data):
        remaining = len(data) - offset
        if remaining < 8:
            raise DmapProtocolError("truncated DMAP frame header")
        tag_bytes = data[offset : offset + 4]
        try:
            tag = tag_bytes.decode("ascii")
        except UnicodeDecodeError as error:
            raise DmapProtocolError("DMAP tag is not ASCII") from error
        length = struct.unpack(">I", data[offset + 4 : offset + 8])[0]
        offset += 8
        if length > len(data) - offset:
            raise DmapProtocolError("DMAP declared length exceeds remaining response")
        frame_count[0] += 1
        if frame_count[0] > limits.max_frames:
            raise DmapProtocolError("DMAP frame limit exceeded")
        raw_value = data[offset : offset + length]
        offset += length
        if tag in _CONTAINER_TAGS:
            children = _parse_frames(
                raw_value,
                limits,
                depth=depth + 1,
                frame_count=frame_count,
            )
            frames.append(DmapFrame(tag=tag, children=children))
        else:
            if length > limits.max_value_bytes:
                raise DmapProtocolError("DMAP value length limit exceeded")
            frames.append(DmapFrame(tag=tag, value=raw_value))
    return tuple(frames)


@dataclass(frozen=True)
class HttpResponse:
    """A bounded HTTP response returned by the injectable read-only transport."""

    body: bytes
    headers: Mapping[str, str]
    status: int = 200


@dataclass(frozen=True)
class SnapshotLimits:
    """Network and collection limits for one snapshot."""

    request_timeout_seconds: float = 5.0
    total_timeout_seconds: float = 30.0
    max_response_bytes: int = 2 * 1024 * 1024
    max_containers: int = 100
    max_items_per_container: int = 100
    max_frames: int = 50_000
    max_depth: int = 12

    def __post_init__(self) -> None:
        for name in ("request_timeout_seconds", "total_timeout_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{name} must be positive")
        for name in (
            "max_response_bytes",
            "max_containers",
            "max_items_per_container",
            "max_frames",
            "max_depth",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


Transport = Callable[[str, float, int], HttpResponse]


class DaapClient:
    """A GET-only client restricted to the metadata endpoints used by snapshots."""

    def __init__(
        self,
        base_url: str,
        *,
        limits: SnapshotLimits | None = None,
        transport: Transport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme != "http" or parsed.username or parsed.password:
            raise ValueError("DAAP base URL must be unauthenticated HTTP")
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment or not parsed.hostname:
            raise ValueError("DAAP base URL must contain only an origin")
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError as error:
            raise ValueError("DAAP host must be a literal private-LAN IP address") from error
        if not (address.is_private or address.is_loopback or address.is_link_local):
            raise ValueError("DAAP host must be a private-LAN IP address")
        self.base_url = base_url.rstrip("/")
        self.limits = limits or SnapshotLimits()
        self._transport = transport
        self._clock = clock
        self._started = clock()

    def request(self, path: str, query: Sequence[tuple[str, str]]) -> HttpResponse:
        """Perform one allowlisted GET, bounded by request and snapshot deadlines."""

        self._validate_endpoint(path, query)
        remaining = self.limits.total_timeout_seconds - (self._clock() - self._started)
        if remaining <= 0:
            raise DaapTimeoutError("total snapshot deadline exceeded")
        timeout = min(float(self.limits.request_timeout_seconds), remaining)
        url = f"{self.base_url}{path}"
        if query:
            url += "?" + urlencode(query)
        if self._transport is None:
            raise DaapError("no DAAP transport configured")
        try:
            response = self._transport(url, timeout, self.limits.max_response_bytes)
        except (TimeoutError, OSError) as error:
            if isinstance(error, TimeoutError):
                raise DaapTimeoutError(str(error) or "DAAP request timed out") from error
            raise DaapError(f"DAAP transport failed: {error}") from error
        if self._clock() - self._started > self.limits.total_timeout_seconds:
            raise DaapTimeoutError("total snapshot deadline exceeded")
        if response.status != 200:
            raise DaapError(f"HTTP status {response.status} from allowlisted DAAP endpoint")
        if len(response.body) > self.limits.max_response_bytes:
            raise DmapProtocolError("DMAP response byte limit exceeded")
        return response

    @staticmethod
    def _validate_endpoint(path: str, query: Sequence[tuple[str, str]]) -> None:
        keys = tuple(name for name, _value in query)
        allowed = False
        if path == "/server-info" and keys == ():
            allowed = True
        elif path == "/login" and tuple(query) == (("pairing-guid", "0x1"),):
            allowed = True
        elif path == "/databases" and keys == ("session-id",):
            allowed = True
        elif re.fullmatch(r"/databases/[1-9][0-9]*/containers", path) and keys == (
            "session-id",
            "revision-number",
        ):
            allowed = True
        elif re.fullmatch(r"/databases/[1-9][0-9]*/containers/[1-9][0-9]*/items", path) and keys == (
            "session-id",
            "revision-number",
            "meta",
        ):
            allowed = True
        if not allowed:
            raise DaapError("DAAP endpoint is not in the exact read-only allowlist")


def snapshot_daap(
    client: DaapClient,
    *,
    database_id: int,
    container_ids: Sequence[int] = (),
) -> dict[str, object]:
    """Negotiate a bounded metadata snapshot; item requests require explicit IDs."""

    if isinstance(database_id, bool) or not isinstance(database_id, int) or database_id <= 0:
        raise ValueError("database_id must be a positive integer")
    requested_container_ids: list[int] = []
    for value in container_ids:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("container_ids must contain positive integers")
        if value in requested_container_ids:
            raise ValueError("container_ids must not contain duplicates")
        requested_container_ids.append(value)
    parser_limits = DmapLimits(
        max_response_bytes=client.limits.max_response_bytes,
        max_frames=client.limits.max_frames,
        max_depth=client.limits.max_depth,
    )
    server_response = client.request("/server-info", ())
    server_root = _response_root(server_response.body, "msrv", parser_limits)
    server = {
        "name": _required_text(server_root, "minm"),
        "software": _header(server_response.headers, "DAAP-Server"),
    }

    login_root = _response_root(
        client.request("/login", (("pairing-guid", "0x1"),)).body,
        "mlog",
        parser_limits,
    )
    session_id = _required_uint(login_root, "mlid")

    databases_root = _response_root(
        client.request("/databases", (("session-id", str(session_id)),)).body,
        "avdb",
        parser_limits,
    )
    database_frames = _listing_items(databases_root)
    database_frame = next(
        (frame for frame in database_frames if _required_uint(frame, "miid") == database_id),
        None,
    )
    if database_frame is None:
        raise DmapProtocolError(f"requested database {database_id} was not returned")
    database = {
        "id": database_id,
        "name": _required_text(database_frame, "minm"),
        "item_count": _optional_uint(database_frame, "mimc"),
        "reported_container_count": _optional_uint(database_frame, "mctc"),
    }

    try:
        containers_response = client.request(
            f"/databases/{database_id}/containers",
            (("session-id", str(session_id)), ("revision-number", "1")),
        )
        containers_root = _response_root(containers_response.body, "aply", parser_limits)
    except DaapError as error:
        error_type = "timeout" if isinstance(error, DaapTimeoutError) else "protocol"
        return {
            "schema_version": 1,
            "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "status": "incomplete",
            "complete": False,
            "errors": [{"stage": "containers", "type": error_type, "message": str(error)}],
            "provenance": _provenance(client),
            "server": server,
            "authentication": {"kind": "session login", "session_id_observed": session_id},
            "database": database,
            "containers": {
                "status": "error",
                "reported_count": None,
                "returned_count": None,
                "items": None,
            },
            "item_snapshots": [],
        }
    container_frames = _listing_items(containers_root)
    if len(container_frames) > client.limits.max_containers:
        raise DmapProtocolError("container count exceeds configured container limit")
    containers = [_container_dict(frame) for frame in container_frames]
    returned_count = _optional_uint(containers_root, "mrco")
    if returned_count is not None and returned_count != len(containers):
        raise DmapProtocolError("container returned-count does not match parsed listing")

    item_snapshots: list[dict[str, object]] = []
    item_errors: list[dict[str, str]] = []
    container_by_id: dict[int, dict[str, object]] = {}
    for item in containers:
        item_id = item["id"]
        if not isinstance(item_id, int):
            raise DmapProtocolError("parsed container id is not an integer")
        container_by_id[item_id] = item
    metadata = (
        "dmap.itemid,dmap.itemname,daap.songartist,daap.songalbum,"
        "daap.songgenre,daap.songtime,daap.songyear"
    )
    for container_id in requested_container_ids:
        selected = container_by_id.get(container_id)
        if selected is None:
            raise DmapProtocolError(f"requested container {container_id} was not returned")
        reported_items = selected.get("item_count")
        if isinstance(reported_items, int) and reported_items > client.limits.max_items_per_container:
            message = f"container {container_id} item count exceeds configured item limit"
            item_errors.append(
                {"stage": f"container-items:{container_id}", "type": "limit", "message": message}
            )
            item_snapshots.append(
                {
                    "container_id": container_id,
                    "container_name": selected["name"],
                    "status": "error",
                    "reported_count": reported_items,
                    "returned_count": None,
                    "items": None,
                }
            )
            continue
        items_root = _response_root(
            client.request(
                f"/databases/{database_id}/containers/{container_id}/items",
                (
                    ("session-id", str(session_id)),
                    ("revision-number", "1"),
                    ("meta", metadata),
                ),
            ).body,
            "apso",
            parser_limits,
        )
        item_frames = _listing_items(items_root)
        if len(item_frames) > client.limits.max_items_per_container:
            raise DmapProtocolError(
                f"container {container_id} returned more items than configured limit"
            )
        item_returned_count = _optional_uint(items_root, "mrco")
        if item_returned_count is not None and item_returned_count != len(item_frames):
            raise DmapProtocolError("container item returned-count does not match parsed listing")
        item_snapshots.append(
            {
                "container_id": container_id,
                "container_name": selected["name"],
                "status": "complete",
                "reported_count": _optional_uint(items_root, "mtco"),
                "returned_count": len(item_frames),
                "items": [_track_dict(frame) for frame in item_frames],
            }
        )

    return {
        "schema_version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "incomplete" if item_errors else "complete",
        "complete": not item_errors,
        "errors": item_errors,
        "provenance": _provenance(client),
        "server": server,
        "authentication": {"kind": "session login", "session_id_observed": session_id},
        "database": database,
        "containers": {
            "status": "complete",
            "reported_count": _optional_uint(containers_root, "mtco"),
            "returned_count": len(containers),
            "items": containers,
        },
        "item_snapshots": item_snapshots,
    }


def _provenance(client: DaapClient) -> dict[str, object]:
    return {
        "base_url": client.base_url,
        "transport": "read-only DAAP over private LAN HTTP",
        "warning": "Unencrypted HTTP was used only on the explicitly configured private LAN endpoint.",
        "method": "GET",
        "audio_fetched": False,
        "write_endpoints_used": False,
        "limits": {
            "request_timeout_seconds": client.limits.request_timeout_seconds,
            "total_timeout_seconds": client.limits.total_timeout_seconds,
            "max_response_bytes": client.limits.max_response_bytes,
            "max_containers": client.limits.max_containers,
            "max_items_per_container": client.limits.max_items_per_container,
        },
    }


def _response_root(data: bytes, expected_tag: str, limits: DmapLimits) -> DmapFrame:
    frames = parse_dmap(data, limits)
    if len(frames) != 1 or frames[0].tag != expected_tag:
        raise DmapProtocolError(f"expected one {expected_tag} response frame")
    root = frames[0]
    status = _required_uint(root, "mstt")
    if status != 200:
        raise DmapProtocolError(f"DAAP protocol status was {status}, expected 200")
    return root


def _child(frame: DmapFrame, tag: str) -> DmapFrame | None:
    matches = [child for child in frame.children if child.tag == tag]
    if len(matches) > 1:
        raise DmapProtocolError(f"duplicate {tag} field")
    return matches[0] if matches else None


def _required_uint(frame: DmapFrame, tag: str) -> int:
    value = _optional_uint(frame, tag)
    if value is None:
        raise DmapProtocolError(f"missing required {tag} integer")
    return value


def _optional_uint(frame: DmapFrame, tag: str) -> int | None:
    child = _child(frame, tag)
    if child is None:
        return None
    if child.value is None or len(child.value) not in (1, 2, 4, 8):
        raise DmapProtocolError(f"{tag} is not a bounded unsigned integer")
    return int.from_bytes(child.value, "big", signed=False)


def _required_text(frame: DmapFrame, tag: str) -> str:
    child = _child(frame, tag)
    if child is None or child.value is None:
        raise DmapProtocolError(f"missing required {tag} text")
    try:
        return child.value.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DmapProtocolError(f"{tag} is not valid UTF-8") from error


def _listing_items(root: DmapFrame) -> tuple[DmapFrame, ...]:
    listing = _child(root, "mlcl")
    if listing is None:
        raise DmapProtocolError("missing required mlcl listing; response is not an empty list")
    if any(child.tag != "mlit" for child in listing.children):
        raise DmapProtocolError("unexpected non-item frame in mlcl listing")
    return listing.children


def _container_dict(frame: DmapFrame) -> dict[str, object]:
    return {
        "id": _required_uint(frame, "miid"),
        "name": _required_text(frame, "minm"),
        "item_count": _optional_uint(frame, "mimc"),
        "persistent_id": _optional_uint(frame, "mper"),
        "parent_container_id": _optional_uint(frame, "mpco"),
        "smart_playlist": _optional_uint(frame, "aeSP"),
        "playlist_type": _optional_uint(frame, "aePS"),
    }


def _optional_text(frame: DmapFrame, tag: str) -> str | None:
    child = _child(frame, tag)
    if child is None:
        return None
    if child.value is None:
        raise DmapProtocolError(f"{tag} is not text")
    try:
        return child.value.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DmapProtocolError(f"{tag} is not valid UTF-8") from error


def _track_dict(frame: DmapFrame) -> dict[str, object]:
    return {
        "id": _required_uint(frame, "miid"),
        "title": _required_text(frame, "minm"),
        "artist": _optional_text(frame, "asar"),
        "album": _optional_text(frame, "asal"),
        "genre": _optional_text(frame, "asgn"),
        "duration_ms": _optional_uint(frame, "astm"),
        "year": _optional_uint(frame, "asyr"),
    }


def _header(headers: Mapping[str, str], name: str) -> str | None:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None
