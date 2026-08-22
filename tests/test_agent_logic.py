import json

import pytest

from worker.agent_logic import (
    COORDINATOR_TOOLS,
    READ_ONLY_DENIED_TOOLS,
    REVIEW_DENIED_TOOLS,
    coordinator_agents_json,
    coordinator_doctrine,
    draft_build_launch,
    default_agent_roster,
    independent_reviewer,
    read_only_knowledge_launch,
    read_only_review_launch,
    standard_heavy_launch,
)


def test_fable_inner_loop_roster_uses_verified_lower_models_and_no_reviewer():
    roster = default_agent_roster()
    assert set(roster) == {"research-scout", "worker"}
    assert roster["research-scout"].model == "claude-haiku-4-5"
    assert roster["worker"].model == "claude-sonnet-5"
    assert "reviewer" not in roster
    assert COORDINATOR_TOOLS == ("Agent", "mcp__atlas_knowledge__knowledge_read")


def test_initial_agent_roster_is_read_only_until_workspace_confinement_exists():
    forbidden = {"Write", "Edit", "Bash", "NotebookEdit"}
    for agent in default_agent_roster().values():
        assert forbidden.isdisjoint(agent.tools)
    assert forbidden.isdisjoint(independent_reviewer().tools)
    assert {"Read", "Glob", "Grep"}.isdisjoint(COORDINATOR_TOOLS)


def test_independent_reviewer_is_host_launched_opus_and_cannot_repair_subject():
    reviewer = independent_reviewer()
    assert reviewer.model == "claude-opus-5"
    assert "Remain\nread-only" in reviewer.prompt
    assert "changed\nartifact invalidates" in reviewer.prompt


def test_shared_doctrine_preserves_kb_loop_and_file_editing_invariants():
    prompt = default_agent_roster()["worker"].prompt
    assert "bounded observe-plan-delegate-build-check loop" in prompt
    assert "smallest\ncoherent" in prompt
    assert "Never delete, weaken, skip" in prompt
    assert "author never issues the independent review verdict" in prompt


def test_cli_agent_json_is_deterministic_and_contains_no_kb_runtime_dependency():
    encoded = coordinator_agents_json()
    decoded = json.loads(encoded)
    assert list(decoded) == ["research-scout", "worker"]
    assert "C:\\Users\\danie\\kb" not in encoded
    assert "orgs/" not in encoded and "HEARTBEAT" not in encoded
    assert encoded == coordinator_agents_json()


def test_coordinator_profile_overlay_cannot_create_a_cheap_profile():
    doctrine = coordinator_doctrine("knowledge-build-review")
    assert "required evidence" in doctrine
    assert "deterministic checks" in doctrine
    with pytest.raises(ValueError, match="unknown coordinator profile"):
        coordinator_doctrine("cheap-fast-cycle")


def test_read_only_knowledge_launch_is_fable_led_and_excludes_ambient_settings():
    spec = read_only_knowledge_launch("knowledge-heavy")
    assert spec.model == "claude-fable-5"
    assert spec.tools == COORDINATOR_TOOLS
    assert spec.denied_tools == READ_ONLY_DENIED_TOOLS
    assert spec.setting_sources == ("project",)
    assert set(json.loads(spec.agents_json)) == {"research-scout", "worker"}
    assert "evidence-backed synthesis" in spec.system_prompt


def test_combined_slice_returns_private_artifact_without_claiming_external_file_mutation():
    spec = read_only_knowledge_launch("knowledge-build-review")
    assert "complete private deliverable" in spec.system_prompt
    assert "No external file mutation" in spec.system_prompt
    with pytest.raises(ValueError, match="requires a knowledge profile"):
        read_only_knowledge_launch("standard-heavy")


def test_host_review_launch_is_fresh_opus_without_delegation():
    spec = read_only_review_launch()
    assert spec.model == "claude-opus-5"
    assert "Agent" in REVIEW_DENIED_TOOLS
    assert spec.denied_tools == REVIEW_DENIED_TOOLS
    assert "Remain\nread-only" in spec.system_prompt


def test_build_launch_uses_fable_delegation_but_no_source_or_file_tools():
    spec = draft_build_launch()
    assert spec.profile_id == "build-review"
    assert spec.tools == ("Agent",)
    agents = json.loads(spec.agents_json)
    assert agents["research-scout"]["tools"] == []
    assert agents["worker"]["tools"] == []
    review = read_only_review_launch("build-review")
    assert review.tools == ()


def test_standard_heavy_uses_the_same_fable_delegation_envelope_without_forced_review():
    spec = standard_heavy_launch()
    assert spec.profile_id == "standard-heavy"
    assert spec.model == "claude-fable-5"
    assert spec.tools == ("Agent",)
    agents = json.loads(spec.agents_json)
    assert agents["research-scout"]["model"] == "claude-haiku-4-5"
    assert agents["worker"]["model"] == "claude-sonnet-5"
    assert all(agent["tools"] == [] for agent in agents.values())
    assert "when useful" in spec.system_prompt
