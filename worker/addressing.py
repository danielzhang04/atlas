"""Decide whether a normalized utterance is directed at Atlas."""
from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Callable

from worker import router

__all__ = ["Addressing"]


def _vocab_forms(raw: str) -> set[str]:
    joined = router.normalize(raw)
    spoken = router.normalize(raw.replace("-", " ").replace("_", " "))
    return {value for value in (joined, spoken) if value}


class Addressing:
    def __init__(
        self,
        window_s: float,
        vocab: Iterable[str],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window = float(window_s)
        self._clock = clock
        self._last: float | None = None
        self._tokens: set[str] = set()
        self._phrases: list[str] = []
        for raw in vocab:
            for form in _vocab_forms(raw):
                if " " in form:
                    self._phrases.append(form)
                else:
                    self._tokens.add(form)

    def mark_activity(self) -> None:
        self._last = self._clock()

    def is_addressed(self, normalized_utterance: str) -> bool:
        if self._last is not None:
            elapsed = self._clock() - self._last
            if elapsed <= self._window:
                return True
        tokens = set(normalized_utterance.split())
        if "atlas" in tokens or tokens & self._tokens:
            return True
        padded = f" {normalized_utterance} "
        return any(f" {phrase} " in padded for phrase in self._phrases)
