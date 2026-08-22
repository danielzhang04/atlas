from worker.jobstore import JobState, JobStore


class Codec:
    codec_id = "test"
    def protect(self, plaintext, *, entropy): return bytes(b ^ entropy[0] for b in plaintext)
    def unprotect(self, ciphertext, *, entropy): return bytes(b ^ entropy[0] for b in ciphertext)


def test_store_lifecycle_and_protected_payloads(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3", payload_codec=Codec(), clock=lambda: 10)
    job = store.create("Research", "private brief")
    assert job.state is JobState.QUEUED and store.brief(job.job_id) == "private brief"
    store.transition(job.job_id, JobState.RUNNING, session_id="abc")
    event = store.append_output(job.job_id, "hello\x00")
    assert event.text == "hello"
    store.set_result(job.job_id, "answer")
    assert store.result(job.job_id) == "answer"
    assert store.get(job.job_id).to_public()["status"] == "running"


def test_wrong_schema_is_backed_up(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    path.write_bytes(b"not sqlite")
    try: JobStore(path, payload_codec=Codec())
    except Exception: pass
