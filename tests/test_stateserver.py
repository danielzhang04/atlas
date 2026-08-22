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
        publisher = StatePublisher(clock=lambda: _dt(0), voice="mars")
        publisher.start_session()
        publisher.set_state("LISTENING")
        publisher.set_audio_energy(0.625)
        server = await stateserver.start(publisher, 0, clock=lambda: _dt(1))
        try:
            return (
                await _request(server),
                await _request(server, path="/signal"),
                await _request(server, path="/"),
                list(server.addresses),
            )
        finally:
            await server.stop()

    state_response, signal, page, addresses = asyncio.run(scenario())
    payload = json.loads(state_response[2])
    assert state_response[0] == 200
    assert payload["heartbeat"] == _dt(1).isoformat()
    assert payload["state"] == "LISTENING"
    assert json.loads(signal[2]) == {"energy": 0.625}
    assert page[0] == 200 and "Atlas Engine" in page[2]
    assert state_response[1]["cache-control"] == "no-store"
    assert state_response[1]["x-frame-options"] == "DENY"
    assert all(address[0] == "127.0.0.1" for address in addresses)


def test_jobs_events_after_mcp_and_health_are_fixed_public_projections():
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
        server = await stateserver.start(
            StatePublisher(clock=lambda: _dt(0)), 0,
            job_provider=lambda: jobs,
            job_event_provider=lambda requested, after: (
                after_seen.append((requested, after)) or events
            ),
            mcp_provider=lambda: mcp,
            health_provider=lambda: {"claude": True, "mcp": mcp},
        )
        try:
            return (
                await _request(server, path="/jobs"),
                await _request(server, path=f"/jobs/{job_id}/events?after=3"),
                await _request(server, path="/mcp"),
                await _request(server, path="/health"),
                await _request(server, path=f"/jobs/{job_id}/events?after=bad"),
                await _request(server, path=f"/jobs/{job_id}/events?after={'9' * 21}"),
            )
        finally:
            await server.stop()

    job_response, event_response, mcp_response, health_response, invalid, oversized = asyncio.run(scenario())
    expected = {key: value for key, value in jobs[0].items() if key != "secret"}
    assert json.loads(job_response[2]) == {"jobs": [expected]}
    assert json.loads(event_response[2]) == {"events": [{
        "sequence": 4, "timestamp": 12.5, "kind": "output", "text": "working\n",
    }]}
    assert after_seen == [(job_id, 3)]
    assert json.loads(mcp_response[2]) == {"servers": [{
        "name": "google", "connected": True, "tools": 11, "error": None,
    }]}
    assert "must-not-escape" not in health_response[2]
    assert json.loads(health_response[2]) == {
        "claude": True,
        "mcp": [{"name": "google", "connected": True, "tools": 11, "error": None}],
    }
    assert invalid[0] == 400
    assert oversized[0] == 400


def test_pairing_protects_private_results_and_job_cancellation():
    job_id = str(uuid4())
    cancelled = []

    async def scenario():
        authorizer = stateserver.PairingAuthorizer(token="pair-token")
        server = await stateserver.start(
            StatePublisher(clock=lambda: _dt(0)), 0,
            authorizer=authorizer,
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
            result = await _request(
                server, path=f"/jobs/{job_id}/result", headers=authorized,
            )
            cancel = await _request(
                server, "POST", f"/jobs/{job_id}/cancel", body="{}", headers=authorized,
            )
            return denied, bad_pair, paired, result, cancel
        finally:
            await server.stop()

    denied, bad_pair, paired, result, cancel = asyncio.run(scenario())
    assert denied[0] == 401 and bad_pair[0] == 401 and paired[0] == 200
    assert json.loads(result[2]) == {"job_id": job_id, "result": "Private result"}
    assert json.loads(cancel[2])["job"]["status"] == "cancelled"
    assert cancelled == [job_id]


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
