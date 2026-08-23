"""Expose bounded local-file operations within configured roots."""
from __future__ import annotations

import os
from pathlib import Path
import time
from typing import Callable, Sequence
import unicodedata

__all__ = ["LocalFiles"]
_SKIPPED_DIRECTORIES = frozenset({".git", "node_modules", ".venv", "__pycache__"})
_MAX_DEPTH = 6
_MAX_RESULTS = 20


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
        candidate = Path(path).expanduser().resolve()
        if not candidate.exists() or not any(
            candidate == root or root in candidate.parents for root in self._roots
        ):
            raise ValueError("outside roots")
        return candidate

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
        matches: list[dict] = []
        for root in self._roots:
            if self._clock() >= deadline:
                break
            self._scan(root, needle, deadline, matches)
        matches.sort(key=lambda item: item["modified"], reverse=True)
        return matches[:maximum]

    def _scan(self, root: Path, needle: str, deadline: float,
              matches: list[dict]) -> None:
        if not root.is_dir():
            return
        pending = [(root, 0)]
        while pending and self._clock() < deadline:
            directory, depth = pending.pop()
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if self._clock() >= deadline:
                            return
                        self._visit(entry, depth, needle, pending, matches)
            except OSError:
                continue

    def _visit(self, entry, depth: int, needle: str,
               pending: list[tuple[Path, int]], matches: list[dict]) -> None:
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
        matches.append({
            "path": str(resolved), "size": stat.st_size, "modified": stat.st_mtime,
        })

    def open(self, path: str | Path) -> dict:
        resolved = self.resolve(path)
        self._opener(str(resolved))
        return {"opened": str(resolved)}

    def read(self, path: str | Path, max_bytes: int = 16_384) -> dict:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("invalid max_bytes")
        resolved = self.resolve(path)
        if not resolved.is_file():
            raise ValueError("not a text file")
        with resolved.open("rb") as stream:
            raw = stream.read(max_bytes)
        if b"\x00" in raw:
            raise ValueError("not a text file")
        text = raw.decode("utf-8", errors="replace")
        cleaned = "".join(char for char in text
                          if char in "\n\t" or unicodedata.category(char) != "Cc")
        return {
            "path": str(resolved), "bytes": resolved.stat().st_size, "text": cleaned,
        }
