#!/usr/bin/env python3
"""Reconcile two Apple iTunes XML library exports and write a loss report.

Read-only. The script never opens, converts, or modifies an ``.itl``
library; it only reads XML export artifacts and writes a new report file.

Examples
--------
    PYTHONPATH=src python scripts/music_reconcile_apple.py \
        --before "/path/iTunes Music Library.xml.last" \
        --after  "/path/iTunes Music Library.xml" \
        --media-root "/path/payload/iTunes Media" \
        --out artifacts/music/reconcile/report.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from studios_music.apple_xml import AppleXmlError, parse_apple_library_xml  # noqa: E402
from studios_music.reconcile import reconcile_apple_snapshots  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reconcile two Apple iTunes XML exports (read-only)."
    )
    parser.add_argument("--before", required=True, help="older XML export path")
    parser.add_argument("--after", required=True, help="newer XML export path")
    parser.add_argument(
        "--media-root",
        default=None,
        help=(
            "local directory holding the archived media. When omitted, every "
            "file-backed location is reported as unresolved."
        ),
    )
    parser.add_argument("--out", required=True, help="report JSON output path")
    parser.add_argument(
        "--quiet", action="store_true", help="suppress the stdout summary"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        before = parse_apple_library_xml(args.before)
        after = parse_apple_library_xml(args.after)
    except AppleXmlError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    report = reconcile_apple_snapshots(
        before, after, media_root=args.media_root
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    if not args.quiet:
        loss = report.to_dict()["loss_report"]
        print(f"report: {out_path}")
        print(f"tracks: {len(before.tracks)} -> {len(after.tracks)}")
        print(f"playlists: {len(before.playlists)} -> {len(after.playlists)}")
        print(f"lost tracks: {len(loss['lost_track_ids'])}")
        print(f"added tracks: {len(loss['added_track_ids'])}")
        print(f"lost playlists: {len(loss['lost_playlists'])}")
        print(f"play count: {loss['play_count_before']} -> {loss['play_count_after']}")
        print(f"broken paths (after): {len(loss['broken_paths_after'])}")
        print(f"playlists preserved: {loss['playlists_preserved']}")
        print(f"has loss: {loss['has_loss']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
