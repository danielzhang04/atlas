"""Follow the Windows default capture and render endpoints.

One polling thread watches both endpoints every 1.5 seconds. It emits friendly names;
the followers below turn those names into LiveKit console stream reopens. Polling is
deliberate because COM apartment threading around notification callbacks is fragile.
"""
from __future__ import annotations

import ast
import inspect
import logging
import threading
import textwrap

logger = logging.getLogger("atlas.devicewatch")

logging.getLogger("comtypes").setLevel(logging.WARNING)


def decide(prev_id: str | None, current_id: str | None) -> str:
    """Classify a probe as a baseline, endpoint swap, or no action."""
    if current_id is None:
        return "none"
    if prev_id is None:
        return "baseline"
    return "swap" if current_id != prev_id else "none"


def current_default_output():
    """Return the Windows default render endpoint id and friendly name."""
    try:
        from pycaw.utils import AudioUtilities

        device = AudioUtilities.GetSpeakers()
        wrapped = device if hasattr(device, "id") else AudioUtilities.CreateDevice(device)
        if wrapped is None or not wrapped.id:
            return None
        return wrapped.id, (wrapped.FriendlyName or "")
    except Exception:
        logger.debug("default-output probe failed", exc_info=True)
        return None


def current_default_input():
    """Return the Windows eCapture/eConsole endpoint id and friendly name."""
    try:
        from pycaw.constants import EDataFlow, ERole
        from pycaw.utils import AudioUtilities

        enumerator = AudioUtilities.GetDeviceEnumerator()
        device = enumerator.GetDefaultAudioEndpoint(
            EDataFlow.eCapture.value,
            ERole.eConsole.value,
        )
        wrapped = device if hasattr(device, "id") else AudioUtilities.CreateDevice(device)
        if wrapped is None or not wrapped.id:
            return None
        return wrapped.id, (wrapped.FriendlyName or "")
    except Exception:
        logger.debug("default-input probe failed", exc_info=True)
        return None


class DeviceWatcher:
    """Daemon thread that can poll input and output in one COM apartment.

    The legacy ``probe``/``on_change`` pair remains as a generic test seam. Production
    supplies input and output probes plus their independent callbacks.
    """

    def __init__(
        self,
        probe=None,
        on_change=None,
        period_s: float = 1.5,
        initial_delay_s: float = 0.0,
        *,
        input_probe=None,
        output_probe=None,
        on_input_change=None,
        on_output_change=None,
        initial_ids: dict[str, str | None] | None = None,
    ) -> None:
        self._channels = []
        if probe is not None:
            self._channels.append(("device", probe, on_change))
        if input_probe is not None:
            self._channels.append(("input", input_probe, on_input_change))
        if output_probe is not None:
            self._channels.append(("output", output_probe, on_output_change))
        if not self._channels:
            raise ValueError("DeviceWatcher needs at least one probe")
        self._period_s = period_s
        self._initial_delay_s = initial_delay_s
        self._initial_ids = dict(initial_ids or {})
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run,
            name="atlas-devicewatch",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def _run(self) -> None:
        try:
            import comtypes

            comtypes.CoInitialize()
        except Exception:
            pass
        if self._initial_delay_s:
            self._stop.wait(self._initial_delay_s)
        previous = {
            name: self._initial_ids.get(name)
            for name, _probe, _callback in self._channels
        }
        initialized = {
            name: name in self._initial_ids
            for name, _probe, _callback in self._channels
        }
        while not self._stop.is_set():
            for channel, probe, callback in self._channels:
                current = None
                try:
                    current = probe()
                except Exception:
                    logger.debug("default-%s probe raised", channel, exc_info=True)
                current_id, current_name = current if current else (None, None)
                action = decide(previous[channel], current_id)
                first_reopen = (
                    initialized[channel]
                    and previous[channel] is None
                    and current_id is not None
                )
                if first_reopen:
                    action = "swap"
                if action == "baseline":
                    previous[channel] = current_id
                    initialized[channel] = True
                elif action == "swap":
                    previous[channel] = current_id
                    initialized[channel] = True
                    try:
                        callback(current_name)
                    except Exception:
                        logger.exception("%s-device callback raised", channel)
            self._stop.wait(self._period_s)


def livekit_capture_rate(module=None) -> int:
    """Read the console capture rate from the installed LiveKit implementation."""
    if module is None:
        from livekit.agents.cli import _legacy as module

    for name in ("CAPTURE_SAMPLE_RATE", "SAMPLE_RATE"):
        value = getattr(module, name, None)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return int(value)

    console = getattr(module, "AgentsConsole", None)
    method = getattr(console, "set_microphone_enabled", None)
    if method is None:
        raise RuntimeError("LiveKit console capture rate is unavailable")
    source = textwrap.dedent(inspect.getsource(method))
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "attr", None) != "InputStream":
            continue
        for keyword in node.keywords:
            if keyword.arg != "samplerate":
                continue
            try:
                value = ast.literal_eval(keyword.value)
            except (ValueError, TypeError):
                value = None
                if isinstance(keyword.value, ast.Name):
                    value = getattr(module, keyword.value.id, None)
                    if value is None:
                        value = getattr(method, "__globals__", {}).get(keyword.value.id)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                return int(value)
    raise RuntimeError("LiveKit console capture rate is unavailable")


class OutputFollower:
    """Move the LiveKit speaker stream, restarting on a stale PortAudio table."""

    def __init__(
        self,
        console,
        *,
        resolve_output,
        sd_module,
        lock=None,
        initial_idx: int | None = None,
        request_restart=None,
    ) -> None:
        self._console = console
        self._resolve_output = resolve_output
        self._sd = sd_module
        self._lock = lock or threading.Lock()
        self._request_restart = request_restart or (lambda reason: None)
        self._last_idx = initial_idx

    def _status(self, idx: int | None) -> dict:
        if idx is None:
            return {"name": None, "following": True}
        try:
            name = self._sd.query_devices(idx)["name"]
        except Exception:
            name = str(idx)
        return {"name": name, "following": True}

    def _open(self, idx: int) -> bool:
        try:
            self._console.set_speaker_enabled(True, device=idx)
            return True
        except Exception:
            logger.critical("output swap: opening device %d failed", idx, exc_info=True)
            return False

    def swap_to(self, name: str) -> dict:
        with self._lock:
            idx = self._resolve_output(name)
            if idx is None:
                logger.critical(
                    "output follow: Windows default moved to %r but the boot PortAudio "
                    "snapshot has no matching device; requesting worker restart",
                    name,
                )
                self._request_restart(f"unresolvable output device {name!r}")
                return self._status(self._last_idx)
            if self._open(idx):
                self._last_idx = idx
                return self._status(idx)
            logger.critical(
                "output follow: device %d (%r) failed to open; requesting worker restart",
                idx,
                name,
            )
            self._request_restart(f"stale snapshot opening output device {idx} ({name!r})")
            return self._status(None)


class InputFollower:
    """Reopen LiveKit and wake-word streams on the new default capture device."""

    def __init__(
        self,
        console,
        *,
        resolve_input,
        sd_module,
        switch_wake_input,
        lock=None,
        initial_idx: int | None = None,
        on_failure=None,
        capture_rate: int | None = None,
    ) -> None:
        self._console = console
        self._resolve_input = resolve_input
        self._sd = sd_module
        self._switch_wake_input = switch_wake_input
        self._lock = lock or threading.Lock()
        self._last_idx = initial_idx
        self._on_failure = on_failure or (lambda reason: None)
        self._capture_rate = capture_rate or livekit_capture_rate()

    def _status(self, idx: int | None) -> dict:
        if idx is None:
            return {"name": None, "following": True}
        try:
            name = self._sd.query_devices(idx)["name"]
        except Exception:
            name = str(idx)
        return {"name": name, "following": True}

    def _failed_status(self, idx: int | None, field: str, reason: str) -> dict:
        status = self._status(idx)
        status["following"] = False
        status[field] = reason
        return status

    def _supports_livekit_capture(self, idx: int) -> bool:
        blocksize = max(1, self._capture_rate // 100)
        try:
            with self._sd.InputStream(
                device=idx,
                samplerate=self._capture_rate,
                channels=1,
                dtype="int16",
                blocksize=blocksize,
            ) as stream:
                stream.read(blocksize)
            return True
        except Exception:
            logger.warning(
                "input follow: device %d cannot capture at LiveKit's %d Hz rate",
                idx,
                self._capture_rate,
            )
            return False

    def swap_to(self, name: str) -> dict:
        with self._lock:
            idx = self._resolve_input(name)
            if idx is None:
                logger.critical(
                    "input follow: Windows default moved to %r but the boot PortAudio "
                    "snapshot has no matching device",
                    name,
                )
                reason = "input device unavailable"
                self._on_failure(reason)
                return self._failed_status(self._last_idx, "error", reason)
            if not self._supports_livekit_capture(idx):
                self._switch_wake_input(idx)
                return self._failed_status(idx, "reason", "rate unsupported")
            try:
                self._console.set_microphone_enabled(True, device=idx)
            except Exception:
                logger.critical(
                    "input follow: LiveKit failed to reopen device %d (%r)",
                    idx,
                    name,
                    exc_info=True,
                )
                self._switch_wake_input(idx)
                reason = "livekit input reopen failed"
                self._on_failure(reason)
                return self._failed_status(idx, "error", reason)
            self._switch_wake_input(idx)
            self._last_idx = idx
            return self._status(idx)
