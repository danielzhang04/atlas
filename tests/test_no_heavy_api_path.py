import ast
from pathlib import Path
import pytest

import time

from worker.contracts import Lane, Request, RouteDecision
from worker.frontdesk import FrontDesk
from worker.jobstore import JobStore
from worker.routing_policy import FastDispatchRejected, dispatch_fast, route
from worker.subscription_worker import WorkerHealth, WorkerHealthStatus


class FakePayloadCodec:
    codec_id = "test-reverse-v1"

    def protect(self, plaintext, *, entropy):
        return plaintext[::-1]

    def unprotect(self, ciphertext, *, entropy):
        return ciphertext[::-1]


def test_standalone_core_has_no_kbmcp_imports():
    root = Path(__file__).parents[1] / "worker"
    for path in (root / "contracts.py", root / "routing_policy.py", root / "jobstore.py", root / "subscription_worker.py",
                 root / "turn_interpreter.py", root / "voice_frontdesk.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        imports += [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
        assert not any(name == "kbmcp" or name.startswith("kbmcp.") for name in imports)


def test_slow_work_does_not_invoke_any_callback():
    calls = []
    request = Request("document.compose", target="draft", durable_artifact=True, artifact="draft.md")
    decision = route(request)
    assert decision.lane is Lane.SLOW
    assert calls == []


def test_fast_executor_guard_rejects_slow_decisions_before_dispatch():
    class Executor:
        def __init__(self):
            self.calls = []

        def execute_fast(self, request):
            self.calls.append(request)

    executor = Executor()
    decision = route(Request("document.compose", target="draft", durable_artifact=True, artifact="draft.md"))
    with pytest.raises(FastDispatchRejected):
        dispatch_fast(decision, Request("document.compose", target="draft", durable_artifact=True, artifact="draft.md"), executor)
    assert executor.calls == []


def test_fast_executor_guard_recomputes_forged_fast_decisions():
    class Executor:
        def execute_fast(self, request):
            raise AssertionError("slow work reached executor")

    slow_request = Request("document.compose", target="draft", durable_artifact=True, artifact="draft.md")
    with pytest.raises(FastDispatchRejected):
        dispatch_fast(RouteDecision(Lane.FAST), slow_request, Executor())


def test_unavailable_is_the_only_failure_mode_exposed_by_worker_seam():
    source = (Path(__file__).parents[1] / "worker" / "subscription_worker.py").read_text(encoding="utf-8")
    assert "SubscriptionWorkerUnavailable" in source
    assert "fallback" not in source.lower()


def test_raw_heavy_context_forces_slow_even_when_typed_request_claims_fast():
    request = Request("calendar.create_event", target="event")
    assert route(request, raw_utterance="Research these sources, then create and verify the event.").lane is Lane.SLOW


def test_long_detailed_single_event_is_not_fast_without_the_positive_typed_shape():
    request = Request("calendar.create_event", target="event")
    raw = "Schedule one detailed calendar event with agenda " + "notes " * 100
    assert route(request, raw_utterance=raw).lane is Lane.SLOW


def test_frontdesk_raw_guard_persists_slow_and_never_calls_executor(tmp_path):
    class Executor:
        def execute_fast(self, request):
            raise AssertionError("executor must not run on voice admission")

    health = WorkerHealth(WorkerHealthStatus.AVAILABLE, worker_id="test", checked_at=time.time())
    with JobStore(tmp_path / "raw-guard.sqlite", payload_codec=FakePayloadCodec()) as store:
        desk = FrontDesk(store=store, fast_executor=Executor(), worker_health=health)
        outcome = desk.submit(Request("calendar.create_event", target="event"),
                              raw_utterance="Research and synthesize these sources, then schedule it.")
        assert outcome.lane is Lane.SLOW
        assert outcome.status == "queued"
