"""Read-only parsing of Apple iTunes XML library exports.

The parser never opens or converts an ``.itl`` library. It reads only the
plist XML export artifacts that live beside it in the immutable source
archive, and it fails closed when the payload is missing, malformed, or
structurally incomplete.
"""

from __future__ import annotations

import hashlib
import plistlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


class AppleXmlError(RuntimeError):
    """Raised when an Apple XML export cannot be read as a library export."""


@dataclass(frozen=True)
class AppleTrack:
    """One track as recorded in an Apple XML export."""

    track_id: int
    persistent_id: str | None
    name: str | None
    artist: str | None
    album: str | None
    album_artist: str | None
    genre: str | None
    kind: str | None
    track_type: str | None
    total_time_ms: int | None
    size_bytes: int | None
    play_count: int | None
    skip_count: int | None
    rating: int | None
    loved: bool
    year: int | None
    track_number: int | None
    disc_number: int | None
    date_added: datetime | None
    date_modified: datetime | None
    play_date_utc: datetime | None
    location: str | None

    @property
    def is_file_backed(self) -> bool:
        return self.track_type == "File"


@dataclass(frozen=True)
class ApplePlaylist:
    """One playlist (manual or smart) as recorded in an Apple XML export."""

    playlist_id: int | None
    persistent_id: str | None
    name: str
    smart: bool
    smart_criteria: str | None
    track_ids: tuple[int, ...]

    @property
    def item_count(self) -> int:
        return len(self.track_ids)


@dataclass(frozen=True)
class AppleLibrarySnapshot:
    """A parsed, immutable view of one Apple XML export."""

    library_persistent_id: str
    application_version: str | None
    exported_at: datetime | None
    source_path: str
    source_sha256: str
    tracks: dict[int, AppleTrack] = field(default_factory=dict)
    playlists: tuple[ApplePlaylist, ...] = ()

    def to_summary(self) -> dict:
        """Return a deterministic, JSON-serializable summary."""

        return {
            "library_persistent_id": self.library_persistent_id,
            "application_version": self.application_version,
            "exported_at": self.exported_at.isoformat() if self.exported_at else None,
            "source_path": self.source_path,
            "source_sha256": self.source_sha256,
            "track_count": len(self.tracks),
            "playlist_count": len(self.playlists),
            "playlists": [
                {
                    "name": playlist.name,
                    "playlist_id": playlist.playlist_id,
                    "persistent_id": playlist.persistent_id,
                    "smart": playlist.smart,
                    "smart_criteria": playlist.smart_criteria,
                    "item_count": playlist.item_count,
                }
                for playlist in self.playlists
            ],
        }


def _optional_datetime(value: object) -> datetime | None:
    """Return an aware UTC datetime.

    Apple plist ``<date>`` values carry no timezone offset; they are UTC by
    convention. The offset is attached explicitly so comparisons across
    exports are offset-safe instead of accidentally mixing naive and aware
    values.
    """

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return None


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _optional_str(value: object) -> str | None:
    if isinstance(value, str):
        return value
    return None


def _parse_track(track_id: int, raw: object) -> AppleTrack:
    if not isinstance(raw, dict):
        raise AppleXmlError(f"track {track_id} is not a dictionary")

    declared_id = _optional_int(raw.get("Track ID"))
    if declared_id is None:
        raise AppleXmlError(f"track {track_id} has no Track ID")

    return AppleTrack(
        track_id=declared_id,
        persistent_id=_optional_str(raw.get("Persistent ID")),
        name=_optional_str(raw.get("Name")),
        artist=_optional_str(raw.get("Artist")),
        album=_optional_str(raw.get("Album")),
        album_artist=_optional_str(raw.get("Album Artist")),
        genre=_optional_str(raw.get("Genre")),
        kind=_optional_str(raw.get("Kind")),
        track_type=_optional_str(raw.get("Track Type")),
        total_time_ms=_optional_int(raw.get("Total Time")),
        size_bytes=_optional_int(raw.get("Size")),
        play_count=_optional_int(raw.get("Play Count")),
        skip_count=_optional_int(raw.get("Skip Count")),
        rating=_optional_int(raw.get("Rating")),
        loved=bool(raw.get("Loved", False)),
        year=_optional_int(raw.get("Year")),
        track_number=_optional_int(raw.get("Track Number")),
        disc_number=_optional_int(raw.get("Disc Number")),
        date_added=_optional_datetime(raw.get("Date Added")),
        date_modified=_optional_datetime(raw.get("Date Modified")),
        play_date_utc=_optional_datetime(raw.get("Play Date UTC")),
        location=_optional_str(raw.get("Location")),
    )


def _parse_playlist(raw: object) -> ApplePlaylist:
    if not isinstance(raw, dict):
        raise AppleXmlError("playlist entry is not a dictionary")

    name = _optional_str(raw.get("Name"))
    if not name:
        raise AppleXmlError("playlist entry has no Name")

    items = raw.get("Playlist Items")
    track_ids: list[int] = []
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                track_id = _optional_int(item.get("Track ID"))
                if track_id is not None:
                    track_ids.append(track_id)

    criteria = raw.get("Smart Criteria")
    return ApplePlaylist(
        playlist_id=_optional_int(raw.get("Playlist ID")),
        persistent_id=_optional_str(raw.get("Playlist Persistent ID")),
        name=name,
        smart=bool(raw.get("Smart", False)),
        smart_criteria=_optional_str(criteria) if isinstance(criteria, str) else None,
        track_ids=tuple(track_ids),
    )


def parse_apple_library_xml(path: str | Path) -> AppleLibrarySnapshot:
    """Parse an Apple iTunes XML export read-only.

    Raises :class:`AppleXmlError` when the file is missing, is not a plist,
    or does not carry the structure of a library export.
    """

    source = Path(path)
    try:
        payload = source.read_bytes()
    except OSError as exc:
        raise AppleXmlError(f"cannot read Apple XML export {source}: {exc}") from exc

    try:
        parsed = plistlib.loads(payload)
    except Exception as exc:  # plistlib raises several unrelated types
        raise AppleXmlError(f"cannot parse Apple XML export {source}: {exc}") from exc

    if not isinstance(parsed, dict):
        raise AppleXmlError(f"Apple XML export {source} is not a plist dictionary")

    library_id = _optional_str(parsed.get("Library Persistent ID"))
    if not library_id:
        raise AppleXmlError(
            f"Apple XML export {source} has no Library Persistent ID"
        )

    raw_tracks = parsed.get("Tracks")
    if not isinstance(raw_tracks, dict):
        raise AppleXmlError(f"Apple XML export {source} has no Tracks dictionary")

    tracks: dict[int, AppleTrack] = {}
    for key, raw in raw_tracks.items():
        try:
            track_id = int(key)
        except (TypeError, ValueError) as exc:
            raise AppleXmlError(f"non-numeric track key {key!r}") from exc
        track = _parse_track(track_id, raw)
        tracks[track.track_id] = track

    raw_playlists = parsed.get("Playlists")
    playlists: list[ApplePlaylist] = []
    if isinstance(raw_playlists, list):
        for raw in raw_playlists:
            playlists.append(_parse_playlist(raw))

    return AppleLibrarySnapshot(
        library_persistent_id=library_id,
        application_version=_optional_str(parsed.get("Application Version")),
        exported_at=_optional_datetime(parsed.get("Date")),
        source_path=str(source),
        source_sha256=hashlib.sha256(payload).hexdigest(),
        tracks=tracks,
        playlists=tuple(playlists),
    )
