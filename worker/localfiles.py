"""Expose bounded local-file operations within configured roots."""
from __future__ import annotations

import codecs
import heapq
import os
from pathlib import Path
import stat
import time
from typing import Callable, Sequence

__all__ = ["LocalFiles"]
_SKIPPED_DIRECTORIES = frozenset({".git", "node_modules", ".venv", "__pycache__"})
_MAX_DEPTH = 6
_MAX_RESULTS = 20
_OPENABLE_EXTENSIONS = frozenset({
    ".txt",
    ".md",
    ".csv",
    ".tsv",
    ".json",
    ".yaml",
    ".yml",
    ".log",
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".mp3",
    ".wav",
    ".mp4",
    ".mov",
    ".docx",
    ".xlsx",
    ".pptx",
    ".html",
    ".htm",
    ".py",
    ".ts",
    ".ipynb",
})
_TEXT_EXTENSIONS = frozenset({
    ".txt",
    ".md",
    ".csv",
    ".tsv",
    ".json",
    ".yaml",
    ".yml",
    ".log",
    ".py",
    ".js",
    ".ts",
    ".html",
    ".htm",
    ".xml",
    ".ini",
    ".toml",
    ".ipynb",
})
_UTF16_BOMS = (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class LocalFiles:
    def __init__(
        self, roots: Sequence[Path], *,
        clock: Callable[[], float] = time.monotonic,
        opener: Callable[[str], object] = os.startfile,
    ) -> None:
        if not roots:
            raise ValueError("at least one file root is required")
        self._roots = tuple(Path(root).expanduser().resolve() for root in roots)
        self._clock = clock
        self._opener = opener

    def resolve(self, path: str | Path) -> Path:
        expanded = Path(path).expanduser()
        candidate = Path(os.path.abspath(expanded))
        lexical_root = self._containing_root(candidate)
        if lexical_root is None:
            raise ValueError("outside roots")
        self._refuse_reparse_points(candidate, lexical_root)
        resolved = candidate.resolve()
        if self._containing_root(resolved) is None:
            raise ValueError("outside roots")
        return resolved

    def _containing_root(self, candidate: Path) -> Path | None:
        return next(
            (
                root
                for root in self._roots
                if candidate == root or root in candidate.parents
            ),
            None,
        )

    def _refuse_reparse_points(self, candidate: Path, root: Path) -> None:
        current = root
        for component in candidate.relative_to(root).parts:
            current /= component
            try:
                item_stat = os.lstat(current)
            except OSError as exc:
                raise ValueError("outside roots") from exc
            attributes = getattr(item_stat, "st_file_attributes", 0)
            if attributes & _REPARSE_POINT or stat.S_ISLNK(item_stat.st_mode):
                raise ValueError("reparse points are not allowed")

    def find(self, query: str, limit: int = _MAX_RESULTS,
             budget_s: float = 2.0) -> list[dict]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("invalid query")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("invalid limit")
        if (isinstance(budget_s, bool)
                or not isinstance(budget_s, (int, float)) or budget_s <= 0):
            raise ValueError("invalid budget")
        maximum = min(limit, _MAX_RESULTS)
        needle = query.strip().casefold()
        deadline = self._clock() + float(budget_s)
        matches: list[tuple[float, int, dict]] = []
        sequence = [0]
        for root in self._roots:
            if self._clock() >= deadline:
                break
            self._scan(root, needle, deadline, matches, maximum, sequence)
        newest = [item for _modified, _sequence, item in matches]
        newest.sort(key=lambda item: item["modified"], reverse=True)
        return newest

    def _scan(self, root: Path, needle: str, deadline: float,
              matches: list[tuple[float, int, dict]], maximum: int,
              sequence: list[int]) -> None:
        if not root.is_dir():
            return
        pending = [(root, 0)]
        while pending:
            if self._clock() >= deadline:
                return
            directory, depth = pending.pop()
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if self._clock() >= deadline:
                            return
                        self._visit(
                            entry,
                            depth,
                            needle,
                            pending,
                            matches,
                            maximum,
                            sequence,
                        )
            except OSError:
                continue

    def _visit(self, entry, depth: int, needle: str,
               pending: list[tuple[Path, int]],
               matches: list[tuple[float, int, dict]], maximum: int,
               sequence: list[int]) -> None:
        if entry.name.casefold() in _SKIPPED_DIRECTORIES:
            return
        try:
            if entry.is_dir(follow_symlinks=False) and depth < _MAX_DEPTH - 1:
                pending.append((self.resolve(entry.path), depth + 1))
            if needle not in entry.name.casefold():
                return
            resolved = self.resolve(entry.path)
            stat = resolved.stat()
        except (OSError, ValueError):
            return
        item = {
            "path": str(resolved),
            "size": stat.st_size,
            "modified": stat.st_mtime,
        }
        candidate = (stat.st_mtime, sequence[0], item)
        sequence[0] += 1
        if len(matches) < maximum:
            heapq.heappush(matches, candidate)
            return
        heapq.heappushpop(matches, candidate)

    def open(self, path: str | Path) -> dict:
        resolved = self.resolve(path)
        if not resolved.is_file() or resolved.suffix.casefold() not in _OPENABLE_EXTENSIONS:
            raise ValueError("not an openable document")
        self._opener(str(resolved))
        return {"opened": str(resolved)}

    def read(self, path: str | Path, max_bytes: int = 16_384) -> dict:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("invalid max_bytes")
        resolved = self.resolve(path)
        if not resolved.is_file() or resolved.suffix.casefold() not in _TEXT_EXTENSIONS:
            raise ValueError("not a text file")
        with resolved.open("rb") as stream:
            total_bytes = os.fstat(stream.fileno()).st_size
            raw = stream.read(max_bytes)
        truncated = total_bytes > len(raw)
        text = _decode_text(raw, truncated=truncated)
        return {
            "path": str(resolved),
            "bytes": total_bytes,
            "text": text,
            "truncated": truncated,
        }


def _decode_text(raw: bytes, *, truncated: bool) -> str:
    utf8_decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    try:
        return utf8_decoder.decode(raw, final=not truncated)
    except UnicodeDecodeError:
        if not raw.startswith(_UTF16_BOMS):
            raise ValueError("not a text file") from None
    utf16_decoder = codecs.getincrementaldecoder("utf-16")(errors="strict")
    try:
        return utf16_decoder.decode(raw, final=not truncated)
    except UnicodeDecodeError:
        raise ValueError("not a text file") from None
