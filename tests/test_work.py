import asyncio
import json
import time

from worker.jobstore import JobState, JobStore
from worker.work import WorkManager


class Codec:
    codec_id = "test"

    def protect(self, plaintext, *, entropy):
        return b"protected:" + plaintext[::-1]

    def unprotect(self, ciphertext, *, entropy):
        return ciphertext[len(b"protected:"):][::-1]


class FakeLauncher:
    available = True

    def __init__(
        self,
        *,
        launch_delay=0,
        launch_error=None,
        session_id="abcdef12",
        log_frames=None,
        status="running",
    ):
        self.launch_delay = launch_delay
        self.launch_error = launch_error
        self.session_id = session_id
        self.log_frames = list(log_frames or [[]])
        self.status_value = status
        self.launch_calls = []
        self.log_calls = []
        self.status_calls = []
        self.cancel_calls = []

    def launch(self, **kwargs):
        self.launch_calls.append(kwargs)
        if self.launch_delay:
            time.sleep(self.launch_delay)
        if self.launch_error is not None:
            raise self.launch_error
        return self.session_id

    def logs(self, session_id):
        self.log_calls.append(session_id)
        index = min(len(self.log_calls) - 1, len(self.log_frames) - 1)
        return list(self.log_frames[index])

    def status(self, session_id):
        self.status_calls.append(session_id)
        return self.status_value

    def cancel(self, session_id):
        self.cancel_calls.append(session_id)


def make_store():
    return JobStore(payload_codec=Codec())


def wait_for_state(store, job_id, expected, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = store.get(job_id)
        if job.state is expected:
            return job
        time.sleep(0.005)
    raise AssertionError(f"job did not reach {expected.value}")


def make_running_job(store, *, session_id="abcdef12"):
    job = store.create("Task", "brief")
    return store.transition(job.job_id, JobState.RUNNING, session_id=session_id)


def result_frame(manager, job_id, status="succeeded", summary="done"):
    payload = {
        "job_id": job_id,
        "status": status,
        "summary": summary,
    }
    nonce = manager._nonce(job_id)
    return f"ATLAS_RESULT_V1:{nonce}:{json.dumps(payload)}"


def output_texts(store, job_id):
    return [
        event.text
        for event in store.events(job_id)
        if event.kind == "output"
    ]


def test_launch_returns_queued_within_100_ms_when_launcher_sleeps_one_second(tmp_path):
    store = make_store()
    launcher = FakeLauncher(launch_delay=1.0)
    manager = WorkManager(store, launcher, tmp_path)

    started = time.monotonic()
    job = manager.launch("Task", "brief")
    elapsed = time.monotonic() - started

    assert elapsed < 0.1
    assert job.state is JobState.QUEUED
    wait_for_state(store, job.job_id, JobState.RUNNING)


def test_launch_transitions_from_launching_to_running_with_session_id(tmp_path):
    store = make_store()
    launcher = FakeLauncher(session_id="fedcba98")
    manager = WorkManager(store, launcher, tmp_path)

    job = manager.launch("Task", "brief")
    running = wait_for_state(store, job.job_id, JobState.RUNNING)

    states = [
        event.text
        for event in store.events(job.job_id)
        if event.kind == "state"
    ]
    assert states == ["queued", "launching", "running"]
    assert running.session_id == "fedcba98"
    assert launcher.launch_calls[0]["cwd"] == tmp_path / job.job_id


def test_launcher_exception_transitions_job_to_failed_launch_failed(tmp_path):
    store = make_store()
    launcher = FakeLauncher(launch_error=RuntimeError("boom"))
    manager = WorkManager(store, launcher, tmp_path)

    job = manager.launch("Task", "brief")
    failed = wait_for_state(store, job.job_id, JobState.FAILED)

    assert failed.error == "launch_failed"


def test_run_appends_each_new_redrawn_log_line_exactly_once(tmp_path):
    store = make_store()
    job = make_running_job(store)
    launcher = FakeLauncher(
        log_frames=[
            ["A"],
            ["A"],
            ["A", "B"],
            ["A", "B", "C"],
        ],
        status="running",
    )
    manager = WorkManager(store, launcher, tmp_path, poll_s=0.005)

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(manager.run(stop))
        deadline = asyncio.get_running_loop().time() + 0.5
        while len(launcher.log_calls) < 4:
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError("manager did not poll four frames")
            await asyncio.sleep(0.005)
        stop.set()
        await task

    asyncio.run(scenario())

    assert output_texts(store, job.job_id) == ["A", "B", "C"]


def test_done_succeeded_frame_stores_summary_and_result(tmp_path):
    store = make_store()
    job = make_running_job(store)
    launcher = FakeLauncher(status="done")
    manager = WorkManager(store, launcher, tmp_path)
    launcher.log_frames = [[result_frame(manager, job.job_id, "succeeded", "finished")]]

    manager._poll(job)

    terminal = store.get(job.job_id)
    assert terminal.state is JobState.SUCCEEDED
    assert terminal.summary == "finished"
    assert store.result(job.job_id) == "finished"


def test_done_failed_frame_records_task_failed_and_summary(tmp_path):
    store = make_store()
    job = make_running_job(store)
    launcher = FakeLauncher(status="done")
    manager = WorkManager(store, launcher, tmp_path)
    launcher.log_frames = [[result_frame(manager, job.job_id, "failed", "blocked")]]

    manager._poll(job)

    terminal = store.get(job.job_id)
    assert terminal.state is JobState.FAILED
    assert terminal.error == "task_failed"
    assert terminal.summary == "blocked"


def test_done_cancelled_frame_records_cancelled_summary(tmp_path):
    store = make_store()
    job = make_running_job(store)
    launcher = FakeLauncher(status="done")
    manager = WorkManager(store, launcher, tmp_path)
    launcher.log_frames = [[result_frame(manager, job.job_id, "cancelled", "stopped")]]

    manager._poll(job)

    terminal = store.get(job.job_id)
    assert terminal.state is JobState.CANCELLED
    assert terminal.summary == "stopped"


def test_done_without_result_frame_records_result_missing(tmp_path):
    store = make_store()
    job = make_running_job(store)
    launcher = FakeLauncher(log_frames=[["ordinary output"]], status="done")
    manager = WorkManager(store, launcher, tmp_path)

    manager._poll(job)

    terminal = store.get(job.job_id)
    assert terminal.state is JobState.FAILED
    assert terminal.error == "result_missing"


def test_needs_input_session_records_needs_input_failure(tmp_path):
    store = make_store()
    job = make_running_job(store)
    launcher = FakeLauncher(status="needs_input")
    manager = WorkManager(store, launcher, tmp_path)

    manager._poll(job)

    terminal = store.get(job.job_id)
    assert terminal.state is JobState.FAILED
    assert terminal.error == "needs_input"


def test_failed_session_records_session_failed(tmp_path):
    store = make_store()
    job = make_running_job(store)
    launcher = FakeLauncher(status="failed")
    manager = WorkManager(store, launcher, tmp_path)

    manager._poll(job)

    terminal = store.get(job.job_id)
    assert terminal.state is JobState.FAILED
    assert terminal.error == "session_failed"


def test_cancel_calls_launcher_and_records_cancelled(tmp_path):
    store = make_store()
    job = make_running_job(store, session_id="fedcba98")
    launcher = FakeLauncher()
    manager = WorkManager(store, launcher, tmp_path)

    terminal = manager.cancel(job.job_id)

    assert launcher.cancel_calls == ["fedcba98"]
    assert terminal.state is JobState.CANCELLED


def test_on_terminal_fires_exactly_once_for_each_job(tmp_path):
    store = make_store()
    job = make_running_job(store)
    launcher = FakeLauncher(status="done")
    manager = WorkManager(store, launcher, tmp_path)
    launcher.log_frames = [[result_frame(manager, job.job_id)]]
    notifications = []
    manager.on_terminal(notifications.append)

    manager._poll(job)
    manager._poll(job)

    assert [item.job_id for item in notifications] == [job.job_id]


def test_restart_reattaches_and_polls_running_rows(tmp_path):
    store = make_store()
    job = make_running_job(store, session_id="fedcba98")
    store.append_output(job.job_id, "already stored")
    launcher = FakeLauncher(
        log_frames=[["already stored", "reattached"]],
        status="running",
    )
    manager = WorkManager(store, launcher, tmp_path, poll_s=0.005)

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(manager.run(stop))
        deadline = asyncio.get_running_loop().time() + 0.5
        while not launcher.log_calls:
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError("manager did not reattach")
            await asyncio.sleep(0.005)
        stop.set()
        await task

    asyncio.run(scenario())

    assert launcher.log_calls[0] == "fedcba98"
    assert output_texts(store, job.job_id) == ["already stored", "reattached"]
    assert store.get(job.job_id).state is JobState.RUNNING


def test_restart_fails_sessionless_rows_as_orphaned(tmp_path):
    store = make_store()
    job = store.create("Task", "brief")
    store.transition(job.job_id, JobState.LAUNCHING)
    launcher = FakeLauncher()
    manager = WorkManager(store, launcher, tmp_path)

    async def scenario():
        stop = asyncio.Event()
        stop.set()
        await manager.run(stop)

    asyncio.run(scenario())

    terminal = store.get(job.job_id)
    assert terminal.state is JobState.FAILED
    assert terminal.error == "orphaned"
