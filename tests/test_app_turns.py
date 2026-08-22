"""Voice-turn integration around reflexes, streaming speech, and work completion."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from worker import app
from worker.engagement import ENGAGED, Engagement
from worker.jobstore import JobState
from worker.state import LISTENING, StatePublisher


class FakeBrain:
    def __init__(self, chunks=("First sentence. ", "Second sentence.")) -> None:
        self.chunks = chunks
        self.calls = []

    async def respond(self, text):
        self.calls.append(text)
        for chunk in self.chunks:
            yield chunk


class FakeSession:
    def __init__(self) -> None:
        self.spoken = []
        self.interruptions = 0

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


def test_submit_voice_turn_streams_into_say_and_mirrors_the_exchange():
    brain = FakeBrain()
    session = FakeSession()
    publisher = StatePublisher()
    publisher.set_state(LISTENING)

    result = asyncio.run(app._submit_voice_turn(
        "tell me something", brain=brain, session=session, publisher=publisher,
    ))

    assert result == "First sentence. Second sentence."
    assert brain.calls == ["tell me something"]
    assert session.spoken == [["First sentence. ", "Second sentence."]]
    assert publisher.state == LISTENING
    assert [(line["role"], line["text"]) for line in publisher.snapshot()["transcript"]] == [
        ("user", "tell me something"),
        ("atlas", "First sentence. Second sentence."),
    ]


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


def test_tool_events_and_terminal_jobs_are_mirrored_and_spoken_only_when_engaged():
    publisher = StatePublisher()
    session = FakeSession()
    engagement = Engagement()
    engagement.wake()

    app._record_tool(publisher, "open", SimpleNamespace(status="ok"))
    succeeded = SimpleNamespace(state=JobState.SUCCEEDED, summary="  Draft   verified.  ")
    app._announce_terminal(succeeded, publisher, session, engagement)
    engagement.dismiss()
    failed = SimpleNamespace(state=JobState.FAILED, summary=None)
    app._announce_terminal(failed, publisher, session, engagement)

    lines = [(line["role"], line["text"]) for line in publisher.snapshot()["transcript"]]
    assert lines == [
        ("tool", "open: ok"),
        ("atlas", "Done — Draft verified."),
        ("atlas", "That task hit a problem; it's in History."),
    ]
    assert engagement.state != ENGAGED
    assert session.spoken == ["Done — Draft verified."]


def test_cancelled_completion_is_bounded():
    assert app._terminal_line(SimpleNamespace(
        state=JobState.CANCELLED, summary=None,
    )) == "Cancelled."
    line = app._terminal_line(SimpleNamespace(
        state=JobState.SUCCEEDED, summary="x" * 500,
    ))
    assert len(line) == 320
