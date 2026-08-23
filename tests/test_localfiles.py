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


def test_open_uses_the_resolved_path_and_small_read_returns_full_text(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    note = root / "notes.txt"
    note.write_bytes(b"first\nsecond line tail")
    opened = []
    files = LocalFiles([root], opener=opened.append)

    assert files.open(note) == {"opened": str(note.resolve())}
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

    assert opened_result == {"opened": str(document.resolve())}
    assert read_result["text"] == "hello"
    assert opened == [str(document.resolve())]
    assert offloaded == ["open", "read"]
    assert deadlines == [5.0, 5.0]


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
