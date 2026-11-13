"""Tests for read-only parsing of Apple iTunes XML library exports."""

from __future__ import annotations

import hashlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from studios_music.apple_xml import (
    AppleXmlError,
    parse_apple_library_xml,
)


def _plist_xml(
    tracks: str = "",
    playlists: str = "",
    *,
    library_id: str = "F5B1B63F06E1C897",
    date: str = "2024-08-02T17:29:42Z",
    version: str = "12.9.5.5",
) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
\t<key>Major Version</key><integer>1</integer>
\t<key>Minor Version</key><integer>1</integer>
\t<key>Application Version</key><string>{version}</string>
\t<key>Date</key><date>{date}</date>
\t<key>Library Persistent ID</key><string>{library_id}</string>
\t<key>Tracks</key>
\t<dict>
{tracks}
\t</dict>
\t<key>Playlists</key>
\t<array>
{playlists}
\t</array>
</dict>
</plist>
"""


TRACK_A = """\t\t<key>8607</key>
\t\t<dict>
\t\t\t<key>Track ID</key><integer>8607</integer>
\t\t\t<key>Persistent ID</key><string>A113A1DB83EB4288</string>
\t\t\t<key>Name</key><string>Professional Griefers</string>
\t\t\t<key>Artist</key><string>Deadmau5</string>
\t\t\t<key>Album</key><string>Professional Griefers - Single</string>
\t\t\t<key>Total Time</key><integer>243750</integer>
\t\t\t<key>Play Count</key><integer>7</integer>
\t\t\t<key>Rating</key><integer>81</integer>
\t\t\t<key>Date Added</key><date>2012-08-14T18:40:10Z</date>
\t\t\t<key>Location</key><string>file:///Users/archive/Music/iTunes/iTunes%20Media/Music/deadmau5/01%20track.m4a</string>
\t\t\t<key>Track Type</key><string>File</string>
\t\t\t<key>Kind</key><string>Purchased AAC audio file</string>
\t\t</dict>"""

TRACK_B = """\t\t<key>8609</key>
\t\t<dict>
\t\t\t<key>Track ID</key><integer>8609</integer>
\t\t\t<key>Persistent ID</key><string>A4C49D2FA5FA04F2</string>
\t\t\t<key>Name</key><string>Shake Your Coconuts</string>
\t\t\t<key>Artist</key><string>Junior Senior</string>
\t\t\t<key>Album</key><string>Shake Your Coconuts - Single</string>
\t\t\t<key>Total Time</key><integer>315320</integer>
\t\t\t<key>Play Count</key><integer>5</integer>
\t\t\t<key>Rating</key><integer>80</integer>
\t\t\t<key>Date Added</key><date>2012-08-15T03:28:47Z</date>
\t\t\t<key>Location</key><string>file:///Users/archive/Music/iTunes/iTunes%20Media/Music/Junior%20Senior/02%20track.m4a</string>
\t\t\t<key>Track Type</key><string>File</string>
\t\t\t<key>Kind</key><string>Purchased AAC audio file</string>
\t\t</dict>"""

PLAYLIST_PLAIN = """\t\t<dict>
\t\t\t<key>Name</key><string>Mixes</string>
\t\t\t<key>Playlist ID</key><integer>45954</integer>
\t\t\t<key>Playlist Persistent ID</key><string>1A04CD9111C1013B</string>
\t\t\t<key>Playlist Items</key>
\t\t\t<array>
\t\t\t\t<dict><key>Track ID</key><integer>8607</integer></dict>
\t\t\t\t<dict><key>Track ID</key><integer>8609</integer></dict>
\t\t\t</array>
\t\t</dict>"""

PLAYLIST_SMART = """\t\t<dict>
\t\t\t<key>Name</key><string>Recently Added</string>
\t\t\t<key>Playlist ID</key><integer>61565</integer>
\t\t\t<key>Playlist Persistent ID</key><string>0A92E8F98158C035</string>
\t\t\t<key>Smart</key><true/>
\t\t\t<key>Smart Criteria</key><string>Date Added is in the last 30 days</string>
\t\t</dict>"""


class TestAppleXmlParsing(unittest.TestCase):
    def _write(self, directory: Path, name: str, content: str) -> Path:
        path = directory / name
        path.write_text(content, encoding="utf-8")
        return path

    def test_parses_tracks_playlists_and_provenance(self) -> None:
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            path = self._write(
                directory,
                "library.xml",
                _plist_xml(TRACK_A + "\n" + TRACK_B, PLAYLIST_PLAIN + "\n" + PLAYLIST_SMART),
            )

            snapshot = parse_apple_library_xml(path)

            self.assertEqual(snapshot.library_persistent_id, "F5B1B63F06E1C897")
            self.assertEqual(snapshot.application_version, "12.9.5.5")
            self.assertEqual(snapshot.exported_at.isoformat(), "2024-08-02T17:29:42+00:00")
            self.assertEqual(len(snapshot.tracks), 2)
            self.assertEqual(len(snapshot.playlists), 2)
            self.assertEqual(
                snapshot.source_sha256,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )

    def test_track_fields_are_preserved_verbatim(self) -> None:
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            path = self._write(directory, "library.xml", _plist_xml(TRACK_A))

            track = parse_apple_library_xml(path).tracks[8607]

            self.assertEqual(track.persistent_id, "A113A1DB83EB4288")
            self.assertEqual(track.name, "Professional Griefers")
            self.assertEqual(track.artist, "Deadmau5")
            self.assertEqual(track.total_time_ms, 243750)
            self.assertEqual(track.play_count, 7)
            self.assertEqual(track.rating, 81)
            self.assertEqual(track.date_added.isoformat(), "2012-08-14T18:40:10+00:00")
            self.assertEqual(
                track.location,
                "file:///Users/archive/Music/iTunes/iTunes%20Media/Music/deadmau5/01%20track.m4a",
            )

    def test_playlist_membership_order_and_smart_flag(self) -> None:
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            path = self._write(
                directory,
                "library.xml",
                _plist_xml(TRACK_A + "\n" + TRACK_B, PLAYLIST_PLAIN + "\n" + PLAYLIST_SMART),
            )

            playlists = parse_apple_library_xml(path).playlists

            plain = next(p for p in playlists if p.name == "Mixes")
            smart = next(p for p in playlists if p.name == "Recently Added")
            self.assertEqual(plain.track_ids, (8607, 8609))
            self.assertFalse(plain.smart)
            self.assertTrue(smart.smart)
            self.assertEqual(
                smart.smart_criteria, "Date Added is in the last 30 days"
            )
            self.assertEqual(smart.track_ids, ())

    def test_missing_file_fails_closed(self) -> None:
        with TemporaryDirectory() as raw:
            path = Path(raw) / "absent.xml"
            with self.assertRaises(AppleXmlError):
                parse_apple_library_xml(path)

    def test_non_plist_payload_fails_closed(self) -> None:
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            path = self._write(directory, "broken.xml", "<?xml version='1.0'?><nope/>")

            with self.assertRaises(AppleXmlError):
                parse_apple_library_xml(path)

    def test_export_without_tracks_fails_closed(self) -> None:
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            path = self._write(
                directory,
                "empty.xml",
                """<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
\t<key>Major Version</key><integer>1</integer>
\t<key>Library Persistent ID</key><string>ABC</string>
</dict>
</plist>
""",
            )

            with self.assertRaises(AppleXmlError):
                parse_apple_library_xml(path)

    def test_parse_is_deterministic(self) -> None:
        with TemporaryDirectory() as raw:
            directory = Path(raw)
            path = self._write(
                directory,
                "library.xml",
                _plist_xml(TRACK_A + "\n" + TRACK_B, PLAYLIST_PLAIN),
            )

            first = parse_apple_library_xml(path)
            second = parse_apple_library_xml(path)

            self.assertEqual(first.to_summary(), second.to_summary())


if __name__ == "__main__":
    unittest.main()
