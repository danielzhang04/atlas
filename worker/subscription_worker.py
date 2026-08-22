"""The local subscription-worker seam.

This module intentionally contains no worker implementation.  A process-owned worker can satisfy
the protocol later; callers must treat an absent or unhealthy worker as unavailable.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
import re
import inspect
from typing import Any, Protocol, runtime_checkable

from .contracts import Job


class WorkerHealthStatus(str, Enum):
    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class WorkerHealth:
    status: WorkerHealthStatus
    reason: str = ""
    worker_id: str = "local-subscription"
    checked_at: float | None = None

    def __post_init__(self) -> None:
        try:
            status = self.status if isinstance(self.status, WorkerHealthStatus) else WorkerHealthStatus(self.status)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid worker health status") from exc
        object.__setattr__(self, "status", status)
        if not isinstance(self.reason, str):
            raise TypeError("worker health reason must be a string")
        if len(self.reason) > 64:
            raise ValueError("worker health reason is too long")
        if self.reason and not re.fullmatch(r"[A-Za-z0-9_. -]+", self.reason):
            raise ValueError("worker health reason must be a safe code")
        if not isinstance(self.worker_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", self.worker_id):
            raise ValueError("invalid worker id")
        if self.checked_at is not None and (not isinstance(self.checked_at, (int, float)) or not math.isfinite(self.checked_at)):
            raise ValueError("invalid health timestamp")

    @property
    def healthy(self) -> bool:
        return self.status is WorkerHealthStatus.AVAILABLE


class SubscriptionWorkerUnavailable(RuntimeError):
    """Raised when the required local worker is absent or unhealthy."""

    def __init__(self, health: WorkerHealth) -> None:
        self.health = health
        detail = health.reason or health.status.value
        super().__init__(f"subscription worker unavailable: {detail}")


UnavailableWorkerError = SubscriptionWorkerUnavailable
WorkerUnavailable = SubscriptionWorkerUnavailable
HealthStatus = WorkerHealthStatus


@runtime_checkable
class SubscriptionWorker(Protocol):
    """Legacy worker-process shape; FrontDesk does not invoke this protocol.

    The durable production admission boundary is ``JobStore.claim_next``.  Implementations that
    still expose ``submit`` must treat it as an acceptance-only operation; FrontDesk never calls
    it, so an arbitrary worker cannot block voice submission.
    """

    def health(self) -> WorkerHealth:
        ...

    def submit(self, job: Job) -> Any:
        ...


def enqueue_nonblocking(worker: SubscriptionWorker, job: Job) -> bool:
    """Legacy/test-only seam validator; return true only for synchronous acceptance.

    FrontDesk never invokes this seam: SQLite is the durable outbox. Keeping this helper honest
    protects older callers—an async-shaped method is closed and rejected, never falsely reported
    as accepted merely because its coroutine object was created.
    """
    method = getattr(worker, "enqueue", None)
    if not callable(method):
        method = getattr(worker, "submit", None)
    if not callable(method):
        raise TypeError("subscription worker has no enqueue seam")
    returned = method(job)
    if inspect.isawaitable(returned):
        close = getattr(returned, "close", None)
        if callable(close):
            close()
        return False
    return returned is not False


def require_healthy(worker: SubscriptionWorker) -> WorkerHealth:
    """Validate the seam without selecting or constructing another execution route."""
    if worker is None:
        raise SubscriptionWorkerUnavailable(
            WorkerHealth(WorkerHealthStatus.UNAVAILABLE, "worker not configured")
        )
    try:
        health = worker.health()
    except Exception as exc:
        # Never expose exception text from the worker boundary; it may contain private context.
        raise SubscriptionWorkerUnavailable(
            WorkerHealth(WorkerHealthStatus.UNAVAILABLE, "health_check_failed")
        ) from None
    try:
        if not isinstance(health, WorkerHealth):
            raise TypeError("worker health must be WorkerHealth")
    except (TypeError, ValueError):
        raise SubscriptionWorkerUnavailable(
            WorkerHealth(WorkerHealthStatus.UNAVAILABLE, "invalid_health")
        ) from None
    if not health.healthy:
        raise SubscriptionWorkerUnavailable(health)
    return health


check_health = require_healthy


__all__ = ["WorkerHealthStatus", "WorkerHealth", "SubscriptionWorker",
           "SubscriptionWorkerUnavailable", "UnavailableWorkerError", "require_healthy", "check_health",
           "enqueue_nonblocking"]
