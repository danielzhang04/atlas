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


def test_open_uses_the_resolved_path_and_read_returns_bounded_clean_text(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    note = root / "notes.custom"
    note.write_bytes("first\nsecond\tline\x01tail".encode("utf-8"))
    opened = []
    files = LocalFiles([root], opener=opened.append)

    assert files.open(note) == {"opened": str(note.resolve())}
    assert opened == [str(note.resolve())]
    result = files.read(note, max_bytes=12)
    assert result == {
        "path": str(note.resolve()),
        "bytes": note.stat().st_size,
        "text": "first\nsecond",
    }


def test_read_rejects_binary_files(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    binary = root / "image.bin"
    binary.write_bytes(b"prefix\x00suffix")

    with pytest.raises(ValueError, match="text file"):
        LocalFiles([root], opener=lambda _path: None).read(binary)


def test_constructor_rejects_an_empty_root_list():
    with pytest.raises(ValueError, match="root"):
        LocalFiles([], opener=lambda _path: None)
