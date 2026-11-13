"""Tests for bounded, read-only DAAP metadata snapshots."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock

from studios_music.daap import (
    DaapClient,
    DmapLimits,
    DmapProtocolError,
    HttpResponse,
    SnapshotLimits,
    parse_dmap,
    snapshot_daap,
)


def tlv(tag: str, value: bytes) -> bytes:
    return tag.encode("ascii") + struct.pack(">I", len(value)) + value


def negotiation_prefix() -> list[HttpResponse]:
    return [
        HttpResponse(
            tlv("msrv", tlv("mstt", struct.pack(">I", 200)) + tlv("minm", b"NASServer")),
            {"DAAP-Server": "mt-daapd/0.2.4.1"},
        ),
        HttpResponse(
            tlv("mlog", tlv("mstt", struct.pack(">I", 200)) + tlv("mlid", struct.pack(">I", 1))),
            {},
        ),
        HttpResponse(
            tlv(
                "avdb",
                tlv("mstt", struct.pack(">I", 200))
                + tlv("mlcl", tlv("mlit", tlv("miid", struct.pack(">I", 1)) + tlv("minm", b"NASServer"))),
            ),
            {},
        ),
    ]


class TestDmapParser(unittest.TestCase):
    def test_parses_nested_tlv_frames_without_guessing_unknown_values(self) -> None:
        payload = tlv(
            "aply",
            tlv("mstt", struct.pack(">I", 200))
            + tlv("mlcl", tlv("mlit", tlv("miid", struct.pack(">I", 7)) + tlv("minm", b"Mix")))
            + tlv("zzzz", b"opaque"),
        )

        frames = parse_dmap(payload)

        self.assertEqual(frames[0].tag, "aply")
        self.assertEqual(frames[0].children[0].tag, "mstt")
        self.assertEqual(frames[0].children[1].children[0].children[1].value, b"Mix")
        self.assertEqual(frames[0].children[2].value, b"opaque")

    def test_rejects_truncated_header_and_declared_payload(self) -> None:
        with self.assertRaisesRegex(DmapProtocolError, "header"):
            parse_dmap(b"minm\x00")
        with self.assertRaisesRegex(DmapProtocolError, "declared length"):
            parse_dmap(b"minm" + struct.pack(">I", 4) + b"x")

    def test_enforces_response_frame_depth_and_value_limits(self) -> None:
        one = tlv("minm", b"x")
        nested = tlv("mlcl", tlv("mlit", tlv("mlcl", one)))
        with self.assertRaisesRegex(DmapProtocolError, "response byte limit"):
            parse_dmap(one, DmapLimits(max_response_bytes=8))
        with self.assertRaisesRegex(DmapProtocolError, "frame limit"):
            parse_dmap(one + one, DmapLimits(max_frames=1))
        with self.assertRaisesRegex(DmapProtocolError, "depth limit"):
            parse_dmap(nested, DmapLimits(max_depth=2))
        with self.assertRaisesRegex(DmapProtocolError, "value length limit"):
            parse_dmap(tlv("minm", b"12345"), DmapLimits(max_value_bytes=4))


class TestDaapSnapshot(unittest.TestCase):
    def test_negotiates_metadata_without_requesting_any_container_items_by_default(self) -> None:
        responses = [
            HttpResponse(
                tlv("msrv", tlv("mstt", struct.pack(">I", 200)) + tlv("minm", b"NASServer")),
                {"DAAP-Server": "mt-daapd/0.2.4.1"},
            ),
            HttpResponse(
                tlv("mlog", tlv("mstt", struct.pack(">I", 200)) + tlv("mlid", struct.pack(">I", 1))),
                {},
            ),
            HttpResponse(
                tlv(
                    "avdb",
                    tlv("mstt", struct.pack(">I", 200))
                    + tlv("mtco", struct.pack(">I", 1))
                    + tlv("mrco", struct.pack(">I", 1))
                    + tlv(
                        "mlcl",
                        tlv(
                            "mlit",
                            tlv("miid", struct.pack(">I", 1))
                            + tlv("minm", b"NASServer")
                            + tlv("mimc", struct.pack(">I", 19_273))
                            + tlv("mctc", struct.pack(">I", 28)),
                        ),
                    ),
                ),
                {},
            ),
            HttpResponse(
                tlv(
                    "aply",
                    tlv("mstt", struct.pack(">I", 200))
                    + tlv("mtco", struct.pack(">I", 2))
                    + tlv("mrco", struct.pack(">I", 2))
                    + tlv(
                        "mlcl",
                        tlv(
                            "mlit",
                            tlv("miid", struct.pack(">I", 1))
                            + tlv("minm", b"NASServer")
                            + tlv("mimc", struct.pack(">I", 19_273)),
                        )
                        + tlv(
                            "mlit",
                            tlv("miid", struct.pack(">I", 5_963_829))
                            + tlv("minm", b"LOST Playlist")
                            + tlv("mimc", struct.pack(">I", 24)),
                        ),
                    ),
                ),
                {},
            ),
        ]
        requested: list[tuple[str, float, int]] = []

        def transport(url: str, timeout: float, max_bytes: int) -> HttpResponse:
            requested.append((url, timeout, max_bytes))
            return responses.pop(0)

        client = DaapClient(
            "http://192.168.50.140:3689",
            limits=SnapshotLimits(request_timeout_seconds=2, total_timeout_seconds=10),
            transport=transport,
        )
        result = snapshot_daap(client, database_id=1)

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["server"]["name"], "NASServer")
        self.assertEqual(result["server"]["software"], "mt-daapd/0.2.4.1")
        self.assertEqual(result["database"]["item_count"], 19_273)
        self.assertEqual(result["containers"]["returned_count"], 2)
        self.assertEqual(result["containers"]["items"][1]["name"], "LOST Playlist")
        self.assertEqual(result["item_snapshots"], [])
        self.assertEqual(
            [entry[0] for entry in requested],
            [
                "http://192.168.50.140:3689/server-info",
                "http://192.168.50.140:3689/login?pairing-guid=0x1",
                "http://192.168.50.140:3689/databases?session-id=1",
                "http://192.168.50.140:3689/databases/1/containers?session-id=1&revision-number=1",
            ],
        )
        self.assertTrue(all(entry[1] <= 2 for entry in requested))
        self.assertEqual(result["provenance"]["method"], "GET")
        self.assertIn("unencrypted", result["provenance"]["warning"].lower())
        self.assertFalse(result["provenance"]["audio_fetched"])
        self.assertFalse(result["provenance"]["write_endpoints_used"])
        self.assertEqual(responses, [])

    def test_explicit_container_item_snapshot_is_bounded_and_read_only(self) -> None:
        responses = negotiation_prefix()
        responses.extend(
            [
                HttpResponse(
                    tlv(
                        "aply",
                        tlv("mstt", struct.pack(">I", 200))
                        + tlv("mtco", struct.pack(">I", 1))
                        + tlv("mrco", struct.pack(">I", 1))
                        + tlv(
                            "mlcl",
                            tlv(
                                "mlit",
                                tlv("miid", struct.pack(">I", 5_963_829))
                                + tlv("minm", b"LOST Playlist")
                                + tlv("mimc", struct.pack(">I", 1)),
                            ),
                        ),
                    ),
                    {},
                ),
                HttpResponse(
                    tlv(
                        "apso",
                        tlv("mstt", struct.pack(">I", 200))
                        + tlv("mtco", struct.pack(">I", 1))
                        + tlv("mrco", struct.pack(">I", 1))
                        + tlv(
                            "mlcl",
                            tlv(
                                "mlit",
                                tlv("miid", struct.pack(">I", 42))
                                + tlv("minm", b"Recovered Song")
                                + tlv("asar", b"Recovered Artist")
                                + tlv("asal", b"Recovered Album")
                                + tlv("astm", struct.pack(">I", 180_000)),
                            ),
                        ),
                    ),
                    {},
                ),
            ]
        )
        requested: list[str] = []

        def transport(url: str, _timeout: float, _max_bytes: int) -> HttpResponse:
            requested.append(url)
            return responses.pop(0)

        result = snapshot_daap(
            DaapClient("http://192.168.50.140:3689", transport=transport),
            database_id=1,
            container_ids=(5_963_829,),
        )

        self.assertTrue(result["complete"])
        snapshot = result["item_snapshots"][0]
        self.assertEqual(snapshot["container_id"], 5_963_829)
        self.assertEqual(snapshot["returned_count"], 1)
        self.assertEqual(snapshot["items"][0]["title"], "Recovered Song")
        self.assertEqual(snapshot["items"][0]["duration_ms"], 180_000)
        self.assertEqual(
            requested[-1],
            "http://192.168.50.140:3689/databases/1/containers/5963829/items"
            "?session-id=1&revision-number=1&meta=dmap.itemid%2Cdmap.itemname%2C"
            "daap.songartist%2Cdaap.songalbum%2Cdaap.songgenre%2Cdaap.songtime%2Cdaap.songyear",
        )
        self.assertEqual(responses, [])

    def test_selected_container_over_item_cap_is_incomplete_without_item_request(self) -> None:
        responses = negotiation_prefix()
        responses.append(
            HttpResponse(
                tlv(
                    "aply",
                    tlv("mstt", struct.pack(">I", 200))
                    + tlv("mlcl", tlv("mlit", tlv("miid", struct.pack(">I", 1)) + tlv("minm", b"NASServer") + tlv("mimc", struct.pack(">I", 19_273)))),
                ),
                {},
            )
        )
        requested: list[str] = []

        def transport(url: str, _timeout: float, _max_bytes: int) -> HttpResponse:
            requested.append(url)
            return responses.pop(0)

        result = snapshot_daap(
            DaapClient(
                "http://192.168.50.140:3689",
                limits=SnapshotLimits(max_items_per_container=100),
                transport=transport,
            ),
            database_id=1,
            container_ids=(1,),
        )

        self.assertFalse(result["complete"])
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["errors"][0]["stage"], "container-items:1")
        self.assertEqual(result["item_snapshots"][0]["status"], "error")
        self.assertIsNone(result["item_snapshots"][0]["items"])
        self.assertEqual(len(requested), 4)
        self.assertEqual(responses, [])

    def test_timeout_is_an_explicit_incomplete_stage_not_an_empty_listing(self) -> None:
        responses = negotiation_prefix()

        def transport(_url: str, _timeout: float, _max_bytes: int) -> HttpResponse:
            if responses:
                return responses.pop(0)
            raise TimeoutError("simulated stalled container listing")

        result = snapshot_daap(
            DaapClient("http://192.168.50.140:3689", transport=transport),
            database_id=1,
        )

        self.assertEqual(result["status"], "incomplete")
        self.assertFalse(result["complete"])
        self.assertEqual(result["containers"]["status"], "error")
        self.assertIsNone(result["containers"]["items"])
        self.assertEqual(result["errors"][0]["stage"], "containers")
        self.assertEqual(result["errors"][0]["type"], "timeout")
        self.assertIn("stalled", result["errors"][0]["message"])


class TestDaapSnapshotCli(unittest.TestCase):
    def test_default_is_dry_run_and_does_not_create_output(self) -> None:
        from scripts.music_daap_snapshot import main

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "snapshot.json"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                status = main(
                    [
                        "--base-url",
                        "http://192.168.50.140:3689",
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(status, 0)
            self.assertFalse(output.exists())
            plan = json.loads(stdout.getvalue())
            self.assertTrue(plan["dry_run"])
            self.assertFalse(plan["network_accessed"])
            self.assertIn("--execute", plan["next_step"])

    def test_execute_atomically_writes_snapshot_receipt(self) -> None:
        from scripts import music_daap_snapshot

        result = {
            "schema_version": 1,
            "status": "complete",
            "complete": True,
            "errors": [],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "snapshot.json"
            with mock.patch.object(music_daap_snapshot, "snapshot_daap", return_value=result):
                status = music_daap_snapshot.main(
                    [
                        "--base-url",
                        "http://192.168.50.140:3689",
                        "--output",
                        str(output),
                        "--execute",
                    ]
                )

            self.assertEqual(status, 0)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), result)
            self.assertEqual([path.name for path in output.parent.iterdir()], ["snapshot.json"])


if __name__ == "__main__":
    unittest.main()
