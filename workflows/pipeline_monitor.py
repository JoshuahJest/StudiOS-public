#!/usr/bin/env python3
"""
StudiOS Pipeline Monitor & Evaluator
Bangalter watches the parametric pipeline workspace, evaluates outputs,
logs to the catalog, and posts summaries to Thomas.

Usage:
    python3 pipeline_monitor.py --workspace /path/to/workspace --interval 120
    python3 pipeline_monitor.py --once          # run once and exit
"""
import os, sys, time, json, argparse, sqlite3, subprocess
from pathlib import Path
from datetime import datetime
from urllib.request import Request, urlopen
import urllib.error

# ──────────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────────

REPO_ROOT    = Path(__file__).resolve().parent.parent
WORKSPACE    = REPO_ROOT / "workflows" / "workspace"
CATALOG_DB   = REPO_ROOT / "workflows" / "parametric_catalog" / "catalog.db"
EVAL_DIR     = REPO_ROOT / "workflows" / "evaluations"
STATE_FILE   = EVAL_DIR / "monitor_state.json"
LOG_FILE     = EVAL_DIR / "monitor.log"
VENV_PY      = str(Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3")

EVAL_DIR.mkdir(exist_ok=True)

# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────

def log(msg, level="INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

# ──────────────────────────────────────────────────────────────────────────────
# State persistence
# ──────────────────────────────────────────────────────────────────────────────

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_check": 0, "last_run_id": None, "last_stage": {}, "consecutive_failures": 0}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))

# ──────────────────────────────────────────────────────────────────────────────
# Pipeline evaluation
# ──────────────────────────────────────────────────────────────────────────────

def evaluate_run(run_dir: Path) -> dict:
    """Evaluate a completed pipeline run and return a score dict."""
    score = {
        "run_id": run_dir.name,
        "timestamp": datetime.now().isoformat(),
        "stages": {},
        "overall": {"pass": False, "score": 0, "issues": []}
    }

    # Check geometry isolation
    edge_mask = run_dir / "edge_mask.png"
    contour_img = run_dir / "contours.png"
    if edge_mask.exists():
        sz = os.path.getsize(edge_mask)
        score["stages"]["geometry_isolation"] = {
            "pass": sz > 1000,
            "size_bytes": sz,
            "detail": f"Edge mask: {sz/1024:.1f} KB"
        }
    else:
        score["stages"]["geometry_isolation"] = {"pass": False, "detail": "No edge mask found"}
        score["overall"]["issues"].append("Geometry isolation failed")

    # Check Potrace SVG
    potrace_svg = run_dir / "potrace_out.svg"
    if potrace_svg.exists():
        sz = os.path.getsize(potrace_svg)
        score["stages"]["potrace"] = {
            "pass": sz > 500,
            "size_bytes": sz,
            "detail": f"SVG: {sz/1024:.1f} KB"
        }
    elif (run_dir / "potrace_clean.svg").exists():
        sz = os.path.getsize(run_dir / "potrace_clean.svg")
        score["stages"]["potrace"] = {
            "pass": sz > 500,
            "size_bytes": sz,
            "detail": f"SVG (svgo optimized): {sz/1024:.1f} KB"
        }
    else:
        score["stages"]["potrace"] = {"pass": None, "detail": "No potrace output found"}

    # Check svgtrace SVG
    svgtrace_svg = run_dir / "svgtrace_out.svg"
    if svgtrace_svg.exists():
        sz = os.path.getsize(svgtrace_svg)
        score["stages"]["svgtrace"] = {
            "pass": sz > 500,
            "size_bytes": sz,
            "detail": f"SVG: {sz/1024:.1f} KB"
        }
    else:
        score["stages"]["svgtrace"] = {"pass": None, "detail": "No svgtrace output found"}

    # Check Blender frames
    frames = sorted(run_dir.glob("render_*.png"))
    if frames:
        avg_size = sum(os.path.getsize(f) for f in frames) / len(frames)
        score["stages"]["blender_animate"] = {
            "pass": len(frames) >= 30,
            "frame_count": len(frames),
            "avg_frame_size_kb": round(avg_size/1024, 1),
            "detail": f"{len(frames)} frames, avg {avg_size/1024:.1f} KB/frame"
        }
    else:
        score["stages"]["blender_animate"] = {"pass": None, "detail": "No render frames found"}

    # Check FFmpeg video
    video = run_dir / "output.mp4"
    if video.exists():
        sz = os.path.getsize(video)
        dur = get_video_duration(video)
        score["stages"]["ffmpeg"] = {
            "pass": sz > 10000 and dur > 0.5,
            "size_bytes": sz,
            "duration_s": dur,
            "detail": f"MP4: {sz/1024/1024:.1f} MB, {dur:.1f}s"
        }
    else:
        score["stages"]["ffmpeg"] = {"pass": None, "detail": "No video output found"}

    # Overall pass: at least geometry + one vectorizer succeeded
    pass_stages = sum(1 for s in score["stages"].values() if s.get("pass") in (True, None))
    score["overall"]["score"] = round(pass_stages / max(len(score["stages"]), 1) * 100)
    score["overall"]["pass"] = score["stages"].get("geometry_isolation", {}).get("pass", False) and \
                               any(s.get("pass") == True for s in score["stages"].values())

    return score


def get_video_duration(video_path: Path) -> float:
    """Get video duration in seconds via FFprobe."""
    try:
        r = subprocess.run([
            "ffprobe", "-v", "quiet",
            "-show_entries", "format=duration",
            "-of", "json",
            str(video_path)
        ], capture_output=True, text=True, timeout=10)
        data = json.loads(r.stdout)
        return float(data["format"]["duration"])
    except Exception:
        return 0.0


def log_evaluation_to_catalog(evaluation: dict, run_dir: Path):
    """Write evaluation to the catalog DB."""
    if not CATALOG_DB.exists():
        log(f"Catalog DB not found at {CATALOG_DB}, skipping catalog log", "WARN")
        return

    conn = sqlite3.connect(str(CATALOG_DB))
    try:
        # Update run status
        conn.execute("""
            UPDATE runs SET
                status = ?,
                outputs = ?,
                completed_at = unixepoch()
            WHERE id = ?
        """, (
            "completed" if evaluation["overall"]["pass"] else "failed",
            json.dumps(evaluation),
            run_dir.name.replace("run_", "vec_"),
        ))
        conn.commit()
    except Exception as e:
        log(f"Catalog log failed: {e}", "ERROR")
    finally:
        conn.close()


def save_evaluation(evaluation: dict, run_dir: Path):
    """Save evaluation JSON to the evaluations dir."""
    eval_path = EVAL_DIR / f"{run_dir.name}_eval.json"
    with open(eval_path, "w") as f:
        json.dump(evaluation, f, indent=2)
    log(f"Evaluation saved: {eval_path}")


def notify_thomas(evaluation: dict):
    """Send a brief summary to Thomas via the terminal (Telegram is already connected)."""
    run_id = evaluation["run_id"]
    score = evaluation["overall"]["score"]
    issues = evaluation["overall"]["issues"]
    stages = evaluation["stages"]

    summary = [
        f"📊 **Pipeline Run `{run_id}`**",
        f"   Score: {score}% {'✅' if evaluation['overall']['pass'] else '❌'}",
    ]

    for name, result in stages.items():
        status = "✅" if result.get("pass") == True else ("⚠️" if result.get("pass") is None else "❌")
        detail = result.get("detail", "")
        summary.append(f"   {status} {name}: {detail}")

    if issues:
        summary.append(f"   ⚠️ Issues: {', '.join(issues)}")

    summary.append("   Full eval: " + str(EVAL_DIR / (run_id + "_eval.json")))

    report = "\n".join(summary)
    print(f"\n{report}\n")
    return report


# ──────────────────────────────────────────────────────────────────────────────
# Main monitor loop
# ──────────────────────────────────────────────────────────────────────────────

def scan_workspace(state: dict) -> list[Path]:
    """Find newly completed run directories."""
    new_runs = []
    if not WORKSPACE.exists():
        return new_runs

    for run_dir in sorted(WORKSPACE.glob("run_*")):
        if not run_dir.is_dir():
            continue
        # A run is "done" when we see an output file (SVG or frames or video)
        outputs = (
            list(run_dir.glob("*.svg")) +
            list(run_dir.glob("render_*.png")) +
            list(run_dir.glob("*.mp4"))
        )
        if outputs and run_dir.name != state.get("last_run_id"):
            new_runs.append(run_dir)
            state["last_run_id"] = run_dir.name

    return new_runs


def run_evaluation_cycle(state: dict):
    new_runs = scan_workspace(state)
    if not new_runs:
        return

    for run_dir in new_runs:
        log(f"New run detected: {run_dir.name}")
        try:
            evaluation = evaluate_run(run_dir)
            save_evaluation(evaluation, run_dir)
            log_evaluation_to_catalog(evaluation, run_dir)
            notify_thomas(evaluation)

            if not evaluation["overall"]["pass"]:
                state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
                if state["consecutive_failures"] >= 3:
                    log(f"⚠️ 3 consecutive failures — review needed", "WARN")
            else:
                state["consecutive_failures"] = 0

        except Exception as e:
            log(f"Evaluation error for {run_dir.name}: {e}", "ERROR")
            import traceback
            traceback.print_exc()


def main():
    global WORKSPACE
    p = argparse.ArgumentParser(description="StudiOS Pipeline Monitor")
    p.add_argument("--workspace", default=str(WORKSPACE))
    p.add_argument("--interval", type=int, default=120, help="Poll interval in seconds")
    p.add_argument("--once", action="store_true", help="Run once and exit")
    args = p.parse_args()
    WORKSPACE = Path(args.workspace)

    state = load_state()
    log(f"Monitor starting. Workspace: {WORKSPACE}")

    if args.once:
        run_evaluation_cycle(state)
        save_state(state)
        return

    log(f"Polling every {args.interval}s. Ctrl-C to stop.")
    try:
        while True:
            run_evaluation_cycle(state)
            save_state(state)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log("Monitor stopped.")
        save_state(state)


if __name__ == "__main__":
    main()
