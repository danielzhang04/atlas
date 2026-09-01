"""Suppress action claims that are not backed by successful host tools."""
from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from fnmatch import fnmatchcase
import logging
import re
from typing import Any

from .router import normalize

ACTION_CLAIM_VERBS = (
    "opened", "launched", "sent", "created", "closed", "deleted",
    "played", "paused", "started", "moved", "minimized", "maximized",
)
_ACTION_EXPRESSION = "|".join(re.escape(verb) for verb in ACTION_CLAIM_VERBS)
_PATTERNS = {
    "action": re.compile(r"\b(?:" + _ACTION_EXPRESSION + r")\b", re.I),
    # Perfective "done" claims are matched per sentence with fullmatch, so the
    # first-person forms ("I have done that.") are safe to add here: a negated
    # sentence ("I have not done that.", "I haven't done that.") simply fails
    # the fullmatch and is never treated as a claim.
    "done": re.compile(
        r"^\s*(?:done|all done|(?:it|that|this|the (?:task|job)) (?:is|'s) done"
        r"|(?:I|we)(?:'ve| have| had)? done (?:that|it|this|so))\s*[.!]?\s*$", re.I),
    "subject": re.compile(r"\b(?:I(?:'ve| have)?|we(?:'ve| have)?)\b", re.I),
    "negation": re.compile(r"\b(?:not|never|cannot|can't|couldn't|didn't|haven't|hasn't|wasn't|weren't)\b", re.I),
    "sentence": re.compile(r"[^.!?\n]+[.!?]?"),
}
_ASSOCIATIONS = {
    # `open` licenses "opened" because, after the open-dedupe fix
    # (tools.py open_web), an ok `open` result -- including the
    # `already: true` shape -- only ever follows a real, successful open by
    # this host inside the last 15 seconds. A failed launch leaves no stamp,
    # so a retry re-invokes the opener instead of reporting already-open.
    "opened": ("open", "open_folder", "focus_window"), "sent": ("*send*",),
    "launched": ("launch_work", "kb_*launch*", "window_action", "media_key"),
    # A successful `open` launches; it does not play (item 4 -- boss
    # amendment). Only "started" is licensed by bare open evidence.
    "started": ("open", "launch_work", "kb_*launch*", "window_action", "media_key"),
    "created": ("*create*", "*draft*"), "closed": ("window_action(close)",),
    "deleted": ("*delete*", "press_delete"), "moved": ("window_action",),
    "minimized": ("window_action",), "maximized": ("window_action",),
    "played": ("media_key", "*play*"), "paused": ("media_key", "*play*"),
}
_MUTATION_MARKERS = ("send", "create", "draft", "delete", "launch", "write", "edit", "update")
_MUTATION_NAMES = frozenset({"mutate", "cancel_work", "close", "focus", "open_file"})
UNBACKED_ACTION_REPLY = "I did not actually do that - I have no tool result. Want me to? "
# Same rebuttal, minus the offer: used when the host DID attempt the action and
# the attempt failed. "Want me to?" is wrong there -- it was already tried.
FAILED_ATTEMPT_REPLY = "That did not go through - the confirmation failed. "

# Detection-only (item 7): a refusal-shaped reply is never delayed and never
# rewritten -- the host must NEVER fabricate "I can do that with X - shall
# I?". This is kept solely so a false "I don't have access" next to a
# registered, on-topic tool still logs a bounded, tool-name-only WARNING;
# that log was the only observable signal of the false refusal.
_REFUSAL_MARKERS = ("can t", "cannot", "unable to", "don t have access", "do not have access", "no access")
_REFUSAL_PATTERNS = {
    "folder": re.compile(
        r"\b(?:open|show|launch)\b.*\b(?:folder|directory)\b|"
        r"\b(?:folder|directory)\b.*\b(?:open|show|launch)\b", re.I,
    ),
    "find": re.compile(r"\b(?:find|locate|search)\b.*\b(?:file|folder|directory)\b", re.I),
    "read": re.compile(r"\bread\b.*\bfile\b", re.I),
    "mail": re.compile(r"\b(?:count|how many)\b.*\b(?:email|emails|mail|messages)\b", re.I),
    "open": re.compile(r"\b(?:open|show|launch)\b", re.I),
}
_REFUSAL_ROUTES = (
    ("open_folder", "folder"), ("find_file", "find"), ("read_file", "read"),
    ("count_mail", "mail"), ("open", "open"),
)
logger = logging.getLogger("atlas.brain")
# The claim label is already a verb from the closed action pattern; the cap
# holds even if that pattern ever grows a longer alternative.
_CLAIM_LABEL_LIMIT = 24


class ClaimGuard:
    """Associate narrated action claims with successful host-tool evidence."""

    def __init__(self, transcript: str = "", schemas: Sequence[Mapping[str, Any]] = ()) -> None:
        self.transcript = normalize(transcript)
        self.available_tools = frozenset(
            schema["name"] for schema in schemas if isinstance(schema.get("name"), str)
        )
        self._substituted = False

    def observe(self, text: str) -> None:
        """Run detection-only side effects (the refusal WARNING) on a chunk.

        Kept separate from `delayed()` so the caller can run it for EVERY
        streamed chunk: brain.py short-circuits `delayed()` once anything is
        held, which used to mute the false-refusal log for the rest of the
        turn.
        """
        self._log_refused_capability(text)

    def delayed(self, text: str) -> bool:
        if _PATTERNS["action"].search(text):
            return True
        # A short "Done." glues to the next sentence in the same chunk
        # (split_spoken/_sentence_end refuse a boundary under 12 stripped
        # chars), so the anchored done pattern must be matched per sentence
        # within the chunk (item 1), not against the whole, now-longer text.
        return any(
            _PATTERNS["done"].fullmatch(match.group(0).strip())
            for match in _PATTERNS["sentence"].finditer(text)
        )

    @staticmethod
    def evidence(name: str, arguments: Mapping[str, Any], ok: bool) -> tuple[str, bool]:
        folded = name.casefold()
        close = folded == "window_action" and normalize(str(arguments.get("action", ""))) == "close"
        return ("window_action(close)" if close else folded), ok

    def evaluate(self, sentence: str, tool_evidence: list[tuple[str, bool]]) -> str | None:
        for claim in self._claims(sentence):
            if not any(ok and self._supports(name, claim) for name, ok in tool_evidence):
                # A label, never the model's sentence: this WARNING is now
                # persisted (worker.log), and the sentence it suppressed is
                # generated prose about Daniel's own request. The verb comes
                # from the closed action pattern; the length is what is left
                # of the diagnostic value.
                logger.warning(
                    "unbacked action claim suppressed (claim=%s chars=%d)",
                    claim[:_CLAIM_LABEL_LIMIT],
                    len(sentence),
                )
                if self._substituted:
                    # Item 6: only the first rebuttal is spoken. The caller
                    # (brain.py) emits it as the LAST chunk of the turn, so
                    # it already covers every claim in the reply; repeating
                    # "I did not actually do that" for each later unbacked
                    # sentence would be noisy and say nothing new -- so
                    # later unbacked chunks are dropped silently instead.
                    return None
                self._substituted = True
                return UNBACKED_ACTION_REPLY
        return sentence

    def _log_refused_capability(self, reply: str) -> None:
        if not any(marker in normalize(reply) for marker in _REFUSAL_MARKERS):
            return
        capability = next(
            (name for name, pattern in _REFUSAL_ROUTES
             if name in self.available_tools and _REFUSAL_PATTERNS[pattern].search(self.transcript)),
            None,
        )
        if capability is not None:
            logger.warning("available capability refusal suppressed (tool=%s)", capability)

    def _claims(self, text: str) -> Iterator[str]:
        for match in _PATTERNS["sentence"].finditer(text):
            sentence = match.group(0).strip()
            if not sentence or sentence.endswith("?"):
                continue
            if _PATTERNS["done"].fullmatch(sentence):
                yield "done"
                continue
            for verb in _PATTERNS["action"].finditer(sentence):
                subjects = list(_PATTERNS["subject"].finditer(sentence, 0, verb.start()))
                if subjects and not _PATTERNS["negation"].search(
                    sentence[subjects[-1].end():verb.start()]
                ):
                    yield verb.group(0).casefold()

    @staticmethod
    def _supports(name: str, claim: str) -> bool:
        if claim != "done":
            return any(fnmatchcase(name, pattern) for pattern in _ASSOCIATIONS.get(claim, ()))
        associated = any(fnmatchcase(name, pattern) for patterns in _ASSOCIATIONS.values()
                         for pattern in patterns)
        return name != "cancel_pending" and (
            name in _MUTATION_NAMES or associated or any(marker in name for marker in _MUTATION_MARKERS)
        )
