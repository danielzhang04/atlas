from pathlib import Path


def test_work_lane_has_no_heavy_api_path():
    text = "\n".join(Path(name).read_text(encoding="utf-8") for name in ("worker/claude_launcher.py", "worker/work.py"))
    assert "anthropic" not in text and "claude_agent_sdk" not in text
    assert '"--print"' not in text and '"-p"' not in text
