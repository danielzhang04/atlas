"""Standalone voice-facing admission boundary for Atlas work.

FrontDesk is deliberately a durable-outbox adapter. It owns classification, so callers cannot
forge a fast route; it persists both lanes before acknowledging them; and it never calls
arbitrary worker or executor code on the voice-facing submit, status, or cancel path.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Callable

from .contracts import Job, JobClaim, JobState, Lane, Request, SlowTaskPayload, utc_timestamp
from .jobstore import InvalidTransition, JobStore
from .routing_policy import RoutingPolicy, bind_atomic_calendar_request
from .subscription_worker import SubscriptionWorker, WorkerHealth, WorkerHealthStatus


@dataclass(frozen=True, slots=True)
class FrontDeskOutcome:
    """Bounded admission acknowledgement for either durable lane."""

    status: str
    lane: Lane = Lane.SLOW
    job_id: str | None = None
    summary: str = ""
    error_code: str | None = None
    replayed: bool = False

    @property
    def accepted(self) -> bool:
        return self.status == JobState.QUEUED.value

    @property
    def state(self) -> JobState | None:
        try:
            return JobState(self.status)
        except ValueError:
            return None

    @property
    def acknowledgment(self) -> str:
        return self.summary


class FrontDesk:
    """Admit typed requests into a lane-aware durable SQLite outbox.

    ``worker_health`` is an externally maintained, bounded snapshot. It is intentionally not
    obtained by invoking a worker method here: a health probe can hang just like enqueue, which
    would violate the voice boundary. A missing/stale snapshot fails closed as unavailable.
    """

    def __init__(self, *, store: JobStore | None = None, worker: SubscriptionWorker | None = None,
                 fast_executor: Any | None = None, policy: RoutingPolicy | None = None,
                 job_store: JobStore | None = None, subscription_worker: SubscriptionWorker | None = None,
                 worker_health: WorkerHealth | None = None,
                 health_snapshot: WorkerHealth | None = None,
                 health_provider: Callable[[], WorkerHealth] | None = None,
                 clock: Callable[[], float] = utc_timestamp,
                 max_health_age: float = 30.0) -> None:
        if store is None:
            store = job_store
        if worker is None:
            worker = subscription_worker
        if not isinstance(store, JobStore):
            raise TypeError("front desk requires a JobStore")
        snapshot = worker_health if worker_health is not None else health_snapshot
        if snapshot is not None and not isinstance(snapshot, WorkerHealth):
            raise TypeError("worker_health must be a WorkerHealth snapshot")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if health_provider is not None and not callable(health_provider):
            raise TypeError("health_provider must be callable")
        if not isinstance(max_health_age, (int, float)) or not math.isfinite(max_health_age) or max_health_age <= 0:
            raise ValueError("max_health_age must be positive and finite")
        self.store = store
        # Compatibility arguments are intentionally ignored. SQLite claims are the only
        # worker/executor seam; no arbitrary object is inspected or invoked here.
        self.policy = policy or RoutingPolicy()
        self.worker_health = snapshot or WorkerHealth(
            WorkerHealthStatus.UNAVAILABLE, "health_snapshot_missing",
        )
        self._health_provider = health_provider
        self._clock = clock
        self._max_health_age = float(max_health_age)

    def submit(self, request: Request, *, idempotency_key: str | None = None,
               public_payload: dict[str, Any] | None = None,
               raw_utterance: str | None = None) -> Any:
        """Classify and admit one already-typed bounded request."""
        if not isinstance(request, Request):
            raise TypeError("front desk requires a bounded Request")
        request = bind_atomic_calendar_request(request, raw_utterance)
        decision = self.policy.classify(request, raw_utterance=raw_utterance)

        slow_payload = None
        if decision.lane is Lane.SLOW and raw_utterance is not None:
            slow_payload = SlowTaskPayload(
                instruction=raw_utterance,
                request_fingerprint=request.fingerprint(),
                submitted_at=float(self._clock()),
            )
        job, replayed = self.store.create_with_replay(
            request, idempotency_key=idempotency_key, public_payload=public_payload,
            lane=decision.lane, slow_payload=slow_payload,
        )
        if job.state is not JobState.QUEUED:
            return self._outcome(job, replayed=replayed)
        if decision.lane is Lane.SLOW and not self._health_is_fresh():
            job = self._mark_unavailable(job.job_id)
            return self._outcome(job)
        # The queued row is the durable acceptance boundary. The separate worker process later
        # calls JobStore.claim_next; no arbitrary worker callback runs on this path.
        return self._outcome(job, replayed=replayed)

    handle = submit
    handle_request = submit
    dispatch = submit
    accept = submit

    def status(self, job_id: str) -> Job:
        return self.store.get(job_id)

    get_status = status

    def cancel(self, job_id: str) -> Job:
        """Cancel durably and idempotently; a worker polling later cannot claim queued work."""
        return self.store.request_cancel(job_id)

    cancel_job = cancel

    def claim_next(self, worker_id: str, *, lane: Lane | str = Lane.SLOW,
                   lease_seconds: float = 30.0) -> JobClaim | None:
        """Worker-side durable claim seam; competing processes are serialized by SQLite."""
        return self.store.claim_next(worker_id, lane=lane, lease_seconds=lease_seconds)

    def _mark_unavailable(self, job_id: str) -> Job:
        try:
            return self.store.transition(
                job_id, JobState.UNAVAILABLE,
                public_payload={"code": "subscription_worker_unavailable"},
            )
        except InvalidTransition:
            return self.store.get(job_id)

    def _health_is_fresh(self) -> bool:
        snapshot = self.worker_health
        if self._health_provider is not None:
            try:
                snapshot = self._health_provider()
            except Exception:
                return False
            if not isinstance(snapshot, WorkerHealth):
                return False
        if snapshot.status is not WorkerHealthStatus.AVAILABLE:
            return False
        checked_at = snapshot.checked_at
        if checked_at is None:
            return False
        try:
            age = float(self._clock()) - float(checked_at)
        except (TypeError, ValueError, OverflowError):
            return False
        return math.isfinite(age) and 0 <= age <= self._max_health_age

    def _outcome(self, job: Job, *, replayed: bool = False) -> FrontDeskOutcome:
        if job.state is JobState.UNAVAILABLE:
            return FrontDeskOutcome(
                status=JobState.UNAVAILABLE.value, lane=job.lane, job_id=job.job_id,
                summary="Work is unavailable right now.", error_code="subscription_worker_unavailable",
                replayed=replayed,
            )
        return FrontDeskOutcome(
            status=job.state.value, lane=job.lane, job_id=job.job_id,
            summary="Work accepted and queued." if job.state is JobState.QUEUED else "Work is no longer queued.",
            replayed=replayed,
        )


__all__ = ["FrontDesk", "FrontDeskOutcome"]
