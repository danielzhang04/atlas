"""Behavior tests for the streaming conversational brain."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from worker.brain import BASE_SYSTEM, Brain, split_spoken
from worker.tools import AppEntry, PendingAction, Tool, ToolRegistry, builtin


@dataclass(frozen=True, slots=True)
class ToolResult:
    status: str
    content: str
    confirm_id: str | None = None


class FakeRegistry:
    def __init__(self, *results: ToolResult) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.taints: list[bool] = []
        self.transcripts: list[str | None] = []
        self.when_called = None
        self._pending: PendingAction | None = None

    @property
    def pending(self) -> PendingAction | None:
        return self._pending

    def schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "lookup",
                "description": "Look something up.",
                "input_schema": {"type": "object", "properties": {}},
            },
            {
                "name": "mutate",
                "description": "Change something.",
                "input_schema": {"type": "object", "properties": {}},
            },
        ]

    async def call(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        tainted: bool = False,
        transcript: str | None = None,
    ) -> ToolResult:
        if self.when_called is not None:
            self.when_called()
        self.calls.append((name, dict(arguments)))
        self.taints.append(tainted)
        self.transcripts.append(transcript)
        result = self.results.pop(0) if self.results else ToolResult("ok", "done")
        if result.status == "needs_confirmation" and result.confirm_id is not None:
            self._pending = PendingAction(
                confirm_id=result.confirm_id,
                name=name,
                arguments=dict(arguments),
                summary=name,
                expires=float("inf"),
            )
        elif name in {"confirm", "cancel_pending"}:
            self._pending = None
        return result

    async def confirm(self, confirm_id: str) -> ToolResult:
        return await self.call("confirm", {"confirm_id": confirm_id})

    def cancel_pending(self) -> ToolResult:
        self.calls.append(("cancel_pending", {}))
        self._pending = None
        return self.results.pop(0) if self.results else ToolResult("ok", "cancelled")


class FakeStream:
    def __init__(self, deltas=(), *, content=(), stop_reason="end_turn", delay=0.0) -> None:
        self.deltas = list(deltas)
        self.final = SimpleNamespace(content=list(content), stop_reason=stop_reason)
        self.delay = delay

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    @property
    def text_stream(self):
        async def iterate():
            for delta in self.deltas:
                if self.delay:
                    await asyncio.sleep(self.delay)
                yield delta

        return iterate()

    async def get_final_message(self):
        return self.final


class ErrorStream:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def __aenter__(self):
        raise self.error

    async def __aexit__(self, *_args):
        return False


class FakeMessages:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("unexpected model call")
        response = self.responses.pop(0)
        return ErrorStream(response) if isinstance(response, Exception) else response


class FakeClient:
    def __init__(self, *responses) -> None:
        self.messages = FakeMessages(responses)


def text_block(text: str):
    return SimpleNamespace(type="text", text=text)


def tool_block(call_id="toolu_1", name="lookup", arguments=None):
    return SimpleNamespace(type="tool_use", id=call_id, name=name, input=arguments or {})


async def collect(brain: Brain, transcript: str) -> list[str]:
    return [chunk async for chunk in brain.respond(transcript)]


async def return_value(value):
    return value


def registry_tool(name, run, *, policy="instant"):
    return Tool(
        name=name,
        description="Test tool.",
        input_schema={"type": "object", "properties": {}},
        run=run,
        policy=policy,
    )


class BrainWork:
    def __init__(self):
        self.launches = []

    def launch(self, title, brief):
        self.launches.append((title, brief))
        return SimpleNamespace(job_id="job-1", title=title)

    def active(self):
        return []

    def recent(self, _n):
        return []

    def cancel(self, job_id):
        return SimpleNamespace(job_id=job_id, title="Task", state="cancelled")


def test_plain_reply_streams_chunks_and_remembers_exchange():
    client = FakeClient(FakeStream(
        ["First sentence. Sec", "ond sentence! Tail"],
        content=[text_block("First sentence. Second sentence! Tail")],
    ))
    local_timezone = datetime.now().astimezone().tzinfo
    clock = lambda: datetime(2026, 8, 22, 9, 41, 29, tzinfo=local_timezone)
    now = clock().astimezone()
    brain = Brain(client, FakeRegistry(), model="fast", persona="Dry and composed.", clock=clock)

    chunks = asyncio.run(collect(brain, "Hello"))

    assert chunks == ["First sentence. ", "Second sentence! ", "Tail"]
    assert brain._history == [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "First sentence. Second sentence! Tail"},
    ]
    call = client.messages.calls[0]
    assert call["system"] == [
        {
            "type": "text",
            "text": BASE_SYSTEM + "\n\nVoice and personality:\nDry and composed.",
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": f"Now: {now.isoformat(timespec='minutes')} ({now.tzname()}). Daniel is in this timezone.",
        },
    ]
    assert "cache_control" not in call["system"][1]
    assert call["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in call["tools"][0]
    assert call["tool_choice"] == {"type": "auto"}


def test_tool_use_continues_with_result_and_invokes_callback():
    result = ToolResult("error", "TimeoutError")
    registry = FakeRegistry(result)
    events = []
    first_content = [text_block("Checking now. "), tool_block(arguments={"day": "today"})]
    client = FakeClient(
        FakeStream(["Checking now. "], content=first_content, stop_reason="tool_use"),
        FakeStream(["Nothing came back."], content=[text_block("Nothing came back.")]),
    )
    brain = Brain(
        client, registry, model="fast", persona="", on_tool=lambda name, value: events.append((name, value)),
    )

    chunks = asyncio.run(collect(brain, "What's today?"))

    assert chunks == ["Checking now. ", "Nothing came back."]
    assert registry.calls == [("lookup", {"day": "today"})]
    assert events == [("lookup", result)]
    continuation = client.messages.calls[1]["messages"]
    assert continuation[-2] == {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Checking now. "},
            {"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {"day": "today"}},
        ],
    }
    assert continuation[-1] == {"role": "user", "content": [{
        "type": "tool_result",
        "tool_use_id": "toolu_1",
        "content": "TimeoutError",
        "is_error": True,
    }]}


def test_closed_affirmative_with_action_words_executes_pending(monkeypatch):
    mutation_calls = []
    registry = ToolRegistry()
    registry.register(registry_tool(
        "google__draft_gmail_message",
        lambda arguments: return_value(mutation_calls.append(arguments) or "sent"),
        policy="confirm",
    ))
    monkeypatch.setattr(
        "worker.tools.secrets.token_urlsafe",
        lambda _length: "confirm-123",
    )
    asyncio.run(registry.call(
        "google__draft_gmail_message",
        {"recipient": "daniel@example.test"},
    ))
    events = []
    client = FakeClient(FakeStream(["Drafted."], content=[text_block("Drafted.")]))
    brain = Brain(
        client,
        registry,
        model="fast",
        persona="",
        on_tool=lambda name, result: events.append((name, result)),
    )

    result = asyncio.run(collect(brain, "yes go ahead and create the draft"))

    assert result == ["Drafted."]
    assert mutation_calls == [{"recipient": "daniel@example.test"}]
    assert registry.pending is None
    assert [name for name, _result in events] == ["confirm"]
    assert brain._history == [
        {"role": "user", "content": "yes go ahead and create the draft"},
        {
            "role": "assistant",
            "content": "Done — google__draft_gmail_message executed.",
        },
    ]
    request = client.messages.calls[0]
    assert request["tool_choice"] == {"type": "none"}
    assert request["messages"] == [{
        "role": "user",
        "content": "yes go ahead and create the draft",
    }]
    assert "Done — google__draft_gmail_message executed." in request["system"][-1]["text"]


def test_bare_confirm_executes_pending():
    mutation_calls = []
    registry = ToolRegistry()
    registry.register(registry_tool(
        "mutate",
        lambda arguments: return_value(mutation_calls.append(arguments) or "done"),
        policy="confirm",
    ))
    asyncio.run(registry.call("mutate", {"message": "hello"}))
    client = FakeClient(FakeStream(["Done."], content=[text_block("Done.")]))
    brain = Brain(client, registry, model="fast", persona="")

    assert asyncio.run(collect(brain, "confirm")) == ["Done."]
    assert mutation_calls == [{"message": "hello"}]
    assert registry.pending is None


def test_closed_affirmative_allows_normalized_argument_key_words():
    mutation_calls = []
    registry = ToolRegistry()
    registry.register(registry_tool(
        "mutate",
        lambda arguments: return_value(mutation_calls.append(arguments) or "done"),
        policy="confirm",
    ))
    arguments = {"recipient_email": "daniel@example.test"}
    asyncio.run(registry.call("mutate", arguments))
    client = FakeClient(FakeStream(["Done."], content=[text_block("Done.")]))
    brain = Brain(client, registry, model="fast", persona="")

    result = asyncio.run(collect(brain, "yes send to my recipient email"))

    assert result == ["Done."]
    assert mutation_calls == [arguments]


def test_closed_negative_cancels_pending_without_executing():
    mutation_calls = []
    registry = ToolRegistry()
    registry.register(registry_tool(
        "mutate",
        lambda arguments: return_value(mutation_calls.append(arguments) or "sent"),
        policy="confirm",
    ))
    asyncio.run(registry.call("mutate", {"message": "hello"}))
    client = FakeClient(FakeStream(["Cancelled."], content=[text_block("Cancelled.")]))
    brain = Brain(client, registry, model="fast", persona="")

    assert asyncio.run(collect(brain, "no never mind")) == ["Cancelled."]

    assert mutation_calls == []
    assert registry.pending is None
    request = client.messages.calls[0]
    assert request["tool_choice"] == {"type": "none"}
    assert request["messages"] == [{"role": "user", "content": "no never mind"}]
    assert brain._history[-1] == {"role": "assistant", "content": "Cancelled."}


def test_affirmative_with_extra_content_is_a_normal_turn_without_execution():
    mutation_calls = []
    registry = ToolRegistry()
    registry.register(registry_tool(
        "mutate",
        lambda arguments: return_value(mutation_calls.append(arguments) or "sent"),
        policy="confirm",
    ))
    pending = asyncio.run(registry.call("mutate", {"message": "hello"}))
    client = FakeClient(FakeStream(
        ["That sounds good."],
        content=[text_block("That sounds good.")],
    ))
    brain = Brain(client, registry, model="fast", persona="")

    assert asyncio.run(collect(brain, "yes it was great")) == ["That sounds good."]

    assert mutation_calls == []
    assert registry.pending is not None
    assert registry.pending.confirm_id == pending.confirm_id
    assert client.messages.calls[0]["tool_choice"] == {"type": "auto"}
    assert client.messages.calls[0]["messages"] == [{
        "role": "user",
        "content": "yes it was great",
    }]


def test_negative_with_extra_content_is_a_normal_turn_without_cancelling():
    registry = ToolRegistry()
    registry.register(registry_tool(
        "mutate",
        lambda _arguments: return_value("sent"),
        policy="confirm",
    ))
    pending = asyncio.run(registry.call("mutate", {"message": "hello"}))
    client = FakeClient(FakeStream(
        ["Which one?"],
        content=[text_block("Which one?")],
    ))
    brain = Brain(client, registry, model="fast", persona="")

    assert asyncio.run(collect(brain, "not that one")) == ["Which one?"]
    assert registry.pending is not None
    assert registry.pending.confirm_id == pending.confirm_id
    assert client.messages.calls[0]["tool_choice"] == {"type": "auto"}


def test_expired_pending_is_ignored_by_affirmative_turn():
    now = [10.0]
    mutation_calls = []
    registry = ToolRegistry(clock=lambda: now[0])
    registry.register(registry_tool(
        "mutate",
        lambda arguments: return_value(mutation_calls.append(arguments) or "sent"),
        policy="confirm",
    ))
    builtin(registry, {}, BrainWork())
    asyncio.run(registry.call("mutate", {"message": "hello"}))
    now[0] += 121.0
    client = FakeClient(FakeStream(
        ["There is nothing pending."],
        content=[text_block("There is nothing pending.")],
    ))
    brain = Brain(client, registry, model="fast", persona="")

    assert asyncio.run(collect(brain, "Yes")) == ["There is nothing pending."]

    assert mutation_calls == []
    assert registry.pending is None
    assert client.messages.calls[0]["tool_choice"] == {"type": "auto"}


def test_confirm_narration_failure_returns_and_remembers_deterministic_host_line():
    mutation_calls = []
    registry = ToolRegistry()
    registry.register(registry_tool(
        "mutate",
        lambda arguments: return_value(mutation_calls.append(arguments) or "done"),
        policy="confirm",
    ))
    asyncio.run(registry.call("mutate", {"message": "hello"}))
    client = FakeClient(RuntimeError("narration failed"))
    brain = Brain(client, registry, model="fast", persona="")

    result = asyncio.run(collect(brain, "confirm"))

    assert result == ["Done — mutate executed."]
    assert mutation_calls == [{"message": "hello"}]
    assert brain._history == [
        {"role": "user", "content": "confirm"},
        {"role": "assistant", "content": "Done — mutate executed."},
    ]


def test_cancel_narration_timeout_returns_and_remembers_deterministic_host_line():
    registry = ToolRegistry()
    registry.register(registry_tool(
        "mutate",
        lambda _arguments: return_value("done"),
        policy="confirm",
    ))
    asyncio.run(registry.call("mutate", {}))
    client = FakeClient(FakeStream(
        ["Too late."],
        content=[text_block("Too late.")],
        delay=0.05,
    ))
    brain = Brain(
        client,
        registry,
        model="fast",
        persona="",
        turn_timeout_s=0.01,
    )

    result = asyncio.run(collect(brain, "no never mind"))

    assert result == ["Cancelled."]
    assert brain._history == [
        {"role": "user", "content": "no never mind"},
        {"role": "assistant", "content": "Cancelled."},
    ]


def test_model_issued_confirmation_is_host_only_and_cannot_consume_new_pending():
    mutation_calls = []
    registry = ToolRegistry()
    registry.register(registry_tool(
        "mutate",
        lambda arguments: return_value(mutation_calls.append(arguments) or "sent"),
        policy="confirm",
    ))
    events = []
    client = FakeClient(
        FakeStream(content=[tool_block(name="mutate")], stop_reason="tool_use"),
        FakeStream(
            content=[tool_block(name="confirm", arguments={"confirm_id": "invented"})],
            stop_reason="tool_use",
        ),
        FakeStream(["Please confirm first."], content=[text_block("Please confirm first.")]),
    )
    brain = Brain(
        client,
        registry,
        model="fast",
        persona="",
        on_tool=lambda name, result: events.append((name, result.status)),
    )

    assert asyncio.run(collect(brain, "Send it")) == ["Please confirm first."]
    assert mutation_calls == []
    assert registry.pending is not None
    assert events == [("mutate", "needs_confirmation"), ("confirm", "error")]
    refusal = client.messages.calls[2]["messages"][-1]["content"][0]
    assert refusal["content"] == "host-only"
    assert "confirm" not in {
        tool["name"]
        for tool in client.messages.calls[0]["tools"]
    }
    assert "cancel_pending" not in {
        tool["name"]
        for tool in client.messages.calls[0]["tools"]
    }


def test_model_confirmation_after_external_content_is_host_only_and_pending_survives(
    monkeypatch,
):
    mutation_calls = []
    registry = ToolRegistry()
    registry.register(registry_tool(
        "mutate",
        lambda arguments: return_value(mutation_calls.append(arguments) or "sent"),
        policy="confirm",
    ))
    registry.register(registry_tool(
        "google__read",
        lambda _arguments: return_value("external content"),
    ))
    builtin(registry, {}, BrainWork())
    monkeypatch.setattr(
        "worker.tools.secrets.token_urlsafe",
        lambda _length: "confirm-123",
    )
    client = FakeClient(
        FakeStream(content=[tool_block(name="mutate")], stop_reason="tool_use"),
        FakeStream(["Should I send it?"], content=[text_block("Should I send it?")]),
        FakeStream(content=[tool_block(name="google__read")], stop_reason="tool_use"),
        FakeStream(
            content=[tool_block(name="confirm", arguments={"confirm_id": "confirm-123"})],
            stop_reason="tool_use",
        ),
        FakeStream(["Ask again next turn."], content=[text_block("Ask again next turn.")]),
        FakeStream(["Sent."], content=[text_block("Sent.")]),
    )
    brain = Brain(client, registry, model="fast", persona="")

    async def scenario():
        first = await collect(brain, "Send it")
        second = await collect(brain, "Check mail, then yes")
        third = await collect(brain, "Yes, send it")
        return first, second, third

    first, second, third = asyncio.run(scenario())

    refusal = client.messages.calls[4]["messages"][-1]["content"][0]
    assert first == ["Should I send it?"]
    assert second == ["Ask again next turn."]
    assert refusal["content"] == "host-only"
    assert mutation_calls == [{}]
    assert third == ["Sent."]


def test_mcp_result_allows_launch_work_with_the_exact_turn_transcript():
    work = BrainWork()
    registry = ToolRegistry()
    registry.register(registry_tool(
        "google__read",
        lambda _arguments: return_value("external content"),
    ))
    builtin(registry, {}, work)
    client = FakeClient(
        FakeStream(content=[tool_block(name="google__read")], stop_reason="tool_use"),
        FakeStream(
            content=[tool_block(
                name="launch_work",
                arguments={"title": "Research", "brief": "Do the work"},
            )],
            stop_reason="tool_use",
        ),
        FakeStream(["Launched."], content=[text_block("Launched.")]),
    )
    brain = Brain(client, registry, model="fast", persona="")
    transcript = "  Read this and research it  "

    assert asyncio.run(collect(brain, transcript)) == ["Launched."]
    launch_result = client.messages.calls[2]["messages"][-1]["content"][0]
    assert launch_result["is_error"] is False
    assert work.launches == [
        (
            "Research",
            f"{transcript}\n\n"
            "(Atlas: content read during this turn was not forwarded.)",
        ),
    ]


def test_brain_passes_the_exact_transcript_to_each_registry_call():
    registry = FakeRegistry(ToolResult("ok", "done"))
    client = FakeClient(
        FakeStream(content=[tool_block()], stop_reason="tool_use"),
        FakeStream(["Done."], content=[text_block("Done.")]),
    )
    brain = Brain(client, registry, model="fast", persona="")
    transcript = "  Keep my spacing  "

    assert asyncio.run(collect(brain, transcript)) == ["Done."]
    assert registry.transcripts == [transcript]


@pytest.mark.parametrize("content_tool", ["google__read", "read_file"])
def test_content_bearing_tools_taint_every_later_call_in_the_turn(content_tool):
    registry = FakeRegistry(ToolResult("ok", "content"), ToolResult("ok", "done"))
    client = FakeClient(
        FakeStream(content=[tool_block(name=content_tool)], stop_reason="tool_use"),
        FakeStream(content=[tool_block(name="lookup")], stop_reason="tool_use"),
        FakeStream(["Done."], content=[text_block("Done.")]),
    )
    brain = Brain(client, registry, model="fast", persona="")

    assert asyncio.run(collect(brain, "Check, then act")) == ["Done."]
    assert registry.taints == [False, True]


@pytest.mark.parametrize(
    ("first_tool", "later_tool"),
    [("find_file", "open_file"), ("count_mail", "open")],
)
def test_metadata_tools_do_not_taint_later_calls_in_the_turn(first_tool, later_tool):
    registry = FakeRegistry(ToolResult("ok", "metadata"), ToolResult("ok", "done"))
    client = FakeClient(
        FakeStream(content=[tool_block(name=first_tool)], stop_reason="tool_use"),
        FakeStream(content=[tool_block(name=later_tool)], stop_reason="tool_use"),
        FakeStream(["Done."], content=[text_block("Done.")]),
    )
    brain = Brain(client, registry, model="fast", persona="")

    assert asyncio.run(collect(brain, "Check, then open it")) == ["Done."]
    assert registry.calls == [(first_tool, {}), (later_tool, {})]
    assert registry.taints == [False, False]


def test_content_result_refuses_a_later_action_in_the_same_tool_batch():
    closed = []
    registry = ToolRegistry()
    registry.register(registry_tool(
        "google__read",
        lambda _arguments: return_value("external content"),
    ))
    builtin(
        registry,
        {"vscode": AppEntry(exe="vscode", words=("editor",))},
        BrainWork(),
        profile_closer=closed.append,
    )
    client = FakeClient(
        FakeStream(
            content=[
                tool_block(call_id="read", name="google__read"),
                tool_block(call_id="close", name="close", arguments={"app": "editor"}),
            ],
            stop_reason="tool_use",
        ),
        FakeStream(["Ask again."], content=[text_block("Ask again.")]),
    )
    brain = Brain(client, registry, model="fast", persona="")

    assert asyncio.run(collect(brain, "Read and close")) == ["Ask again."]
    results = client.messages.calls[1]["messages"][-1]["content"]
    assert results[1]["content"] == (
        "refused after external content; ask Daniel again next turn"
    )
    assert closed == []


def test_mcp_result_refuses_later_https_open_but_allows_configured_alias():
    opened = []
    registry = ToolRegistry()
    registry.register(registry_tool(
        "google__read",
        lambda _arguments: return_value("external content"),
    ))
    builtin(
        registry,
        {"gmail": AppEntry(url="https://mail.google.com/", words=("gmail",))},
        BrainWork(),
        opener=opened.append,
    )
    client = FakeClient(
        FakeStream(content=[tool_block(name="google__read")], stop_reason="tool_use"),
        FakeStream(
            content=[
                tool_block(
                    call_id="toolu_url",
                    name="open",
                    arguments={"target": "https://example.com/"},
                ),
                tool_block(
                    call_id="toolu_alias",
                    name="open",
                    arguments={"target": "gmail"},
                ),
            ],
            stop_reason="tool_use",
        ),
        FakeStream(["Gmail is open."], content=[text_block("Gmail is open.")]),
    )
    brain = Brain(client, registry, model="fast", persona="")

    assert asyncio.run(collect(brain, "Read this, open the link and Gmail")) == [
        "Gmail is open."
    ]
    results = client.messages.calls[2]["messages"][-1]["content"]
    assert results[0]["content"] == (
        "refused after external content; ask Daniel again next turn"
    )
    assert results[1]["is_error"] is False
    assert opened == ["https://mail.google.com/"]


def test_streamed_text_is_yielded_before_tool_runs():
    seen: list[str] = []
    registry = FakeRegistry(ToolResult("ok", "result"))
    registry.when_called = lambda: seen.append("called")
    client = FakeClient(
        FakeStream(["Let me check. "], content=[tool_block()], stop_reason="tool_use"),
        FakeStream(["Done."], content=[text_block("Done.")]),
    )
    brain = Brain(client, registry, model="fast", persona="")

    async def scenario():
        iterator = brain.respond("check")
        first = await anext(iterator)
        assert seen == []
        rest = [chunk async for chunk in iterator]
        return first, rest

    first, rest = asyncio.run(scenario())
    assert first == "Let me check. "
    assert rest == ["Done."]
    assert seen == ["called"]


def test_fifth_model_round_disables_tools():
    responses = [FakeStream(content=[tool_block(f"toolu_{index}")], stop_reason="tool_use")
                 for index in range(1, 5)]
    responses.append(FakeStream(["Finished."], content=[text_block("Finished.")]))
    client = FakeClient(*responses)

    assert asyncio.run(collect(Brain(client, FakeRegistry(), model="fast", persona=""), "loop")) == [
        "Finished."
    ]
    assert [call["tool_choice"] for call in client.messages.calls] == [
        {"type": "auto"}, {"type": "auto"}, {"type": "auto"}, {"type": "auto"},
        {"type": "none"},
    ]


def test_timeout_returns_only_fixed_sentence():
    client = FakeClient(FakeStream(["late"], content=[text_block("late")], delay=0.05))
    brain = Brain(client, FakeRegistry(), model="fast", persona="", turn_timeout_s=0.01)

    assert asyncio.run(collect(brain, "hello")) == ["I lost that one to a timeout. Still here."]
    assert brain._history == []


def test_provider_exception_is_sanitized(caplog):
    class ProviderFailure(RuntimeError):
        status_code = 500

    client = FakeClient(ProviderFailure("private token must not escape"))
    brain = Brain(client, FakeRegistry(), model="fast", persona="")

    assert asyncio.run(collect(brain, "hello")) == ["I couldn't reach my model just now. Still here."]
    assert "status=500" in caplog.text
    assert "private token" not in caplog.text
    assert brain._history == []


def test_history_is_limited_to_configured_exchanges():
    client = FakeClient(*[
        FakeStream([f"Answer {index}."], content=[text_block(f"Answer {index}.")])
        for index in range(3)
    ])
    brain = Brain(client, FakeRegistry(), model="fast", persona="", history_exchanges=2)

    async def scenario():
        for index in range(3):
            await collect(brain, f"Question {index}")

    asyncio.run(scenario())

    assert brain._history == [
        {"role": "user", "content": "Question 1"},
        {"role": "assistant", "content": "Answer 1."},
        {"role": "user", "content": "Question 2"},
        {"role": "assistant", "content": "Answer 2."},
    ]
    assert client.messages.calls[2]["messages"][:2] == [
        {"role": "user", "content": "Question 0"},
        {"role": "assistant", "content": "Answer 0."},
    ]


def test_transcript_is_bounded():
    brain = Brain(FakeClient(), FakeRegistry(), model="fast", persona="")
    with pytest.raises(ValueError):
        asyncio.run(collect(brain, "x" * 4_097))


def test_base_system_routes_file_analysis_and_mail_counts_to_the_safe_tools():
    assert "find_file" in BASE_SYSTEM
    assert "read_file" in BASE_SYSTEM
    assert "analysis that needs code or produces artifacts" in BASE_SYSTEM
    assert "count_mail" in BASE_SYSTEM
    assert "never count from a search page" in BASE_SYSTEM
    assert "summarize, sum, or analyze a truncated read_file result" in BASE_SYSTEM
    assert "closes every window" in BASE_SYSTEM
    assert "reading or acting inside a web page, or Chrome, uses launch_work" in BASE_SYSTEM
    assert "unless the tool result for that call" in BASE_SYSTEM
    assert "do not narrate between tool calls" in BASE_SYSTEM
    assert "read every summary field back" in BASE_SYSTEM
    assert "host alone confirms or cancels" in BASE_SYSTEM
    assert "Do not call a confirmation tool" in BASE_SYSTEM
    assert "user_google_email" not in BASE_SYSTEM


def test_split_spoken_uses_sentence_newline_and_length_boundaries():
    chunks, remainder = split_spoken("Short. This is a complete sentence!\nTrailing")
    assert chunks == ["Short. This is a complete sentence!\n"]
    assert remainder == "Trailing"

    long = "word " * 40 + "tail"
    chunks, remainder = split_spoken(long)
    assert len(chunks) == 1
    assert len(chunks[0]) <= 160
    assert chunks[0].endswith(" ")
    assert remainder
