"""Typed candidate/review frames and optional host evidence gates for private heavy work."""
from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
import re
from typing import Any, Iterable
from uuid import UUID

from .broker_ipc import BrokerReadReceipt


MAX_KNOWLEDGE_ANSWER_BYTES = 16_384
MAX_KNOWLEDGE_FRAME_BYTES = 24_576
MAX_KNOWLEDGE_LOG_BYTES = 1_000_000
_NONCE = re.compile(r"[A-Za-z0-9_-]{24,128}")
_SAFE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,256}")
_SAFE_CODE = re.compile(r"[a-z][a-z0-9_.:-]{0,127}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


class KnowledgeWorkflowError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class KnowledgeCandidateResult:
    job_id: str
    status: str
    answer: str = field(default="", repr=False)
    evidence_ids: tuple[str, ...] = ()
    error_code: str | None = None

    def __post_init__(self) -> None:
        _job_id(self.job_id)
        if self.status not in {"candidate", "parked", "failed"}:
            raise ValueError("invalid knowledge candidate status")
        _evidence_ids(self.evidence_ids)
        if not isinstance(self.answer, str) or len(self.answer.encode("utf-8")) > MAX_KNOWLEDGE_ANSWER_BYTES:
            raise ValueError("invalid knowledge candidate answer")
        if self.status == "candidate":
            if not self.answer.strip() or self.error_code is not None:
                raise ValueError("candidate result is incomplete")
        elif self.answer or self.evidence_ids or not _safe_code(self.error_code):
            raise ValueError("non-candidate result is invalid")

    @property
    def candidate_digest(self) -> str:
        return sha256(self.answer.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class KnowledgeReviewResult:
    job_id: str
    verdict: str
    candidate_digest: str
    evidence_ids: tuple[str, ...]
    findings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _job_id(self.job_id)
        if self.verdict not in {"pass", "rework", "parked"}:
            raise ValueError("invalid knowledge review verdict")
        if not isinstance(self.candidate_digest, str) or _DIGEST.fullmatch(self.candidate_digest) is None:
            raise ValueError("invalid reviewed candidate digest")
        _evidence_ids(self.evidence_ids)
        if not isinstance(self.findings, tuple) or not 1 <= len(self.findings) <= 16:
            raise ValueError("knowledge review requires bounded findings")
        if any(not isinstance(item, str) or not item.strip() or len(item) > 1_024 for item in self.findings):
            raise ValueError("invalid knowledge review finding")


def parse_candidate_frame(logs: str, *, nonce: str, job_id: str) -> KnowledgeCandidateResult:
    value = _parse_frame(logs, prefix="ATLAS_CANDIDATE_V1", nonce=nonce)
    if set(value) != {"job_id", "status", "answer", "evidence_ids", "error_code"}:
        raise KnowledgeWorkflowError("knowledge candidate schema is invalid")
    if value.get("job_id") != job_id or not isinstance(value.get("evidence_ids"), list):
        raise KnowledgeWorkflowError("knowledge candidate correlation failed")
    try:
        return KnowledgeCandidateResult(
            value["job_id"], value["status"], value["answer"], tuple(value["evidence_ids"]),
            value["error_code"],
        )
    except (KeyError, TypeError, ValueError):
        raise KnowledgeWorkflowError("knowledge candidate schema is invalid") from None


def parse_review_frame(logs: str, *, nonce: str, job_id: str) -> KnowledgeReviewResult:
    value = _parse_frame(logs, prefix="ATLAS_REVIEW_V1", nonce=nonce)
    if set(value) != {"job_id", "verdict", "candidate_digest", "evidence_ids", "findings"}:
        raise KnowledgeWorkflowError("knowledge review schema is invalid")
    if (
        value.get("job_id") != job_id
        or not isinstance(value.get("evidence_ids"), list)
        or not isinstance(value.get("findings"), list)
    ):
        raise KnowledgeWorkflowError("knowledge review correlation failed")
    try:
        return KnowledgeReviewResult(
            value["job_id"], value["verdict"], value["candidate_digest"],
            tuple(value["evidence_ids"]), tuple(value["findings"]),
        )
    except (KeyError, TypeError, ValueError):
        raise KnowledgeWorkflowError("knowledge review schema is invalid") from None


def require_evidence(evidence_ids: tuple[str, ...], receipts: Iterable[BrokerReadReceipt], *,
                     minimum: int) -> tuple[BrokerReadReceipt, ...]:
    _evidence_ids(evidence_ids)
    if isinstance(minimum, bool) or not isinstance(minimum, int) or not 1 <= minimum <= 32:
        raise ValueError("invalid evidence minimum")
    receipt_rows = tuple(receipts)
    if any(not isinstance(item, BrokerReadReceipt) for item in receipt_rows):
        raise TypeError("evidence receipts must be broker read receipts")
    by_id = {item.proposal_id: item for item in receipt_rows}
    if len(by_id) != len(receipt_rows):
        raise KnowledgeWorkflowError("broker evidence receipts are ambiguous")
    try:
        selected = tuple(by_id[item] for item in evidence_ids)
    except KeyError:
        raise KnowledgeWorkflowError("model cited unobserved evidence") from None
    signatures = {
        (item.capability_id, item.parameters_hash, item.content_digest) for item in selected
    }
    if len(signatures) < minimum:
        raise KnowledgeWorkflowError("knowledge evidence gate was not met")
    return selected


def _parse_frame(logs: str, *, prefix: str, nonce: str) -> dict[str, Any]:
    if not isinstance(logs, str) or len(logs.encode("utf-8")) > MAX_KNOWLEDGE_LOG_BYTES:
        raise KnowledgeWorkflowError("knowledge logs are invalid")
    if not isinstance(nonce, str) or _NONCE.fullmatch(nonce) is None:
        raise ValueError("invalid knowledge result nonce")
    marker = f"{prefix}:{nonce}:"
    terminal_rendered = "\x1b" in logs
    normalized = _ANSI_ESCAPE.sub("", logs) if terminal_rendered else logs
    frames = []
    for line in normalized.splitlines():
        candidate = line.lstrip() if terminal_rendered else line
        if candidate.startswith(marker):
            frames.append(candidate[len(marker):].strip() if terminal_rendered
                          else candidate[len(marker):])
    if terminal_rendered:
        frames = list(dict.fromkeys(frames))
    if len(frames) != 1 or len(frames[0].encode("utf-8")) > MAX_KNOWLEDGE_FRAME_BYTES:
        raise KnowledgeWorkflowError("knowledge result frame is missing or ambiguous")
    try:
        value = json.loads(frames[0])
    except json.JSONDecodeError:
        raise KnowledgeWorkflowError("knowledge result frame is malformed") from None
    if not isinstance(value, dict):
        raise KnowledgeWorkflowError("knowledge result frame is malformed")
    return value


def _job_id(value: str) -> None:
    try:
        UUID(value)
    except (TypeError, ValueError):
        raise ValueError("invalid knowledge job id") from None


def _evidence_ids(value: tuple[str, ...]) -> None:
    if (
        not isinstance(value, tuple)
        or len(value) > 32
        or len(set(value)) != len(value)
        or any(not isinstance(item, str) or _SAFE_ID.fullmatch(item) is None for item in value)
    ):
        raise ValueError("invalid knowledge evidence ids")


def _safe_code(value: str | None) -> bool:
    return isinstance(value, str) and _SAFE_CODE.fullmatch(value) is not None


__all__ = [
    "KnowledgeCandidateResult", "KnowledgeReviewResult", "KnowledgeWorkflowError",
    "parse_candidate_frame", "parse_review_frame", "require_evidence",
]
