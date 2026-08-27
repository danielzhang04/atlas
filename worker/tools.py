"""Register model tools and execute the built-in host capabilities."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
import importlib
import json
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
    "McpToolError",
    "PendingAction",
    "Policy",
    "Tool",
    "ToolRegistry",
    "ToolResult",
    "WorkLike",
    "builtin",
    "load_apps",
    "register_count_mail",
]

Policy = Literal["instant", "confirm"]
_Status = Literal["ok", "error", "needs_confirmation"]
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_CONTENT_LIMIT = 4096
_READBACK_ARGUMENT_LIMIT = 1_200
_READBACK_VALUE_LIMIT = 160
_HOST_ONLY_TOOLS = frozenset({"confirm", "cancel_pending"})
_TAINT_REFUSAL = "refused after external content; ask Daniel again next turn"
_TAINTED_BRIEF_SUFFIX = "\n\n(Atlas: content read during this turn was not forwarded.)"
_TRUNCATED = "...[truncated]"
_DESKTOP_DELETE_CHORDS = ("delete", "ctrl+d", "ctrl+x", "shift+delete")
_DESKTOP_ALLOWED_CHORDS = frozenset({
    "alt+tab", "backspace", "ctrl+a", "ctrl+c", "ctrl+f", "ctrl+l", "ctrl+p",
    "ctrl+s", "ctrl+t", "ctrl+v", "ctrl+w", "ctrl+y", "ctrl+z", "down", "end",
    "enter", "escape", "home", "left", "pagedown", "pageup", "right", "space",
    "tab", "up",
})
_DESKTOP_MEDIA_KEYS = (
    "play_pause", "next", "previous", "volume_up", "volume_down", "mute",
)
_FOUND_MESSAGES = re.compile(r"^Found\s+(\d+)\s+messages?\s+matching\b", re.IGNORECASE)
_NEXT_PAGE_TOKEN = re.compile(
    r"^[ \t]*(?:Next[ \t]+page[ \t]+token|page_token)[ \t]*:[ \t]*(\S+)[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)


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
                 timeout_s: float = 8.0) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        self._clock = clock
        self._timeout_s = timeout_s
        self._tools: dict[str, Tool] = {}
        self._pending: PendingAction | None = None
        self._open_aliases: frozenset[str] = frozenset()

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
                return ToolResult("error", _TAINT_REFUSAL)
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
            if tool.policy == "confirm":
                serialized = json.dumps(copied, ensure_ascii=False)
                if len(serialized) > _READBACK_ARGUMENT_LIMIT:
                    return ToolResult("error", "too large to read back; split it")
                pending = PendingAction(
                    confirm_id=secrets.token_urlsafe(8),
                    name=name,
                    arguments=copied,
                    summary=_readback_summary(name, copied),
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
        if name in {
            "close",
            "focus",
            "open_file",
            "open_folder",
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
        try:
            async with asyncio.timeout(self._timeout_s):
                if tool.execute_prepared is not None and host_state is not None:
                    value = await tool.execute_prepared(arguments, host_state)
                else:
                    value = await tool.run(arguments)
        except McpToolError as exc:
            return ToolResult("error", str(exc))
        except Exception as exc:
            return ToolResult("error", type(exc).__name__)
        if isinstance(value, ToolResult):
            return ToolResult(value.status, _bound_content(value.content), value.confirm_id)
        return ToolResult("ok", _bound_content(_serialize(value)))


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
) -> None:
    aliases = _aliases(apps)
    registry._configure_open_aliases(aliases)

    def desktop_api() -> Any:
        return desktop if desktop is not None else _desktopcontrol()

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
                    opener(app.url)
                else:
                    if (
                        isinstance(opened, Mapping)
                        and opened.get("focused") is True
                        and opened.get("existing") is True
                    ):
                        return ToolResult("ok", "focused existing window")
            elif app.url is not None:
                dynamic_url = paired_url() if name == "atlas" and paired_url is not None else None
                opener(dynamic_url or app.url)
            else:
                return ToolResult("error", "unknown app")
            return {"opened": name}
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
        query = _text_argument(arguments, "query", maximum=512)
        return await asyncio.to_thread(files.find, query)

    async def open_file(arguments: dict) -> dict:
        path = _text_argument(arguments, "path", maximum=2048)
        return files.open(path)

    async def open_folder(arguments: dict) -> dict:
        path = _text_argument(arguments, "path", maximum=2048)
        return files.open_folder(path)

    async def read_file(arguments: dict) -> dict:
        path = _text_argument(arguments, "path", maximum=2048)
        return await files.read_file(path)

    async def list_desktop_windows(arguments: dict) -> dict[str, Any]:
        _only_arguments(arguments, {"limit"})
        limit = arguments.get("limit", 40)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise ValueError("invalid limit")
        inventory = desktop_api().list_windows(limit=limit)
        return _bounded_window_inventory(inventory)

    async def focus_desktop_window(arguments: dict) -> dict:
        return desktop_api().focus_window(**_window_target(arguments))

    async def desktop_window_action(arguments: dict) -> dict:
        _only_arguments(
            arguments, {"action", "title", "pid", "x", "y", "width", "height"},
        )
        action = _text_argument(arguments, "action", maximum=64).casefold()
        target = _window_target(arguments, ignored={"action", "x", "y", "width", "height"})
        extras = {
            name: _integer_argument(arguments, name)
            for name in ("x", "y", "width", "height")
            if name in arguments
        }
        return desktop_api().window_action(action, **target, **extras)

    async def desktop_media_key(arguments: dict) -> dict:
        _only_arguments(arguments, {"key"})
        return desktop_api().media_key(
            _text_argument(arguments, "key", maximum=32).casefold(),
        )

    async def desktop_click(arguments: dict) -> dict:
        _only_arguments(arguments, {"x", "y", "title", "pid"})
        target = _window_target(arguments, optional=True, ignored={"x", "y"})
        return desktop_api().click(
            _integer_argument(arguments, "x"),
            _integer_argument(arguments, "y"),
            **target,
        )

    async def desktop_type_text(arguments: dict) -> dict:
        _only_arguments(arguments, {"text"})
        text = arguments.get("text")
        if not isinstance(text, str) or not text or len(text) > 4000:
            raise ValueError("invalid text")
        return desktop_api().type_text(text)

    async def desktop_press_keys(arguments: dict) -> ToolResult | dict:
        _only_arguments(arguments, {"chord"})
        api = desktop_api()
        chord = api.normalize_chord(arguments.get("chord"))
        if chord in _DESKTOP_DELETE_CHORDS:
            return ToolResult("error", "delete chords require press_delete")
        return api.press_keys(chord)

    def prepare_delete(arguments: dict) -> _PreparedAction:
        _only_arguments(arguments, {"chord"})
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
        _only_arguments(arguments, {"chord", "window", "pid"})
        try:
            return desktop_api().press_delete(
                arguments["chord"], expected_hwnd=expected_hwnd,
            )
        except Exception:
            return ToolResult("error", "focused window changed; delete not executed")

    definitions = (
        ("open", "Open an allowlisted app or HTTPS URL.", {"target": {"type": "string"}}, open_target),
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
            ("find_file", "Find files and folders under configured roots.", {
                "query": {"type": "string"},
            }, find_file),
            ("open_file", "Open an inert document or media file under configured roots.", {
                "path": {"type": "string"},
            }, open_file),
            ("open_folder", "Open a directory under configured roots in Explorer.", {
                "path": {"type": "string"},
            }, open_folder),
            ("read_file", "Read small text or route previewed large-file analysis to launch_work.", {
                "path": {"type": "string"},
            }, read_file),
        )
    for name, description, properties, run in definitions:
        schema = {
            "type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False,
        }
        registry.register(Tool(name, description, schema, run))

    target_properties = {
        "title": {"type": "string"},
        "pid": {"type": "integer", "minimum": 1},
    }
    target_choice = [{"required": ["title"]}, {"required": ["pid"]}]
    desktop_definitions = (
        (
            "list_windows",
            "List visible top-level windows without exposing native handles.",
            {"limit": {"type": "integer", "minimum": 1, "maximum": 100}},
            [],
            list_desktop_windows,
        ),
        (
            "focus_window",
            "Focus one host-resolved visible window by title or pid.",
            target_properties,
            target_choice,
            focus_desktop_window,
        ),
        (
            "window_action",
            "Minimize, maximize, restore, close, move, or resize a host-resolved window.",
            {
                **target_properties,
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
            },
            [{"required": ["title", "action"]}, {"required": ["pid", "action"]}],
            desktop_window_action,
        ),
        (
            "media_key",
            "Press one allowlisted media key.",
            {"key": {"type": "string", "enum": list(_DESKTOP_MEDIA_KEYS)}},
            [{"required": ["key"]}],
            desktop_media_key,
        ),
        (
            "click",
            "Click screen coordinates, or coordinates relative to a host-resolved window.",
            {"x": {"type": "integer"}, "y": {"type": "integer"}, **target_properties},
            [{"required": ["x", "y"]}],
            desktop_click,
        ),
        (
            "type_text",
            "Type Unicode text into the foreground application.",
            {"text": {"type": "string", "minLength": 1, "maxLength": 4000}},
            [{"required": ["text"]}],
            desktop_type_text,
        ),
        (
            "press_keys",
            "Press one allowlisted non-delete key chord in the foreground application.",
            {"chord": {"type": "string", "enum": sorted(_DESKTOP_ALLOWED_CHORDS)}},
            [{"required": ["chord"]}],
            desktop_press_keys,
        ),
    )
    for name, description, properties, choices, run in desktop_definitions:
        schema = {
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
        }
        if choices:
            schema["oneOf"] = choices
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
            total = 0
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
                total += page_count
                token_match = _NEXT_PAGE_TOKEN.search(content)
                page_token = token_match.group(1) if token_match is not None else None
                if page_token is None:
                    return total, True
                if page_count < 500:
                    return ToolResult("error", "unexpected mail search result")
                if page_token in seen_tokens:
                    return total, False
                seen_tokens.add(page_token)
            return total, page_token is None

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
                f"{inbox_text} in your inbox, {primary_text} in Primary",
            )
        total, exact = inbox_count
        return {"query": query, "count": total, "exact": exact}

    schema = {
        "type": "object", "properties": {"query": {"type": "string"}},
        "required": ["query"], "additionalProperties": False,
    }
    registry.register(Tool(
        "count_mail", "Count Gmail messages exactly across bounded search pages.",
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


def _text_argument(arguments: Mapping[str, Any], name: str, *, maximum: int) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"invalid {name}")
    return value.strip()


def _only_arguments(arguments: Mapping[str, Any], allowed: set[str]) -> None:
    if set(arguments) - allowed:
        raise ValueError("unexpected argument")


def _integer_argument(arguments: Mapping[str, Any], name: str) -> int:
    value = arguments.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"invalid {name}")
    return value


def _window_target(
    arguments: Mapping[str, Any],
    *,
    optional: bool = False,
    ignored: set[str] | None = None,
) -> dict[str, Any]:
    ignored = ignored or set()
    _only_arguments(arguments, {"title", "pid"} | ignored)
    has_title = "title" in arguments
    has_pid = "pid" in arguments
    if has_title and has_pid:
        raise ValueError("provide title or pid, not both")
    if not has_title and not has_pid:
        if optional:
            return {}
        raise ValueError("missing title or pid")
    if has_title:
        return {"title": _text_argument(arguments, "title", maximum=512)}
    pid = _integer_argument(arguments, "pid")
    if pid <= 0:
        raise ValueError("invalid pid")
    return {"pid": pid}


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


def _readback_summary(name: str, arguments: Mapping[str, Any]) -> str:
    if name == "press_delete":
        chord = _CONTROL_CHARACTERS.sub("", str(arguments.get("chord", "")))
        title = _CONTROL_CHARACTERS.sub("", str(arguments.get("window", "")))
        pid = arguments.get("pid")
        return f"press_delete - chord: {chord}; window: {title}; pid: {pid}"
    details = []
    for key, value in arguments.items():
        serialized = _serialize(value)
        cleaned = _CONTROL_CHARACTERS.sub("", serialized)
        if len(cleaned) > _READBACK_VALUE_LIMIT:
            omitted = len(cleaned) - _READBACK_VALUE_LIMIT
            cleaned = f"{cleaned[:_READBACK_VALUE_LIMIT]} ...(+{omitted} chars)"
        details.append(f"{key}: {cleaned}")
    return f"{name} - " + ("; ".join(details) if details else "no arguments")


def _bound_content(value: str) -> str:
    cleaned = _CONTROL_CHARACTERS.sub("", str(value))
    if len(cleaned) <= _CONTENT_LIMIT:
        return cleaned
    return cleaned[:_CONTENT_LIMIT - len(_TRUNCATED)] + _TRUNCATED
