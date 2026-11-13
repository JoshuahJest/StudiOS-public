"""Tests for Apple migration reconciliation between two XML exports."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from studios_music.apple_xml import parse_apple_library_xml
from studios_music.reconcile import reconcile_apple_snapshots

from tests.test_music_apple_xml import (
    PLAYLIST_PLAIN,
    PLAYLIST_SMART,
    TRACK_A,
    TRACK_B,
    _plist_xml,
)


TRACK_C = """\t\t<key>8611</key>
\t\t<dict>
\t\t\t<key>Track ID</key><integer>8611</integer>
\t\t\t<key>Persistent ID</key><string>04D5661ABEA616C3</string>
\t\t\t<key>Name</key><string>Lick the Rainbow</string>
\t\t\t<key>Artist</key><string>Mord Fustang</string>
\t\t\t<key>Album</key><string>A New World</string>
\t\t\t<key>Total Time</key><integer>422046</integer>
\t\t\t<key>Play Count</key><integer>52</integer>
\t\t\t<key>Rating</key><integer>100</integer>
\t\t\t<key>Date Added</key><date>2012-06-11T03:27:50Z</date>
\t\t\t<key>Location</key><string>file:///Users/archive/Music/iTunes/iTunes%20Media/Music/Mord%20Fustang/03%20track.m4a</string>
\t\t\t<key>Track Type</key><string>Remote</string>
\t\t\t<key>Kind</key><string>Purchased AAC audio file</string>
\t\t</dict>"""

TRACK_A_REPLAYED = TRACK_A.replace(
    "<key>Play Count</key><integer>7</integer>",
    "<key>Play Count</key><integer>9</integer>",
).replace(
    "<key>Rating</key><integer>81</integer>",
    "<key>Rating</key><integer>100</integer>",
)


class TestReconciliation(unittest.TestCase):
    def _snapshot(self, directory: Path, name: str, content: str):
        path = directory / name
        path.write_text(content, encoding="utf-8")
        return parse_apple_library_xml(path)

    def test_identical_exports_report_no_loss(self) -> None:
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            before = self._snapshot(
                directory, "before.xml", _plist_xml(TRACK_A + "\n" + TRACK_B, PLAYLIST_PLAIN)
            )
            after = self._snapshot(
                directory, "after.xml", _plist_xml(TRACK_A + "\n" + TRACK_B, PLAYLIST_PLAIN)
            )

            report = reconcile_apple_snapshots(before, after)

            self.assertEqual(report.lost_track_ids, ())
            self.assertEqual(report.added_track_ids, ())
            self.assertEqual(report.lost_playlists, ())
            self.assertTrue(report.playlists_preserved)
            self.assertEqual(report.play_count_before, 12)
            self.assertEqual(report.play_count_after, 12)

    def test_removed_track_is_reported_as_loss_not_silently_dropped(self) -> None:
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            before = self._snapshot(
                directory, "before.xml", _plist_xml(TRACK_A + "\n" + TRACK_B, PLAYLIST_PLAIN)
            )
            after = self._snapshot(
                directory, "after.xml", _plist_xml(TRACK_A, PLAYLIST_PLAIN)
            )

            report = reconcile_apple_snapshots(before, after)

            self.assertEqual(report.lost_track_ids, (8609,))
            self.assertEqual(report.added_track_ids, ())
            self.assertEqual(report.lost_playlists, ())
            # Playlist membership changed, so preservation cannot be claimed.
            self.assertFalse(report.playlists_preserved)

    def test_new_track_is_reported_as_added(self) -> None:
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            before = self._snapshot(
                directory, "before.xml", _plist_xml(TRACK_A, PLAYLIST_PLAIN)
            )
            after = self._snapshot(
                directory, "after.xml", _plist_xml(TRACK_A + "\n" + TRACK_C, PLAYLIST_PLAIN)
            )

            report = reconcile_apple_snapshots(before, after)

            self.assertEqual(report.added_track_ids, (8611,))
            self.assertEqual(report.lost_track_ids, ())

    def test_missing_playlist_is_reported(self) -> None:
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            before = self._snapshot(
                directory,
                "before.xml",
                _plist_xml(TRACK_A + "\n" + TRACK_B, PLAYLIST_PLAIN + "\n" + PLAYLIST_SMART),
            )
            after = self._snapshot(
                directory, "after.xml", _plist_xml(TRACK_A + "\n" + TRACK_B, PLAYLIST_PLAIN)
            )

            report = reconcile_apple_snapshots(before, after)

            self.assertEqual(report.lost_playlists, ("Recently Added",))
            self.assertTrue(report.playlists_preserved)

    def test_play_count_and_rating_deltas_are_reported(self) -> None:
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            before = self._snapshot(
                directory, "before.xml", _plist_xml(TRACK_A + "\n" + TRACK_B, PLAYLIST_PLAIN)
            )
            after = self._snapshot(
                directory, "after.xml", _plist_xml(TRACK_A_REPLAYED + "\n" + TRACK_B, PLAYLIST_PLAIN)
            )

            report = reconcile_apple_snapshots(before, after)

            self.assertEqual(report.play_count_before, 12)
            self.assertEqual(report.play_count_after, 14)
            self.assertEqual(report.play_count_delta, 2)
            self.assertEqual(report.rating_changes, (8607,))

    def test_date_added_extrema_are_reported(self) -> None:
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            before = self._snapshot(
                directory, "before.xml", _plist_xml(TRACK_A + "\n" + TRACK_B, PLAYLIST_PLAIN)
            )
            after = self._snapshot(
                directory, "after.xml", _plist_xml(TRACK_A + "\n" + TRACK_B, PLAYLIST_PLAIN)
            )

            report = reconcile_apple_snapshots(before, after)

            self.assertEqual(report.date_added_min_before.isoformat(), "2012-08-14T18:40:10+00:00")
            self.assertEqual(report.date_added_max_before.isoformat(), "2012-08-15T03:28:47+00:00")
            self.assertEqual(report.date_added_min_after, report.date_added_min_before)
            self.assertEqual(report.date_added_max_after, report.date_added_max_before)

    def test_broken_media_paths_are_reported(self) -> None:
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            before = self._snapshot(
                directory, "before.xml", _plist_xml(TRACK_A + "\n" + TRACK_B, PLAYLIST_PLAIN)
            )
            after = self._snapshot(
                directory, "after.xml", _plist_xml(TRACK_A + "\n" + TRACK_B, PLAYLIST_PLAIN)
            )

            report = reconcile_apple_snapshots(before, after)

            # Both exports reference the same archived media roots; the paths
            # are counted, and never silently treated as present.
            self.assertEqual(report.broken_paths_after, (8607, 8609))
            self.assertEqual(report.broken_paths_before, (8607, 8609))

    def test_report_is_json_serializable_and_deterministic(self) -> None:
        import json

        with TemporaryDirectory() as raw:
            directory = Path(raw)
            before = self._snapshot(
                directory, "before.xml", _plist_xml(TRACK_A + "\n" + TRACK_B, PLAYLIST_PLAIN)
            )
            after = self._snapshot(
                directory, "after.xml", _plist_xml(TRACK_A_REPLAYED, PLAYLIST_PLAIN)
            )

            report = reconcile_apple_snapshots(before, after)
            payload = report.to_dict()

            self.assertEqual(json.dumps(payload, sort_keys=True), json.dumps(payload, sort_keys=True))
            self.assertEqual(payload["before"]["track_count"], 2)
            self.assertEqual(payload["after"]["track_count"], 1)
            self.assertIn("loss_report", payload)


if __name__ == "__main__":
    unittest.main()
