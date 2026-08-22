import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from worker.broker_ipc import BrokerIpcServer
from worker.capability_runner import BrokeredReadObservation


JOB_ID = "3f75564b-cad1-4b9e-9e79-4f15013b43c2"
TOKEN = "t" * 43


class RecordingDispatcher:
    def __init__(self):
        self.calls = []

    def dispatch_observed(self, call):
        self.calls.append(call)
        content_json = json.dumps({"items": [{"title": "Evidence"}]},
                                  sort_keys=True, separators=(",", ":"))
        from hashlib import sha256
        return BrokeredReadObservation(
            call.capability_id, "proposal-1", "a" * 64, content_json,
            sha256(content_json.encode()).hexdigest(), False,
        )


def post(endpoint, body, *, token=TOKEN):
    request = Request(
        endpoint.url, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urlopen(request, timeout=2) as response:
        return response.status, json.loads(response.read())


def test_authenticated_job_bound_read_returns_private_observation_without_token_echo():
    dispatcher = RecordingDispatcher()
    server = BrokerIpcServer(
        dispatcher, job_id=JOB_ID, token_factory=lambda: TOKEN, clock=lambda: 100.0,
        allowed_capabilities=frozenset({"google.docs.read"}),
    )
    endpoint = server.start()
    try:
        status, value = post(endpoint, {
            "job_id": JOB_ID, "capability_id": "google.docs.read",
            "parameters": {"document_id": "doc-1"},
        })
        assert status == 200
        assert value["job_id"] == JOB_ID and value["request_id"] == 1
        assert value["content"]["items"][0]["title"] == "Evidence"
        assert TOKEN not in json.dumps(value) and TOKEN not in repr(endpoint)
        assert dispatcher.calls[0].idempotency_key == f"ipc:{JOB_ID}:1"
        assert server.receipts[0].proposal_id == value["proposal_id"]
        assert not hasattr(server.receipts[0], "content")
    finally:
        server.close()


def test_wrong_token_wrong_job_and_mutation_fail_before_dispatch():
    dispatcher = RecordingDispatcher()
    server = BrokerIpcServer(
        dispatcher, job_id=JOB_ID, token_factory=lambda: TOKEN,
        allowed_capabilities=frozenset({"google.docs.read"}),
    )
    endpoint = server.start()
    try:
        with pytest.raises(HTTPError) as wrong_token:
            post(endpoint, {"job_id": JOB_ID, "capability_id": "google.docs.read",
                            "parameters": {"document_id": "doc-1"}}, token="x" * 43)
        assert wrong_token.value.code == 403

        with pytest.raises(HTTPError) as wrong_job:
            post(endpoint, {"job_id": "149d8c94-b0f5-4d5a-827f-61db05db3be4",
                            "capability_id": "google.docs.read",
                            "parameters": {"document_id": "doc-1"}})
        assert wrong_job.value.code == 403

        with pytest.raises(HTTPError) as mutation:
            post(endpoint, {"job_id": JOB_ID, "capability_id": "google.calendar.create",
                            "parameters": {"event": {}}})
        assert mutation.value.code == 400
        with pytest.raises(HTTPError) as out_of_scope_read:
            post(endpoint, {"job_id": JOB_ID, "capability_id": "google.drive.read",
                            "parameters": {"file_id": "other"}})
        assert out_of_scope_read.value.code == 400
        assert dispatcher.calls == []
    finally:
        server.close()


def test_expiry_and_request_budget_stop_the_loop():
    now = [100.0]
    dispatcher = RecordingDispatcher()
    server = BrokerIpcServer(
        dispatcher, job_id=JOB_ID, token_factory=lambda: TOKEN, clock=lambda: now[0],
        ttl_seconds=10, max_requests=1,
        allowed_capabilities=frozenset({"google.docs.read"}),
    )
    endpoint = server.start()
    body = {"job_id": JOB_ID, "capability_id": "google.docs.read",
            "parameters": {"document_id": "doc-1"}}
    try:
        assert post(endpoint, body)[0] == 200
        with pytest.raises(HTTPError) as exhausted:
            post(endpoint, body)
        assert exhausted.value.code == 429
        now[0] = 111.0
        with pytest.raises(HTTPError) as expired:
            post(endpoint, body)
        assert expired.value.code == 410
    finally:
        server.close()


def test_malformed_or_oversized_requests_fail_closed():
    dispatcher = RecordingDispatcher()
    server = BrokerIpcServer(
        dispatcher, job_id=JOB_ID, token_factory=lambda: TOKEN,
        allowed_capabilities=frozenset({"google.docs.read"}),
    )
    endpoint = server.start()
    try:
        request = Request(
            endpoint.url, data=b"not-json", method="POST",
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        )
        with pytest.raises(HTTPError) as malformed:
            urlopen(request, timeout=2)
        assert malformed.value.code == 400

        request = Request(
            endpoint.url, data=b"x" * 8_193, method="POST",
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        )
        with pytest.raises(HTTPError) as oversized:
            urlopen(request, timeout=2)
        assert oversized.value.code == 413
        assert dispatcher.calls == []
    finally:
        server.close()
