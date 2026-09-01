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
from typing import Any, Awaitable, Callable, Literal, Mapping, Protocol
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
    # Consulted only when policy == "instant" (see ToolRegistry.call); this is
    # what makes "escalate can only move instant -> confirm" structurally true
    # rather than a convention callers must remember.
    escalate: Callable[[Mapping], bool] | None = None


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


@dataclass(frozen=True, slots=True)
class Handle:
    path: str
    kind: Literal["file", "folder"]


class _HandleTable:
    """Per-turn map of host-minted ids to targets this host itself validated.

    The taint wall exists because the model must never turn external content
    into an action target. Handles keep that true while still allowing
    search-then-act: the only writer is `ToolRegistry._mint_handle`, called by
    the `find_file` builtin with paths `LocalFiles` already resolved inside the
    configured roots. Nothing a model says -- and nothing an MCP server
    returns, including text that looks like "handle: f1" -- can add an entry,
    so a resolvable handle always names a path this host produced this turn.
    Ids are not secrets: every entry is already host-validated, so guessing one
    only ever reaches another of this turn's own in-roots search results.

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

    def mint(self, path: str, kind: str) -> str | None:
        if not isinstance(path, str) or not path or kind not in ("file", "folder"):
            raise ValueError("invalid handle target")
        # The bound is per turn: _entries is what clear() empties.
        if len(self._entries) >= _HANDLE_LIMIT:
            return None
        handle = f"f{self._next}"
        self._next += 1
        self._entries[handle] = Handle(path, kind)
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
        self._executions: dict[object, dict[str, str]] = {}
        self._execution_observer: Callable[[dict[str, str] | None], None] | None = None
        self._handles = _HandleTable()

    def begin_turn(self) -> None:
        """Reset per-turn host state. Handles live for exactly one turn."""
        self._handles.clear()

    def _mint_handle(self, path: str, kind: str) -> str | None:
        """Host-only: record a target this host validated, return its id."""
        return self._handles.mint(path, kind)

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
            if tool.policy == "confirm" and self.pending is not None:
                return ToolResult(
                    "error",
                    "a previous action is still awaiting Daniel's yes or no",
                )
            copied = deepcopy(dict(arguments))
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
                    return ToolResult(
                        "error",
                        "a previous action is still awaiting Daniel's yes or no",
                    )
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
                    summary=_readback_summary(name, copied, condensed=condensed),
                    expires=self._clock() + 120.0,
                    host_state=host_state,
                )
                self._pending = pending
                return ToolResult(
                    "needs_confirmation",
                    _bound_content(
                        "NOT EXECUTED. Read this summary back to Daniel and wait for his yes or no: "
                        f"{pending.summary}."
                    ),
                    pending.confirm_id,
                )
            return await self._execute(tool, copied, host_state=host_state)
        except Exception as exc:
            return ToolResult("error", type(exc).__name__)

    def _configure_open_aliases(self, aliases: Mapping[str, Any]) -> None:
        self._open_aliases = frozenset(aliases)

    def _refused_after_external_content(
        self,
        name: str,
        arguments: Mapping[str, Any],
    ) -> bool:
        if name in _HANDLE_TOOLS:
            # The only thing that survives taint here is a handle this host
            # minted earlier in THIS turn; a model-supplied path -- and an
            # unminted or expired handle -- is still refused.
            return not self._handle_only_call(arguments)
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
        target = arguments.get("target")
        if not isinstance(target, str):
            return False
        normalized = target.strip().casefold()
        if normalized in self._open_aliases:
            return False
        return _direct_https(target.strip())

    def _handle_only_call(self, arguments: Mapping[str, Any]) -> bool:
        if arguments.get("path") is not None:
            return False
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
        return {"opened": name, "via": "web"}

    async def open_target(arguments: dict) -> ToolResult | dict:
        target = _text_argument(arguments, "target", maximum=2048)
        entry = aliases.get(target.casefold())
        if entry is not None:
            name, app = entry
            if app.exe is not None:
                try:
                    opened = profile_opener(app.exe, None)
                except desktopapps.DesktopAppError:
                    if app.url is None:
                        raise
                    return open_web(name, app.url)
                else:
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
            return {"opened": target}
        return ToolResult("error", "unknown app")

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
        query = _text_argument(arguments, "query", maximum=512)
        # Kinds are stat'ed on the same worker thread as the scan; minting then
        # happens back on the loop thread so the registry stays single-threaded.
        found = await asyncio.to_thread(_matches_with_kind, files, query)
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
        if handle is None:
            return _text_argument(arguments, "path", maximum=2048)
        if arguments.get("path") is not None:
            raise ValueError("provide either path or handle, not both")
        entry = registry._resolve_handle(handle)
        if entry is None:
            raise ValueError(_UNKNOWN_HANDLE)
        if entry.kind == expected:
            return entry.path
        if expected == "file":
            raise ValueError("that handle is a folder; use open_folder")
        # "open the folder that file is in": the parent is derived by the host
        # from an already-validated path, never supplied by the model, and
        # LocalFiles re-checks confinement before opening it.
        return str(Path(entry.path).parent)

    async def open_file(arguments: dict) -> dict:
        return files.open(file_target(arguments, "file"))

    async def open_folder(arguments: dict) -> dict:
        return files.open_folder(file_target(arguments, "folder"))

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
        ("open", _open_description(aliases), {"target": {"type": "string"}}, open_target),
        ("focus", "Focus an allowlisted desktop app.", {"app": {"type": "string"}}, focus),
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
        definitions += (
            ("find_file", (
                "Find files and folders under configured roots. Every match carries a "
                "handle you can pass to open_file or open_folder for the rest of this turn."
            ), {
                "query": {"type": "string"},
            }, find_file),
            ("open_file", (
                "Open an inert document or media file under configured roots. Pass handle "
                "whenever this turn's find_file returned one; pass path only without a "
                "handle -- never both. After any tool that returns outside content, only a "
                "handle works."
            ), {
                "path": {"type": "string"}, "handle": {"type": "string"},
            }, open_file),
            ("open_folder", (
                "Open a directory under configured roots in Explorer. Pass handle whenever "
                "this turn's find_file returned one -- a file's handle opens the folder "
                "containing it; pass path only without a handle, never both. After any tool "
                "that returns outside content, only a handle works."
            ), {
                "path": {"type": "string"}, "handle": {"type": "string"},
            }, open_folder),
            ("read_file", "Read small text or route previewed large-file analysis to launch_work.", {
                "path": {"type": "string"},
            }, read_file),
        )
    for name, description, properties, run in definitions:
        # Handle tools take exactly one of path/handle, so neither is required
        # by the schema; the host enforces the choice (a top-level oneOf is not
        # accepted by the Messages API -- api_incompatible_tool_names).
        schema = {
            "type": "object", "properties": properties,
            "required": [] if name in _HANDLE_TOOLS else list(properties),
            "additionalProperties": False,
        }
        registry.register(Tool(name, description, schema, run))

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
        registry.register(Tool(name, description, schema, run))

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
    if not listed:
        return "Open an allowlisted app or HTTPS URL."
    return (
        "Open an allowlisted app or HTTPS URL. Aliases open the real desktop app "
        f"when configured: {listed}."
    )


def _matches_with_kind(files: LocalFiles, query: str) -> list[tuple[dict, str]]:
    """Pair each host-produced match with the kind the host stats itself."""
    matches: list[tuple[dict, str]] = []
    for item in files.find(query):
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
    parsed = urlsplit(value)
    return (parsed.scheme in {"http", "https"} and bool(parsed.netloc)
            and parsed.username is None and parsed.password is None)


def _direct_https(value: str) -> bool:
    parsed = urlsplit(value)
    return (parsed.scheme == "https" and bool(parsed.netloc)
            and parsed.username is None and parsed.password is None)


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


def _readback_summary(
    name: str,
    arguments: Mapping[str, Any],
    *,
    condensed: bool = False,
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
    details = []
    for key, value in arguments.items():
        # Condensing exists to tame the one oversized value (usually content);
        # short values like path carry the discriminating detail a confirmation
        # turns on (e.g. the filename tail of an overwrite target) and are read
        # back whole under the normal per-value limit instead.
        value_maximum = maximum
        squeeze = condensed
        if condensed and len(_serialize(value)) <= _READBACK_VALUE_LIMIT:
            value_maximum = _READBACK_VALUE_LIMIT
            squeeze = False
        details.append(f"{key}: {_readback_value(value, value_maximum, total=squeeze)}")
    return f"{name} - " + ("; ".join(details) if details else "no arguments")


def _readback_value(value: Any, maximum: int | None, *, total: bool = False) -> str:
    cleaned = _CONTROL_CHARACTERS.sub("", _serialize(value))
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
