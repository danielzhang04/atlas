"""Local read-only /state HTTP surface (Task 5, design §3).

Tests drive a REAL aiohttp AppRunner + TCPSite on an ephemeral port (port 0 — NEVER the 4360
desk default, so a running desk worker can't collide with the suite) and use aiohttp's own
ClientSession as the HTTP client. No new test deps and no pytest-asyncio: each test drives one
`asyncio.run()` coroutine (the test_toolreg.py precedent).

Verified against the INSTALLED aiohttp 3.14.1 in atlas/.venv (see stateserver.py header for the
exact symbol/line references).
"""
import asyncio
import json
from hashlib import sha256
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import aiohttp

from worker import stateserver
from worker.actionauth import HEADER, PairingAuthorizer
from worker.contracts import ProtectedTaskResult
from worker.state import StatePublisher
from worker.state import ACTING, SPEAKING, THINKING
from worker.subscription_worker import WorkerHealth, WorkerHealthStatus


def _dt(sec: int) -> datetime:
    return datetime(2026, 7, 20, 12, 0, sec, tzinfo=timezone.utc)


async def _get(server, path: str = "/state", headers=None):
    """GET against the ephemeral port; returns (status, lowercased-headers, raw-text)."""
    url = f"http://127.0.0.1:{server.port}{path}"
    async with aiohttp.ClientSession() as sess:
        async with sess.get(url, headers=headers or {}) as resp:
            text = await resp.text()
            headers = {k.lower(): v for k, v in resp.headers.items()}
            return resp.status, headers, text


async def _post(server, path: str, body, headers=None):
    url = f"http://127.0.0.1:{server.port}{path}"
    async with aiohttp.ClientSession() as sess:
        async with sess.post(url, data=body, headers=headers or {}) as resp:
            text = await resp.text()
            response_headers = {k.lower(): v for k, v in resp.headers.items()}
            return resp.status, response_headers, text


def test_state_endpoint_returns_200_and_full_schema():
    async def scenario():
        pub = StatePublisher(clock=lambda: _dt(0), voice="mars")
        pub.start_session()
        srv = await stateserver.start(pub, 0)
        try:
            return await _get(srv)
        finally:
            await srv.stop()

    status, _headers, text = asyncio.run(scenario())
    assert status == 200
    body = json.loads(text)
    assert set(body.keys()) == {
        "version", "state", "since", "session_id", "voice", "transcript", "heartbeat",
        "filed_cards", "output_device", "audio_energy",
    }
    assert body["version"] == 1
    assert body["state"] == "ASLEEP"
    assert body["voice"] == "mars"
    assert body["session_id"] is not None


def test_state_surface_header_distinguishes_voice_from_observer_hosts():
    async def scenario():
        pub = StatePublisher(clock=lambda: _dt(0))
        voice = await stateserver.start(pub, 0)
        try:
            voice_response = await _get(voice)
        finally:
            await voice.stop()
        observer = await stateserver.start(pub, 0, surface_mode="observer")
        try:
            observer_response = await _get(observer)
        finally:
            await observer.stop()
        return voice_response, observer_response

    voice, observer = asyncio.run(scenario())
    assert voice[1]["x-atlas-surface"] == "voice"
    assert observer[1]["x-atlas-surface"] == "observer"


def test_mirrored_state_is_fixed_schema_bounded_and_fails_closed():
    mirrored = {
        "version": 1, "state": "LISTENING", "since": _dt(1).isoformat(),
        "session_id": "session-1", "voice": "mars",
        "transcript": [{"t": _dt(2).isoformat(), "role": "user", "text": "Hey Atlas", "token": "no"}],
        "filed_cards": [], "output_device": {"configured": "follow", "resolved": "Speakers"},
        "audio_energy": .73,
        "credential": "must-not-escape",
    }

    async def scenario():
        pub = StatePublisher(clock=lambda: _dt(0))
        good = await stateserver.start(pub, 0, state_provider=lambda: mirrored, surface_mode="mirror")
        try:
            good_response = await _get(good)
        finally:
            await good.stop()
        bad = await stateserver.start(pub, 0, state_provider=lambda: {"state": "LISTENING"},
                                      surface_mode="mirror")
        try:
            bad_response = await _get(bad)
        finally:
            await bad.stop()
        return good_response, bad_response

    good, bad = asyncio.run(scenario())
    body = json.loads(good[2])
    assert good[0] == 200 and good[1]["x-atlas-surface"] == "mirror"
    assert body["state"] == "LISTENING" and body["transcript"][0]["text"] == "Hey Atlas"
    assert body["audio_energy"] == .73
    assert "must-not-escape" not in good[2] and '"token"' not in good[2]
    assert bad[0] == 503


def test_signal_endpoint_is_bounded_and_supports_an_async_mirror():
    async def scenario():
        pub = StatePublisher(clock=lambda: _dt(0))
        pub.set_state("LISTENING")
        pub.set_audio_energy(.625)
        local = await stateserver.start(pub, 0)
        try:
            local_response = await _get(local, "/signal")
        finally:
            await local.stop()

        async def mirrored_signal():
            return 4.2

        mirror = await stateserver.start(pub, 0, signal_provider=mirrored_signal)
        try:
            mirror_response = await _get(mirror, "/signal")
        finally:
            await mirror.stop()
        return local_response, mirror_response

    local, mirror = asyncio.run(scenario())
    assert json.loads(local[2]) == {"energy": .625}
    assert json.loads(mirror[2]) == {"energy": 1.0}


def test_transcript_ring_round_trips():
    async def scenario():
        pub = StatePublisher(clock=lambda: _dt(3))
        pub.start_session()
        pub.add_line("user", "what's in the queue")
        pub.add_line("atlas", "Three cards in flight.")
        srv = await stateserver.start(pub, 0)
        try:
            return await _get(srv)
        finally:
            await srv.stop()

    _status, _headers, text = asyncio.run(scenario())
    body = json.loads(text)
    assert body["transcript"] == [
        {"t": _dt(3).isoformat(), "role": "user", "text": "what's in the queue"},
        {"t": _dt(3).isoformat(), "role": "atlas", "text": "Three cards in flight."},
    ]


def test_cache_control_is_no_store():
    async def scenario():
        pub = StatePublisher(clock=lambda: _dt(0))
        srv = await stateserver.start(pub, 0)
        try:
            return await _get(srv)
        finally:
            await srv.stop()

    _status, headers, _text = asyncio.run(scenario())
    assert headers.get("cache-control") == "no-store"


def test_heartbeat_advances_between_two_requests():
    async def scenario():
        ticks = iter([_dt(10), _dt(11)])
        pub = StatePublisher(clock=lambda: _dt(0))  # snapshot() `since` is frozen...
        srv = await stateserver.start(pub, 0, clock=lambda: next(ticks))  # ...heartbeat advances
        try:
            _s1, _h1, t1 = await _get(srv)
            _s2, _h2, t2 = await _get(srv)
            return t1, t2
        finally:
            await srv.stop()

    t1, t2 = asyncio.run(scenario())
    h1 = json.loads(t1)["heartbeat"]
    h2 = json.loads(t2)["heartbeat"]
    assert h1 == _dt(10).isoformat()
    assert h2 == _dt(11).isoformat()
    assert h2 > h1


def test_bound_to_localhost_only():
    async def scenario():
        pub = StatePublisher(clock=lambda: _dt(0))
        srv = await stateserver.start(pub, 0)
        try:
            return list(srv.addresses)
        finally:
            await srv.stop()

    addrs = asyncio.run(scenario())
    assert addrs, "server should report at least one bound address"
    for sockaddr in addrs:
        assert sockaddr[0] == "127.0.0.1"
    assert stateserver.HOST == "127.0.0.1"


def test_only_state_route_exists():
    async def scenario():
        pub = StatePublisher(clock=lambda: _dt(0))
        srv = await stateserver.start(pub, 0)
        try:
            _status, _headers, _text = await _get(srv, "/secrets")
            return _status
        finally:
            await srv.stop()

    assert asyncio.run(scenario()) == 404


def test_standalone_root_and_known_assets_have_explicit_types_and_cache_headers():
    async def scenario():
        pub = StatePublisher(clock=lambda: _dt(0))
        srv = await stateserver.start(pub, 0)
        try:
            return (
                await _get(srv, "/"),
                await _get(srv, "/ui/styles.css"),
                await _get(srv, "/ui/app.js"),
            )
        finally:
            await srv.stop()

    (root_status, root_headers, root_text), (css_status, css_headers, css_text), (js_status, js_headers, js_text) = asyncio.run(scenario())
    assert root_status == 200
    assert root_headers["content-type"].startswith("text/html")
    assert root_headers["cache-control"] == "no-cache"
    assert root_headers["x-content-type-options"] == "nosniff"
    assert root_headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in root_headers["content-security-policy"]
    assert "Atlas" in root_text
    assert css_status == 200
    assert css_headers["content-type"].startswith("text/css")
    assert css_headers["cache-control"] == "no-cache"
    assert ".atlas-engine" in css_text
    assert js_status == 200
    assert js_headers["content-type"].startswith("application/javascript")
    assert js_headers["cache-control"] == "no-cache"
    assert "refreshState" in js_text
    assert "action.confirmable === true" in js_text
    assert "This proposal cannot be confirmed" in js_text


def test_unknown_assets_and_traversal_are_not_served():
    async def scenario():
        pub = StatePublisher(clock=lambda: _dt(0))
        srv = await stateserver.start(pub, 0)
        try:
            unknown = await _get(srv, "/ui/not-a-file.txt")
            traversal = await _get(srv, "/ui/../../worker/stateserver.py")
            control = await _get(srv, "/control")
            return unknown[0], traversal[0], control[0]
        finally:
            await srv.stop()

    assert asyncio.run(scenario()) == (404, 404, 404)


def test_capabilities_defaults_empty_and_accepts_injected_key_free_catalog():
    async def scenario():
        pub = StatePublisher(clock=lambda: _dt(0))
        default_srv = await stateserver.start(pub, 0)
        try:
            empty = await _get(default_srv, "/capabilities")
        finally:
            await default_srv.stop()

        catalog = [{"id": "browser", "label": "Browser", "status": "connected", "api_key": "must-not-escape"}]
        configured_srv = await stateserver.start(pub, 0, catalog_provider=lambda: catalog)
        try:
            configured = await _get(configured_srv, "/capabilities")
        finally:
            await configured_srv.stop()
        return empty, configured

    (empty_status, empty_headers, empty_text), (configured_status, configured_headers, configured_text) = asyncio.run(scenario())
    assert empty_status == 200
    assert json.loads(empty_text) == []
    assert empty_headers["content-type"].startswith("application/json")
    assert empty_headers["cache-control"] == "no-store"
    assert configured_status == 200
    assert json.loads(configured_text) == [{"id": "browser", "label": "Browser", "status": "connected"}]
    assert "must-not-escape" not in configured_text
    assert configured_headers["cache-control"] == "no-store"


def test_jobs_projection_is_public_bounded_and_fail_closed():
    async def scenario():
        pub = StatePublisher(clock=lambda: _dt(0))
        jobs = [{
            "id": "job-1", "status": "queued", "lane": "slow",
            "operation": "document.compose", "updated_at": "123.0",
            "proposal_id": "proposal-1", "target": "private-draft",
            "result_available": True,
            "payload": "must-not-escape", "token": "secret-token",
        }] + [{
            "id": f"extra-{index}", "status": "queued", "lane": "fast",
            "operation": "calendar.list",
        } for index in range(110)]
        configured = await stateserver.start(pub, 0, job_provider=lambda: jobs)
        try:
            safe = await _get(configured, "/jobs")
        finally:
            await configured.stop()
        failed = await stateserver.start(
            pub, 0, job_provider=lambda: (_ for _ in ()).throw(RuntimeError("private failure")))
        try:
            empty = await _get(failed, "/jobs")
        finally:
            await failed.stop()
        return safe, empty

    (safe_status, safe_headers, safe_text), (empty_status, _empty_headers, empty_text) = asyncio.run(scenario())
    assert safe_status == 200
    body = json.loads(safe_text)
    assert len(body["jobs"]) == 100
    assert body["jobs"][0] == {
        "id": "job-1", "status": "queued", "lane": "slow",
        "operation": "document.compose", "updated_at": "123.0",
        "proposal_id": "proposal-1", "result_available": True,
    }
    assert "private-draft" not in safe_text
    assert "must-not-escape" not in safe_text
    assert "secret-token" not in safe_text
    assert safe_headers["cache-control"] == "no-store"
    assert empty_status == 200
    assert json.loads(empty_text) == {"jobs": []}


def test_job_events_and_subscription_health_are_bounded_public_projections():
    async def scenario():
        job_id = str(uuid4())
        events = [{
            "sequence": 1,
            "kind": "transitioned",
            "state": "running",
            "timestamp": 123.5,
            "worker_id": "atlas-subscription",
            "code": "claimed",
            "token": "must-not-escape",
        }, {
            "sequence": 2, "kind": "transitioned", "state": "running",
            "timestamp": float("nan"), "code": "must-be-dropped",
        }]
        health = WorkerHealth(
            WorkerHealthStatus.AVAILABLE, "subscription_attested",
            worker_id="atlas-subscription", checked_at=123.5,
        )
        srv = await stateserver.start(
            StatePublisher(clock=lambda: _dt(0)), 0,
            job_event_provider=lambda requested: events if requested == job_id else [],
            health_provider=lambda: health,
        )
        try:
            return (
                await _get(srv, f"/jobs/{job_id}/events"),
                await _get(srv, "/health"),
                await _get(srv, "/jobs/not-a-job/events"),
            )
        finally:
            await srv.stop()

    event_response, health_response, malformed = asyncio.run(scenario())
    assert event_response[0] == 200
    assert json.loads(event_response[2]) == {"events": [{
        "sequence": 1, "timestamp": 123.5, "kind": "transitioned",
        "state": "running", "code": "claimed", "worker_id": "atlas-subscription",
    }]}
    assert "must-not-escape" not in event_response[2]
    assert json.loads(health_response[2]) == {
        "status": "available", "reason": "subscription_attested",
        "worker_id": "atlas-subscription", "checked_at": 123.5,
    }
    assert malformed[0] == 404


def test_private_result_requires_pairing_and_returns_only_fixed_encrypted_result_projection():
    async def scenario():
        auth = PairingAuthorizer(token="pair-token")
        bearer, _ = auth.pair("pair-token")
        job_id = str(uuid4())
        answer = "# Private result\n\nThe complete reviewed draft."
        result = ProtectedTaskResult(
            job_id, answer, sha256(answer.encode()).hexdigest(),
            ("receipt-1", "receipt-2"), artifact_name="brief.md",
        )
        srv = await stateserver.start(
            StatePublisher(clock=lambda: _dt(0)), 0,
            action_authorizer=auth, result_provider=lambda requested: result,
        )
        try:
            denied = await _get(srv, f"/jobs/{job_id}/result")
            malformed = await _get(srv, "/jobs/not-a-job/result", {HEADER: bearer})
            allowed = await _get(srv, f"/jobs/{job_id}/result", {HEADER: bearer})
            return denied, malformed, allowed
        finally:
            await srv.stop()

    denied, malformed, allowed = asyncio.run(scenario())
    assert denied[0] == 401 and malformed[0] == 404 and allowed[0] == 200
    assert allowed[1]["cache-control"] == "no-store"
    assert json.loads(allowed[2]) == {
        "version": 1, "job_id": json.loads(allowed[2])["job_id"],
        "answer": "# Private result\n\nThe complete reviewed draft.",
        "candidate_digest": sha256(
            "# Private result\n\nThe complete reviewed draft.".encode()
        ).hexdigest(),
        "evidence_count": 2, "artifact_name": "brief.md",
    }


class _ActionBroker:
    def __init__(self):
        self.actions = [{
            "id": "open-file-1",
            "label": "Open a file",
            "preview": "Open notes.md in the local editor",
            "proposal_hash": "hash-open-1",
            "status": "pending",
            "risk": "local desktop",
            "confirmable": True,
            "command": "must-never-escape",
        }]
        self.calls = []

    def list_actions(self):
        return self.actions

    def run_action(self, action_id, proposal_hash, **context):
        self.calls.append(("run", action_id, proposal_hash))
        return {"id": action_id, "status": "running"}

    def cancel_action(self, action_id, proposal_hash, **context):
        self.calls.append(("cancel", action_id, proposal_hash))
        return {"id": action_id, "status": "cancelled"}


def test_action_listing_requires_pairing_and_pair_route_returns_memory_bearer():
    async def scenario():
        auth = PairingAuthorizer(token="pair-token")
        srv = await stateserver.start(StatePublisher(clock=lambda: _dt(0)), 0,
                                      action_broker=_ActionBroker(), action_authorizer=auth)
        try:
            unauthorized = await _get(srv, "/actions")
            headers = {"content-type": "application/json",
                       "origin": f"http://127.0.0.1:{srv.port}"}
            paired = await _post(srv, "/pair", json.dumps({"token": "pair-token"}).encode(), headers)
            return unauthorized, paired
        finally:
            await srv.stop()
    unauthorized, paired = asyncio.run(scenario())
    assert unauthorized[0] == 401
    assert paired[0] == 200
    assert "set-cookie" not in paired[1]
    token = json.loads(paired[2])["action_token"]
    assert isinstance(token, str) and len(token) >= 32


def test_guided_setup_requires_pairing_same_origin_and_a_fixed_guide_id():
    calls = []

    def start_guide(guide_id):
        calls.append(guide_id)
        if guide_id != "browser":
            raise KeyError(guide_id)
        return SimpleNamespace(job_id=str(uuid4()), status="queued",
                               lane=SimpleNamespace(value="slow"))

    async def scenario():
        auth = PairingAuthorizer(token="pair-token")
        bearer, _ = auth.pair("pair-token")
        srv = await stateserver.start(StatePublisher(clock=lambda: _dt(0)), 0,
                                      action_authorizer=auth,
                                      guided_setup_provider=start_guide)
        body = b"{}"
        valid = {"content-type": "application/json",
                 "origin": f"http://127.0.0.1:{srv.port}", HEADER: bearer}
        try:
            denied = await _post(srv, "/guided-setups/browser", body, {
                "content-type": "application/json", "origin": valid["origin"]})
            foreign = await _post(srv, "/guided-setups/browser", body, {
                **valid, "origin": "https://evil.example"})
            unknown = await _post(srv, "/guided-setups/not-reviewed", body, valid)
            accepted = await _post(srv, "/guided-setups/browser", body, valid)
            return denied, foreign, unknown, accepted
        finally:
            await srv.stop()

    denied, foreign, unknown, accepted = asyncio.run(scenario())
    assert denied[0] == 401 and foreign[0] == 403 and unknown[0] == 404
    assert accepted[0] == 202 and json.loads(accepted[2])["ok"] is True
    assert calls == ["not-reviewed", "browser"]


def test_receipt_history_requires_pairing_and_is_fixed_schema():
    async def scenario():
        auth = PairingAuthorizer(token="pair-token")
        cookie, _ = auth.pair("pair-token")
        receipt = {
            "version": 1, "timestamp": "2026-08-20T00:00:00+00:00",
            "proposal_id": "proposal-1", "capability_id": "browser.click",
            "parameters_hash": "a" * 64, "status": "succeeded",
            "session_id": "session-1", "device_id": "device-1",
            "confirmation_channel": "ui", "error_code": None,
            "body": "must-never-escape",
        }
        srv = await stateserver.start(StatePublisher(clock=lambda: _dt(0)), 0,
                                      action_authorizer=auth,
                                      receipt_provider=lambda: [receipt])
        try:
            denied = await _get(srv, "/receipts")
            allowed = await _get(srv, "/receipts", {HEADER: cookie})
            return denied, allowed
        finally:
            await srv.stop()
    denied, allowed = asyncio.run(scenario())
    assert denied[0] == 401 and allowed[0] == 200
    assert json.loads(allowed[2])["receipts"][0]["status"] == "succeeded"
    assert "must-never-escape" not in allowed[2]


def test_actions_projection_is_empty_without_broker_and_sanitized_with_broker():
    async def scenario():
        pub = StatePublisher(clock=lambda: _dt(0))
        empty_srv = await stateserver.start(pub, 0)
        try:
            empty = await _get(empty_srv, "/actions")
        finally:
            await empty_srv.stop()

        broker = _ActionBroker()
        auth = PairingAuthorizer(token="pair-token")
        cookie, _ = auth.pair("pair-token")
        configured_srv = await stateserver.start(pub, 0, action_broker=broker,
                                                  action_authorizer=auth)
        try:
            configured = await _get(configured_srv, "/actions", {HEADER: cookie})
        finally:
            await configured_srv.stop()
        return empty, configured

    (empty_status, _empty_headers, empty_text), (configured_status, _configured_headers, configured_text) = asyncio.run(scenario())
    assert empty_status == 200
    assert json.loads(empty_text) == {"actions": []}
    assert configured_status == 200
    body = json.loads(configured_text)
    assert body["actions"] == [{
        "id": "open-file-1",
        "label": "Open a file",
        "preview": "Open notes.md in the local editor",
        "proposal_hash": "hash-open-1",
        "status": "pending",
        "risk": "local desktop",
        "confirmable": True,
    }]
    assert "must-never-escape" not in configured_text


def test_actions_require_same_origin_json_and_matching_proposal_hash():
    async def scenario():
        broker = _ActionBroker()
        auth = PairingAuthorizer(token="pair-token")
        cookie, _ = auth.pair("pair-token")
        pub = StatePublisher(clock=lambda: _dt(0))
        srv = await stateserver.start(pub, 0, action_broker=broker, action_authorizer=auth)
        try:
            valid_body = json.dumps({"proposal_hash": "hash-open-1"}).encode()
            valid_headers = {"content-type": "application/json", "origin": f"http://127.0.0.1:{srv.port}",
                             HEADER: cookie}
            run = await _post(srv, "/actions/open-file-1/run", valid_body, valid_headers)
            cancel = await _post(srv, "/actions/open-file-1/cancel", valid_body, valid_headers)
            mismatch = await _post(srv, "/actions/open-file-1/run", json.dumps({"proposal_hash": "stale"}).encode(), valid_headers)
            foreign = await _post(srv, "/actions/open-file-1/run", valid_body, {"content-type": "application/json", "origin": "https://evil.example", HEADER: cookie})
            wrong_type = await _post(srv, "/actions/open-file-1/run", valid_body, {
                "content-type": "text/plain", "origin": f"http://127.0.0.1:{srv.port}",
                HEADER: cookie})
            return run, cancel, mismatch, foreign, wrong_type, broker.calls
        finally:
            await srv.stop()

    run, cancel, mismatch, foreign, wrong_type, calls = asyncio.run(scenario())
    assert run[0] == 200
    assert cancel[0] == 200
    assert mismatch[0] == 409
    assert foreign[0] == 403
    assert wrong_type[0] == 415
    assert calls == [("run", "open-file-1", "hash-open-1"), ("cancel", "open-file-1", "hash-open-1")]


def test_unconfirmable_action_rejects_run_but_remains_cancellable():
    async def scenario():
        broker = _ActionBroker()
        broker.actions[0]["confirmable"] = False
        auth = PairingAuthorizer(token="pair-token")
        cookie, _ = auth.pair("pair-token")
        srv = await stateserver.start(StatePublisher(clock=lambda: _dt(0)), 0,
                                      action_broker=broker, action_authorizer=auth)
        try:
            body = json.dumps({"proposal_hash": "hash-open-1"}).encode()
            headers = {"content-type": "application/json",
                       "origin": f"http://127.0.0.1:{srv.port}", HEADER: cookie}
            listing = await _get(srv, "/actions", {HEADER: cookie})
            run = await _post(srv, "/actions/open-file-1/run", body, headers)
            cancel = await _post(srv, "/actions/open-file-1/cancel", body, headers)
            return listing, run, cancel, broker.calls
        finally:
            await srv.stop()

    listing, run, cancel, calls = asyncio.run(scenario())
    assert json.loads(listing[2])["actions"][0]["confirmable"] is False
    assert run[0] == 409 and "not confirmable" in run[2]
    assert cancel[0] == 200
    assert calls == [("cancel", "open-file-1", "hash-open-1")]


def test_confirmed_run_projects_acting_until_broker_settles_then_restores_prior_state():
    class WaitingBroker(_ActionBroker):
        def __init__(self):
            super().__init__()
            self.actions[0]["confirmable"] = True
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def run_action(self, action_id, proposal_hash, **context):
            self.calls.append(("run", action_id, proposal_hash))
            self.started.set()
            await self.release.wait()
            return {"id": action_id, "status": "completed"}

    async def scenario():
        broker = WaitingBroker()
        auth = PairingAuthorizer(token="pair-token")
        token, _ = auth.pair("pair-token")
        pub = StatePublisher(clock=lambda: _dt(0))
        pub.set_state(THINKING)
        srv = await stateserver.start(pub, 0, action_broker=broker, action_authorizer=auth)
        try:
            headers = {"content-type": "application/json",
                       "origin": f"http://127.0.0.1:{srv.port}", HEADER: token}
            pending = asyncio.create_task(_post(
                srv, "/actions/open-file-1/run",
                json.dumps({"proposal_hash": "hash-open-1"}).encode(), headers))
            await broker.started.wait()
            state_during = pub.state
            broker.release.set()
            response = await pending
            return state_during, pub.state, response
        finally:
            await srv.stop()

    state_during, state_after, response = asyncio.run(scenario())
    assert state_during == ACTING
    assert state_after == THINKING
    assert response[0] == 200


def test_concurrent_confirmed_runs_hold_acting_through_voice_transition_until_last_settles():
    class WaitingBroker(_ActionBroker):
        def __init__(self):
            super().__init__()
            self.actions[0]["confirmable"] = True
            self.started = asyncio.Event()
            self.started_count = 0
            self.release_first = asyncio.Event()
            self.release_second = asyncio.Event()

        async def run_action(self, action_id, proposal_hash, **context):
            self.calls.append(("run", action_id, proposal_hash))
            self.started_count += 1
            if self.started_count == 2:
                self.started.set()
            if self.started_count == 1:
                await self.release_first.wait()
            else:
                await self.release_second.wait()
            return {"id": action_id, "status": "completed"}

    async def scenario():
        broker = WaitingBroker()
        pub = StatePublisher(clock=lambda: _dt(0))
        pub.set_state(THINKING)
        srv = stateserver.StateServer(pub, action_broker=broker)

        class Request:
            def __init__(self, proposal_hash):
                self.proposal_hash = proposal_hash

        srv._authorize_action_request = lambda _request: type(
            "Context", (), {"session_id": "session", "device_id": "device"})()

        async def read_body(request):
            return {"proposal_hash": request.proposal_hash}

        srv._read_action_body = read_body
        first = asyncio.create_task(srv._run_action(Request("hash-open-1"), "open-file-1", "run"))
        second = asyncio.create_task(srv._run_action(Request("hash-open-1"), "open-file-1", "run"))
        await asyncio.wait_for(broker.started.wait(), timeout=2)
        pub.set_state(SPEAKING)
        held_after_voice = pub.state
        broker.release_first.set()
        first_response = await first
        held_after_first = pub.state
        broker.release_second.set()
        second_response = await second
        return held_after_voice, held_after_first, pub.state, first_response, second_response

    held_after_voice, held_after_first, final_state, first_response, second_response = asyncio.run(scenario())
    assert held_after_voice == ACTING
    assert held_after_first == ACTING
    assert final_state == SPEAKING
    assert first_response.status == 200
    assert second_response.status == 200


def test_actions_reject_unknown_ids_and_oversized_bodies_without_control_fallback():
    async def scenario():
        broker = _ActionBroker()
        auth = PairingAuthorizer(token="pair-token")
        cookie, _ = auth.pair("pair-token")
        pub = StatePublisher(clock=lambda: _dt(0))
        srv = await stateserver.start(pub, 0, action_broker=broker, action_authorizer=auth)
        try:
            headers = {"content-type": "application/json",
                       "origin": f"http://127.0.0.1:{srv.port}",
                       HEADER: cookie}
            unknown = await _post(srv, "/actions/../../worker/stateserver.py/run", b'{"proposal_hash":"hash-open-1"}', headers)
            oversized = await _post(srv, "/actions/open-file-1/run", b"{" + b"a" * (stateserver.ACTION_BODY_LIMIT + 1) + b"}", headers)
            control = await _post(srv, "/run", b'{}', headers)
            return unknown[0], oversized[0], control[0]
        finally:
            await srv.stop()

    assert asyncio.run(scenario()) == (404, 413, 404)


def test_broker_failures_emit_sanitized_rejection_receipt():
    class RejectingBroker(_ActionBroker):
        def run_action(self, *_args, **_context):
            raise RuntimeError("secret detail must not be journaled")

        def record_rejected(self, action_id, reason_code, **context):
            self.calls.append(("rejected", action_id, reason_code, context))

    async def scenario():
        broker = RejectingBroker()
        auth = PairingAuthorizer(token="pair-token")
        token, context = auth.pair("pair-token")
        srv = await stateserver.start(StatePublisher(clock=lambda: _dt(0)), 0,
                                      action_broker=broker, action_authorizer=auth)
        try:
            headers = {"content-type": "application/json",
                       "origin": f"http://127.0.0.1:{srv.port}", HEADER: token}
            response = await _post(
                srv, "/actions/open-file-1/run",
                json.dumps({"proposal_hash": "hash-open-1"}).encode(), headers)
            return response, broker.calls, context
        finally:
            await srv.stop()
    response, calls, context = asyncio.run(scenario())
    assert response[0] == 502
    assert calls == [("rejected", "open-file-1", "runtimeerror", {
        "session_id": context.session_id, "device_id": context.device_id})]


def test_response_is_key_free_under_poisoned_env(monkeypatch):
    """Set FAKE secrets in os.environ and prove the serialized response never contains them —
    the surface is built from publisher.snapshot() + heartbeat and NEVER reads process env."""
    sentinel = "SUPER-SECRET-SENTINEL-VALUE-9f3a2b1c7d"
    monkeypatch.setenv("ATLAS_FAKE_SECRET", sentinel)
    monkeypatch.setenv("FAKE_API_KEY", sentinel)

    async def scenario():
        pub = StatePublisher(clock=lambda: _dt(0), voice="mars")
        pub.start_session()
        pub.add_line("user", "hello")
        srv = await stateserver.start(pub, 0)
        try:
            return await _get(srv)
        finally:
            await srv.stop()

    _status, _headers, text = asyncio.run(scenario())
    assert sentinel not in text
