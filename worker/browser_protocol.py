"""Typed, bounded protocol shared by the Atlas browser bridge and test endpoints.

This module deliberately contains no HTTP, browser, or credential handling.  It is the
small trust-boundary contract used by an authenticated transport.  Page data is always
returned as untrusted evidence and is bounded/redacted before it crosses the boundary.
"""
from __future__ import annotations

import copy
import json
import re
import threading
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping
from urllib.parse import urlsplit


MAX_MESSAGE_BYTES = 64 * 1024
MAX_TEXT = 20_000
MAX_TARGET = 1_000
MAX_TAB_ID = 200
MAX_DOCUMENT_ID = 200
MAX_REQUEST_ID = 200
MAX_RESULT_ITEMS = 500
MAX_RESULT_BYTES = 256_000

_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
_SENSITIVE_KEYS = frozenset({
    "access_token", "refresh_token", "token", "authorization", "api_key", "password",
    "cookie", "cookies", "set-cookie", "session", "session_id", "local_storage",
    "session_storage", "headers",
})


class BrowserProtocolError(ValueError):
    """A malformed, unauthorized, stale, or replayed bridge message."""


class BrowserOperation(str, Enum):
    INSPECT = "inspect"
    NAVIGATE = "navigate"
    EXTRACT = "extract"
    CLICK = "click"
    TYPE = "type"
    SELECT = "select"
    SCROLL = "scroll"
    UPLOAD = "upload"
    DOWNLOAD = "download"
    SUBMIT = "submit"


_OPERATIONS = frozenset(item.value for item in BrowserOperation)


def _bounded_string(value: Any, limit: int, label: str) -> str:
    if not isinstance(value, str) or len(value) > limit or any(ord(char) < 32 for char in value):
        raise BrowserProtocolError(f"invalid {label}")
    return value


def _identifier(value: Any, label: str, limit: int = 200) -> str:
    if not isinstance(value, str) or len(value) > limit or not _IDENTIFIER.fullmatch(value):
        raise BrowserProtocolError(f"invalid {label}")
    return value


def canonical_origin(value: str) -> str:
    """Return the only origin spelling accepted by the bridge allowlist.

    Paths, queries, fragments, credentials, control characters, and non-web schemes are
    rejected.  Default ports are normalized away, and host names are lower-cased.  Callers
    compare this exact return value; substring or suffix matching is intentionally absent.
    """
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise BrowserProtocolError("invalid origin")
    if any(ord(char) < 32 or char.isspace() for char in value):
        raise BrowserProtocolError("invalid origin")
    parsed = urlsplit(value)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        raise BrowserProtocolError("invalid origin")
    if parsed.username is not None or parsed.password is not None:
        raise BrowserProtocolError("origin credentials are not allowed")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise BrowserProtocolError("origin must not contain a path, query, or fragment")
    try:
        port = parsed.port
    except ValueError as exc:
        raise BrowserProtocolError("invalid origin port") from exc
    hostname = parsed.hostname.lower().rstrip(".")
    if not hostname:
        raise BrowserProtocolError("invalid origin host")
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    if port is None or port == (443 if scheme == "https" else 80):
        netloc = hostname
    else:
        netloc = f"{hostname}:{port}"
    return f"{scheme}://{netloc}"


def canonical_url(value: str) -> str:
    """Validate a navigation URL and return it unchanged for the browser."""
    if not isinstance(value, str) or not value or len(value) > 8192:
        raise BrowserProtocolError("invalid navigation URL")
    if any(ord(char) < 32 or char.isspace() for char in value):
        raise BrowserProtocolError("invalid navigation URL")
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise BrowserProtocolError("navigation URL must use http or https")
    if parsed.username is not None or parsed.password is not None:
        raise BrowserProtocolError("navigation URL credentials are not allowed")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise BrowserProtocolError("invalid navigation URL port") from exc
    return value


def origin_from_url(value: str) -> str:
    """Extract a canonical origin from a validated full URL."""
    canonical_url(value)
    parsed = urlsplit(value)
    return canonical_origin(f"{parsed.scheme}://{parsed.netloc}")


def _redact(value: Any, *, depth: int = 0) -> Any:
    if depth > 10:
        return "[bounded]"
    if isinstance(value, str):
        return value[:MAX_TEXT]
    if isinstance(value, list):
        return [_redact(item, depth=depth + 1) for item in value[:MAX_RESULT_ITEMS]]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:MAX_RESULT_ITEMS]:
            name = str(key)
            if name.casefold() in _SENSITIVE_KEYS:
                continue
            result[name] = _redact(item, depth=depth + 1)
        return result
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return str(value)[:MAX_TEXT]


@dataclass(frozen=True)
class BrowserRequest:
    request_id: str
    sequence: int
    operation: BrowserOperation
    tab_id: str
    origin: str
    document_id: str
    target: str = ""
    value: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "request_id": self.request_id,
            "sequence": self.sequence,
            "operation": self.operation.value,
            "tab_id": self.tab_id,
            "origin": self.origin,
            "document_id": self.document_id,
            "target": self.target,
            "value": self.value,
        }


@dataclass(frozen=True)
class BrowserResponse:
    request_id: str
    sequence: int
    ok: bool
    result: Any = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "version": 1,
            "request_id": self.request_id,
            "sequence": self.sequence,
            "ok": self.ok,
        }
        if self.ok:
            body["result"] = _redact(copy.deepcopy(self.result))
        else:
            body["error"] = _bounded_string(self.error or "browser operation failed", 200, "error")
        return body


def validate_request(payload: Mapping[str, Any] | BrowserRequest) -> BrowserRequest:
    if isinstance(payload, BrowserRequest):
        # Dataclasses are not a validation bypass: callers can construct one directly, so run
        # the same field and operation checks as dictionary payloads below.
        request = BrowserRequest(
            request_id=_identifier(payload.request_id, "request id", MAX_REQUEST_ID),
            sequence=payload.sequence,
            operation=BrowserOperation(payload.operation),
            tab_id=_identifier(payload.tab_id, "tab id", MAX_TAB_ID),
            origin=canonical_origin(payload.origin),
            document_id=_identifier(payload.document_id, "document id", MAX_DOCUMENT_ID),
            target=_bounded_string(payload.target, MAX_TARGET, "target"),
            value=_bounded_string(payload.value, MAX_TEXT, "value"),
        )
    else:
        if not isinstance(payload, Mapping):
            raise BrowserProtocolError("request must be an object")
        allowed = {"version", "request_id", "sequence", "operation", "tab_id", "origin",
                   "document_id", "target", "value"}
        if set(payload) != allowed or payload.get("version") != 1:
            raise BrowserProtocolError("request schema mismatch")
        try:
            operation = BrowserOperation(payload["operation"])
        except (ValueError, TypeError) as exc:
            raise BrowserProtocolError("unsupported browser operation") from exc
        request = BrowserRequest(
            request_id=_identifier(payload["request_id"], "request id", MAX_REQUEST_ID),
            sequence=payload["sequence"], operation=operation,
            tab_id=_identifier(payload["tab_id"], "tab id", MAX_TAB_ID),
            origin=canonical_origin(payload["origin"]),
            document_id=_identifier(payload["document_id"], "document id", MAX_DOCUMENT_ID),
            target=_bounded_string(payload["target"], MAX_TARGET, "target"),
            value=_bounded_string(payload["value"], MAX_TEXT, "value"),
        )
    if not isinstance(request.sequence, int) or isinstance(request.sequence, bool) or request.sequence < 1:
        raise BrowserProtocolError("invalid request sequence")
    if request.operation is BrowserOperation.NAVIGATE:
        target_origin = origin_from_url(request.value)
        if target_origin != request.origin:
            raise BrowserProtocolError("navigation target is outside the bound origin")
    elif request.operation in {BrowserOperation.CLICK, BrowserOperation.TYPE, BrowserOperation.SELECT,
                               BrowserOperation.UPLOAD, BrowserOperation.DOWNLOAD,
                               BrowserOperation.SUBMIT} and not request.target:
        raise BrowserProtocolError("target is required for this operation")
    if request.operation is BrowserOperation.INSPECT and (request.target or request.value):
        raise BrowserProtocolError("inspect does not accept target or value")
    # JSON serialization is an explicit message-size gate, not an assumption about callers.
    try:
        encoded = json.dumps(request.as_dict(), ensure_ascii=False, separators=(",", ":"),
                             allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise BrowserProtocolError("request is not JSON-compatible") from exc
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise BrowserProtocolError("request exceeds message size limit")
    return request


def validate_response(payload: Mapping[str, Any], request: BrowserRequest) -> BrowserResponse:
    if not isinstance(payload, Mapping):
        raise BrowserProtocolError("response must be an object")
    if set(payload) - {"version", "request_id", "sequence", "ok", "result", "error"}:
        raise BrowserProtocolError("response schema mismatch")
    if payload.get("version") != 1 or payload.get("request_id") != request.request_id:
        raise BrowserProtocolError("response is not bound to request")
    if payload.get("sequence") != request.sequence or not isinstance(payload.get("ok"), bool):
        raise BrowserProtocolError("response sequence mismatch")
    if payload["ok"]:
        if "error" in payload:
            raise BrowserProtocolError("successful response cannot contain an error")
        result = _redact(copy.deepcopy(payload.get("result")))
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > MAX_RESULT_BYTES:
            raise BrowserProtocolError("response exceeds size limit")
        return BrowserResponse(request.request_id, request.sequence, True, result=result)
    error = payload.get("error")
    if not isinstance(error, str) or not error or len(error) > 200 or any(ord(c) < 32 for c in error):
        raise BrowserProtocolError("invalid response error")
    return BrowserResponse(request.request_id, request.sequence, False, error=error)


class ReplayGuard:
    """Reject duplicate request IDs and non-increasing sequence numbers atomically."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen: set[str] = set()
        self._last_sequence = 0

    def accept(self, request: BrowserRequest) -> None:
        request = validate_request(request)
        with self._lock:
            if request.request_id in self._seen or request.sequence <= self._last_sequence:
                raise BrowserProtocolError("replayed browser request")
            self._seen.add(request.request_id)
            self._last_sequence = request.sequence


def new_request(*, sequence: int, operation: BrowserOperation | str, tab_id: str,
                origin: str, document_id: str, target: str = "", value: str = "") -> BrowserRequest:
    request = BrowserRequest(
        request_id=uuid.uuid4().hex, sequence=sequence,
        operation=BrowserOperation(operation), tab_id=tab_id,
        origin=canonical_origin(origin), document_id=document_id,
        target=target, value=value)
    return validate_request(request)
