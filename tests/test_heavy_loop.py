from dataclasses import replace
import json

import pytest

from worker.contracts import Request
from worker.heavy_loop import (
    CapabilityObservation,
    DirectiveKind,
    HeavyLoopController,
    LoopDirective,
    LoopError,
    LoopStatus,
    RolePolicy,
    default_execution_profiles,
    parse_loop_directive,
    render_directive_prompt,
    render_role_system_prompt,
    select_execution_profile,
)


JOB_ID = "3f75564b-cad1-4b9e-9e79-4f15013b43c2"


class RecordingDispatcher:
    def __init__(self, observations=None):
        self.calls = []
        self.observations = list(observations or [])

    def dispatch(self, *, role, capability_id, parameters, idempotency_key):
        self.calls.append((role, capability_id, dict(parameters), idempotency_key))
        if self.observations:
            return self.observations.pop(0)
        return CapabilityObservation("succeeded", "ok", evidence_count=1,
                                     progress_token=f"result-{len(self.calls)}")


def directive(state, kind, **kwargs):
    return LoopDirective(
        job_id=state.job_id,
        role=state.current_role,
        step=state.model_turns + 1,
        state_hash=state.state_hash,
        nonce=state.turn_nonce,
        kind=kind,
        **kwargs,
    )


def test_standard_heavy_can_complete_in_one_fable_turn():
    profiles = default_execution_profiles()
    controller = HeavyLoopController(profiles, RecordingDispatcher(), nonce_factory=lambda: "n" * 32)
    state = controller.start(JOB_ID, "standard-heavy", "Give a bounded answer.")

    assert state.current_role == "coordinator"
    assert state.current_model == "claude-fable-5"
    outcome = controller.advance(state, directive(state, DirectiveKind.COMPLETE, summary="Answer."))

    assert outcome.status is LoopStatus.SUCCEEDED
    assert outcome.state.model_turns == 1


def test_knowledge_heavy_cannot_shortcut_evidence_or_independent_review():
    controller = HeavyLoopController(default_execution_profiles(), RecordingDispatcher(),
                                     nonce_factory=lambda: "n" * 32)
    state = controller.start(JOB_ID, "knowledge-heavy", "Research and synthesize the topic.")

    first = controller.advance(state, directive(state, DirectiveKind.COMPLETE, summary="Looks done."))
    assert first.status is LoopStatus.RUNNABLE
    assert first.state.observations[-1].code == "evidence_gate_failed"

    second = controller.advance(
        first.state,
        directive(first.state, DirectiveKind.CALL, capability_id="agent.dispatch",
                  parameters={"role": "reviewer", "task": "Review the unsupported answer."}),
    )
    assert second.status is LoopStatus.RUNNABLE
    assert second.state.current_role == "reviewer"
    assert second.state.current_model == "claude-opus-5"

    reviewed = controller.advance(
        second.state,
        directive(second.state, DirectiveKind.COMPLETE, summary="It lacks sources.",
                  result={"verdict": "rework", "findings": ["missing evidence"]}),
    )
    assert reviewed.status is LoopStatus.RUNNABLE
    assert reviewed.state.current_role == "coordinator"
    assert reviewed.state.review_passed is False


def test_role_delegation_resolves_verified_model_in_host_policy():
    controller = HeavyLoopController(default_execution_profiles(), RecordingDispatcher(),
                                     nonce_factory=lambda: "n" * 32)
    state = controller.start(JOB_ID, "knowledge-heavy", "Research a topic.")
    delegated = controller.advance(
        state,
        directive(state, DirectiveKind.CALL, capability_id="agent.dispatch",
                  parameters={"role": "research-scout", "task": "Find two primary sources."}),
    )

    assert delegated.state.current_role == "research-scout"
    assert delegated.state.current_model == "claude-haiku-4-5"
    assert delegated.state.delegations == 1

    first_read = controller.advance(
        delegated.state,
        directive(delegated.state, DirectiveKind.CALL, capability_id="knowledge.read",
                  parameters={"source": "primary-1"}),
    )
    second_read = controller.advance(
        first_read.state,
        directive(first_read.state, DirectiveKind.CALL, capability_id="knowledge.read",
                  parameters={"source": "primary-2"}),
    )
    returned = controller.advance(
        second_read.state,
        directive(second_read.state, DirectiveKind.COMPLETE, summary="Two sources found."),
    )
    assert returned.state.current_role == "coordinator"
    assert returned.state.evidence_count == 2
    assert returned.state.observations[-1].code == "delegate_completed"


def test_model_cannot_name_or_downgrade_a_delegated_model():
    controller = HeavyLoopController(default_execution_profiles(), RecordingDispatcher(),
                                     nonce_factory=lambda: "n" * 32)
    state = controller.start(JOB_ID, "knowledge-heavy", "Research a topic.")
    spoof = directive(
        state, DirectiveKind.CALL, capability_id="agent.dispatch",
        parameters={"role": "research-scout", "task": "Do it cheaply.", "model": "haiku"},
    )
    with pytest.raises(LoopError, match="delegation parameters"):
        controller.advance(state, spoof)

    with pytest.raises(ValueError, match="verified host registry"):
        RolePolicy("cheap-worker", "unverified-cheap-model", frozenset({"knowledge.read"}))


def test_knowledge_review_is_bound_to_the_exact_evidence_and_candidate_generation():
    controller = HeavyLoopController(default_execution_profiles(), RecordingDispatcher(),
                                     nonce_factory=lambda: "n" * 32)
    state = controller.start(JOB_ID, "knowledge-heavy", "Research and answer with evidence.")
    scout = controller.advance(
        state, directive(state, DirectiveKind.CALL, capability_id="agent.dispatch",
                         parameters={"role": "research-scout", "task": "Find two sources."}))
    first = controller.advance(
        scout.state, directive(scout.state, DirectiveKind.CALL,
                               capability_id="knowledge.read", parameters={"source": "one"}))
    second = controller.advance(
        first.state, directive(first.state, DirectiveKind.CALL,
                               capability_id="knowledge.read", parameters={"source": "two"}))
    returned = controller.advance(
        second.state, directive(second.state, DirectiveKind.COMPLETE, summary="Sources ready."))

    staged = controller.advance(
        returned.state, directive(returned.state, DirectiveKind.COMPLETE,
                                  summary="Evidence-backed candidate."))
    assert staged.status is LoopStatus.RUNNABLE
    assert staged.state.observations[-1].code == "independent_review_required"
    assert staged.state.candidate_generation == 1

    review = controller.advance(
        staged.state, directive(staged.state, DirectiveKind.CALL,
                                capability_id="agent.dispatch",
                                parameters={"role": "reviewer", "task": "Review candidate 1."}))
    passed = controller.advance(
        review.state, directive(review.state, DirectiveKind.COMPLETE, summary="Supported.",
                                result={"verdict": "pass", "findings": []}))
    finished = controller.advance(
        passed.state, directive(passed.state, DirectiveKind.COMPLETE,
                                summary="Evidence-backed candidate."))
    assert finished.status is LoopStatus.SUCCEEDED

    # A changed answer is a new candidate and cannot inherit the prior review verdict.
    changed = controller.advance(
        passed.state, directive(passed.state, DirectiveKind.COMPLETE,
                                summary="Materially changed candidate."))
    assert changed.status is LoopStatus.RUNNABLE
    assert changed.state.candidate_generation == 2
    assert changed.state.observations[-1].code == "independent_review_required"


def test_build_profile_requires_fresh_reviewer_and_reviewer_cannot_edit():
    dispatcher = RecordingDispatcher()
    controller = HeavyLoopController(default_execution_profiles(), dispatcher,
                                     nonce_factory=lambda: "n" * 32)
    state = controller.start(JOB_ID, "build-review", "Edit the scoped artifact and verify it.")
    premature = controller.advance(state, directive(state, DirectiveKind.COMPLETE, summary="Done."))
    assert premature.status is LoopStatus.RUNNABLE
    assert premature.state.observations[-1].code == "artifact_change_required"

    worker = controller.advance(
        premature.state,
        directive(premature.state, DirectiveKind.CALL, capability_id="agent.dispatch",
                  parameters={"role": "worker", "task": "Make the scoped edit."}),
    )
    edited = controller.advance(
        worker.state,
        directive(worker.state, DirectiveKind.CALL, capability_id="artifact.patch",
                  parameters={"path": "draft.md", "patch": "change"}),
    )
    returned = controller.advance(
        edited.state,
        directive(edited.state, DirectiveKind.COMPLETE, summary="Scoped edit complete."),
    )

    review = controller.advance(
        returned.state,
        directive(returned.state, DirectiveKind.CALL, capability_id="agent.dispatch",
                  parameters={"role": "reviewer", "task": "Review the exact artifact generation."}),
    )
    with pytest.raises(LoopError, match="not allowed"):
        controller.advance(
            review.state,
            directive(review.state, DirectiveKind.CALL, capability_id="artifact.patch",
                      parameters={"path": "draft.md", "patch": "change"}),
        )

    verified = controller.advance(
        review.state,
        directive(review.state, DirectiveKind.CALL, capability_id="verify.run",
                  parameters={"check": "declared-tests"}),
    )
    passed = controller.advance(
        verified.state,
        directive(verified.state, DirectiveKind.COMPLETE, summary="Checks pass.",
                  result={"verdict": "pass", "findings": []}),
    )
    finished = controller.advance(
        passed.state,
        directive(passed.state, DirectiveKind.COMPLETE, summary="Reviewed draft ready."),
    )
    assert finished.status is LoopStatus.SUCCEEDED


def test_edit_after_review_invalidates_the_exact_generation_verdict():
    controller = HeavyLoopController(default_execution_profiles(), RecordingDispatcher(),
                                     nonce_factory=lambda: "n" * 32)
    state = controller.start(JOB_ID, "build-review", "Build and review a draft.")
    worker = controller.advance(
        state, directive(state, DirectiveKind.CALL, capability_id="agent.dispatch",
                         parameters={"role": "worker", "task": "Create the draft."}))
    edited = controller.advance(
        worker.state, directive(worker.state, DirectiveKind.CALL,
                                capability_id="artifact.create",
                                parameters={"path": "draft.md", "content": "v1"}))
    returned = controller.advance(
        edited.state, directive(edited.state, DirectiveKind.COMPLETE, summary="v1 ready"))
    review = controller.advance(
        returned.state, directive(returned.state, DirectiveKind.CALL,
                                  capability_id="agent.dispatch",
                                  parameters={"role": "reviewer", "task": "Review v1."}))
    verified = controller.advance(
        review.state, directive(review.state, DirectiveKind.CALL,
                                capability_id="verify.run",
                                parameters={"check": "declared-tests"}))
    passed = controller.advance(
        verified.state, directive(verified.state, DirectiveKind.COMPLETE, summary="v1 passes",
                                result={"verdict": "pass", "findings": []}))
    assert passed.state.reviewed_generation == 1

    repair = controller.advance(
        passed.state, directive(passed.state, DirectiveKind.CALL,
                                capability_id="agent.dispatch",
                                parameters={"role": "worker", "task": "Change v1 to v2."}))
    v2 = controller.advance(
        repair.state, directive(repair.state, DirectiveKind.CALL,
                                capability_id="artifact.patch",
                                parameters={"path": "draft.md", "patch": "v2"}))
    back = controller.advance(
        v2.state, directive(v2.state, DirectiveKind.COMPLETE, summary="v2 ready"))
    finish = controller.advance(
        back.state, directive(back.state, DirectiveKind.COMPLETE, summary="Done."))

    assert back.state.artifact_generation == 2
    assert back.state.review_passed is False
    assert back.state.verified_generation is None
    assert finish.status is LoopStatus.RUNNABLE
    assert finish.state.observations[-1].code == "deterministic_verification_required"


def test_standard_profile_cannot_use_delegation_to_bypass_edit_verification_and_review():
    controller = HeavyLoopController(default_execution_profiles(), RecordingDispatcher(),
                                     nonce_factory=lambda: "n" * 32)
    state = controller.start(JOB_ID, "standard-heavy", "Make a small coherent edit.")
    worker = controller.advance(
        state, directive(state, DirectiveKind.CALL, capability_id="agent.dispatch",
                         parameters={"role": "worker", "task": "Edit only draft.md."}))
    edited = controller.advance(
        worker.state, directive(worker.state, DirectiveKind.CALL,
                                capability_id="artifact.patch",
                                parameters={"path": "draft.md", "patch": "change"}))
    returned = controller.advance(
        edited.state, directive(edited.state, DirectiveKind.COMPLETE, summary="Edit made."))
    shortcut = controller.advance(
        returned.state, directive(returned.state, DirectiveKind.COMPLETE, summary="Done."))

    assert shortcut.status is LoopStatus.RUNNABLE
    assert shortcut.state.observations[-1].code == "deterministic_verification_required"


def test_repeated_identical_call_and_observation_parks_for_no_progress():
    repeated = CapabilityObservation("succeeded", "read", evidence_count=0,
                                     progress_token="same-generation")
    dispatcher = RecordingDispatcher([repeated, repeated, repeated])
    controller = HeavyLoopController(default_execution_profiles(), dispatcher,
                                     nonce_factory=lambda: "n" * 32)
    state = controller.start(JOB_ID, "standard-heavy", "Inspect until useful evidence exists.")
    first = controller.advance(
        state, directive(state, DirectiveKind.CALL, capability_id="knowledge.read",
                         parameters={"source": "source-1"}))
    second = controller.advance(
        first.state, directive(first.state, DirectiveKind.CALL, capability_id="knowledge.read",
                               parameters={"source": "source-1"}))
    third = controller.advance(
        second.state, directive(second.state, DirectiveKind.CALL, capability_id="knowledge.read",
                                parameters={"source": "source-1"}))

    assert third.status is LoopStatus.READY_FOR_REVIEW
    assert third.state.no_progress_count == 2
    assert len(dispatcher.calls) == 3


def test_stale_or_wrong_role_directive_is_rejected_before_dispatch():
    dispatcher = RecordingDispatcher()
    controller = HeavyLoopController(default_execution_profiles(), dispatcher,
                                     nonce_factory=lambda: "n" * 32)
    state = controller.start(JOB_ID, "standard-heavy", "Read one item.")
    stale = LoopDirective(
        job_id=state.job_id, role="coordinator", step=1, state_hash="0" * 64,
        nonce=state.turn_nonce, kind=DirectiveKind.CALL,
        capability_id="knowledge.read", parameters={"source": "source-1"},
    )
    with pytest.raises(LoopError, match="state correlation"):
        controller.advance(state, stale)
    assert dispatcher.calls == []


def test_ask_user_pauses_without_consuming_a_capability_call():
    controller = HeavyLoopController(default_execution_profiles(), RecordingDispatcher(),
                                     nonce_factory=lambda: "n" * 32)
    state = controller.start(JOB_ID, "standard-heavy", "Work from an ambiguous request.")
    outcome = controller.advance(
        state, directive(state, DirectiveKind.ASK_USER,
                         question="Which document should I use?"))
    assert outcome.status is LoopStatus.WAITING_USER
    assert outcome.state.capability_calls == 0


def test_unknown_profile_role_and_capability_fail_closed():
    controller = HeavyLoopController(default_execution_profiles(), RecordingDispatcher(),
                                     nonce_factory=lambda: "n" * 32)
    with pytest.raises(LoopError, match="unknown execution profile"):
        controller.start(JOB_ID, "cheap-fast-cycle", "Do too little.")

    state = controller.start(JOB_ID, "standard-heavy", "Do bounded work.")
    with pytest.raises(LoopError, match="unknown delegated role"):
        controller.advance(
            state,
            directive(state, DirectiveKind.CALL, capability_id="agent.dispatch",
                      parameters={"role": "unverified-model", "task": "Do work."}),
        )
    with pytest.raises(LoopError, match="not allowed"):
        controller.advance(
            state,
            directive(state, DirectiveKind.CALL, capability_id="shell.execute",
                      parameters={"command": "whoami"}),
        )


def test_role_prompts_compile_shared_doctrine_without_model_selected_authority():
    controller = HeavyLoopController(default_execution_profiles(), RecordingDispatcher(),
                                     nonce_factory=lambda: "n" * 32)
    state = controller.start(JOB_ID, "build-review", "private instruction must remain user data")
    coordinator_prompt = render_role_system_prompt(state)
    assert "claude-fable-5" not in coordinator_prompt
    assert "private instruction" not in coordinator_prompt
    assert "Never name a model" in coordinator_prompt
    assert "never permits skipping stages" in coordinator_prompt

    worker = controller.advance(
        state, directive(state, DirectiveKind.CALL, capability_id="agent.dispatch",
                         parameters={"role": "worker", "task": "Edit the scoped file."}))
    worker_prompt = render_role_system_prompt(worker.state)
    assert "inspect the named target" in worker_prompt
    assert "never delete, weaken, skip, or bypass" in worker_prompt
    assert "never issue the independent review verdict" in worker_prompt

    # A separate fresh controller makes the reviewer branch independent of the worker branch.
    fresh = controller.start(JOB_ID, "build-review", "review the exact generation")
    review = controller.advance(
        fresh, directive(fresh, DirectiveKind.CALL, capability_id="agent.dispatch",
                         parameters={"role": "reviewer", "task": "Review only."}))
    review_prompt = render_role_system_prompt(review.state)
    assert "Remain read-only" in review_prompt
    assert "convert missing evidence into a pass" in review_prompt


def test_host_profile_selection_cannot_fast_cycle_heavy_request_features():
    assert select_execution_profile(Request("answer.compose")) == "standard-heavy"
    assert select_execution_profile(Request("research.synthesize")) == "knowledge-heavy"
    assert select_execution_profile(Request("document.compose", durable_artifact=True)) == "build-review"
    assert select_execution_profile(
        Request("answer.compose"),
        raw_utterance="Launch a knowledge-heavy workflow and compare sources.",
    ) == "knowledge-heavy"
    assert select_execution_profile(
        Request("document.compose", research=True, durable_artifact=True),
    ) == "knowledge-build-review"
    assert select_execution_profile(
        Request("answer.compose"),
        raw_utterance="Research the issue, then build and verify the document.",
    ) == "knowledge-build-review"
    assert select_execution_profile(
        Request("answer.compose"), named_profile="knowledge-heavy") == "knowledge-heavy"
    assert select_execution_profile(
        Request("research.synthesize"), named_profile="standard-heavy") == "knowledge-heavy"
    assert select_execution_profile(
        Request("document.compose"), named_profile="knowledge-heavy",
    ) == "knowledge-build-review"
    with pytest.raises(LoopError, match="unknown named execution profile"):
        select_execution_profile(Request("answer.compose"), named_profile="cheap-fast-cycle")

    with pytest.raises(ValueError, match="cannot contribute evidence"):
        CapabilityObservation("failed", "source_failed", evidence_count=1)


def test_wall_time_and_aggregate_frame_budgets_park_instead_of_spinning():
    base = default_execution_profiles()["standard-heavy"]
    timed_profile = replace(base, max_wall_seconds=10)
    ticks = iter((100.0, 111.0))
    timed = HeavyLoopController(
        {"standard-heavy": timed_profile}, RecordingDispatcher(),
        nonce_factory=lambda: "n" * 32, clock=lambda: next(ticks),
    )
    timed_state = timed.start(JOB_ID, "standard-heavy", "Do bounded work.")
    expired = timed.advance(
        timed_state, directive(timed_state, DirectiveKind.COMPLETE, summary="Done."))
    assert expired.status is LoopStatus.READY_FOR_REVIEW
    assert expired.code == "wall_time_budget_exhausted"

    byte_profile = replace(base, max_total_frame_bytes=16_384)
    dispatcher = RecordingDispatcher()
    bounded = HeavyLoopController(
        {"standard-heavy": byte_profile}, dispatcher,
        nonce_factory=lambda: "n" * 32, clock=lambda: 100.0,
    )
    byte_state = bounded.start(JOB_ID, "standard-heavy", "Read bounded material.")
    first = bounded.advance(
        byte_state, directive(byte_state, DirectiveKind.CALL,
                              capability_id="knowledge.read",
                              parameters={"source": "a" * 8_000}))
    exhausted = bounded.advance(
        first.state, directive(first.state, DirectiveKind.CALL,
                               capability_id="knowledge.read",
                               parameters={"source": "b" * 8_000}))
    assert exhausted.status is LoopStatus.READY_FOR_REVIEW
    assert exhausted.code == "frame_byte_budget_exhausted"
    assert len(dispatcher.calls) == 1


def test_directive_transport_is_single_frame_bounded_and_state_correlated():
    controller = HeavyLoopController(default_execution_profiles(), RecordingDispatcher(),
                                     nonce_factory=lambda: "n" * 32, clock=lambda: 100.0)
    state = controller.start(JOB_ID, "standard-heavy", "Private task.")
    prompt = render_directive_prompt(state, "Private task.")
    assert "claude-fable-5" not in prompt
    assert "ATLAS_DIRECTIVE_V1:" in prompt
    assert state.state_hash in prompt

    raw = {
        "job_id": state.job_id, "role": state.current_role, "step": 1,
        "state_hash": state.state_hash, "nonce": state.turn_nonce,
        "kind": "complete", "summary": "Bounded answer.",
        "parameters": {}, "result": {}, "capability_id": None,
        "question": None, "error_code": None,
    }
    frame = f"ATLAS_DIRECTIVE_V1:{state.turn_nonce}:{json.dumps(raw)}"
    parsed = parse_loop_directive(f"log line\n{frame}\n", state)
    assert parsed.kind is DirectiveKind.COMPLETE
    assert parsed.summary == "Bounded answer."

    with pytest.raises(LoopError, match="missing or ambiguous"):
        parse_loop_directive(f"{frame}\n{frame}", state)
    raw["unexpected"] = "field"
    bad = f"ATLAS_DIRECTIVE_V1:{state.turn_nonce}:{json.dumps(raw)}"
    with pytest.raises(LoopError, match="schema"):
        parse_loop_directive(bad, state)
