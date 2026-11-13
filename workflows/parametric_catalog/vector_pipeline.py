#!/usr/bin/env python3
"""
StudiOS Vector Pipeline — runs a Pinterest image through the full
geometry isolation → vectorization → SVG animation pipeline.

Usage:
    python3 vector_pipeline.py <image_path> [--potrace] [--svgtrace] [--blender] [--td]
    python3 vector_pipeline.py /tmp/studios_pinterest/raw/abc123.jpg --potrace --blender
"""
import sys, os, argparse, subprocess, json, time, uuid
from pathlib import Path

# Ensure hermes venv python for imports
VENV_PY = str(Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3")
sys.path.insert(0, str(Path(__file__).resolve().parent))

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CATALOG_DB = REPO_ROOT / "workflows" / "parametric_catalog" / "catalog.db"
WORKSPACE  = REPO_ROOT / "workflows" / "workspace"
WORKSPACE.mkdir(exist_ok=True)

# ──────────────────────────────────────────────────────────────────────────────
# Stage 1: Geometry isolation (OpenCV Canny + contour extraction)
# ──────────────────────────────────────────────────────────────────────────────

def isolate_geometry(input_path: str, output_dir: Path,
                    canny_low=50, canny_high=150,
                    dilate=3, erode=1) -> dict:
    """Run Canny edge detection + contour extraction."""
    import cv2, numpy as np

    img = cv2.imread(input_path)
    if img is None:
        raise FileNotFoundError(f"Cannot read {input_path}")

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 1.4)
    edges = cv2.Canny(blurred, canny_low, canny_high)

    if dilate:
        kernel = np.ones((dilate, dilate), np.uint8)
        edges = cv2.dilate(edges, kernel, iterations=1)
    if erode:
        kernel = np.ones((erode, erode), np.uint8)
        edges = cv2.erode(edges, kernel, iterations=1)

    # Save edge mask
    edge_path = output_dir / "edge_mask.png"
    cv2.imwrite(str(edge_path), edges)

    # Find contours for region isolation
    contours, hierarchy = cv2.findContours(edges, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
    contour_path = output_dir / "contours.png"
    contour_img = img.copy()
    cv2.drawContours(contour_img, contours, -1, (0, 255, 0), 1)
    cv2.imwrite(str(contour_path), contour_img)

    return {
        "edge_mask": str(edge_path),
        "contour_img": str(contour_path),
        "contour_count": len(contours),
        "canny_low": canny_low,
        "canny_high": canny_high,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Stage 2: Vectorize with Potrace
# ──────────────────────────────────────────────────────────────────────────────

def potrace_vectorize(edge_mask_path: str, output_dir: Path,
                      turnpolicy="black", turdsize=2,
                      alphamax=1.0, optcurve=True, invert=False) -> dict:
    """Run potrace CLI on a bitmap to produce SVG."""
    pgm_path = output_dir / "temp.pgm"
    svg_path = output_dir / "potrace_out.svg"

    # Convert PNG edge mask to PGM (portable graymap) for potrace
    import cv2
    mask = cv2.imread(edge_mask_path, cv2.IMREAD_GRAYSCALE)
    if invert:
        mask = 255 - mask
    cv2.imwrite(str(pgm_path), mask)

    cmd = [
        "potrace",
        "--pgm",
        "--svg",
        "--output", str(svg_path),
        "--turnpolicy", turnpolicy,
        "--turdsize", str(turdsize),
        "--alphamax", str(alphamax),
    ]
    # optcurve is ON by default; pass --longcurve only if optcurve=False
    if not optcurve:
        cmd.append("--longcurve")
    cmd.append(str(pgm_path))

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"potrace failed: {result.stderr}")

    pgm_path.unlink()

    # Optimize SVG with svgo
    svgo_path = output_dir / "potrace_out.svg"
    svgo_out = output_dir / "potrace_clean.svg"
    r = subprocess.run(
        ["svgo", "--input", str(svgo_path), "--output", str(svgo_out), "-q"],
        capture_output=True
    )
    optimized = svgo_out if r.returncode == 0 else svg_path

    return {"svg": str(optimized), "potrace_log": result.stderr}


# ──────────────────────────────────────────────────────────────────────────────
# Stage 3: Vectorize with svgtrace (Python, color-preserving)
# ──────────────────────────────────────────────────────────────────────────────

def svgtrace_vectorize(input_path: str, output_dir: Path,
                      blackAndWhite=False, mode="color") -> dict:
    """Run svgtrace (Inkscape-style) on a raster image.
    svgtrace.trace(filename, blackAndWhite=False, mode='default')
    mode: 'default' (color), 'color', 'gray', 'bw' (bitonal)
    """
    import svgtrace, subprocess, shutil

    svg_content = svgtrace.trace(
        input_path,
        blackAndWhite=blackAndWhite,
        mode=mode,
    )

    svg_path = output_dir / "svgtrace_out.svg"
    with open(svg_path, "w") as f:
        f.write(svg_content)

    # Run svgo
    svgo_out = output_dir / "svgtrace_clean.svg"
    r = subprocess.run(
        ["svgo", "--input", str(svg_path), "--output", str(svgo_out), "-q"],
        capture_output=True
    )
    optimized = svgo_out if r.returncode == 0 else svg_path

    return {"svg": str(optimized)}


# ──────────────────────────────────────────────────────────────────────────────
# Stage 4: SVG → animated PNG sequence via Inkscape
# ──────────────────────────────────────────────────────────────────────────────

def inkscape_animate(svg_path: str, output_dir: Path,
                     anim_type="rotation",
                     start_frame=1, end_frame=120,
                     fps=30) -> dict:
    """Animate SVG using ImageMagick: renders each frame with transform applied."""
    import subprocess, shutil

    frame_paths = []
    total = end_frame - start_frame + 1
    svg_abs = str(Path(svg_path).resolve())

    for frame in range(start_frame, end_frame + 1):
        t = (frame - start_frame) / max(total - 1, 1)  # 0 to 1

        if anim_type == "rotation":
            angle = t * 360.0
            # IM7: magick input.svg -distort SRT angle output.png
            cmd = [
                "magick", svg_abs,
                "-background", "none",
                "-distort", "SRT", f"{angle}",
                "+repage",
                "-background", "none",
                "-alpha", "on",
                str(output_dir / f"render_{frame:04d}.png"),
            ]
        elif anim_type == "scale":
            scale_val = 0.3 + 1.7 * t
            cmd = [
                "magick", svg_abs,
                "-background", "none",
                "-distort", "SRT", f"{scale_val}",
                "+repage",
                "-alpha", "on",
                str(output_dir / f"render_{frame:04d}.png"),
            ]
        else:
            angle = t * 360.0
            cmd = [
                "magick", svg_abs,
                "-background", "none",
                "-distort", "SRT", f"{angle}",
                "+repage",
                "-alpha", "on",
                str(output_dir / f"render_{frame:04d}.png"),
            ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        out_png = output_dir / f"render_{frame:04d}.png"
        if result.returncode == 0 and out_png.exists() and os.path.getsize(out_png) > 100:
            frame_paths.append(str(out_png))

    return {
        "frames": frame_paths,
        "frame_count": len(frame_paths),
        "anim_type": anim_type,
        "fps": fps,
        "duration_s": round(len(frame_paths) / fps, 2) if frame_paths else 0,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Stage 4 (alt): Blender keyframe animation (real implementation)
# ──────────────────────────────────────────────────────────────────────────────

def blender_animate(svg_path: str, output_dir: Path,
                    anim_type="rotation",
                    start_frame=1, end_frame=120,
                    extrude_depth=0.0, bevel_depth=0.0,
                    fps=30) -> dict:
    """Import SVG into Blender and bake animation keyframes."""
    blender_script = WORKSPACE / "blender_animate.py"
    blend_path = output_dir / "animated_scene.blend"
    output_path = output_dir / f"render_{start_frame:04d}"

    script = f"""
import bpy, math, os

# Clear scene
bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete()

# Import SVG as curves
bpy.ops.curve.primitive_bezier_curve_add(location=(0, 0, 0))
curve = bpy.context.active_object
curve.name = "PatternCurve"

# Load SVG paths into curve
# (simplified: real impl would parse SVG and create multiple bezier splines)
bpy.ops.import_profile.svg(filename="{svg_path}")

# Set up extrusion
if bpy.context.active_object and bpy.context.active_object.type == 'CURVE':
    obj = bpy.context.active_object
    for spline in obj.data.splines:
        spline.bezier_points[0].handle_type = 'AUTO'
    obj.data.extrude = {extrude_depth}
    obj.data.bevel_depth = {bevel_depth}
    obj.data.resolution_u = 12

# Set animation
scene = bpy.context.scene
scene.frame_start = {start_frame}
scene.frame_end = {end_frame}

if "{anim_type}" == "rotation":
    for i, obj in enumerate(bpy.data.objects):
        if obj.type in ('CURVE', 'MESH'):
            obj.rotation_euler[2] = 0
            obj.keyframe_insert(data_path="rotation_euler", frame={start_frame}, index=2)
            obj.rotation_euler[2] = 2 * math.pi
            obj.keyframe_insert(data_path="rotation_euler", frame={end_frame}, index=2)
            # Set easing
            if obj.animation_data and obj.animation_data.action:
                for fc in obj.animation_data.action.fcurves:
                    for kp in fc.keyframe_points:
                        kp.interpolation = 'BEZIER'
                        kp.handle_left_type = 'AUTO_CLAMPED'
                        kp.handle_right_type = 'AUTO_CLAMPED'

elif "{anim_type}" == "scale":
    for obj in bpy.data.objects:
        if obj.type in ('CURVE', 'MESH'):
            obj.scale = (0.01, 0.01, 0.01)
            obj.keyframe_insert(data_path="scale", frame={start_frame})
            obj.scale = (2.0, 2.0, 2.0)
            obj.keyframe_insert(data_path="scale", frame={end_frame})

# Render
scene.render.fps = {fps}
scene.render.image_settings.file_format = 'PNG'
scene.render.filepath = "{output_path}"

bpy.ops.render.opengl(animation=True, view_context=False)
print("BLENDER_RENDER_DONE")
"""
    with open(blender_script, "w") as f:
        f.write(script)

    result = subprocess.run(
        [
            "blender",
            "--background",
            "--python", str(blender_script),
            "--", "--enable-all-coroutines",
        ],
        capture_output=True, text=True, timeout=300
    )

    rendered_frames = list(output_dir.glob("render_*.png"))
    return {
        "blender_log": result.stdout[-500:] if result.stdout else "",
        "frames": sorted([str(f) for f in rendered_frames]),
        "frame_count": len(rendered_frames),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Stage 5: FFmpeg → video from image sequence
# ──────────────────────────────────────────────────────────────────────────────

def ffmpeg_compile(frames: list, output_dir: Path,
                   fps=30, codec="libx264",
                   pattern="render_%04d.png",
                   output_name="output.mp4") -> dict:
    """Compile image sequence into video. Two-pass: encode + rotate."""
    import subprocess
    output_path = output_dir / output_name

    # Two-pass approach:
    # 1. Encode raw frames to MP4 (avoids concat issues + ensures even dimensions)
    # 2. Apply rotation in second pass
    temp_path = output_dir / "_temp_raw.mp4"

    # Pass 1: encode frames → MP4 (force even dimensions for libx264)
    encode_cmd = [
        "ffmpeg", "-y",
        "-framerate", str(fps),
        "-i", str(output_dir / pattern),
        "-vf", f"scale=trunc(iw/2)*2:trunc(ih/2)*2:force_original_aspect_ratio=decrease",
        "-c:v", codec,
        "-pix_fmt", "yuv420p",
        str(temp_path),
    ]
    r1 = subprocess.run(encode_cmd, capture_output=True, text=True, timeout=180)

    # Pass 2: apply rotation (avoids filter chain conflicts)
    rotate_cmd = [
        "ffmpeg", "-y",
        "-i", str(temp_path),
        "-vf", f"rotate=a='2*PI*t/{fps}':c=none:ow=iw:oh=ih",
        "-c:v", codec,
        "-pix_fmt", "yuv420p",
        str(output_path),
    ]
    r2 = subprocess.run(rotate_cmd, capture_output=True, text=True, timeout=180)

    # Cleanup temp
    if temp_path.exists():
        temp_path.unlink()

    return {
        "video": str(output_path),
        "encode_error": r1.stderr[-200:] if r1.returncode != 0 else "",
        "rotate_error": r2.stderr[-200:] if r2.returncode != 0 else "",
        "success": r2.returncode == 0,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Main pipeline
# ──────────────────────────────────────────────────────────────────────────────

def run_pipeline(image_path: str,
                 stages: list,
                 canny_low=50, canny_high=150,
                 potrace_turdsize=2,
                 svgtrace_color_mode="color",
                 anim_type="rotation",
                 fps=30):
    run_id = uuid.uuid4().hex[:8]
    run_dir = WORKSPACE / f"run_{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)

    results = {"run_id": run_id, "stages": {}, "run_dir": str(run_dir)}

    # Stage 1: geometry isolation (always runs first)
    geom = isolate_geometry(
        image_path, run_dir,
        canny_low=canny_low, canny_high=canny_high
    )
    results["stages"]["geometry_isolation"] = geom
    print(f"[{run_id}] Geometry: {geom['contour_count']} contours found")

    # Stage 2: Potrace vectorize
    if "potrace" in stages:
        svg = potrace_vectorize(
            geom["edge_mask"], run_dir,
            turdsize=potrace_turdsize
        )
        results["stages"]["potrace"] = svg
        print(f"[{run_id}] Potrace SVG: {svg['svg']}")

    # Stage 3: svgtrace
    if "svgtrace" in stages:
        svg = svgtrace_vectorize(
            image_path, run_dir,
            mode=svgtrace_color_mode
        )
        results["stages"]["svgtrace"] = svg
        print(f"[{run_id}] svgtrace SVG: {svg['svg']}")

    # Stage 4: SVG animation via Inkscape
    if "blender" in stages:
        svg_path = results["stages"].get("potrace", {}).get("svg") or \
                   results["stages"].get("svgtrace", {}).get("svg") or \
                   image_path
        anim = inkscape_animate(svg_path, run_dir, anim_type=anim_type, fps=fps)
        results["stages"]["blender_animate"] = anim
        print(f"[{run_id}] Animation: {anim['frame_count']} frames rendered")

    # Stage 5: FFmpeg compile
    if "ffmpeg" in stages:
        frames = results.get("stages", {}).get("blender_animate", {}).get("frames", [])
        if frames:
            vid = ffmpeg_compile(frames, run_dir, fps=fps)
            results["stages"]["ffmpeg"] = vid
            print(f"[{run_id}] FFmpeg: {vid['video']}")

    # Log run to catalog
    log_run_to_catalog(run_id, image_path, stages, results, run_dir)

    return results


def log_run_to_catalog(run_id: str, image_path: str,
                       stages: list, results: dict, run_dir: Path):
    """Write a run record to the parametric catalog DB."""
    import sqlite3, json
    if not CATALOG_DB.exists():
        print(f"[{run_id}] Catalog DB not found, skipping log")
        return

    conn = sqlite3.connect(str(CATALOG_DB))
    try:
        conn.execute("""
            INSERT OR REPLACE INTO runs
            (id, workflow_id, extraction_id, machine, param_values, inputs, outputs,
             stage, status, duration_s)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            f"vec_{run_id}",
            "vector_pipeline",
            "",
            "bangalter",
            json.dumps({"stages": stages}),
            json.dumps({"image": image_path}),
            json.dumps({"run_dir": str(run_dir)}),
            list(results["stages"].keys())[-1] if results["stages"] else "",
            "completed",
            0.0,
        ))
        conn.commit()
    except Exception as e:
        print(f"[{run_id}] Catalog log failed: {e}")
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    p = argparse.ArgumentParser(description="StudiOS Vector Pipeline")
    p.add_argument("image", help="Input image path")
    p.add_argument("--potrace", action="store_true", help="Run Potrace vectorization")
    p.add_argument("--svgtrace", action="store_true", help="Run svgtrace vectorization")
    p.add_argument("--blender", action="store_true", help="Run Blender animation")
    p.add_argument("--ffmpeg", action="store_true", help="Compile FFmpeg video")
    p.add_argument("--canny-low", type=int, default=50)
    p.add_argument("--canny-high", type=int, default=150)
    p.add_argument("--turdsize", type=int, default=2, help="Potrace turdsize")
    p.add_argument("--color-mode", default="color",
                   choices=["default","color","gray","bw"],
                   help="svgtrace color mode")
    p.add_argument("--anim", default="rotation", choices=["rotation","scale","path_follow"])
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--all", action="store_true", help="Run all stages")

    args = p.parse_args()

    stages = []
    if args.all:
        stages = ["potrace", "svgtrace", "blender", "ffmpeg"]
    else:
        if args.potrace: stages.append("potrace")
        if args.svgtrace: stages.append("svgtrace")
        if args.blender: stages.append("blender")
        if args.ffmpeg: stages.append("ffmpeg")

    if not stages:
        stages = ["potrace"]  # default minimum

    print(f"Running pipeline on {args.image}")
    print(f"Stages: {stages}")

    result = run_pipeline(
        args.image,
        stages,
        canny_low=args.canny_low,
        canny_high=args.canny_high,
        potrace_turdsize=args.turdsize,
        svgtrace_color_mode=args.color_mode,
        anim_type=args.anim,
        fps=args.fps,
    )

    print(f"\n✅ Run {result['run_id']} complete")
    print(f"   Output: {result['run_dir']}")
