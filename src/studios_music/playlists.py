"""Fail-closed playlist snapshots and portable M3U8 rendering."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class InaccessiblePlaylistError(RuntimeError):
    """Raised when publication is attempted from an inaccessible source."""


@dataclass(frozen=True)
class PlaylistItem:
    track_ref: str
    relative_path: str


@dataclass(frozen=True)
class PlaylistSnapshot:
    source_system: str
    source_playlist_id: str
    snapshot_id: str | None
    captured_at: str
    accessible: bool
    inaccessible_reason: str | None
    items: tuple[PlaylistItem, ...]

    def __post_init__(self) -> None:
        if not self.accessible and self.items:
            raise ValueError("inaccessible snapshot cannot claim playlist items")
        if self.accessible and self.inaccessible_reason:
            raise ValueError("accessible snapshot cannot have an inaccessible reason")

    def require_publishable(self) -> "PlaylistSnapshot":
        if not self.accessible:
            reason = self.inaccessible_reason or "source was inaccessible"
            raise InaccessiblePlaylistError(
                f"playlist {self.source_system}:{self.source_playlist_id} "
                f"is not publishable: {reason}"
            )
        return self


def _validated_relative_path(library_root: Path, raw_path: str) -> str:
    path = PurePosixPath(raw_path)
    if path.is_absolute():
        raise ValueError("playlist paths must be relative")
    if not raw_path or ".." in path.parts:
        raise ValueError(f"playlist path escapes library root: {raw_path}")

    root = library_root.resolve()
    candidate = (root / Path(*path.parts)).resolve()
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"playlist path escapes library root: {raw_path}") from exc
    return relative.as_posix()


def render_relative_m3u8(
    library_root: str | Path,
    items: tuple[PlaylistItem, ...],
) -> str:
    """Render an ordered UTF-8 M3U using paths relative to the library root."""

    root = Path(library_root)
    lines = ["#EXTM3U"]
    lines.extend(_validated_relative_path(root, item.relative_path) for item in items)
    return "\n".join(lines) + "\n"
