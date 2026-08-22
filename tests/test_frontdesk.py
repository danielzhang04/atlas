import threading
import time
from hashlib import sha256

import pytest

from worker.contracts import JobState, Request, utc_timestamp
from worker.frontdesk import FrontDesk, FrontDeskOutcome
from worker.jobstore import IdempotencyConflict, InvalidTransition, JobStore
from worker.payload_codec import PayloadProtectionError
from worker.subscription_worker import WorkerHealth, WorkerHealthStatus, enqueue_nonblocking


def healthy():
    return WorkerHealth(WorkerHealthStatus.AVAILABLE, worker_id="local-test", checked_at=utc_timestamp())


class FakePayloadCodec:
    codec_id = "test-xor-v1"

    def protect(self, plaintext, *, entropy):
        key = sha256(entropy).digest()
        return bytes(value ^ key[index % len(key)] for index, value in enumerate(plaintext))

    def unprotect(self, ciphertext, *, entropy):
        return self.protect(ciphertext, entropy=entropy)


class SleepingWorker:
    def __init__(self):
        self.health_calls = 0
        self.enqueue_calls = 0

    def health(self):
        self.health_calls += 1
        time.sleep(30)
        return healthy()

    def enqueue(self, job):
        self.enqueue_calls += 1
        time.sleep(30)


class Fast:
    def __init__(self):
        self.calls = []

    def execute_fast(self, request):
        self.calls.append(request)
        return {"fast": True}


def slow_request(**kwargs):
    target = kwargs.pop("target", "draft")
    return Request("document.compose", target=target, durable_artifact=True, artifact="draft.md", **kwargs)


def test_fast_calendar_executes_once_without_job(tmp_path):
    fast = Fast()
    with JobStore(tmp_path / "jobs.sqlite") as store:
        result = FrontDesk(store=store, fast_executor=fast, worker_health=healthy()).submit(
            Request("calendar.create_event", target="event-1"),
            raw_utterance="Schedule a meeting tomorrow at 3pm",
        )
        assert isinstance(result, FrontDeskOutcome)
        assert result.status == "queued"
        assert result.lane.value == "fast"
        assert result.job_id is not None
        assert fast.calls == []
        assert store.get(result.job_id).lane.value == "fast"
        assert dict(store.get(result.job_id).request.parameters) == {
            "action": "create", "all_day": False, "calendar_id": "primary",
            "date_expression": "tomorrow", "duration_minutes": 30,
            "event_kind": "meeting", "schema": "calendar.fast.v1",
            "time_expression": "3PM", "timezone_policy": "atlas_local", "title": "Meeting",
        }


def test_blocking_fast_executor_is_never_called_and_fast_submit_is_bounded(tmp_path):
    class BlockingFast:
        def execute_fast(self, request):
            raise AssertionError("fast executor must be claimed by a runner, not voice submit")

    with JobStore(tmp_path / "fast.sqlite") as store:
        started = time.monotonic()
        result = FrontDesk(store=store, fast_executor=BlockingFast(), worker_health=healthy()).submit(
            Request("calendar.create_event", target="event-1"),
            raw_utterance="Schedule a meeting tomorrow at 3pm",
        )
        assert time.monotonic() - started < 1
        assert result.lane.value == "fast"


def test_fast_and_slow_runners_cannot_cross_claim(tmp_path):
    with JobStore(tmp_path / "lanes.sqlite") as store:
        fast_job = FrontDesk(store=store, worker_health=healthy()).submit(
            Request("calendar.create_event", target="event-1"),
            raw_utterance="Schedule a meeting tomorrow at 3pm",
        )
        slow_job = FrontDesk(store=store, worker_health=healthy()).submit(slow_request())
        assert store.claim_next("slow-runner").job_id == slow_job.job_id
        assert store.claim_next("fast-runner", lane="fast").job_id == fast_job.job_id
        assert store.claim_next("slow-runner", lane="fast") is None


def test_idempotency_key_binds_lane_as_well_as_request(tmp_path):
    from worker.jobstore import IdempotencyConflict

    request = Request("calendar.create_event", target="event-1")
    with JobStore(tmp_path / "lane-idempotency.sqlite") as store:
        store.create(request, lane="fast", idempotency_key="same-key")
        with pytest.raises(IdempotencyConflict):
            store.create(request, lane="slow", idempotency_key="same-key")


def test_slow_submit_is_durable_ack_and_does_not_call_worker(tmp_path):
    worker = SleepingWorker()
    with JobStore(tmp_path / "jobs.sqlite") as store:
        started = time.monotonic()
        result = FrontDesk(store=store, worker=worker, fast_executor=Fast(), worker_health=healthy()).submit(slow_request())
        assert time.monotonic() - started < 1
        assert isinstance(result, FrontDeskOutcome)
        assert result.status == "queued"
        assert store.get(result.job_id).request.target == "draft"
        assert worker.health_calls == 0
        assert worker.enqueue_calls == 0
        desk = FrontDesk(store=store, worker=worker, worker_health=healthy())
        desk.status(result.job_id)
        desk.cancel(result.job_id)
        assert worker.health_calls == 0
        assert worker.enqueue_calls == 0


def test_constructor_never_reads_compatibility_worker_or_executor_properties(tmp_path):
    class Exploding:
        @property
        def health_snapshot(self):
            raise AssertionError("worker property must not be inspected")

        @property
        def execute_fast(self):
            raise AssertionError("executor property must not be inspected")

    with JobStore(tmp_path / "properties.sqlite") as store:
        result = FrontDesk(store=store, worker=Exploding(), fast_executor=Exploding(),
                           worker_health=healthy()).submit(slow_request())
        assert result.status == "queued"


def test_async_only_legacy_enqueue_is_not_reported_as_accepted(tmp_path):
    class AsyncOnly:
        async def submit(self, job):
            raise AssertionError("async body must not be awaited")

    with JobStore(tmp_path / "jobs.sqlite") as store:
        job = store.create(slow_request())
        assert enqueue_nonblocking(AsyncOnly(), job) is False


def test_missing_or_unhealthy_health_snapshot_is_explicit_unavailable(tmp_path):
    with JobStore(tmp_path / "jobs.sqlite") as store:
        result = FrontDesk(store=store, worker=SleepingWorker(), fast_executor=Fast()).submit(slow_request())
        assert result.status == "unavailable"
        assert store.get(result.job_id).state is JobState.UNAVAILABLE


def test_stale_health_snapshot_fails_closed_without_calling_worker(tmp_path):
    worker = SleepingWorker()
    stale = WorkerHealth(WorkerHealthStatus.AVAILABLE, worker_id="local-test", checked_at=100.0)
    with JobStore(tmp_path / "jobs.sqlite") as store:
        result = FrontDesk(store=store, worker=worker, fast_executor=Fast(), worker_health=stale,
                           clock=lambda: 200.0, max_health_age=30.0).submit(slow_request())
        assert result.status == "unavailable"
        assert worker.health_calls == 0
        assert worker.enqueue_calls == 0


def test_unavailable_race_outcome_uses_authoritative_returned_state(tmp_path, monkeypatch):
    with JobStore(tmp_path / "unavailable-race.sqlite") as store:
        original = store.transition

        def interposed(job_id, state, **kwargs):
            if state is JobState.UNAVAILABLE:
                return original(job_id, JobState.RUNNING)
            return original(job_id, state, **kwargs)

        monkeypatch.setattr(store, "transition", interposed)
        result = FrontDesk(store=store, worker_health=WorkerHealth(
            WorkerHealthStatus.UNAVAILABLE, worker_id="local-test", checked_at=time.time(),
        )).submit(slow_request())
        assert result.status == "running"
        assert result.error_code is None
        assert store.get(result.job_id).state is JobState.RUNNING


@pytest.mark.parametrize("checked_at", [None, 200.0])
def test_missing_or_future_health_timestamp_fails_closed(tmp_path, checked_at):
    snapshot = WorkerHealth(WorkerHealthStatus.AVAILABLE, worker_id="local-test", checked_at=checked_at)
    with JobStore(tmp_path / "health-time.sqlite") as store:
        result = FrontDesk(store=store, worker_health=snapshot, fast_executor=Fast(),
                           clock=lambda: 100.0).submit(slow_request())
        assert result.status == "unavailable"


def test_raw_idempotency_replay_is_exact_and_does_not_duplicate_outbox(tmp_path):
    request = slow_request()
    path = tmp_path / "jobs.sqlite"
    with JobStore(path) as store:
        first = FrontDesk(store=store, worker_health=healthy(), fast_executor=Fast()).submit(
            request, idempotency_key="raw-secret-key"
        )
        second = FrontDesk(store=store, worker_health=healthy(), fast_executor=Fast()).submit(
            request, idempotency_key="raw-secret-key"
        )
        assert second.job_id == first.job_id
        assert second.replayed
        assert len(store.events(first.job_id)) == 1


def test_multi_instance_claim_is_single_delivery(tmp_path):
    path = tmp_path / "jobs.sqlite"
    first = JobStore(path)
    second = JobStore(path)
    try:
        job = first.create(slow_request())
        claims = []

        def claim(store, worker_id):
            claims.append(store.claim_next(worker_id))

        threads = [threading.Thread(target=claim, args=(first, "one")),
                   threading.Thread(target=claim, args=(second, "two"))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sorted(item is not None for item in claims) == [False, True]
        claimed = next(item for item in claims if item is not None)
        assert claimed.job_id == job.job_id
        assert claimed.lease_owner in {"one", "two"}
    finally:
        first.close()
        second.close()


def test_multiple_processes_with_same_worker_id_cannot_claim_concurrently(tmp_path):
    path = tmp_path / "single-worker-ceiling.sqlite"
    first = JobStore(path)
    second = JobStore(path)
    try:
        first.create(slow_request(target="one"))
        first.create(slow_request(target="two"))
        claims = []

        def claim(store):
            claims.append(store.claim_next("atlas-subscription"))

        threads = [threading.Thread(target=claim, args=(first,)), threading.Thread(target=claim, args=(second,))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sum(item is not None for item in claims) == 1
        assert len(first.claimed_jobs("atlas-subscription")) == 1
    finally:
        first.close()
        second.close()


def test_live_lease_is_not_recovered_and_owner_can_renew(tmp_path):
    clock = lambda: 100.0
    path = tmp_path / "lease.sqlite"
    store = JobStore(path, clock=clock)
    try:
        job = store.create(slow_request())
        claimed = store.claim_next("owner", lease_seconds=30)
        assert claimed.job_id == job.job_id
        assert store.recover_orphans() == []
        renewed = store.renew_lease(job.job_id, "owner", claimed.lease_token, lease_seconds=60)
        assert renewed.lease_until == 160.0
    finally:
        store.close()


def test_expiry_recovery_rechecks_lease_when_renewal_is_interposed(tmp_path, monkeypatch):
    now = [100.0]
    store = JobStore(tmp_path / "interposed.sqlite", clock=lambda: now[0])
    try:
        job = store.create(slow_request())
        claim = store.claim_next("owner", lease_seconds=10)
        now[0] = 200.0
        original = store.transition

        def interposed(job_id, state, **kwargs):
            if state is JobState.ORPHANED:
                with pytest.raises(InvalidTransition):
                    store.renew_lease(job_id, "owner", claim.lease_token)
            return original(job_id, state, **kwargs)

        monkeypatch.setattr(store, "transition", interposed)
        assert [item.job_id for item in store.recover_orphans()] == [job.job_id]
        assert store.get(job.job_id).state is JobState.ORPHANED
    finally:
        store.close()


def test_claimed_completion_is_owner_fenced_and_expiry_fenced(tmp_path):
    now = [100.0]
    store = JobStore(tmp_path / "fenced.sqlite", clock=lambda: now[0])
    try:
        job = store.create(slow_request())
        claim = store.claim_next("owner", lease_seconds=30)
        with pytest.raises(InvalidTransition):
            store.transition(job.job_id, JobState.SUCCEEDED)
        with pytest.raises(InvalidTransition):
            store.complete_success(job.job_id, "other", claim.lease_token)
        with pytest.raises(InvalidTransition):
            store.complete_success(job.job_id, "owner", "wrong-token-value-that-is-long-enough")
        assert store.complete_success(job.job_id, "owner", claim.lease_token).state is JobState.SUCCEEDED

        cancelled = store.create(slow_request(target="second"))
        cancel_claim = store.claim_next("owner", lease_seconds=30)
        store.request_cancel(cancelled.job_id)
        assert store.acknowledge_cancel(
            cancelled.job_id, "owner", cancel_claim.lease_token,
        ).state is JobState.CANCELLED
    finally:
        store.close()


def test_expired_cancel_request_is_atomically_settled_on_recovery(tmp_path):
    now = [100.0]
    store = JobStore(tmp_path / "cancel-recovery.sqlite", clock=lambda: now[0])
    try:
        job = store.create(slow_request())
        store.claim_next("owner", lease_seconds=10)
        store.request_cancel(job.job_id)
        now[0] = 200.0
        recovered = store.recover_orphans()
        assert [item.job_id for item in recovered] == [job.job_id]
        assert store.get(job.job_id).state is JobState.CANCELLED
        assert store.get(job.job_id).lease_owner is None
    finally:
        store.close()


def test_cancel_after_restart_prevents_claim_and_is_idempotent(tmp_path):
    path = tmp_path / "jobs.sqlite"
    first = JobStore(path)
    job = first.create(slow_request())
    first.close()
    second = JobStore(path)
    try:
        assert FrontDesk(store=second, worker_health=healthy()).cancel(job.job_id).state is JobState.CANCELLED
        assert FrontDesk(store=second, worker_health=healthy()).cancel(job.job_id).state is JobState.CANCELLED
        assert second.claim_next("after-restart") is None
    finally:
        second.close()


def test_embedded_secrets_are_absent_from_sqlite_and_worker_gets_sanitized_request(tmp_path):
    path = tmp_path / "secrets.sqlite"
    secret = "api_key=ultra-private-secret-value"
    request = slow_request(target=secret)
    with JobStore(path) as store:
        job = FrontDesk(store=store, worker_health=healthy()).submit(request).job_id
        claimed = store.claim_next("worker")
        assert claimed.job_id == job
        assert claimed.request.target == "[REDACTED]"
        assert secret not in path.read_bytes().decode("utf-8", errors="ignore")
        assert all(secret not in str(event.public_payload) for event in store.events(job))


def test_ordinary_prose_with_token_word_is_not_silently_mutated(tmp_path):
    with JobStore(tmp_path / "prose.sqlite") as store:
        job = FrontDesk(store=store, worker_health=healthy()).submit(
            slow_request(target="research token economics")
        )
        claimed = store.claim_next("worker")
        assert claimed.job_id == job.job_id
        assert claimed.request.target == "research token economics"


def test_embedded_json_api_key_is_redacted_but_token_prose_is_not(tmp_path):
    secret = '{"api_key":"ABCD1234567890"}'
    with JobStore(tmp_path / "json-secret.sqlite") as store:
        first = FrontDesk(store=store, worker_health=healthy()).submit(slow_request(target=secret))
        claimed = store.claim_next("worker")
        assert claimed.job_id == first.job_id
        assert claimed.request.target == "[REDACTED]"
        assert secret not in (tmp_path / "json-secret.sqlite").read_bytes().decode("utf-8", errors="ignore")
        second = FrontDesk(store=store, worker_health=healthy()).submit(
            slow_request(target="research token: economics")
        )
        claimed_second = store.claim_next("worker-two")
        assert claimed_second.job_id == second.job_id
        assert claimed_second.request.target == "research token: economics"


def test_frontdesk_owns_classification_and_slow_never_calls_fast(tmp_path):
    fast = Fast()
    with JobStore(tmp_path / "jobs.sqlite") as store:
        result = FrontDesk(store=store, worker_health=healthy(), fast_executor=fast).submit(slow_request())
        assert result.status == "queued"
        assert fast.calls == []


def test_protected_slow_payload_is_encrypted_replay_bound_and_claim_fenced(tmp_path):
    path = tmp_path / "protected.sqlite"
    codec = FakePayloadCodec()
    raw = "Prepare a private multi-step analysis for project Zephyr."
    with JobStore(path, payload_codec=codec) as store:
        desk = FrontDesk(store=store, worker_health=healthy())
        first = desk.submit(
            slow_request(), raw_utterance=raw, idempotency_key="same-request",
        )
        replay = desk.submit(
            slow_request(request_id=store.get(first.job_id).request.request_id),
            raw_utterance=raw, idempotency_key="same-request",
        )
        assert replay.job_id == first.job_id
        assert replay.replayed
        assert raw.encode() not in path.read_bytes()

        claim = store.claim_next("subscription-worker")
        assert claim.job_id == first.job_id
        assert claim.lease_token not in path.read_text(encoding="utf-8", errors="ignore")
        with pytest.raises(InvalidTransition):
            store.get_slow_payload(first.job_id, "other-worker", claim.lease_token)
        with pytest.raises(InvalidTransition):
            store.get_slow_payload(
                first.job_id, "subscription-worker", "wrong-token-value-that-is-long-enough",
            )
        payload = store.get_slow_payload(first.job_id, "subscription-worker", claim.lease_token)
        assert payload.instruction == raw
        assert payload.request_fingerprint == claim.request.fingerprint()


def test_protected_payload_idempotency_conflicts_on_changed_instruction(tmp_path):
    codec = FakePayloadCodec()
    with JobStore(tmp_path / "payload-conflict.sqlite", payload_codec=codec) as store:
        request = slow_request()
        desk = FrontDesk(store=store, worker_health=healthy())
        desk.submit(request, raw_utterance="First exact instruction", idempotency_key="one")
        with pytest.raises(IdempotencyConflict):
            desk.submit(request, raw_utterance="Changed exact instruction", idempotency_key="one")


def test_worker_cannot_forge_internal_completion_flag_without_claim_token(tmp_path):
    with JobStore(tmp_path / "completion-bypass.sqlite") as store:
        job = store.create(slow_request())
        store.claim_next("worker")
        with pytest.raises(InvalidTransition):
            store.transition(
                job.job_id, JobState.SUCCEEDED,
                _expected_state=JobState.RUNNING,
                _expected_owner="worker",
                _worker_completion=True,
                _require_unexpired=True,
            )


def test_protected_payload_access_fails_after_lease_expiry(tmp_path):
    now = [100.0]
    codec = FakePayloadCodec()
    with JobStore(tmp_path / "payload-expiry.sqlite", payload_codec=codec, clock=lambda: now[0]) as store:
        desk = FrontDesk(store=store, worker_health=WorkerHealth(
            WorkerHealthStatus.AVAILABLE, worker_id="worker", checked_at=100.0,
        ), clock=lambda: now[0])
        outcome = desk.submit(slow_request(), raw_utterance="Exact protected instruction")
        claim = store.claim_next("worker", lease_seconds=10)
        now[0] = 111.0
        with pytest.raises(InvalidTransition):
            store.get_slow_payload(outcome.job_id, "worker", claim.lease_token)


def test_payload_protection_failure_rolls_back_job_and_event(tmp_path):
    class FailingCodec(FakePayloadCodec):
        codec_id = "test-failing-v1"

        def protect(self, plaintext, *, entropy):
            raise PayloadProtectionError("injected failure")

    path = tmp_path / "protection-failure.sqlite"
    with JobStore(path, payload_codec=FailingCodec()) as store:
        desk = FrontDesk(store=store, worker_health=healthy())
        with pytest.raises(PayloadProtectionError):
            desk.submit(slow_request(), raw_utterance="Must never become plaintext")
        assert store._connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        assert store._connection.execute("SELECT COUNT(*) FROM job_events").fetchone()[0] == 0


def test_tampered_protected_payload_fails_integrity_check(tmp_path):
    codec = FakePayloadCodec()
    with JobStore(tmp_path / "payload-tamper.sqlite", payload_codec=codec) as store:
        outcome = FrontDesk(store=store, worker_health=healthy()).submit(
            slow_request(), raw_utterance="Integrity-bound instruction",
        )
        claim = store.claim_next("worker")
        row = store._connection.execute(
            "SELECT ciphertext FROM slow_payloads WHERE job_id = ?", (outcome.job_id,),
        ).fetchone()
        tampered = bytearray(row["ciphertext"])
        tampered[-1] ^= 1
        store._connection.execute(
            "UPDATE slow_payloads SET ciphertext = ? WHERE job_id = ?", (bytes(tampered), outcome.job_id),
        )
        store._connection.commit()
        with pytest.raises(PayloadProtectionError):
            store.get_slow_payload(outcome.job_id, "worker", claim.lease_token)


def test_standalone_import_guard():
    import inspect
    import worker.frontdesk as module

    source = inspect.getsource(module)
    assert "worker.app" not in source
    assert "worker.fastlane" not in source
    assert "kbmcp" not in source
