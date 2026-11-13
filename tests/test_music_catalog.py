"""Tests for the StudiOS music catalog foundation."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from studios_music.catalog import MusicCatalog


class TestMusicCatalog(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "music-catalog.sqlite"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_new_catalog_has_schema_version_two(self) -> None:
        with MusicCatalog(self.db_path) as catalog:
            self.assertEqual(catalog.schema_version(), 2)
        self.assertTrue(self.db_path.exists())

    def test_version_one_catalog_migrates_without_losing_rows(self) -> None:
        connection = sqlite3.connect(self.db_path)
        connection.executescript(
            """
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO schema_meta VALUES ('schema_version', '1');
            CREATE TABLE tracks (
                track_id INTEGER PRIMARY KEY AUTOINCREMENT,
                canonical_path TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE track_identities (
                track_id INTEGER NOT NULL REFERENCES tracks(track_id) ON DELETE CASCADE,
                source_system TEXT NOT NULL,
                source_id TEXT NOT NULL,
                PRIMARY KEY (source_system, source_id),
                UNIQUE (track_id, source_system, source_id)
            );
            CREATE TABLE source_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                source_system TEXT NOT NULL,
                source_ref TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                status TEXT NOT NULL
            );
            INSERT INTO tracks(track_id, canonical_path)
            VALUES (41, 'Artist/Album/01 Track.flac');
            INSERT INTO track_identities(track_id, source_system, source_id)
            VALUES (41, 'spotify', 'spotify:track:CaseSensitive');
            INSERT INTO source_snapshots VALUES (
                'snap-1', 'spotify', 'playlist-1', '2026-10-04T07:00:00Z',
                'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                'accessible'
            );
            """
        )
        connection.close()

        with MusicCatalog(self.db_path) as catalog:
            self.assertEqual(catalog.schema_version(), 2)
            self.assertEqual(
                catalog.find_tracks_by_identity(
                    "spotify", "spotify:track:CaseSensitive"
                ),
                (41,),
            )
            self.assertEqual(
                catalog._conn.execute(
                    "SELECT snapshot_id FROM source_snapshots"
                ).fetchall(),
                [("snap-1",)],
            )
            table_names = {
                row[0]
                for row in catalog._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertIn("identifier_assertions", table_names)
            self.assertIn("metadata_observations", table_names)

    def test_track_identity_is_namespaced_by_source(self) -> None:
        with MusicCatalog(self.db_path) as catalog:
            track_id = catalog.add_track(canonical_path="Artist/Album/01 Track.flac")
            catalog.add_identity(track_id, "spotify", "spotify:track:abc")
            catalog.add_identity(track_id, "apple", "PERSISTENT123")

            self.assertEqual(
                catalog.find_track_by_identity("spotify", "spotify:track:abc"),
                track_id,
            )
            self.assertEqual(
                catalog.find_track_by_identity("apple", "PERSISTENT123"),
                track_id,
            )

    def test_exact_source_identity_can_assert_multiple_tracks(self) -> None:
        with MusicCatalog(self.db_path) as catalog:
            first = catalog.add_track("Artist/Album/01 First.flac")
            second = catalog.add_track("Artist/Album/02 Second.flac")
            catalog.add_identity(first, "spotify", "OpaqueCase")
            catalog.add_identity(second, "spotify", "OpaqueCase")

            self.assertEqual(
                catalog.find_tracks_by_identity("spotify", "OpaqueCase"),
                (first, second),
            )
            self.assertEqual(
                catalog.find_tracks_by_identity("spotify", "opaquecase"),
                (),
            )
            with self.assertRaisesRegex(RuntimeError, "ambiguous"):
                catalog.find_track_by_identity("spotify", "OpaqueCase")

    def test_identifier_assertions_are_append_only_observations(self) -> None:
        with MusicCatalog(self.db_path) as catalog:
            track_id = catalog.add_track("Artist/Album/01 Track.flac")
            assertion_id = catalog.add_identifier_assertion(
                track_id,
                identifier_type="isrc",
                identifier_value="US-AAA-26-00001",
                source_system="spotify",
                observed_at="2026-10-04T07:00:00Z",
            )

            assertions = catalog.list_identifier_assertions(track_id=track_id)
            self.assertEqual(len(assertions), 1)
            self.assertEqual(assertions[0].assertion_id, assertion_id)
            self.assertEqual(assertions[0].identifier_type, "isrc")
            self.assertEqual(assertions[0].identifier_value, "US-AAA-26-00001")
            self.assertEqual(assertions[0].source_system, "spotify")
            self.assertEqual(assertions[0].observed_at, "2026-10-04T07:00:00Z")

    def test_metadata_observations_preserve_source_values(self) -> None:
        with MusicCatalog(self.db_path) as catalog:
            track_id = catalog.add_track("Björk/Debut/01 Human Behaviour.flac")
            observation_id = catalog.record_metadata_observation(
                track_id,
                source_system="musicbrainz",
                source_id="RecordingCaseSensitive",
                observed_at="2026-10-04T07:00:00Z",
                artist="Björk",
                title="Human Behaviour (Album Version)",
                album="Debut",
                duration_ms=252000,
            )

            observations = catalog.list_metadata_observations(track_id=track_id)
            self.assertEqual(len(observations), 1)
            self.assertEqual(observations[0].observation_id, observation_id)
            self.assertEqual(observations[0].source_id, "RecordingCaseSensitive")
            self.assertEqual(observations[0].artist, "Björk")
            self.assertEqual(observations[0].title, "Human Behaviour (Album Version)")
            self.assertEqual(observations[0].duration_ms, 252000)

    def test_metadata_observation_requires_a_value(self) -> None:
        with MusicCatalog(self.db_path) as catalog:
            track_id = catalog.add_track("Artist/Album/01 Track.flac")
            with self.assertRaisesRegex(ValueError, "metadata value"):
                catalog.record_metadata_observation(
                    track_id,
                    source_system="spotify",
                    source_id="track-1",
                    observed_at="2026-10-04T07:00:00Z",
                )

    def test_source_snapshot_ids_are_immutable(self) -> None:
        with MusicCatalog(self.db_path) as catalog:
            catalog.record_source_snapshot(
                snapshot_id="spotify-playlist-1-snap-1",
                source_system="spotify",
                source_ref="playlist-1",
                captured_at="2026-10-04T07:00:00Z",
                payload_sha256="a" * 64,
                status="accessible",
            )
            with self.assertRaises(sqlite3.IntegrityError):
                catalog.record_source_snapshot(
                    snapshot_id="spotify-playlist-1-snap-1",
                    source_system="spotify",
                    source_ref="playlist-1",
                    captured_at="2026-10-04T07:01:00Z",
                    payload_sha256="b" * 64,
                    status="accessible",
                )

    def test_catalog_rejects_unknown_snapshot_status(self) -> None:
        with MusicCatalog(self.db_path) as catalog:
            with self.assertRaisesRegex(ValueError, "snapshot status"):
                catalog.record_source_snapshot(
                    snapshot_id="bad",
                    source_system="spotify",
                    source_ref="playlist-1",
                    captured_at="2026-10-04T07:00:00Z",
                    payload_sha256="a" * 64,
                    status="empty-because-request-failed",
                )


if __name__ == "__main__":
    unittest.main()
