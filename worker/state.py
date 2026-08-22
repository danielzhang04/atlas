"""Publish the in-process voice state and bounded transcript snapshot."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import logging
from typing import Callable
import uuid

__all__ = [
    "ASLEEP",
    "LISTENING",
    "SPEAKING",
    "STATE_FROM_AGENT",
    "THINKING",
    "StatePublisher",
]

logger = logging.getLogger("atlas.state")

ASLEEP = "ASLEEP"
LISTENING = "LISTENING"
THINKING = "THINKING"
SPEAKING = "SPEAKING"

SNAPSHOT_VERSION = 1
DEFAULT_RING = 50

STATE_FROM_AGENT = {
    "thinking": THINKING,
    "speaking": SPEAKING,
    "listening": LISTENING,
    "idle": LISTENING,
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class StatePublisher:
    """Own the voice state, transcript ring, and synchronous event stream."""

    def __init__(
        self,
        clock: Callable[[], datetime] = _utcnow,
        ring_size: int = DEFAULT_RING,
        voice: str | None = None,
    ) -> None:
        self._clock = clock
        self.voice = voice
        self._state = ASLEEP
        self._since = clock()
        self._session_id: str | None = None
        self._ring: deque[dict] = deque(maxlen=ring_size)
        self._output_device: dict | None = None
        self._audio_energy = 0.0
        self._subs: list[Callable] = []

    @property
    def state(self) -> str:
        return self._state

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def audio_energy(self) -> float:
        return self._audio_energy if self._state != ASLEEP else 0.0

    def start_session(self) -> str:
        self._session_id = str(uuid.uuid4())
        return self._session_id

    def set_state(self, value: str) -> None:
        if value == self._state:
            return
        self._state = value
        self._since = self._clock()
        self._emit(("state", value))

    def set_output_device(self, status: dict | None) -> None:
        self._output_device = status

    def set_audio_energy(self, value: float) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            self._audio_energy = 0.0
            return
        self._audio_energy = max(0.0, min(1.0, float(value)))

    def add_line(self, role: str, text: str) -> None:
        line = {"t": self._clock().isoformat(), "role": role, "text": text}
        self._ring.append(line)
        self._emit(("line", line))

    def subscribe(self, fn: Callable) -> None:
        self._subs.append(fn)

    def unsubscribe(self, fn: Callable) -> None:
        try:
            self._subs.remove(fn)
        except ValueError:
            pass

    def snapshot(self) -> dict:
        return {
            "version": SNAPSHOT_VERSION,
            "state": self._state,
            "since": self._since.isoformat(),
            "session_id": self._session_id,
            "voice": self.voice,
            "transcript": list(self._ring),
            "output_device": dict(self._output_device) if self._output_device else None,
            "audio_energy": round(self.audio_energy, 4),
        }

    def _emit(self, event: tuple) -> None:
        for fn in list(self._subs):
            try:
                fn(event)
            except Exception:
                logger.exception("atlas state subscriber raised; skipping")
