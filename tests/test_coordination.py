"""Tests for the StudiOS coordination package."""

import unittest
from pathlib import Path
import tempfile
import shutil

from studios_coordination.contracts import (
    JobRecord, JobState, AttemptRecord, AttemptState, LeaseRecord,
)
from studios_coordination.store import Store
from studios_coordination.policy import PolicyGates, PolicyError, evaluate_gates
from studios_coordination.scheduler import (
    WorkerCapability, JobRequest, RouteEstimate, SchedulingPolicy, route_job,
)
from studios_coordination.transport import LoopbackTransport
from studios_coordination.receipts import build_receipt, compute_content_hash, write_receipt, read_receipt
from studios_coordination.adapters import get_adapter, list_kinds


class TestContracts(unittest.TestCase):
    def test_job_record_defaults(self):
        j = JobRecord(job_id="j1", task_kind="benchmark")
        self.assertEqual(j.state, JobState.PROPOSED)
        self.assertEqual(j.privacy_class, "private")
        self.assertEqual(j.execution_mode, "batch")

    def test_job_state_enum_is_str(self):
        self.assertEqual(JobState.PROPOSED.value, "proposed")
        self.assertIsInstance(JobState.PROPOSED, str)


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Store(Path(self.tmp) / "test.db")

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.tmp)

    def test_insert_and_get_job(self):
        j = JobRecord(job_id="j1", task_kind="benchmark")
        self.db.insert_job(j)
        fetched = self.db.get_job("j1")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.job_id, "j1")
        self.assertEqual(fetched.state, JobState.PROPOSED)

    def test_update_job_state(self):
        j = JobRecord(job_id="j2", task_kind="benchmark")
        self.db.insert_job(j)
        ok = self.db.update_job_state("j2", JobState.RUNNING, "worker1")
        self.assertTrue(ok)
        fetched = self.db.get_job("j2")
        self.assertEqual(fetched.state, JobState.RUNNING)
        self.assertEqual(fetched.assigned_worker, "worker1")

    def test_lease_acquire_and_release(self):
        import uuid
        rec = LeaseRecord(
            job_id="j3",
            worker_id="w1",
            expires_at="2099-01-01T00:00:00+00:00",
            fencing_token=str(uuid.uuid4()),
        )
        self.assertTrue(self.db.acquire_lease(rec))
        self.assertFalse(self.db.acquire_lease(rec))  # duplicate blocked
        released = self.db.release_lease("j3", "w1")
        self.assertTrue(released)
        self.assertIsNone(self.db.get_lease("j3"))

    def test_list_jobs_filtered(self):
        for i in range(5):
            self.db.insert_job(JobRecord(job_id=f"j{i}", task_kind="benchmark"))
        self.db.update_job_state("j0", JobState.RUNNING)
        self.db.update_job_state("j1", JobState.RUNNING)
        running = list(self.db.list_jobs(JobState.RUNNING))
        self.assertEqual(len(running), 2)


class TestPolicy(unittest.TestCase):
    def test_restricted_requires_local(self):
        gates = PolicyGates(require_local_for_restricted=True)
        worker = WorkerCapability(
            worker_id="w1", task_kinds=("benchmark",),
            backends=("cpu",), vram_gb=0, system_memory_gb=64,
            max_parallel=2, active_jobs=0, local=False, online=True,
        )
        job = JobRecord(job_id="j1", task_kind="benchmark", privacy_class="restricted")
        with self.assertRaises(PolicyError) as ctx:
            evaluate_gates(job, worker, gates=gates,
                           estimated_cost_usd=0, estimated_energy_wh=0,
                           estimated_runtime_seconds=0, attempt_count=0)
        self.assertIn("restricted", str(ctx.exception))

    def test_offline_worker_blocked(self):
        gates = PolicyGates()
        worker = WorkerCapability(
            worker_id="w1", task_kinds=("benchmark",),
            backends=("cpu",), vram_gb=0, system_memory_gb=64,
            max_parallel=2, active_jobs=0, local=True, online=False,
        )
        job = JobRecord(job_id="j1", task_kind="benchmark")
        with self.assertRaises(PolicyError) as ctx:
            evaluate_gates(job, worker, gates=gates,
                           estimated_cost_usd=0, estimated_energy_wh=0,
                           estimated_runtime_seconds=0, attempt_count=0)
        self.assertIn("offline", str(ctx.exception))


class TestScheduler(unittest.TestCase):
    def test_route_job_picks_lowest_energy(self):
        workers = [
            WorkerCapability("w1", task_kinds=("benchmark",), backends=("gpu",),
                            vram_gb=24, system_memory_gb=64, max_parallel=2,
                            active_jobs=0, local=True, online=True),
        ]
        estimates = [
            RouteEstimate("w1", "benchmark", runtime_seconds=120,
                         energy_wh=500, credit_cost=0),
        ]
        job = JobRequest("j1", task_kind="benchmark", privacy_class="private",
                         execution_mode="batch")
        decision = route_job(job, workers, estimates, SchedulingPolicy())
        self.assertEqual(decision.worker_id, "w1")
        self.assertEqual(decision.energy_wh, 500)

    def test_route_job_no_matching_worker(self):
        workers = [
            WorkerCapability("w1", task_kinds=("comfyui",), backends=("gpu",),
                            vram_gb=24, system_memory_gb=64, max_parallel=2,
                            active_jobs=0, local=True, online=True),
        ]
        estimates = [
            RouteEstimate("w1", "comfyui", runtime_seconds=120,
                         energy_wh=500, credit_cost=0),
        ]
        job = JobRequest("j1", task_kind="benchmark", privacy_class="private",
                         execution_mode="batch")
        decision = route_job(job, workers, estimates, SchedulingPolicy())
        self.assertIsNone(decision.worker_id)


class TestTransport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.t = LoopbackTransport(Path(self.tmp) / "transport")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_submit_and_list_proposal(self):
        job = JobRecord(job_id="j1", task_kind="benchmark")
        self.t.submit_proposal(job)
        proposals = list(self.t.list_proposals())
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0][0], "j1")

    def test_withdraw_proposal(self):
        job = JobRecord(job_id="j2", task_kind="benchmark")
        self.t.submit_proposal(job)
        self.assertTrue(self.t.withdraw_proposal("j2"))
        self.assertFalse(list(self.t.list_proposals()))
        self.assertFalse(self.t.withdraw_proposal("nonexistent"))

    def test_claim_and_receipt(self):
        self.t.write_claim("j3", "w1")
        self.t.write_receipt("j3", "w1", "completed")
        claim = self.t.read_claim("j3")
        receipt = self.t.read_receipt("j3")
        self.assertEqual(claim["worker_id"], "w1")
        self.assertEqual(receipt["outcome"], "completed")

    def test_purge(self):
        job = JobRecord(job_id="j4", task_kind="benchmark")
        self.t.submit_proposal(job)
        self.t.write_claim("j4", "w1")
        self.t.write_receipt("j4", "w1", "completed")
        self.t.purge("j4")
        self.assertIsNone(self.t.read_claim("j4"))
        self.assertIsNone(self.t.read_receipt("j4"))


class TestReceipts(unittest.TestCase):
    def test_build_receipt_and_roundtrip(self):
        receipt = build_receipt("j1", "w1", "completed", None)
        self.assertEqual(receipt.outcome, "completed")
        self.assertEqual(receipt.content_hash, "none")

        tmp = tempfile.mktemp(suffix=".json")
        write_receipt(receipt, tmp)
        loaded = read_receipt(tmp)
        self.assertEqual(loaded.job_id, "j1")
        self.assertEqual(loaded.worker_id, "w1")

    def test_compute_content_hash_none(self):
        self.assertEqual(compute_content_hash(None), "none")

    def test_compute_content_hash_absent(self):
        self.assertEqual(compute_content_hash("/nonexistent/path"), "absent")


class TestAdapters(unittest.TestCase):
    def test_list_kinds(self):
        kinds = list_kinds()
        self.assertIn("comfyui-workflow", kinds)
        self.assertIn("benchmark", kinds)

    def test_get_adapter_benchmark(self):
        adapter = get_adapter("benchmark")
        self.assertEqual(adapter.kind, "benchmark")
        errors = adapter.validate_params({"benchmark_name": "test"})
        self.assertEqual(errors, [])

    def test_get_adapter_unknown_raises(self):
        from studios_coordination.adapters import AdapterError
        with self.assertRaises(AdapterError):
            get_adapter("nonexistent-kind")


if __name__ == "__main__":
    unittest.main()
