"""In-process voice state and transcript publication."""
from __future__ import annotations

from datetime import datetime, timezone

from worker.state import ASLEEP, LISTENING, SPEAKING, STATE_FROM_AGENT, THINKING, StatePublisher


def _dt(second: int) -> datetime:
    return datetime(2026, 8, 22, 12, 0, second, tzinfo=timezone.utc)


def test_initial_snapshot_has_only_live_voice_fields():
    publisher = StatePublisher(clock=lambda: _dt(0), voice="mars")
    assert publisher.state == ASLEEP
    assert publisher.snapshot() == {
        "version": 1,
        "state": ASLEEP,
        "since": _dt(0).isoformat(),
        "session_id": None,
        "voice": "mars",
        "transcript": [],
        "output_device": None,
        "audio_energy": 0.0,
    }


def test_state_transitions_stamp_changes_and_ignore_noops():
    now = [_dt(0)]
    events = []
    publisher = StatePublisher(clock=lambda: now[-1])
    publisher.subscribe(events.append)
    now.append(_dt(1))
    publisher.set_state(LISTENING)
    now.append(_dt(2))
    publisher.set_state(LISTENING)
    publisher.set_state(THINKING)
    publisher.set_state(SPEAKING)
    assert publisher.state == SPEAKING
    assert publisher.snapshot()["since"] == _dt(2).isoformat()
    assert [event[1] for event in events if event[0] == "state"] == [
        LISTENING,
        THINKING,
        SPEAKING,
    ]


def test_agent_state_mapping_matches_livekit_literals():
    assert STATE_FROM_AGENT == {
        "thinking": THINKING,
        "speaking": SPEAKING,
        "listening": LISTENING,
        "idle": LISTENING,
    }


def test_sessions_are_unique_and_transcript_is_bounded():
    publisher = StatePublisher(clock=lambda: _dt(0))
    first = publisher.start_session()
    second = publisher.start_session()
    for index in range(60):
        publisher.add_line("user", f"line {index}")
    transcript = publisher.snapshot()["transcript"]
    assert first != second == publisher.session_id
    assert len(transcript) == 50
    assert transcript[0]["text"] == "line 10"
    assert transcript[-1] == {
        "t": _dt(0).isoformat(),
        "role": "user",
        "text": "line 59",
    }


def test_custom_ring_subscriptions_and_unsubscribe():
    publisher = StatePublisher(clock=lambda: _dt(0), ring_size=2)
    events = []
    publisher.subscribe(events.append)
    publisher.add_line("atlas", "one")
    publisher.add_line("atlas", "two")
    publisher.add_line("atlas", "three")
    publisher.unsubscribe(events.append)
    publisher.set_state(LISTENING)
    assert [line["text"] for line in publisher.snapshot()["transcript"]] == ["two", "three"]
    assert len([event for event in events if event[0] == "line"]) == 3
    assert not any(event[0] == "state" for event in events)


def test_bad_subscriber_does_not_block_other_subscribers():
    publisher = StatePublisher(clock=lambda: _dt(0))
    received = []

    def fail(_event):
        raise RuntimeError("test failure")

    publisher.subscribe(fail)
    publisher.subscribe(received.append)
    publisher.set_state(LISTENING)
    publisher.add_line("user", "still delivered")
    assert received[-1][0] == "line"


def test_audio_energy_is_bounded_and_hidden_asleep():
    publisher = StatePublisher(clock=lambda: _dt(0))
    publisher.set_audio_energy(0.42)
    assert publisher.audio_energy == 0.0
    publisher.set_state(LISTENING)
    assert publisher.audio_energy == 0.42
    publisher.set_audio_energy(4.0)
    assert publisher.audio_energy == 1.0
    publisher.set_audio_energy("bad")
    assert publisher.audio_energy == 0.0


def test_output_device_and_voice_are_runtime_settable():
    publisher = StatePublisher(clock=lambda: _dt(0))
    publisher.set_output_device({"configured": "follow", "resolved": "Headphones", "following": True})
    publisher.voice = "matilda"
    snapshot = publisher.snapshot()
    assert snapshot["voice"] == "matilda"
    assert snapshot["output_device"] == {
        "configured": "follow",
        "resolved": "Headphones",
        "following": True,
    }


def test_output_device_helpers_preserve_follow_and_pin_modes():
    from worker import app

    follow = app._output_device_status(
        {"tts_output_device": "follow"},
        resolve=lambda _value: (_ for _ in ()).throw(AssertionError("must not resolve")),
        boot_default=lambda: "Headphones",
    )
    pinned = app._output_device_status(
        {"tts_output_device": "Speakers"},
        resolve=lambda _value: 5,
    )
    assert follow == {"configured": "follow", "resolved": "Headphones", "following": True}
    assert pinned["configured"] == "Speakers" and pinned["following"] is False


def test_console_output_args_do_not_pin_follow_mode():
    from worker import app

    assert app._console_output_args(
        ["worker.app", "console"],
        {"tts_output_device": "follow"},
        resolve=lambda _value: 5,
    ) == []
