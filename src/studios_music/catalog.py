"""SQLite-backed identity and provenance catalog for owned music."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import TracebackType

from .models import IdentifierAssertion, MetadataObservation

_ALLOWED_SNAPSHOT_STATUSES = frozenset({"accessible", "inaccessible", "error"})
_SCHEMA_VERSION = 2


class MusicCatalog:
    """Small, auditable SQLite catalog with source-namespaced identities."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._create_schema()

    def _create_schema(self) -> None:
        version_table = self._conn.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='schema_meta'
            """
        ).fetchone()
        if version_table is None:
            self._create_version_two_schema()
            return

        version = self.schema_version()
        if version == 1:
            self._migrate_version_one_to_two()
        elif version != _SCHEMA_VERSION:
            raise RuntimeError(f"unsupported music catalog schema version: {version}")

    def _create_version_two_schema(self) -> None:
        self._conn.executescript(
            """
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO schema_meta(key, value) VALUES ('schema_version', '2');

            CREATE TABLE IF NOT EXISTS tracks (
                track_id INTEGER PRIMARY KEY AUTOINCREMENT,
                canonical_path TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS track_identities (
                track_id INTEGER NOT NULL REFERENCES tracks(track_id) ON DELETE CASCADE,
                source_system TEXT NOT NULL,
                source_id TEXT COLLATE BINARY NOT NULL,
                PRIMARY KEY (track_id, source_system, source_id)
            );
            CREATE INDEX IF NOT EXISTS track_identities_lookup
            ON track_identities(source_system, source_id);

            CREATE TABLE IF NOT EXISTS source_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                source_system TEXT NOT NULL,
                source_ref TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('accessible', 'inaccessible', 'error'))
            );

            CREATE TABLE IF NOT EXISTS identifier_assertions (
                assertion_id INTEGER PRIMARY KEY AUTOINCREMENT,
                track_id INTEGER NOT NULL REFERENCES tracks(track_id) ON DELETE CASCADE,
                identifier_type TEXT NOT NULL,
                identifier_value TEXT COLLATE BINARY NOT NULL,
                source_system TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                UNIQUE (
                    track_id, identifier_type, identifier_value,
                    source_system, observed_at
                )
            );
            CREATE INDEX IF NOT EXISTS identifier_assertions_lookup
            ON identifier_assertions(identifier_type, identifier_value);

            CREATE TABLE IF NOT EXISTS metadata_observations (
                observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                track_id INTEGER NOT NULL REFERENCES tracks(track_id) ON DELETE CASCADE,
                source_system TEXT NOT NULL,
                source_id TEXT COLLATE BINARY,
                observed_at TEXT NOT NULL,
                artist TEXT,
                title TEXT,
                album TEXT,
                duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
                CHECK (
                    artist IS NOT NULL OR title IS NOT NULL OR album IS NOT NULL
                    OR duration_ms IS NOT NULL
                )
            );
            CREATE INDEX IF NOT EXISTS metadata_observations_track
            ON metadata_observations(track_id, observation_id);
            COMMIT;
            """
        )

    def _migrate_version_one_to_two(self) -> None:
        self._conn.executescript(
            """
            BEGIN IMMEDIATE;
            ALTER TABLE track_identities RENAME TO track_identities_v1;
            CREATE TABLE track_identities (
                track_id INTEGER NOT NULL REFERENCES tracks(track_id) ON DELETE CASCADE,
                source_system TEXT NOT NULL,
                source_id TEXT COLLATE BINARY NOT NULL,
                PRIMARY KEY (track_id, source_system, source_id)
            );
            INSERT INTO track_identities(track_id, source_system, source_id)
            SELECT track_id, source_system, source_id FROM track_identities_v1;
            DROP TABLE track_identities_v1;
            CREATE INDEX track_identities_lookup
            ON track_identities(source_system, source_id);

            CREATE TABLE identifier_assertions (
                assertion_id INTEGER PRIMARY KEY AUTOINCREMENT,
                track_id INTEGER NOT NULL REFERENCES tracks(track_id) ON DELETE CASCADE,
                identifier_type TEXT NOT NULL,
                identifier_value TEXT COLLATE BINARY NOT NULL,
                source_system TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                UNIQUE (
                    track_id, identifier_type, identifier_value,
                    source_system, observed_at
                )
            );
            CREATE INDEX identifier_assertions_lookup
            ON identifier_assertions(identifier_type, identifier_value);

            CREATE TABLE metadata_observations (
                observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                track_id INTEGER NOT NULL REFERENCES tracks(track_id) ON DELETE CASCADE,
                source_system TEXT NOT NULL,
                source_id TEXT COLLATE BINARY,
                observed_at TEXT NOT NULL,
                artist TEXT,
                title TEXT,
                album TEXT,
                duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
                CHECK (
                    artist IS NOT NULL OR title IS NOT NULL OR album IS NOT NULL
                    OR duration_ms IS NOT NULL
                )
            );
            CREATE INDEX metadata_observations_track
            ON metadata_observations(track_id, observation_id);

            UPDATE schema_meta SET value='2' WHERE key='schema_version';
            COMMIT;
            """
        )

    def schema_version(self) -> int:
        row = self._conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        if row is None:
            raise RuntimeError("music catalog schema version is missing")
        return int(row[0])

    def add_track(self, canonical_path: str) -> int:
        if not canonical_path or Path(canonical_path).is_absolute():
            raise ValueError("canonical_path must be a non-empty relative path")
        cursor = self._conn.execute(
            "INSERT INTO tracks(canonical_path) VALUES (?)",
            (canonical_path,),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("SQLite did not return a track ID")
        return int(cursor.lastrowid)

    def add_identity(self, track_id: int, source_system: str, source_id: str) -> None:
        if not source_system or not source_id:
            raise ValueError("source_system and source_id are required")
        self._conn.execute(
            """
            INSERT INTO track_identities(track_id, source_system, source_id)
            VALUES (?, ?, ?)
            """,
            (track_id, source_system, source_id),
        )

    def find_track_by_identity(self, source_system: str, source_id: str) -> int | None:
        track_ids = self.find_tracks_by_identity(source_system, source_id)
        if len(track_ids) > 1:
            raise RuntimeError(
                f"identity is ambiguous: {source_system}:{source_id}"
            )
        return None if not track_ids else track_ids[0]

    def find_tracks_by_identity(
        self, source_system: str, source_id: str
    ) -> tuple[int, ...]:
        rows = self._conn.execute(
            """
            SELECT track_id FROM track_identities
            WHERE source_system=? AND source_id=?
            ORDER BY track_id
            """,
            (source_system, source_id),
        ).fetchall()
        return tuple(int(row[0]) for row in rows)

    def add_identifier_assertion(
        self,
        track_id: int,
        *,
        identifier_type: str,
        identifier_value: str,
        source_system: str,
        observed_at: str,
    ) -> int:
        if not all((identifier_type, identifier_value, source_system, observed_at)):
            raise ValueError("identifier assertion fields are required")
        cursor = self._conn.execute(
            """
            INSERT INTO identifier_assertions(
                track_id, identifier_type, identifier_value,
                source_system, observed_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                track_id,
                identifier_type,
                identifier_value,
                source_system,
                observed_at,
            ),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("SQLite did not return an assertion ID")
        return int(cursor.lastrowid)

    def list_identifier_assertions(
        self,
        *,
        track_id: int | None = None,
        identifier_type: str | None = None,
        identifier_value: str | None = None,
    ) -> tuple[IdentifierAssertion, ...]:
        clauses: list[str] = []
        parameters: list[int | str] = []
        for column, value in (
            ("track_id", track_id),
            ("identifier_type", identifier_type),
            ("identifier_value", identifier_value),
        ):
            if value is not None:
                clauses.append(f"{column}=?")
                parameters.append(value)
        where = "" if not clauses else " WHERE " + " AND ".join(clauses)
        rows = self._conn.execute(
            """
            SELECT assertion_id, track_id, identifier_type, identifier_value,
                   source_system, observed_at
            FROM identifier_assertions
            """
            + where
            + " ORDER BY assertion_id",
            parameters,
        ).fetchall()
        return tuple(IdentifierAssertion(*row) for row in rows)

    def record_metadata_observation(
        self,
        track_id: int,
        *,
        source_system: str,
        observed_at: str,
        source_id: str | None = None,
        artist: str | None = None,
        title: str | None = None,
        album: str | None = None,
        duration_ms: int | None = None,
    ) -> int:
        if not source_system or not observed_at:
            raise ValueError("source_system and observed_at are required")
        if all(value is None for value in (artist, title, album, duration_ms)):
            raise ValueError("at least one metadata value is required")
        if duration_ms is not None and duration_ms < 0:
            raise ValueError("duration_ms must be non-negative")
        cursor = self._conn.execute(
            """
            INSERT INTO metadata_observations(
                track_id, source_system, source_id, observed_at,
                artist, title, album, duration_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                track_id,
                source_system,
                source_id,
                observed_at,
                artist,
                title,
                album,
                duration_ms,
            ),
        )
        if cursor.lastrowid is None:
            raise RuntimeError("SQLite did not return an observation ID")
        return int(cursor.lastrowid)

    def list_metadata_observations(
        self, *, track_id: int | None = None
    ) -> tuple[MetadataObservation, ...]:
        where = "" if track_id is None else " WHERE track_id=?"
        parameters: tuple[int, ...] = () if track_id is None else (track_id,)
        rows = self._conn.execute(
            """
            SELECT observation_id, track_id, source_system, source_id, observed_at,
                   artist, title, album, duration_ms
            FROM metadata_observations
            """
            + where
            + " ORDER BY observation_id",
            parameters,
        ).fetchall()
        return tuple(MetadataObservation(*row) for row in rows)

    def record_source_snapshot(
        self,
        *,
        snapshot_id: str,
        source_system: str,
        source_ref: str,
        captured_at: str,
        payload_sha256: str,
        status: str,
    ) -> None:
        if status not in _ALLOWED_SNAPSHOT_STATUSES:
            allowed = ", ".join(sorted(_ALLOWED_SNAPSHOT_STATUSES))
            raise ValueError(f"snapshot status must be one of: {allowed}")
        self._conn.execute(
            """
            INSERT INTO source_snapshots(
                snapshot_id, source_system, source_ref, captured_at,
                payload_sha256, status
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                source_system,
                source_ref,
                captured_at,
                payload_sha256,
                status,
            ),
        )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "MusicCatalog":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
