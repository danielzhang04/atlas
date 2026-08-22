import asyncio
from hashlib import sha256

import pytest

from worker.contracts import Lane, Request, utc_timestamp
from worker.frontdesk import FrontDesk
from worker.jobstore import JobStore
from worker.routing_policy import route
from worker.subscription_worker import WorkerHealth, WorkerHealthStatus
from worker.turn_interpreter import InterpretedTurn, TurnInterpretationError, TurnKind
from worker.voice_frontdesk import VoiceFrontDesk


def healthy():
    return WorkerHealth(WorkerHealthStatus.AVAILABLE, worker_id="voice-test", checked_at=utc_timestamp())


class FakePayloadCodec:
    codec_id = "test-xor-v1"

    def protect(self, plaintext, *, entropy):
        key = sha256(entropy).digest()
        return bytes(value ^ key[index % len(key)] for index, value in enumerate(plaintext))

    def unprotect(self, ciphertext, *, entropy):
        return self.protect(ciphertext, entropy=entropy)


class FakeInterpreter:
    def __init__(self, turn=None, error=None, narration="I queued that work."):
        self.turn = turn
        self.error = error
        self.narration = narration
        self.calls = []
        self.narration_calls = []

    async def interpret(self, raw, catalog):
        self.calls.append((raw, catalog))
        if self.error:
            raise self.error
        return self.turn

    async def narrate_route(self, turn, facts):
        self.narration_calls.append((turn, facts))
        return self.narration

def desk(tmp_path):
    store = JobStore(tmp_path / "voice.sqlite", payload_codec=FakePayloadCodec())
    return store, FrontDesk(store=store, worker_health=healthy())


def test_simple_conversational_reply_has_no_job(tmp_path):
    store, frontdesk = desk(tmp_path)
    try:
        interpreter = FakeInterpreter(InterpretedTurn(TurnKind.REPLY, "Hello there."))
        result = asyncio.run(VoiceFrontDesk(interpreter, frontdesk).handle("hello", catalog=[]))
        assert result.kind is TurnKind.REPLY
        assert result.text == "Hello there."
        assert result.job_id is None
        assert len(interpreter.calls) == 1
    finally:
        store.close()


def test_interpreter_error_fails_closed_without_admission(tmp_path):
    store, frontdesk = desk(tmp_path)
    try:
        result = asyncio.run(VoiceFrontDesk(
            FakeInterpreter(error=TurnInterpretationError("timeout")), frontdesk).handle("do it"))
        assert result.error_code == "conversation_timeout"
        assert result.text == "I lost that reply to a timeout. I'm still here. Try that again."
        assert "safe" not in result.text.lower()
        assert result.job_id is None
    finally:
        store.close()


def test_host_facts_are_narrated_by_the_conversational_model(tmp_path):
    store, frontdesk = desk(tmp_path)
    try:
        turn = InterpretedTurn(TurnKind.REQUEST, request=Request("calendar.create_event", target="event"))
        interpreter = FakeInterpreter(turn, narration="Got it. I queued the meeting.")
        result = asyncio.run(VoiceFrontDesk(interpreter, frontdesk).handle(
            "Schedule a meeting tomorrow at 3pm"
        ))
        assert result.text == "Got it. I queued the meeting."
        assert result.status == "queued"
        assert result.lane is Lane.FAST
        assert interpreter.narration_calls[0][1] == {
            "status": "queued", "lane": "fast", "error_code": None,
            "replayed": False, "job_visible": True,
        }
    finally:
        store.close()


def test_forged_calendar_request_cannot_bypass_heavy_raw_research(tmp_path):
    store, frontdesk = desk(tmp_path)
    try:
        turn = InterpretedTurn(TurnKind.REQUEST, request=Request("calendar.create_event", target="event"))
        result = asyncio.run(VoiceFrontDesk(FakeInterpreter(turn), frontdesk).handle(
            "Research these sources, then create a calendar event and verify it."
        ))
        assert result.lane is Lane.SLOW
        assert frontdesk.claim_next("fast", lane=Lane.FAST) is None
        assert frontdesk.claim_next("slow", lane=Lane.SLOW).job_id == result.job_id
    finally:
        store.close()


def test_conversational_response_never_creates_hidden_work(tmp_path):
    store, frontdesk = desk(tmp_path)
    try:
        turn = InterpretedTurn(TurnKind.REPLY, "Let's decide what the report should cover first.")
        result = asyncio.run(VoiceFrontDesk(FakeInterpreter(turn), frontdesk).handle("Write a Google Doc report."))
        assert result.kind is TurnKind.REPLY
        assert result.job_id is None
        assert result.text == "Let's decide what the report should cover first."
        assert store.recent_jobs() == ()
    finally:
        store.close()


def test_host_does_not_infer_an_action_from_conversational_text(tmp_path):
    store, frontdesk = desk(tmp_path)
    try:
        result = asyncio.run(VoiceFrontDesk(
            FakeInterpreter(InterpretedTurn(TurnKind.REPLY, "What time should I use?")), frontdesk
        ).handle("Schedule a meeting."))
        assert result.kind is TurnKind.REPLY
        assert result.text == "What time should I use?"
        assert result.job_id is None
    finally:
        store.close()


@pytest.mark.parametrize("raw", [
    "Can you schedule a meeting tomorrow?",
    "Could you set up an all-day event Friday?",
    "Would you arrange an appointment next week?",
])
def test_model_clarification_does_not_get_overridden_by_phrase_matching(tmp_path, raw):
    store, frontdesk = desk(tmp_path)
    try:
        result = asyncio.run(VoiceFrontDesk(
            FakeInterpreter(InterpretedTurn(TurnKind.REPLY, "What time should I use?")), frontdesk
        ).handle(raw))
        assert result.kind is TurnKind.REPLY
        assert result.text == "What time should I use?"
        assert result.job_id is None
    finally:
        store.close()


@pytest.mark.parametrize("raw", [
    "Can you teach me how to arrange an appointment?",
    "Please explain how to create a calendar event.",
    "I want to learn how to set up a meeting.",
])
def test_informational_calendar_request_with_model_reply_creates_no_job(tmp_path, raw):
    store, frontdesk = desk(tmp_path)
    try:
        result = asyncio.run(VoiceFrontDesk(
            FakeInterpreter(InterpretedTurn(TurnKind.REPLY, "Here is how.")), frontdesk
        ).handle(raw))
        assert result.kind is TurnKind.REPLY
        assert result.job_id is None
        assert result.text == "Here is how."
    finally:
        store.close()


def test_raw_web_search_and_calendar_action_is_slow(tmp_path):
    store, frontdesk = desk(tmp_path)
    try:
        turn = InterpretedTurn(TurnKind.REQUEST, request=Request("calendar.create_event", target="event"))
        result = asyncio.run(VoiceFrontDesk(FakeInterpreter(turn), frontdesk).handle(
            "Search the web for information and schedule one calendar event."
        ))
        assert result.lane is Lane.SLOW
    finally:
        store.close()


def test_destructive_and_web_summary_forgery_are_slow():
    forged = Request("calendar.create_event", target="event")
    assert route(forged, raw_utterance=r"Delete C:\Windows") .lane is Lane.SLOW
    assert route(forged, raw_utterance="Read first ten websites and produce a two page summary").lane is Lane.SLOW


def test_calendar_voice_shapes_include_all_day_set_up_and_read_question():
    assert route(Request("calendar.create_event", target="event"),
                 raw_utterance="Set up an all-day event tomorrow").lane is Lane.FAST
    assert route(Request("calendar.create_event", target="meeting"),
                 raw_utterance="Arrange a meeting next Tuesday at 09:30").lane is Lane.FAST
    assert route(Request("calendar.read_event", target="calendar"),
                 raw_utterance="What's on my calendar tomorrow?").lane is Lane.FAST
    assert route(Request("calendar.create_event", target="event"),
                 raw_utterance="How do I create an event?").lane is Lane.SLOW


@pytest.mark.parametrize("raw", [
    "Please explain how to create a calendar event.",
    "I want to learn how to set up a meeting.",
    "Can you teach me how to arrange an appointment?",
    "Show me how to create an event.",
])
def test_explanatory_calendar_create_requests_are_never_fast(raw):
    request = Request("calendar.create_event", target="event")
    assert route(request, raw_utterance=raw).lane is Lane.SLOW


def test_long_specific_single_calendar_event_is_slow_without_a_typed_fast_shape():
    request = Request("calendar.create_event", target="event")
    raw = "Schedule one calendar event with this detailed agenda: " + "bring notes " * 80
    assert route(request, raw_utterance=raw).lane is Lane.SLOW


def test_heavy_cues_are_conservatively_slow():
    cases = (
        ("Write a meaningful Google Doc", "document.compose"),
        ("Research and synthesize these sources", "calendar.create_event"),
        ("Open the browser, then use the desktop app", "calendar.create_event"),
    )
    for raw, operation in cases:
        assert route(Request(operation, target="x"), raw_utterance=raw).lane is Lane.SLOW
