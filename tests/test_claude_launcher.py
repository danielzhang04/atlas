import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from worker.claude_launcher import ClaudeLauncher, parse_result, scrubbed_environment


class Runner:
    def __init__(self): self.calls = []
    def __call__(self, argv, **kw):
        self.calls.append(tuple(argv))
        if "agents" in argv: return SimpleNamespace(returncode=0, stdout="[]", stderr="")
        return SimpleNamespace(returncode=0, stdout="backgrounded · abcdef12", stderr="")


def test_launch_exact_argv(tmp_path):
    runner, sid = Runner(), str(uuid4())
    launcher = ClaudeLauncher("claude", runner=runner)
    assert launcher.launch(session_id=sid, name="atlas-x", prompt="do it", cwd=tmp_path) == "abcdef12"
    assert runner.calls[0] == ("claude", "--bg", "--chrome", "--brief", "--setting-sources", "user",
        "--permission-mode", "auto", "--tools", "default", "--model", "claude-fable-5", "--effort",
        "medium", "--session-id", sid, "--name", "atlas-x", "do it")


def test_result_and_environment():
    job, nonce = str(uuid4()), "nonce"
    frame = f'ATLAS_RESULT_V1:{nonce}:' + json.dumps({"job_id": job, "status": "succeeded", "summary": "done"})
    assert parse_result([frame], nonce=nonce, job_id=job) == "done"
    assert "OPENAI_API_KEY" not in scrubbed_environment({"OPENAI_API_KEY": "x", "PATH": "ok"})
