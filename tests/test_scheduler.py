import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from studios_coordination.scheduler import (
    JobRequest,
    RouteEstimate,
    SchedulingPolicy,
    WorkerCapability,
    claim_job,
    route_job,
)


class SchedulerRoutingTests(unittest.TestCase):
    def setUp(self):
        self.mac = WorkerCapability(
            worker_id="macbook-m5-max",
            task_kinds=("private-rag", "intent", "h3-preview"),
            backends=("mlx", "mps", "comfyui"),
            vram_gb=128,
            system_memory_gb=128,
            max_parallel=2,
            active_jobs=0,
            local=True,
            online=True,
        )
        self.enki = WorkerCapability(
            worker_id="enki-ii-4090",
            task_kinds=("private-rag", "h3-preview", "h3-final"),
            backends=("cuda", "comfyui", "pinokio"),
            vram_gb=24,
            system_memory_gb=64,
            max_parallel=1,
            active_jobs=0,
            local=True,
            online=True,
        )
        self.ada = WorkerCapability(
            worker_id="cuda-6000-ada",
            task_kinds=("h3-preview", "h3-final", "model-training"),
            backends=("cuda", "comfyui", "pinokio"),
            vram_gb=48,
            system_memory_gb=128,
            max_parallel=1,
            active_jobs=0,
            local=True,
            online=True,
        )

    def test_background_private_job_chooses_lowest_energy_qualified_worker(self):
        job = JobRequest(
            job_id="rag-1",
            task_kind="private-rag",
            privacy_class="restricted",
            execution_mode="background",
            required_backends=("mlx",),
        )
        estimates = [
            RouteEstimate("macbook-m5-max", "private-rag", 80, 1.3, 0),
            RouteEstimate("enki-ii-4090", "private-rag", 30, 3.0, 0),
        ]

        decision = route_job(job, [self.mac, self.enki], estimates, SchedulingPolicy())

        self.assertEqual(decision.worker_id, "macbook-m5-max")
        self.assertEqual(decision.reason, "lowest-policy-cost")
        self.assertEqual(decision.credit_cost, 0)

    def test_interactive_job_prioritizes_latency(self):
        job = JobRequest(
            job_id="preview-1",
            task_kind="h3-preview",
            privacy_class="internal",
            execution_mode="interactive",
            required_backends=("comfyui",),
            min_vram_gb=20,
        )
        estimates = [
            RouteEstimate("macbook-m5-max", "h3-preview", 420, 5.0, 0),
            RouteEstimate("enki-ii-4090", "h3-preview", 95, 7.0, 0),
        ]

        decision = route_job(job, [self.mac, self.enki], estimates, SchedulingPolicy())

        self.assertEqual(decision.worker_id, "enki-ii-4090")

    def test_full_quality_video_routes_to_worker_with_required_vram(self):
        job = JobRequest(
            job_id="final-1",
            task_kind="h3-final",
            privacy_class="internal",
            execution_mode="background",
            required_backends=("cuda", "comfyui"),
            min_vram_gb=40,
        )
        estimates = [
            RouteEstimate("enki-ii-4090", "h3-final", 500, 40, 0),
            RouteEstimate("cuda-6000-ada", "h3-final", 180, 22, 0),
        ]

        decision = route_job(job, [self.enki, self.ada], estimates, SchedulingPolicy())

        self.assertEqual(decision.worker_id, "cuda-6000-ada")

    def test_cloud_worker_requires_explicit_policy_and_credit_budget(self):
        cloud = WorkerCapability(
            worker_id="cloud-gpu",
            task_kinds=("h3-final",),
            backends=("cuda", "comfyui"),
            vram_gb=80,
            system_memory_gb=192,
            max_parallel=4,
            active_jobs=0,
            local=False,
            online=True,
        )
        job = JobRequest("cloud-1", "h3-final", "public", "background")
        estimate = RouteEstimate("cloud-gpu", "h3-final", 60, 0, 1.25)

        denied = route_job(job, [cloud], [estimate], SchedulingPolicy())
        allowed = route_job(
            job,
            [cloud],
            [estimate],
            SchedulingPolicy(allow_cloud=True, max_credit_cost=2.0),
        )

        self.assertIsNone(denied.worker_id)
        self.assertEqual(denied.reason, "no-qualified-worker")
        self.assertEqual(allowed.worker_id, "cloud-gpu")

    def test_restricted_job_never_routes_to_cloud_even_when_cloud_is_enabled(self):
        cloud = WorkerCapability(
            worker_id="cloud-gpu",
            task_kinds=("private-rag",),
            backends=("cuda",),
            vram_gb=80,
            system_memory_gb=192,
            max_parallel=4,
            active_jobs=0,
            local=False,
            online=True,
        )
        job = JobRequest("private-1", "private-rag", "restricted", "background")
        estimate = RouteEstimate("cloud-gpu", "private-rag", 10, 0, 0.01)

        decision = route_job(
            job,
            [cloud],
            [estimate],
            SchedulingPolicy(allow_cloud=True, max_credit_cost=10),
        )

        self.assertIsNone(decision.worker_id)

    def test_runtime_and_energy_budgets_filter_routes(self):
        job = JobRequest("budget-1", "h3-final", "internal", "background")
        estimates = [RouteEstimate("cuda-6000-ada", "h3-final", 180, 22, 0)]

        too_slow = route_job(
            job,
            [self.ada],
            estimates,
            SchedulingPolicy(max_runtime_seconds=120),
        )
        too_hungry = route_job(
            job,
            [self.ada],
            estimates,
            SchedulingPolicy(max_energy_wh=20),
        )

        self.assertIsNone(too_slow.worker_id)
        self.assertIsNone(too_hungry.worker_id)

    def test_busy_worker_is_not_routed_more_work(self):
        busy = WorkerCapability(**{**self.enki.__dict__, "active_jobs": 1})
        job = JobRequest("busy-1", "h3-preview", "internal", "background")
        estimate = RouteEstimate("enki-ii-4090", "h3-preview", 90, 7, 0)

        decision = route_job(job, [busy], [estimate], SchedulingPolicy())

        self.assertIsNone(decision.worker_id)
        self.assertEqual(decision.reason, "no-qualified-worker")


class DurableLeaseTests(unittest.TestCase):
    def test_claim_is_idempotent_and_exclusive_until_expiry(self):
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            lease_path = Path(directory) / "job.lease.json"

            first = claim_job(lease_path, job_id="job-1", worker_id="enki", now=now, ttl_seconds=30)
            duplicate = claim_job(lease_path, job_id="job-1", worker_id="enki", now=now, ttl_seconds=30)
            competing = claim_job(lease_path, job_id="job-1", worker_id="ada", now=now, ttl_seconds=30)
            after_expiry = claim_job(
                lease_path,
                job_id="job-1",
                worker_id="ada",
                now=now + timedelta(seconds=31),
                ttl_seconds=30,
            )

            self.assertTrue(first.acquired)
            self.assertTrue(duplicate.acquired)
            self.assertEqual(duplicate.reason, "already-owned")
            self.assertFalse(competing.acquired)
            self.assertEqual(competing.reason, "leased-by-other-worker")
            self.assertTrue(after_expiry.acquired)

    def test_malformed_existing_lease_fails_closed(self):
        now = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            lease_path = Path(directory) / "job-1.lease.json"
            lease_path.write_text("not-json", encoding="utf-8")

            result = claim_job(
                lease_path,
                job_id="job-1",
                worker_id="worker-a",
                now=now,
                ttl_seconds=60,
            )

            self.assertFalse(result.acquired)
            self.assertEqual(result.reason, "malformed-existing-lease")


if __name__ == "__main__":
    unittest.main()
