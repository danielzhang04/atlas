from pathlib import Path


def test_work_lane_has_no_heavy_api_path():
    paths = ("worker/claude_launcher.py", "worker/work.py")
    text = "\n".join(Path(name).read_text(encoding="utf-8") for name in paths)

    assert "anthropic" not in text
    assert "claude_agent_sdk" not in text
    assert '"--print"' not in text
    assert '"-p"' not in text
