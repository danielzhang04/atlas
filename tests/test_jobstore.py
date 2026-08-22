import pytest

from worker.contracts import JobState, Request
from worker.jobstore import IdempotencyConflict, InvalidTransition, JobStore


def test_sqlite_store_has_strict_transitions_monotonic_events_and_redacted_payload(tmp_path):
    path = tmp_path / "safe;local database.sqlite"
    raw_key = "raw-idempotency-secret"
    with JobStore(path) as store:
        job = store.create(
            Request("calendar.create_event", target="event-1"),
            idempotency_key=raw_key,
            public_payload={"summary": "safe", "token": "must-not-persist", "api_key": "api-key-secret",
                             "access-token": "access-token-secret", "body": "private"},
        )
        assert store.create(job.request, idempotency_key=raw_key).job_id == job.job_id
        assert not hasattr(job, "idempotency_key")
        assert job.idempotency_digest and job.idempotency_digest != raw_key
        # The original raw key is the only replay input; the returned digest is storage metadata.
        assert store.create(job.request, idempotency_key=raw_key).job_id == job.job_id
        assert store.create(job.request, idempotency_key=job.idempotency_digest).job_id != job.job_id
        assert "must-not-persist" not in str(store.get(job.job_id).public_payload)
        raw_db = path.read_text(encoding="utf-8", errors="ignore")
        assert "must-not-persist" not in raw_db
        assert "api-key-secret" not in raw_db
        assert "access-token-secret" not in raw_db
        assert raw_key not in raw_db
        assert store.transition(job.job_id, JobState.RUNNING).state is JobState.RUNNING
        assert store.transition(job.job_id, JobState.SUCCEEDED, public_payload={"result": "done"}).state is JobState.SUCCEEDED
        with pytest.raises(InvalidTransition):
            store.transition(job.job_id, JobState.RUNNING)
        events = store.events(job.job_id)
        assert [event.sequence for event in events] == list(range(1, len(events) + 1))
        assert all(events[i].sequence < events[i + 1].sequence for i in range(len(events) - 1))


def test_idempotency_conflict_is_rejected(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite")
    try:
        store.create(Request("calendar.create_event", target="event-1"), idempotency_key="one")
        with pytest.raises(IdempotencyConflict):
            store.create(Request("calendar.create_event", target="event-2"), idempotency_key="one")
    finally:
        store.close()


def test_oversized_sensitive_key_is_redacted_before_key_truncation_and_bounded(tmp_path):
    path = tmp_path / "adversarial.sqlite"
    oversized_key = ("b" * 600) + "api_key"
    secret = "oversized-key-secret"
    store = JobStore(path)
    try:
        store.create(Request("calendar.create_event", target="event-1"),
                     public_payload={oversized_key: secret})
    finally:
        store.close()
    raw_db = path.read_bytes()
    assert secret.encode() not in raw_db
    assert oversized_key.encode() not in raw_db
    assert b"b" * 513 not in raw_db


def test_cancellation_is_idempotent_and_restart_recovers_running_orphans(tmp_path):
    path = tmp_path / "restart.sqlite"
    first = JobStore(path)
    queued = first.create(Request("calendar.create_event", target="event-1"))
    assert first.cancel(queued.job_id).state is JobState.CANCELLED
    running = first.create(Request("calendar.create_event", target="event-2"))
    first.transition(running.job_id, JobState.RUNNING)
    first.close()

    second = JobStore(path)
    recovered = second.recover_orphans()
    assert [job.job_id for job in recovered] == [running.job_id]
    assert second.get(running.job_id).state is JobState.ORPHANED
    assert second.cancel(queued.job_id).state is JobState.CANCELLED
    second.close()


def test_repeated_cancellation_is_atomic_and_idempotent(tmp_path):
    import threading
    store = JobStore(tmp_path / "cancel.sqlite")
    job = store.create(Request("calendar.create_event", target="event-1"))
    outcomes = []

    def cancel():
        outcomes.append(store.cancel(job.job_id).state)

    threads = [threading.Thread(target=cancel) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert all(state is JobState.CANCELLED for state in outcomes)
    assert store.get(job.job_id).state is JobState.CANCELLED
    assert len(store.events(job.job_id)) == 2
    store.close()


def test_recovery_continues_when_a_competing_transition_wins(tmp_path, monkeypatch):
    store = JobStore(tmp_path / "recovery.sqlite")
    job = store.create(Request("calendar.create_event", target="event-1"))
    store.transition(job.job_id, JobState.RUNNING)
    original = store.transition

    def competing_transition(job_id, state, **kwargs):
        if state is JobState.ORPHANED:
            original(job_id, JobState.SUCCEEDED)
            raise InvalidTransition("competing owner won")
        return original(job_id, state, **kwargs)

    monkeypatch.setattr(store, "transition", competing_transition)
    assert store.recover_orphans() == []
    assert store.get(job.job_id).state is JobState.SUCCEEDED
    store.close()


def test_recent_jobs_is_bounded_newest_first_and_validates_limit(tmp_path):
    ticks = iter([1.0, 2.0, 3.0])
    store = JobStore(tmp_path / "recent.sqlite", clock=lambda: next(ticks))
    try:
        first = store.create(Request("document.compose", target="first"))
        second = store.create(Request("document.compose", target="second"))
        store.transition(first.job_id, JobState.RUNNING)

        assert [job.job_id for job in store.recent_jobs(1)] == [first.job_id]
        assert [job.job_id for job in store.recent_jobs(2)] == [first.job_id, second.job_id]
        for invalid in (0, 101, True, 1.5):
            with pytest.raises(ValueError):
                store.recent_jobs(invalid)
    finally:
        store.close()
