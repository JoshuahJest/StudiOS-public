#!/usr/bin/env python3
"""Create a bounded, read-only JSON inventory of exact music roots."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from studios_music.inventory import (
    InventoryBudget,
    inventory_roots_hard_timeout,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Inventory exact legacy iTunes/Music roots without unbounded traversal. "
            "Symlinks are never followed."
        )
    )
    parser.add_argument("roots", nargs="+", help="Exact directories to inventory")
    parser.add_argument("--max-depth", type=int, default=3)
    parser.add_argument("--max-entries", type=int, default=2_000)
    parser.add_argument("--max-seconds", type=float, default=30.0)
    parser.add_argument(
        "--hash-file-max-bytes",
        type=int,
        default=16 * 1024 * 1024,
        help="Hash candidate database/XML files no larger than this size",
    )
    parser.add_argument("--output", required=True, help="Destination JSON manifest")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    result = inventory_roots_hard_timeout(
        [Path(root) for root in args.roots],
        InventoryBudget(
            max_depth=args.max_depth,
            max_entries=args.max_entries,
            max_seconds=args.max_seconds,
        ),
        hash_file_max_bytes=args.hash_file_max_bytes,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        f"wrote {output}: {len(result['entries'])} entries, "
        f"{len(result['errors'])} errors, truncated={result['truncated']}"
    )
    if result["errors"]:
        return 2
    if result["truncated"]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
