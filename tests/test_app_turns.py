"""Voice-turn integration around reflexes, streaming speech, and work completion."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from worker import addressing as addressing_mod
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

    assert addressing_mod.vocabulary(cfg) == ["research", "draft"]


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
