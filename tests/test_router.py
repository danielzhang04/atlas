"""Router lane dispatch. V1 (Task 10) changed the signature from `route(utterance) -> str` to
`route(utterance, intents) -> ("reflex", name) | ("fast", None)` — these assertions are updated to
the new contract (the reflex-specific matching is covered in test_reflex.py)."""
import pytest

from worker.router import route


def test_unmatched_utterance_routes_fast():
    assert route("what's in the queue?", {}) == ("fast", None)
    # a real intent set present, but this utterance matches none of it -> still fast
    assert route("tell me the plan", {"dismiss": {"phrases": ["that's all"]}}) == ("fast", None)


def test_reflex_match_returns_intent_name():
    assert route("that's all", {"dismiss": {"phrases": ["that's all"]}}) == ("reflex", "dismiss")


def test_route_rejects_empty():
    with pytest.raises(ValueError):
        route("   ", {})
