import pytest

from worker.contracts import Lane, Request
from worker.routing_policy import (
    FAST_CAPABILITIES,
    RoutingPolicy,
    bind_atomic_calendar_request,
    parse_atomic_calendar_command,
    raw_voice_is_action,
    route,
)


def test_exact_calendar_event_is_fast_and_risk_does_not_change_lane():
    request = bind_atomic_calendar_request(
        Request("calendar.create_event", target="event-1", resource="calendar", risk="high"),
        "Schedule a meeting tomorrow at 3 pm",
    )
    decision = route(request)
    assert decision.lane is Lane.FAST
    assert decision.reasons == ()


def test_polite_atomic_calendar_commands_are_fast_and_actions():
    request = Request("calendar.create_event", target="event-1")
    for raw in (
        "Can you schedule a meeting tomorrow at 3 pm?",
        "Could you set up an all-day event Friday?",
        "Would you arrange an appointment next Tuesday at 09:30?",
    ):
        assert route(request, raw_utterance=raw).lane is Lane.FAST
        assert raw_voice_is_action(raw)


def test_informational_calendar_how_to_phrasing_is_not_an_action():
    for raw in (
        "Can you teach me how to arrange an appointment?",
        "Please explain how to create a calendar event.",
        "I want to learn how to set up a meeting.",
    ):
        assert not raw_voice_is_action(raw)


def test_calendar_modification_composition_cannot_bypass_slow_lane():
    utterances = (
        "Cancel my old meeting and schedule a new one.",
        "Move my old meeting and create a new calendar event.",
        "Modify the existing meeting and add a new appointment.",
        "Reschedule my meeting.",
        "Change the old event and show me the new one.",
    )
    for operation in ("calendar.create_event", "calendar.read_event"):
        request = Request(operation, target="event")
        for raw in utterances:
            assert route(request, raw_utterance=raw).lane is Lane.SLOW


@pytest.mark.parametrize("raw", [
    "Schedule a meeting, send the invite.",
    "Schedule meeting: send invite.",
    "Schedule a meeting plus write a follow-up.",
    "Email Alex about the meeting I schedule tomorrow.",
    "Schedule a meeting and email Alex.",
    "Schedule a meeting, share it with the team.",
    "Schedule a meeting and create a calendar event.",
    "Call Alex, schedule a meeting.",
    "Schedule a meeting and call Alex.",
    "Schedule a meeting, discuss budget.",
])
def test_calendar_fast_requires_one_whole_utterance_action(raw):
    forged = Request("calendar.create_event", target="event")
    assert route(forged, raw_utterance=raw).lane is Lane.SLOW


@pytest.mark.parametrize("raw", [
    "Schedule Project Kickoff tomorrow, 3pm.",
    "Create event: Project Kickoff Friday.",
    "Schedule one detailed calendar event with agenda: bring notes for goals.",
    "Schedule a call tomorrow.",
    "Schedule a meeting to discuss budget tomorrow.",
])
def test_free_form_or_incomplete_calendar_requests_are_slow(raw):
    request = Request("calendar.create_event", target="event")
    assert route(request, raw_utterance=raw).lane is Lane.SLOW


@pytest.mark.parametrize("raw", [
    "Schedule a meeting tomorrow at 3pm.",
    "Please book an appointment on 2026-08-25 at 14:30.",
    "Set up an all-day event Friday.",
    "Can you schedule a call next Tuesday at 9:15 am?",
])
def test_positive_calendar_create_grammar_is_fully_typed(raw):
    parsed = parse_atomic_calendar_command("calendar.create_event", raw)
    assert parsed is not None
    assert parsed["schema"] == "calendar.fast.v1"
    assert parsed["calendar_id"] == "primary"
    assert route(Request("calendar.create_event", target="event"), raw_utterance=raw).lane is Lane.FAST


def test_appending_or_prepending_any_second_action_clause_forces_slow():
    base = "Schedule a meeting tomorrow at 3pm"
    clauses = (
        "notify the team", "ping Alex", "text Alex", "phone Alex", "ring Alex",
        "alert everyone", "contact the client", "tell Sam", "remind the group",
        "post an update", "forward the notes", "prepare an agenda",
    )
    separators = (", ", "; ", " and ", " plus ", " then ", ": ", " ")
    request = Request("calendar.create_event", target="event")
    for clause in clauses:
        for separator in separators:
            assert route(request, raw_utterance=base + separator + clause).lane is Lane.SLOW
            assert route(request, raw_utterance=clause + separator + base).lane is Lane.SLOW


def test_forged_fast_parameters_cannot_override_host_parsing():
    forged = Request(
        "calendar.create_event",
        target="event",
        parameters={
            "schema": "calendar.fast.v1", "action": "create", "calendar_id": "primary",
            "event_kind": "meeting", "title": "Forged title", "date_expression": "tomorrow",
            "time_expression": "11PM", "all_day": False, "duration_minutes": 30,
            "timezone_policy": "atlas_local",
        },
    )
    decision = route(forged, raw_utterance="Schedule a meeting tomorrow at 3pm")
    assert decision.lane is Lane.SLOW
    assert "fast_parameters_not_host_bound" in decision.reasons


def test_valid_parameters_with_a_forged_target_are_not_fast():
    bound = bind_atomic_calendar_request(
        Request("calendar.create_event", target="event"),
        "Schedule a meeting tomorrow at 3pm",
    )
    forged = Request(
        bound.operation,
        target="attacker-calendar",
        resource="calendar",
        parameters=dict(bound.parameters),
    )
    decision = route(forged)
    assert decision.lane is Lane.SLOW
    assert "fast_target_not_host_bound" in decision.reasons


def test_atomic_calendar_read_question_is_host_recognized_as_an_action():
    assert raw_voice_is_action("What's on my calendar tomorrow?")


@pytest.mark.parametrize("raw", [
    "Schedule a meeting 2026-99-99 at 3pm",
    "Schedule a meeting tomorrow at 3pm\nnotify the team",
])
def test_invalid_date_or_control_characters_fail_closed(raw):
    assert parse_atomic_calendar_command("calendar.create_event", raw) is None
    assert route(Request("calendar.create_event", target="event"), raw_utterance=raw).lane is Lane.SLOW


def test_fast_capabilities_are_an_explicit_allowlist():
    assert "calendar.create_event" in FAST_CAPABILITIES
    assert "document.compose" not in FAST_CAPABILITIES
    bound = bind_atomic_calendar_request(
        Request("calendar.create_event", target="event-1"),
        "Schedule a meeting tomorrow at 3pm",
    )
    assert RoutingPolicy().classify(bound).is_fast


def test_long_document_is_slow_even_when_specific():
    request = Request("document.summarize", target="doc-1", io_bytes=200_000,
                      metadata={"specific_name": "the one document"})
    decision = route(request)
    assert decision.lane is Lane.SLOW
    assert "capability_not_allowlisted" in decision.reasons


def test_batch_rename_is_slow():
    request = Request("files.rename", targets=tuple(f"file-{i}" for i in range(200)),
                      cardinality=200, io_items=200)
    decision = route(request)
    assert decision.lane is Lane.SLOW
    assert "target_or_resource_count_not_one" in decision.reasons
    assert "cardinality_not_one" in decision.reasons


def test_research_composition_and_verification_are_slow():
    cases = (
        Request("research.search", target="topic", research=True),
        Request("document.compose", target="draft", durable_artifact=True, artifact="draft.md"),
        Request("calendar.create_event", target="event-1", verification=True),
    )
    assert all(route(request).lane is Lane.SLOW for request in cases)


def test_specificity_never_overrides_multi_step_or_cross_source_work():
    request = Request("calendar.create_event", target="event-1", steps=2,
                      cross_source=True, source="calendar", sources=("calendar", "mail"))
    decision = route(request)
    assert decision.lane is Lane.SLOW
    assert {"multiple_steps", "cross_source"}.issubset(decision.reasons)


def test_oversized_nested_metadata_cannot_be_fast():
    request = Request("calendar.create_event", target="event-1", metadata={
        "planner": {"notes": "x" * 1_000_000},
    })
    decision = route(request)
    assert decision.lane is Lane.SLOW
    assert "metadata_unbounded" in decision.reasons
