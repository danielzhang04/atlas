"""Expose bounded local-file operations within configured roots."""
from __future__ import annotations

import asyncio
import codecs
import heapq
import logging
import os
from pathlib import Path
import re
import stat
import time
from typing import Callable, Mapping, Sequence

from .desktopapps import _launch_folder, known_folder_path

__all__ = ["LocalFiles", "resolve_file_roots", "valid_file_root"]
logger = logging.getLogger("atlas.localfiles")
_SKIPPED_DIRECTORIES = frozenset({".git", "node_modules", ".venv", "__pycache__"})
# Constitution rule 1, enforced structurally rather than by convention.
# file_roots is a READ scope, and once it contains a whole home directory the
# lexical "is it under a root" test alone would happily resolve
# ~/.claude.json (Claude auth state), ~/.ssh, ~/.aws, or the browser profiles
# and token caches under AppData -- all of which rule 1 says Atlas must never
# read. These names are refused at LocalFiles.resolve, the single choke point
# every read/open/search result passes through, so an excluded path can
# neither be opened, read, nor even appear in a find_file result, no matter
# which root nominally contains it. Widening file_roots therefore cannot
# widen credential exposure.
#
# The load-bearing rule is the DOT one below: under a root, any path component
# beginning with "." is refused. Enumerating credential filenames is a losing
# game -- .claude.json, .mcp.json, .git-credentials, .pypirc, .m2/settings.xml,
# .cargo/credentials.toml, .jupyter/*_config.json, .yarnrc.yml all hold live
# secrets, several of them in extensions LocalFiles.read decodes happily, and
# the next tool ships its own. Hidden dotfiles are configuration, not Daniel's
# documents, so refusing the whole class is both safer and simpler to audit
# than a list that must be maintained forever. These two sets then cover the
# credential locations that are NOT dot-prefixed.
_EXCLUDED_DIRECTORY_NAMES = frozenset({
    "appdata",
    "application data",
})
# Matched as delimiter-bounded SEGMENTS of the whole component, not as the
# whole stem. Equality was a hole with a name: matching the stem exactly meant
# `credentials.json` was refused while `credentials (1).json` -- the name
# Chrome gives the SECOND download of the same file -- sailed through, along
# with credentials-prod.json, tokens.json, access_token.json and
# oauth_creds_backup.json. Real secret files are named by humans and browsers,
# who suffix, prefix, pluralize and parenthesize freely, so the rule has to
# survive decoration. Splitting the component on every non-alphanumeric run and
# testing each piece does: decoration lands in its own segment and the
# incriminating word still stands alone.
#
# Segment-bounding is also what SPARES the near-misses. `tokenizer_config.json`
# splits to {tokenizer, config, json} -- "tokenizer" is not "token", so it
# stays readable, where a plain startswith("token") would have refused it. The
# rule is deliberately over-inclusive in the other direction (a note named
# `secret_santa.md` is refused), because a refused document is a nuisance and a
# leaked token is not recoverable.
_EXCLUDED_SEGMENTS = frozenset({
    "apikey",
    "apikeys",
    "creds",
    "credential",
    "credentials",
    # "key"/"keys" as standalone segments catch the very common
    # `API_KEYS.txt` / `my_key.txt` shape. They cost a false positive on a
    # document literally named `key_findings.md`; that trade is taken
    # deliberately, in the safe direction. `keyboard-shortcuts.md` is
    # untouched -- "keyboard" is its own segment and is not "key".
    "key",
    "keys",
    "keystore",
    "netrc",
    "oauth",
    "passwd",
    "password",
    "passwords",
    "secret",
    "secrets",
    "token",
    "tokens",
})
# Names whose incriminating form spans several segments, so no single segment
# carries it. Matched as a prefix of the component.
_EXCLUDED_STEM_PREFIXES = (
    "client_secret", "id_dsa", "id_ecdsa", "id_ed25519", "id_rsa",
    "service-account", "service_account",
)
# Key and certificate material. These are refused by NAME rather than left to
# the fact that _TEXT_EXTENSIONS happens not to list them: that allowlist is a
# decoding decision, and someone adding ".pem" to it one day to read a cert
# chain must not silently open every private key in the home tree with it.
_EXCLUDED_EXTENSIONS = frozenset({
    ".asc", ".gpg", ".jks", ".key", ".keystore", ".p12", ".pem", ".pfx", ".ppk",
})
# Extensionless credential files that carry no incriminating segment at all.
# (`.netrc` is already covered by the dot rule; `_netrc` is its Windows twin.)
_EXCLUDED_FILE_NAMES = frozenset({"_netrc"})
_SEGMENT_SEPARATORS = re.compile(r"[^a-z0-9]+")
_EXCLUDED_PATH = "excluded path"
_MAX_DEPTH = 6
_MAX_RESULTS = 20
_QUERY_STOP_WORDS = frozenset({
    "the",
    "a",
    "an",
    "of",
    "my",
    "file",
    "files",
    "spec",
    "document",
    "doc",
})
_NAME_SEPARATORS = re.compile(r"[-_.\s]+")
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
_CLOUD_REPARSE_MASK = 0xFFFF0000
_CLOUD_REPARSE_FAMILY = 0x90000000
_CLOUD_PLACEHOLDER_ERROR = "file not available yet (cloud placeholder)"
_FILE_DEADLINE_S = 5.0
_READ_CAP_BYTES = 16_384
_PREVIEW_CHARS = 1_024
_PREVIEW_READ_BYTES = _PREVIEW_CHARS * 4 + len(codecs.BOM_UTF16_LE)
_CHEAP_LINE_COUNT_BYTES = 1_048_576
_LARGE_FILE_NOTE = (
    "too large to read in-lane; use launch_work with this exact path for analysis"
)


def _is_cloud_files_tag(value) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value & _CLOUD_REPARSE_MASK == _CLOUD_REPARSE_FAMILY
    )


class LocalFiles:
    def __init__(
        self, roots: Sequence[Path | str], *,
        clock: Callable[[], float] = time.monotonic,
        opener: Callable[[str], object] = os.startfile,
        folder_opener: Callable[[str], object] = _launch_folder,
        known_folder_resolver: Callable[[str], Path] = known_folder_path,
    ) -> None:
        if not roots:
            raise ValueError("at least one file root is required")
        self._roots, self._folders, self._root_names = self._resolve_roots(
            roots,
            known_folder_resolver,
        )
        self._clock = clock
        self._opener = opener
        self._folder_opener = folder_opener

    @property
    def folders(self) -> Mapping[str, Path]:
        return dict(self._folders)

    @property
    def root_names(self) -> Mapping[str, Path]:
        """Casefolded root name -> the resolved directory the host owns.

        This is what makes roots nameable. The model never supplies a path
        for these: it picks one of these host-authored names, and the host
        substitutes its own already-resolved Path.
        """
        return dict(self._root_names)

    def resolve_root(self, name: object) -> Path:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("invalid root")
        resolved = self._root_names.get(name.strip().casefold())
        if resolved is None:
            raise ValueError(_unknown_root_message(self._root_names))
        return resolved

    @staticmethod
    def _resolve_roots(
        roots: Sequence[Path | str | Mapping],
        known_folder_resolver: Callable[[str], Path],
    ) -> tuple[tuple[Path, ...], dict[str, Path], dict[str, Path]]:
        resolved_roots = []
        folders = {}
        names: dict[str, Path] = {}
        for root in roots:
            # A malformed entry is a config error, not an unavailable
            # directory: it raises here rather than being skipped, so a typo
            # in atlas.yaml fails loudly at startup instead of silently
            # shrinking the read scope.
            configured, requested_name = _root_entry(root)
            folder_name = None
            try:
                if configured.startswith("known:"):
                    folder_name = configured.removeprefix("known:")
                    candidate = Path(known_folder_resolver(folder_name))
                else:
                    candidate = Path(configured).expanduser()
                resolved = candidate.resolve()
            except Exception as exc:
                logger.warning("skipping file root %s: %s", configured, exc)
                continue
            if not resolved.is_dir():
                logger.warning("skipping file root %s: directory is unavailable", configured)
                continue
            # A root is never exclusion-checked below (only its contents are),
            # so a root pointing INTO a hidden config tree would hand back
            # exactly what the exclusion exists to deny -- and, now that roots
            # are nameable, hand it back through a name that survives the taint
            # wall. One line in atlas.yaml ({path: ~/.claude/projects}) must not
            # be able to do that, so a dot-prefixed component in the root itself
            # is refused outright. Rule 1 is not a per-config opt-in.
            # Deliberately only the DOT rule, not _EXCLUDED_DIRECTORY_NAMES:
            # legitimate roots do live under AppData (every pytest tmp_path
            # does), and naming one is an explicit choice, not an accident.
            if any(part.casefold().startswith(".") for part in resolved.parts):
                logger.warning("skipping file root %s: hidden directories are not roots",
                               configured)
                continue
            resolved_roots.append(resolved)
            if folder_name:
                folders[folder_name] = resolved
            name = _root_display_name(requested_name, folder_name, resolved)
            if not name:
                continue
            if name in names:
                # First entry wins, deterministically: the enum the model sees
                # must name exactly one directory per name.
                logger.warning("duplicate file root name %s; keeping the first", name)
                continue
            names[name] = resolved
        return tuple(resolved_roots), folders, names

    def resolve(self, path: str | Path) -> Path:
        expanded = Path(path).expanduser()
        candidate = Path(os.path.abspath(expanded))
        lexical_root = self._containing_root(candidate)
        if lexical_root is None:
            raise ValueError("outside roots")
        _refuse_excluded(candidate, lexical_root)
        self._refuse_reparse_points(candidate, lexical_root)
        resolved = _strip_extended_prefix(candidate.resolve())
        resolved_root = self._containing_root(resolved)
        if resolved_root is None:
            raise ValueError("outside roots")
        # Checked AGAIN on the resolved path, not just the lexical one. The
        # lexical check reads the name the caller typed; resolve() reads the
        # name Windows really uses, and the two differ for 8.3 short names
        # ("APPDAT~1" -> "AppData"), which would otherwise walk straight past
        # the exclusion above -- pinned by
        # test_an_8_3_short_name_cannot_walk_past_the_exclusion, which fails
        # if this line goes. (Trailing dots and spaces are handled one step
        # earlier: os.path.abspath above goes through GetFullPathName, which
        # strips them, so "AppData./notes.md" is already "AppData\notes.md"
        # by the time the lexical check runs.)
        _refuse_excluded(resolved, resolved_root)
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
            is_link = stat.S_ISLNK(item_stat.st_mode)
            is_reparse = bool(attributes & _REPARSE_POINT)
            reparse_tag = getattr(item_stat, "st_reparse_tag", None)
            cloud_placeholder = is_reparse and _is_cloud_files_tag(reparse_tag)
            if is_link or (is_reparse and not cloud_placeholder):
                raise ValueError("reparse points are not allowed")

    def find(self, query: str, limit: int = _MAX_RESULTS,
             budget_s: float = 2.0, root: str | None = None) -> list[dict]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("invalid query")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("invalid limit")
        if (isinstance(budget_s, bool)
                or not isinstance(budget_s, (int, float)) or budget_s <= 0):
            raise ValueError("invalid budget")
        maximum = min(limit, _MAX_RESULTS)
        # Scoping to one root narrows a home-wide search back to a sharp one
        # ("the spec in my downloads"). The name is resolved to the host's own
        # Path here; an unknown name raises before any scanning happens.
        scanned = self._roots if root is None else (self.resolve_root(root),)
        tokens = tuple(
            token
            for token in _normalize_name(query).split()
            if token not in _QUERY_STOP_WORDS
        )
        if not tokens:
            return []
        deadline = self._clock() + float(budget_s)
        exact_matches: list[tuple[float, int, dict]] = []
        fallback_matches: list[tuple[int, float, int, dict]] = []
        sequence = [0]
        # Roots may nest (a home root alongside the Documents root inside it).
        # Without these two sets the overlap is visible to Daniel as duplicate
        # rows and is paid for twice in the time budget: `matched` dedupes
        # results, `walked` dedupes the directories the scan descends into.
        matched_paths: set[str] = set()
        walked: set[str] = set()
        # Each root gets a fair share of what is LEFT of the budget, rather
        # than the roots racing for one shared deadline. Under a single
        # deadline the LAST root is the one that starves: once file_roots
        # included a whole home tree, the earlier roots reliably spent the
        # entire 2s and home was never scanned at all, so files sitting
        # directly in ~ were intermittently unfindable while the search
        # truthfully reported nothing. Slicing makes position irrelevant --
        # which is why the config order is left alone, and why it can stay a
        # readability choice instead of a correctness one. A root that
        # finishes early donates its remainder to those after it.
        for index, scan_root in enumerate(scanned):
            now = self._clock()
            if now >= deadline:
                break
            share = (deadline - now) / (len(scanned) - index)
            if str(scan_root) in walked:
                continue
            walked.add(str(scan_root))
            self._scan(
                scan_root,
                tokens,
                now + share,
                exact_matches,
                fallback_matches,
                maximum,
                sequence,
                matched_paths,
                walked,
            )
        if exact_matches:
            results = [item for _modified, _sequence, item in exact_matches]
            results.sort(key=lambda item: item["modified"], reverse=True)
            return results
        fallback = [
            (matched, modified, item)
            for matched, modified, _sequence, item in fallback_matches
        ]
        fallback.sort(key=lambda candidate: (candidate[0], candidate[1]), reverse=True)
        return [item for _matched, _modified, item in fallback]

    def _scan(self, root: Path, tokens: tuple[str, ...], deadline: float,
              exact_matches: list[tuple[float, int, dict]],
              fallback_matches: list[tuple[int, float, int, dict]], maximum: int,
              sequence: list[int], matched_paths: set[str],
              walked: set[str]) -> None:
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
                            tokens,
                            pending,
                            exact_matches,
                            fallback_matches,
                            maximum,
                            sequence,
                            matched_paths,
                            walked,
                        )
            except OSError:
                continue

    def _visit(self, entry, depth: int, tokens: tuple[str, ...],
               pending: list[tuple[Path, int]],
               exact_matches: list[tuple[float, int, dict]],
               fallback_matches: list[tuple[int, float, int, dict]], maximum: int,
               sequence: list[int], matched_paths: set[str],
               walked: set[str]) -> None:
        if entry.name.casefold() in _SKIPPED_DIRECTORIES:
            return
        try:
            if entry.is_dir(follow_symlinks=False) and depth < _MAX_DEPTH - 1:
                child = self.resolve(entry.path)
                if str(child) not in walked:
                    walked.add(str(child))
                    pending.append((child, depth + 1))
            normalized_name = _normalize_name(entry.name)
            matched = sum(token in normalized_name for token in tokens)
            if matched < (len(tokens) + 1) // 2:
                return
            resolved = self.resolve(entry.path)
            stat = resolved.stat()
        except (OSError, ValueError):
            return
        if str(resolved) in matched_paths:
            return
        matched_paths.add(str(resolved))
        item = {
            "path": str(resolved),
            "size": stat.st_size,
            "modified": stat.st_mtime,
        }
        current_sequence = sequence[0]
        sequence[0] += 1
        if matched == len(tokens):
            candidate = (stat.st_mtime, current_sequence, item)
            if len(exact_matches) < maximum:
                heapq.heappush(exact_matches, candidate)
                return
            heapq.heappushpop(exact_matches, candidate)
            return
        candidate = (matched, stat.st_mtime, current_sequence, item)
        if len(fallback_matches) < maximum:
            heapq.heappush(fallback_matches, candidate)
            return
        heapq.heappushpop(fallback_matches, candidate)

    def open(self, path: str | Path) -> dict:
        resolved = self.resolve(path)
        if not resolved.is_file() or resolved.suffix.casefold() not in _OPENABLE_EXTENSIONS:
            raise ValueError("not an openable document")
        self._opener(str(resolved))
        return {"opened": str(resolved)}

    def open_folder(self, path: str | Path) -> dict:
        resolved = self.resolve(path)
        if not resolved.is_dir():
            raise ValueError("not a directory")
        self._folder_opener(str(resolved))
        return {"opened": str(resolved)}

    def read(self, path: str | Path, max_bytes: int = _READ_CAP_BYTES) -> dict:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("invalid max_bytes")
        resolved = self.resolve(path)
        if not resolved.is_file() or resolved.suffix.casefold() not in _TEXT_EXTENSIONS:
            raise ValueError("not a text file")
        with resolved.open("rb") as stream:
            total_bytes = os.fstat(stream.fileno()).st_size
            if total_bytes <= max_bytes:
                raw = stream.read(max_bytes)
                text = _decode_text(raw, truncated=False)
                return {
                    "path": str(resolved),
                    "bytes": total_bytes,
                    "text": text,
                    "truncated": False,
                }
            if total_bytes <= _CHEAP_LINE_COUNT_BYTES:
                raw = stream.read()
                text = _decode_text(raw, truncated=False)
                preview = text[:_PREVIEW_CHARS]
                lines = len(text.splitlines())
            else:
                raw = stream.read(_PREVIEW_READ_BYTES)
                preview = _decode_text(raw, truncated=True)[:_PREVIEW_CHARS]
                lines = None
        result = {
            "path": str(resolved),
            "bytes": total_bytes,
            "truncated": True,
            "preview": preview,
            "note": _LARGE_FILE_NOTE,
        }
        if lines is not None:
            result["lines"] = lines
        return result

    async def open_file(self, path: str | Path) -> dict:
        """Open a file off-loop, bounding a possible cloud hydration stall."""
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self.open, path),
                timeout=_FILE_DEADLINE_S,
            )
        except TimeoutError:
            return {"error": _CLOUD_PLACEHOLDER_ERROR}

    async def read_file(self, path: str | Path, max_bytes: int = _READ_CAP_BYTES) -> dict:
        """Read a file off-loop, bounding a possible cloud hydration stall."""
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self.read, path, max_bytes),
                timeout=_FILE_DEADLINE_S,
            )
        except TimeoutError:
            return {"error": _CLOUD_PLACEHOLDER_ERROR}


def resolve_file_roots(
    roots: Sequence[Path | str],
    known_folder_resolver: Callable[[str], Path] = known_folder_path,
) -> tuple[Path, ...]:
    """Resolve a file_roots list (config/atlas.yaml) into existing directory
    paths, exactly the way LocalFiles resolves its own roots (known: alias
    handling included). Public so other consumers of the same file_roots
    list -- currently the files MCP server's argv in worker/mcp_client.py --
    share this one resolver instead of growing a second one that could
    drift from it."""
    resolved, _folders, _names = LocalFiles._resolve_roots(roots, known_folder_resolver)
    return resolved


def valid_file_root(root: object) -> bool:
    """Is this a well-formed file_roots / file_write_roots entry?

    Lives here, next to the resolver, so every consumer of a roots list --
    worker/runtime.py building LocalFiles and worker/mcp_client.py expanding
    a {file_roots} argv token -- validates it identically instead of growing
    two rules that drift.
    """
    if isinstance(root, str):
        return bool(root.strip())
    if isinstance(root, Mapping):
        if set(root) - {"path", "name"} or "path" not in root:
            return False
        path = root["path"]
        name = root.get("name", "unnamed")
        return (
            isinstance(path, str) and bool(path.strip())
            and isinstance(name, str) and bool(name.strip())
        )
    return False


def _root_entry(root: Path | str | Mapping) -> tuple[str, str | None]:
    """Split one file_roots entry into its path string and explicit name.

    Two forms, the second backward compatible with the first:
      - "known:Downloads" / "C:/Users/danie/kb" -- named implicitly (the
        known-folder name, or the directory's own basename).
      - {path: "C:/Users/danie", name: "home"} -- named explicitly, for roots
        whose basename is a poor name for Daniel to say out loud.
    """
    # Path objects never come from YAML, but LocalFiles is also constructed
    # directly (tests, and any programmatic caller), so they stay accepted.
    if isinstance(root, Path):
        return str(root), None
    if not valid_file_root(root):
        raise ValueError("invalid file root")
    if isinstance(root, Mapping):
        name = root.get("name")
        return str(root["path"]).strip(), (name.strip() if isinstance(name, str) else None)
    return str(root), None


def _root_display_name(
    requested: str | None,
    folder_name: str | None,
    resolved: Path,
) -> str:
    return (requested or folder_name or resolved.name).strip().casefold()


_UNKNOWN_ROOT_NAME_LIMIT = 12
_UNKNOWN_ROOT_CHARACTER_LIMIT = 160


def _unknown_root_message(names: Mapping[str, Path]) -> str:
    listed = ", ".join(sorted(names)[:_UNKNOWN_ROOT_NAME_LIMIT])[
        :_UNKNOWN_ROOT_CHARACTER_LIMIT
    ]
    if not listed:
        return "unknown root; no named roots are configured"
    return f"unknown root; the configured roots are: {listed}"


_EXTENDED_PREFIX = "\\\\?\\"


def _strip_extended_prefix(resolved: Path) -> Path:
    """Undo the extended-length form resolve() can return.

    Past MAX_PATH, Path.resolve() hands back \\\\?\\C:\\... . _containing_root
    compares lexically, so that form matches no root and a path genuinely
    inside one is refused as "outside roots" -- a wrong answer, not just a
    confusing one. Long paths were rare under four narrow roots and are not
    rare under a whole home directory (node_modules alone).
    """
    text = str(resolved)
    if text.startswith(_EXTENDED_PREFIX) and not text.startswith(_EXTENDED_PREFIX + "UNC"):
        return Path(text[len(_EXTENDED_PREFIX):])
    return resolved


def _refuse_excluded(candidate: Path, root: Path) -> None:
    """Refuse credential-shaped components anywhere below a root.

    Only components BELOW the root are examined. A root itself is Daniel's
    explicit configuration, so pointing file_roots inside (say) AppData stays
    possible on purpose -- it just cannot happen by accident from a wide root.
    """
    for component in candidate.relative_to(root).parts:
        if _is_excluded_component(component):
            raise ValueError(_EXCLUDED_PATH)


def _is_excluded_component(component: str) -> bool:
    folded = component.casefold()
    _stem, _dot, extension = folded.rpartition(".")
    return (
        folded.startswith(".")
        or folded in _EXCLUDED_DIRECTORY_NAMES
        or folded in _EXCLUDED_FILE_NAMES
        or folded.startswith(_EXCLUDED_STEM_PREFIXES)
        or (bool(_dot) and f".{extension}" in _EXCLUDED_EXTENSIONS)
        or not _EXCLUDED_SEGMENTS.isdisjoint(_SEGMENT_SEPARATORS.split(folded))
    )


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


def _normalize_name(value: str) -> str:
    return _NAME_SEPARATORS.sub(" ", value.casefold()).strip()
