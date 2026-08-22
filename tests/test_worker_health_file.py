from hashlib import sha256

from worker.contracts import Request
from worker.frontdesk import FrontDesk
from worker.jobstore import JobStore
from worker.subscription_cli import (
    _api_environment_absent, _arguments, _available_knowledge_capabilities, run,
)
from worker.runtime import RuntimeServices
from worker.subscription_worker import WorkerHealth, WorkerHealthStatus
from worker.worker_health_file import publish_health, read_health


class FakePayloadCodec:
    codec_id = "test-xor-v1"

    def protect(self, plaintext, *, entropy):
        key = sha256(entropy).digest()
        return bytes(value ^ key[index % len(key)] for index, value in enumerate(plaintext))

    def unprotect(self, ciphertext, *, entropy):
        return self.protect(ciphertext, entropy=entropy)


def test_health_file_roundtrip_and_malformed_input_fail_closed(tmp_path):
    path = tmp_path / "health.json"
    expected = WorkerHealth(WorkerHealthStatus.AVAILABLE, worker_id="worker-1",
                            checked_at=123.0)
    publish_health(path, expected)
    assert read_health(path) == expected
    path.write_text('{"status":"available","access_token":"secret"}', encoding="utf-8")
    assert read_health(path).status is WorkerHealthStatus.UNAVAILABLE
    assert read_health(path).reason == "health_file_invalid"


def test_frontdesk_reads_fresh_health_for_each_slow_admission(tmp_path):
    now = [100.0]
    current = [WorkerHealth(WorkerHealthStatus.UNAVAILABLE, "worker_stopped",
                            worker_id="worker-1", checked_at=now[0])]
    with JobStore(tmp_path / "jobs.sqlite", payload_codec=FakePayloadCodec(),
                  clock=lambda: now[0]) as store:
        desk = FrontDesk(
            store=store,
            health_provider=lambda: current[0],
            clock=lambda: now[0],
        )
        unavailable = desk.submit(Request("document.compose", target="draft", steps=2),
                                  raw_utterance="Write a local draft.")
        assert unavailable.status == "unavailable"
        current[0] = WorkerHealth(WorkerHealthStatus.AVAILABLE, worker_id="worker-1",
                                  checked_at=now[0])
        queued = desk.submit(Request("document.compose", target="other", steps=2),
                             raw_utterance="Write another local draft.")
        assert queued.status == "queued"


def test_subscription_cli_requires_explicit_human_flag_and_no_api_environment(capsys):
    assert run([]) == 2
    assert "confirm-subscription-auth" in capsys.readouterr().out
    assert _api_environment_absent({}) is True
    assert _api_environment_absent({"ANTHROPIC_API_KEY": "metered"}) is False
    assert _api_environment_absent({"ANTHROPIC_API_KEY": ""}) is True
    for selector in (
        "CLAUDE_CODE_USE_ANTHROPIC_AWS", "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_USE_MANTLE", "CLAUDE_CODE_USE_VERTEX",
    ):
        assert _api_environment_absent({selector: "1"}) is False
    assert _api_environment_absent({"claude_code_use_foundry": "1"}) is False


def test_subscription_cli_exposes_only_actually_bound_knowledge_sources():
    empty = RuntimeServices({}, None, None, None, None, None, False, None, None)
    assert _available_knowledge_capabilities(empty) == frozenset()
    browser = RuntimeServices({}, None, None, None, object(), None, False, None, None)
    assert _available_knowledge_capabilities(browser) == frozenset({"browser.inspect"})
    google = RuntimeServices({}, None, None, None, None, object(), True, None, None)
    assert _available_knowledge_capabilities(google) == frozenset({
        "google.drive.list", "google.drive.read", "google.docs.read",
        "google.gmail.read", "google.calendar.read",
    })


def test_subscription_cli_accepts_explicit_isolated_state_paths(tmp_path):
    args = _arguments([
        "--job-store", str(tmp_path / "jobs.sqlite3"),
        "--health-file", str(tmp_path / "health.json"),
        "--agentic-workspace", str(tmp_path / "agent-jobs"),
    ])
    assert args.job_store == str(tmp_path / "jobs.sqlite3")
    assert args.health_file == str(tmp_path / "health.json")
    assert args.agentic_workspace == str(tmp_path / "agent-jobs")
