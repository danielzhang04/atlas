import sqlite3

from worker.jobstore import JobState, JobStore, SCHEMA_VERSION


class Codec:
    codec_id = "test"

    def protect(self, plaintext, *, entropy):
        return b"protected:" + plaintext[::-1]

    def unprotect(self, ciphertext, *, entropy):
        prefix = b"protected:"
        assert ciphertext.startswith(prefix)
        return ciphertext[len(prefix):][::-1]


class IncrementingClock:
    def __init__(self):
        self.value = 0

    def __call__(self):
        self.value += 1
        return self.value


def make_store(path=":memory:", clock=None):
    options = {"payload_codec": Codec()}
    if clock is not None:
        options["clock"] = clock
    return JobStore(path, **options)


def test_create_and_get_round_trip_public_job_fields():
    store = make_store(clock=lambda: 10)

    created = store.create("  Research  ", "private brief")
    fetched = store.get(created.job_id)

    assert fetched == created
    assert fetched.title == "Research"
    assert fetched.state is JobState.QUEUED
    assert fetched.to_public() == {
        "id": created.job_id,
        "title": "Research",
        "status": "queued",
        "session_id": None,
        "created_at": 10.0,
        "updated_at": 10.0,
        "summary": None,
        "error": None,
    }


def test_active_returns_only_active_jobs_in_oldest_first_order():
    clock = IncrementingClock()
    store = make_store(clock=clock)
    first = store.create("First", "brief one")
    second = store.create("Second", "brief two")
    third = store.create("Third", "brief three")
    store.transition(second.job_id, JobState.SUCCEEDED, summary="done")

    assert [job.job_id for job in store.active()] == [first.job_id, third.job_id]


def test_recent_returns_only_terminal_jobs_in_newest_first_order():
    clock = IncrementingClock()
    store = make_store(clock=clock)
    first = store.create("First", "brief one")
    second = store.create("Second", "brief two")
    store.transition(second.job_id, JobState.FAILED, error="failed")
    store.transition(first.job_id, JobState.SUCCEEDED, summary="done")

    assert [job.job_id for job in store.recent(2)] == [first.job_id, second.job_id]


def test_brief_and_result_round_trip_through_codec_without_plaintext_at_rest(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    store = make_store(path)
    job = store.create("Research", "private brief")
    store.set_result(job.job_id, "private result")

    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT brief,result FROM jobs WHERE job_id=?",
            (job.job_id,),
        ).fetchone()

    assert store.brief(job.job_id) == "private brief"
    assert store.result(job.job_id) == "private result"
    assert b"private brief" not in bytes(row[0])
    assert b"private result" not in bytes(row[1])


def test_transition_updates_job_and_appends_state_events():
    clock = IncrementingClock()
    store = make_store(clock=clock)
    job = store.create("Task", "brief")

    running = store.transition(
        job.job_id,
        JobState.RUNNING,
        session_id="abcdef12",
    )
    failed = store.transition(
        job.job_id,
        JobState.FAILED,
        summary="partial",
        error="task_failed",
    )

    assert running.session_id == "abcdef12"
    assert failed.state is JobState.FAILED
    assert failed.summary == "partial"
    assert failed.error == "task_failed"
    assert [(event.kind, event.text) for event in store.events(job.job_id)] == [
        ("state", "queued"),
        ("state", "running"),
        ("state", "failed"),
    ]


def test_append_output_truncates_lines_to_2048_characters():
    store = make_store()
    job = store.create("Task", "brief")

    event = store.append_output(job.job_id, "x" * 2_100)

    assert event.text == "x" * 2_048


def test_append_output_past_2000_line_cap_returns_none_without_persisting():
    store = make_store()
    job = store.create("Task", "brief")
    for index in range(2_000):
        store.append_output(job.job_id, f"line {index}")

    result = store.append_output(job.job_id, "one too many")

    output = [event for event in store.events(job.job_id) if event.kind == "output"]
    assert result is None
    assert len(output) == 2_000
    assert all(event.text != "one too many" for event in output)


def test_append_output_redacts_named_secret_assignments_before_persisting():
    store = make_store()
    job = store.create("Task", "brief")

    event = store.append_output(
        job.job_id,
        "api_key=alpha access-token:bravo refresh_token=charlie secret:delta "
        "password=echo Authorization:foxtrot bearer=golf",
    )

    assert event.text == " ".join(["[redacted]"] * 7)
    assert store.events(job.job_id)[-1].text == event.text


def test_append_output_redacts_anthropic_key_shapes_before_persisting():
    store = make_store()
    job = store.create("Task", "brief")

    event = store.append_output(job.job_id, "key sk-ant-AbCdEf0123_more tail")

    assert event.text == "key [redacted] tail"
    assert store.events(job.job_id)[-1].text == event.text


def test_append_output_strips_control_characters():
    store = make_store()
    job = store.create("Task", "brief")

    event = store.append_output(job.job_id, "a\x00b\tc\nd\re\x7ff")

    assert event.text == "abcdef"


def test_events_after_returns_only_later_events_in_sequence_order():
    store = make_store()
    job = store.create("Task", "brief")
    first_output = store.append_output(job.job_id, "first")
    store.append_output(job.job_id, "second")
    store.transition(job.job_id, JobState.RUNNING, session_id="abcdef12")

    events = store.events(job.job_id, after=first_output.sequence)

    assert [event.sequence for event in events] == [3, 4]
    assert [(event.kind, event.text) for event in events] == [
        ("output", "second"),
        ("state", "running"),
    ]


def test_schema_version_mismatch_renames_database_to_pre_revamp(tmp_path):
    path = tmp_path / "jobs.sqlite3"
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE legacy_marker(value TEXT)")
        connection.execute("INSERT INTO legacy_marker VALUES('old data')")
        connection.execute("PRAGMA user_version=99")
        connection.commit()
    finally:
        connection.close()

    store = make_store(path)
    backup = tmp_path / "jobs.sqlite3.pre-revamp"

    assert backup.exists()
    with sqlite3.connect(backup) as connection:
        marker = connection.execute("SELECT value FROM legacy_marker").fetchone()[0]
    with sqlite3.connect(path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert marker == "old data"
    assert version == SCHEMA_VERSION
    assert store.active() == ()
