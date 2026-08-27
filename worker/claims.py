"""Suppress action claims that are not backed by successful host tools."""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from fnmatch import fnmatchcase
import logging
import re
from typing import Any

from .router import normalize

ACTION_CLAIM_VERBS = (
    "open", "opened", "launched", "sent", "created", "closed", "deleted",
    "done", "played", "paused", "started", "moved", "minimized", "maximized",
)
_ACTION_EXPRESSION = "|".join(r"open(?![-]|\s+source\b)" if verb == "open" else re.escape(verb)
                              for verb in ACTION_CLAIM_VERBS)
_PATTERNS = {
    "action": re.compile(r"\b(?:" + _ACTION_EXPRESSION + r")\b", re.I),
    "open_state": re.compile(r"\bis(?:\s+(?:already|currently|now))?\s+open\b(?![-]|\s+source\b)", re.I),
    "done": re.compile(r"^\s*(?:done|all done|(?:it|that|this|the (?:task|job)) (?:is|'s) done)\s*[.!]?\s*$", re.I),
    "subject": re.compile(r"\b(?:I(?:'ve| have)?|we(?:'ve| have)?)\b", re.I),
    "negation": re.compile(r"\b(?:not|never|cannot|can't|couldn't|didn't|haven't|hasn't|wasn't|weren't)\b", re.I),
    "sentence": re.compile(r"[^.!?\n]+[.!?]?"),
    "folder": re.compile(
        r"\b(?:open|show|launch)\b.*\b(?:folder|directory)\b|"
        r"\b(?:folder|directory)\b.*\b(?:open|show|launch)\b", re.I,
    ),
    "find": re.compile(r"\b(?:find|locate|search)\b.*\b(?:file|folder|directory)\b", re.I),
    "read": re.compile(r"\bread\b.*\bfile\b", re.I),
    "mail": re.compile(r"\b(?:count|how many)\b.*\b(?:email|emails|mail|messages)\b", re.I),
    "open": re.compile(r"\b(?:open|show|launch)\b", re.I),
}
_ASSOCIATIONS = {
    "opened": ("open", "open_folder", "focus_window"), "sent": ("*send*",),
    "launched": ("launch_work", "kb_*launch*", "window_action", "media_key"),
    "started": ("launch_work", "kb_*launch*", "window_action", "media_key"),
    "created": ("*create*", "*draft*"), "closed": ("window_action(close)",),
    "deleted": ("*delete*", "press_delete"), "moved": ("window_action",),
    "minimized": ("window_action",), "maximized": ("window_action",),
    "played": ("media_key", "*play*"), "paused": ("media_key", "*play*"),
}
_REFUSAL_MARKERS = ("can t", "cannot", "unable to", "don t have access", "do not have access", "no access")
_REFUSAL_ROUTES = (
    ("open_folder", "folder"), ("find_file", "find"), ("read_file", "read"),
    ("count_mail", "mail"), ("open", "open"),
)
_TARGET_ARGUMENTS = frozenset({"app", "application", "profile", "target", "window"})
_MUTATION_MARKERS = ("send", "create", "draft", "delete", "launch", "write", "edit", "update")
_MUTATION_NAMES = frozenset({"mutate", "cancel_work", "close", "focus", "open_file"})
UNBACKED_ACTION_REPLY = "I did not actually do that - I have no tool result. Want me to?"
logger = logging.getLogger("atlas.brain")


class ClaimGuard:
    """Associate narrated action claims with successful host-tool evidence."""

    def __init__(self, transcript: str, registry: Any, schemas: list[dict[str, Any]]) -> None:
        self.transcript = normalize(transcript)
        self.available_tools = frozenset(schema["name"] for schema in schemas
                                         if isinstance(schema.get("name"), str))
        self.targets = self._registry_targets(registry, schemas)
        self._substituted = False
    def delayed(self, text: str) -> bool:
        return bool(_PATTERNS["action"].search(text)) or any(
            marker in normalize(text) for marker in _REFUSAL_MARKERS)
    def remember(self, arguments: Mapping[str, Any]) -> None:
        for key, value in arguments.items():
            if (normalize(str(key)) in _TARGET_ARGUMENTS and isinstance(value, str)
                    and (target := normalize(value))):
                self.targets.add(target)
    @staticmethod
    def evidence(name: str, arguments: Mapping[str, Any], ok: bool) -> tuple[str, bool]:
        folded = name.casefold()
        close = folded == "window_action" and normalize(str(arguments.get("action", ""))) == "close"
        return ("window_action(close)" if close else folded), ok
    def evaluate(self, sentence: str, tool_evidence: list[tuple[str, bool]]) -> str | None:
        if (capability := self._refused_capability(sentence)) is not None:
            logger.warning("available capability refusal suppressed (tool=%s)", capability)
            return f"I can do that with {capability} - shall I?"
        for claim in self._claims(sentence):
            if not any(ok and self._supports(name, claim) for name, ok in tool_evidence):
                logger.warning("unbacked action claim suppressed (claim=%s)", claim)
                if self._substituted:
                    return None
                self._substituted = True
                return UNBACKED_ACTION_REPLY
        return sentence
    def _refused_capability(self, reply: str) -> str | None:
        if not any(marker in normalize(reply) for marker in _REFUSAL_MARKERS):
            return None
        return next((name for name, pattern in _REFUSAL_ROUTES
                     if name in self.available_tools and _PATTERNS[pattern].search(self.transcript)), None)

    def _claims(self, text: str) -> Iterator[str]:
        for match in _PATTERNS["sentence"].finditer(text):
            sentence = match.group(0).strip()
            if not sentence or sentence.endswith("?"):
                continue
            if _PATTERNS["done"].fullmatch(sentence):
                yield "done"
                continue
            open_states = list(_PATTERNS["open_state"].finditer(sentence))
            for verb in _PATTERNS["action"].finditer(sentence):
                if any(state.start() <= verb.start() < state.end() for state in open_states):
                    continue
                subjects = list(_PATTERNS["subject"].finditer(sentence, 0, verb.start()))
                if subjects and not _PATTERNS["negation"].search(
                    sentence[subjects[-1].end():verb.start()]
                ):
                    folded = verb.group(0).casefold()
                    yield "opened" if folded == "open" else folded
            for state in open_states:
                prefix = normalize(sentence[:state.start()])
                if any(prefix == target or prefix.endswith(" " + target) for target in self.targets):
                    yield "opened"

    @staticmethod
    def _supports(name: str, claim: str) -> bool:
        if claim != "done":
            return any(fnmatchcase(name, pattern) for pattern in _ASSOCIATIONS.get(claim, ()))
        associated = any(fnmatchcase(name, pattern) for patterns in _ASSOCIATIONS.values()
                         for pattern in patterns)
        return name != "cancel_pending" and (
            name in _MUTATION_NAMES or associated or any(marker in name for marker in _MUTATION_MARKERS)
        )

    @staticmethod
    def _registry_targets(registry: Any, schemas: list[dict[str, Any]]) -> set[str]:
        targets = {normalize(value) for value in getattr(registry, "_open_aliases", ())
                   if isinstance(value, str) and normalize(value)}
        for schema in schemas:
            properties = schema.get("input_schema", {}).get("properties", {})
            if not isinstance(properties, Mapping):
                continue
            for key, definition in properties.items():
                if normalize(str(key)) not in _TARGET_ARGUMENTS or not isinstance(definition, Mapping):
                    continue
                targets.update(normalize(value) for value in definition.get("enum", ())
                               if isinstance(value, str) and normalize(value))
        return targets
