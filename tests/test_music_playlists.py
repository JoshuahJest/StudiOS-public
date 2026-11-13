"""Tests for fail-closed playlist snapshots and M3U8 output."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from studios_music.playlists import (
    InaccessiblePlaylistError,
    PlaylistItem,
    PlaylistSnapshot,
    render_relative_m3u8,
)


class TestPlaylistSnapshots(unittest.TestCase):
    def test_inaccessible_snapshot_cannot_be_published_as_empty(self) -> None:
        snapshot = PlaylistSnapshot(
            source_system="spotify",
            source_playlist_id="discover-weekly",
            snapshot_id=None,
            captured_at="2026-10-04T07:00:00Z",
            accessible=False,
            inaccessible_reason="HTTP 403",
            items=(),
        )
        with self.assertRaisesRegex(InaccessiblePlaylistError, "HTTP 403"):
            snapshot.require_publishable()

    def test_accessible_empty_snapshot_is_publishable(self) -> None:
        snapshot = PlaylistSnapshot(
            source_system="spotify",
            source_playlist_id="new-empty-playlist",
            snapshot_id="snap-empty",
            captured_at="2026-10-04T07:00:00Z",
            accessible=True,
            inaccessible_reason=None,
            items=(),
        )
        self.assertIs(snapshot.require_publishable(), snapshot)

    def test_inaccessible_snapshot_cannot_claim_items(self) -> None:
        with self.assertRaisesRegex(ValueError, "inaccessible snapshot"):
            PlaylistSnapshot(
                source_system="spotify",
                source_playlist_id="playlist-1",
                snapshot_id=None,
                captured_at="2026-10-04T07:00:00Z",
                accessible=False,
                inaccessible_reason="timeout",
                items=(PlaylistItem("track-1", "Artist/Album/01.flac"),),
            )


class TestM3U8(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "library"
        (self.root / "Björk" / "Debut").mkdir(parents=True)
        (self.root / "Massive Attack" / "Mezzanine").mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_render_preserves_order_and_uses_relative_posix_paths(self) -> None:
        items = (
            PlaylistItem("track-2", "Massive Attack/Mezzanine/01 Angel.flac"),
            PlaylistItem("track-1", "Björk/Debut/01 Human Behaviour.flac"),
        )
        rendered = render_relative_m3u8(self.root, items)
        self.assertEqual(
            rendered,
            "#EXTM3U\n"
            "Massive Attack/Mezzanine/01 Angel.flac\n"
            "Björk/Debut/01 Human Behaviour.flac\n",
        )

    def test_render_rejects_paths_outside_library_root(self) -> None:
        items = (PlaylistItem("track-x", "../outside.flac"),)
        with self.assertRaisesRegex(ValueError, "escapes library root"):
            render_relative_m3u8(self.root, items)

    def test_render_rejects_absolute_paths(self) -> None:
        items = (PlaylistItem("track-x", "/tmp/outside.flac"),)
        with self.assertRaisesRegex(ValueError, "relative"):
            render_relative_m3u8(self.root, items)


if __name__ == "__main__":
    unittest.main()
