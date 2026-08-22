"""In-memory, trusted-channel action proposal broker.

Models may prepare actions, but only a caller authenticated as a configured trusted
channel can confirm one.  Proposals bind a canonical parameter hash and are consumed
exactly once by ``execute``.  This module deliberately contains no HTTP/UI/voice code.
"""
from __future__ import annotations

import copy
import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable


PROPOSED = "proposed"
CONFIRMED = "confirmed"
EXECUTING = "executing"
SUCCEEDED = "succeeded"
FAILED = "failed"
EXPIRED = "expired"
CANCELLED = "cancelled"
TERMINAL = frozenset({SUCCEEDED, FAILED, EXPIRED, CANCELLED})


class ActionError(RuntimeError):
    pass


class ActionExpired(ActionError):
    pass


class ActionNotConfirmed(ActionError):
    pass


class ReplayDetected(ActionError):
    pass


def parameter_hash(parameters: dict[str, Any]) -> str:
    """Stable digest for JSON-compatible parameters; no implicit stringification."""
    try:
        canonical = json.dumps(parameters, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                               allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("action parameters must be JSON-compatible") from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ActionSnapshot:
    proposal_id: str
    capability_id: str
    parameters: dict[str, Any]
    parameters_hash: str
    status: str
    created_at: float
    expires_at: float
    confirmation_channel: str | None = None
    receipt: Any = None
    failure: str | None = None
    idempotency_key: str | None = None
    session_id: str | None = None
    device_id: str | None = None


@dataclass
class _Action:
    snapshot: ActionSnapshot
    executor: Callable[[dict[str, Any]], Any]


class ActionBroker:
    """Small injectable broker suitable for a local paired UI/service process.

    ``trusted_channels`` must be supplied by the service boundary, not model input.
    The broker never accepts a model's boolean confirmation field.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 id_factory: Callable[[], str] | None = None,
                 trusted_channels: frozenset[str] | set[str] = frozenset({"ui", "service"}),
                 default_ttl_s: float = 300.0,
                 context_provider: Callable[[], tuple[str, str] | None] | None = None,
                 receipt_journal: Any | None = None) -> None:
        self._clock = clock
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._trusted_channels = frozenset(trusted_channels)
        self._default_ttl_s = float(default_ttl_s)
        self._context_provider = context_provider
        self._receipt_journal = receipt_journal
        self._actions: dict[str, _Action] = {}
        self._idempotency: dict[tuple[str, str], str] = {}

    def propose(self, capability_id: str, parameters: dict[str, Any], executor: Callable[[dict[str, Any]], Any],
                *, ttl_s: float | None = None, idempotency_key: str | None = None,
                session_id: str | None = None, device_id: str | None = None) -> ActionSnapshot:
        if not capability_id or not callable(executor):
            raise ValueError("capability id and callable executor are required")
        requested_hash = parameter_hash(parameters)
        if idempotency_key is not None:
            key = (capability_id, idempotency_key)
            existing = self._idempotency.get(key)
            if existing is not None:
                snapshot = self.get(existing)
                if snapshot.parameters_hash != requested_hash:
                    raise ActionError("idempotency key was reused with different parameters")
                return snapshot
        now = self._clock()
        ttl = self._default_ttl_s if ttl_s is None else float(ttl_s)
        if ttl <= 0:
            raise ValueError("proposal ttl must be positive")
        params = copy.deepcopy(parameters)
        if self._context_provider is not None and (session_id is None or device_id is None):
            context = self._context_provider()
            if context is None:
                raise ActionError("trusted Atlas UI must be paired before preparing an action")
            session_id, device_id = context
        proposal_id = self._id_factory()
        if proposal_id in self._actions:
            raise ActionError("proposal id collision")
        record = ActionSnapshot(proposal_id, capability_id, params, requested_hash, PROPOSED,
                                now, now + ttl, idempotency_key=idempotency_key,
                                session_id=session_id, device_id=device_id)
        self._actions[record.proposal_id] = _Action(record, executor)
        if idempotency_key is not None:
            self._idempotency[(capability_id, idempotency_key)] = record.proposal_id
        return record

    def get(self, proposal_id: str) -> ActionSnapshot:
        try:
            self._expire_if_needed(proposal_id)
            return self._copy(self._actions[proposal_id].snapshot)
        except KeyError as exc:
            raise ActionError("unknown proposal") from exc

    def confirm(self, proposal_id: str, *, channel: str, parameters_hash: str,
                session_id: str | None = None, device_id: str | None = None) -> ActionSnapshot:
        """Record a confirmation event emitted by a trusted UI/service channel."""
        if channel not in self._trusted_channels:
            raise ActionError("confirmation channel is not trusted")
        action = self._lookup_live(proposal_id)
        record = action.snapshot
        self._check_context(record, session_id=session_id, device_id=device_id)
        if record.status == CONFIRMED:
            if record.confirmation_channel == channel and record.parameters_hash == parameters_hash:
                return self._copy(record)  # idempotent delivery of the same UI event
            raise ReplayDetected("proposal was already confirmed")
        if record.status != PROPOSED:
            raise ReplayDetected(f"proposal cannot be confirmed from {record.status}")
        if parameters_hash != record.parameters_hash:
            raise ActionError("confirmation does not bind the proposed parameters")
        action.snapshot = self._replace(record, status=CONFIRMED, confirmation_channel=channel)
        return self._copy(action.snapshot)

    def cancel(self, proposal_id: str, *, channel: str, parameters_hash: str,
               session_id: str | None = None, device_id: str | None = None) -> ActionSnapshot:
        """Cancel a pending proposal through a trusted boundary."""
        if channel not in self._trusted_channels:
            raise ActionError("cancellation channel is not trusted")
        action = self._lookup_live(proposal_id)
        record = action.snapshot
        self._check_context(record, session_id=session_id, device_id=device_id)
        if parameters_hash != record.parameters_hash:
            raise ActionError("cancellation does not bind the proposed parameters")
        if record.status not in (PROPOSED, CONFIRMED):
            raise ReplayDetected(f"proposal cannot be cancelled from {record.status}")
        action.snapshot = self._replace(record, status=CANCELLED,
                                        confirmation_channel=channel,
                                        failure="cancelled by trusted user interface",
                                        receipt={"outcome": "cancelled"})
        self._journal_terminal(action.snapshot)
        return self._copy(action.snapshot)

    def list(self, *, include_terminal: bool = True) -> list[ActionSnapshot]:
        """Return immutable copies, newest first, expiring stale proposals first."""
        for proposal_id in tuple(self._actions):
            self._expire_if_needed(proposal_id)
        snapshots = [self._copy(action.snapshot) for action in self._actions.values()]
        if not include_terminal:
            snapshots = [item for item in snapshots if item.status not in TERMINAL]
        return sorted(snapshots, key=lambda item: item.created_at, reverse=True)

    def execute(self, proposal_id: str, *, parameters_hash: str | None = None) -> ActionSnapshot:
        action = self._lookup_live(proposal_id)
        record = action.snapshot
        if record.status == PROPOSED:
            raise ActionNotConfirmed("proposal has not received a trusted confirmation")
        if record.status in TERMINAL or record.status == EXECUTING:
            raise ReplayDetected(f"proposal already consumed ({record.status})")
        if record.status != CONFIRMED:
            raise ReplayDetected(f"proposal cannot execute from {record.status}")
        if parameters_hash is not None and parameters_hash != record.parameters_hash:
            raise ActionError("execution parameters do not match proposal")
        action.snapshot = self._replace(record, status=EXECUTING)
        try:
            receipt = action.executor(copy.deepcopy(record.parameters))
        except Exception as exc:
            action.snapshot = self._replace(action.snapshot, status=FAILED,
                                            failure=f"{type(exc).__name__}: {exc}",
                                            receipt={"outcome": "failed",
                                                     "error_code": type(exc).__name__})
        else:
            action.snapshot = self._replace(action.snapshot, status=SUCCEEDED,
                                            receipt=copy.deepcopy(receipt))
        self._journal_terminal(action.snapshot)
        return self._copy(action.snapshot)

    def journal_rejection(self, proposal_id: str, *, reason_code: str, channel: str,
                          session_id: str | None = None, device_id: str | None = None) -> None:
        """Record a rejected attempt without consuming the still-reviewable proposal."""
        if self._receipt_journal is None:
            return
        action = self._actions.get(proposal_id)
        if action is None:
            return
        record = action.snapshot
        self._receipt_journal.append_rejected(
            proposal_id=record.proposal_id, capability_id=record.capability_id,
            parameters_hash=record.parameters_hash, reason_code=reason_code,
            session_id=session_id, device_id=device_id, confirmation_channel=channel)

    def _lookup_live(self, proposal_id: str) -> _Action:
        try:
            self._expire_if_needed(proposal_id)
            action = self._actions[proposal_id]
            if action.snapshot.status == EXPIRED:
                raise ActionExpired("proposal expired")
            return action
        except KeyError as exc:
            raise ActionError("unknown proposal") from exc

    @staticmethod
    def _check_context(record: ActionSnapshot, *, session_id: str | None,
                       device_id: str | None) -> None:
        if record.session_id is not None and session_id != record.session_id:
            raise ActionError("proposal belongs to a different session")
        if record.device_id is not None and device_id != record.device_id:
            raise ActionError("proposal belongs to a different device")

    def _expire_if_needed(self, proposal_id: str) -> None:
        action = self._actions.get(proposal_id)
        if action is None:
            return
        record = action.snapshot
        if record.status in (PROPOSED, CONFIRMED) and self._clock() >= record.expires_at:
            action.snapshot = self._replace(record, status=EXPIRED, failure="proposal expired",
                                            receipt={"outcome": "expired"})
            self._journal_terminal(action.snapshot)

    def _journal_terminal(self, record: ActionSnapshot) -> None:
        if self._receipt_journal is not None:
            self._receipt_journal.append_terminal(record)

    @staticmethod
    def _replace(record: ActionSnapshot, **changes: Any) -> ActionSnapshot:
        return ActionSnapshot(**{**record.__dict__, **changes})

    @staticmethod
    def _copy(record: ActionSnapshot) -> ActionSnapshot:
        return ActionSnapshot(**{**record.__dict__, "parameters": copy.deepcopy(record.parameters),
                                 "receipt": copy.deepcopy(record.receipt)})
