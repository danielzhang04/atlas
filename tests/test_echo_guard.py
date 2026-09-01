"""CC5 rework: THE FIX -- deterministic transcript echo-suppression.

worker.app.SpeechEchoGuard is what actually stops Atlas's own TTS output
from re-entering the mic and being treated as a user turn (the three
AgentSession knobs in config/atlas.yaml's `voice:` section do not, per the
adversarial review -- see the diagnosis comment at the AgentSession
construction in worker/app.py).

Two blockers found in re-review of the first pass, both fixed here:

  BLOCKER 1: a single common word ("stop" while Atlas says "I'll stop the
  music") was being silently dropped whenever it overlapped the buffer at
  all -- worse than the min_interruption_words knob it fronts, since it ate
  exactly the most likely genuine barge-in words. Fixed with a tiered match
  rule in _echo_is_match: 1 token never drops; 2 tokens only drop on a
  contiguous bigram match; 3+ tokens keep the original subsequence/overlap
  rule.

  BLOCKER 2: the buffer was evicted on a fixed wall-clock window measured
  from when record_spoken was called -- but record_spoken fires as text
  drains out of the LLM/TTS pipeline, which has no backpressure from actual
  audio playout, so a long reply's words could all be "recorded" many
  seconds before its audio (and the tail's own echo) finishes playing.
  Fixed by anchoring eviction to the SPEECH lifecycle (session.agent_state)
  instead: the buffer never time-evicts while speaking=True, and the
  eviction clock (tail_s, then buffer_window_s) only starts once speaking
  is confirmed to have stopped.

These tests are mutation-grade: exact echo, tail-fragment echo, tiered
single/double/triple-word matching, genuinely different words, a
generation-finishes-early long reply, echo within/after the post-speech
tail+window, buffer capping, the SPEAKING-state gate, and the dropped-event
counter. Also covers AtlasAgent.stt_node, the hook point that actually
applies the guard to livekit's STT event stream.
"""
from __future__ import annotations

import asyncio

import pytest
from livekit.agents import stt

from worker import app


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _guard(**kwargs) -> tuple[app.SpeechEchoGuard, _FakeClock]:
    clock = _FakeClock()
    guard = app.SpeechEchoGuard(clock=clock, **kwargs)
    return guard, clock


# ---------------------------------------------------------------------------
# _echo_normalize / _echo_is_match: the pure matching primitives
# ---------------------------------------------------------------------------

def test_normalize_casefolds_and_strips_punctuation():
    assert app._echo_normalize("Two, fifty-five!") == ["two", "fifty", "five"]


def test_normalize_empty_and_non_string_are_empty():
    assert app._echo_normalize("") == []
    assert app._echo_normalize(None) == []  # type: ignore[arg-type]
    assert app._echo_normalize("   ") == []


def test_is_match_empty_transcript_never_matches():
    assert not app._echo_is_match([], ["alpha", "bravo"], min_overlap_ratio=0.0)


# --- BLOCKER 1: tiered match rules -----------------------------------------

def test_is_match_single_token_never_matches_even_if_present():
    # "stop" appears in the buffer, but a lone word must never be treated
    # as an echo -- see the class/function docstrings for why.
    buffer = ["i", "ll", "stop", "the", "music"]
    for word in ("stop", "no", "yes"):
        assert not app._echo_is_match([word], buffer, min_overlap_ratio=0.0)


def test_is_match_two_tokens_requires_a_contiguous_bigram():
    buffer = ["i", "ll", "stop", "the", "music"]
    # "stop the" is exactly how Atlas said it, back to back -> echo.
    assert app._echo_is_match(["stop", "the"], buffer, min_overlap_ratio=0.0)
    # "stop it" is NOT contiguous in the buffer (Atlas said "stop the",
    # not "stop it") -> not an echo, even though both share "stop".
    assert not app._echo_is_match(["stop", "it"], buffer, min_overlap_ratio=0.0)
    # Reversed order doesn't count as the same bigram.
    assert not app._echo_is_match(["the", "stop"], buffer, min_overlap_ratio=0.0)


# --- F2(a): contiguity now covers 3-4 tokens too ---------------------------

def test_is_match_three_and_four_tokens_require_contiguity():
    buffer = ["the", "meeting", "is", "at", "two", "fifty", "five"]
    # Contiguous tail fragment -> still an echo.
    assert app._echo_is_match(["two", "fifty", "five"], buffer, min_overlap_ratio=0.8)
    assert app._echo_is_match(["at", "two", "fifty", "five"], buffer, min_overlap_ratio=0.8)
    # "meeting ... two ... five" is in order but scattered: at three words
    # that is chance overlap with a long reply, not an echo.
    assert not app._echo_is_match(["meeting", "two", "five"], buffer, min_overlap_ratio=0.8)
    # Out of order is not an echo either, whatever the overlap ratio says.
    assert not app._echo_is_match(["five", "two", "meeting"], buffer, min_overlap_ratio=0.8)


def test_is_match_contiguity_is_not_backwards_between_lengths():
    # The old rule protected "stop it" (2 tokens, contiguity required) but
    # not the longer, more specific "stop it now" (3 tokens, subsequence).
    buffer = ["i", "ll", "stop", "the", "music", "now", "if", "you", "want"]
    assert not app._echo_is_match(["stop", "it"], buffer, min_overlap_ratio=0.8)
    assert not app._echo_is_match(["stop", "it", "now"], buffer, min_overlap_ratio=0.8)


def test_is_match_five_tokens_keep_the_subsequence_rule():
    buffer = ["the", "meeting", "today", "is", "at", "two", "fifty", "five", "sharp"]
    # Five in-order words scattered through the buffer is unambiguous.
    assert app._echo_is_match(
        ["meeting", "is", "two", "fifty", "five"], buffer, min_overlap_ratio=0.8,
    )


def test_is_match_overlap_ratio_boundary():
    buffer = ["alpha", "bravo", "charlie", "delta", "foxtrot"]
    # 5 tokens, "echo" is the only non-match -> 4/5 = 80%, clears a 0.8 gate
    # exactly; the same words against a 0.9 gate do not.
    words = ["alpha", "bravo", "charlie", "delta", "echo"]
    assert app._echo_is_match(words, buffer, min_overlap_ratio=0.8)
    assert not app._echo_is_match(words, buffer, min_overlap_ratio=0.9)
    # 6 tokens, 4 match -> 67%, below the gate and not in-order-complete.
    below = ["alpha", "bravo", "charlie", "delta", "echo", "golf"]
    assert not app._echo_is_match(below, buffer, min_overlap_ratio=0.8)


# ---------------------------------------------------------------------------
# SpeechEchoGuard: mutation-grade filter behavior
# ---------------------------------------------------------------------------

def test_exact_echo_is_dropped_while_speaking():
    guard, _clock = _guard()
    guard.record_spoken("The meeting is at two fifty five.")
    assert guard.should_drop("The meeting is at two fifty five", speaking=True)


def test_partial_echo_tail_fragment_is_dropped_while_speaking():
    guard, _clock = _guard()
    guard.record_spoken("The meeting is at two fifty five.")
    # The diagnosis's own example: STT only catches the tail of what Atlas
    # said, as an in-order subsequence of the buffer.
    assert guard.should_drop("two fifty five", speaking=True)


def test_genuinely_different_user_words_pass_through():
    guard, _clock = _guard()
    guard.record_spoken("The meeting is at two fifty five.")
    assert not guard.should_drop("cancel that meeting", speaking=True)


# --- BLOCKER 1, at the should_drop level (Atlas said "I'll stop the music") -

def test_single_common_words_always_pass_through_even_while_speaking():
    guard, _clock = _guard()
    guard.record_spoken("I'll stop the music for you now")
    for word in ("stop", "no", "yes"):
        assert not guard.should_drop(word, speaking=True)


def test_two_word_non_contiguous_echo_passes_through():
    guard, _clock = _guard()
    guard.record_spoken("I'll stop the music for you now")
    assert not guard.should_drop("stop it", speaking=True)


def test_two_word_contiguous_echo_is_dropped():
    guard, _clock = _guard()
    guard.record_spoken("I'll stop the music for you now")
    assert guard.should_drop("stop the", speaking=True)


# --- F2(a) at the should_drop level: the reviewer's live probes -------------

_LONG_REPLY = (
    "There are three things on the calendar that stand out today. "
    "The first one is the design review at ten, which usually runs long, "
    "so I would close out anything else before that. "
    "After that there is a gap until one, and then the vendor call, "
    "which is the one that keeps moving around in the week. "
    "The last one is a check in with the team late in the afternoon, "
    "and I can move that one if you would rather keep the evening clear. "
    "Where things stand right now, nothing is double booked, "
    "and the only travel is the trip out to the office on Thursday. "
    "I can read any of that back in more detail if you want it."
)


def test_genuine_barge_in_survives_a_long_reply_in_the_buffer():
    # The reviewer's probes: every word of these is somewhere in a 100+ word
    # reply, and the old 3-token subsequence rule ate both of them.
    guard, _clock = _guard()
    guard.record_spoken(_LONG_REPLY)
    assert not guard.should_drop("close that one", speaking=True)
    assert not guard.should_drop("where is that one", speaking=True)
    assert guard.dropped_count == 0


def test_a_contiguous_fragment_of_that_same_long_reply_is_still_dropped():
    guard, _clock = _guard()
    guard.record_spoken(_LONG_REPLY)
    # Said back to back by Atlas -> a real echo, and still caught.
    assert guard.should_drop("the design review at ten", speaking=True)
    assert guard.should_drop("move that one", speaking=True)


def test_the_diagnosed_echo_is_still_dropped_after_the_tightening():
    guard, _clock = _guard()
    guard.record_spoken("The meeting is at two fifty five.")
    assert guard.should_drop("two fifty five", speaking=True)


# --- F2(b): the recency bound ----------------------------------------------

def test_a_new_utterance_clears_the_previous_reply_from_the_buffer():
    guard, clock = _guard(tail_s=1.0, buffer_window_s=600.0)
    guard.record_spoken("The meeting is at two fifty five.")
    guard.note_speaking(True)  # that reply's speech ends at T=0
    clock.advance(180.0)  # three minutes later, Daniel says something else
    # buffer_window_s is far away, so only the recency bound can clear this.
    assert not guard.should_drop("what about tomorrow instead", speaking=False)
    guard.record_spoken("Okay, I will hold off on that.")  # a new utterance
    guard.note_speaking(True)

    # Words from the earlier reply are no longer matchable against anything.
    assert not guard.should_drop("two fifty five", speaking=True)
    assert not guard.should_drop("the meeting is at two fifty five", speaking=True)
    # The utterance actually being spoken is still guarded.
    assert guard.should_drop("i will hold off", speaking=True)


def test_a_long_reply_is_never_cleared_out_from_under_itself():
    guard, clock = _guard(tail_s=1.0, buffer_window_s=600.0)
    # Production shape after the session's first reply: a previous utterance
    # set the speaking anchor, then went quiet past the tail.
    guard.note_speaking(True)
    clock.advance(120.0)
    # Text drains chunk by chunk; between chunks an STT event lands in the TTS
    # time-to-first-byte gap while agent_state is not yet "speaking" (the F2b
    # re-review race) and must NOT reset the latch and wipe earlier chunks.
    guard.record_spoken("the meeting today is scheduled for three")
    guard.note_speaking(False)
    clock.advance(0.5)  # text drains fast (no backpressure); sub-tail gaps
    guard.record_spoken("oclock this afternoon in the main office")
    guard.note_speaking(False)
    clock.advance(0.5)
    guard.record_spoken("with the whole team")
    assert guard.should_drop("scheduled for three oclock", speaking=True)


def test_rapid_consecutive_utterances_still_share_one_buffer():
    guard, clock = _guard(tail_s=1.0, buffer_window_s=600.0)
    guard.record_spoken("The meeting is at two fifty five.")
    guard.note_speaking(True)
    clock.advance(0.4)  # still inside the tail: the audio overlaps
    guard.record_spoken("Anything else?")
    guard.note_speaking(True)
    assert guard.should_drop("two fifty five", speaking=True)


# --- BLOCKER 2: eviction anchored to the speech lifecycle, not record time -

def test_long_reply_tail_survives_generation_finishing_early():
    """record_spoken drains as fast as text generates (T=0-3 here), NOT
    paced by actual audio playout, which can run much longer for a long
    reply. The buffer must not evict mid-utterance just because wall clock
    has moved past buffer_window_s, as long as agent_state is still
    "speaking"."""
    guard, clock = _guard(tail_s=1.0, buffer_window_s=10.0)
    clock.value = 0.0
    guard.record_spoken("the meeting today is scheduled for three")
    clock.value = 1.5
    guard.record_spoken("oclock this afternoon in the main office")
    clock.value = 3.0  # all text drained by T=3 for an 18s-long reply
    clock.value = 18.0  # far past buffer_window_s(10), but STILL SPEAKING
    assert guard.should_drop("three oclock", speaking=True)


def test_echo_shortly_after_a_long_reply_ends_is_still_dropped():
    guard, clock = _guard(tail_s=1.0, buffer_window_s=10.0)
    clock.value = 0.0
    guard.record_spoken("the meeting today is scheduled for three oclock")
    clock.value = 18.0
    guard.note_speaking(True)  # last confirmed speaking at T=18 (speech end)
    clock.value = 18.5  # speaking_end + 0.5, inside the 1s tail
    assert guard.should_drop("three oclock", speaking=False)


def test_unrelated_words_well_after_a_long_reply_ends_pass_through():
    guard, clock = _guard(tail_s=1.0, buffer_window_s=3.0)
    clock.value = 0.0
    guard.record_spoken("the meeting today is scheduled for three oclock")
    clock.value = 18.0
    guard.note_speaking(True)  # speaking confirmed to have ended at T=18
    clock.value = 18.0 + 5.0  # speaking_end + 5, past tail_s(1)+window(3)=4
    assert not guard.should_drop("cancel that entirely instead", speaking=False)


def test_echo_after_the_tail_window_passes():
    guard, clock = _guard(tail_s=1.0)
    guard.record_spoken("two fifty five")
    guard.note_speaking(True)  # confirm speaking at T=0
    clock.advance(1.5)  # past the 1s tail
    assert not guard.should_drop("two fifty five", speaking=False)


def test_echo_within_the_tail_window_after_speaking_stops_is_still_dropped():
    guard, clock = _guard(tail_s=1.0)
    guard.record_spoken("two fifty five")
    guard.note_speaking(True)
    clock.advance(0.5)  # inside the 1s tail
    assert guard.should_drop("two fifty five", speaking=False)


def test_nothing_is_filtered_while_listening_with_no_recent_speech():
    guard, _clock = _guard()
    # Atlas has never been confirmed speaking; ordinary listening-state
    # speech is never touched, regardless of buffer content.
    assert not guard.should_drop("hey atlas what's on my calendar", speaking=False)


def test_buffer_is_capped_by_word_count_while_speaking():
    guard, _clock = _guard(max_words=5, buffer_window_s=1000.0)
    guard.record_spoken("alpha bravo charlie delta echo foxtrot golf")
    # Only the last 5 words survive the cap: charlie delta echo foxtrot golf.
    assert not guard.should_drop("alpha bravo", speaking=True)  # evicted pair
    assert guard.should_drop("delta echo foxtrot", speaking=True)  # still present


def test_dropped_count_increments_only_on_actual_drops():
    guard, _clock = _guard()
    guard.record_spoken("two fifty five")
    assert guard.dropped_count == 0
    guard.should_drop("cancel that meeting", speaking=True)  # no match
    assert guard.dropped_count == 0
    guard.should_drop("two fifty five", speaking=True)  # matches
    assert guard.dropped_count == 1
    guard.should_drop("two fifty five", speaking=True)  # matches again
    assert guard.dropped_count == 2


def test_dropped_count_is_zero_outside_the_filter_window_even_if_it_would_match():
    guard, clock = _guard(tail_s=1.0)
    guard.record_spoken("two fifty five")
    guard.note_speaking(True)
    clock.advance(5.0)
    guard.should_drop("two fifty five", speaking=False)
    assert guard.dropped_count == 0


def test_record_spoken_ignores_empty_or_whitespace_chunks():
    guard, _clock = _guard()
    guard.record_spoken("")
    guard.record_spoken("   ")
    assert not guard.should_drop("anything else entirely", speaking=True)


def test_echo_guard_logs_a_bounded_counter_only_never_the_transcript_text(caplog):
    import logging

    guard, _clock = _guard()
    guard.record_spoken("two fifty five")
    with caplog.at_level(logging.DEBUG, logger="atlas.app"):
        guard.should_drop("two fifty five", speaking=True)
    messages = [record.getMessage() for record in caplog.records]
    assert any("total=1" in message for message in messages)
    assert not any("two fifty five" in message for message in messages)


# ---------------------------------------------------------------------------
# AtlasAgent.stt_node: the hook point that applies the guard
# ---------------------------------------------------------------------------

def _speech_event(kind: stt.SpeechEventType, text: str) -> stt.SpeechEvent:
    return stt.SpeechEvent(
        type=kind, alternatives=[stt.SpeechData(language="en", text=text)],
    )


class _FakeSessionState:
    def __init__(self, agent_state: str) -> None:
        self.agent_state = agent_state


class _SttWiringAgent(app.AtlasAgent):
    """AtlasAgent with `session` overridden so stt_node can be exercised
    without a live AgentSession/JobContext."""

    def __init__(self, *, agent_state: str) -> None:
        super().__init__(instructions="test", llm=None, tools=[])
        self._fake_session = _FakeSessionState(agent_state)

    @property
    def session(self):
        return self._fake_session


def _patch_default_stt_stream(monkeypatch, events) -> None:
    async def _fake_default_stream(_agent, _audio, _model_settings):
        for event in events:
            yield event

    monkeypatch.setattr(app.Agent.default, "stt_node", _fake_default_stream)


async def _collect(agent) -> list[stt.SpeechEvent]:
    return [event async for event in agent.stt_node(None, None)]


def test_stt_node_drops_an_echoing_interim_transcript_while_speaking(monkeypatch):
    agent = _SttWiringAgent(agent_state="speaking")
    agent.echo_guard.record_spoken("two fifty five")
    events = [_speech_event(stt.SpeechEventType.INTERIM_TRANSCRIPT, "two fifty five")]
    _patch_default_stt_stream(monkeypatch, events)

    result = asyncio.run(_collect(agent))

    assert result == []
    assert agent.echo_guard.dropped_count == 1


def test_stt_node_passes_through_genuine_speech_while_speaking(monkeypatch):
    agent = _SttWiringAgent(agent_state="speaking")
    agent.echo_guard.record_spoken("two fifty five")
    events = [_speech_event(stt.SpeechEventType.INTERIM_TRANSCRIPT, "cancel that meeting")]
    _patch_default_stt_stream(monkeypatch, events)

    result = asyncio.run(_collect(agent))

    assert result == events
    assert agent.echo_guard.dropped_count == 0


def test_stt_node_never_drops_a_single_common_word_while_speaking(monkeypatch):
    agent = _SttWiringAgent(agent_state="speaking")
    agent.echo_guard.record_spoken("I'll stop the music for you now")
    events = [_speech_event(stt.SpeechEventType.INTERIM_TRANSCRIPT, "stop")]
    _patch_default_stt_stream(monkeypatch, events)

    result = asyncio.run(_collect(agent))

    assert result == events
    assert agent.echo_guard.dropped_count == 0


def test_stt_node_passes_through_matching_text_while_listening(monkeypatch):
    # Genuine barge-in is only about *content*; the guard also requires the
    # SPEAKING(+tail) window. Never having observed speaking=True means
    # note_speaking never set an anchor, so the window is never open here,
    # independent of the timing boundary itself (covered separately above).
    agent = _SttWiringAgent(agent_state="listening")
    agent.echo_guard.record_spoken("two fifty five")
    events = [_speech_event(stt.SpeechEventType.INTERIM_TRANSCRIPT, "two fifty five")]
    _patch_default_stt_stream(monkeypatch, events)

    result = asyncio.run(_collect(agent))

    assert result == events


def test_stt_node_never_filters_non_transcript_event_types(monkeypatch):
    agent = _SttWiringAgent(agent_state="speaking")
    agent.echo_guard.record_spoken("two fifty five")
    start_event = stt.SpeechEvent(type=stt.SpeechEventType.START_OF_SPEECH)
    _patch_default_stt_stream(monkeypatch, [start_event])

    result = asyncio.run(_collect(agent))

    assert result == [start_event]


def test_stt_node_drops_an_echoing_final_transcript_while_speaking(monkeypatch):
    """F5(b): FINAL_TRANSCRIPT is in _ECHO_FILTERED_EVENT_TYPES and nothing
    pinned it -- yet it is the event that actually becomes a user turn. An
    interim event that slips through is a spurious interruption; a final one
    that slips through is a whole turn Atlas answers itself with."""
    agent = _SttWiringAgent(agent_state="speaking")
    agent.echo_guard.record_spoken("The meeting is at two fifty five.")
    events = [_speech_event(stt.SpeechEventType.FINAL_TRANSCRIPT, "two fifty five")]
    _patch_default_stt_stream(monkeypatch, events)

    result = asyncio.run(_collect(agent))

    assert result == []
    assert agent.echo_guard.dropped_count == 1


def test_stt_node_drops_an_echoing_preflight_transcript_while_speaking(monkeypatch):
    agent = _SttWiringAgent(agent_state="speaking")
    agent.echo_guard.record_spoken("The meeting is at two fifty five.")
    events = [_speech_event(stt.SpeechEventType.PREFLIGHT_TRANSCRIPT, "two fifty five")]
    _patch_default_stt_stream(monkeypatch, events)

    result = asyncio.run(_collect(agent))

    assert result == []
    assert agent.echo_guard.dropped_count == 1


def test_tts_node_feeds_every_chunk_it_speaks_into_the_echo_guard(monkeypatch):
    """F5(a): the whole guard rests on this one feed line in tts_node --
    without it the buffer is always empty and the filter silently never
    fires, with every should_drop test above still passing."""
    agent = _SttWiringAgent(agent_state="speaking")

    async def _fake_default_tts(_agent, text, _model_settings):
        async for chunk in text:
            yield chunk

    monkeypatch.setattr(app.Agent.default, "tts_node", _fake_default_tts)

    async def _text():
        yield "The meeting is at "
        yield "two fifty five."

    async def _drain():
        return [chunk async for chunk in agent.tts_node(_text(), None)]

    spoken = asyncio.run(_drain())

    assert "".join(spoken).strip() == "The meeting is at two fifty five."
    # Both chunks reached the guard, in the order Atlas speaks them.
    assert agent.echo_guard.should_drop("the meeting is at", speaking=True)
    assert agent.echo_guard.should_drop("two fifty five", speaking=True)


def test_stt_node_notes_speaking_state_even_on_non_transcript_events(monkeypatch):
    """BLOCKER 2's note_speaking-on-every-event wiring: a non-transcript
    event (e.g. END_OF_SPEECH, or a periodic RECOGNITION_USAGE) while
    speaking=True still refreshes the guard's anchor, so a transcript event
    arriving immediately after still sees a fresh window."""
    agent = _SttWiringAgent(agent_state="speaking")
    agent.echo_guard.record_spoken("two fifty five")
    events = [
        stt.SpeechEvent(type=stt.SpeechEventType.END_OF_SPEECH),
        _speech_event(stt.SpeechEventType.INTERIM_TRANSCRIPT, "two fifty five"),
    ]
    _patch_default_stt_stream(monkeypatch, events)

    result = asyncio.run(_collect(agent))

    assert result == [events[0]]
    assert agent.echo_guard.dropped_count == 1


def test_within_echo_window_reports_the_same_window_should_drop_filters_in():
    """The reflex lane's gate on destroying a pending action (unit DD-1).

    A one-token "stop" is never dropped as an echo, so the only way to tell a
    self-echo from a real one is WHEN it arrived: inside the speaking window
    (or its short tail) it may be Atlas's own voice; after that it cannot be.
    """
    guard, clock = _guard(tail_s=1.0)

    # Nothing has been said yet: an utterance now is certainly Daniel's.
    assert guard.within_echo_window() is False

    guard.record_spoken("the draft is ready, yes or no?")
    guard.note_speaking(True)
    assert guard.within_echo_window() is True

    # Speech stops; the tail is still suspect...
    guard.note_speaking(False)
    clock.advance(0.5)
    assert guard.within_echo_window() is True

    # ...and past the tail it is the quiet period again.
    clock.advance(1.0)
    assert guard.within_echo_window() is False
