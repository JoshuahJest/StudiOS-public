"""Tests for deterministic, read-only music track matching."""

from __future__ import annotations

import unittest

from studios_music.matching import (
    MatchBasis,
    MatchStatus,
    SourceIdentifier,
    TrackFacts,
    match_track,
    normalize_metadata,
)


class TestMetadataNormalization(unittest.TestCase):
    def test_nfkc_casefolds_and_replaces_punctuation_and_separators(self) -> None:
        self.assertEqual(
            normalize_metadata("  BJÖRK／Army—of:Me (Live)  "),
            "björk army of me live",
        )

    def test_normalization_retains_diacritics_and_version_words(self) -> None:
        self.assertNotEqual(normalize_metadata("Beyoncé"), normalize_metadata("Beyonce"))
        self.assertNotEqual(
            normalize_metadata("Song (Live)"),
            normalize_metadata("Song (Studio)"),
        )


class TestDeterministicMatching(unittest.TestCase):
    def test_isrc_matches_before_lower_priority_evidence(self) -> None:
        query = TrackFacts(isrcs=("USAAA2600001",))
        candidate = TrackFacts(track_id=7, isrcs=("USAAA2600001",))

        result = match_track(query, (candidate,))

        self.assertEqual(result.status, MatchStatus.MATCHED)
        self.assertEqual(result.track_ids, (7,))
        self.assertEqual(result.basis, MatchBasis.ISRC)
        self.assertEqual(result.track_id, 7)

    def test_recording_mbid_matches_but_release_mbid_does_not(self) -> None:
        recording = match_track(
            TrackFacts(recording_mbids=("recording-1",)),
            (TrackFacts(track_id=1, recording_mbids=("recording-1",)),),
        )
        release_only = match_track(
            TrackFacts(release_mbids=("release-1",)),
            (TrackFacts(track_id=1, release_mbids=("release-1",)),),
        )

        self.assertEqual(recording.status, MatchStatus.MATCHED)
        self.assertEqual(recording.basis, MatchBasis.RECORDING_MBID)
        self.assertEqual(release_only.status, MatchStatus.NO_MATCH)

    def test_only_trusted_source_crosswalks_match_and_ids_are_opaque(self) -> None:
        candidate = TrackFacts(
            track_id=3,
            source_ids=(SourceIdentifier("spotify", "OpaqueCase"),),
        )

        trusted = match_track(
            TrackFacts(source_ids=(SourceIdentifier("spotify", "OpaqueCase"),)),
            (candidate,),
            trusted_source_systems=frozenset({"spotify"}),
        )
        wrong_case = match_track(
            TrackFacts(source_ids=(SourceIdentifier("spotify", "opaquecase"),)),
            (candidate,),
            trusted_source_systems=frozenset({"spotify"}),
        )
        untrusted = match_track(
            TrackFacts(source_ids=(SourceIdentifier("spotify", "OpaqueCase"),)),
            (candidate,),
        )

        self.assertEqual(trusted.basis, MatchBasis.TRUSTED_SOURCE_CROSSWALK)
        self.assertEqual(wrong_case.status, MatchStatus.NO_MATCH)
        self.assertEqual(untrusted.status, MatchStatus.NO_MATCH)

    def test_metadata_match_uses_all_fields_and_inclusive_duration_window(self) -> None:
        query = TrackFacts(
            artist="Björk",
            title="Army—of Me (Live)",
            album="Post",
            duration_ms=240000,
        )
        candidate = TrackFacts(
            track_id=11,
            artist="BJÖRK",
            title="army of me live",
            album="POST",
            duration_ms=242000,
        )

        result = match_track(query, (candidate,))

        self.assertEqual(result.status, MatchStatus.MATCHED)
        self.assertEqual(result.basis, MatchBasis.METADATA_DURATION)

    def test_metadata_does_not_fuzzy_match_or_exceed_duration_window(self) -> None:
        query = TrackFacts(
            artist="Beyoncé",
            title="Halo (Live)",
            album="I Am... Sasha Fierce",
            duration_ms=240000,
        )
        candidates = (
            TrackFacts(
                track_id=1,
                artist="Beyonce",
                title="Halo (Live)",
                album="I Am... Sasha Fierce",
                duration_ms=240000,
            ),
            TrackFacts(
                track_id=2,
                artist="Beyoncé",
                title="Halo (Studio)",
                album="I Am... Sasha Fierce",
                duration_ms=240000,
            ),
            TrackFacts(
                track_id=3,
                artist="Beyoncé",
                title="Halo (Live)",
                album="I Am... Sasha Fierce",
                duration_ms=242001,
            ),
        )

        self.assertEqual(match_track(query, candidates).status, MatchStatus.NO_MATCH)

    def test_exact_identifier_mapping_to_multiple_tracks_is_ambiguous(self) -> None:
        result = match_track(
            TrackFacts(isrcs=("USAAA2600001",)),
            (
                TrackFacts(track_id=9, isrcs=("USAAA2600001",)),
                TrackFacts(track_id=4, isrcs=("USAAA2600001",)),
            ),
        )

        self.assertEqual(result.status, MatchStatus.AMBIGUOUS)
        self.assertEqual(result.track_ids, (4, 9))
        self.assertEqual(result.basis, MatchBasis.ISRC)
        self.assertIsNone(result.track_id)

    def test_contradictory_exact_identifiers_are_ambiguous(self) -> None:
        result = match_track(
            TrackFacts(
                isrcs=("USAAA2600001",),
                recording_mbids=("recording-b",),
            ),
            (
                TrackFacts(track_id=1, isrcs=("USAAA2600001",)),
                TrackFacts(track_id=2, recording_mbids=("recording-b",)),
            ),
        )

        self.assertEqual(result.status, MatchStatus.AMBIGUOUS)
        self.assertEqual(result.track_ids, (1, 2))
        self.assertEqual(result.basis, MatchBasis.CONTRADICTORY_IDENTIFIERS)

    def test_metadata_multiple_matches_remain_ambiguous_and_sorted(self) -> None:
        common = dict(
            artist="Artist",
            title="Title",
            album="Album",
            duration_ms=100000,
        )
        result = match_track(
            TrackFacts(**common),
            (TrackFacts(track_id=8, **common), TrackFacts(track_id=3, **common)),
        )

        self.assertEqual(result.status, MatchStatus.AMBIGUOUS)
        self.assertEqual(result.track_ids, (3, 8))
        self.assertEqual(result.basis, MatchBasis.METADATA_DURATION)


if __name__ == "__main__":
    unittest.main()
