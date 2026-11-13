#!/usr/bin/env python3
"""Archive a complete legacy music source; defaults to dry-run."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from studios_music.archive import (
    ArchiveLimits,
    ArchiveTimeoutError,
    ArchiveTransportError,
    UnsafeSourceError,
    archive_source,
    archive_ssh_source,
    validate_ssh_source,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", help="Local iTunes/Music source directory")
    parser.add_argument("archive_root", nargs="?", help="Local archive root (local mode)")
    parser.add_argument(
        "--archive-root",
        dest="archive_root_option",
        help="Local/NAS destination root (required for SSH mode)",
    )
    parser.add_argument("--source-id", required=True, help="Unique immutable source ID")
    parser.add_argument("--ssh-host", help="SSH source hostname")
    parser.add_argument("--ssh-user", help="SSH source username")
    parser.add_argument("--ssh-source-path", help="Absolute source directory on SSH host")
    parser.add_argument("--ssh-identity", help="Dedicated local SSH private-key path")
    parser.add_argument(
        "--ssh-known-hosts",
        help="Dedicated known_hosts file; no global or default host keys are used",
    )
    parser.add_argument(
        "--expected-filesystem-type",
        choices=("apfs",),
        help="Required explicit SSH archive destination type (first phase: apfs only)",
    )
    parser.add_argument("--connect-timeout-seconds", type=_positive_int, default=15)
    parser.add_argument("--io-timeout-seconds", type=_positive_int, default=300)
    parser.add_argument("--operation-timeout-seconds", type=_positive_int, default=172800)
    parser.add_argument("--max-entries", type=_positive_int, default=1_000_000)
    parser.add_argument("--max-file-bytes", type=_positive_int, default=1 << 40)
    parser.add_argument("--max-total-bytes", type=_positive_int, default=2 << 40)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Perform the verified copy; without this flag the command is a dry run",
    )
    return parser


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    ssh_values = (
        args.ssh_host,
        args.ssh_user,
        args.ssh_source_path,
        args.ssh_identity,
        args.ssh_known_hosts,
    )
    ssh_mode = any(value is not None for value in ssh_values)
    if ssh_mode:
        if not all(value is not None for value in ssh_values):
            parser.error(
                "SSH mode requires --ssh-host, --ssh-user, --ssh-source-path, "
                "--ssh-identity, and --ssh-known-hosts"
            )
        if args.source is not None or args.archive_root is not None:
            parser.error("SSH mode does not accept local source/archive positional paths")
        if args.archive_root_option is None:
            parser.error("SSH mode requires --archive-root")
        if args.expected_filesystem_type is None:
            parser.error("SSH mode requires --expected-filesystem-type apfs")
        try:
            identity, known_hosts = validate_ssh_source(
                source_host=args.ssh_host,
                source_user=args.ssh_user,
                source_path=args.ssh_source_path,
                identity_file=args.ssh_identity,
                known_hosts_file=args.ssh_known_hosts,
            )
        except UnsafeSourceError as error:
            parser.error(str(error))
        archive_root = Path(args.archive_root_option).expanduser().absolute()
        destination = archive_root / args.source_id
        if args.execute:
            try:
                result = archive_ssh_source(
                    archive_root,
                    source_id=args.source_id,
                    source_host=args.ssh_host,
                    source_user=args.ssh_user,
                    source_path=args.ssh_source_path,
                    identity_file=identity,
                    known_hosts_file=known_hosts,
                    expected_filesystem_type=args.expected_filesystem_type,
                    limits=ArchiveLimits(
                        max_entries=args.max_entries,
                        max_file_bytes=args.max_file_bytes,
                        max_total_bytes=args.max_total_bytes,
                    ),
                    connect_timeout_seconds=args.connect_timeout_seconds,
                    io_timeout_seconds=args.io_timeout_seconds,
                    operation_timeout_seconds=args.operation_timeout_seconds,
                )
            except (ArchiveTransportError, ArchiveTimeoutError) as error:
                # The staged partial copy is preserved; rerun the same command
                # to resume rather than restarting the transfer from zero.
                print(
                    f"TRANSPORT INTERRUPTED: {error}",
                    file=sys.stderr,
                )
                print(
                    "The staged partial archive was preserved. Rerun the same "
                    "command to resume from the existing partial copy.",
                    file=sys.stderr,
                )
                return 75
            print(
                f"archive={result['archive_path']} files={result['file_count']} "
                f"bytes={result['total_bytes']} verified={result['verified']} "
                f"content_manifest_sha256={result['content_manifest_sha256']} "
                f"manifest_sha256={result['manifest_sha256']}"
            )
            return 0
        print("DRY RUN — no SSH connection made; no files copied")
        print(f"source: ssh://{args.ssh_user}@{args.ssh_host}{args.ssh_source_path}")
        print(f"identity: {identity}")
        print(f"known hosts: {known_hosts}")
        print(f"destination: {destination}")
        print(f"expected filesystem: {args.expected_filesystem_type}")
        return 0

    if args.archive_root_option is not None:
        parser.error("--archive-root is only valid with SSH source options")
    if args.source is None or args.archive_root is None:
        parser.error("local mode requires source and archive_root positional paths")
    source = Path(args.source).absolute()
    destination = Path(args.archive_root).absolute() / args.source_id
    if not source.is_dir():
        print(f"ERROR: source is not a directory: {source}")
        return 2
    if args.execute:
        result = archive_source(source, args.archive_root, source_id=args.source_id)
        print(
            f"archive={result['archive_path']} files={result['file_count']} "
            f"bytes={result['total_bytes']} verified={result['verified']}"
        )
        return 0
    print("DRY RUN — no files copied")
    print(f"source: {source}")
    print(f"destination: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
