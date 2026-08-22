"""Typed execution coordinator shared by Atlas fast and slow lanes.

Models and transcripts cannot call adapters.  They may only produce a bounded capability call;
this host coordinator validates it against the manifest and routes it through the single
``ActionBroker`` instance owned by ``RuntimeServices``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
import copy
from hashlib import sha256
import json
import math
import re
from types import MappingProxyType
from typing import Any, Mapping

from .actionbroker import ActionSnapshot, PROPOSED, SUCCEEDED
from .contracts import JobClaim, JobState, Lane, Request
from .jobstore import JobStore
from .routing_policy import RoutingPolicy
from .runtime import RuntimeServices


MAX_CALL_BYTES = 8_192
MAX_OBSERVATION_BYTES = 32_768
_CAPABILITY_ID = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){1,4}")
_FAST_CALENDAR = {
    "calendar.create_event": "google.calendar.create",
    "calendar.event.create": "google.calendar.create",
    "calendar.read_event": "google.calendar.read",
    "calendar.event.read": "google.calendar.read",
    "calendar.get_event": "google.calendar.read",
    "calendar.event.get": "google.calendar.read",
}
OBSERVABLE_READ_CAPABILITIES = frozenset({
    "browser.inspect", "google.drive.list", "google.drive.read", "google.docs.read",
    "google.gmail.read", "google.calendar.read",
})
_PRIVATE_SECRET_FIELDS = frozenset({
    "access_token", "refresh_token", "token", "authorization", "api_key", "password",
    "cookie", "cookies", "set-cookie", "session", "session_id", "headers",
})


class CapabilityDispatchError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TypedCapabilityCall:
    capability_id: str
    parameters: Mapping[str, Any]
    idempotency_key: str

    def __post_init__(self) -> None:
        if not isinstance(self.capability_id, str) or _CAPABILITY_ID.fullmatch(self.capability_id) is None:
            raise ValueError("invalid capability id")
        if (not isinstance(self.idempotency_key, str) or not self.idempotency_key
                or len(self.idempotency_key) > 512 or any(ord(char) < 32 for char in self.idempotency_key)):
            raise ValueError("invalid capability idempotency key")
        if not isinstance(self.parameters, Mapping) or len(self.parameters) > 64:
            raise TypeError("capability parameters must be a bounded mapping")
        try:
            plain = copy.deepcopy(dict(self.parameters))
            encoded = json.dumps(plain, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                 allow_nan=False)
        except (TypeError, ValueError):
            raise ValueError("capability parameters must be JSON-compatible") from None
        if len(encoded.encode("utf-8")) > MAX_CALL_BYTES:
            raise ValueError("capability parameters exceed the bounded limit")
        object.__setattr__(self, "parameters", MappingProxyType(plain))


@dataclass(frozen=True, slots=True)
class BrokeredCapabilityResult:
    capability_id: str
    status: str
    proposal_id: str
    parameters_hash: str


@dataclass(frozen=True, slots=True)
class BrokeredReadObservation:
    """Bounded private model input; never a public job/event/receipt projection."""

    capability_id: str
    proposal_id: str
    parameters_hash: str
    content_json: str = field(repr=False)
    content_digest: str
    truncated: bool

    def __post_init__(self) -> None:
        if self.capability_id not in OBSERVABLE_READ_CAPABILITIES:
            raise ValueError("observation capability is not an approved read")
        if not isinstance(self.content_json, str):
            raise TypeError("private observation content must be canonical JSON")
        encoded = self.content_json.encode("utf-8")
        if len(encoded) > MAX_OBSERVATION_BYTES:
            raise ValueError("private observation exceeds its bounded frame")
        try:
            parsed = json.loads(self.content_json)
        except json.JSONDecodeError:
            raise ValueError("private observation content is invalid") from None
        canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=False, allow_nan=False)
        if canonical != self.content_json:
            raise ValueError("private observation content is not canonical")
        if sha256(encoded).hexdigest() != self.content_digest:
            raise ValueError("private observation digest is invalid")

    @property
    def content(self) -> Any:
        """Return a fresh plain value so callers cannot invalidate the recorded digest."""
        return json.loads(self.content_json)


class SharedCapabilityBroker:
    """Closed dispatcher over one RuntimeServices/ActionBroker instance."""

    def __init__(self, services: RuntimeServices) -> None:
        if not isinstance(services, RuntimeServices):
            raise TypeError("shared broker requires RuntimeServices")
        self.services = services

    def dispatch(self, call: TypedCapabilityCall, *,
                 action_context: tuple[str, str] | None = None) -> BrokeredCapabilityResult:
        if not isinstance(call, TypedCapabilityCall):
            raise TypeError("dispatch requires a typed capability call")
        if action_context is not None:
            if (not isinstance(action_context, tuple) or len(action_context) != 2
                    or any(not isinstance(value, str) or not value or len(value) > 128
                           or any(ord(char) < 32 for char in value)
                           for value in action_context)):
                raise ValueError("invalid trusted action context")
        manifest = self.services.catalog.get(call.capability_id)
        if manifest is None:
            raise CapabilityDispatchError("capability is not registered")
        parameters = dict(call.parameters)
        snapshot = self._prepare(
            call.capability_id, parameters, call.idempotency_key, action_context=action_context)
        if manifest.confirmation == "none":
            snapshot = self._execute_host_read(snapshot)
        return BrokeredCapabilityResult(
            call.capability_id, snapshot.status, snapshot.proposal_id, snapshot.parameters_hash,
        )

    def dispatch_observed(self, call: TypedCapabilityCall) -> BrokeredReadObservation:
        """Execute one reviewed read and return a bounded private observation.

        Mutations and proposals never enter this channel. The returned content is suitable for a
        protected model turn, not for the public job store, UI, or receipt journal.
        """
        if not isinstance(call, TypedCapabilityCall):
            raise TypeError("observed dispatch requires a typed capability call")
        if call.capability_id not in OBSERVABLE_READ_CAPABILITIES:
            raise CapabilityDispatchError("capability is not approved for private observation")
        result = self.dispatch(call)
        if result.status != SUCCEEDED:
            raise CapabilityDispatchError("observed read did not complete")
        snapshot = self.services.broker.get(result.proposal_id)
        content, truncated = _bounded_private_observation(snapshot.receipt)
        encoded = json.dumps(content, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode("utf-8")
        return BrokeredReadObservation(
            result.capability_id, result.proposal_id, result.parameters_hash,
            encoded.decode("utf-8"), sha256(encoded).hexdigest(), truncated,
        )

    def _execute_host_read(self, snapshot: ActionSnapshot) -> ActionSnapshot:
        if snapshot.status == SUCCEEDED:
            return snapshot
        if snapshot.status != PROPOSED:
            raise CapabilityDispatchError("read action is not executable")
        self.services.broker.confirm(
            snapshot.proposal_id, channel="service", parameters_hash=snapshot.parameters_hash,
            session_id=snapshot.session_id, device_id=snapshot.device_id,
        )
        completed = self.services.broker.execute(
            snapshot.proposal_id, parameters_hash=snapshot.parameters_hash)
        if completed.status != SUCCEEDED:
            raise CapabilityDispatchError("capability execution failed")
        return completed

    def _prepare(self, capability_id: str, p: dict[str, Any], key: str, *,
                 action_context: tuple[str, str] | None = None) -> ActionSnapshot:
        desktop, browser, google = self.services.desktop, self.services.browser, self.services.google
        session_id, device_id = action_context or (None, None)
        if capability_id == "desktop.open":
            _exact_keys(p, {"app_id", "target_alias"}, optional={"target_alias"})
            if desktop is None:
                raise CapabilityDispatchError("desktop adapter is unavailable")
            return desktop.prepare_open(_text(p, "app_id"), _optional_text(p, "target_alias"),
                                        idempotency_key=key, session_id=session_id,
                                        device_id=device_id)
        if capability_id == "desktop.focus":
            _exact_keys(p, {"app_id"})
            if desktop is None:
                raise CapabilityDispatchError("desktop adapter is unavailable")
            return desktop.prepare_focus(_text(p, "app_id"), idempotency_key=key,
                                         session_id=session_id, device_id=device_id)
        if capability_id == "browser.inspect":
            _exact_keys(p, {"tab_id"})
            if browser is None:
                raise CapabilityDispatchError("browser adapter is unavailable")
            return self.services.broker.propose(
                capability_id, p, lambda value: browser.inspect(value["tab_id"]),
                idempotency_key=key,
            )
        if capability_id in {"browser.navigate", "browser.extract", "browser.click", "browser.type",
                             "browser.select", "browser.scroll", "browser.upload", "browser.download",
                             "browser.submit"}:
            _exact_keys(p, {"tab_id", "origin", "target", "value"}, optional={"target", "value"})
            if browser is None:
                raise CapabilityDispatchError("browser adapter is unavailable")
            return browser.prepare(
                _text(p, "tab_id"), capability_id.split(".", 1)[1],
                target=_optional_text(p, "target") or "", value=_optional_text(p, "value") or "",
                origin=_text(p, "origin"), idempotency_key=key,
            )
        if capability_id == "google.drive.list":
            return self._google_read(google, capability_id, p, key, {"query"}, {"query"},
                                     lambda value: google.list_drive(value.get("query", "")))
        if capability_id == "google.drive.read":
            return self._google_read(google, capability_id, p, key, {"file_id"}, set(),
                                     lambda value: google.read_drive(value["file_id"]))
        if capability_id == "google.docs.read":
            return self._google_read(google, capability_id, p, key, {"document_id"}, set(),
                                     lambda value: google.read_doc(value["document_id"]))
        if capability_id == "google.gmail.read":
            return self._google_read(google, capability_id, p, key, {"query"}, {"query"},
                                     lambda value: {"count": google.count_gmail(value.get("query", ""))})
        if capability_id == "google.calendar.read":
            return self._google_read(
                google, capability_id, p, key,
                {"calendar_id", "max_results", "time_min", "time_max"},
                {"time_min", "time_max"},
                lambda value: google.list_calendar(
                    value["calendar_id"], max_results=value["max_results"],
                    time_min=value.get("time_min"), time_max=value.get("time_max")),
            )
        if capability_id == "google.gmail.draft":
            _exact_keys(p, {"to", "subject", "body"})
            if google is None:
                raise CapabilityDispatchError("Google adapter is unavailable")
            return google.prepare_gmail_draft(
                _text(p, "to"), _text(p, "subject", allow_empty=True), _text(p, "body", allow_empty=True),
                idempotency_key=key)
        if capability_id == "google.gmail.send":
            _exact_keys(p, {"draft_id"})
            if google is None:
                raise CapabilityDispatchError("Google adapter is unavailable")
            return google.prepare_gmail_send(_text(p, "draft_id"), idempotency_key=key)
        if capability_id == "google.calendar.create":
            _exact_keys(p, {"calendar_id", "event"})
            if google is None:
                raise CapabilityDispatchError("Google adapter is unavailable")
            return google.prepare_calendar_create(
                _mapping(p, "event"), _text(p, "calendar_id"), idempotency_key=key)
        if capability_id == "google.calendar.update":
            _exact_keys(p, {"calendar_id", "event_id", "event"})
            if google is None:
                raise CapabilityDispatchError("Google adapter is unavailable")
            return google.prepare_calendar_update(
                _text(p, "event_id"), _mapping(p, "event"), _text(p, "calendar_id"),
                idempotency_key=key)
        if capability_id == "google.calendar.delete":
            _exact_keys(p, {"calendar_id", "event_id"})
            if google is None:
                raise CapabilityDispatchError("Google adapter is unavailable")
            return google.prepare_calendar_delete(
                _text(p, "event_id"), _text(p, "calendar_id"), idempotency_key=key)
        raise CapabilityDispatchError("capability has no reviewed runner")

    def _google_read(self, google, capability_id, p, key, allowed, optional, executor):
        _exact_keys(p, allowed, optional=optional)
        if google is None:
            raise CapabilityDispatchError("Google adapter is unavailable")
        return self.services.broker.propose(capability_id, p, executor, idempotency_key=key)


def fast_calendar_call(request: Request, *, idempotency_key: str,
                       now: datetime | None = None) -> TypedCapabilityCall:
    """Convert only a fully host-bound FAST calendar request into an adapter call."""
    if not isinstance(request, Request) or not RoutingPolicy().classify(request).is_fast:
        raise CapabilityDispatchError("request is not eligible for fast execution")
    capability_id = _FAST_CALENDAR.get(request.operation)
    if capability_id is None:
        raise CapabilityDispatchError("fast capability has no reviewed binding")
    p = dict(request.parameters)
    current = now or datetime.now().astimezone()
    day = _resolve_day(_text(p, "date_expression"), current.date())
    calendar_id = _text(p, "calendar_id")
    if p["action"] == "read":
        start = datetime.combine(day, time.min, tzinfo=current.tzinfo)
        end = start + timedelta(days=1)
        parameters = {
            "calendar_id": calendar_id,
            "max_results": p["max_results"],
            "time_min": start.isoformat(),
            "time_max": end.isoformat(),
        }
    else:
        if p["all_day"]:
            event = {"summary": p["title"], "start": {"date": day.isoformat()},
                     "end": {"date": (day + timedelta(days=1)).isoformat()}}
        else:
            start = datetime.combine(day, _resolve_time(_text(p, "time_expression")),
                                     tzinfo=current.tzinfo)
            end = start + timedelta(minutes=p["duration_minutes"])
            event = {"summary": p["title"], "start": {"dateTime": start.isoformat()},
                     "end": {"dateTime": end.isoformat()}}
        parameters = {"calendar_id": calendar_id, "event": event}
    return TypedCapabilityCall(capability_id, parameters, idempotency_key)


class FastCapabilityWorker:
    """One-shot durable FAST claimant using the same broker as slow typed calls."""

    def __init__(self, store: JobStore, broker: SharedCapabilityBroker, *,
                 worker_id: str = "atlas-fast", lease_seconds: float = 30.0) -> None:
        if not isinstance(store, JobStore) or not isinstance(broker, SharedCapabilityBroker):
            raise TypeError("fast worker requires JobStore and SharedCapabilityBroker")
        self.store, self.broker = store, broker
        self.worker_id, self.lease_seconds = worker_id, lease_seconds

    def run_once(self) -> JobState | None:
        claim = self.store.claim_next(self.worker_id, lane=Lane.FAST,
                                      lease_seconds=self.lease_seconds)
        if claim is None:
            return None
        try:
            call = fast_calendar_call(claim.job.request, idempotency_key=claim.job.job_id)
            result = self.broker.dispatch(call)
            return self.store.complete_success(
                claim.job.job_id, self.worker_id, claim.lease_token,
                public_payload={"code": "action_prepared" if result.status == PROPOSED else "action_completed",
                                "capability_id": result.capability_id,
                                "proposal_id": result.proposal_id,
                                "parameters_hash": result.parameters_hash},
            ).state
        except Exception:
            return self.store.complete_failure(
                claim.job.job_id, self.worker_id, claim.lease_token,
                public_payload={"code": "capability_dispatch_failed"},
            ).state


def _exact_keys(parameters: dict[str, Any], allowed: set[str], *, optional: set[str] = set()) -> None:
    if set(parameters) - allowed or not (allowed - optional).issubset(parameters):
        raise ValueError("capability parameters do not match the reviewed schema")


def _text(parameters: Mapping[str, Any], key: str, *, allow_empty: bool = False) -> str:
    value = parameters.get(key)
    if (not isinstance(value, str) or len(value) > 2_048 or any(ord(char) < 32 for char in value)
            or (not allow_empty and not value)):
        raise ValueError(f"invalid {key}")
    return value


def _optional_text(parameters: Mapping[str, Any], key: str) -> str | None:
    return None if key not in parameters or parameters[key] is None else _text(parameters, key)


def _mapping(parameters: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = parameters.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"invalid {key}")
    return copy.deepcopy(dict(value))


def _bounded_private_observation(value: Any) -> tuple[Any, bool]:
    """Remove credential-shaped fields and fit one observation into a deterministic frame."""
    truncated = [False]

    def visit(item: Any, depth: int = 0) -> Any:
        if depth >= 6:
            truncated[0] = True
            return "[DEPTH_TRUNCATED]"
        if item is None or isinstance(item, (bool, int)):
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                truncated[0] = True
                return "[NONFINITE_NUMBER]"
            return item
        if isinstance(item, str):
            if len(item) > 8_192:
                truncated[0] = True
                return item[:8_192]
            return item
        if isinstance(item, bytes):
            truncated[0] = True
            return item[:8_192].decode("utf-8", errors="replace")
        if isinstance(item, Mapping):
            result: dict[str, Any] = {}
            for index, (key, child) in enumerate(item.items()):
                if index >= 100:
                    truncated[0] = True
                    break
                key_text = str(key)
                if key_text.casefold() in _PRIVATE_SECRET_FIELDS:
                    continue
                if len(key_text) > 256:
                    truncated[0] = True
                    key_text = key_text[:190] + ":" + sha256(key_text.encode("utf-8")).hexdigest()
                if key_text in result:
                    truncated[0] = True
                    continue
                result[key_text] = visit(child, depth + 1)
            return result
        if isinstance(item, (list, tuple)):
            if len(item) > 100:
                truncated[0] = True
            return [visit(child, depth + 1) for child in item[:100]]
        truncated[0] = True
        return f"[UNSUPPORTED_{type(item).__name__.upper()}]"

    bounded = visit(copy.deepcopy(value))
    encoded = json.dumps(bounded, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_OBSERVATION_BYTES:
        digest = sha256(encoded).hexdigest()
        preview = encoded[:16_384].decode("utf-8", errors="replace")
        bounded = {"preview": preview, "source_digest": digest, "truncated": True}
        truncated[0] = True
    return bounded, truncated[0]


def _resolve_day(expression: str, today: date) -> date:
    try:
        return date.fromisoformat(expression)
    except ValueError:
        pass
    if expression == "today":
        return today
    if expression == "tomorrow":
        return today + timedelta(days=1)
    match = re.fullmatch(r"(?:(this|next) )?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)",
                         expression)
    if match is None:
        raise ValueError("unsupported calendar date")
    weekday = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday").index(match.group(2))
    delta = (weekday - today.weekday()) % 7
    if match.group(1) == "next":
        delta = delta + 7 if delta else 7
    return today + timedelta(days=delta)


def _resolve_time(expression: str) -> time:
    compact = expression.replace(".", "").strip().upper()
    for fmt in ("%I:%M %p", "%I %p", "%H:%M"):
        try:
            return datetime.strptime(compact, fmt).time()
        except ValueError:
            continue
    raise ValueError("unsupported calendar time")


__all__ = ["TypedCapabilityCall", "BrokeredCapabilityResult", "BrokeredReadObservation",
           "OBSERVABLE_READ_CAPABILITIES", "SharedCapabilityBroker",
           "FastCapabilityWorker", "CapabilityDispatchError", "fast_calendar_call"]
