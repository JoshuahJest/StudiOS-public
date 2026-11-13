"""Reconciliation between two Apple iTunes XML library exports.

The reconciler produces an explicit preservation/loss report. It never
rewrites the source exports and never treats an unresolvable path as a
present file: unresolved media locations are reported as broken so that
a later migration cannot mistake them for intact content.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from studios_music.apple_xml import AppleLibrarySnapshot, ApplePlaylist, AppleTrack


@dataclass(frozen=True)
class ReconciliationReport:
    """An explicit, auditable comparison of two Apple XML exports."""

    before: AppleLibrarySnapshot
    after: AppleLibrarySnapshot

    lost_track_ids: tuple[int, ...]
    added_track_ids: tuple[int, ...]
    changed_track_ids: tuple[int, ...]

    lost_playlists: tuple[str, ...]
    added_playlists: tuple[str, ...]
    playlists_preserved: bool

    play_count_before: int
    play_count_after: int
    rating_changes: tuple[int, ...]
    date_added_min_before: datetime | None
    date_added_max_before: datetime | None
    date_added_min_after: datetime | None
    date_added_max_after: datetime | None
    broken_paths_before: tuple[int, ...]
    broken_paths_after: tuple[int, ...]
    unavailable_track_ids: tuple[int, ...]

    @property
    def play_count_delta(self) -> int:
        return self.play_count_after - self.play_count_before

    @property
    def has_loss(self) -> bool:
        return bool(
            self.lost_track_ids
            or self.lost_playlists
            or self.broken_paths_after
            or self.unavailable_track_ids
        )

    def to_dict(self) -> dict:
        """Return a deterministic, JSON-serializable loss report."""

        def _iso(value: datetime | None) -> str | None:
            return value.isoformat() if value else None

        return {
            "before": self.before.to_summary(),
            "after": self.after.to_summary(),
            "loss_report": {
                "has_loss": self.has_loss,
                "lost_track_ids": list(self.lost_track_ids),
                "added_track_ids": list(self.added_track_ids),
                "changed_track_ids": list(self.changed_track_ids),
                "lost_playlists": list(self.lost_playlists),
                "added_playlists": list(self.added_playlists),
                "playlists_preserved": self.playlists_preserved,
                "play_count_before": self.play_count_before,
                "play_count_after": self.play_count_after,
                "play_count_delta": self.play_count_delta,
                "rating_changes": list(self.rating_changes),
                "date_added_min_before": _iso(self.date_added_min_before),
                "date_added_max_before": _iso(self.date_added_max_before),
                "date_added_min_after": _iso(self.date_added_min_after),
                "date_added_max_after": _iso(self.date_added_max_after),
                "broken_paths_before": list(self.broken_paths_before),
                "broken_paths_after": list(self.broken_paths_after),
                "unavailable_track_ids": list(self.unavailable_track_ids),
            },
        }


def _playlist_key(playlist: ApplePlaylist) -> str:
    return playlist.persistent_id or f"name:{playlist.name}"


def _playlists_by_key(snapshot: AppleLibrarySnapshot) -> dict[str, ApplePlaylist]:
    return {_playlist_key(playlist): playlist for playlist in snapshot.playlists}


def _resolve_location(location: str | None, media_root: Path | None) -> Path | None:
    """Resolve an Apple ``Location`` URL to an existing file, if possible.

    When ``media_root`` is ``None`` the location cannot be resolved and the
    caller reports it as broken. When it is given, the path suffix is
    matched against the root so that both the original
    ``.../iTunes/iTunes Media/...`` layout and the flattened archive layout
    resolve to the same file.
    """

    if not location:
        return None
    parsed = urlparse(location)
    if parsed.scheme not in ("file", ""):
        return None
    raw_path = unquote(parsed.path or location)
    parts = [part for part in Path(raw_path).parts if part not in ("/",)]
    if media_root is None:
        return None
    for index in range(len(parts)):
        candidate = media_root.joinpath(*parts[index:])
        if candidate.is_file():
            return candidate
    return None


def _broken_paths(
    snapshot: AppleLibrarySnapshot, media_root: Path | None
) -> tuple[int, ...]:
    broken = []
    for track_id in sorted(snapshot.tracks):
        track = snapshot.tracks[track_id]
        if not track.is_file_backed:
            continue
        if _resolve_location(track.location, media_root) is None:
            broken.append(track_id)
    return tuple(broken)


def _unavailable(snapshot: AppleLibrarySnapshot) -> tuple[int, ...]:
    return tuple(
        sorted(
            track_id
            for track_id, track in snapshot.tracks.items()
            if track.track_type is not None and track.track_type != "File"
        )
    )


def _play_count(snapshot: AppleLibrarySnapshot) -> int:
    return sum(track.play_count or 0 for track in snapshot.tracks.values())


def _date_added_extrema(
    snapshot: AppleLibrarySnapshot,
) -> tuple[datetime | None, datetime | None]:
    values = [
        track.date_added for track in snapshot.tracks.values() if track.date_added
    ]
    if not values:
        return None, None
    return min(values), max(values)


def _changed_tracks(
    before: AppleLibrarySnapshot, after: AppleLibrarySnapshot
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Return (changed track ids, rating-changed track ids)."""

    changed: list[int] = []
    rating_changes: list[int] = []
    for track_id in sorted(set(before.tracks) & set(after.tracks)):
        old = before.tracks[track_id]
        new = after.tracks[track_id]
        if old.rating != new.rating:
            rating_changes.append(track_id)
        if (
            old.play_count != new.play_count
            or old.rating != new.rating
            or old.name != new.name
            or old.artist != new.artist
            or old.album != new.album
            or old.total_time_ms != new.total_time_ms
            or old.location != new.location
            or old.date_added != new.date_added
        ):
            changed.append(track_id)
    return tuple(changed), tuple(rating_changes)


def reconcile_apple_snapshots(
    before: AppleLibrarySnapshot,
    after: AppleLibrarySnapshot,
    *,
    media_root: str | Path | None = None,
) -> ReconciliationReport:
    """Compare two Apple XML exports and return an explicit loss report.

    ``media_root`` is the local directory that holds the archived media. When
    it is omitted, every file-backed location is reported as unresolved,
    because a path that cannot be checked must not be counted as intact.
    """

    root = Path(media_root) if media_root is not None else None

    before_ids = set(before.tracks)
    after_ids = set(after.tracks)

    before_playlists = _playlists_by_key(before)
    after_playlists = _playlists_by_key(after)

    lost_playlists = tuple(
        sorted(
            playlist.name
            for key, playlist in before_playlists.items()
            if key not in after_playlists
        )
    )
    added_playlists = tuple(
        sorted(
            playlist.name
            for key, playlist in after_playlists.items()
            if key not in before_playlists
        )
    )

    surviving_preserved = True
    for key, playlist in before_playlists.items():
        counterpart = after_playlists.get(key)
        if counterpart is None:
            continue
        if counterpart.track_ids != playlist.track_ids:
            surviving_preserved = False
        # A playlist whose membership now points at tracks that are absent
        # from the library has not been preserved; it is a dangling
        # reference and must not be reported as intact.
        if any(track_id not in after.tracks for track_id in counterpart.track_ids):
            surviving_preserved = False

    changed, rating_changes = _changed_tracks(before, after)
    min_before, max_before = _date_added_extrema(before)
    min_after, max_after = _date_added_extrema(after)

    return ReconciliationReport(
        before=before,
        after=after,
        lost_track_ids=tuple(sorted(before_ids - after_ids)),
        added_track_ids=tuple(sorted(after_ids - before_ids)),
        changed_track_ids=changed,
        lost_playlists=lost_playlists,
        added_playlists=added_playlists,
        playlists_preserved=surviving_preserved,
        play_count_before=_play_count(before),
        play_count_after=_play_count(after),
        rating_changes=rating_changes,
        date_added_min_before=min_before,
        date_added_max_before=max_before,
        date_added_min_after=min_after,
        date_added_max_after=max_after,
        broken_paths_before=_broken_paths(before, root),
        broken_paths_after=_broken_paths(after, root),
        unavailable_track_ids=_unavailable(after),
    )
