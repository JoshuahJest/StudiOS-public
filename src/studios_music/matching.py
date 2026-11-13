"""Pure deterministic matching primitives for catalog tracks."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Iterable


_DURATION_TOLERANCE_MS = 2000


class MatchStatus(str, Enum):
    MATCHED = "matched"
    AMBIGUOUS = "ambiguous"
    NO_MATCH = "no_match"


class MatchBasis(str, Enum):
    ISRC = "isrc"
    RECORDING_MBID = "recording_mbid"
    TRUSTED_SOURCE_CROSSWALK = "trusted_source_crosswalk"
    METADATA_DURATION = "metadata_duration"
    CONTRADICTORY_IDENTIFIERS = "contradictory_identifiers"


@dataclass(frozen=True, order=True)
class SourceIdentifier:
    source_system: str
    source_id: str

    def __post_init__(self) -> None:
        if not self.source_system or not self.source_id:
            raise ValueError("source identifiers require a system and opaque ID")


@dataclass(frozen=True)
class TrackFacts:
    """Immutable facts about either an input track or a catalog candidate."""

    track_id: int | None = None
    isrcs: tuple[str, ...] = ()
    recording_mbids: tuple[str, ...] = ()
    release_mbids: tuple[str, ...] = ()
    source_ids: tuple[SourceIdentifier, ...] = ()
    artist: str | None = None
    title: str | None = None
    album: str | None = None
    duration_ms: int | None = None

    def __post_init__(self) -> None:
        if self.duration_ms is not None and self.duration_ms < 0:
            raise ValueError("duration_ms must be non-negative")


@dataclass(frozen=True)
class MatchResult:
    status: MatchStatus
    track_ids: tuple[int, ...]
    basis: MatchBasis | None

    @property
    def track_id(self) -> int | None:
        if self.status is MatchStatus.MATCHED and len(self.track_ids) == 1:
            return self.track_ids[0]
        return None

    @property
    def candidate_track_ids(self) -> tuple[int, ...]:
        return self.track_ids


def normalize_metadata(value: str) -> str:
    """NFKC/casefold metadata and turn punctuation/separators into spaces."""

    normalized = unicodedata.normalize("NFKC", value).casefold()
    words = []
    current: list[str] = []
    for character in normalized:
        if unicodedata.category(character)[0] in {"P", "Z"} or character.isspace():
            if current:
                words.append("".join(current))
                current = []
        else:
            current.append(character)
    if current:
        words.append("".join(current))
    return " ".join(words)


def _matching_ids(
    query_values: tuple[str, ...],
    candidates: tuple[TrackFacts, ...],
    attribute: str,
) -> frozenset[int]:
    wanted = frozenset(query_values)
    if not wanted:
        return frozenset()
    return frozenset(
        candidate.track_id
        for candidate in candidates
        if wanted.intersection(getattr(candidate, attribute))
        and candidate.track_id is not None
    )


def _crosswalk_matches(
    query: TrackFacts,
    candidates: tuple[TrackFacts, ...],
    trusted_source_systems: frozenset[str],
) -> frozenset[int]:
    wanted = frozenset(
        identifier
        for identifier in query.source_ids
        if identifier.source_system in trusted_source_systems
    )
    return frozenset(
        candidate.track_id
        for candidate in candidates
        if wanted.intersection(candidate.source_ids) and candidate.track_id is not None
    )


def _candidate_has_identifier_conflict(
    query: TrackFacts,
    candidate: TrackFacts,
    trusted_source_systems: frozenset[str],
) -> bool:
    for attribute in ("isrcs", "recording_mbids"):
        query_values = frozenset(getattr(query, attribute))
        candidate_values = frozenset(getattr(candidate, attribute))
        if query_values and candidate_values and query_values.isdisjoint(candidate_values):
            return True

    for source_system in trusted_source_systems:
        query_values = {
            value.source_id
            for value in query.source_ids
            if value.source_system == source_system
        }
        candidate_values = {
            value.source_id
            for value in candidate.source_ids
            if value.source_system == source_system
        }
        if query_values and candidate_values and query_values.isdisjoint(candidate_values):
            return True
    return False


def _metadata_matches(query: TrackFacts, candidate: TrackFacts) -> bool:
    query_text = (query.artist, query.title, query.album)
    candidate_text = (candidate.artist, candidate.title, candidate.album)
    if any(value is None for value in query_text + candidate_text):
        return False
    if query.duration_ms is None or candidate.duration_ms is None:
        return False

    normalized_query = tuple(normalize_metadata(value) for value in query_text if value)
    normalized_candidate = tuple(
        normalize_metadata(value) for value in candidate_text if value
    )
    if len(normalized_query) != 3 or len(normalized_candidate) != 3:
        return False
    return (
        normalized_query == normalized_candidate
        and abs(query.duration_ms - candidate.duration_ms) <= _DURATION_TOLERANCE_MS
    )


def _result(track_ids: Iterable[int], basis: MatchBasis) -> MatchResult:
    ordered = tuple(sorted(set(track_ids)))
    status = MatchStatus.MATCHED if len(ordered) == 1 else MatchStatus.AMBIGUOUS
    return MatchResult(status=status, track_ids=ordered, basis=basis)


def match_track(
    query: TrackFacts,
    candidates: Iterable[TrackFacts],
    *,
    trusted_source_systems: frozenset[str] = frozenset(),
) -> MatchResult:
    """Return a deterministic result without mutating candidates or a catalog."""

    candidate_tuple = tuple(candidates)
    track_ids = tuple(candidate.track_id for candidate in candidate_tuple)
    if any(track_id is None for track_id in track_ids):
        raise ValueError("catalog candidates require track_id")
    if len(set(track_ids)) != len(track_ids):
        raise ValueError("catalog candidate track_id values must be unique")

    evidence = (
        (MatchBasis.ISRC, _matching_ids(query.isrcs, candidate_tuple, "isrcs")),
        (
            MatchBasis.RECORDING_MBID,
            _matching_ids(query.recording_mbids, candidate_tuple, "recording_mbids"),
        ),
        (
            MatchBasis.TRUSTED_SOURCE_CROSSWALK,
            _crosswalk_matches(query, candidate_tuple, trusted_source_systems),
        ),
    )
    present = tuple((basis, ids) for basis, ids in evidence if ids)
    if present:
        primary_basis, primary_ids = present[0]
        all_ids = frozenset().union(*(ids for _, ids in present))
        evidence_conflicts = any(ids != primary_ids for _, ids in present[1:])
        candidate_conflicts = any(
            _candidate_has_identifier_conflict(
                query, candidate, trusted_source_systems
            )
            for candidate in candidate_tuple
            if candidate.track_id in all_ids
        )
        if evidence_conflicts or candidate_conflicts:
            return _result(all_ids, MatchBasis.CONTRADICTORY_IDENTIFIERS)
        return _result(primary_ids, primary_basis)

    metadata_ids = frozenset(
        candidate.track_id
        for candidate in candidate_tuple
        if _metadata_matches(query, candidate) and candidate.track_id is not None
    )
    if not metadata_ids:
        return MatchResult(MatchStatus.NO_MATCH, (), None)

    conflicting_metadata = any(
        _candidate_has_identifier_conflict(query, candidate, trusted_source_systems)
        for candidate in candidate_tuple
        if candidate.track_id in metadata_ids
    )
    basis = (
        MatchBasis.CONTRADICTORY_IDENTIFIERS
        if conflicting_metadata
        else MatchBasis.METADATA_DURATION
    )
    return _result(metadata_ids, basis)
