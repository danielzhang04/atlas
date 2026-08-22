"""In-memory pairing boundary for the loopback Atlas action UI."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Callable

HEADER = "x-atlas-action-token"


@dataclass(frozen=True)
class ActionContext:
    session_id: str
    device_id: str


class PairingAuthorizer:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 token: str | None = None, ttl_s: float = 43_200) -> None:
        self._clock, self._ttl_s = clock, float(ttl_s)
        self._pairing_token: str | None = token or secrets.token_urlsafe(24)
        self._sessions: dict[str, tuple[ActionContext, float]] = {}
        self._active: ActionContext | None = None
        self._active_digest: str | None = None
        self._failures = 0

    @property
    def pairing_token(self) -> str:
        if self._pairing_token is None:
            raise PermissionError("pairing token has already been consumed")
        return self._pairing_token

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def pair(self, token: str) -> tuple[str, ActionContext]:
        if self._failures >= 8:
            raise PermissionError("pairing attempts exhausted; restart Atlas to mint a new token")
        if (self._pairing_token is None or not isinstance(token, str)
                or not hmac.compare_digest(token, self._pairing_token)):
            self._failures += 1
            raise PermissionError("invalid pairing token")
        raw = secrets.token_urlsafe(32)
        context = ActionContext(secrets.token_urlsafe(16), secrets.token_urlsafe(16))
        digest = self._digest(raw)
        self._sessions.clear()
        self._sessions[digest] = (context, self._clock() + self._ttl_s)
        self._active = context
        self._active_digest = digest
        self._pairing_token = None
        self._failures = 0
        return raw, context

    def authorize(self, raw_cookie: str | None) -> ActionContext:
        if not raw_cookie:
            raise PermissionError("Atlas action UI is not paired")
        item = self._sessions.get(self._digest(raw_cookie))
        if item is None or self._clock() >= item[1]:
            raise PermissionError("Atlas action UI pairing is invalid or expired")
        return item[0]

    def active_context(self) -> tuple[str, str] | None:
        if self._active is None or self._active_digest is None:
            return None
        item = self._sessions.get(self._active_digest)
        if item is None or self._clock() >= item[1]:
            return None
        return self._active.session_id, self._active.device_id
