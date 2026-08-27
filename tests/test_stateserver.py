"""Loopback state, work, MCP, pairing, and UI HTTP surface."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import time
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
    assert "function drawParticles(now)" in script[2]
    assert "Math.min(2" in script[2]
    assert "new Path2D()" in script[2]
    assert '"data-frame-cost-ms"' in script[2]
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
    assert 'kbUnlockButton.id = "kb-unlock-button";' in script[2]
    assert 'callNativeWindow("unlock_kb")' in script[2]
    assert '"atlas unlock kb"' in script[2]
    assert '"unlock the dashboard"' in script[2]
    assert "session ${server.session}" in script[2]


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
    }, {
        "name": "kb", "connected": True, "tools": 22, "error": None,
        "session": "held", "token": "must-not-escape",
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
            health_provider=lambda: {
                "claude": True,
                "mcp": mcp,
                "traces": {
                    "enabled": True,
                    "turns_today": 7,
                    "avg_ms_today": 125.5,
                    "cache_hit_ratio_today": 0.75,
                    "cost_usd_today": 0.0123,
                    "secret": "must-not-escape",
                },
            },
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
        "mcp": [
            {"name": "google", "connected": True, "tools": 11, "error": None},
            {
                "name": "kb", "connected": True, "tools": 22, "error": None,
                "session": "held",
            },
        ],
        "traces": {
            "enabled": True,
            "turns_today": 7,
            "avg_ms_today": 125.5,
            "cache_hit_ratio_today": 0.75,
            "cost_usd_today": 0.0123,
        },
    }
    assert missing_mcp[0] == 404
    assert invalid[0] == 400
    assert oversized[0] == 400


def test_health_returns_cached_trace_snapshot_while_database_is_exclusively_locked(tmp_path):
    from worker.traces import TraceRecorder

    path = tmp_path / "traces.db"
    recorder = TraceRecorder(path)
    turn = recorder.begin_turn(wake_kind="wake")
    recorder.route(turn, ms=1, ok=True)
    recorder.end_turn(
        turn, addressed=True, wake_kind="wake", outcome="responded", total_ms=7,
    )
    assert recorder.summary(days=1)["turns"] == 1

    def health():
        cached = recorder.health
        return {"traces": {
            "enabled": cached["enabled"], "turns_today": cached["turns"],
            "avg_ms_today": cached["avg_ms"],
            "cache_hit_ratio_today": cached["cache_hit_ratio"],
            "cost_usd_today": cached["cost_usd"],
        }}

    async def scenario():
        server = await stateserver.start(
            StatePublisher(clock=lambda: _dt(0)), 0, health_provider=health,
        )
        lock = sqlite3.connect(path, timeout=0)
        lock.execute("BEGIN EXCLUSIVE")
        try:
            started = time.perf_counter()
            response = await _request(server, path="/health")
            return response, time.perf_counter() - started
        finally:
            lock.rollback()
            lock.close()
            await server.stop()

    response, elapsed = asyncio.run(scenario())
    recorder.close()
    assert elapsed < 0.1
    assert json.loads(response[2])["traces"]["turns_today"] == 1


def test_entrypoint_warms_model_only_after_build_and_state_server_start(monkeypatch):
    from worker import app

    sys.modules.pop("worker.traces", None)
    calls = []
    publishers = []
    placeholder_audio = []
    build_active = False

    real_publisher = app.state.StatePublisher

    def make_publisher(*args, **kwargs):
        calls.append("publisher-created")
        publisher = real_publisher(*args, **kwargs)
        placeholder_audio.append(publisher.snapshot()["audio"])
        return publisher

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
            calls.append("session-started")
            return None

        def interrupt(self):
            return None

    class FakeServer:
        port = 4321

    async def start_server(*_args, **_kwargs):
        publishers.append(_args[0])
        snapshot = publishers[0].snapshot()
        assert snapshot["ready"] is False
        assert snapshot["audio"] == placeholder_audio[0]
        calls.append("server-started")
        return FakeServer()

    class FakeContext:
        room = object()

        async def connect(self):
            calls.append("livekit-connected")
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
        "wake_input_device": "Desk microphone",
        "wake_model": "hey_atlas",
    }
    monkeypatch.setattr(app, "TEXT_MODE", True)
    monkeypatch.setattr(app, "_cfg", lambda: cfg)
    monkeypatch.setattr(app.runtime, "build", build)
    monkeypatch.setattr(app.state, "StatePublisher", make_publisher)
    monkeypatch.setattr(app.jobobject, "assign_current_process", lambda: None)
    monkeypatch.setattr(app.envload, "load_private_environment", lambda: None)
    monkeypatch.setattr(app.router, "Addressing", lambda *_args: object())
    monkeypatch.setattr(app.router, "vocabulary", lambda _cfg: [])
    monkeypatch.setattr(
        app.wakeword,
        "resolve_input_device",
        lambda _device: calls.append("wake-resolved") or 7,
    )
    monkeypatch.setattr(
        app.wakeword,
        "InputDeviceSwitch",
        lambda _device: calls.append("wake-switch-created") or object(),
    )
    monkeypatch.setattr(
        app.devicewatch,
        "audio_status",
        lambda _cfg: calls.append("audio-status") or {},
    )
    monkeypatch.setattr(app.devicewatch, "AudioRestartCoalescer", lambda *_args, **_kwargs: SimpleNamespace(request=lambda _reason: None))
    monkeypatch.setattr(app.devicewatch, "audio_failure_callback", lambda *_args: None)
    monkeypatch.setattr(
        app.threading,
        "Thread",
        lambda **_kwargs: SimpleNamespace(
            start=lambda: calls.append("wake-listener-started")
        ),
    )
    monkeypatch.setattr(app, "AgentSession", lambda **_kwargs: FakeSession())
    monkeypatch.setattr(app, "AtlasAgent", lambda **_kwargs: SimpleNamespace(turn_handler=None))
    monkeypatch.setattr(app, "_build_tts", lambda _cfg: object())
    monkeypatch.setattr(app.deepgram, "STTv2", lambda **_kwargs: object())
    monkeypatch.setattr(app.silero.VAD, "load", lambda: object())
    monkeypatch.setattr(app.stateserver, "start", start_server)
    monkeypatch.setattr(app, "_emit_ui_url", lambda *_args: calls.append("ui-emitted"))
    real_create_task = asyncio.create_task
    scheduled = iter(("mcp-scheduled", "work-scheduled"))

    def record_create_task(coro):
        calls.append(next(scheduled))
        return real_create_task(coro)

    monkeypatch.setattr(app.asyncio, "create_task", record_create_task)

    asyncio.run(app.entrypoint(FakeContext()))

    assert publishers[0].snapshot()["ready"] is True
    assert calls == [
        "build-returned",
        "publisher-created",
        "server-started",
        "ui-emitted",
        "warm",
        "wake-resolved",
        "wake-switch-created",
        "audio-status",
        "wake-listener-started",
        "mcp-scheduled",
        "work-scheduled",
        "livekit-connected",
        "session-started",
    ]
    assert "worker.traces" not in sys.modules


def test_wake_before_session_exists_publishes_listening_without_speaking():
    from worker import app

    events = []
    publisher = SimpleNamespace(
        start_session=lambda: events.append("session-started"),
        set_state=lambda value: events.append(("state", value)),
        add_line=lambda role, text: events.append(("line", role, text)),
    )
    engagement = app.engagement_mod.Engagement(30)
    addressing = SimpleNamespace(mark_activity=lambda: events.append("activity"))

    app._engage_wake(
        None,
        session_started=False,
        publisher=publisher,
        engagement=engagement,
        addressing=addressing,
    )

    assert engagement.state == app.engagement_mod.ENGAGED
    assert events == [
        "activity",
        "session-started",
        ("state", app.state.LISTENING),
        ("line", "atlas", app.WAKE_LINE),
    ]


def test_entrypoint_cleans_up_every_startup_failure(monkeypatch):
    from worker import app

    cleanup_failures = []
    for failure in ("warm", "connect", "session"):
        events = []
        background_tasks = []

        class FakeWork:
            launcher = SimpleNamespace(available=True)

            def on_terminal(self, _callback):
                return None

            async def cancel(self, _job_id):
                return True

            async def cancel_active(self, *, timeout_s):
                events.append(("work-cancelled", timeout_s))

            async def run(self, stop):
                await stop.wait()

        class FakeMcp:
            async def connect(self, _registry):
                await asyncio.Event().wait()

            async def close(self):
                events.append("mcp-closed")

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
                if failure == "warm":
                    raise RuntimeError("warm failed")

        class FakeSession:
            input = SimpleNamespace(set_audio_enabled=lambda _enabled: None)

            async def start(self, **_kwargs):
                if failure == "session":
                    raise RuntimeError("session failed")

            def interrupt(self):
                events.append("session-interrupted")

        class FakeServer:
            port = 4321

        class FakeContext:
            room = object()

            async def connect(self):
                if failure == "connect":
                    raise RuntimeError("connect failed")

            def add_shutdown_callback(self, _callback):
                events.append("shutdown-registered")

            def shutdown(self, _reason):
                return None

        async def stop_server(_store, _server):
            events.append("server-stopped")

        async def start_server(*_args, **_kwargs):
            return FakeServer()

        real_create_task = asyncio.create_task

        def record_create_task(coro):
            task = real_create_task(coro)
            background_tasks.append(task)
            return task

        cfg = {
            "active_voice": "mars",
            "engagement_timeout_s": 30,
            "address_window_s": 10,
            "state_port": 0,
            "wake_model": "hey_atlas",
        }
        with monkeypatch.context() as patch:
            patch.setattr(app, "TEXT_MODE", True)
            patch.setattr(app, "_cfg", lambda: cfg)
            patch.setattr(app.runtime, "build", lambda *_args, **_kwargs: FakeRuntime())
            patch.setattr(app.jobobject, "assign_current_process", lambda: None)
            patch.setattr(app.envload, "load_private_environment", lambda: None)
            patch.setattr(app.router, "Addressing", lambda *_args: object())
            patch.setattr(app.router, "vocabulary", lambda _cfg: [])
            patch.setattr(app.wakeword, "InputDeviceSwitch", lambda _device: object())
            patch.setattr(app.devicewatch, "audio_status", lambda _cfg: {})
            patch.setattr(app.devicewatch, "AudioRestartCoalescer", lambda *_args, **_kwargs: SimpleNamespace(request=lambda _reason: None))
            patch.setattr(app.devicewatch, "audio_failure_callback", lambda *_args: None)
            patch.setattr(
                app.threading,
                "Thread",
                lambda **_kwargs: SimpleNamespace(start=lambda: None),
            )
            patch.setattr(app, "AgentSession", lambda **_kwargs: FakeSession())
            patch.setattr(app, "AtlasAgent", lambda **_kwargs: SimpleNamespace(turn_handler=None))
            patch.setattr(app, "_build_tts", lambda _cfg: object())
            patch.setattr(app.deepgram, "STTv2", lambda **_kwargs: object())
            patch.setattr(app.silero.VAD, "load", lambda: object())
            patch.setattr(app.stateserver, "start", start_server)
            patch.setattr(app, "_emit_ui_url", lambda *_args: None)
            patch.setattr(app, "_flush_store_and_stop_state_server", stop_server)
            patch.setattr(app.asyncio, "create_task", record_create_task)

            async def scenario():
                try:
                    await app.entrypoint(FakeContext())
                except RuntimeError as exc:
                    assert str(exc) == f"{failure} failed"
                else:
                    raise AssertionError("startup failure did not propagate")

                assert "server-stopped" in events
                assert all(task.done() for task in background_tasks)

            try:
                asyncio.run(scenario())
            except AssertionError as exc:
                cleanup_failures.append(f"{failure}: {exc}")
            finally:
                app.wakeword.shutting_down.clear()

    assert cleanup_failures == []


def test_entrypoint_early_shutdown_is_registered_and_idempotent(monkeypatch):
    from worker import app

    events = []
    callback_box = {}
    shutdown_tasks = []

    class FakeWork:
        launcher = SimpleNamespace(available=True)

        def on_terminal(self, _callback):
            return None

        async def cancel(self, _job_id):
            return True

        async def cancel_active(self, *, timeout_s):
            events.append(("work-cancelled", timeout_s))

    class FakeMcp:
        async def close(self):
            events.append("mcp-closed")

        def status(self):
            return []

    class FakeRuntime:
        def __init__(self):
            self.brain = SimpleNamespace(on_tool=None)
            self.work = FakeWork()
            self.mcp = FakeMcp()
            self.registry = object()
            self.store = SimpleNamespace(events=lambda *_args: [], result=lambda *_args: None)

    class FakeServer:
        port = 4321

    class FakeContext:
        def add_shutdown_callback(self, callback):
            callback_box["callback"] = callback

        def shutdown(self, _reason):
            return None

    async def stop_server(_store, _server):
        events.append("server-stopped")
        await asyncio.sleep(0)

    async def start_server(*_args, **_kwargs):
        return FakeServer()

    def emit_ui(*_args):
        callback = callback_box["callback"]
        shutdown_tasks.append(asyncio.create_task(callback()))
        raise RuntimeError("ui failed")

    cfg = {
        "active_voice": "mars",
        "engagement_timeout_s": 30,
        "address_window_s": 10,
        "state_port": 0,
    }
    monkeypatch.setattr(app, "_cfg", lambda: cfg)
    monkeypatch.setattr(app.runtime, "build", lambda *_args, **_kwargs: FakeRuntime())
    monkeypatch.setattr(app.jobobject, "assign_current_process", lambda: None)
    monkeypatch.setattr(app.envload, "load_private_environment", lambda: None)
    monkeypatch.setattr(app.router, "Addressing", lambda *_args: object())
    monkeypatch.setattr(app.router, "vocabulary", lambda _cfg: [])
    monkeypatch.setattr(app.stateserver, "start", start_server)
    monkeypatch.setattr(app, "_emit_ui_url", emit_ui)
    monkeypatch.setattr(app, "_flush_store_and_stop_state_server", stop_server)

    async def scenario():
        try:
            await app.entrypoint(FakeContext())
        except RuntimeError as exc:
            assert str(exc) == "ui failed"
        else:
            raise AssertionError("startup failure did not propagate")
        await asyncio.gather(*shutdown_tasks)

    asyncio.run(scenario())

    assert events.count("server-stopped") == 1
    assert events.count("mcp-closed") == 1
    app.wakeword.shutting_down.clear()


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


def test_kb_session_channel_requires_launcher_token_and_forwards_only_in_memory(monkeypatch):
    received = []

    class FakeMcp:
        async def set_session(self, server, token, expires_at):
            received.append((server, token, expires_at))

        def session_origin(self, server):
            assert server == "kb"
            return "http://127.0.0.1:5317"

    from worker import mcp_client

    monkeypatch.setattr(mcp_client, "active_mcp_servers", lambda: FakeMcp())

    async def scenario():
        server = await stateserver.start(
            StatePublisher(clock=lambda: _dt(0)),
            0,
            shutdown_token="launcher-token",
        )
        body = json.dumps({
            "token": "private-operator-bearer",
            "expiresAt": "2099-01-01T00:00:00Z",
        })
        origin = f"http://127.0.0.1:{server.port}"
        try:
            denied = await _request(
                server,
                "POST",
                "/kb/session",
                body=body,
                headers={"Content-Type": "application/json", "Origin": origin},
            )
            accepted = await _request(
                server,
                "POST",
                "/kb/session",
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Origin": origin,
                    stateserver.SHUTDOWN_HEADER: "launcher-token",
                },
            )
            config = await _request(
                server,
                path="/kb/config",
                headers={stateserver.SHUTDOWN_HEADER: "launcher-token"},
            )
            return denied, accepted, config
        finally:
            await server.stop()

    denied, accepted, config = asyncio.run(scenario())
    assert denied[0] == 403
    assert json.loads(accepted[2]) == {"ok": True}
    assert received == [(
        "kb",
        "private-operator-bearer",
        "2099-01-01T00:00:00Z",
    )]
    assert "private-operator-bearer" not in accepted[2]
    assert json.loads(config[2]) == {
        "enabled": True,
        "origin": "http://127.0.0.1:5317",
    }


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
