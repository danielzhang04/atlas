"""Voice-clean text (conversation-rules design §2). Streaming-safe by construction for
SINGLE-character markers: they are removed per-chunk at character level (a split "**" still dies);
line-anchored headers, list markers, and links are best-effort within a chunk. Chunks are never
.strip()ped — a single leading/trailing space is the word boundary between streamed segments.

COUPLING NOTE (review 2026-07-22): the multi-char [quiet] marker is NOT independently
streaming-safe — a chunk split mid-marker ("[", "quiet]") would leak audible fragments. Today
livekit's default `filter_markdown` transform runs UPSTREAM of tts_node and buffers any unclosed
"[" until the bracket closes, so the marker always arrives contiguous (verified against installed
livekit-agents 1.6.6: generation.py:355 + filters.py has_incomplete_pattern). If
`tts_text_transforms` is ever customized/emptied, or that upstream bracket-buffering changes on
upgrade, add a bracket-span buffer to our tts_node override — test_split_quiet_marker_limitation
documents the dependency."""
import re

# Silent-turn marker (Gate finding #1, 2026-07-21): the LLM's ONLY way to say nothing is to reply
# exactly [quiet] — an LLM turn always produces text, so silence needs a token. Stripped here so
# it can never be spoken; callers also avoid mirroring marker-only turns.
_QUIET = re.compile(r"\[quiet\]", re.IGNORECASE)
_QUIET_TURN = re.compile(r"\s*\[quiet\][.!\s]*$", re.IGNORECASE)

_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_HEADER = re.compile(r"(?m)^[ \t]*#{1,6}[ \t]+")
_LIST_MARKER = re.compile(r"(?m)^[ \t]*(?:[-•*]|\d+\.)[ \t]+")
_MULTISPACE = re.compile(r"[ \t]{2,}")


def is_quiet_turn(text: str) -> bool:
    """True when an assistant turn is the silent-turn marker (allowing trailing punctuation) —
    the caller must then avoid mirroring the turn."""
    return bool(_QUIET_TURN.fullmatch(text))


def sanitize_for_tts(text: str) -> str:
    # Header regex runs BEFORE the char-level strip: stripping "#" alone would leave its trailing
    # space ("# Status" -> " Status"). The char pass still catches "#" split from its space (PR #44).
    # [quiet] first: it must never reach the speaker, and _LINK would otherwise leave it intact
    # (no parenthesized target). A marker-only turn (trailing punctuation included) sanitizes to
    # nothing at all -> no synthesis; a rule-violating mixed turn just loses the marker.
    if _QUIET_TURN.fullmatch(text):
        return ""
    t = _QUIET.sub("", text)
    t = _LINK.sub(r"\1", t)
    t = _HEADER.sub("", t)
    t = _LIST_MARKER.sub("", t)
    t = t.replace("`", "").replace("*", "").replace("#", "")
    t = t.replace("_", " ")
    return _MULTISPACE.sub(" ", t)
