"""Configured reflex matching for dismiss, cancel, and repeat."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from worker import router

INTENTS_PATH = Path(__file__).resolve().parents[1] / "config" / "intents.yaml"


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


@pytest.fixture
def intents():
    return router.load_intents(INTENTS_PATH)


@pytest.mark.parametrize(
    "utterance,expected",
    [
        ("That's all.", "dismiss"),
        ("Thats all", "dismiss"),
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


def test_normalize_separates_words_at_every_non_alphanumeric_character():
    assert router.normalize("Atlas—what time is it?") == "atlas what time is it"
    assert router.normalize("faceless/youtube_and.more") == "faceless youtube and more"


def test_config_and_empty_input_boundary(intents):
    assert set(intents) == {"dismiss", "cancel", "repeat"}
    with pytest.raises(ValueError):
        router.route("   ", intents)


def test_vocab_matches_tokens_and_word_boundary_phrases_without_substrings():
    gate = router.Addressing(30, ["email", "google drive", "faceless-youtube"])

    assert gate.is_addressed(router.normalize("Atlas, what time is it?"))
    assert gate.is_addressed(router.normalize("Check my email"))
    assert gate.is_addressed(router.normalize("Open Google Drive"))
    assert gate.is_addressed(router.normalize("Start faceless youtube"))
    assert not gate.is_addressed(router.normalize("The filename is calendarized"))
    assert not gate.is_addressed(router.normalize("That is an atlaslike shape"))


def test_activity_window_is_inclusive_and_checks_do_not_rearm_it():
    clock = FakeClock()
    gate = router.Addressing(30, (), clock=clock)
    gate.mark_activity()

    clock.value = 20
    assert gate.is_addressed(router.normalize("ordinary follow up"))
    clock.value = 30
    assert gate.is_addressed(router.normalize("another follow up"))
    clock.value = 30.01
    assert not gate.is_addressed(router.normalize("room conversation"))


def test_addressed_window_and_late_vocab_routing():
    clock = FakeClock()
    gate = router.Addressing(90, ("atlas",), clock=clock)
    gate.mark_activity()

    clock.value = 34
    assert gate.is_addressed(router.normalize("ordinary follow up"))
    clock.value = 100
    assert not gate.is_addressed(router.normalize("ordinary room conversation"))
    assert gate.is_addressed(router.normalize("could you atlas check my calendar"))


def test_mark_activity_rearms_the_window():
    clock = FakeClock()
    gate = router.Addressing(5, (), clock=clock)
    gate.mark_activity()
    clock.value = 6
    assert not gate.is_addressed(router.normalize("ordinary follow up"))

    gate.mark_activity()
    clock.value = 11
    assert gate.is_addressed(router.normalize("ordinary follow up"))


def test_vocabulary_uses_only_the_explicit_address_terms():
    cfg = {
        "address_vocab": ["gmail", "inbox", "workers"],
        "apps": {"browser": {"words": ["browser"]}},
    }

    assert router.vocabulary(cfg) == ["gmail", "inbox", "workers"]


def test_production_vocabulary_contains_only_unambiguous_domain_words():
    config_path = Path(__file__).resolve().parents[1] / "config" / "atlas.yaml"
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert router.vocabulary(cfg) == [
        "gmail",
        "inbox",
        "unread",
        "calendar",
        "youtube",
        "notion",
        "github",
        "spotify",
        "workers",
    ]


@pytest.mark.parametrize(
    "configured",
    [None, "gmail", ["gmail", ""]],
)
def test_vocabulary_rejects_invalid_address_terms(configured):
    with pytest.raises(ValueError, match="address_vocab"):
        router.vocabulary({"address_vocab": configured})
