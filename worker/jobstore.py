"""Small SQLite job/event store for the standalone Atlas work plane.

The store owns lifecycle state only.  Public payloads are treated as untrusted input and are
redacted, bounded, and serialized before they reach SQLite.
"""
from __future__ import annotations

from hashlib import sha256
from hmac import compare_digest
import json
import math
from pathlib import Path
import re
import sqlite3
import secrets
import threading
from typing import Any, Callable, Mapping
from uuid import uuid4

from .contracts import (
    EventKind, Job, JobClaim, JobEvent, JobState, Lane, ProtectedTaskResult, Request,
    SlowTaskPayload, utc_timestamp,
)
from .payload_codec import PayloadCodec, PayloadProtectionError


class JobStoreError(RuntimeError):
    pass


class InvalidTransition(JobStoreError):
    pass


class IdempotencyConflict(JobStoreError):
    pass


class UnknownJob(JobStoreError):
    pass


_ALLOWED: dict[JobState, frozenset[JobState]] = {
    JobState.QUEUED: frozenset({JobState.RUNNING, JobState.CANCEL_REQUESTED, JobState.CANCELLED,
                               JobState.UNAVAILABLE}),
    JobState.RUNNING: frozenset({JobState.SUCCEEDED, JobState.FAILED, JobState.CANCEL_REQUESTED,
                                 JobState.ORPHANED, JobState.UNAVAILABLE}),
    JobState.CANCEL_REQUESTED: frozenset({JobState.CANCELLED, JobState.FAILED, JobState.UNAVAILABLE}),
    JobState.SUCCEEDED: frozenset(), JobState.FAILED: frozenset(),
    JobState.CANCELLED: frozenset(), JobState.ORPHANED: frozenset(), JobState.UNAVAILABLE: frozenset(),
}
_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|cookie|"
    r"authorization|credential|client[_-]?secret|private[_-]?key|set[_-]?cookie|body|content|header)", re.I
)
_SENSITIVE_VALUE = re.compile(
    # Strongly named credentials may use short values; generic token/secret prose must look
    # credential-shaped so "research token: economics" remains ordinary worker input.
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|client[_-]?secret|"
    r"private[_-]?key|authorization)\s*[\"']?\s*[:=]\s*[\"']?[^\s,;\"'}]{4,}|"
    r"(?:token|secret)\s*[:=]\s*[\"']?(?:[A-Za-z0-9._~+/=-]{12,})|"
    r"(?:token|secret)[-_][A-Za-z0-9._-]{4,}|bearer\s+[A-Za-z0-9._~+/=-]{8,}|"
    r"(?:sk|pk)-[A-Za-z0-9_-]{12,}|-----BEGIN(?: [^-]+)? PRIVATE KEY-----", re.I
)
MAX_PUBLIC_PAYLOAD_BYTES = 4_096
MAX_PUBLIC_STRING = 512
MAX_PUBLIC_DEPTH = 4
MAX_PUBLIC_ITEMS = 64
MAX_PROTECTED_PAYLOAD_BYTES = 32_768


def _json_value(value: Any, *, key: str = "", depth: int = 0) -> Any:
    if _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if depth >= MAX_PUBLIC_DEPTH and isinstance(value, (Mapping, list, tuple, set, frozenset)):
        return "[NESTED_REDACTED]"
    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, str):
            return "[REDACTED]" if _SENSITIVE_VALUE.search(value) else value[:MAX_PUBLIC_STRING]
        return value
    if isinstance(value, Mapping):
        result = {}
        iterator = iter(value.items())
        for _ in range(MAX_PUBLIC_ITEMS):
            try:
                raw_key, item = next(iterator)
            except StopIteration:
                break
            raw_key_text = str(raw_key)
            safe_key = raw_key_text[:MAX_PUBLIC_STRING]
            # Inspect the complete caller key before truncating it.  An attacker must not hide a
            # sensitive suffix beyond the public key bound and cause its value to be serialized.
            if len(raw_key_text) > MAX_PUBLIC_STRING or _SENSITIVE_KEY.search(raw_key_text):
                result[safe_key] = "[REDACTED]"
            else:
                result[safe_key] = _json_value(item, key=raw_key_text, depth=depth + 1)
        else:
            try:
                next(iterator)
                result["_truncated"] = "[ITEM_LIMIT]"
            except StopIteration:
                pass
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        iterator = iter(value)
        result = []
        for _ in range(MAX_PUBLIC_ITEMS):
            try:
                result.append(_json_value(next(iterator), depth=depth + 1))
            except StopIteration:
                break
        else:
            try:
                next(iterator)
                result.append("[ITEM_LIMIT]")
            except StopIteration:
                pass
        return result
    return "[UNSUPPORTED]"


def redact_public_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    if payload is None:
        return {}
    if not isinstance(payload, Mapping):
        raise TypeError("public payload must be a mapping")
    safe = _json_value(payload)
    encoded = json.dumps(safe, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if len(encoded.encode("utf-8")) <= MAX_PUBLIC_PAYLOAD_BYTES:
        return safe
    # Keep the journal bounded even when callers provide many ordinary fields.
    return {"summary": "[PAYLOAD_REDACTED:TOO_LARGE]"}


def _payload_json(payload: Mapping[str, Any] | None) -> str:
    return json.dumps(redact_public_payload(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _request_json(request: Request) -> str:
    # Metadata is intentionally not persisted.  Request fields are worker input, but the durable
    # outbox stores a sanitized/opaque form: arbitrary embedded credentials must never become
    # recoverable SQLite bytes or public event payloads.
    return _payload_json(request.canonical() | {"request_id": request.request_id})


def _request_from_json(raw: str) -> Request:
    data = json.loads(raw)
    return Request(**data)


def _idempotency_digest(value: str | None) -> str | None:
    if value is None:
        return None
    return sha256(value.encode("utf-8")).hexdigest()


def _lease_token_digest(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", value):
        raise InvalidTransition("invalid lease token")
    return sha256(value.encode("ascii")).hexdigest()


def _payload_entropy(job_id: str, request_fingerprint: str) -> bytes:
    return sha256(f"atlas-slow-v1:{job_id}:{request_fingerprint}".encode("utf-8")).digest()


def _result_entropy(job_id: str, request_fingerprint: str) -> bytes:
    return sha256(f"atlas-result-v1:{job_id}:{request_fingerprint}".encode("utf-8")).digest()


class JobStore:
    """Thread-safe SQLite store with atomic state/event writes."""

    def __init__(self, path: str | Path = ":memory:", *, clock: Callable[[], float] = utc_timestamp,
                 payload_codec: PayloadCodec | None = None) -> None:
        if not isinstance(path, (str, Path)):
            raise TypeError("SQLite path must be text or Path")
        self.path = str(path)
        if self.path != ":memory:":
            if "\x00" in self.path:
                raise ValueError("SQLite path contains NUL")
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        if payload_codec is not None and not isinstance(payload_codec, PayloadCodec):
            raise TypeError("payload_codec must implement PayloadCodec")
        if payload_codec is not None and (
            not isinstance(payload_codec.codec_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", payload_codec.codec_id)
        ):
            raise ValueError("payload codec id is invalid")
        self._payload_codec = payload_codec
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                request_json TEXT NOT NULL,
                request_fingerprint TEXT NOT NULL,
                state TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                idempotency_key TEXT UNIQUE,
                public_payload TEXT NOT NULL,
                lease_owner TEXT,
                lease_until REAL,
                lease_token_digest TEXT,
                lane TEXT NOT NULL DEFAULT 'slow'
            );
            CREATE TABLE IF NOT EXISTS job_events (
                job_id TEXT NOT NULL REFERENCES jobs(job_id),
                sequence INTEGER NOT NULL,
                kind TEXT NOT NULL,
                state TEXT NOT NULL,
                timestamp REAL NOT NULL,
                public_payload TEXT NOT NULL,
                PRIMARY KEY (job_id, sequence)
            );
            CREATE INDEX IF NOT EXISTS jobs_state_idx ON jobs(state);
            CREATE TABLE IF NOT EXISTS slow_payloads (
                job_id TEXT PRIMARY KEY REFERENCES jobs(job_id) ON DELETE CASCADE,
                codec_id TEXT NOT NULL,
                ciphertext BLOB NOT NULL,
                plaintext_digest TEXT NOT NULL,
                replay_digest TEXT,
                replay_salt BLOB
            );
            CREATE TABLE IF NOT EXISTS protected_results (
                job_id TEXT PRIMARY KEY REFERENCES jobs(job_id) ON DELETE CASCADE,
                codec_id TEXT NOT NULL,
                ciphertext BLOB NOT NULL,
                plaintext_digest TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            """
        )
        columns = {row[1] for row in self._connection.execute("PRAGMA table_info(jobs)")}
        if "lease_owner" not in columns:
            self._connection.execute("ALTER TABLE jobs ADD COLUMN lease_owner TEXT")
        if "lease_until" not in columns:
            self._connection.execute("ALTER TABLE jobs ADD COLUMN lease_until REAL")
        if "lane" not in columns:
            self._connection.execute("ALTER TABLE jobs ADD COLUMN lane TEXT NOT NULL DEFAULT 'slow'")
        if "lease_token_digest" not in columns:
            self._connection.execute("ALTER TABLE jobs ADD COLUMN lease_token_digest TEXT")
        payload_columns = {row[1] for row in self._connection.execute("PRAGMA table_info(slow_payloads)")}
        if "replay_digest" not in payload_columns:
            self._connection.execute("ALTER TABLE slow_payloads ADD COLUMN replay_digest TEXT")
        if "replay_salt" not in payload_columns:
            self._connection.execute("ALTER TABLE slow_payloads ADD COLUMN replay_salt BLOB")
        self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "JobStore":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def create(self, request: Request, *, idempotency_key: str | None = None,
               public_payload: Mapping[str, Any] | None = None,
               lane: Lane | str = Lane.SLOW,
               slow_payload: SlowTaskPayload | None = None) -> Job:
        return self.create_with_replay(
            request, idempotency_key=idempotency_key, public_payload=public_payload, lane=lane,
            slow_payload=slow_payload,
        )[0]

    create_job = create
    put = create

    def create_with_replay(self, request: Request, *, idempotency_key: str | None = None,
                           public_payload: Mapping[str, Any] | None = None,
                           lane: Lane | str = Lane.SLOW,
                           slow_payload: SlowTaskPayload | None = None) -> tuple[Job, bool]:
        """Create a job and report whether an exact idempotent record already existed.

        The boolean is intentionally derived while holding the same transaction as ``create``;
        callers can therefore avoid submitting an idempotent replay twice without a racy lookup.
        """
        if not isinstance(request, Request):
            raise TypeError("job request must be a Request")
        try:
            lane = lane if isinstance(lane, Lane) else Lane(lane)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid job lane") from exc
        if idempotency_key is not None and (not isinstance(idempotency_key, str) or not idempotency_key.strip()):
            raise ValueError("idempotency_key must be non-empty when provided")
        now = float(self._clock())
        payload = _payload_json(public_payload)
        stored_idempotency_key = _idempotency_digest(idempotency_key)
        fingerprint = request.fingerprint()
        if slow_payload is not None:
            if not isinstance(slow_payload, SlowTaskPayload):
                raise TypeError("slow_payload must be a SlowTaskPayload")
            if lane is not Lane.SLOW or slow_payload.request_fingerprint != fingerprint:
                raise ValueError("slow payload does not match its slow request")
            if self._payload_codec is None:
                raise PayloadProtectionError("protected payload codec is unavailable")
            protected_plaintext = slow_payload.to_bytes()
        else:
            protected_plaintext = None
        with self._lock, self._transaction():
            if stored_idempotency_key is not None:
                row = self._connection.execute(
                    "SELECT * FROM jobs WHERE idempotency_key = ?", (stored_idempotency_key,)
                ).fetchone()
                if row is not None:
                    if row["request_fingerprint"] != fingerprint or Lane(row["lane"]) is not lane:
                        raise IdempotencyConflict("idempotency key has different request or lane")
                    payload_row = self._connection.execute(
                        "SELECT replay_digest, replay_salt FROM slow_payloads WHERE job_id = ?", (row["job_id"],)
                    ).fetchone()
                    stored_payload_digest = payload_row["replay_digest"] if payload_row is not None else None
                    replay_salt = bytes(payload_row["replay_salt"]) if (
                        payload_row is not None and payload_row["replay_salt"] is not None
                    ) else None
                    candidate_digest = slow_payload.replay_digest(replay_salt) if (
                        slow_payload is not None and replay_salt is not None
                    ) else None
                    if stored_payload_digest != candidate_digest:
                        raise IdempotencyConflict("idempotency key has different protected payload")
                    return self._job(row), True
            job_id = str(uuid4())
            self._connection.execute(
                "INSERT INTO jobs (job_id, request_json, request_fingerprint, state, created_at, "
                "updated_at, idempotency_key, public_payload, lease_owner, lease_until, lease_token_digest, lane) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (job_id, _request_json(request), fingerprint, JobState.QUEUED.value,
                 now, now, stored_idempotency_key, payload, None, None, None, lane.value),
            )
            if protected_plaintext is not None:
                entropy = _payload_entropy(job_id, fingerprint)
                ciphertext = self._payload_codec.protect(protected_plaintext, entropy=entropy)
                if not isinstance(ciphertext, bytes) or not ciphertext:
                    raise PayloadProtectionError("payload codec returned invalid ciphertext")
                if len(ciphertext) > MAX_PROTECTED_PAYLOAD_BYTES:
                    raise PayloadProtectionError("protected payload ciphertext is oversized")
                replay_salt = secrets.token_bytes(16)
                plaintext_digest = sha256(replay_salt + protected_plaintext).hexdigest()
                replay_digest = slow_payload.replay_digest(replay_salt)
                self._connection.execute(
                    "INSERT INTO slow_payloads (job_id, codec_id, ciphertext, plaintext_digest, replay_digest, replay_salt) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (job_id, self._payload_codec.codec_id, sqlite3.Binary(ciphertext),
                     plaintext_digest, replay_digest, sqlite3.Binary(replay_salt)),
                )
            self._append_event(job_id, EventKind.CREATED, JobState.QUEUED, now, json.loads(payload))
            return self.get(job_id), False

    def get(self, job_id: str) -> Job:
        with self._lock:
            row = self._connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            raise UnknownJob(job_id)
        return self._job(row)

    get_job = get

    def recent_jobs(self, limit: int = 50) -> tuple[Job, ...]:
        """Return bounded newest-first job snapshots for the local status projection."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("job limit must be between 1 and 100")
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM jobs ORDER BY updated_at DESC, job_id DESC LIMIT ?", (limit,)
            ).fetchall()
        return tuple(self._job(row) for row in rows)

    def transition(self, job_id: str, state: JobState | str, *, public_payload: Mapping[str, Any] | None = None,
                   kind: EventKind | str = EventKind.TRANSITIONED, _expected_state: JobState | None = None,
                   _expected_owner: str | None = None, _lease_expired_at: float | None = None,
                   _expected_token_digest: str | None = None,
                   _worker_completion: bool = False, _require_unexpired: bool = False,
                   _protected_result: ProtectedTaskResult | None = None) -> Job:
        try:
            new_state = state if isinstance(state, JobState) else JobState(state)
            event_kind = kind if isinstance(kind, EventKind) else EventKind(kind)
        except ValueError as exc:
            raise InvalidTransition("unknown state or event kind") from exc
        with self._lock, self._transaction():
            row = self._connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                raise UnknownJob(job_id)
            current = JobState(row["state"])
            if _worker_completion and (_expected_owner is None or _expected_token_digest is None):
                raise InvalidTransition("worker completion requires owner and claim token fencing")
            if _protected_result is not None:
                if (
                    not isinstance(_protected_result, ProtectedTaskResult)
                    or _protected_result.job_id != job_id
                    or new_state is not JobState.SUCCEEDED
                    or not _worker_completion
                ):
                    raise InvalidTransition("protected result requires fenced successful completion")
                if self._payload_codec is None:
                    raise PayloadProtectionError("protected payload codec is unavailable")
            if _expected_state is not None and current is not _expected_state:
                raise InvalidTransition("job state changed before fenced transition")
            if _expected_owner is not None and row["lease_owner"] != _expected_owner:
                raise InvalidTransition("worker does not own the job lease")
            if _expected_token_digest is not None:
                stored_digest = row["lease_token_digest"]
                if not isinstance(stored_digest, str) or not compare_digest(stored_digest, _expected_token_digest):
                    raise InvalidTransition("lease token does not own the job claim")
            if _lease_expired_at is not None:
                lease_until = row["lease_until"]
                if lease_until is not None and not math.isfinite(float(lease_until)):
                    raise InvalidTransition("job lease timestamp is invalid")
                if _require_unexpired:
                    check_at = float(self._clock()) if _lease_expired_at is None else _lease_expired_at
                    if not math.isfinite(check_at) or lease_until is None or lease_until <= check_at:
                        raise InvalidTransition("job lease has expired")
                elif lease_until is not None and lease_until > _lease_expired_at:
                    raise InvalidTransition("job lease is still active")
            elif _require_unexpired:
                check_at = float(self._clock())
                lease_until = row["lease_until"]
                if (not math.isfinite(check_at) or lease_until is None
                        or not math.isfinite(float(lease_until)) or lease_until <= check_at):
                    raise InvalidTransition("job lease has expired")
            if (current in {JobState.RUNNING, JobState.CANCEL_REQUESTED} and new_state in {
                    JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED,
                    JobState.ORPHANED, JobState.UNAVAILABLE,
                } and row["lease_owner"] is not None and not _worker_completion
                    and _lease_expired_at is None):
                raise InvalidTransition("claimed work requires owner-fenced completion")
            if new_state not in _ALLOWED[current]:
                raise InvalidTransition(f"{current.value} -> {new_state.value} is not allowed")
            now = float(self._clock())
            payload = _payload_json(public_payload)
            protected_row = None
            if _protected_result is not None:
                plaintext = _protected_result.to_bytes()
                entropy = _result_entropy(job_id, row["request_fingerprint"])
                ciphertext = self._payload_codec.protect(plaintext, entropy=entropy)
                if not isinstance(ciphertext, bytes) or not ciphertext:
                    raise PayloadProtectionError("protected result ciphertext is invalid")
                if len(ciphertext) > MAX_PROTECTED_PAYLOAD_BYTES:
                    raise PayloadProtectionError("protected result ciphertext is oversized")
                protected_row = (
                    job_id, self._payload_codec.codec_id, sqlite3.Binary(ciphertext),
                    sha256(plaintext).hexdigest(), now,
                )
            clear_lease = new_state in {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED,
                                        JobState.ORPHANED, JobState.UNAVAILABLE}
            self._connection.execute(
                "UPDATE jobs SET state = ?, updated_at = ?, public_payload = ?, "
                "lease_owner = CASE WHEN ? THEN NULL ELSE lease_owner END, "
                "lease_until = CASE WHEN ? THEN NULL ELSE lease_until END, "
                "lease_token_digest = CASE WHEN ? THEN NULL ELSE lease_token_digest END WHERE job_id = ?",
                (new_state.value, now, payload, clear_lease, clear_lease, clear_lease, job_id),
            )
            if protected_row is not None:
                self._connection.execute(
                    "INSERT INTO protected_results "
                    "(job_id, codec_id, ciphertext, plaintext_digest, created_at) "
                    "VALUES (?, ?, ?, ?, ?)", protected_row,
                )
            self._append_event(job_id, event_kind, new_state, now, json.loads(payload))
            return self.get(job_id)

    def request_cancel(self, job_id: str) -> Job:
        # Read, decide, update, and append the event under one write transaction.  A concurrent
        # canceller therefore observes either the pre-cancel state or the committed result, never
        # a stale state between a separate read and transition call.
        with self._lock, self._transaction():
            row = self._connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                raise UnknownJob(job_id)
            current = JobState(row["state"])
            if current in {JobState.QUEUED, JobState.RUNNING}:
                new_state = JobState.CANCELLED if current is JobState.QUEUED else JobState.CANCEL_REQUESTED
                now = float(self._clock())
                payload = _payload_json({"reason": "cancel_requested"})
                self._connection.execute(
                    "UPDATE jobs SET state = ?, updated_at = ?, public_payload = ?, "
                    "lease_owner = CASE WHEN ? THEN NULL ELSE lease_owner END, "
                    "lease_until = CASE WHEN ? THEN NULL ELSE lease_until END, "
                    "lease_token_digest = CASE WHEN ? THEN NULL ELSE lease_token_digest END WHERE job_id = ?",
                    (new_state.value, now, payload, new_state is JobState.CANCELLED,
                     new_state is JobState.CANCELLED, new_state is JobState.CANCELLED, job_id),
                )
                self._append_event(job_id, EventKind.CANCEL_REQUESTED, new_state, now, json.loads(payload))
            # A repeated request, including one racing a terminal transition, is a no-op.
            return self.get(job_id)

    cancel = request_cancel
    cancel_job = request_cancel
    transition_job = transition

    def claim_next(self, worker_id: str, *, lane: Lane | str = Lane.SLOW,
                   lease_seconds: float = 30.0) -> JobClaim | None:
        """Atomically claim one queued outbox item for a worker process.

        SQLite's write transaction serializes competing processes.  A claimed job is RUNNING and
        carries a bounded lease, so a second process cannot deliver the same queued item.
        """
        if not isinstance(worker_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", worker_id):
            raise ValueError("invalid worker id")
        try:
            lane = lane if isinstance(lane, Lane) else Lane(lane)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid job lane") from exc
        if (not isinstance(lease_seconds, (int, float)) or isinstance(lease_seconds, bool)
                or not math.isfinite(float(lease_seconds)) or lease_seconds <= 0 or lease_seconds > 86_400):
            raise ValueError("lease_seconds must be bounded and positive")
        with self._lock, self._transaction():
            existing = self._connection.execute(
                "SELECT 1 FROM jobs WHERE lease_owner = ? AND state IN (?, ?) LIMIT 1",
                (worker_id, JobState.RUNNING.value, JobState.CANCEL_REQUESTED.value),
            ).fetchone()
            if existing is not None:
                return None
            row = self._connection.execute(
                "SELECT * FROM jobs WHERE state = ? AND lane = ? ORDER BY created_at, job_id LIMIT 1",
                (JobState.QUEUED.value, lane.value),
            ).fetchone()
            if row is None:
                return None
            now = float(self._clock())
            lease_until = now + float(lease_seconds)
            lease_token = secrets.token_urlsafe(32)
            lease_token_digest = _lease_token_digest(lease_token)
            payload = _payload_json({"worker_id": worker_id, "code": "claimed"})
            self._connection.execute(
                "UPDATE jobs SET state = ?, updated_at = ?, public_payload = ?, lease_owner = ?, lease_until = ?, "
                "lease_token_digest = ? WHERE job_id = ?",
                (JobState.RUNNING.value, now, payload, worker_id, lease_until,
                 lease_token_digest, row["job_id"]),
            )
            self._append_event(row["job_id"], EventKind.TRANSITIONED, JobState.RUNNING, now,
                               json.loads(payload))
            return JobClaim(self.get(row["job_id"]), lease_token)

    claim = claim_next
    claim_job = claim_next
    poll = claim_next

    def claimed_jobs(self, worker_id: str) -> tuple[Job, ...]:
        """Return public records for claims owned by one worker; never expose claim digests."""
        if not isinstance(worker_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", worker_id):
            raise ValueError("invalid worker id")
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM jobs WHERE lease_owner = ? AND state IN (?, ?) ORDER BY created_at, job_id",
                (worker_id, JobState.RUNNING.value, JobState.CANCEL_REQUESTED.value),
            ).fetchall()
        return tuple(self._job(row) for row in rows)

    def recover_orphans(self) -> list[Job]:
        now = float(self._clock())
        with self._lock:
            ids = [row["job_id"] for row in self._connection.execute(
                "SELECT job_id FROM jobs WHERE state IN (?, ?) AND (lease_until IS NULL OR lease_until <= ?) "
                "ORDER BY created_at", (JobState.RUNNING.value, JobState.CANCEL_REQUESTED.value, now)
            )]
        recovered: list[Job] = []
        for job_id in ids:
            try:
                current = self.get(job_id).state
                target = JobState.ORPHANED if current is JobState.RUNNING else JobState.CANCELLED
                recovered.append(self.transition(
                    job_id, target, kind=EventKind.RECOVERED,
                    public_payload={"reason": "worker_restart"}, _expected_state=current,
                    _lease_expired_at=now,
                ))
            except InvalidTransition:
                # Another owner may have completed/cancelled the job after the snapshot above.
                # Its committed terminal state is authoritative; recovery must continue.
                continue
            except UnknownJob:
                continue
        return recovered

    recover_restart_orphans = recover_orphans

    def renew_lease(self, job_id: str, worker_id: str, lease_token: str, *,
                    lease_seconds: float = 30.0) -> Job:
        """Extend only the current owner's running lease; stale owners cannot revive work."""
        if not isinstance(worker_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", worker_id):
            raise ValueError("invalid worker id")
        if (not isinstance(lease_seconds, (int, float)) or isinstance(lease_seconds, bool)
                or not math.isfinite(float(lease_seconds)) or lease_seconds <= 0 or lease_seconds > 86_400):
            raise ValueError("lease_seconds must be bounded and positive")
        with self._lock, self._transaction():
            row = self._connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                raise UnknownJob(job_id)
            now = float(self._clock())
            lease_until = row["lease_until"]
            if (JobState(row["state"]) is not JobState.RUNNING or row["lease_owner"] != worker_id
                    or lease_until is None or not math.isfinite(float(lease_until)) or lease_until <= now
                    or not isinstance(row["lease_token_digest"], str)
                    or not compare_digest(row["lease_token_digest"], _lease_token_digest(lease_token))):
                raise InvalidTransition("only the current running lease owner may renew")
            self._connection.execute(
                "UPDATE jobs SET updated_at = ?, lease_until = ? WHERE job_id = ?",
                (now, now + float(lease_seconds), job_id),
            )
            return self.get(job_id)

    def complete_success(self, job_id: str, worker_id: str, lease_token: str, *,
                         public_payload: Mapping[str, Any] | None = None,
                         protected_result: ProtectedTaskResult | None = None) -> Job:
        if protected_result is not None:
            if public_payload is not None:
                raise ValueError("protected completion owns its public projection")
            public_payload = {
                "summary": "Private result available.",
                "result_available": True,
            }
        return self.transition(job_id, JobState.SUCCEEDED, public_payload=public_payload,
                               _expected_state=JobState.RUNNING, _expected_owner=worker_id,
                               _expected_token_digest=_lease_token_digest(lease_token),
                               _worker_completion=True,
                               _require_unexpired=True, _protected_result=protected_result)

    def complete_failure(self, job_id: str, worker_id: str, lease_token: str, *,
                         public_payload: Mapping[str, Any] | None = None) -> Job:
        return self.transition(job_id, JobState.FAILED, public_payload=public_payload,
                               _expected_state=JobState.RUNNING, _expected_owner=worker_id,
                               _expected_token_digest=_lease_token_digest(lease_token),
                               _worker_completion=True,
                               _require_unexpired=True)

    def acknowledge_cancel(self, job_id: str, worker_id: str, lease_token: str, *,
                           public_payload: Mapping[str, Any] | None = None) -> Job:
        return self.transition(job_id, JobState.CANCELLED, public_payload=public_payload,
                               _expected_state=JobState.CANCEL_REQUESTED, _expected_owner=worker_id,
                               _expected_token_digest=_lease_token_digest(lease_token),
                               _worker_completion=True,
                               _require_unexpired=True)

    def get_slow_payload(self, job_id: str, worker_id: str, lease_token: str) -> SlowTaskPayload:
        """Decrypt private worker input only for the current live claim capability."""
        token_digest = _lease_token_digest(lease_token)
        with self._lock, self._transaction():
            row = self._connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                raise UnknownJob(job_id)
            now = float(self._clock())
            if (JobState(row["state"]) is not JobState.RUNNING or row["lease_owner"] != worker_id
                    or row["lease_until"] is None or not math.isfinite(float(row["lease_until"]))
                    or float(row["lease_until"]) <= now
                    or not isinstance(row["lease_token_digest"], str)
                    or not compare_digest(row["lease_token_digest"], token_digest)):
                raise InvalidTransition("only the current live claim may access protected payload")
            payload_row = self._connection.execute(
                "SELECT * FROM slow_payloads WHERE job_id = ?", (job_id,)
            ).fetchone()
            if payload_row is None or self._payload_codec is None:
                raise PayloadProtectionError("protected payload is unavailable")
            if payload_row["codec_id"] != self._payload_codec.codec_id:
                raise PayloadProtectionError("protected payload codec does not match")
            plaintext = self._payload_codec.unprotect(
                bytes(payload_row["ciphertext"]),
                entropy=_payload_entropy(job_id, row["request_fingerprint"]),
            )
            if not isinstance(plaintext, bytes) or len(plaintext) > MAX_PROTECTED_PAYLOAD_BYTES:
                raise PayloadProtectionError("protected payload plaintext is invalid")
            replay_salt = bytes(payload_row["replay_salt"]) if payload_row["replay_salt"] is not None else None
            if replay_salt is None or not compare_digest(
                sha256(replay_salt + plaintext).hexdigest(), payload_row["plaintext_digest"]
            ):
                raise PayloadProtectionError("protected payload integrity check failed")
            payload = SlowTaskPayload.from_bytes(plaintext)
            if payload.request_fingerprint != row["request_fingerprint"]:
                raise PayloadProtectionError("protected payload request binding failed")
            return payload

    def get_protected_result(self, job_id: str) -> ProtectedTaskResult:
        """Decrypt a completed private result for the local current-user application."""
        with self._lock:
            row = self._connection.execute(
                "SELECT j.state, j.request_fingerprint, r.codec_id, r.ciphertext, "
                "r.plaintext_digest FROM jobs j LEFT JOIN protected_results r "
                "ON r.job_id = j.job_id WHERE j.job_id = ?", (job_id,),
            ).fetchone()
        if row is None:
            raise UnknownJob(job_id)
        if JobState(row["state"]) is not JobState.SUCCEEDED or row["ciphertext"] is None:
            raise InvalidTransition("protected result is unavailable")
        if self._payload_codec is None or row["codec_id"] != self._payload_codec.codec_id:
            raise PayloadProtectionError("protected result codec does not match")
        entropy = _result_entropy(job_id, row["request_fingerprint"])
        plaintext = self._payload_codec.unprotect(bytes(row["ciphertext"]), entropy=entropy)
        if not isinstance(plaintext, bytes) or not compare_digest(
            sha256(plaintext).hexdigest(), row["plaintext_digest"],
        ):
            raise PayloadProtectionError("protected result integrity check failed")
        try:
            result = ProtectedTaskResult.from_bytes(plaintext)
        except ValueError:
            raise PayloadProtectionError("protected result plaintext is invalid") from None
        if result.job_id != job_id:
            raise PayloadProtectionError("protected result binding failed")
        return result

    succeed = complete_success
    fail = complete_failure
    cancel_acknowledged = acknowledge_cancel

    def events(self, job_id: str) -> tuple[JobEvent, ...]:
        self.get(job_id)
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM job_events WHERE job_id = ? ORDER BY sequence ASC", (job_id,)
            ).fetchall()
        return tuple(JobEvent(row["job_id"], row["sequence"], EventKind(row["kind"]),
                              JobState(row["state"]), row["timestamp"], json.loads(row["public_payload"]))
                     for row in rows)

    list_events = events
    get_events = events

    def _append_event(self, job_id: str, kind: EventKind, state: JobState, timestamp: float,
                      payload: Mapping[str, Any]) -> None:
        row = self._connection.execute("SELECT COALESCE(MAX(sequence), 0) + 1 FROM job_events WHERE job_id = ?",
                                       (job_id,)).fetchone()
        sequence = int(row[0])
        self._connection.execute("INSERT INTO job_events VALUES (?, ?, ?, ?, ?, ?)",
                                 (job_id, sequence, kind.value, state.value, timestamp, _payload_json(payload)))

    def _job(self, row: sqlite3.Row) -> Job:
        return Job(row["job_id"], _request_from_json(row["request_json"]), JobState(row["state"]),
                   row["created_at"], row["updated_at"], row["idempotency_key"],
                   json.loads(row["public_payload"]), row["lease_owner"], row["lease_until"],
                   Lane(row["lane"]))

    class _Transaction:
        def __init__(self, owner: "JobStore") -> None:
            self.owner = owner

        def __enter__(self) -> "JobStore._Transaction":
            self.owner._connection.execute("BEGIN IMMEDIATE")
            return self

        def __exit__(self, exc_type: Any, *_args: Any) -> None:
            if exc_type:
                self.owner._connection.rollback()
            else:
                self.owner._connection.commit()

    def _transaction(self) -> "JobStore._Transaction":
        return self._Transaction(self)


__all__ = ["JobStore", "JobStoreError", "InvalidTransition", "IdempotencyConflict", "UnknownJob",
           "redact_public_payload", "MAX_PUBLIC_PAYLOAD_BYTES"]
