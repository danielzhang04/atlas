"""Run the standalone Atlas LiveKit voice worker."""
from __future__ import annotations

import asyncio
from collections import Counter, deque
import inspect
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any, Awaitable, Callable, Sequence

import yaml

from livekit.agents import Agent, AgentSession, JobContext, StopResponse, WorkerOptions, cli, stt
from livekit.plugins import deepgram, silero

from worker import brain as brain_mod
from worker import desktopapps
from worker import devicewatch
from worker import engagement as engagement_mod
from worker import envload, jobobject, router, runtime, sanitize, state, stateserver
from worker import tools as tools_mod
from worker import wakeword, work as work_mod
from worker.jobstore import JobState

__all__ = ["AtlasAgent", "entrypoint", "main"]

ATLAS = Path(__file__).resolve().parents[1]
logger = logging.getLogger("atlas.app")

TTS_VOICE = "aura-2-andromeda-en"
FOLLOW_SENTINEL = devicewatch.FOLLOW_SENTINEL
TEXT_MODE = "--text" in sys.argv
DEFAULT_DISMISS = ["that's all", "go to sleep"]
WAKE_LINE = "Hey boss. What can I do for you?"
# Spoken when a reflex reaches no branch that says anything -- "repeat" with
# nothing to repeat, or an intent this funnel does not route.
REPEAT_FALLBACK = "I don't have anything to repeat yet. "
# A reflex "cancel" that drops a pending action says so. Silence there reads as
# the cancel not landing, and the pending would still be waiting.
PENDING_CANCELLED_REPLY = "Cancelled. "
REFLEX_FALLBACK = "I heard that, but I don't have a way to act on it. "
# What the deterministic open lane says out loud. Every one of these is a
# sentence about what the HOST just did, said the way Daniel would say it --
# nothing here reports a model's intention, and nothing here is spoken unless
# the host tool it describes has already returned.
REFLEX_OPENING = "Opening {name}. "
REFLEX_ALREADY_OPEN = "{name} was already open. I brought it to the front. "
REFLEX_OPEN_FAILED = "I couldn't open {name}. "
REFLEX_BROUGHT_BACK = "Bringing that back. "
REFLEX_NOTHING_TO_BRING_BACK = "I haven't opened anything to bring back yet. "
REFLEX_BRING_BACK_FAILED = "I couldn't bring that back. "
# Which host tool answers each kind of match, and with what arguments. Every
# value that reaches a tool is host-resolved (constitution rules 3 and 7): the
# model supplies nothing here, and the only arguments are words out of the
# host's own closed alias/root vocabularies.
#
# Every entry must ALSO be instant-tier, and that is not asserted here -- it
# is checked against the live registry in _match_reflex_open, because this
# table cannot know what a tool was registered as. A confirm-tier tool reached
# from this lane would run, mint a pending action, and have its readback
# swallowed: Atlas would say "I couldn't open gmail" while a live single-use
# pending sat waiting for the next bare "yes" (rules 4 and 5).
_REFLEX_OPEN_TOOLS = {
    "alias": lambda name: ("open", {"target": name}),
    "root": lambda name: ("open_folder", {"root": name}),
    "last": lambda _name: ("focus_last_opened", {}),
}
SLEEP_LINE = "Okay, sleeping. Wake me when you need something."
_BG_TASKS: set[asyncio.Task] = set()
RESTART_EXIT_CODE = 21
_worker_exit_code = 0


def _build_tts(cfg: dict):
    entry = (cfg.get("voices") or {}).get(cfg.get("active_voice") or "")
    if not entry:
        return deepgram.TTS(model=TTS_VOICE)
    if entry["vendor"] == "deepgram":
        return deepgram.TTS(model=entry["model"])
    if entry["vendor"] == "elevenlabs":
        from livekit.agents.utils import http_context
        from livekit.plugins import elevenlabs

        return elevenlabs.TTS(
            voice_id=entry["voice_id"],
            model=entry["model"],
            api_key=os.environ.get("ELEVENLABS_API_KEY"),
            http_session=http_context.http_session(),
        )
    raise ValueError(f"unknown voice vendor: {entry['vendor']}")


def _load_intents() -> dict:
    path = ATLAS / "config" / "intents.yaml"
    if path.is_file():
        return router.load_intents(path)
    logger.warning("intents.yaml missing; reflex lane limited to dismiss")
    return {"dismiss": {"phrases": DEFAULT_DISMISS}}


def _cfg() -> dict:
    return yaml.safe_load((ATLAS / "config" / "atlas.yaml").read_text(encoding="utf-8"))


_VOICE_DEFAULTS = {
    "min_interruption_words": 2,
    "min_interruption_duration_s": 0.8,
    "aec_warmup_duration_s": 4.0,
    # See TurnAck and the `voice:` comment in config/atlas.yaml for how 1.8
    # was chosen from Daniel's own trace database.
    "ack_delay_s": 1.8,
    # Neither of these claims anything. See _ack_lines: at the moment an ack
    # is spoken the host does not yet know what kind of turn this is.
    "ack_lines": ("One second. ", "Still with you. "),
}
_ACK_LINE_LIMIT = 60
_ACK_LINE_COUNT = 8
# Marks a transcript line as the instant ack rather than an answer. The only
# reader is _last_atlas_line ("say that again"); it rides the existing
# per-line `source` field, which nothing else in the host or the UI reads.
ACK_LINE_SOURCE = "ack"
# An ack has to be true in every world the turn could still turn out to be.
# It is spoken when the FIRST spoken chunk has not arrived within
# ack_delay_s, and that deadline pops while the model is still in its first
# round -- measured at 4.4-12.6s on tool turns, so essentially always before
# any tool call has been made. Nothing has run, nothing has been decided, and
# a confirm-tier pending does not exist yet, so the host cannot tell a
# question apart from a mutating action about to ask for permission.
#
# "On it." in front of "NOT EXECUTED. Read this back to Daniel and wait for
# his yes or no" is Atlas saying it is doing the thing it is about to ask
# permission for. That is the failure this rule exists for, and it is not
# repairable by checking the pending: at ack time the information does not
# exist. It is repairable in the WORDS. An ack may say Atlas is present and
# that time is passing, and NOTHING else.
#
# ALLOWLIST, not a blocklist, and the shape is the point. The first version of
# this check listed the ways an ack could be wrong -- first-person subjects,
# action stems, a few phrases -- and review broke it seventeen ways with plain
# English that named no verb from the list ("It's underway.", "In progress.",
# "Consider it handled.", "Task accepted.", "Sure thing.") plus two languages
# it had never heard of. There is no finite list of ways to claim an action,
# so enumerating them is a losing race. There IS a finite list of words that
# assert only time or presence, because that is the entire permitted meaning,
# and it is about thirty words long. Every token of a configured ack line must
# come from it.
#
# It also closes the homoglyph seam for free, with no normalization tricks: a
# Cyrillic "О" in "Оn it." simply is not any of these tokens, and the
# character gate below rejects it before tokenizing anyway.
#
# Still a backstop, not a proof: these words can be assembled into something
# clumsy, and nothing here judges tone. It guarantees only that an ack cannot
# assert an action, which is the property truthfulness depends on. A new line
# is still read by a human, and adding a word here is a code change that gets
# reviewed -- which is the correct amount of friction for a sentence Atlas
# says in front of a mutating readback.
_ACK_WORDS = frozenset({
    # Time passing. No verb of doing, no object, nothing a task could be.
    "a", "an", "one", "two", "few", "just", "moment", "moments", "second",
    "seconds", "sec", "minute", "minutes", "bit", "while", "soon", "shortly",
    "longer", "more", "another", "hang", "hold", "on", "yet",
    # Presence. Second person only: "you" cannot be the actor of an Atlas
    # claim, and there is deliberately no first-person word in this set, so a
    # line built from it has no subject to hang an action on.
    "still", "here", "there", "with", "you",
    # Connective and courtesy.
    "and", "okay", "ok", "please",
})
# The exact characters an ack line may contain. Letters are ASCII only ON
# PURPOSE: it is what stops a homoglyph ("Оn it." with a Cyrillic О) from
# reaching the tokenizer at all, and it also rejects zero-width joiners,
# digits, and anything else invisible in a config diff. Checked against the
# string that will actually be SPOKEN, not against a normalized copy of it --
# the previous version validated router.normalize(value) and then spoke
# value.strip(), which are different strings whenever the line is not pure
# ASCII, and that gap was the whole bug.
_ACK_ALLOWED_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ .,'!?-"
)
_ACK_WORD_SPLIT = re.compile(r"[^a-z]+")
# SpeechEchoGuard's tunables (2026-09-01 final gate, F7). These are the knobs
# that decide whether a genuine barge-in survives, and they were reachable
# only by editing the class default -- while the three livekit knobs above,
# which the diagnosis showed do NOT fix self-barge-in, were the configurable
# ones. Same names as the constructor keywords, so the mapping is 1:1 and a
# value here cannot silently mean something else in code.
_ECHO_GUARD_DEFAULTS = {
    "tail_s": 1.0,
    "buffer_window_s": 10.0,
    "max_words": 500,
    "min_overlap_ratio": 0.8,
}


def _voice_config(cfg: dict) -> dict:
    """Validate & resolve the `voice:` config section on load (see the
    diagnosis comment at the AgentSession construction below). Missing
    section or missing keys fall back to _VOICE_DEFAULTS; present keys are
    type/range checked and raise, matching worker.runtime.build's style,
    rather than silently falling back to a value nobody chose."""
    raw = cfg.get("voice") or {}
    if not isinstance(raw, dict):
        raise ValueError("invalid Atlas configuration: voice")
    words = raw.get("min_interruption_words", _VOICE_DEFAULTS["min_interruption_words"])
    if isinstance(words, bool) or not isinstance(words, int) or words < 0:
        raise ValueError("invalid Atlas configuration: voice.min_interruption_words")
    duration = raw.get(
        "min_interruption_duration_s", _VOICE_DEFAULTS["min_interruption_duration_s"],
    )
    if (isinstance(duration, bool) or not isinstance(duration, (int, float))
            or duration <= 0):
        raise ValueError("invalid Atlas configuration: voice.min_interruption_duration_s")
    warmup = raw.get("aec_warmup_duration_s", _VOICE_DEFAULTS["aec_warmup_duration_s"])
    if isinstance(warmup, bool) or not isinstance(warmup, (int, float)) or warmup <= 0:
        raise ValueError("invalid Atlas configuration: voice.aec_warmup_duration_s")
    ack_delay = raw.get("ack_delay_s", _VOICE_DEFAULTS["ack_delay_s"])
    if (isinstance(ack_delay, bool) or not isinstance(ack_delay, (int, float))
            or ack_delay <= 0):
        raise ValueError("invalid Atlas configuration: voice.ack_delay_s")
    return {
        "min_interruption_words": words,
        "min_interruption_duration_s": float(duration),
        "aec_warmup_duration_s": float(warmup),
        "ack_delay_s": float(ack_delay),
        "ack_lines": _ack_lines(raw),
        "echo_guard": _echo_guard_config(raw),
    }


def _ack_lines(raw: dict) -> tuple[str, ...]:
    """Validate `voice.ack_lines` -- what Atlas says while a slow turn runs.

    Bounded in both directions on purpose: these are spoken over the top of
    Daniel waiting, so a long one costs more silence than it buys, and a
    hundred variants would just be a hundred ways to be wrong. An empty list
    is a valid way to turn the ack off, which is why it does not raise.

    Three content rules, all refusals rather than fallbacks, and all three
    applied to `spoken` -- the exact string this function is about to hand
    back for TTS. Validating anything else (a normalized copy, say) is the
    "checks one string, speaks another" bug this had in review: a Cyrillic
    "О" vanished under router.normalize, so "Оn it." validated as "n it" and
    then went to the speaker verbatim.

      - Only permitted characters (_ACK_ALLOWED_CHARS). ASCII letters and a
        little punctuation, which is what makes a homoglyph, a zero-width
        joiner or a stray digit fail here rather than downstream.
      - At least two words. SpeechEchoGuard passes single-token transcripts
        straight through by design, so that a lone "stop" always interrupts.
        A one-word ack is therefore an ack that CANNOT be filtered when it
        comes back off the speakers -- an unfiltered transcript arriving in
        the middle of the reply it was introducing, i.e. Atlas barging in on
        its own answer. Measured: should_drop("sure") and should_drop("okay")
        are both False, while "one second" and "still with you" are both
        caught.
      - Every word from _ACK_WORDS, the closed vocabulary of time and
        presence. See the note there for why this is an allowlist: at ack
        time the host does not yet know whether the turn ends in work or in a
        confirm-tier readback for work that has NOT happened, so any line
        asserting action is false on some turns no matter which line is
        chosen -- and the ways to assert an action are not enumerable, while
        the ways to assert only time and presence are.
    """
    values = raw.get("ack_lines")
    if values is None:
        return tuple(_VOICE_DEFAULTS["ack_lines"])
    if not isinstance(values, list) or len(values) > _ACK_LINE_COUNT:
        raise ValueError("invalid Atlas configuration: voice.ack_lines")
    lines = []
    for value in values:
        if (not isinstance(value, str) or not value.strip()
                or len(value) > _ACK_LINE_LIMIT):
            raise ValueError("invalid Atlas configuration: voice.ack_lines")
        # The string that will be spoken, and therefore the string that is
        # checked. Everything below reads only this.
        spoken = value.strip()
        stray = sorted(set(spoken) - _ACK_ALLOWED_CHARS)
        if stray:
            raise ValueError(
                "invalid Atlas configuration: voice.ack_lines may only use "
                "plain ASCII letters and simple punctuation; "
                f"{[hex(ord(char)) for char in stray]} is not allowed ({value!r})"
            )
        tokens = [token for token in _ACK_WORD_SPLIT.split(spoken.casefold()) if token]
        if len(tokens) < 2:
            raise ValueError(
                "invalid Atlas configuration: voice.ack_lines needs at least "
                "two words per line (a one-word ack cannot be echo-filtered)"
            )
        unknown = sorted(set(tokens) - _ACK_WORDS)
        if unknown:
            raise ValueError(
                "invalid Atlas configuration: voice.ack_lines may only say "
                "that Atlas is present and that time is passing -- an ack is "
                "spoken before the host knows whether the turn will act at "
                f"all. {unknown} is not in that vocabulary ({value!r})"
            )
        # One trailing space, like every other host line this file speaks, so
        # the ack and the model's first sentence do not run together.
        lines.append(f"{spoken} ")
    return tuple(lines)


def _echo_guard_config(raw: dict) -> dict:
    """Validate the echo-guard knobs inside the `voice:` section (F7), in the
    same style as the three above: a missing key takes the class default, a
    present one is type/range checked and raises rather than falling back to
    a value nobody chose. The keys are SpeechEchoGuard's own constructor
    keywords."""
    def _positive(name: str) -> float:
        value = raw.get(name, _ECHO_GUARD_DEFAULTS[name])
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(f"invalid Atlas configuration: voice.{name}")
        return float(value)

    max_words = raw.get("max_words", _ECHO_GUARD_DEFAULTS["max_words"])
    if isinstance(max_words, bool) or not isinstance(max_words, int) or max_words < 1:
        raise ValueError("invalid Atlas configuration: voice.max_words")
    ratio = raw.get("min_overlap_ratio", _ECHO_GUARD_DEFAULTS["min_overlap_ratio"])
    # A ratio over 1 can never be met and one at/below 0 is met by anything,
    # so both ends are refused rather than silently disabling or maximizing
    # the guard.
    if (isinstance(ratio, bool) or not isinstance(ratio, (int, float))
            or not 0 < ratio <= 1):
        raise ValueError("invalid Atlas configuration: voice.min_overlap_ratio")
    return {
        "tail_s": _positive("tail_s"),
        "buffer_window_s": _positive("buffer_window_s"),
        "max_words": max_words,
        "min_overlap_ratio": float(ratio),
    }


def _stt_keyterms(cfg: dict) -> list[str]:
    values = cfg.get("stt_keyterms") or ["Atlas"]
    if not isinstance(values, list) or len(values) > 64:
        return ["Atlas"]
    terms = [
        value.strip()
        for value in values
        if isinstance(value, str) and 0 < len(value.strip()) <= 64
    ]
    return terms[:64] or ["Atlas"]


def _addressing_from_config(cfg: dict, *, clock=None) -> router.Addressing:
    kwargs = {} if clock is None else {"clock": clock}
    return router.Addressing(
        float(cfg.get("addressed_window_s", 90)),
        router.vocabulary(cfg),
        **kwargs,
    )


def _apply_agent_state(engagement, publisher, agent_state: str) -> None:
    if engagement.state != engagement_mod.ENGAGED:
        return
    mapped = state.STATE_FROM_AGENT.get(agent_state)
    if mapped is not None:
        publisher.set_state(mapped)


def _restart_worker(reason: str, shutdown=None) -> None:
    global _worker_exit_code
    logger.critical("audio-follow requested worker restart: %s", reason)
    _worker_exit_code = RESTART_EXIT_CODE
    if shutdown is not None:
        shutdown(reason)


def _wake_model_callback(publisher, loop=None):
    def _changed(model_name: str) -> None:
        setter = getattr(publisher, "set_wake_model", None)
        if setter is None:
            logger.warning("wake-model state hook is unavailable")
            return
        if loop is None:
            setter(model_name)
            return
        loop.call_soon_threadsafe(setter, model_name)

    return _changed


def _should_cancel_active_jobs(shutdown_jobs_requested: bool) -> bool:
    return not shutdown_jobs_requested and _worker_exit_code != RESTART_EXIT_CODE


async def _flush_store_and_stop_state_server(store, server) -> None:
    try:
        store.close()
    except Exception:
        logger.exception("could not flush the job store during shutdown")
    await server.stop()


# --- CC5 rework: deterministic transcript echo-suppression ----------------
# Adversarial review (2026-08-31/09-01) found the three AgentSession knobs
# below do NOT stop the diagnosed bug:
#   - aec_warmup_duration is one-shot per SESSION, not per-utterance
#     (livekit-agents 1.6.6 voice/agent_session.py:1694-1748:
#     _on_aec_warmup_expired permanently zeroes _aec_warmup_remaining after
#     the FIRST "speaking" transition; _update_agent_state only arms the
#     timer `if self._aec_warmup_remaining > 0`, so only Atlas's very first
#     utterance of a session ever gets the grace period).
#   - min_interruption_duration is never consulted on the STT
#     interim-transcript path Atlas actually uses (turn_detection="stt").
#     AgentActivity.on_interim_transcript (agent_activity.py:2072-2090)
#     calls _interrupt_by_audio_activity() unconditionally on any non-empty
#     interim text; that function (agent_activity.py:1896-1969) only gates
#     on min_words (`interruption_options["min_words"]`, line ~1920) and the
#     one-shot AEC warmup -- `min_duration` is read only by the separate
#     VAD-activity path (on_vad_inference_done, ~line 2038), which STT-mode
#     barely uses. The diagnosis's own example, "two fifty five", is 3
#     words -- it clears min_interruption_words=2 and still interrupts.
# The three knobs are kept as harmless defense-in-depth (min_words=2 still
# blocks true one-word fragments on the STT path; the other two still help
# a VAD-driven or first-utterance interruption) but are NOT the fix. THE
# FIX is SpeechEchoGuard below: Atlas knows exactly what it is currently
# saying (AtlasAgent.tts_node observes every chunk before speaking it) and
# drops STT events that echo that text, before livekit's own interruption
# or turn-detection logic ever sees them.
_ECHO_NON_WORD_RE = re.compile(r"[^\w\s]", re.UNICODE)
_ECHO_FILTERED_EVENT_TYPES = frozenset({
    stt.SpeechEventType.INTERIM_TRANSCRIPT,
    stt.SpeechEventType.PREFLIGHT_TRANSCRIPT,
    stt.SpeechEventType.FINAL_TRANSCRIPT,
})


def _echo_normalize(text: str) -> list[str]:
    """casefold + strip punctuation -> word list, for fuzzy echo matching."""
    if not isinstance(text, str) or not text:
        return []
    return _ECHO_NON_WORD_RE.sub(" ", text.casefold()).split()


def _echo_contains_contiguous_ngram(words: list[str], buffer_words: list[str]) -> bool:
    """Did Atlas say exactly these words, back to back and in this order?"""
    n = len(words)
    if n == 0 or len(buffer_words) < n:
        return False
    return any(
        buffer_words[i:i + n] == words
        for i in range(len(buffer_words) - n + 1)
    )


def _echo_is_match(
    transcript_words: list[str], buffer_words: list[str], *, min_overlap_ratio: float,
) -> bool:
    """Tiered echo match rule (2026-09-01 rework, BLOCKER 1: a single common
    word like "stop" was being silently erased whenever it happened to
    overlap with what Atlas was saying -- worse than the min_words knob it
    fronts, since it ate exactly the most likely genuine barge-in words):

      - 1 token: NEVER an echo. livekit's own min_interruption_words=2
        already can't be interrupted by a lone word, so the event needs to
        survive to let plain confirmations ("yes", "no", "stop") through --
        dropping it here would be strictly worse than not filtering at all.
      - 2 to 4 tokens: an echo only if Atlas spoke those exact words back to
        back, contiguously and in that order -- "stop the" matches Atlas
        having literally said "...stop the...", but "stop it" does NOT match
        "...stop the music..." even though "stop" is common to both. A clean
        tail fragment ("two fifty five" out of "...at two fifty five") is
        contiguous by construction, so it still drops.
      - 5+ tokens: an in-order (not necessarily contiguous) subsequence of
        the buffer, or at least min_overlap_ratio of tokens present anywhere
        in it (for a garbled or reordered fragment). At this length an echo
        is unambiguous; scattered matches of that many words are not chance.

    2026-09-01 final gate, F2(a): the discontiguous subsequence rule started
    at 3 tokens, which ate genuine barge-ins whenever the buffer was long --
    "close that one" and "where is that one" both dissolve into the scattered
    words of a 191-word reply. It also read backwards: "stop it" was
    protected by the contiguity requirement while the longer, MORE specific
    "stop it now" was not. Contiguity now covers every short transcript.

    Known limitation (does not need a tier of its own -- it's a floor on
    accuracy, not a bug): this is exact/near-exact word matching, not
    phonetic. A homophone-garbled STT transcription of an echo ("too fifty
    fife" for "two fifty five") won't match here and falls back on whatever
    the min_interruption_words knob alone provides -- i.e. no better than
    before this fix for that specific failure mode.
    """
    n = len(transcript_words)
    if n == 0:
        return False
    if n == 1:
        return False
    if n <= 4:
        return _echo_contains_contiguous_ngram(transcript_words, buffer_words)
    remaining = iter(buffer_words)
    if all(word in remaining for word in transcript_words):
        return True
    available = Counter(buffer_words)
    matched = 0
    for word in transcript_words:
        if available[word] > 0:
            matched += 1
            available[word] -= 1
    return (matched / n) >= min_overlap_ratio


class SpeechEchoGuard:
    """Drop STT transcripts that are an echo of Atlas's own recent TTS
    output. Fed by AtlasAgent.tts_node (record_spoken, once per chunk, in
    the order Atlas will actually speak them) and consulted by
    AtlasAgent.stt_node (note_speaking on every STT event, should_drop on
    every transcript-bearing one).

    Filter window: while the session is in the "speaking" agent_state, plus
    a short `tail_s` afterward (echo can arrive slightly after playout ends
    too). Outside that window nothing is ever dropped, regardless of buffer
    content -- genuine barge-in during silence, or speech from well after
    Atlas finished, always passes through untouched. See _echo_is_match for
    the tiered content-match rule (BLOCKER 1 fix).

    Eviction is anchored to the SPEECH lifecycle, not to when record_spoken
    happened to be called (2026-09-01 rework, BLOCKER 2: record_spoken fires
    as LLM/TTS text drains, which has no backpressure from actual audio
    playout -- an 18s-long reply can be fully recorded by T=3s, so a fixed
    window measured from record-time expired long before the tail of a long
    reply's audio, and its own echo, ever arrived). So: while agent_state is
    "speaking", the buffer is never time-evicted -- it holds the ENTIRE
    current utterance regardless of length, bounded only by `max_words` (a
    memory cap, not a content-relevance one). The eviction clock starts only
    once speaking is confirmed to have stopped (`note_speaking`/`should_drop`
    observing speaking=False after having seen speaking=True); tail_s and
    buffer_window_s both then run from THAT moment, not from individual
    words' record time.

    Bounded the other way by the recency rule in `_begin_speech_if_idle`
    (2026-09-01 final gate, F2b): the buffer holds the current utterance,
    not the conversation. A new utterance starting after the previous
    speech's tail has passed clears it first, so no transcript is ever
    checked against a reply from minutes ago.

    Known tradeoff: a user who deliberately echoes Atlas immediately after
    it stops speaking ("yes, 2:55" right after Atlas says "...2:55") can
    still be dropped if it lands inside the short tail window and repeats
    Atlas's own wording closely enough (a lone "yes" is protected by the
    1-token rule above regardless of timing). This is accepted -- the tail
    window is short (~1s default) and anything under 5 words must repeat
    Atlas contiguously, so it only bites genuinely Atlas-shaped phrasing
    said immediately after Atlas stops, not ordinary conversation.
    """

    def __init__(
        self,
        *,
        tail_s: float = 1.0,
        buffer_window_s: float = 10.0,
        max_words: int = 500,
        min_overlap_ratio: float = 0.8,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._tail_s = tail_s
        self._buffer_window_s = buffer_window_s
        self._max_words = max_words
        self._min_overlap_ratio = min_overlap_ratio
        self._clock = clock
        self._buffer: deque[str] = deque()
        self._speaking_last_seen_at: float | None = None
        self._last_recorded_at: float | None = None
        self._utterance_open = False
        self._dropped_count = 0

    @property
    def dropped_count(self) -> int:
        return self._dropped_count

    def record_spoken(self, text: str) -> None:
        """Feed a chunk of text Atlas is about to speak. Capped only by
        word count -- see the class docstring (BLOCKER 2) for why this must
        NOT time-evict against the moment it was called."""
        words = _echo_normalize(text)
        if not words:
            return
        self._begin_speech_if_idle()
        self._last_recorded_at = self._clock()
        self._buffer.extend(words)
        while len(self._buffer) > self._max_words:
            self._buffer.popleft()

    def _begin_speech_if_idle(self) -> None:
        """Recency bound (2026-09-01 final gate, F2b): the first chunk of a
        NEW utterance clears the buffer.

        Without this, words from a reply given minutes ago stayed matchable
        for as long as max_words held them, so a later unrelated utterance
        was checked against a reply Daniel had long since heard and answered.
        The buffer's only job is the CURRENT speech (plus whatever is still
        draining right behind it), so cross-utterance matching may span
        rapid consecutive utterances -- their audio overlaps -- but never a
        reply from a previous exchange.

        "New" means: no speech is open, and the last confirmed speaking is
        more than tail_s ago (i.e. even the echo tail of that speech has
        passed). Chunks of the utterance already in flight leave the latch
        set, so a long reply is never cleared out from under itself.
        """
        if self._utterance_open:
            return
        self._utterance_open = True
        # Final-gate re-review (F2b race): the latch can be reset by an STT
        # event arriving in the TTS time-to-first-byte gap, when text has
        # drained but agent_state is not yet "speaking". Anchor idleness on
        # BOTH clocks -- last confirmed audio AND last recorded text -- so a
        # reply mid-drain is never treated as a stale previous utterance and
        # cleared out from under itself.
        candidates = [
            anchor for anchor in (self._speaking_last_seen_at, self._last_recorded_at)
            if anchor is not None
        ]
        if candidates and (self._clock() - max(candidates)) > self._tail_s:
            self._buffer.clear()

    def note_speaking(self, speaking: bool) -> None:
        """Sample the live agent_state. Called from AtlasAgent.stt_node on
        EVERY STT event (not just transcript-bearing ones) so the "last
        confirmed speaking" anchor stays fresh through a long reply even
        when generation has already finished draining text (BLOCKER 2);
        should_drop also calls this, so a caller that only ever calls
        should_drop (as every test here does) still gets correct behavior.
        """
        now = self._clock()
        if speaking:
            self._speaking_last_seen_at = now
            self._utterance_open = True
        else:
            if (
                self._speaking_last_seen_at is not None
                and (now - self._speaking_last_seen_at) > self._tail_s
            ):
                # Speech is over, tail included: the next recorded chunk
                # belongs to a new utterance (see _begin_speech_if_idle).
                self._utterance_open = False
            self._maybe_clear_stale_buffer(now)

    def _maybe_clear_stale_buffer(self, now: float) -> None:
        if self._speaking_last_seen_at is None:
            return
        if (now - self._speaking_last_seen_at) > self._buffer_window_s:
            self._buffer.clear()

    def _within_filter_window(self, now: float, *, speaking: bool) -> bool:
        if speaking:
            return True
        if self._speaking_last_seen_at is None:
            return False
        return (now - self._speaking_last_seen_at) <= self._tail_s

    def within_echo_window(self) -> bool:
        """Was Atlas speaking (or just finished) when this transcript landed?

        The same window `should_drop` filters in, read without a transcript.
        A one-token utterance is never treated as an echo by design -- livekit
        needs two words to interrupt, and a lone "stop" must always be able to
        stop the speaking -- but that also means a self-echoed "stop" reaches
        the reflex lane. Destroying a pending action is not something a
        possible echo may do, so the reflex lane asks this first.
        """
        return self._within_filter_window(self._clock(), speaking=False)

    def should_drop(self, transcript: str, *, speaking: bool) -> bool:
        now = self._clock()
        self.note_speaking(speaking)
        if not self._within_filter_window(now, speaking=speaking):
            return False
        transcript_words = _echo_normalize(transcript)
        if not transcript_words or not self._buffer:
            return False
        if not _echo_is_match(
            transcript_words, list(self._buffer), min_overlap_ratio=self._min_overlap_ratio,
        ):
            return False
        self._dropped_count += 1
        if self._dropped_count == 1 or self._dropped_count % 20 == 0:
            # Rule 10: bounded, and never the transcript text itself -- a
            # running count only.
            logger.debug(
                "echo guard dropped a self-barge-in-shaped transcript (total=%d)",
                self._dropped_count,
            )
        return True
# ---------------------------------------------------------------------------


class AtlasAgent(Agent):
    """Suppress autonomous LiveKit replies after the host handles a finalized turn."""

    def __init__(self, *args, echo_guard: SpeechEchoGuard | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.turn_handler = None
        # Config-built when the worker constructs it (F7); the class defaults
        # stand for tests and any caller that does not care.
        self.echo_guard = SpeechEchoGuard() if echo_guard is None else echo_guard

    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:
        handler = self.turn_handler
        if handler is not None:
            await handler(getattr(new_message, "text_content", None) or "")
        raise StopResponse()

    def tts_node(self, text, model_settings):
        async def _clean():
            async for chunk in text:
                cleaned = sanitize.sanitize_for_tts(chunk)
                self.echo_guard.record_spoken(cleaned)
                yield cleaned

        return Agent.default.tts_node(self, _clean(), model_settings)

    async def stt_node(self, audio, model_settings):
        """THE FIX (CC5 rework): drop STT events that echo what Atlas is
        currently saying, before they ever reach livekit's own
        interruption/turn-detection logic.

        Agent.stt_node is a documented, first-class override seam
        ("You can override this node with your own implementation", livekit
        -agents 1.6.6 voice/agent.py:345-369) -- not a monkeypatch.
        AgentActivity binds AudioRecognition directly to
        `self._agent.stt_node` (voice/agent_activity.py:1000), so a
        dropped event here never reaches on_interim_transcript /
        on_final_transcript (:2072-2131) and therefore never calls
        _interrupt_by_audio_activity and never becomes a recognized user
        turn -- one filter point kills both bad effects of the diagnosed
        echo.

        Only INTERIM/PREFLIGHT/FINAL transcript events carry text worth
        checking; START_OF_SPEECH/END_OF_SPEECH/RECOGNITION_USAGE (and
        anything with no alternatives) pass through untouched, as does any
        event outside the SpeechEchoGuard filter window (see its
        docstring) or that doesn't match the recently-spoken buffer --
        i.e. genuine user speech, including genuine barge-in, is never
        touched by this filter.

        note_speaking is called for EVERY event, not just transcript ones
        (BLOCKER 2 fix): livekit-agents also emits START_OF_SPEECH,
        END_OF_SPEECH, and periodic RECOGNITION_USAGE events, which arrive
        regardless of whether there is text to filter, keeping the guard's
        "last confirmed speaking" anchor from going stale during long
        stretches with no transcript-bearing event.
        """
        default_stream = Agent.default.stt_node(self, audio, model_settings)
        async for event in default_stream:
            speaking = self.session.agent_state == "speaking"
            self.echo_guard.note_speaking(speaking)
            if event.type in _ECHO_FILTERED_EVENT_TYPES and event.alternatives:
                if self.echo_guard.should_drop(
                    event.alternatives[0].text, speaking=speaking,
                ):
                    continue
            yield event


class TurnAck:
    """Say something short if the model has not started talking yet.

    Daniel's complaint is that acting feels slow, and the measurement behind
    the threshold is his own trace database: over the single-round turns
    recorded there (no tool call, so the whole wait is one model round), the
    round took 1.0-8.1s with a median of 2.9s, and only 8% of them finished
    inside 1.6s. Turns that DO call a tool are worse -- the first round alone
    ran 4.4-12.6s before the tool even started. So the silence this fills is
    the normal case, not an outlier.

    1.8s, and the two bounds that pick it:
      - Below it sits the fast tail. Atlas speaks its first complete SENTENCE
        rather than its first token (brain.split_spoken), so audio starts
        somewhat before the round finishes; a threshold much under 1.5s would
        start prefixing an ack onto replies that were about to arrive anyway,
        which turns one answer into two utterances and reads worse than the
        silence did.
      - Above it, the silence has already done its damage: this IS the point
        where Daniel starts wondering whether Atlas heard him, and an ack
        that lands after he has repeated himself is an ack that arrives into
        a barge-in.
    Config-tunable (`voice.ack_delay_s`), because the right value moves with
    the model and the prefix size.

    Exactly once per turn, structurally: the wait is raced against the FIRST
    chunk only (see _submit_voice_turn), so there is no second place this can
    fire from. Variants rotate rather than being drawn at random -- the same
    two lines in the same order are quieter to live with than a shuffle, and
    a test can pin what gets said.

    `wait` is a seam, not a knob: it is asyncio.wait_for in production, and a
    fake in tests, so ack timing is pinned without any test spending real
    seconds waiting for a real clock.

    What it may SAY is the constraint that matters, and it is a constraint on
    the config, not on this class: at the moment an ack goes out the host does
    not know what kind of turn it is in, so a line that claims action is false
    on every turn that ends in a confirm-tier readback. _ack_lines refuses
    such a line on load; the full argument is in config/atlas.yaml.
    """

    def __init__(
        self,
        lines: Sequence[str],
        delay_s: float,
        *,
        wait: Callable[..., Awaitable[Any]] = asyncio.wait_for,
    ) -> None:
        self._lines = tuple(lines)
        self.delay_s = float(delay_s)
        self.wait = wait
        self._next = 0

    @property
    def enabled(self) -> bool:
        return bool(self._lines) and self.delay_s > 0

    def line(self) -> str:
        if not self._lines:
            return ""
        line = self._lines[self._next % len(self._lines)]
        self._next += 1
        return line


class TurnOwnership:
    """Identify the response task that owns brain and TTS work."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    @property
    def in_flight(self) -> bool:
        return self._task is not None and not self._task.done()

    async def run(self, operation: Callable[[], Awaitable[Any]]) -> Any:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("voice turn has no asyncio task")
        previous = self._task
        if previous is not None and previous is not task and not previous.done():
            # cancel() only REQUESTS cancellation and is not awaited, so the
            # outgoing turn can still be unwinding while this one starts: the
            # two overlap on shared registry state (the per-turn file-handle
            # table, cleared by brain.respond -> registry.begin_turn). That
            # overlap is fail-closed by construction rather than by timing --
            # handle ids are monotonic for the life of the registry, so an id
            # the cancelled turn minted can never be re-minted here and a
            # stale reference resolves to nothing instead of to a new target.
            previous.cancel()
        self._task = task
        try:
            return await operation()
        finally:
            if self._task is task:
                self._task = None

    def cancel(self) -> bool:
        task = self._task
        if task is None or task.done():
            return False
        task.cancel()
        return True


async def _submit_voice_turn(
    text: str,
    *,
    brain: brain_mod.Brain,
    session,
    publisher: state.StatePublisher,
    engagement: engagement_mod.Engagement,
    context: str | None = None,
    source: str | None = None,
    ack: TurnAck | None = None,
) -> str:
    """Speak one brain turn and return WHAT THE MODEL SAID.

    The return value is the model's own speech, so `""` means the model
    produced nothing -- including on the guard above, which speaks nothing at
    all. The host may still have spoken a fallback; callers derive the trace
    outcome from this value precisely because it cannot drift from it.

    An `ack` (see TurnAck) does not change that in either direction. It is
    yielded into the SAME say() as the model's own text rather than spoken
    from a second call, which is what makes it land first instead of queueing
    behind a reply that has not started -- and what puts it through
    AtlasAgent.tts_node, so SpeechEchoGuard records it like every other thing
    Atlas says and its own echo cannot come back as a barge-in. It is
    deliberately kept out of `spoken`: an ack is the host clearing its
    throat, not an answer, so a turn where the model then says nothing is
    still recorded as the empty turn it was.
    """
    if not isinstance(text, str) or not text.strip():
        return ""
    publisher.add_line("user", text, source=source)
    publisher.set_state(state.THINKING)
    spoken: list[str] = []
    respond_started: float | None = None

    def _ack_line() -> str:
        # Quiet while a readback from an EARLIER turn is still standing --
        # that turn is waiting on one word from Daniel and has no use for
        # filler. Read what this does not do: a pending minted by THIS turn
        # cannot be seen from here, because the deadline pops in the middle
        # of the model's first round, before the tool call that would mint
        # it. There is no version of this check that covers the ordinary
        # confirm turn; the information does not exist yet. What makes the
        # ack safe in front of a readback is that no shipped ack line claims
        # an action at all -- see _ack_lines and the note in
        # config/atlas.yaml, which is where that is enforced.
        registry = getattr(brain, "registry", None)
        if registry is not None and getattr(registry, "pending", None) is not None:
            return ""
        return ack.line()

    async def _tee():
        nonlocal respond_started
        response = brain.respond(text, context=context) if context is not None else brain.respond(text)
        if ack is None or not ack.enabled:
            async for chunk in response:
                if respond_started is None:
                    respond_started = time.perf_counter()
                spoken.append(chunk)
                yield chunk
            return
        # Only the FIRST chunk is raced, which is what makes "at most once
        # per turn" structural rather than a flag someone has to remember to
        # clear. shield keeps the model's turn running through the timeout --
        # wait_for would cancel the very generation being waited on.
        iterator = response.__aiter__()
        first = asyncio.ensure_future(iterator.__anext__())
        try:
            try:
                chunk = await ack.wait(asyncio.shield(first), ack.delay_s)
            except TimeoutError:
                line = _ack_line()
                if line:
                    # Timed from here: this is when Atlas starts making
                    # sound, so it is when the RESPOND leg starts.
                    respond_started = time.perf_counter()
                    # Tagged, so "say that again" cannot replay it: an ack is
                    # never an answer, and after a barge-in it would otherwise
                    # be the newest atlas line in the ring.
                    publisher.add_line("atlas", line, source=ACK_LINE_SOURCE)
                    yield line
                chunk = await first
        except StopAsyncIteration:
            return
        finally:
            if not first.done():
                first.cancel()
        while True:
            if respond_started is None:
                respond_started = time.perf_counter()
            spoken.append(chunk)
            yield chunk
            try:
                chunk = await iterator.__anext__()
            except StopAsyncIteration:
                return

    from worker import traces as traces_mod

    responded = False
    try:
        await session.say(_tee(), add_to_chat_ctx=False)
        if spoken:
            responded = True
        elif traces_mod.speech_was_interrupted():
            # Daniel talked over this turn. The silence is his, not a fault, so
            # the host says nothing at all -- an apology here is a false one.
            logger.info("voice turn produced no speech after a barge-in")
        else:
            # Last funnel before the speaker: whatever went wrong upstream, an
            # addressed turn never ends without Atlas saying something. Spoken
            # INSIDE the instrumented window so the RESPOND leg times the audio
            # Daniel actually heard, and left ok=False so the leg agrees with
            # the turn's "empty" outcome instead of reporting a model reply.
            logger.warning("voice turn produced no speech; speaking the host fallback")
            respond_started = time.perf_counter()
            await session.say(brain_mod.EMPTY_TURN_REPLY, add_to_chat_ctx=False)
            publisher.add_line("atlas", brain_mod.EMPTY_TURN_REPLY)
    finally:
        traces_mod.record_current_respond(
            ms=(round((time.perf_counter() - respond_started) * 1000)
                if respond_started is not None else 0),
            ok=responded,
        )
        if engagement.state == engagement_mod.ENGAGED:
            publisher.set_state(state.LISTENING)
    response = "".join(spoken)
    if response:
        publisher.add_line("atlas", response)
    return response


def _last_atlas_line(publisher: state.StatePublisher) -> str | None:
    """The last thing Atlas ANSWERED, for "say that again".

    Ack lines are skipped. An ack is filler spoken while the reply is still
    being generated, so it is never the answer -- and after an ack followed by
    a barge-in it is the last atlas line in the ring, which made "say that
    again" replay "One second." instead of the reply Daniel wanted repeated,
    or instead of the honest REPEAT_FALLBACK when there is nothing to repeat.
    """
    for line in reversed(publisher.snapshot()["transcript"]):
        if line.get("source") == ACK_LINE_SOURCE:
            continue
        if line.get("role") == "atlas" and isinstance(line.get("text"), str):
            return line["text"]
    return None


def _address_window_open(addressing: router.Addressing) -> bool:
    """Probe only the activity window; an empty utterance cannot hit vocabulary."""
    return addressing.is_addressed("")


def _match_reflex_open(text: str, registry) -> router.OpenReflex | None:
    """Can the host answer this utterance itself, right now?

    Four things have to be true, and all four are checked here rather than
    inside the grammar, because they are about the HOST's state rather than
    about the words:

      - there is a registry (the text lane's fake brains have none);
      - it holds no pending action. A confirm-tier readback is waiting for
        exactly one word from Daniel, and quietly doing something else in
        the middle of that is not a speed improvement -- that turn goes to
        the model, which can see the readback in its history;
      - the tool this match names is actually registered. `open_folder` only
        exists when file roots resolved, and `focus_last_opened` is not in
        every stand-in registry;
      - and that tool is INSTANT-tier. This lane speaks a single sentence
        about what already happened and never runs a second round, so a
        confirm-tier tool here would mint a pending action whose readback
        nobody says: Atlas would report a failure it did not have while a
        live, single-use pending waited to be consumed by the next bare
        "yes" about anything (rules 4 and 5). Today all three names are
        instant and register() refuses duplicates, so this cannot fire --
        which is the point of checking it at the gate rather than trusting a
        comment on the table above.

    Every registry access is defensive. This runs on EVERY addressed turn, so
    a stand-in registry missing one property must degrade to the model lane,
    not crash the turn -- the same reason `pending` is read with getattr.
    """
    if registry is None or getattr(registry, "pending", None) is not None:
        return None
    match = router.reflex_open(
        text,
        aliases=getattr(registry, "open_aliases", ()) or (),
        roots=getattr(registry, "root_names", ()) or (),
    )
    if match is None:
        return None
    name, _arguments = _REFLEX_OPEN_TOOLS[match.kind](match.name)
    policy = getattr(registry, "policy", None)
    if not callable(policy) or policy(name) != "instant":
        return None
    return match


async def _run_reflex_open(match: router.OpenReflex, registry) -> tuple[str, str]:
    """Run one host-resolved open; return the tool it ran and what to say.

    The NAME comes back with the sentence because the turn is filed into
    history and into the transcript store by the caller, and the store's
    contract is the names of the tools the turn touched (worker/transcript
    module docstring). A reflex turn really did run one -- reporting an empty
    tools column for it would make an open indistinguishable from small talk
    (DD-wave review, LOW-5).
    """
    # A reflex turn is still a turn. brain.respond is what normally resets the
    # per-turn handle table, and skipping the brain must not quietly extend a
    # previous turn's handles across this one -- "handles live for exactly one
    # turn" is what makes a stale id fail closed instead of resolving to
    # something the model was never shown. Nothing below mints or spends one;
    # this is only about not leaving the last turn's table standing.
    registry.begin_turn()
    name, arguments = _REFLEX_OPEN_TOOLS[match.kind](match.name)
    result = await registry.call(name, arguments)
    if match.kind == "last":
        if result.status == "ok":
            return name, REFLEX_BROUGHT_BACK
        if result.content == tools_mod.NOTHING_RECENTLY_OPENED:
            return name, REFLEX_NOTHING_TO_BRING_BACK
        return name, REFLEX_BRING_BACK_FAILED
    if result.status != "ok":
        # The host refused or failed, so nothing is on screen and saying
        # "Opening X" would be a lie. The turn does NOT fall through to the
        # model afterwards: a second attempt at an action that just failed is
        # a worse answer than an honest short one, and the trace already
        # carries the failed TOOL_CALL row for anyone asking why.
        logger.info("reflex open did not succeed (kind=%s)", match.kind)
        return name, REFLEX_OPEN_FAILED.format(name=match.name)
    if result.content == tools_mod.FOCUSED_EXISTING_WINDOW:
        return name, REFLEX_ALREADY_OPEN.format(name=match.name)
    return name, REFLEX_OPENING.format(name=match.name)


async def _handle_reflex(
    text: str,
    *,
    intents: dict,
    session,
    publisher: state.StatePublisher,
    dismiss,
    cancel_turn=None,
    on_spoken=None,
    registry=None,
    speaking_probe=None,
    source: str | None = None,
    open_match: router.OpenReflex | None = None,
    remember=None,
) -> bool:
    if not isinstance(text, str) or not text.strip():
        return False
    if open_match is not None:
        # The deterministic open lane. It joins this funnel rather than
        # sitting beside it so that everything the funnel guarantees still
        # holds: the utterance is published as a user line, exactly one
        # sentence is spoken, that sentence is mirrored into the transcript,
        # and the caller's on_spoken hook decides what the clocks do.
        #
        # Interrupt first, for the same reason the cancel branch below does:
        # this lane speaks immediately, and an outgoing model reply is only
        # stopped by task cancellation, which does not silence audio already
        # handed to TTS. Without it the open confirmation lands on top of the
        # previous answer.
        session.interrupt()
        publisher.add_line("user", text, source=source)
        tool_name, line = await _run_reflex_open(open_match, registry)
        await session.say(line, add_to_chat_ctx=False)
        publisher.add_line("atlas", line)
        # The model never saw this turn (see Brain.remember_host_exchange):
        # without the exchange in its history, the next "close that" resolves
        # to the turn BEFORE this one. The tool name travels with it so the
        # filed turn says which tool it ran, the same as every model turn.
        if remember is not None:
            remember(text, line, (tool_name,))
        if on_spoken is not None:
            on_spoken()
        return True
    lane, intent = router.route(text, intents)
    if lane != "reflex":
        return False
    repeated = _last_atlas_line(publisher) if intent == "repeat" else None
    publisher.add_line("user", text, source=source)
    if intent == "dismiss":
        dismiss()
    elif intent == "cancel":
        # Stopping the speech is unconditional: whoever said it, and whether or
        # not it was Atlas's own voice coming back, "stop" stops the talking.
        session.interrupt()
        if cancel_turn is not None:
            cancel_turn()
        # Dropping the PENDING action is not unconditional. A one-token
        # "cancel" or "stop" is never filtered as an echo (the guard passes
        # 1-token transcripts by design), so a word Atlas itself just said
        # could otherwise destroy a single-use mutating action nobody
        # cancelled. Mid-speech it only stops the readback; the pending
        # survives for an explicit follow-up in the quiet after it.
        echoing = bool(speaking_probe()) if callable(speaking_probe) else False
        if echoing:
            logger.info("reflex cancel arrived mid-speech; pending action kept")
        # The reflex lane never reaches the brain, so "cancel" used to stop the
        # speech and the in-flight turn while leaving the pending action alive:
        # Daniel cancelled, heard nothing, and the next plain "yes" -- about
        # anything -- still had a mutating action waiting to consume it.
        if not echoing and getattr(registry, "pending", None) is not None:
            registry.cancel_pending()
            await session.say(PENDING_CANCELLED_REPLY, add_to_chat_ctx=False)
            publisher.add_line("atlas", PENDING_CANCELLED_REPLY)
            if on_spoken is not None:
                on_spoken()
    elif intent == "repeat" and repeated:
        await session.say(repeated, add_to_chat_ctx=False)
        if on_spoken is not None:
            on_spoken()
    else:
        # There was no terminal branch here: "say that again" with nothing to
        # repeat returned True having said nothing at all. In practice this IS
        # the repeat-only safety net -- every caller gates on dismiss, cancel,
        # or repeat, and an intent this funnel does not route (unlock_kb) never
        # arrives here, it goes to the brain through _respond. The branch is
        # written generally so a future caller cannot fall through silently.
        # `dismiss` and `cancel` stay silent on purpose and are handled above.
        logger.warning("reflex intent produced no reply (intent=%s)", str(intent)[:32])
        line = REPEAT_FALLBACK if intent == "repeat" else REFLEX_FALLBACK
        await session.say(line, add_to_chat_ctx=False)
        publisher.add_line("atlas", line)
        if on_spoken is not None:
            on_spoken()
    return True


async def _handle_audio_turn_inner(
    text: str,
    *,
    intents: dict,
    brain: brain_mod.Brain,
    session,
    publisher: state.StatePublisher,
    engagement: engagement_mod.Engagement,
    addressing: router.Addressing,
    sleep,
    turn_ownership: TurnOwnership | None = None,
    source: str = "speech",
    speaking_probe=None,
    ack: TurnAck | None = None,
    _trace=None,
    _trace_meta: dict | None = None,
) -> str:
    if not isinstance(text, str) or not text.strip():
        return ""

    if source not in {"speech", "typed"}:
        raise ValueError("turn source must be speech or typed")
    line_source = "typed" if source == "typed" else None
    ownership = turn_ownership or TurnOwnership()
    route_started = time.perf_counter()
    route_recorded = False

    def _record_route() -> None:
        nonlocal route_recorded
        if _trace is not None and not route_recorded:
            _trace[0].route(
                _trace[1],
                ms=round((time.perf_counter() - route_started) * 1000),
                ok=True,
            )
            route_recorded = True

    def _mark(*, addressed: bool, wake_kind: str, outcome: str) -> None:
        if _trace_meta is not None:
            _trace_meta.update(
                addressed=addressed, wake_kind=wake_kind, outcome=outcome,
            )
        _record_route()

    lane, intent = router.route(text, intents)

    def _dismiss() -> None:
        engagement.dismiss()
        session.interrupt()
        ownership.cancel()
        sleep()

    if lane == "reflex" and intent == "dismiss":
        _mark(addressed=False, wake_kind="reflex", outcome="dismissed")
        await _handle_reflex(
            text,
            intents=intents,
            session=session,
            publisher=publisher,
            dismiss=_dismiss,
            source=line_source,
        )
        return ""
    if lane == "reflex" and intent == "cancel":
        cancel_allowed = (
            source == "typed" or ownership.in_flight or _address_window_open(addressing)
        )
        if cancel_allowed:
            _mark(addressed=False, wake_kind="reflex", outcome="cancelled")

            def _cancel_spoken() -> None:
                # "Cancelled." is a real reply, so the follow-up ("do the other
                # one instead") must not need the wake word again -- the same
                # rule the repeat lane and the empty-turn fallback follow.
                if engagement.state != engagement_mod.ENGAGED:
                    return
                engagement.interacted()
                addressing.mark_activity()

            await _handle_reflex(
                text,
                intents=intents,
                session=session,
                publisher=publisher,
                dismiss=_dismiss,
                cancel_turn=ownership.cancel,
                on_spoken=_cancel_spoken,
                registry=getattr(brain, "registry", None),
                speaking_probe=speaking_probe,
                source=line_source,
            )
            return ""
    previous = engagement.state
    if engagement.tick() != engagement_mod.ENGAGED:
        _mark(addressed=False, wake_kind="ambient", outcome="asleep")
        if previous == engagement_mod.ENGAGED:
            sleep(announce=False)
        return ""
    normalized = router.normalize(text)
    if lane == "reflex" and intent == "cancel":
        _mark(addressed=False, wake_kind="ambient", outcome="ignored")
        publisher.add_line("ambient", text)
        return ""
    reply_window = _address_window_open(addressing)
    if source != "typed" and not addressing.is_addressed(normalized):
        _mark(addressed=False, wake_kind="ambient", outcome="ignored")
        publisher.add_line("ambient", text)
        return ""
    _mark(
        addressed=True,
        wake_kind="reply" if reply_window else "wake",
        outcome="error",
    )
    engagement.interacted()
    if lane == "reflex" and intent == "repeat":
        def _repeated() -> None:
            # The reflex funnel always speaks now (its terminal else), so the
            # outcome is stamped here instead of riding the error->responded
            # promotion, which no longer fires for a turn that returns "".
            _mark(
                addressed=True,
                wake_kind="reply" if reply_window else "wake",
                outcome="responded",
            )
            if engagement.state != engagement_mod.ENGAGED:
                return
            engagement.interacted()
            addressing.mark_activity()

        await ownership.run(lambda: _handle_reflex(
            text,
            intents=intents,
            session=session,
            publisher=publisher,
            dismiss=_dismiss,
            cancel_turn=ownership.cancel,
            on_spoken=_repeated,
            source=line_source,
        ))
        return ""
    # Deterministic opens, checked HERE and not earlier: an "open downloads"
    # has no address vocabulary in it, so it has to clear exactly the same
    # engagement and addressing gates a model turn clears. Everything above
    # this line is unchanged; the only turns that behave differently are the
    # ones that would have reached the model and asked it to call the very
    # same instant, host-resolved tool.
    #
    # Trace shape, deliberately: a reflex turn writes ROUTE and TOOL_CALL and
    # no RESPOND step, so its speech never enters the health rollup's RESPOND
    # timings. That is right and it is the rule the dismiss, cancel and repeat
    # lanes already follow -- RESPOND measures how long Daniel waited for a
    # model answer, and a lane whose whole claim is that no model round
    # happened would drag that number toward zero and hide the regression the
    # metric exists to catch. The TOOL_CALL row carries the host cost, which
    # is the only cost there was.
    open_match = _match_reflex_open(text, getattr(brain, "registry", None))
    if open_match is not None:
        def _opened() -> None:
            _mark(
                addressed=True,
                wake_kind="reply" if reply_window else "wake",
                outcome="responded",
            )
            if engagement.state != engagement_mod.ENGAGED:
                return
            engagement.interacted()
            addressing.mark_activity()

        await ownership.run(lambda: _handle_reflex(
            text,
            intents=intents,
            session=session,
            publisher=publisher,
            dismiss=_dismiss,
            cancel_turn=ownership.cancel,
            on_spoken=_opened,
            registry=brain.registry,
            source=line_source,
            open_match=open_match,
            remember=getattr(brain, "remember_host_exchange", None),
        ))
        return ""
    async def _respond() -> str:
        context = publisher.ambient_context(text)
        response = await _submit_voice_turn(
            text,
            brain=brain,
            session=session,
            publisher=publisher,
            engagement=engagement,
            context=context,
            source=line_source,
            ack=ack,
        )
        # Derived from the answer itself, not signalled out of the funnel: a
        # silent turn IS the silent outcome, so the two cannot drift apart, and
        # every path that returns "" -- including the funnel's own input guard
        # -- is recorded honestly without knowing it is being watched. Which
        # KIND of silence comes from the same turn flag the funnel reads.
        from worker import traces as traces_mod
        if response:
            outcome = "responded"
        elif traces_mod.speech_was_interrupted():
            # A barge-in, not a failed turn. Recorded apart from "empty" so the
            # trace agrees with the funnel above, which stays silent for it.
            outcome = "interrupted"
        else:
            outcome = "empty"
        _mark(
            addressed=True,
            wake_kind="reply" if reply_window else "wake",
            outcome=outcome,
        )
        # The window used to be refreshed only `if response`, so a silent turn
        # closed the addressing window and the next follow-up needed the wake
        # word again -- silence compounding into more silence. The host spoke
        # either way, so the window is refreshed either way.
        if engagement.state == engagement_mod.ENGAGED:
            engagement.interacted()
            addressing.mark_activity()
        return response

    return await ownership.run(_respond)


async def _handle_audio_turn(
    text: str,
    *,
    intents: dict,
    brain: brain_mod.Brain,
    session,
    publisher: state.StatePublisher,
    engagement: engagement_mod.Engagement,
    addressing: router.Addressing,
    sleep,
    turn_ownership: TurnOwnership | None = None,
    source: str = "speech",
    speaking_probe=None,
    ack: TurnAck | None = None,
    trace_recorder=None,
) -> str:
    if trace_recorder is None or not isinstance(text, str) or not text.strip():
        return await _handle_audio_turn_inner(
            text,
            intents=intents,
            brain=brain,
            session=session,
            publisher=publisher,
            engagement=engagement,
            addressing=addressing,
            sleep=sleep,
            turn_ownership=turn_ownership,
            source=source,
            speaking_probe=speaking_probe,
            ack=ack,
        )

    from worker import traces as traces_mod
    started = time.perf_counter()
    metadata = {"addressed": False, "wake_kind": "ambient", "outcome": "error"}
    turn = trace_recorder.begin_turn(wake_kind="ambient")
    token = traces_mod.activate(trace_recorder, turn)
    try:
        response = await _handle_audio_turn_inner(
            text,
            intents=intents,
            brain=brain,
            session=session,
            publisher=publisher,
            engagement=engagement,
            addressing=addressing,
            sleep=sleep,
            turn_ownership=turn_ownership,
            source=source,
            speaking_probe=speaking_probe,
            ack=ack,
            _trace=(trace_recorder, turn),
            _trace_meta=metadata,
        )
        # Only promote on a turn that actually produced speech. This used to
        # stamp "responded" on any addressed turn that did not raise, so a
        # silent turn was recorded as a reply that never happened.
        if metadata["addressed"] and metadata["outcome"] == "error" and response:
            metadata["outcome"] = "responded"
        return response
    except asyncio.CancelledError:
        metadata["outcome"] = "cancelled"
        raise
    except Exception:
        if metadata["addressed"]:
            metadata["outcome"] = "speech_failed"
        raise
    finally:
        traces_mod.reset(token)
        trace_recorder.end_turn(
            turn,
            **metadata,
            total_ms=round((time.perf_counter() - started) * 1000),
        )


async def _handle_typed_turn(
    text: str,
    *,
    ready: bool,
    intents: dict,
    brain: brain_mod.Brain,
    session,
    publisher: state.StatePublisher,
    engagement: engagement_mod.Engagement,
    addressing: router.Addressing,
    engage,
    sleep,
    turn_ownership: TurnOwnership | None = None,
    ack: TurnAck | None = None,
    trace_recorder=None,
) -> str:
    """Route deliberate UI text through the same guarded turn path as speech."""
    if not ready:
        raise RuntimeError("voice session is not ready")
    if engagement.tick() != engagement_mod.ENGAGED:
        engage()
    return await _handle_audio_turn(
        text,
        intents=intents,
        brain=brain,
        session=session,
        publisher=publisher,
        engagement=engagement,
        addressing=addressing,
        sleep=sleep,
        turn_ownership=turn_ownership,
        source="typed",
        ack=ack,
        trace_recorder=trace_recorder,
    )


async def _sleep_watch(
    engagement: engagement_mod.Engagement,
    sleep,
    *,
    interval_s: float = 5.0,
    turn_ownership: TurnOwnership | None = None,
) -> None:
    while True:
        await asyncio.sleep(interval_s)
        previous = engagement.state
        in_flight = turn_ownership is not None and turn_ownership.in_flight
        current = engagement.tick(turn_in_flight=in_flight)
        if previous == engagement_mod.ENGAGED and current == engagement_mod.ASLEEP:
            sleep(announce=False)


def _sleep_session(
    session,
    publisher: state.StatePublisher,
    *,
    announce: bool = True,
) -> bool:
    if not session.input.audio_enabled:
        return False
    session.input.set_audio_enabled(False)
    publisher.set_state(state.ASLEEP)
    if announce:
        session.say(SLEEP_LINE, add_to_chat_ctx=False)
        publisher.add_line("atlas", SLEEP_LINE)
    else:
        publisher.add_line("system", "auto-sleep")
    return True


def _record_tool(name: str, result: tools_mod.ToolResult) -> None:
    # Tool calls used to be mirrored into the transcript ring as a "tool"
    # role line; that cluttered the chat with rows Daniel didn't want to see.
    # worker/tools.py already records every tool call into the traces DB
    # independently (traces_mod.record_current_tool_call), so nothing here
    # needs to publish a line. Kept as the on_tool callback target (wired at
    # the bottom of this module) in case a future non-transcript cue (e.g. a
    # UI ping) needs the hook -- it takes only what the on_tool contract
    # gives it; the StatePublisher parameter went with the transcript line.
    return None


def _terminal_line(job) -> str:
    if job.state is JobState.SUCCEEDED:
        summary = " ".join((job.summary or "").split())
        return (f"Done -- {summary}" if summary else "Done.")[:320]
    if job.state is JobState.CANCELLED:
        return "Cancelled."
    return "That task hit a problem; it's in History."


async def _await_speech(value) -> None:
    await value


def _announce_terminal(job, publisher, session, engagement, addressing=None) -> None:
    line = _terminal_line(job)
    publisher.add_line("atlas", line)
    if engagement.state != engagement_mod.ENGAGED:
        return
    engagement.interacted()
    if addressing is not None:
        addressing.mark_activity()
    speech = session.say(line, add_to_chat_ctx=False)
    if not inspect.isawaitable(speech):
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(_await_speech(speech))
    else:
        asyncio.ensure_future(speech)


def _jobs_projection(work: work_mod.WorkManager) -> list[dict]:
    return [job.to_public() for job in [*work.active(), *work.recent(50)]]


def _emit_ui_url(authorizer: stateserver.PairingAuthorizer, port: int) -> str:
    url = stateserver.pairing_url(authorizer, port)
    if url is None:
        raise RuntimeError("Atlas UI pairing is unavailable")
    print(f"ATLAS_UI {url}", flush=True)
    return url


def _engage_wake(
    session: Any | None,
    *,
    session_started: bool,
    publisher: state.StatePublisher,
    engagement: engagement_mod.Engagement,
    addressing: router.Addressing,
) -> None:
    """Publish a wake immediately; use session audio only once it is ready."""
    already = engagement.state == engagement_mod.ENGAGED
    engagement.wake()
    addressing.mark_activity()
    if not already:
        publisher.start_session()
        publisher.set_state(state.LISTENING)
        publisher.add_line("atlas", WAKE_LINE)
    if not session_started or session is None:
        return
    session.input.set_audio_enabled(True)
    if not already:
        session.say(WAKE_LINE, add_to_chat_ctx=False)


async def _connect_mcp_and_settle(mcp, registry, brain: brain_mod.Brain) -> None:
    """Refresh the prompt snapshot as each MCP server arrives, then settle.

    connect() returns only once EVERY server has either connected or been
    declared terminally errored, and one sick server now retries to
    exhaustion (3 attempts x 60s + backoff = up to ~190s, see
    config/mcp.yaml defaults). Holding the snapshot until then made every
    healthy server's tools invisible to the model for that whole window --
    Atlas would say it cannot read files while the files server had been
    connected for three minutes (BB-wave review, finding 4). Each arrival
    rebuilds instead: refresh_tools() is a no-op unless the capability text
    actually changed, so this costs nothing when nothing changed.

    The settle boundary still matters and is unchanged: begin/mark bracket
    the MCP state-transition churn (refresh_capabilities stays coalesced),
    and mark_tools_settled is what first ARMS the cache-floor check, so no
    floor check runs against a half-connected surface.
    """
    begin_settle = getattr(brain, "begin_capability_settle", None)
    if begin_settle is not None:
        begin_settle()

    def _on_server(_name: str, _registry) -> None:
        brain.refresh_tools()

    await mcp.connect(registry, on_server=_on_server)
    brain.refresh_tools()
    brain.mark_tools_settled()


async def entrypoint(ctx: JobContext) -> None:
    jobobject.assign_current_process()
    wakeword.shutting_down.clear()
    envload.load_private_environment()
    cfg = _cfg()
    voice_cfg = _voice_config(cfg)
    trace_cfg = cfg.get("traces") if isinstance(cfg.get("traces"), dict) else {}
    pricing_cfg = cfg.get("pricing") if isinstance(cfg.get("pricing"), dict) else {}
    trace_recorder = None
    authorizer = stateserver.PairingAuthorizer()
    server: stateserver.StateServer | None = None

    def _paired_url() -> str | None:
        if server is None:
            return None
        return stateserver.pairing_url(authorizer, server.port)

    services = runtime.build(cfg, paired_url=_paired_url)

    def _traces():
        nonlocal trace_recorder
        if trace_recorder is None:
            from worker import traces as traces_mod
            trace_recorder = traces_mod.TraceRecorder(
                trace_cfg.get("path"),
                enabled=trace_cfg.get("enabled") is not False,
                pricing={key: value for key, value in pricing_cfg.items()
                         if isinstance(value, dict)},
                cache_ttl=pricing_cfg.get("cache_ttl", "5m"),
                tool_names=services.registry.names(),
                model_names=(cfg.get("fast_model"),),
                retention_days=trace_cfg.get("retention_days", 30),
            )
        return trace_recorder

    user = cfg.get("user")
    user_name = user.get("name") if isinstance(user, dict) else None
    publisher = state.StatePublisher(voice=cfg.get("active_voice"), user_name=user_name)
    services.registry.set_execution_observer(publisher.set_tool)
    intents = _load_intents()
    # One TurnAck for the worker, not one per turn: rotating the variants is
    # the point (see TurnAck), and a fresh object every turn would say the
    # same word every time.
    turn_ack = TurnAck(voice_cfg["ack_lines"], voice_cfg["ack_delay_s"])
    loop = asyncio.get_running_loop()
    session: Any | None = None
    session_started = False
    mcp_task: asyncio.Task | None = None
    work_task: asyncio.Task | None = None
    desktop_status_task: asyncio.Task | None = None
    engagement: engagement_mod.Engagement | None = None
    turn_ownership: TurnOwnership | None = None
    wake_switch: Any | None = None
    audio_watcher: Any | None = None
    restart_coalescer: devicewatch.AudioRestartCoalescer | None = None
    audio_failure = None
    stop_work = asyncio.Event()
    sleep_task: asyncio.Task | None = None
    shutdown_jobs_requested = False
    shutdown_started = False
    shutdown_done = asyncio.Event()

    def _sleep(announce: bool = True) -> bool:
        if session is None:
            return False
        return _sleep_session(session, publisher, announce=announce)

    async def _submit_text_turn(text: str) -> None:
        if (
            session is None
            or engagement is None
            or turn_ownership is None
            or not session_started
            or not publisher.ready
        ):
            raise RuntimeError("voice session is not ready")
        await _handle_typed_turn(
            text,
            ready=True,
            intents=intents,
            brain=services.brain,
            session=session,
            publisher=publisher,
            engagement=engagement,
            addressing=addressing,
            engage=_engage,
            sleep=_sleep,
            turn_ownership=turn_ownership,
            ack=turn_ack,
            trace_recorder=_traces(),
        )

    async def _request_shutdown() -> None:
        nonlocal shutdown_jobs_requested
        shutdown_jobs_requested = True
        if engagement is not None:
            engagement.dismiss()
        if session is not None:
            session.interrupt()
        if turn_ownership is not None:
            turn_ownership.cancel()
        await services.work.cancel_active(timeout_s=15.0)
        loop.call_later(0.05, ctx.shutdown, "desktop requested shutdown")

    async def _shutdown() -> None:
        nonlocal shutdown_started
        if shutdown_started:
            await shutdown_done.wait()
            return
        shutdown_started = True
        try:
            wakeword.shutting_down.set()
            if audio_watcher is not None:
                audio_watcher.stop()
            if session is not None:
                session.interrupt()
            if turn_ownership is not None:
                turn_ownership.cancel()
            if sleep_task is not None:
                sleep_task.cancel()
                await asyncio.gather(sleep_task, return_exceptions=True)
            if desktop_status_task is not None:
                desktop_status_task.cancel()
                await asyncio.gather(desktop_status_task, return_exceptions=True)
            if _should_cancel_active_jobs(shutdown_jobs_requested):
                await services.work.cancel_active(timeout_s=15.0)
            stop_work.set()
            if mcp_task is not None:
                mcp_task.cancel()
            if work_task is not None:
                await asyncio.gather(work_task, return_exceptions=True)
            if mcp_task is not None:
                await asyncio.gather(mcp_task, return_exceptions=True)
            await services.mcp.close()
            if server is not None:
                await _flush_store_and_stop_state_server(services.store, server)
        finally:
            # Closing the persistent sinks must never be able to leave
            # shutdown_done unset. Anything waiting on it -- a second
            # _request_shutdown, the entrypoint's own exit path -- waits
            # forever if this block raises, so an app that cannot close a
            # database would hang instead of quitting. The narrow
            # TimeoutError catch below used to be the only guard, which meant
            # any OTHER failure here had exactly that effect.
            try:
                if trace_recorder is not None:
                    try:
                        await asyncio.wait_for(asyncio.to_thread(trace_recorder.close), 2.1)
                    except TimeoutError:
                        logger.warning("turn tracing close exceeded shutdown deadline")
                transcript_store = getattr(services, "transcript", None)
                if transcript_store is not None:
                    # No deadline and no thread: every exchange was committed
                    # synchronously as it happened, so this closes a
                    # connection rather than flushing a backlog. Nothing is
                    # lost if the process dies before it.
                    transcript_store.close()
            except Exception as exc:
                logger.warning("shutdown close failed (type=%s)", type(exc).__name__)
            finally:
                shutdown_done.set()

    desktop_status = desktopapps.StatusSnapshot()

    def _health() -> dict[str, Any]:
        snapshot = desktop_status.get()
        summary = (trace_recorder.health if trace_recorder is not None else {
            "enabled": trace_cfg.get("enabled") is not False,
            "turns": 0, "avg_ms": 0.0, "cache_hit_ratio": 0.0, "cost_usd": 0.0,
        })
        return {
            "claude": services.work.launcher.available,
            "mcp": services.mcp.status(),
            "apps": snapshot["apps"],
            "as_of": snapshot["as_of"],
            "cache_floor_ok": getattr(services.brain, "cache_floor_ok", None),
            "traces": {
                "enabled": summary["enabled"],
                "turns_today": summary["turns"],
                "avg_ms_today": summary["avg_ms"],
                "cache_hit_ratio_today": summary["cache_hit_ratio"],
                "cost_usd_today": summary["cost_usd"],
            },
        }

    server = await stateserver.start(
        publisher,
        int(cfg["state_port"]),
        authorizer=authorizer,
        job_provider=lambda: _jobs_projection(services.work),
        job_event_provider=services.store.events,
        result_provider=services.store.result,
        cancel_provider=services.work.cancel,
        health_provider=_health,
        registry=services.registry,
        text_turn_provider=_submit_text_turn,
        shutdown_token=os.environ.get("ATLAS_SHUTDOWN_TOKEN"),
        shutdown_provider=_request_shutdown,
    )
    desktop_status_task = asyncio.create_task(desktop_status.run())
    _BG_TASKS.add(desktop_status_task)
    desktop_status_task.add_done_callback(_BG_TASKS.discard)
    try:
        ctx.add_shutdown_callback(_shutdown)
        _emit_ui_url(authorizer, server.port)
        services.warm_model_client()

        engagement = engagement_mod.Engagement(float(cfg["engagement_timeout_s"]))
        addressing = _addressing_from_config(cfg)
        turn_ownership = TurnOwnership()
        wake_device = cfg.get("wake_input_device")
        initial_wake_device = None
        if wake_device and wake_device != FOLLOW_SENTINEL:
            initial_wake_device = wakeword.resolve_input_device(wake_device)
        wake_switch = wakeword.InputDeviceSwitch(initial_wake_device)
        publisher.set_audio(devicewatch.audio_status(cfg))
        restart_coalescer = devicewatch.AudioRestartCoalescer(
            lambda reason: _restart_worker(reason, ctx.shutdown),
            loop=loop,
        )
        audio_failure = devicewatch.audio_failure_callback(
            publisher,
            "input",
            restart_coalescer.request,
        )

        def _engage() -> None:
            _engage_wake(
                session,
                session_started=session_started,
                publisher=publisher,
                engagement=engagement,
                addressing=addressing,
            )

        def _on_wake() -> None:
            loop.call_soon_threadsafe(_engage)

        def _on_audio_signal(value: float, bands: list[float]) -> None:
            loop.call_soon_threadsafe(publisher.set_audio_signal, value, bands)

        threading.Thread(
            target=wakeword.listen,
            args=(_on_wake, cfg["wake_model"]),
            kwargs={
                "device": cfg.get("wake_input_device"),
                "threshold": cfg.get("wake_threshold", wakeword.THRESHOLD),
                "patience": cfg.get("wake_patience", wakeword.PATIENCE),
                "on_signal": _on_audio_signal,
                "bands_enabled": lambda: publisher.state != state.ASLEEP,
                "device_switch": wake_switch,
                "on_failure": audio_failure,
                "on_model": _wake_model_callback(publisher, loop),
            },
            daemon=True,
        ).start()

        services.brain.on_tool = _record_tool
        pending_terminal = []

        def _deliver_terminal(job) -> None:
            if session is None:
                pending_terminal.append(job)
                return
            _announce_terminal(job, publisher, session, engagement, addressing)

        services.work.on_terminal(
            lambda job: loop.call_soon_threadsafe(_deliver_terminal, job)
        )

        mcp_task = asyncio.create_task(
            _connect_mcp_and_settle(services.mcp, services.registry, services.brain)
        )
        work_task = asyncio.create_task(services.work.run(stop_work))
        _BG_TASKS.update((mcp_task, work_task))
        mcp_task.add_done_callback(_BG_TASKS.discard)
        work_task.add_done_callback(_BG_TASKS.discard)

        await ctx.connect()
        stt_kwargs: dict[str, Any] = {"model": "flux-general-en"}
        keyterms = _stt_keyterms(cfg)
        if keyterms:
            stt_kwargs["keyterm"] = keyterms
        # --- Self-barge-in diagnosis + fix (CC5, reworked after adversarial
        # review found the original three-knobs-only mitigation didn't stop
        # the bug) -----------------------------------------------------------
        # Atlas's own TTS output re-enters the laptop mic (acoustic loopback,
        # not a room echo), gets transcribed by deepgram, and is delivered as
        # a normal user turn 1-2ms after playout starts -- fast enough that
        # it reads as a barge-in on Atlas's own speech, not a real
        # interruption. Proven in the field: fragments like "two fifty five"
        # arriving as user turns cut ~30% of responses short before AEC had
        # time to converge. Before this unit, AgentSession was constructed
        # with zero interruption tuning -- every default applied.
        #
        # THE FIX is AtlasAgent.stt_node/SpeechEchoGuard (defined above,
        # right before AtlasAgent): Atlas already knows exactly what it is
        # currently saying (every chunk flows through AtlasAgent.tts_node
        # before reaching TTS), so it can drop STT events that echo that
        # text -- deterministically, without gating the mic -- before
        # livekit's own interruption/turn-detection logic ever sees them.
        # See the comment on SpeechEchoGuard and AtlasAgent.stt_node for the
        # full mechanism and the hook-point evidence.
        #
        # The three AgentSession knobs below are kept as harmless
        # defense-in-depth, NOT as the fix -- their real, measured semantics
        # (verified by reading the installed livekit-agents 1.6.6 source, not
        # assumed) are weaker than the first pass of this unit believed:
        #   - min_interruption_words=2 (voice.min_interruption_words): the
        #     ONE knob that actually gates the STT interim-transcript path
        #     Atlas uses (agent_activity.py:1896-1969,
        #     _interrupt_by_audio_activity, ~line 1920). Still only blocks
        #     fragments shorter than 2 words -- "two fifty five" is 3 words
        #     and clears it, which is exactly why the knob alone was
        #     insufficient and SpeechEchoGuard exists.
        #   - min_interruption_duration=0.8s (voice.min_interruption_duration_s):
        #     NOT consulted on the STT path at all. on_interim_transcript
        #     (agent_activity.py:2072-2090) calls
        #     _interrupt_by_audio_activity() unconditionally on any non-empty
        #     interim text; `min_duration` is only read by the separate
        #     VAD-activity path (on_vad_inference_done, ~line 2038). This
        #     knob only helps a VAD-driven interruption, which STT turn
        #     detection barely exercises -- kept for that residual case, not
        #     for the diagnosed bug.
        #   - aec_warmup_duration=4.0s (voice.aec_warmup_duration_s): a
        #     ONE-SHOT per-session grace period, not per-utterance.
        #     agent_session.py:1694-1701's _on_aec_warmup_expired permanently
        #     zeroes _aec_warmup_remaining the first time it fires, and
        #     _update_agent_state (:1734-1748) only arms the timer while that
        #     value is still > 0 -- so only Atlas's very first utterance of a
        #     session ever gets the grace period. Every later response (the
        #     common case, and where the field evidence came from) gets none
        #     of it. Kept because the first utterance still benefits.
        # All three are still config-driven from config/atlas.yaml's `voice:`
        # section, validated on load by _voice_config() above -- see that
        # file's `voice:` comment for the same corrected semantics.
        #
        # The airtight fallback -- session.input.set_audio_enabled(False) for
        # the duration of agent speech, the same primitive _sleep_session
        # already uses to gate the mic while Atlas is asleep (see app.py:549,
        # `if not session.input.audio_enabled: return False` /
        # `session.input.set_audio_enabled(False)`) -- is still NOT
        # implemented here. It would also suppress genuine user barge-in
        # while Atlas is talking, and that's Daniel's pending decision, not a
        # default this unit should ship silently. SpeechEchoGuard is meant to
        # make that fallback unnecessary for the common case: it only drops
        # transcripts that match what Atlas is currently saying, so genuine
        # barge-in (different words) is untouched.
        # ---------------------------------------------------------------
        session = AgentSession(
            stt=deepgram.STTv2(**stt_kwargs),
            vad=silero.VAD.load(),
            llm=None,
            tts=_build_tts(cfg),
            turn_detection="stt",
            min_interruption_words=voice_cfg["min_interruption_words"],
            min_interruption_duration=voice_cfg["min_interruption_duration_s"],
            aec_warmup_duration=voice_cfg["aec_warmup_duration_s"],
        )

        # F3 observability (CC5), scoped to what's cheap here: SpeechHandle
        # exposes a clean, synchronous `.interrupted` bool
        # (livekit.agents.voice.speech_handle.SpeechHandle.interrupted --
        # `self._interrupt_fut.done()`), and AgentSession emits a
        # "speech_created" event synchronously, in-line inside
        # AgentActivity.say() (agent_activity.py:1346-1349), before any
        # await -- i.e. in the same asyncio-task/contextvars context as the
        # `session.say(...)` call that created it. That means a listener
        # registered here can read worker.traces' active (recorder, turn)
        # pair via traces_mod.active_turn() and get the *same* turn that
        # worker/app.py's response funnel (_submit_voice_turn, protected
        # region) is about to record a RESPOND step for -- without this unit
        # touching that funnel's call sites. SpeechHandle also registers its
        # internal done-callback in __init__, before any caller ever awaits
        # the handle, so a done-callback added here fires (via
        # asyncio.Future's call_soon ordering) before session.say()'s own
        # await resumes -- i.e. before _submit_voice_turn's
        # record_current_respond() runs -- so there is no race. See
        # worker/traces.py (mark_speech_interrupted, _Turn.speech_interrupted,
        # TraceRecorder.respond) for the other half of this.
        #
        # Deliberately NOT implemented: mirroring the same flag onto the
        # published transcript line (a `truncated: true` field on the atlas
        # chat line) as the diagnosis's item 2 also asked for. That requires
        # either touching worker/app.py's response funnel at the
        # `publisher.add_line("atlas", response)` call (CC1's protected
        # region, :227-241) or adding an "amend last line" surface to
        # worker/state.py (outside this unit's file scope: config + this
        # session-construction region + traces only). Skipped rather than
        # contorted around either boundary; the RESPOND trace step already
        # carries the same signal for now, and this is a small follow-up
        # once CC1's funnel work lands.
        def _on_speech_created(event) -> None:
            from worker import traces as traces_mod

            active = traces_mod.active_turn()
            if active is None:
                return
            _, active_turn = active

            def _mark_if_interrupted(handle) -> None:
                if handle.interrupted:
                    traces_mod.mark_speech_interrupted(active_turn)

            event.speech_handle.add_done_callback(_mark_if_interrupted)

        session.on("speech_created", _on_speech_created)

        agent = AtlasAgent(
            instructions="Atlas voice I/O is host controlled.",
            llm=None,
            tools=[],
            echo_guard=SpeechEchoGuard(**voice_cfg["echo_guard"]),
        )
        await session.start(agent=agent, room=ctx.room)
        session_started = True
        publisher.ready = True
        for terminal_job in pending_terminal:
            _announce_terminal(terminal_job, publisher, session, engagement, addressing)
        pending_terminal.clear()

        agent.turn_handler = _submit_text_turn

        if TEXT_MODE:
            return

        audio_watcher = devicewatch.start_audio_follow(
            cfg,
            publisher,
            wake_switch,
            request_restart=restart_coalescer.request,
        )
        session.input.set_audio_enabled(False)

        @session.on("agent_state_changed")
        def _on_agent_state(event) -> None:
            _apply_agent_state(engagement, publisher, event.new_state)

        async def _audio_turn(text: str) -> None:
            await _handle_audio_turn(
                text,
                intents=intents,
                brain=services.brain,
                session=session,
                publisher=publisher,
                engagement=engagement,
                addressing=addressing,
                sleep=_sleep,
                turn_ownership=turn_ownership,
                speaking_probe=agent.echo_guard.within_echo_window,
                ack=turn_ack,
                trace_recorder=_traces(),
            )

        agent.turn_handler = _audio_turn
        sleep_task = asyncio.create_task(_sleep_watch(
            engagement,
            _sleep,
            turn_ownership=turn_ownership,
        ))
        _BG_TASKS.add(sleep_task)
        sleep_task.add_done_callback(_BG_TASKS.discard)

    except BaseException:
        try:
            await asyncio.shield(_shutdown())
        finally:
            raise


def _console_output_args(
    argv: list[str],
    cfg: dict,
    resolve=wakeword.resolve_output_device,
) -> list[str]:
    if "console" not in argv or "--text" in argv or "--list-devices" in argv:
        return []
    if any(value == "--output-device" or value.startswith("--output-device=") for value in argv):
        return []
    configured = cfg.get("tts_output_device")
    if not configured or configured == FOLLOW_SENTINEL:
        return []
    index = resolve(configured)
    if index is None:
        logger.critical("configured TTS output device was not found")
        return []
    return ["--output-device", str(index)]


def _console_input_args(
    argv: list[str],
    cfg: dict,
    resolve=wakeword.resolve_input_device,
) -> list[str]:
    if "console" not in argv or "--text" in argv or "--list-devices" in argv:
        return []
    if any(value == "--input-device" or value.startswith("--input-device=") for value in argv):
        return []
    configured = cfg.get("wake_input_device")
    if not configured or configured == FOLLOW_SENTINEL:
        return []
    index = resolve(configured)
    if index is None:
        logger.critical("configured wake input device was not found")
        return []
    return ["--input-device", str(index)]


WORKER_LOG_MAX_BYTES = 256 * 1024
WORKER_LOG_BACKUPS = 2
WORKER_LOG_MESSAGE_LIMIT = 200
# A token that looks like a path, a URL, or an address. Kept deliberately
# blunt: "1/3" (a retry count) survives, "C:\Users\...", "~/.claude/projects",
# "https://host/x" and "someone@example.test" do not.
_UNSAFE_LOG_TOKEN = re.compile(r"[\\~@]|[A-Za-z]:[\\/]|/[A-Za-z]")
# Secret shapes are sanitize.secret_shaped -- ONE definition, shared with the
# conversation store (worker/transcript.py), because two persistent sinks
# checking two copies of the same pattern is one copy too many. The comment
# explaining what it covers and why lives with the pattern.


class _HostShapedFormatter(logging.Formatter):
    """Write the host's own sentence and stop where it stops being one.

    A file handler on the whole atlas.* tree persists every WARNING any module
    raises, including ones this unit does not own and ones not written yet.
    Most are already host-shaped -- fixed text plus a count, a category, an
    exception type -- but not all: a traceback carries absolute source paths
    and the original OSError text, and the file-root warnings interpolate
    configured paths. Rule 10 is a property of the FILE, so it is enforced
    here, where every record must pass, instead of trusting 60-odd call sites
    to stay disciplined.

    This enforces the SHAPE of what is written; it is not a promise that any
    particular caller is well behaved. Tracebacks never reach the file (the
    console lane still gets them -- the record is read, never mutated), and
    every line is capped. Redaction runs to the END of the message rather than
    over the one offending token: paths with spaces are the norm on Windows,
    so "root C:\\Users\\d\\Tax Returns 2025 is unavailable" would otherwise
    keep the half that actually says something about Daniel. The host's fixed
    prefix survives because it is written before the interpolation.
    """

    def format(self, record: logging.LogRecord) -> str:
        message = " ".join(str(record.getMessage()).split())
        kept = []
        for token in message.split(" "):
            if _UNSAFE_LOG_TOKEN.search(token) or sanitize.secret_shaped(token):
                kept.append("<redacted>")
                break
            kept.append(token)
        redacted = " ".join(kept)[:WORKER_LOG_MESSAGE_LIMIT]
        return f"{self.formatTime(record)} {record.levelname} {record.name} {redacted}"


def _configure_worker_logging(local_app_data=None):
    """Persist worker WARNING+ lines to one bounded rotating file.

    These lines existed only in the console the desktop drains, so the ones
    worth reading after the fact -- a claim the guard refused, a turn that said
    nothing, a dropped trace record -- were gone by the time anyone asked what
    happened. What makes keeping them safe is not the call sites' good manners
    but _HostShapedFormatter above, which every record goes through (rule 10).
    The file is opened on the first record (delay=True), so a worker that never
    warns leaves nothing behind.
    """
    root = local_app_data if local_app_data is not None else os.environ.get("LOCALAPPDATA")
    if not root:
        return None
    try:
        path = Path(root) / "Atlas" / "logs" / "worker.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path,
            maxBytes=WORKER_LOG_MAX_BYTES,
            backupCount=WORKER_LOG_BACKUPS,
            encoding="utf-8",
            delay=True,
        )
    except Exception:
        return None
    handler.setLevel(logging.WARNING)
    handler.setFormatter(_HostShapedFormatter())
    handler._atlas_worker_file_handler = True
    atlas_logger = logging.getLogger("atlas")
    for existing in list(atlas_logger.handlers):
        if getattr(existing, "_atlas_worker_file_handler", False):
            atlas_logger.removeHandler(existing)
            existing.close()
    atlas_logger.addHandler(handler)
    # Only ever lowers the floor to WARNING, never raises it: the console lane
    # keeps whatever level the worker runtime configured for INFO and below.
    if atlas_logger.getEffectiveLevel() > logging.WARNING:
        atlas_logger.setLevel(logging.WARNING)
    return handler


def main() -> int:
    global _worker_exit_code
    _worker_exit_code = 0
    jobobject.assign_current_process()
    _configure_worker_logging()
    try:
        cfg = _cfg()
        sys.argv.extend(_console_output_args(sys.argv, cfg))
        sys.argv.extend(_console_input_args(sys.argv, cfg))
    except Exception:
        logger.exception("could not resolve configured audio devices")
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
    return _worker_exit_code


if __name__ == "__main__":
    sys.exit(main())
