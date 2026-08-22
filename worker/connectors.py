"""Bounded browser and Google connector contracts; transports are always injected."""
from __future__ import annotations

import copy
import re
from typing import Any, Callable, Protocol
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_RESPONSE_BYTES, MAX_TARGET, MAX_VALUE = 256_000, 1_000, 20_000
_ID = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
_TOKEN_FIELDS = frozenset({"access_token", "refresh_token", "token", "authorization", "api_key",
                           "password", "cookie", "cookies", "set-cookie", "session",
                           "session_id", "local_storage", "session_storage", "headers"})


class ConnectorError(RuntimeError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def urllib_json_transport(*, method: str, url: str, headers: dict[str, str],
                          json: dict | None, timeout: float) -> dict[str, Any]:
    """Bounded JSON transport for trusted runtime wiring; redirects are refused."""
    payload = None
    request_headers = dict(headers)
    if json is not None:
        import json as json_module
        payload = json_module.dumps(json, allow_nan=False).encode("utf-8")
        request_headers["content-type"] = "application/json"
    request = Request(url, data=payload, headers=request_headers, method=method)
    with build_opener(_NoRedirect).open(request, timeout=timeout) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ConnectorError("connector response exceeded size limit")
        if not raw:
            body: Any = {}
        else:
            import json as json_module
            body = json_module.loads(raw.decode("utf-8"))
        return {"status": response.status, "body": body}


def _id(value: str, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"invalid {label}")
    return value


def _http_url(value: str, label: str, *, origin_only: bool = False) -> str:
    if not isinstance(value, str) or len(value) > 2048 or any(ord(ch) < 32 for ch in value):
        raise ValueError(f"invalid {label}")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(f"invalid {label}")
    if origin_only and (parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("origin must not contain a path, query, or fragment")
    return value.rstrip("/") if origin_only else value


def _bounded(value: Any) -> Any:
    if isinstance(value, bytes): value = value[:MAX_RESPONSE_BYTES].decode("utf-8", errors="replace")
    if isinstance(value, str): return value[:MAX_RESPONSE_BYTES]
    if isinstance(value, list): return [_bounded(item) for item in value[:500]]
    if isinstance(value, dict):
        return {str(k): _bounded(v) for k, v in list(value.items())[:500] if str(k).casefold() not in _TOKEN_FIELDS}
    return value


class _Client:
    def __init__(self, base_url: str, transport: Callable[..., Any], *, timeout_s: float = 10.0) -> None:
        self._base_url, self._transport = _http_url(base_url, "connector base URL", origin_only=True), transport
        self._timeout_s = float(timeout_s)
        if self._timeout_s <= 0: raise ValueError("timeout must be positive")

    def _request(self, method: str, path: str, *, payload: dict | None = None,
                 query: dict[str, str] | None = None, base_url: str | None = None,
                 extra_headers: dict[str, str] | None = None) -> Any:
        if not path.startswith("/") or "//" in path: raise ValueError("invalid connector path")
        url = (base_url or self._base_url) + path + (("?" + urlencode(query)) if query else "")
        headers = {"accept": "application/json"}
        if extra_headers:
            headers.update(extra_headers)
        try:
            response = self._transport(method=method, url=url, headers=headers, json=copy.deepcopy(payload), timeout=self._timeout_s)
        except TimeoutError as exc: raise ConnectorError("connector request timed out") from exc
        except Exception as exc: raise ConnectorError("connector request failed") from exc
        status = response.get("status", 200) if isinstance(response, dict) else getattr(response, "status", 200)
        body = response.get("body", response) if isinstance(response, dict) else getattr(response, "body", response)
        if not 200 <= int(status) < 300: raise ConnectorError(f"connector returned HTTP {status}")
        return _bounded(body)


class BrowserConnector(_Client):
    """Local browser bridge actions are bound to a configured origin and tab ID."""
    ALLOWED_ACTIONS = frozenset({"navigate", "extract", "click", "type", "select", "scroll", "upload", "download", "submit"})

    def __init__(self, base_url: str, transport: Callable[..., Any], *, allowed_origins: set[str] | None = None, timeout_s: float = 10.0) -> None:
        bridge = urlsplit(_http_url(base_url, "browser bridge URL", origin_only=True))
        if bridge.scheme == "http" and bridge.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("plain HTTP browser bridges must be loopback-only")
        super().__init__(base_url, transport, timeout_s=timeout_s)
        self._allowed_origins = frozenset(_http_url(x, "allowed origin", origin_only=True) for x in (allowed_origins or set()))

    def inspect_tab(self, tab_id: str) -> Any:
        raw = self._request("GET", f"/v1/tabs/{quote(_id(tab_id, 'tab id'), safe='')}")
        if not isinstance(raw, dict): raise ConnectorError("browser bridge returned invalid tab evidence")
        allowed = {"tab_id", "document_id", "title", "url", "origin", "visible_text",
                   "accessibility_tree", "selection"}
        evidence = {key: _bounded(value) for key, value in raw.items() if key in allowed}
        return {"source": "browser", "trust": "untrusted", "scope": "visible_tab_read",
                "account": "paired_browser_profile",
                "provenance": {"connector": "browser_bridge", "tab_id": tab_id,
                               "document_id": evidence.get("document_id")},
                "instruction_policy": "evidence_only", "evidence": evidence}

    def action(self, tab_id: str, action: str, *, target: str = "", value: str = "", origin: str,
               document_id: str) -> Any:
        payload = self.validate_action(tab_id, action, target=target, value=value, origin=origin)
        # This is an optimistic-concurrency contract: the trusted bridge must compare both fields
        # atomically with its current document immediately before dispatching the interaction.
        payload["expected_origin"] = payload.pop("origin")
        payload["expected_document_id"] = _id(document_id, "browser document id")
        return self._request("POST", f"/v1/tabs/{quote(_id(tab_id, 'tab id'), safe='')}/actions",
                             payload=payload)

    def validate_action(self, tab_id: str, action: str, *, target: str = "", value: str = "",
                        origin: str) -> dict[str, str]:
        _id(tab_id, "tab id")
        if action not in self.ALLOWED_ACTIONS or not isinstance(target, str) or not isinstance(value, str) or len(target) > MAX_TARGET or len(value) > MAX_VALUE or any(ord(c) < 32 for c in target):
            raise ValueError("invalid browser interaction")
        bound_origin = _http_url(origin, "origin", origin_only=True)
        if not self._allowed_origins: raise ValueError("browser origin allowlist is empty")
        if bound_origin not in self._allowed_origins: raise ValueError("origin is not allowlisted")
        if action == "navigate": _http_url(value, "navigation URL")
        if action == "upload": _id(value, "approved file alias")
        return {"action": action, "target": target, "value": value, "origin": bound_origin}

    def attest_tab_origin(self, tab_id: str, expected_origin: str) -> Any:
        state = self.inspect_tab(tab_id)
        evidence = state.get("evidence", {}) if isinstance(state, dict) else {}
        observed = evidence.get("origin")
        if not isinstance(observed, str) and isinstance(evidence.get("url"), str):
            parsed = urlsplit(evidence["url"])
            observed = f"{parsed.scheme}://{parsed.netloc}"
        if observed != expected_origin: raise ConnectorError("browser tab origin changed or was not attested")
        if not isinstance(evidence.get("document_id"), str):
            raise ConnectorError("browser bridge did not supply a document identity")
        return state

    def navigate(self, tab_id, url, *, origin, document_id): return self.action(tab_id, "navigate", value=url, origin=origin, document_id=document_id)
    def extract(self, tab_id, target="", *, origin, document_id): return self.action(tab_id, "extract", target=target, origin=origin, document_id=document_id)
    def click(self, tab_id, target, *, origin, document_id): return self.action(tab_id, "click", target=target, origin=origin, document_id=document_id)
    def type(self, tab_id, target, value, *, origin, document_id): return self.action(tab_id, "type", target=target, value=value, origin=origin, document_id=document_id)
    def select(self, tab_id, target, value, *, origin, document_id): return self.action(tab_id, "select", target=target, value=value, origin=origin, document_id=document_id)
    def scroll(self, tab_id, value, *, origin, document_id): return self.action(tab_id, "scroll", value=value, origin=origin, document_id=document_id)
    def upload(self, tab_id, target, file_alias, *, origin, document_id): return self.action(tab_id, "upload", target=target, value=file_alias, origin=origin, document_id=document_id)
    def download(self, tab_id, target, *, origin, document_id): return self.action(tab_id, "download", target=target, origin=origin, document_id=document_id)
    def submit(self, tab_id, target, *, origin, document_id): return self.action(tab_id, "submit", target=target, origin=origin, document_id=document_id)


class GoogleBrokerInvoke(Protocol):
    """Credential-free call seam implemented by a separate local Google broker client."""

    def __call__(self, operation: str, parameters: dict[str, Any], *, binding: str | None) -> Any:
        ...


class GoogleBrokerConnector:
    """Typed Google client whose process never receives OAuth material.

    The injected invoker is expected to cross a reviewed local IPC boundary.  Only closed operation
    names and bounded ordinary parameters cross that boundary; the credential broker owns PKCE,
    token storage, refresh, account selection, and request authorization.
    """

    _OPERATIONS = frozenset({
        "connection.bind", "drive.list", "drive.read", "docs.read", "docs.update",
        "gmail.count", "gmail.draft.create", "gmail.draft.read", "gmail.draft.send",
        "calendar.list", "calendar.read", "calendar.create", "calendar.update",
        "calendar.delete",
    })

    def __init__(self, invoke: GoogleBrokerInvoke) -> None:
        if not callable(invoke):
            raise TypeError("Google broker invoker must be callable")
        self._invoke = invoke

    def _call(self, operation: str, parameters: dict[str, Any], *, binding: str | None = None) -> Any:
        if operation not in self._OPERATIONS:
            raise ValueError("Google broker operation is not allowlisted")
        if not isinstance(parameters, dict) or len(parameters) > 100:
            raise ValueError("invalid Google broker parameters")
        try:
            return _bounded(self._invoke(operation, copy.deepcopy(parameters), binding=binding))
        except Exception:
            raise ConnectorError("Google credential broker request failed") from None

    def bind_connection(self) -> Callable[..., Any]:
        result = self._call("connection.bind", {})
        binding = result.get("binding") if isinstance(result, dict) else None
        if not isinstance(binding, str) or _ID.fullmatch(binding) is None:
            raise ConnectorError("Google credential broker returned an invalid binding")

        def bound(operation: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
            kwargs["_broker_binding"] = binding
            return operation(*args, **kwargs)

        return bound

    def require_connection(self) -> None:
        self.bind_connection()

    @staticmethod
    def validate_calendar_event(event: dict[str, Any], calendar_id: str = "primary") -> None:
        _id(calendar_id, "calendar id")
        if not isinstance(event, dict) or not event or len(event) > 100:
            raise ValueError("invalid calendar event")

    @staticmethod
    def gmail_rfc822(to: str, subject: str, body: str) -> None:
        if (not isinstance(to, str) or not to or len(to) > 500
                or any(char in to for char in "\r\n")
                or not isinstance(subject, str) or len(subject) > 1_000
                or any(char in subject for char in "\r\n")
                or not isinstance(body, str) or len(body) > 100_000):
            raise ValueError("invalid Gmail draft")

    @staticmethod
    def _require_etag(expected_etag: str | None) -> str:
        if (not isinstance(expected_etag, str) or not expected_etag or len(expected_etag) > 1_000
                or any(ord(char) < 32 for char in expected_etag)):
            raise ValueError("invalid expected version")
        return expected_etag

    def list_drive_files(self, query="", *, _broker_binding=None):
        if not isinstance(query, str) or len(query) > 1000:
            raise ValueError("Drive query is too long")
        return self._call("drive.list", {"query": query}, binding=_broker_binding)

    def read_drive_file(self, file_id, *, _broker_binding=None):
        return self._call("drive.read", {"file_id": _id(file_id, "file id")}, binding=_broker_binding)

    def read_doc(self, document_id, *, _broker_binding=None):
        return self._call("docs.read", {"document_id": _id(document_id, "document id")}, binding=_broker_binding)

    def update_doc(self, document_id, requests, *, _broker_binding=None):
        if not isinstance(requests, list) or not requests or len(requests) > 100:
            raise ValueError("invalid Docs update requests")
        return self._call("docs.update", {"document_id": _id(document_id, "document id"),
                                          "requests": requests}, binding=_broker_binding)

    def count_gmail(self, query="", *, _broker_binding=None):
        if not isinstance(query, str) or len(query) > 1000:
            raise ValueError("invalid Gmail query")
        return self._call("gmail.count", {"query": query}, binding=_broker_binding)

    def create_gmail_draft(self, to, subject, body, *, _broker_binding=None):
        self.gmail_rfc822(to, subject, body)
        return self._call("gmail.draft.create", {"to": to, "subject": subject, "body": body},
                          binding=_broker_binding)

    def read_gmail_draft(self, draft_id, *, _broker_binding=None):
        return self._call("gmail.draft.read", {"draft_id": _id(draft_id, "draft id")}, binding=_broker_binding)

    def send_gmail_draft(self, draft_id, *, _broker_binding=None):
        return self._call("gmail.draft.send", {"draft_id": _id(draft_id, "draft id")}, binding=_broker_binding)

    def list_calendar_events(self, calendar_id="primary", *, max_results=100,
                             time_min=None, time_max=None, _broker_binding=None):
        if isinstance(max_results, bool) or not 1 <= max_results <= 500:
            raise ValueError("invalid calendar result limit")
        parameters = {"calendar_id": _id(calendar_id, "calendar id"), "max_results": max_results}
        if time_min is not None:
            parameters["time_min"] = _bounded_iso_timestamp(time_min)
        if time_max is not None:
            parameters["time_max"] = _bounded_iso_timestamp(time_max)
        return self._call("calendar.list", parameters, binding=_broker_binding)

    def read_calendar_event(self, event_id, calendar_id="primary", *, _broker_binding=None):
        return self._call("calendar.read", {"event_id": _id(event_id, "event id"),
                                             "calendar_id": _id(calendar_id, "calendar id")},
                          binding=_broker_binding)

    def create_calendar_event(self, event, calendar_id="primary", *, _broker_binding=None):
        self.validate_calendar_event(event, calendar_id)
        return self._call("calendar.create", {"event": event, "calendar_id": calendar_id},
                          binding=_broker_binding)

    def update_calendar_event(self, event_id, event, calendar_id="primary", *,
                              expected_etag=None, _broker_binding=None):
        self.validate_calendar_event(event, calendar_id)
        self._require_etag(expected_etag)
        return self._call("calendar.update", {"event_id": _id(event_id, "event id"),
                                               "event": event, "calendar_id": calendar_id,
                                               "expected_etag": expected_etag}, binding=_broker_binding)

    def delete_calendar_event(self, event_id, calendar_id="primary", *, expected_etag=None,
                              _broker_binding=None):
        self._require_etag(expected_etag)
        return self._call("calendar.delete", {"event_id": _id(event_id, "event id"),
                                               "calendar_id": calendar_id,
                                               "expected_etag": expected_etag}, binding=_broker_binding)


def _bounded_iso_timestamp(value: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 64
            or any(ord(char) < 32 for char in value)):
        raise ValueError("invalid calendar timestamp")
    return value
