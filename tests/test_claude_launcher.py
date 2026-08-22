import json
from types import SimpleNamespace
from uuid import uuid4

from worker.claude_launcher import (
    ClaudeLauncher,
    METERED_PROVIDER_ENV,
    parse_result,
    scrubbed_environment,
    worker_prompt,
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


def test_status_maps_running_literal(tmp_path):
    launcher = launcher_for_state("working")

    assert launcher.status("abcdef12", cwd=tmp_path) == "running"


def test_status_maps_done_literal(tmp_path):
    launcher = launcher_for_state("ready-for-review")

    assert launcher.status("abcdef12", cwd=tmp_path) == "done"


def test_status_maps_failed_literal(tmp_path):
    launcher = launcher_for_state("error")

    assert launcher.status("abcdef12", cwd=tmp_path) == "failed"


def test_status_maps_needs_input_literal(tmp_path):
    launcher = launcher_for_state("waiting")

    assert launcher.status("abcdef12", cwd=tmp_path) == "needs_input"


def test_status_maps_unknown_literal(tmp_path):
    launcher = launcher_for_state("mystery")

    assert launcher.status("abcdef12", cwd=tmp_path) == "unknown"


def test_logs_strip_ansi_and_collapse_only_consecutive_identical_lines(tmp_path):
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

    assert launcher.logs("abcdef12", cwd=tmp_path) == ["A", "B", "A", "A"]


def test_session_operations_use_explicit_working_directory(tmp_path):
    rows = json.dumps([{"id": "abcdef12", "state": "working"}])
    runner = RecordingRunner(
        [
            command_result(rows),
            command_result("output"),
            command_result(),
        ]
    )
    launcher = ClaudeLauncher("claude", runner=runner)

    assert launcher.status("abcdef12", cwd=tmp_path) == "running"
    assert launcher.logs("abcdef12", cwd=tmp_path) == ["output"]
    launcher.cancel("abcdef12", cwd=tmp_path)

    assert [call[1]["cwd"] for call in runner.calls] == [
        tmp_path,
        tmp_path,
        tmp_path,
    ]
    assert runner.calls[0][0][-1] == str(tmp_path)
    assert "--cwd" not in runner.calls[1][0]


def test_parse_result_accepts_new_minimal_frame():
    job_id = str(uuid4())
    frame = result_frame(job_id, "nonce", "succeeded", "completed")

    assert parse_result([frame], nonce="nonce", job_id=job_id) == (
        "succeeded",
        "completed",
    )


def test_parse_result_accepts_live_two_line_frame_with_raw_backslash_path():
    job_id = "52dbb226-…"
    summary = "Wrote a 3-line haiku …"
    first = (
        f'ATLAS_RESULT_V1:nonce:{{"job_id":"{job_id}",'
        '"status":"succeeded","summary":"Wrote a 3-line haiku …",'
    )
    second = (
        r'"error_code":null,"artifacts":["C:\Users\danie\AppData\Local\Atlas\jobs\52dbb226-…\haiku.txt"]}'
    )

    assert parse_result([first, second], nonce="nonce", job_id=job_id) == (
        "succeeded",
        summary,
    )


def test_parse_result_accepts_three_line_wrapped_frame_with_terminal_noise():
    job_id = str(uuid4())
    logs = [
        f"ATLAS_RESULT_V1:nonce:{{\"job_id\":\"{job_id}\",",
        '    "status":"succeeded","summary":"three',
        '    lines","error_code":null,"artifacts":[]} trailing chrome',
        "✻ Baked for 14s",
    ]

    assert parse_result(logs, nonce="nonce", job_id=job_id) == (
        "succeeded",
        "three lines",
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
        "summary": "one factual sentence",
    }
    frame = f"ATLAS_RESULT_V1:nonce:{json.dumps(payload)}"

    assert parse_result([frame], nonce="nonce", job_id=job_id) is None


def test_parse_result_ignores_wrapped_prompt_template_and_accepts_real_frame():
    job_id = str(uuid4())
    logs = [
        f'ATLAS_RESULT_V1:nonce:{{"job_id":"{job_id}",',
        '  "status":"succeeded|failed|cancelled","summary":"one factual',
        '                                      sentence"}',
        "ordinary terminal output",
        f'ATLAS_RESULT_V1:nonce:{{"job_id":"{job_id}","status":"succeeded",',
        '  "summary":"finished cleanly"}',
    ]

    assert parse_result(logs, nonce="nonce", job_id=job_id) == (
        "succeeded",
        "finished cleanly",
    )


def test_worker_prompt_uses_minimal_result_frame_contract():
    job_id = str(uuid4())

    prompt = worker_prompt(job_id, "nonce", "Do it")
    template = prompt.split("\n\n", 1)[0]

    assert template.endswith('"summary":"one factual sentence"}')
    assert "artifacts" not in prompt
    assert "}}" not in prompt


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
