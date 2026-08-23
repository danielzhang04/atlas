"""Process-tree protection is installed before worker startup work."""
from __future__ import annotations

import argparse
import asyncio

import pytest

from worker import app, chat


class StartupStopped(Exception):
    pass


def test_app_entrypoint_assigns_current_process_before_environment_loading(monkeypatch):
    calls = []
    monkeypatch.setattr(
        app.jobobject,
        "assign_current_process",
        lambda: calls.append("assign"),
    )

    def stop_during_environment_load():
        calls.append("environment")
        raise StartupStopped

    monkeypatch.setattr(app.envload, "load_private_environment", stop_during_environment_load)

    with pytest.raises(StartupStopped):
        asyncio.run(app.entrypoint(None))

    assert calls == ["assign", "environment"]


def test_app_main_assigns_current_process_before_configuration(monkeypatch):
    calls = []
    monkeypatch.setattr(
        app.jobobject,
        "assign_current_process",
        lambda: calls.append("assign"),
    )
    monkeypatch.setattr(app, "_cfg", lambda: calls.append("config") or {})
    monkeypatch.setattr(app.cli, "run_app", lambda _options: calls.append("run"))
    monkeypatch.setattr(app.sys, "argv", ["worker.app"])

    assert app.main() == 0
    assert calls == ["assign", "config", "run"]


def test_chat_main_assigns_current_process_before_argument_parsing(monkeypatch):
    calls = []
    monkeypatch.setattr(
        chat.jobobject,
        "assign_current_process",
        lambda: calls.append("assign"),
    )
    monkeypatch.setattr(
        chat,
        "_arguments",
        lambda: calls.append("arguments") or argparse.Namespace(
            utterance="hello",
            no_mcp=True,
        ),
    )

    async def fake_run(utterance, *, no_mcp):
        calls.append((utterance, no_mcp))

    monkeypatch.setattr(chat, "run", fake_run)

    assert chat.main() == 0
    assert calls == ["assign", "arguments", ("hello", True)]
