from hashlib import sha256
import json

import pytest

from worker.broker_ipc import BrokerReadReceipt
from worker.knowledge_workflow import (
    KnowledgeWorkflowError, parse_candidate_frame, parse_review_frame, require_evidence,
)


JOB_ID = "3f75564b-cad1-4b9e-9e79-4f15013b43c2"
NONCE = "nonce_value_that_is_long_enough"


def receipt(number, *, parameter="a", content="b"):
    return BrokerReadReceipt(
        number, "google.drive.read", f"proposal-{number}", parameter * 64, content * 64, False,
    )


def frame(prefix, body):
    return f"log\n{prefix}:{NONCE}:{json.dumps(body, separators=(',', ':'))}\n"


def test_candidate_and_fresh_review_frames_are_nonce_and_digest_bound():
    candidate = parse_candidate_frame(frame("ATLAS_CANDIDATE_V1", {
        "job_id": JOB_ID, "status": "candidate", "answer": "Bounded private synthesis.",
        "evidence_ids": ["proposal-1", "proposal-2"], "error_code": None,
    }), nonce=NONCE, job_id=JOB_ID)
    review = parse_review_frame(frame("ATLAS_REVIEW_V1", {
        "job_id": JOB_ID, "verdict": "pass", "candidate_digest": candidate.candidate_digest,
        "evidence_ids": ["proposal-3", "proposal-4"], "findings": ["Evidence supports the answer."],
    }), nonce=NONCE, job_id=JOB_ID)
    assert review.candidate_digest == sha256(candidate.answer.encode()).hexdigest()


def test_evidence_gate_rejects_unobserved_or_repeated_reads():
    receipts = (receipt(1), receipt(2), receipt(3, parameter="c", content="d"))
    with pytest.raises(KnowledgeWorkflowError, match="unobserved"):
        require_evidence(("proposal-1", "proposal-9"), receipts, minimum=2)
    with pytest.raises(KnowledgeWorkflowError, match="not met"):
        require_evidence(("proposal-1", "proposal-2"), receipts, minimum=2)
    assert [item.proposal_id for item in require_evidence(
        ("proposal-1", "proposal-3"), receipts, minimum=2,
    )] == ["proposal-1", "proposal-3"]


def test_duplicate_or_wrong_nonce_frames_fail_closed():
    body = {
        "job_id": JOB_ID, "status": "parked", "answer": "", "evidence_ids": [],
        "error_code": "evidence_unavailable",
    }
    one = frame("ATLAS_CANDIDATE_V1", body)
    with pytest.raises(KnowledgeWorkflowError, match="missing or ambiguous"):
        parse_candidate_frame(one + one, nonce=NONCE, job_id=JOB_ID)
    with pytest.raises(KnowledgeWorkflowError, match="missing or ambiguous"):
        parse_candidate_frame(one, nonce="other_nonce_that_is_long_enough", job_id=JOB_ID)


def test_terminal_redraws_of_one_indented_frame_are_deduplicated():
    body = {
        "job_id": JOB_ID, "status": "candidate", "answer": "ATLAS_SUBSCRIPTION_SMOKE_OK",
        "evidence_ids": [], "error_code": None,
    }
    payload = json.dumps(body, separators=(",", ":"))
    marker = f"ATLAS_CANDIDATE_V1:{NONCE}:{payload}"
    logs = (
        "\x1b[2J❯ echoed prompt mentions ATLAS_CANDIDATE_V1 but is not a frame\x1b[0m\n"
        f"  \x1b[32m{marker}\x1b[0m \n"
        f"\x1b[H  {marker} \n"
    )
    result = parse_candidate_frame(logs, nonce=NONCE, job_id=JOB_ID)
    assert result.answer == "ATLAS_SUBSCRIPTION_SMOKE_OK"


def test_terminal_redraws_with_distinct_frames_remain_ambiguous():
    first = {
        "job_id": JOB_ID, "status": "candidate", "answer": "first",
        "evidence_ids": [], "error_code": None,
    }
    second = {**first, "answer": "second"}
    logs = "\x1b[2J" + "\n".join(
        f"  ATLAS_CANDIDATE_V1:{NONCE}:{json.dumps(body, separators=(',', ':'))}"
        for body in (first, second)
    )
    with pytest.raises(KnowledgeWorkflowError, match="missing or ambiguous"):
        parse_candidate_frame(logs, nonce=NONCE, job_id=JOB_ID)
