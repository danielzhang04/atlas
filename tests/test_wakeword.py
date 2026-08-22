"""WakeGate — the ghost-wake debounce (2026-08-12).

The gate is the pure decision seam of worker/wakeword.py's listen() loop: one score per 80ms
frame in, "wake"/"spike"/None out. These tests pin the ghost-wake fix: single-frame threshold
crossings must NOT wake Atlas, sustained crossings must, and one phrase fires exactly one wake.
"""
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
