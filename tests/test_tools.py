import asyncio
import json
from dataclasses import dataclass

import pytest

from worker.tools import AppEntry, Tool, ToolRegistry, builtin, load_apps


async def _return(value):
    return value


def _call(registry, name, arguments):
    return asyncio.run(registry.call(name, arguments))


def _tool(name="sample", *, run=None, policy="instant"):
    return Tool(
        name=name,
        description="Sample tool",
        input_schema={"type": "object", "properties": {}},
        run=run or (lambda _: _return({"answer": 42})),
        policy=policy,
    )


def test_register_schemas_and_duplicate_name():
    registry = ToolRegistry()
    registry.register(_tool())

    assert registry.names() == ["sample"]
    assert registry.schemas() == [{
        "name": "sample",
        "description": "Sample tool",
        "input_schema": {"type": "object", "properties": {}},
    }]
    with pytest.raises(ValueError, match="duplicate"):
        registry.register(_tool())


def test_instant_call_reports_success_exception_and_timeout():
    registry = ToolRegistry(timeout_s=0.05)

    async def fail(_):
        raise RuntimeError("sensitive details")

    async def slow(_):
        await asyncio.sleep(1)

    registry.register(_tool("ok"))
    registry.register(_tool("fail", run=fail))
    registry.register(_tool("slow", run=slow))

    result = _call(registry, "ok", {})
    assert result.status == "ok"
    assert json.loads(result.content) == {"answer": 42}
    assert _call(registry, "fail", {}).content == "RuntimeError"
    timeout = _call(registry, "slow", {})
    assert timeout.status == "error"
    assert timeout.content == "TimeoutError"
    assert _call(registry, "missing", {}).content == "unknown tool"


def test_confirm_is_single_use_and_executes_the_pending_tool():
    calls = []
    registry = ToolRegistry()
    registry.register(_tool("send", run=lambda args: _return(calls.append(args) or "sent"),
                            policy="confirm"))
    builtin(registry, {}, _FakeWork())

    pending = _call(registry, "send", {"to": "Daniel"})
    assert pending.status == "needs_confirmation"
    assert pending.confirm_id
    assert "send" in pending.content and "Daniel" in pending.content
    assert calls == []

    confirmed = _call(registry, "confirm", {"confirm_id": pending.confirm_id})
    assert confirmed.status == "ok"
    assert confirmed.content == "sent"
    assert calls == [{"to": "Daniel"}]
    second = _call(registry, "confirm", {"confirm_id": pending.confirm_id})
    assert second.status == "error"
    assert second.content == "nothing to confirm"


def test_confirm_executes_the_arguments_that_were_summarized():
    calls = []
    registry = ToolRegistry()
    registry.register(_tool("send", run=lambda args: _return(calls.append(args)), policy="confirm"))
    builtin(registry, {}, _FakeWork())
    arguments = {"message": {"body": "approved"}}

    pending = _call(registry, "send", arguments)
    arguments["message"]["body"] = "changed"
    assert _call(registry, "confirm", {"confirm_id": pending.confirm_id}).status == "ok"

    assert calls == [{"message": {"body": "approved"}}]


def test_confirm_expiry_replacement_and_cancel_pending():
    now = [10.0]
    calls = []
    registry = ToolRegistry(clock=lambda: now[0])
    registry.register(_tool("first", run=lambda args: _return(calls.append(("first", args))),
                            policy="confirm"))
    registry.register(_tool("second", run=lambda args: _return(calls.append(("second", args))),
                            policy="confirm"))
    builtin(registry, {}, _FakeWork())

    old = _call(registry, "first", {"n": 1})
    new = _call(registry, "second", {"n": 2})
    assert _call(registry, "confirm", {"confirm_id": old.confirm_id}).status == "error"
    assert _call(registry, "confirm", {"confirm_id": new.confirm_id}).status == "ok"
    assert calls == [("second", {"n": 2})]

    expired = _call(registry, "first", {"n": 3})
    now[0] += 121
    assert _call(registry, "confirm", {"confirm_id": expired.confirm_id}).content == "nothing to confirm"
    cancelled = _call(registry, "second", {"n": 4})
    assert _call(registry, "cancel_pending", {}).status == "ok"
    assert _call(registry, "confirm", {"confirm_id": cancelled.confirm_id}).content == "nothing to confirm"


def test_open_resolves_aliases_https_and_rejects_other_targets():
    opened = []
    registry = ToolRegistry()
    apps = {
        "gmail": AppEntry(url="https://mail.google.com/", words=("gmail", "email")),
    }
    builtin(registry, apps, _FakeWork(), opener=opened.append)

    result = _call(registry, "open", {"target": "Email"})
    assert result.status == "ok"
    assert json.loads(result.content) == {"opened": "gmail"}
    assert opened == ["https://mail.google.com/"]

    result = _call(registry, "open", {"target": "https://example.com/x"})
    assert json.loads(result.content) == {"opened": "https://example.com/x"}
    assert opened[-1] == "https://example.com/x"

    for target in ("http://example.com", "calc.exe", "unknown"):
        result = _call(registry, "open", {"target": target})
        assert result.status == "error"
        assert result.content == "unknown app"


def test_open_exe_and_focus_use_signed_profiles():
    opened = []
    focused = []
    registry = ToolRegistry()
    apps = {
        "vscode": AppEntry(exe="vscode", words=("vs code", "vscode", "editor")),
        "gmail": AppEntry(url="https://mail.google.com/", words=("gmail",)),
    }
    builtin(
        registry,
        apps,
        _FakeWork(),
        profile_opener=lambda app_id, url=None: opened.append((app_id, url)),
        profile_focuser=focused.append,
    )

    result = _call(registry, "open", {"target": "VS Code"})
    assert json.loads(result.content) == {"opened": "vscode"}
    assert opened == [("vscode", None)]
    result = _call(registry, "focus", {"app": "editor"})
    assert json.loads(result.content) == {"focused": "vscode"}
    assert focused == ["vscode"]
    result = _call(registry, "focus", {"app": "gmail"})
    assert result.status == "error" and result.content == "unknown app"


@dataclass
class _State:
    value: str


@dataclass
class _Job:
    job_id: str
    title: str
    state: _State
    created_at: float


class _FakeWork:
    def __init__(self):
        self.cancelled = []
        self.running = _Job("job-1", "Research", _State("running"), 12.5)
        self.done = _Job("job-2", "Finished", _State("succeeded"), 8.0)

    def launch(self, title, brief):
        assert brief == "Compare options"
        return _Job("job-new", title, _State("queued"), 20.0)

    def active(self):
        return [self.running]

    def recent(self, n):
        assert n == 5
        return [self.done]

    def cancel(self, job_id):
        self.cancelled.append(job_id)
        return _Job(job_id, "Research", _State("cancelled"), 12.5)


def test_work_builtins_launch_report_status_and_cancel():
    work = _FakeWork()
    registry = ToolRegistry()
    builtin(registry, {}, work)

    launched = _call(registry, "launch_work", {"title": "Compare", "brief": "Compare options"})
    assert json.loads(launched.content) == {
        "job_id": "job-new", "status": "launching", "title": "Compare",
    }
    status = json.loads(_call(registry, "work_status", {}).content)
    assert status == [
        {"job_id": "job-1", "title": "Research", "status": "running", "started_at": 12.5},
        {"job_id": "job-2", "title": "Finished", "status": "succeeded", "started_at": 8.0},
    ]
    cancelled = json.loads(_call(registry, "cancel_work", {"job_id": "job-1"}).content)
    assert cancelled == {"job_id": "job-1", "status": "cancelled", "title": "Research"}
    assert work.cancelled == ["job-1"]


def test_content_is_bounded_and_control_characters_are_stripped():
    registry = ToolRegistry()
    registry.register(_tool(run=lambda _: _return("a\x00b\x1fc" + "x" * 10_000)))

    result = _call(registry, "sample", {})

    assert "\x00" not in result.content and "\x1f" not in result.content
    assert len(result.content) == 4096
    assert result.content.endswith("…[truncated]")


def test_load_apps_reads_the_teachable_alias_config(tmp_path):
    path = tmp_path / "apps.yaml"
    path.write_text(
        "apps:\n  gmail: {url: 'https://mail.google.com/', words: [gmail, email]}\n"
        "  vscode: {exe: vscode, words: [vs code, editor]}\n",
        encoding="utf-8",
    )

    apps = load_apps(path)

    assert apps["gmail"] == AppEntry(url="https://mail.google.com/", words=("gmail", "email"))
    assert apps["vscode"] == AppEntry(exe="vscode", words=("vs code", "editor"))
