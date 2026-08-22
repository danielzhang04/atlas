"""Configured reflex matching for dismiss, cancel, and repeat."""
from __future__ import annotations

from pathlib import Path

import pytest

from worker import router

INTENTS_PATH = Path(__file__).resolve().parents[1] / "config" / "intents.yaml"


@pytest.fixture
def intents():
    return router.load_intents(INTENTS_PATH)


@pytest.mark.parametrize(
    "utterance,expected",
    [
        ("That's all.", "dismiss"),
        ("Go to sleep", "dismiss"),
        ("Cancel.", "cancel"),
        ("Never mind", "cancel"),
        ("Stop", "cancel"),
        ("Repeat that.", "repeat"),
        ("Say that again", "repeat"),
    ],
)
def test_exact_reflexes(intents, utterance, expected):
    assert router.route(utterance, intents) == ("reflex", expected)


@pytest.mark.parametrize(
    "utterance",
    [
        "cancel the deploy card",
        "repeat that to the team",
        "stop the render",
        "that's all I know about it",
        "go to sleep after the render finishes",
        "what's in the queue?",
    ],
)
def test_content_bearing_near_misses_reach_conversation(intents, utterance):
    assert router.route(utterance, intents) == ("fast", None)


def test_bounded_filler_is_allowed(intents):
    assert router.route("Okay. Go to sleep please.", intents) == ("reflex", "dismiss")
    assert router.route("Atlas, stop now.", intents) == ("reflex", "cancel")


def test_custom_intents_and_patterns_drive_matching():
    custom = {
        "greet": {"phrases": ["hello there"]},
        "credit": {"patterns": [r"how much credit (is|do i have) (left|remaining)"]},
    }
    assert router.route("Hello, there!", custom) == ("reflex", "greet")
    assert router.route("How much credit is left?", custom) == ("reflex", "credit")
    assert router.route("so how much credit is left again", custom) == ("fast", None)


def test_config_and_empty_input_boundary(intents):
    assert set(intents) == {"dismiss", "cancel", "repeat"}
    with pytest.raises(ValueError):
        router.route("   ", intents)
