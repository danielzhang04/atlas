"""Local read-only HTTP `/state` surface (design §3, Task 5).

An aiohttp server started inside `app.py::entrypoint()` on the existing job-context event loop
— same placement discipline as `_build_tts()`, which keeps it inside the job context and off the
wake thread (the V0 landmine). Bound to **127.0.0.1 ONLY**. The primary route, `GET /state`, returns
`publisher.snapshot()` plus a `heartbeat` stamped at request time; header `cache-control:
no-store`. It also serves the standalone UI, a sanitized capabilities catalog, and optional
proposal actions through an injected broker. Action execution is same-origin, hash-bound, and
never exposes an arbitrary command route.

Provably key-free: the response body is `publisher.snapshot()` (pure in-process state) plus the
`heartbeat` timestamp and NOTHING from `os.environ` — the scoped-key carve-out requires this
surface to never reflect process env.

API verified against the INSTALLED aiohttp 3.14.1 at
`atlas/.venv/Lib/site-packages/aiohttp/`:
  - `aiohttp.web.Application()` and `app.router.add_get(path, handler)`
    (web_urldispatcher.py:1204 `add_get`).
  - `web.AppRunner(app)` then `await runner.setup()` (web_runner.py:387 `AppRunner`;
    BaseRunner.setup web_runner.py:299).
  - `web.TCPSite(runner, host, port)` then `await site.start()`; `site.port` returns the actual
    bound port after start when `port=0` was requested (web_runner.py:87/113/133).
  - `runner.addresses` -> list of socket `getsockname()` tuples (web_runner.py:284), used to
    prove the localhost bind.
  - `web.json_response(data, headers=...)` serializes `data` as JSON and sets
    `content-type: application/json` (web_response.py:857).
  - `await runner.cleanup()` tears down sites + server for teardown (BaseRunner.cleanup
    web_runner.py:316).
"""
import asyncio
import inspect
import logging
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from aiohttp import web
from worker.actionauth import HEADER, PairingAuthorizer
from worker.contracts import ProtectedTaskResult
from worker.jobstore import InvalidTransition, UnknownJob
from worker.payload_codec import PayloadProtectionError

logger = logging.getLogger("atlas.stateserver")

# localhost ONLY — the surface never binds a routable interface (design §3).
HOST = "127.0.0.1"
UI_ROOT = Path(__file__).resolve().parents[1] / "ui"

# Explicit allow-list: this is not a general-purpose file server.
UI_ASSETS = {
    "/ui/styles.css": ("styles.css", "text/css"),
    "/ui/app.js": ("app.js", "application/javascript"),
    "/ui/favicon.svg": ("favicon.svg", "image/svg+xml"),
}


def _empty_catalog() -> list[dict[str, str]]:
    return []


CatalogProvider = Callable[[], Any]
StateProvider = Callable[[], Any]
ReceiptProvider = Callable[[], Any]
JobProvider = Callable[[], Any]
JobEventProvider = Callable[[str], Any]
ResultProvider = Callable[[str], ProtectedTaskResult]
HealthProvider = Callable[[], Any]
SignalProvider = Callable[[], Any]
GuidedSetupProvider = Callable[[str], Any]
CATALOG_FIELDS = ("id", "label", "status", "detail", "kind")
ACTION_FIELDS = ("id", "label", "preview", "proposal_hash", "status", "created_at", "risk", "receipt")
ACTION_BODY_LIMIT = 8192
ACTION_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
GUIDE_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
    "form-action 'none'"
)
RECEIPT_FIELDS = ("version", "timestamp", "proposal_id", "capability_id", "parameters_hash",
                  "status", "session_id", "device_id", "confirmation_channel", "error_code")
JOB_FIELDS = ("id", "status", "lane", "operation", "updated_at", "code", "proposal_id", "summary")
JOB_EVENT_TEXT_FIELDS = ("code", "summary", "reason", "worker_id")
JOB_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.I,
)
VOICE_STATES = {"ASLEEP", "LISTENING", "THINKING", "ACTING", "SPEAKING"}


@web.middleware
async def _security_headers(request: web.Request, handler) -> web.StreamResponse:
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        _add_security_headers(exc)
        raise
    _add_security_headers(response)
    return response


def _add_security_headers(response: web.StreamResponse) -> None:
    response.headers["content-security-policy"] = CONTENT_SECURITY_POLICY
    response.headers["x-content-type-options"] = "nosniff"
    response.headers["referrer-policy"] = "no-referrer"
    response.headers["x-frame-options"] = "DENY"


def _safe_catalog(value: Any) -> list[dict[str, str]]:
    """Project provider output to non-secret display metadata only."""
    if not isinstance(value, list):
        return []
    catalog: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        safe = {
            field: item[field]
            for field in CATALOG_FIELDS
            if isinstance(item.get(field), str)
        }
        catalog.append(safe)
    return catalog


def _safe_actions(value: Any) -> list[dict[str, Any]]:
    """Project broker output to display metadata; commands never cross this HTTP boundary."""
    if not isinstance(value, list):
        return []
    actions: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        safe = {
            field: item[field]
            for field in ACTION_FIELDS
            if isinstance(item.get(field), str)
        }
        # Missing or malformed confirmability is fail-closed at the HTTP trust boundary.
        safe["confirmable"] = item.get("confirmable") is True
        if isinstance(safe.get("id"), str) and isinstance(safe.get("proposal_hash"), str):
            actions.append(safe)
    return actions


def _safe_receipts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value[:200]:
        if not isinstance(item, dict):
            continue
        safe = {field: item.get(field) for field in RECEIPT_FIELDS
                if isinstance(item.get(field), (str, int)) or item.get(field) is None}
        if (isinstance(safe.get("proposal_id"), str)
                and isinstance(safe.get("parameters_hash"), str)
                and isinstance(safe.get("status"), str)):
            result.append(safe)
    return result


def _safe_jobs(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value[:100]:
        if not isinstance(item, dict):
            continue
        safe = {field: item[field] for field in JOB_FIELDS
                if isinstance(item.get(field), str) and len(item[field]) <= 512}
        if item.get("result_available") is True:
            safe["result_available"] = True
        if all(isinstance(safe.get(field), str) for field in ("id", "status", "lane", "operation")):
            result.append(safe)
    return result


def _safe_job_events(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    result = []
    for item in value[:100]:
        if not isinstance(item, dict):
            continue
        sequence = item.get("sequence")
        timestamp = item.get("timestamp")
        kind = item.get("kind")
        state = item.get("state")
        if (isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1
                or isinstance(timestamp, bool) or not isinstance(timestamp, (int, float))
                or not math.isfinite(float(timestamp))
                or not isinstance(kind, str) or not isinstance(state, str)):
            continue
        safe = {
            "sequence": sequence,
            "timestamp": timestamp,
            "kind": kind[:64],
            "state": state[:64],
        }
        for field in JOB_EVENT_TEXT_FIELDS:
            value = item.get(field)
            if isinstance(value, str):
                safe[field] = value[:512]
        result.append(safe)
    return result


def _safe_worker_health(value: Any) -> dict[str, Any]:
    if value is None:
        return {"status": "unavailable", "reason": "health_not_reported"}
    status = getattr(value, "status", None)
    status = getattr(status, "value", status)
    reason = getattr(value, "reason", "")
    worker_id = getattr(value, "worker_id", "")
    checked_at = getattr(value, "checked_at", None)
    if status not in {"available", "degraded", "unavailable"}:
        return {"status": "unavailable", "reason": "health_invalid"}
    result: dict[str, Any] = {"status": status}
    if isinstance(reason, str):
        result["reason"] = reason[:64]
    if isinstance(worker_id, str):
        result["worker_id"] = worker_id[:128]
    if (isinstance(checked_at, (int, float)) and not isinstance(checked_at, bool)
            and math.isfinite(float(checked_at))):
        result["checked_at"] = checked_at
    return result


def _safe_remote_state(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or value.get("version") != 1 or value.get("state") not in VOICE_STATES:
        return None
    since = value.get("since")
    session_id = value.get("session_id")
    voice = value.get("voice")
    if (not isinstance(since, str) or len(since) > 128
            or session_id is not None and (not isinstance(session_id, str) or len(session_id) > 128)
            or voice is not None and (not isinstance(voice, str) or len(voice) > 128)):
        return None
    transcript = []
    for line in value.get("transcript", [])[:50] if isinstance(value.get("transcript"), list) else []:
        if (not isinstance(line, dict) or line.get("role") not in {"user", "atlas"}
                or not isinstance(line.get("t"), str) or len(line["t"]) > 128
                or not isinstance(line.get("text"), str) or len(line["text"]) > 8_192):
            continue
        transcript.append({"t": line["t"], "role": line["role"], "text": line["text"]})
    filed_cards = []
    for card in value.get("filed_cards", [])[:100] if isinstance(value.get("filed_cards"), list) else []:
        if not isinstance(card, dict):
            continue
        safe_card = {field: card[field][:512] for field in ("id", "action", "state")
                     if isinstance(card.get(field), str)}
        if "id" in safe_card and "state" in safe_card:
            filed_cards.append(safe_card)
    output = value.get("output_device")
    safe_output = None
    if isinstance(output, dict):
        safe_output = {field: output.get(field)[:512] if isinstance(output.get(field), str) else None
                       for field in ("configured", "resolved")}
    return {
        "version": 1,
        "state": value["state"],
        "since": since,
        "session_id": session_id,
        "voice": voice,
        "transcript": transcript,
        "filed_cards": filed_cards,
        "output_device": safe_output,
        "audio_energy": _safe_audio_energy(value.get("audio_energy")),
    }


def _safe_audio_energy(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    numeric = float(value)
    if not math.isfinite(numeric):
        return 0.0
    return round(max(0.0, min(1.0, numeric)), 4)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class StateServer:
    """A running `/state` server: owns the aiohttp `AppRunner`/`TCPSite` and exposes `.stop()`.

    Constructed with the publisher it mirrors and an injectable `clock` (real UTC by default) so
    the request-time `heartbeat` is deterministic under test. It is an observer of the publisher,
    never a controller — it only reads `snapshot()`.
    """

    def __init__(
        self,
        publisher,
        clock: Callable[[], datetime] = _utcnow,
        state_provider: StateProvider | None = None,
        catalog_provider: CatalogProvider | None = None,
        action_broker: Any | None = None,
        action_authorizer: PairingAuthorizer | None = None,
        receipt_provider: ReceiptProvider | None = None,
        job_provider: JobProvider | None = None,
        job_event_provider: JobEventProvider | None = None,
        result_provider: ResultProvider | None = None,
        health_provider: HealthProvider | None = None,
        signal_provider: SignalProvider | None = None,
        guided_setup_provider: GuidedSetupProvider | None = None,
        surface_mode: str = "voice",
    ) -> None:
        if surface_mode not in {"voice", "observer", "mirror"}:
            raise ValueError("invalid Atlas surface mode")
        self._publisher = publisher
        self._clock = clock
        self._state_provider = state_provider
        self._catalog_provider = catalog_provider or _empty_catalog
        self._action_broker = action_broker
        self._action_authorizer = action_authorizer
        self._receipt_provider = receipt_provider
        self._job_provider = job_provider
        self._job_event_provider = job_event_provider
        self._result_provider = result_provider
        self._health_provider = health_provider
        self._signal_provider = signal_provider
        self._guided_setup_provider = guided_setup_provider
        self._surface_mode = surface_mode
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    async def _handle_state(self, request: web.Request) -> web.Response:
        # snapshot() (local or from an injected reviewed loopback mirror) plus a fresh heartbeat
        # are the ONLY body sources. os.environ is never read.
        if self._state_provider is None:
            payload = self._publisher.snapshot()
        else:
            try:
                payload = self._state_provider()
                if hasattr(payload, "__await__"):
                    payload = await payload
            except Exception:
                raise web.HTTPServiceUnavailable(text="voice worker state unavailable") from None
            payload = _safe_remote_state(payload)
            if payload is None:
                raise web.HTTPServiceUnavailable(text="voice worker state unavailable")
        payload["heartbeat"] = self._clock().isoformat()
        return web.json_response(payload, headers={
            "cache-control": "no-store",
            "x-atlas-surface": self._surface_mode,
        })

    async def _handle_signal(self, request: web.Request) -> web.Response:
        if self._signal_provider is None:
            value = getattr(self._publisher, "audio_energy", 0.0)
        else:
            try:
                value = self._signal_provider()
                if hasattr(value, "__await__"):
                    value = await value
            except Exception:
                value = 0.0
        return web.json_response({"energy": _safe_audio_energy(value)},
                                 headers={"cache-control": "no-store"})

    async def _handle_guided_setup(self, request: web.Request) -> web.Response:
        self._authorize_action_request(request)
        await self._read_action_body(request)
        guide_id = request.match_info["guide_id"]
        if GUIDE_ID_RE.fullmatch(guide_id) is None or self._guided_setup_provider is None:
            raise web.HTTPNotFound()
        try:
            result = await asyncio.to_thread(self._guided_setup_provider, guide_id)
        except KeyError:
            raise web.HTTPNotFound()
        except Exception:
            logger.exception("guided setup admission failed")
            raise web.HTTPServiceUnavailable(text="guided setup is unavailable")
        job_id = getattr(result, "job_id", None)
        status = getattr(result, "status", None)
        lane = getattr(getattr(result, "lane", None), "value", getattr(result, "lane", None))
        if (not isinstance(job_id, str) or JOB_ID_RE.fullmatch(job_id) is None
                or not isinstance(status, str) or not isinstance(lane, str)):
            raise web.HTTPServiceUnavailable(text="guided setup admission is invalid")
        payload = {"ok": status == "queued", "job_id": job_id, "status": status, "lane": lane}
        return web.json_response(payload, status=202 if status == "queued" else 409,
                                 headers={"cache-control": "no-store"})

    def _file_response(self, filename: str, content_type: str, cache_control: str) -> web.Response:
        path = UI_ROOT / filename
        try:
            body = path.read_bytes()
        except (FileNotFoundError, OSError):
            raise web.HTTPNotFound()
        return web.Response(
            body=body,
            content_type=content_type,
            charset="utf-8",
            headers={"cache-control": cache_control},
        )

    async def _handle_index(self, request: web.Request) -> web.Response:
        return self._file_response("index.html", "text/html", "no-cache")

    async def _handle_asset(self, request: web.Request) -> web.Response:
        asset = UI_ASSETS.get(request.path)
        if asset is None:
            raise web.HTTPNotFound()
        filename, content_type = asset
        return self._file_response(filename, content_type, "no-cache")

    async def _handle_capabilities(self, request: web.Request) -> web.Response:
        try:
            result = self._catalog_provider()
            if hasattr(result, "__await__"):
                result = await result
        except Exception:
            logger.exception("capability catalog provider failed")
            result = []
        catalog = _safe_catalog(result)
        return web.json_response(catalog, headers={"cache-control": "no-store"})

    async def _broker_actions(self) -> list[dict[str, str]]:
        broker = self._action_broker
        if broker is None:
            return []
        provider = getattr(broker, "list_actions", None) or getattr(broker, "list_pending", None)
        if provider is None and callable(broker):
            provider = broker
        if provider is None:
            return []
        try:
            result = provider()
            if hasattr(result, "__await__"):
                result = await result
            return _safe_actions(result)
        except Exception:
            logger.exception("action broker projection failed")
            return []

    async def _handle_actions(self, request: web.Request) -> web.Response:
        if self._action_broker is None:
            return web.json_response({"actions": []}, headers={"cache-control": "no-store"})
        self._authorize_action_request(request)
        return web.json_response({"actions": await self._broker_actions()}, headers={"cache-control": "no-store"})

    async def _handle_receipts(self, request: web.Request) -> web.Response:
        self._authorize_action_request(request)
        if self._receipt_provider is None:
            return web.json_response({"receipts": []}, headers={"cache-control": "no-store"})
        try:
            result = await asyncio.to_thread(self._receipt_provider)
        except Exception:
            logger.exception("receipt history provider failed")
            result = []
        return web.json_response({"receipts": _safe_receipts(result)},
                                 headers={"cache-control": "no-store"})

    async def _handle_jobs(self, request: web.Request) -> web.Response:
        if self._job_provider is None:
            return web.json_response({"jobs": []}, headers={"cache-control": "no-store"})
        try:
            result = await asyncio.to_thread(self._job_provider)
        except Exception:
            logger.exception("job projection provider failed")
            result = []
        return web.json_response({"jobs": _safe_jobs(result)},
                                 headers={"cache-control": "no-store"})

    async def _handle_job_events(self, request: web.Request) -> web.Response:
        job_id = request.match_info["job_id"]
        if JOB_ID_RE.fullmatch(job_id) is None or self._job_event_provider is None:
            raise web.HTTPNotFound()
        try:
            result = await asyncio.to_thread(self._job_event_provider, job_id)
        except (UnknownJob, InvalidTransition):
            raise web.HTTPNotFound()
        except Exception:
            logger.exception("job event projection failed")
            result = []
        return web.json_response({"events": _safe_job_events(result)},
                                 headers={"cache-control": "no-store"})

    async def _handle_health(self, request: web.Request) -> web.Response:
        if self._health_provider is None:
            result = None
        else:
            try:
                result = await asyncio.to_thread(self._health_provider)
            except Exception:
                logger.exception("subscription health provider failed")
                result = None
        return web.json_response(_safe_worker_health(result),
                                 headers={"cache-control": "no-store"})

    async def _handle_job_result(self, request: web.Request) -> web.Response:
        self._authorize_action_request(request)
        job_id = request.match_info["job_id"]
        if JOB_ID_RE.fullmatch(job_id) is None or self._result_provider is None:
            raise web.HTTPNotFound()
        try:
            result = await asyncio.to_thread(self._result_provider, job_id)
        except (UnknownJob, InvalidTransition):
            raise web.HTTPNotFound()
        except PayloadProtectionError:
            logger.exception("protected result provider failed")
            raise web.HTTPServiceUnavailable(text="private result is unavailable")
        if not isinstance(result, ProtectedTaskResult) or result.job_id != job_id:
            logger.error("protected result provider returned an invalid result")
            raise web.HTTPServiceUnavailable(text="private result is unavailable")
        return web.json_response({
            "version": result.version,
            "job_id": result.job_id,
            "answer": result.answer,
            "candidate_digest": result.candidate_digest,
            "evidence_count": len(result.evidence_ids),
            "artifact_name": result.artifact_name,
        }, headers={"cache-control": "no-store"})

    def _authorize_action_request(self, request: web.Request):
        if self._action_authorizer is None:
            raise web.HTTPServiceUnavailable(text="Atlas action UI pairing is unavailable")
        try:
            return self._action_authorizer.authorize(request.headers.get(HEADER))
        except PermissionError as exc:
            raise web.HTTPUnauthorized(text=str(exc))

    async def _handle_pair(self, request: web.Request) -> web.Response:
        if self._action_authorizer is None:
            raise web.HTTPServiceUnavailable(text="Atlas action UI pairing is unavailable")
        payload = await self._read_action_body(request)
        try:
            raw_cookie, _context = self._action_authorizer.pair(payload.get("token", ""))
        except PermissionError as exc:
            raise web.HTTPUnauthorized(text=str(exc))
        # This bearer remains in the paired page's memory and is sent only in an explicit header.
        # Browser cookies are deliberately not used: localhost cookies are shared across ports.
        return web.json_response({"ok": True, "action_token": raw_cookie},
                                 headers={"cache-control": "no-store"})

    def _same_origin(self, request: web.Request) -> bool:
        host = request.host.rsplit(":", 1)[0].strip("[]").lower()
        if host not in {"127.0.0.1", "localhost"}:
            return False
        origin = request.headers.get("Origin")
        if not origin:
            return False
        expected = f"{request.scheme}://{request.host}".rstrip("/")
        return origin.rstrip("/") == expected

    async def _read_action_body(self, request: web.Request) -> dict[str, Any] | None:
        if not self._same_origin(request):
            raise web.HTTPForbidden(text="same-origin action requests only")
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(text="application/json required")
        if request.content_length is not None and request.content_length > ACTION_BODY_LIMIT:
            raise web.HTTPRequestEntityTooLarge(max_size=ACTION_BODY_LIMIT, actual_size=request.content_length)
        body = await request.content.read(ACTION_BODY_LIMIT + 1)
        if len(body) > ACTION_BODY_LIMIT:
            raise web.HTTPRequestEntityTooLarge(max_size=ACTION_BODY_LIMIT, actual_size=len(body))
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise web.HTTPBadRequest(text="valid JSON required")
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(text="JSON object required")
        return payload

    async def _run_action(self, request: web.Request, action_id: str, operation: str) -> web.Response:
        if self._action_broker is None:
            raise web.HTTPServiceUnavailable(text="action broker unavailable")
        if not ACTION_ID_RE.fullmatch(action_id):
            raise web.HTTPNotFound()
        context = self._authorize_action_request(request)
        payload = await self._read_action_body(request)
        proposal_hash = payload.get("proposal_hash") if payload else None
        if not isinstance(proposal_hash, str) or not proposal_hash:
            raise web.HTTPBadRequest(text="proposal_hash required")
        actions = await self._broker_actions()
        proposal = next((item for item in actions if item.get("id") == action_id), None)
        if proposal is None:
            raise web.HTTPNotFound()
        if proposal.get("proposal_hash") != proposal_hash:
            reject = getattr(self._action_broker, "record_rejected", None)
            if callable(reject):
                await asyncio.to_thread(reject, action_id, "proposal_hash_mismatch",
                                        session_id=context.session_id,
                                        device_id=context.device_id)
            raise web.HTTPConflict(text="proposal hash mismatch")
        if operation == "run" and proposal.get("confirmable") is not True:
            reject = getattr(self._action_broker, "record_rejected", None)
            if callable(reject):
                await asyncio.to_thread(reject, action_id, "proposal_unconfirmable",
                                        session_id=context.session_id,
                                        device_id=context.device_id)
            raise web.HTTPConflict(
                text="proposal is not confirmable; cancel it and prepare a smaller action")
        method = getattr(self._action_broker, f"{operation}_action", None) or getattr(self._action_broker, operation, None)
        if method is None:
            raise web.HTTPServiceUnavailable(text="action operation unavailable")
        is_run = operation == "run"
        if is_run:
            self._publisher.begin_confirmed_action()
        try:
            if inspect.iscoroutinefunction(method):
                result = await method(action_id, proposal_hash,
                                      session_id=context.session_id, device_id=context.device_id)
            else:
                result = await asyncio.to_thread(
                    method, action_id, proposal_hash,
                    session_id=context.session_id, device_id=context.device_id)
        except Exception as exc:
            reject = getattr(self._action_broker, "record_rejected", None)
            if callable(reject):
                try:
                    await asyncio.to_thread(
                        reject, action_id, type(exc).__name__.casefold()[:64],
                        session_id=context.session_id, device_id=context.device_id)
                except Exception:
                    logger.exception("action rejection receipt failed")
            logger.exception("action broker operation failed")
            raise web.HTTPBadGateway(text="action operation failed")
        finally:
            if is_run:
                self._publisher.end_confirmed_action()
        safe_result = {"id": action_id, "status": operation}
        if isinstance(result, dict):
            for field in ("status", "message"):
                if isinstance(result.get(field), str):
                    safe_result[field] = result[field]
            receipt = result.get("receipt")
            if isinstance(receipt, dict):
                safe_result["receipt"] = {
                    key: value for key, value in receipt.items()
                    if key in {"outcome", "capability_id", "proposal_hash",
                               "confirmation_channel", "error_code"}
                    and isinstance(value, str)
                }
        return web.json_response({"ok": True, "action": safe_result}, headers={"cache-control": "no-store"})

    async def _handle_run_action(self, request: web.Request) -> web.Response:
        return await self._run_action(request, request.match_info["action_id"], "run")

    async def _handle_cancel_action(self, request: web.Request) -> web.Response:
        return await self._run_action(request, request.match_info["action_id"], "cancel")

    async def start(self, port: int) -> "StateServer":
        app = web.Application(middlewares=[_security_headers])
        app.router.add_get("/state", self._handle_state)
        app.router.add_get("/signal", self._handle_signal)
        app.router.add_get("/", self._handle_index)
        for route in UI_ASSETS:
            app.router.add_get(route, self._handle_asset)
        app.router.add_get("/capabilities", self._handle_capabilities)
        app.router.add_post("/pair", self._handle_pair)
        app.router.add_get("/actions", self._handle_actions)
        app.router.add_get("/receipts", self._handle_receipts)
        app.router.add_get("/jobs", self._handle_jobs)
        app.router.add_get("/jobs/{job_id}/events", self._handle_job_events)
        app.router.add_get("/jobs/{job_id}/result", self._handle_job_result)
        app.router.add_get("/health", self._handle_health)
        app.router.add_post("/guided-setups/{guide_id}", self._handle_guided_setup)
        app.router.add_post("/actions/{action_id}/run", self._handle_run_action)
        app.router.add_post("/actions/{action_id}/cancel", self._handle_cancel_action)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, HOST, port)
        await self._site.start()
        logger.info("atlas /state serving on http://%s:%s", HOST, self.port)
        return self

    @property
    def port(self) -> int:
        """The actually-bound port (resolves an ephemeral `port=0` after start)."""
        return self._site.port if self._site is not None else 0

    @property
    def addresses(self) -> list:
        """Bound socket addresses (`getsockname()` tuples) — proves the localhost bind."""
        return self._runner.addresses if self._runner is not None else []

    async def stop(self) -> None:
        """Tear down the site + server. Idempotent."""
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None


async def start(
    publisher,
    port: int,
    clock: Callable[[], datetime] = _utcnow,
    state_provider: StateProvider | None = None,
    catalog_provider: CatalogProvider | None = None,
    action_broker: Any | None = None,
    action_authorizer: PairingAuthorizer | None = None,
    receipt_provider: ReceiptProvider | None = None,
    job_provider: JobProvider | None = None,
    job_event_provider: JobEventProvider | None = None,
    result_provider: ResultProvider | None = None,
    health_provider: HealthProvider | None = None,
    signal_provider: SignalProvider | None = None,
    guided_setup_provider: GuidedSetupProvider | None = None,
    surface_mode: str = "voice",
) -> StateServer:
    """Start the `/state` server bound to `127.0.0.1:<port>`. Awaitable; returns the handle
    (call `.stop()` to tear it down). Pass `port=0` for an ephemeral port (tests)."""
    return await StateServer(publisher, clock=clock, state_provider=state_provider,
                             catalog_provider=catalog_provider,
                             action_broker=action_broker,
                             action_authorizer=action_authorizer,
                             receipt_provider=receipt_provider,
                             job_provider=job_provider,
                             job_event_provider=job_event_provider,
                             result_provider=result_provider,
                             health_provider=health_provider, signal_provider=signal_provider,
                             guided_setup_provider=guided_setup_provider,
                             surface_mode=surface_mode).start(port)
