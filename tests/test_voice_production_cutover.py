import ast
import asyncio
from hashlib import sha256
from pathlib import Path

import pytest

from livekit.agents import StopResponse
from worker.app import AtlasAgent, _stt_keyterms
from worker.contracts import utc_timestamp
from worker.subscription_worker import WorkerHealth, WorkerHealthStatus
from worker.turn_interpreter import StructuredToolResponse
from worker.voice_runtime import build_voice_runtime


ROOT = Path(__file__).resolve().parents[1]


class FakePayloadCodec:
    codec_id = "test-xor-v1"

    def protect(self, plaintext, *, entropy):
        key = sha256(entropy).digest()
        return bytes(value ^ key[index % len(key)] for index, value in enumerate(plaintext))

    def unprotect(self, ciphertext, *, entropy):
        return self.protect(ciphertext, entropy=entropy)


class StructuredClient:
    def __init__(self):
        self.calls = 0

    async def create(self, **_kwargs):
        self.calls += 1
        if self.calls == 2:
            return StructuredToolResponse(text="I queued the draft. It is visible in Workers.")
        return StructuredToolResponse({})


def test_live_worker_and_runtime_have_no_legacy_kb_or_tool_loop_imports():
    forbidden = {"kbmcp", "worker.fastlane", "worker.anthropic_compat", "worker.toolreg"}
    for relative in ("worker/app.py", "worker/runtime.py", "worker/voice_runtime.py"):
        source = (ROOT / relative).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = {node.module or "" for node in ast.walk(tree)
                   if isinstance(node, ast.ImportFrom)}
        imports |= {alias.name for node in ast.walk(tree)
                    if isinstance(node, ast.Import) for alias in node.names}
        assert not any(name in forbidden or name.startswith("kbmcp.") for name in imports)
    app_source = (ROOT / "worker" / "app.py").read_text(encoding="utf-8")
    assert "llm=None" in app_source
    assert "tools=[]" in app_source
    assert "voice_runtime.desk.handle" in app_source
    assert "addressing" not in app_source
    assert "_silence_watcher" not in app_source
    assert "gated (not addressed)" not in app_source
    voice_source = (ROOT / "worker" / "voice_frontdesk.py").read_text(encoding="utf-8")
    assert "couldn't safely" not in voice_source.lower()
    assert "raw_voice_is_action" not in voice_source


def test_standalone_config_has_no_kb_ops_or_heavy_api_fallback_keys():
    config = (ROOT / "config" / "atlas.yaml").read_text(encoding="utf-8")
    assert "ops_root:" not in config
    assert "escalation_model:" not in config
    assert "max_tool_turns:" not in config
    assert "engagement_timeout_s:" not in config
    assert "engaged_window_s:" not in config
    assert "interpreter_timeout_s: 10.0" in config


def test_voice_runtime_durably_admits_without_invoking_a_model_executor(tmp_path):
    health = WorkerHealth(
        WorkerHealthStatus.AVAILABLE,
        worker_id="cutover-test",
        checked_at=utc_timestamp(),
    )
    runtime = build_voice_runtime(
        {"job_store_path": str(tmp_path / "atlas.sqlite3")},
        structured_client=StructuredClient(),
        payload_codec=FakePayloadCodec(),
        worker_health=health,
    )
    try:
        result = asyncio.run(runtime.desk.handle("Write and verify a local draft."))
        assert result.status == "queued"
        assert result.text == "I queued the draft. It is visible in Workers."
        assert runtime.store.get(result.job_id).state.value == "queued"
        assert runtime.jobs_projection() == [{
            "id": result.job_id,
            "status": "queued",
            "lane": "slow",
            "operation": "claude.connected",
            "updated_at": str(runtime.store.get(result.job_id).updated_at),
        }]
        events = runtime.job_events_projection(result.job_id)
        assert len(events) == 1
        assert events[0]["sequence"] == 1
        assert events[0]["kind"] == "created"
        assert events[0]["state"] == "queued"
        assert isinstance(events[0]["timestamp"], float)
    finally:
        runtime.close()


def test_agent_always_stops_after_host_handler():
    seen = []
    agent = AtlasAgent(instructions="host controlled", llm=None, tools=[])

    async def handler(text):
        seen.append(text)

    agent.turn_handler = handler
    message = type("Message", (), {"text_content": "hello"})()
    with pytest.raises(StopResponse):
        asyncio.run(agent.on_user_turn_completed(None, message))
    assert seen == ["hello"]


def test_agent_without_host_handler_fails_closed():
    agent = AtlasAgent(instructions="host controlled", llm=None, tools=[])
    message = type("Message", (), {"text_content": "must not fall through"})()
    with pytest.raises(StopResponse):
        asyncio.run(agent.on_user_turn_completed(None, message))


def test_stt_keyterms_are_bounded_and_standalone():
    assert _stt_keyterms({}) == ["Atlas"]
    assert _stt_keyterms({"stt_keyterms": ["Atlas", "Calendar"]}) == ["Atlas", "Calendar"]
    assert _stt_keyterms({"stt_keyterms": ["x" * 65]}) == ["Atlas"]
