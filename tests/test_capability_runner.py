from hashlib import sha256
from pathlib import Path

import pytest

from worker import runtime
from worker.capability_runner import (
    CapabilityDispatchError,
    FastCapabilityWorker,
    SharedCapabilityBroker,
    TypedCapabilityCall,
)
from worker.connectors import BrowserConnector, GoogleBrokerConnector
from worker.contracts import JobState, Request, utc_timestamp
from worker.frontdesk import FrontDesk
from worker.jobstore import JobStore
from worker.subscription_worker import WorkerHealth, WorkerHealthStatus


ROOT = Path(__file__).resolve().parents[1]


class FakePayloadCodec:
    codec_id = "test-xor-v1"

    def protect(self, plaintext, *, entropy):
        key = sha256(entropy).digest()
        return bytes(value ^ key[index % len(key)] for index, value in enumerate(plaintext))

    def unprotect(self, ciphertext, *, entropy):
        return self.protect(ciphertext, entropy=entropy)


class GoogleBroker:
    def __init__(self):
        self.calls = []

    def __call__(self, operation, parameters, *, binding):
        self.calls.append((operation, parameters, binding))
        if operation == "connection.bind":
            return {"binding": "account-generation-1", "access_token": "must-be-filtered"}
        if operation == "calendar.list":
            return {"items": [{"id": "event-1", "summary": "Review"}],
                    "authorization": "must-be-filtered"}
        return {"ok": True, "operation": operation}


def _services(google_broker=None, **cfg):
    connector = GoogleBrokerConnector(google_broker) if google_broker is not None else None
    return runtime.build_runtime(ROOT, cfg, google_connector=connector)


def _healthy():
    return WorkerHealth(WorkerHealthStatus.AVAILABLE, worker_id="fast-test",
                        checked_at=utc_timestamp())


def test_fast_calendar_read_uses_shared_broker_and_external_google_client(tmp_path):
    external = GoogleBroker()
    services = _services(external)
    broker = SharedCapabilityBroker(services)
    with JobStore(tmp_path / "fast-read.sqlite", payload_codec=FakePayloadCodec()) as store:
        outcome = FrontDesk(store=store, worker_health=_healthy()).submit(
            Request("calendar.read_event", target="calendar"),
            raw_utterance="What's on my calendar 2026-08-22?",
        )
        assert FastCapabilityWorker(store, broker).run_once() is JobState.SUCCEEDED
        completed = store.get(outcome.job_id)
        assert completed.public_payload["code"] == "action_completed"
        assert [call[0] for call in external.calls] == ["connection.bind", "calendar.list"]
        assert external.calls[-1][2] == "account-generation-1"
        snapshot = services.broker.get(completed.public_payload["proposal_id"])
        assert snapshot.status == "succeeded"
        assert "authorization" not in snapshot.receipt
        assert "access_token" not in repr(snapshot)


def test_fast_calendar_create_stops_at_hash_bound_confirmation(tmp_path):
    external = GoogleBroker()
    services = _services(external)
    broker = SharedCapabilityBroker(services)
    with JobStore(tmp_path / "fast-create.sqlite", payload_codec=FakePayloadCodec()) as store:
        outcome = FrontDesk(store=store, worker_health=_healthy()).submit(
            Request("calendar.create_event", target="event"),
            raw_utterance="Schedule a meeting 2026-08-22 at 3 PM",
        )
        assert FastCapabilityWorker(store, broker).run_once() is JobState.SUCCEEDED
        completed = store.get(outcome.job_id)
        assert completed.public_payload["code"] == "action_prepared"
        snapshot = services.broker.get(completed.public_payload["proposal_id"])
        assert snapshot.status == "proposed"
        assert all(call[0] != "calendar.create" for call in external.calls)

        services.broker.confirm(snapshot.proposal_id, channel="ui",
                                parameters_hash=snapshot.parameters_hash)
        terminal = services.broker.execute(snapshot.proposal_id,
                                           parameters_hash=snapshot.parameters_hash)
        assert terminal.status == "succeeded"
        assert external.calls[-1][0] == "calendar.create"
        assert external.calls[-1][2] == "account-generation-1"


def test_slow_typed_calls_use_the_same_broker_but_cannot_auto_confirm_mutations():
    external = GoogleBroker()
    services = _services(external)
    shared = SharedCapabilityBroker(services)
    result = shared.dispatch(TypedCapabilityCall(
        "google.calendar.create",
        {"calendar_id": "primary", "event": {
            "summary": "Slow planned review",
            "start": {"date": "2026-08-22"}, "end": {"date": "2026-08-23"},
        }},
        "slow-job-1",
    ))
    assert result.status == "proposed"
    assert all(call[0] != "calendar.create" for call in external.calls)


def test_browser_reads_execute_and_interactions_remain_proposals():
    calls = []

    def transport(**kwargs):
        calls.append(kwargs)
        if kwargs["method"] == "GET":
            return {"status": 200, "body": {"tab_id": "tab-1", "document_id": "doc-1",
                    "origin": "https://example.com", "visible_text": "untrusted page"}}
        return {"status": 200, "body": {"ok": True}}

    services = _services()
    connector = BrowserConnector("http://127.0.0.1:4370", transport,
                                 allowed_origins={"https://example.com"})
    services.browser = runtime.BrowserActions(connector, services.broker)
    shared = SharedCapabilityBroker(services)

    inspected = shared.dispatch(TypedCapabilityCall(
        "browser.inspect", {"tab_id": "tab-1"}, "inspect-1"))
    assert inspected.status == "succeeded" and len(calls) == 1

    prepared = shared.dispatch(TypedCapabilityCall(
        "browser.type", {"tab_id": "tab-1", "origin": "https://example.com",
                         "target": "#search", "value": "safe query"}, "type-1"))
    assert prepared.status == "proposed"
    assert len(calls) == 2  # second GET attests origin; no POST interaction before confirmation
    assert all(call["method"] != "POST" for call in calls)


def test_read_observation_returns_bounded_private_content_and_filters_secret_fields():
    external = GoogleBroker()
    shared = SharedCapabilityBroker(_services(external))
    observed = shared.dispatch_observed(TypedCapabilityCall(
        "google.calendar.read",
        {"calendar_id": "primary", "max_results": 10},
        "observed-calendar-1",
    ))
    assert observed.content == {"items": [{"id": "event-1", "summary": "Review"}]}
    assert observed.truncated is False
    assert len(observed.content_digest) == 64
    assert "authorization" not in repr(observed)
    first_copy = observed.content
    first_copy["items"][0]["summary"] = "tampered"
    assert observed.content["items"][0]["summary"] == "Review"


def test_read_observation_truncates_large_content_and_never_observes_mutations():
    class LargeGoogleBroker(GoogleBroker):
        def __call__(self, operation, parameters, *, binding):
            if operation == "connection.bind":
                return {"binding": "account-generation-1"}
            if operation == "docs.read":
                return {"text": "x" * 100_000, "access_token": "must-not-cross"}
            return super().__call__(operation, parameters, binding=binding)

    shared = SharedCapabilityBroker(_services(LargeGoogleBroker()))
    observed = shared.dispatch_observed(TypedCapabilityCall(
        "google.docs.read", {"document_id": "doc-1"}, "observed-doc-1",
    ))
    assert observed.truncated is True
    assert "must-not-cross" not in repr(observed)
    assert len(str(observed.content)) < 33_000

    with pytest.raises(CapabilityDispatchError, match="not approved"):
        shared.dispatch_observed(TypedCapabilityCall(
            "google.calendar.create",
            {"calendar_id": "primary", "event": {
                "summary": "No", "start": {"date": "2026-08-22"},
                "end": {"date": "2026-08-23"},
            }},
            "observed-mutation-1",
        ))


def test_unregistered_or_schema_smuggled_capability_calls_fail_closed():
    shared = SharedCapabilityBroker(_services())
    with pytest.raises(CapabilityDispatchError, match="not registered"):
        shared.dispatch(TypedCapabilityCall("shell.execute", {"command": "whoami"}, "bad-1"))
    with pytest.raises(ValueError, match="reviewed schema"):
        shared.dispatch(TypedCapabilityCall(
            "desktop.open", {"app_id": "chrome", "command": "powershell"}, "bad-2"))


def test_runtime_source_has_no_environment_bearer_binding():
    source = (ROOT / "worker" / "runtime.py").read_text(encoding="utf-8")
    config = (ROOT / "config" / "atlas.yaml").read_text(encoding="utf-8")
    assert "google_oauth_token_env" not in source
    assert "GOOGLE_ACCESS_TOKEN" not in source
    assert "google_oauth_token_env" not in config
    assert "GOOGLE_ACCESS_TOKEN" not in config
