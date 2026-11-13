"""Regression tests for fail-closed legacy-library source archives."""

from __future__ import annotations

import hashlib
import fcntl
import importlib.util
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from studios_music import archive
from studios_music.archive import (
    ArchiveCleanupError,
    ArchiveExistsError,
    ArchiveTimeoutError,
    ArchiveTransportError,
    UnsafeSourceError,
    archive_source,
)


class TestArchiveSource(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        # macOS returns /var/... even though /var is a symlink to /private/var;
        # use the canonical test root so ancestor-symlink rejection remains strict.
        self.base = Path(self.tempdir.name).resolve()
        self.source = self.base / "Legacy iTunes"
        (self.source / "Album Artwork").mkdir(parents=True)
        (self.source / "iTunes Library.itl").write_bytes(b"legacy-db")
        (self.source / "iTunes Music Library.xml").write_text(
            "<plist/>", encoding="utf-8"
        )
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
            "if behavior == 'pipe-hold':\n"
            "    subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(2)'])\n"
            "    raise SystemExit(0)\n"
            "if behavior == 'stderr':\n"
            "    sys.stderr.write('X' * 1000000)\n"
            "    raise SystemExit(7)\n"
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
            "import json, os, shlex, shutil, sys, time\n"
            "log = os.environ.get('FAKE_TRANSPORT_LOG')\n"
            "if log:\n"
            "    with open(log, 'a') as handle:\n"
            "        handle.write(json.dumps({'tool': 'rsync', 'argv': sys.argv[1:]}) + '\\n')\n"
            "behavior = os.environ.get('FAKE_RSYNC_BEHAVIOR', '')\n"
            "if behavior == 'timeout':\n"
            "    time.sleep(30)\n"
            "remote = sys.argv[-2].split(':', 1)[1]\n"
            "source = shlex.split(remote)[0].rstrip('/')\n"
            "destination = sys.argv[-1]\n"
            "if destination != '.':\n"
            "    sys.stderr.write('destination was not descriptor-bound cwd')\n"
            "    raise SystemExit(92)\n"
            "original_cwd = os.getcwd()\n"
            "if behavior == 'redirect-before-open':\n"
            "    moved = original_cwd + '.moved'\n"
            "    os.rename(original_cwd, moved)\n"
            "    os.symlink(os.environ['FAKE_ATTACKER_DIR'], original_cwd)\n"
            "if behavior == 'symlink-race':\n"
            "    victim = os.path.join(source, 'iTunes Library.itl')\n"
            "    outside = os.environ['FAKE_OUTSIDE_FILE']\n"
            "    os.unlink(victim)\n"
            "    os.symlink(outside, victim)\n"
            "    shutil.copytree(source, destination, dirs_exist_ok=True, symlinks=True)\n"
            "else:\n"
            "    shutil.copytree(source, destination, dirs_exist_ok=True, symlinks=True)\n"
            "shutil.copystat(source, destination, follow_symlinks=False)\n"
            "if behavior == 'source-change':\n"
            "    with open(os.path.join(source, 'iTunes Library.itl'), 'ab') as handle:\n"
            "        handle.write(b'-changed')\n"
            "if behavior == 'destination-race':\n"
            "    final = os.path.join(os.path.dirname(os.path.dirname(original_cwd)), os.environ['FAKE_FINAL_NAME'])\n"
            "    os.mkdir(final)\n"
            "    with open(os.path.join(final, 'intruder'), 'wb') as handle:\n"
            "        handle.write(b'keep')\n"
            "if behavior == 'tamper-staging':\n"
            "    moved = original_cwd + '.moved'\n"
            "    os.rename(original_cwd, moved)\n"
            "    attacker = os.environ['FAKE_ATTACKER_DIR']\n"
            "    os.symlink(attacker, original_cwd)\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        return executable

    def _ssh_kwargs(self, **overrides: object) -> dict[str, object]:
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

    def _run_ssh(self, **overrides: object) -> dict[str, object]:
        with mock.patch.dict(os.environ, {"FAKE_TRANSPORT_LOG": str(self.transport_log)}):
            return archive.archive_ssh_source(
                self.archive_root, **self._ssh_kwargs(**overrides)
            )

    def _transport_records(self) -> list[dict[str, object]]:
        return [
            json.loads(line)
            for line in self.transport_log.read_text(encoding="utf-8").splitlines()
        ]

    def test_ssh_archive_binds_every_transport_to_explicit_known_hosts(self) -> None:
        self._run_ssh()

        records = self._transport_records()
        ssh_argv = next(record["argv"] for record in records if record["tool"] == "ssh")
        rsync_argv = next(record["argv"] for record in records if record["tool"] == "rsync")
        expected = f"UserKnownHostsFile={self.known_hosts.absolute()}"
        self.assertIn(expected, ssh_argv)
        self.assertIn("GlobalKnownHostsFile=/dev/null", ssh_argv)
        remote_shell = rsync_argv[rsync_argv.index("-e") + 1]
        self.assertIn(shlex.quote(str(self.known_hosts.absolute())), remote_shell)
        self.assertIn("StrictHostKeyChecking=yes", remote_shell)
        self.assertNotIn("UserKnownHostsFile=~", remote_shell)
        self.assertEqual(rsync_argv[-1], ".")

    def test_rsync_destination_stays_bound_if_payload_name_is_redirected(self) -> None:
        attacker = self.base / "attacker-destination"
        attacker.mkdir()
        with mock.patch.dict(
            os.environ,
            {
                "FAKE_TRANSPORT_LOG": str(self.transport_log),
                "FAKE_RSYNC_BEHAVIOR": "redirect-before-open",
                "FAKE_ATTACKER_DIR": str(attacker),
            },
        ):
            with self.assertRaisesRegex(UnsafeSourceError, "staging payload"):
                archive.archive_ssh_source(self.archive_root, **self._ssh_kwargs())

        self.assertEqual(list(attacker.iterdir()), [])
        self.assertEqual(list(self.archive_root.glob(".*.partial*")), [])

    def test_production_runner_prevents_local_sleep_for_the_entire_archive(self) -> None:
        runner = (Path(__file__).parents[1] / "scripts" / "run_icarus_archive.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "/usr/bin/caffeinate -dimsu env PYTHONPATH=src \"$PYTHON\" scripts/music_archive.py",
            runner,
        )

    def test_production_runner_retries_transport_interruptions_without_losing_progress(self) -> None:
        runner = (Path(__file__).parents[1] / "scripts" / "run_icarus_archive.sh").read_text(
            encoding="utf-8"
        )
        # Exit 75 is the archive CLI's transport-interrupted code. The runner
        # must retry on it, because the partial copy is preserved and each
        # retry resumes rather than restarting the transfer.
        self.assertIn('if [[ "$rc" -eq 75 ]]', runner)
        self.assertIn("attempt=$((attempt + 1))", runner)
        self.assertIn("MAX_ATTEMPTS", runner)

    def test_cli_reports_transport_interruption_with_a_resume_exit_code(self) -> None:
        script = Path(__file__).parents[1] / "scripts" / "music_archive.py"
        spec = importlib.util.spec_from_file_location("music_archive_cli", script)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        argv = [
            "music_archive.py",
            "--archive-root",
            str(self.archive_root),
            "--source-id",
            "icarus-iii-itunes-001",
            "--ssh-host",
            "archive-host.local",
            "--ssh-user",
            "archive",
            "--ssh-source-path",
            str(self.source),
            "--ssh-identity",
            str(self.identity),
            "--ssh-known-hosts",
            str(self.known_hosts),
            "--expected-filesystem-type",
            "apfs",
            "--execute",
        ]
        with mock.patch.object(sys, "argv", argv):
            with mock.patch.object(
                module,
                "archive_ssh_source",
                side_effect=ArchiveTransportError("rsync archive transfer was interrupted"),
            ):
                with mock.patch.object(sys, "stderr", io.StringIO()) as stderr:
                    self.assertEqual(module.main(), 75)

        self.assertIn("TRANSPORT INTERRUPTED", stderr.getvalue())
        self.assertIn("preserved", stderr.getvalue())

    def test_remote_manifest_uses_caffeinated_system_python_not_python3(self) -> None:
        self._run_ssh()

        records = self._transport_records()
        ssh_records = [record for record in records if record["tool"] == "ssh"]
        self.assertEqual(len(ssh_records), 2)
        for record in ssh_records:
            argv = record["argv"]
            self.assertIsInstance(argv, list)
            assert isinstance(argv, list)
            self.assertTrue(
                argv[-1].startswith(
                    "/usr/bin/caffeinate -dimsu /usr/bin/python -c "
                )
            )
            self.assertNotIn("python3", argv[-1])
        rsync_record = next(record for record in records if record["tool"] == "rsync")
        rsync_argv = rsync_record["argv"]
        self.assertIsInstance(rsync_argv, list)
        assert isinstance(rsync_argv, list)
        self.assertIn(
            "--rsync-path=/usr/bin/caffeinate -dimsu /usr/bin/rsync",
            rsync_argv,
        )
        compile(archive._REMOTE_MANIFEST_SCRIPT, "<remote-manifest>", "exec")
        forbidden = ("f\"", "f'", "exist_ok=", "encoding=", "os.scandir", "st_mtime_ns")
        self.assertFalse(any(token in archive._REMOTE_MANIFEST_SCRIPT for token in forbidden))

    def test_ssh_archive_records_provenance_hashes_and_metadata(self) -> None:
        track = self.source / "iTunes Library.itl"
        track.chmod(0o640)
        os.utime(track, ns=(1_600_000_000_123_456_789, 1_600_000_000_123_456_789))
        (self.source / "Empty Playlist Cache").mkdir()

        result = self._run_ssh()

        published = self.archive_root / "icarus-iii-itunes-001"
        manifest = json.loads((published / "manifest.json").read_text(encoding="utf-8"))
        entry = next(item for item in manifest["files"] if item["relative_path"] == "iTunes Library.itl")
        self.assertEqual(result["file_count"], 3)
        self.assertEqual(manifest["source_kind"], "ssh")
        self.assertEqual(manifest["provenance"]["known_hosts_file"], str(self.known_hosts.absolute()))
        self.assertEqual(entry["mode"], 0o640)
        self.assertEqual(entry["mtime_seconds"], 1_600_000_000)
        self.assertNotIn("mtime", entry)
        self.assertEqual(entry["source_sha256"], entry["archive_sha256"])
        self.assertEqual(
            manifest["metadata_verified"],
            [
                "hierarchy",
                "type",
                "size",
                "data_fork_sha256",
                "posix_mode",
                "mtime_seconds",
            ],
        )
        self.assertEqual(
            manifest["metadata_best_effort_unverified"],
            ["extended_attributes", "resource_forks", "access_control_lists"],
        )
        stable_content = {
            "directories": manifest["directories"],
            "files": manifest["files"],
        }
        expected_content_hash = hashlib.sha256(
            json.dumps(stable_content, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self.assertEqual(manifest["content_manifest_sha256"], expected_content_hash)
        self.assertEqual(result["content_manifest_sha256"], expected_content_hash)
        self.assertEqual((published / "payload" / "iTunes Library.itl").stat().st_mode & 0o7777, 0o640)
        rsync = next(record for record in self._transport_records() if record["tool"] == "rsync")
        self.assertIn("-aE", rsync["argv"])

    def test_symlink_swap_cannot_publish_outside_file(self) -> None:
        outside = self.base / "outside-secret"
        outside.write_bytes(b"must-not-be-archived")
        environment = {
            "FAKE_TRANSPORT_LOG": str(self.transport_log),
            "FAKE_RSYNC_BEHAVIOR": "symlink-race",
            "FAKE_OUTSIDE_FILE": str(outside),
        }
        with mock.patch.dict(os.environ, environment):
            with self.assertRaisesRegex(UnsafeSourceError, "symlink"):
                archive.archive_ssh_source(self.archive_root, **self._ssh_kwargs())

        self.assertFalse((self.archive_root / "icarus-iii-itunes-001").exists())
        self.assertEqual(list(self.archive_root.glob(".*.partial*")), [])

    def test_source_rewalk_change_never_publishes(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "FAKE_TRANSPORT_LOG": str(self.transport_log),
                "FAKE_RSYNC_BEHAVIOR": "source-change",
            },
        ):
            with self.assertRaisesRegex(IOError, "source changed"):
                archive.archive_ssh_source(self.archive_root, **self._ssh_kwargs())

        self.assertFalse((self.archive_root / "icarus-iii-itunes-001").exists())

    def test_atomic_publish_never_clobbers_destination_created_during_copy(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "FAKE_TRANSPORT_LOG": str(self.transport_log),
                "FAKE_RSYNC_BEHAVIOR": "destination-race",
                "FAKE_FINAL_NAME": "icarus-iii-itunes-001",
            },
        ):
            with self.assertRaises(ArchiveExistsError):
                archive.archive_ssh_source(self.archive_root, **self._ssh_kwargs())

        destination = self.archive_root / "icarus-iii-itunes-001"
        self.assertEqual((destination / "intruder").read_bytes(), b"keep")
        self.assertFalse((destination / "manifest.json").exists())
        self.assertEqual(list(self.archive_root.glob(".*.partial*")), [])

    def test_published_name_replacement_during_final_verify_fails_and_is_preserved(self) -> None:
        published = self.archive_root / "icarus-iii-itunes-001"
        displaced = self.archive_root / "displaced-original"
        original_scan = archive._scan_local_tree
        scans = 0

        def replace_after_final_scan(descriptor: int, limits: archive.ArchiveLimits):
            nonlocal scans
            result = original_scan(descriptor, limits)
            scans += 1
            if scans == 2:
                published.rename(displaced)
                published.mkdir()
                (published / "intruder").write_bytes(b"keep")
            return result

        with mock.patch.object(archive, "_scan_local_tree", side_effect=replace_after_final_scan):
            with self.assertRaisesRegex(UnsafeSourceError, "published archive"):
                self._run_ssh()

        self.assertEqual((published / "intruder").read_bytes(), b"keep")
        self.assertTrue((displaced / "manifest.json").is_file())

    def test_root_lock_rejects_concurrent_cooperative_writer(self) -> None:
        locked_fd = os.open(self.archive_root, os.O_RDONLY | os.O_DIRECTORY)
        fcntl.flock(locked_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with self.assertRaisesRegex(UnsafeSourceError, "locked"):
                self._run_ssh()
        finally:
            os.close(locked_fd)

        self.assertFalse(self.transport_log.exists())

    def test_no_replace_primitive_refuses_existing_target(self) -> None:
        source = self.archive_root / "source"
        target = self.archive_root / "target"
        source.mkdir()
        target.mkdir()
        (target / "keep").write_bytes(b"keep")
        root_fd = os.open(self.archive_root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with self.assertRaises(FileExistsError):
                archive._rename_no_replace(root_fd, "source", root_fd, "target")
        finally:
            os.close(root_fd)
        self.assertTrue(source.is_dir())
        self.assertEqual((target / "keep").read_bytes(), b"keep")

    def test_manifest_protocol_rejects_duplicates_and_trailing_frames(self) -> None:
        entry = {
            "type": "file",
            "relative_path": "track.mp3",
            "size": 5,
            "sha256": "0" * 64,
            "mode": 0o644,
            "mtime_seconds": 1,
        }
        duplicate = self._manifest_bytes([entry, entry], file_count=2, total_bytes=10)
        trailing = self._manifest_bytes([entry], file_count=1, total_bytes=5) + b"{}\n"

        with self.assertRaisesRegex(IOError, "duplicate"):
            archive._parse_remote_manifest(duplicate, archive.ArchiveLimits())
        with self.assertRaisesRegex(IOError, "trailing"):
            archive._parse_remote_manifest(trailing, archive.ArchiveLimits())

    def _manifest_bytes(
        self, entries: list[dict[str, object]], *, file_count: int, total_bytes: int
    ) -> bytes:
        frames: list[dict[str, object]] = [
            {"type": "header", "schema_version": 1}
        ]
        frames.extend(entries)
        frames.append(
            {
                "type": "complete",
                "entry_count": len(entries),
                "file_count": file_count,
                "total_bytes": total_bytes,
            }
        )
        return b"".join(
            json.dumps(frame, sort_keys=True, separators=(",", ":")).encode("utf-8")
            + b"\n"
            for frame in frames
        )

    def test_manifest_protocol_enforces_count_size_and_numeric_types(self) -> None:
        too_large = {
            "type": "file",
            "relative_path": "track.mp3",
            "size": 6,
            "sha256": "0" * 64,
            "mode": 0o644,
            "mtime_seconds": 1,
        }
        boolean_size = dict(too_large, size=True)

        with self.assertRaisesRegex(IOError, "file size limit"):
            archive._parse_remote_manifest(
                self._manifest_bytes([too_large], file_count=1, total_bytes=6),
                archive.ArchiveLimits(max_file_bytes=5),
            )
        with self.assertRaisesRegex(IOError, "integer"):
            archive._parse_remote_manifest(
                self._manifest_bytes([boolean_size], file_count=1, total_bytes=1),
                archive.ArchiveLimits(),
            )
        with self.assertRaisesRegex(ValueError, "max_entries"):
            archive.ArchiveLimits(max_entries=True)
        with self.assertRaisesRegex(ValueError, "hard maximum"):
            archive.ArchiveLimits(max_entries=10**12)
        with self.assertRaisesRegex(ValueError, "timeout"):
            archive.archive_ssh_source(
                self.archive_root,
                **self._ssh_kwargs(operation_timeout_seconds=0),
            )

    def test_transport_timeout_kills_process_and_preserves_staging(self) -> None:
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
                    self.archive_root,
                    **self._ssh_kwargs(operation_timeout_seconds=0.2),
                )
        self.assertLess(time.monotonic() - started, 5)
        # A timeout is a transport interruption, so the staging tree survives
        # for the next attempt to resume instead of being deleted.
        self.assertEqual(len(list(self.archive_root.glob(".*.partial*"))), 1)

    def test_timeout_also_bounds_inherited_pipe_drain(self) -> None:
        started = time.monotonic()
        with mock.patch.dict(
            os.environ,
            {
                "FAKE_TRANSPORT_LOG": str(self.transport_log),
                "FAKE_SSH_BEHAVIOR": "pipe-hold",
            },
        ):
            with self.assertRaises(ArchiveTimeoutError):
                archive.archive_ssh_source(
                    self.archive_root,
                    **self._ssh_kwargs(operation_timeout_seconds=1),
                )
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(len(list(self.archive_root.glob(".*.partial*"))), 1)

    def test_stderr_is_bounded_and_reports_truncation(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "FAKE_TRANSPORT_LOG": str(self.transport_log),
                "FAKE_SSH_BEHAVIOR": "stderr",
            },
        ):
            with self.assertRaisesRegex(IOError, "truncated") as raised:
                archive.archive_ssh_source(
                    self.archive_root,
                    **self._ssh_kwargs(max_stderr_bytes=1024),
                )
        self.assertLess(len(str(raised.exception)), 3000)

    def test_archive_root_symlink_is_rejected_before_transport(self) -> None:
        real_root = self.base / "real-archives"
        real_root.mkdir()
        self.archive_root.rmdir()
        self.archive_root.symlink_to(real_root, target_is_directory=True)

        with self.assertRaisesRegex(UnsafeSourceError, "symlink"):
            self._run_ssh()
        self.assertFalse(self.transport_log.exists())

    def test_missing_archive_root_is_rejected_without_creating_ancestors(self) -> None:
        missing_root = self.base / "missing-parent" / "archives"

        with self.assertRaisesRegex(UnsafeSourceError, "already exist"):
            archive.archive_ssh_source(missing_root, **self._ssh_kwargs())

        self.assertFalse(missing_root.parent.exists())
        self.assertFalse(self.transport_log.exists())

    def test_ssh_archive_rejects_wrong_filesystem_before_transport(self) -> None:
        with self.assertRaisesRegex(UnsafeSourceError, "filesystem type"):
            self._run_ssh(expected_filesystem_type="smbfs")

        self.assertFalse(self.transport_log.exists())

    def test_actual_non_apfs_archive_root_is_rejected_before_transport(self) -> None:
        wrong = archive._FilesystemIdentity("smbfs", (1, 2), "//nas/share", "/Volumes/share")
        with mock.patch.object(archive, "_filesystem_identity", return_value=wrong):
            with self.assertRaisesRegex(UnsafeSourceError, "expected apfs, found smbfs"):
                self._run_ssh()

        self.assertFalse(self.transport_log.exists())

    def test_mount_identity_change_before_transfer_fails_closed(self) -> None:
        first = archive._FilesystemIdentity("apfs", (1, 2), "/dev/disk-test", "/test")
        changed = archive._FilesystemIdentity("apfs", (3, 4), "/dev/disk-other", "/test")
        with mock.patch.object(
            archive, "_filesystem_identity", side_effect=[first, changed]
        ):
            with self.assertRaisesRegex(UnsafeSourceError, "mount identity changed"):
                self._run_ssh()

        self.assertFalse(self.transport_log.exists())

    def test_mount_identity_change_before_publication_fails_closed(self) -> None:
        first = archive._FilesystemIdentity("apfs", (1, 2), "/dev/disk-test", "/test")
        changed = archive._FilesystemIdentity("apfs", (3, 4), "/dev/disk-other", "/test")
        with mock.patch.object(
            archive, "_filesystem_identity", side_effect=[first, first, first, changed]
        ):
            with self.assertRaisesRegex(UnsafeSourceError, "mount identity changed"):
                self._run_ssh()

        self.assertFalse((self.archive_root / "icarus-iii-itunes-001").exists())

    def test_mount_identity_change_after_final_verification_fails_closed(self) -> None:
        first = archive._FilesystemIdentity("apfs", (1, 2), "/dev/disk-test", "/test")
        changed = archive._FilesystemIdentity("apfs", (3, 4), "/dev/disk-other", "/test")
        with mock.patch.object(
            archive,
            "_filesystem_identity",
            side_effect=[first, first, first, first, changed],
        ):
            with self.assertRaisesRegex(UnsafeSourceError, "mount identity changed"):
                self._run_ssh()

        self.assertFalse((self.archive_root / "icarus-iii-itunes-001").exists())

    def test_staging_replacement_is_detected_and_never_published(self) -> None:
        attacker = self.base / "attacker"
        attacker.mkdir()
        with mock.patch.dict(
            os.environ,
            {
                "FAKE_TRANSPORT_LOG": str(self.transport_log),
                "FAKE_RSYNC_BEHAVIOR": "tamper-staging",
                "FAKE_ATTACKER_DIR": str(attacker),
            },
        ):
            with self.assertRaisesRegex(UnsafeSourceError, "staging"):
                archive.archive_ssh_source(self.archive_root, **self._ssh_kwargs())
        self.assertFalse((self.archive_root / "icarus-iii-itunes-001").exists())
        self.assertEqual(list(self.archive_root.glob(".*.partial*")), [])

    def test_destination_rewalk_detects_file_name_swap_after_open(self) -> None:
        payload = self.base / "payload"
        payload.mkdir()
        track = payload / "track.mp3"
        track.write_bytes(b"inside")
        outside = self.base / "outside"
        outside.write_bytes(b"outside")
        root_fd = os.open(payload, os.O_RDONLY | os.O_DIRECTORY)
        original_fstat = os.fstat
        swapped = False

        def swap_after_open(descriptor: int):
            nonlocal swapped
            value = original_fstat(descriptor)
            if descriptor != root_fd and not swapped and value.st_size == len(b"inside"):
                swapped = True
                track.unlink()
                track.symlink_to(outside)
            return value

        try:
            with mock.patch.object(archive.os, "fstat", side_effect=swap_after_open):
                with self.assertRaisesRegex(UnsafeSourceError, "changed"):
                    archive._scan_local_tree(root_fd, archive.ArchiveLimits())
        finally:
            os.close(root_fd)

    def test_filesystem_publication_probe_failure_stops_before_transport(self) -> None:
        with mock.patch.object(
            archive, "_verify_archive_filesystem", side_effect=OSError("SMB no-replace unsupported")
        ):
            with self.assertRaisesRegex(OSError, "no-replace unsupported"):
                self._run_ssh()
        self.assertFalse(self.transport_log.exists())

    def test_cleanup_failure_is_not_hidden(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "FAKE_TRANSPORT_LOG": str(self.transport_log),
                "FAKE_SSH_BEHAVIOR": "stderr",
            },
        ), mock.patch.object(
            archive, "_cleanup_staging", side_effect=OSError("cleanup denied")
        ):
            with self.assertRaisesRegex(ArchiveCleanupError, "cleanup denied") as raised:
                archive.archive_ssh_source(self.archive_root, **self._ssh_kwargs())
        self.assertIsNotNone(raised.exception.__cause__)

    def test_missing_or_symlinked_trust_files_are_rejected(self) -> None:
        with self.assertRaisesRegex(UnsafeSourceError, "known_hosts"):
            archive.archive_ssh_source(
                self.archive_root,
                **self._ssh_kwargs(known_hosts_file=self.base / "missing"),
            )
        linked = self.base / "known-hosts-link"
        linked.symlink_to(self.known_hosts)
        with self.assertRaisesRegex(UnsafeSourceError, "symlink"):
            archive.archive_ssh_source(
                self.archive_root,
                **self._ssh_kwargs(known_hosts_file=linked),
            )

    def test_ssh_cli_is_dry_run_without_contacting_source(self) -> None:
        script = Path(__file__).parents[1] / "scripts" / "music_archive.py"
        result = subprocess.run(
            [
                sys.executable,
                str(script),
                "--archive-root",
                str(self.archive_root),
                "--source-id",
                "icarus-iii-itunes-001",
                "--ssh-host",
                "archive-host.local",
                "--ssh-user",
                "archive",
                "--ssh-source-path",
                "/Users/archive/Music/iTunes",
                "--ssh-identity",
                str(self.identity),
                "--ssh-known-hosts",
                str(self.known_hosts),
                "--expected-filesystem-type",
                "apfs",
            ],
            cwd=Path(__file__).parents[1],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DRY RUN", result.stdout)
        self.assertIn("archive-host.local", result.stdout)
        self.assertIn(str(self.known_hosts), result.stdout)
        self.assertIn("expected filesystem: apfs", result.stdout)

    def test_ssh_cli_requires_explicit_filesystem_expectation(self) -> None:
        script = Path(__file__).parents[1] / "scripts" / "music_archive.py"
        result = subprocess.run(
            [
                sys.executable,
                str(script),
                "--archive-root",
                str(self.archive_root),
                "--source-id",
                "icarus-iii-itunes-001",
                "--ssh-host",
                "archive-host.local",
                "--ssh-user",
                "archive",
                "--ssh-source-path",
                "/Users/archive/Music/iTunes",
                "--ssh-identity",
                str(self.identity),
                "--ssh-known-hosts",
                str(self.known_hosts),
            ],
            cwd=Path(__file__).parents[1],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("requires --expected-filesystem-type apfs", result.stderr)

    def test_ssh_cli_rejects_smb_filesystem_expectation(self) -> None:
        script = Path(__file__).parents[1] / "scripts" / "music_archive.py"
        result = subprocess.run(
            [
                sys.executable,
                str(script),
                "--archive-root",
                str(self.archive_root),
                "--source-id",
                "icarus-iii-itunes-001",
                "--ssh-host",
                "archive-host.local",
                "--ssh-user",
                "archive",
                "--ssh-source-path",
                "/Users/archive/Music/iTunes",
                "--ssh-identity",
                str(self.identity),
                "--ssh-known-hosts",
                str(self.known_hosts),
                "--expected-filesystem-type",
                "smbfs",
            ],
            cwd=Path(__file__).parents[1],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid choice", result.stderr)

    def test_archive_copies_complete_tree_and_verifies_destination_hashes(self) -> None:
        result = archive_source(
            self.source,
            self.archive_root,
            source_id="icarus-ii-itunes-001",
        )

        published = self.archive_root / "icarus-ii-itunes-001"
        manifest = json.loads((published / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(result["file_count"], 3)
        self.assertEqual((published / "payload" / "iTunes Library.itl").read_bytes(), b"legacy-db")
        self.assertTrue(all(item["source_sha256"] == item["archive_sha256"] for item in manifest["files"]))

    def test_archive_refuses_to_overwrite_an_existing_source_id(self) -> None:
        archive_source(self.source, self.archive_root, source_id="legacy")
        with self.assertRaises(ArchiveExistsError):
            archive_source(self.source, self.archive_root, source_id="legacy")

    def test_archive_refuses_symlinks_in_source_tree(self) -> None:
        (self.source / "outside-link").symlink_to(self.base / "outside")
        with self.assertRaises(UnsafeSourceError):
            archive_source(self.source, self.archive_root, source_id="legacy")

    def test_archive_refuses_destination_inside_source(self) -> None:
        with self.assertRaises(UnsafeSourceError):
            archive_source(self.source, self.source / "archives", source_id="legacy")

    def test_cli_is_dry_run_unless_execute_is_explicit(self) -> None:
        script = Path(__file__).parents[1] / "scripts" / "music_archive.py"
        result = subprocess.run(
            [
                sys.executable,
                str(script),
                str(self.source),
                str(self.archive_root),
                "--source-id",
                "legacy",
            ],
            cwd=Path(__file__).parents[1],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DRY RUN", result.stdout)
        self.assertFalse((self.archive_root / "legacy").exists())


if __name__ == "__main__":
    unittest.main()
