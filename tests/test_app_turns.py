"""Voice-turn integration around reflexes, streaming speech, and work completion."""
from __future__ import annotations

import asyncio
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime, timedelta, timezone
import sqlite3
from types import SimpleNamespace

import pytest

from worker import app, router
from worker.router import Addressing
from worker.engagement import ENGAGED, Engagement
from worker.jobstore import JobState
from worker.state import LISTENING, StatePublisher
from worker.tools import Tool, ToolRegistry


class FakeBrain:
    def __init__(self, chunks=("First sentence. ", "Second sentence.")) -> None:
        self.chunks = chunks
        self.calls = []
        self.contexts = []

    async def respond(self, text, *, context=None):
        self.calls.append(text)
        self.contexts.append(context)
        for chunk in self.chunks:
            yield chunk


class FakeSnapshotBrain:
    def __init__(self, log: list[str] | None = None) -> None:
        self.refreshes = 0
        self.settles = 0
        self.log = log if log is not None else []

    def refresh_tools(self) -> bool:
        self.refreshes += 1
        self.log.append("refresh")
        return True

    def mark_tools_settled(self) -> None:
        self.settles += 1
        self.log.append("settle")


class FakeSession:
    def __init__(self) -> None:
        self.spoken = []
        self.interruptions = 0
        self.input = FakeInput()

    async def say(self, source, *, add_to_chat_ctx):
        assert add_to_chat_ctx is False
        if hasattr(source, "__aiter__"):
            chunks = []
            async for chunk in source:
                chunks.append(chunk)
            self.spoken.append(chunks)
        else:
            self.spoken.append(source)

    def interrupt(self):
        self.interruptions += 1


class FakeInput:
    def __init__(self) -> None:
        self.audio_enabled = True

    def set_audio_enabled(self, enabled: bool) -> None:
        self.audio_enabled = enabled


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def _dt(second: int) -> datetime:
    return datetime(2026, 8, 22, 12, 0, second, tzinfo=timezone.utc)


def _engage_typed(session, publisher, engagement, addressing, calls):
    calls.append("engage")
    engagement.wake()
    addressing.mark_activity()
    session.input.set_audio_enabled(True)
    publisher.start_session()
    publisher.set_state(LISTENING)


def test_wake_model_callback_publishes_runtime_model():
    publisher = StatePublisher()

    app._wake_model_callback(publisher)("hey_atlas_v2")

    assert publisher.snapshot()["wake_model"] == "hey_atlas_v2"


def test_typed_turn_wakes_and_is_addressed_without_spoken_vocabulary():
    brain = FakeBrain(chunks=("Ready.",))
    session = FakeSession()
    session.input.set_audio_enabled(False)
    publisher = StatePublisher()
    engagement = Engagement(120)
    addressing = Addressing(30, ())
    engage_calls = []

    asyncio.run(app._handle_typed_turn(
        "show my calendar",
        ready=True,
        intents={},
        brain=brain,
        session=session,
        publisher=publisher,
        engagement=engagement,
        addressing=addressing,
        engage=lambda: _engage_typed(
            session, publisher, engagement, addressing, engage_calls,
        ),
        sleep=lambda announce=True: None,
    ))

    assert engage_calls == ["engage"]
    assert brain.calls == ["show my calendar"]
    user_line = next(
        line for line in publisher.snapshot()["transcript"] if line["role"] == "user"
    )
    assert user_line["source"] == "typed"


def test_typed_reflex_uses_the_reflex_path_without_address_vocabulary():
    session = FakeSession()
    publisher = StatePublisher()
    publisher.add_line("atlas", "The previous answer.")
    engagement = Engagement(120)
    engagement.wake()

    asyncio.run(app._handle_typed_turn(
        "repeat that",
        ready=True,
        intents={"repeat": {"phrases": ["repeat that"]}},
        brain=FakeBrain(),
        session=session,
        publisher=publisher,
        engagement=engagement,
        addressing=Addressing(30, ()),
        engage=lambda: None,
        sleep=lambda announce=True: None,
    ))

    assert session.spoken == ["The previous answer."]
    assert publisher.snapshot()["transcript"][-1]["source"] == "typed"


def test_pending_confirmation_typed_yes_confirms_after_addressed_wake():
    calls = []
    registry = ToolRegistry()

    async def execute(arguments):
        calls.append(arguments)
        return "done"

    registry.register(Tool(
        "confirm_action",
        "Confirm it.",
        {
            "type": "object",
            "properties": {"target": {"type": "string"}},
            "required": ["target"],
            "additionalProperties": False,
        },
        execute,
        policy="confirm",
    ))

    class ConfirmingBrain(FakeBrain):
        async def respond(self, text, *, context=None):
            self.calls.append(text)
            assert text == "yes"
            pending = registry.pending
            assert pending is not None
            result = await registry.confirm(pending.confirm_id)
            assert result.status == "ok"
            yield "Done."

    async def scenario():
        pending = await registry.call("confirm_action", {"target": "report"})
        assert pending.status == "needs_confirmation"
        session = FakeSession()
        publisher = StatePublisher()
        engagement = Engagement(120)
        addressing = Addressing(30, ())
        await app._handle_typed_turn(
            "yes",
            ready=True,
            intents={},
            brain=ConfirmingBrain(),
            session=session,
            publisher=publisher,
            engagement=engagement,
            addressing=addressing,
            engage=lambda: _engage_typed(
                session, publisher, engagement, addressing, [],
            ),
            sleep=lambda announce=True: None,
        )

    asyncio.run(scenario())

    assert registry.pending is None
    assert calls == [{"target": "report"}]


def test_typed_turn_rejects_before_the_voice_session_is_ready():
    with pytest.raises(RuntimeError, match="voice session is not ready"):
        asyncio.run(app._handle_typed_turn(
            "hello",
            ready=False,
            intents={},
            brain=FakeBrain(),
            session=FakeSession(),
            publisher=StatePublisher(),
            engagement=Engagement(120),
            addressing=Addressing(30, ()),
            engage=lambda: None,
            sleep=lambda announce=True: None,
        ))


def test_submit_voice_turn_streams_into_say_and_mirrors_the_exchange():
    brain = FakeBrain()
    session = FakeSession()
    publisher = StatePublisher()
    publisher.set_state(LISTENING)
    engagement = Engagement(120)
    engagement.wake()

    result = asyncio.run(app._submit_voice_turn(
        "tell me something",
        brain=brain,
        session=session,
        publisher=publisher,
        engagement=engagement,
    ))

    assert result == "First sentence. Second sentence."
    assert brain.calls == ["tell me something"]
    assert session.spoken == [["First sentence. ", "Second sentence."]]
    assert publisher.state == LISTENING
    assert [(line["role"], line["text"]) for line in publisher.snapshot()["transcript"]] == [
        ("user", "tell me something"),
        ("atlas", "First sentence. Second sentence."),
    ]


def test_mcp_prompt_snapshot_refreshes_on_each_arrival_not_only_at_settle():
    """A server that retries to exhaustion holds connect() for up to ~190s
    (config/mcp.yaml: 3 attempts x 60s + backoff). The tools of servers that
    already arrived must be in the prompt before that, not after it, or
    Atlas denies capabilities it has had for minutes (BB-wave review,
    finding 4). The settle boundary still runs exactly once, last."""
    delays = []
    log = []

    async def fake_sleep(delay):
        delays.append(delay)
        await asyncio.sleep(0)

    class FakeMcp:
        async def connect(self, registry, *, on_server):
            registry.append("one")
            log.append("arrived one")
            on_server("one", registry)
            # Stands in for the second server still retrying.
            await fake_sleep(1)
            registry.append("two")
            log.append("arrived two")
            on_server("two", registry)

    brain = FakeSnapshotBrain(log)
    asyncio.run(app._connect_mcp_and_settle(FakeMcp(), [], brain))

    # The first server's refresh lands BEFORE the second one arrives.
    assert log == [
        "arrived one", "refresh",
        "arrived two", "refresh",
        "refresh", "settle",
    ]
    assert brain.settles == 1
    assert delays == [1]


def test_mcp_prompt_snapshot_refreshes_after_settle_callbacks():
    class FakeMcp:
        def __init__(self) -> None:
            self.on_server = None

        async def connect(self, registry, *, on_server):
            self.on_server = on_server

    mcp = FakeMcp()
    brain = FakeSnapshotBrain()
    asyncio.run(app._connect_mcp_and_settle(mcp, [], brain))
    assert brain.refreshes == 1
    assert brain.settles == 1

    mcp.on_server("reconnected", [])
    assert brain.refreshes == 2


def test_submit_voice_turn_does_not_restore_listening_after_dismissal():
    engagement = Engagement(120)
    engagement.wake()
    publisher = StatePublisher()

    class DismissingSession(FakeSession):
        async def say(self, source, *, add_to_chat_ctx):
            async for _chunk in source:
                engagement.dismiss()
                publisher.set_state("ASLEEP")

    asyncio.run(app._submit_voice_turn(
        "tell me something",
        brain=FakeBrain(chunks=("Answer.",)),
        session=DismissingSession(),
        publisher=publisher,
        engagement=engagement,
    ))

    assert publisher.state == "ASLEEP"


def test_reflex_dismiss_cancel_and_repeat_never_call_the_brain():
    intents = {
        "dismiss": {"phrases": ["go to sleep"]},
        "cancel": {"phrases": ["cancel"]},
        "repeat": {"phrases": ["repeat that"]},
    }

    async def scenario():
        publisher = StatePublisher()
        publisher.set_state(LISTENING)
        publisher.add_line("atlas", "The last answer.")
        session = FakeSession()
        dismissed = []
        assert await app._handle_reflex(
            "go to sleep", intents=intents, session=session, publisher=publisher,
            dismiss=lambda: dismissed.append(True),
        )
        assert await app._handle_reflex(
            "cancel", intents=intents, session=session, publisher=publisher,
            dismiss=lambda: None,
        )
        assert await app._handle_reflex(
            "repeat that", intents=intents, session=session, publisher=publisher,
            dismiss=lambda: None,
        )
        return publisher, session, dismissed

    publisher, session, dismissed = asyncio.run(scenario())
    assert dismissed == [True]
    assert session.interruptions == 1
    assert session.spoken == ["The last answer."]
    assert [line["text"] for line in publisher.snapshot()["transcript"][-3:]] == [
        "go to sleep", "cancel", "repeat that",
    ]


def test_non_reflex_is_left_for_the_conversational_lane():
    handled = asyncio.run(app._handle_reflex(
        "cancel the render", intents={"cancel": {"phrases": ["cancel"]}},
        session=FakeSession(), publisher=StatePublisher(), dismiss=lambda: None,
    ))
    assert handled is False


def test_tool_events_no_longer_reach_the_transcript_and_terminal_jobs_are_mirrored_and_spoken_only_when_engaged():
    publisher = StatePublisher()
    session = FakeSession()
    engagement = Engagement(120)
    engagement.wake()

    # _record_tool is the on_tool callback wired to services.brain.on_tool; it
    # used to mirror a "tool" role line into the transcript ring, but that
    # cluttered the chat -- tool calls are already recorded in the traces DB
    # independently (worker/tools.py). It must be a no-op on the transcript,
    # and it takes exactly the on_tool signature (name, result): the
    # publisher parameter went away with the transcript line it published.
    app._record_tool("open", SimpleNamespace(status="ok"))
    succeeded = SimpleNamespace(state=JobState.SUCCEEDED, summary="  Draft   verified.  ")
    app._announce_terminal(succeeded, publisher, session, engagement)
    engagement.dismiss()
    failed = SimpleNamespace(state=JobState.FAILED, summary=None)
    app._announce_terminal(failed, publisher, session, engagement)

    lines = [(line["role"], line["text"]) for line in publisher.snapshot()["transcript"]]
    assert lines == [
        ("atlas", "Done -- Draft verified."),
        ("atlas", "That task hit a problem; it's in History."),
    ]
    assert "tool" not in [role for role, _text in lines]
    assert engagement.state != ENGAGED
    assert session.spoken == ["Done -- Draft verified."]


@pytest.mark.parametrize("fails", [False, True])
def test_registry_tool_state_is_visible_only_while_execution_is_in_flight(fails):
    async def scenario():
        publisher = StatePublisher(clock=lambda: _dt(0))
        registry = ToolRegistry(execution_clock=lambda: _dt(0))
        started = asyncio.Event()
        release = asyncio.Event()

        async def run(_arguments):
            started.set()
            await release.wait()
            if fails:
                raise RuntimeError("tool failure")
            return "done"

        registry.register(Tool(
            "search_messages",
            "Search messages.",
            {"type": "object", "properties": {}},
            run,
        ))
        registry.set_execution_observer(publisher.set_tool)
        task = asyncio.create_task(registry.call("search_messages", {}))
        await started.wait()
        active = publisher.snapshot()["tool"]
        release.set()
        result = await task
        return active, publisher.snapshot()["tool"], result

    active, cleared, result = asyncio.run(scenario())

    assert active == {"name": "search_messages", "since": _dt(0).isoformat()}
    assert cleared is None
    assert result.status == ("error" if fails else "ok")


def test_engaged_completion_refreshes_silence_and_address_windows():
    clock = FakeClock()
    publisher = StatePublisher()
    session = FakeSession()
    engagement = Engagement(120, clock=clock)
    addressing = Addressing(30, (), clock=clock)
    engagement.wake()
    addressing.mark_activity()
    clock.value = 40

    succeeded = SimpleNamespace(state=JobState.SUCCEEDED, summary="Finished.")
    app._announce_terminal(succeeded, publisher, session, engagement, addressing)

    clock.value = 70
    assert engagement.tick() == "ENGAGED"
    assert addressing.is_addressed("ordinary follow up")


def test_completion_callback_does_not_refresh_clocks_after_dismissal():
    clock = FakeClock()
    publisher = StatePublisher()
    session = FakeSession()
    engagement = Engagement(120, clock=clock)
    addressing = Addressing(30, (), clock=clock)
    engagement.wake()
    addressing.mark_activity()
    clock.value = 40
    engagement.dismiss()

    succeeded = SimpleNamespace(state=JobState.SUCCEEDED, summary="Finished.")
    app._announce_terminal(succeeded, publisher, session, engagement, addressing)

    assert session.spoken == []
    assert not addressing.is_addressed("")


def test_ambient_turn_is_recorded_without_model_speech_or_clock_refresh():
    clock = FakeClock()
    brain = FakeBrain()
    session = FakeSession()
    publisher = StatePublisher()
    engagement = Engagement(120, clock=clock)
    addressing = Addressing(30, ["calendar"], clock=clock)
    engagement.wake()
    addressing.mark_activity()
    clock.value = 40
    sleep_calls = []

    asyncio.run(app._handle_audio_turn(
        "someone should bring snacks",
        intents={},
        brain=brain,
        session=session,
        publisher=publisher,
        engagement=engagement,
        addressing=addressing,
        sleep=lambda announce=True: sleep_calls.append(announce),
    ))

    assert brain.calls == []
    assert session.spoken == []
    assert sleep_calls == []
    assert [(line["role"], line["text"]) for line in publisher.snapshot()["transcript"]] == [
        ("ambient", "someone should bring snacks"),
    ]
    clock.value = 120.01
    assert engagement.tick() == "ASLEEP"


def test_prior_speech_reference_injects_only_recent_ambient_lines():
    routing_clock = FakeClock()
    now = [datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)]
    publisher = StatePublisher(clock=lambda: now[0])
    publisher.add_line("ambient", "old room instruction")
    now[0] += timedelta(seconds=181)
    publisher.add_line("user", "visible user line")
    publisher.add_line("ambient", "recent room instruction")
    publisher.add_line("assistant", "visible assistant line")
    brain = FakeBrain(chunks=("Understood.",))
    engagement = Engagement(120, clock=routing_clock)
    addressing = Addressing(90, ("atlas",), clock=routing_clock)
    engagement.wake()
    addressing.mark_activity()

    asyncio.run(app._handle_audio_turn(
        "Atlas, I just said do what I asked",
        intents={},
        brain=brain,
        session=FakeSession(),
        publisher=publisher,
        engagement=engagement,
        addressing=addressing,
        sleep=lambda announce=True: None,
    ))

    assert brain.calls == ["Atlas, I just said do what I asked"]
    assert brain.contexts == [
        "Overheard while not addressed (unverified, may not be for you):\n"
        "[2026-08-26T12:03:01+00:00] recent room instruction"
    ]


def test_ambient_context_is_absent_without_prior_speech_reference():
    publisher = StatePublisher()
    publisher.add_line("ambient", "recent room instruction")
    brain = FakeBrain(chunks=("Okay.",))
    engagement = Engagement(120)
    addressing = Addressing(90, ("atlas",))
    engagement.wake()
    addressing.mark_activity()

    asyncio.run(app._handle_audio_turn(
        "Atlas, check my calendar",
        intents={},
        brain=brain,
        session=FakeSession(),
        publisher=publisher,
        engagement=engagement,
        addressing=addressing,
        sleep=lambda announce=True: None,
    ))

    assert brain.contexts == [None]


def test_blank_audio_turn_does_not_refresh_clocks_or_record_a_line():
    clock = FakeClock()
    brain = FakeBrain()
    session = FakeSession()
    publisher = StatePublisher()
    engagement = Engagement(120, clock=clock)
    addressing = Addressing(30, (), clock=clock)
    engagement.wake()
    addressing.mark_activity()
    clock.value = 10

    asyncio.run(app._handle_audio_turn(
        "   ",
        intents={},
        brain=brain,
        session=session,
        publisher=publisher,
        engagement=engagement,
        addressing=addressing,
        sleep=lambda announce=True: None,
    ))

    assert brain.calls == []
    assert publisher.snapshot()["transcript"] == []
    clock.value = 120.01
    assert engagement.tick() == "ASLEEP"


def test_addressed_turn_refreshes_engagement_and_reply_window():
    clock = FakeClock()
    brain = FakeBrain(chunks=("You have two events.",))
    session = FakeSession()
    publisher = StatePublisher()
    engagement = Engagement(120, clock=clock)
    addressing = Addressing(30, ["calendar"], clock=clock)
    engagement.wake()
    addressing.mark_activity()
    clock.value = 40

    asyncio.run(app._handle_audio_turn(
        "what is on my calendar",
        intents={},
        brain=brain,
        session=session,
        publisher=publisher,
        engagement=engagement,
        addressing=addressing,
        sleep=lambda announce=True: None,
    ))

    assert brain.calls == ["what is on my calendar"]
    assert session.spoken == [["You have two events."]]
    clock.value = 70
    assert engagement.tick() == "ENGAGED"
    assert addressing.is_addressed("ordinary follow up")


def test_real_brain_turn_records_exact_metadata_steps_without_payloads(tmp_path):
    from worker.brain import Brain
    from worker.tools import Tool, ToolRegistry
    from worker.traces import TraceRecorder

    sentinels = {
        "prompt": "PromptIdentifier94731", "argument": "ArgumentIdentifier94732",
        "result": "ResultIdentifier94733", "output": "OutputIdentifier94734",
        "exception": "ExceptionIdentifier94735",
    }

    class Stream:
        def __init__(self, deltas, content, reason, usage):
            self._deltas = deltas
            self.final = SimpleNamespace(
                content=content, stop_reason=reason, usage=SimpleNamespace(**usage),
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        @property
        def text_stream(self):
            async def iterate():
                for delta in self._deltas:
                    yield delta
            return iterate()

        async def get_final_message(self):
            return self.final

    tool_block = SimpleNamespace(
        type="tool_use", id="toolu_1", name="lookup",
        input={"query": sentinels["argument"]},
    )
    usages = {
        "input_tokens": 10, "output_tokens": 2,
        "cache_read_input_tokens": 30, "cache_creation_input_tokens": 4,
    }
    streams = [
        Stream([], [tool_block], "tool_use", usages),
        Stream([sentinels["output"]], [], "end_turn", usages),
    ]

    class Messages:
        def stream(self, **_kwargs):
            return streams.pop(0)

    registry = ToolRegistry()

    async def lookup(arguments):
        assert arguments["query"] == sentinels["argument"]
        try:
            raise RuntimeError(sentinels["exception"])
        except RuntimeError as exc:
            return {"value": sentinels["result"], "error": str(exc)}

    registry.register(Tool("lookup", "Look up a value.", {"type": "object"}, lookup))
    brain = Brain(
        SimpleNamespace(messages=Messages()), registry, model="fast", persona="",
    )
    recorder = TraceRecorder(
        tmp_path / "traces.db", tool_names=registry.names(), model_names=("fast",),
    )
    engagement = Engagement(120)
    engagement.wake()

    asyncio.run(app._handle_audio_turn(
        f"Atlas {sentinels['prompt']}",
        intents={},
        brain=brain,
        session=FakeSession(),
        publisher=StatePublisher(),
        engagement=engagement,
        addressing=Addressing(30, ("atlas",)),
        sleep=lambda announce=True: None,
        trace_recorder=recorder,
    ))
    assert recorder.summary(days=1)["turns"] == 1
    recorder.close()

    with sqlite3.connect(tmp_path / "traces.db") as connection:
        steps = connection.execute(
            "SELECT kind,name,ok,tokens_in,tokens_out FROM steps ORDER BY seq"
        ).fetchall()
        turn = connection.execute("SELECT outcome,model FROM turns").fetchone()
    assert steps == [
        ("ROUTE", None, 1, 0, 0),
        ("GENERATE", "fast", 1, 20, 4),
        ("TOOL_CALL", "lookup", 1, 0, 0),
        ("RESPOND", None, 1, 0, 0),
    ]
    assert turn == ("responded", "fast")
    database_bytes = b"".join(
        path.read_bytes() for path in tmp_path.glob("traces.db*") if path.is_file()
    )
    for sentinel in sentinels.values():
        assert sentinel.encode("ascii") not in database_bytes


def test_speech_failure_after_first_chunk_records_failed_response_timing(tmp_path, monkeypatch):
    from worker.traces import TraceRecorder

    class FailingSession(FakeSession):
        async def say(self, source, *, add_to_chat_ctx):
            assert add_to_chat_ctx is False
            iterator = source.__aiter__()
            assert await anext(iterator) == "Answer."
            raise RuntimeError("speaker failed")

    marks = iter((0.0, 0.001, 0.002, 0.100, 0.125, 0.130))
    monkeypatch.setattr(app.time, "perf_counter", lambda: next(marks))
    recorder = TraceRecorder(tmp_path / "traces.db")
    engagement = Engagement(120)
    engagement.wake()

    with pytest.raises(RuntimeError, match="speaker failed"):
        asyncio.run(app._handle_audio_turn(
            "atlas question", intents={}, brain=FakeBrain(chunks=("Answer.",)),
            session=FailingSession(), publisher=StatePublisher(), engagement=engagement,
            addressing=Addressing(30, ("atlas",)), sleep=lambda announce=True: None,
            trace_recorder=recorder,
        ))
    assert recorder.summary(days=1)["turns"] == 1
    recorder.close()

    with sqlite3.connect(tmp_path / "traces.db") as connection:
        outcome = connection.execute("SELECT outcome FROM turns").fetchone()[0]
        respond = connection.execute(
            "SELECT ms,ok FROM steps WHERE kind='RESPOND'"
        ).fetchone()
    assert outcome == "speech_failed"
    assert respond == (25, 0)


def test_reply_restamps_clocks_before_turn_releases_ownership():
    ownership = app.TurnOwnership()
    marks = []

    class RecordingAddressing(Addressing):
        def mark_activity(self):
            marks.append(ownership.in_flight)
            super().mark_activity()

    engagement = Engagement(120)
    engagement.wake()

    asyncio.run(app._handle_audio_turn(
        "question",
        intents={},
        brain=FakeBrain(chunks=("Answer.",)),
        session=FakeSession(),
        publisher=StatePublisher(),
        engagement=engagement,
        addressing=RecordingAddressing(30, ("question",)),
        sleep=lambda announce=True: None,
        turn_ownership=ownership,
    ))

    assert marks == [True]
    assert not ownership.in_flight


def test_cancel_outside_address_window_is_ambient_and_does_not_refresh_engagement():
    clock = FakeClock()
    brain = FakeBrain()
    session = FakeSession()
    publisher = StatePublisher()
    engagement = Engagement(120, clock=clock)
    addressing = Addressing(5, (), clock=clock)
    engagement.wake()
    addressing.mark_activity()
    clock.value = 6

    asyncio.run(app._handle_audio_turn(
        "cancel",
        intents={"cancel": {"phrases": ["cancel"]}},
        brain=brain,
        session=session,
        publisher=publisher,
        engagement=engagement,
        addressing=addressing,
        sleep=lambda announce=True: None,
    ))

    assert session.interruptions == 0
    assert brain.calls == []
    assert engagement.state == "ENGAGED"
    assert [(line["role"], line["text"]) for line in publisher.snapshot()["transcript"]] == [
        ("ambient", "cancel"),
    ]
    clock.value = 120.01
    assert engagement.tick() == "ASLEEP"


def test_cancel_inside_address_window_interrupts_without_refreshing_engagement():
    clock = FakeClock()
    brain = FakeBrain()
    session = FakeSession()
    publisher = StatePublisher()
    engagement = Engagement(10, clock=clock)
    addressing = Addressing(5, (), clock=clock)
    engagement.wake()
    addressing.mark_activity()
    clock.value = 4

    asyncio.run(app._handle_audio_turn(
        "never mind",
        intents={"cancel": {"phrases": ["never mind"]}},
        brain=brain,
        session=session,
        publisher=publisher,
        engagement=engagement,
        addressing=addressing,
        sleep=lambda announce=True: None,
    ))

    assert session.interruptions == 1
    assert brain.calls == []
    clock.value = 10.01
    assert engagement.tick() == "ASLEEP"


def test_cancel_during_response_cancels_owned_turn_outside_address_window():
    async def scenario():
        clock = FakeClock()
        started = asyncio.Event()

        class BlockingBrain(FakeBrain):
            async def respond(self, text):
                self.calls.append(text)
                started.set()
                await asyncio.Event().wait()
                yield "unreachable"

        brain = BlockingBrain()
        session = FakeSession()
        publisher = StatePublisher()
        engagement = Engagement(120, clock=clock)
        addressing = Addressing(5, ("question",), clock=clock)
        ownership = app.TurnOwnership()
        engagement.wake()
        addressing.mark_activity()
        clock.value = 40
        response_task = asyncio.create_task(app._handle_audio_turn(
            "question",
            intents={"cancel": {"phrases": ["cancel"]}},
            brain=brain,
            session=session,
            publisher=publisher,
            engagement=engagement,
            addressing=addressing,
            sleep=lambda announce=True: None,
            turn_ownership=ownership,
        ))
        await started.wait()
        clock.value = 80
        await app._handle_audio_turn(
            "cancel",
            intents={"cancel": {"phrases": ["cancel"]}},
            brain=brain,
            session=session,
            publisher=publisher,
            engagement=engagement,
            addressing=addressing,
            sleep=lambda announce=True: None,
            turn_ownership=ownership,
        )
        result = await asyncio.gather(response_task, return_exceptions=True)
        return clock, brain, session, publisher, engagement, ownership, result

    clock, brain, session, publisher, engagement, ownership, result = asyncio.run(scenario())

    assert isinstance(result[0], asyncio.CancelledError)
    assert not ownership.in_flight
    assert brain.calls == ["question"]
    assert session.interruptions == 1
    assert [line["text"] for line in publisher.snapshot()["transcript"]] == [
        "question",
        "cancel",
    ]
    clock.value = 160.01
    assert engagement.tick() == "ASLEEP"


def test_dismiss_is_global_cancels_owned_turn_and_never_refreshes_clocks():
    async def scenario():
        clock = FakeClock()
        engagement = Engagement(10, clock=clock)
        addressing = Addressing(5, (), clock=clock)
        ownership = app.TurnOwnership()
        session = FakeSession()
        publisher = StatePublisher()
        started = asyncio.Event()

        async def operation():
            started.set()
            await asyncio.Event().wait()

        response_task = asyncio.create_task(ownership.run(operation))
        await started.wait()
        engagement.wake()
        addressing.mark_activity()
        clock.value = 20
        sleep_calls = []
        await app._handle_audio_turn(
            "go to sleep",
            intents={"dismiss": {"phrases": ["go to sleep"]}},
            brain=FakeBrain(),
            session=session,
            publisher=publisher,
            engagement=engagement,
            addressing=addressing,
            sleep=lambda announce=True: sleep_calls.append(announce),
            turn_ownership=ownership,
        )
        result = await asyncio.gather(response_task, return_exceptions=True)
        return engagement, addressing, ownership, session, sleep_calls, result

    engagement, addressing, ownership, session, sleep_calls, result = asyncio.run(scenario())

    assert isinstance(result[0], asyncio.CancelledError)
    assert engagement.state == "ASLEEP"
    assert not addressing.is_addressed("")
    assert not ownership.in_flight
    assert session.interruptions == 1
    assert sleep_calls == [True]


def test_repeat_requires_an_addressed_engaged_turn():
    clock = FakeClock()
    session = FakeSession()
    publisher = StatePublisher()
    publisher.add_line("atlas", "The last answer.")
    engagement = Engagement(120, clock=clock)
    addressing = Addressing(5, (), clock=clock)
    engagement.wake()
    addressing.mark_activity()
    clock.value = 6

    asyncio.run(app._handle_audio_turn(
        "repeat that",
        intents={"repeat": {"phrases": ["repeat that"]}},
        brain=FakeBrain(),
        session=session,
        publisher=publisher,
        engagement=engagement,
        addressing=addressing,
        sleep=lambda announce=True: None,
    ))

    assert session.spoken == []
    assert publisher.snapshot()["transcript"][-1]["role"] == "ambient"


def test_repeat_does_nothing_while_asleep_even_when_explicitly_addressed():
    session = FakeSession()
    publisher = StatePublisher()
    publisher.add_line("atlas", "The last answer.")

    asyncio.run(app._handle_audio_turn(
        "Atlas, repeat that",
        intents={"repeat": {"phrases": ["repeat that"]}},
        brain=FakeBrain(),
        session=session,
        publisher=publisher,
        engagement=Engagement(120),
        addressing=Addressing(30, ()),
        sleep=lambda announce=True: None,
    ))

    assert session.spoken == []
    assert [line["text"] for line in publisher.snapshot()["transcript"]] == [
        "The last answer.",
    ]


def test_explicitly_addressed_repeat_speaks_and_refreshes_both_clocks():
    clock = FakeClock()
    session = FakeSession()
    publisher = StatePublisher()
    publisher.add_line("atlas", "The last answer.")
    engagement = Engagement(20, clock=clock)
    addressing = Addressing(5, (), clock=clock)
    engagement.wake()
    addressing.mark_activity()
    clock.value = 6

    asyncio.run(app._handle_audio_turn(
        "Atlas, repeat that",
        intents={"repeat": {"phrases": ["repeat that"]}},
        brain=FakeBrain(),
        session=session,
        publisher=publisher,
        engagement=engagement,
        addressing=addressing,
        sleep=lambda announce=True: None,
    ))

    assert session.spoken == ["The last answer."]
    clock.value = 11
    assert engagement.tick() == "ENGAGED"
    assert addressing.is_addressed("")


def test_expired_turn_sleeps_silently_before_address_check():
    clock = FakeClock()
    brain = FakeBrain()
    session = FakeSession()
    publisher = StatePublisher()
    engagement = Engagement(10, clock=clock)
    addressing = Addressing(30, ["atlas"], clock=clock)
    engagement.wake()
    clock.value = 11
    sleep_calls = []

    asyncio.run(app._handle_audio_turn(
        "Atlas, are you there?",
        intents={},
        brain=brain,
        session=session,
        publisher=publisher,
        engagement=engagement,
        addressing=addressing,
        sleep=lambda announce=True: sleep_calls.append(announce),
    ))

    assert sleep_calls == [False]
    assert brain.calls == []


def test_sleep_watch_calls_unannounced_sleep_once_on_transition():
    async def scenario():
        clock = FakeClock()
        engagement = Engagement(10, clock=clock)
        engagement.wake()
        clock.value = 11
        calls = []
        task_box = {}

        def sleep(announce=True):
            calls.append(announce)
            task_box["task"].cancel()

        task = asyncio.create_task(app._sleep_watch(
            engagement,
            sleep,
            interval_s=0,
        ))
        task_box["task"] = task
        try:
            await task
        except asyncio.CancelledError:
            pass
        return engagement, calls

    engagement, calls = asyncio.run(scenario())
    assert engagement.state == "ASLEEP"
    assert calls == [False]


def test_sleep_watch_holds_timeout_until_the_owned_turn_finishes():
    async def scenario():
        clock = FakeClock()
        engagement = Engagement(10, clock=clock)
        engagement.wake()
        ownership = app.TurnOwnership()
        release = asyncio.Event()
        started = asyncio.Event()
        sleep_calls = []
        watch_box = {}

        async def operation():
            started.set()
            await release.wait()
            return "done"

        response_task = asyncio.create_task(ownership.run(operation))
        await started.wait()
        clock.value = 11

        def sleep(announce=True):
            sleep_calls.append(announce)
            watch_box["task"].cancel()

        watch_task = asyncio.create_task(app._sleep_watch(
            engagement,
            sleep,
            interval_s=0,
            turn_ownership=ownership,
        ))
        watch_box["task"] = watch_task
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        still_engaged = engagement.state
        calls_during_turn = list(sleep_calls)
        release.set()
        await response_task
        await asyncio.gather(watch_task, return_exceptions=True)
        return still_engaged, calls_during_turn, engagement.state, sleep_calls

    still_engaged, calls_during_turn, final_state, sleep_calls = asyncio.run(scenario())

    assert still_engaged == "ENGAGED"
    assert calls_during_turn == []
    assert final_state == "ASLEEP"
    assert sleep_calls == [False]


def test_unannounced_sleep_mutes_and_records_auto_sleep_without_speech():
    session = FakeSession()
    publisher = StatePublisher()
    publisher.set_state(LISTENING)

    assert app._sleep_session(session, publisher, announce=False)

    assert not session.input.audio_enabled
    assert session.spoken == []
    assert publisher.state == "ASLEEP"
    assert [(line["role"], line["text"]) for line in publisher.snapshot()["transcript"]] == [
        ("system", "auto-sleep"),
    ]


def test_address_vocabulary_uses_only_explicit_configuration():
    cfg = {
        "address_vocab": ["research", "draft"],
        "apps": {"mail": {"words": ["email", "inbox"]}},
    }

    assert router.vocabulary(cfg) == ["research", "draft"]


def test_production_listening_config_matches_wave_two_defaults():
    cfg = app._cfg()

    assert cfg["engagement_timeout_s"] == 120
    assert cfg["addressed_window_s"] == 90
    assert cfg["address_vocab"] == ["gmail", "inbox", "unread", "calendar", "youtube", "notion", "github", "spotify", "workers"]


def test_addressing_factory_reads_non_default_addressed_window():
    clock = FakeClock()
    addressing = app._addressing_from_config(
        {"addressed_window_s": 7, "address_vocab": []},
        clock=clock,
    )
    addressing.mark_activity()

    clock.value = 7
    assert addressing.is_addressed("ordinary follow up")
    clock.value = 7.01
    assert not addressing.is_addressed("ordinary follow up")


def test_cancelled_completion_is_bounded():
    assert app._terminal_line(SimpleNamespace(
        state=JobState.CANCELLED, summary=None,
    )) == "Cancelled."
    line = app._terminal_line(SimpleNamespace(
        state=JobState.SUCCEEDED, summary="x" * 500,
    ))
    assert len(line) == 320


# --- Unit CC1: no addressed turn ends in silence, and traces say so. ---------


def test_silent_brain_turn_speaks_a_fallback_and_records_an_empty_outcome(tmp_path):
    from worker.traces import TraceRecorder

    session = FakeSession()
    publisher = StatePublisher()
    engagement = Engagement(120)
    engagement.wake()
    addressing = Addressing(30, ("atlas",))
    recorder = TraceRecorder(tmp_path / "traces.db")
    assert app._address_window_open(addressing) is False

    response = asyncio.run(app._handle_audio_turn(
        "atlas what is the plan",
        intents={},
        brain=FakeBrain(chunks=()),
        session=session,
        publisher=publisher,
        engagement=engagement,
        addressing=addressing,
        sleep=lambda announce=True: None,
        trace_recorder=recorder,
    ))
    recorder.close()

    fallback = app.brain_mod.EMPTY_TURN_REPLY
    # The empty stream reached the speaker, then the host said something anyway.
    assert session.spoken == [[], fallback]
    # The return value is what the MODEL said, which is nothing -- that is the
    # signal the outcome is derived from.
    assert response == ""
    atlas_lines = [
        line["text"] for line in publisher.snapshot()["transcript"]
        if line["role"] == "atlas"
    ]
    assert atlas_lines == [fallback]
    # Item 3: the addressing window is refreshed on the fallback too, so the
    # follow-up does not need the wake word again.
    assert app._address_window_open(addressing) is True

    with sqlite3.connect(tmp_path / "traces.db") as connection:
        outcome = connection.execute("SELECT outcome FROM turns").fetchone()[0]
        respond = connection.execute(
            "SELECT ok FROM steps WHERE kind='RESPOND'"
        ).fetchone()
    assert outcome == "empty"
    # The RESPOND leg must not claim a delivered model reply while the turn row
    # says the turn was empty.
    assert respond == (0,)


def test_a_speaking_turn_is_still_recorded_as_responded(tmp_path):
    from worker.traces import TraceRecorder

    recorder = TraceRecorder(tmp_path / "traces.db")
    engagement = Engagement(120)
    engagement.wake()

    response = asyncio.run(app._handle_audio_turn(
        "atlas what is the plan",
        intents={},
        brain=FakeBrain(chunks=("All good. ",)),
        session=FakeSession(),
        publisher=StatePublisher(),
        engagement=engagement,
        addressing=Addressing(30, ("atlas",)),
        sleep=lambda announce=True: None,
        trace_recorder=recorder,
    ))
    recorder.close()

    assert response == "All good. "
    with sqlite3.connect(tmp_path / "traces.db") as connection:
        outcome = connection.execute("SELECT outcome FROM turns").fetchone()[0]
    assert outcome == "responded"


def test_addressed_turn_with_no_speech_is_not_promoted_to_responded(tmp_path, monkeypatch):
    from worker.traces import TraceRecorder

    async def _silent(text, **kwargs):
        kwargs["_trace_meta"].update(addressed=True, wake_kind="wake", outcome="error")
        return ""

    monkeypatch.setattr(app, "_handle_audio_turn_inner", _silent)
    recorder = TraceRecorder(tmp_path / "traces.db")

    asyncio.run(app._handle_audio_turn(
        "atlas question",
        intents={},
        brain=FakeBrain(),
        session=FakeSession(),
        publisher=StatePublisher(),
        engagement=Engagement(120),
        addressing=Addressing(30, ("atlas",)),
        sleep=lambda announce=True: None,
        trace_recorder=recorder,
    ))
    recorder.close()

    with sqlite3.connect(tmp_path / "traces.db") as connection:
        outcome = connection.execute("SELECT outcome FROM turns").fetchone()[0]
    # The promotion used to stamp "responded" on any addressed turn that did
    # not raise -- including one that said nothing.
    assert outcome == "error"


def test_repeat_with_nothing_to_repeat_speaks_instead_of_falling_through(caplog):
    caplog.set_level("WARNING", logger="atlas.app")
    session = FakeSession()
    publisher = StatePublisher()
    marks = []

    handled = asyncio.run(app._handle_reflex(
        "say that again",
        intents={"repeat": {"phrases": ["say that again"]}},
        session=session,
        publisher=publisher,
        dismiss=lambda: None,
        on_spoken=lambda: marks.append("spoken"),
    ))

    assert handled is True
    assert session.spoken == [app.REPEAT_FALLBACK]
    assert marks == ["spoken"]
    assert publisher.snapshot()["transcript"][-1]["text"] == app.REPEAT_FALLBACK
    assert "reflex intent produced no reply (intent=repeat)" in caplog.text


def test_reflex_funnel_cannot_return_handled_without_speaking(caplog):
    # A DIRECT call with an intent no live caller routes here: unlock_kb goes
    # to the brain through _respond, so this is not a live path -- it pins the
    # terminal else as a general net, so a future caller cannot fall through.
    caplog.set_level("WARNING", logger="atlas.app")
    session = FakeSession()

    handled = asyncio.run(app._handle_reflex(
        "atlas, unlock kb",
        intents={},
        session=session,
        publisher=StatePublisher(),
        dismiss=lambda: None,
    ))

    assert router.route("atlas, unlock kb", {}) == ("reflex", "unlock_kb")
    assert handled is True
    assert session.spoken == [app.REFLEX_FALLBACK]
    assert "reflex intent produced no reply (intent=unlock_kb)" in caplog.text


def test_unlock_kb_reaches_the_brain_rather_than_the_reflex_funnel():
    brain = FakeBrain(chunks=("Unlocking. ",))
    session = FakeSession()
    engagement = Engagement(120)
    engagement.wake()

    response = asyncio.run(app._handle_audio_turn(
        "atlas, unlock kb",
        intents={},
        brain=brain,
        session=session,
        publisher=StatePublisher(),
        engagement=engagement,
        addressing=Addressing(30, ("atlas",)),
        sleep=lambda announce=True: None,
    ))

    # Pins the corrected comment on _handle_reflex's terminal else.
    assert brain.calls == ["atlas, unlock kb"]
    assert response == "Unlocking. "
    assert app.REFLEX_FALLBACK not in str(session.spoken)


def test_dismiss_and_cancel_reflexes_stay_silent():
    session = FakeSession()
    dismissed = []

    asyncio.run(app._handle_reflex(
        "go to sleep",
        intents={"dismiss": {"phrases": ["go to sleep"]}},
        session=session,
        publisher=StatePublisher(),
        dismiss=lambda: dismissed.append("dismiss"),
    ))
    asyncio.run(app._handle_reflex(
        "stop",
        intents={"cancel": {"phrases": ["stop"]}},
        session=session,
        publisher=StatePublisher(),
        dismiss=lambda: None,
    ))

    # The terminal else must not turn the deliberately silent reflexes noisy.
    assert dismissed == ["dismiss"]
    assert session.spoken == []


def test_repeat_reflex_that_speaks_records_a_responded_outcome(tmp_path):
    from worker.traces import TraceRecorder

    publisher = StatePublisher()
    publisher.add_line("atlas", "The previous answer.")
    session = FakeSession()
    recorder = TraceRecorder(tmp_path / "traces.db")
    engagement = Engagement(120)
    engagement.wake()

    asyncio.run(app._handle_audio_turn(
        "atlas, say that again",
        intents={"repeat": {"phrases": ["say that again"]}},
        brain=FakeBrain(),
        session=session,
        publisher=publisher,
        engagement=engagement,
        addressing=Addressing(30, ("atlas",)),
        sleep=lambda announce=True: None,
        trace_recorder=recorder,
    ))
    recorder.close()

    assert session.spoken == ["The previous answer."]
    with sqlite3.connect(tmp_path / "traces.db") as connection:
        outcome = connection.execute("SELECT outcome FROM turns").fetchone()[0]
    assert outcome == "responded"


# --- Unit DD-1: cancel really cancels, and a barge-in is not a failure ------


def _confirm_registry():
    async def _run(_arguments):
        return "sent"

    registry = ToolRegistry()
    registry.register(Tool(
        name="send_message",
        description="Send a message.",
        input_schema={"type": "object", "properties": {}},
        run=_run,
        policy="confirm",
    ))
    return registry


def test_reflex_cancel_drops_the_pending_action_and_says_so():
    """The reflex lane never reaches the brain, so it has to do this itself.

    "cancel" used to stop the speech and the in-flight turn while leaving the
    pending mutating action alive and unmentioned: Daniel heard nothing, and
    the next plain "yes" -- about anything at all -- still had an action
    waiting to consume it.
    """
    # The real cancel vocabulary from config/intents.yaml -- every phrase that
    # reaches this lane has to clear the pending action, not just "cancel".
    intents = {"cancel": {"phrases": ["cancel", "never mind", "stop"]}}
    session = FakeSession()
    publisher = StatePublisher()
    registry = _confirm_registry()

    async def scenario():
        for phrase in ("cancel", "never mind", "stop"):
            await registry.call("send_message", {})
            assert registry.pending is not None
            handled = await app._handle_reflex(
                phrase,
                intents=intents,
                session=session,
                publisher=publisher,
                dismiss=lambda: None,
                registry=registry,
            )
            assert handled is True
            assert registry.pending is None

    asyncio.run(scenario())
    assert session.interruptions == 3
    assert session.spoken == [app.PENDING_CANCELLED_REPLY] * 3
    assert [(line["role"], line["text"]) for line in publisher.snapshot()["transcript"]] == [
        ("user", "cancel"),
        ("atlas", app.PENDING_CANCELLED_REPLY),
        ("user", "never mind"),
        ("atlas", app.PENDING_CANCELLED_REPLY),
        ("user", "stop"),
        ("atlas", app.PENDING_CANCELLED_REPLY),
    ]


def test_reflex_cancel_with_nothing_pending_stays_silent():
    """Unchanged behavior: cancelling an in-flight turn says nothing."""
    session = FakeSession()
    publisher = StatePublisher()
    registry = _confirm_registry()
    cancelled = []

    handled = asyncio.run(app._handle_reflex(
        "cancel",
        intents={"cancel": {"phrases": ["cancel"]}},
        session=session,
        publisher=publisher,
        dismiss=lambda: None,
        cancel_turn=lambda: cancelled.append(True),
        registry=registry,
    ))

    assert handled is True
    assert cancelled == [True]
    assert session.interruptions == 1
    assert session.spoken == []
    assert [line["role"] for line in publisher.snapshot()["transcript"]] == ["user"]


class BargeInBrain:
    """A turn whose speech Daniel talked over: nothing said, flag set."""

    def __init__(self) -> None:
        self.calls = []

    async def respond(self, text, *, context=None):
        from worker import traces as traces_mod

        self.calls.append(text)
        active = traces_mod.active_turn()
        if active is not None:
            traces_mod.mark_speech_interrupted(active[1])
        if False:  # pragma: no cover - an async generator that yields nothing
            yield ""


def test_a_barge_in_is_not_apologised_for_and_is_recorded_as_interrupted(tmp_path):
    from worker.traces import TraceRecorder

    session = FakeSession()
    publisher = StatePublisher()
    engagement = Engagement(120)
    engagement.wake()
    recorder = TraceRecorder(tmp_path / "traces.db")

    response = asyncio.run(app._handle_audio_turn(
        "atlas what is the plan",
        intents={},
        brain=BargeInBrain(),
        session=session,
        publisher=publisher,
        engagement=engagement,
        addressing=Addressing(30, ("atlas",)),
        sleep=lambda announce=True: None,
        trace_recorder=recorder,
    ))
    recorder.close()

    assert response == ""
    # The empty stream reached the speaker and the host added nothing after it.
    assert session.spoken == [[]]
    assert app.brain_mod.EMPTY_TURN_REPLY not in session.spoken
    assert [line["role"] for line in publisher.snapshot()["transcript"]] == ["user"]

    with sqlite3.connect(tmp_path / "traces.db") as connection:
        outcome = connection.execute("SELECT outcome FROM turns").fetchone()[0]
    # Not "empty" -- and not silently downgraded to "other" either, which is
    # what an outcome missing from the trace vocabulary would become.
    assert outcome == "interrupted"


def test_worker_file_log_is_bounded_and_warning_only(tmp_path):
    handler = app._configure_worker_logging(tmp_path)
    atlas_logger = logging.getLogger("atlas")
    try:
        assert isinstance(handler, RotatingFileHandler)
        assert handler.maxBytes == 256 * 1024
        assert handler.backupCount == 2
        assert handler.level == logging.WARNING
        assert Path(handler.baseFilename) == tmp_path / "Atlas" / "logs" / "worker.log"
        assert handler in atlas_logger.handlers
        # Replacing it leaves exactly one, and closes the old one.
        second = app._configure_worker_logging(tmp_path)
        tagged = [
            existing for existing in atlas_logger.handlers
            if getattr(existing, "_atlas_worker_file_handler", False)
        ]
        assert tagged == [second]
    finally:
        for existing in list(atlas_logger.handlers):
            if getattr(existing, "_atlas_worker_file_handler", False):
                atlas_logger.removeHandler(existing)
                existing.close()


def test_worker_file_log_keeps_host_shapes_and_not_what_daniel_said(tmp_path):
    """Rule 10: the persisted line says WHAT happened, never what was said."""
    utterance = "atlas read me the message from my accountant"
    handler = app._configure_worker_logging(tmp_path)
    atlas_logger = logging.getLogger("atlas")
    engagement = Engagement(120)
    engagement.wake()
    try:
        asyncio.run(app._submit_voice_turn(
            utterance,
            brain=FakeBrain(chunks=()),
            session=FakeSession(),
            publisher=StatePublisher(),
            engagement=engagement,
        ))
        handler.flush()
        payload = Path(handler.baseFilename).read_text(encoding="utf-8")
    finally:
        atlas_logger.removeHandler(handler)
        handler.close()

    assert "voice turn produced no speech; speaking the host fallback" in payload
    assert "WARNING" in payload
    assert utterance not in payload
    assert "accountant" not in payload


def _worker_log(tmp_path, emit):
    handler = app._configure_worker_logging(tmp_path)
    atlas_logger = logging.getLogger("atlas")
    try:
        emit()
        handler.flush()
        return Path(handler.baseFilename).read_text(encoding="utf-8")
    finally:
        atlas_logger.removeHandler(handler)
        handler.close()


def test_worker_file_log_never_persists_a_traceback(tmp_path):
    """Rule 10 is a property of the FILE, not of 60 well-behaved call sites.

    A traceback carries absolute source paths for the whole install tree plus
    the original OSError text -- which is usually a path too. The console lane
    still gets it; the file must not.
    """
    logger = logging.getLogger("atlas.probe")

    def emit():
        try:
            raise PermissionError(r"C:\Users\danie\Atlas\config\atlas.yaml denied")
        except PermissionError:
            logger.exception("could not flush the job store during shutdown")

    payload = _worker_log(tmp_path, emit)

    assert "could not flush the job store during shutdown" in payload
    assert "Traceback" not in payload
    assert "PermissionError" not in payload
    assert "atlas.yaml" not in payload
    assert "danie" not in payload


def test_worker_file_log_redacts_from_the_first_unsafe_token_to_end_of_message(tmp_path):
    """Redaction runs to the end of the message, not over one token.

    Windows paths have spaces in them ("Tax Returns 2025"), so redacting only
    the token that carried the backslash left the half that actually says
    something about Daniel sitting in the file.
    """
    logger = logging.getLogger("atlas.probe")

    def emit():
        logger.warning("skipping file root %s: directory is unavailable", r"C:\Users\danie\Desktop")
        logger.warning("skipping file root %s", r"C:\Users\danie\Documents\Tax Returns 2025 Draft")
        logger.warning("skipping file root %s: hidden directories are not roots", "~/.claude/projects")
        logger.warning("mail from %s failed", "someone@example.test")
        logger.warning("fetch of %s failed", "https://mail.example.test/inbox")
        logger.warning("wake input failed; retrying in %.1f seconds (%d/%d)", 0.5, 1, 3)
        logger.warning("overlong %s", "x" * 4_000)

    payload = _worker_log(tmp_path, emit)

    # The host's fixed prefix is written before the interpolation, so it
    # survives; everything from the unsafe token onward does not.
    assert "skipping file root <redacted>" in payload
    assert "mail from <redacted>" in payload
    assert "fetch of <redacted>" in payload
    for leaked in (
        "danie", "Desktop", "Documents", "Tax", "Returns", "2025", "Draft",
        ".claude", "projects", "example.test", "https", "directory is unavailable",
    ):
        assert leaked not in payload
    # A count is not a path: blunt redaction must not eat the useful part.
    assert "retrying in 0.5 seconds (1/3)" in payload
    # Every message is capped, so one huge record cannot push the useful
    # history out of the rotation window on its own.
    overlong = next(line for line in payload.splitlines() if " overlong " in line)
    assert len(overlong.split(" atlas.probe ", 1)[1]) == app.WORKER_LOG_MESSAGE_LIMIT


def test_worker_file_log_redacts_secret_shapes_as_defense_in_depth(tmp_path):
    """Nothing in atlas.* should ever log one; the file does not rely on that."""
    logger = logging.getLogger("atlas.probe")

    def emit():
        logger.warning("provider rejected key %s", "sk-ant-api03-EXAMPLENOTREAL")
        logger.warning("auth header was %s", "Bearer EXAMPLENOTREALTOKEN")
        logger.warning("session token %s expired", "eyJhbGciOiJIUzI1NiJ9.EXAMPLE")
        logger.warning("pairing token %s rejected", "0123456789abcdef0123")

    payload = _worker_log(tmp_path, emit)

    assert payload.count("<redacted>") == 4
    for leaked in ("sk-ant", "EXAMPLENOTREAL", "eyJ", "0123456789abcdef"):
        assert leaked not in payload
    # The host's own words still say what happened.
    assert "provider rejected key <redacted>" in payload
    assert "pairing token <redacted>" in payload


# --- Unit DD-1 rework: a possible echo may stop speech, never a pending ----


def test_reflex_cancel_mid_speech_stops_the_readback_but_keeps_the_pending():
    """A one-token "stop" is never filtered as an echo -- by design.

    livekit needs two words to interrupt, and a lone "stop" has to be able to
    stop Atlas mid-sentence, so the echo guard passes it through. That makes
    it exactly the word most likely to arrive as Atlas's own voice coming back
    off the speakers -- which must not be allowed to destroy a single-use
    mutating action nobody cancelled.
    """
    session = FakeSession()
    publisher = StatePublisher()
    registry = _confirm_registry()
    cancelled = []

    async def scenario():
        await registry.call("send_message", {})
        return await app._handle_reflex(
            "stop",
            intents={"cancel": {"phrases": ["cancel", "never mind", "stop"]}},
            session=session,
            publisher=publisher,
            dismiss=lambda: None,
            cancel_turn=lambda: cancelled.append(True),
            registry=registry,
            speaking_probe=lambda: True,
        )

    assert asyncio.run(scenario()) is True
    # The speech stops and the in-flight turn is still cancelled...
    assert session.interruptions == 1
    assert cancelled == [True]
    # ...but the readback Daniel was answering survives for an explicit
    # follow-up in the quiet afterwards, and nothing was said about it.
    assert registry.pending is not None
    assert session.spoken == []
    assert [line["role"] for line in publisher.snapshot()["transcript"]] == ["user"]


def test_reflex_cancel_in_the_quiet_period_still_drops_the_pending():
    session = FakeSession()
    publisher = StatePublisher()
    registry = _confirm_registry()

    async def scenario():
        await registry.call("send_message", {})
        return await app._handle_reflex(
            "cancel",
            intents={"cancel": {"phrases": ["cancel", "never mind", "stop"]}},
            session=session,
            publisher=publisher,
            dismiss=lambda: None,
            registry=registry,
            speaking_probe=lambda: False,
        )

    assert asyncio.run(scenario()) is True
    assert registry.pending is None
    assert session.spoken == [app.PENDING_CANCELLED_REPLY]


def test_reflex_cancel_refreshes_the_addressing_window_when_it_speaks():
    """"Cancelled." is a reply, so the follow-up needs no wake word."""
    session = FakeSession()
    publisher = StatePublisher()
    engagement = Engagement(120)
    engagement.wake()
    addressing = Addressing(30, ("atlas",))
    registry = _confirm_registry()

    async def scenario():
        await registry.call("send_message", {})
        assert app._address_window_open(addressing) is False
        brain = FakeBrain()
        brain.registry = registry
        await app._handle_audio_turn_inner(
            "cancel",
            intents={"cancel": {"phrases": ["cancel"]}},
            brain=brain,
            session=session,
            publisher=publisher,
            engagement=engagement,
            addressing=addressing,
            sleep=lambda announce=True: None,
            source="typed",
        )

    asyncio.run(scenario())

    assert registry.pending is None
    assert session.spoken == [app.PENDING_CANCELLED_REPLY]
    assert app._address_window_open(addressing) is True
