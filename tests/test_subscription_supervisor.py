import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
import re
import sys

import pytest

from worker.contracts import JobState, Request
from worker.agent_logic import (
    read_only_knowledge_launch, read_only_review_launch, standard_heavy_launch,
)
from worker.frontdesk import FrontDesk
from worker.jobstore import JobStore
from worker.broker_ipc import BrokerIpcServer
from worker.capability_runner import BrokeredReadObservation
from worker.knowledge_mcp import BrokerMcpClient, BrokerMcpLaunchConfig
from worker.subscription_supervisor import (
    ClaudeBackgroundTransport,
    AgenticRuntimeConfig,
    CommandResult,
    LocalCommandRunner,
    METERED_PROVIDER_ENV,
    SessionState,
    SubscriptionAuthorization,
    SubscriptionSupervisor,
    SupervisorError,
    parse_worker_result,
    scrub_subscription_environment,
)
from worker.subscription_worker import WorkerHealth, WorkerHealthStatus


class FakePayloadCodec:
    codec_id = "test-reverse-v1"

    def protect(self, plaintext, *, entropy):
        return plaintext[::-1]

    def unprotect(self, ciphertext, *, entropy):
        return ciphertext[::-1]


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.session_id = None
        self.prompt = None
        self.state = "working"
        self.result_status = "succeeded"
        self.reported_background_id = None
        self.background_stdout = None
        self.name = None

    def run(self, argv, *, cwd, env, timeout):
        argv = tuple(argv)
        self.calls.append((argv, cwd, dict(env), timeout))
        if "--bg" in argv:
            self.session_id = argv[argv.index("--session-id") + 1]
            self.prompt = argv[-1]
            self.name = argv[argv.index("--name") + 1]
            reported = self.reported_background_id or self.session_id[:8]
            return CommandResult(
                0, self.background_stdout or f"backgrounded · {reported}\n",
            )
        if len(argv) > 1 and argv[1] == "agents":
            reported = self.reported_background_id or self.session_id
            return CommandResult(0, json.dumps([
                {"id": reported, "state": self.state, "name": self.name},
            ]))
        if len(argv) > 1 and argv[1] == "logs":
            nonce = re.search(r"ATLAS_RESULT_V1:([A-Za-z0-9_-]+):", self.prompt).group(1)
            result = {
                "job_id": self.session_id,
                "status": self.result_status,
                "summary": "Verified local result.",
                "error_code": None,
                "artifacts": ["output/result.md"],
            }
            return CommandResult(0, f"work log\nATLAS_RESULT_V1:{nonce}:{json.dumps(result)}\n")
        if len(argv) > 1 and argv[1] == "stop":
            reported = self.reported_background_id or self.session_id
            if not reported.startswith(argv[2]):
                return CommandResult(1, "not found\n")
            self.state = "stopped"
            return CommandResult(0, "stopped\n")
        raise AssertionError(argv)


class UnusedDispatcher:
    def dispatch_observed(self, _call):
        raise AssertionError("launch construction must not dispatch")


class KnowledgeDispatcher:
    def __init__(self):
        self.calls = []

    def dispatch_observed(self, call):
        self.calls.append(call)
        number = len(self.calls)
        content = json.dumps(
            {"source": dict(call.parameters), "fact": f"verified fact {number}"},
            sort_keys=True, separators=(",", ":"),
        )
        return BrokeredReadObservation(
            call.capability_id, f"proposal-{number}",
            sha256(json.dumps(dict(call.parameters), sort_keys=True).encode()).hexdigest(),
            content, sha256(content.encode()).hexdigest(), False,
        )


class KnowledgeRunner:
    def __init__(self, *, candidate_answers=None, review_verdicts=None, reads_per_stage=2,
                 wrong_review_digest=False):
        self.calls = []
        self.sessions = {}
        self.candidate_answers = list(candidate_answers or ["Two-source private synthesis."])
        self.review_verdicts = list(review_verdicts or ["pass"])
        self.reads_per_stage = reads_per_stage
        self.wrong_review_digest = wrong_review_digest
        self.author_count = 0
        self.review_count = 0

    def run(self, argv, *, cwd, env, timeout):
        argv = tuple(argv)
        self.calls.append((argv, cwd, dict(env), timeout))
        if "--bg" in argv:
            session_id = argv[argv.index("--session-id") + 1]
            prompt = argv[-1]
            job_id = env.get("ATLAS_BROKER_JOB_ID")
            if job_id is None:
                job_id = re.search(r'"job_id":"([0-9a-f-]{36})"', prompt).group(1)
            if "ATLAS_BROKER_URL" in env:
                client = BrokerMcpClient.from_environment(env)
                evidence = [
                    client.read(
                        "google.drive.read", {"file_id": f"source-{index}-{session_id}"},
                    )["proposal_id"]
                    for index in range(self.reads_per_stage)
                ]
            else:
                evidence = []
            if "ATLAS_CANDIDATE_V1:" in prompt:
                nonce = re.search(r"ATLAS_CANDIDATE_V1:([A-Za-z0-9_-]+):", prompt).group(1)
                answer = self.candidate_answers[min(
                    self.author_count, len(self.candidate_answers) - 1,
                )]
                self.author_count += 1
                body = {
                    "job_id": job_id, "status": "candidate",
                    "answer": answer, "evidence_ids": evidence,
                    "error_code": None,
                }
                marker = f"ATLAS_CANDIDATE_V1:{nonce}:"
            else:
                nonce = re.search(r"ATLAS_REVIEW_V1:([A-Za-z0-9_-]+):", prompt).group(1)
                digest = re.search(r'"candidate_digest":\s*"([0-9a-f]{64})"', prompt).group(1)
                verdict = self.review_verdicts[min(
                    self.review_count, len(self.review_verdicts) - 1,
                )]
                self.review_count += 1
                body = {
                    "job_id": job_id, "verdict": verdict,
                    "candidate_digest": "0" * 64 if self.wrong_review_digest else digest,
                    "evidence_ids": evidence,
                    "findings": [
                        "Independent observations support the exact candidate."
                        if verdict == "pass" else "Revise the unsupported claim.",
                    ],
                }
                marker = f"ATLAS_REVIEW_V1:{nonce}:"
            self.sessions[session_id] = marker + json.dumps(body, separators=(",", ":"))
            return CommandResult(0, f"backgrounded: {session_id[:8]}\n")
        if len(argv) > 1 and argv[1] == "agents":
            return CommandResult(0, json.dumps([
                {"id": session_id, "state": "completed"} for session_id in self.sessions
            ]))
        if len(argv) > 1 and argv[1] == "logs":
            short = argv[2]
            session_id = next(key for key in self.sessions if key.startswith(short))
            return CommandResult(0, self.sessions[session_id] + "\n")
        if len(argv) > 1 and argv[1] == "stop":
            return CommandResult(0, "stopped\n")
        raise AssertionError(argv)


def broker_mcp_config(tmp_path):
    package_root = tmp_path / "atlas-runtime"
    adapter = package_root / "worker" / "knowledge_mcp.py"
    adapter.parent.mkdir(parents=True, exist_ok=True)
    adapter.write_text("# test adapter", encoding="utf-8")
    python = tmp_path / "python.exe"
    python.write_text("test", encoding="utf-8")
    server = BrokerIpcServer(
        UnusedDispatcher(), job_id="3f75564b-cad1-4b9e-9e79-4f15013b43c2",
        token_factory=lambda: "a" * 43,
        allowed_capabilities=frozenset({"google.drive.read"}),
    )
    endpoint = server.start()
    return server, BrokerMcpLaunchConfig(endpoint, python, package_root)


def setup_supervisor(tmp_path, *, now=None):
    now = now or [100.0]
    codec = FakePayloadCodec()
    store = JobStore(tmp_path / "worker.sqlite", payload_codec=codec, clock=lambda: now[0])
    health = WorkerHealth(
        WorkerHealthStatus.AVAILABLE, worker_id="subscription-test", checked_at=now[0],
    )
    desk = FrontDesk(store=store, worker_health=health, clock=lambda: now[0])
    outcome = desk.submit(
        Request("answer.compose", target="bounded topic"),
        raw_utterance="Explain the bounded topic.",
    )
    runner = FakeRunner()
    transport = ClaudeBackgroundTransport(
        runner,
        environment={
            "PATH": "safe-path", "CLAUDE_CONFIG_DIR": "safe-config-path",
            "ANTHROPIC_API_KEY": "must-not-cross", "GOOGLE_ACCESS_TOKEN": "must-not-cross",
        },
    )
    authorization = SubscriptionAuthorization(
        "claude-subscription", checked_at=now[0], api_environment_absent=True,
        human_confirmed=True,
    )
    supervisor = SubscriptionSupervisor(
        store, transport, workdir=tmp_path, authorization=authorization,
        lease_seconds=30, clock=lambda: now[0],
    )
    return now, store, desk, outcome, runner, supervisor


def setup_connected_supervisor(tmp_path, *, now=None):
    now = now or [100.0]
    store = JobStore(tmp_path / "connected.sqlite", payload_codec=FakePayloadCodec(),
                     clock=lambda: now[0])
    desk = FrontDesk(
        store=store,
        worker_health=WorkerHealth(
            WorkerHealthStatus.AVAILABLE, worker_id="subscription-test", checked_at=now[0]),
        clock=lambda: now[0],
    )
    instruction = "Open Nasdaq in Chrome, then summarize what loaded."
    outcome = desk.submit(
        Request("claude.connected", target="connected-cli", app="claude-code", steps=2),
        raw_utterance=instruction,
    )
    workspace_root = tmp_path / "connected-jobs"
    workspace_root.mkdir()
    runner = FakeRunner()
    supervisor = SubscriptionSupervisor(
        store,
        ClaudeBackgroundTransport(runner, environment={
            "PATH": "safe", "CLAUDE_CONFIG_DIR": "safe-config",
            "ANTHROPIC_API_KEY": "must-not-cross", "GOOGLE_ACCESS_TOKEN": "must-not-cross",
        }),
        workdir=tmp_path,
        authorization=SubscriptionAuthorization(
            "claude-subscription", checked_at=now[0], api_environment_absent=True,
            human_confirmed=True,
        ),
        lease_seconds=30,
        clock=lambda: now[0],
        connected_workspace_root=workspace_root,
    )
    return now, store, outcome, instruction, runner, supervisor, workspace_root


def setup_knowledge_supervisor(tmp_path, runner=None, *, now=None, combined=False):
    now = now or [100.0]
    store = JobStore(tmp_path / "knowledge.sqlite", payload_codec=FakePayloadCodec(),
                     clock=lambda: now[0])
    desk = FrontDesk(
        store=store,
        worker_health=WorkerHealth(
            WorkerHealthStatus.AVAILABLE, worker_id="subscription-test", checked_at=now[0]),
        clock=lambda: now[0],
    )
    request = (
        Request(
            "document.compose", research=True, cross_source=True,
            durable_artifact=True, artifact="brief.md",
        )
        if combined else Request("research.synthesize", research=True, cross_source=True)
    )
    outcome = desk.submit(
        request,
        raw_utterance=("Research two private sources and draft a complete brief."
                       if combined else "Compare two connected private sources."),
    )
    workspace_root = tmp_path / "agent-jobs"
    workspace_root.mkdir()
    dispatcher = KnowledgeDispatcher()
    runner = runner or KnowledgeRunner()
    supervisor = SubscriptionSupervisor(
        store, ClaudeBackgroundTransport(runner, environment={"PATH": "safe"}),
        workdir=tmp_path,
        authorization=SubscriptionAuthorization(
            "claude-subscription", checked_at=now[0], api_environment_absent=True,
            human_confirmed=True,
        ),
        lease_seconds=30, clock=lambda: now[0],
        agentic_runtime=AgenticRuntimeConfig(
            dispatcher, frozenset({"google.drive.read"}), Path(sys.executable),
            Path(__file__).parents[1], workspace_root,
        ),
    )
    return now, store, desk, outcome, dispatcher, runner, supervisor


def setup_build_supervisor(tmp_path, runner=None, *, now=None, request=None,
                           instruction="Draft the complete project brief."):
    now = now or [100.0]
    store = JobStore(tmp_path / "build.sqlite", payload_codec=FakePayloadCodec(),
                     clock=lambda: now[0])
    desk = FrontDesk(
        store=store,
        worker_health=WorkerHealth(
            WorkerHealthStatus.AVAILABLE, worker_id="subscription-test", checked_at=now[0]),
        clock=lambda: now[0],
    )
    outcome = desk.submit(
        request or Request("document.compose", durable_artifact=True, artifact="draft.md"),
        raw_utterance=instruction,
    )
    workspace_root = tmp_path / "agent-jobs"
    workspace_root.mkdir()
    runner = runner or KnowledgeRunner(candidate_answers=["# Project brief\n\nComplete draft."])
    supervisor = SubscriptionSupervisor(
        store, ClaudeBackgroundTransport(runner, environment={"PATH": "safe"}),
        workdir=tmp_path,
        authorization=SubscriptionAuthorization(
            "claude-subscription", checked_at=now[0], api_environment_absent=True,
            human_confirmed=True,
        ),
        lease_seconds=30, clock=lambda: now[0],
        agentic_runtime=AgenticRuntimeConfig(
            UnusedDispatcher(), frozenset(), Path(sys.executable),
            Path(__file__).parents[1], workspace_root,
        ),
    )
    return now, store, desk, outcome, runner, supervisor


def test_subscription_background_launch_is_structurally_subscription_only_and_result_fenced(tmp_path):
    now, store, _desk, outcome, runner, supervisor = setup_supervisor(tmp_path)
    try:
        active = supervisor.start_next()
        argv, _cwd, env, _timeout = runner.calls[0]
        assert "--bg" in argv and "--safe-mode" in argv and "--strict-mcp-config" in argv
        assert "--print" not in argv and "-p" not in argv and "--bare" not in argv
        assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
        assert argv[argv.index("--tools") + 1] == ""
        assert "ANTHROPIC_API_KEY" not in env and "GOOGLE_ACCESS_TOKEN" not in env
        assert env["CLAUDE_CONFIG_DIR"] == "safe-config-path"
        assert "Explain the bounded topic." in runner.prompt
        assert argv[argv.index("--model") + 1] == "claude-fable-5"
        assert active.claim.lease_token not in runner.prompt

        runner.state = "completed"
        assert supervisor.poll(active) is JobState.SUCCEEDED
        completed = store.get(outcome.job_id)
        assert completed.state is JobState.SUCCEEDED
        assert completed.public_payload["summary"] == "Verified local result."
    finally:
        store.close()


def test_knowledge_profile_fails_closed_when_no_source_runtime_is_configured(tmp_path):
    now = [100.0]
    codec = FakePayloadCodec()
    store = JobStore(tmp_path / "heavy.sqlite", payload_codec=codec, clock=lambda: now[0])
    desk = FrontDesk(
        store=store,
        worker_health=WorkerHealth(
            WorkerHealthStatus.AVAILABLE, worker_id="subscription-test", checked_at=now[0]),
        clock=lambda: now[0],
    )
    outcome = desk.submit(
        Request("research.synthesize", research=True),
        raw_utterance="Launch a knowledge-heavy workflow and compare sources.",
    )
    runner = FakeRunner()
    supervisor = SubscriptionSupervisor(
        store, ClaudeBackgroundTransport(runner, environment={"PATH": "safe"}),
        workdir=tmp_path,
        authorization=SubscriptionAuthorization(
            "claude-subscription", checked_at=now[0], api_environment_absent=True,
            human_confirmed=True,
        ),
        lease_seconds=30, clock=lambda: now[0],
    )
    try:
        assert supervisor.start_next() is None
        failed = store.get(outcome.job_id)
        assert failed.state is JobState.FAILED
        assert failed.public_payload["code"] == "knowledge_sources_unavailable"
        assert runner.calls == []
    finally:
        store.close()


def test_knowledge_profile_runs_fable_then_fresh_opus_and_encrypts_passed_answer(tmp_path):
    _now, store, _desk, outcome, dispatcher, runner, supervisor = setup_knowledge_supervisor(tmp_path)
    try:
        active = supervisor.start_next()
        assert active.stage == "agentic-author"
        assert supervisor.poll(active) is JobState.RUNNING
        assert supervisor.poll(active) is JobState.SUCCEEDED
        completed = store.get(outcome.job_id)
        assert dict(completed.public_payload) == {
            "result_available": True, "summary": "Private result available.",
        }
        protected = store.get_protected_result(outcome.job_id)
        assert protected.answer == "Two-source private synthesis."
        launches = [call for call in runner.calls if "--bg" in call[0]]
        assert [call[0][call[0].index("--model") + 1] for call in launches] == [
            "claude-fable-5", "claude-opus-5",
        ]
        assert len(dispatcher.calls) == 4
        assert supervisor._broker_servers == {}
    finally:
        store.close()


def test_build_profile_produces_private_draft_then_fresh_opus_review_without_file_tools(tmp_path):
    _now, store, _desk, outcome, runner, supervisor = setup_build_supervisor(tmp_path)
    try:
        active = supervisor.start_next()
        assert active.profile_id == "build-review" and active.stage == "agentic-author"
        assert supervisor.poll(active) is JobState.RUNNING
        assert supervisor.poll(active) is JobState.SUCCEEDED
        protected = store.get_protected_result(outcome.job_id)
        assert protected.answer == "# Project brief\n\nComplete draft."
        assert protected.artifact_name == "draft.md"
        launches = [call for call in runner.calls if "--bg" in call[0]]
        assert [call[0][call[0].index("--model") + 1] for call in launches] == [
            "claude-fable-5", "claude-opus-5",
        ]
        assert launches[0][0][launches[0][0].index("--tools") + 1] == "Agent"
        assert launches[1][0][launches[1][0].index("--tools") + 1] == ""
        assert all("ATLAS_BROKER_TOKEN" not in call[2] for call in launches)
        assert supervisor._broker_servers == {}
    finally:
        store.close()


def test_standard_profile_uses_fable_agent_envelope_and_completes_without_forced_opus(tmp_path):
    runner = KnowledgeRunner(candidate_answers=["Complete bounded answer."])
    _now, store, _desk, outcome, runner, supervisor = setup_build_supervisor(
        tmp_path, runner=runner, request=Request("answer.compose"),
        instruction="Explain this bounded topic.",
    )
    try:
        active = supervisor.start_next()
        assert active.profile_id == "standard-heavy" and active.stage == "agentic-author"
        assert supervisor.poll(active) is JobState.SUCCEEDED
        protected = store.get_protected_result(outcome.job_id)
        assert protected.answer == "Complete bounded answer."
        launches = [call for call in runner.calls if "--bg" in call[0]]
        assert len(launches) == 1
        assert launches[0][0][launches[0][0].index("--model") + 1] == "claude-fable-5"
        assert launches[0][0][launches[0][0].index("--tools") + 1] == "Agent"
    finally:
        store.close()


def test_code_change_stays_fail_closed_until_external_workspace_is_activated(tmp_path):
    _now, store, _desk, outcome, runner, supervisor = setup_build_supervisor(
        tmp_path,
        request=Request("code.change", durable_artifact=True, artifact="change.patch"),
    )
    try:
        assert supervisor.start_next() is None
        failed = store.get(outcome.job_id)
        assert failed.state is JobState.FAILED
        assert failed.public_payload["code"] == "external_workspace_not_activated"
        assert runner.calls == []
    finally:
        store.close()


def test_combined_profile_keeps_both_evidence_and_artifact_review_floors(tmp_path):
    runner = KnowledgeRunner(candidate_answers=["# Evidence-backed brief\n\nSupported draft."])
    _now, store, _desk, outcome, dispatcher, runner, supervisor = setup_knowledge_supervisor(
        tmp_path, runner, combined=True,
    )
    try:
        active = supervisor.start_next()
        assert active.profile_id == "knowledge-build-review"
        assert supervisor.poll(active) is JobState.RUNNING
        assert supervisor.poll(active) is JobState.SUCCEEDED
        assert store.get_protected_result(outcome.job_id).answer.startswith("# Evidence-backed")
        assert len(dispatcher.calls) == 4
        launches = [call for call in runner.calls if "--bg" in call[0]]
        assert launches[0][0][launches[0][0].index("--tools") + 1] == (
            "Agent,mcp__atlas_knowledge__knowledge_read"
        )
        assert launches[1][0][launches[1][0].index("--model") + 1] == "claude-opus-5"
    finally:
        store.close()


def test_knowledge_review_rework_launches_new_fable_generation_then_new_opus(tmp_path):
    runner = KnowledgeRunner(
        candidate_answers=["Unsupported first draft.", "Corrected private synthesis."],
        review_verdicts=["rework", "pass"],
    )
    _now, store, _desk, outcome, _dispatcher, runner, supervisor = setup_knowledge_supervisor(
        tmp_path, runner,
    )
    try:
        active = supervisor.start_next()
        assert supervisor.poll(active) is JobState.RUNNING  # author 1 -> review 1
        assert supervisor.poll(active) is JobState.RUNNING  # review 1 -> author 2
        assert supervisor.poll(active) is JobState.RUNNING  # author 2 -> review 2
        assert supervisor.poll(active) is JobState.SUCCEEDED
        assert store.get_protected_result(outcome.job_id).answer == "Corrected private synthesis."
        launches = [call for call in runner.calls if "--bg" in call[0]]
        assert [call[0][call[0].index("--model") + 1] for call in launches] == [
            "claude-fable-5", "claude-opus-5", "claude-fable-5", "claude-opus-5",
        ]
    finally:
        store.close()


def test_repeated_rework_candidate_parks_as_no_progress(tmp_path):
    runner = KnowledgeRunner(
        candidate_answers=["Same unsupported draft.", "Same unsupported draft."],
        review_verdicts=["rework"],
    )
    _now, store, _desk, outcome, _dispatcher, _runner, supervisor = setup_knowledge_supervisor(
        tmp_path, runner,
    )
    try:
        active = supervisor.start_next()
        assert supervisor.poll(active) is JobState.RUNNING
        assert supervisor.poll(active) is JobState.RUNNING
        assert supervisor.poll(active) is JobState.FAILED
        assert store.get(outcome.job_id).public_payload["code"] == "agentic_no_progress"
        assert supervisor._broker_servers == {}
    finally:
        store.close()


def test_missing_author_evidence_and_wrong_review_digest_fail_closed(tmp_path):
    first = tmp_path / "missing"
    first.mkdir()
    _now, store, _desk, outcome, _dispatcher, _runner, supervisor = setup_knowledge_supervisor(
        first, KnowledgeRunner(reads_per_stage=1),
    )
    try:
        active = supervisor.start_next()
        assert supervisor.poll(active) is JobState.FAILED
        assert store.get(outcome.job_id).public_payload["code"] == "agentic_result_invalid"
    finally:
        store.close()

    second = tmp_path / "digest"
    second.mkdir()
    _now, store, _desk, outcome, _dispatcher, _runner, supervisor = setup_knowledge_supervisor(
        second, KnowledgeRunner(wrong_review_digest=True),
    )
    try:
        active = supervisor.start_next()
        assert supervisor.poll(active) is JobState.RUNNING
        assert supervisor.poll(active) is JobState.FAILED
        assert store.get(outcome.job_id).public_payload["code"] == "agentic_result_invalid"
    finally:
        store.close()


def test_knowledge_cancellation_and_deadline_close_broker(tmp_path):
    cancel_root = tmp_path / "cancel"
    cancel_root.mkdir()
    _now, store, desk, outcome, _dispatcher, runner, supervisor = setup_knowledge_supervisor(
        cancel_root,
    )
    try:
        active = supervisor.start_next()
        desk.cancel(outcome.job_id)
        assert supervisor.poll(active) is JobState.CANCELLED
        assert supervisor._broker_servers == {}
        assert any(len(call[0]) > 1 and call[0][1] == "stop" for call in runner.calls)
    finally:
        store.close()


def test_restart_reconciliation_leaves_completed_review_for_expiry_recovery(tmp_path):
    now, store, _desk, outcome, _dispatcher, runner, first = setup_knowledge_supervisor(tmp_path)
    try:
        active = first.start_next()
        assert first.poll(active) is JobState.RUNNING
        current = first._active[outcome.job_id]
        assert current.stage == "agentic-review"
        review_short = current.session_id
        first._close_broker(outcome.job_id)  # process death drops its in-memory server
        second = SubscriptionSupervisor(
            store, first.transport, workdir=tmp_path,
            authorization=first.authorization, lease_seconds=30, clock=lambda: now[0],
            agentic_runtime=first.agentic_runtime,
        )
        assert second.reconcile_after_restart() == ()
        stops = [call[0][2] for call in runner.calls if len(call[0]) > 2 and call[0][1] == "stop"]
        assert review_short not in stops
        assert store.get(outcome.job_id).state is JobState.RUNNING
        now[0] = 131.0
        second.reconcile_after_restart()
        assert store.get(outcome.job_id).state is JobState.ORPHANED
    finally:
        store.close()


def test_knowledge_deadline_closes_broker(tmp_path):
    deadline_root = tmp_path / "deadline"
    deadline_root.mkdir()
    now, store, _desk, outcome, _dispatcher, runner, supervisor = setup_knowledge_supervisor(
        deadline_root,
    )
    try:
        active = supervisor.start_next()
        supervisor._active[outcome.job_id] = replace(active, deadline_at=101.0)
        now[0] = 102.0
        assert supervisor.poll(active) is JobState.FAILED
        assert store.get(outcome.job_id).public_payload["code"] == "agentic_deadline_exhausted"
        assert supervisor._broker_servers == {}
    finally:
        store.close()


def test_agentic_launch_uses_explicit_fable_roster_and_closed_read_only_surface(tmp_path):
    runner = FakeRunner()
    transport = ClaudeBackgroundTransport(runner, environment={"PATH": "safe"})
    session_id = "3f75564b-cad1-4b9e-9e79-4f15013b43c2"
    server, mcp = broker_mcp_config(tmp_path)
    try:
        short = transport.launch_agentic(
            session_id=session_id, name="atlas-knowledge", prompt="Research the bounded question.",
            cwd=tmp_path, spec=read_only_knowledge_launch("knowledge-heavy"), broker_mcp=mcp,
        )
    finally:
        server.close()
    argv = runner.calls[0][0]
    assert short == session_id[:8]
    assert "--safe-mode" not in argv and "--bare" not in argv and "--print" not in argv
    assert argv[argv.index("--model") + 1] == "claude-fable-5"
    assert argv[argv.index("--tools") + 1] == "Agent,mcp__atlas_knowledge__knowledge_read"
    assert argv[argv.index("--setting-sources") + 1] == "project"
    assert argv[argv.index("--disallowedTools") + 1] == (
        "Bash,Read,Glob,Grep,Write,Edit,NotebookEdit,WebFetch,WebSearch"
    )
    config_json = argv[argv.index("--mcp-config") + 1]
    assert "ATLAS_BROKER_TOKEN" not in config_json and "a" * 43 not in config_json
    assert runner.calls[0][2]["ATLAS_BROKER_TOKEN"] == "a" * 43
    agents = json.loads(argv[argv.index("--agents") + 1])
    assert agents["research-scout"]["model"] == "claude-haiku-4-5"
    assert agents["worker"]["model"] == "claude-sonnet-5"
    assert "reviewer" not in agents
    assert runner.calls[0][3] == 60.0


def test_agentic_launch_accepts_one_cli_assigned_background_id(tmp_path):
    runner = FakeRunner()
    runner.reported_background_id = "c1c6dec5"
    runner.background_stdout = "Session started in the background.\n"
    transport = ClaudeBackgroundTransport(runner, environment={"PATH": "safe"})
    assert transport.launch_agentic(
        session_id="3f75564b-cad1-4b9e-9e79-4f15013b43c2",
        name="atlas-standard", prompt="Complete the bounded task.", cwd=tmp_path,
        spec=standard_heavy_launch(),
    ) == "c1c6dec5"


def test_agentic_launch_rejects_ambiguous_background_ids(tmp_path):
    runner = FakeRunner()
    runner.reported_background_id = "c1c6dec5\nbackgrounded: deadbeef"
    transport = ClaudeBackgroundTransport(runner, environment={"PATH": "safe"})
    with pytest.raises(SupervisorError, match="invalid background session id"):
        transport.launch_agentic(
            session_id="3f75564b-cad1-4b9e-9e79-4f15013b43c2",
            name="atlas-standard", prompt="Complete the bounded task.", cwd=tmp_path,
            spec=standard_heavy_launch(),
        )


@pytest.mark.parametrize("name", ["CLAUDE.md", "CLAUDE.local.md", ".mcp.json", ".claude"])
def test_agentic_launch_rejects_workspace_customization_that_could_widen_authority(tmp_path, name):
    target = tmp_path / name
    target.mkdir() if name == ".claude" else target.write_text("untrusted", encoding="utf-8")
    transport = ClaudeBackgroundTransport(FakeRunner(), environment={"PATH": "safe"})
    server, mcp = broker_mcp_config(tmp_path.parent / f"mcp-{name.replace('.', '-')}")
    try:
        with pytest.raises(ValueError, match="ambient Claude customization"):
            transport.launch_agentic(
                session_id="3f75564b-cad1-4b9e-9e79-4f15013b43c2",
                name="atlas-knowledge", prompt="Research.", cwd=tmp_path,
                spec=read_only_knowledge_launch("knowledge-heavy"), broker_mcp=mcp,
            )
    finally:
        server.close()


def test_fresh_review_launch_is_host_selected_opus_without_agent_tool(tmp_path):
    runner = FakeRunner()
    transport = ClaudeBackgroundTransport(runner, environment={"PATH": "safe"})
    server, mcp = broker_mcp_config(tmp_path)
    try:
        transport.launch_review(
            session_id="3f75564b-cad1-4b9e-9e79-4f15013b43c2",
            name="atlas-review", prompt="Review candidate generation 2.", cwd=tmp_path,
            spec=read_only_review_launch(), broker_mcp=mcp,
        )
    finally:
        server.close()
    argv = runner.calls[0][0]
    assert argv[argv.index("--model") + 1] == "claude-opus-5"
    assert argv[argv.index("--tools") + 1] == "mcp__atlas_knowledge__knowledge_read"
    assert "Agent" in argv[argv.index("--disallowedTools") + 1].split(",")
    assert "--agents" not in argv and "--safe-mode" not in argv


def test_cancel_request_stops_exact_session_and_uses_host_claim(tmp_path):
    _now, store, desk, outcome, runner, supervisor = setup_supervisor(tmp_path)
    try:
        active = supervisor.start_next()
        desk.cancel(outcome.job_id)
        assert supervisor.poll(active) is JobState.CANCELLED
        stop_calls = [call for call in runner.calls if len(call[0]) > 1 and call[0][1] == "stop"]
        assert stop_calls[0][0][2] == active.session_id
    finally:
        store.close()


@pytest.mark.parametrize("state,code", [
    ("needs_input", "subscription_needs_input"),
    ("unrecognized-new-state", "subscription_status_unknown"),
])
def test_waiting_or_unknown_session_fails_closed_instead_of_renewing_forever(tmp_path, state, code):
    _now, store, _desk, outcome, runner, supervisor = setup_supervisor(tmp_path)
    try:
        active = supervisor.start_next()
        runner.state = state
        assert supervisor.poll(active) is JobState.FAILED
        assert store.get(outcome.job_id).public_payload["code"] == code
    finally:
        store.close()


def test_restart_stops_owned_session_and_only_expired_claim_is_orphaned(tmp_path):
    now, store, _desk, outcome, runner, supervisor = setup_supervisor(tmp_path)
    try:
        supervisor.start_next()
        assert supervisor.reconcile_after_restart() == (outcome.job_id,)
        assert store.get(outcome.job_id).state is JobState.RUNNING
        now[0] = 131.0
        assert supervisor.reconcile_after_restart() == ()
        assert store.get(outcome.job_id).state is JobState.ORPHANED
    finally:
        store.close()


def test_restart_discovers_and_stops_cli_assigned_background_session(tmp_path):
    now, store, _desk, outcome, runner, supervisor = setup_supervisor(tmp_path)
    runner.reported_background_id = "c1c6dec5"
    runner.background_stdout = "Session started in the background.\n"
    try:
        active = supervisor.start_next()
        assert active.session_id == "c1c6dec5"
        assert supervisor.reconcile_after_restart() == (outcome.job_id,)
        stop_ids = [
            call[0][2] for call in runner.calls
            if len(call[0]) > 2 and call[0][1] == "stop"
        ]
        assert "c1c6dec5" in stop_ids
    finally:
        store.close()


def test_result_parser_handles_terminal_redraws_but_rejects_plain_duplicates_and_unsafe_artifacts():
    job_id = "3f75564b-cad1-4b9e-9e79-4f15013b43c2"
    nonce = "nonce_value_that_is_long_enough"
    good = json.dumps({"job_id": job_id, "status": "succeeded", "summary": "done", "artifacts": []})
    assert parse_worker_result(f"ATLAS_RESULT_V1:{nonce}:{good}", nonce=nonce, job_id=job_id).status == "succeeded"
    template = json.dumps({
        "job_id": job_id, "status": "succeeded|failed|cancelled",
        "summary": "bounded factual summary", "error_code": None, "artifacts": [],
    })
    terminal_logs = (
        f"\x1b[38;2;255;255;255m  ATLAS_RESULT_V1:{nonce}:{template}\x1b[39m\n"
        f"  ATLAS_RESULT_V1:{nonce}:{good}\x1b[K\n"
        f"  ATLAS_RESULT_V1:{nonce}:{good}\x1b[K\n"
    )
    assert parse_worker_result(terminal_logs, nonce=nonce, job_id=job_id).summary == "done"
    with pytest.raises(SupervisorError):
        parse_worker_result(
            f"ATLAS_RESULT_V1:{nonce}:{good}\nATLAS_RESULT_V1:{nonce}:{good}",
            nonce=nonce, job_id=job_id,
        )
    conflicting = json.dumps({
        "job_id": job_id, "status": "failed", "summary": "different",
        "error_code": "worker_failed", "artifacts": [],
    })
    with pytest.raises(SupervisorError):
        parse_worker_result(
            f"\x1b[m  ATLAS_RESULT_V1:{nonce}:{good}\x1b[K\n"
            f"  ATLAS_RESULT_V1:{nonce}:{conflicting}\x1b[K",
            nonce=nonce, job_id=job_id,
        )
    unsafe = json.dumps({
        "job_id": job_id, "status": "succeeded", "summary": "done", "artifacts": ["../escape"],
    })
    with pytest.raises(SupervisorError):
        parse_worker_result(f"ATLAS_RESULT_V1:{nonce}:{unsafe}", nonce=nonce, job_id=job_id)


def test_environment_scrub_removes_provider_and_generic_secret_names():
    environment = {
        "PATH": "keep", "USERPROFILE": "keep", "SERVICE_PASSWORD": "drop",
        "CLAUDE_CODE_OAUTH_TOKEN": "drop", "AWS_PROFILE": "drop", "OPENAI_BASE_URL": "drop",
    }
    environment.update({key: "drop" for key in METERED_PROVIDER_ENV})
    scrubbed = scrub_subscription_environment(environment)
    assert scrubbed == {"PATH": "keep", "USERPROFILE": "keep"}


def test_local_command_runner_decodes_claude_output_as_utf8(monkeypatch, tmp_path):
    observed = {}

    class Completed:
        returncode = 0
        stdout = None
        stderr = ""

    def fake_run(*args, **kwargs):
        observed.update(kwargs)
        return Completed()

    monkeypatch.setattr("worker.subscription_supervisor.subprocess.run", fake_run)
    result = LocalCommandRunner().run(
        ("claude", "--version"), cwd=tmp_path, env={"PATH": "safe"}, timeout=15.0,
    )
    assert observed["encoding"] == "utf-8" and observed["errors"] == "replace"
    assert result.stdout == "" and result.stderr == ""


def test_only_one_paid_background_run_can_be_active(tmp_path):
    _now, store, desk, _outcome, runner, supervisor = setup_supervisor(tmp_path)
    try:
        first = supervisor.start_next()
        second_outcome = desk.submit(
            Request("document.compose", target="second", durable_artifact=True, artifact="second.md"),
            raw_utterance="Write the second local draft.",
        )
        assert supervisor.start_next() is None
        assert len([call for call in runner.calls if "--bg" in call[0]]) == 1
        runner.state = "completed"
        assert supervisor.poll(first) is JobState.SUCCEEDED
        assert supervisor.start_next() is None
        second = store.get(second_outcome.job_id)
        assert second.state is JobState.FAILED
        assert second.public_payload["code"] == "agentic_runtime_unavailable"
        assert len([call for call in runner.calls if "--bg" in call[0]]) == 1
    finally:
        store.close()


def test_connected_voice_job_uses_normal_claude_user_integrations_and_exact_utterance(tmp_path):
    _now, store, outcome, instruction, runner, supervisor, workspace_root = (
        setup_connected_supervisor(tmp_path)
    )
    try:
        active = supervisor.start_next()
        assert active.profile_id == "connected-cli" and active.stage == "connected"
        assert active.workspace == (workspace_root / outcome.job_id).resolve()
        launch = next(call for call in runner.calls if "--bg" in call[0])
        argv, cwd, environment, _timeout = launch
        assert cwd == active.workspace
        assert "--chrome" in argv and "--brief" in argv
        assert argv[argv.index("--setting-sources") + 1] == "user"
        assert argv[argv.index("--permission-mode") + 1] == "auto"
        assert argv[argv.index("--tools") + 1] == "default"
        assert "--safe-mode" not in argv
        assert "--no-chrome" not in argv
        assert "--strict-mcp-config" not in argv
        assert instruction in runner.prompt
        assert "ANTHROPIC_API_KEY" not in environment
        assert "GOOGLE_ACCESS_TOKEN" not in environment

        runner.state = "completed"
        assert supervisor.poll(active) is JobState.SUCCEEDED
        completed = store.get(outcome.job_id)
        assert completed.public_payload["summary"] == "Verified local result."
    finally:
        store.close()


def test_connected_job_fails_honestly_when_connected_runtime_is_not_configured(tmp_path):
    now = [100.0]
    store = JobStore(tmp_path / "missing-connected.sqlite", payload_codec=FakePayloadCodec(),
                     clock=lambda: now[0])
    try:
        outcome = FrontDesk(
            store=store,
            worker_health=WorkerHealth(
                WorkerHealthStatus.AVAILABLE, worker_id="subscription-test", checked_at=now[0]),
            clock=lambda: now[0],
        ).submit(
            Request("claude.connected", target="connected-cli", steps=2),
            raw_utterance="Open Drive.",
        )
        runner = FakeRunner()
        supervisor = SubscriptionSupervisor(
            store, ClaudeBackgroundTransport(runner, environment={"PATH": "safe"}),
            workdir=tmp_path,
            authorization=SubscriptionAuthorization(
                "claude-subscription", checked_at=now[0], api_environment_absent=True,
                human_confirmed=True,
            ),
            lease_seconds=30, clock=lambda: now[0],
        )
        assert supervisor.start_next() is None
        failed = store.get(outcome.job_id)
        assert failed.state is JobState.FAILED
        assert failed.public_payload["code"] == "connected_runtime_unavailable"
        assert runner.calls == []
    finally:
        store.close()


def test_process_lifetime_authorization_stays_valid_but_unconfirmed_blocks(tmp_path):
    now, store, _desk, outcome, runner, supervisor = setup_supervisor(tmp_path)
    try:
        now[0] = 401.0
        assert supervisor.start_next() is not None
        assert store.get(outcome.job_id).state is JobState.RUNNING
    finally:
        store.close()

    unconfirmed_root = tmp_path / "unconfirmed"
    unconfirmed_root.mkdir()
    _now, store, _desk, outcome, runner, supervisor = setup_supervisor(unconfirmed_root)
    try:
        supervisor.authorization = replace(supervisor.authorization, human_confirmed=False)
        with pytest.raises(SupervisorError):
            supervisor.start_next()
        assert store.get(outcome.job_id).state is JobState.QUEUED
        assert runner.calls == []
    finally:
        store.close()
