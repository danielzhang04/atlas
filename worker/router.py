"""Match configured exact voice reflexes before a conversational model turn."""
from __future__ import annotations

from pathlib import Path
import re

import yaml

__all__ = ["filler_variants", "load_intents", "normalize", "route"]

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
