"""WakeGate — the ghost-wake debounce (2026-08-12).

The gate is the pure decision seam of worker/wakeword.py's listen() loop: one score per 80ms
frame in, "wake"/"spike"/None out. These tests pin the ghost-wake fix: single-frame threshold
crossings must NOT wake Atlas, sustained crossings must, and one phrase fires exactly one wake.
"""
import numpy as np

from worker import wakeword
from worker.wakeword import PATIENCE, REFRACTORY_S, THRESHOLD, WakeGate, normalized_audio_energy


def feed(gate, scores, t0=100.0, dt=0.08):
    """Feed one score per frame at the real 80ms cadence; return the event list."""
    return [gate.update(s, t0 + i * dt) for i, s in enumerate(scores)]


def test_single_frame_spike_is_suppressed_not_fired():
    gate = WakeGate(threshold=0.5, patience=3)
    events = feed(gate, [0.1, 0.92, 0.1, 0.1])
    assert "wake" not in events
    assert events[2] == "spike"          # reported on the frame the run ends
    assert gate.peak == 0.92 and gate.spike_frames == 1


def test_two_frame_spike_below_patience_is_suppressed():
    gate = WakeGate(threshold=0.5, patience=3)
    events = feed(gate, [0.6, 0.7, 0.2])
    assert "wake" not in events
    assert events[2] == "spike" and gate.spike_frames == 2


def test_sustained_run_fires_exactly_once_on_the_patience_frame():
    gate = WakeGate(threshold=0.5, patience=3)
    assert feed(gate, [0.6, 0.7]) == [None, None]
    assert gate.update(0.8, 100.16) == "wake"
    assert gate.peak == 0.8              # the value the fire-time log line reports
    # run continues past patience, then ends: no trailing spike after a fired run
    assert feed(gate, [0.9, 0.9, 0.2], t0=100.24) == [None, None, None]


def test_refractory_blocks_an_echo_run_then_recovers():
    gate = WakeGate(threshold=0.5, patience=2, refractory_s=3.0)
    assert feed(gate, [0.9, 0.9], t0=100.0) == [None, "wake"]
    # a second run 1s later (own-TTS echo territory): reaches patience inside refractory -> silent
    assert feed(gate, [0.9, 0.9, 0.1], t0=101.0) == [None, None, None]
    # a run after the window has passed fires again
    assert feed(gate, [0.9, 0.9], t0=104.5) == [None, "wake"]


def test_interrupted_run_does_not_accumulate_across_the_gap():
    gate = WakeGate(threshold=0.5, patience=3)
    events = feed(gate, [0.6, 0.6, 0.1, 0.6, 0.6, 0.1])
    assert "wake" not in events          # 2+2 non-consecutive frames never reach patience 3


def test_peak_resets_between_runs():
    gate = WakeGate(threshold=0.5, patience=3)
    feed(gate, [0.95, 0.1])              # first spike peaks at 0.95
    feed(gate, [0.55, 0.1], t0=200.0)    # second run must not inherit the old peak
    assert gate.peak == 0.55


def test_patience_one_matches_old_single_frame_behavior():
    gate = WakeGate(threshold=0.5, patience=1)
    assert feed(gate, [0.6]) == ["wake"]


def test_defaults_are_the_shipped_tuning():
    gate = WakeGate()
    assert (gate.threshold, gate.patience, gate.refractory_s) == (THRESHOLD, PATIENCE, REFRACTORY_S)


def test_audio_energy_is_bounded_perceptual_and_discards_frame_shape():
    assert normalized_audio_energy([0] * 1280) == 0.0
    quiet = normalized_audio_energy([100] * 1280)
    voice = normalized_audio_energy([4000, -4000] * 640)
    loud = normalized_audio_energy([32767, -32768] * 640)
    assert 0.0 <= quiet < voice < loud <= 1.0
    assert normalized_audio_energy([]) == 0.0
    assert normalized_audio_energy([float("nan")]) == 0.0


def test_resample_audio_frame_converts_hfp_8khz_to_model_rate():
    source_rate = 8000
    positions = np.arange(round(source_rate * 0.08)) / source_rate
    source = np.rint(np.sin(2.0 * np.pi * 1000.0 * positions) * 12000.0).astype(np.int16)

    resampled = wakeword.resample_audio_frame(source, source_rate)

    frequencies = np.fft.rfftfreq(resampled.size, d=1.0 / wakeword.SAMPLE_RATE)
    peak = frequencies[int(np.argmax(np.abs(np.fft.rfft(resampled))))]
    assert resampled.shape == (wakeword.FRAME_SAMPLES,)
    assert resampled.dtype == np.int16
    assert abs(peak - 1000.0) <= 12.5


def test_resample_audio_frame_rejects_invalid_rate_and_empty_input():
    assert not np.any(wakeword.resample_audio_frame([], 8000))
    assert not np.any(wakeword.resample_audio_frame([1, 2], None))
    assert not np.any(wakeword.resample_audio_frame([1, 2], float("nan")))


def test_audio_bands_are_log_spaced_bounded_and_smoothed():
    positions = np.arange(wakeword.FRAME_SAMPLES) / wakeword.SAMPLE_RATE
    tone = np.rint(np.sin(2.0 * np.pi * 1000.0 * positions) * 16000.0).astype(np.int16)

    attack = wakeword.normalized_audio_bands(tone, [0.0] * wakeword.BAND_COUNT)
    decay = wakeword.normalized_audio_bands(
        np.zeros(wakeword.FRAME_SAMPLES, dtype=np.int16),
        attack,
    )

    edges = np.geomspace(60.0, wakeword.SAMPLE_RATE / 2.0, wakeword.BAND_COUNT + 1)
    expected_band = int(np.searchsorted(edges, 1000.0, side="right") - 1)
    assert len(attack) == wakeword.BAND_COUNT
    assert all(0.0 <= value <= 1.0 for value in attack)
    assert int(np.argmax(attack)) == expected_band
    assert max(attack) > 0.2
    assert 0.0 < max(decay) < max(attack)


class FakeWakeModel:
    def __init__(self):
        self.frames = []

    def predict(self, frame):
        self.frames.append(frame.copy())
        return {"hey_test": 0.1}


class FakeInputStream:
    def __init__(self, options):
        self.options = options
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def read(self, count):
        frame = np.full((count, 1), 1200, dtype=np.int16)
        return frame, False


class FakeSoundDevice:
    def __init__(self):
        self.default = type("Default", (), {"device": (3, 9)})()
        self.rates = {3: 8000.0, 4: 48000.0}
        self.streams = []

    def query_devices(self, index, kind=None):
        return {
            "name": f"microphone-{index}",
            "default_samplerate": self.rates[index],
        }

    def InputStream(self, **options):
        stream = FakeInputStream(options)
        self.streams.append(stream)
        return stream


def test_listen_opens_current_default_at_native_rate():
    sd = FakeSoundDevice()
    model = FakeWakeModel()

    wakeword.listen(
        lambda: None,
        model_name="hey_test",
        device="follow",
        sd_module=sd,
        model_loader=lambda _name: (model, "hey_test"),
        clock=lambda: 100.0,
        max_frames=1,
    )

    assert len(sd.streams) == 1
    assert sd.streams[0].options == {
        "device": None,
        "samplerate": 8000.0,
        "channels": 1,
        "dtype": "int16",
        "blocksize": 640,
    }
    assert sd.streams[0].closed
    assert model.frames[0].shape == (wakeword.FRAME_SAMPLES,)


def test_listen_closes_and_reopens_on_input_switch_with_native_rates():
    sd = FakeSoundDevice()
    model = FakeWakeModel()
    switch = wakeword.InputDeviceSwitch(3)
    signals = []

    def on_signal(energy, bands):
        signals.append((energy, bands))
        if len(signals) == 1:
            switch.switch_to(4)

    wakeword.listen(
        lambda: None,
        model_name="hey_test",
        device="follow",
        on_signal=on_signal,
        device_switch=switch,
        sd_module=sd,
        model_loader=lambda _name: (model, "hey_test"),
        clock=lambda: 100.0,
        max_frames=2,
    )

    assert [stream.options["device"] for stream in sd.streams] == [3, 4]
    assert [stream.options["samplerate"] for stream in sd.streams] == [8000.0, 48000.0]
    assert [stream.options["blocksize"] for stream in sd.streams] == [640, 3840]
    assert all(stream.closed for stream in sd.streams)
    assert len(model.frames) == 2
    assert all(frame.shape == (wakeword.FRAME_SAMPLES,) for frame in model.frames)
    assert len(signals) == 2
    assert all(len(bands) == wakeword.BAND_COUNT for _energy, bands in signals)
