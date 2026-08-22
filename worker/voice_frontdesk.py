"""Host-side orchestration for conversational voice and hidden work routing.

Claude owns dialogue. A model tool call is only a proposed route; the host owns classification,
durable admission, authority, and receipts. After admission, bounded backend facts go back to
Claude for a natural spoken explanation.
"""
from __future__ import annotations

from dataclasses import dataclass
import asyncio
import re
from typing import Any

from .contracts import Lane, Request
from .frontdesk import FrontDesk, FrontDeskOutcome
from .turn_interpreter import InterpretedTurn, TurnInterpretationError, TurnInterpreter, TurnKind


MAX_HOST_TEXT = 512
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SECRET_TEXT = re.compile(r"(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]|bearer\s+", re.I)


@dataclass(frozen=True, slots=True)
class VoiceTurnOutcome:
    kind: TurnKind
    text: str
    job_id: str | None = None
    status: str | None = None
    lane: Lane | None = None
    error_code: str | None = None


class VoiceFrontDesk:
    """Run one conversational turn and hand only an explicit work proposal to FrontDesk."""

    def __init__(self, interpreter: TurnInterpreter, frontdesk: FrontDesk) -> None:
        if not callable(getattr(interpreter, "interpret", None)):
            raise TypeError("interpreter must provide interpret")
        if not isinstance(frontdesk, FrontDesk):
            raise TypeError("frontdesk must be a FrontDesk")
        self.interpreter = interpreter
        self.frontdesk = frontdesk

    async def handle(self, raw_utterance: str, *, catalog: Any = None,
                     idempotency_key: str | None = None) -> VoiceTurnOutcome:
        try:
            interpreted = await self.interpreter.interpret(raw_utterance, catalog)
        except TurnInterpretationError as exc:
            return VoiceTurnOutcome(
                TurnKind.REPLY, exc.public_message,
                error_code=f"conversation_{exc.reason}",
            )
        except Exception:
            error = TurnInterpretationError("provider_error")
            return VoiceTurnOutcome(
                TurnKind.REPLY, error.public_message,
                error_code="conversation_provider_error",
            )
        if not isinstance(interpreted, InterpretedTurn):
            return VoiceTurnOutcome(
                TurnKind.REPLY, TurnInterpretationError().public_message,
                error_code="conversation_response_invalid",
            )

        if interpreted.kind in {TurnKind.REPLY, TurnKind.CLARIFY}:
            text = _safe_text(interpreted.text)
            if text:
                return VoiceTurnOutcome(interpreted.kind, text)
            return VoiceTurnOutcome(
                TurnKind.REPLY, TurnInterpretationError().public_message,
                error_code="conversation_response_invalid",
            )

        if interpreted.kind is not TurnKind.REQUEST or not isinstance(interpreted.request, Request):
            return VoiceTurnOutcome(
                TurnKind.REPLY, TurnInterpretationError().public_message,
                error_code="conversation_response_invalid",
            )
        return await self._admit(interpreted, raw_utterance=raw_utterance,
                                 idempotency_key=idempotency_key)

    handle_turn = handle

    async def _admit(self, turn: InterpretedTurn, *, raw_utterance: str,
                     idempotency_key: str | None) -> VoiceTurnOutcome:
        request = turn.request
        if not isinstance(request, Request):
            return VoiceTurnOutcome(
                TurnKind.REPLY, TurnInterpretationError().public_message,
                error_code="conversation_response_invalid",
            )
        try:
            outcome = await asyncio.to_thread(
                self.frontdesk.submit, request, raw_utterance=raw_utterance,
                idempotency_key=idempotency_key,
            )
        except Exception:
            text = await self._narrate_or_fallback(turn, {
                "status": "failed", "lane": None, "error_code": "admission_failed",
                "replayed": False, "job_visible": False,
            })
            return VoiceTurnOutcome(TurnKind.REQUEST, text, error_code="admission_failed")
        if not isinstance(outcome, FrontDeskOutcome):
            text = await self._narrate_or_fallback(turn, {
                "status": "failed", "lane": None, "error_code": "invalid_admission",
                "replayed": False, "job_visible": False,
            })
            return VoiceTurnOutcome(TurnKind.REQUEST, text, error_code="invalid_admission")
        text = await self._narrate_or_fallback(turn, {
            "status": outcome.status,
            "lane": outcome.lane.value,
            "error_code": outcome.error_code,
            "replayed": outcome.replayed,
            "job_visible": outcome.job_id is not None,
        })
        return VoiceTurnOutcome(TurnKind.REQUEST, text, job_id=outcome.job_id,
                                status=outcome.status, lane=outcome.lane,
                                error_code=outcome.error_code)

    async def _narrate_or_fallback(self, turn: InterpretedTurn, facts: dict[str, Any]) -> str:
        try:
            text = _safe_text(await self.interpreter.narrate_route(turn, facts))
            if text:
                return text
        except Exception:
            pass
        # This is the only deterministic dialogue: the conversational model itself is
        # unavailable, so there is no model left to phrase the bounded host facts.
        status = facts.get("status")
        if status == "queued":
            return "I queued that work, but I couldn't describe its status just now."
        if status == "unavailable":
            return "That work couldn't start because its worker is unavailable."
        return "That work ran into a problem before it could start."

def _safe_text(value: str) -> str:
    if not isinstance(value, str):
        return ""
    value = _CONTROL.sub("", value).strip()
    if not value or len(value) > MAX_HOST_TEXT:
        return ""
    if _SECRET_TEXT.search(value):
        return "I can't repeat sensitive content."
    return value


__all__ = ["VoiceTurnOutcome", "VoiceFrontDesk"]
