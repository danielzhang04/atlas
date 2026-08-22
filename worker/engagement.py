"""Explicit wake/sleep state for the Atlas voice session.

The local wake-word detector is the only transition into ``ENGAGED``. An explicit dismiss phrase
is the only transition back to ``ASLEEP``. Atlas does not silently expire a live conversation:
once Daniel wakes it, every finalized utterance belongs to that conversation until he dismisses it.
"""

ASLEEP = "ASLEEP"
ENGAGED = "ENGAGED"


class Engagement:
    def __init__(self):
        self._state = ASLEEP

    @property
    def state(self) -> str:
        return self._state

    def wake(self) -> None:
        """The local wake word fired: enter the continuous conversation."""
        self._state = ENGAGED

    def dismiss(self) -> None:
        """An explicit dismiss phrase fired: leave the conversation immediately."""
        self._state = ASLEEP
