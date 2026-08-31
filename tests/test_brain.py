"""Behavior tests for the streaming conversational brain."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from worker import brain as brain_mod
from worker.brain import BASE_SYSTEM, PROVIDER_REPLY, TIMEOUT_REPLY, Brain, split_spoken
from worker.claims import UNBACKED_ACTION_REPLY, ClaimGuard
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
    def __init__(
        self,
        deltas=(),
        *,
        content=(),
        stop_reason="end_turn",
        delay=0.0,
        usage=None,
    ) -> None:
        self.deltas = list(deltas)
        self.final = SimpleNamespace(content=list(content), stop_reason=stop_reason, usage=usage)
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
    def __init__(self, responses, token_counts=(4_096,)) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.token_counts = list(token_counts)
        self.count_calls: list[dict[str, Any]] = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("unexpected model call")
        response = self.responses.pop(0)
        return ErrorStream(response) if isinstance(response, Exception) else response

    async def count_tokens(self, **kwargs):
        self.count_calls.append(kwargs)
        result = self.token_counts.pop(0)
        if isinstance(result, Exception):
            raise result
        return SimpleNamespace(input_tokens=result)


class FakeClient:
    def __init__(self, *responses, token_counts=(4_096,)) -> None:
        self.messages = FakeMessages(responses, token_counts)


class DelayedFinalStream(FakeStream):
    def __init__(self, *args, final_delay, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.final_delay = final_delay

    async def get_final_message(self):
        await asyncio.sleep(self.final_delay)
        return self.final


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
    brain = Brain(
        client, FakeRegistry(), model="fast", persona="Dry and composed.", clock=clock,
        cache_ttl="1h",
    )
    brain.mark_tools_settled()

    async def scenario():
        chunks = await collect(brain, "Hello")
        await asyncio.sleep(0)
        return chunks

    chunks = asyncio.run(scenario())

    assert chunks == ["First sentence. ", "Second sentence! ", "Tail"]
    assert brain._history == [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "First sentence. Second sentence! Tail"},
    ]
    call = client.messages.calls[0]
    assert call["system"][0]["type"] == "text"
    assert call["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert call["system"][0]["text"].startswith(
        BASE_SYSTEM + "\n\nVoice and personality:\nDry and composed."
    )
    assert "lookup" in call["system"][0]["text"]
    assert "mutate" in call["system"][0]["text"]
    assert "Look something up." not in call["system"][0]["text"]
    assert call["system"][1] == {
        "type": "text",
        "text": f"Now: {now.isoformat(timespec='minutes')} ({now.tzname()}). Daniel is in this timezone.",
    }
    assert "cache_control" not in call["system"][1]
    assert call["tools"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert "cache_control" not in call["tools"][0]
    assert call["tool_choice"] == {"type": "auto"}
    assert client.messages.count_calls == [{
        "model": "fast",
        "system": [call["system"][0]],
        "tools": call["tools"],
        "messages": [],
    }]


@pytest.mark.parametrize(
    ("tokens", "expected", "warning"),
    [
        (4_095, False, "prompt cache floor unmet: 4095 tokens"),
        (4_096, True, None),
    ],
)
def test_first_turn_checks_cache_floor_once_without_blocking(tokens, expected, warning, caplog):
    client = FakeClient(
        FakeStream(["First."], content=[text_block("First.")]),
        FakeStream(["Second."], content=[text_block("Second.")]),
        token_counts=(tokens,),
    )
    brain = Brain(client, FakeRegistry(), model="fast", persona="")
    brain.mark_tools_settled()

    async def scenario():
        first = await collect(brain, "First turn")
        second = await collect(brain, "Second turn")
        await asyncio.sleep(0)
        return first, second

    assert asyncio.run(scenario()) == (["First."], ["Second."])
    assert brain.cache_floor_ok is expected
    assert len(client.messages.count_calls) == 1
    if warning is None:
        assert "prompt cache floor unmet" not in caplog.text
    else:
        assert caplog.text.count(warning) == 1


def test_cache_floor_failure_is_logged_once_and_remains_unknown(caplog):
    client = FakeClient(
        FakeStream(["First."], content=[text_block("First.")]),
        FakeStream(["Second."], content=[text_block("Second.")]),
        token_counts=(RuntimeError("must-not-escape"),),
    )
    brain = Brain(client, FakeRegistry(), model="fast", persona="")
    brain.mark_tools_settled()

    async def scenario():
        await collect(brain, "First turn")
        await collect(brain, "Second turn")
        await asyncio.sleep(0)

    asyncio.run(scenario())

    assert brain.cache_floor_ok is None
    assert len(client.messages.count_calls) == 1
    assert caplog.text.count("prompt cache floor check failed: RuntimeError") == 1
    assert "must-not-escape" not in caplog.text


def test_cache_floor_waits_for_first_turn_and_settle_then_rearms_per_snapshot():
    registry = FakeRegistry()
    client = FakeClient(
        FakeStream(["Before settle."], content=[text_block("Before settle.")]),
        token_counts=(4_096, 4_095, 4_096),
    )
    brain = Brain(client, registry, model="fast", persona="")

    async def scenario():
        await collect(brain, "Early turn")
        await asyncio.sleep(0)
        assert client.messages.count_calls == []

        brain.mark_tools_settled()
        await asyncio.sleep(0)
        assert len(client.messages.count_calls) == 1

        original = registry.schemas()
        registry.schemas = lambda: [
            *original,
            {
                "name": "second_generation",
                "description": "Second generation.",
                "input_schema": {"type": "object", "properties": {}},
            },
        ]
        assert brain.refresh_tools() is True
        assert brain.refresh_tools() is False
        await asyncio.sleep(0)
        assert len(client.messages.count_calls) == 2

        registry.schemas = lambda: [
            *original,
            {
                "name": "third_generation",
                "description": "Third generation.",
                "input_schema": {"type": "object", "properties": {}},
            },
        ]
        assert brain.refresh_tools() is True
        await asyncio.sleep(0)
        assert len(client.messages.count_calls) == 3

        registry.schemas = lambda: [
            *original,
            {
                "name": "fourth_generation",
                "description": "Fourth generation.",
                "input_schema": {"type": "object", "properties": {}},
            },
        ]
        assert brain.refresh_tools() is True
        await asyncio.sleep(0)

    asyncio.run(scenario())

    assert len(client.messages.count_calls) == 3
    assert brain.cache_floor_ok is None


def test_cache_floor_count_never_blocks_the_first_stream_chunk():
    release = asyncio.Event()
    started = asyncio.Event()
    client = FakeClient(FakeStream(
        ["Immediate response."],
        content=[text_block("Immediate response.")],
    ))
    brain = Brain(client, FakeRegistry(), model="fast", persona="")
    original_count_tokens = client.messages.count_tokens

    async def gated_count_tokens(**kwargs):
        started.set()
        await release.wait()
        return await original_count_tokens(**kwargs)

    client.messages.count_tokens = gated_count_tokens
    brain.mark_tools_settled()

    async def scenario():
        response = brain.respond("Hello")
        first = await asyncio.wait_for(anext(response), timeout=0.1)
        assert first == "Immediate response."
        assert release.is_set() is False
        await asyncio.sleep(0)
        assert started.is_set() is True
        assert release.is_set() is False
        release.set()
        await asyncio.sleep(0)
        await response.aclose()

    asyncio.run(scenario())


def test_prompt_snapshot_rebuilds_only_when_registered_tool_name_set_changes(caplog):
    caplog.set_level("INFO", logger="atlas.brain")
    registry = FakeRegistry()
    client = FakeClient(FakeStream(["Done."], content=[text_block("Done.")]))
    brain = Brain(client, registry, model="fast", persona="")
    original_tools = brain._request_tools()

    assert brain.refresh_tools() is False
    registry.schemas = lambda: [
        {**schema, "description": "Reconnected description."}
        for schema in original_tools
    ]
    assert brain.refresh_tools() is False
    assert brain._request_tools() == original_tools

    registry.schemas = lambda: [
        *original_tools,
        {
            "name": "new_tool",
            "description": "New capability.",
            "input_schema": {"type": "object", "properties": {}},
        },
    ]
    assert brain.refresh_tools() is True
    assert [tool["name"] for tool in brain._request_tools()] == ["lookup", "mutate", "new_tool"]
    assert caplog.text.count("brain prompt snapshot rebuilt") == 1


def test_tool_snapshot_excludes_api_incompatible_tool_but_keeps_it_registered(caplog):
    """Regression test for the tools.11.custom.input_schema 400.

    api_incompatible_tool_names only detected a top-level oneOf/allOf/anyOf;
    it did not stop it from being sent to the model, so a third-party (e.g.
    remote MCP) tool with that shape would still 400 every conversational
    turn. _replace_tool_snapshot must exclude just that tool from what is
    sent to the model -- and from the capability-text tool listing -- while
    leaving it registered and directly callable through the registry.
    """
    caplog.set_level("WARNING", logger="atlas.brain")

    async def run(_arguments):
        return {"ok": True}

    registry = ToolRegistry()
    registry.register(Tool(
        "good_tool", "A normal tool.", {"type": "object", "properties": {}}, run,
    ))
    registry.register(Tool(
        "bad_tool",
        "A tool with an API-incompatible schema.",
        {
            "type": "object",
            "properties": {},
            "oneOf": [{"required": ["a"]}, {"required": ["b"]}],
        },
        run,
    ))

    brain = Brain(FakeClient(), registry, model="fast", persona="")

    outbound_names = {tool["name"] for tool in brain._request_tools()}
    assert outbound_names == {"good_tool"}
    assert "good_tool" in brain._capability_text
    assert "bad_tool" not in brain._capability_text

    assert set(registry.names()) == {"good_tool", "bad_tool"}
    assert asyncio.run(registry.call("bad_tool", {})).status == "ok"

    assert caplog.text.count(
        "tool schema uses API-incompatible shape, excluded from model (tools=bad_tool)"
    ) == 1


def test_usage_records_cache_read_and_creation_tokens(caplog):
    caplog.set_level("INFO", logger="atlas.brain")
    usage = SimpleNamespace(
        input_tokens=5_100,
        output_tokens=23,
        cache_read_input_tokens=4_000,
        cache_creation_input_tokens=1_100,
    )
    client = FakeClient(FakeStream(["Okay."], content=[text_block("Okay.")], usage=usage))
    brain = Brain(client, FakeRegistry(), model="fast", persona="")

    assert asyncio.run(collect(brain, "Hello")) == ["Okay."]
    assert brain.last_usage == {
        "input_tokens": 5_100,
        "output_tokens": 23,
        "cache_read_input_tokens": 4_000,
        "cache_creation_input_tokens": 1_100,
    }
    assert "cache_read_input_tokens=4000" in caplog.text
    assert "cache_creation_input_tokens=1100" in caplog.text


@pytest.mark.parametrize("claim", ["I opened Spotify.", "I've opened Spotify."])
def test_unbacked_action_claim_is_suppressed_by_the_host(caplog, claim):
    client = FakeClient(FakeStream(
        [claim],
        content=[text_block(claim)],
    ))
    brain = Brain(client, FakeRegistry(), model="fast", persona="")

    assert "".join(asyncio.run(collect(brain, "Open Spotify"))) == (
        "I did not actually do that - I have no tool result. Want me to? "
    )
    assert caplog.text.count("unbacked action claim suppressed") == 1


def test_action_claim_after_ok_tool_result_passes_through():
    registry = FakeRegistry(ToolResult("ok", "opened"))
    client = FakeClient(
        FakeStream(
            content=[tool_block(name="open", arguments={"target": "spotify"})],
            stop_reason="tool_use",
        ),
        FakeStream(["Spotify is open."], content=[text_block("Spotify is open.")]),
    )
    brain = Brain(client, registry, model="fast", persona="")

    assert asyncio.run(collect(brain, "Open Spotify")) == ["Spotify is open."]


def test_known_registry_target_state_claim_requires_a_relevant_call():
    # The guard is restricted to perfective, first-person claims (item 3): a bare
    # state description like "Spotify is open." has no "I"/"we" subject, so it is
    # not an attributed claim even when the target is a known registry alias --
    # the dead open_state/target-tracking machinery that used to intercept this
    # sentence has been removed.
    registry = ToolRegistry()
    builtin(
        registry,
        {"spotify": AppEntry(url="https://spotify.test/", words=("spotify", "music"))},
        BrainWork(),
        opener=lambda _target: None,
    )
    reply = "Spotify is open."
    client = FakeClient(FakeStream([reply], content=[text_block(reply)]))
    brain = Brain(client, registry, model="fast", persona="")

    assert asyncio.run(collect(brain, "Is Spotify open?")) == [reply]


def test_unrelated_ok_read_does_not_license_failed_open_claim():
    registry = FakeRegistry(
        ToolResult("ok", "read result"),
        ToolResult("error", "could not open"),
    )
    client = FakeClient(
        FakeStream(content=[tool_block(name="read_file")], stop_reason="tool_use"),
        FakeStream(
            content=[tool_block(name="open", arguments={"target": "spotify"})],
            stop_reason="tool_use",
        ),
        FakeStream(["I opened Spotify."], content=[text_block("I opened Spotify.")]),
    )
    brain = Brain(client, registry, model="fast", persona="")

    assert asyncio.run(collect(brain, "Read the note, then open Spotify")) == [
        "I did not actually do that - I have no tool result. Want me to? "
    ]


def test_cancel_pending_does_not_license_open_claim():
    registry = FakeRegistry(ToolResult("ok", "cancelled"))
    registry._pending = PendingAction(
        confirm_id="confirm-123",
        name="open",
        arguments={"target": "spotify"},
        summary="open Spotify",
        expires=float("inf"),
    )
    client = FakeClient(
        FakeStream(["I opened Spotify."], content=[text_block("I opened Spotify.")]),
    )
    brain = Brain(client, registry, model="fast", persona="")

    assert asyncio.run(collect(brain, "no")) == [
        "I did not actually do that - I have no tool result. Want me to? "
    ]


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (
            "No problem, I opened Spotify.",
            "I did not actually do that - I have no tool result. Want me to? ",
        ),
        ("I did not open Spotify.", "I did not open Spotify."),
        (
            "I couldn't open X, but I opened Y.",
            "I did not actually do that - I have no tool result. Want me to? ",
        ),
        ("I couldn't open Spotify.", "I couldn't open Spotify."),
        (
            "The task is done.",
            "I did not actually do that - I have no tool result. Want me to? ",
        ),
    ],
)
def test_action_claim_negation_is_clause_local(reply, expected):
    client = FakeClient(FakeStream([reply], content=[text_block(reply)]))
    brain = Brain(client, FakeRegistry(), model="fast", persona="")

    assert "".join(asyncio.run(collect(brain, "Help me"))) == expected


def test_done_glued_to_a_short_sentence_is_still_delayed_and_substituted():
    # Item 1 fix: _sentence_end (brain.py) refuses a boundary on "Done." (5
    # stripped chars, under the 12-char minimum), so it glues to the next
    # sentence inside the same chunk. delayed() must match _PATTERNS["done"]
    # per sentence within the chunk, not against the whole (now longer)
    # chunk -- otherwise this glued "Done." is never held and streams
    # verbatim after a failed tool call.
    registry = FakeRegistry(ToolResult("error", "could not open"))
    client = FakeClient(
        FakeStream(
            content=[tool_block(name="open", arguments={"target": "spotify"})],
            stop_reason="tool_use",
        ),
        FakeStream(
            ["Done. I will keep an eye on that for you."],
            content=[text_block("Done. I will keep an eye on that for you.")],
        ),
    )
    brain = Brain(client, registry, model="fast", persona="")

    assert asyncio.run(collect(brain, "Open Spotify")) == [
        "I did not actually do that - I have no tool result. Want me to? "
    ]


@pytest.mark.parametrize(
    "reply",
    [
        "Spotify was created in 2006.",
        "The store is open today.",
        "This project is open source.",
        "The question is open-ended.",
    ],
)
def test_factual_action_words_are_not_treated_as_attributed_claims(reply):
    client = FakeClient(FakeStream([reply], content=[text_block(reply)]))
    brain = Brain(client, FakeRegistry(), model="fast", persona="")

    assert asyncio.run(collect(brain, "Tell me a fact")) == [reply]


@pytest.mark.parametrize("reply", ["Spotify has many playlists.", "Would you like a playlist?"])
def test_non_action_answers_and_questions_are_not_changed(reply):
    client = FakeClient(FakeStream([reply], content=[text_block(reply)]))
    brain = Brain(client, FakeRegistry(), model="fast", persona="")

    assert asyncio.run(collect(brain, "Tell me about Spotify")) == [reply]


def test_registry_capabilities_are_named_in_the_system_prompt():
    registry = ToolRegistry()
    registry.register(registry_tool(
        "open_folder",
        lambda _arguments: return_value({"opened": "C:/Users/danie/kb"}),
    ))
    client = FakeClient(
        FakeStream(content=[tool_block(name="open_folder")], stop_reason="tool_use"),
        FakeStream(["The folder is open."], content=[text_block("The folder is open.")]),
    )
    brain = Brain(
        client,
        registry,
        model="fast",
        persona="",
        mcp_status=[{
            "name": "google", "state": "not_configured",
            "detail": "private detail must not enter the prompt",
        }],
    )

    assert asyncio.run(collect(brain, "open my kb folder")) == ["The folder is open."]
    system_text = client.messages.calls[0]["system"][0]["text"]
    assert "open_folder" in system_text
    assert "google: not_configured" in system_text
    assert "private detail" not in system_text
    assert "Before saying you cannot do something, check this list" in system_text


def test_capability_prefix_changes_only_when_explicitly_refreshed():
    registry = ToolRegistry()
    client = FakeClient(
        FakeStream(["First."], content=[text_block("First.")]),
        FakeStream(["Second."], content=[text_block("Second.")]),
        FakeStream(["Third."], content=[text_block("Third.")]),
    )
    brain = Brain(
        client, registry, model="fast", persona="",
        mcp_status=[{"name": "google", "state": "connecting"}],
    )

    asyncio.run(collect(brain, "one"))
    registry.register(registry_tool("google__search", lambda _arguments: return_value({})))
    asyncio.run(collect(brain, "two"))
    brain.refresh_capabilities([{"name": "google", "state": "connected"}])
    asyncio.run(collect(brain, "three"))

    prefixes = [call["system"][0]["text"] for call in client.messages.calls]
    assert prefixes[0] == prefixes[1]
    assert prefixes[2] != prefixes[1]
    assert "google__search" in prefixes[2]
    assert "google: connected" in prefixes[2]


def test_capability_text_is_stable_across_snapshot_permutations():
    schemas = [{"name": "zeta"}, {"name": "alpha"}, {"name": "middle"}]
    states = [
        {"name": "zeta", "state": "error"},
        {"name": "alpha", "state": "connected"},
        {"name": "middle", "state": "not_configured"},
    ]

    expected = brain_mod._capability_system_text(schemas, states)

    assert brain_mod._capability_system_text(list(reversed(schemas)), states[1:] + states[:1]) == expected


def test_refusal_sentence_streams_through_undelayed_and_unchanged(caplog):
    # The host must NEVER fabricate "I can do that with X - shall I?" (item 1):
    # the capability-refusal substitution is gone, so a refusal-shaped
    # sentence is not an action claim, is never held by guard.delayed(), and
    # streams through verbatim -- even when a matching tool is registered.
    # Item 7: detection is kept (markers + route check against registered
    # tool names) purely to log a bounded, tool-name-only WARNING -- that
    # log is the only observable signal of a false capability refusal.
    registry = ToolRegistry()
    registry.register(registry_tool(
        "open_folder",
        lambda _arguments: return_value({"opened": "C:/Users/danie/kb"}),
    ))
    refusal = "I don't have access to your local desktop folders."
    client = FakeClient(FakeStream([refusal], content=[text_block(refusal)]))
    brain = Brain(client, registry, model="fast", persona="")

    assert asyncio.run(collect(brain, "open my kb folder")) == [refusal]
    assert "available capability refusal suppressed (tool=open_folder)" in caplog.text


@pytest.mark.parametrize("refusal", [
    "I can't do that right now.",
    "I don't have access to that.",
    "I'm unable to reach that folder.",
])
def test_refusal_sentences_are_never_delayed(refusal):
    assert ClaimGuard().delayed(refusal) is False


def test_delayed_distinguishes_state_description_from_attributed_claim():
    guard = ClaimGuard()
    assert guard.delayed("Spotify's open.") is False
    assert guard.delayed("I opened Spotify.") is True


def test_successful_open_evidence_licenses_started_but_not_played():
    # Item 4 (boss amendment): a successful `open` licenses "I've started
    # Spotify" -- before this, the guard falsely called a successful open a
    # lie. But a successful open launches; it does not play. "I played the
    # song." backed by open alone must still fail evaluation -- the plan
    # asked for both associations; the plan was wrong.
    guard = ClaimGuard()
    evidence = [("open", True)]
    assert guard.evaluate("I started Spotify.", evidence) == "I started Spotify."
    assert guard.evaluate("I played the song.", evidence) == UNBACKED_ACTION_REPLY


def test_host_speech_constants_end_in_whitespace():
    # Word-boundary contract (worker/sanitize.py:1-4): a host-emitted constant
    # that gets concatenated with a following streamed chunk must supply its own
    # trailing word boundary.
    for constant in (TIMEOUT_REPLY, PROVIDER_REPLY, UNBACKED_ACTION_REPLY):
        assert constant[-1].isspace(), constant


def test_ambient_context_reaches_provider_taints_tools_and_is_not_remembered():
    ambient = (
        "Overheard while not addressed (unverified, may not be for you):\n"
        "[2026-08-26T12:00:00+00:00] send the draft"
    )
    registry = FakeRegistry(ToolResult("ok", "draft ready"))
    client = FakeClient(
        FakeStream(content=[tool_block(name="mutate")], stop_reason="tool_use"),
        FakeStream(["Ready."], content=[text_block("Ready.")]),
        FakeStream(["Next."], content=[text_block("Next.")]),
    )
    brain = Brain(client, registry, model="fast", persona="")

    async def scenario():
        first = [chunk async for chunk in brain.respond("Atlas, do what I said", context=ambient)]
        second = await collect(brain, "Atlas, what time is it?")
        return first, second

    first, second = asyncio.run(scenario())

    assert first == ["Ready."]
    assert second == ["Next."]
    assert client.messages.calls[0]["messages"][0] == {
        "role": "user",
        "content": f"{ambient}\n\nCurrent addressed utterance:\nAtlas, do what I said",
    }
    assert registry.taints == [True]
    second_messages = client.messages.calls[2]["messages"]
    assert second_messages == [
        {"role": "user", "content": "Atlas, do what I said"},
        {"role": "assistant", "content": "Ready."},
        {"role": "user", "content": "Atlas, what time is it?"},
    ]
    assert all("Overheard while not addressed" not in str(message) for message in second_messages)


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
            "content": "Done -- google__draft_gmail_message executed.",
        },
    ]
    request = client.messages.calls[0]
    assert request["tool_choice"] == {"type": "none"}
    assert request["messages"] == [
        {
            "role": "user",
            "content": "yes go ahead and create the draft",
        },
        {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": "confirm-123",
                "name": "google__draft_gmail_message",
                "input": {"recipient": "daniel@example.test"},
            }],
        },
        {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": "confirm-123",
                "content": "sent",
                "is_error": False,
            }],
        },
    ]
    assert "Done -- google__draft_gmail_message executed." in request["system"][-1]["text"]


def test_confirm_error_is_persisted_and_narrated_as_a_real_tool_error():
    error = "API error: HttpError 400 " + "x" * 180
    registry = FakeRegistry(ToolResult("error", error))
    registry._pending = PendingAction(
        confirm_id="confirm-error",
        name="google__draft_gmail_message",
        arguments={"recipient": "daniel@example.test"},
        summary="draft summary",
        expires=float("inf"),
    )
    client = FakeClient(FakeStream(
        ["The draft failed."],
        content=[text_block("The draft failed.")],
    ))
    brain = Brain(client, registry, model="fast", persona="")

    result = asyncio.run(collect(brain, "confirm"))

    host_line = f"That didn't go through: {error[:160]}."
    assert result == ["The draft failed."]
    assert registry.pending is None
    assert brain._history == [
        {"role": "user", "content": "confirm"},
        {"role": "assistant", "content": host_line},
    ]
    request = client.messages.calls[0]
    assert host_line in request["system"][-1]["text"]
    assert request["messages"][-1] == {
        "role": "user",
        "content": [{
            "type": "tool_result",
            "tool_use_id": "confirm-error",
            "content": error,
            "is_error": True,
        }],
    }


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

    assert result == ["Done -- mutate executed."]
    assert mutation_calls == [{"message": "hello"}]
    assert brain._history == [
        {"role": "user", "content": "confirm"},
        {"role": "assistant", "content": "Done -- mutate executed."},
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
        FakeStream(["Finished."], content=[text_block("Finished.")]),
    )
    brain = Brain(client, registry, model="fast", persona="")
    transcript = "  Keep my spacing  "

    assert asyncio.run(collect(brain, transcript)) == ["Finished."]
    assert registry.transcripts == [transcript]


@pytest.mark.parametrize("content_tool", ["google__read", "read_file"])
def test_content_bearing_tools_taint_every_later_call_in_the_turn(content_tool):
    registry = FakeRegistry(ToolResult("ok", "content"), ToolResult("ok", "done"))
    client = FakeClient(
        FakeStream(content=[tool_block(name=content_tool)], stop_reason="tool_use"),
        FakeStream(content=[tool_block(name="lookup")], stop_reason="tool_use"),
        FakeStream(["Finished."], content=[text_block("Finished.")]),
    )
    brain = Brain(client, registry, model="fast", persona="")

    assert asyncio.run(collect(brain, "Check, then act")) == ["Finished."]
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
        FakeStream(["Finished."], content=[text_block("Finished.")]),
    )
    brain = Brain(client, registry, model="fast", persona="")

    assert asyncio.run(collect(brain, "Check, then open it")) == ["Finished."]
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
        FakeStream(
            ["Let me check. "],
            content=[tool_block(name="open", arguments={"target": "spotify"})],
            stop_reason="tool_use",
        ),
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


def test_claim_sentence_waits_while_safe_sentence_streams_first():
    # Item 2 (order preservation): once anything has been held, every later
    # chunk is held too, so held chunks re-emit contiguously instead of
    # letting a later safe sentence ("It's playing now.") leak out live
    # ahead of the still-pending verdict for the held claim. Item 3: the
    # rebuttal for the held claim is emitted LAST, after the passing
    # sentence that followed it in the model's own text.
    seen: list[str] = []
    registry = FakeRegistry(ToolResult("error", "could not open"))
    registry.when_called = lambda: seen.append("called")
    client = FakeClient(
        FakeStream(
            ["Let me check. ", "I opened Spotify. ", "It's playing now."],
            content=[tool_block(name="open", arguments={"target": "spotify"})],
            stop_reason="tool_use",
        ),
        FakeStream(),
    )
    brain = Brain(client, registry, model="fast", persona="")

    async def scenario():
        iterator = brain.respond("Open Spotify")
        first = await anext(iterator)
        assert seen == []
        rest = [chunk async for chunk in iterator]
        return first, rest

    first, rest = asyncio.run(scenario())
    assert first == "Let me check. "
    assert rest == [
        "It's playing now.",
        " I did not actually do that - I have no tool result. Want me to? ",
    ]
    assert seen == ["called"]
    # Full output must match model order with clean word boundaries -- no
    # jammed "a.B" joins from a dropped or misplaced space.
    assert first + "".join(rest) == (
        "Let me check. It's playing now. I did not actually do that - "
        "I have no tool result. Want me to? "
    )


def test_main_loop_stream_classification_order_preserved_for_a_later_safe_sentence():
    # Item 2, site C (brain.py's main tool-loop stream classification): a
    # SUCCESSFUL tool result means no substitution and no item-3 reordering,
    # so this isolates pure classification-order-preservation -- a passing
    # sentence after a held claim must not leak out live ahead of it.
    registry = FakeRegistry(ToolResult("ok", "opened"))
    client = FakeClient(
        FakeStream(
            ["Let me check. ", "I opened Spotify. ", "It's playing now."],
            content=[tool_block(name="open", arguments={"target": "spotify"})],
            stop_reason="tool_use",
        ),
        FakeStream(),
    )
    brain = Brain(client, registry, model="fast", persona="")

    assert asyncio.run(collect(brain, "Open Spotify")) == [
        "Let me check. ",
        "I opened Spotify. ",
        "It's playing now.",
    ]


def test_main_loop_buffer_flush_order_preserved_without_terminal_punctuation():
    # Item 2, site D (brain.py's final buffer flush): a reply with no
    # terminal punctuation on its last sentence never forms a chunk via
    # split_spoken and is only seen at the post-loop "if buffer:" flush.
    # SUCCESSFUL evidence avoids item 3's reorder so this isolates the flush
    # site's own order-preservation.
    registry = FakeRegistry(ToolResult("ok", "opened"))
    client = FakeClient(
        FakeStream(
            ["I opened Spotify. ", "It is playing now"],
            content=[tool_block(name="open", arguments={"target": "spotify"})],
            stop_reason="tool_use",
        ),
        FakeStream(),
    )
    brain = Brain(client, registry, model="fast", persona="")

    assert asyncio.run(collect(brain, "Open Spotify")) == [
        "I opened Spotify. ",
        "It is playing now",
    ]


def test_narration_stream_order_preserved_for_failed_confirm():
    # Item 2, site A (brain.py's confirm/narration-path stream
    # classification): a failed confirm's narration streams a claim, then a
    # reassurance -- the reassurance must not leak out live ahead of the
    # still-pending claim's verdict. Unlike the main loop, the confirm-path
    # tail does not reorder (item 3 applies only to the main loop -- see its
    # test), so the caveat must precede the reassurance in plain stream
    # order here.
    registry = FakeRegistry(ToolResult("error", "could not open"))
    registry._pending = PendingAction(
        confirm_id="confirm-123",
        name="open",
        arguments={"target": "spotify"},
        summary="open Spotify",
        expires=float("inf"),
    )
    client = FakeClient(
        FakeStream(
            ["I opened Spotify. ", "Everything is ready for you now."],
            content=[text_block("I opened Spotify. Everything is ready for you now.")],
        ),
    )
    brain = Brain(client, registry, model="fast", persona="")

    assert asyncio.run(collect(brain, "confirm")) == [
        "I did not actually do that - I have no tool result. Want me to? ",
        "Everything is ready for you now.",
    ]


def test_narration_buffer_flush_order_preserved_without_terminal_punctuation():
    # Item 2, site B (brain.py's confirm/narration-path buffer flush): the
    # reassurance has no terminal punctuation, so it is only seen at that
    # path's own "if buffer:" flush, not the stream classification loop.
    registry = FakeRegistry(ToolResult("error", "could not open"))
    registry._pending = PendingAction(
        confirm_id="confirm-123",
        name="open",
        arguments={"target": "spotify"},
        summary="open Spotify",
        expires=float("inf"),
    )
    client = FakeClient(
        FakeStream(
            ["I opened Spotify. ", "Everything is ready for you now"],
            content=[text_block("I opened Spotify. Everything is ready for you now")],
        ),
    )
    brain = Brain(client, registry, model="fast", persona="")

    assert asyncio.run(collect(brain, "confirm")) == [
        "I did not actually do that - I have no tool result. Want me to? ",
        "Everything is ready for you now",
    ]


def test_substitution_is_the_final_yielded_chunk_when_later_chunks_pass():
    # Item 3: the substitution (the host's rebuttal) must be the last thing
    # spoken, even when multiple held chunks after the unbacked claim each
    # pass evaluation individually -- a buried rebuttal followed by more
    # (falsely reassuring) sentences is exactly the bug being fixed.
    registry = FakeRegistry(ToolResult("error", "could not open"))
    client = FakeClient(
        FakeStream(
            [
                "I opened Spotify. ",
                "It is playing music now. ",
                "Everything is ready for you now.",
            ],
            content=[tool_block(name="open", arguments={"target": "spotify"})],
            stop_reason="tool_use",
        ),
        FakeStream(),
    )
    brain = Brain(client, registry, model="fast", persona="")

    result = asyncio.run(collect(brain, "Open Spotify"))

    assert result == [
        "It is playing music now. ",
        "Everything is ready for you now.",
        " I did not actually do that - I have no tool result. Want me to? ",
    ]
    assert result[-1].strip() == UNBACKED_ACTION_REPLY.strip()


def test_history_remembers_only_the_prefix_actually_yielded_when_closed_mid_flush():
    # Item 5: a barge-in that closes the generator partway through the final
    # flush must not remember sentences Daniel never heard. All three
    # sentences are held (the first is a claim, the rest follow it), so
    # nothing streams live and the whole reply is only produced by the
    # flush's try/finally -- closing after the first item proves history
    # reflects only what was actually yielded.
    registry = FakeRegistry(ToolResult("ok", "opened"))
    client = FakeClient(
        FakeStream(
            content=[tool_block(name="open", arguments={"target": "spotify"})],
            stop_reason="tool_use",
        ),
        FakeStream(
            ["I opened Spotify. ", "It's playing now. ", "Enjoy the music."],
            content=[text_block("I opened Spotify. It's playing now. Enjoy the music.")],
        ),
    )
    brain = Brain(client, registry, model="fast", persona="")

    async def scenario():
        iterator = brain.respond("do three things")
        first = await anext(iterator)
        await iterator.aclose()
        return first

    first = asyncio.run(scenario())
    assert first == "I opened Spotify. "
    assert brain._history == [
        {"role": "user", "content": "do three things"},
        {"role": "assistant", "content": "I opened Spotify. "},
    ]


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


def test_three_tool_rounds_each_get_a_fresh_model_call_budget():
    class SlowRegistry(FakeRegistry):
        async def call(self, *args, **kwargs):
            await asyncio.sleep(0.05)
            return await super().call(*args, **kwargs)

    responses = [
        DelayedFinalStream(
            content=[tool_block(f"toolu_{index}")],
            stop_reason="tool_use",
            final_delay=0.03,
        )
        for index in range(3)
    ]
    responses.append(
        DelayedFinalStream(
            ["Finished."],
            content=[text_block("Finished.")],
            final_delay=0.03,
        ),
    )
    client = FakeClient(*responses)
    brain = Brain(
        client,
        SlowRegistry(),
        model="fast",
        persona="",
        turn_timeout_s=0.15,
        turn_ceiling_s=1.0,
    )

    assert asyncio.run(collect(brain, "loop")) == ["Finished."]
    assert len(client.messages.calls) == 4


def test_model_call_timeout_returns_only_fixed_sentence():
    client = FakeClient(FakeStream(["late"], content=[text_block("late")], delay=0.05))
    brain = Brain(
        client,
        FakeRegistry(),
        model="fast",
        persona="",
        turn_timeout_s=0.01,
        turn_ceiling_s=0.1,
    )

    assert asyncio.run(collect(brain, "hello")) == ["I lost that one to a timeout. Still here. "]
    assert brain._history == []


def test_turn_ceiling_covers_tool_rounds():
    class SlowRegistry(FakeRegistry):
        async def call(self, *args, **kwargs):
            await asyncio.sleep(0.05)
            return await super().call(*args, **kwargs)

    client = FakeClient(
        FakeStream(content=[tool_block()], stop_reason="tool_use"),
    )
    brain = Brain(
        client,
        SlowRegistry(),
        model="fast",
        persona="",
        turn_timeout_s=0.1,
        turn_ceiling_s=0.01,
    )

    assert asyncio.run(collect(brain, "loop")) == [
        "I lost that one to a timeout. Still here. ",
    ]


def test_provider_exception_is_sanitized(caplog):
    class ProviderFailure(RuntimeError):
        status_code = 500

    client = FakeClient(ProviderFailure("private token must not escape"))
    brain = Brain(client, FakeRegistry(), model="fast", persona="")

    assert asyncio.run(collect(brain, "hello")) == ["I couldn't reach my model just now. Still here. "]
    assert "status=500" in caplog.text
    assert "private token" not in caplog.text
    assert "detail=n/a" in caplog.text
    assert brain._history == []


def test_provider_exception_detail_surfaces_bounded_api_error_message(caplog):
    class ProviderFailure(RuntimeError):
        status_code = 400

        def __init__(self, message: str) -> None:
            super().__init__(message)
            self.message = message

    long_message = (
        "input_schema does not support oneOf, allOf, or anyOf at the top level"
        + " padding" * 20
    )
    client = FakeClient(ProviderFailure(long_message))
    brain = Brain(client, FakeRegistry(), model="fast", persona="")

    assert asyncio.run(collect(brain, "hello")) == ["I couldn't reach my model just now. Still here. "]
    assert "status=400" in caplog.text
    assert len(long_message) > 120
    assert f"detail={long_message[:120]}" in caplog.text
    assert long_message not in caplog.text


def test_provider_exception_message_is_ignored_without_int_status_code(caplog):
    """.message is only trusted when the exception is API-error-shaped.

    Guards against a future, unrelated dependency raising something with a
    coincidental .message attribute (but no int status_code) and having its
    text land in the log unbounded/unreviewed.
    """
    class NotAnApiError(RuntimeError):
        def __init__(self, message: str) -> None:
            super().__init__(message)
            self.message = message

    client = FakeClient(NotAnApiError("must not leak into detail"))
    brain = Brain(client, FakeRegistry(), model="fast", persona="")

    assert asyncio.run(collect(brain, "hello")) == ["I couldn't reach my model just now. Still here. "]
    assert "status=unknown" in caplog.text
    assert "detail=n/a" in caplog.text
    assert "must not leak into detail" not in caplog.text


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
    assert (
        "If read_file reports truncated, do not analyse the preview -- "
        "call launch_work with the exact path."
    ) in BASE_SYSTEM
    assert "closes every window" in BASE_SYSTEM
    assert "reading or acting inside a web page, or Chrome, uses launch_work" in BASE_SYSTEM
    assert "unless the tool result for that call" in BASE_SYSTEM
    assert "do not narrate between tool calls" in BASE_SYSTEM
    assert "read every summary field back" in BASE_SYSTEM
    assert "host alone confirms or cancels" in BASE_SYSTEM
    assert "Do not call a confirmation tool" in BASE_SYSTEM
    assert "\"myself\" means" in BASE_SYSTEM
    assert "Daniel's own address" in BASE_SYSTEM
    assert "user_google_email" not in BASE_SYSTEM


def test_base_system_enforces_concise_non_narrated_voice_without_changing_confirm_flow():
    assert "Default answers are at most two short sentences." in BASE_SYSTEM
    assert "Do not narrate steps for instant tools" in BASE_SYSTEM
    assert 'Do not say "Let me search" or "Now let me read"' in BASE_SYSTEM
    assert '"No - I can\'t <X>. <one enablement hint>."' in BASE_SYSTEM
    assert "Voice summaries are one or two sentences unless Daniel names a length." in BASE_SYSTEM
    assert "Never repeat Daniel's request back." in BASE_SYSTEM
    assert (
        "A tool result of needs_confirmation means to read every summary field back in one sentence and ask\n"
        "Daniel for yes or no. Wait for his answer. The host alone confirms or cancels on a later turn."
    ) in BASE_SYSTEM
    assert (
        "Never say you launched, opened, sent, created, or closed anything unless the tool result for that call\n"
        "says ok. If a tool is refused or errors, say so in one sentence and ask what Daniel wants."
    ) in BASE_SYSTEM


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
