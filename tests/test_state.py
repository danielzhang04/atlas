"""In-process voice state and transcript publication."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from worker.state import ASLEEP, LISTENING, SPEAKING, STATE_FROM_AGENT, THINKING, StatePublisher


def _dt(second: int) -> datetime:
    return datetime(2026, 8, 22, 12, 0, second, tzinfo=timezone.utc)


def test_initial_snapshot_publishes_live_voice_wake_and_user_configuration():
    publisher = StatePublisher(clock=lambda: _dt(0), voice="mars", user_name="Daniel")
    assert publisher.state == ASLEEP
    assert publisher.snapshot() == {
        "version": 1,
        "ready": False,
        "state": ASLEEP,
        "since": _dt(0).isoformat(),
        "session_id": None,
        "voice": "mars",
        "wake_model": None,
        "user": {"name": "Daniel"},
        "tool": None,
        "transcript": [],
        "audio": {
            "input": {"name": None, "following": False},
            "output": {"name": None, "following": False},
        },
        "audio_energy": 0.0,
    }


def test_tool_state_publishes_only_name_and_start_time_then_clears():
    publisher = StatePublisher(clock=lambda: _dt(0))

    publisher.set_tool({"name": "search_messages", "since": _dt(1).isoformat()})
    assert publisher.snapshot()["tool"] == {
        "name": "search_messages",
        "since": _dt(1).isoformat(),
    }

    publisher.set_tool(None)
    assert publisher.snapshot()["tool"] is None


def test_wake_model_is_trimmed_and_bounded_in_the_public_snapshot():
    publisher = StatePublisher(
        clock=lambda: _dt(0),
        wake_model=f"  {'wake' * 40}  ",
    )

    assert publisher.snapshot()["wake_model"] == ("wake" * 40)[:128]


def test_wake_model_is_runtime_settable_bounded_and_clearable():
    publisher = StatePublisher(clock=lambda: _dt(0))

    publisher.set_wake_model(f"  {'wake' * 40}  ")
    assert publisher.snapshot()["wake_model"] == ("wake" * 40)[:128]

    publisher.set_wake_model(None)
    assert publisher.snapshot()["wake_model"] is None


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


@pytest.mark.parametrize(
    "phrase",
    [
        "i just said",
        "as i said",
        "as i asked",
        "like i said",
        "what i said",
        "do what i said",
        "i told you",
        "do what i asked",
        "my last instruction",
    ],
)
def test_every_prior_speech_trigger_recalls_ambient_context(phrase):
    publisher = StatePublisher(clock=lambda: _dt(0))
    publisher.add_line("ambient", "quietly schedule the review")

    context = publisher.ambient_context(f"Atlas, {phrase} please")

    assert context is not None
    assert "quietly schedule the review" in context


def test_ambient_context_budget_keeps_newest_lines():
    publisher = StatePublisher(clock=lambda: _dt(0))
    for index in range(30):
        publisher.add_line("ambient", f"line-{index:02d} " + ("x" * 180))

    context = publisher.ambient_context("Atlas, as I said")

    assert context is not None
    assert len(context) <= 4000
    assert "line-29" in context
    assert "line-00" not in context


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


def test_audio_devices_and_voice_are_runtime_settable():
    publisher = StatePublisher(clock=lambda: _dt(0))
    publisher.set_audio({
        "input": {"name": "Headset microphone", "following": True},
        "output": {"name": "Speakers", "following": False},
    })
    publisher.set_audio_device(
        "output",
        {"name": "Headphones", "following": True},
    )
    publisher.voice = "matilda"
    snapshot = publisher.snapshot()
    assert snapshot["voice"] == "matilda"
    assert snapshot["audio"] == {
        "input": {"name": "Headset microphone", "following": True},
        "output": {"name": "Headphones", "following": True},
    }
    assert "output_device" not in snapshot


def test_audio_status_helpers_preserve_follow_and_pin_modes():
    from worker import devicewatch

    status = devicewatch.audio_status(
        {
            "wake_input_device": "follow",
            "tts_output_device": "Speakers",
        },
        resolve_input=lambda _value: (_ for _ in ()).throw(AssertionError("unused")),
        resolve_output=lambda _value: 5,
        boot_input=lambda: "Headset microphone",
        boot_output=lambda: "unused",
        query_device=lambda index: {"name": f"device-{index}"},
    )
    assert status == {
        "input": {"name": "Headset microphone", "following": True},
        "output": {"name": "device-5", "following": False},
    }


def test_audio_bands_are_bounded_copied_and_hidden_asleep():
    publisher = StatePublisher(clock=lambda: _dt(0))
    values = [index / 23 for index in range(24)]
    publisher.set_audio_bands(values)
    values[0] = 1.0
    assert publisher.audio_bands == [0.0] * 24

    publisher.set_state(LISTENING)
    visible = publisher.audio_bands
    assert visible[0] == 0.0
    assert visible[-1] == 1.0
    visible[0] = 1.0
    assert publisher.audio_bands[0] == 0.0

    publisher.set_audio_bands([float("nan")] * 24)
    assert publisher.audio_bands == [0.0] * 24


def test_console_output_args_do_not_pin_follow_mode():
    from worker import app

    assert app._console_output_args(
        ["worker.app", "console"],
        {"tts_output_device": "follow"},
        resolve=lambda _value: 5,
    ) == []
