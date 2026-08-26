from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager, suppress
from copy import deepcopy
from datetime import timedelta
import json
import logging
from pathlib import Path
import subprocess
from typing import Any, AsyncContextManager, TYPE_CHECKING
import unicodedata

import yaml

from .jobobject import kill_process_tree
from .tools import McpToolError, Policy, Tool

if TYPE_CHECKING:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters
    from .tools import ToolRegistry


__all__ = ["McpServers", "load_mcp_config", "policy_for"]


_LOGGER = logging.getLogger(__name__)
_MAX_CONTENT = 4_096
_RECIPIENT_ARGUMENTS = frozenset({"to", "cc", "bcc", "recipient", "attendees"})
_SELF_RECIPIENTS = frozenset({"me", "myself", "my email"})
_TRUNCATED = "…[truncated]"


SessionFactory = Callable[
    [str, "StdioServerParameters"], AsyncContextManager["ClientSession"]
]
ServerHook = Callable[[str, "ToolRegistry"], None]
ProcessTreeKiller = Callable[..., subprocess.CompletedProcess]


def _load_mcp_transport():
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    return ClientSession, StdioServerParameters, stdio_client


class _PidTrackingStdio(AbstractAsyncContextManager):
    def __init__(self, spec: StdioServerParameters, on_pid: Callable[[int], None]) -> None:
        _, _, stdio_client = _load_mcp_transport()
        self._context = stdio_client(spec, errlog=subprocess.DEVNULL)
        self._on_pid = on_pid

    async def __aenter__(self):
        streams = await self._context.__aenter__()
        process = getattr(self._context, "process", None)
        generator = getattr(self._context, "gen", None)
        frame = getattr(generator, "ag_frame", None)
        if process is None and frame is not None:
            process = frame.f_locals.get("process")
        pid = getattr(process, "pid", None)
        if isinstance(pid, int) and pid > 0:
            self._on_pid(pid)
        return streams

    async def __aexit__(self, exc_type, exc_value, traceback):
        return await self._context.__aexit__(exc_type, exc_value, traceback)


def load_mcp_config(path: Path) -> dict:
    value = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(value, dict):
        raise ValueError("MCP config must be a mapping")
    return value


def policy_for(server_cfg: Mapping, defaults: Mapping, tool_name: str) -> Policy:
    if "instant" in server_cfg:
        return "instant" if tool_name in server_cfg.get("instant", ()) else "confirm"
    prefixes = defaults.get("instant_prefixes", ())
    return "instant" if any(tool_name.startswith(prefix) for prefix in prefixes) else "confirm"


def _blocked_tools(server_cfg: Mapping) -> frozenset[str]:
    blocked = server_cfg.get("blocked", ())
    if not isinstance(blocked, (list, tuple)) or not all(
        isinstance(name, str) and name for name in blocked
    ):
        raise ValueError("invalid MCP blocked tool list")
    return frozenset(blocked)


@asynccontextmanager
async def _stdio_session(
    _server_name: str,
    spec: StdioServerParameters,
    *,
    on_pid: Callable[[int], None] = lambda _pid: None,
):
    ClientSession, _, _ = _load_mcp_transport()
    async with _PidTrackingStdio(spec, on_pid) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            yield session


class McpServers:
    def __init__(
        self,
        config: Mapping,
        *,
        claude_config_path: Path = Path.home() / ".claude.json",
        session_factory: SessionFactory | None = None,
        on_server: ServerHook | None = None,
        account_values: Mapping[str, str] | None = None,
        killer: ProcessTreeKiller = kill_process_tree,
    ):
        self._config = config
        self._claude_config_path = Path(claude_config_path)
        self._session_factory = session_factory or self._default_session
        self._on_server = on_server
        self._account_values = dict(account_values or {})
        self._killer = killer
        self._sessions: dict[str, ClientSession] = {}
        self._call_settings: dict[str, tuple[Mapping, float]] = {}
        self._server_pids: dict[str, int] = {}
        self._server_tasks: dict[str, asyncio.Task[None]] = {}
        self._stop_events: dict[str, asyncio.Event] = {}
        self._closed = False
        servers = config.get("servers", {})
        self._status = {
            name: {"name": name, "connected": False, "tools": 0, "error": None}
            for name in servers
        }

    def _default_session(
        self,
        server_name: str,
        spec: StdioServerParameters,
    ) -> AsyncContextManager[ClientSession]:
        return _stdio_session(
            server_name,
            spec,
            on_pid=lambda pid: self._server_pids.__setitem__(server_name, pid),
        )

    async def connect(
        self,
        registry: ToolRegistry,
        *,
        on_server: ServerHook | None = None,
    ) -> None:
        if self._closed:
            raise RuntimeError("MCP servers are closed")
        servers = self._config.get("servers", {})
        defaults = self._config.get("defaults", {})
        timeout_s = float(defaults.get("connect_timeout_s", 20))
        server_hook = on_server or self._on_server
        ready_events = []
        for name, server_cfg in servers.items():
            ready = asyncio.Event()
            stop = asyncio.Event()
            self._stop_events[name] = stop
            self._server_tasks[name] = asyncio.create_task(
                self._run_server(
                    name,
                    server_cfg,
                    defaults,
                    timeout_s,
                    registry,
                    server_hook,
                    ready,
                    stop,
                ),
                name=f"mcp-{name}",
            )
            ready_events.append(ready)
        try:
            await asyncio.gather(*(ready.wait() for ready in ready_events))
        except asyncio.CancelledError:
            await self._cancel_server_tasks()
            raise

    async def _run_server(
        self,
        name: str,
        server_cfg: Mapping,
        defaults: Mapping,
        timeout_s: float,
        registry: ToolRegistry,
        on_server: ServerHook | None,
        ready: asyncio.Event,
        stop: asyncio.Event,
    ) -> None:
        stack = AsyncExitStack()
        try:
            async with asyncio.timeout(timeout_s):
                spec = self._resolve_spec(server_cfg)
                session = await stack.enter_async_context(self._session_factory(name, spec))
                listed = await session.list_tools()
                blocked = _blocked_tools(server_cfg)
                mirrored = [
                    self._mirror_tool(name, server_cfg, defaults, session, tool)
                    for tool in listed.tools
                    if tool.name not in blocked
                ]
                for tool in mirrored:
                    registry.register(tool)
            self._sessions[name] = session
            self._call_settings[name] = (
                server_cfg,
                float(defaults.get("call_timeout_s", 8)),
            )
            self._status[name].update(connected=True, tools=len(mirrored), error=None)
            if on_server is not None:
                try:
                    on_server(name, registry)
                except Exception as exc:
                    _LOGGER.warning("MCP server %s hook failed: %s",
                                    name, type(exc).__name__)
            ready.set()
            await stop.wait()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = type(exc).__name__
            self._status[name].update(connected=False, tools=0, error=error)
            _LOGGER.warning("MCP server %s connection failed: %s", name, error)
        finally:
            ready.set()
            self._sessions.pop(name, None)
            self._call_settings.pop(name, None)
            self._kill_server_tree(name)
            with suppress(Exception):
                await stack.aclose()

    def _resolve_spec(self, server_cfg: Mapping) -> StdioServerParameters:
        _, StdioServerParameters, _ = _load_mcp_transport()
        source: Mapping = server_cfg
        config_name = server_cfg.get("from_claude_config")
        if config_name:
            claude_config = json.loads(self._claude_config_path.read_text(encoding="utf-8"))
            source = claude_config["mcpServers"][config_name]
        return StdioServerParameters(
            command=source["command"],
            args=list(source.get("args", ())),
            env=dict(source["env"]) if source.get("env") is not None else None,
        )

    def _mirror_tool(self, server_name, server_cfg, defaults, session, remote_tool) -> Tool:
        timeout_s = float(defaults.get("call_timeout_s", 8))
        schema = _without_account_parameter(
            remote_tool.inputSchema,
            server_cfg.get("account_param"),
        )

        async def run(arguments: dict) -> str:
            text = await self._call_session(
                session,
                server_cfg,
                timeout_s,
                remote_tool.name,
                arguments,
            )
            return _bounded_text(text)

        return Tool(
            name=f"{server_name}__{remote_tool.name}",
            description=(remote_tool.description or "")[:512],
            input_schema=schema,
            policy=policy_for(server_cfg, defaults, remote_tool.name),
            run=run,
        )

    async def call_raw(
        self,
        server: str,
        tool: str,
        arguments: Mapping[str, Any],
    ) -> str:
        session = self._sessions.get(server)
        settings = self._call_settings.get(server)
        if session is None or settings is None:
            raise RuntimeError(f"{server} not connected")
        server_cfg, timeout_s = settings
        return await self._call_session(
            session,
            server_cfg,
            timeout_s,
            tool,
            arguments,
        )

    async def _call_session(
        self,
        session: ClientSession,
        server_cfg: Mapping,
        timeout_s: float,
        tool: str,
        arguments: Mapping[str, Any],
    ) -> str:
        if tool in _blocked_tools(server_cfg):
            raise McpToolError("unknown MCP tool")
        call_arguments = dict(arguments)
        account_param = server_cfg.get("account_param")
        if account_param is not None:
            if not isinstance(account_param, str) or not account_param:
                raise RuntimeError("invalid MCP account parameter")
            account_value = self._account_values.get(account_param)
            if not isinstance(account_value, str) or not account_value:
                raise RuntimeError("MCP account is not configured")
            call_arguments = _normalize_recipients(call_arguments, account_value)
            call_arguments.pop(account_param, None)
            call_arguments[account_param] = account_value
        result = await session.call_tool(
            tool,
            arguments=call_arguments,
            read_timeout_seconds=timedelta(seconds=timeout_s),
        )
        text = "".join(
            block.text
            for block in result.content
            if getattr(block, "type", None) == "text"
        )
        clean = _clean_text(text)
        is_error = bool(getattr(result, "isError", False))
        if is_error or clean.startswith("Error calling tool"):
            message = _bounded_text(clean) or "MCP tool call failed"
            raise McpToolError(message)
        return clean

    def status(self) -> list[dict]:
        return [dict(value) for value in self._status.values()]

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._sessions.clear()
        self._call_settings.clear()
        for name in tuple(self._server_tasks):
            self._kill_server_tree(name)
        for event in self._stop_events.values():
            event.set()
        tasks = list(self._server_tasks.values())
        if tasks:
            try:
                async with asyncio.timeout(10):
                    await asyncio.gather(*tasks, return_exceptions=True)
            except TimeoutError:
                await self._cancel_server_tasks()
        self._server_tasks.clear()
        self._stop_events.clear()
        for name in tuple(self._server_pids):
            self._kill_server_tree(name)
        self._server_pids.clear()

    async def _cancel_server_tasks(self) -> None:
        tasks = list(self._server_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _kill_server_tree(self, name: str) -> None:
        pid = self._server_pids.pop(name, None)
        if pid is None:
            return
        try:
            self._killer(pid, check=False)
        except Exception:
            _LOGGER.warning("MCP server %s process-tree cleanup failed", name)


def _bounded_text(value: str) -> str:
    clean = _clean_text(value)
    if len(clean) <= _MAX_CONTENT:
        return clean
    return clean[:_MAX_CONTENT - len(_TRUNCATED)] + _TRUNCATED


def _clean_text(value: str) -> str:
    return "".join(char for char in value
                   if char in "\n\t" or unicodedata.category(char) != "Cc")


def _normalize_recipients(arguments: Mapping[str, Any], account: str) -> dict[str, Any]:
    normalized = dict(arguments)
    local_part = account.partition("@")[0].strip().casefold()
    self_values = set(_SELF_RECIPIENTS)
    if local_part:
        self_values.add(local_part)
    for name, value in normalized.items():
        if name.casefold() not in _RECIPIENT_ARGUMENTS:
            continue
        if isinstance(value, str):
            if value.strip().casefold() in self_values:
                normalized[name] = account
            continue
        if isinstance(value, list):
            normalized[name] = [
                account
                if isinstance(item, str) and item.strip().casefold() in self_values
                else item
                for item in value
            ]
    return normalized


def _without_account_parameter(schema: dict, account_param: Any) -> dict:
    mirrored = deepcopy(schema)
    if not isinstance(account_param, str) or not account_param:
        return mirrored
    properties = mirrored.get("properties")
    if isinstance(properties, dict):
        properties.pop(account_param, None)
    required = mirrored.get("required")
    if isinstance(required, list):
        mirrored["required"] = [name for name in required if name != account_param]
    return mirrored
