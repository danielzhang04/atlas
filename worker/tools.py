"""Register model tools and execute the built-in host capabilities."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import importlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import time
from typing import Any, Awaitable, Callable, Literal, Mapping, Protocol, Sequence
import unicodedata
from urllib.parse import urlsplit

import yaml

from worker import desktopapps
from worker.localfiles import LocalFiles

__all__ = [
    "AppEntry",
    "Handle",
    "McpToolError",
    "PendingAction",
    "Policy",
    "Tool",
    "ToolRegistry",
    "ToolResult",
    "WorkLike",
    "api_incompatible_tool_names",
    "builtin",
    "load_apps",
    "register_count_mail",
]

Policy = Literal["instant", "confirm"]
_Status = Literal["ok", "error", "needs_confirmation"]
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_CONTENT_LIMIT = 4096
# Above this serialized size the readback condenses (it never refuses; see
# ToolRegistry.call) and every value is cut to
# _READBACK_CONDENSED_VALUE_LIMIT.
_READBACK_ARGUMENT_LIMIT = 1_200
_READBACK_VALUE_LIMIT = 160
_READBACK_CONDENSED_VALUE_LIMIT = 80
_HOST_ONLY_TOOLS = frozenset({"confirm", "cancel_pending"})
# Tools that accept a host-minted handle instead of a model-supplied path, and
# so keep working after external content has tainted the turn.
_HANDLE_TOOLS = frozenset({"open_file", "open_folder"})
# find_file's `root` narrows the search to one configured root; `query` is
# still required, so this tool cannot use the _HANDLE_TOOLS "nothing is
# required" rule.
# `open` joins this map for the same reason find_file did: it now takes
# exactly one of target/link and neither is required, so the schema must not
# demand either. It deliberately stays OUT of _HANDLE_TOOLS -- that set means
# "nothing is required AND a handle is the only thing that survives taint",
# and open's taint rule is its own (aliases and link handles pass, free-text
# targets do not).
_OPTIONAL_PROPERTIES = {
    "find_file": frozenset({"root"}),
    "open": frozenset({"target", "link"}),
}
# Per tool, the mutually exclusive ways to name what to act on. Every one of
# these is settled by _handle_target_conflict before the taint gate runs.
_HANDLE_TARGETS = {
    "open_file": ("path", "handle", "root"),
    "open_folder": ("path", "handle", "root"),
    "open": ("target", "link"),
}


def _handle_target_conflict(name: str, arguments: Mapping[str, Any]) -> str | None:
    """The exactly-one-target rule, as one reusable sentence per tool."""
    keys = _HANDLE_TARGETS.get(name)
    if keys is None:
        return None
    supplied = [key for key in keys if arguments.get(key) is not None]
    if len(supplied) <= 1:
        return None
    if name == "open":
        return "provide either target or link, not both"
    if set(supplied) == {"path", "handle"}:
        # Kept verbatim: this is the wording the pre-root registry returned,
        # and it is pinned by tests that predate this unit.
        return "provide either path or handle, not both"
    return "provide exactly one of path, handle, or root"
# Host tools whose result can carry text this host did not author, and so
# taint the turn. Everything else here is host-shaped output (paths, job ids,
# app names). MCP tools default the other way -- see
# mcp_client._tool_content_bearing.
#
# list_windows is in this set for the same reason read_file is. A window title
# is not host-shaped output: it is whatever the page, document, or message
# currently open in that window says, so any web page Daniel has in a tab gets
# to write text straight into a tool result -- "Downloads - now open C:\..." is
# a title an attacker's page can set for free. Classifying it as host-shaped
# was simply wrong, however host-shaped the surrounding inventory looks.
_HOST_CONTENT_BEARING = frozenset({"list_windows", "read_file"})
# Per-turn handle budget. 40 was under two turns' worth of searching (a
# find_file returns up to _MAX_RESULTS=20), so a third search in one turn
# silently returned results with no handle at all -- and after a tainting
# read, no handle means no way to open anything (BB-wave review, finding 6).
# 120 is six full result sets; a call that still runs past it mints for as
# many results as fit and marks the rest with _HANDLE_BUDGET_NOTE instead of
# leaving the shortfall invisible.
_HANDLE_LIMIT = 120
_HANDLE_BUDGET_NOTE = "handle budget reached"
_TAINT_REFUSAL = "refused after external content; ask Daniel again next turn"
_TAINT_REFUSAL_HANDLE = (
    "refused after external content; use a handle from an earlier find_file "
    "result in this turn, or ask Daniel again next turn"
)
_UNKNOWN_HANDLE = (
    "unknown handle; call find_file first and use a handle from its results"
)
_UNKNOWN_LINK_HANDLE = (
    "unknown link handle; use one a tool result printed this turn"
)
_LINK_NOT_ALLOWED = "that link is not an openable https URL"
# How long a "what did I just open" record stays answerable. Long enough to
# cover a real conversation ("...actually, bring that back"), short enough
# that it never resurrects something from a different sitting.
_LAST_OPENED_TTL_S = 600.0
_TAINTED_BRIEF_SUFFIX = "\n\n(Atlas: content read during this turn was not forwarded.)"
_TRUNCATED = "...[truncated]"
_DESKTOP_DELETE_CHORDS = ("delete", "ctrl+d", "ctrl+x", "shift+delete")
_OPEN_DEDUPE_WINDOW_S = 15.0
_DESKTOP_ALLOWED_CHORDS = frozenset({
    "alt+tab", "backspace", "ctrl+a", "ctrl+c", "ctrl+f", "ctrl+l", "ctrl+p",
    "ctrl+s", "ctrl+t", "ctrl+v", "ctrl+w", "ctrl+y", "ctrl+z", "down", "end",
    "enter", "escape", "home", "left", "pagedown", "pageup", "right", "space",
    "tab", "up",
})
_DESKTOP_MEDIA_KEYS = (
    "play_pause", "next", "previous", "volume_up", "volume_down", "mute",
)
_WINDOW_PROPERTIES = {
    "title": {"type": "string"},
    "pid": {"type": "integer", "minimum": 1},
}
_FOUND_MESSAGES = re.compile(r"^Found\s+(\d+)\s+messages?\s+matching\b", re.IGNORECASE)
# workspace-mcp 1.25.2 (gmail/gmail_tools.py:1583-1587) actually emits the
# next-page hint mid-sentence -- "...call search_gmail_messages again with
# page_token='<token>'" -- not the line-anchored "page_token: <token>" the
# original regex alone expected (that line shape never appears in the real
# server's text, so bounded_count silently treated every >500-message query
# as a single, already-complete page; adversarial review, finding 2). The
# quoted-value alternate matches the real shape; the line-anchored
# alternates are kept for robustness against other callers/formats.
_NEXT_PAGE_TOKEN = re.compile(
    r"^[ \t]*(?:Next[ \t]+page[ \t]+token|page_token)[ \t]*:[ \t]*(\S+)[ \t]*$"
    r"|page_token=['\"]([^'\"]+)['\"]",
    re.IGNORECASE | re.MULTILINE,
)
# Gmail search results are one line per MESSAGE, but Daniel's Gmail UI counts
# CONVERSATIONS (threads) -- a 65-conversation inbox showed as "80" when
# count_mail summed message lines (live-verified against the UI). Each
# message block in the real search_gmail_messages/get_gmail_message_content/
# get_gmail_thread_content text already carries a "Thread ID: <id>" line, so
# counting DISTINCT thread ids across pages gets the UI-matching number
# without a second API surface.
_THREAD_ID = re.compile(r"^[ \t]*Thread ID[ \t]*:[ \t]*(\S+)[ \t]*$", re.IGNORECASE | re.MULTILINE)

logger = logging.getLogger("atlas.tools")


class McpToolError(RuntimeError):
    """A bounded MCP failure that is safe to return to the model."""
    def __init__(self, message: object) -> None:
        clean = _CONTROL_CHARACTERS.sub("", str(message))
        super().__init__((clean or "MCP tool call failed")[:300])


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    input_schema: dict
    run: Callable[[dict], Awaitable[Any]]
    policy: Policy = "instant"
    prepare: Callable[[dict], dict | _PreparedAction] | None = None
    execute_prepared: Callable[[dict, Any], Awaitable[Any]] | None = None
    domain: str | None = None
    # Whether this tool's result can carry content from outside Atlas, and so
    # taints the rest of the turn. Declared per tool instead of guessed from
    # the tool's NAME: the old name-shape rule ("__" in name) taints on every
    # MCP tool, including files__list_allowed_directories, whose entire output
    # is the host's own CLI argv. That misclassification turned a harmless
    # orientation call into turn-wide silence.
    #
    # None means UNDECLARED, and is the default on purpose. A `bool` default
    # would be fail-open: any Tool built outside builtin()/_mirror_tool --
    # including a `google__read` stood up ad hoc -- would silently declare
    # itself harmless and disarm the taint wall. Undeclared instead falls back
    # to the name shape (see brain._content_bearing_tool), which still taints
    # anything that looks like an MCP tool.
    content_bearing: bool | None = None
    # Consulted only when policy == "instant" (see ToolRegistry.call); this is
    # what makes "escalate can only move instant -> confirm" structurally true
    # rather than a convention callers must remember.
    escalate: Callable[[Mapping], bool] | None = None
    # Argument names this tool's confirm readback must ALWAYS name, in this
    # order, whether or not the model supplied them (see _readback_summary).
    # Consulted only on the confirm path. A readback is exact -- it lists the
    # arguments that will actually run -- but "exact" is not the same as
    # "answerable by voice": a Gmail reply sent with reply_all=True and only a
    # thread_id reads back with no recipient at all, so Daniel would be saying
    # yes to a send whose audience the readback never names. Listing "to" here
    # makes the omission itself audible ("to: (not set)") instead of
    # invisible. Declared per tool in
    # config/mcp.yaml (readback_keys:), never guessed from the schema's
    # required list -- the whole point is the keys that are OPTIONAL to the
    # remote server but load-bearing for Daniel's yes.
    readback_keys: tuple[str, ...] = ()
    # Argument names this tool REFUSES outright, before any policy branch and
    # before any readback exists. The companion to config/mcp.yaml's
    # strip_args: -- that config removes a dangerous property from the
    # mirrored schema the model sees, and this set refuses the same name if it
    # arrives anyway. Two walls for one decision: the schema is what a
    # well-behaved model reads, this is what holds when the model is being
    # driven by page text it just read. See mcp_client._tool_stripped_arguments
    # for the authoritative refusal at the session boundary, which covers
    # call_raw too.
    refused_arguments: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class _PreparedAction:
    arguments: dict[str, Any]
    host_state: Any


@dataclass(frozen=True, slots=True)
class ToolResult:
    status: _Status
    content: str
    confirm_id: str | None = None


@dataclass(frozen=True, slots=True)
class PendingAction:
    confirm_id: str
    name: str
    arguments: dict[str, Any]
    summary: str
    expires: float
    host_state: Any = None


_PENDING_COLLISION_SUMMARY_LIMIT = 120


def _pending_collision(pending: PendingAction) -> str:
    """Refuse a second confirm-tier call while one is still pending.

    The refusal used to name no action and offer no way out ("a previous action
    is still awaiting Daniel's yes or no"), so the model relayed a dead end:
    Daniel heard that something was waiting but not WHAT, and neither he nor the
    model could tell which answer would clear it. The pending's own readback
    summary is already bounded when it is built and is trimmed again here, so
    one collision cannot spend the reply on a wall of arguments.
    """
    summary = _condensed_readback(str(pending.summary))[:_PENDING_COLLISION_SUMMARY_LIMIT]
    detail = f": {summary}" if summary else ""
    return (
        f"a previous action is still awaiting Daniel's yes or no{detail}. "
        "Ask him to confirm or cancel that one first."
    )


_HandleKind = Literal["file", "folder", "link"]
# Id prefixes, per kind. Nothing depends on the letter -- resolve() looks the
# id up whole and every caller checks `kind` itself -- but a link id that
# reads like "u7" and a file id that reads like "f7" keep the transcript
# legible, and keep a model that guesses from shape guessing wrong rather
# than plausibly.
_HANDLE_PREFIXES: dict[str, str] = {"file": "f", "folder": "f", "link": "u"}


@dataclass(frozen=True, slots=True)
class Handle:
    kind: _HandleKind
    value: str


class _HandleTable:
    """Per-turn map of host-minted ids to targets this host itself validated.

    The taint wall exists because the model must never turn external content
    into an action target. Handles keep that true while still allowing
    search-then-act: the only writer is `ToolRegistry._mint_handle`, called by
    the `find_file` builtin with paths `LocalFiles` already resolved inside the
    configured roots, and by the MCP mirror with URLs it validated against a
    configured host allowlist. Nothing a model says -- and nothing an MCP
    server returns, including text that looks like "handle: f1" -- can add an
    entry, so a resolvable handle always names a target this host produced and
    checked this turn.
    Ids are not secrets: every entry is already host-validated, so guessing one
    only ever reaches another of this turn's own in-roots search results, or
    one of this turn's own allowlisted links.

    Targets are TAGGED, not bare paths: a "link" handle can never be spent as
    a file path (file_target checks kind) and a "file" handle can never be
    spent as a URL (open_target checks kind), so widening the table to URLs
    did not widen what open_file/open_folder will act on.

    Ids are minted from a counter that `clear()` deliberately does NOT reset.
    Positional per-turn ids aliased: turn 1's "f1" named one file, turn 2's
    "f1" named a different one, so a stale id the model still had in history
    silently opened the WRONG file with an ok status. Monotonic ids make that
    fail closed -- an id from any earlier turn simply is not in the table.
    """

    __slots__ = ("_entries", "_next")

    def __init__(self) -> None:
        self._entries: dict[str, Handle] = {}
        self._next = 1

    def clear(self) -> None:
        # Entries only. _next survives for the life of the registry.
        self._entries.clear()

    def mint(self, value: str, kind: str) -> str | None:
        if not isinstance(value, str) or not value or kind not in _HANDLE_PREFIXES:
            raise ValueError("invalid handle target")
        # The bound is per turn and SHARED across kinds: _entries is what
        # clear() empties, and one budget means a page full of links cannot
        # crowd out the file handles a later find_file in the same turn needs
        # (or the reverse) without the shortfall being noted at both sites.
        if len(self._entries) >= _HANDLE_LIMIT:
            return None
        handle = f"{_HANDLE_PREFIXES[kind]}{self._next}"
        self._next += 1
        self._entries[handle] = Handle(kind, value)
        return handle

    def resolve(self, handle: Any) -> Handle | None:
        if not isinstance(handle, str):
            return None
        return self._entries.get(handle)


@dataclass(frozen=True, slots=True)
class AppEntry:
    words: tuple[str, ...]
    url: str | None = None
    exe: str | None = None


@dataclass(frozen=True, slots=True)
class _OpenedRecord:
    """What the host last put on Daniel's screen, for focus_last_opened.

    Every field is written by host code from a real open that returned ok.
    `_handle` is a native HWND and stays private for the same reason every
    other handle in Atlas does (rule 12): it is never serialized into a tool
    result, never reaches the model, and is only ever spent by
    desktopcontrol.focus_resolved_window inside this process.
    """

    kind: str
    label: str
    at: float
    title: str | None = None
    pid: int | None = None
    process_path: str | None = None
    _handle: int | None = None


class _JobLike(Protocol):
    job_id: str
    title: str
    state: Any
    created_at: Any


class WorkLike(Protocol):
    def launch(self, title: str, brief: str) -> _JobLike: ...

    def active(self) -> list[_JobLike]: ...

    def recent(self, n: int) -> list[_JobLike]: ...

    def cancel(self, job_id: str) -> _JobLike: ...


class ToolRegistry:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 execution_clock: Callable[[], datetime] | None = None,
                 timeout_s: float = 8.0) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        self._clock = clock
        self._execution_clock = execution_clock or (lambda: datetime.now(timezone.utc))
        self._timeout_s = timeout_s
        self._tools: dict[str, Tool] = {}
        self._pending: PendingAction | None = None
        self._open_aliases: frozenset[str] = frozenset()
        self._root_names: frozenset[str] = frozenset()
        self._executions: dict[object, dict[str, str]] = {}
        self._execution_observer: Callable[[dict[str, str] | None], None] | None = None
        self._handles = _HandleTable()
        self._link_hosts: frozenset[str] = frozenset()
        self._last_opened: _OpenedRecord | None = None

    def begin_turn(self) -> None:
        """Reset per-turn host state. Handles live for exactly one turn.

        _last_opened deliberately SURVIVES: "bring back what you just opened"
        is asked on the turn AFTER the open, so a per-turn record would be
        empty exactly when it is needed. It is bounded by time instead
        (_LAST_OPENED_TTL_S) and holds one record, never a history.
        """
        self._handles.clear()

    def _mint_handle(self, value: str, kind: str) -> str | None:
        """Host-only: record a target this host validated, return its id."""
        return self._handles.mint(value, kind)

    def _configure_link_hosts(self, hosts: Any) -> None:
        """Host-only: the closed host vocabulary openable link handles rest on.

        Accumulated (not replaced) because each MCP server contributes its own
        `link_hosts:` list as it connects. Minting already checked the URL
        against the ONE server's list that produced it; this union is what the
        second, open-time check re-validates against, so a link handle can
        still only ever name a host Daniel configured somewhere.
        """
        names = frozenset(
            host.strip().casefold()
            for host in hosts
            if isinstance(host, str) and host.strip()
        )
        self._link_hosts |= names

    def _link_host_allowed(self, url: Any) -> bool:
        """https, no userinfo, and a configured host -- checked at both ends."""
        if not isinstance(url, str) or not url or not _direct_https(url):
            return False
        hostname = urlsplit(url).hostname
        return hostname is not None and hostname.casefold() in self._link_hosts

    def _note_opened(self, record: Mapping[str, Any]) -> None:
        """Host-only: remember the one thing this host most recently opened.

        Called by host code on the success path of a real open, never by a
        tool argument and never from anything a model or an MCP server said.
        """
        kind = record.get("kind")
        # Bounded and scrubbed like every other stored field: `label` is the
        # one part of this record that is later spoken back inside a tool
        # result, so it goes through the same treatment as a window title
        # even though today's writers only ever pass host-shaped strings.
        label = _optional_text(record.get("label"))
        if not isinstance(kind, str) or label is None:
            return
        self._last_opened = _OpenedRecord(
            kind=kind,
            label=label,
            at=self._clock(),
            title=_optional_text(record.get("title")),
            pid=_optional_positive_int(record.get("pid")),
            process_path=_optional_text(record.get("process_path")),
            _handle=_optional_positive_int(record.get("hwnd")),
        )

    def _recent_open(self) -> _OpenedRecord | None:
        record = self._last_opened
        if record is None:
            return None
        if self._clock() - record.at > _LAST_OPENED_TTL_S:
            self._last_opened = None
            return None
        return record

    def _resolve_handle(self, handle: Any) -> Handle | None:
        return self._handles.resolve(handle)

    @property
    def pending(self) -> PendingAction | None:
        pending = self._pending
        if pending is not None and pending.expires <= self._clock():
            self._pending = None
            return None
        return pending

    def register(self, tool: Tool) -> None:
        if not _TOOL_NAME.fullmatch(tool.name):
            raise ValueError("invalid tool name")
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.name}")
        if tool.policy not in ("instant", "confirm"):
            raise ValueError("invalid tool policy")
        if not isinstance(tool.input_schema, dict) or tool.input_schema.get("type") != "object":
            raise ValueError("tool input schema must describe an object")
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> bool:
        tool = self._tools.pop(name, None)
        if tool is None:
            return False
        if self._pending is not None and self._pending.name == name:
            self._pending = None
        return True

    def set_execution_observer(
        self,
        observer: Callable[[dict[str, str] | None], None] | None,
    ) -> None:
        self._execution_observer = observer

    def _publish_execution(self) -> None:
        if self._execution_observer is None:
            return
        newest = next(reversed(self._executions.values()), None)
        self._execution_observer(dict(newest) if newest is not None else None)

    def names(self) -> list[str]:
        return list(self._tools)

    def content_bearing(self, name: str) -> bool | None:
        """Whether a registered tool's output taints the turn.

        None means "no answer here" -- either the name is not registered, or
        the tool it names never declared one. Both leave the caller on its own
        fail-closed fallback.
        """
        tool = self._tools.get(name)
        return None if tool is None else tool.content_bearing

    def schemas(self) -> list[dict]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": deepcopy(tool.input_schema),
            }
            for tool in self._tools.values()
            if tool.name not in _HOST_ONLY_TOOLS
        ]

    async def call(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        tainted: bool = False,
        transcript: str | None = None,
    ) -> ToolResult:
        if name in _HOST_ONLY_TOOLS:
            return ToolResult("error", "host-only")
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult("error", "unknown tool")
        try:
            # Stripped arguments are refused FIRST, ahead of every other gate:
            # a name that was removed from the model-facing schema must never
            # reach a pending readback (where Daniel would be asked to approve
            # an argument Atlas already decided he may not be asked about) and
            # never reach the remote server. Ordering it before the
            # pending-collision check also means the refusal cannot be used to
            # probe whether an action is pending.
            if tool.refused_arguments:
                refused = sorted(tool.refused_arguments.intersection(arguments))
                if refused:
                    return ToolResult(
                        "error", f"argument not available: {', '.join(refused)}",
                    )
            if tool.policy == "confirm" and self.pending is not None:
                return ToolResult("error", _pending_collision(self.pending))
            copied = deepcopy(dict(arguments))
            # Exactly-one(path/handle/root) is settled BEFORE the taint gate,
            # because the taint gate's own reasoning depends on it: "a root-only
            # call carries no model-authored target" is only true of a call that
            # really is root-only. Deciding admission first and validating the
            # shape afterwards would have the layer that makes the claim trust a
            # check that has not run yet. It still runs again in file_target --
            # the schema cannot express the constraint (no top-level oneOf), so
            # both the gate and the executor enforce it independently.
            conflict = _handle_target_conflict(name, copied)
            if conflict is not None:
                return ToolResult("error", conflict)
            if tainted and self._refused_after_external_content(name, copied):
                return ToolResult(
                    "error",
                    _TAINT_REFUSAL_HANDLE if name in _HANDLE_TOOLS else _TAINT_REFUSAL,
                )
            if tool.prepare is not None:
                prepared = tool.prepare(copied)
                if isinstance(prepared, _PreparedAction):
                    copied = prepared.arguments
                    host_state = prepared.host_state
                else:
                    copied = prepared
                    host_state = None
            else:
                host_state = None
            if tainted and name == "launch_work":
                if not isinstance(transcript, str) or not transcript.strip():
                    return ToolResult("error", "missing turn transcript")
                copied["brief"] = f"{transcript}{_TAINTED_BRIEF_SUFFIX}"
            # escalate may only turn an instant tool into a confirm for this
            # call; a tool whose declared policy is already "confirm" never
            # reaches this branch's condition, so escalate is structurally
            # ignored for it (never a de-escalation path).
            effective_policy = tool.policy
            if effective_policy == "instant" and tool.escalate is not None:
                try:
                    escalated = bool(tool.escalate(copied))
                except Exception:
                    escalated = True  # fail closed: a broken rule still confirms
                if escalated:
                    effective_policy = "confirm"
            if effective_policy == "confirm":
                # Mirrors the declared-confirm guard above: an escalated
                # instant tool reaches this branch too, and must not clobber
                # an already-pending action just because its own policy was
                # "instant" going in (rule 5: one expiring, single-use
                # pending action).
                if self.pending is not None:
                    return ToolResult("error", _pending_collision(self.pending))
                # Large arguments CONDENSE the readback; they never refuse
                # the call (BB-wave review, finding 5). The old refusal
                # ("too large to read back; split it") was advice that
                # cannot be followed for an overwrite tool: splitting a
                # write_file into two calls does not write half a file, it
                # writes the first half and then replaces it with the
                # second. What confirmation actually needs is that Daniel
                # hears WHAT is being written WHERE and how much of it --
                # not the whole payload read aloud. So oversized arguments
                # get a bounded per-value summary (first
                # _READBACK_CONDENSED_VALUE_LIMIT characters plus the total
                # length) while `copied` -- the exact, complete arguments --
                # is what the pending action stores and what confirm()
                # later executes.
                serialized = json.dumps(copied, ensure_ascii=False)
                condensed = len(serialized) > _READBACK_ARGUMENT_LIMIT
                # Forward hazard (handles): a confirm-tier tool that stored a
                # handle in these arguments would fail at confirm time --
                # begin_turn clears the table at the START of the later turn,
                # before registry.confirm runs, so the id would no longer
                # resolve. That is fail-closed, and unreachable today:
                # open_file/open_folder are instant and never_instant reaches
                # MCP tools only. If a future tier change makes either one
                # confirm, it needs a prepare() that resolves handle -> path
                # at snapshot time (which also makes the readback human
                # -readable). "Find, then confirm-open" can never work by
                # handle alone.
                pending = PendingAction(
                    confirm_id=secrets.token_urlsafe(8),
                    name=name,
                    arguments=copied,
                    summary=_readback_summary(
                        name, copied,
                        condensed=condensed,
                        required_keys=tool.readback_keys,
                        schema_keys=_schema_keys(tool),
                    ),
                    expires=self._clock() + 120.0,
                    host_state=host_state,
                )
                self._pending = pending
                return ToolResult(
                    "needs_confirmation",
                    # NOT _bound_content: it strips every C0 control, newlines
                    # included, which would flatten the one-pair-per-line
                    # readback back into a single line and hand the field
                    # boundaries straight back to the values. The summary is
                    # already bounded per value and in total when it is built.
                    _bound_readback(
                        "NOT EXECUTED. Read every line of this summary back to Daniel and wait "
                        f"for his yes or no.\n{pending.summary}"
                    ),
                    pending.confirm_id,
                )
            return await self._execute(tool, copied, host_state=host_state)
        except Exception as exc:
            return ToolResult("error", type(exc).__name__)

    def _configure_open_aliases(self, aliases: Mapping[str, Any]) -> None:
        self._open_aliases = frozenset(aliases)

    def _configure_root_names(self, names: Any) -> None:
        """Host-only: the closed root vocabulary the taint carve-out rests on."""
        self._root_names = frozenset(names)

    def _refused_after_external_content(
        self,
        name: str,
        arguments: Mapping[str, Any],
    ) -> bool:
        if name in _HANDLE_TOOLS:
            # The only thing that survives taint here is a handle this host
            # minted earlier in THIS turn; a model-supplied path -- and an
            # unminted or expired handle -- is still refused.
            return not self._handle_only_call(name, arguments)
        if name in {
            "close",
            "focus",
            "cancel_work",
            "focus_window",
            "window_action",
            "media_key",
            "click",
            "type_text",
            "press_keys",
            "press_delete",
        }:
            return True
        if name != "open":
            return False
        link = arguments.get("link")
        if link is not None:
            # A link handle is host-minted from a URL this host already
            # validated against the configured host allowlist, so it is the
            # same shape of carve-out as a file handle: the model chose an id,
            # not a destination. That is exactly what "open my skincare guide"
            # needs -- the Drive URL only ever exists inside a tainting google
            # result, so a rule that refuses every URL after external content
            # refuses the only form the answer can take.
            entry = self._handles.resolve(link)
            return entry is None or entry.kind != "link"
        target = arguments.get("target")
        if not isinstance(target, str):
            return False
        normalized = target.strip().casefold()
        if normalized in self._open_aliases:
            return False
        return _direct_https(target.strip())

    def _handle_only_call(self, name: str, arguments: Mapping[str, Any]) -> bool:
        """True when the target came from the host, not from the model.

        Two shapes qualify. A handle this host minted earlier in THIS turn,
        and -- boss-approved for the CC3 unit -- a bare `root`.

        `root` survives taint because it is not a target the model authored:
        the schema offers a closed enum generated from the LIVE resolved
        file_roots, so the widest thing planted content can achieve is to
        pick a different one of the N directories Daniel already configured
        as readable. It cannot name a new directory, cannot traverse, and
        cannot reach anything outside the roots -- LocalFiles.resolve still
        runs on the host's own Path afterwards. That is the same carve-out
        shape as configured open aliases in _refused_after_external_content
        below: a closed, host-authored vocabulary survives taint; free text
        does not. `path` is free text, so a call carrying one is still
        refused even when a root is also present.

        The membership test is HERE rather than left to LocalFiles further
        downstream, so the sentence above is enforced by the same line that
        relies on it: an out-of-enum root is free text wearing a root's name,
        and is refused as free text. Only open_folder accepts a root at all
        (a root is a directory), so a root smuggled into open_file does not
        buy passage either.
        """
        if arguments.get("path") is not None:
            return False
        root = arguments.get("root")
        if root is not None:
            return (
                name == "open_folder"
                and isinstance(root, str)
                and root.strip().casefold() in self._root_names
            )
        return self._handles.resolve(arguments.get("handle")) is not None

    async def confirm(self, confirm_id: str) -> ToolResult:
        pending = self.pending
        if pending is None:
            return ToolResult("error", "nothing to confirm")
        if pending.confirm_id != confirm_id:
            return ToolResult("error", "nothing to confirm")
        self._pending = None
        return await self._execute(
            self._tools[pending.name], pending.arguments, host_state=pending.host_state,
        )

    def cancel_pending(self) -> ToolResult:
        self._pending = None
        return ToolResult("ok", "cancelled")

    async def _execute(
        self,
        tool: Tool,
        arguments: dict[str, Any],
        *,
        host_state: Any = None,
    ) -> ToolResult:
        started = time.perf_counter()
        call_token = object()
        self._executions[call_token] = {
            "name": tool.name,
            "since": self._execution_clock().isoformat(),
        }
        try:
            self._publish_execution()
            try:
                async with asyncio.timeout(self._timeout_s):
                    if tool.execute_prepared is not None and host_state is not None:
                        value = await tool.execute_prepared(arguments, host_state)
                    else:
                        value = await tool.run(arguments)
            except McpToolError as exc:
                result = ToolResult("error", str(exc))
            except ValueError as exc:
                # Host-authored, bounded validation text (e.g. "missing title
                # or pid"); safe to surface so the model can self-correct
                # instead of retrying blind against a bare "ValueError".
                result = ToolResult("error", _CONTROL_CHARACTERS.sub("", str(exc))[:200])
            except Exception as exc:
                result = ToolResult("error", type(exc).__name__)
            else:
                if isinstance(value, ToolResult):
                    result = ToolResult(
                        value.status, _bound_content(value.content), value.confirm_id,
                    )
                else:
                    result = ToolResult("ok", _bound_content(_serialize(value)))
        finally:
            self._executions.pop(call_token, None)
            self._publish_execution()
        from worker import traces as traces_mod
        traces_mod.record_current_tool_call(
            tool.name,
            ms=round((time.perf_counter() - started) * 1000),
            ok=result.status == "ok",
        )
        return result


def load_apps(path: Path) -> dict[str, AppEntry]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict) or not isinstance(loaded.get("apps"), dict):
        raise ValueError("apps config must contain an apps mapping")
    apps: dict[str, AppEntry] = {}
    for name, raw in loaded["apps"].items():
        if not isinstance(name, str) or not name or not isinstance(raw, dict):
            raise ValueError("invalid app entry")
        words = raw.get("words")
        url, exe = raw.get("url"), raw.get("exe")
        if (url is None and exe is None) or not isinstance(words, list) or not words:
            raise ValueError(f"invalid app entry: {name}")
        if not all(isinstance(word, str) and word.strip() for word in words):
            raise ValueError(f"invalid app words: {name}")
        if url is not None and not _configured_url(url):
            raise ValueError(f"invalid app URL: {name}")
        if exe is not None and (not isinstance(exe, str) or not exe):
            raise ValueError(f"invalid app profile: {name}")
        apps[name] = AppEntry(words=tuple(words), url=url, exe=exe)
    return apps


def _desktopcontrol() -> Any:
    return importlib.import_module("worker.desktopcontrol")


def builtin(
    registry: ToolRegistry,
    apps: Mapping[str, AppEntry],
    work: WorkLike,
    *,
    opener: Callable[[str], None] = os.startfile,
    profile_opener: Callable[[str, str | None], object] = desktopapps.open_profile,
    profile_focuser: Callable[[str], object] = desktopapps.focus_profile,
    profile_closer: Callable[[str], object] = desktopapps.close_profile,
    paired_url: Callable[[], str | None] | None = None,
    files: LocalFiles | None = None,
    desktop: Any | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    aliases = _aliases(apps)
    registry._configure_open_aliases(aliases)
    recent_web_opens: dict[str, float] = {}

    def desktop_api() -> Any:
        return desktop if desktop is not None else _desktopcontrol()

    def open_web(name: str, url: str) -> dict:
        # The stamp is written only AFTER a successful open, and a dedupe hit
        # never refreshes it. Stamping first poisoned the window: a failed
        # launch left a stamp, so "yes, try again" inside 15s answered
        # already=True without retrying, and each repeat slid the window
        # forward so the retry never came. `already` now means exactly "this
        # host really opened it, less than 15s ago".
        now = clock()
        last = recent_web_opens.get(name)
        if last is not None and now - last < _OPEN_DEDUPE_WINDOW_S:
            return {"opened": name, "via": "web", "already": True}
        opener(url)
        recent_web_opens[name] = now
        note_web_open(name)
        return {"opened": name, "via": "web"}

    def note_web_open(label: str) -> None:
        """Record a browser open as the last thing opened -- deliberately
        WITHOUT any window identity.

        The brain tells the model that focus_last_opened brings back what was
        just opened. Leaving browser opens unrecorded made that a lie in the
        worst direction: the record still held some earlier app or file, so
        "bring that back" raised a stale window and called it the thing
        Daniel had just opened.

        Recorded without title/pid/process_path on purpose. A browser open
        genuinely has no window Atlas can name: webbrowser hands the URL to
        whatever browser is registered, usually as a TAB in a window that
        already existed, so there is no new window to attribute and the
        browser's other windows are indistinguishable from it. Rather than
        invent an identity, the record carries none -- and focus_last_opened
        reads that as "the last open is not refocusable" and says so.
        """
        registry._note_opened({"kind": "web", "label": label})

    def note_app_open(app_id: str, name: str, opened: Any) -> None:
        """Record a desktop-app open so focus_last_opened can find it again."""
        if not isinstance(opened, Mapping):
            return
        registry._note_opened({
            "kind": "app",
            "label": name,
            "pid": opened.get("pid"),
            # Resolved host-side from the allowlisted profile id: the process
            # path is the identity that survives a launcher pid exiting, and
            # it never travels in the tool result the model sees.
            "process_path": desktopapps.profile_executable_path(app_id),
        })

    async def open_target(arguments: dict) -> ToolResult | dict:
        link = arguments.get("link")
        if link is not None:
            # Gate-and-executor double check (the file-handle precedent): the
            # taint gate already resolved this id, and the URL was validated
            # at MINT time against the configured link hosts. Both checks run
            # again here, at the moment of the actual open, so the executor
            # never trusts a decision made by the layer above it.
            entry = registry._resolve_handle(link)
            if entry is None or entry.kind != "link":
                return ToolResult("error", _UNKNOWN_LINK_HANDLE)
            url = entry.value
            if not registry._link_host_allowed(url):
                return ToolResult("error", _LINK_NOT_ALLOWED)
            opener(url)
            note_web_open(url)
            return {"opened": url, "via": "link"}
        target = _text_argument(arguments, "target", maximum=2048)
        entry = aliases.get(target.casefold())
        if entry is not None:
            name, app = entry
            if app.exe is not None:
                try:
                    # Off-thread for the same reason as open_file: the
                    # launcher now polls for its window before returning.
                    opened = await asyncio.to_thread(profile_opener, app.exe, None)
                except desktopapps.DesktopAppError:
                    if app.url is None:
                        raise
                    return open_web(name, app.url)
                else:
                    note_app_open(app.exe, name, opened)
                    if (
                        isinstance(opened, Mapping)
                        and opened.get("focused") is True
                        and opened.get("existing") is True
                    ):
                        return ToolResult("ok", "focused existing window")
                    return {"opened": name, "via": "desktop"}
            elif app.url is not None:
                dynamic_url = paired_url() if name == "atlas" and paired_url is not None else None
                return open_web(name, dynamic_url or app.url)
            else:
                return ToolResult("error", "unknown app")
        if _direct_https(target):
            opener(target)
            note_web_open(target)
            return {"opened": target}
        return ToolResult("error", "unknown app")

    async def focus_last_opened(_: dict) -> ToolResult | dict:
        # ZERO arguments, deliberately, and that is the whole security
        # argument for this tool: there is no property for a model to fill,
        # so there is nothing planted external content can steer. It acts
        # only on a record HOST code wrote from its own successful open. It
        # therefore survives taint by construction rather than by carve-out,
        # which is why it appears in no taint list below.
        record = registry._recent_open()
        if record is None:
            return ToolResult("error", "nothing recently opened")
        if (
            record._handle is None
            and record.pid is None
            and record.process_path is None
        ):
            # The last open carries no window identity at all -- a browser
            # open, which usually became a tab in a window that already
            # existed. Refusing here is the point: without it this fell
            # through to the older record underneath and confidently raised
            # something Daniel did not just open.
            return ToolResult(
                "error",
                f"{record.label} opened in the browser -- there is no Atlas "
                f"window to bring back",
            )
        api = desktop_api()
        # Live hwnd -> pid -> process path. Each step is skipped when the
        # record carries nothing for it, and a step that fails falls through
        # to the next rather than ending the call: a window that has since
        # been closed and reopened keeps the same process path but neither
        # its old handle nor (for a document) any pid Atlas ever knew.
        # Broad except: every step here is a native call whose failure modes
        # (window gone, ambiguous match, control unavailable) are all "try
        # the next identity", and none of them should end the turn.
        if (
            record._handle is not None
            and record.title is not None
            and record.pid is not None
        ):
            try:
                api.focus_resolved_window({
                    "_handle": record._handle,
                    "title": record.title,
                    "pid": record.pid,
                })
            except Exception:
                pass
            else:
                return {"focused": record.label}
        if record.pid is not None:
            try:
                api.focus_window(pid=record.pid)
            except Exception:
                pass
            else:
                return {"focused": record.label}
        if record.process_path is not None:
            # The WHOLE list, not the first match, and this is the ordinary
            # path rather than a corner: note_app_open stores only a pid and
            # a process path, and the pid it stores is the launcher's, which
            # for most apps has already exited by the time this runs. So a
            # first-match lookup here was Atlas routinely picking an
            # arbitrary window of a multi-window app and calling it the one
            # Daniel opened.
            try:
                windows = api.windows_by_process_path(record.process_path)
            except Exception:
                windows = []
            if len(windows) > 1:
                # Honest refusal. `label` is host-shaped; the candidates'
                # titles are page text and are deliberately not listed.
                return ToolResult(
                    "error",
                    f"{record.label} has more than one window open and Atlas "
                    f"cannot tell which one it opened",
                )
            if windows:
                try:
                    api.focus_resolved_window(windows[0])
                except Exception:
                    pass
                else:
                    return {"focused": record.label}
        # Honest failure, not a swallowed one: the record exists, the window
        # does not. `label` is host-shaped (a path, an app name Atlas
        # configured) -- never the window's own title, which is page text.
        return ToolResult("error", f"{record.label} is no longer open")

    async def focus(arguments: dict) -> ToolResult | dict:
        target = _text_argument(arguments, "app", maximum=256)
        entry = aliases.get(target.casefold())
        if entry is None or entry[1].exe is None:
            return ToolResult("error", "unknown app")
        name, app = entry
        profile_focuser(app.exe)
        return {"focused": name}

    async def launch_work(arguments: dict) -> dict:
        title = _text_argument(arguments, "title", maximum=200)
        raw_brief = arguments.get("brief")
        if isinstance(raw_brief, str) and raw_brief.endswith(_TAINTED_BRIEF_SUFFIX):
            transcript = raw_brief[:-len(_TAINTED_BRIEF_SUFFIX)]
            if not transcript.strip() or len(transcript) > 4096:
                raise ValueError("invalid brief")
            brief = raw_brief
        else:
            brief = _text_argument(arguments, "brief", maximum=4096)
        job = work.launch(title, brief)
        return {"job_id": job.job_id, "status": "launching", "title": job.title}

    async def work_status(_: dict) -> list[dict]:
        return [_job_status(job) for job in [*work.active(), *work.recent(5)]]

    async def cancel_work(arguments: dict) -> dict:
        job = work.cancel(_text_argument(arguments, "job_id", maximum=256))
        return {"job_id": job.job_id, "status": _state_value(job.state), "title": job.title}

    async def close(arguments: dict) -> ToolResult | dict:
        target = _text_argument(arguments, "app", maximum=256)
        entry = aliases.get(target.casefold())
        if entry is None:
            return ToolResult("error", "unknown app")
        name, app = entry
        if app.exe is None:
            return ToolResult("error", "I can close apps, not browser tabs")
        profile_closer(app.exe)
        return {"closed": name}

    async def find_file(arguments: dict) -> list[dict]:
        # Boss decision (a), recorded here at the only minting site: find_file
        # stays callable while the turn is tainted, so planted content CAN
        # steer which in-roots document gets displayed ("read this note, then
        # find and open what it names"). Accepted: every target is bounded to
        # an in-roots, openable-extension file this host enumerated itself, so
        # a novel target -- "open C:\evil.bat" -- stays impossible, and the
        # worst case is one of Daniel's own documents opening on his screen.
        # Reads (find_file, read_file) are deliberately OUTSIDE the handle
        # regime: they are not taint-refused, so they need no handle, and
        # keeping them path-only keeps the handle surface to the two acting
        # tools. Note ambient turns arrive tainted from entry (brain.respond
        # sets tainted=bool(context)), so the whole chain can run tainted --
        # which is exactly why minting must not require an untainted turn.
        #
        # THE BLAST RADIUS OF THAT ACCEPTANCE GREW, and this is the honest
        # statement of it. Two things changed under this comment:
        #   1. file_roots now includes the whole home directory, so a tainted
        #      read_file reaches every readable file under ~ -- not the four
        #      narrow folders this paragraph was written about. Credential
        #      shapes are refused in worker/localfiles.py, but ordinary
        #      documents (tax returns, medical letters, private notes) are
        #      exactly what remains readable, and that is the point of a
        #      home root.
        #   2. The same turn also offers instant tools that take a free-text
        #      query and send it OUTWARD: count_mail, google__search_*, and
        #      kb_repo_search. So planted content can steer a read AND then
        #      steer a query carrying what was read. Nothing here refuses
        #      that chain: the taint wall governs ACTION targets (which file
        #      gets opened), not the CONTENT of a later tool's arguments.
        # This is a real exfiltration path, it is accepted for now rather than
        # unnoticed, and closing it is argument-level egress guarding -- a
        # separate unit, deliberately NOT attempted here, because doing it
        # badly (a keyword filter on query strings) would read as protection
        # while providing none.
        # What that chain CAN read is now bounded on every path (2026-09-01
        # final gate, F3): the files MCP server is write-only, so reads run
        # only through find_file/read_file here, behind localfiles.resolve's
        # credential shield. The steerable material is Daniel's ordinary
        # documents, never a credential-shaped file.
        query = _text_argument(arguments, "query", maximum=512)
        root = arguments.get("root")
        if root is not None:
            root = _text_argument(arguments, "root", maximum=128)
            # Validated on the loop thread so an unknown name comes back as a
            # clean, immediate error instead of costing a worker thread and a
            # whole scan budget first.
            files.resolve_root(root)
        # Kinds are stat'ed on the same worker thread as the scan; minting then
        # happens back on the loop thread so the registry stays single-threaded.
        found = await asyncio.to_thread(_matches_with_kind, files, query, root)
        results = []
        for item, kind in found:
            handle = registry._mint_handle(item["path"], kind)
            # Minting is per turn and bounded (_HANDLE_LIMIT). Past the
            # bound the result is still returned -- it is a real, in-roots
            # match -- but it carries note: "handle budget reached" so the
            # model can see WHY that row has no handle and say so, instead
            # of the shortfall being silent and the model concluding the
            # file cannot be opened for some unstated reason.
            results.append(
                {**item, "kind": kind, "note": _HANDLE_BUDGET_NOTE} if handle is None
                else {**item, "kind": kind, "handle": handle}
            )
        return results

    def file_target(arguments: dict, expected: Literal["file", "folder"]) -> str:
        handle = arguments.get("handle")
        root = arguments.get("root")
        if root is not None:
            # Exactly one of path/handle/root, enforced here rather than in the
            # schema: a top-level oneOf is rejected by the Messages API
            # (api_incompatible_tool_names), so the schema marks nothing
            # required and the host does the choosing -- the C1 precedent.
            if handle is not None or arguments.get("path") is not None:
                raise ValueError("provide exactly one of path, handle, or root")
            if expected != "folder":
                raise ValueError("root names a folder; use open_folder")
            # The model chose a NAME; the host substitutes the Path it already
            # resolved at startup. LocalFiles.resolve still runs downstream, so
            # a root that has since been removed or replaced by a link is
            # refused exactly like any other target.
            return str(files.resolve_root(root))
        if handle is None:
            return _text_argument(arguments, "path", maximum=2048)
        if arguments.get("path") is not None:
            raise ValueError("provide either path or handle, not both")
        entry = registry._resolve_handle(handle)
        if entry is None:
            raise ValueError(_UNKNOWN_HANDLE)
        if entry.kind == "link":
            # A URL is not a path. Spending a link handle here would hand
            # LocalFiles.resolve a string it would reject anyway, but saying
            # so plainly is what keeps the two handle kinds from blurring.
            raise ValueError("that handle is a link; pass it to open as link")
        if entry.kind == expected:
            return entry.value
        if expected == "file":
            raise ValueError("that handle is a folder; use open_folder")
        # "open the folder that file is in": the parent is derived by the host
        # from an already-validated path, never supplied by the model, and
        # LocalFiles re-checks confinement before opening it.
        return str(Path(entry.value).parent)

    # LocalFiles' own off-loop, deadline-bounded wrappers, not the raw sync
    # ones these used to call. Opening now waits (bounded) for the window it
    # produced so it can focus it, and blocking the event loop for that long
    # would stall speech, status, and every other turn in flight.
    async def open_file(arguments: dict) -> dict:
        return await files.open_file(file_target(arguments, "file"))

    async def open_folder(arguments: dict) -> dict:
        return await files.open_directory(file_target(arguments, "folder"))

    async def read_file(arguments: dict) -> dict:
        path = _text_argument(arguments, "path", maximum=2048)
        return await files.read_file(path)

    async def list_desktop_windows(arguments: dict) -> dict[str, Any]:
        values = _desktop_arguments(arguments, integers=("limit",))
        limit = values.get("limit", 40)
        if not 1 <= limit <= 100:
            raise ValueError("invalid limit")
        inventory = desktop_api().list_windows(limit=limit)
        return _bounded_window_inventory(inventory)

    async def focus_desktop_window(arguments: dict) -> dict:
        return desktop_api().focus_window(**_desktop_arguments(
            arguments, window="required",
        ))

    async def desktop_window_action(arguments: dict) -> dict:
        values = _desktop_arguments(
            arguments,
            ("action",), integers=("x", "y", "width", "height"), window="required",
        )
        action = _text_argument(arguments, "action", maximum=64).casefold()
        return desktop_api().window_action(action, **values)

    async def desktop_media_key(arguments: dict) -> dict:
        _desktop_arguments(arguments, ("key",))
        return desktop_api().media_key(
            _text_argument(arguments, "key", maximum=32).casefold(),
        )

    async def desktop_click(arguments: dict) -> dict:
        values = _desktop_arguments(
            arguments, integers=("x", "y"), required=("x", "y"), window="optional",
        )
        return desktop_api().click(
            values.pop("x"), values.pop("y"), **values,
        )

    async def desktop_type_text(arguments: dict) -> dict:
        _desktop_arguments(arguments, ("text",))
        text = arguments.get("text")
        if not isinstance(text, str) or not text or len(text) > 4000:
            raise ValueError("invalid text")
        return desktop_api().type_text(text)

    async def desktop_press_keys(arguments: dict) -> ToolResult | dict:
        _desktop_arguments(arguments, ("chord",))
        api = desktop_api()
        chord = api.normalize_chord(arguments.get("chord"))
        if chord in _DESKTOP_DELETE_CHORDS:
            return ToolResult("error", "delete chords require press_delete")
        return api.press_keys(chord)

    def prepare_delete(arguments: dict) -> _PreparedAction:
        _desktop_arguments(arguments, ("chord",))
        api = desktop_api()
        chord = api.normalize_chord(arguments.get("chord"))
        if chord not in _DESKTOP_DELETE_CHORDS:
            raise ValueError("invalid delete chord")
        identity = api.focused_window_identity()
        title = identity.get("title")
        pid = identity.get("pid")
        hwnd = identity.get("_handle")
        if (
            not isinstance(title, str)
            or not title
            or len(title) > 512
            or isinstance(pid, bool)
            or not isinstance(pid, int)
            or pid <= 0
            or isinstance(hwnd, bool)
            or not isinstance(hwnd, int)
            or hwnd <= 0
        ):
            raise ValueError("invalid foreground window identity")
        return _PreparedAction(
            arguments={"chord": chord, "window": title, "pid": pid},
            host_state=hwnd,
        )

    async def desktop_press_delete(arguments: dict) -> ToolResult:
        return ToolResult("error", "delete confirmation state is unavailable")

    async def execute_desktop_press_delete(
        arguments: dict,
        expected_hwnd: Any,
    ) -> ToolResult | dict:
        _desktop_arguments(arguments, ("chord", "window", "pid"))
        try:
            return desktop_api().press_delete(
                arguments["chord"], expected_hwnd=expected_hwnd,
            )
        except Exception:
            return ToolResult("error", "focused window changed; delete not executed")

    definitions = (
        ("open", _open_description(aliases), {
            "target": {"type": "string"}, "link": {"type": "string"},
        }, open_target),
        ("focus", "Focus an allowlisted desktop app.", {"app": {"type": "string"}}, focus),
        (
            "focus_last_opened",
            "Bring the thing Atlas most recently opened back to the front. Takes no "
            "arguments -- never call list_windows first.",
            {}, focus_last_opened,
        ),
        ("launch_work", "Launch longer work in the background.", {
            "title": {"type": "string"}, "brief": {"type": "string"},
        }, launch_work),
        ("work_status", "List active and recent work.", {}, work_status),
        ("cancel_work", "Cancel a background job.", {"job_id": {"type": "string"}}, cancel_work),
        ("close", "Gracefully close every window of an allowlisted desktop app.", {
            "app": {"type": "string"},
        }, close),
    )
    if files is not None:
        # getattr, not attribute access: `files` is a duck type here (the
        # confined-service seam), and a stand-in without named roots should
        # lose the `root` argument, not fail to register the file tools.
        # Host-to-host wiring: LocalFiles hands the registry the private
        # window record for each successful open so focus_last_opened can
        # find it again. getattr for the same duck-type reason as below -- a
        # stand-in without the hook simply records nothing.
        set_observer = getattr(files, "set_open_observer", None)
        if callable(set_observer):
            set_observer(registry._note_opened)
        root_names = sorted(getattr(files, "root_names", {}))
        # The registry needs the same closed vocabulary the schema enum
        # publishes, so the taint carve-out can check membership itself.
        registry._configure_root_names(root_names)
        listed = _root_list(root_names)
        # An empty enum is not a valid schema, so a LocalFiles whose roots all
        # failed to resolve simply offers no `root` property -- path/handle
        # still work, and the descriptions omit the roots sentence.
        root_property = (
            {"root": {"type": "string", "enum": root_names}} if root_names else {}
        )
        definitions += (
            ("find_file", (
                "Find files and folders under configured roots. Every match carries a "
                "handle you can pass to open_file or open_folder for the rest of this turn."
                f"{_roots_sentence(listed)}"
                + (
                    " Pass root to search just that one. Searches are time-bounded, so a "
                    "search across a large root can be partial."
                    if root_names else ""
                )
            ), {
                "query": {"type": "string"}, **root_property,
            }, find_file),
            ("open_file", (
                "Open an inert document or media file under configured roots. Handles come "
                "from find_file: pass handle whenever this turn's find_file returned one; "
                "pass path only without a handle -- never both. After any tool that returns "
                "outside content, only a handle works."
            ), {
                "path": {"type": "string"}, "handle": {"type": "string"},
            }, open_file),
            ("open_folder", (
                "Open a directory under configured roots in Explorer. To open a root itself, "
                "pass root -- that is the way to answer \"open my downloads\", and it keeps "
                "working after a tool has returned outside content."
                f"{_roots_sentence(listed)} Otherwise pass handle, which find_file returns "
                "for every match -- a file's handle opens the folder containing it. Pass "
                "path only without a handle or root, never more than one of the three."
            ), {
                "path": {"type": "string"}, "handle": {"type": "string"}, **root_property,
            }, open_folder),
            ("read_file", "Read small text or route previewed large-file analysis to launch_work.", {
                "path": {"type": "string"},
            }, read_file),
        )
    for name, description, properties, run in definitions:
        # Handle tools take exactly one of path/handle/root, so none of them is
        # required by the schema; the host enforces the choice (a top-level
        # oneOf is not accepted by the Messages API --
        # api_incompatible_tool_names).
        optional = _OPTIONAL_PROPERTIES.get(name, frozenset())
        schema = {
            "type": "object", "properties": properties,
            "required": [] if name in _HANDLE_TOOLS else [
                key for key in properties if key not in optional
            ],
            "additionalProperties": False,
        }
        registry.register(Tool(
            name, description, schema, run,
            content_bearing=name in _HOST_CONTENT_BEARING,
        ))

    desktop_definitions = (
        (
            "list_windows", "List visible top-level windows without exposing native handles.",
            _desktop_schema({"limit": {"type": "integer", "minimum": 1, "maximum": 100}}),
            list_desktop_windows,
        ),
        (
            "focus_window",
            "Focus one host-resolved visible window by title or pid. "
            "Provide exactly one of title or pid, never both.",
            _desktop_schema(window="required"), focus_desktop_window,
        ),
        (
            "window_action",
            "Minimize, maximize, restore, close, move, or resize a host-resolved window. "
            "Provide exactly one of title or pid, never both.",
            _desktop_schema({
                "action": {
                    "type": "string",
                    "enum": [
                        "minimize", "maximize", "restore", "close", "move", "resize",
                        "move:left-half", "move:right-half", "move:center",
                    ],
                },
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "width": {"type": "integer", "minimum": 1},
                "height": {"type": "integer", "minimum": 1},
            }, required=("action",), window="required"),
            desktop_window_action,
        ),
        (
            "media_key", "Press one allowlisted media key.",
            _desktop_schema(
                {"key": {"type": "string", "enum": list(_DESKTOP_MEDIA_KEYS)}},
                required=("key",),
            ), desktop_media_key,
        ),
        (
            "click", "Click screen coordinates, or coordinates relative to a host-resolved window.",
            _desktop_schema(
                {"x": {"type": "integer"}, "y": {"type": "integer"}},
                required=("x", "y"), window="optional",
            ), desktop_click,
        ),
        (
            "type_text", "Type Unicode text into the foreground application.",
            _desktop_schema(
                {"text": {"type": "string", "minLength": 1, "maxLength": 4000}},
                required=("text",),
            ), desktop_type_text,
        ),
        (
            "press_keys", "Press one allowlisted non-delete key chord in the foreground application.",
            _desktop_schema(
                {"chord": {"type": "string", "enum": sorted(_DESKTOP_ALLOWED_CHORDS)}},
                required=("chord",),
            ), desktop_press_keys,
        ),
    )
    for name, description, schema, run in desktop_definitions:
        registry.register(Tool(
            name, description, schema, run,
            content_bearing=name in _HOST_CONTENT_BEARING,
        ))

    delete_schema = {
        "type": "object",
        "properties": {
            "chord": {"type": "string", "enum": list(_DESKTOP_DELETE_CHORDS)},
        },
        "required": ["chord"],
        "additionalProperties": False,
    }
    registry.register(Tool(
        "press_delete",
        "Press a delete chord in the foreground application after confirmation.",
        delete_schema,
        desktop_press_delete,
        policy="confirm",
        prepare=prepare_delete,
        execute_prepared=execute_desktop_press_delete,
        content_bearing="press_delete" in _HOST_CONTENT_BEARING,
    ))


def register_count_mail(
    registry: ToolRegistry,
    search: Callable[[dict], Awaitable[str]],
) -> None:
    """Register an exact, bounded counter over Gmail search result pages."""

    async def count_mail(arguments: dict) -> ToolResult | dict:
        query = _text_argument(arguments, "query", maximum=1024)

        async def bounded_count(target_query: str) -> tuple[int, bool] | ToolResult:
            threads: set[str] = set()
            page_token = None
            seen_tokens: set[str] = set()
            for _page in range(4):
                search_arguments = {
                    "query": target_query,
                    "page_size": 500,
                    "include_headers": False,
                }
                if page_token is not None:
                    search_arguments["page_token"] = page_token
                try:
                    content = await search(search_arguments)
                except RuntimeError as exc:
                    if str(exc) == "google not connected":
                        return ToolResult("error", "Google isn't connected yet")
                    raise
                found = _FOUND_MESSAGES.search(content)
                if found is None:
                    return ToolResult("error", "unexpected mail search result")
                page_count = int(found.group(1))
                if page_count > 500:
                    return ToolResult("error", "unexpected mail search result")
                page_threads = _THREAD_ID.findall(content)
                # Fail closed: a page that reports messages but carries no
                # Thread ID lines is not the shape count_mail was built
                # against (see the module-level _THREAD_ID comment) -- silently
                # falling back to the message count would reintroduce the
                # UI mismatch this rewrite exists to fix.
                if page_count > 0 and not page_threads:
                    return ToolResult("error", "unexpected mail search result")
                threads.update(page_threads)
                token_match = _NEXT_PAGE_TOKEN.search(content)
                page_token = (
                    (token_match.group(1) or token_match.group(2))
                    if token_match is not None else None
                )
                if page_token is None:
                    return len(threads), True
                if page_count < 500:
                    return ToolResult("error", "unexpected mail search result")
                if page_token in seen_tokens:
                    return len(threads), False
                seen_tokens.add(page_token)
            return len(threads), page_token is None

        inbox_count = await bounded_count(query)
        if isinstance(inbox_count, ToolResult):
            return inbox_count
        if re.search(r"(?:^|\s)in:inbox(?:\s|$)", query, re.IGNORECASE):
            primary_query = query
            if not re.search(r"(?:^|\s)category:primary(?:\s|$)", query, re.IGNORECASE):
                primary_query += " category:primary"
            primary_count = await bounded_count(primary_query)
            if isinstance(primary_count, ToolResult):
                return primary_count
            inbox_total, inbox_exact = inbox_count
            primary_total, primary_exact = primary_count
            inbox_text = str(inbox_total) if inbox_exact else f"at least {inbox_total}"
            primary_text = str(primary_total) if primary_exact else f"at least {primary_total}"
            return ToolResult(
                "ok",
                f"{inbox_text} conversations in your inbox, {primary_text} in Primary",
            )
        total, exact = inbox_count
        return {"query": query, "conversations": total, "exact": exact}

    schema = {
        "type": "object", "properties": {"query": {"type": "string"}},
        "required": ["query"], "additionalProperties": False,
    }
    registry.register(Tool(
        "count_mail",
        "Count Gmail conversations exactly across bounded search pages, matching "
        "the count Daniel's Gmail UI shows (not a raw message count).",
        schema, count_mail,
    ))


def _aliases(apps: Mapping[str, AppEntry]) -> dict[str, tuple[str, AppEntry]]:
    aliases: dict[str, tuple[str, AppEntry]] = {}
    for name, app in apps.items():
        for word in app.words:
            folded = word.casefold()
            if folded in aliases:
                raise ValueError(f"duplicate app alias: {word}")
            aliases[folded] = (name, app)
    return aliases


_OPEN_DESCRIPTION_NAME_LIMIT = 60
# Alias names are host-configured but unbounded in LENGTH, so the count cap
# alone does not bound the schema text. Truncate the joined list too, always
# at a whole name (deterministic for the same config, and never a half name).
_OPEN_DESCRIPTION_CHARACTER_LIMIT = 600


def _open_description(aliases: Mapping[str, tuple[str, AppEntry]]) -> str:
    names = sorted({name for name, _ in aliases.values()})
    kept = names[:_OPEN_DESCRIPTION_NAME_LIMIT]
    while kept and len(", ".join(kept)) > _OPEN_DESCRIPTION_CHARACTER_LIMIT:
        kept.pop()
    listed = ", ".join(kept)
    if len(names) > len(kept):
        listed += ", ..." if listed else "..."
    link_sentence = (
        " To open a link a tool result printed, pass link with that result's handle "
        "instead of target; never paste the URL itself."
    )
    if not listed:
        return f"Open an allowlisted app or HTTPS URL.{link_sentence}"
    # The alias list stays LAST: it is the one unbounded part of this
    # description, and the schema test that bounds it reads everything after
    # the colon. A sentence appended after the list would land inside what
    # that test measures.
    return (
        f"Open an allowlisted app or HTTPS URL.{link_sentence} Aliases open the real "
        f"desktop app when configured: {listed}."
    )


_ROOT_DESCRIPTION_NAME_LIMIT = 12
# Root names are host-configured but unbounded in LENGTH, so the count cap
# alone does not bound the schema text -- same reasoning, and same
# whole-name truncation, as _open_description above.
_ROOT_DESCRIPTION_CHARACTER_LIMIT = 200


def _root_list(names: Sequence[str]) -> str:
    kept = list(names[:_ROOT_DESCRIPTION_NAME_LIMIT])
    while kept and len(", ".join(kept)) > _ROOT_DESCRIPTION_CHARACTER_LIMIT:
        kept.pop()
    listed = ", ".join(kept)
    if len(names) > len(kept):
        listed += ", ..." if listed else "..."
    return listed


def _roots_sentence(listed: str) -> str:
    """Name the roots in the schema text.

    Without this the model has no way to learn the roots exist short of
    calling a tool to discover them, which is how "I have no local kb folder"
    got said about a configured root.
    """
    return f" Roots: {listed}." if listed else ""


def _matches_with_kind(
    files: LocalFiles,
    query: str,
    root: str | None = None,
) -> list[tuple[dict, str]]:
    """Pair each host-produced match with the kind the host stats itself."""
    matches: list[tuple[dict, str]] = []
    # Unscoped searches keep the one-argument call: `files` is a duck type,
    # and only a scoped search needs the newer signature.
    found = files.find(query) if root is None else files.find(query, root=root)
    for item in found:
        if not isinstance(item, Mapping):
            continue
        path = item.get("path")
        if not isinstance(path, str) or not path:
            continue
        try:
            kind = "folder" if Path(path).is_dir() else "file"
        except OSError:
            kind = "file"
        matches.append((dict(item), kind))
    return matches


def _text_argument(arguments: Mapping[str, Any], name: str, *, maximum: int) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"invalid {name}")
    return value.strip()


def _desktop_arguments(
    arguments: Mapping[str, Any], fields: tuple[str, ...] = (), *,
    integers: tuple[str, ...] = (),
    required: tuple[str, ...] = (),
    window: Literal["required", "optional"] | None = None,
) -> dict[str, Any]:
    allowed = {*fields, *integers}
    if window:
        allowed.update(("title", "pid"))
    if set(arguments) - allowed:
        raise ValueError("unexpected argument")
    values = {}
    for name in integers:
        value = arguments.get(name)
        if name in required or name in arguments:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"invalid {name}")
            values[name] = value
    if window is None:
        return values
    has_title = "title" in arguments
    has_pid = "pid" in arguments
    if has_title and has_pid:
        raise ValueError("provide title or pid, not both")
    if not has_title and not has_pid:
        if window == "required":
            raise ValueError("missing title or pid")
        return values
    if has_title:
        values["title"] = _text_argument(arguments, "title", maximum=512)
        return values
    pid = arguments.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ValueError("invalid pid")
    values["pid"] = pid
    return values


def _desktop_schema(
    properties: dict[str, dict[str, Any]] | None = None, *,
    required: tuple[str, ...] = (),
    window: Literal["required", "optional"] | None = None,
) -> dict[str, Any]:
    # The Anthropic Messages API rejects a tool input_schema with a top-level
    # oneOf/allOf/anyOf, so the "exactly one of title or pid" constraint for
    # window == "required" cannot be expressed here. It is stated in the
    # affected tools' descriptions instead, and enforced host-side at
    # execution time by _desktop_arguments.
    target_properties = deepcopy(_WINDOW_PROPERTIES) if window else {}
    schema: dict[str, Any] = {
        "type": "object", "properties": {**target_properties, **(properties or {})},
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return schema


_API_INCOMPATIBLE_SCHEMA_KEYS = ("oneOf", "allOf", "anyOf")


def api_incompatible_tool_names(schemas: list[dict[str, Any]]) -> list[str]:
    """Return tool names whose input_schema uses a shape the Messages API rejects.

    The Anthropic Messages API rejects any tool ``input_schema`` with a
    top-level ``oneOf``/``allOf``/``anyOf`` key (HTTP 400: "input_schema does
    not support oneOf, allOf, or anyOf at the top level"). This only inspects
    the schema's own top level; nested uses inside "properties" values are a
    separate, API-legal shape and are not flagged.
    """
    names: list[str] = []
    for schema in schemas:
        name = schema.get("name")
        input_schema = schema.get("input_schema")
        if not isinstance(name, str) or not isinstance(input_schema, Mapping):
            continue
        if any(key in input_schema for key in _API_INCOMPATIBLE_SCHEMA_KEYS):
            names.append(name)
    return names


def _bounded_window_inventory(inventory: Any) -> dict[str, Any]:
    if not isinstance(inventory, Mapping):
        raise ValueError("invalid window inventory")
    windows = inventory.get("windows")
    total = inventory.get("total")
    truncated = inventory.get("truncated")
    if (
        not isinstance(windows, list)
        or isinstance(total, bool)
        or not isinstance(total, int)
        or total < len(windows)
        or not isinstance(truncated, bool)
    ):
        raise ValueError("invalid window inventory")
    bounded = {
        "windows": deepcopy(windows),
        "total": total,
        "truncated": truncated,
    }
    while len(_serialize(bounded)) > _CONTENT_LIMIT and bounded["windows"]:
        bounded["windows"].pop()
        bounded["truncated"] = True
    if len(_serialize(bounded)) > _CONTENT_LIMIT:
        raise ValueError("window inventory metadata is too large")
    return bounded


def _configured_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        return (parsed.scheme in {"http", "https"} and bool(parsed.netloc)
                and parsed.username is None and parsed.password is None)
    except ValueError:
        return False


def _direct_https(value: str) -> bool:
    """The ONE predicate for "this is a plain https URL", at both ends.

    Used at link-MINT time (mcp_client._openable_link) and again at OPEN time
    (ToolRegistry._link_host_allowed, open's typed-URL branch), deliberately
    shared so the two can never drift on what counts as direct.

    Fail-soft, and that is load-bearing rather than tidy. `urlsplit` RAISES
    ValueError on a netloc holding a character that NFKC-normalizes into one
    of `/?#@:` -- CPython's `_checknetloc` -- so a Drive file named
    "Link: https://docs.google.com<fullwidth #>evil.com" is an attacker-
    plantable exception. Uncaught it escaped the mint closure, escaped
    `pattern.sub`, escaped the mirrored tool's run(), and ToolRegistry.call
    turned the WHOLE google result into ToolResult("error", "ValueError") --
    one shared file permanently breaking every Drive tool. A candidate that
    cannot even be parsed is simply not a direct https URL: say so and let
    the rest of the result through.

    Ports are restricted to the https default. A URL is only ever opened by
    handing it to the browser, and an allowlisted HOST on an unexpected port
    is a different service than the one the allowlist vouched for.
    """
    try:
        parsed = urlsplit(value)
        return (parsed.scheme == "https" and bool(parsed.netloc)
                and parsed.username is None and parsed.password is None
                and parsed.port in (None, 443))
    except ValueError:
        # Unparseable netloc, or a port that is not a number. Both are
        # "not a direct https URL", never an exception for a caller to wear.
        return False


def _optional_text(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return _CONTROL_CHARACTERS.sub("", value)[:512] or None


def _optional_positive_int(value: Any) -> int | None:
    """A pid or a native handle, or nothing. Never a bool, never <= 0."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _state_value(state: Any) -> str:
    value = getattr(state, "value", state)
    return str(value)


def _job_status(job: _JobLike) -> dict:
    return {
        "job_id": job.job_id,
        "title": job.title,
        "status": _state_value(job.state),
        "started_at": job.created_at,
    }


def _serialize(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


# Stands in for a readback_keys argument the model did not supply, so the
# omission is audible instead of invisible. Deliberately says only what is
# always true -- Atlas is supplying no value for this key -- rather than
# naming a mechanism. What absence MEANS is tool-specific: on
# send_gmail_message with reply_all it is a thread-wide To and Cc derived
# server-side; on draft_gmail_message with a thread_id it is the recipient and
# subject taken from the message being replied to (workspace-mcp 1.25.2,
# gmail/gmail_tools.py:3018-3021); on a plain draft it is an empty To. A
# readback may not assert a mechanism that does not run, so the mechanism
# lives in each tool's describe: text (config/mcp.yaml) where it can be
# tool-specific, and this string stays true everywhere.
_READBACK_OMITTED = "(not set)"

# ONE PAIR PER LINE. The readback is what the model reads aloud to Daniel
# before he approves a send, so its field boundaries have to be unforgeable by
# the values themselves -- a string value can otherwise splice a reassuring
# "to: sam@example.test" in right after a real "(not set)" placeholder, using
# text the model just read off a page.
#
# A newline is the only separator a value provably cannot contain: every C0
# control (\n and \r included) is already removed by _CONTROL_CHARACTERS, and
# U+2028/2029 by the format strip below. So the split is exact with NO
# escaping, which is why this shape and not quoting or delimiter-escaping --
# every escape mutates the value, and rule 4 wants the readback exact. A
# semicolon in a filename stays a semicolon; press_delete's chord is read back
# character for character.
#
# Aloud it is also the better shape: a field list is the form a model renders
# most naturally as "To: ... Subject: ...", where a run-on line invites it to
# blur one field into the next.
_READBACK_LINE_SEPARATOR = "\n"

# Invisible characters that can make the rendered readback LIE about the value
# it renders: the bidi controls and isolates (U+202E and friends can display a
# recipient reversed while a different address is what gets sent), the
# zero-width and deprecated format characters, and the line/paragraph
# separators that would otherwise forge a pair boundary above. Category Cf
# plus Zl/Zp, so a format character Unicode adds later is stripped by default.
#
# The three exceptions are orthography, not formatting, and removing them
# corrupted real names: U+00AD SOFT HYPHEN made "co-op.txt" read back as
# "coop.txt", U+200D ZERO WIDTH JOINER flattened emoji families in filenames,
# and U+200C ZERO WIDTH NON-JOINER changes the spelling of Arabic and Persian
# words. All three are bidi class BN (boundary-neutral), so none of them can
# reorder text or forge a field, which is the whole job of this strip.
#
# They ARE invisible, and that is a deliberate fidelity trade, not an
# oversight: two distinct values can still RENDER identically, so
# "C:\\notes\\co<SHY>op.txt" and "C:\\notes\\coop.txt" are different files that
# read back the same. The filename case is the real one. In a recipient
# address the same trick mostly self-defeats, because IDNA2008 disallows all
# three, so a crafted domain bounces rather than misdelivers. Stripping them
# would trade a rare display ambiguity for corrupting every legitimate name
# that needs them, which is the worse deal on a readback whose job is to be
# exact.
_READBACK_FORMAT_CATEGORIES = frozenset({"Cf", "Zl", "Zp"})
_READBACK_FORMAT_KEPT = frozenset({
    "­",  # SOFT HYPHEN
    "‌",  # ZERO WIDTH NON-JOINER
    "‍",  # ZERO WIDTH JOINER
})


def _readback_text(value: Any) -> str:
    """Serialize one argument value, exactly, minus what could misrender it."""
    text = _CONTROL_CHARACTERS.sub("", _serialize(value))
    if not text.isascii():
        text = "".join(
            character for character in text
            if character in _READBACK_FORMAT_KEPT
            or unicodedata.category(character) not in _READBACK_FORMAT_CATEGORIES
        )
    return text


def _schema_keys(tool: Tool) -> tuple[str, ...]:
    """The tool's own declared argument names, in the schema's order.

    Host-side knowledge the model cannot influence: for a mirrored MCP tool
    this is the remote server's schema (minus whatever strip_args and the
    account parameter removed), and for a built-in it is the registry's own.
    A malformed or absent schema yields nothing and the ordering simply falls
    back to declared keys then the model's order -- never an exception on the
    confirm path.
    """
    properties = tool.input_schema.get("properties") if isinstance(
        tool.input_schema, Mapping) else None
    if not isinstance(properties, Mapping):
        return ()
    return tuple(key for key in properties if isinstance(key, str))


def _readback_order(
    arguments: Mapping[str, Any],
    required_keys: Sequence[str],
    schema_keys: Sequence[str],
) -> dict[str, Any]:
    """Host-decided field order: declared keys, then the tool's own schema,
    then whatever the model invented.

    Field order used to be the MODEL'S JSON order for any tool without
    readback_keys, and the readback is bounded, so a model could push the
    field the decision turns on past the end of what Daniel ever hears -- forty
    junk arguments in front of a write_file's `path` and `content` did exactly
    that. Ordering cannot be left to the caller when the caller is the thing
    being checked.

    The three tiers, in the order that survives a bound:
      1. readback_keys from config -- the rule-5 keys a spoken yes turns on.
      2. The tool's OWN input schema properties, in the schema's order. This is
         what generalizes readback_keys to every tool for free: `path` and
         `content` are the write_file schema, `note0..note39` are not, so the
         real arguments lead and the invented ones queue behind them.
      3. Everything else, in the model's order -- rendered, never hidden, but
         it can no longer displace a declared argument.
    """
    ordered: dict[str, Any] = {}
    for key in required_keys:
        ordered[key] = arguments[key] if key in arguments else _READBACK_OMITTED
    for key in schema_keys:
        if key in arguments and key not in ordered:
            ordered[key] = arguments[key]
    for key, value in arguments.items():
        if key not in ordered:
            ordered[key] = value
    return ordered


def _readback_summary(
    name: str,
    arguments: Mapping[str, Any],
    *,
    condensed: bool = False,
    required_keys: Sequence[str] = (),
    schema_keys: Sequence[str] = (),
) -> str:
    if name == "press_delete":
        # The delete chord is read back whole, always: it is short, and it
        # is the one readback where every character matters.
        arguments = {key: arguments.get(key, "") for key in ("chord", "window", "pid")}
        maximum = None
    elif condensed:
        maximum = _READBACK_CONDENSED_VALUE_LIMIT
    else:
        maximum = _READBACK_VALUE_LIMIT
    # A declared key the model DID supply is rendered normally -- the
    # placeholder is only ever a stand-in for absence, never a replacement for
    # a value.
    arguments = _readback_order(arguments, required_keys, schema_keys)
    details = []
    for key, value in arguments.items():
        # Condensing exists to tame the one oversized value (usually content);
        # short values like path carry the discriminating detail a confirmation
        # turns on (e.g. the filename tail of an overwrite target) and are read
        # back whole under the normal per-value limit instead.
        value_maximum = maximum
        squeeze = condensed
        if condensed and len(_readback_text(value)) <= _READBACK_VALUE_LIMIT:
            value_maximum = _READBACK_VALUE_LIMIT
            squeeze = False
        # Keys go through the same cleaning and bound as values. An argument
        # NAME is model-chosen too, so it must not be able to carry a newline
        # into the sentence and open a line of its own.
        details.append(
            f"{_readback_value(key, _READBACK_VALUE_LIMIT)}: "
            f"{_readback_value(value, value_maximum, total=squeeze)}"
        )
    if not details:
        return f"{name} - no arguments"
    return name + _READBACK_LINE_SEPARATOR + _READBACK_LINE_SEPARATOR.join(
        _within_readback_budget(details, len(name)),
    )


# A readback that runs past _CONTENT_LIMIT used to be cut mid-value by the
# content bound, so whole fields vanished behind a "...[truncated]" marker that
# said something was cut but not that SIXTEEN ARGUMENTS were gone. These two
# bounds are applied here instead, where the field structure still exists and
# the loss can be counted and stated.
#
# The budget leaves room for the instruction sentence _bound_readback wraps
# around this, so a summary built here never reaches that backstop.
_READBACK_SUMMARY_LIMIT = 3_200
# Not a size bound -- a speakability one. Nothing on the curated surface takes
# anywhere near this many arguments, and past it a spoken readback stops being
# something a person can hold in their head long enough to answer.
_READBACK_MAX_FIELDS = 24
# Width of the longest omission line below. It carries no ": ", so it can
# never be mistaken for a field, and a value cannot forge one because a value
# cannot open a line of its own.
_OMITTED_FIELDS_RESERVE = 80


def _within_readback_budget(details: list[str], used: int) -> list[str]:
    """Drop whole fields, never half of one, and always say how many.

    Refusing the call instead was the other option and is the wrong one here:
    the BB-wave review already settled that an oversized confirm must condense
    rather than refuse, because "split it into two calls" is advice that cannot
    be followed for an overwrite -- it writes half a file and then replaces it
    with the other half. So the readback stays answerable by telling the truth
    about its own completeness, and Daniel can say no to a send whose last line
    admits it did not show him everything.
    """
    shown: list[str] = []
    for index, line in enumerate(details):
        # The omission line has to fit even when the field that triggered it
        # is the one being dropped, so its worst-case width is reserved.
        if (
            index >= _READBACK_MAX_FIELDS
            or used + 1 + len(line) > _READBACK_SUMMARY_LIMIT - _OMITTED_FIELDS_RESERVE
        ):
            remaining = len(details) - index
            shown.append(
                f"({remaining} more argument{'' if remaining == 1 else 's'} not shown"
                " - say no unless you know what they are)"
            )
            return shown
        shown.append(line)
        used += 1 + len(line)
    return shown



def _condensed_readback(summary: str) -> str:
    """One line, for the two HOST NOTES that only need to identify a pending
    action -- never for the approval readback itself.

    _pending_collision and _supersede_note both say something ABOUT a pending
    action ("one is still waiting", "that one was cancelled") in a single
    sentence; neither is the thing Daniel says yes to. Flattening the newlines
    would hand the pair boundary back to the values, so this rebuilds the line
    from the exact split instead: a newline cannot occur inside a value, so
    splitting on it is lossless, and only then are the delimiters neutralized
    inside each value (semicolon to comma, colon-space to colon -- both
    inaudible). Fidelity is traded ONLY here, where nothing is approved.
    """
    name, separator, rest = summary.partition(_READBACK_LINE_SEPARATOR)
    if not separator:
        return summary
    pairs = []
    for line in rest.split(_READBACK_LINE_SEPARATOR):
        key, found, value = line.partition(": ")
        if not found:
            pairs.append(_neutralized(line))
        else:
            pairs.append(f"{_neutralized(key)}: {_neutralized(value)}")
    return f"{name} - " + "; ".join(pairs)


# Colon and semicolon lookalikes, folded to ASCII first so a fullwidth or
# small-form separator meets the same rule instead of sailing past a check
# that only knows the ASCII byte. Written as escapes, never as literals: half
# of these are indistinguishable from ":" in an editor, which is the whole
# point of them.
#
# The list does not have to be exhaustive, and that is a deliberate structural
# claim, not a shrug. It feeds ONLY the one-line host notes below, where the
# worst case is a misleading identification of an action nobody is being asked
# to approve. The approval readback is unforgeable by construction (a value
# cannot contain a newline) and depends on none of it.
_SEPARATOR_CONFUSABLES = str.maketrans({
    # colon-shaped
    "：": ":", "﹕": ":", "︓": ":", "∶": ":", "꞉": ":",
    "ː": ":", "˸": ":", "׃": ":", "܃": ":", "։": ":",
    "᛫": ":", "⁚": ":", "⁝": ":", "꛶": ":",
    # semicolon-shaped
    "；": ";", "﹔": ";", ";": ";", "؛": ";", "⁏": ";",
    "⸵": ";",
})
_COLON_SPACE = re.compile(r":\s+")


def _neutralized(text: str) -> str:
    return _COLON_SPACE.sub(":", text.translate(_SEPARATOR_CONFUSABLES).replace(";", ","))


def _readback_value(value: Any, maximum: int | None, *, total: bool = False) -> str:
    cleaned = _readback_text(value)
    if maximum is None or len(cleaned) <= maximum:
        return cleaned
    if total:
        # Condensed form: the size is the point ("5,120 characters going to
        # this path"), so state the whole length rather than the remainder.
        return f"{cleaned[:maximum]} ...({len(cleaned)} chars total)"
    return f"{cleaned[:maximum]} ...(+{len(cleaned) - maximum} chars)"


def _bound_content(value: str) -> str:
    cleaned = _CONTROL_CHARACTERS.sub("", str(value))
    if len(cleaned) <= _CONTENT_LIMIT:
        return cleaned
    return cleaned[:_CONTENT_LIMIT - len(_TRUNCATED)] + _TRUNCATED


# Every control character except the newline that separates readback pairs.
# The pairs themselves were built from values _readback_text had already
# stripped, so the only newlines in this text are the ones the host wrote.
_READBACK_CONTROL_CHARACTERS = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")


def _bound_readback(value: str) -> str:
    """_bound_content for the one message whose line breaks are structural."""
    cleaned = _READBACK_CONTROL_CHARACTERS.sub("", str(value))
    if len(cleaned) <= _CONTENT_LIMIT:
        return cleaned
    return cleaned[:_CONTENT_LIMIT - len(_TRUNCATED)] + _TRUNCATED
