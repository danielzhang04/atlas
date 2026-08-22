"""Authenticated loopback IPC for private Atlas read observations.

The subscription worker owns this server and the single in-process capability broker. A strict MCP
child receives one short-lived bearer capability and may request only the read operations that
``SharedCapabilityBroker.dispatch_observed`` already admits. No observation is persisted here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
import re
import secrets
import threading
import time
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit
from uuid import UUID

from .capability_runner import (
    BrokeredReadObservation,
    CapabilityDispatchError,
    OBSERVABLE_READ_CAPABILITIES,
    TypedCapabilityCall,
)


MAX_IPC_REQUEST_BYTES = 8_192
MAX_IPC_REJECT_DRAIN_BYTES = 16_384
MAX_IPC_RESPONSE_BYTES = 65_536
IPC_SOCKET_TIMEOUT_SECONDS = 5.0
_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_SAFE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,256}")


class ObservedDispatcher(Protocol):
    def dispatch_observed(self, call: TypedCapabilityCall) -> BrokeredReadObservation:
        ...


@dataclass(frozen=True, slots=True)
class BrokerReadReceipt:
    """Non-content metadata retained in memory for the host evidence gate."""

    request_id: int
    capability_id: str
    proposal_id: str
    parameters_hash: str
    content_digest: str
    truncated: bool

    def __post_init__(self) -> None:
        if isinstance(self.request_id, bool) or not isinstance(self.request_id, int) or self.request_id < 1:
            raise ValueError("invalid broker receipt request id")
        if self.capability_id not in OBSERVABLE_READ_CAPABILITIES:
            raise ValueError("invalid broker receipt capability")
        if not isinstance(self.proposal_id, str) or _SAFE_ID.fullmatch(self.proposal_id) is None:
            raise ValueError("invalid broker receipt proposal id")
        if not isinstance(self.parameters_hash, str) or _DIGEST.fullmatch(self.parameters_hash) is None:
            raise ValueError("invalid broker receipt parameters hash")
        if not isinstance(self.content_digest, str) or _DIGEST.fullmatch(self.content_digest) is None:
            raise ValueError("invalid broker receipt content digest")
        if not isinstance(self.truncated, bool):
            raise TypeError("invalid broker receipt truncation flag")


@dataclass(frozen=True, slots=True)
class BrokerIpcEndpoint:
    url: str
    job_id: str
    expires_at: float
    allowed_capabilities: tuple[str, ...]
    token: str = field(repr=False)

    def __post_init__(self) -> None:
        try:
            UUID(self.job_id)
        except (TypeError, ValueError):
            raise ValueError("invalid broker IPC job id") from None
        if not isinstance(self.url, str):
            raise ValueError("invalid broker IPC URL")
        parts = urlsplit(self.url)
        try:
            port = parts.port
        except ValueError:
            raise ValueError("invalid broker IPC URL") from None
        if (
            parts.scheme != "http"
            or parts.hostname != "127.0.0.1"
            or port is None
            or not 1 <= port <= 65_535
            or parts.netloc != f"127.0.0.1:{port}"
            or parts.path != "/v1/read"
            or parts.query
            or parts.fragment
            or parts.username is not None
            or parts.password is not None
        ):
            raise ValueError("invalid broker IPC URL")
        if not isinstance(self.token, str) or _TOKEN.fullmatch(self.token) is None:
            raise ValueError("invalid broker IPC token")
        if (isinstance(self.expires_at, bool) or not isinstance(self.expires_at, (int, float))
                or not math.isfinite(float(self.expires_at))):
            raise ValueError("invalid broker IPC expiry")
        if (
            not isinstance(self.allowed_capabilities, tuple)
            or not self.allowed_capabilities
            or tuple(sorted(set(self.allowed_capabilities))) != self.allowed_capabilities
            or not set(self.allowed_capabilities).issubset(OBSERVABLE_READ_CAPABILITIES)
        ):
            raise ValueError("invalid broker IPC capability scope")

    def mcp_environment(self) -> Mapping[str, str]:
        return MappingProxyType({
            "ATLAS_BROKER_URL": self.url,
            "ATLAS_BROKER_TOKEN": self.token,
            "ATLAS_BROKER_JOB_ID": self.job_id,
            "ATLAS_BROKER_CAPABILITIES": ",".join(self.allowed_capabilities),
        })


class BrokerIpcServer:
    """Single-job, single-threaded, bounded loopback capability server."""

    def __init__(self, dispatcher: ObservedDispatcher, *, job_id: str,
                 allowed_capabilities: frozenset[str],
                 ttl_seconds: float = 900.0, max_requests: int = 64,
                 token_factory: Callable[[], str] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if not callable(getattr(dispatcher, "dispatch_observed", None)):
            raise TypeError("broker IPC requires an observed dispatcher")
        try:
            UUID(job_id)
        except (TypeError, ValueError):
            raise ValueError("invalid broker IPC job id") from None
        if (isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float))
                or not math.isfinite(float(ttl_seconds)) or not 10 <= float(ttl_seconds) <= 3_600):
            raise ValueError("invalid broker IPC TTL")
        if (isinstance(max_requests, bool) or not isinstance(max_requests, int)
                or not 1 <= max_requests <= 256):
            raise ValueError("invalid broker IPC request bound")
        if (
            not isinstance(allowed_capabilities, frozenset)
            or not allowed_capabilities
            or not allowed_capabilities.issubset(OBSERVABLE_READ_CAPABILITIES)
        ):
            raise ValueError("invalid broker IPC capability scope")
        if not callable(clock):
            raise TypeError("broker IPC clock must be callable")
        self._dispatcher = dispatcher
        self._job_id = job_id
        self._ttl_seconds = float(ttl_seconds)
        self._max_requests = max_requests
        self._allowed_capabilities = allowed_capabilities
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(32))
        self._clock = clock
        self._httpd: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._token: str | None = None
        self._expires_at: float | None = None
        self._request_count = 0
        self._receipts: list[BrokerReadReceipt] = []
        self._lock = threading.Lock()

    @property
    def receipts(self) -> tuple[BrokerReadReceipt, ...]:
        with self._lock:
            return tuple(self._receipts)

    def start(self) -> BrokerIpcEndpoint:
        if self._httpd is not None:
            raise RuntimeError("broker IPC server is already running")
        token = self._token_factory()
        if not isinstance(token, str) or _TOKEN.fullmatch(token) is None:
            raise ValueError("broker IPC token factory returned an invalid token")
        now = float(self._clock())
        if not math.isfinite(now):
            raise ValueError("broker IPC clock returned an invalid timestamp")
        self._token = token
        self._expires_at = now + self._ttl_seconds
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "AtlasBroker"
            sys_version = ""

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
                owner._handle(self)

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
                owner._reply(self, 405, {"code": "method_not_allowed"})

            def log_message(self, _format: str, *args: Any) -> None:
                return

        httpd = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=httpd.serve_forever, name="atlas-broker-ipc", daemon=True)
        self._httpd, self._thread = httpd, thread
        thread.start()
        port = int(httpd.server_address[1])
        return BrokerIpcEndpoint(
            f"http://127.0.0.1:{port}/v1/read", self._job_id, self._expires_at,
            tuple(sorted(self._allowed_capabilities)), token,
        )

    def close(self) -> None:
        httpd, thread = self._httpd, self._thread
        self._httpd = None
        self._thread = None
        self._token = None
        self._expires_at = None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if thread is not None:
            thread.join(timeout=5.0)

    def __enter__(self) -> "BrokerIpcServer":
        self.start()
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        if handler.path != "/v1/read":
            self._reply(handler, 404, {"code": "not_found"})
            return
        authorization = handler.headers.get("Authorization", "")
        expected = f"Bearer {self._token or ''}"
        authorized = hmac.compare_digest(authorization, expected)
        if handler.headers.get("Transfer-Encoding") is not None:
            self._reply(handler, 400, {"code": "invalid_request"})
            return
        content_type = handler.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._reply(handler, 415, {"code": "unsupported_media_type"})
            return
        try:
            length = int(handler.headers.get("Content-Length", ""))
        except ValueError:
            self._reply(handler, 400, {"code": "invalid_request"})
            return
        if length < 1:
            self._reply(handler, 400, {"code": "invalid_request"})
            return
        if length > MAX_IPC_REQUEST_BYTES:
            if authorized and length <= MAX_IPC_REJECT_DRAIN_BYTES:
                try:
                    handler.connection.settimeout(IPC_SOCKET_TIMEOUT_SECONDS)
                    handler.rfile.read(length)
                except (OSError, TimeoutError):
                    pass
            self._reply(handler, 413, {"code": "request_too_large"})
            return
        try:
            # A bad bearer gets only a very small drain window. Consuming a valid bounded request
            # before replying prevents Windows from replacing deliberate HTTP errors with a TCP
            # abort, without letting an unauthenticated local peer occupy the server for 5 seconds.
            handler.connection.settimeout(IPC_SOCKET_TIMEOUT_SECONDS if authorized else 0.25)
            raw = handler.rfile.read(length)
        except (OSError, TimeoutError):
            self._reply(handler, 400, {"code": "invalid_request"})
            return
        if len(raw) != length:
            self._reply(handler, 400, {"code": "invalid_request"})
            return
        if not authorized:
            self._reply(handler, 403, {"code": "forbidden"})
            return
        # Consume the bounded request body before a terminal rejection. On Windows, replying and
        # closing while the client is still sending can turn a deliberate 410/429 into WSAECONNABORTED.
        now = float(self._clock())
        if not math.isfinite(now) or self._expires_at is None or now >= self._expires_at:
            self._reply(handler, 410, {"code": "capability_expired"})
            return
        with self._lock:
            if self._request_count >= self._max_requests:
                self._reply(handler, 429, {"code": "request_budget_exhausted"})
                return
            self._request_count += 1
            request_id = self._request_count
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._reply(handler, 400, {"code": "invalid_request"})
            return
        if not isinstance(value, dict) or set(value) != {"job_id", "capability_id", "parameters"}:
            self._reply(handler, 400, {"code": "invalid_request"})
            return
        if value.get("job_id") != self._job_id:
            self._reply(handler, 403, {"code": "job_mismatch"})
            return
        if value.get("capability_id") not in self._allowed_capabilities:
            self._reply(handler, 400, {"code": "capability_rejected"})
            return
        try:
            call = TypedCapabilityCall(
                value["capability_id"], value["parameters"],
                f"ipc:{self._job_id}:{request_id}",
            )
            observation = self._dispatcher.dispatch_observed(call)
        except (TypeError, ValueError, CapabilityDispatchError):
            self._reply(handler, 400, {"code": "capability_rejected"})
            return
        receipt = BrokerReadReceipt(
            request_id, observation.capability_id, observation.proposal_id,
            observation.parameters_hash, observation.content_digest, observation.truncated,
        )
        with self._lock:
            self._receipts.append(receipt)
        response = {
            "version": 1,
            "job_id": self._job_id,
            "request_id": request_id,
            "capability_id": observation.capability_id,
            "proposal_id": observation.proposal_id,
            "parameters_hash": observation.parameters_hash,
            "content": observation.content,
            "content_digest": observation.content_digest,
            "truncated": observation.truncated,
        }
        self._reply(handler, 200, response)

    @staticmethod
    def _reply(handler: BaseHTTPRequestHandler, status: int, body: Mapping[str, Any]) -> None:
        raw = json.dumps(dict(body), sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(raw) > MAX_IPC_RESPONSE_BYTES:
            status = 500
            raw = b'{"code":"response_too_large"}'
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(raw)))
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(raw)


__all__ = ["BrokerIpcEndpoint", "BrokerIpcServer", "BrokerReadReceipt", "ObservedDispatcher"]
