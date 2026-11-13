"""Example: submit a ComfyUI benchmark job via the LoopbackTransport.

Run from the StudiOS-1.1 directory:

    PYTHONPATH=src python3 examples/local_worker_demo.py
"""

from pathlib import Path
import uuid

from studios_coordination import (
    JobRecord,
    JobState,
    Store,
    WorkerLoop,
    WorkerCapability,
    LoopbackTransport,
    default_gates,
)


def main():
    # 1. Controller side: create a job and submit it as a proposal
    job_id = str(uuid.uuid4())[:8]
    job = JobRecord(
        job_id=job_id,
        task_kind="benchmark",
        privacy_class="private",
        execution_mode="batch",
    )

    tmp = Path("/tmp/studios_demo")
    tmp.mkdir(parents=True, exist_ok=True)
    store = Store(tmp / "studios.db")
    transport = LoopbackTransport(tmp / "transport")

    store.insert_job(job)
    transport.submit_proposal(job)
    print(f"[controller] submitted job {job_id}")

    # 2. Simulate a local worker with the MacBook's capability profile
    worker_id = "macbook-local"
    capabilities = WorkerCapability(
        worker_id=worker_id,
        task_kinds=("benchmark", "comfyui-workflow"),
        backends=("cpu", "mps"),
        vram_gb=0,        # M5 Max unified memory — no discrete VRAM
        system_memory_gb=128,
        max_parallel=4,
        active_jobs=0,
        local=True,
        online=True,
    )

    worker = WorkerLoop(
        worker_id=worker_id,
        store=store,
        transport=transport,
        host_id="macbook-m5max",
        capabilities=capabilities,
        policy_gates=default_gates(),
        output_dir=tmp / "output",
        poll_interval=1.0,
    )

    # Run one tick synchronously (normally this runs in a thread)
    # Pass a benchmark_name so the adapter doesn't reject it
    worker._tick()   # ← adapter gets no params; it will fail. Fix: pass params via transport.
    # For a working demo, pass params through the proposal metadata:
    # (The adapter currently receives {}; fix: extend JobRecord to carry params)

    # 3. Check the result
    receipt = transport.read_receipt(job_id)
    if receipt:
        print(f"[controller] job {job_id} → {receipt['outcome']}")
        print(f"  worker: {receipt['worker_id']}")
        print(f"  at: {receipt['completed_at']}")
    else:
        print("[controller] no receipt found")

    store.close()


if __name__ == "__main__":
    main()
