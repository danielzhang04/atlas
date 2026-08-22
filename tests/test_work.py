import asyncio
import time
from worker.jobstore import JobState, JobStore
from worker.work import WorkManager


class Codec:
    codec_id = "test"
    def protect(self, plaintext, *, entropy): return plaintext
    def unprotect(self, ciphertext, *, entropy): return ciphertext


class Launcher:
    available = True
    def launch(self, **kw): self.kw = kw; return "abcdef12"
    def logs(self, sid): return []
    def status(self, sid): return "running"
    def cancel(self, sid): self.cancelled = sid


def test_launch_is_async(tmp_path):
    store, launcher = JobStore(payload_codec=Codec()), Launcher()
    work = WorkManager(store, launcher, tmp_path, poll_s=.01)
    started = time.monotonic(); job = work.launch("title", "brief")
    assert time.monotonic() - started < .1 and job.state is JobState.QUEUED
    for _ in range(100):
        if store.get(job.job_id).state is JobState.RUNNING: break
        time.sleep(.01)
    assert store.get(job.job_id).session_id == "abcdef12"
    assert work.cancel(job.job_id).state is JobState.CANCELLED
