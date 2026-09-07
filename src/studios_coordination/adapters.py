"""Registered workload adapters — the only code the scheduler may execute.

Each adapter is a versioned, typed workload kind. The scheduler never issues
raw shell commands; it calls an adapter method.
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class AdapterError(Exception):
    pass


@dataclass(frozen=True)
class AdapterContext:
    """Everything an adapter gets at runtime."""
    job_id: str
    input_artifact: Path | None
    output_dir: Path
    host_id: str
    workdir: Path


class WorkloadAdapter(ABC):
    """Base class for all StudiOS workload adapters."""

    # Set by the registry on discovery
    kind: str = "base"
    version: str = "0.0.0"

    @abstractmethod
    def run(self, ctx: AdapterContext, params: dict[str, Any]) -> Path | None:
        """Execute the workload. Returns the output artifact path or None."""
        ...

    @abstractmethod
    def validate_params(self, params: dict[str, Any]) -> list[str]:
        """Return list of validation error messages. Empty = valid."""
        ...

    def description(self) -> str:
        return f"{self.kind} v{self.version}"


# ── Built-in adapters ──────────────────────────────────────────────────────

class ComfyUIAdapter(WorkloadAdapter):
    """Submit a ComfyUI workflow via the local HTTP API."""

    kind = "comfyui-workflow"
    version = "0.2.0"

    def __init__(self, comfy_url: str = "http://127.0.0.1:8188"):
        self.comfy_url = comfy_url.rstrip("/")
        self._poll_interval = 5  # seconds
        self._poll_timeout = 300  # 5 minutes

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = []
        has_workflow = "workflow_json" in params or "workflow_path" in params
        if not has_workflow:
            errors.append("must supply workflow_json (str) or workflow_path (str)")
        if "workflow_json" in params and not isinstance(params["workflow_json"], str):
            errors.append("workflow_json must be a string")
        if "workflow_path" in params and not isinstance(params["workflow_path"], str):
            errors.append("workflow_path must be a string")
        if "prompt_inputs" in params and not isinstance(params.get("prompt_inputs"), dict):
            errors.append("prompt_inputs must be a dict of node_id -> value")
        return errors

    def _poll_for_completion(self, prompt_id: str, client: httpx.Client) -> dict:
        """Poll /history until the prompt result appears. Raises AdapterError on timeout."""
        elapsed = 0
        while elapsed < self._poll_timeout:
            resp = client.get(f"{self.comfy_url}/history/{prompt_id}")
            resp.raise_for_status()
            history = resp.json()
            if prompt_id in history:
                return history[prompt_id]
            time.sleep(self._poll_interval)
            elapsed += self._poll_interval
        raise AdapterError(f"prompt {prompt_id} did not complete within {self._poll_timeout}s")

    def _apply_prompt_inputs(self, wf: dict, prompt_inputs: dict | None) -> dict:
        """Apply user-provided input values to workflow nodes."""
        if not prompt_inputs:
            return wf
        wf = json.loads(json.dumps(wf))  # deep copy
        for node_id, value in prompt_inputs.items():
            if node_id in wf.get("nodes", wf):
                wf["nodes"][node_id].update(value)
            elif node_id in wf:
                wf[node_id].update(value)
        return wf

    def _download_output(self, filename: str, ctx: AdapterContext, client: httpx.Client) -> Path:
        """Download output file from /view and save to output_dir."""
        resp = client.get(f"{self.comfy_url}/view", params={"filename": filename})
        resp.raise_for_status()
        out_path = ctx.output_dir / filename
        out_path.write_bytes(resp.content)
        return out_path

    def run(self, ctx: AdapterContext, params: dict[str, Any]) -> Path | None:
        import httpx  # lazy — only needed when this adapter is actually used

        # Load workflow
        if "workflow_json" in params:
            wf = json.loads(params["workflow_json"])
        else:
            wf_path = Path(params["workflow_path"])
            wf = json.loads(wf_path.read_text(encoding="utf-8"))

        # Apply optional node inputs
        prompt_inputs = params.get("prompt_inputs")
        wf = self._apply_prompt_inputs(wf, prompt_inputs)

        with httpx.Client(timeout=self._poll_timeout + 30.0) as client:
            # Submit workflow
            resp = client.post(
                f"{self.comfy_url}/prompt",
                json={"prompt": wf},
            )
            resp.raise_for_status()
            prompt_id = resp.json().get("prompt_id")
            if not prompt_id:
                raise AdapterError("no prompt_id returned from /prompt")

            # Poll for completion
            result = self._poll_for_completion(prompt_id, client)

            # Download outputs
            outputs = result.get("outputs", {})
            saved_paths = []
            for node_id, output_data in outputs.items():
                if "images" in output_data:
                    for img in output_data["images"]:
                        filename = img.get("filename")
                        if filename:
                            saved_paths.append(self._download_output(filename, ctx, client))
                elif "video" in output_data or "videos" in output_data:
                    videos = output_data.get("videos", [output_data.get("video", {})])
                    for vid in videos:
                        filename = vid.get("filename")
                        if filename:
                            saved_paths.append(self._download_output(filename, ctx, client))

            if not saved_paths:
                raise AdapterError(f"no output files found for prompt {prompt_id}")

            # Return first output path (primary artifact)
            return saved_paths[0]

    def description(self) -> str:
        return f"ComfyUI workflow runner targeting {self.comfy_url}"


class BenchmarkAdapter(WorkloadAdapter):
    """Run a fixed benchmark suite and return a JSON results artifact."""

    kind = "benchmark"
    version = "0.1.0"

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = []
        if "benchmark_name" not in params:
            errors.append("benchmark_name is required")
        return errors

    def run(self, ctx: AdapterContext, params: dict[str, Any]) -> Path | None:
        import json, time

        name = params["benchmark_name"]
        result = {
            "benchmark": name,
            "host_id": ctx.host_id,
            "job_id": ctx.job_id,
            "started_at": time.time(),
            "score": 0.0,
        }
        out_path = ctx.output_dir / f"{ctx.job_id}_benchmark.json"
        out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        return out_path

    def description(self) -> str:
        return "Fixed benchmark runner (placeholder — swap for real suite)"


# ── Registry ──────────────────────────────────────────────────────────────

_REGISTRY: dict[str, type[WorkloadAdapter]] = {
    ComfyUIAdapter.kind: ComfyUIAdapter,
    BenchmarkAdapter.kind: BenchmarkAdapter,
}


def register_adapter(cls: type[WorkloadAdapter]) -> None:
    _REGISTRY[cls.kind] = cls


def get_adapter(kind: str, version: str | None = None, **init_kwargs: Any) -> WorkloadAdapter:
    cls = _REGISTRY.get(kind)
    if cls is None:
        raise AdapterError(f"unknown adapter kind: {kind}")
    return cls(**init_kwargs)


def list_kinds() -> list[str]:
    return list(_REGISTRY.keys())
