"""StudiOS Coordination — durable, policy-gated job scheduling."""

from .contracts import (
    JobState,
    AttemptState,
    JobRecord,
    AttemptRecord,
    LeaseRecord,
    AuditEvent,
)
from .store import Store
from .policy import PolicyGates, PolicyError, evaluate_gates, default_gates
from .scheduler import (
    WorkerCapability,
    JobRequest,
    RouteEstimate,
    SchedulingPolicy,
    route_job,
    ENKI_II_WORKER,
    refresh_remote_workers,
    dispatch_task_to_enkii,
)
from .adapters import get_adapter, list_kinds, register_adapter
from .transport import LoopbackTransport
from .remote_transport import RemoteTransport, TransportError, remote_transport
from .receipts import build_receipt, write_receipt, read_receipt
from .worker import WorkerLoop

__all__ = [
    # contracts
    "JobState", "AttemptState", "JobRecord", "AttemptRecord",
    "LeaseRecord", "AuditEvent",
    # store
    "Store",
    # policy
    "PolicyGates", "PolicyError", "evaluate_gates", "default_gates",
    # scheduler
    "WorkerCapability", "JobRequest", "RouteEstimate",
    "SchedulingPolicy", "route_job",
    # adapters
    "get_adapter", "list_kinds", "register_adapter",
    # transport
    "LoopbackTransport", "RemoteTransport", "TransportError", "remote_transport",
    # receipts
    "build_receipt", "write_receipt", "read_receipt",
    # worker
    "WorkerLoop",
]
