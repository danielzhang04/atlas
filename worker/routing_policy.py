"""Deterministic fast/slow classification for standalone Atlas requests."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
import re
from typing import Protocol

from .contracts import Lane, Request, RouteDecision


# This is intentionally explicit.  Adding a capability here is a policy decision, not an
# inference from a request's wording or specificity.
FAST_CAPABILITIES = frozenset({
    "calendar.create_event",
    "calendar.event.create",
    "calendar.read_event",
    "calendar.event.read",
    "calendar.get_event",
    "calendar.event.get",
})
FAST_OPERATION_ALLOWLIST = FAST_CAPABILITIES
MAX_FAST_IO_ITEMS = 1
MAX_FAST_IO_BYTES = 64 * 1024
MAX_RAW_UTTERANCE = 4_096

_RAW_HEAVY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("raw_research_or_synthesis", re.compile(r"\b(research|synthesi[sz]e|investigate|literature review|compare sources|look up)\b", re.I)),
    ("raw_durable_content", re.compile(r"\b(google\s+doc|document|report|article|essay|content|draft)\b", re.I)),
    ("raw_multi_step", re.compile(r"\b(then|after that|step\s+\d+|workflow)\b|\bfirst\b.{0,160}\bnext\b|;", re.I)),
    ("raw_batch_or_cardinality", re.compile(r"\b(all(?![- ]day)|every|each|batch|multiple|in bulk|for each)\b", re.I)),
    ("raw_iteration", re.compile(r"\b(revise|revision|iterate|iteration|retry|again|improve|version)\b", re.I)),
    ("raw_verification", re.compile(r"\b(verify|verification|validate|proofread|check that|test)\b", re.I)),
    ("raw_cross_source_or_app", re.compile(r"\b(across|between|cross[- ]app|cross[- ]source|source|sources|gmail|browser|desktop)\b", re.I)),
)
_ACTION_WORDS = re.compile(r"\b(schedule|create|add|book|set|arrange|read|show|list|check|view|get|search|find|write|draft|delete|open|send|update|edit|run)\b", re.I)

# FAST is deliberately a positive, whole-utterance grammar. Free-form titles, agendas, invitees,
# or trailing prose are not accepted here: they go to the subscription lane. This avoids trying to
# prove atomicity by enumerating every possible second-action synonym.
_PREFIX = r"(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?(?:please\s+)?"
_DATE = (
    r"(?:today|tomorrow|(?:this|next)\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|\d{4}-\d{2}-\d{2})"
)
_TIME = r"(?:1[0-2]|0?[1-9])(?::[0-5]\d)?\s*(?:a\.?m\.?|p\.?m\.?)|(?:[01]\d|2[0-3]):[0-5]\d"
_CREATE_GRAMMAR = re.compile(
    rf"^{_PREFIX}(?:schedule|create|add|book|set\s+up|arrange)\s+(?:me\s+)?(?:an?\s+)?"
    rf"(?P<all_day>all[- ]day\s+)?(?P<kind>calendar\s+event|event|meeting|appointment|call)"
    rf"(?:\s+(?:for|on)\s+|\s+)(?P<date>{_DATE})"
    rf"(?:\s*(?:,|\bat\b)\s*(?P<time>{_TIME}))?\s*[?.!]*$",
    re.I,
)
_READ_GRAMMAR = re.compile(
    rf"^{_PREFIX}(?:read|show|list|check|view|get)\s+(?:me\s+)?(?:(?:my|the)\s+)?"
    rf"(?P<object>calendar|events?|meetings?|appointments?)"
    rf"(?:\s+(?:for|on)\s+|\s+)(?P<date>{_DATE})\s*[?.!]*$",
    re.I,
)
_WHATS_ON_GRAMMAR = re.compile(
    rf"^what(?:'s|\s+is)\s+(?:on\s+)?(?:(?:my|the)\s+)?calendar"
    rf"(?:\s+(?:for|on)\s+|\s+)(?P<date>{_DATE})\s*[?.!]*$",
    re.I,
)
# Polite "can/could/would you" imperatives remain eligible for an atomic action.
# Educational markers (including how-to constructions) are handled separately below.
_QUESTION_PREFIX = re.compile(r"^(?:how|what|where|when|is|are|do you)\b", re.I)
_INFORMATIONAL_CREATE = re.compile(
    r"\b(?:explain|teach|learn|learning|understand|guide|guidance|tutorial|instructions?|"
    r"walk\s+me\s+through|show\s+me|tell\s+me)\b|"
    r"\bhow\s+(?:to|do\s+I|can\s+I|would\s+I)\b",
    re.I,
)


def raw_heavy_reasons(raw_utterance: str) -> tuple[str, ...]:
    if not isinstance(raw_utterance, str) or len(raw_utterance) > MAX_RAW_UTTERANCE:
        return ("raw_utterance_unbounded",)
    text = raw_utterance.strip()
    return tuple(reason for reason, pattern in _RAW_HEAVY_PATTERNS if pattern.search(text))


def parse_atomic_calendar_command(operation: str, raw_utterance: str) -> dict[str, object] | None:
    """Parse only the deliberately small calendar FAST grammar into host-owned arguments."""
    if not isinstance(raw_utterance, str) or not raw_utterance.strip() or len(raw_utterance) > MAX_RAW_UTTERANCE:
        return None
    text = raw_utterance.strip()
    if any(ord(char) < 32 for char in text):
        return None
    if operation in {"calendar.create_event", "calendar.event.create"}:
        match = _CREATE_GRAMMAR.fullmatch(text)
        if match is None:
            return None
        all_day = bool(match.group("all_day"))
        time_value = match.group("time")
        if all_day == bool(time_value):
            # All-day commands have no time; timed commands require one. Missing or conflicting
            # scheduling fields are never inferred by a fast runner.
            return None
        kind = re.sub(r"\s+", " ", match.group("kind").lower())
        date_expression = _normalize_date(match.group("date"))
        if date_expression is None:
            return None
        return {
            "schema": "calendar.fast.v1",
            "action": "create",
            "calendar_id": "primary",
            "event_kind": kind,
            "title": "Calendar event" if kind in {"event", "calendar event"} else kind.capitalize(),
            "date_expression": date_expression,
            "time_expression": _normalize_time(time_value) if time_value else None,
            "all_day": all_day,
            "duration_minutes": None if all_day else 30,
            "timezone_policy": "atlas_local",
        }
    elif operation in {"calendar.read_event", "calendar.event.read", "calendar.get_event", "calendar.event.get"}:
        match = _READ_GRAMMAR.fullmatch(text) or _WHATS_ON_GRAMMAR.fullmatch(text)
        if match is None:
            return None
        date_expression = _normalize_date(match.group("date"))
        if date_expression is None:
            return None
        return {
            "schema": "calendar.fast.v1",
            "action": "read",
            "calendar_id": "primary",
            "date_expression": date_expression,
            "timezone_policy": "atlas_local",
            "max_results": 25,
        }
    else:
        return None


def _normalize_time(value: str) -> str:
    return re.sub(r"\.", "", re.sub(r"\s+", " ", value.strip().upper()))


def _normalize_date(value: str) -> str | None:
    value = re.sub(r"\s+", " ", value.strip().lower())
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            date.fromisoformat(value)
        except ValueError:
            return None
    return value


def raw_calendar_voice_reasons(operation: str, raw_utterance: str) -> tuple[str, ...]:
    """Return a single fail-closed reason unless the entire utterance matches FAST grammar."""
    if not isinstance(raw_utterance, str) or not raw_utterance.strip() or len(raw_utterance) > MAX_RAW_UTTERANCE:
        return ("raw_utterance_unbounded",)
    if operation not in FAST_CAPABILITIES:
        return ("raw_operation_not_registered_for_voice_fast",)
    if parse_atomic_calendar_command(operation, raw_utterance) is None:
        return ("raw_not_positive_atomic_calendar_grammar",)
    return ()


def bind_atomic_calendar_request(request: Request, raw_utterance: str | None) -> Request:
    """Bind parser-derived arguments without accepting model/caller supplied replacements."""
    if raw_utterance is None or request.operation not in FAST_CAPABILITIES:
        return request
    parsed = parse_atomic_calendar_command(request.operation, raw_utterance)
    if parsed is None or request.parameters:
        return request
    return replace(request, target="primary-calendar", resource="calendar", parameters=parsed)


def _valid_bound_calendar_parameters(operation: str, parameters: object) -> bool:
    if not isinstance(parameters, dict) and not hasattr(parameters, "items"):
        return False
    plain = dict(parameters)
    action = "create" if operation in {"calendar.create_event", "calendar.event.create"} else "read"
    required = ({"schema", "action", "calendar_id", "event_kind", "title", "date_expression",
                 "time_expression", "all_day", "duration_minutes", "timezone_policy"}
                if action == "create" else
                {"schema", "action", "calendar_id", "date_expression", "timezone_policy", "max_results"})
    if set(plain) != required or plain.get("schema") != "calendar.fast.v1" or plain.get("action") != action:
        return False
    if plain.get("calendar_id") != "primary" or plain.get("timezone_policy") != "atlas_local":
        return False
    date_expression = plain.get("date_expression")
    if not isinstance(date_expression, str) or _normalize_date(date_expression) != date_expression:
        return False
    if not re.fullmatch(_DATE, date_expression, re.I):
        return False
    if action == "read":
        return plain.get("max_results") == 25 and not isinstance(plain.get("max_results"), bool)
    kind = plain.get("event_kind")
    if kind not in {"calendar event", "event", "meeting", "appointment", "call"}:
        return False
    expected_title = "Calendar event" if kind in {"event", "calendar event"} else str(kind).capitalize()
    if plain.get("title") != expected_title or not isinstance(plain.get("all_day"), bool):
        return False
    if plain["all_day"]:
        return plain.get("time_expression") is None and plain.get("duration_minutes") is None
    time_expression = plain.get("time_expression")
    return (isinstance(time_expression, str)
            and _normalize_time(time_expression) == time_expression
            and re.fullmatch(_TIME, time_expression, re.I) is not None
            and plain.get("duration_minutes") == 30
            and not isinstance(plain.get("duration_minutes"), bool))


def raw_voice_is_action(raw_utterance: str) -> bool:
    if not isinstance(raw_utterance, str):
        return False
    text = raw_utterance.strip()
    if any(parse_atomic_calendar_command(operation, text) is not None for operation in FAST_CAPABILITIES):
        return True
    if _QUESTION_PREFIX.search(text) or _INFORMATIONAL_CREATE.search(text):
        return False
    return bool(_ACTION_WORDS.search(text))


class FastDispatchRejected(RuntimeError):
    """Raised when a non-fast decision is presented to a fast executor."""


class FastExecutor(Protocol):
    def execute_fast(self, request: Request):
        ...


@dataclass(frozen=True, slots=True)
class RoutingPolicy:
    """A pure policy object; it has no worker or callback dependency."""

    capabilities: frozenset[str] = FAST_CAPABILITIES
    max_io_items: int = MAX_FAST_IO_ITEMS
    max_io_bytes: int = MAX_FAST_IO_BYTES

    def __post_init__(self) -> None:
        if not self.capabilities:
            raise ValueError("fast capability allowlist cannot be empty")
        if self.max_io_items < 1 or self.max_io_bytes < 1:
            raise ValueError("fast I/O bounds must be positive")

    def classify(self, request: Request, *, raw_utterance: str | None = None) -> RouteDecision:
        if not isinstance(request, Request):
            raise TypeError("routing requires a Request")
        reasons: list[str] = []
        if request.operation not in self.capabilities:
            reasons.append("capability_not_allowlisted")
        if request.operation_count != 1:
            reasons.append("operation_count_not_one")
        if request.target_count > 1 or request.resource_count > 1:
            reasons.append("target_or_resource_count_not_one")
        if request.target_count + request.resource_count == 0:
            reasons.append("target_or_resource_missing")
        if request.cardinality != 1:
            reasons.append("cardinality_not_one")
        if request.cross_source or request.source_count > 1:
            reasons.append("cross_source")
        if request.cross_app or request.app_count > 1:
            reasons.append("cross_app")
        if request.research:
            reasons.append("research")
        if request.discovery:
            reasons.append("discovery")
        if request.iteration:
            reasons.append("iteration")
        if request.verification:
            reasons.append("verification")
        if request.steps != 1:
            reasons.append("multiple_steps")
        if request.durable_artifact or request.artifact is not None:
            reasons.append("durable_artifact")
        if request.io_items > self.max_io_items or request.io_bytes > self.max_io_bytes:
            reasons.append("I/O_unbounded")
        if request.metadata_oversized:
            reasons.append("metadata_unbounded")
        if request.parameters_oversized:
            reasons.append("parameters_unbounded")
        if request.operation in FAST_CAPABILITIES and request.parameters:
            if request.target != "primary-calendar" or request.resource != "calendar":
                reasons.append("fast_target_not_host_bound")
        if raw_utterance is not None:
            reasons.extend(reason for reason in raw_heavy_reasons(raw_utterance) if reason not in reasons)
            if request.operation in FAST_CAPABILITIES:
                reasons.extend(reason for reason in raw_calendar_voice_reasons(request.operation, raw_utterance)
                                if reason not in reasons)
                parsed = parse_atomic_calendar_command(request.operation, raw_utterance)
                if request.parameters and (parsed is None or dict(request.parameters) != parsed):
                    reasons.append("fast_parameters_not_host_bound")
            else:
                reasons.append("raw_operation_not_registered_for_voice_fast")
        if request.operation in FAST_CAPABILITIES and raw_utterance is None:
            if not _valid_bound_calendar_parameters(request.operation, request.parameters):
                reasons.append("fast_parameters_missing_or_invalid")
        return RouteDecision(Lane.SLOW if reasons else Lane.FAST, tuple(reasons))

    route = classify


DEFAULT_POLICY = RoutingPolicy()


def classify(request: Request, *, raw_utterance: str | None = None) -> RouteDecision:
    return DEFAULT_POLICY.classify(request, raw_utterance=raw_utterance)


def route(request: Request, *, raw_utterance: str | None = None) -> RouteDecision:
    return classify(request, raw_utterance=raw_utterance)


def is_fast(request: Request, *, raw_utterance: str | None = None) -> bool:
    return classify(request, raw_utterance=raw_utterance).is_fast


def dispatch_fast(decision: RouteDecision, request: Request, executor: FastExecutor):
    """Fail closed before any fast executor can receive slow work."""
    if not isinstance(decision, RouteDecision) or decision.lane is not Lane.FAST:
        raise FastDispatchRejected("only a fast route may enter the fast executor")
    if not isinstance(request, Request):
        raise TypeError("fast dispatch requires a Request")
    # Recompute from the request as well: callers cannot forge a fast decision for slow work.
    if not DEFAULT_POLICY.classify(request).is_fast:
        raise FastDispatchRejected("request is not eligible for the fast executor")
    return executor.execute_fast(request)


__all__ = ["FAST_CAPABILITIES", "FAST_OPERATION_ALLOWLIST", "MAX_RAW_UTTERANCE", "RoutingPolicy", "DEFAULT_POLICY",
           "raw_heavy_reasons", "parse_atomic_calendar_command", "bind_atomic_calendar_request",
           "raw_calendar_voice_reasons", "raw_voice_is_action", "classify", "route", "is_fast", "FastExecutor",
           "FastDispatchRejected", "dispatch_fast"]
