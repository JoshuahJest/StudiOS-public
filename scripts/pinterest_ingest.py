#!/usr/bin/env python3
"""Pinterest board ingest for the StudiOS parametric catalog.

Downloads every pin from one or more Pinterest boards using the official
v5 API, writes the images to local storage, and records each board and pin
as a Source in the catalog database.

Authentication is via ``PINTEREST_ACCESS_TOKEN`` (env var or
``~/.hermes/.env``). No token means no work is attempted -- the script exits
with a clear message rather than failing mid-run.

Usage:
    PYTHONPATH=src python3 scripts/pinterest_ingest.py --board <board_id> [...]
    PYTHONPATH=src python3 scripts/pinterest_ingest.py --board <id> --limit 50
    PYTHONPATH=src python3 scripts/pinterest_ingest.py --list-boards
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Iterator

import requests

PINTEREST_API = "https://api.pinterest.com/v5"
DEFAULT_RAW_DIR = Path(os.environ.get("STUDIOS_PINTEREST_RAW", "~/Pictures/studios_pinterest/raw")).expanduser()
DEFAULT_REPO = Path(__file__).resolve().parent.parent

# Pinterest API v5 allows 1000 calls/hour per token.
REQUEST_INTERVAL_S = 0.4


class PinterestError(RuntimeError):
    pass


def load_token() -> str | None:
    """Read the access token from the environment, then from ~/.hermes/.env."""
    token = os.environ.get("PINTEREST_ACCESS_TOKEN")
    if token:
        return token.strip()
    env_file = Path(os.path.expanduser("~/.hermes/.env"))
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("PINTEREST_ACCESS_TOKEN="):
                return line.split("=", 1)[1].strip().strip("'\"")
    return None


def headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def api_get(path: str, token: str, params: dict | None = None) -> dict:
    url = path if path.startswith("http") else f"{PINTEREST_API}{path}"
    resp = requests.get(url, headers=headers(token), params=params, timeout=30)
    if resp.status_code == 401:
        raise PinterestError("Pinterest rejected the access token (401). Re-authorise and retry.")
    if resp.status_code == 429:
        raise PinterestError("Pinterest rate limit hit (429). Retry later.")
    if resp.status_code >= 400:
        raise PinterestError(f"Pinterest API error {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def list_boards(token: str) -> list[dict]:
    user = api_get("/me", token)
    return api_get(f"/users/{user['username']}/boards", token, {"page_size": 100}).get("items", [])


def iter_pins(board_id: str, token: str, limit: int | None = None) -> Iterator[dict]:
    """Yield pins for a board, following the API's cursor pagination."""
    url = f"{PINTEREST_API}/boards/{board_id}/pins"
    params = {"page_size": 100}
    seen = 0
    while url:
        payload = api_get(url, token, params)
        params = None  # the next_page_url already carries the cursor
        for pin in payload.get("items", []):
            yield pin
            seen += 1
            if limit and seen >= limit:
                return
        url = payload.get("next_page_url")
        time.sleep(REQUEST_INTERVAL_S)


def pin_image_url(pin: dict) -> str | None:
    media = pin.get("media") or {}
    images = media.get("images") or {}
    for key in ("original", "1200x", "736x", "564x"):
        if images.get(key, {}).get("url"):
            return images[key]["url"]
    return None


def download(url: str, dest: Path) -> bool:
    resp = requests.get(url, timeout=60)
    if resp.status_code != 200 or len(resp.content) < 1024:
        return False
    dest.write_bytes(resp.content)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest Pinterest boards into the StudiOS catalog.")
    parser.add_argument("--board", action="append", default=[], help="Board id (repeatable).")
    parser.add_argument("--list-boards", action="store_true", help="List the token owner's boards and exit.")
    parser.add_argument("--limit", type=int, default=None, help="Max pins per board.")
    parser.add_argument("--raw-dir", default=str(DEFAULT_RAW_DIR), help="Where to write images.")
    parser.add_argument("--no-catalog", action="store_true", help="Download only; skip catalog records.")
    args = parser.parse_args(argv)

    token = load_token()
    if not token:
        print(
            "PINTEREST_ACCESS_TOKEN is not set. Create a Pinterest app, obtain a token with "
            "board:read and pin:read scopes, then set it with:\n"
            "  hermes config set PINTEREST_ACCESS_TOKEN '<token>'",
            file=sys.stderr,
        )
        return 2

    if args.list_boards:
        try:
            boards = list_boards(token)
        except PinterestError as exc:
            print(f"Pinterest API error: {exc}", file=sys.stderr)
            return 1
        for b in boards:
            print(f"{b.get('id')}\t{b.get('name')}\t{b.get('pin_count', '?')} pins")
        return 0

    if not args.board:
        print("No board specified. Use --board <id> or --list-boards.", file=sys.stderr)
        return 2

    raw_dir = Path(args.raw_dir).expanduser()
    raw_dir.mkdir(parents=True, exist_ok=True)

    catalog = None
    if not args.no_catalog:
        sys.path.insert(0, str(DEFAULT_REPO / "workflows" / "parametric_catalog"))
        try:
            from schema import Source, init

            init()
            catalog = Source
        except Exception as exc:  # catalog is optional; downloads still matter
            print(f"Catalog unavailable ({exc}); continuing without catalog records.", file=sys.stderr)

    total_saved = 0
    for board_id in args.board:
        try:
            board = api_get(f"/boards/{board_id}", token)
        except PinterestError as exc:
            print(f"board {board_id}: {exc}", file=sys.stderr)
            continue

        board_dir = raw_dir / board_id
        board_dir.mkdir(parents=True, exist_ok=True)
        saved = 0
        for pin in iter_pins(board_id, token, args.limit):
            img_url = pin_image_url(pin)
            if not img_url:
                continue
            ext = Path(img_url.split("?")[0]).suffix or ".jpg"
            dest = board_dir / f"{pin.get('id')}{ext}"
            if dest.exists() and dest.stat().st_size > 1024:
                saved += 1
                continue
            try:
                if download(img_url, dest):
                    saved += 1
            except requests.RequestException:
                continue

        total_saved += saved
        print(f"board {board.get('name') or board_id}: {saved} pins downloaded -> {board_dir}")

        if catalog is not None:
            catalog(
                label=f"Pinterest: {board.get('name') or board_id}",
                kind="pinterest_board",
                uri=f"{PINTEREST_API}/boards/{board_id}",
                metadata={
                    "name": board.get("name"),
                    "board_id": board_id,
                    "pin_count": saved,
                    "local_dir": str(board_dir),
                },
            ).save()

    print(f"total pins downloaded: {total_saved}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
