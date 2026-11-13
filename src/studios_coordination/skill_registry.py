"""Skill-to-Worker registry for StudiOS prioritization.

Maps task_kind → required skill tags → which workers have those skills.
Reads live state from:
  - MacBook Hermes skills dir + config
  - ENKI-II LM Studio API :1234
  - SOLARIS local config (when repaired)
"""

from __future__ import annotations

import json
import logging
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from .host_identity import LOCAL_WORKER_ID

log = logging.getLogger("skill-registry")

# ── Skill tag taxonomy ────────────────────────────────────────────────────────

# Which skill tags a task_kind requires to be considered skilled
TASK_KIND_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "comfyui-workflow": ("comfyui",),
    "cuda-inference": ("cuda", "torch"),
    "video-render": ("nvenc", "ffmpeg"),
    "training": ("cuda", "torch", "training"),
    "benchmark": ("benchmark-runner",),
    "document-draft": ("writing", "notes"),
    "code-review": ("code-analysis", "security-audit"),
    "local-ollama": ("ollama",),
    "image-generation": ("comfyui", "diffusion"),
    "nas-file-op": ("nas-access",),
}

# Which backend a skill tag implies
SKILL_BACKEND_MAP: dict[str, tuple[str, ...]] = {
    "comfyui": ("comfyui",),
    "cuda": ("cuda",),
    "torch": ("torch", "cuda"),
    "nvenc": ("nvenc",),
    "ffmpeg": ("ffmpeg",),
    "training": ("cuda", "torch"),
    "benchmark-runner": ("benchmark",),
    "writing": ("cpu",),
    "notes": ("cpu",),
    "code-analysis": ("cpu",),
    "security-audit": ("cpu",),
    "ollama": ("ollama",),
    "diffusion": ("cuda", "mps"),
    "nas-access": ("nas",),
}


@dataclass
class WorkerSkills:
    """Skills and backends available on a worker."""
    worker_id: str
    skill_tags: tuple[str, ...] = field(default_factory=tuple)
    backends: tuple[str, ...] = field(default_factory=tuple)
    models_loaded: tuple[str, ...] = field(default_factory=tuple)
    comfyui_url: str | None = None   # e.g. "http://127.0.0.1:8188"
    lmstudio_url: str | None = None  # e.g. "http://<enkii-tailnet-ip>:1234"


class SkillRegistry:
    """Maps task_kind → required skills → available worker skills."""

    def __init__(self):
        self._workers: dict[str, WorkerSkills] = {}
        self._cache_ttl_seconds = 120  # refresh LM Studio every 2 min
        self._last_lmstudio_fetch: float = 0

    # ── Worker registration ───────────────────────────────────────────────────

    def register_worker(self, skills: WorkerSkills) -> None:
        """Register a worker's known skills and backends."""
        self._workers[skills.worker_id] = skills
        log.debug("Registered worker %s with skills=%s backends=%s",
                  skills.worker_id, skills.skill_tags, skills.backends)

    def unregister_worker(self, worker_id: str) -> None:
        self._workers.pop(worker_id, None)

    # ── Required skills per task kind ────────────────────────────────────────

    def get_required_skills(self, task_kind: str) -> tuple[str, ...]:
        """Return the skill tags required for a task kind."""
        return TASK_KIND_REQUIREMENTS.get(task_kind, ())

    def get_implied_backends(self, task_kind: str) -> tuple[str, ...]:
        """Return the backends implied by a task kind's required skills."""
        required = self.get_required_skills(task_kind)
        backends: set[str] = set()
        for skill in required:
            backends.update(SKILL_BACKEND_MAP.get(skill, ()))
        return tuple(sorted(backends))

    # ── Skill availability ───────────────────────────────────────────────────

    def worker_has_skill(self, worker_id: str, skill_tag: str) -> bool:
        """Check if a worker has a specific skill tag."""
        worker = self._workers.get(worker_id)
        if worker is None:
            return False
        return skill_tag in worker.skill_tags

    def worker_has_all_skills(self, worker_id: str, skill_tags: tuple[str, ...]) -> bool:
        """Check if a worker has all required skill tags."""
        for tag in skill_tags:
            if not self.worker_has_skill(worker_id, tag):
                return False
        return True

    def worker_has_backend(self, worker_id: str, backend: str) -> bool:
        """Check if a worker has a specific backend (e.g. 'cuda', 'mps')."""
        worker = self._workers.get(worker_id)
        if worker is None:
            return False
        return backend in worker.backends

    def worker_has_all_backends(self, worker_id: str,
                                 backends: tuple[str, ...]) -> bool:
        """Check if a worker has all required backends."""
        for be in backends:
            if not self.worker_has_backend(worker_id, be):
                return False
        return True

    # ── Scoring ─────────────────────────────────────────────────────────────

    def skill_score(self, task_kind: str, worker_id: str) -> float:
        """
        0.0 = perfect skill match (all required skills present)
        1.0 = no required skills present
        Interpolated for partial coverage.
        """
        required = self.get_required_skills(task_kind)
        if not required:
            return 0.0  # No skill requirements = always a match

        worker = self._workers.get(worker_id)
        if worker is None:
            return 1.0  # Unknown worker = no match

        if not required:
            return 0.0

        present = sum(1 for tag in required if tag in worker.skill_tags)
        missing_ratio = (len(required) - present) / len(required)
        return missing_ratio

    def backend_score(self, task_kind: str, worker_id: str) -> float:
        """
        0.0 = all implied backends available
        1.0 = no implied backends available
        """
        implied = self.get_implied_backends(task_kind)
        if not implied:
            return 0.0

        worker = self._workers.get(worker_id)
        if worker is None:
            return 1.0

        present = sum(1 for be in implied if be in worker.backends)
        missing_ratio = (len(implied) - present) / len(implied)
        return missing_ratio

    # ── Model discovery ─────────────────────────────────────────────────────

    def fetch_enkii_models(self, lmstudio_url: str = "http://<enkii-tailnet-ip>:1234",
                           cache_ttl: int = 120) -> tuple[str, ...]:
        """Fetch model list from ENKI-II's LM Studio API."""
        import time
        now = time.time()
        if now - self._last_lmstudio_fetch < cache_ttl:
            # Return cached from worker if available
            for w in self._workers.values():
                if w.lmstudio_url == lmstudio_url:
                    return w.models_loaded
            return ()

        try:
            req = urllib.request.Request(
                f"{lmstudio_url}/v1/models",
                headers={"Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=8.0) as resp:
                data = json.loads(resp.read().decode())
            models = tuple(
                m.get("id", "")
                for m in data.get("data", [])
                if m.get("id")
            )
            self._last_lmstudio_fetch = now
            log.debug("Fetched %d models from LM Studio at %s", len(models), lmstudio_url)
            return models
        except Exception as e:
            log.warning("Failed to fetch LM Studio models from %s: %s", lmstudio_url, e)
            return ()

    def fetch_macbook_skills(self, hermes_home: Path | None = None) -> tuple[str, ...]:
        """
        Read installed skill tags from the MacBook Hermes skills directory.
        Reads directory names under ~/.hermes/skills/ as skill tags.
        """
        import os
        if hermes_home is None:
            hermes_home = Path.home() / ".hermes"

        skills_dir = hermes_home / "skills"
        if not skills_dir.exists():
            return ()

        tags: list[str] = []
        for category_dir in skills_dir.iterdir():
            if not category_dir.is_dir():
                continue
            for skill_dir in category_dir.iterdir():
                if skill_dir.is_dir() and (skill_dir / "SKILL.md").exists():
                    tags.append(skill_dir.name)
        return tuple(sorted(set(tags)))

    # ── Convenience ──────────────────────────────────────────────────────────

    def best_skilled_worker(self, task_kind: str,
                            worker_ids: list[str]) -> str | None:
        """Return the worker with the lowest skill_score for a task_kind."""
        scores = {
            wid: self.skill_score(task_kind, wid)
            for wid in worker_ids
        }
        if not scores:
            return None
        best = min(scores, key=lambda w: (scores[w], w))
        if scores[best] >= 1.0:
            return None  # No worker has any required skill
        return best

    def describe_worker(self, worker_id: str) -> dict[str, Any]:
        """Return a readable description of a worker's skills."""
        worker = self._workers.get(worker_id)
        if worker is None:
            return {"worker_id": worker_id, "registered": False}
        return {
            "worker_id": worker.worker_id,
            "registered": True,
            "skill_tags": list(worker.skill_tags),
            "backends": list(worker.backends),
            "models_loaded": list(worker.models_loaded),
            "comfyui_url": worker.comfyui_url,
            "lmstudio_url": worker.lmstudio_url,
        }

    def list_workers(self) -> list[str]:
        return list(self._workers.keys())

    def summary(self) -> dict[str, Any]:
        """Return a full registry summary for debugging/audit."""
        return {
            worker_id: self.describe_worker(worker_id)
            for worker_id in self._workers
        }


# ── Default global registry ────────────────────────────────────────────────────

DEFAULT_REGISTRY = SkillRegistry()


def register_macbook_worker(registry: SkillRegistry | None = None) -> WorkerSkills:
    """Register the MacBook M5 Max as a worker with known skills."""
    if registry is None:
        registry = DEFAULT_REGISTRY

    # ComfyUI is on MacBook at two ports
    comfyui_urls = []
    try:
        import httpx
        with httpx.Client(timeout=3.0) as client:
            r = client.get("http://127.0.0.1:8188/system_stats")
            if r.status_code == 200:
                comfyui_urls.append("http://127.0.0.1:8188")
    except Exception:
        pass

    # Hermes skills on disk
    skill_tags = registry.fetch_macbook_skills()

    # Unified memory MPS capability (MacBook M5 Max has no discrete GPU but has MPS)
    backends = ["mps", "cpu"]
    if comfyui_urls:
        backends.append("comfyui")

    skills = WorkerSkills(
        worker_id=LOCAL_WORKER_ID,
        skill_tags=skill_tags,
        backends=tuple(sorted(set(backends))),
        models_loaded=("minimax-m2.7",),  # OAuth provider, not a loaded model
        comfyui_url=comfyui_urls[0] if comfyui_urls else None,
    )
    registry.register_worker(skills)
    return skills


def register_enkii_worker(registry: SkillRegistry | None = None,
                          lmstudio_url: str = "http://<enkii-tailnet-ip>:1234") -> WorkerSkills:
    """Register ENKI-II RTX 4090 as a worker with known skills."""
    if registry is None:
        registry = DEFAULT_REGISTRY

    models = registry.fetch_enkii_models(lmstudio_url)
    # ENKI-II has LM Studio models but no ComfyUI
    skills = WorkerSkills(
        worker_id="enki-ii",
        skill_tags=("cuda", "torch", "nvenc", "ffmpeg", "lm-studio"),
        backends=("cuda", "nvenc", "torch"),
        models_loaded=models,
        lmstudio_url=lmstudio_url,
    )
    registry.register_worker(skills)
    return skills


def bootstrap_default_registry() -> SkillRegistry:
    """Populate the default registry with known workers. Call at startup."""
    register_macbook_worker(DEFAULT_REGISTRY)
    register_enkii_worker(DEFAULT_REGISTRY)
    return DEFAULT_REGISTRY
