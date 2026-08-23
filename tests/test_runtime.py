"""Composition-root tests for the conversational and background work lanes."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from worker import runtime
from worker.brain import Brain
from worker.jobstore import JobStore
from worker.mcp_client import McpServers
from worker.tools import ToolRegistry
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
        "google_account": "owner@example.test",
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
            "count_mail",
        ]
        assert built.mcp.status() == [{
            "name": "demo", "connected": False, "tools": 0, "error": None,
        }]
        assert factory_calls == []
    finally:
        built.store.close()


def test_default_anthropic_client_is_built_on_first_turn_access(monkeypatch, tmp_path):
    root = _root(tmp_path)
    monkeypatch.setattr(runtime, "ATLAS", root)
    created = []
    messages = object()

    class Client:
        def __init__(self):
            self.messages = messages

    lazy_client = runtime._LazyAnthropicClient(
        factory=lambda: created.append(Client()) or created[-1],
    )
    built = runtime.build({
        "fast_model": "claude-test",
        "google_account": "owner@example.test",
        "job_store_path": ":memory:",
        "work_workspace_path": str(tmp_path / "jobs"),
    }, client=lazy_client, launcher=FakeLauncher())
    try:
        assert created == []
        assert built.brain.client.messages is messages
        assert len(created) == 1
        assert built.brain.client.messages is messages
        assert len(created) == 1
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


def test_build_passes_resolved_known_folders_to_work_manager(monkeypatch, tmp_path):
    root = _root(tmp_path)
    documents = tmp_path / "OneDrive" / "Documents"
    documents.mkdir(parents=True)
    monkeypatch.setattr(runtime, "ATLAS", root)
    original_local_files = runtime.LocalFiles

    def local_files(roots):
        return original_local_files(
            roots,
            known_folder_resolver=lambda name: documents if name == "Documents" else None,
        )

    monkeypatch.setattr(runtime, "LocalFiles", local_files)
    built = runtime.build({
        "fast_model": "claude-test",
        "google_account": "owner@example.test",
        "job_store_path": ":memory:",
        "work_workspace_path": str(tmp_path / "jobs"),
        "file_roots": ["known:Documents"],
    }, client=FakeClient(), launcher=FakeLauncher())
    try:
        assert built.work.folders == {"Documents": documents.resolve()}
    finally:
        built.store.close()


def test_build_registers_count_mail_before_google_connects_and_swaps_in_raw_search(
    monkeypatch,
    tmp_path,
):
    root = _root(tmp_path)
    monkeypatch.setattr(runtime, "ATLAS", root)

    class CapturingMcp:
        def __init__(self, _config, **kwargs):
            self.on_server = kwargs["on_server"]
            self.account_values = kwargs["account_values"]
            self.calls = []

        async def call_raw(self, server, tool, arguments):
            self.calls.append((server, tool, arguments))
            return "Found 3 messages matching 'in:inbox':"

    monkeypatch.setattr(runtime, "McpServers", CapturingMcp)
    built = runtime.build({
        "fast_model": "claude-test",
        "google_account": "owner@example.test",
        "job_store_path": ":memory:",
        "work_workspace_path": str(tmp_path / "jobs"),
    }, client=FakeClient(), launcher=FakeLauncher())

    try:
        disconnected = asyncio.run(
            built.registry.call("count_mail", {"query": "in:inbox"}),
        )

        assert disconnected.content == "Google isn't connected yet"
        built.mcp.on_server("demo", built.registry)
        assert built.registry.names().count("count_mail") == 1

        built.mcp.on_server("google", built.registry)
        result = asyncio.run(built.registry.call("count_mail", {"query": "in:inbox"}))

        assert json.loads(result.content) == {
            "query": "in:inbox",
            "count": 3,
            "exact": True,
        }
        assert built.mcp.calls == [(
            "google",
            "search_gmail_messages",
            {
                "query": "in:inbox",
                "page_size": 500,
                "include_headers": False,
            },
        )]
        assert built.mcp.account_values == {
            "user_google_email": "owner@example.test",
        }
    finally:
        built.store.close()
