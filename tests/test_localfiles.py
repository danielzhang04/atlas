"""Root confinement and bounded local-file behavior."""
from __future__ import annotations

import os

import pytest

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


def test_open_uses_the_resolved_path_and_read_reports_truncation(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    note = root / "notes.txt"
    note.write_bytes(b"first\nsecond line tail")
    opened = []
    files = LocalFiles([root], opener=opened.append)

    assert files.open(note) == {"opened": str(note.resolve())}
    assert opened == [str(note.resolve())]
    result = files.read(note, max_bytes=12)
    assert result == {
        "path": str(note.resolve()),
        "bytes": note.stat().st_size,
        "text": "first\nsecond",
        "truncated": True,
    }


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
