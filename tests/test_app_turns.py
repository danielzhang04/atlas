"""Voice-turn integration around reflexes, streaming speech, and work completion."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from worker import app
from worker.addressing import Addressing
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
    engagement = Engagement(120)
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


def test_reflex_runs_before_timeout_check_and_never_calls_brain():
    clock = FakeClock()
    brain = FakeBrain()
    session = FakeSession()
    publisher = StatePublisher()
    engagement = Engagement(10, clock=clock)
    addressing = Addressing(5, (), clock=clock)
    engagement.wake()
    clock.value = 11

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

    assert session.interruptions == 1
    assert brain.calls == []
    assert engagement.state == "ENGAGED"


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


def test_address_vocab_combines_app_words_and_configured_terms(tmp_path: Path):
    apps_path = tmp_path / "apps.yaml"
    apps_path.write_text(
        "apps:\n  mail: {url: 'https://mail.example/', words: [email, inbox]}\n",
        encoding="utf-8",
    )

    assert app._address_vocab(
        {"address_vocab": ["research", "draft"]},
        apps_path=apps_path,
    ) == ("email", "inbox", "research", "draft")


def test_production_listening_config_matches_wave_two_defaults():
    cfg = app._cfg()

    assert cfg["engagement_timeout_s"] == 120
    assert cfg["address_window_s"] == 30
    assert cfg["address_vocab"] == [
        "email",
        "emails",
        "inbox",
        "mail",
        "calendar",
        "file",
        "files",
        "folder",
        "open",
        "close",
        "launch",
        "cancel",
        "status",
        "workers",
        "job",
        "jobs",
        "research",
        "summary",
        "write",
        "draft",
        "remind",
        "timer",
    ]


def test_cancelled_completion_is_bounded():
    assert app._terminal_line(SimpleNamespace(
        state=JobState.CANCELLED, summary=None,
    )) == "Cancelled."
    line = app._terminal_line(SimpleNamespace(
        state=JobState.SUCCEEDED, summary="x" * 500,
    ))
    assert len(line) == 320
