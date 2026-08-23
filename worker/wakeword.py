"""Local wake-word listener (openwakeword 0.6.0; custom-trained "hey atlas", pretrained
"hey jarvis" as fallback).

Always-on, on-device: it reads the mic in 80 ms / 1280-sample frames at 16 kHz and never
sends audio anywhere — the only thing that leaves this module is the `on_wake()` call when
the wake score crosses threshold. Audio only leaves the PC AFTER wake, via the Deepgram STT
stream that app.py opens on the engagement transition (spec §2 Listening decision).

openwakeword facts (installed 0.6.0, verified against site-packages):
- `Model(wakeword_models=[<arg>], inference_framework="onnx")`. tflite_runtime is NOT installed
  in atlas/.venv; onnxruntime is — so onnx is the working framework.
- Each entry in `wakeword_models` is resolved per-entry (model.py L89-100): if `os.path.exists(entry)`
  it is loaded AS A FILE and keyed by its STEM `os.path.splitext(os.path.basename(entry))[0]`
  (model.py L92); otherwise it is treated as a pretrained NAME, matched via `name.replace(" ", "_")`
  against the shipped catalog and keyed by that bare name (model.py L95-100).
- So a custom-trained model → pass the full path `config/hey_atlas.onnx`, predict-key = `hey_atlas`.
  A pretrained model → pass the bare name `hey_jarvis`, predict-key = `hey_jarvis`. Because we name
  the custom file `<wake_model>.onnx`, the stem == the configured name in both cases; `load_model()`
  returns the exact key anyway so a rename can never silently break the score lookup in `listen()`.
- `model.predict(frame)` -> dict keyed as above for single-output wake models (model.py L313-314),
  e.g. {"hey_atlas": 0.91}. Trigger at > 0.5.
- Feature models (melspectrogram.onnx + embedding_model.onnx) are shared by ALL wake models and
  loaded by the preprocessor from the default cache (utils.py L70-73). A path-loaded custom model
  needs ONLY those two — no pretrained download. Pretrained names additionally need `<name>_v0.1.onnx`.
  `ensure_models()` fetches whatever is missing via openwakeword.utils.download_models (which always
  grabs the feature models, and skips any name it can't match in the catalog — utils.py L662-668).
"""
import logging
import math
import os
import threading
from pathlib import Path
from typing import Callable

import numpy as np

logger = logging.getLogger("atlas.wakeword")

ATLAS = Path(__file__).resolve().parents[1]  # same root convention as worker/app.py

FRAME_SAMPLES = 1280   # 80 ms @ 16 kHz — openwakeword's expected chunk
SAMPLE_RATE = 16000
THRESHOLD = 0.5
PATIENCE = 3           # consecutive frames above threshold before a wake fires (240 ms @ 80 ms/frame)
REFRACTORY_S = 3.0     # one wake per phrase, not one per 80ms frame while scores stay high
BAND_COUNT = 24
BAND_ATTACK = 0.45
BAND_DECAY = 0.18

# Set by app.py's shutdown callback so the wake thread's error path stays quiet on Ctrl+C.
# A mic/model failure MID-RUN still logs CRITICAL (Atlas going silently DEAF is the real
# incident); only teardown-time stream teardown is suppressed.
shutting_down = threading.Event()


def normalized_audio_energy(samples) -> float:
    """Map one int16 microphone frame to a perceptual 0..1 loudness scalar.

    Only the returned scalar leaves this function. The frame is neither copied nor retained.
    -55 dBFS is treated as silence and -10 dBFS as full visual energy.
    """
    try:
        count = len(samples)
        if count < 1:
            return 0.0
        mean_square = sum(float(sample) * float(sample) for sample in samples) / count
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(mean_square) or mean_square <= 0:
        return 0.0
    rms = math.sqrt(mean_square) / 32768.0
    dbfs = 20.0 * math.log10(max(rms, 1e-9))
    return round(max(0.0, min(1.0, (dbfs + 55.0) / 45.0)), 4)


def resample_audio_frame(samples, source_rate: float) -> np.ndarray:
    """Linearly resample one native-rate 80 ms frame to 1280 int16 samples."""
    values = np.asarray(samples)
    if values.ndim > 1:
        values = values[:, 0]
    values = values.reshape(-1)
    if values.size < 1 or not math.isfinite(float(source_rate)) or source_rate <= 0:
        return np.zeros(FRAME_SAMPLES, dtype=np.int16)
    if values.size == FRAME_SAMPLES and abs(float(source_rate) - SAMPLE_RATE) < 0.5:
        return values.astype(np.int16, copy=False)
    source_positions = np.linspace(0.0, 1.0, num=values.size, endpoint=False)
    target_positions = np.linspace(0.0, 1.0, num=FRAME_SAMPLES, endpoint=False)
    resampled = np.interp(target_positions, source_positions, values.astype(np.float32))
    return np.clip(np.rint(resampled), -32768, 32767).astype(np.int16)


def normalized_audio_bands(samples, previous=None) -> list[float]:
    """Return 24 smoothed log-spaced FFT band amplitudes bounded to 0..1."""
    values = np.asarray(samples)
    if values.ndim > 1:
        values = values[:, 0]
    values = values.reshape(-1).astype(np.float32)
    if values.size < 2 or not np.all(np.isfinite(values)):
        raw = np.zeros(BAND_COUNT, dtype=np.float32)
    else:
        values /= 32768.0
        window = np.hanning(values.size).astype(np.float32)
        scale = max(float(window.sum()) / 2.0, 1.0)
        magnitudes = np.abs(np.fft.rfft(values * window)) / scale
        frequencies = np.fft.rfftfreq(values.size, d=1.0 / SAMPLE_RATE)
        edges = np.geomspace(60.0, SAMPLE_RATE / 2.0, BAND_COUNT + 1)
        raw = np.zeros(BAND_COUNT, dtype=np.float32)
        for index in range(BAND_COUNT):
            if index == BAND_COUNT - 1:
                mask = (frequencies >= edges[index]) & (frequencies <= edges[index + 1])
            else:
                mask = (frequencies >= edges[index]) & (frequencies < edges[index + 1])
            amplitude = float(np.max(magnitudes[mask])) if np.any(mask) else 0.0
            dbfs = 20.0 * math.log10(max(amplitude, 1e-9))
            raw[index] = max(0.0, min(1.0, (dbfs + 70.0) / 60.0))
    try:
        prior = np.asarray(previous, dtype=np.float32)
    except (TypeError, ValueError):
        prior = np.zeros(BAND_COUNT, dtype=np.float32)
    if prior.shape != (BAND_COUNT,) or not np.all(np.isfinite(prior)):
        prior = np.zeros(BAND_COUNT, dtype=np.float32)
    coefficients = np.where(raw > prior, BAND_ATTACK, BAND_DECAY)
    smoothed = prior + coefficients * (raw - prior)
    return [round(float(value), 4) for value in np.clip(smoothed, 0.0, 1.0)]


def _resolve_model(model_name: str) -> tuple[str, str]:
    """Map a configured wake_model to (openwakeword-arg, predict-dict-key).
    Custom-trained models live at <ATLAS>/config/<name>.onnx and load by full path (keyed by
    the file stem); anything else is passed through as a pretrained name (keyed by that name)."""
    custom = ATLAS / "config" / f"{model_name}.onnx"
    if custom.exists():
        return str(custom), custom.stem
    return model_name, model_name


def ensure_models(model_name: str) -> None:
    """Idempotently fetch the onnx models the given wake model needs into the default cache.
    A custom `config/<name>.onnx` needs only the shared feature models; a pretrained name also
    needs `<name>_v0.1.onnx`."""
    import openwakeword
    models_dir = os.path.join(os.path.dirname(openwakeword.__file__), "resources", "models")
    needed = ["melspectrogram.onnx", "embedding_model.onnx"]
    if not (ATLAS / "config" / f"{model_name}.onnx").exists():
        needed.append(f"{model_name}_v0.1.onnx")  # pretrained model file lives in the cache too
    if all(os.path.exists(os.path.join(models_dir, f)) for f in needed):
        return
    from openwakeword.utils import download_models
    download_models([model_name])  # always fetches feature models; skips names not in the catalog


def load_model(model_name: str):
    """Build an openwakeword Model for a single wake word (onnx framework) and return
    (model, predict_key). `predict_key` is the key `model.predict()` uses for this model, which
    `listen()` must look up — see the module docstring for how it is derived per model kind."""
    ensure_models(model_name)
    from openwakeword.model import Model
    model_arg, predict_key = _resolve_model(model_name)
    model = Model(wakeword_models=[model_arg], inference_framework="onnx")
    return model, predict_key


class WakeGate:
    """Debounce for the wake score stream (2026-08-12 ghost-wake fix).

    The old trigger fired on ANY single 80 ms frame crossing threshold — one glitchy frame
    (mic buffer overflow under CPU starvation, an ambient-audio spike) woke Atlas, and the desk
    heard "Hey boss" / auto-sleep cycles with nobody speaking. A real "hey atlas" utterance holds
    the score above threshold for many consecutive frames (that fact is why the refractory window
    existed at all), so requiring `patience` consecutive frames kills one-frame spikes without
    delaying a real wake by more than patience*80 ms.

    Pure state machine, no audio imports: feed one score per frame to update() and it returns
    "wake"  — patience reached outside the refractory window; fire on_wake now
    "spike" — a run above threshold ended BEFORE reaching patience (the ghost-wake signature;
              logged so false-trigger pressure stays visible in worker diagnostics)
    None    — nothing to act on this frame
    `peak` and `run` expose the just-ended/just-fired run's max score and length for logging."""

    def __init__(self, threshold: float = THRESHOLD, patience: int = PATIENCE,
                 refractory_s: float = REFRACTORY_S) -> None:
        self.threshold = threshold
        self.patience = max(1, int(patience))
        self.refractory_s = refractory_s
        self.peak = 0.0        # max score of the current/just-reported run
        self.run = 0           # consecutive frames above threshold in the current run
        self.spike_frames = 0  # length of the last run that ended below patience (for logging)
        self._last_trigger = None  # monotonic time of the last fired wake

    def update(self, score: float, now: float):
        if score > self.threshold:
            self.peak = score if self.run == 0 else max(self.peak, score)
            self.run += 1
            if self.run == self.patience and (
                    self._last_trigger is None or now - self._last_trigger > self.refractory_s):
                self._last_trigger = now
                return "wake"
            return None
        ended_early = 0 < self.run < self.patience
        if ended_early:
            self.spike_frames = self.run   # length of the run that just ended, for the caller's log
        self.run = 0
        # peak intentionally survives until the next run starts so the caller can log it.
        return "spike" if ended_early else None


class InputDeviceSwitch:
    """Thread-safe handoff that asks the wake loop to reopen on a new device index."""

    def __init__(self, device: int | None = None) -> None:
        self._device = device
        self._generation = 0
        self._lock = threading.Lock()

    def current(self) -> tuple[int | None, int]:
        with self._lock:
            return self._device, self._generation

    def switch_to(self, device: int | None) -> None:
        with self._lock:
            self._device = device
            self._generation += 1


def resolve_input_device(substring: str | None, devices=None):
    """Index of the first input device whose name contains substring (case-insensitive).
    None/empty substring or no match -> None (system default). Pinning matters: Windows
    drifts the default input (e.g. to a Bluetooth hands-free path whose audio is too
    degraded/stuttery to score — 2026-07-20 desk finding), while a named wired mic is stable."""
    if not substring or substring.casefold() == "follow":
        return None
    if devices is None:
        import sounddevice as sd
        devices = sd.query_devices()
    for i, d in enumerate(devices):
        if d["max_input_channels"] > 0 and substring.lower() in d["name"].lower():
            return i
    logger.warning("wake input device %r not found — falling back to system default", substring)
    return None


def resolve_output_device(substring: str | None, devices=None):
    """Index of the first OUTPUT device whose name contains substring (case-insensitive).
    None/empty substring or no match -> None (system default). This is the speaker analogue of
    resolve_input_device: the wake INPUT is pinned by name because the Windows default drifts to a
    Bluetooth hands-free path; the TTS OUTPUT has the SAME drift (default hops to an AirPods HFP
    sink that is inaudible) and needs the same explicit pin, so Atlas speaks to the speaker Daniel
    is actually on (2026-07-21 finding: TTS was silent on the main speaker).

    First-match note: on Windows sounddevice enumerates the SAME physical speaker once per host API
    (MME, WASAPI, DirectSound...), so one substring can match several rows; we return the first,
    normally the MME copy (host-API 0). That is fine for playback — pass a more specific substring
    if a particular host API is required."""
    if not substring:
        return None
    if devices is None:
        import sounddevice as sd
        devices = sd.query_devices()
    for i, d in enumerate(devices):
        if d["max_output_channels"] > 0 and substring.lower() in d["name"].lower():
            return i
    logger.warning("tts output device %r not found — falling back to system default", substring)
    return None


def listen(
    on_wake: Callable[[], None],
    model_name: str = "hey_jarvis",
    device: str | None = None,
    threshold: float = THRESHOLD,
    patience: int = PATIENCE,
    on_energy: Callable[[float], None] | None = None,
    on_signal: Callable[[float, list[float]], None] | None = None,
    device_switch: InputDeviceSwitch | None = None,
    sd_module=None,
    model_loader=load_model,
    clock=None,
    max_frames: int | None = None,
) -> None:
    """Read native-rate 80 ms frames, resample to 16 kHz, and score wake audio.

    Follow mode starts on the current system default. ``device_switch`` changes cause the
    active stream to close after at most one frame and reopen at the new device's native rate.
    """
    try:
        if sd_module is None:
            import sounddevice as sd
        else:
            sd = sd_module
        if clock is None:
            import time as _time

            clock = _time.monotonic
        model, predict_key = model_loader(model_name)
        initial_device = resolve_input_device(device)
        switch = device_switch or InputDeviceSwitch(initial_device)
        gate = WakeGate(threshold=threshold, patience=patience)
        smoothed_bands = [0.0] * BAND_COUNT
        processed = 0
        while True:
            selected_device, generation = switch.current()
            query_device = selected_device
            if query_device is None:
                query_device = sd.default.device[0]
            device_info = sd.query_devices(query_device, kind="input")
            native_rate = float(device_info["default_samplerate"])
            native_samples = max(1, round(native_rate * FRAME_SAMPLES / SAMPLE_RATE))
            logger.info(
                "wake listener on input device: %s at %d Hz",
                device_info.get("name", "system default"),
                round(native_rate),
            )
            with sd.InputStream(
                device=selected_device,
                samplerate=native_rate,
                channels=1,
                dtype="int16",
                blocksize=native_samples,
            ) as stream:
                while switch.current()[1] == generation:
                    frame, _ = stream.read(native_samples)
                    model_frame = resample_audio_frame(frame, native_rate)
                    energy = normalized_audio_energy(model_frame)
                    smoothed_bands = normalized_audio_bands(model_frame, smoothed_bands)
                    if on_energy is not None:
                        try:
                            on_energy(energy)
                        except Exception:
                            logger.exception("audio-energy observer failed; disabling visual signal")
                            on_energy = None
                    if on_signal is not None:
                        try:
                            on_signal(energy, smoothed_bands)
                        except Exception:
                            logger.exception("audio-signal observer failed; disabling visual signal")
                            on_signal = None
                    scores = model.predict(model_frame)
                    event = gate.update(scores.get(predict_key, 0.0), clock())
                    if event == "wake":
                        logger.info(
                            "wake fired (peak score %.2f over %d frames)",
                            gate.peak,
                            gate.run,
                        )
                        on_wake()
                    elif event == "spike":
                        logger.info(
                            "wake spike suppressed (peak score %.2f, %d frame(s) < patience %d)",
                            gate.peak,
                            gate.spike_frames,
                            gate.patience,
                        )
                    processed += 1
                    if max_frames is not None and processed >= max_frames:
                        return
    except Exception:
        if shutting_down.is_set():
            logger.info("wake listener stopped during shutdown")
            return
        logger.critical(
            "wake-word listener died — Atlas is DEAF until the worker restarts "
            "(likely mic device conflict or model load failure)",
            exc_info=True,
        )
