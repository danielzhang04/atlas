"""Composition-root tests for the conversational and background work lanes."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from worker import runtime
from worker.brain import Brain
from worker.jobstore import JobStore
from worker.mcp_client import McpServers
from worker.tools import Tool, ToolRegistry
from worker.work import WorkManager


class FakeClient:
    pass


class FakeLauncher:
    available = True


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "atlas"
    config = root / "config"
    config.mkdir(parents=True)
    (config / "apps.yaml").write_text(
        "apps:\n  gmail: {url: 'https://mail.google.com/', words: [gmail]}\n",
        encoding="utf-8",
    )
    (config / "mcp.yaml").write_text(
        "servers:\n  demo: {command: demo}\ndefaults: {connect_timeout_s: 1}\n",
        encoding="utf-8",
    )
    (config / "persona.md").write_text("Dry and concise.", encoding="utf-8")
    return root


def test_build_composes_every_lane_without_connecting_or_launching(monkeypatch, tmp_path):
    root = _root(tmp_path)
    monkeypatch.setattr(runtime, "ATLAS", root)
    factory_calls = []

    def session_factory(*args):
        factory_calls.append(args)
        raise AssertionError("build must not connect MCP")

    client = FakeClient()
    launcher = FakeLauncher()
    built = runtime.build({
        "fast_model": "claude-test",
        "google_account": "daniel@example.com",
        "job_store_path": ":memory:",
        "work_workspace_path": str(tmp_path / "jobs"),
        "turn_timeout_s": 3,
        "max_tokens": 123,
        "file_roots": [str(tmp_path)],
    }, client=client, launcher=launcher, session_factory=session_factory)
    try:
        assert isinstance(built.registry, ToolRegistry)
        assert isinstance(built.mcp, McpServers)
        assert isinstance(built.work, WorkManager)
        assert isinstance(built.brain, Brain)
        assert isinstance(built.store, JobStore)
        assert built.work.launcher is launcher
        assert built.brain.client is client
        assert built.brain.model == "claude-test"
        assert built.brain.max_tokens == 123
        assert built.brain.turn_timeout_s == 3
        assert built.registry.names() == [
            "open", "focus", "confirm", "cancel_pending",
            "launch_work", "work_status", "cancel_work", "close",
            "find_file", "open_file", "read_file",
        ]
        assert built.mcp.status() == [{
            "name": "demo", "connected": False, "tools": 0, "error": None,
        }]
        assert factory_calls == []
    finally:
        built.store.close()


def test_build_requires_the_small_trusted_configuration(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "ATLAS", _root(tmp_path))
    try:
        runtime.build({"job_store_path": ":memory:"}, client=FakeClient(), launcher=FakeLauncher())
    except ValueError as exc:
        assert "configuration" in str(exc)
    else:
        raise AssertionError("missing composition settings must fail at the config boundary")


def test_build_retains_a_google_connection_hook_for_count_mail(monkeypatch, tmp_path):
    root = _root(tmp_path)
    monkeypatch.setattr(runtime, "ATLAS", root)

    class CapturingMcp:
        def __init__(self, _config, **kwargs):
            self.on_server = kwargs["on_server"]

    monkeypatch.setattr(runtime, "McpServers", CapturingMcp)
    built = runtime.build({
        "fast_model": "claude-test",
        "google_account": "daniel@example.com",
        "job_store_path": ":memory:",
        "work_workspace_path": str(tmp_path / "jobs"),
    }, client=FakeClient(), launcher=FakeLauncher())

    async def search(_arguments):
        return "Found 3 messages matching 'in:inbox':"

    try:
        built.registry.register(Tool(
            name="google__search_gmail_messages",
            description="Search Gmail.",
            input_schema={"type": "object", "properties": {}},
            run=search,
        ))
        built.mcp.on_server("demo", built.registry)
        assert "count_mail" not in built.registry.names()

        built.mcp.on_server("google", built.registry)
        result = asyncio.run(built.registry.call("count_mail", {"query": "in:inbox"}))

        assert json.loads(result.content) == {
            "query": "in:inbox",
            "count": 3,
            "exact": True,
        }
    finally:
        built.store.close()
