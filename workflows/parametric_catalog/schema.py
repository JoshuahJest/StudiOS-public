"""
StudiOS Parametric Creation Catalog
Schema + DB layer for tracking source materials, extracted patterns,
parametric methods, workflow variants, and output assets.

Schema design principles:
- Each source image can yield multiple patterns (geometry, texture, color, rhythm)
- Each pattern can be recreated via multiple METHODs (Blender curve ops, TouchDesigner
  TOPs, ComfyUI i2i, SVG commands, etc.)
- Each method tracks params/knobs so it can be re-run or iterated
- Workflow variants are named sequences of methods with a stated aesthetic goal
- Runs track actual parameter values used, machine that ran it, outputs produced
"""

import sqlite3, uuid, json, time
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = REPO_ROOT / "workflows" / "parametric_catalog" / "studi_catalog.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# ──────────────────────────────────────────────────────────────────────────────
# Schema
# ──────────────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id          TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK(kind IN ('pinterest_board','pinterest_pin','local_file','url')),
    uri         TEXT,                          -- board URL, file path, or URL
    thumbnail   TEXT,                          -- local preview path
    metadata    TEXT,                          -- JSON: board name, pin count, etc.
    created_at  REAL DEFAULT (unixepoch()),
    updated_at  REAL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS extractions (
    id              TEXT PRIMARY KEY,
    source_id       TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    extraction_type TEXT NOT NULL CHECK(extraction_type IN (
                    'geometry','edge','texture','color_palette','rhythm',
                    'pattern_tile','vector_outline','silhouette','symmetry_axis')),
    label           TEXT NOT NULL,
    input_params    TEXT,                      -- JSON: threshold, model used, etc.
    output_path     TEXT,                      -- path to isolated raster/vector
    mask_path       TEXT,                      -- optional isolation mask
    quality_score   REAL,                      -- 0-1 human or automated quality estimate
    created_at      REAL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS reverse_prompts (
    id              TEXT PRIMARY KEY,
    extraction_id   TEXT NOT NULL REFERENCES extractions(id) ON DELETE CASCADE,
    provider        TEXT NOT NULL CHECK(provider IN ('minimax','openai','ollama','comfyui')),
    model           TEXT NOT NULL,
    prompt          TEXT NOT NULL,
    style_tags      TEXT,                      -- JSON array of style keywords
    quality_score   REAL,
    created_at      REAL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS parametric_methods (
    id              TEXT PRIMARY KEY,
    label           TEXT NOT NULL,
    toolchain_stage TEXT NOT NULL CHECK(toolchain_stage IN (
                    'vectorize','reconstruct','tile','animate','render','composite')),
    engine          TEXT NOT NULL CHECK(engine IN (
                    'blender','touchdesigner','comfyui','ffmpeg','svg','manual')),
    implementation  TEXT NOT NULL,            -- 'blender_script' | 'svg_commands' | 'comfyui_workflow' | 'td_top'
    input_extraction_types TEXT,               -- JSON: list of extraction.types this consumes
    param_schema    TEXT,                      -- JSON: named params with types/defaults/bounds
    description     TEXT,                      -- human description of what this method does
    created_at      REAL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS workflow_variants (
    id              TEXT PRIMARY KEY,
    label           TEXT NOT NULL,
    description     TEXT,
    aesthetic_goal  TEXT,                      -- free text: "Art Deco geometric panel"
    methods         TEXT NOT NULL,            -- JSON array of {method_id, order, param_overrides}
    machine         TEXT CHECK(machine IN ('bangalter','enki2','fukurokuju','shared')),
    status          TEXT DEFAULT 'draft' CHECK(status IN (
                    'draft','verified','archived')),
    created_at      REAL DEFAULT (unixepoch()),
    updated_at      REAL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS runs (
    id              TEXT PRIMARY KEY,
    workflow_id     TEXT NOT NULL REFERENCES workflow_variants(id),
    extraction_id   TEXT REFERENCES extractions(id),
    machine         TEXT NOT NULL,
    param_values    TEXT,                      -- JSON: actual params used
    inputs          TEXT,                      -- JSON: input paths / URIs used
    outputs         TEXT,                      -- JSON: output paths produced
    stage           TEXT,                      -- current/last stage completed
    status          TEXT NOT NULL CHECK(status IN (
                    'queued','running','paused','completed','failed')),
    error           TEXT,
    duration_s      REAL,
    created_at      REAL DEFAULT (unixepoch()),
    completed_at    REAL
);

CREATE TABLE IF NOT EXISTS assets (
    id              TEXT PRIMARY KEY,
    run_id          TEXT REFERENCES runs(id),
    kind            TEXT NOT NULL CHECK(kind IN (
                    'vector_svg','vector_blend','raster_png','raster_tga',
                    'animation_mp4','animation_frames','texture_tile','style_preset')),
    path            TEXT NOT NULL,
    thumbnail       TEXT,
    metadata        TEXT,                      -- JSON: dims, fps, duration, etc.
    created_at      REAL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS style_catalog (
    id              TEXT PRIMARY KEY,
    label           TEXT NOT NULL UNIQUE,
    description     TEXT,
    keywords        TEXT,                      -- JSON array
    mood            TEXT,
    era_inflection  TEXT,                      -- e.g. "1920s Art Deco", "1980s Memphis"
    reference_path  TEXT,                      -- example image
    created_at      REAL DEFAULT (unixepoch())
);

CREATE INDEX IF NOT EXISTS idx_extractions_source  ON extractions(source_id);
CREATE INDEX IF NOT EXISTS idx_runs_workflow       ON runs(workflow_id);
CREATE INDEX IF NOT EXISTS idx_runs_status          ON runs(status);
CREATE INDEX IF NOT EXISTS idx_assets_run          ON assets(run_id);
"""

# ──────────────────────────────────────────────────────────────────────────────
# Dataclasses
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Source:
    label: str
    kind: str
    uri: str = ""
    thumbnail: str = ""
    metadata: dict = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def save(self, db):
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""INSERT OR REPLACE INTO sources
            (id,label,kind,uri,thumbnail,metadata,updated_at)
            VALUES (?,?,?,?,?,?,unixepoch())""",
            (self.id, self.label, self.kind, self.uri,
             self.thumbnail, json.dumps(self.metadata)))
        conn.commit(); conn.close()

@dataclass
class Extraction:
    source_id: str
    extraction_type: str
    label: str
    input_params: dict = field(default_factory=dict)
    output_path: str = ""
    mask_path: str = ""
    quality_score: float = 0.0
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def save(self, db):
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""INSERT OR REPLACE INTO extractions
            (id,source_id,extraction_type,label,input_params,output_path,mask_path,quality_score)
            VALUES (?,?,?,?,?,?,?,?)""",
            (self.id, self.source_id, self.extraction_type, self.label,
             json.dumps(self.input_params), self.output_path, self.mask_path, self.quality_score))
        conn.commit(); conn.close()

@dataclass
class ParametricMethod:
    label: str
    toolchain_stage: str
    engine: str
    implementation: str  # blender_script | svg_commands | comfyui_workflow | td_top
    input_extraction_types: list = field(default_factory=list)
    param_schema: dict = field(default_factory=dict)
    description: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def save(self, db):
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""INSERT OR REPLACE INTO parametric_methods
            (id,label,toolchain_stage,engine,implementation,input_extraction_types,param_schema,description)
            VALUES (?,?,?,?,?,?,?,?)""",
            (self.id, self.label, self.toolchain_stage, self.engine,
             self.implementation, json.dumps(self.input_extraction_types),
             json.dumps(self.param_schema), self.description))
        conn.commit(); conn.close()

@dataclass
class WorkflowVariant:
    label: str
    methods: list  # [{method_id, order, param_overrides}, ...]
    aesthetic_goal: str = ""
    description: str = ""
    machine: str = "bangalter"
    status: str = "draft"
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def save(self, db):
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""INSERT OR REPLACE INTO workflow_variants
            (id,label,description,aesthetic_goal,methods,machine,status,updated_at)
            VALUES (?,?,?,?,?,?,?,unixepoch())""",
            (self.id, self.label, self.description, self.aesthetic_goal,
             json.dumps(self.methods), self.machine, self.status))
        conn.commit(); conn.close()

@dataclass
class Run:
    workflow_id: str
    machine: str
    extraction_id: str = ""
    param_values: dict = field(default_factory=dict)
    inputs: dict = field(default_factory=dict)
    outputs: list = field(default_factory=list)
    stage: str = ""
    status: str = "queued"
    error: str = ""
    duration_s: float = 0.0
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def save(self, db):
        completed_at = time.time() if self.status in ('completed','failed') else None
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""INSERT OR REPLACE INTO runs
            (id,workflow_id,extraction_id,machine,param_values,inputs,outputs,stage,status,error,duration_s,completed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (self.id, self.workflow_id, self.extraction_id, self.machine,
             json.dumps(self.param_values), json.dumps(self.inputs),
             json.dumps(self.outputs), self.stage, self.status,
             self.error, self.duration_s, completed_at))
        conn.commit(); conn.close()

# ──────────────────────────────────────────────────────────────────────────────
# Init
# ──────────────────────────────────────────────────────────────────────────────

def init():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    print(f"✅ Catalog DB initialized at {DB_PATH}")

if __name__ == "__main__":
    init()
