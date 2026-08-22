from hashlib import sha256
import json

import pytest

from worker.contracts import Lane, ProtectedTaskResult, Request, SlowTaskPayload
from worker.jobstore import InvalidTransition, JobStore
from worker.payload_codec import PayloadProtectionError


class RecordingCodec:
    codec_id = "test-xor-v1"

    def __init__(self):
        self.plaintexts = []

    def protect(self, plaintext, *, entropy):
        self.plaintexts.append(plaintext)
        key = sha256(entropy).digest()
        return bytes(value ^ key[index % len(key)] for index, value in enumerate(plaintext))

    def unprotect(self, ciphertext, *, entropy):
        key = sha256(entropy).digest()
        return bytes(value ^ key[index % len(key)] for index, value in enumerate(ciphertext))


def running_job(store):
    request = Request("research.synthesize", research=True)
    payload = SlowTaskPayload("Compare the private sources.", request.fingerprint(), 100.0)
    job = store.create(request, lane=Lane.SLOW, slow_payload=payload)
    claim = store.claim_next("atlas-subscription", lease_seconds=60)
    assert claim.job_id == job.job_id
    return job, claim


def test_success_atomically_stores_private_answer_outside_public_projection(tmp_path):
    codec = RecordingCodec()
    store = JobStore(tmp_path / "jobs.sqlite", payload_codec=codec, clock=lambda: 100.0)
    try:
        job, claim = running_job(store)
        answer = "Private synthesis from connected sources."
        result = ProtectedTaskResult(
            job.job_id, answer, sha256(answer.encode()).hexdigest(), ("proposal-1", "proposal-2"),
            artifact_name="synthesis.md",
        )
        completed = store.complete_success(
            job.job_id, "atlas-subscription", claim.lease_token,
            protected_result=result,
        )
        assert answer not in str(dict(completed.public_payload))
        assert dict(completed.public_payload) == {
            "result_available": True, "summary": "Private result available.",
        }
        assert store.get_protected_result(job.job_id) == result
        assert answer.encode() not in (tmp_path / "jobs.sqlite").read_bytes()
    finally:
        store.close()


def test_protected_result_accepts_legacy_v1_and_rejects_path_like_artifact_names():
    job_id = "00000000-0000-4000-8000-000000000001"
    answer = "Private answer"
    legacy = json.dumps({
        "version": 1, "job_id": job_id, "answer": answer,
        "candidate_digest": sha256(answer.encode()).hexdigest(), "evidence_ids": [],
    }, separators=(",", ":")).encode()
    assert ProtectedTaskResult.from_bytes(legacy).artifact_name is None
    with pytest.raises(ValueError, match="artifact name"):
        ProtectedTaskResult(
            job_id, answer, sha256(answer.encode()).hexdigest(), artifact_name="../escape.md",
        )


def test_protected_result_requires_fenced_success_and_is_unavailable_while_running(tmp_path):
    codec = RecordingCodec()
    store = JobStore(tmp_path / "jobs.sqlite", payload_codec=codec, clock=lambda: 100.0)
    try:
        job, _claim = running_job(store)
        codec.plaintexts.clear()
        answer = "Private answer"
        result = ProtectedTaskResult(job.job_id, answer, sha256(answer.encode()).hexdigest())
        with pytest.raises(InvalidTransition, match="unavailable"):
            store.get_protected_result(job.job_id)
        with pytest.raises(InvalidTransition):
            store.transition(job.job_id, "succeeded", _protected_result=result)
        with pytest.raises(InvalidTransition):
            store.complete_success(
                job.job_id, "atlas-subscription", "x" * 43, protected_result=result,
            )
        assert codec.plaintexts == []
        assert store.get(job.job_id).state.value == "running"
    finally:
        store.close()


def test_protected_completion_rejects_caller_controlled_public_payload(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite", payload_codec=RecordingCodec(), clock=lambda: 100.0)
    try:
        job, claim = running_job(store)
        answer = "Private answer"
        result = ProtectedTaskResult(job.job_id, answer, sha256(answer.encode()).hexdigest())
        with pytest.raises(ValueError, match="owns its public projection"):
            store.complete_success(
                job.job_id, "atlas-subscription", claim.lease_token,
                public_payload={"summary": answer}, protected_result=result,
            )
        assert store.get(job.job_id).state.value == "running"
    finally:
        store.close()


def test_protected_result_integrity_failure_is_sanitized(tmp_path):
    codec = RecordingCodec()
    store = JobStore(tmp_path / "jobs.sqlite", payload_codec=codec, clock=lambda: 100.0)
    try:
        job, claim = running_job(store)
        answer = "Private answer"
        result = ProtectedTaskResult(job.job_id, answer, sha256(answer.encode()).hexdigest())
        store.complete_success(
            job.job_id, "atlas-subscription", claim.lease_token, protected_result=result,
        )
        store._connection.execute(
            "UPDATE protected_results SET plaintext_digest = ? WHERE job_id = ?",
            ("0" * 64, job.job_id),
        )
        store._connection.commit()
        with pytest.raises(PayloadProtectionError, match="integrity check failed"):
            store.get_protected_result(job.job_id)
    finally:
        store.close()
