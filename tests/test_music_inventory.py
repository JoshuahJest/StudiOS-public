"""Tests for bounded, read-only legacy music inventory."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from studios_music.inventory import (
    InventoryBudget,
    inventory_roots,
    inventory_roots_hard_timeout,
)


class TestMusicInventory(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "candidate"
        (self.root / "Previous iTunes Libraries").mkdir(parents=True)
        (self.root / "Album Artwork").mkdir()
        (self.root / "iTunes Media" / "Music").mkdir(parents=True)
        (self.root / "Music Library.musiclibrary").mkdir()
        sparsebundle = self.root / "Icarus III.sparsebundle"
        (sparsebundle / "bands").mkdir(parents=True)
        (sparsebundle / "bands" / "0").write_bytes(b"band")
        (self.root / "iTunes Library.itl").write_bytes(b"legacy-db")
        (self.root / "iTunes Music Library.xml").write_text("<plist/>", encoding="utf-8")
        (self.root / "Previous iTunes Libraries" / "iTunes Library 2020.itl").write_bytes(
            b"older-db"
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_inventory_classifies_legacy_itunes_artifacts(self) -> None:
        result = inventory_roots(
            [self.root],
            InventoryBudget(max_depth=3, max_entries=100, max_seconds=10),
        )
        by_kind = {}
        for entry in result["entries"]:
            by_kind.setdefault(entry["kind"], []).append(entry["relative_path"])
        paths = {entry["relative_path"] for entry in result["entries"]}

        self.assertIn("iTunes Library.itl", by_kind["itunes_database"])
        self.assertIn("iTunes Music Library.xml", by_kind["itunes_xml"])
        self.assertIn("Previous iTunes Libraries", by_kind["previous_libraries"])
        self.assertIn("Album Artwork", by_kind["artwork_directory"])
        self.assertIn("iTunes Media", by_kind["media_directory"])
        self.assertIn("Music Library.musiclibrary", by_kind["music_library_bundle"])
        self.assertIn("Icarus III.sparsebundle", by_kind["disk_image_bundle"])
        self.assertNotIn("Icarus III.sparsebundle/bands", paths)
        self.assertFalse(result["truncated"])
        self.assertEqual(result["errors"], [])

    def test_inventory_hashes_small_database_and_xml_files(self) -> None:
        result = inventory_roots(
            [self.root],
            InventoryBudget(max_depth=1, max_entries=100, max_seconds=10),
            hash_file_max_bytes=1024,
        )
        files = {
            entry["relative_path"]: entry
            for entry in result["entries"]
            if entry["entry_type"] == "file"
        }
        self.assertRegex(files["iTunes Library.itl"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(files["iTunes Music Library.xml"]["sha256"], r"^[0-9a-f]{64}$")

    def test_inventory_stops_at_entry_budget(self) -> None:
        for index in range(20):
            (self.root / f"unrelated-{index}.txt").write_text("x", encoding="utf-8")
        result = inventory_roots(
            [self.root],
            InventoryBudget(max_depth=3, max_entries=5, max_seconds=10),
        )
        self.assertTrue(result["truncated"])
        self.assertEqual(result["truncation_reason"], "entry budget exceeded")
        self.assertLessEqual(len(result["entries"]), 5)

    def test_inventory_interrupts_a_blocked_directory_listing(self) -> None:
        def blocked_scandir(_path: object) -> list[object]:
            time.sleep(1)
            return []

        started = time.monotonic()
        with patch("studios_music.inventory.os.scandir", blocked_scandir):
            result = inventory_roots(
                [self.root],
                InventoryBudget(max_depth=1, max_entries=10, max_seconds=0.05),
            )
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.5)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["truncation_reason"], "time budget exceeded")

    def test_hard_timeout_terminates_an_uninterruptible_worker(self) -> None:
        def uninterruptible_listing(_directory: object, _seconds: float) -> list[object]:
            time.sleep(2)
            return []

        started = time.monotonic()
        with patch(
            "studios_music.inventory._scandir_with_timeout",
            uninterruptible_listing,
        ):
            result = inventory_roots_hard_timeout(
                [self.root],
                InventoryBudget(max_depth=1, max_entries=10, max_seconds=0.1),
            )
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1.0)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["truncation_reason"], "hard time budget exceeded")
        self.assertIn("worker was terminated", result["errors"][0]["error"])

    def test_missing_root_is_an_error_not_an_empty_inventory(self) -> None:
        missing = Path(self.tmp.name) / "does-not-exist"
        result = inventory_roots(
            [missing],
            InventoryBudget(max_depth=1, max_entries=10, max_seconds=10),
        )
        self.assertEqual(result["entries"], [])
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("does not exist", result["errors"][0]["error"])

    def test_inventory_order_is_deterministic(self) -> None:
        budget = InventoryBudget(max_depth=3, max_entries=100, max_seconds=10)
        first = inventory_roots([self.root], budget)
        second = inventory_roots([self.root], budget)
        first_paths = [entry["relative_path"] for entry in first["entries"]]
        second_paths = [entry["relative_path"] for entry in second["entries"]]
        self.assertEqual(first_paths, second_paths)

    def test_cli_writes_json_manifest(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        output = Path(self.tmp.name) / "inventory.json"
        completed = subprocess.run(
            [
                sys.executable,
                str(repo_root / "scripts" / "music_inventory.py"),
                str(self.root),
                "--max-depth",
                "2",
                "--max-entries",
                "100",
                "--max-seconds",
                "10",
                "--output",
                str(output),
            ],
            cwd=repo_root,
            env={"PYTHONPATH": str(repo_root / "src")},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["errors"], [])
        self.assertTrue(any(entry["kind"] == "itunes_database" for entry in payload["entries"]))


if __name__ == "__main__":
    unittest.main()
