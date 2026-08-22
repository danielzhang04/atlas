"""Behavior tests for the streaming conversational brain."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from worker.brain import BASE_SYSTEM, Brain, split_spoken


@dataclass(frozen=True, slots=True)
class ToolResult:
    status: str
    content: str
    confirm_id: str | None = None


class FakeRegistry:
    def __init__(self, *results: ToolResult) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.when_called = None

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

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        if self.when_called is not None:
            self.when_called()
        self.calls.append((name, dict(arguments)))
        return self.results.pop(0) if self.results else ToolResult("ok", "done")


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


def test_confirmation_status_and_id_survive_for_the_later_confirm_turn():
    registry = FakeRegistry(
        ToolResult("needs_confirmation", "mutate {\"message\": \"hello\"}", "confirm-123"),
        ToolResult("ok", "sent"),
    )
    client = FakeClient(
        FakeStream(content=[tool_block(name="mutate")], stop_reason="tool_use"),
        FakeStream(["Should I send it?"], content=[text_block("Should I send it?")]),
        FakeStream(
            content=[tool_block(name="confirm", arguments={"confirm_id": "confirm-123"})],
            stop_reason="tool_use",
        ),
        FakeStream(["Sent."], content=[text_block("Sent.")]),
    )
    brain = Brain(client, registry, model="fast", persona="")

    async def scenario():
        first = await collect(brain, "Send the message")
        second = await collect(brain, "Yes")
        return first, second

    first, second = asyncio.run(scenario())

    assert first == ["Should I send it?"]
    assert second == ["Sent."]
    tool_result = client.messages.calls[1]["messages"][-1]["content"][0]
    assert tool_result["content"] == (
        'needs_confirmation (confirm_id: confirm-123): mutate {"message": "hello"}'
    )
    assert "Host pending confirmation id: confirm-123." in client.messages.calls[2]["system"][0]["text"]
    assert brain._pending_confirm_id is None


def test_confirmation_cannot_execute_in_the_turn_that_created_it():
    registry = FakeRegistry(
        ToolResult("needs_confirmation", "mutate {}", "confirm-123"),
    )
    events = []
    client = FakeClient(
        FakeStream(content=[tool_block(name="mutate")], stop_reason="tool_use"),
        FakeStream(
            content=[tool_block(name="confirm", arguments={"confirm_id": "confirm-123"})],
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
    assert registry.calls == [("mutate", {})]
    assert events == [("mutate", "needs_confirmation"), ("confirm", "error")]
    assert brain._pending_confirm_id == "confirm-123"


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


def test_google_account_rule_is_conditional():
    no_account = Brain(FakeClient(), FakeRegistry(), model="fast", persona="")
    account = Brain(
        FakeClient(), FakeRegistry(), model="fast", persona="", google_account="daniel@example.com",
    )

    assert "user_google_email" not in no_account._system_text
    assert "Google tools need user_google_email = daniel@example.com." in account._system_text


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
