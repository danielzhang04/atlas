"""Worker state core (design §2/§3): pure publisher — Atlas state machine, transcript
ring, one event stream. No I/O, no asyncio. Injectable clock stamps `since` and line `t`."""
from datetime import datetime, timezone

from worker.state import (
    ACTING,
    ASLEEP,
    LISTENING,
    SPEAKING,
    STATE_FROM_AGENT,
    THINKING,
    StatePublisher,
)


def _dt(sec: int) -> datetime:
    return datetime(2026, 7, 20, 12, 0, sec, tzinfo=timezone.utc)


def test_initial_state_is_asleep():
    p = StatePublisher(clock=lambda: _dt(0))
    assert p.state == ASLEEP
    assert p.session_id is None
    snap = p.snapshot()
    assert snap["state"] == ASLEEP
    assert snap["session_id"] is None
    assert snap["transcript"] == []


def test_state_transitions_wake_think_speak_sleep():
    now = [_dt(0)]
    p = StatePublisher(clock=lambda: now[-1])
    # wake -> LISTENING
    now.append(_dt(1))
    p.start_session()
    p.set_state(LISTENING)
    assert p.state == LISTENING
    assert p.session_id is not None
    assert p.snapshot()["since"] == _dt(1).isoformat()
    # llm-start -> THINKING
    now.append(_dt(2))
    p.set_state(THINKING)
    assert p.state == THINKING
    # confirmed broker execution -> ACTING
    now.append(_dt(3))
    p.set_state(ACTING)
    assert p.state == ACTING
    # tts-start -> SPEAKING
    now.append(_dt(4))
    p.set_state(SPEAKING)
    assert p.state == SPEAKING
    # tts-end -> LISTENING
    now.append(_dt(5))
    p.set_state(LISTENING)
    assert p.state == LISTENING
    # sleep -> ASLEEP
    now.append(_dt(6))
    p.set_state(ASLEEP)
    assert p.state == ASLEEP
    assert p.snapshot()["since"] == _dt(6).isoformat()


def test_set_state_noop_does_not_restamp_or_reemit():
    now = [_dt(0)]
    events = []
    p = StatePublisher(clock=lambda: now[-1])
    p.subscribe(events.append)
    now.append(_dt(1))
    p.set_state(LISTENING)
    now.append(_dt(2))
    p.set_state(LISTENING)  # same state -> no-op
    assert p.snapshot()["since"] == _dt(1).isoformat()
    assert [e for e in events if e[0] == "state"] == [("state", LISTENING)]


def test_confirmed_action_holds_acting_and_reveals_latest_voice_state_at_final_end():
    now = [_dt(0)]
    events = []
    p = StatePublisher(clock=lambda: now[-1])
    p.set_state(THINKING)
    p.subscribe(events.append)

    now.append(_dt(1))
    p.begin_confirmed_action()
    assert p.state == ACTING
    assert p.snapshot()["since"] == _dt(1).isoformat()

    now.append(_dt(2))
    p.set_state(SPEAKING)  # voice event while the confirmed action is still running
    assert p.state == ACTING
    assert p.snapshot()["since"] == _dt(1).isoformat()
    assert [event for event in events if event[0] == "state"] == [("state", ACTING)]

    now.append(_dt(3))
    p.end_confirmed_action()
    assert p.state == SPEAKING
    assert p.snapshot()["since"] == _dt(3).isoformat()
    assert [event for event in events if event[0] == "state"] == [
        ("state", ACTING), ("state", SPEAKING),
    ]


def test_confirmed_action_depth_keeps_acting_until_all_runs_settle():
    p = StatePublisher(clock=lambda: _dt(0))
    p.set_state(LISTENING)
    p.begin_confirmed_action()
    p.begin_confirmed_action()
    p.set_state(THINKING)
    p.end_confirmed_action()
    assert p.state == ACTING
    p.set_state(SPEAKING)
    p.end_confirmed_action()
    assert p.state == SPEAKING


def test_agent_state_mapping_matches_livekit_literal():
    # livekit AgentState = Literal["initializing","idle","listening","thinking","speaking"]
    # (voice/events.py:312). thinking/speaking map directly; idle/listening -> LISTENING;
    # initializing is deliberately unmapped (session warm-up while ASLEEP).
    assert STATE_FROM_AGENT["thinking"] == THINKING
    assert STATE_FROM_AGENT["speaking"] == SPEAKING
    assert STATE_FROM_AGENT["listening"] == LISTENING
    assert STATE_FROM_AGENT["idle"] == LISTENING
    assert "initializing" not in STATE_FROM_AGENT


def test_new_session_id_per_wake():
    p = StatePublisher(clock=lambda: _dt(0))
    s1 = p.start_session()
    s2 = p.start_session()
    assert s1 != s2
    assert p.session_id == s2


def test_add_line_ring_truncates_at_50():
    p = StatePublisher(clock=lambda: _dt(0))
    for i in range(60):
        p.add_line("user", f"line {i}")
    snap = p.snapshot()
    assert len(snap["transcript"]) == 50
    assert snap["transcript"][0]["text"] == "line 10"
    assert snap["transcript"][-1]["text"] == "line 59"


def test_add_line_custom_ring_size():
    p = StatePublisher(clock=lambda: _dt(0), ring_size=3)
    for i in range(5):
        p.add_line("atlas", str(i))
    texts = [ln["text"] for ln in p.snapshot()["transcript"]]
    assert texts == ["2", "3", "4"]


def test_line_shape_and_clock_stamp():
    now = [_dt(7)]
    p = StatePublisher(clock=lambda: now[-1])
    p.add_line("atlas", "Yes?")
    line = p.snapshot()["transcript"][0]
    assert line == {"t": _dt(7).isoformat(), "role": "atlas", "text": "Yes?"}


def test_subscriber_receives_state_and_line_events():
    p = StatePublisher(clock=lambda: _dt(0))
    events = []
    p.subscribe(events.append)
    p.set_state(LISTENING)
    p.add_line("user", "what's in the queue")
    assert ("state", LISTENING) in events
    line_events = [e for e in events if e[0] == "line"]
    assert len(line_events) == 1
    assert line_events[0][1]["text"] == "what's in the queue"
    assert line_events[0][1]["role"] == "user"


def test_unsubscribe_stops_delivery():
    p = StatePublisher(clock=lambda: _dt(0))
    events = []
    p.subscribe(events.append)
    p.unsubscribe(events.append)
    p.set_state(LISTENING)
    assert events == []


def test_subscriber_exception_does_not_break_publisher():
    p = StatePublisher(clock=lambda: _dt(0))
    good = []

    def boom(ev):
        raise RuntimeError("subscriber blew up")

    p.subscribe(boom)
    p.subscribe(good.append)
    p.set_state(THINKING)  # must not raise
    p.add_line("user", "still delivered")
    assert ("state", THINKING) in good
    assert good[-1][0] == "line"
    assert p.state == THINKING


def test_snapshot_schema_keys():
    p = StatePublisher(clock=lambda: _dt(0), voice="mars")
    p.start_session()
    snap = p.snapshot()
    assert set(snap.keys()) == {
        "version", "state", "since", "session_id", "voice", "transcript", "filed_cards",
        "output_device", "audio_energy",
    }
    assert snap["version"] == 1
    assert snap["voice"] == "mars"
    assert snap["filed_cards"] == []  # Task 9: present, empty until a card is filed
    assert snap["output_device"] is None  # M4: null until the wiring sets a pin status
    assert snap["audio_energy"] == 0.0
    assert "heartbeat" not in snap    # the HTTP layer (Task 5) stamps it at request time


def test_snapshot_surfaces_output_device_pin():
    # M4 (2026-07-21): a configured-but-unresolved pin must be visible in /state so the dashboard
    # can flag that TTS fell back to the (drifting) system default.
    p = StatePublisher(clock=lambda: _dt(0))
    p.set_output_device({"configured": "Speakers", "resolved": None})
    assert p.snapshot()["output_device"] == {"configured": "Speakers", "resolved": None}
    p.set_output_device({"configured": "Speakers", "resolved": "Speakers (Realtek)"})
    assert p.snapshot()["output_device"]["resolved"] == "Speakers (Realtek)"


def test_voice_is_settable_by_wiring():
    p = StatePublisher(clock=lambda: _dt(0))
    assert p.snapshot()["voice"] is None
    p.voice = "matilda"
    assert p.snapshot()["voice"] == "matilda"


def test_audio_energy_is_ephemeral_bounded_and_hidden_while_asleep():
    p = StatePublisher(clock=lambda: _dt(0))
    p.set_audio_energy(.42)
    assert p.audio_energy == 0.0 and p.snapshot()["audio_energy"] == 0.0
    p.set_state(LISTENING)
    assert p.audio_energy == .42
    p.set_audio_energy(4.0)
    assert p.audio_energy == 1.0
    p.set_audio_energy(-2.0)
    assert p.audio_energy == 0.0
    p.set_audio_energy("not-a-number")
    assert p.audio_energy == 0.0


def test_output_device_status_follow_mode_reports_following():
    from worker import app
    status = app._output_device_status(
        {"tts_output_device": "follow"},
        resolve=lambda s, devices=None: (_ for _ in ()).throw(AssertionError("must not resolve in follow mode")),
        boot_default=lambda: "Headphones (Px7 S2e)")
    assert status == {"configured": "follow", "resolved": "Headphones (Px7 S2e)", "following": True}


def test_output_device_status_pin_mode_reports_not_following():
    from worker import app
    status = app._output_device_status(
        {"tts_output_device": "Speakers (Realtek"}, resolve=lambda s, devices=None: 5)
    assert status["following"] is False
    assert status["configured"] == "Speakers (Realtek"


def test_console_output_args_follow_mode_passes_no_flag():
    from worker import app
    out = app._console_output_args(["worker.app", "console"], {"tts_output_device": "follow"},
                                   resolve=lambda s, devices=None: 5)
    assert out == []
