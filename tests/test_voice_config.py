"""CC5: config-driven self-barge-in mitigation.

Covers the `voice:` config section (parsing + validation), the three
AgentSession interruption/AEC knobs actually reaching the AgentSession
constructor, and the `interrupted` flag plumbed from a SpeechHandle
done-callback into the RESPOND trace step via worker.traces' active-turn
side channel (see the diagnosis comment at the AgentSession construction in
worker/app.py and the accompanying comments in worker/traces.py).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from worker import app

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# _voice_config: parsing + validation
# ---------------------------------------------------------------------------

_ECHO_DEFAULTS = {
    "tail_s": 1.0,
    "buffer_window_s": 10.0,
    "max_words": 500,
    "min_overlap_ratio": 0.8,
}
_DEFAULTS = {
    "min_interruption_words": 2,
    "min_interruption_duration_s": 0.8,
    "aec_warmup_duration_s": 4.0,
    "echo_guard": _ECHO_DEFAULTS,
}


def test_voice_config_defaults_when_section_absent():
    assert app._voice_config({}) == _DEFAULTS


def test_voice_config_defaults_when_section_empty():
    assert app._voice_config({"voice": {}}) == _DEFAULTS


def test_voice_config_overrides_are_used_when_present():
    cfg = {"voice": {
        "min_interruption_words": 3,
        "min_interruption_duration_s": 1.2,
        "aec_warmup_duration_s": 5.0,
    }}
    assert app._voice_config(cfg) == {
        "min_interruption_words": 3,
        "min_interruption_duration_s": 1.2,
        "aec_warmup_duration_s": 5.0,
        "echo_guard": _ECHO_DEFAULTS,
    }


def test_voice_config_partial_override_keeps_other_defaults():
    resolved = app._voice_config({"voice": {"min_interruption_words": 5}})
    assert resolved == {**_DEFAULTS, "min_interruption_words": 5}


@pytest.mark.parametrize("voice", [1, "voice", [1, 2], True])
def test_voice_config_rejects_non_mapping_section(voice):
    with pytest.raises(ValueError, match=r"invalid Atlas configuration: voice$"):
        app._voice_config({"voice": voice})


@pytest.mark.parametrize("bad", [-1, 1.5, "2", True, False])
def test_voice_config_rejects_invalid_min_interruption_words(bad):
    with pytest.raises(ValueError, match="min_interruption_words"):
        app._voice_config({"voice": {"min_interruption_words": bad}})


@pytest.mark.parametrize("bad", [0, -0.1, "0.8", True])
def test_voice_config_rejects_invalid_min_interruption_duration(bad):
    with pytest.raises(ValueError, match="min_interruption_duration_s"):
        app._voice_config({"voice": {"min_interruption_duration_s": bad}})


@pytest.mark.parametrize("bad", [0, -1.0, "4.0", True])
def test_voice_config_rejects_invalid_aec_warmup_duration(bad):
    with pytest.raises(ValueError, match="aec_warmup_duration_s"):
        app._voice_config({"voice": {"aec_warmup_duration_s": bad}})


def test_production_config_voice_section_resolves_to_the_chosen_defaults():
    cfg = yaml.safe_load(
        (ROOT / "config" / "atlas.yaml").read_text(encoding="utf-8"),
    )
    assert app._voice_config(cfg) == _DEFAULTS


# --- F7: the echo guard's own tunables, exposed in the same section --------

def test_echo_guard_knobs_are_written_out_in_the_production_config():
    """They must be VISIBLE in atlas.yaml, not merely defaulted: the point of
    F7 is that the knobs that decide whether a genuine barge-in survives are
    the ones Daniel can reach without editing Python."""
    raw = yaml.safe_load(
        (ROOT / "config" / "atlas.yaml").read_text(encoding="utf-8"),
    )["voice"]
    for name, value in _ECHO_DEFAULTS.items():
        assert raw[name] == value


def test_echo_guard_knobs_are_the_speech_echo_guard_constructor_keywords():
    """1:1 with SpeechEchoGuard(...), so a config value cannot mean something
    else in code -- and so a renamed constructor keyword fails here rather
    than at worker start."""
    import inspect

    params = inspect.signature(app.SpeechEchoGuard.__init__).parameters
    for name, value in _ECHO_DEFAULTS.items():
        assert name in params
        assert params[name].default == value


def test_echo_guard_overrides_are_used_when_present():
    cfg = {"voice": {
        "tail_s": 2.5,
        "buffer_window_s": 30.0,
        "max_words": 120,
        "min_overlap_ratio": 0.95,
    }}
    assert app._voice_config(cfg)["echo_guard"] == {
        "tail_s": 2.5,
        "buffer_window_s": 30.0,
        "max_words": 120,
        "min_overlap_ratio": 0.95,
    }


def test_echo_guard_partial_override_keeps_other_defaults():
    resolved = app._voice_config({"voice": {"tail_s": 3.0}})["echo_guard"]
    assert resolved == {**_ECHO_DEFAULTS, "tail_s": 3.0}


@pytest.mark.parametrize("name", ["tail_s", "buffer_window_s"])
@pytest.mark.parametrize("bad", [0, -1.0, "1.0", True])
def test_voice_config_rejects_invalid_echo_guard_seconds(name, bad):
    with pytest.raises(ValueError, match=f"voice.{name}"):
        app._voice_config({"voice": {name: bad}})


@pytest.mark.parametrize("bad", [0, -5, 1.5, "500", True])
def test_voice_config_rejects_invalid_echo_guard_max_words(bad):
    with pytest.raises(ValueError, match="voice.max_words"):
        app._voice_config({"voice": {"max_words": bad}})


@pytest.mark.parametrize("bad", [0, -0.1, 1.01, 2, "0.8", True])
def test_voice_config_rejects_invalid_echo_guard_min_overlap_ratio(bad):
    # Above 1 can never be met and at/below 0 is met by anything: both would
    # silently turn the guard off or all the way up.
    with pytest.raises(ValueError, match="voice.min_overlap_ratio"):
        app._voice_config({"voice": {"min_overlap_ratio": bad}})


def test_agent_session_signature_still_has_the_three_voice_kwargs():
    """Pin the exact kwarg names this unit passes to AgentSession(...). If a
    future livekit-agents upgrade renames or removes one of these (they were
    already marked deprecated in 1.6.6, in favor of
    turn_handling=TurnHandlingOptions(...) -- see deprecate_params in
    livekit/agents/voice/agent_session.py), the real AgentSession(**kwargs)
    call in worker.app.entrypoint would silently start raising a TypeError
    at session construction; this test fails loudly in CI instead."""
    import inspect

    from livekit.agents import AgentSession

    params = inspect.signature(AgentSession.__init__).parameters
    assert "min_interruption_words" in params
    assert "min_interruption_duration" in params
    assert "aec_warmup_duration" in params


# ---------------------------------------------------------------------------
# Session construction: the three knobs reach AgentSession, and a
# speech_created listener gets registered.
# ---------------------------------------------------------------------------

def _run_entrypoint_capturing_session(monkeypatch, cfg: dict):
    """Drive worker.app.entrypoint() far enough (in TEXT_MODE) to reach the
    AgentSession construction, mirroring the harness in
    test_stateserver.test_entrypoint_warms_model_only_after_build_and_state_server_start.
    Returns (agent_session_kwargs, handlers, atlas_agent_kwargs) where
    handlers maps event name (e.g. "speech_created") to the callback
    worker.app registered via session.on(...), and atlas_agent_kwargs is what
    AtlasAgent(...) was constructed with (F7: the echo guard is built from
    config there).
    """
    session_kwargs: dict = {}
    handlers: dict = {}
    agent_kwargs: dict = {}

    class FakeWork:
        launcher = SimpleNamespace(available=True)

        def on_terminal(self, _callback):
            return None

        async def cancel(self, _job_id):
            return True

        async def run(self, _stop):
            return None

    class FakeMcp:
        async def connect(self, _registry, **_kwargs):
            return None

        def status(self):
            return []

    class FakeRuntime:
        def __init__(self):
            self.brain = SimpleNamespace(on_tool=None)
            self.work = FakeWork()
            self.mcp = FakeMcp()
            self.registry = SimpleNamespace(set_execution_observer=lambda _observer: None)
            self.store = SimpleNamespace(events=lambda *_args: [], result=lambda *_args: None)

        def warm_model_client(self):
            return None

    def build(_cfg, *, paired_url):
        assert callable(paired_url)
        return FakeRuntime()

    class FakeSession:
        input = SimpleNamespace(set_audio_enabled=lambda _enabled: None)

        async def start(self, **_kwargs):
            return None

        def interrupt(self):
            return None

        def on(self, event, handler):
            handlers[event] = handler

    def fake_agent_session(**kwargs):
        session_kwargs.update(kwargs)
        return FakeSession()

    class FakeServer:
        port = 4321

    async def start_server(*_args, **_kwargs):
        return FakeServer()

    class FakeContext:
        room = object()

        async def connect(self):
            return None

        def add_shutdown_callback(self, _callback):
            return None

        def shutdown(self, _reason):
            return None

    monkeypatch.setattr(app, "TEXT_MODE", True)
    monkeypatch.setattr(app, "_cfg", lambda: cfg)
    monkeypatch.setattr(app.runtime, "build", build)
    monkeypatch.setattr(app.jobobject, "assign_current_process", lambda: None)
    monkeypatch.setattr(app.envload, "load_private_environment", lambda: None)
    monkeypatch.setattr(app.router, "Addressing", lambda *_args: object())
    monkeypatch.setattr(app.router, "vocabulary", lambda _cfg: [])
    monkeypatch.setattr(app.wakeword, "resolve_input_device", lambda _device: 7)
    monkeypatch.setattr(app.wakeword, "InputDeviceSwitch", lambda _device: object())
    monkeypatch.setattr(app.devicewatch, "audio_status", lambda _cfg: {})
    monkeypatch.setattr(
        app.devicewatch, "AudioRestartCoalescer",
        lambda *_args, **_kwargs: SimpleNamespace(request=lambda _reason: None),
    )
    monkeypatch.setattr(app.devicewatch, "audio_failure_callback", lambda *_args: None)
    monkeypatch.setattr(
        app.threading, "Thread",
        lambda **_kwargs: SimpleNamespace(start=lambda: None),
    )
    monkeypatch.setattr(app, "AgentSession", fake_agent_session)
    def fake_atlas_agent(**kwargs):
        agent_kwargs.update(kwargs)
        return SimpleNamespace(turn_handler=None)

    monkeypatch.setattr(app, "AtlasAgent", fake_atlas_agent)
    monkeypatch.setattr(app, "_build_tts", lambda _cfg: object())
    monkeypatch.setattr(app.deepgram, "STTv2", lambda **_kwargs: object())
    monkeypatch.setattr(app.silero.VAD, "load", lambda: object())
    monkeypatch.setattr(app.stateserver, "start", start_server)
    monkeypatch.setattr(app.desktopapps, "status", lambda **_kwargs: [])
    monkeypatch.setattr(app, "_emit_ui_url", lambda *_args: None)

    asyncio.run(app.entrypoint(FakeContext()))
    return session_kwargs, handlers, agent_kwargs


_BASE_CFG = {
    "active_voice": "mars",
    "engagement_timeout_s": 30,
    "addressed_window_s": 10,
    "state_port": 0,
    "wake_input_device": "Desk microphone",
    "wake_model": "hey_atlas",
}


def test_session_construction_passes_default_voice_knobs_to_agent_session(monkeypatch):
    session_kwargs, _handlers, _agent_kwargs = _run_entrypoint_capturing_session(monkeypatch, dict(_BASE_CFG))

    assert session_kwargs["min_interruption_words"] == 2
    assert session_kwargs["min_interruption_duration"] == 0.8
    assert session_kwargs["aec_warmup_duration"] == 4.0
    # Sanity: existing, unrelated session wiring is untouched.
    assert session_kwargs["turn_detection"] == "stt"


def test_session_construction_passes_configured_voice_knobs_to_agent_session(monkeypatch):
    cfg = dict(_BASE_CFG, voice={
        "min_interruption_words": 4,
        "min_interruption_duration_s": 1.5,
        "aec_warmup_duration_s": 6.0,
    })
    session_kwargs, _handlers, _agent_kwargs = _run_entrypoint_capturing_session(monkeypatch, cfg)

    assert session_kwargs["min_interruption_words"] == 4
    assert session_kwargs["min_interruption_duration"] == 1.5
    assert session_kwargs["aec_warmup_duration"] == 6.0


def test_configured_echo_guard_knobs_reach_the_agents_guard(monkeypatch):
    """F7 end to end: config -> _voice_config -> the SpeechEchoGuard the
    AtlasAgent actually filters with. Without the wiring the guard would keep
    its class defaults and the config section would be decoration."""
    cfg = dict(_BASE_CFG, voice={
        "tail_s": 2.0,
        "buffer_window_s": 25.0,
        "max_words": 90,
        "min_overlap_ratio": 0.6,
    })
    _session_kwargs, _handlers, agent_kwargs = _run_entrypoint_capturing_session(
        monkeypatch, cfg,
    )

    guard = agent_kwargs["echo_guard"]
    assert isinstance(guard, app.SpeechEchoGuard)
    assert guard._tail_s == 2.0
    assert guard._buffer_window_s == 25.0
    assert guard._max_words == 90
    assert guard._min_overlap_ratio == 0.6


def test_session_construction_registers_a_speech_created_listener(monkeypatch):
    _session_kwargs, handlers, _agent_kwargs = _run_entrypoint_capturing_session(monkeypatch, dict(_BASE_CFG))

    assert callable(handlers.get("speech_created"))


def test_invalid_voice_config_fails_entrypoint_before_touching_session(monkeypatch):
    cfg = dict(_BASE_CFG, voice={"min_interruption_words": -1})
    with pytest.raises(ValueError, match="min_interruption_words"):
        _run_entrypoint_capturing_session(monkeypatch, cfg)


# ---------------------------------------------------------------------------
# Interrupted-flag plumbing: speech_created -> done-callback -> traces
# ---------------------------------------------------------------------------

class _FakeSpeechHandle:
    """Stands in for livekit.agents.voice.speech_handle.SpeechHandle far
    enough to exercise the plumbing: add_done_callback stores the callback,
    and fire() invokes it -- the same shape SpeechHandle guarantees (its own
    internal done-callback is registered in __init__, before any caller
    awaits the handle, so callers' done-callbacks run before the awaiting
    session.say() resumes; fire() here models that same "already resolved
    when the caller finds out" ordering)."""

    def __init__(self, *, interrupted: bool) -> None:
        self.interrupted = interrupted
        self._callbacks: list = []

    def add_done_callback(self, callback) -> None:
        self._callbacks.append(callback)

    def fire(self) -> None:
        for callback in self._callbacks:
            callback(self)


def test_speech_created_handler_marks_the_active_turn_when_interrupted(monkeypatch):
    _session_kwargs, handlers, _agent_kwargs = _run_entrypoint_capturing_session(monkeypatch, dict(_BASE_CFG))
    on_speech_created = handlers["speech_created"]
    # Imported here, not at module top: worker/app.py's _on_speech_created
    # also does `from worker import traces as traces_mod` lazily at call
    # time, looking up whatever's currently in sys.modules["worker.traces"].
    # A module-level import in this file would go stale (a different
    # module object, with its own separate _ACTIVE contextvar) whenever an
    # earlier-running test elsewhere pops worker.traces from sys.modules
    # (test_stateserver.py's entrypoint harness does exactly that) between
    # this file's collection and this test's execution.
    from worker import traces as traces_mod

    # enabled=True (the default) so _step() actually appends -- close()/
    # end_turn() are never called in this test, so no file ever gets
    # touched despite the in-memory path.
    recorder = traces_mod.TraceRecorder(":memory:")
    turn = recorder.begin_turn(wake_kind="wake")
    token = traces_mod.activate(recorder, turn)
    try:
        handle = _FakeSpeechHandle(interrupted=True)
        on_speech_created(SimpleNamespace(speech_handle=handle))
        assert turn.speech_interrupted is False  # not yet -- handle isn't done
        handle.fire()
        assert turn.speech_interrupted is True
    finally:
        traces_mod.reset(token)

    recorder.respond(turn, ms=5, ok=True)
    assert turn.steps[-1] == {
        "kind": "RESPOND", "name": None, "ms": 5, "ok": 1,
        "tokens_in": 0, "tokens_out": 0, "cache_read": 0, "cache_write": 0,
        "interrupted": 1,
    }


def test_speech_created_handler_leaves_the_turn_unmarked_when_not_interrupted(monkeypatch):
    _session_kwargs, handlers, _agent_kwargs = _run_entrypoint_capturing_session(monkeypatch, dict(_BASE_CFG))
    on_speech_created = handlers["speech_created"]
    # See the comment in the previous test: import fresh here, not at
    # module top, to avoid a stale worker.traces module reference.
    from worker import traces as traces_mod

    # enabled=True (the default) so _step() actually appends -- close()/
    # end_turn() are never called in this test, so no file ever gets
    # touched despite the in-memory path.
    recorder = traces_mod.TraceRecorder(":memory:")
    turn = recorder.begin_turn(wake_kind="wake")
    token = traces_mod.activate(recorder, turn)
    try:
        handle = _FakeSpeechHandle(interrupted=False)
        on_speech_created(SimpleNamespace(speech_handle=handle))
        handle.fire()
        assert turn.speech_interrupted is False
    finally:
        traces_mod.reset(token)

    recorder.respond(turn, ms=5, ok=True)
    assert turn.steps[-1]["interrupted"] == 0


def test_speech_created_handler_is_a_no_op_before_any_turn_is_active(monkeypatch):
    """say() calls that happen with no _handle_audio_turn in flight at all
    -- the WAKE_LINE from _engage_wake, and _announce_terminal's job-done
    lines -- run with traces_mod.active_turn() returning None (neither is
    reachable from inside _handle_audio_turn_inner: _engage_wake fires off
    a wakeword callback, _announce_terminal off a background job-completion
    callback). The handler must not blow up or touch trace state there.

    NOTE: this is narrower than it looks. The dismiss and "repeat" reflex
    lanes' session.say() calls (SLEEP_LINE, the repeated line) run INSIDE
    _handle_audio_turn_inner, which _handle_audio_turn brackets with
    traces.activate()/reset() -- so active_turn() is NOT None for those.
    See test_dismiss_and_repeat_reflex_speech_sets_the_flag_but_never_persists_it
    below for that (different, still harmless) case."""
    _session_kwargs, handlers, _agent_kwargs = _run_entrypoint_capturing_session(monkeypatch, dict(_BASE_CFG))
    on_speech_created = handlers["speech_created"]
    # See the comment in the marks_the_active_turn test above: import fresh
    # here, not at module top, to avoid a stale worker.traces reference.
    from worker import traces as traces_mod

    assert traces_mod.active_turn() is None
    handle = _FakeSpeechHandle(interrupted=True)
    on_speech_created(SimpleNamespace(speech_handle=handle))  # must not raise
    handle.fire()  # must not raise -- no callback was ever registered


def test_dismiss_and_repeat_reflex_speech_sets_the_flag_but_never_persists_it(monkeypatch):
    """The dismiss lane's SLEEP_LINE and the "repeat" reflex's repeated line
    both call session.say() from inside _handle_audio_turn_inner, which
    _handle_audio_turn runs with a turn already activate()'d -- so an
    echo/interruption on one of those lines DOES flow through the same
    speech_created listener and DOES set turn.speech_interrupted, exactly
    like the main response lane. It's harmless only because neither lane
    ever calls TraceRecorder.respond() (only _submit_voice_turn's tee'd
    response does) -- so the flag lands on the in-memory _Turn object but is
    never read into a persisted RESPOND step: turn.steps stays empty."""
    _session_kwargs, handlers, _agent_kwargs = _run_entrypoint_capturing_session(monkeypatch, dict(_BASE_CFG))
    on_speech_created = handlers["speech_created"]
    from worker import traces as traces_mod

    recorder = traces_mod.TraceRecorder(":memory:")
    turn = recorder.begin_turn(wake_kind="reflex")
    token = traces_mod.activate(recorder, turn)
    try:
        handle = _FakeSpeechHandle(interrupted=True)
        on_speech_created(SimpleNamespace(speech_handle=handle))
        handle.fire()
        assert turn.speech_interrupted is True  # set...
    finally:
        traces_mod.reset(token)

    recorder.end_turn(turn, addressed=False, wake_kind="reflex", outcome="dismissed", total_ms=1)
    assert turn.steps == []  # ...but never persisted: no RESPOND step exists
