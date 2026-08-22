"""Redacted append-only local action receipt journal.

This journal is an audit/history projection, not an event store.  It deliberately writes
only fixed identifiers, an immutable parameter hash, status, and a bounded error *code*.
It never serializes action parameters, executor output, exception text, tokens, cookies,
headers, page content, or arbitrary caller-supplied dictionaries.
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


SCHEMA_VERSION = 1
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled", "expired"})
REJECTED_STATUS = "rejected"
ALL_STATUSES = TERMINAL_STATUSES | {REJECTED_STATUS}
MAX_HISTORY = 200
MAX_HISTORY_BYTES = 512_000
_HASH = re.compile(r"^[a-f0-9]{64}$")
_ID = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
_CODE = re.compile(r"^[a-z0-9_.-]{1,64}$")
_SCHEMA_KEYS = frozenset({"version", "timestamp", "proposal_id", "capability_id", "parameters_hash",
                          "status", "session_id", "device_id", "confirmation_channel", "error_code"})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ReceiptJournal:
    """Process-local locked JSONL writer with bounded history reads.

    For multi-process writers, callers must provide a process-level serialization boundary.
    Each record is still emitted as one ``os.write`` call with ``O_APPEND`` after the in-process
    lock, so no code path uses a read/modify/rewrite journal update.
    """

    def __init__(self, path: str | Path, *, clock: Callable[[], datetime] = _utcnow) -> None:
        self._path = Path(path)
        self._clock = clock
        self._lock = threading.Lock()

    def append_terminal(self, snapshot: Any) -> dict[str, Any]:
        """Append one terminal ActionBroker-like snapshot, stripping every unsafe field."""
        status = getattr(snapshot, "status", None)
        if status not in TERMINAL_STATUSES:
            raise ValueError("only terminal action outcomes may be journaled")
        receipt = getattr(snapshot, "receipt", None)
        error_code = None
        if status == "failed" and isinstance(receipt, dict):
            candidate = receipt.get("error_code")
            if isinstance(candidate, str) and _CODE.fullmatch(candidate.casefold()):
                error_code = candidate.casefold()
        record = self._record(
            proposal_id=getattr(snapshot, "proposal_id", None), capability_id=getattr(snapshot, "capability_id", None),
            parameters_hash=getattr(snapshot, "parameters_hash", None), status=status,
            session_id=getattr(snapshot, "session_id", None), device_id=getattr(snapshot, "device_id", None),
            confirmation_channel=getattr(snapshot, "confirmation_channel", None), error_code=error_code,
        )
        self._append(record)
        return dict(record)

    def append_rejected(self, *, proposal_id: str, capability_id: str, parameters_hash: str,
                        reason_code: str, session_id: str | None = None, device_id: str | None = None,
                        confirmation_channel: str | None = None) -> dict[str, Any]:
        """Journal a callable rejection attempt (bad binding, untrusted channel, replay, etc.)."""
        record = self._record(proposal_id=proposal_id, capability_id=capability_id, parameters_hash=parameters_hash,
                              status=REJECTED_STATUS, session_id=session_id, device_id=device_id,
                              confirmation_channel=confirmation_channel, error_code=reason_code)
        self._append(record)
        return dict(record)

    def read_latest(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return at most ``limit`` newest valid, schema-exact records; corrupt lines are ignored."""
        if not isinstance(limit, int) or not 1 <= limit <= MAX_HISTORY:
            raise ValueError(f"history limit must be 1..{MAX_HISTORY}")
        try:
            with self._path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - MAX_HISTORY_BYTES))
                data = handle.read(MAX_HISTORY_BYTES)
        except FileNotFoundError:
            return []
        # A partial leading record (when tail-reading) is never trusted.
        lines = data.splitlines()
        if size > MAX_HISTORY_BYTES and lines:
            lines = lines[1:]
        latest: list[dict[str, Any]] = []
        for raw in reversed(lines):
            if len(latest) >= limit:
                break
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if self._valid_record(parsed):
                latest.append(parsed)
        return latest

    def _record(self, *, proposal_id: Any, capability_id: Any, parameters_hash: Any, status: str,
                session_id: Any, device_id: Any, confirmation_channel: Any, error_code: Any) -> dict[str, Any]:
        for label, value in (("proposal id", proposal_id), ("capability id", capability_id)):
            if not isinstance(value, str) or not _ID.fullmatch(value):
                raise ValueError(f"invalid {label}")
        if not isinstance(parameters_hash, str) or not _HASH.fullmatch(parameters_hash):
            raise ValueError("invalid parameters hash")
        if status not in ALL_STATUSES:
            raise ValueError("invalid receipt status")
        for label, value in (("session id", session_id), ("device id", device_id), ("confirmation channel", confirmation_channel)):
            if value is not None and (not isinstance(value, str) or not _ID.fullmatch(value)):
                raise ValueError(f"invalid {label}")
        if error_code is not None and (not isinstance(error_code, str) or not _CODE.fullmatch(error_code.casefold())):
            raise ValueError("invalid error code")
        now = self._clock()
        if not isinstance(now, datetime):
            raise TypeError("receipt clock must return datetime")
        return {"version": SCHEMA_VERSION, "timestamp": now.astimezone(timezone.utc).isoformat(),
                "proposal_id": proposal_id, "capability_id": capability_id, "parameters_hash": parameters_hash,
                "status": status, "session_id": session_id, "device_id": device_id,
                "confirmation_channel": confirmation_channel,
                "error_code": error_code.casefold() if error_code is not None else None}

    def _append(self, record: dict[str, Any]) -> None:
        body = (json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
            fd = os.open(self._path, flags, 0o600)
            try:
                written = os.write(fd, body)  # exactly one append operation per record
                if written != len(body):
                    raise OSError("short receipt journal write")
                os.fsync(fd)
            finally:
                os.close(fd)

    @staticmethod
    def _valid_record(value: Any) -> bool:
        if not isinstance(value, dict) or set(value) != _SCHEMA_KEYS or value.get("version") != SCHEMA_VERSION:
            return False
        try:
            if value["status"] not in ALL_STATUSES or not _ID.fullmatch(value["proposal_id"]) or not _ID.fullmatch(value["capability_id"]):
                return False
            if not _HASH.fullmatch(value["parameters_hash"]):
                return False
            datetime.fromisoformat(value["timestamp"])
            for field in ("session_id", "device_id", "confirmation_channel"):
                if value[field] is not None and (not isinstance(value[field], str) or not _ID.fullmatch(value[field])):
                    return False
            return value["error_code"] is None or (isinstance(value["error_code"], str) and bool(_CODE.fullmatch(value["error_code"])))
        except (KeyError, TypeError, ValueError):
            return False
