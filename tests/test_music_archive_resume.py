"""Resumable-transport regression tests for the SSH source archive."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from studios_music import archive
from studios_music.archive import (
    ArchiveTimeoutError,
    ArchiveTransportError,
    UnsafeSourceError,
)


class TestArchiveResume(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tempdir.name).resolve()
        self.source = self.base / "Legacy iTunes"
        (self.source / "Album Artwork").mkdir(parents=True)
        (self.source / "iTunes Library.itl").write_bytes(b"legacy-db")
        (self.source / "Album Artwork" / "cache.bin").write_bytes(b"artwork")
        self.archive_root = self.base / "archives"
        self.archive_root.mkdir()
        self.identity = self.base / "id_ed25519"
        self.identity.write_text("test identity placeholder", encoding="utf-8")
        self.known_hosts = self.base / "known_hosts_icarus_iii"
        self.known_hosts.write_text("archive-host.local ssh-ed25519 AAAATEST\n", encoding="utf-8")
        self.transport_log = self.base / "transport.jsonl"
        self.fake_ssh = self._fake_ssh()
        self.fake_rsync = self._fake_rsync()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _fake_ssh(self) -> Path:
        executable = self.base / "fake-ssh"
        executable.write_text(
            f"#!{sys.executable}\n"
            "import json, os, shlex, subprocess, sys, time\n"
            "log = os.environ.get('FAKE_TRANSPORT_LOG')\n"
            "if log:\n"
            "    with open(log, 'a') as handle:\n"
            "        handle.write(json.dumps({'tool': 'ssh', 'argv': sys.argv[1:]}) + '\\n')\n"
            "behavior = os.environ.get('FAKE_SSH_BEHAVIOR', '')\n"
            "if behavior == 'timeout':\n"
            "    time.sleep(30)\n"
            "if behavior == 'connection-reset':\n"
            "    sys.stderr.write('Read from remote host: Connection reset by peer\\n')\n"
            "    raise SystemExit(255)\n"
            "command = sys.argv[-1]\n"
            "parts = shlex.split(command)\n"
            "expected_prefix = ['/usr/bin/caffeinate', '-dimsu', '/usr/bin/python']\n"
            "if parts[:3] != expected_prefix:\n"
            "    sys.stderr.write('remote command did not use caffeinated Python 2 path')\n"
            "    raise SystemExit(91)\n"
            f"parts[2] = {sys.executable!r}\n"
            "raise SystemExit(subprocess.call(parts[2:]))\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        return executable

    def _fake_rsync(self) -> Path:
        executable = self.base / "fake-rsync"
        executable.write_text(
            f"#!{sys.executable}\n"
            "import json, os, shlex, shutil, sys\n"
            "log = os.environ.get('FAKE_TRANSPORT_LOG')\n"
            "if log:\n"
            "    with open(log, 'a') as handle:\n"
            "        handle.write(json.dumps({'tool': 'rsync', 'argv': sys.argv[1:]}) + '\\n')\n"
            "behavior = os.environ.get('FAKE_RSYNC_BEHAVIOR', '')\n"
            "if behavior == 'connection-reset':\n"
            "    sys.stderr.write('Read from remote host: Connection reset by peer\\n')\n"
            "    sys.stderr.write('client_loop: send disconnect: Broken pipe\\n')\n"
            "    raise SystemExit(255)\n"
            "remote = sys.argv[-2].split(':', 1)[1]\n"
            "source = shlex.split(remote)[0].rstrip('/')\n"
            "destination = sys.argv[-1]\n"
            "if destination != '.':\n"
            "    sys.stderr.write('destination was not descriptor-bound cwd')\n"
            "    raise SystemExit(92)\n"
            "shutil.copytree(source, destination, dirs_exist_ok=True, symlinks=True)\n"
            "shutil.copystat(source, destination, follow_symlinks=False)\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        return executable

    def _kwargs(self, **overrides: object) -> dict[str, object]:
        values: dict[str, object] = {
            "source_id": "icarus-iii-itunes-001",
            "source_host": "archive-host.local",
            "source_user": "archive",
            "source_path": str(self.source),
            "identity_file": self.identity,
            "known_hosts_file": self.known_hosts,
            "ssh_executable": str(self.fake_ssh),
            "rsync_executable": str(self.fake_rsync),
            "expected_filesystem_type": "apfs",
            "operation_timeout_seconds": 20,
        }
        values.update(overrides)
        return values

    def _staging(self) -> list[Path]:
        return sorted(self.archive_root.glob(".*.partial*"))

    def test_rsync_connection_reset_is_classified_as_transport_failure(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "FAKE_TRANSPORT_LOG": str(self.transport_log),
                "FAKE_RSYNC_BEHAVIOR": "connection-reset",
            },
        ):
            with self.assertRaises(ArchiveTransportError) as raised:
                archive.archive_ssh_source(self.archive_root, **self._kwargs())

        self.assertIn("Connection reset by peer", str(raised.exception))

    def test_manifest_connection_reset_is_classified_as_transport_failure(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "FAKE_TRANSPORT_LOG": str(self.transport_log),
                "FAKE_SSH_BEHAVIOR": "connection-reset",
            },
        ):
            with self.assertRaises(ArchiveTransportError):
                archive.archive_ssh_source(self.archive_root, **self._kwargs())

    def test_transport_failure_preserves_staging_so_the_next_attempt_resumes(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "FAKE_TRANSPORT_LOG": str(self.transport_log),
                "FAKE_RSYNC_BEHAVIOR": "connection-reset",
            },
        ):
            with self.assertRaises(ArchiveTransportError):
                archive.archive_ssh_source(self.archive_root, **self._kwargs())

        staging = self._staging()
        self.assertEqual(len(staging), 1)
        self.assertTrue((staging[0] / "payload").is_dir())
        self.assertTrue((staging[0] / "resume_state.json").is_file())
        state = json.loads((staging[0] / "resume_state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["source_id"], "icarus-iii-itunes-001")
        self.assertFalse((self.archive_root / "icarus-iii-itunes-001").exists())

    def test_resume_reuses_the_same_staging_directory_and_publishes(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "FAKE_TRANSPORT_LOG": str(self.transport_log),
                "FAKE_RSYNC_BEHAVIOR": "connection-reset",
            },
        ):
            with self.assertRaises(ArchiveTransportError):
                archive.archive_ssh_source(self.archive_root, **self._kwargs())

        staging = self._staging()
        self.assertEqual(len(staging), 1)
        inode = staging[0].stat().st_ino

        with mock.patch.dict(os.environ, {"FAKE_TRANSPORT_LOG": str(self.transport_log)}):
            result = archive.archive_ssh_source(self.archive_root, **self._kwargs())

        self.assertTrue(result["verified"])
        published = self.archive_root / "icarus-iii-itunes-001"
        self.assertTrue((published / "manifest.json").is_file())
        self.assertTrue((published / "payload" / "iTunes Library.itl").is_file())
        self.assertEqual(self._staging(), [])
        self.assertFalse((published / "resume_state.json").exists())

    def test_unsafe_source_failure_still_removes_staging(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "FAKE_TRANSPORT_LOG": str(self.transport_log),
                "FAKE_RSYNC_BEHAVIOR": "connection-reset",
            },
        ):
            with self.assertRaises(ArchiveTransportError):
                archive.archive_ssh_source(self.archive_root, **self._kwargs())

        staging = self._staging()[0]
        # Replace the held staging tree with an unsafe symlink payload; the next
        # attempt must refuse it rather than copy through it.
        (staging / "payload").rmdir()
        (staging / "payload").symlink_to(self.base, target_is_directory=True)

        with mock.patch.dict(os.environ, {"FAKE_TRANSPORT_LOG": str(self.transport_log)}):
            with self.assertRaises(UnsafeSourceError):
                archive.archive_ssh_source(self.archive_root, **self._kwargs())

        self.assertFalse((self.archive_root / "icarus-iii-itunes-001").exists())

    def test_timeout_preserves_staging_for_resume(self) -> None:
        started = time.monotonic()
        with mock.patch.dict(
            os.environ,
            {
                "FAKE_TRANSPORT_LOG": str(self.transport_log),
                "FAKE_SSH_BEHAVIOR": "timeout",
            },
        ):
            with self.assertRaises(ArchiveTimeoutError):
                archive.archive_ssh_source(
                    self.archive_root, **self._kwargs(operation_timeout_seconds=0.2)
                )
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(len(self._staging()), 1)

    def test_staging_name_is_stable_across_attempts(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "FAKE_TRANSPORT_LOG": str(self.transport_log),
                "FAKE_RSYNC_BEHAVIOR": "connection-reset",
            },
        ):
            with self.assertRaises(ArchiveTransportError):
                archive.archive_ssh_source(self.archive_root, **self._kwargs())

        names = [path.name for path in self._staging()]
        self.assertEqual(names, [".icarus-iii-itunes-001.partial"])

    def test_rsync_temporary_names_are_recognised(self) -> None:
        # Shapes observed from openrsync 2.6.9 during an interrupted transfer.
        for name in (
            ".._15.nimhatQUfp",
            ".big.bin.EPEufra9JU",
            ".0000ABCD.EfGhIjKl",
        ):
            self.assertTrue(archive._is_rsync_temporary_name(name), name)

    def test_source_content_names_are_not_mistaken_for_temporaries(self) -> None:
        for name in (
            ".DS_Store",
            ".hidden",
            "..hidden",
            "album.itc",
            ".config.json",
            ".._15",
            ".abc.def",
        ):
            self.assertFalse(archive._is_rsync_temporary_name(name), name)

    def test_rsync_temporary_left_in_payload_is_removed_not_treated_as_content(self) -> None:
        staging = self._archive_root_staging()
        payload = staging / "payload"
        (payload / "Album Artwork").mkdir(parents=True, exist_ok=True)
        stray = payload / "Album Artwork" / ".._15.nimhatQUfp"
        stray.write_bytes(b"partial")
        (payload / "Album Artwork" / "real.itc").write_bytes(b"artwork")

        descriptor = os.open(payload, os.O_RDONLY | os.O_DIRECTORY)
        try:
            archive._remove_rsync_temporaries(descriptor)
            scanned = archive._scan_local_tree(descriptor, archive.ArchiveLimits())
        finally:
            os.close(descriptor)

        names = {entry["relative_path"] for entry in scanned["entries"]}
        self.assertIn("Album Artwork/real.itc", names)
        self.assertNotIn("Album Artwork/.._15.nimhatQUfp", names)

    def test_source_file_matching_the_temporary_shape_is_never_deleted(self) -> None:
        staging = self._archive_root_staging()
        payload = staging / "payload"
        (payload / "Album Artwork").mkdir(parents=True, exist_ok=True)
        genuine = payload / "Album Artwork" / ".._15.nimhatQUfp"
        genuine.write_bytes(b"real source content")

        descriptor = os.open(payload, os.O_RDONLY | os.O_DIRECTORY)
        try:
            removed = archive._remove_rsync_temporaries(
                descriptor, {"Album Artwork/.._15.nimhatQUfp"}
            )
        finally:
            os.close(descriptor)

        self.assertEqual(removed, 0)
        self.assertTrue(genuine.exists())

    def test_rsync_temporaries_are_removed_from_the_payload_before_publishing(self) -> None:
        staging = self._archive_root_staging()
        payload = staging / "payload"
        (payload / "Album Artwork").mkdir(parents=True, exist_ok=True)
        stray = payload / "Album Artwork" / ".._15.nimhatQUfp"
        stray.write_bytes(b"partial")

        descriptor = os.open(payload, os.O_RDONLY | os.O_DIRECTORY)
        try:
            removed = archive._remove_rsync_temporaries(descriptor)
        finally:
            os.close(descriptor)

        self.assertEqual(removed, 1)
        self.assertFalse(stray.exists())

    def test_resume_publishes_even_when_a_temporary_survives_the_transfer(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "FAKE_TRANSPORT_LOG": str(self.transport_log),
                "FAKE_RSYNC_BEHAVIOR": "connection-reset",
            },
        ):
            with self.assertRaises(ArchiveTransportError):
                archive.archive_ssh_source(self.archive_root, **self._kwargs())

        staging = self._staging()[0]
        # A previous interrupted attempt left rsync's own temporary behind.
        (staging / "payload" / "Album Artwork").mkdir(parents=True, exist_ok=True)
        (staging / "payload" / "Album Artwork" / ".._15.nimhatQUfp").write_bytes(
            b"partial"
        )

        with mock.patch.dict(os.environ, {"FAKE_TRANSPORT_LOG": str(self.transport_log)}):
            result = archive.archive_ssh_source(self.archive_root, **self._kwargs())

        self.assertTrue(result["verified"])
        published = self.archive_root / "icarus-iii-itunes-001"
        self.assertFalse(
            (published / "payload" / "Album Artwork" / ".._15.nimhatQUfp").exists()
        )

    def test_directory_times_are_restored_after_a_resume(self) -> None:
        staging = self._archive_root_staging()
        payload = staging / "payload"
        (payload / "Album Artwork").mkdir(parents=True, exist_ok=True)
        (payload / "Album Artwork" / "real.itc").write_bytes(b"artwork")
        # Writing a file into a directory changes that directory's mtime.
        os.utime(payload / "Album Artwork", (1_600_000_000, 1_600_000_000))

        descriptor = os.open(payload, os.O_RDONLY | os.O_DIRECTORY)
        try:
            applied = archive._apply_directory_times(
                descriptor,
                [
                    {
                        "type": "directory",
                        "relative_path": "Album Artwork",
                        "mtime_seconds": 1_420_000_123,
                    }
                ],
            )
            scanned = archive._scan_local_tree(descriptor, archive.ArchiveLimits())
        finally:
            os.close(descriptor)

        self.assertEqual(applied, 1)
        entries = {e["relative_path"]: e for e in scanned["entries"]}
        self.assertEqual(entries["Album Artwork"]["mtime_seconds"], 1_420_000_123)

    def test_removing_a_temporary_does_not_break_the_resume(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "FAKE_TRANSPORT_LOG": str(self.transport_log),
                "FAKE_RSYNC_BEHAVIOR": "connection-reset",
            },
        ):
            with self.assertRaises(ArchiveTransportError):
                archive.archive_ssh_source(self.archive_root, **self._kwargs())

        staging = self._staging()[0]
        artwork = staging / "payload" / "Album Artwork"
        artwork.mkdir(parents=True, exist_ok=True)
        (artwork / ".._15.nimhatQUfp").write_bytes(b"partial")

        with mock.patch.dict(os.environ, {"FAKE_TRANSPORT_LOG": str(self.transport_log)}):
            result = archive.archive_ssh_source(self.archive_root, **self._kwargs())

        self.assertTrue(result["verified"])
        published = self.archive_root / "icarus-iii-itunes-001"
        self.assertFalse((published / "payload" / "Album Artwork" / ".._15.nimhatQUfp").exists())

    def _archive_root_staging(self) -> Path:
        """Create a staging tree the same way the archiver does."""

        staging = self.archive_root / ".icarus-iii-itunes-001.partial"
        (staging / "payload").mkdir(parents=True, exist_ok=True)
        (staging / "resume_state.json").write_text(
            json.dumps(
                {
                    "attempt": 1,
                    "schema_version": 1,
                    "source_host": "archive-host.local",
                    "source_id": "icarus-iii-itunes-001",
                    "source_path": str(self.source),
                    "source_user": "archive",
                    "updated_at": "2026-10-26T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        return staging


if __name__ == "__main__":
    unittest.main()
