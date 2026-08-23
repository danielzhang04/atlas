"""System-default audio device probes, polling, and stream followers."""
from __future__ import annotations

import logging
import threading
import time

from worker import devicewatch


def test_decide_first_observation_is_baseline_not_swap():
    assert devicewatch.decide(None, "id-A") == "baseline"


def test_decide_same_id_is_no_action():
    assert devicewatch.decide("id-A", "id-A") == "none"


def test_decide_changed_id_is_swap():
    assert devicewatch.decide("id-A", "id-B") == "swap"


def test_decide_probe_failure_is_no_action():
    assert devicewatch.decide("id-A", None) == "none"
    assert devicewatch.decide(None, None) == "none"


def test_current_default_input_uses_capture_console_role(monkeypatch):
    from pycaw.constants import EDataFlow, ERole
    from pycaw.utils import AudioUtilities

    calls = []
    endpoint = object()

    class Enumerator:
        def GetDefaultAudioEndpoint(self, flow, role):
            calls.append((flow, role))
            return endpoint

    class Wrapped:
        id = "capture-id"
        FriendlyName = "Headset Microphone"

    monkeypatch.setattr(AudioUtilities, "GetDeviceEnumerator", lambda: Enumerator())
    monkeypatch.setattr(AudioUtilities, "CreateDevice", lambda raw: Wrapped())

    assert devicewatch.current_default_input() == (
        "capture-id",
        "Headset Microphone",
    )
    assert calls == [(EDataFlow.eCapture.value, ERole.eConsole.value)]


def test_current_default_output_returns_wrapped_speaker(monkeypatch):
    from pycaw.utils import AudioUtilities

    class Speaker:
        id = "render-id"
        FriendlyName = "Bluetooth Headphones"

    monkeypatch.setattr(AudioUtilities, "GetSpeakers", lambda: Speaker())

    assert devicewatch.current_default_output() == (
        "render-id",
        "Bluetooth Headphones",
    )


def test_one_watcher_polls_input_and_output_and_fires_independently():
    input_sequence = [
        ("input-A", "Built-in microphone"),
        ("input-A", "Built-in microphone"),
        ("input-B", "Headset microphone"),
    ]
    output_sequence = [
        ("output-A", "Speakers"),
        ("output-B", "Headphones"),
    ]
    input_calls = []
    output_calls = []
    fired = threading.Event()

    def input_probe():
        return input_sequence.pop(0) if input_sequence else ("input-B", "Headset microphone")

    def output_probe():
        return output_sequence.pop(0) if output_sequence else ("output-B", "Headphones")

    def on_input(name):
        input_calls.append(name)
        if output_calls:
            fired.set()

    def on_output(name):
        output_calls.append(name)
        if input_calls:
            fired.set()

    watcher = devicewatch.DeviceWatcher(
        input_probe=input_probe,
        output_probe=output_probe,
        on_input_change=on_input,
        on_output_change=on_output,
        period_s=0.01,
    )
    watcher.start()
    assert fired.wait(timeout=2.0)
    watcher.stop()
    assert input_calls == ["Headset microphone"]
    assert output_calls == ["Headphones"]


def test_watcher_survives_probe_exception():
    sequence = ["boom", ("id-A", "Realtek"), ("id-B", "Px7")]
    calls = []
    fired = threading.Event()

    def probe():
        item = sequence.pop(0) if sequence else ("id-B", "Px7")
        if item == "boom":
            raise OSError("COM says no")
        return item

    def on_change(name):
        calls.append(name)
        fired.set()

    watcher = devicewatch.DeviceWatcher(
        probe=probe,
        on_change=on_change,
        period_s=0.01,
    )
    watcher.start()
    assert fired.wait(timeout=2.0)
    watcher.stop()
    assert calls == ["Px7"]


class FakeConsole:
    def __init__(self, *, fail_speaker_on=None, fail_microphone_on=None):
        self.calls = []
        self.fail_speaker_on = fail_speaker_on or set()
        self.fail_microphone_on = fail_microphone_on or set()

    def set_speaker_enabled(self, enable, *, device=None):
        self.calls.append(("speaker", enable, device))
        if enable and device in self.fail_speaker_on:
            raise RuntimeError(f"cannot open {device}")

    def set_microphone_enabled(self, enable, *, device=None):
        self.calls.append(("microphone", enable, device))
        if enable and device in self.fail_microphone_on:
            raise RuntimeError(f"cannot open {device}")


class FakeSd:
    def __init__(self, *, bad=None, default=(1, 2)):
        self.bad = bad or set()
        self.default = type("Default", (), {"device": default})()

    def query_devices(self, index=None, kind=None):
        if index in self.bad:
            raise ValueError(f"no device {index}")
        return {"name": f"device-{index}"}


def test_output_follower_opens_resolved_device():
    console = FakeConsole()
    restarts = []
    follower = devicewatch.OutputFollower(
        console,
        resolve_output=lambda _name: 5,
        sd_module=FakeSd(),
        request_restart=restarts.append,
    )

    status = follower.swap_to("Speakers")

    assert console.calls == [("speaker", True, 5)]
    assert restarts == []
    assert status == {"name": "device-5", "following": True}


def test_output_follower_requests_restart_for_stale_snapshot():
    console = FakeConsole()
    restarts = []
    follower = devicewatch.OutputFollower(
        console,
        resolve_output=lambda _name: None,
        sd_module=FakeSd(),
        initial_idx=5,
        request_restart=restarts.append,
    )

    status = follower.swap_to("Brand New Device")

    assert console.calls == []
    assert len(restarts) == 1
    assert "Brand New Device" in restarts[0]
    assert status == {"name": "device-5", "following": True}


def test_output_follower_requests_restart_when_reopen_fails():
    console = FakeConsole(fail_speaker_on={5})
    restarts = []
    follower = devicewatch.OutputFollower(
        console,
        resolve_output=lambda _name: 5,
        sd_module=FakeSd(),
        initial_idx=4,
        request_restart=restarts.append,
    )

    status = follower.swap_to("Speakers")

    assert console.calls == [("speaker", True, 5)]
    assert len(restarts) == 1
    assert "stale snapshot" in restarts[0]
    assert status == {"name": None, "following": True}


def test_input_follower_reopens_livekit_then_switches_wake_stream():
    console = FakeConsole()
    wake_devices = []
    restarts = []
    follower = devicewatch.InputFollower(
        console,
        resolve_input=lambda _name: 7,
        sd_module=FakeSd(),
        switch_wake_input=wake_devices.append,
        request_restart=restarts.append,
    )

    status = follower.swap_to("Headset microphone")

    assert console.calls == [("microphone", True, 7)]
    assert wake_devices == [7]
    assert restarts == []
    assert status == {"name": "device-7", "following": True}


def test_input_follower_restart_path_does_not_move_wake_on_livekit_failure():
    console = FakeConsole(fail_microphone_on={7})
    wake_devices = []
    restarts = []
    follower = devicewatch.InputFollower(
        console,
        resolve_input=lambda _name: 7,
        sd_module=FakeSd(),
        switch_wake_input=wake_devices.append,
        request_restart=restarts.append,
    )

    status = follower.swap_to("Headset microphone")

    assert console.calls == [("microphone", True, 7)]
    assert wake_devices == []
    assert len(restarts) == 1
    assert status == {"name": None, "following": True}


def test_start_audio_follow_wires_both_directions_into_one_watcher():
    from worker import app, state, wakeword

    publisher = state.StatePublisher()
    wake_switch = wakeword.InputDeviceSwitch(1)
    watcher_instances = []
    console = FakeConsole()

    class FakeWatcher:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.started = False
            watcher_instances.append(self)

        def start(self):
            self.started = True

    watcher = app._start_audio_follow(
        {"wake_input_device": "follow", "tts_output_device": "follow"},
        publisher,
        wake_switch,
        console_factory=lambda: console,
        watcher_cls=FakeWatcher,
        input_probe=lambda: ("input-A", "Initial microphone"),
        output_probe=lambda: ("output-A", "Initial speakers"),
        resolve_input=lambda _name: 7,
        resolve_output=lambda _name: 8,
        sd_module=FakeSd(),
        request_restart=lambda _reason: None,
    )

    assert watcher is watcher_instances[0]
    assert watcher.started
    assert watcher.kwargs["period_s"] == 1.5
    assert watcher.kwargs["initial_ids"] == {
        "input": "input-A",
        "output": "output-A",
    }
    watcher.kwargs["on_input_change"]("New microphone")
    watcher.kwargs["on_output_change"]("New speakers")
    assert console.calls == [
        ("microphone", True, 7),
        ("speaker", True, 8),
    ]
    assert wake_switch.current()[0] == 7
    assert publisher.snapshot()["audio"] == {
        "input": {"name": "device-7", "following": True},
        "output": {"name": "device-8", "following": True},
    }


def test_start_audio_follow_skips_when_both_devices_are_pinned():
    from worker import app, state, wakeword

    publisher = state.StatePublisher()
    wake_switch = wakeword.InputDeviceSwitch(1)

    watcher = app._start_audio_follow(
        {"wake_input_device": "Intel", "tts_output_device": "Speakers"},
        publisher,
        wake_switch,
        console_factory=lambda: (_ for _ in ()).throw(AssertionError("unused")),
    )

    assert watcher is None


def test_start_audio_follow_dead_probe_marks_direction_not_following():
    from worker import app, wakeword

    published = []

    class Publisher:
        def set_audio_device(self, direction, status):
            published.append((direction, status))

    watcher = app._start_audio_follow(
        {"wake_input_device": "follow", "tts_output_device": "Speakers"},
        Publisher(),
        wakeword.InputDeviceSwitch(),
        input_probe=lambda: None,
    )

    assert watcher is None
    assert published == [
        ("input", {"name": None, "following": False}),
    ]


def test_start_audio_follow_console_failure_marks_active_direction_not_following():
    from worker import app, state, wakeword

    publisher = state.StatePublisher()
    wake_switch = wakeword.InputDeviceSwitch()

    watcher = app._start_audio_follow(
        {"wake_input_device": "follow", "tts_output_device": "Speakers"},
        publisher,
        wake_switch,
        input_probe=lambda: ("input-A", "Headset microphone"),
        console_factory=lambda: (_ for _ in ()).throw(ImportError("no console")),
    )

    assert watcher is None
    assert publisher.snapshot()["audio"]["input"] == {
        "name": "Headset microphone",
        "following": False,
    }


def test_watcher_initial_delay_defers_first_poll():
    polls = []

    def probe():
        polls.append(1)
        return "id-A", "Realtek"

    watcher = devicewatch.DeviceWatcher(
        probe=probe,
        on_change=lambda _name: None,
        period_s=0.01,
        initial_delay_s=0.2,
    )
    watcher.start()
    time.sleep(0.05)
    early = len(polls)
    time.sleep(0.25)
    watcher.stop()
    assert early == 0
    assert len(polls) > 0


def test_comtypes_logger_is_silenced():
    assert logging.getLogger("comtypes").level >= logging.WARNING


def test_worker_restart_uses_reserved_audio_exit_code(monkeypatch):
    from worker import app

    exits = []
    monkeypatch.setattr(app.os, "_exit", exits.append)

    app._restart_worker("test audio change")

    assert exits == [21]
