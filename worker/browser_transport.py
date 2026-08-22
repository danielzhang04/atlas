"""Authenticated fail-closed transport for the typed browser protocol.

The endpoint is injected so production wiring can choose a local authenticated channel while
tests use :class:`FakeBrowserEndpoint`.  No network, native host, or browser process is started
here.  Authentication is an opaque, ambient proof supplied only for the call and is never
retained, serialized into receipts, or included in exception text.
"""
from __future__ import annotations

import copy
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Any, Callable, Mapping

from worker.browser_protocol import (
    BrowserOperation, BrowserProtocolError, BrowserRequest, BrowserResponse, ReplayGuard,
    validate_request, validate_response, new_request,
)


class BrowserTransportError(RuntimeError):
    pass


class AuthenticatedBrowserTransport:
    """Call an injected endpoint only after an opaque authentication proof is present."""

    def __init__(self, endpoint: Callable[..., Mapping[str, Any]],
                 auth_provider: Callable[[], Any], *, timeout_s: float = 10.0) -> None:
        if not callable(endpoint) or not callable(auth_provider):
            raise ValueError("authenticated browser endpoint and provider are required")
        timeout = float(timeout_s)
        if timeout <= 0 or timeout > 60:
            raise ValueError("browser transport timeout must be between 0 and 60 seconds")
        self._endpoint = endpoint
        self._auth_provider = auth_provider
        self._timeout_s = timeout
        self._guard = ReplayGuard()
        self._sequence = 0
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="atlas-browser")

    def request(self, operation: BrowserOperation | str, *, tab_id: str, origin: str,
                document_id: str, target: str = "", value: str = "") -> BrowserResponse:
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
        request = new_request(sequence=sequence, operation=operation, tab_id=tab_id,
                              origin=origin, document_id=document_id, target=target, value=value)
        return self.send(request)

    def send(self, request: BrowserRequest | Mapping[str, Any]) -> BrowserResponse:
        request = validate_request(request)
        try:
            proof = self._auth_provider()
        except Exception as exc:
            raise BrowserTransportError("browser authentication unavailable") from exc
        # A proof is intentionally opaque.  The transport only checks presence/type and does not
        # retain or print it.  Providers may return a short-lived bearer, MAC, or OS-bound proof.
        if not isinstance(proof, str) or not proof or len(proof) > 4096:
            raise BrowserTransportError("browser authentication unavailable")
        self._guard.accept(request)
        envelope = copy.deepcopy(request.as_dict())
        try:
            future = self._executor.submit(
                self._endpoint, envelope, auth=proof, timeout=self._timeout_s)
            raw = future.result(timeout=self._timeout_s)
        except FutureTimeout as exc:
            raise BrowserTransportError("browser transport timed out") from exc
        except Exception as exc:
            raise BrowserTransportError("browser transport failed") from exc
        try:
            return validate_response(raw, request)
        except BrowserProtocolError as exc:
            raise BrowserTransportError("invalid browser transport response") from exc

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


class FakeBrowserEndpoint:
    """Deterministic in-process endpoint for protocol tests; it has no external side effects."""

    def __init__(self, handler: Callable[[BrowserRequest], Any] | None = None,
                 *, expected_auth: str = "test-auth") -> None:
        self._handler = handler or (lambda _request: {"ok": True, "result": {}})
        self._expected_auth = expected_auth
        self.calls: list[dict[str, Any]] = []
        self._guard = ReplayGuard()

    def __call__(self, payload: Mapping[str, Any], *, auth: Any, timeout: float) -> dict[str, Any]:
        if auth != self._expected_auth:
            raise BrowserTransportError("browser authentication rejected")
        request = validate_request(payload)
        self._guard.accept(request)
        self.calls.append({"request_id": request.request_id, "sequence": request.sequence,
                           "operation": request.operation.value, "timeout": timeout})
        try:
            result = self._handler(request)
        except Exception as exc:
            return {"version": 1, "request_id": request.request_id,
                    "sequence": request.sequence, "ok": False,
                    "error": type(exc).__name__.lower()[:200]}
        if isinstance(result, Mapping) and "ok" in result and "result" in result:
            response = dict(result)
        else:
            response = {"ok": True, "result": result}
        response.update({"version": 1, "request_id": request.request_id,
                         "sequence": request.sequence})
        return response
