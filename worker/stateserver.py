"""Serve the loopback Atlas state, work, MCP, and command-center surface."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import hmac
import inspect
import json
import logging
import math
from pathlib import Path
import re
import secrets
import time
from typing import Any, Callable
from urllib.parse import quote

from aiohttp import web

__all__ = ["HEADER", "HOST", "PairingAuthorizer", "StateServer", "pairing_url", "start"]

logger = logging.getLogger("atlas.stateserver")

HOST = "127.0.0.1"
HEADER = "x-atlas-action-token"
UI_ROOT = Path(__file__).resolve().parents[1] / "ui"
UI_ASSETS = {
    "/ui/styles.css": ("styles.css", "text/css"),
    "/ui/app.js": ("app.js", "application/javascript"),
    "/ui/favicon.svg": ("favicon.svg", "image/svg+xml"),
}
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
    "form-action 'none'"
)
BODY_LIMIT = 8_192
JOB_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.I,
)
JOB_FIELDS = (
    "id", "title", "status", "session_id", "created_at", "updated_at", "summary", "error",
)
MCP_FIELDS = ("name", "connected", "tools", "error")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PairingAuthorizer:
    """Mint one in-memory bearer from a one-use browser-fragment token."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        token: str | None = None,
        ttl_s: float = 43_200,
    ) -> None:
        self._clock = clock
        self._ttl_s = float(ttl_s)
        self._pairing_token: str | None = token or secrets.token_urlsafe(24)
        self._bearers: dict[str, float] = {}
        self._failures = 0

    @property
    def pairing_token(self) -> str:
        if self._pairing_token is None:
            raise PermissionError("pairing token has already been consumed")
        return self._pairing_token

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def pair(self, token: str) -> str:
        if self._failures >= 8:
            raise PermissionError("pairing attempts exhausted; restart Atlas")
        if (
            self._pairing_token is None
            or not isinstance(token, str)
            or not hmac.compare_digest(self._digest(token), self._digest(self._pairing_token))
        ):
            self._failures += 1
            raise PermissionError("invalid pairing token")
        bearer = secrets.token_urlsafe(32)
        self._bearers = {self._digest(bearer): self._clock() + self._ttl_s}
        self._pairing_token = None
        self._failures = 0
        return bearer

    def authorize(self, bearer: str | None) -> None:
        if not bearer:
            raise PermissionError("Atlas UI is not paired")
        expires = self._bearers.get(self._digest(bearer))
        if expires is None or self._clock() >= expires:
            raise PermissionError("Atlas UI pairing is invalid or expired")


def pairing_url(authorizer: PairingAuthorizer, port: int) -> str | None:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65_535:
        return None
    try:
        token = authorizer.pairing_token
    except PermissionError:
        return None
    return f"http://{HOST}:{port}/#pair={quote(token, safe='')}"


def _security_middleware(port: Callable[[], int]):
    @web.middleware
    async def middleware(request: web.Request, handler) -> web.StreamResponse:
        try:
            serving_port = port()
            allowed_hosts = {
                f"127.0.0.1:{serving_port}",
                f"localhost:{serving_port}",
            }
            if request.headers.get("Host") not in allowed_hosts:
                raise web.HTTPForbidden(text="loopback Host required")
            response = await handler(request)
        except web.HTTPException as exc:
            _add_security_headers(exc)
            raise
        _add_security_headers(response)
        return response

    return middleware


def _add_security_headers(response: web.StreamResponse) -> None:
    response.headers["content-security-policy"] = CONTENT_SECURITY_POLICY
    response.headers["x-content-type-options"] = "nosniff"
    response.headers["referrer-policy"] = "no-referrer"
    response.headers["x-frame-options"] = "DENY"


async def _provide(provider, *args):
    if inspect.iscoroutinefunction(provider):
        return await provider(*args)
    result = await asyncio.to_thread(provider, *args)
    if inspect.isawaitable(result):
        return await result
    return result


def _finite_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _safe_job(value: Any) -> dict[str, Any] | None:
    if hasattr(value, "to_public"):
        value = value.to_public()
    if not isinstance(value, dict):
        return None
    if not all(isinstance(value.get(name), str) for name in ("id", "title", "status")):
        return None
    if not all(_finite_number(value.get(name)) for name in ("created_at", "updated_at")):
        return None
    result: dict[str, Any] = {}
    for field in JOB_FIELDS:
        item = value.get(field)
        if field in {"created_at", "updated_at"}:
            result[field] = item
        elif item is None:
            result[field] = None
        elif isinstance(item, str):
            result[field] = item[:2_048]
    return result if set(result) == set(JOB_FIELDS) else None


def _safe_jobs(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [job for item in value[:100] if (job := _safe_job(item)) is not None]


def _safe_events(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    events = []
    for item in value[:2_000]:
        get = item.get if isinstance(item, dict) else lambda name: getattr(item, name, None)
        sequence = get("sequence")
        timestamp = get("timestamp")
        kind = get("kind")
        text = get("text")
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence < 1
            or not _finite_number(timestamp)
            or kind not in {"state", "output"}
            or not isinstance(text, str)
        ):
            continue
        events.append({
            "sequence": sequence,
            "timestamp": timestamp,
            "kind": kind,
            "text": text[:2_048],
        })
    return events


def _safe_mcp(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    servers = []
    for item in value[:32]:
        if not isinstance(item, dict):
            continue
        if (
            not isinstance(item.get("name"), str)
            or not isinstance(item.get("connected"), bool)
            or isinstance(item.get("tools"), bool)
            or not isinstance(item.get("tools"), int)
        ):
            continue
        error = item.get("error")
        if error is not None and not isinstance(error, str):
            continue
        servers.append({
            "name": item["name"][:128],
            "connected": item["connected"],
            "tools": max(0, item["tools"]),
            "error": error[:128] if error is not None else None,
        })
    return servers


class StateServer:
    def __init__(
        self,
        publisher,
        *,
        clock: Callable[[], datetime] = _utcnow,
        authorizer: PairingAuthorizer | None = None,
        job_provider=None,
        job_event_provider=None,
        result_provider=None,
        cancel_provider=None,
        mcp_provider=None,
        health_provider=None,
    ) -> None:
        self._publisher = publisher
        self._clock = clock
        self._authorizer = authorizer
        self._job_provider = job_provider
        self._job_event_provider = job_event_provider
        self._result_provider = result_provider
        self._cancel_provider = cancel_provider
        self._mcp_provider = mcp_provider
        self._health_provider = health_provider
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    async def _handle_state(self, _request: web.Request) -> web.Response:
        payload = self._publisher.snapshot()
        payload["heartbeat"] = self._clock().isoformat()
        return web.json_response(payload, headers={"cache-control": "no-store"})

    async def _handle_signal(self, _request: web.Request) -> web.Response:
        value = getattr(self._publisher, "audio_energy", 0.0)
        if not _finite_number(value):
            value = 0.0
        return web.json_response(
            {"energy": round(max(0.0, min(1.0, float(value))), 4)},
            headers={"cache-control": "no-store"},
        )

    def _file_response(self, filename: str, content_type: str) -> web.Response:
        try:
            body = (UI_ROOT / filename).read_bytes()
        except OSError:
            raise web.HTTPNotFound() from None
        return web.Response(
            body=body,
            content_type=content_type,
            charset="utf-8",
            headers={"cache-control": "no-cache"},
        )

    async def _handle_index(self, _request: web.Request) -> web.Response:
        return self._file_response("index.html", "text/html")

    async def _handle_asset(self, request: web.Request) -> web.Response:
        asset = UI_ASSETS.get(request.path)
        if asset is None:
            raise web.HTTPNotFound()
        return self._file_response(*asset)

    async def _handle_jobs(self, _request: web.Request) -> web.Response:
        value = []
        if self._job_provider is not None:
            try:
                value = await _provide(self._job_provider)
            except Exception as exc:
                logger.warning("job provider failed: %s", type(exc).__name__)
        return web.json_response(
            {"jobs": _safe_jobs(value)}, headers={"cache-control": "no-store"},
        )

    async def _handle_job_events(self, request: web.Request) -> web.Response:
        self._authorize(request)
        job_id = request.match_info["job_id"]
        if JOB_ID.fullmatch(job_id) is None or self._job_event_provider is None:
            raise web.HTTPNotFound()
        raw_after = request.query.get("after", "0")
        if not raw_after.isdigit() or len(raw_after) > 20:
            raise web.HTTPBadRequest(text="after must be a non-negative integer")
        after = int(raw_after)
        try:
            value = await _provide(self._job_event_provider, job_id, after)
        except KeyError:
            raise web.HTTPNotFound() from None
        except Exception as exc:
            logger.warning("job event provider failed: %s", type(exc).__name__)
            raise web.HTTPServiceUnavailable(text="job events unavailable") from None
        return web.json_response(
            {"events": _safe_events(value)}, headers={"cache-control": "no-store"},
        )

    async def _handle_mcp(self, _request: web.Request) -> web.Response:
        value = []
        if self._mcp_provider is not None:
            try:
                value = await _provide(self._mcp_provider)
            except Exception as exc:
                logger.warning("MCP status provider failed: %s", type(exc).__name__)
        return web.json_response(
            {"servers": _safe_mcp(value)}, headers={"cache-control": "no-store"},
        )

    async def _handle_health(self, _request: web.Request) -> web.Response:
        value: Any = {}
        if self._health_provider is not None:
            try:
                value = await _provide(self._health_provider)
            except Exception as exc:
                logger.warning("health provider failed: %s", type(exc).__name__)
        if not isinstance(value, dict):
            value = {}
        payload = {
            "claude": value.get("claude") is True,
            "mcp": _safe_mcp(value.get("mcp", [])),
        }
        return web.json_response(payload, headers={"cache-control": "no-store"})

    def _authorize(self, request: web.Request) -> None:
        if self._authorizer is None:
            raise web.HTTPServiceUnavailable(text="Atlas UI pairing is unavailable")
        try:
            self._authorizer.authorize(request.headers.get(HEADER))
        except PermissionError as exc:
            raise web.HTTPUnauthorized(text=str(exc)) from None

    def _same_origin(self, request: web.Request) -> bool:
        host = request.host.rsplit(":", 1)[0].strip("[]").lower()
        origin = request.headers.get("Origin")
        expected = f"{request.scheme}://{request.host}".rstrip("/")
        return host in {"127.0.0.1", "localhost"} and bool(origin) and origin.rstrip("/") == expected

    async def _read_json(self, request: web.Request) -> dict[str, Any]:
        if not self._same_origin(request):
            raise web.HTTPForbidden(text="same-origin requests only")
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(text="application/json required")
        if request.content_length is not None and request.content_length > BODY_LIMIT:
            raise web.HTTPRequestEntityTooLarge(max_size=BODY_LIMIT, actual_size=request.content_length)
        body = await request.content.read(BODY_LIMIT + 1)
        if len(body) > BODY_LIMIT:
            raise web.HTTPRequestEntityTooLarge(max_size=BODY_LIMIT, actual_size=len(body))
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise web.HTTPBadRequest(text="valid JSON required") from None
        if not isinstance(value, dict):
            raise web.HTTPBadRequest(text="JSON object required")
        return value

    async def _handle_pair(self, request: web.Request) -> web.Response:
        if self._authorizer is None:
            raise web.HTTPServiceUnavailable(text="Atlas UI pairing is unavailable")
        payload = await self._read_json(request)
        try:
            bearer = self._authorizer.pair(payload.get("token", ""))
        except PermissionError as exc:
            raise web.HTTPUnauthorized(text=str(exc)) from None
        return web.json_response(
            {"ok": True, "action_token": bearer}, headers={"cache-control": "no-store"},
        )

    async def _handle_job_result(self, request: web.Request) -> web.Response:
        self._authorize(request)
        job_id = request.match_info["job_id"]
        if JOB_ID.fullmatch(job_id) is None or self._result_provider is None:
            raise web.HTTPNotFound()
        try:
            result = await _provide(self._result_provider, job_id)
        except KeyError:
            raise web.HTTPNotFound() from None
        except Exception as exc:
            logger.warning("job result provider failed: %s", type(exc).__name__)
            raise web.HTTPServiceUnavailable(text="private result unavailable") from None
        if not isinstance(result, str):
            raise web.HTTPNotFound()
        return web.json_response(
            {"job_id": job_id, "result": result[:65_536]},
            headers={"cache-control": "no-store"},
        )

    async def _handle_cancel_job(self, request: web.Request) -> web.Response:
        self._authorize(request)
        await self._read_json(request)
        job_id = request.match_info["job_id"]
        if JOB_ID.fullmatch(job_id) is None or self._cancel_provider is None:
            raise web.HTTPNotFound()
        try:
            value = await _provide(self._cancel_provider, job_id)
        except KeyError:
            raise web.HTTPNotFound() from None
        except Exception as exc:
            logger.warning("job cancellation failed: %s", type(exc).__name__)
            raise web.HTTPConflict(text="job could not be cancelled") from None
        job = _safe_job(value)
        if job is None:
            raise web.HTTPServiceUnavailable(text="job cancellation returned invalid state")
        return web.json_response({"job": job}, headers={"cache-control": "no-store"})

    async def start(self, port: int) -> "StateServer":
        app = web.Application(middlewares=[_security_middleware(lambda: self.port)])
        app.router.add_get("/", self._handle_index)
        for route in UI_ASSETS:
            app.router.add_get(route, self._handle_asset)
        app.router.add_get("/state", self._handle_state)
        app.router.add_get("/signal", self._handle_signal)
        app.router.add_post("/pair", self._handle_pair)
        app.router.add_get("/health", self._handle_health)
        app.router.add_get("/mcp", self._handle_mcp)
        app.router.add_get("/jobs", self._handle_jobs)
        app.router.add_get("/jobs/{job_id}/events", self._handle_job_events)
        app.router.add_get("/jobs/{job_id}/result", self._handle_job_result)
        app.router.add_post("/jobs/{job_id}/cancel", self._handle_cancel_job)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, HOST, port)
        await self._site.start()
        logger.info("Atlas state server listening on http://%s:%s", HOST, self.port)
        return self

    @property
    def port(self) -> int:
        return self._site.port if self._site is not None else 0

    @property
    def addresses(self) -> list:
        return self._runner.addresses if self._runner is not None else []

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None


async def start(
    publisher,
    port: int,
    clock: Callable[[], datetime] = _utcnow,
    *,
    authorizer: PairingAuthorizer | None = None,
    job_provider=None,
    job_event_provider=None,
    result_provider=None,
    cancel_provider=None,
    mcp_provider=None,
    health_provider=None,
) -> StateServer:
    server = StateServer(
        publisher,
        clock=clock,
        authorizer=authorizer,
        job_provider=job_provider,
        job_event_provider=job_event_provider,
        result_provider=result_provider,
        cancel_provider=cancel_provider,
        mcp_provider=mcp_provider,
        health_provider=health_provider,
    )
    return await server.start(port)
