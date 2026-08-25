"""Normalize and route utterances before a conversational model turn."""
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
import re
import time
from typing import Callable

import yaml

__all__ = ["Addressing", "filler_variants", "load_intents", "normalize", "route", "vocabulary"]

_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]")
_LEGACY_STRIP = re.compile(r"[^a-z0-9\s]")
_WS = re.compile(r"\s+")
_LEAD_FILLER = {
    "okay", "ok", "no", "yes", "yeah", "please", "atlas", "hey", "now", "just",
    "uh", "um", "so", "alright", "all", "right",
}
_TAIL_FILLER = {"please", "now", "atlas", "okay", "ok"}


def normalize(value: str) -> str:
    separated = _NON_ALPHANUMERIC.sub(" ", value.casefold())
    return _WS.sub(" ", separated).strip()


def _legacy_normalize(value: str) -> str:
    return _WS.sub(" ", _LEGACY_STRIP.sub("", value.casefold())).strip()


def vocabulary(cfg: dict) -> list[str]:
    """Return only the explicit, reviewed addressed-speech vocabulary."""
    configured = cfg.get("address_vocab")
    if not isinstance(configured, list):
        raise ValueError("invalid Atlas configuration: address_vocab")
    if not all(isinstance(word, str) and word.strip() for word in configured):
        raise ValueError("invalid Atlas configuration: address_vocab")
    return list(configured)


def _vocab_forms(raw: str) -> set[str]:
    joined = normalize(raw)
    spoken = normalize(raw.replace("-", " ").replace("_", " "))
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


def filler_variants(normalized: str) -> list[str]:
    tokens = normalized.split()
    while len(tokens) > 1 and tokens[0] in _LEAD_FILLER:
        tokens.pop(0)
    without_lead = " ".join(tokens)
    tokens = list(tokens)
    while len(tokens) > 1 and tokens[-1] in _TAIL_FILLER:
        tokens.pop()
    without_edges = " ".join(tokens)
    variants = [normalized]
    for value in (without_lead, without_edges):
        if value not in variants:
            variants.append(value)
    return variants


def load_intents(path: str | Path) -> dict:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    intents = data.get("intents", data)
    if not isinstance(intents, dict):
        raise ValueError("intents config must contain a mapping")
    return intents


def route(utterance: str, intents: dict) -> tuple[str, str | None]:
    if not isinstance(utterance, str) or not utterance.strip():
        raise ValueError("utterance is empty")
    variants = filler_variants(normalize(utterance))
    for variant in filler_variants(_legacy_normalize(utterance)):
        if variant not in variants:
            variants.append(variant)
    for name, raw in (intents or {}).items():
        spec = raw if isinstance(raw, dict) else {}
        phrases = set()
        for phrase in spec.get("phrases", ()):
            if not isinstance(phrase, str):
                continue
            phrases.add(normalize(phrase))
            phrases.add(_legacy_normalize(phrase))
        if any(value in phrases for value in variants):
            return "reflex", str(name)
        patterns = spec.get("patterns", ())
        if any(re.fullmatch(pattern, value) for pattern in patterns for value in variants):
            return "reflex", str(name)
    return "fast", None
