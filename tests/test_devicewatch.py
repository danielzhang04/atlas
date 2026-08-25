"""System-default audio device probes, polling, and stream followers."""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from types import SimpleNamespace

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


def test_watcher_treats_first_endpoint_after_missing_initial_probe_as_reopen():
    sequence = [None, ("input-B", "Headset microphone")]
    calls = []
    fired = threading.Event()

    def probe():
        return sequence.pop(0) if sequence else ("input-B", "Headset microphone")

    watcher = devicewatch.DeviceWatcher(
        input_probe=probe,
        on_input_change=lambda name: (calls.append(name), fired.set()),
        initial_ids={"input": None},
        period_s=0.01,
    )
    watcher.start()
    assert fired.wait(timeout=2.0)
    watcher.stop()
    assert calls == ["Headset microphone"]


def test_watcher_reopens_both_directions_after_both_initial_probes_are_missing():
    input_sequence = [None, ("input-B", "Headset microphone")]
    output_sequence = [None, ("output-B", "Headphones")]
    calls = []
    fired = threading.Event()

    def probe(sequence):
        return sequence.pop(0) if sequence else None

    def changed(direction, name):
        calls.append((direction, name))
        if len(calls) == 2:
            fired.set()

    watcher = devicewatch.DeviceWatcher(
        input_probe=lambda: probe(input_sequence),
        output_probe=lambda: probe(output_sequence),
        on_input_change=lambda name: changed("input", name),
        on_output_change=lambda name: changed("output", name),
        initial_ids={"input": None, "output": None},
        period_s=0.01,
    )
    watcher.start()
    assert fired.wait(timeout=2.0)
    watcher.stop()

    assert calls == [
        ("input", "Headset microphone"),
        ("output", "Headphones"),
    ]


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
    def __init__(self, *, bad=None, default=(1, 2), capture_error=None):
        self.bad = bad or set()
        self.default = type("Default", (), {"device": default})()
        self.capture_error = capture_error
        self.capture_calls = []

    def query_devices(self, index=None, kind=None):
        if index in self.bad:
            raise ValueError(f"no device {index}")
        return {"name": f"device-{index}"}

    def InputStream(self, **options):
        self.capture_calls.append(options)
        error = self.capture_error

        class Stream:
            def __enter__(self):
                if error is not None:
                    raise error
                return self

            def __exit__(self, *_args):
                return None

            def read(self, count):
                if error is not None:
                    raise error
                return bytes(count * 2), False

        return Stream()


def test_livekit_capture_rate_is_read_from_installed_console_module():
    module = SimpleNamespace(SAMPLE_RATE=24_000)

    assert devicewatch.livekit_capture_rate(module=module) == 24_000


def test_livekit_capture_rate_is_extracted_from_installed_console_code():
    from livekit.agents.cli import _legacy

    assert devicewatch.livekit_capture_rate(module=_legacy) == 24_000


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
    sd = FakeSd()
    wake_devices = []
    failures = []
    follower = devicewatch.InputFollower(
        console,
        resolve_input=lambda _name: 7,
        sd_module=sd,
        switch_wake_input=wake_devices.append,
        on_failure=failures.append,
        capture_rate=24_000,
    )

    status = follower.swap_to("Headset microphone")

    assert console.calls == [("microphone", True, 7)]
    assert wake_devices == [7]
    assert failures == []
    assert sd.capture_calls == [{
        "device": 7,
        "samplerate": 24_000,
        "channels": 1,
        "dtype": "int16",
        "blocksize": 240,
    }]
    assert status == {"name": "device-7", "following": True}


def test_input_follower_reports_livekit_reopen_failure_through_failure_callback():
    console = FakeConsole(fail_microphone_on={7})
    wake_devices = []
    failures = []
    follower = devicewatch.InputFollower(
        console,
        resolve_input=lambda _name: 7,
        sd_module=FakeSd(),
        switch_wake_input=wake_devices.append,
        on_failure=failures.append,
        capture_rate=24_000,
    )

    status = follower.swap_to("Headset microphone")

    assert console.calls == [("microphone", True, 7)]
    assert wake_devices == [7]
    assert failures == ["livekit input reopen failed"]
    assert status == {
        "name": "device-7",
        "following": False,
        "error": "livekit input reopen failed",
    }


def test_unsupported_livekit_rate_keeps_wake_native_and_leaves_livekit_unchanged():
    console = FakeConsole()
    sd = FakeSd(capture_error=RuntimeError("invalid sample rate"))
    wake_devices = []
    failures = []
    follower = devicewatch.InputFollower(
        console,
        resolve_input=lambda _name: 7,
        sd_module=sd,
        switch_wake_input=wake_devices.append,
        on_failure=failures.append,
        capture_rate=24_000,
    )

    status = follower.swap_to("HFP microphone")

    assert wake_devices == [7]
    assert console.calls == []
    assert failures == []
    assert status == {
        "name": "device-7",
        "following": False,
        "reason": "rate unsupported",
    }


def test_start_audio_follow_wires_both_directions_into_one_watcher():
    from worker import state, wakeword

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

    watcher = devicewatch.start_audio_follow(
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


def test_start_audio_follow_requires_and_wires_restart_callback():
    from inspect import Parameter, signature

    from worker import state, wakeword

    publisher = state.StatePublisher()
    captured = {}
    restarts = []

    class FakeInputFollower:
        def __init__(self, _console, *, on_failure, **_kwargs):
            captured["failure"] = on_failure

        def swap_to(self, _name):
            raise AssertionError("unused")

    class FakeWatcher:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            pass

    assert (
        signature(devicewatch.start_audio_follow).parameters["request_restart"].default
        is Parameter.empty
    )
    devicewatch.start_audio_follow(
        {"wake_input_device": "follow", "tts_output_device": "Speakers"},
        publisher,
        wakeword.InputDeviceSwitch(),
        request_restart=restarts.append,
        input_probe=lambda: ("input-A", "Initial microphone"),
        console_factory=FakeConsole,
        watcher_cls=FakeWatcher,
        input_follower_cls=FakeInputFollower,
        sd_module=FakeSd(),
    )

    captured["failure"]("follower failed")

    assert restarts == ["follower failed"]
    assert publisher.snapshot()["audio"]["input"] == {
        "name": "Initial microphone",
        "following": False,
    }


def test_start_audio_follow_skips_when_both_devices_are_pinned():
    from worker import state, wakeword

    publisher = state.StatePublisher()
    wake_switch = wakeword.InputDeviceSwitch(1)

    watcher = devicewatch.start_audio_follow(
        {"wake_input_device": "Intel", "tts_output_device": "Speakers"},
        publisher,
        wake_switch,
        request_restart=lambda _reason: None,
        console_factory=lambda: (_ for _ in ()).throw(AssertionError("unused")),
    )

    assert watcher is None


def test_start_audio_follow_keeps_polling_direction_with_missing_initial_probe():
    from worker import wakeword

    published = []
    watcher_instances = []

    class Publisher:
        def set_audio_device(self, direction, status):
            published.append((direction, status))

        def snapshot(self):
            return {"audio": {"input": {"name": "boot microphone"}}}

    class FakeWatcher:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.started = False
            watcher_instances.append(self)

        def start(self):
            self.started = True

    watcher = devicewatch.start_audio_follow(
        {"wake_input_device": "follow", "tts_output_device": "Speakers"},
        Publisher(),
        wakeword.InputDeviceSwitch(),
        request_restart=lambda _reason: None,
        input_probe=lambda: None,
        console_factory=FakeConsole,
        watcher_cls=FakeWatcher,
        resolve_input=lambda _name: 7,
        sd_module=FakeSd(),
    )

    assert watcher is watcher_instances[0]
    assert watcher.started
    assert watcher.kwargs["initial_ids"] == {"input": None}
    assert published == [("input", {"name": "boot microphone", "following": False})]
    watcher.kwargs["on_input_change"]("Headset microphone")
    assert published[-1] == (
        "input",
        {"name": "device-7", "following": True},
    )


def test_start_audio_follow_console_failure_marks_active_direction_not_following():
    from worker import state, wakeword

    publisher = state.StatePublisher()
    wake_switch = wakeword.InputDeviceSwitch()

    watcher = devicewatch.start_audio_follow(
        {"wake_input_device": "follow", "tts_output_device": "Speakers"},
        publisher,
        wake_switch,
        request_restart=lambda _reason: None,
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


def test_worker_restart_requests_graceful_shutdown_with_reserved_exit_code(monkeypatch):
    from worker import app

    shutdowns = []
    monkeypatch.setattr(app, "_worker_exit_code", 0)

    app._restart_worker("test audio change", shutdowns.append)

    assert app._worker_exit_code == 21
    assert shutdowns == ["test audio change"]


def test_audio_restart_coalescer_collapses_events_within_two_seconds():
    callbacks = []
    restarts = []

    class Handle:
        def __init__(self, callback):
            self.callback = callback
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

    class Loop:
        def call_soon_threadsafe(self, callback, *args):
            callback(*args)

        def call_later(self, delay, callback):
            handle = Handle(callback)
            callbacks.append((delay, handle))
            return handle

    coalescer = devicewatch.AudioRestartCoalescer(
        restarts.append,
        loop=Loop(),
    )

    coalescer.request("input changed")
    coalescer.request("output changed")
    assert callbacks[0][1].cancelled
    assert callbacks[0][0] == 2.0
    assert callbacks[1][0] == 2.0
    callbacks[1][1].callback()

    assert restarts == ["input changed; output changed"]


def test_audio_failure_publishes_error_before_requesting_restart():
    events = []

    class Publisher:
        def snapshot(self):
            return {"audio": {"input": {"name": "Headset microphone"}}}

        def set_audio_device(self, direction, status):
            events.append(("publish", direction, status))

    callback = devicewatch.audio_failure_callback(
        Publisher(),
        "input",
        lambda reason: events.append(("restart", reason)),
    )

    callback("wake input unavailable")

    assert events == [
        (
            "publish",
            "input",
            {
                "name": "Headset microphone",
                "following": False,
                "error": "wake input unavailable",
            },
        ),
        ("restart", "wake input unavailable"),
    ]


def test_audio_restart_preserves_jobs_while_normal_worker_shutdown_cancels_them(
    monkeypatch,
):
    from worker import app

    monkeypatch.setattr(app, "_worker_exit_code", app.RESTART_EXIT_CODE)
    assert not app._should_cancel_active_jobs(False)

    monkeypatch.setattr(app, "_worker_exit_code", 0)
    assert app._should_cancel_active_jobs(False)
    assert not app._should_cancel_active_jobs(True)


def test_audio_restart_flushes_job_store_before_stopping_state_server():
    from worker import app

    events = []

    class Store:
        def close(self):
            events.append("store flushed")

    class Server:
        async def stop(self):
            events.append("state server stopped")

    asyncio.run(app._flush_store_and_stop_state_server(Store(), Server()))

    assert events == ["store flushed", "state server stopped"]
