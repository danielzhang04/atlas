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


# --- DD-3: the deterministic "open <known thing>" grammar -------------------
# The whole promise of this lane is that it is narrower than it looks: it
# matches a fixed verb, an optional determiner, and a name that IS one of the
# host's configured words -- nothing else. The two matrices below are the
# statement of that promise from both sides, and the near-miss one is the one
# that matters: every utterance in it must reach the model untouched, because
# a reflex that guesses wrong acts on Daniel's screen with nobody to catch it.
ALIASES = (
    "gmail", "email", "mail", "inbox", "calendar", "drive", "google drive",
    "youtube", "notion", "atlas", "command center", "chrome", "browser",
    "vs code", "vscode", "code", "terminal", "notepad", "spotify", "music",
)
ROOTS = ("desktop", "documents", "downloads", "kb", "home")


def _open(utterance):
    return router.reflex_open(utterance, aliases=ALIASES, roots=ROOTS)


@pytest.mark.parametrize(
    "utterance,kind,name",
    [
        ("open downloads", "root", "downloads"),
        ("Open my downloads.", "root", "downloads"),
        ("open the documents folder", "root", "documents"),
        ("Atlas, open my downloads folder please", "root", "downloads"),
        ("open home", "root", "home"),
        ("open gmail", "alias", "gmail"),
        ("Open Gmail!", "alias", "gmail"),
        ("open my email", "alias", "email"),
        ("open up spotify", "alias", "spotify"),
        ("launch chrome", "alias", "chrome"),
        ("Open VS Code", "alias", "vs code"),
        ("open the terminal", "alias", "terminal"),
        # "atlas" is both the wake word and a configured alias; the tail
        # filler rule must not eat it out of a two-word open.
        ("open atlas", "alias", "atlas"),
        ("open command center", "alias", "command center"),
        ("bring that back", "last", ""),
        ("Bring it back up.", "last", ""),
    ],
)
def test_reflex_open_matches_the_host_vocabulary(utterance, kind, name):
    assert _open(utterance) == router.OpenReflex(kind, name)


@pytest.mark.parametrize(
    "utterance",
    [
        # A name the host does not know -- the whole utterance goes to the model.
        "open the file I was just looking at",
        "open that",
        "open my emails",
        "open the downloads i just made",
        "open my tax return",
        # A verb with an argument beyond the name.
        "open gmail and archive everything",
        "open spotify and play something quiet",
        "open my downloads and my documents",
        "open notepad, then type the address",
        "open downloads in a new window",
        # Not this grammar's verbs. These read at least as naturally as a
        # request to READ something, which is exactly why they fall through.
        "show me my email",
        "pull up my inbox",
        "go to youtube",
        "can you open gmail",
        "check gmail",
        "close chrome",
        # A verb with nothing after it.
        "open",
        "open now",
        "open please",
        # Not the bring-back phrases.
        "bring that up",
        "bring back the one from yesterday",
        "put that back",
        # Ordinary conversation that happens to contain a known name.
        "what's in my downloads",
        "is gmail open",
        "did you open gmail",
    ],
)
def test_reflex_open_near_misses_reach_the_model(utterance):
    assert _open(utterance) is None


def test_reflex_open_refuses_a_name_two_host_tools_would_answer():
    """Nothing here can tell which one Daniel meant, so it guesses neither."""
    assert router.reflex_open(
        "open kb", aliases=("kb",), roots=("kb",),
    ) is None


def test_reflex_open_matches_nothing_without_a_configured_vocabulary():
    assert router.reflex_open("open downloads") is None
    assert router.reflex_open("open gmail", aliases=(), roots=()) is None


def test_reflex_open_returns_the_host_word_not_the_spoken_one():
    """What reaches the tool is the configured word, cased as configured --
    never the raw utterance, and never a normalized fragment of it."""
    match = router.reflex_open("open GOOGLE Drive", aliases=("Google Drive",), roots=())
    assert match == router.OpenReflex("alias", "Google Drive")


@pytest.mark.parametrize("utterance", ["", "   ", None, 7])
def test_reflex_open_ignores_empty_and_non_text_input(utterance):
    assert _open(utterance) is None


def test_reflex_open_folder_suffix_is_roots_only():
    """"folder" may only ever resolve a directory, so it cannot turn an app
    alias into something it is not."""
    assert _open("open my chrome folder") is None
    assert _open("open my downloads folder") == router.OpenReflex("root", "downloads")


def test_production_apps_and_roots_answer_daniels_common_opens():
    """The grammar against the REAL configured vocabularies, not a fixture:
    a rename in config/apps.yaml that silently stops matching fails here."""
    from worker.tools import load_apps

    root = Path(__file__).resolve().parents[1]
    aliases = [
        word for app in load_apps(root / "config" / "apps.yaml").values()
        for word in app.words
    ]
    roots = ["desktop", "documents", "downloads", "kb", "home"]

    for utterance, expected in (
        ("open my downloads", ("root", "downloads")),
        ("open gmail", ("alias", "gmail")),
        ("open spotify", ("alias", "spotify")),
        ("launch chrome", ("alias", "chrome")),
        ("open my desktop", ("root", "desktop")),
    ):
        assert router.reflex_open(
            utterance, aliases=aliases, roots=roots,
        ) == router.OpenReflex(*expected)


def test_the_folder_suffix_obeys_the_same_refusal_the_bare_name_does():
    """LOW-6. " folder" used to be a separate early return that ran AFTER the
    both-vocabularies refusal had already been passed, so the guard advertised
    as "refuses rather than guesses" had a door in it: with kb configured as
    an app and a root, "open kb" refused (right) while "open kb folder"
    quietly picked the directory."""
    both = {"aliases": ("kb",), "roots": ("kb",)}

    assert router.reflex_open("open kb", **both) is None
    assert router.reflex_open("open my kb folder", **both) is None

    # With the stem in one vocabulary only, the suffix still resolves -- the
    # fix is a consistency rule, not a removal.
    assert router.reflex_open(
        "open my kb folder", aliases=(), roots=("kb",),
    ) == router.OpenReflex("root", "kb")


def test_an_alias_named_x_folder_does_not_beat_a_root_named_x():
    """The other half of the same door: the alias branch matched the whole
    name first and returned, so a configured app literally called "notes
    folder" won over a root "notes" with nothing checking that both readings
    existed."""
    assert router.reflex_open(
        "open notes folder", aliases=("notes folder",), roots=("notes",),
    ) is None

    # Only one reading, so it still resolves.
    assert router.reflex_open(
        "open notes folder", aliases=("notes folder",), roots=(),
    ) == router.OpenReflex("alias", "notes folder")


def test_a_normalize_collision_resolves_the_same_way_every_run():
    """MEDIUM-2, the grammar's half. The registry publishes these vocabularies
    as frozensets, so iteration order moves with PYTHONHASHSEED: with
    'e-mail' and 'e mail' configured for different apps, "open my e mail"
    used to resolve to one of them on some restarts and the other on others.
    ToolRegistry refuses that config at load; this pins that if one ever does
    reach the grammar, the answer is at least the same every time."""
    aliases = ("gmail", "e-mail", "outlook", "e mail")
    match = router.reflex_open("open my e mail", aliases=aliases, roots=())

    assert match == router.OpenReflex("alias", "e mail")  # sorted, so first wins
    for _ in range(20):
        assert router.reflex_open(
            "open my e mail", aliases=tuple(reversed(aliases)), roots=(),
        ) == match


@pytest.mark.parametrize(
    "values,field",
    [
        (("vs-code", "vs code"), "words"),
        (("my kb", "my_kb"), "roots"),
        (("Gmail", "gmail!"), "words"),
    ],
)
def test_check_open_vocabulary_names_both_colliding_entries(values, field):
    with pytest.raises(ValueError, match="same spoken word"):
        router.check_open_vocabulary(values, field=field)


def test_check_open_vocabulary_accepts_a_clean_vocabulary():
    router.check_open_vocabulary(ALIASES, field="words")
    router.check_open_vocabulary(ROOTS, field="roots")
    # Duplicates of the SAME word are not a collision -- there is nothing to
    # choose between, and both spell the one reachable alias.
    router.check_open_vocabulary(("gmail", "gmail"), field="words")


@pytest.mark.parametrize("unreachable", ["!!!", "???", "---", "   ", "..."])
def test_an_alias_that_can_never_be_spoken_is_refused_at_load(unreachable):
    """R2. normalize() keeps only [a-z0-9], so an entry made of punctuation
    normalizes to nothing and no utterance can ever produce its key. It failed
    closed -- the reflex lane simply never matched it -- but nobody told
    Daniel, so a typo in apps.yaml turned into "Atlas just ignores me when I
    say that". Same failure as a collision, so it is said the same way."""
    with pytest.raises(ValueError, match="no speakable characters"):
        router.check_open_vocabulary(("gmail", unreachable), field="words")


def test_a_non_string_vocabulary_entry_is_refused_for_the_same_reason():
    """One step earlier on the same failure: a vocabulary word nothing can
    ever say. Unreachable in production (both call sites pass mapping keys),
    which is exactly why it should raise rather than be quietly dropped."""
    with pytest.raises(ValueError, match="must be strings"):
        router.check_open_vocabulary(("gmail", 7), field="words")
    with pytest.raises(ValueError, match="must be strings"):
        router.check_open_vocabulary(("gmail", None), field="roots")


def test_the_registry_refuses_an_unspeakable_alias_at_configure_time():
    """The check has to bite where the config is actually loaded, not only in
    the router's own unit tests."""
    from worker.tools import ToolRegistry

    registry = ToolRegistry()
    with pytest.raises(ValueError, match="no speakable characters"):
        registry._configure_open_aliases({"gmail": None, "!!!": None})
    with pytest.raises(ValueError, match="no speakable characters"):
        registry._configure_root_names(["downloads", "###"])

    assert registry.open_aliases == frozenset()
    assert registry.root_names == frozenset()
