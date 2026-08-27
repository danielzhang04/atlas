"""Composition-root tests for the conversational and background work lanes."""
from __future__ import annotations

import asyncio
import builtins
import json
from pathlib import Path
import threading

import pytest

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
        "turn_ceiling_s": 9,
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
        assert built.brain.turn_ceiling_s == 9
        assert built.registry.names() == [
            "open", "focus", "launch_work", "work_status", "cancel_work", "close",
            "find_file", "open_file", "open_folder", "read_file",
            "list_windows", "focus_window", "window_action", "media_key", "click",
            "type_text", "press_keys", "press_delete",
            "count_mail",
        ]
        assert built.mcp.status() == [{
            "name": "demo", "connected": False, "tools": 0, "error": None,
        }]
        assert factory_calls == []
    finally:
        built.store.close()


def test_lazy_anthropic_client_warm_starts_one_background_thread(monkeypatch):
    real_thread = threading.Thread
    constructed = []
    started = []

    class FakeThread:
        def __init__(self, **kwargs):
            constructed.append(kwargs)
            threading.Event().wait(timeout=0.05)

        def start(self):
            started.append(self)

    monkeypatch.setattr(runtime.threading, "Thread", FakeThread)
    lazy_client = runtime._LazyAnthropicClient(factory=FakeClient)
    barrier = threading.Barrier(3)

    def warm_concurrently():
        barrier.wait()
        lazy_client.warm()

    callers = [real_thread(target=warm_concurrently) for _ in range(2)]
    for caller in callers:
        caller.start()
    barrier.wait()
    for caller in callers:
        caller.join(timeout=1.0)
    lazy_client.warm()

    assert all(not caller.is_alive() for caller in callers)
    assert len(constructed) == 1
    assert len(started) == 1


def test_runtime_warm_model_client_delegates_without_mutating_runtime(monkeypatch, tmp_path):
    root = _root(tmp_path)
    monkeypatch.setattr(runtime, "ATLAS", root)

    class RecordingClient:
        def __init__(self):
            self.warm_calls = 0

        def warm(self):
            self.warm_calls += 1

    client = RecordingClient()
    built = runtime.build({
        "fast_model": "claude-test",
        "google_account": "owner@example.test",
        "job_store_path": ":memory:",
        "work_workspace_path": str(tmp_path / "jobs"),
    }, client=client, launcher=FakeLauncher())
    before = (built.registry, built.mcp, built.work, built.brain, built.store)
    try:
        built.warm_model_client()
        built.warm_model_client()

        assert client.warm_calls == 2
        assert (built.registry, built.mcp, built.work, built.brain, built.store) == before
    finally:
        built.store.close()


def test_lazy_anthropic_client_constructs_on_turn_before_warm(monkeypatch):
    messages = object()

    class Client:
        def __init__(self):
            self.messages = messages

    class ForbiddenThread:
        def __init__(self, *args, **kwargs):
            raise AssertionError("a turn before warm must stay on the lazy path")

    monkeypatch.setattr(runtime.threading, "Thread", ForbiddenThread)
    lazy_client = runtime._LazyAnthropicClient(factory=Client)

    assert lazy_client.messages is messages


def test_build_does_not_import_anthropic_or_start_warm_thread(monkeypatch, tmp_path):
    root = _root(tmp_path)
    monkeypatch.setattr(runtime, "ATLAS", root)
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "anthropic" or name.startswith("anthropic."):
            raise AssertionError("build must not import anthropic")
        return real_import(name, *args, **kwargs)

    class ForbiddenThread:
        def __init__(self, *args, **kwargs):
            raise AssertionError("build must not create the warm-up thread")

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(runtime.threading, "Thread", ForbiddenThread)
    built = runtime.build({
        "fast_model": "claude-test",
        "google_account": "owner@example.test",
        "job_store_path": ":memory:",
        "work_workspace_path": str(tmp_path / "jobs"),
    }, launcher=FakeLauncher())
    built.store.close()


def test_build_rejects_turn_ceiling_below_turn_timeout(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "ATLAS", _root(tmp_path))

    try:
        runtime.build({
            "fast_model": "claude-test",
            "google_account": "owner@example.test",
            "job_store_path": ":memory:",
            "work_workspace_path": str(tmp_path / "jobs"),
            "turn_timeout_s": 5,
            "turn_ceiling_s": 4,
        }, client=FakeClient(), launcher=FakeLauncher())
    except ValueError as exc:
        assert "turn_ceiling_s" in str(exc)
    else:
        raise AssertionError("turn ceiling below per-attempt timeout must fail")


@pytest.mark.parametrize("ceiling", [float("inf"), float("nan"), -1, 0, "30"])
def test_build_rejects_invalid_turn_ceiling(monkeypatch, tmp_path, ceiling):
    monkeypatch.setattr(runtime, "ATLAS", _root(tmp_path))

    with pytest.raises(ValueError, match="turn_ceiling_s"):
        runtime.build({
            "fast_model": "claude-test",
            "google_account": "owner@example.test",
            "job_store_path": ":memory:",
            "work_workspace_path": str(tmp_path / "jobs"),
            "turn_ceiling_s": ceiling,
        }, client=FakeClient(), launcher=FakeLauncher())


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
            self.responses = [
                "Found 61 messages matching 'in:inbox':\n1. first",
                "Found 14 messages matching 'in:inbox category:primary':\n1. first",
            ]

        async def call_raw(self, server, tool, arguments):
            self.calls.append((server, tool, arguments))
            return self.responses.pop(0)

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

        assert result.content == "61 in your inbox, 14 in Primary"
        assert built.mcp.calls == [
            (
                "google",
                "search_gmail_messages",
                {
                    "query": "in:inbox",
                    "page_size": 500,
                    "include_headers": False,
                },
            ),
            (
                "google",
                "search_gmail_messages",
                {
                    "query": "in:inbox category:primary",
                    "page_size": 500,
                    "include_headers": False,
                },
            ),
        ]
        assert built.mcp.account_values == {
            "user_google_email": "owner@example.test",
        }
    finally:
        built.store.close()
