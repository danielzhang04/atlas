"""Bounded local phrase matcher for explicit voice-session controls.

Production config contains only dismiss phrases. Every unmatched utterance returns ``fast`` for
legacy API compatibility, which now means "continue to Claude"; work routing happens behind that
conversation through a host-validated tool proposal.

`route(utterance, intents)` returns `("reflex", intent_name)` for a reflex hit and `("fast", None)`
otherwise. `intents` is the mapping returned by `load_intents(path)` — the router holds NO phrases
of its own, so the matching data is entirely data-driven and unit-testable off any dict.

Pure by contract: no I/O beyond `load_intents` reading the YAML file; `route` touches only its args.
"""
import re
from pathlib import Path

import yaml

# Normalization shared by phrase matching and pattern matching: casefold, drop everything that is
# not an ascii letter / digit / space (punctuation, apostrophes — Deepgram may omit them), collapse
# runs of whitespace. Adapted from V0's _is_dismiss/_norm_phrase, now the router's single normalizer.
_STRIP = re.compile(r"[^a-z0-9\s]")
_WS = re.compile(r"\s+")


def normalize(s: str) -> str:
    return _WS.sub(" ", _STRIP.sub("", s.casefold())).strip()


# Gate-C desk finding (2026-07-21): real dismissals arrive wrapped in filler — "Okay. Go to sleep.",
# "No. Go to sleep." — and exact matching bounced them to the LLM, which then ROLE-PLAYED sleeping
# with the mic still open. Bounded fix: strip leading filler tokens and trailing courtesy tokens
# BEFORE phrase matching. Content-bearing words are deliberately NOT in these sets, so
# "cancel the deploy card" still routes fast (near-miss protection intact).
_LEAD_FILLER = {"okay", "ok", "no", "yes", "yeah", "please", "atlas", "hey", "now", "just",
                "uh", "um", "so", "alright", "all", "right"}
_TAIL_FILLER = {"please", "now", "atlas", "okay", "ok"}


def filler_variants(norm: str) -> list:
    """Candidate forms of an already-normalized utterance for phrase matching: as-is,
    lead-filler-stripped, and lead+tail-stripped (bounded sets, iterative). All variants are
    matched against both the original and stripped forms. Never strips to empty — a pure-filler
    utterance keeps its last token."""
    tokens = norm.split()
    while len(tokens) > 1 and tokens[0] in _LEAD_FILLER:
        tokens.pop(0)
    lead = " ".join(tokens)
    tokens = list(tokens)
    while len(tokens) > 1 and tokens[-1] in _TAIL_FILLER:
        tokens.pop()
    both = " ".join(tokens)
    out = [norm]
    for v in (lead, both):
        if v not in out:
            out.append(v)
    return out


def load_intents(path) -> dict:
    """Load reflex intents from a YAML file. Returns the intent mapping
    `{name: {"phrases": [...], "patterns": [...]}}` (both keys optional per intent)."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return data.get("intents", data)


def route(utterance: str, intents: dict) -> tuple:
    """Classify `utterance` against `intents`. Empty/whitespace still raises ValueError (the caller
    must never route a blank final transcript). First intent to match wins (declaration order)."""
    if not utterance or not utterance.strip():
        raise ValueError("utterance is empty")
    variants = filler_variants(normalize(utterance))
    for name, spec in (intents or {}).items():
        spec = spec or {}
        phrases = {normalize(p) for p in (spec.get("phrases") or [])}
        if any(v in phrases for v in variants):
            return ("reflex", name)
        for pattern in (spec.get("patterns") or []):
            # Anchored: re.fullmatch must consume a WHOLE variant, so content-bearing extra
            # words defeat the match (near-misses stay in the fast lane).
            if any(re.fullmatch(pattern, v) for v in variants):
                return ("reflex", name)
    return ("fast", None)
