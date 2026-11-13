"""Minimal NAS file server — serves /Volumes/Dump/* via HTTPS through tailscale serve.

Run with:
    python3 -m studios_nas_server --port 8766 --serve-dir /Volumes/Dump
    # Then: tailscale serve --bg 8766
    # Access via: $STUDIOS_PUBLIC_ORIGIN/nas/...
"""

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import argparse
import os
from pathlib import Path

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o for o in [os.environ.get("STUDIOS_PUBLIC_ORIGIN", "")] if o],
    allow_methods=["GET"],
    allow_headers=["X-Task-ID"],
)

SERVE_DIR = Path("/Volumes/Dump")
PORT = 8766


@app.get("/nas/{filepath:path}")
def serve_nas_file(filepath: str):
    """Serve a file from the NAS mount. E.g. /nas/studios_test_input.mp4"""
    safe_path = (SERVE_DIR / filepath).resolve()
    if not str(safe_path).startswith(str(SERVE_DIR.resolve())):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    if not safe_path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(safe_path)


@app.get("/api/nas/list")
def list_nas_files():
    """List top-level files in the NAS mount."""
    try:
        items = [
            {"name": p.name, "size": p.stat().st_size, "is_dir": p.is_dir()}
            for p in sorted(SERVE_DIR.iterdir())
        ]
        return {"dir": str(SERVE_DIR), "items": items}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "studios-nas-server", "serve_dir": str(SERVE_DIR)}


if __name__ == "__main__":
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--serve-dir", default=str(SERVE_DIR))
    args = parser.parse_args()
    SERVE_DIR = Path(args.serve_dir)
    print(f"Serving {SERVE_DIR} on port {args.port}")
    uvicorn.run(app, host="127.0.0.1", port=args.port)
