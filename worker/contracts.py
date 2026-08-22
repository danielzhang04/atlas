"""Immutable contracts for standalone Atlas work.

These types are deliberately execution-neutral.  A request can be classified and persisted
without importing an adapter, connector, or orchestration runtime.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import json
import math
import re
from types import MappingProxyType
from typing import Any, Mapping
from uuid import uuid4


MAX_CONTRACT_STRING = 512
MAX_METADATA_DEPTH = 4
MAX_METADATA_ITEMS = 64
MAX_METADATA_BYTES = 8_192
MAX_SLOW_INSTRUCTION_BYTES = 16_384
MAX_PROTECTED_RESULT_BYTES = 16_384


class Lane(str, Enum):
    FAST = "fast"
    SLOW = "slow"


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    ORPHANED = "orphaned"
    UNAVAILABLE = "unavailable"


class EventKind(str, Enum):
    CREATED = "created"
    TRANSITIONED = "transitioned"
    CANCEL_REQUESTED = "cancel_requested"
    RECOVERED = "recovered"
    RESULT = "result"


def _bounded_text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    if len(value) > MAX_CONTRACT_STRING:
        raise ValueError(f"{label} exceeds the bounded string limit")
    return value.strip()


def _tuple_strings(values: Any, label: str) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        values = (values,)
    try:
        result = tuple(values)
    except TypeError as exc:
        raise TypeError(f"{label} must be a string sequence") from exc
    return tuple(_bounded_text(value, label) for value in result)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(_freeze(item) for item in value)
    return value


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(item) for item in value]
    return value


def _bounded_metadata(value: Any, *, depth: int = 0, state: list[bool] | None = None) -> Any:
    """Freeze metadata while remembering when caller input exceeded policy bounds."""
    state = state if state is not None else [False]
    if depth >= MAX_METADATA_DEPTH:
        state[0] = True
        return "[NESTED_METADATA_REDACTED]"
    if isinstance(value, Mapping):
        bounded: dict[str, Any] = {}
        iterator = iter(value.items())
        for _ in range(MAX_METADATA_ITEMS):
            try:
                key, item = next(iterator)
            except StopIteration:
                break
            key_text = str(key)
            if len(key_text) > MAX_CONTRACT_STRING:
                state[0] = True
                key_text = key_text[:MAX_CONTRACT_STRING]
            bounded[key_text] = _bounded_metadata(item, depth=depth + 1, state=state)
        else:
            try:
                next(iterator)
                state[0] = True
            except StopIteration:
                pass
        return MappingProxyType(bounded)
    if isinstance(value, (list, tuple, set, frozenset)):
        iterator = iter(value)
        bounded = []
        for _ in range(MAX_METADATA_ITEMS):
            try:
                bounded.append(_bounded_metadata(next(iterator), depth=depth + 1, state=state))
            except StopIteration:
                break
        else:
            try:
                next(iterator)
                state[0] = True
            except StopIteration:
                pass
        return tuple(bounded)
    if isinstance(value, str):
        if len(value) > MAX_CONTRACT_STRING:
            state[0] = True
            return value[:MAX_CONTRACT_STRING]
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    state[0] = True
    return "[UNSUPPORTED_METADATA]"


@dataclass(frozen=True, slots=True)
class Request:
    """A bounded description of requested work, without executable instructions."""

    operation: str
    target: str | None = None
    resource: str | None = None
    operations: tuple[str, ...] = ()
    targets: tuple[str, ...] = ()
    resources: tuple[str, ...] = ()
    source: str | None = None
    sources: tuple[str, ...] = ()
    app: str | None = None
    apps: tuple[str, ...] = ()
    cardinality: int = 1
    steps: int = 1
    risk: str = "unknown"
    cross_source: bool = False
    cross_app: bool = False
    research: bool = False
    discovery: bool = False
    iteration: bool = False
    verification: bool = False
    durable_artifact: bool = False
    io_items: int = 1
    io_bytes: int = 0
    artifact: str | None = None
    # Host-bound, operation-specific arguments. The voice interpreter cannot populate this
    # field; deterministic parsers or trusted local callers must bind it before fast execution.
    parameters: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)
    request_id: str = field(default_factory=lambda: str(uuid4()))
    metadata: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)
    _metadata_oversized: bool = field(init=False, default=False, repr=False, compare=False)
    _parameters_oversized: bool = field(init=False, default=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "operation", _bounded_text(self.operation, "operation"))
        object.__setattr__(self, "risk", _bounded_text(self.risk, "risk"))
        for label in ("target", "resource", "source", "app", "artifact"):
            value = getattr(self, label)
            if value is not None:
                object.__setattr__(self, label, _bounded_text(value, label))
        object.__setattr__(self, "operations", _tuple_strings(self.operations, "operations"))
        object.__setattr__(self, "targets", _tuple_strings(self.targets, "targets"))
        object.__setattr__(self, "resources", _tuple_strings(self.resources, "resources"))
        object.__setattr__(self, "sources", _tuple_strings(self.sources, "sources"))
        object.__setattr__(self, "apps", _tuple_strings(self.apps, "apps"))
        if isinstance(self.cardinality, bool) or not isinstance(self.cardinality, int) or self.cardinality < 1:
            raise ValueError("cardinality must be a positive integer")
        if isinstance(self.steps, bool) or not isinstance(self.steps, int) or self.steps < 1:
            raise ValueError("steps must be a positive integer")
        if isinstance(self.io_items, bool) or not isinstance(self.io_items, int) or self.io_items < 0:
            raise ValueError("io_items must be a non-negative integer")
        if isinstance(self.io_bytes, bool) or not isinstance(self.io_bytes, int) or self.io_bytes < 0:
            raise ValueError("io_bytes must be a non-negative integer")
        object.__setattr__(self, "request_id", _bounded_text(self.request_id, "request_id"))
        state = [False]
        metadata = _bounded_metadata(self.metadata, state=state)
        if len(json.dumps(_plain(metadata), sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")) > MAX_METADATA_BYTES:
            state[0] = True
        object.__setattr__(self, "metadata", metadata)
        object.__setattr__(self, "_metadata_oversized", state[0])
        parameter_state = [False]
        parameters = _bounded_metadata(self.parameters, state=parameter_state)
        if not isinstance(parameters, Mapping):
            raise TypeError("parameters must be a mapping")
        if len(json.dumps(_plain(parameters), sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")) > MAX_METADATA_BYTES:
            parameter_state[0] = True
        object.__setattr__(self, "parameters", parameters)
        object.__setattr__(self, "_parameters_oversized", parameter_state[0])

    @property
    def metadata_oversized(self) -> bool:
        return self._metadata_oversized

    @property
    def parameters_oversized(self) -> bool:
        return self._parameters_oversized

    @property
    def operation_count(self) -> int:
        return len(self.operations) if self.operations else 1

    @property
    def target_count(self) -> int:
        return len(self.targets) + (1 if self.target else 0)

    @property
    def resource_count(self) -> int:
        return len(self.resources) + (1 if self.resource else 0)

    @property
    def source_count(self) -> int:
        return len(self.sources) + (1 if self.source else 0)

    @property
    def app_count(self) -> int:
        return len(self.apps) + (1 if self.app else 0)

    def canonical(self) -> dict[str, Any]:
        """Return only stable, non-executable request fields for hashing/persistence."""
        return {
            "operation": self.operation, "target": self.target, "resource": self.resource,
            "operations": self.operations, "targets": self.targets, "resources": self.resources,
            "source": self.source, "sources": self.sources, "app": self.app, "apps": self.apps,
            "cardinality": self.cardinality, "steps": self.steps, "cross_source": self.cross_source,
            "risk": self.risk,
            "cross_app": self.cross_app, "research": self.research, "discovery": self.discovery,
            "iteration": self.iteration, "verification": self.verification,
            "durable_artifact": self.durable_artifact, "io_items": self.io_items,
            "io_bytes": self.io_bytes, "artifact": self.artifact,
            "parameters": _plain(self.parameters),
        }

    def fingerprint(self) -> str:
        body = json.dumps(self.canonical(), sort_keys=True, separators=(",", ":"), default=str)
        return sha256(body.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RouteDecision:
    lane: Lane
    reasons: tuple[str, ...] = ()

    @property
    def is_fast(self) -> bool:
        return self.lane is Lane.FAST


@dataclass(frozen=True, slots=True)
class Job:
    job_id: str
    request: Request
    state: JobState
    created_at: float
    updated_at: float
    # This is storage metadata only.  Callers must submit the original raw key to JobStore.create;
    # this digest is deliberately not a replayable input key.
    idempotency_digest: str | None = None
    public_payload: Mapping[str, Any] = field(default_factory=dict, repr=False)
    lease_owner: str | None = None
    lease_until: float | None = None
    lane: Lane = Lane.SLOW

    def __post_init__(self) -> None:
        if not isinstance(self.job_id, str) or not self.job_id.strip():
            raise ValueError("job_id must be a non-empty string")
        if not isinstance(self.state, JobState):
            object.__setattr__(self, "state", JobState(self.state))
        if self.idempotency_digest is not None and (not isinstance(self.idempotency_digest, str) or not self.idempotency_digest.strip()):
            raise ValueError("idempotency_digest must be non-empty when provided")
        if self.lease_owner is not None and (not isinstance(self.lease_owner, str) or not self.lease_owner.strip()):
            raise ValueError("lease_owner must be non-empty when provided")
        if self.lease_until is not None and not isinstance(self.lease_until, (int, float)):
            raise ValueError("lease_until must be numeric when provided")
        if not isinstance(self.lane, Lane):
            object.__setattr__(self, "lane", Lane(self.lane))
        object.__setattr__(self, "public_payload", _freeze(self.public_payload))


@dataclass(frozen=True, slots=True)
class JobClaim:
    """A leased job plus its unpersisted per-claim capability token."""

    job: Job
    lease_token: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.job, Job) or self.job.state is not JobState.RUNNING:
            raise ValueError("a claim requires a running Job")
        if not isinstance(self.lease_token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", self.lease_token):
            raise ValueError("invalid lease token")

    @property
    def job_id(self) -> str:
        return self.job.job_id

    @property
    def request(self) -> Request:
        return self.job.request

    @property
    def state(self) -> JobState:
        return self.job.state

    @property
    def lease_owner(self) -> str | None:
        return self.job.lease_owner

    @property
    def lease_until(self) -> float | None:
        return self.job.lease_until

    @property
    def lane(self) -> Lane:
        return self.job.lane


@dataclass(frozen=True, slots=True)
class SlowTaskPayload:
    """Private worker input that is encrypted separately from public job metadata."""

    instruction: str = field(repr=False)
    request_fingerprint: str
    submitted_at: float
    version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.instruction, str) or not self.instruction.strip():
            raise ValueError("slow instruction must be non-empty")
        if len(self.instruction.encode("utf-8")) > MAX_SLOW_INSTRUCTION_BYTES:
            raise ValueError("slow instruction exceeds the protected payload limit")
        if not isinstance(self.request_fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", self.request_fingerprint):
            raise ValueError("invalid slow payload request fingerprint")
        if (not isinstance(self.submitted_at, (int, float)) or isinstance(self.submitted_at, bool)
                or not math.isfinite(float(self.submitted_at))):
            raise ValueError("invalid slow payload timestamp")
        if self.version != 1:
            raise ValueError("unsupported slow payload version")

    def to_bytes(self) -> bytes:
        return json.dumps({
            "version": self.version,
            "instruction": self.instruction,
            "request_fingerprint": self.request_fingerprint,
            "submitted_at": float(self.submitted_at),
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    def replay_digest(self, salt: bytes) -> str:
        """Bind replay to exact input without timestamp sensitivity or a bare dictionary hash."""
        if not isinstance(salt, bytes) or len(salt) < 16:
            raise ValueError("slow payload replay salt is invalid")
        body = json.dumps({
            "version": self.version,
            "instruction": self.instruction,
            "request_fingerprint": self.request_fingerprint,
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return sha256(salt + body).hexdigest()

    @classmethod
    def from_bytes(cls, value: bytes) -> "SlowTaskPayload":
        if not isinstance(value, bytes) or len(value) > MAX_SLOW_INSTRUCTION_BYTES + 1_024:
            raise ValueError("invalid protected slow payload")
        try:
            decoded = json.loads(value.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("invalid protected slow payload") from None
        if not isinstance(decoded, dict) or set(decoded) != {
            "version", "instruction", "request_fingerprint", "submitted_at",
        }:
            raise ValueError("invalid protected slow payload schema")
        return cls(**decoded)


@dataclass(frozen=True, slots=True)
class ProtectedTaskResult:
    """Private terminal output encrypted separately from public job and event projections."""

    job_id: str
    answer: str = field(repr=False)
    candidate_digest: str
    evidence_ids: tuple[str, ...] = ()
    artifact_name: str | None = None
    version: int = 1

    def __post_init__(self) -> None:
        try:
            from uuid import UUID
            UUID(self.job_id)
        except (TypeError, ValueError):
            raise ValueError("invalid protected result job id") from None
        if not isinstance(self.answer, str) or not self.answer.strip():
            raise ValueError("protected result answer must be non-empty")
        if len(self.answer.encode("utf-8")) > MAX_PROTECTED_RESULT_BYTES:
            raise ValueError("protected result answer exceeds the bounded limit")
        if not isinstance(self.candidate_digest, str) or re.fullmatch(
            r"[0-9a-f]{64}", self.candidate_digest,
        ) is None:
            raise ValueError("invalid protected result candidate digest")
        if sha256(self.answer.encode("utf-8")).hexdigest() != self.candidate_digest:
            raise ValueError("protected result candidate digest does not match answer")
        if not isinstance(self.evidence_ids, tuple) or len(self.evidence_ids) > 32:
            raise ValueError("invalid protected result evidence ids")
        if len(set(self.evidence_ids)) != len(self.evidence_ids) or any(
            not isinstance(item, str) or re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", item) is None
            for item in self.evidence_ids
        ):
            raise ValueError("invalid protected result evidence ids")
        if self.artifact_name is not None and (
            not isinstance(self.artifact_name, str)
            or not self.artifact_name.strip()
            or len(self.artifact_name.encode("utf-8")) > 255
            or self.artifact_name in {".", ".."}
            or "/" in self.artifact_name
            or "\\" in self.artifact_name
            or "\x00" in self.artifact_name
        ):
            raise ValueError("invalid protected result artifact name")
        if self.version != 1:
            raise ValueError("unsupported protected result version")

    def to_bytes(self) -> bytes:
        return json.dumps({
            "version": self.version,
            "job_id": self.job_id,
            "answer": self.answer,
            "candidate_digest": self.candidate_digest,
            "evidence_ids": list(self.evidence_ids),
            "artifact_name": self.artifact_name,
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    @classmethod
    def from_bytes(cls, value: bytes) -> "ProtectedTaskResult":
        if not isinstance(value, bytes) or len(value) > 32_768:
            raise ValueError("invalid protected result")
        try:
            decoded = json.loads(value.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("invalid protected result") from None
        if not isinstance(decoded, dict) or set(decoded) not in ({
            "version", "job_id", "answer", "candidate_digest", "evidence_ids", "artifact_name",
        }, {
            "version", "job_id", "answer", "candidate_digest", "evidence_ids",
        }):
            raise ValueError("invalid protected result schema")
        evidence_ids = decoded.get("evidence_ids")
        if not isinstance(evidence_ids, list):
            raise ValueError("invalid protected result schema")
        decoded["evidence_ids"] = tuple(evidence_ids)
        decoded.setdefault("artifact_name", None)
        return cls(**decoded)


@dataclass(frozen=True, slots=True)
class JobEvent:
    job_id: str
    sequence: int
    kind: EventKind
    state: JobState
    timestamp: float
    public_payload: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.sequence, int) or self.sequence < 1:
            raise ValueError("event sequence must be positive")
        if not isinstance(self.kind, EventKind):
            object.__setattr__(self, "kind", EventKind(self.kind))
        if not isinstance(self.state, JobState):
            object.__setattr__(self, "state", JobState(self.state))
        object.__setattr__(self, "public_payload", _freeze(self.public_payload))


@dataclass(frozen=True, slots=True)
class Result:
    job_id: str
    status: str
    summary: str = ""
    error_code: str | None = None
    public_payload: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.status not in {"succeeded", "failed", "cancelled", "unavailable"}:
            raise ValueError("invalid result status")
        if not isinstance(self.summary, str):
            raise TypeError("summary must be a string")
        object.__setattr__(self, "public_payload", _freeze(self.public_payload))


RequestContract = Request
WorkRequest = Request
ImmutableRequest = Request
Route = RouteDecision
JobRecord = Job
ClaimedJob = JobClaim
Event = JobEvent
JobResult = Result


def utc_timestamp() -> float:
    return datetime.now(timezone.utc).timestamp()
