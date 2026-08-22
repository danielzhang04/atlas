"""Production-path guards for the three-lane Atlas integration."""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest
from livekit.agents import StopResponse

from worker.app import AtlasAgent, _stt_keyterms

ROOT = Path(__file__).resolve().parents[1]
REMOVED = {
    "actionauth", "actionbroker", "browser_protocol", "browser_transport", "capabilities",
    "capability_runner", "connectors", "contracts", "frontdesk", "guided_setup", "localfiles",
    "receipts", "routing_policy", "turn_interpreter", "voice_frontdesk", "voice_runtime",
}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    imported.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    imported.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "worker"
        for alias in node.names
    )
    return imported


def test_voice_worker_imports_new_lanes_and_removed_modules_are_absent():
    imports = _imports(ROOT / "worker" / "app.py")
    assert {"brain", "mcp_client", "tools", "work"} <= imports
    assert all(not (ROOT / "worker" / f"{name}.py").exists() for name in REMOVED)
    assert not (ROOT / "browser_bridge").exists()


def test_production_config_has_only_revamp_composition_keys():
    config = (ROOT / "config" / "atlas.yaml").read_text(encoding="utf-8")
    for key in ("google_account:", "work_workspace_path:", "turn_timeout_s:", "max_tokens:"):
        assert key in config
    for removed in (
        "local_file_roots", "desktop_target_aliases", "browser_bridge_url",
        "google_broker_endpoint", "receipt_journal_path", "agentic_workspace_path",
        "subscription_health_path", "interpreter_timeout_s",
    ):
        assert removed not in config


def test_agent_always_stops_after_the_host_turn_handler():
    seen = []
    agent = AtlasAgent(instructions="host controlled", llm=None, tools=[])

    async def handler(text):
        seen.append(text)

    agent.turn_handler = handler
    message = type("Message", (), {"text_content": "hello"})()
    with pytest.raises(StopResponse):
        asyncio.run(agent.on_user_turn_completed(None, message))
    assert seen == ["hello"]


def test_stt_keyterms_are_bounded():
    assert _stt_keyterms({}) == ["Atlas"]
    assert _stt_keyterms({"stt_keyterms": ["Atlas", "Calendar"]}) == ["Atlas", "Calendar"]
    assert _stt_keyterms({"stt_keyterms": ["x" * 65]}) == ["Atlas"]
