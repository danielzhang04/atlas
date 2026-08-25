"""Loopback state, work, MCP, pairing, and UI HTTP surface."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import aiohttp

from worker import stateserver
from worker.state import StatePublisher


def _dt(second: int) -> datetime:
    return datetime(2026, 8, 22, 12, 0, second, tzinfo=timezone.utc)


async def _request(server, method="GET", path="/state", *, body=None, headers=None):
    url = f"http://127.0.0.1:{server.port}{path}"
    async with aiohttp.ClientSession() as session:
        async with session.request(method, url, data=body, headers=headers or {}) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, await response.text()


def test_state_signal_assets_and_security_headers():
    async def scenario():
        publisher = StatePublisher(
            clock=lambda: _dt(0),
            voice="mars",
            wake_model="hey_atlas",
        )
        publisher.start_session()
        publisher.set_state("LISTENING")
        publisher.set_audio({
            "input": {"name": "Headset microphone", "following": True},
            "output": {"name": "Headphones", "following": True},
        })
        publisher.set_audio_signal(0.625, [index / 23 for index in range(24)])
        server = await stateserver.start(publisher, 0, clock=lambda: _dt(1))
        try:
            return (
                await _request(server),
                await _request(server, path="/signal"),
                await _request(server, path="/"),
                await _request(server, path="/ui/styles.css"),
                await _request(server, path="/ui/app.js"),
                list(server.addresses),
            )
        finally:
            await server.stop()

    state_response, signal, page, styles, script, addresses = asyncio.run(scenario())
    payload = json.loads(state_response[2])
    assert state_response[0] == 200
    assert payload["heartbeat"] == _dt(1).isoformat()
    assert payload["state"] == "LISTENING"
    assert payload["wake_model"] == "hey_atlas"
    assert payload["audio"] == {
        "input": {"name": "Headset microphone", "following": True},
        "output": {"name": "Headphones", "following": True},
    }
    assert "output_device" not in payload
    assert json.loads(signal[2]) == {
        "energy": 0.625,
        "bands": [round(index / 23, 4) for index in range(24)],
    }
    assert page[0] == 200 and "Atlas Engine" in page[2]
    assert 'id="engine-canvas"' in page[2]
    assert 'data-view="live"' in page[2]
    assert 'aria-controls="history-view"' in page[2]
    assert 'id="audio-line"' in page[2]
    assert 'class="core"' not in page[2]
    assert styles[0] == 200 and "--header: 40px" in styles[2]
    assert ".view[hidden]" in styles[2]
    assert "display: none !important" in styles[2]
    assert ".view-home" not in styles[2]
    assert ".engine::before" not in styles[2]
    assert ".orbit" not in styles[2]
    assert "background-size:" not in styles[2]
    assert script[0] == 200 and "const BAR_COUNT = 96" in script[2]
    assert "const INPUT_BANDS = 24" in script[2]
    assert "const UNIQUE_BANDS = 48" in script[2]
    assert "new Float32Array(BAR_COUNT)" in script[2]
    assert "const barStartX = new Float32Array(BAR_COUNT)" in script[2]
    assert "Math.min(2" in script[2]
    assert "new Path2D()" in script[2]
    assert "target > current ? .5 : .12" in script[2]
    assert "requestAnimationFrame" in script[2]
    assert "window.__atlasEngineMetrics" in script[2]
    assert 'publicJson("/signal"' in script[2]
    assert 'currentView !== "live"' in script[2]
    assert 'document.visibilityState !== "visible"' in script[2]
    assert "/jobs/${encodeURIComponent(job.id)}/result" in script[2]
    assert "renderAudio(payload.audio)" in script[2]
    assert state_response[1]["cache-control"] == "no-store"
    assert state_response[1]["x-frame-options"] == "DENY"
    assert all(address[0] == "127.0.0.1" for address in addresses)


def test_state_endpoint_bounds_an_untrusted_wake_model_value():
    class Publisher:
        audio_energy = 0.0
        audio_bands = [0.0] * 24

        @staticmethod
        def snapshot():
            return {"wake_model": "wake" * 100}

    async def scenario():
        server = await stateserver.start(Publisher(), 0, clock=lambda: _dt(1))
        try:
            return await _request(server)
        finally:
            await server.stop()

    response = asyncio.run(scenario())

    assert response[0] == 200
    assert json.loads(response[2])["wake_model"] == ("wake" * 100)[:128]


def test_ui_bearer_survives_reload_only_in_session_and_clears_when_invalid():
    async def scenario():
        server = await stateserver.start(StatePublisher(clock=lambda: _dt(0)), 0)
        try:
            return (
                await _request(server, path="/"),
                await _request(server, path="/ui/app.js"),
            )
        finally:
            await server.stop()

    page, script = asyncio.run(scenario())

    assert 'id="repair-button"' in page[2]
    assert "Re-pair" in page[2]
    assert "sessionStorage.setItem" in script[2]
    assert "sessionStorage.getItem" in script[2]
    assert "sessionStorage.removeItem" in script[2]
    assert "localStorage" not in script[2]
    assert 'authenticatedJson("/pair/bootstrap"' in script[2]
    assert "window.setTimeout(clearPairing" in script[2]
    assert "if (response.status === 401 && (authenticated || clearUnauthorized)) clearPairing();" in script[2]
    assert "restorePairing();" in script[2]
    assert "paired until ${time}" in script[2]
    assert 'publicJson("/health"' in script[2]
    assert '"/mcp"' not in script[2]
    assert "renderMcp(health.mcp);" in script[2]


def test_jobs_events_and_health_are_fixed_public_projections():
    job_id = str(uuid4())
    after_seen = []
    jobs = [{
        "id": job_id,
        "title": "Research",
        "status": "running",
        "session_id": "session-1",
        "created_at": 10.0,
        "updated_at": 12.0,
        "summary": None,
        "error": None,
        "secret": "must-not-escape",
    }]
    events = [SimpleNamespace(
        sequence=4, timestamp=12.5, kind="output", text="working\n",
    )]
    mcp = [{
        "name": "google", "connected": True, "tools": 11, "error": None,
        "env": "must-not-escape",
    }]

    async def scenario():
        authorizer = stateserver.PairingAuthorizer(token="pair-token")
        bearer = authorizer.pair("pair-token")
        server = await stateserver.start(
            StatePublisher(clock=lambda: _dt(0)), 0,
            authorizer=authorizer,
            job_provider=lambda: jobs,
            job_event_provider=lambda requested, after: (
                after_seen.append((requested, after)) or events
            ),
            health_provider=lambda: {"claude": True, "mcp": mcp},
        )
        event_headers = {stateserver.HEADER: bearer}
        try:
            return (
                await _request(server, path="/jobs"),
                await _request(
                    server,
                    path=f"/jobs/{job_id}/events?after=3",
                    headers=event_headers,
                ),
                await _request(server, path="/health"),
                await _request(server, path="/mcp"),
                await _request(
                    server,
                    path=f"/jobs/{job_id}/events?after=bad",
                    headers=event_headers,
                ),
                await _request(
                    server,
                    path=f"/jobs/{job_id}/events?after={'9' * 21}",
                    headers=event_headers,
                ),
            )
        finally:
            await server.stop()

    job_response, event_response, health_response, missing_mcp, invalid, oversized = asyncio.run(scenario())
    expected = {key: value for key, value in jobs[0].items() if key != "secret"}
    assert json.loads(job_response[2]) == {"jobs": [expected]}
    assert json.loads(event_response[2]) == {"events": [{
        "sequence": 4, "timestamp": 12.5, "kind": "output", "text": "working\n",
    }]}
    assert after_seen == [(job_id, 3)]
    assert "must-not-escape" not in health_response[2]
    assert json.loads(health_response[2]) == {
        "claude": True,
        "mcp": [{"name": "google", "connected": True, "tools": 11, "error": None}],
    }
    assert missing_mcp[0] == 404
    assert invalid[0] == 400
    assert oversized[0] == 400


def test_entrypoint_warms_model_only_after_build_and_state_server_start(monkeypatch):
    from worker import app

    calls = []
    build_active = False

    class FakeWork:
        launcher = SimpleNamespace(available=True)

        def on_terminal(self, _callback):
            return None

        async def cancel(self, _job_id):
            return True

        async def run(self, _stop):
            return None

    class FakeMcp:
        async def connect(self, _registry):
            return None

        def status(self):
            return []

    class FakeRuntime:
        def __init__(self):
            self.brain = SimpleNamespace(on_tool=None)
            self.work = FakeWork()
            self.mcp = FakeMcp()
            self.registry = object()
            self.store = SimpleNamespace(events=lambda *_args: [], result=lambda *_args: None)

        def warm_model_client(self):
            assert not build_active
            calls.append("warm")

    def build(_cfg, *, paired_url):
        nonlocal build_active
        assert callable(paired_url)
        build_active = True
        result = FakeRuntime()
        build_active = False
        calls.append("build-returned")
        return result

    class FakeSession:
        input = SimpleNamespace(set_audio_enabled=lambda _enabled: None)

        async def start(self, **_kwargs):
            return None

        def interrupt(self):
            return None

    class FakeServer:
        port = 4321

    async def start_server(*_args, **_kwargs):
        calls.append("server-started")
        return FakeServer()

    class FakeContext:
        room = object()

        async def connect(self):
            return None

        def add_shutdown_callback(self, _callback):
            return None

        def shutdown(self, _reason):
            return None

    cfg = {
        "active_voice": "mars",
        "engagement_timeout_s": 30,
        "address_window_s": 10,
        "state_port": 0,
    }
    monkeypatch.setattr(app, "TEXT_MODE", True)
    monkeypatch.setattr(app, "_cfg", lambda: cfg)
    monkeypatch.setattr(app.runtime, "build", build)
    monkeypatch.setattr(app.jobobject, "assign_current_process", lambda: None)
    monkeypatch.setattr(app.envload, "load_private_environment", lambda: None)
    monkeypatch.setattr(app.router, "Addressing", lambda *_args: object())
    monkeypatch.setattr(app.router, "vocabulary", lambda _cfg: [])
    monkeypatch.setattr(app.wakeword, "InputDeviceSwitch", lambda _device: object())
    monkeypatch.setattr(app.devicewatch, "audio_status", lambda _cfg: {})
    monkeypatch.setattr(app.devicewatch, "AudioRestartCoalescer", lambda *_args, **_kwargs: SimpleNamespace(request=lambda _reason: None))
    monkeypatch.setattr(app.devicewatch, "audio_failure_callback", lambda *_args: None)
    monkeypatch.setattr(app, "AgentSession", lambda **_kwargs: FakeSession())
    monkeypatch.setattr(app, "AtlasAgent", lambda **_kwargs: SimpleNamespace(turn_handler=None))
    monkeypatch.setattr(app, "_build_tts", lambda _cfg: object())
    monkeypatch.setattr(app.deepgram, "STTv2", lambda **_kwargs: object())
    monkeypatch.setattr(app.silero.VAD, "load", lambda: object())
    monkeypatch.setattr(app.stateserver, "start", start_server)
    monkeypatch.setattr(app, "_emit_ui_url", lambda *_args: calls.append("ui-emitted"))

    asyncio.run(app.entrypoint(FakeContext()))

    assert calls == ["build-returned", "server-started", "ui-emitted", "warm"]


def test_pairing_protects_job_events_private_results_and_cancellation():
    job_id = str(uuid4())
    cancelled = []
    events = [SimpleNamespace(
        sequence=2,
        timestamp=12.5,
        kind="output",
        text="private output",
    )]

    async def scenario():
        authorizer = stateserver.PairingAuthorizer(token="pair-token")
        server = await stateserver.start(
            StatePublisher(clock=lambda: _dt(0)), 0,
            authorizer=authorizer,
            job_event_provider=lambda requested, _after: (
                events if requested == job_id else []
            ),
            result_provider=lambda requested: "Private result" if requested == job_id else None,
            cancel_provider=lambda requested: cancelled.append(requested) or {
                "id": requested, "title": "Work", "status": "cancelled",
                "session_id": None, "created_at": 1.0, "updated_at": 2.0,
                "summary": None, "error": None,
            },
        )
        origin = f"http://127.0.0.1:{server.port}"
        json_headers = {"content-type": "application/json", "origin": origin}
        try:
            denied_events = await _request(
                server,
                path=f"/jobs/{job_id}/events",
            )
            denied = await _request(server, path=f"/jobs/{job_id}/result")
            bad_pair = await _request(
                server, "POST", "/pair", body=json.dumps({"token": "\u2603"}),
                headers=json_headers,
            )
            paired = await _request(
                server, "POST", "/pair", body=json.dumps({"token": "pair-token"}),
                headers=json_headers,
            )
            bearer = json.loads(paired[2])["action_token"]
            authorized = {**json_headers, stateserver.HEADER: bearer}
            event_response = await _request(
                server,
                path=f"/jobs/{job_id}/events",
                headers=authorized,
            )
            result = await _request(
                server, path=f"/jobs/{job_id}/result", headers=authorized,
            )
            cancel = await _request(
                server, "POST", f"/jobs/{job_id}/cancel", body="{}", headers=authorized,
            )
            return denied_events, denied, bad_pair, paired, event_response, result, cancel
        finally:
            await server.stop()

    denied_events, denied, bad_pair, paired, event_response, result, cancel = asyncio.run(
        scenario()
    )
    assert denied_events[0] == 401
    assert denied[0] == 401
    assert bad_pair[0] == 401
    assert paired[0] == 200
    assert json.loads(event_response[2]) == {"events": [{
        "sequence": 2,
        "timestamp": 12.5,
        "kind": "output",
        "text": "private output",
    }]}
    assert json.loads(result[2]) == {"job_id": job_id, "result": "Private result"}
    assert json.loads(cancel[2])["job"]["status"] == "cancelled"
    assert cancelled == [job_id]


def test_pairing_returns_expiry_and_renewal_requires_live_bearer_and_loopback_host():
    now = [10.0]
    wall_now = 2_000_000_000.0
    job_id = str(uuid4())

    async def scenario():
        authorizer = stateserver.PairingAuthorizer(
            clock=lambda: now[0],
            wall_clock=lambda: wall_now,
            token="pair-token",
            ttl_s=60.0,
        )
        server = await stateserver.start(
            StatePublisher(clock=lambda: _dt(0)),
            0,
            authorizer=authorizer,
        )
        origin = f"http://127.0.0.1:{server.port}"
        pair_headers = {"content-type": "application/json", "origin": origin}
        try:
            paired = await _request(
                server,
                "POST",
                "/pair",
                body=json.dumps({"token": "pair-token"}),
                headers=pair_headers,
            )
            bearer = json.loads(paired[2])["action_token"]
            missing = await _request(server, path="/pair/bootstrap")
            wrong = await _request(
                server,
                path="/pair/bootstrap",
                headers={stateserver.HEADER: "wrong"},
            )
            bad_host = await _request(
                server,
                path="/pair/bootstrap",
                headers={
                    "Host": f"evil.test:{server.port}",
                    stateserver.HEADER: bearer,
                },
            )
            bootstrap = await _request(
                server,
                path="/pair/bootstrap",
                headers={stateserver.HEADER: bearer},
            )
            bootstrap_token = json.loads(bootstrap[2])["token"]
            repaired = await _request(
                server,
                "POST",
                "/pair",
                body=json.dumps({"token": bootstrap_token}),
                headers=pair_headers,
            )
            fresh_bearer = json.loads(repaired[2])["action_token"]
            reused = await _request(
                server,
                "POST",
                "/pair",
                body=json.dumps({"token": bootstrap_token}),
                headers=pair_headers,
            )
            old_bearer = await _request(
                server,
                path=f"/jobs/{job_id}/events",
                headers={stateserver.HEADER: bearer},
            )
            fresh = await _request(
                server,
                path=f"/jobs/{job_id}/events",
                headers={stateserver.HEADER: fresh_bearer},
            )
            now[0] += 61.0
            expired = await _request(
                server,
                path="/pair/bootstrap",
                headers={stateserver.HEADER: fresh_bearer},
            )
            return paired, missing, wrong, bad_host, bootstrap, repaired, reused, old_bearer, fresh, expired
        finally:
            await server.stop()

    responses = asyncio.run(scenario())
    paired, missing, wrong, bad_host, bootstrap, repaired, reused, old_bearer, fresh, expired = responses

    assert paired[0] == 200
    assert json.loads(paired[2])["expires_at"] == wall_now + 60.0
    assert [missing[0], wrong[0], bad_host[0]] == [403, 403, 403]
    assert bootstrap[0] == 200
    assert repaired[0] == 200
    assert json.loads(repaired[2])["expires_at"] == wall_now + 60.0
    assert reused[0] == 401
    assert old_bearer[0] == 401
    assert fresh[0] == 404
    assert expired[0] == 403


def test_host_allowlist_rejects_every_method_and_route_before_dispatch():
    async def scenario():
        server = await stateserver.start(StatePublisher(clock=lambda: _dt(0)), 0)
        allowed = {"Host": f"localhost:{server.port}"}
        rejected = [
            ("GET", "/state", {"Host": f"evil.test:{server.port}"}),
            ("POST", "/pair", {"Host": "localhost"}),
            (
                "POST",
                "/shutdown",
                {
                    "Host": f"evil.test:{server.port}",
                    stateserver.SHUTDOWN_HEADER: "shutdown-token",
                },
            ),
            ("DELETE", "/not-a-route", {"Host": f"LOCALHOST:{server.port}"}),
        ]
        try:
            accepted = await _request(server, path="/state", headers=allowed)
            denied = [
                await _request(server, method, path, headers=headers)
                for method, path, headers in rejected
            ]
            return accepted, denied
        finally:
            await server.stop()

    accepted, denied = asyncio.run(scenario())

    assert accepted[0] == 200
    assert [response[0] for response in denied] == [403, 403, 403, 403]
    assert all(response[1]["x-frame-options"] == "DENY" for response in denied)


def test_shutdown_requires_exact_launcher_token_before_invoking_provider():
    async def scenario():
        shutdown_calls = []

        async def shutdown():
            shutdown_calls.append("requested")

        server = await stateserver.start(
            StatePublisher(clock=lambda: _dt(0)),
            0,
            shutdown_token="shutdown-token",
            shutdown_provider=shutdown,
        )
        try:
            missing = await _request(server, "POST", "/shutdown")
            wrong = await _request(
                server,
                "POST",
                "/shutdown",
                headers={stateserver.SHUTDOWN_HEADER: "wrong"},
            )
            accepted = await _request(
                server,
                "POST",
                "/shutdown",
                headers={stateserver.SHUTDOWN_HEADER: "shutdown-token"},
            )
            repeated = await _request(
                server,
                "POST",
                "/shutdown",
                headers={stateserver.SHUTDOWN_HEADER: "shutdown-token"},
            )
            return missing, wrong, accepted, repeated, shutdown_calls
        finally:
            await server.stop()

    missing, wrong, accepted, repeated, shutdown_calls = asyncio.run(scenario())

    assert missing[0] == 403
    assert wrong[0] == 403
    assert json.loads(accepted[2]) == {"ok": True}
    assert json.loads(repeated[2]) == {"ok": True}
    assert shutdown_calls == ["requested"]


def test_authorized_shutdown_continues_after_the_request_task_is_cancelled():
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()
        shutdown_calls = []

        async def shutdown():
            started.set()
            await release.wait()
            shutdown_calls.append("completed")

        server = stateserver.StateServer(
            StatePublisher(clock=lambda: _dt(0)),
            shutdown_token="shutdown-token",
            shutdown_provider=shutdown,
        )
        request = SimpleNamespace(headers={
            stateserver.SHUTDOWN_HEADER: "shutdown-token",
        })
        request_task = asyncio.create_task(server._handle_shutdown(request))
        await started.wait()
        request_task.cancel()
        await asyncio.gather(request_task, return_exceptions=True)
        release.set()
        await asyncio.wait_for(server._shutdown_task, timeout=1.0)
        return shutdown_calls

    assert asyncio.run(scenario()) == ["completed"]


def test_pairing_url_contains_one_time_fragment_and_disappears_after_pairing():
    authorizer = stateserver.PairingAuthorizer(token="pair token/+")

    url = stateserver.pairing_url(authorizer, 4360)
    authorizer.pair("pair token/+")

    assert url == "http://127.0.0.1:4360/#pair=pair%20token%2F%2B"
    assert stateserver.pairing_url(authorizer, 4360) is None


def test_removed_routes_and_unknown_assets_are_absent():
    async def scenario():
        server = await stateserver.start(StatePublisher(clock=lambda: _dt(0)), 0)
        try:
            paths = [
                "/capabilities", "/actions", "/receipts", "/guided-setups/browser",
                "/ui/not-present.txt", "/ui/../../worker/stateserver.py",
            ]
            return [await _request(server, path=path) for path in paths]
        finally:
            await server.stop()

    assert [response[0] for response in asyncio.run(scenario())] == [404] * 6


def test_stop_is_idempotent():
    async def scenario():
        server = await stateserver.start(StatePublisher(clock=lambda: _dt(0)), 0)
        await server.stop()
        await server.stop()

    asyncio.run(scenario())
