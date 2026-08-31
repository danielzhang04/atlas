"""Offline prompt-regression checks: no model calls, safe for CI.

Asserts on the exact system text and tool schemas Atlas sends the model --
the prompt-tiering language (which tools are instant, alias-vs-URL guidance)
and the honest, bounded `open` schema from AA2. The recorder-swapped
registry proves these are the *real* production schemas, not a hand-rolled
stand-in, without ever touching the desktop.

The companion live A/B script (scripts/prompt_ab.py) drives real utterances
through the actual model with the same recorders; that one spends API budget
and is not run here.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tests.promptharness import ATLAS, Recorder, build_recording_registry, build_system_text
from worker.tools import AppEntry, ToolRegistry, builtin


def test_system_text_states_tool_tiering_and_alias_vs_url_guidance():
    text = build_system_text()

    # Sentence A: which tools are instant vs. host-confirmed -- the
    # needs_confirmation readback rule (asserted separately below) still
    # applies exactly as written; this line must not read as overriding it.
    assert (
        "Every tool is instant except press_delete\n"
        "and mutating kb/MCP actions; for those, the host runs its own confirmation."
    ) in text
    # Sentence B: instant tools get no permission-asking, no offering.
    assert (
        "For instant tools, never\n"
        "ask permission and never offer to do something you can just do -- act, "
        "then say what you did."
    ) in text
    # The new "already" result field: nothing new happened, don't claim you just did it.
    assert (
        'A tool result with "already": true means nothing new happened -- say it is already '
        "open; never say\nyou just opened it."
    ) in text
    assert "opens the real desktop app when configured" in text
    assert "a URL only opens a web page -- prefer the alias" in text
    # The needs_confirmation readback rule is untouched by the split above.
    assert (
        "A tool result of needs_confirmation means to read every summary field back "
        "in one sentence and ask"
    ) in text


def test_persona_no_longer_licenses_asking_before_a_tool_call():
    persona = (ATLAS / "config" / "persona.md").read_text(encoding="utf-8")

    assert "act without narrating steps or asking first" in persona
    assert "never to ask" in persona
    assert "permission for a tool call you're already able to make" in persona


def test_open_schema_carries_the_real_sorted_alias_vocabulary():
    registry, _recorder = build_recording_registry()

    schemas = {schema["name"]: schema for schema in registry.schemas()}
    description = schemas["open"]["description"]

    assert description.startswith("Open an allowlisted app or HTTPS URL.")
    assert "Aliases open the real desktop app when configured:" in description
    names_text = description.split("configured:", 1)[1].rstrip(".")
    names = [name.strip() for name in names_text.split(",")]
    assert names == sorted(names)
    assert "gmail" in names
    assert "spotify" in names
    # bounded: a name list, not the full (much longer) alias-word vocabulary
    assert "google drive" not in description
    assert "command center" not in description


def test_open_schema_description_is_bounded_for_a_large_app_roster():
    apps = {
        f"app{i}": AppEntry(url=f"https://example.com/{i}/", words=(f"app{i}",))
        for i in range(200)
    }
    registry = ToolRegistry()
    builtin(registry, apps, _DummyWork())

    description = next(
        schema["description"] for schema in registry.schemas() if schema["name"] == "open"
    )

    assert len(description) < 2_500
    assert "..." in description


def test_recorders_capture_open_and_focus_without_touching_the_desktop():
    registry, recorder = build_recording_registry()

    opened = _call(registry, "open", {"target": "gmail"})
    focused = _call(registry, "focus", {"app": "chrome"})

    assert json.loads(opened.content)["opened"] == "gmail"
    assert json.loads(opened.content)["via"] == "web"
    assert json.loads(focused.content) == {"focused": "chrome"}
    assert ("open_web", "https://mail.google.com/") in recorder.calls
    assert ("focus", "chrome") in recorder.calls


class _DummyWork:
    def launch(self, title, brief):
        raise AssertionError("not exercised")

    def active(self):
        return []

    def recent(self, _n):
        return []

    def cancel(self, job_id):
        raise AssertionError("not exercised")


def _call(registry, name, arguments):
    import asyncio

    return asyncio.run(registry.call(name, arguments))


def test_prompt_ab_desktop_stub_records_instead_of_acting(monkeypatch):
    # F3: the script's "nothing actually launches" claim must be enforced, not
    # asserted. The stub replaces worker.desktopcontrol in sys.modules, so the
    # lazy importlib lookup inside worker/tools.py resolves to it.
    import importlib
    import sys

    from scripts import prompt_ab

    monkeypatch.setitem(
        sys.modules, "worker.desktopcontrol",
        importlib.import_module("worker.desktopcontrol"),
    )
    recorder = Recorder()
    stub = prompt_ab.install_desktop_stub(recorder)

    assert importlib.import_module("worker.desktopcontrol") is stub
    assert stub.media_key("play_pause") == {"stubbed": True}
    assert stub.normalize_chord(" Ctrl + C ") == "ctrl+c"
    with pytest.raises(AttributeError):
        stub.some_future_key_presser
    assert ("desktopcontrol.media_key", ("play_pause",), {}) in recorder.calls


def test_prompt_ab_stubs_launch_work_and_the_shell_opening_file_tools():
    # F3: launch_work is real paid background work; open_file/open_folder shell
    # out. The probe script must be unable to reach any of them.
    registry, recorder = build_recording_registry()
    from scripts import prompt_ab

    prompt_ab.stub_side_effecting_tools(registry, recorder)
    launched = _call(registry, "launch_work", {"title": "t", "brief": "b"})

    assert json.loads(launched.content) == {
        "stubbed": "launch_work", "arguments": {"title": "t", "brief": "b"},
    }
    assert ("tool:launch_work", {"title": "t", "brief": "b"}) in recorder.calls
    assert recorder.calls.count(("launch_work", "t", "b")) == 0

    # The file tools exist only when file_roots is configured (as it is in the
    # real config the script loads), so cover that shape too.
    def _never(*_args, **_kwargs):
        raise AssertionError("real file tool reached")

    with_files = ToolRegistry()
    builtin(
        with_files, {"gmail": AppEntry(url="https://mail.test/", words=("gmail",))},
        _DummyWork(), opener=lambda _url: None,
        files=SimpleNamespace(
            find=_never, open=_never, open_folder=_never, read_file=_never, folders={},
        ),
    )
    prompt_ab.stub_side_effecting_tools(with_files, recorder)
    opened = _call(with_files, "open_folder", {"path": "C:/Users/danie/kb"})

    assert json.loads(opened.content)["stubbed"] == "open_folder"


def test_prompt_ab_refuses_to_probe_when_a_stubbed_tool_is_missing():
    registry = ToolRegistry()
    builtin(registry, {"gmail": AppEntry(url="https://mail.test/", words=("gmail",))},
            _DummyWork(), opener=lambda _url: None)
    registry.unregister("launch_work")

    with pytest.raises(SystemExit):
        from scripts import prompt_ab
        prompt_ab.stub_side_effecting_tools(registry, Recorder())
