"""sanitize_for_tts (conversation-rules design §2): markdown never reaches the speaker."""
from worker.sanitize import sanitize_for_tts


def test_bold_and_backticks_stripped():
    assert sanitize_for_tts("you've got **three active projects**") == "you've got three active projects"
    assert sanitize_for_tts("one card in `working` right now") == "one card in working right now"


def test_marker_chars_stripped_even_when_split_across_chunks():
    # streaming: "**bold**" may arrive as "*", "*bold*", "*" — char-level strip survives any split
    assert sanitize_for_tts("*") == ""
    assert sanitize_for_tts("*bold*") == "bold"


def test_headers_and_bullets_dropped():
    assert sanitize_for_tts("# Status\n- one card\n- two cards") == "Status\none card\ntwo cards"
    assert sanitize_for_tts("1. first\n2. second") == "first\nsecond"


def test_links_collapse_to_text():
    assert sanitize_for_tts("see [the dashboard](http://x) for more") == "see the dashboard for more"


def test_underscores_become_spaces_and_whitespace_not_stripped():
    assert sanitize_for_tts("file_card is ready") == "file card is ready"
    # chunk boundaries: single leading/trailing spaces are word separators — preserved
    assert sanitize_for_tts(" and then ") == " and then "


def test_hash_in_pr_number_reads_clean():
    assert sanitize_for_tts("PR #44 merged") == "PR 44 merged"


def test_plain_speech_untouched():
    text = "Quiet. Twenty-ish cards in inbox, four working, nothing stuck."
    assert sanitize_for_tts(text) == text


def test_quiet_marker_never_spoken():
    # Gate finding #1: [quiet] is the LLM's structural silence — stripped before synthesis.
    assert sanitize_for_tts("[quiet]").strip() == ""
    assert sanitize_for_tts("[Quiet].").strip() == ""
    # a rule-violating mixed turn still loses the marker but keeps the content
    assert sanitize_for_tts("[quiet] I'm here").strip() == "I'm here"


def test_is_quiet_turn_detection():
    from worker.sanitize import is_quiet_turn
    assert is_quiet_turn("[quiet]")
    assert is_quiet_turn("  [Quiet]. ")
    assert not is_quiet_turn("[quiet] I'm here")
    assert not is_quiet_turn("Quiet. Twenty-ish cards in inbox.")


def test_split_quiet_marker_limitation():
    """Documents the COUPLING NOTE in sanitize.py (review 2026-07-22): the multi-char [quiet]
    marker is NOT independently streaming-safe. Applied per-chunk to a mid-marker split, the
    fragments leak through — which is exactly why marker suppression depends on livekit's
    upstream filter_markdown buffering unclosed '[' so the marker arrives contiguous. If this
    test ever starts FAILING (fragments no longer leak), sanitize gained its own buffering and
    the coupling note can be dropped; until then, never remove filter_markdown from
    tts_text_transforms."""
    from worker.sanitize import sanitize_for_tts
    leaked = sanitize_for_tts("[") + sanitize_for_tts("quiet") + sanitize_for_tts("]")
    assert leaked != ""          # the split marker leaks when sanitize runs alone
    # the contiguous marker, by contrast, is fully suppressed
    assert sanitize_for_tts("[quiet]") == ""
