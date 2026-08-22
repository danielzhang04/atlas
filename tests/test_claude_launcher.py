import json
from types import SimpleNamespace
from uuid import uuid4

from worker.claude_launcher import (
    ClaudeLauncher,
    METERED_PROVIDER_ENV,
    parse_result,
    scrubbed_environment,
)


class RecordingRunner:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((tuple(argv), kwargs))
        return self.responses.pop(0)


def command_result(stdout="", returncode=0):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")


def result_frame(job_id, nonce, status="succeeded", summary="done"):
    payload = {
        "job_id": job_id,
        "status": status,
        "summary": summary,
    }
    return f"ATLAS_RESULT_V1:{nonce}:{json.dumps(payload)}"


def launcher_for_state(state):
    rows = json.dumps([{"id": "abcdef12", "name": "atlas", "state": state}])
    runner = RecordingRunner([command_result(rows)])
    return ClaudeLauncher("claude", runner=runner)


def test_launch_uses_exact_connected_background_argv(tmp_path):
    runner = RecordingRunner([command_result("backgrounded · abcdef12")])
    launcher = ClaudeLauncher("claude", runner=runner)
    session_id = str(uuid4())

    launcher.launch(
        session_id=session_id,
        name="atlas-x",
        prompt="do it",
        cwd=tmp_path,
    )

    assert runner.calls[0][0] == (
        "claude",
        "--bg",
        "--chrome",
        "--brief",
        "--setting-sources",
        "user",
        "--permission-mode",
        "auto",
        "--tools",
        "default",
        "--model",
        "claude-fable-5",
        "--effort",
        "medium",
        "--session-id",
        session_id,
        "--name",
        "atlas-x",
        "do it",
    )


def test_launch_parses_session_id_from_backgrounded_stdout(tmp_path):
    runner = RecordingRunner([command_result("task backgrounded · ABCDEF12")])
    launcher = ClaudeLauncher("claude", runner=runner)

    actual = launcher.launch(
        session_id=str(uuid4()),
        name="atlas-x",
        prompt="do it",
        cwd=tmp_path,
    )

    assert actual == "abcdef12"
    assert len(runner.calls) == 1


def test_launch_resolves_session_id_from_agents_fallback(tmp_path):
    rows = json.dumps(
        [
            {"id": "11111111", "name": "other", "state": "working"},
            {"sessionId": "ABCDEF12", "name": "atlas-x", "status": "working"},
        ]
    )
    runner = RecordingRunner([command_result("launched"), command_result(rows)])
    launcher = ClaudeLauncher("claude", runner=runner)

    actual = launcher.launch(
        session_id=str(uuid4()),
        name="atlas-x",
        prompt="do it",
        cwd=tmp_path,
    )

    assert actual == "abcdef12"
    assert runner.calls[1][0] == (
        "claude",
        "agents",
        "--json",
        "--all",
        "--cwd",
        str(tmp_path),
    )


def test_status_maps_running_literal():
    assert launcher_for_state("working").status("abcdef12") == "running"


def test_status_maps_done_literal():
    assert launcher_for_state("ready-for-review").status("abcdef12") == "done"


def test_status_maps_failed_literal():
    assert launcher_for_state("error").status("abcdef12") == "failed"


def test_status_maps_needs_input_literal():
    assert launcher_for_state("waiting").status("abcdef12") == "needs_input"


def test_status_maps_unknown_literal():
    assert launcher_for_state("mystery").status("abcdef12") == "unknown"


def test_logs_strip_ansi_and_collapse_only_consecutive_identical_lines():
    stdout = (
        "\x1b[31mA\x1b[0m\n"
        "\x1b[31mA\x1b[0m\n"
        "B\n"
        "\x1b[32mA\x1b[0m\n"
        "\x1b[34mA\x1b[0m\n"
        "\x1b[34mA\x1b[0m\n"
    )
    runner = RecordingRunner([command_result(stdout)])
    launcher = ClaudeLauncher("claude", runner=runner)

    assert launcher.logs("abcdef12") == ["A", "B", "A", "A"]


def test_parse_result_accepts_succeeded_status():
    job_id = str(uuid4())
    frame = result_frame(job_id, "nonce", "succeeded", "completed")

    assert parse_result([frame], nonce="nonce", job_id=job_id) == (
        "succeeded",
        "completed",
    )


def test_parse_result_accepts_failed_status():
    job_id = str(uuid4())
    frame = result_frame(job_id, "nonce", "failed", "could not finish")

    assert parse_result([frame], nonce="nonce", job_id=job_id) == (
        "failed",
        "could not finish",
    )


def test_parse_result_accepts_cancelled_status():
    job_id = str(uuid4())
    frame = result_frame(job_id, "nonce", "cancelled", "stopped")

    assert parse_result([frame], nonce="nonce", job_id=job_id) == (
        "cancelled",
        "stopped",
    )


def test_parse_result_rejects_wrong_job_id():
    expected_job_id = str(uuid4())
    frame = result_frame(str(uuid4()), "nonce")

    assert parse_result([frame], nonce="nonce", job_id=expected_job_id) is None


def test_parse_result_rejects_wrong_nonce():
    job_id = str(uuid4())
    frame = result_frame(job_id, "wrong-nonce")

    assert parse_result([frame], nonce="expected-nonce", job_id=job_id) is None


def test_parse_result_rejects_template_echo():
    job_id = str(uuid4())
    payload = {
        "job_id": job_id,
        "status": "succeeded|failed|cancelled",
        "summary": "bounded factual summary",
        "error_code": None,
        "artifacts": [],
    }
    frame = f"ATLAS_RESULT_V1:nonce:{json.dumps(payload)}"

    assert parse_result([frame], nonce="nonce", job_id=job_id) is None


def test_parse_result_rejects_two_conflicting_frames():
    job_id = str(uuid4())
    first = result_frame(job_id, "nonce", "succeeded", "done")
    second = result_frame(job_id, "nonce", "failed", "not done")

    assert parse_result([first, second], nonce="nonce", job_id=job_id) is None


def test_scrubbed_environment_removes_metered_and_secret_shaped_names():
    source = {name: "secret" for name in METERED_PROVIDER_ENV}
    source.update(
        {
            "CUSTOM_PASSWORD": "secret",
            "service-token": "secret",
            "DB_CREDENTIAL": "secret",
            "PATH": "path-value",
            "USERPROFILE": "profile-value",
            "LOCALAPPDATA": "appdata-value",
        }
    )

    scrubbed = scrubbed_environment(source)

    assert not METERED_PROVIDER_ENV.intersection(scrubbed)
    assert "CUSTOM_PASSWORD" not in scrubbed
    assert "service-token" not in scrubbed
    assert "DB_CREDENTIAL" not in scrubbed
    assert scrubbed == {
        "PATH": "path-value",
        "USERPROFILE": "profile-value",
        "LOCALAPPDATA": "appdata-value",
    }
