"""Normalize and route utterances before a conversational model turn.

Rule 11 measurement for DD-3 (2026-09-01, python 3.13, this machine). The
whole unit adds exactly ONE module-scope import anywhere on the desktop or
worker startup path -- `dataclasses` here, for OpenReflex -- and it is stdlib,
not third party. Measured rather than asserted, in the real startup order
(worker.app imports worker.tools, which has imported `dataclasses` at module
scope since long before this change):

    import worker.tools; before = set(sys.modules); import worker.router
    -> modules added: ['worker.router']            (nothing else, at all)
    -> worker.router import time after tools: 17.1 ms, unchanged from base
    -> idle RSS: 27.00 MiB after worker.tools, 27.79 MiB after adding
       worker.router + worker.traces, identical to base within noise

A module-scope import diff against base 7121cb4 over every file this unit
touches (worker/{app,router,tools,traces,brain}.py) shows `dataclasses` in
router as the only addition and nothing removed. The two `from worker import
router` lines added to worker/tools.py are deliberately function-local, and
worker.traces stays off the startup path exactly as before.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
import re
import time
from typing import Callable

import yaml

__all__ = [
    "Addressing",
    "OpenReflex",
    "check_open_vocabulary",
    "filler_variants",
    "load_intents",
    "normalize",
    "reflex_open",
    "route",
    "vocabulary",
]

_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]")
_LEGACY_STRIP = re.compile(r"[^a-z0-9\s]")
_WS = re.compile(r"\s+")
_LEAD_FILLER = {
    "okay", "ok", "no", "yes", "yeah", "please", "atlas", "hey", "now", "just",
    "uh", "um", "so", "alright", "all", "right",
}
_TAIL_FILLER = {"please", "now", "atlas", "okay", "ok"}
_KB_UNLOCK_PHRASES = {"unlock kb", "unlock the dashboard"}


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


# --- deterministic "open <known thing>" matching (DD-3) --------------------
# ~28% of live turns are this shape and every one of them paid a full model
# round trip (a ~25K-token prefix, 1.0-8.1s of generation measured on Daniel's
# own traces) to reach a host tool that could have answered in milliseconds
# from a closed vocabulary the host already holds.
#
# The whole grammar is below, and it is deliberately small. A wrong reflex is
# worse than a slow turn -- it acts on Daniel's screen without the one reader
# that could have noticed the utterance meant something else -- so this matches
# EXACTLY: a verb from a fixed list, an optional determiner, and then a name
# that must be, character for character after normalization, one of the
# configured `open` aliases or named roots. There is no fuzzy matching, no
# prefix matching, and no scoring. Anything else -- extra words, a conjunction,
# a name the host does not already know -- returns None and takes the model
# lane exactly as it did before this existed.
#
# Verbs are open-shaped only. "show me my email" and "pull up my inbox" are NOT
# here on purpose: they read at least as naturally as "read me my email", and
# a reflex that opened Gmail when Daniel wanted it read to him is the exact
# failure this grammar is built to avoid.
_OPEN_VERBS = (("open", "up"), ("open",), ("launch",))
_OPEN_DETERMINERS = frozenset({"my", "the"})
# Allowed only when the stem is a named ROOT, where it cannot introduce an
# ambiguity: every root is a directory, so "open my downloads folder" and
# "open my downloads" name the same thing.
_ROOT_SUFFIX = "folder"
# focus_last_opened takes no arguments at all, so there is nothing here for a
# name to be wrong about -- these phrases either match or they do not.
_BRING_BACK_PHRASES = frozenset({
    "bring that back", "bring it back",
    "bring that back up", "bring it back up",
})


@dataclass(frozen=True, slots=True)
class OpenReflex:
    """One deterministic host action an utterance resolved to.

    `kind` says which host tool answers it -- "alias" the `open` tool, "root"
    `open_folder`, "last" `focus_last_opened` -- and `name` is the host's own
    vocabulary word, never the raw utterance.
    """

    kind: str
    name: str


def _vocabulary_map(values: Iterable[str]) -> dict[str, str]:
    """Normalized key -> the host's own configured word.

    SORTED, and that is load-bearing rather than tidiness. The registry
    publishes these vocabularies as frozensets, so iteration order varies with
    PYTHONHASHSEED, i.e. across restarts of the same build. `normalize`
    collapses every non-[a-z0-9] character to a space while the alias loader
    only rejects exact casefold duplicates, so two words for DIFFERENT apps
    that differ only in punctuation -- "vs-code" and "vs code" is the one that
    gets added next -- both load and collapse to one key here. Unsorted, the
    winner would be whichever the frozenset happened to yield first: Atlas
    would open one app today and a different one after a restart, with no
    model reader and no confirmation in the way.

    `check_open_vocabulary` refuses that configuration at load, which is where
    a config error belongs. This sort is the second wall: if a vocabulary ever
    reaches the grammar unchecked, the collision resolves the same way every
    time (alphabetically first) instead of nondeterministically.
    """
    mapping: dict[str, str] = {}
    for value in sorted(value for value in values if isinstance(value, str)):
        key = normalize(value)
        if key:
            mapping.setdefault(key, value)
    return mapping


def check_open_vocabulary(values: Iterable[str], *, field: str) -> None:
    """Raise if a closed host vocabulary has a normalize-collision.

    Called at LOAD time (ToolRegistry._configure_open_aliases /
    _configure_root_names), never per turn: a colliding config is a config
    error and should be loud at startup, while a turn that raised would take
    out every addressed utterance instead. Two entries that normalize to one
    key are not a preference to resolve -- one of them can never be spoken to,
    and which one is unknowable from here.

    An entry that normalizes to NOTHING is refused for the same reason and
    said the same way. "!!!" is every bit as unreachable as the loser of a
    collision -- normalize keeps only [a-z0-9], so no utterance can ever
    produce that key -- and failing closed is not the same as telling Daniel.
    Silently loading an alias that can never be spoken to is how a config
    typo becomes "Atlas just ignores me when I say that".
    """
    seen: dict[str, str] = {}
    listed = list(values)
    if not all(isinstance(value, str) for value in listed):
        # Same failure, one step earlier: a non-string entry is a vocabulary
        # word nothing can ever say.
        raise ValueError(f"invalid Atlas configuration: {field} must be strings")
    for value in sorted(listed):
        key = normalize(value)
        if not key:
            raise ValueError(
                f"invalid Atlas configuration: {field} entry {value!r} has no "
                "speakable characters, so nothing said aloud could ever "
                "reach it"
            )
        if key in seen and seen[key] != value:
            raise ValueError(
                f"invalid Atlas configuration: {field} entries "
                f"{seen[key]!r} and {value!r} are the same spoken word"
            )
        seen[key] = value


def reflex_open(
    utterance: str,
    *,
    aliases: Iterable[str] = (),
    roots: Iterable[str] = (),
) -> OpenReflex | None:
    """Resolve an unambiguous "open <known thing>" utterance, or None.

    `aliases` and `roots` are the host's OWN closed vocabularies (see
    ToolRegistry.open_aliases / root_names). A name present in both is refused
    rather than guessed at: two host tools would answer it and nothing here
    can tell which one Daniel meant.
    """
    if not isinstance(utterance, str) or not utterance.strip():
        return None
    alias_map = _vocabulary_map(aliases)
    root_map = _vocabulary_map(roots)
    for variant in filler_variants(normalize(utterance)):
        if variant in _BRING_BACK_PHRASES:
            return OpenReflex("last", "")
        tokens = variant.split()
        for verb in _OPEN_VERBS:
            if tuple(tokens[:len(verb)]) != verb:
                continue
            rest = tokens[len(verb):]
            if rest and rest[0] in _OPEN_DETERMINERS:
                rest = rest[1:]
            name = " ".join(rest)
            if not name:
                break
            # Every host action this name could mean, gathered before any of
            # them is chosen. The " folder" suffix used to be a separate
            # early return further down, which let it walk straight past the
            # both-vocabularies refusal above it: with "kb" configured as an
            # app AND a root, "open kb" refused (right) while "open kb
            # folder" quietly opened the directory, and an alias literally
            # named "x folder" beat a root "x" with nothing checking. One
            # list, one count, one rule for all three readings.
            readings = []
            if name in alias_map:
                readings.append(OpenReflex("alias", alias_map[name]))
            if name in root_map:
                readings.append(OpenReflex("root", root_map[name]))
            if name.endswith(f" {_ROOT_SUFFIX}"):
                stem = name[:-len(_ROOT_SUFFIX) - 1]
                # The stem gets the SAME test the bare name gets. "folder" is
                # only a disambiguator when the stem was unambiguous to begin
                # with; on a stem two host tools would answer, it is just a
                # louder guess.
                if stem in root_map and stem not in alias_map:
                    readings.append(OpenReflex("root", root_map[stem]))
            if len(readings) == 1:
                return readings[0]
            if readings:
                # More than one host tool would answer this and nothing here
                # can tell which one Daniel meant. Refuses rather than
                # guesses -- the model lane can ask him.
                return None
            # The verb matched but the name is not one the host knows. That
            # is the fall-through this grammar exists to protect: the model
            # lane sees the utterance untouched.
            break
    return None
# ---------------------------------------------------------------------------


def route(utterance: str, intents: dict) -> tuple[str, str | None]:
    if not isinstance(utterance, str) or not utterance.strip():
        raise ValueError("utterance is empty")
    variants = filler_variants(normalize(utterance))
    for variant in filler_variants(_legacy_normalize(utterance)):
        if variant not in variants:
            variants.append(variant)
    if any(value in _KB_UNLOCK_PHRASES for value in variants):
        return "reflex", "unlock_kb"
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
