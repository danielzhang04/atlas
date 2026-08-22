import pytest

from worker.subscription_worker import (
    SubscriptionWorker,
    SubscriptionWorkerUnavailable,
    WorkerHealth,
    WorkerHealthStatus,
    require_healthy,
)


class HealthyWorker:
    def health(self):
        return WorkerHealth(WorkerHealthStatus.AVAILABLE, worker_id="test")

    def submit(self, job):
        raise AssertionError("execution is outside this seam test")


class BrokenWorker:
    def health(self):
        return WorkerHealth(WorkerHealthStatus.UNAVAILABLE, "not started")

    def submit(self, job):
        raise AssertionError("unavailable worker must not receive work")


class ExplodingWorker:
    def health(self):
        raise RuntimeError("private api_key=should-not-escape")


def test_worker_is_only_a_typed_seam_and_health_is_explicit():
    worker = HealthyWorker()
    assert isinstance(worker, SubscriptionWorker)
    assert require_healthy(worker).healthy
    assert require_healthy(type("StringStatus", (), {"health": lambda self: WorkerHealth("available")})()).healthy


def test_missing_or_unhealthy_worker_is_unavailable_without_substitution():
    with pytest.raises(SubscriptionWorkerUnavailable) as error:
        require_healthy(BrokenWorker())
    assert error.value.health.status is WorkerHealthStatus.UNAVAILABLE
    with pytest.raises(SubscriptionWorkerUnavailable):
        require_healthy(None)


def test_health_exception_becomes_sanitized_unavailable_code():
    with pytest.raises(SubscriptionWorkerUnavailable) as error:
        require_healthy(ExplodingWorker())
    assert error.value.health.status is WorkerHealthStatus.UNAVAILABLE
    assert error.value.health.reason == "health_check_failed"
    assert "should-not-escape" not in str(error.value)
