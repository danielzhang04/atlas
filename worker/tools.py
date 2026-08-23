"""Register model tools and execute the built-in host capabilities."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
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
_TAINT_REFUSAL = "refused after external content; ask Daniel again next turn"
_TAINTED_BRIEF_SUFFIX = "\n\n(Atlas: content read during this turn was not forwarded.)"
_TRUNCATED = "…[truncated]"
_FOUND_MESSAGES = re.compile(r"^Found\s+(\d+)\s+messages?\s+matching\b", re.IGNORECASE)
_NEXT_PAGE_TOKEN = re.compile(
    r"^[ \t]*(?:Next[ \t]+page[ \t]+token|page_token)[ \t]*:[ \t]*(\S+)[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    input_schema: dict
    run: Callable[[dict], Awaitable[Any]]
    policy: Policy = "instant"


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
        ]

    async def call(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        tainted: bool = False,
        transcript: str | None = None,
    ) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult("error", "unknown tool")
        try:
            copied = deepcopy(dict(arguments))
            if tainted and self._refused_after_external_content(name, copied):
                return ToolResult("error", _TAINT_REFUSAL)
            if tainted and name == "launch_work":
                if not isinstance(transcript, str) or not transcript.strip():
                    return ToolResult("error", "missing turn transcript")
                copied["brief"] = f"{transcript}{_TAINTED_BRIEF_SUFFIX}"
            if tool.policy == "confirm":
                pending = self._pending
                if pending is not None and pending.expires <= self._clock():
                    self._pending = None
                    pending = None
                if (
                    pending is not None
                    and pending.name == name
                    and pending.arguments == copied
                ):
                    return ToolResult("error", "already pending; call confirm")
                serialized = json.dumps(copied)
                pending = PendingAction(
                    confirm_id=secrets.token_urlsafe(8),
                    name=name,
                    arguments=copied,
                    summary=f"{name} {serialized[:300]}",
                    expires=self._clock() + 120.0,
                )
                self._pending = pending
                return ToolResult(
                    "needs_confirmation",
                    _bound_content(
                        f"NOT EXECUTED. Pending: {pending.summary}. Read this back and ask Daniel. "
                        "When he agrees on a later turn, call confirm with "
                        f'confirm_id="{pending.confirm_id}" — do not call {name} again.'
                    ),
                    pending.confirm_id,
                )
            return await self._execute(tool, copied)
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
            "confirm",
            "close",
            "focus",
            "open_file",
            "cancel_work",
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
        pending = self._pending
        if pending is None:
            return ToolResult("error", "nothing to confirm")
        if pending.expires <= self._clock():
            self._pending = None
            return ToolResult("error", "nothing to confirm")
        if pending.confirm_id != confirm_id:
            return ToolResult("error", "nothing to confirm")
        self._pending = None
        return await self._execute(self._tools[pending.name], pending.arguments)

    def cancel_pending(self) -> ToolResult:
        self._pending = None
        return ToolResult("ok", "cancelled")

    async def _execute(self, tool: Tool, arguments: dict[str, Any]) -> ToolResult:
        try:
            async with asyncio.timeout(self._timeout_s):
                value = await tool.run(arguments)
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
        if (url is None) == (exe is None) or not isinstance(words, list) or not words:
            raise ValueError(f"invalid app entry: {name}")
        if not all(isinstance(word, str) and word.strip() for word in words):
            raise ValueError(f"invalid app words: {name}")
        if url is not None and not _configured_url(url):
            raise ValueError(f"invalid app URL: {name}")
        if exe is not None and (not isinstance(exe, str) or not exe):
            raise ValueError(f"invalid app profile: {name}")
        apps[name] = AppEntry(words=tuple(words), url=url, exe=exe)
    return apps


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
) -> None:
    aliases = _aliases(apps)
    registry._configure_open_aliases(aliases)

    async def open_target(arguments: dict) -> ToolResult | dict:
        target = _text_argument(arguments, "target", maximum=2048)
        entry = aliases.get(target.casefold())
        if entry is not None:
            name, app = entry
            if app.url is not None:
                dynamic_url = paired_url() if name == "atlas" and paired_url is not None else None
                opener(dynamic_url or app.url)
            else:
                profile_opener(app.exe or "", None)
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

    async def confirm(arguments: dict) -> ToolResult:
        return await registry.confirm(_text_argument(arguments, "confirm_id", maximum=256))

    async def cancel_pending(_: dict) -> ToolResult:
        return registry.cancel_pending()

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

    async def read_file(arguments: dict) -> dict:
        path = _text_argument(arguments, "path", maximum=2048)
        return files.read(path)

    definitions = (
        ("open", "Open an allowlisted app or HTTPS URL.", {"target": {"type": "string"}}, open_target),
        ("focus", "Focus an allowlisted desktop app.", {"app": {"type": "string"}}, focus),
        (
            "confirm",
            "After Daniel agrees on a later turn, execute the pending action using its confirm_id; "
            "do not call the original tool again.",
            {"confirm_id": {"type": "string"}},
            confirm,
        ),
        ("cancel_pending", "Cancel the pending action.", {}, cancel_pending),
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
            ("read_file", "Read bounded text from a file under configured roots.", {
                "path": {"type": "string"},
            }, read_file),
        )
    for name, description, properties, run in definitions:
        schema = {
            "type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False,
        }
        registry.register(Tool(name, description, schema, run))


def register_count_mail(
    registry: ToolRegistry,
    search: Callable[[dict], Awaitable[str]],
) -> None:
    """Register an exact, bounded counter over Gmail search result pages."""

    async def count_mail(arguments: dict) -> ToolResult | dict:
        query = _text_argument(arguments, "query", maximum=1024)
        total = 0
        page_token = None
        seen_tokens: set[str] = set()
        for _page in range(4):
            search_arguments = {
                "query": query,
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
                return {"query": query, "count": total, "exact": True}
            if page_count < 500:
                return ToolResult("error", "unexpected mail search result")
            if page_token in seen_tokens:
                return {"query": query, "count": total, "exact": False}
            seen_tokens.add(page_token)
        return {"query": query, "count": total, "exact": page_token is None}

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


def _bound_content(value: str) -> str:
    cleaned = _CONTROL_CHARACTERS.sub("", str(value))
    if len(cleaned) <= _CONTENT_LIMIT:
        return cleaned
    return cleaned[:_CONTENT_LIMIT - len(_TRUNCATED)] + _TRUNCATED
