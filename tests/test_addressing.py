"""Addressed-speech gate tests over a deterministic clock."""
from pathlib import Path

import pytest
import yaml

from worker.addressing import Addressing, vocabulary
from worker.router import normalize


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def test_vocab_matches_tokens_and_word_boundary_phrases_without_substrings():
    gate = Addressing(30, ["email", "google drive", "faceless-youtube"])

    assert gate.is_addressed(normalize("Atlas, what time is it?"))
    assert gate.is_addressed(normalize("Check my email"))
    assert gate.is_addressed(normalize("Open Google Drive"))
    assert gate.is_addressed(normalize("Start faceless youtube"))
    assert not gate.is_addressed(normalize("The filename is calendarized"))
    assert not gate.is_addressed(normalize("That is an atlaslike shape"))


def test_activity_window_is_inclusive_and_checks_do_not_rearm_it():
    clock = FakeClock()
    gate = Addressing(30, (), clock=clock)
    gate.mark_activity()

    clock.value = 20
    assert gate.is_addressed(normalize("ordinary follow up"))
    clock.value = 30
    assert gate.is_addressed(normalize("another follow up"))
    clock.value = 30.01
    assert not gate.is_addressed(normalize("room conversation"))


def test_mark_activity_rearms_the_window():
    clock = FakeClock()
    gate = Addressing(5, (), clock=clock)
    gate.mark_activity()
    clock.value = 6
    assert not gate.is_addressed(normalize("ordinary follow up"))

    gate.mark_activity()
    clock.value = 11
    assert gate.is_addressed(normalize("ordinary follow up"))


def test_vocabulary_uses_only_the_explicit_address_terms():
    cfg = {
        "address_vocab": ["gmail", "inbox", "workers"],
        "apps": {"browser": {"words": ["browser"]}},
    }

    assert vocabulary(cfg) == ["gmail", "inbox", "workers"]


def test_production_vocabulary_contains_only_unambiguous_domain_words():
    config_path = Path(__file__).resolve().parents[1] / "config" / "atlas.yaml"
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert vocabulary(cfg) == [
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
        vocabulary({"address_vocab": configured})
