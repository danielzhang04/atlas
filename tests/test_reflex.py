"""Reflex lane (design §7, Task 10): router.load_intents + router.route.

Pure unit tests — no audio, no LLM, no I/O beyond reading the shipped intents.yaml. The matching
data lives ONLY in atlas/config/intents.yaml; the router hardcodes nothing (proved by the
custom-intents and pattern tests, which drive route() off dicts the router has never seen).
"""
from pathlib import Path

import pytest

from worker import router

INTENTS_PATH = Path(__file__).resolve().parents[1] / "config" / "intents.yaml"


@pytest.fixture
def intents():
    return router.load_intents(INTENTS_PATH)


# --- only explicit sleep phrases bypass Claude --------------------------------------------------
@pytest.mark.parametrize(
    "utterance,expected",
    [
        ("That's all.", "dismiss"),
        ("thats all", "dismiss"),          # Deepgram may drop the apostrophe
        ("Go to sleep", "dismiss"),
    ],
)
def test_reflex_intent_matches(intents, utterance, expected):
    assert router.route(utterance, intents) == ("reflex", expected)


# --- near-miss sentences must NOT match a reflex; they go to the fast lane ------------------------
@pytest.mark.parametrize(
    "utterance",
    [
        "cancel the deploy card",
        "repeat that to the team",
        "stop the faceless render",
        "that's all I know about it",
        "what's in the queue?",
        "how much did the render cost",
        "tell me how much credit is left please",   # anchored pattern rejects trailing text
        "Thanks, Atlas!",
        "Cancel.",
        "Repeat that.",
        "How much credit is left?",
        "Something went wrong.",
        "I'm going to fuck you.",
    ],
)
def test_near_miss_goes_fast(intents, utterance):
    assert router.route(utterance, intents) == ("fast", None)


def test_empty_or_whitespace_raises(intents):
    with pytest.raises(ValueError):
        router.route("", intents)
    with pytest.raises(ValueError):
        router.route("   ", intents)


def test_route_is_yaml_driven_not_hardcoded():
    """A custom intents dict the router has never seen drives routing — nothing is baked in."""
    custom = {"greet": {"phrases": ["hello there"]}}
    assert router.route("Hello, there!", custom) == ("reflex", "greet")
    # phrases from the real file are NOT recognised against an unrelated custom set
    assert router.route("that's all", custom) == ("fast", None)
    # and the built-in intents still route when supplied
    assert router.route("cancel", {"cancel": {"phrases": ["cancel"]}}) == ("reflex", "cancel")


def test_patterns_are_anchored_regex():
    custom = {"credit": {"patterns": [r"how much credit (is|do i have) (left|remaining)"]}}
    assert router.route("How much credit do I have remaining?", custom) == ("reflex", "credit")
    assert router.route("How much credit is left", custom) == ("reflex", "credit")
    # anchored: extra words before/after the pattern -> no match
    assert router.route("so how much credit is left again", custom) == ("fast", None)


def test_first_matching_intent_wins():
    # dismiss listed before cancel; an utterance in both resolves to the first declared intent
    intents = {"dismiss": {"phrases": ["stop"]}, "cancel": {"phrases": ["stop"]}}
    assert router.route("Stop.", intents) == ("reflex", "dismiss")


def test_load_intents_shape(intents):
    assert set(intents) == {"dismiss"}
    assert "that's all" in intents["dismiss"]["phrases"]


# --- Gate-C desk finding (2026-07-21): filler-wrapped dismissals must still hit reflex ---

def _intents():
    from worker.router import load_intents
    from pathlib import Path
    return load_intents(Path(__file__).resolve().parents[1] / "config" / "intents.yaml")

def test_filler_wrapped_dismiss_routes_reflex():
    from worker.router import route
    intents = _intents()
    for u in ("Okay. Go to sleep.", "No. Go to sleep.", "atlas go to sleep",
              "yeah thats all", "go to sleep please"):
        assert route(u, intents) == ("reflex", "dismiss"), u

def test_non_sleep_controls_are_conversation():
    from worker.router import route
    intents = _intents()
    assert route("okay cancel that", intents) == ("fast", None)
    assert route("um repeat that please", intents) == ("fast", None)
    assert route("okay thanks atlas", intents) == ("fast", None)

def test_content_words_still_route_fast():
    from worker.router import route
    intents = _intents()
    for u in ("cancel the deploy card", "go to sleep after the render finishes",
              "repeat that to the team", "no I want the other one"):
        assert route(u, intents) == ("fast", None), u
