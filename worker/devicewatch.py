"""TTS output-follow watcher (design docs/specs/2026-07-21-atlas-output-follow-design.md).

Polls the Windows default OUTPUT endpoint via pycaw/COM every ~1.5s and fires a callback
with the new endpoint's FriendlyName when the endpoint ID changes. Polling (not
IMMNotificationClient callbacks) is deliberate: COM apartment threading + asyncio callback
plumbing is fragile, and a 1.5s cadence is imperceptible for a human plugging in headphones.

The watcher knows nothing about livekit or sounddevice — it produces NAMES; the swap
orchestration (Task 2) turns names into stream moves. decide() is the pure decision seam."""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger("atlas.devicewatch")

# pycaw drags comtypes in, and comtypes logs every COM Release at DEBUG — at our 1.5s poll
# cadence that floods pm2 logs (live finding 2026-07-22, it drowned the wake/swap lines).
logging.getLogger("comtypes").setLevel(logging.WARNING)


def decide(prev_id: str | None, current_id: str | None) -> str:
    """'baseline' on first sighting, 'swap' on an id CHANGE after baseline, else 'none'.

    A None current_id means the probe failed (COM hiccup, no endpoint) — never a change.
    The first real sighting is a baseline, not a swap: livekit already opened the output
    stream on the boot-time default, so the boot device is correct by construction."""
    if current_id is None:
        return "none"
    if prev_id is None:
        return "baseline"
    return "swap" if current_id != prev_id else "none"


def current_default_output():
    """(endpoint_id, friendly_name) of the Windows default render endpoint, or None.

    pycaw's GetSpeakers() returns the default eRender/eMultimedia endpoint — the exact
    device Windows moves when Bluetooth headphones connect. Endpoint IDs are stable and
    unique; FriendlyName matches PortAudio's device-name strings closely enough for the
    existing substring resolver. Any COM failure -> None (the watcher treats it as a
    failed poll, never a change).

    API note (verified against installed pycaw 20251023 on 2026-07-21): GetSpeakers() now
    returns a wrapped `AudioDevice` (with `.id`/`.FriendlyName`) directly, so calling the
    older `CreateDevice(raw_immdevice)` on it raises. We use the wrapper if present and fall
    back to CreateDevice for older pycaw that returns a raw IMMDevice — same return contract."""
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


class DeviceWatcher:
    """Daemon thread: poll `probe`, run decide(), fire `on_change(name)` on swaps.

    `probe` returns (endpoint_id, name) or None/raises; both count as a failed poll.
    COM must be initialized per-thread, so the thread body CoInitializes when comtypes is
    present (tests inject pure-python probes and never touch COM)."""

    def __init__(self, probe, on_change, period_s: float = 1.5, initial_delay_s: float = 0.0):
        self._probe = probe
        self._on_change = on_change
        self._period_s = period_s
        # Review finding #1 (2026-07-21): livekit's own set_speaker_enabled call happens at
        # console-audio-mode entry, within seconds of boot — the one window where a watcher
        # swap could race it into an uncaught stream error that kills the console thread.
        # Holding the first poll past that window closes the race; in steady state the
        # watcher is the sole caller and the follower's lock suffices.
        self._initial_delay_s = initial_delay_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="atlas-devicewatch", daemon=True)
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
            pass  # pure-python probes (tests) need no COM
        if self._initial_delay_s:
            self._stop.wait(self._initial_delay_s)
        prev_id: str | None = None
        while not self._stop.is_set():
            current = None
            try:
                current = self._probe()
            except Exception:
                logger.debug("device probe raised", exc_info=True)
            cur_id, cur_name = current if current else (None, None)
            action = decide(prev_id, cur_id)
            if action == "baseline":
                prev_id = cur_id
            elif action == "swap":
                prev_id = cur_id
                try:
                    self._on_change(cur_name)
                except Exception:
                    # The swap layer logs its own CRITICALs; this guard only keeps the
                    # watcher alive if it raised unexpectedly.
                    logger.exception("on_change callback raised")
            self._stop.wait(self._period_s)


class OutputFollower:
    """Turns a new default-device NAME into a live output-stream move (design 'Swap orchestration',
    REVISED 2026-07-22 after the first live failure).

    Live finding (2026-07-22 00:01/00:13 local logs): a Bluetooth disconnect reshuffles the real
    MME device table, so indices resolved from the boot-time PortAudio snapshot can fail to OPEN
    ("device ID out of range") even though pre-validation passed — pre-validation reads the SAME
    stale snapshot, so it cannot catch this. Worse, "reopen previous" can succeed against a
    registered-but-DISCONNECTED Bluetooth endpoint (Windows opens those fine), leaving Atlas
    speaking into the void with /state claiming success — the exact silent-wrong-device failure
    this whole feature exists to kill.

    The only in-process cure (Pa_Terminate/Pa_Reinitialize) kills the wake listener's InputStream,
    and wakeword.listen has NO retry — Atlas would be deaf until restart anyway. So the policy is:
    a swap that cannot cleanly open its resolved device requests a WORKER SELF-RESTART via the
    injected `request_restart` callback (pm2 revives us in seconds with a fresh, correct PortAudio
    snapshot; every pin re-resolves). Restart is honest and total; half-alive audio is neither."""

    def __init__(self, console, *, resolve_output, sd_module, lock=None,
                 initial_idx: int | None = None, request_restart=None):
        self._console = console
        self._resolve_output = resolve_output
        self._sd = sd_module
        self._lock = lock or threading.Lock()
        self._request_restart = request_restart or (lambda reason: None)
        # Seed with the boot device livekit opened (review finding #2) — still used for the
        # /state truth when a swap is refused pre-open.
        self._last_idx: int | None = initial_idx

    def _status(self, idx: int | None) -> dict:
        if idx is None:
            return {"configured": "follow", "resolved": None, "following": True}
        try:
            name = self._sd.query_devices(idx)["name"]
        except Exception:
            name = str(idx)
        return {"configured": "follow", "resolved": name, "following": True}

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
                # Device not in this process's (boot-time) PortAudio snapshot — either a
                # first-ever pairing or a reshuffled table. Only a fresh snapshot can map it,
                # and refreshing in-process kills the retry-less wake listener: restart.
                logger.critical(
                    "output follow: Windows default moved to %r but the boot PortAudio "
                    "snapshot has no matching device — requesting worker restart for a "
                    "fresh snapshot", name)
                self._request_restart(f"unresolvable output device {name!r}")
                return self._status(self._last_idx)
            if self._open(idx):
                self._last_idx = idx
                return self._status(idx)
            # Open failure with a resolved index = the snapshot is stale (live finding
            # 2026-07-22: BT disconnect reshuffles MME; "reopen previous" can land on a
            # disconnected endpoint and play into the void). Don't guess — restart.
            logger.critical(
                "output follow: device %d (%r) failed to open — stale device table; "
                "requesting worker restart for a fresh snapshot", idx, name)
            self._request_restart(f"stale snapshot opening device {idx} ({name!r})")
            return self._status(None)
