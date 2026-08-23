"""Track wake state and directed-interaction silence for the voice session."""
from __future__ import annotations

import time
from typing import Callable

__all__ = ["ASLEEP", "ENGAGED", "Engagement"]

ASLEEP = "ASLEEP"
ENGAGED = "ENGAGED"


class Engagement:
    def __init__(
        self,
        timeout_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.timeout_s = timeout_s
        self._clock = clock
        self._state = ASLEEP
        self._last_activity = 0.0

    @property
    def state(self) -> str:
        return self._state

    def wake(self) -> None:
        """Enter the engaged state and start the silence clock."""
        self._state = ENGAGED
        self._last_activity = self._clock()

    def interacted(self) -> None:
        """Refresh the silence clock for a directed interaction while engaged."""
        if self._state == ENGAGED:
            self._last_activity = self._clock()

    def dismiss(self) -> None:
        """Leave the engaged state immediately."""
        self._state = ASLEEP

    def tick(self) -> str:
        """Apply the silence timeout and return the current state."""
        if self._state == ENGAGED:
            elapsed = self._clock() - self._last_activity
            if elapsed > self.timeout_s:
                self._state = ASLEEP
        return self._state
