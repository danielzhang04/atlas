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


def test_is_match_three_plus_tokens_in_order_subsequence_regardless_of_contiguity():
    # "meeting ... two ... five" appears in order but not contiguously.
    buffer = ["the", "meeting", "is", "at", "two", "fifty", "five"]
    assert app._echo_is_match(["meeting", "two", "five"], buffer, min_overlap_ratio=0.8)


def test_is_match_three_plus_tokens_out_of_order_relies_on_overlap_ratio():
    buffer = ["the", "meeting", "is", "at", "two", "fifty", "five"]
    # "five two meeting" is not an in-order subsequence, but all 3 tokens
    # are present somewhere -> 3/3 = 100% overlap clears the ratio gate.
    assert app._echo_is_match(["five", "two", "meeting"], buffer, min_overlap_ratio=0.8)


def test_is_match_overlap_ratio_boundary():
    buffer = ["alpha", "bravo", "charlie", "delta"]
    # 4 tokens, "echo" is the only non-match -> 3/4 = 75%.
    below_words = ["alpha", "bravo", "charlie", "echo"]
    assert not app._echo_is_match(below_words, buffer, min_overlap_ratio=0.8)
    assert app._echo_is_match(below_words, buffer, min_overlap_ratio=0.75)
    # 5 tokens, 4 match -> 80%, clears a 0.8 gate exactly.
    at_words = ["alpha", "bravo", "charlie", "delta", "echo"]
    assert app._echo_is_match(at_words, buffer, min_overlap_ratio=0.8)


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
