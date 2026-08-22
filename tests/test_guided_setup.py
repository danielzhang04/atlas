from hashlib import sha256

import pytest

from worker.contracts import utc_timestamp
from worker.guided_setup import GUIDES, GuidedSetupAdmission
from worker.jobstore import JobStore
from worker.subscription_worker import WorkerHealth, WorkerHealthStatus


class FakePayloadCodec:
    codec_id = "test-xor-v1"

    def protect(self, plaintext, *, entropy):
        key = sha256(entropy).digest()
        return bytes(value ^ key[index % len(key)] for index, value in enumerate(plaintext))

    def unprotect(self, ciphertext, *, entropy):
        return self.protect(ciphertext, entropy=entropy)


def healthy():
    return WorkerHealth(WorkerHealthStatus.AVAILABLE, worker_id="guide-test",
                        checked_at=utc_timestamp())


def test_guided_setup_is_host_fixed_and_enters_the_normal_slow_queue(tmp_path):
    with JobStore(tmp_path / "jobs.sqlite", payload_codec=FakePayloadCodec()) as store:
        admission = GuidedSetupAdmission(store, healthy)
        outcome = admission.start("browser")
        assert outcome.accepted and outcome.lane.value == "slow"
        job = store.get(outcome.job_id)
        assert job.request.operation == "atlas.guided_setup"
        assert job.request.target == "browser"
        assert job.public_payload == {"summary": "Configure Browser"}
        claim = store.claim_next("guide-test")
        protected = store.get_slow_payload(claim.job_id, "guide-test", claim.lease_token)
        assert "trusted loopback browser bridge" in protected.instruction
        assert "Do not request credentials" in protected.instruction


def test_guided_setup_rejects_arbitrary_ids_before_creating_work(tmp_path):
    with JobStore(tmp_path / "jobs.sqlite", payload_codec=FakePayloadCodec()) as store:
        admission = GuidedSetupAdmission(store, healthy)
        with pytest.raises(KeyError):
            admission.start("../../arbitrary-command")
        assert store.recent_jobs(10) == ()


def test_every_guide_names_files_and_an_external_readiness_check():
    assert set(GUIDES) == {"voice", "subscription", "desktop", "browser", "google", "spotify"}
    for guide in GUIDES.values():
        instruction = guide.instruction()
        assert guide.governing_files and all(path in instruction for path in guide.governing_files)
        assert guide.ready_when in instruction
        assert len(instruction.encode("utf-8")) < 4096
