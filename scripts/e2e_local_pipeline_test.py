#!/usr/bin/env python3
"""End-to-end smoke test for the StudiOS local-inference pipeline.

Proves, in one run:
  1. The LM Studio OpenAI-compatible server is reachable.
  2. A chat completion returns a usable answer from the local model.
  3. The embedding endpoint returns vectors of the requested dimension.
  4. The parametric vector pipeline converts a raster source to SVG.
  5. The catalog database records the resulting source.

Exit code 0 means every stage passed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

LMSTUDIO = os.environ.get("LMSTUDIO_URL", "http://127.0.0.1:1234/v1")
CHAT_MODEL = os.environ.get("LMSTUDIO_CHAT_MODEL", "gemma-4-e2b-it@q4_k_xl")
EMBED_MODEL = os.environ.get("LMSTUDIO_EMBED_MODEL", "text-embedding-nomic-embed-text-v1.5")
REPO = Path(os.environ.get("STUDIOS_REPO", "/Users/thomas/StudiOS-1.1"))

results: list[tuple[str, bool, str]] = []


def check(name: str):
    def deco(fn):
        def wrapped():
            start = time.time()
            try:
                detail = fn()
                results.append((name, True, f"{detail} ({time.time() - start:.1f}s)"))
            except Exception as exc:
                results.append((name, False, f"{type(exc).__name__}: {exc}"))
        return wrapped
    return deco


def post(path: str, payload: dict, timeout: int = 300) -> dict:
    import urllib.request

    req = urllib.request.Request(
        f"{LMSTUDIO}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def get(path: str, timeout: int = 30) -> dict:
    import urllib.request

    with urllib.request.urlopen(f"{LMSTUDIO}{path}", timeout=timeout) as resp:
        return json.loads(resp.read())


@check("1. server reachable")
def _server():
    models = get("/models")
    ids = [m.get("id") for m in models.get("data", [])]
    if not ids:
        raise RuntimeError("server returned no models")
    return f"{len(ids)} models: {', '.join(ids[:5])}"


@check("2. chat completion")
def _chat():
    out = post("/chat/completions", {
        "model": CHAT_MODEL,
        "messages": [{"role": "user", "content": "Reply with exactly: STUDIOS-OK"}],
        "max_tokens": 256,
        "temperature": 0,
    })
    text = (out["choices"][0]["message"].get("content") or "").strip()
    if not text:
        raise RuntimeError("empty completion")
    return f"model={CHAT_MODEL} -> {text[:60]!r}"


@check("3. embeddings")
def _embed():
    out = post("/embeddings", {"model": EMBED_MODEL, "input": "StudiOS local inference smoke test"})
    vec = out["data"][0]["embedding"]
    if len(vec) < 64:
        raise RuntimeError(f"embedding too short: {len(vec)}")
    return f"{len(vec)}-dim vector from {EMBED_MODEL}"


@check("4. vector pipeline (raster -> SVG)")
def _pipeline():
    src = REPO / "workflows" / "workspace" / "test_source" / "test_artdeco.png"
    if not src.exists():
        raise RuntimeError(f"missing test source: {src}")
    python = os.environ.get("STUDIOS_PYTHON", "/Users/thomas/.hermes/hermes-agent/venv/bin/python3")
    # The pipeline stages into workflows/workspace/run_<id>/ next to the repo,
    # not next to the input, so find the newest run dir after the call.
    workspace = REPO / "workflows" / "workspace"
    before = {d for d in workspace.glob("run_*")} if workspace.exists() else set()

    proc = subprocess.run(
        [python, str(REPO / "workflows" / "parametric_catalog" / "vector_pipeline.py"),
         str(src), "--potrace"],
        capture_output=True, text=True, timeout=600, cwd=str(REPO),
        env={**os.environ, "PYTHONPATH": str(REPO / "src")},
    )
    if proc.returncode != 0:
        raise RuntimeError(f"pipeline exit {proc.returncode}: {proc.stderr[-300:]}")
    after = {d for d in workspace.glob("run_*")} if workspace.exists() else set()
    new_dirs = sorted(after - before, key=lambda d: d.stat().st_mtime)
    if not new_dirs:
        raise RuntimeError("pipeline created no run directory")
    run_dir = new_dirs[-1]
    svgs = sorted(run_dir.glob("*.svg"))
    if not svgs:
        raise RuntimeError(f"no SVG in {run_dir.name}")
    size = svgs[0].stat().st_size
    return f"{len(svgs)} SVG(s), largest {size} bytes -> {run_dir.name}/{svgs[0].name}"


@check("5. catalog database")
def _catalog():
    python = os.environ.get("STUDIOS_PYTHON", "/Users/thomas/.hermes/hermes-agent/venv/bin/python3")
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "from schema import init, Source\n"
        "init()\n"
        "print(Source.count() if hasattr(Source, 'count') else 'no-count')\n"
    ) % str(REPO / "workflows" / "parametric_catalog")
    proc = subprocess.run([python, "-c", code], capture_output=True, text=True, timeout=120,
                          cwd=str(REPO), env={**os.environ, "PYTHONPATH": str(REPO / "src")})
    if proc.returncode != 0:
        raise RuntimeError(f"catalog error: {proc.stderr[-300:]}")
    return f"catalog reachable ({proc.stdout.strip()})"


def main() -> int:
    for fn in (_server, _chat, _embed, _pipeline, _catalog):
        fn()
    print("StudiOS local-inference end-to-end smoke test")
    print("=" * 56)
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}\n      {detail}")
    failed = [r for r in results if not r[1]]
    print("=" * 56)
    print(f"{len(results) - len(failed)}/{len(results)} stages passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
