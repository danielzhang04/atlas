from __future__ import annotations

import asyncio
import json
from pathlib import Path

import aiohttp

from worker import stateserver
from worker.state import StatePublisher
from worker.tools import (
    QuickAction,
    Tool,
    ToolRegistry,
    builtin,
    load_apps,
    load_quick_actions,
)


async def _result(value):
    return value


async def _request(server, path, *, body, headers):
    url = f"http://127.0.0.1:{server.port}{path}"
    async with aiohttp.ClientSession() as session:
        async with session.post(url, data=body, headers=headers) as response:
            return response.status, await response.text()


def _registry(calls):
    registry = ToolRegistry()
    registry.register(Tool(
        "instant_action",
        "Run immediately.",
        {
            "type": "object",
            "properties": {"mode": {"type": "string", "enum": ["safe"]}},
            "required": ["mode"],
            "additionalProperties": False,
        },
        lambda arguments: _result(calls.append(("instant", arguments)) or "done"),
    ))
    registry.register(Tool(
        "confirm_action",
        "Wait for confirmation.",
        {
            "type": "object",
            "properties": {"target": {"type": "string", "maxLength": 20}},
            "required": ["target"],
            "additionalProperties": False,
        },
        lambda arguments: _result(calls.append(("confirm", arguments)) or "done"),
        policy="confirm",
    ))
    return registry


def test_quick_action_loader_preserves_order_and_drops_invalid_entries_once(tmp_path, caplog):
    registry = _registry([])
    path = tmp_path / "quick_actions.yaml"
    path.write_text(
        """\
- label: First
  tool: instant_action
  args: {mode: safe}
- label: Unknown
  tool: missing_action
  args: {}
- label: Extra arg
  tool: instant_action
  args: {mode: safe, surprise: true}
- label: Confirm
  tool: confirm_action
  args: {target: report}
""",
        encoding="utf-8",
    )

    actions = load_quick_actions(path, registry)

    assert actions == [
        QuickAction("First", "instant_action", {"mode": "safe"}),
        QuickAction("Confirm", "confirm_action", {"target": "report"}),
    ]
    warnings = [record.message for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "2 invalid quick actions" in warnings[0]
    assert len(warnings[0]) <= 300


def test_quick_action_loader_applies_desktop_one_of_requirements(tmp_path, caplog):
    root = Path(__file__).resolve().parents[1]
    registry = ToolRegistry()
    builtin(registry, load_apps(root / "config" / "apps.yaml"), object())
    path = tmp_path / "quick_actions.yaml"
    path.write_text(
        """\
- label: Missing focus
  tool: focus_window
  args: {}
- label: Missing target
  tool: window_action
  args: {action: maximize}
""",
        encoding="utf-8",
    )

    assert load_quick_actions(path, registry) == []
    warnings = [record.message for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "2 invalid quick actions" in warnings[0]


def test_quick_action_loader_never_crashes_on_malformed_or_oversized_config(tmp_path, caplog):
    registry = _registry([])
    malformed = tmp_path / "malformed.yaml"
    malformed.write_text("- {label: broken", encoding="utf-8")
    oversized = tmp_path / "oversized.yaml"
    oversized.write_text(
        "\n".join(
            f"- {{label: Action {index}, tool: instant_action, args: {{mode: safe}}}}"
            for index in range(16)
        ),
        encoding="utf-8",
    )

    assert load_quick_actions(malformed, registry) == []
    malformed_warnings = [
        record.message for record in caplog.records if record.levelname == "WARNING"
    ]
    assert len(malformed_warnings) == 1
    assert "Dropped quick actions config" in malformed_warnings[0]
    caplog.clear()
    assert len(load_quick_actions(oversized, registry)) == 14
    assert all(len(record.message) <= 300 for record in caplog.records)


def test_shipped_quick_actions_all_match_the_builtin_registry():
    root = Path(__file__).resolve().parents[1]
    registry = ToolRegistry()
    builtin(registry, load_apps(root / "config" / "apps.yaml"), object())

    actions = load_quick_actions(root / "config" / "quick_actions.yaml", registry)

    assert len(actions) == 14
    assert [action.label for action in actions[:4]] == [
        "Spotify", "Windows", "Play / Pause", "Next Track",
    ]


def test_quick_action_endpoint_uses_registry_policy_and_action_authorization():
    calls = []
    results = []
    registry = _registry(calls)
    actions = [
        QuickAction("Instant", "instant_action", {"mode": "safe"}),
        QuickAction("Confirm", "confirm_action", {"target": "report"}),
    ]

    async def scenario():
        authorizer = stateserver.PairingAuthorizer(token="pair-token")
        bearer = authorizer.pair("pair-token")
        server = await stateserver.start(
            StatePublisher(),
            0,
            authorizer=authorizer,
            registry=registry,
            quick_actions=actions,
            quick_result_provider=lambda name, result: results.append((name, result)),
        )
        origin = f"http://127.0.0.1:{server.port}"
        base = {"content-type": "application/json", "origin": origin}
        authorized = {**base, stateserver.HEADER: bearer}
        try:
            state_url = f"http://127.0.0.1:{server.port}/state"
            async with aiohttp.ClientSession() as session:
                async with session.get(state_url) as response:
                    state_payload = await response.json()
            missing = await _request(server, "/actions/quick", body='{"index": 0}', headers=base)
            invalid = await _request(server, "/actions/quick", body='{"index": 9}', headers=authorized)
            instant = await _request(server, "/actions/quick", body='{"index": 0}', headers=authorized)
            confirm = await _request(server, "/actions/quick", body='{"index": 1}', headers=authorized)
            instant_while_pending = await _request(
                server, "/actions/quick", body='{"index": 0}', headers=authorized,
            )
            return state_payload, missing, invalid, instant, confirm, instant_while_pending
        finally:
            await server.stop()

    state_payload, missing, invalid, instant, confirm, instant_while_pending = asyncio.run(scenario())

    assert state_payload["quick_actions"] == [{"label": "Instant"}, {"label": "Confirm"}]
    assert missing[0] == 403
    assert invalid[0] == 400
    assert json.loads(instant[1]) == {"ok": True, "pending": None, "message": "done"}
    confirm_payload = json.loads(confirm[1])
    assert confirm_payload["ok"] is True
    assert confirm_payload["pending"] == {"readback": "confirm_action - target: report"}
    assert "readback" not in confirm_payload
    assert json.loads(instant_while_pending[1]) == {
        "ok": True,
        "pending": {"readback": "confirm_action - target: report"},
        "message": "done",
    }
    assert calls == [("instant", {"mode": "safe"}), ("instant", {"mode": "safe"})]
    assert registry.pending is not None
    assert [name for name, _result_value in results] == [
        "instant_action", "confirm_action", "instant_action",
    ]


def test_text_turn_endpoint_forwards_bounded_text_and_requires_action_authorization():
    turns = []

    async def scenario():
        authorizer = stateserver.PairingAuthorizer(token="pair-token")
        bearer = authorizer.pair("pair-token")
        server = await stateserver.start(
            StatePublisher(),
            0,
            authorizer=authorizer,
            text_turn_provider=lambda text: turns.append(text),
        )
        origin = f"http://127.0.0.1:{server.port}"
        base = {"content-type": "application/json", "origin": origin}
        authorized = {**base, stateserver.HEADER: bearer}
        try:
            missing = await _request(server, "/turn", body='{"text": "hello"}', headers=base)
            blank = await _request(server, "/turn", body='{"text": "  "}', headers=authorized)
            accepted = await _request(server, "/turn", body='{"text": "  hello Atlas  "}', headers=authorized)
            return missing, blank, accepted
        finally:
            await server.stop()

    missing, blank, accepted = asyncio.run(scenario())

    assert missing[0] == 403
    assert blank[0] == 400
    assert json.loads(accepted[1]) == {"ok": True, "pending": None}
    assert turns == ["hello Atlas"]


def test_text_turn_response_projects_existing_pending_confirmation():
    registry = _registry([])

    async def scenario():
        pending = await registry.call("confirm_action", {"target": "report"})
        assert pending.status == "needs_confirmation"
        authorizer = stateserver.PairingAuthorizer(token="pair-token")
        bearer = authorizer.pair("pair-token")
        server = await stateserver.start(
            StatePublisher(),
            0,
            authorizer=authorizer,
            registry=registry,
            text_turn_provider=lambda _text: None,
        )
        origin = f"http://127.0.0.1:{server.port}"
        headers = {
            "content-type": "application/json",
            "origin": origin,
            stateserver.HEADER: bearer,
        }
        try:
            return await _request(server, "/turn", body='{"text": "status"}', headers=headers)
        finally:
            await server.stop()

    response = asyncio.run(scenario())

    assert json.loads(response[1]) == {
        "ok": True,
        "pending": {"readback": "confirm_action - target: report"},
    }
