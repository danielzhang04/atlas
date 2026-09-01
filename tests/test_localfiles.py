"""Root confinement and bounded local-file behavior."""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest

from worker import localfiles
from worker.localfiles import LocalFiles


def test_resolve_accepts_existing_paths_only_inside_configured_roots(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    inside = root / "notes.txt"
    inside.write_text("hello", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    files = LocalFiles([root], opener=lambda _path: None)

    assert files.resolve(inside) == inside.resolve()
    with pytest.raises(ValueError, match="outside roots"):
        files.resolve(outside)
    with pytest.raises(ValueError, match="outside roots"):
        files.resolve(root / "missing.txt")


def test_find_is_case_insensitive_newest_first_limited_and_skips_unsafe_trees(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    old = root / "Haiku old.txt"
    newest = root / "notes-HAIKU.md"
    old.write_text("old", encoding="utf-8")
    newest.write_text("new", encoding="utf-8")
    os.utime(old, (10, 10))
    os.utime(newest, (20, 20))
    skipped = root / ".GIT"
    skipped.mkdir()
    (skipped / "haiku-secret.txt").write_text("skip", encoding="utf-8")
    deep = root
    for index in range(7):
        deep = deep / f"level-{index}"
        deep.mkdir()
    (deep / "haiku-too-deep.txt").write_text("skip", encoding="utf-8")

    results = LocalFiles([root], opener=lambda _path: None).find("haiku", limit=2)

    assert [item["path"] for item in results] == [str(newest.resolve()), str(old.resolve())]
    assert results[0]["size"] == 3
    assert results[0]["modified"] == 20


@pytest.mark.parametrize(
    "query",
    [
        "atlas voice layer design spec",
        "voice layer",
        "atlas-voice",
    ],
)
def test_find_normalizes_realistic_design_spec_names_and_queries(tmp_path, query):
    root = tmp_path / "kb"
    spec = root / "docs" / "specs" / "2026-07-15-atlas-voice-layer-design.md"
    spec.parent.mkdir(parents=True)
    spec.write_text("design", encoding="utf-8")

    results = LocalFiles([root], opener=lambda _path: None).find(query)

    assert [item["path"] for item in results] == [str(spec.resolve())]


def test_find_falls_back_to_half_the_tokens_ranked_by_matches_then_mtime(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    strongest = root / "atlas-voice-layer-notes.md"
    newer_weaker = root / "atlas-voice-overview.md"
    older_weaker = root / "layer-design-outline.md"
    strongest.write_text("three", encoding="utf-8")
    newer_weaker.write_text("two", encoding="utf-8")
    older_weaker.write_text("two", encoding="utf-8")
    os.utime(strongest, (10, 10))
    os.utime(newer_weaker, (30, 30))
    os.utime(older_weaker, (20, 20))

    results = LocalFiles([root], opener=lambda _path: None).find(
        "atlas voice layer design",
    )

    assert [item["path"] for item in results] == [
        str(strongest.resolve()),
        str(newer_weaker.resolve()),
        str(older_weaker.resolve()),
    ]


def test_find_stops_when_the_injected_clock_exhausts_the_budget(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "match.txt").write_text("one", encoding="utf-8")
    ticks = iter((0.0, 0.5, 2.1))
    files = LocalFiles([root], opener=lambda _path: None, clock=lambda: next(ticks, 2.1))

    assert files.find("match", budget_s=2.0) == []


def test_find_keeps_only_the_twenty_newest_matches_while_scanning(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    for index in range(25):
        match = root / f"match-{index:02}.txt"
        match.write_text(str(index), encoding="utf-8")
        os.utime(match, (index, index))

    results = LocalFiles([root], opener=lambda _path: None).find("match", limit=25)

    assert len(results) == 20
    assert [item["modified"] for item in results] == list(range(24, 4, -1))


def test_find_does_not_follow_a_directory_link_outside_the_roots(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "haiku-private.txt").write_text("private", encoding="utf-8")
    try:
        (root / "linked").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory links are unavailable")

    assert LocalFiles([root], opener=lambda _path: None).find("haiku") == []


def test_open_focuses_the_window_it_produced_and_reports_it_honestly(tmp_path):
    """Opening ends with the thing in front, and says whether it managed it.

    The snapshot is taken BEFORE the launch and the focuser is given only
    that snapshot, so what gets focused is the window this open produced --
    never one that was already on screen.
    """
    root = tmp_path / "root"
    root.mkdir()
    note = root / "notes.txt"
    note.write_text("hi", encoding="utf-8")
    folder = root / "plans"
    folder.mkdir()
    order = []
    window = {"title": "notes.txt - Notepad", "pid": 91, "_handle": 9001}
    files = LocalFiles(
        [root],
        opener=lambda path: order.append(("launch", path)),
        folder_opener=lambda path: order.append(("launch_folder", path)),
        window_snapshot=lambda: order.append(("snapshot",)) or frozenset({1}),
        new_window_focuser=lambda before, expected: (
            order.append(("focus", before, expected))
            or (window if len(order) < 5 else None)
        ),
        handler_resolver=lambda suffix: (
            "C:/Windows/notepad.exe" if suffix == ".txt" else None
        ),
    )
    recorded = []
    files.set_open_observer(recorded.append)

    opened = files.open(note)
    listed = files.open_folder(folder)

    assert opened == {"opened": str(note.resolve()), "focused": True}
    # Second open: the focuser found nothing unambiguous, and that is
    # reported as a failure rather than swallowed into a bare "opened".
    assert listed == {"opened": str(folder.resolve()), "focused": False}
    # The focuser is handed the expected handler alongside the snapshot, so
    # what it focuses has to be a window of the process the shell said would
    # handle this file -- not merely a window that turned up in time.
    assert order == [
        ("snapshot",), ("launch", str(note.resolve())),
        ("focus", frozenset({1}), ("C:/Windows/notepad.exe",)),
        ("snapshot",), ("launch_folder", str(folder.resolve())),
        ("focus", frozenset({1}), ()),
    ]
    # Only the successful focus is remembered, and the HWND travels to the
    # host observer -- never in the dict the model reads.
    assert recorded == [{
        "kind": "file",
        "label": str(note.resolve()),
        "title": "notes.txt - Notepad",
        "pid": 91,
        "hwnd": 9001,
    }]
    assert "hwnd" not in opened and "_handle" not in opened


def test_a_foreign_window_that_pops_during_an_open_is_neither_focused_nor_recorded(
    tmp_path, monkeypatch,
):
    """DD-2/F2, end to end through the real focuser.

    `open_file` goes through os.startfile, which returns no focused key, so
    this is the path EVERY file open takes. A Teams call toast appearing
    inside the 2.5s poll used to be focused, `open` reported focused: true
    about it, and the opened-record observer filed the toast's title and pid
    under the file's label -- which a later focus_last_opened would then
    raise as though it were Daniel's document.

    Nothing is stubbed out between LocalFiles and the enumeration here except
    the enumeration itself: the real _focus_new_window wrapper and the real
    desktopcontrol.focus_new_window both run.
    """
    from worker import desktopcontrol, localfiles

    root = tmp_path / "root"
    root.mkdir()
    note = root / "notes.txt"
    note.write_text("hi", encoding="utf-8")

    focused = []
    # One new window, from a process that is not the registered handler.
    toast = {
        "_handle": 70, "_process_path": "C:/Apps/Teams.exe",
        "title": "Incoming call", "pid": 707,
    }
    monkeypatch.setattr(
        desktopcontrol, "_window_records", lambda **_kwargs: [toast],
    )
    monkeypatch.setattr(
        desktopcontrol, "_focus_record",
        lambda record, **_kwargs: focused.append(record),
    )
    monkeypatch.setattr(desktopcontrol, "_apis", lambda *_a, **_k: (None, None))
    # The autouse fixture points localfiles at a no-window stub; this test
    # wants the real thing, and its own patch wins.
    monkeypatch.setattr(localfiles, "_desktopcontrol", lambda: desktopcontrol)

    files = LocalFiles(
        [root],
        opener=lambda _path: None,
        window_snapshot=frozenset,
        handler_resolver=lambda _suffix: "C:/Windows/notepad.exe",
    )
    recorded = []
    files.set_open_observer(recorded.append)

    opened = files.open(note)

    # Honest: the file opened, nothing was brought to the front.
    assert opened == {"opened": str(note.resolve()), "focused": False}
    assert focused == []       # the toast was never focused
    assert recorded == []      # and never filed under the file's label


def test_an_open_with_no_resolvable_handler_focuses_nothing(tmp_path):
    """Unknown identity is reported as focused: false, never guessed at."""
    root = tmp_path / "root"
    root.mkdir()
    note = root / "notes.txt"
    note.write_text("hi", encoding="utf-8")
    seen = []
    files = LocalFiles(
        [root],
        opener=lambda _path: None,
        window_snapshot=frozenset,
        new_window_focuser=lambda before, expected: seen.append(expected),
        handler_resolver=lambda _suffix: None,
    )

    assert files.open(note) == {"opened": str(note.resolve()), "focused": False}
    assert seen == [()]

    # A resolver that raises is the same answer, not a failed open: a shell
    # lookup blowing up must never cost Daniel the file he asked for.
    def explode(_suffix):
        raise OSError("shlwapi said no")

    exploding = LocalFiles(
        [root], opener=lambda _path: None, window_snapshot=frozenset,
        new_window_focuser=lambda before, expected: seen.append(expected),
        handler_resolver=explode,
    )
    assert exploding.open(note) == {
        "opened": str(note.resolve()), "focused": False,
    }
    assert seen == [(), ()]


def test_a_launcher_that_already_focused_is_not_polled_a_second_time(tmp_path):
    """One wait per open, not two.

    The folder opener polls for its own window and reports the answer. Doing
    it again here stacked two 2.5s waits into one call and overran the open
    deadline, so a folder that had opened fine came back as a cloud
    placeholder error. When the launcher answers, that answer is used.
    """
    root = tmp_path / "root"
    folder = root / "plans"
    folder.mkdir(parents=True)
    window = {"title": "plans", "pid": 5, "_handle": 8001}
    polled = []
    files = LocalFiles(
        [root],
        folder_opener=lambda _path: {
            "application": "explorer.exe", "pid": 5, "targeted": True,
            "focused": True, "_window": window,
        },
        window_snapshot=frozenset,
        new_window_focuser=lambda before, _expected: polled.append(before),
    )
    recorded = []
    files.set_open_observer(recorded.append)

    assert files.open_folder(folder) == {
        "opened": str(folder.resolve()), "focused": True,
    }
    assert polled == []  # no second wait
    assert recorded == [{
        "kind": "folder", "label": str(folder.resolve()),
        "title": "plans", "pid": 5, "hwnd": 8001,
    }]


def test_a_launcher_that_reports_no_focus_is_believed(tmp_path):
    root = tmp_path / "root"
    folder = root / "plans"
    folder.mkdir(parents=True)
    polled = []
    files = LocalFiles(
        [root],
        folder_opener=lambda _path: {
            "application": "explorer.exe", "pid": 5, "targeted": True,
            "focused": False, "_window": None,
        },
        window_snapshot=frozenset,
        new_window_focuser=lambda before, _expected: polled.append(before),
    )

    # Explorer answered inside one of several windows it already had, so the
    # launcher could not tell which. That is reported, not re-litigated here.
    assert files.open_folder(folder) == {
        "opened": str(folder.resolve()), "focused": False,
    }
    assert polled == []


def test_open_uses_the_resolved_path_and_small_read_returns_full_text(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    note = root / "notes.txt"
    note.write_bytes(b"first\nsecond line tail")
    opened = []
    files = LocalFiles([root], opener=opened.append)

    assert files.open(note) == {"opened": str(note.resolve()), "focused": False}
    assert opened == [str(note.resolve())]
    result = files.read(note)
    assert result == {
        "path": str(note.resolve()),
        "bytes": note.stat().st_size,
        "text": "first\nsecond line tail",
        "truncated": False,
    }


def test_big_read_returns_only_a_short_preview_and_launch_work_note(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    report = root / "sales.csv"
    text = "month,revenue\n" + "".join(
        f"2026-{index:04},1000\n"
        for index in range(2_000)
    )
    report.write_bytes(text.encode("utf-8"))
    files = LocalFiles([root], opener=lambda _path: None)

    result = asyncio.run(files.read_file(report))

    assert report.stat().st_size > 16_384
    assert result == {
        "path": str(report.resolve()),
        "bytes": report.stat().st_size,
        "truncated": True,
        "preview": text[:1_024],
        "note": (
            "too large to read in-lane; use launch_work with this exact path for analysis"
        ),
        "lines": 2_001,
    }
    assert "text" not in result


@pytest.mark.parametrize(
    "name",
    ["program.exe", "script.bat", "shortcut.lnk", "site.url", "source.js"],
)
def test_open_rejects_executable_shortcut_and_javascript_extensions(tmp_path, name):
    root = tmp_path / "root"
    root.mkdir()
    unsafe = root / name
    unsafe.write_text("content", encoding="utf-8")
    opened = []

    with pytest.raises(ValueError, match="not an openable document"):
        LocalFiles([root], opener=opened.append).open(unsafe)

    assert opened == []


def test_javascript_can_be_read_but_never_opened(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    source = root / "app.js"
    source.write_text("const answer = 42;", encoding="utf-8")
    files = LocalFiles([root], opener=lambda _path: None)

    assert files.read(source)["text"] == "const answer = 42;"
    with pytest.raises(ValueError, match="not an openable document"):
        files.open(source)


def test_read_accepts_utf16_with_bom_and_rejects_invalid_text(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    utf16 = root / "utf16.txt"
    invalid = root / "invalid.txt"
    utf16.write_text("hello", encoding="utf-16")
    invalid.write_bytes(b"\x80not utf-8")
    files = LocalFiles([root], opener=lambda _path: None)

    result = files.read(utf16)

    assert result["text"] == "hello"
    assert result["truncated"] is False
    with pytest.raises(ValueError, match="not a text file"):
        files.read(invalid)


def test_read_rejects_extensions_outside_the_text_allowlist(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    binary = root / "image.bin"
    binary.write_bytes(b"ordinary utf-8 bytes")

    with pytest.raises(ValueError, match="not a text file"):
        LocalFiles([root], opener=lambda _path: None).read(binary)


def test_open_and_read_refuse_paths_with_a_link_component(tmp_path):
    root = tmp_path / "root"
    target = root / "target"
    root.mkdir()
    target.mkdir()
    note = target / "notes.txt"
    note.write_text("private", encoding="utf-8")
    link = root / "linked"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory links are unavailable")
    files = LocalFiles([root], opener=lambda _path: None)

    with pytest.raises(ValueError, match="reparse"):
        files.open(link / "notes.txt")
    with pytest.raises(ValueError, match="reparse"):
        files.read(link / "notes.txt")


def test_constructor_rejects_an_empty_root_list():
    with pytest.raises(ValueError, match="root"):
        LocalFiles([], opener=lambda _path: None)


def test_known_roots_use_the_injected_known_folder_resolver(tmp_path):
    documents = tmp_path / "OneDrive" / "Documents"
    documents.mkdir(parents=True)
    sales = documents / "sales.csv"
    sales.write_text("total\n42\n", encoding="utf-8")
    resolved_names = []

    def resolver(name):
        resolved_names.append(name)
        return documents

    files = LocalFiles(
        ["known:Documents"],
        opener=lambda _path: None,
        known_folder_resolver=resolver,
    )

    assert resolved_names == ["Documents"]
    assert files.folders == {"Documents": documents.resolve()}
    assert files.find("sales") == [{
        "path": str(sales.resolve()),
        "size": sales.stat().st_size,
        "modified": sales.stat().st_mtime,
    }]


def test_unknown_and_missing_roots_are_skipped_with_one_warning_each(tmp_path, caplog):
    missing = tmp_path / "missing"

    def resolver(_name):
        raise ValueError("unknown folder")

    with caplog.at_level("WARNING", logger="atlas.localfiles"):
        files = LocalFiles(
            ["known:Unknown", missing],
            opener=lambda _path: None,
            known_folder_resolver=resolver,
        )

    assert files.find("sales") == []
    assert len(caplog.records) == 2
    assert caplog.messages == [
        "skipping file root known:Unknown: unknown folder",
        f"skipping file root {missing}: directory is unavailable",
    ]


def test_cloud_files_reparse_tag_is_allowed_inside_a_configured_root(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "OneDrive"
    root.mkdir()
    placeholder = root / "notes.txt"
    placeholder.write_text("hydrated", encoding="utf-8")
    real_lstat = os.lstat
    actual = real_lstat(placeholder)

    def tagged_lstat(path):
        if os.fspath(path) != os.fspath(placeholder):
            return real_lstat(path)
        return SimpleNamespace(
            st_file_attributes=localfiles._REPARSE_POINT,
            st_reparse_tag=0x9000C01A,
            st_mode=actual.st_mode,
        )

    files = LocalFiles([root], opener=lambda _path: None)
    monkeypatch.setattr(localfiles.os, "lstat", tagged_lstat)

    files._refuse_reparse_points(placeholder, root)


def test_non_cloud_reparse_tag_remains_refused(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    linked = root / "linked.txt"
    linked.write_text("data", encoding="utf-8")
    actual = os.lstat(linked)

    monkeypatch.setattr(
        localfiles.os,
        "lstat",
        lambda _path: SimpleNamespace(
            st_file_attributes=localfiles._REPARSE_POINT,
            st_reparse_tag=0xA000000C,
            st_mode=actual.st_mode,
        ),
    )
    files = LocalFiles([root], opener=lambda _path: None)

    with pytest.raises(ValueError, match="reparse"):
        files._refuse_reparse_points(linked, root)


def test_open_file_and_read_file_run_off_loop_with_five_second_deadline(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "root"
    root.mkdir()
    document = root / "notes.txt"
    document.write_text("hello", encoding="utf-8")
    opened = []
    offloaded = []
    deadlines = []
    files = LocalFiles([root], opener=opened.append)

    async def to_thread(function, *args):
        offloaded.append(function.__name__)
        return function(*args)

    async def wait_for(awaitable, *, timeout):
        deadlines.append(timeout)
        return await awaitable

    monkeypatch.setattr(localfiles.asyncio, "to_thread", to_thread)
    monkeypatch.setattr(localfiles.asyncio, "wait_for", wait_for)

    opened_result = asyncio.run(files.open_file(document))
    read_result = asyncio.run(files.read_file(document))

    assert opened_result == {"opened": str(document.resolve()), "focused": False}
    assert read_result["text"] == "hello"
    assert opened == [str(document.resolve())]
    assert offloaded == ["open", "read"]
    # Opens get the longer deadline: they also wait for the window to focus.
    assert deadlines == [7.5, 5.0]


@pytest.mark.parametrize("operation", ["open_file", "read_file"])
def test_cloud_hydration_timeout_returns_file_not_available_error(
    tmp_path,
    monkeypatch,
    operation,
):
    root = tmp_path / "root"
    root.mkdir()
    document = root / "notes.txt"
    document.write_text("hello", encoding="utf-8")
    files = LocalFiles([root], opener=lambda _path: None)

    async def stalled_to_thread(_function, *_args):
        await asyncio.Event().wait()

    monkeypatch.setattr(localfiles.asyncio, "to_thread", stalled_to_thread)
    monkeypatch.setattr(localfiles, "_FILE_DEADLINE_S", 0.01)

    result = asyncio.run(getattr(files, operation)(document))

    assert result == {
        "error": "file not available yet (cloud placeholder)",
    }


# --- named roots (CC3) ------------------------------------------------------

def test_roots_are_named_by_known_folder_basename_or_explicit_name(tmp_path):
    documents = tmp_path / "Documents"
    kb = tmp_path / "kb"
    home = tmp_path / "home"
    for directory in (documents, kb, home):
        directory.mkdir()

    files = LocalFiles(
        ["known:Documents", kb, {"path": str(home), "name": "Home"}],
        opener=lambda _path: None,
        known_folder_resolver=lambda _name: documents,
    )

    assert files.root_names == {
        "documents": documents.resolve(),
        "kb": kb.resolve(),
        "home": home.resolve(),
    }
    # Names are casefolded on both sides, so what Daniel says out loud matches.
    assert files.resolve_root("HOME") == home.resolve()
    assert files.resolve_root("  documents  ") == documents.resolve()


def test_resolve_root_refuses_an_invented_name_with_a_usable_error(tmp_path):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    files = LocalFiles([downloads], opener=lambda _path: None)

    with pytest.raises(ValueError) as excinfo:
        files.resolve_root("c:/windows/system32")

    # The error names the real vocabulary so the model can self-correct rather
    # than guess again or claim the folder does not exist.
    assert str(excinfo.value) == "unknown root; the configured roots are: downloads"
    for invalid in ("", "   ", None, 7, True):
        with pytest.raises(ValueError, match="root"):
            files.resolve_root(invalid)


def test_duplicate_root_names_keep_the_first_entry(tmp_path, caplog):
    first = tmp_path / "a" / "kb"
    second = tmp_path / "b" / "kb"
    first.mkdir(parents=True)
    second.mkdir(parents=True)

    with caplog.at_level("WARNING", logger="atlas.localfiles"):
        files = LocalFiles([first, second], opener=lambda _path: None)

    assert files.root_names == {"kb": first.resolve()}
    assert caplog.messages == ["duplicate file root name kb; keeping the first"]
    # Both are still real roots for confinement; only the NAME is one-to-one.
    assert files.resolve(second) == second.resolve()


def test_malformed_root_entries_fail_loudly_instead_of_shrinking_the_scope(tmp_path):
    root = tmp_path / "root"
    root.mkdir()

    for malformed in (
        {"name": "home"},
        {"path": "", "name": "home"},
        {"path": str(root), "name": "  "},
        {"path": str(root), "name": "home", "extra": 1},
        {"path": 7, "name": "home"},
        7,
        None,
    ):
        with pytest.raises(ValueError, match="invalid file root"):
            LocalFiles([malformed], opener=lambda _path: None)

    assert localfiles.valid_file_root({"path": str(root)}) is True
    assert localfiles.valid_file_root("known:Documents") is True
    assert localfiles.valid_file_root("   ") is False


def test_find_can_be_scoped_to_one_root(tmp_path):
    downloads = tmp_path / "Downloads"
    kb = tmp_path / "kb"
    downloads.mkdir()
    kb.mkdir()
    (downloads / "budget.csv").write_text("a", encoding="utf-8")
    (kb / "budget.csv").write_text("b", encoding="utf-8")
    files = LocalFiles(
        [{"path": str(downloads), "name": "downloads"}, kb],
        opener=lambda _path: None,
    )

    everywhere = {item["path"] for item in files.find("budget")}
    scoped = [item["path"] for item in files.find("budget", root="downloads")]

    assert everywhere == {
        str((downloads / "budget.csv").resolve()), str((kb / "budget.csv").resolve()),
    }
    assert scoped == [str((downloads / "budget.csv").resolve())]
    with pytest.raises(ValueError, match="unknown root"):
        files.find("budget", root="desktop")


def test_nested_roots_dedupe_results_without_weakening_confinement(tmp_path):
    home = tmp_path / "home"
    documents = home / "Documents"
    documents.mkdir(parents=True)
    report = documents / "quarterly-report.md"
    report.write_text("numbers", encoding="utf-8")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    files = LocalFiles(
        [documents, {"path": str(home), "name": "home"}],
        opener=lambda _path: None,
    )

    found = files.find("quarterly report")

    # The file lives under BOTH roots. It is one result, not two.
    assert [item["path"] for item in found] == [str(report.resolve())]
    # Each root still resolves to its own directory -- the narrow name wins
    # for its own name, and never widens to the root that contains it.
    assert files.resolve_root("documents") == documents.resolve()
    assert files.resolve_root("home") == home.resolve()
    # Confinement is unchanged by the overlap.
    assert files.resolve(report) == report.resolve()
    with pytest.raises(ValueError, match="outside roots"):
        files.resolve(outside)


# --- credential exclusion (constitution rule 1) -----------------------------

def test_credential_paths_are_refused_even_when_a_root_contains_them(tmp_path):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".claude").mkdir()
    (home / "AppData" / "Local").mkdir(parents=True)
    (home / "Documents").mkdir()
    excluded = [
        home / ".claude.json",
        home / ".ssh" / "id_rsa",
        home / ".claude" / "settings.json",
        home / "AppData" / "Local" / "secrets.json",
        home / ".env",
        home / ".env.local",
        home / ".netrc",
    ]
    for path in excluded:
        path.write_text("secret-token", encoding="utf-8")
    allowed = home / "Documents" / "story-of-the-forest.md"
    allowed.write_text("a story", encoding="utf-8")
    files = LocalFiles([{"path": str(home), "name": "home"}], opener=lambda _path: None)

    for path in excluded:
        with pytest.raises(ValueError, match="excluded path"):
            files.resolve(path)

    # ...and they are invisible to search, so they cannot even be named back.
    assert files.find("secret") == []
    assert {item["path"] for item in files.find("story forest")} == {
        str(allowed.resolve()),
    }
    assert files.resolve(allowed) == allowed.resolve()


def test_excluded_directories_are_matched_case_insensitively(tmp_path):
    home = tmp_path / "home"
    (home / "AppData").mkdir(parents=True)
    (home / "AppData" / "token.json").write_text("t", encoding="utf-8")
    files = LocalFiles([home], opener=lambda _path: None)

    with pytest.raises(ValueError, match="excluded path"):
        files.resolve(home / "AppData" / "token.json")
    with pytest.raises(ValueError, match="excluded path"):
        files.resolve(home / "appdata")


def test_every_hidden_dotfile_is_refused_not_just_the_ones_on_a_list(tmp_path):
    """The exclusion is a class, not an inventory.

    Enumerating credential filenames loses: each of these holds live secrets,
    several in extensions LocalFiles.read decodes, and the next tool ships its
    own name. Refusing hidden components outright is what keeps rule 1 true as
    the toolchain changes underneath it.
    """
    home = tmp_path / "home"
    secrets = [
        home / ".mcp.json",
        home / ".git-credentials",
        home / ".pypirc",
        home / ".yarnrc.yml",
        home / ".m2" / "settings.xml",
        home / ".cargo" / "credentials.toml",
        home / ".jupyter" / "jupyter_server_config.json",
        home / ".terraform.d" / "credentials.tfrc.json",
        home / "Documents" / ".vault" / "token.yaml",
    ]
    for path in secrets:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("token: hunter2", encoding="utf-8")
    files = LocalFiles([{"path": str(home), "name": "home"}], opener=lambda _path: None)

    for path in secrets:
        with pytest.raises(ValueError, match="excluded path"):
            files.resolve(path)
        # Not merely unopenable -- unreadable, and invisible to search, so the
        # path cannot even be reported back for someone else to fetch.
        with pytest.raises(ValueError, match="excluded path"):
            files.read(path)
    assert files.find("credentials") == []
    assert files.find("settings") == []


def test_non_dot_credential_names_are_refused_too(tmp_path):
    home = tmp_path / "home"
    material = home / "material"
    material.mkdir(parents=True)
    (material / "id_rsa").write_text("PRIVATE KEY", encoding="utf-8")
    (material / "credentials").write_text("secret", encoding="utf-8")
    (material / "notes.md").write_text("fine", encoding="utf-8")
    # A DIRECTORY named for credentials is refused too -- the rule runs over
    # every component, so nothing under ~/keys/ is reachable either.
    (home / "keys").mkdir()
    (home / "keys" / "notes.md").write_text("fine", encoding="utf-8")
    files = LocalFiles([home], opener=lambda _path: None)

    for name in ("id_rsa", "credentials"):
        with pytest.raises(ValueError, match="excluded path"):
            files.resolve(material / name)
    with pytest.raises(ValueError, match="excluded path"):
        files.resolve(home / "keys" / "notes.md")
    assert files.resolve(material / "notes.md") == (material / "notes.md").resolve()


def test_a_trailing_dot_cannot_walk_past_the_exclusion(tmp_path):
    """Windows strips trailing dots when it opens a path.

    "AppData." is not "appdata" to a string comparison, but it IS AppData to
    the filesystem. The file is BENIGN (notes.md, no credential segment
    anywhere in it), so the only thing that can refuse this path is the
    excluded DIRECTORY name -- if the trailing dot laundered it, the read
    would go through.

    Where it is closed: os.path.abspath in LocalFiles.resolve goes through
    GetFullPathName, which strips the trailing dot, so the LEXICAL check
    already sees "AppData". The post-resolve re-check is pinned separately by
    the 8.3 test below, the case abspath does not normalize away.
    """
    home = tmp_path / "home"
    (home / "AppData").mkdir(parents=True)
    (home / "AppData" / "notes.md").write_text("ordinary", encoding="utf-8")
    files = LocalFiles([home], opener=lambda _path: None)

    with pytest.raises(ValueError, match="excluded path"):
        files.resolve(home / "AppData" / "notes.md")
    with pytest.raises(ValueError, match="excluded path"):
        files.resolve(str(home / "AppData") + "./notes.md")


@pytest.mark.skipif(os.name != "nt", reason="8.3 short names are Windows-only")
def test_an_8_3_short_name_cannot_walk_past_the_exclusion(tmp_path):
    """"Application Data" also answers to "APPLIC~1" on an 8.3-enabled volume.

    The target file is BENIGN (notes.md): the aliased path shares no text
    with "application data" and the filename carries nothing incriminating,
    so ONLY the re-check on the resolved path (localfiles.resolve, after
    Path.resolve expands the 8.3 alias) can refuse it. Deleting that second
    _refuse_excluded call fails this test -- which it did not when the target
    was named token.json and the lexical filename rule caught it first.
    """
    import ctypes
    import ntpath

    home = tmp_path / "home"
    excluded = home / "Application Data"
    excluded.mkdir(parents=True)
    (excluded / "notes.md").write_text("ordinary", encoding="utf-8")
    buffer = ctypes.create_unicode_buffer(1024)
    ctypes.windll.kernel32.GetShortPathNameW(str(excluded), buffer, 1024)
    short = buffer.value
    if not short or short.casefold() == str(excluded).casefold():
        pytest.skip("8.3 short names are disabled on this volume")

    files = LocalFiles([home], opener=lambda _path: None)

    # GetShortPathNameW shortens every component, which would fail the lexical
    # root test for an unrelated reason. The real attack keeps the root prefix
    # long -- so it IS inside the root -- and shortens only the component the
    # exclusion names.
    alias = home / ntpath.basename(short)
    assert "applic~" in alias.name.casefold()
    assert alias.is_dir()
    # The alias shares no component text with "application data", so only the
    # post-resolve check can catch it.
    with pytest.raises(ValueError, match="excluded path"):
        files.resolve(alias / "notes.md")


def test_credential_stems_are_refused_whatever_extension_they_wear(tmp_path):
    """The extension cannot launder the name.

    .json/.toml/.yaml/.yml/.xml/.ini are all decoded by read(), with a 16KB cap
    -- far more than an OAuth token needs -- so matching only the bare name
    "credentials" would have left the canonical spellings wide open.
    """
    home = tmp_path / "home"
    project = home / "Gmail-MCP-Server"
    project.mkdir(parents=True)
    secrets = [
        project / "credentials.json",
        project / "credentials.toml",
        project / "client_secret_12345.json",
        project / "token.json",
        project / "secrets.yaml",
        project / "oauth_creds.json",
        project / "service-account.json",
        project / "id_rsa.pub",
        # The six names an equality-on-stem rule let straight through. The
        # first is the one that matters most: it is simply what Chrome calls
        # the SECOND download of credentials.json, so the bypass needed no
        # attacker at all -- downloading the same file twice was enough.
        project / "credentials (1).json",
        project / "credentials-prod.json",
        project / "tokens.json",
        project / "access_token.json",
        project / "oauth_creds_backup.json",
        project / "API_KEYS.txt",
    ]
    for path in secrets:
        path.write_text("token: hunter2", encoding="utf-8")
    keeper = project / "README.md"
    tokenizer = project / "tokenizer.py"
    # The documented near-miss. Segment-bounding is what spares it: the
    # component splits to {tokenizer, config, json} and "tokenizer" is not
    # "token", where a bare startswith("token") would have refused it.
    near_miss = project / "tokenizer_config.json"
    keyboard = project / "keyboard-shortcuts.md"
    for path in (keeper, tokenizer, near_miss, keyboard):
        path.write_text("code", encoding="utf-8")
    files = LocalFiles([{"path": str(home), "name": "home"}], opener=lambda _path: None)

    for path in secrets:
        with pytest.raises(ValueError, match="excluded path"):
            files.resolve(path)
    # Only whole SEGMENTS match, so a file that merely starts with the same
    # letters is untouched -- the rule refuses secrets, not vocabulary.
    for path in (tokenizer, keeper, near_miss, keyboard):
        assert files.resolve(path) == path.resolve()
    assert {item["path"] for item in files.find("token")} == {
        str(tokenizer.resolve()), str(near_miss.resolve()),
    }


def test_a_root_inside_a_hidden_directory_is_refused_outright(tmp_path, caplog):
    """One line of config must not be able to undo rule 1.

    A root is never exclusion-checked below -- only its contents are -- so a
    root pointing into a hidden config tree would serve exactly what the
    exclusion denies, and now through a NAME that survives the taint wall.
    """
    hidden = tmp_path / "home" / ".claude" / "projects"
    hidden.mkdir(parents=True)
    (hidden / "session.json").write_text("auth", encoding="utf-8")
    ordinary = tmp_path / "home" / "kb"
    ordinary.mkdir(parents=True)

    with caplog.at_level("WARNING", logger="atlas.localfiles"):
        files = LocalFiles(
            [{"path": str(hidden), "name": "sessions"}, ordinary],
            opener=lambda _path: None,
        )

    assert files.root_names == {"kb": ordinary.resolve()}
    assert caplog.messages == [
        f"skipping file root {hidden}: hidden directories are not roots",
    ]
    # Not merely unnameable -- not a root at all, so nothing under it resolves.
    with pytest.raises(ValueError, match="outside roots"):
        files.resolve(hidden / "session.json")


def test_ordinary_roots_under_appdata_still_work(tmp_path):
    """The root rule is the DOT rule only, deliberately.

    Legitimate roots do live under AppData -- every pytest tmp_path does -- so
    refusing _EXCLUDED_DIRECTORY_NAMES for roots as well would refuse the
    machine's own temp tree. Naming such a root is an explicit choice; ending
    up inside a hidden config tree is an accident.
    """
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "notes.md").write_text("hi", encoding="utf-8")

    files = LocalFiles([root], opener=lambda _path: None)

    assert files.resolve(root / "notes.md") == (root / "notes.md").resolve()


def test_extended_length_paths_are_compared_in_their_ordinary_form():
    """resolve() can hand back \\\\?\\C:\\... past MAX_PATH.

    _containing_root compares lexically, so that form matches no root and a
    path genuinely inside one would be refused as "outside roots" -- a wrong
    answer, and far likelier now that a whole home tree (node_modules) is in
    scope than it was under four narrow roots.
    """
    from pathlib import Path, PureWindowsPath

    stripped = localfiles._strip_extended_prefix(Path(r"\\?\C:\Users\danie\kb\a.md"))

    assert PureWindowsPath(stripped) == PureWindowsPath(r"C:\Users\danie\kb\a.md")
    # A UNC path keeps its prefix: stripping it would change which host it
    # names, and \\?\UNC\server\share is not \\server\share lexically.
    unc = Path(r"\\?\UNC\server\share\a.md")
    assert localfiles._strip_extended_prefix(unc) == unc
    ordinary = Path(r"C:\Users\danie\kb\a.md")
    assert localfiles._strip_extended_prefix(ordinary) == ordinary


def test_key_and_certificate_material_is_refused_by_extension(tmp_path):
    """Explicit, not incidental.

    None of these extensions is in _TEXT_EXTENSIONS today, so read() already
    declines them -- but that allowlist is a DECODING decision. Someone adding
    ".pem" to it to display a certificate chain would otherwise, in the same
    line, open every private key in the home tree. The deny is stated where it
    is meant, so the two decisions cannot be made by accident together.
    """
    home = tmp_path / "home"
    certs = home / "deploy"
    certs.mkdir(parents=True)
    material = [
        certs / "server.pem",
        certs / "private.key",
        certs / "bundle.p12",
        certs / "export.pfx",
        certs / "putty.ppk",
        certs / "java.jks",
        certs / "signed.asc",
        certs / "message.gpg",
        home / "_netrc",
    ]
    for path in material:
        path.write_text("-----BEGIN PRIVATE KEY-----", encoding="utf-8")
    readme = certs / "deploy-notes.md"
    readme.write_text("how to deploy", encoding="utf-8")
    files = LocalFiles([{"path": str(home), "name": "home"}], opener=lambda _path: None)

    for path in material:
        with pytest.raises(ValueError, match="excluded path"):
            files.resolve(path)
    assert files.find("private") == []
    assert files.find("netrc") == []
    assert files.resolve(readme) == readme.resolve()


def test_the_exclusion_over_matches_in_the_safe_direction(tmp_path):
    """Documented cost of segment matching, pinned so it stays deliberate.

    These are ordinary documents that the rule refuses anyway. That is the
    accepted trade: a refused document is a nuisance Daniel can work around, a
    leaked token is not recoverable. Pinned so the cost is visible in review
    rather than discovered in use.
    """
    home = tmp_path / "home"
    documents = home / "Documents"
    documents.mkdir(parents=True)
    collateral = [
        documents / "secrets-of-the-forest.md",
        documents / "key_findings.md",
        documents / "password-reset-instructions.md",
    ]
    for path in collateral:
        path.write_text("an ordinary document", encoding="utf-8")
    files = LocalFiles([home], opener=lambda _path: None)

    for path in collateral:
        with pytest.raises(ValueError, match="excluded path"):
            files.resolve(path)


def test_each_root_gets_a_fair_slice_so_the_last_one_cannot_starve(tmp_path):
    """Position must not decide whether a root is searched at all.

    Under one shared deadline the LAST root starves: once file_roots included a
    whole home tree, the earlier roots reliably spent the entire 2s budget and
    home was never scanned, so a file sitting directly in ~ was unfindable
    while find() truthfully reported nothing found.
    """
    roots = []
    for name in ("desktop", "documents", "home"):
        directory = tmp_path / name
        directory.mkdir()
        roots.append(directory)
    handed = []
    files = LocalFiles(roots, opener=lambda _path: None, clock=lambda: 0.0)
    files._scan = lambda root, _tokens, deadline, *rest: handed.append(
        (root.name, round(deadline, 3)),
    )

    files.find("anything", budget_s=2.0)

    # Each root is handed a share of what is LEFT, so no earlier root can spend
    # a later one's budget, and the last root still reaches the full deadline.
    assert handed == [
        ("desktop", round(2.0 / 3, 3)),
        ("documents", 1.0),
        ("home", 2.0),
    ]


def test_a_root_scoped_search_gets_the_whole_budget(tmp_path):
    """Slicing divides among the roots actually being scanned, not all of them."""
    for name in ("desktop", "documents", "home"):
        (tmp_path / name).mkdir()
    handed = []
    files = LocalFiles(
        [tmp_path / "desktop", tmp_path / "documents",
         {"path": str(tmp_path / "home"), "name": "home"}],
        opener=lambda _path: None,
        clock=lambda: 0.0,
    )
    files._scan = lambda root, _tokens, deadline, *rest: handed.append(
        (root.name, round(deadline, 3)),
    )

    files.find("anything", budget_s=2.0, root="home")

    assert handed == [("home", 2.0)]
