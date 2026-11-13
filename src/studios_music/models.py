"""Shared immutable record types for the music subsystem."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TrackIdentity:
    track_id: int
    source_system: str
    source_id: str


@dataclass(frozen=True)
class SourceSnapshotRecord:
    snapshot_id: str
    source_system: str
    source_ref: str
    captured_at: str
    payload_sha256: str
    status: str


@dataclass(frozen=True)
class IdentifierAssertion:
    assertion_id: int
    track_id: int
    identifier_type: str
    identifier_value: str
    source_system: str
    observed_at: str


@dataclass(frozen=True)
class MetadataObservation:
    observation_id: int
    track_id: int
    source_system: str
    source_id: str | None
    observed_at: str
    artist: str | None
    title: str | None
    album: str | None
    duration_ms: int | None
