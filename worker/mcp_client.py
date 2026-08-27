from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager, suppress
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import subprocess
import time
from typing import Any, AsyncContextManager, TYPE_CHECKING
import unicodedata
import weakref

import yaml

from .jobobject import kill_process_tree
from .tools import McpToolError, Policy, Tool

if TYPE_CHECKING:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters
    from .tools import ToolRegistry


__all__ = ["McpServers", "McpSessionError", "load_mcp_config", "policy_for"]


_LOGGER = logging.getLogger(__name__)
_MAX_CONTENT = 4_096
_SESSION_EXPIRY_SKEW_S = 30.0
_RECIPIENT_ARGUMENTS = frozenset({"to", "cc", "bcc", "recipient", "attendees"})
_SELF_RECIPIENTS = frozenset({"me", "myself", "my email"})
_KB_BRIDGE_DEFAULTS = {
    "enabled": False,
    "mutations": False,
    "path": "C:/Users/danie/kb/dashboard/atlas-bridge",
    "origin": "http://127.0.0.1:5317",
}
_TRUNCATED = "…[truncated]"


SessionFactory = Callable[
    [str, "StdioServerParameters"], AsyncContextManager["ClientSession"]
]
ServerHook = Callable[[str, "ToolRegistry"], None]
ProcessTreeKiller = Callable[..., subprocess.CompletedProcess]
_active_servers: weakref.ReferenceType | None = None


class McpSessionError(ValueError):
    """A short-lived MCP session was rejected before retention."""


def active_mcp_servers() -> "McpServers | None":
    current = _active_servers() if _active_servers is not None else None
    return None if current is None or current._closed else current


def _load_mcp_transport():
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    return ClientSession, StdioServerParameters, stdio_client


class _PidTrackingStdio(AbstractAsyncContextManager):
    def __init__(
        self,
        spec: StdioServerParameters,
        on_pid: Callable[[int], None],
        *,
        exact_environment: bool = False,
        enter_lock: asyncio.Lock | None = None,
    ) -> None:
        _, _, stdio_client = _load_mcp_transport()
        self._context = stdio_client(spec, errlog=subprocess.DEVNULL)
        self._on_pid = on_pid
        self._exact_environment = exact_environment
        self._enter_lock = enter_lock or asyncio.Lock()

    async def __aenter__(self):
        async with self._enter_lock:
            restore_environment = None
            if self._exact_environment:
                from mcp.client import stdio as mcp_stdio

                restore_environment = mcp_stdio.get_default_environment
                mcp_stdio.get_default_environment = lambda: {}
            try:
                streams = await self._context.__aenter__()
            finally:
                if restore_environment is not None:
                    mcp_stdio.get_default_environment = restore_environment
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


def load_mcp_config(path: Path, *, atlas_path: Path | None = None) -> dict:
    value = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(value, dict):
        raise ValueError("MCP config must be a mapping")
    servers = value.get("servers", {})
    if not isinstance(servers, dict):
        raise ValueError("MCP servers must be a mapping")
    if any(
        isinstance(server, dict)
        and (
            "command" in server
            or "enabled_from" in server
            or "env_from" in server
        )
        for server in servers.values()
    ):
        atlas_file = Path(atlas_path) if atlas_path is not None else Path(path).with_name("atlas.yaml")
        atlas = yaml.safe_load(atlas_file.read_text(encoding="utf-8")) if atlas_file.exists() else {}
        if atlas is None:
            atlas = {}
        if not isinstance(atlas, dict):
            raise ValueError("Atlas config must be a mapping")
        value["servers"] = {
            name: _resolve_command_config(server, atlas)
            for name, server in servers.items()
        }
    return value


def _resolve_command_config(server_cfg: Any, atlas: Mapping) -> Any:
    if not isinstance(server_cfg, dict):
        return server_cfg
    if "command" not in server_cfg:
        return server_cfg
    command = server_cfg.get("command")
    if not isinstance(command, list):
        raise ValueError("invalid MCP command argv")
    if not command or not all(isinstance(item, str) and item for item in command):
        raise ValueError("invalid MCP command argv")
    bridge = dict(_KB_BRIDGE_DEFAULTS)
    configured = atlas.get("kb_bridge", {})
    if configured is not None:
        if not isinstance(configured, dict):
            raise ValueError("invalid Atlas kb_bridge config")
        bridge.update(configured)

    def setting(reference: Any) -> Any:
        if not isinstance(reference, str) or not reference.startswith("kb_bridge."):
            raise ValueError("invalid MCP Atlas config reference")
        name = reference.removeprefix("kb_bridge.")
        if name not in bridge:
            raise ValueError("unknown MCP Atlas config reference")
        return bridge[name]

    resolved = dict(server_cfg)
    resolved_argv = [
        item.replace("{kb_bridge.path}", str(bridge["path"]))
        for item in command
    ]
    resolved["command"] = resolved_argv[0]
    resolved["args"] = resolved_argv[1:]
    resolved["exact_environment"] = True
    if "enabled_from" not in resolved:
        raise ValueError("invalid MCP enabled_from")
    enabled_from = resolved.pop("enabled_from")
    enabled = setting(enabled_from)
    if not isinstance(enabled, bool):
        raise ValueError("invalid MCP enabled_from value")
    resolved["enabled"] = enabled
    env_from = resolved.pop("env_from", {})
    if not isinstance(env_from, dict) or not all(
        isinstance(name, str) and name and isinstance(reference, str)
        for name, reference in env_from.items()
    ):
        raise ValueError("invalid MCP command environment mapping")
    child_env = {
        name: _environment_value(setting(reference))
        for name, reference in env_from.items()
    }
    child_env["PATH"] = os.environ.get("PATH", os.defpath)
    child_env["SystemRoot"] = os.environ.get("SystemRoot", "C:/Windows")
    resolved["env"] = child_env
    return resolved


def _environment_value(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, str):
        return value
    raise ValueError("invalid MCP command environment value")


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
    exact_environment: bool = False,
    enter_lock: asyncio.Lock | None = None,
):
    ClientSession, _, _ = _load_mcp_transport()
    async with _PidTrackingStdio(
        spec,
        on_pid,
        exact_environment=exact_environment,
        enter_lock=enter_lock,
    ) as (read_stream, write_stream):
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
        wall_clock: Callable[[], float] = time.time,
    ):
        global _active_servers
        self._config = config
        self._claude_config_path = Path(claude_config_path)
        self._session_factory = session_factory or self._default_session
        self._on_server = on_server
        self._account_values = dict(account_values or {})
        self._killer = killer
        self._wall_clock = wall_clock
        self._stdio_enter_lock = asyncio.Lock()
        self._session_lock = asyncio.Lock()
        self._sessions: dict[str, ClientSession] = {}
        self._session_tokens: dict[str, tuple[str | None, Any, float]] = {}
        self._session_generations: dict[str, int] = {}
        self._session_notifications: dict[str, tuple[ClientSession, int]] = {}
        self._session_expiry_handles: dict[str, asyncio.TimerHandle] = {}
        self._call_settings: dict[str, tuple[Mapping, float]] = {}
        self._server_pids: dict[str, int] = {}
        self._server_tasks: dict[str, asyncio.Task[None]] = {}
        self._stop_events: dict[str, asyncio.Event] = {}
        self._server_tools: dict[str, dict[str, Tool]] = {}
        self._closed = False
        servers = config.get("servers", {})
        self._status = {
            name: {"name": name, "connected": False, "tools": 0, "error": None}
            for name in servers
        }
        if any(
            isinstance(server_cfg, Mapping)
            and server_cfg.get("session_channel") is True
            for server_cfg in servers.values()
        ):
            _active_servers = weakref.ref(self)

    def _default_session(
        self,
        server_name: str,
        spec: StdioServerParameters,
    ) -> AsyncContextManager[ClientSession]:
        server_cfg = self._config.get("servers", {}).get(server_name, {})
        return _stdio_session(
            server_name,
            spec,
            on_pid=lambda pid: self._server_pids.__setitem__(server_name, pid),
            exact_environment=(
                isinstance(server_cfg, Mapping)
                and server_cfg.get("exact_environment") is True
            ),
            enter_lock=self._stdio_enter_lock,
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
        server_hook = self._compose_server_hooks(on_server)
        ready_events = []
        for name, server_cfg in servers.items():
            if isinstance(server_cfg, Mapping) and server_cfg.get("enabled", True) is not True:
                continue
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
        session = None
        try:
            async with asyncio.timeout(timeout_s):
                spec = self._resolve_spec(server_cfg)
                session = await stack.enter_async_context(self._session_factory(name, spec))
                async with self._session_lock:
                    self._sessions[name] = session
                    generation = self._session_generations.get(name, 0)
                    await self._notify_held_session(name, session)
                listed = await session.list_tools()
                async with self._session_lock:
                    if self._session_generations.get(name, 0) != generation:
                        await self._notify_held_session(name, session)
                blocked = _blocked_tools(server_cfg)
                mirrored = [
                    self._mirror_tool(name, server_cfg, defaults, session, tool)
                    for tool in listed.tools
                    if tool.name not in blocked
                ]
            self._call_settings[name] = (
                server_cfg,
                float(defaults.get("call_timeout_s", 8)),
            )
            self._status[name].update(connected=True, tools=len(mirrored), error=None)
            self._replace_server_tools(name, registry, mirrored, on_server)
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
            self._call_settings.pop(name, None)
            self._status[name].update(connected=False, tools=0)
            self._replace_server_tools(name, registry, [], on_server)
            async with self._session_lock:
                if self._sessions.get(name) is session:
                    self._sessions.pop(name, None)
                notified = self._session_notifications.get(name)
                if notified is not None and notified[0] is session:
                    self._session_notifications.pop(name, None)
            self._kill_server_tree(name)
            with suppress(Exception):
                await stack.aclose()

    def _compose_server_hooks(self, extra: ServerHook | None) -> ServerHook | None:
        hooks = []
        if self._on_server is not None:
            hooks.append(self._on_server)
        if extra is not None and extra is not self._on_server:
            hooks.append(extra)
        if not hooks:
            return None

        def composed(name: str, registry: ToolRegistry) -> None:
            for hook in hooks:
                try:
                    hook(name, registry)
                except Exception as exc:
                    _LOGGER.warning(
                        "MCP server %s hook failed: %s",
                        name,
                        type(exc).__name__,
                    )

        return composed

    def _replace_server_tools(
        self,
        name: str,
        registry: ToolRegistry,
        tools: list[Tool],
        on_server: ServerHook | None,
    ) -> None:
        replacement = {tool.name: tool for tool in tools}
        if len(replacement) != len(tools):
            raise ValueError(f"duplicate MCP tool from server: {name}")
        previous = self._server_tools.get(name, {})
        previous_names = frozenset(previous)
        replacement_names = frozenset(replacement)

        for tool_name in previous:
            registry.unregister(tool_name)
        registered = []
        try:
            for tool in replacement.values():
                registry.register(tool)
                registered.append(tool.name)
        except Exception:
            for tool_name in registered:
                registry.unregister(tool_name)
            for tool in previous.values():
                registry.register(tool)
            raise

        if replacement:
            self._server_tools[name] = replacement
        else:
            self._server_tools.pop(name, None)
        if replacement_names != previous_names and on_server is not None:
            on_server(name, registry)

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

    async def set_session(self, server: str, token: str, expires_at: Any) -> None:
        server_cfg = self._config.get("servers", {}).get(server)
        if not isinstance(server_cfg, Mapping) or server_cfg.get("session_channel") is not True:
            raise ValueError("MCP server has no session channel")
        if not isinstance(token, str) or not token:
            raise McpSessionError("invalid MCP session")
        try:
            expiry = _expiry_timestamp(expires_at)
        except ValueError as exc:
            raise McpSessionError(str(exc)) from None
        now = self._wall_clock()
        if expiry <= now + _SESSION_EXPIRY_SKEW_S:
            raise McpSessionError("MCP session expires too soon")
        async with self._session_lock:
            generation = self._session_generations.get(server, 0) + 1
            self._session_generations[server] = generation
            self._session_tokens[server] = (token, expires_at, expiry)
            self._schedule_session_expiry(server, generation, expiry)
            session = self._sessions.get(server)
            if session is not None:
                await self._notify_held_session(server, session)

    def session_origin(self, server: str) -> str | None:
        server_cfg = self._config.get("servers", {}).get(server)
        if (
            not isinstance(server_cfg, Mapping)
            or server_cfg.get("enabled", True) is not True
            or server_cfg.get("session_channel") is not True
        ):
            return None
        environment = server_cfg.get("env")
        origin = environment.get("ATLAS_KB_ORIGIN") if isinstance(environment, Mapping) else None
        return origin if isinstance(origin, str) and origin else None

    async def _notify_held_session(self, server: str, session: ClientSession) -> None:
        held = self._session_tokens.get(server)
        if held is None:
            return
        token, expires_at, expiry = held
        if token is None or expiry <= self._wall_clock():
            self._mark_session_expired(
                server,
                self._session_generations.get(server, 0),
                expiry,
            )
            return
        generation = self._session_generations.get(server, 0)
        notified = self._session_notifications.get(server)
        if notified is not None and notified[0] is session and notified[1] == generation:
            return
        await self._send_session_notification(session, token, expires_at)
        self._session_notifications[server] = (session, generation)

    def _schedule_session_expiry(self, server: str, generation: int, expiry: float) -> None:
        previous = self._session_expiry_handles.pop(server, None)
        if previous is not None:
            previous.cancel()
        delay = max(0.0, expiry - self._wall_clock())
        self._session_expiry_handles[server] = asyncio.get_running_loop().call_later(
            delay,
            self._expire_session,
            server,
            generation,
            expiry,
        )

    def _expire_session(self, server: str, generation: int, expiry: float) -> None:
        if self._session_generations.get(server) != generation:
            return
        remaining = expiry - self._wall_clock()
        if remaining > 0:
            self._session_expiry_handles[server] = asyncio.get_running_loop().call_later(
                remaining,
                self._expire_session,
                server,
                generation,
                expiry,
            )
            return
        self._mark_session_expired(server, generation, expiry)

    def _mark_session_expired(self, server: str, generation: int, expiry: float) -> None:
        held = self._session_tokens.get(server)
        if held is None or self._session_generations.get(server) != generation:
            return
        _, expires_at, held_expiry = held
        if held_expiry != expiry:
            return
        self._session_tokens[server] = (None, expires_at, expiry)
        handle = self._session_expiry_handles.pop(server, None)
        if handle is not None:
            handle.cancel()
        try:
            asyncio.get_running_loop().call_soon(
                self._erase_expired_session,
                server,
                generation,
                expiry,
            )
        except RuntimeError:
            pass

    def _erase_expired_session(self, server: str, generation: int, expiry: float) -> None:
        held = self._session_tokens.get(server)
        if (
            held is not None
            and held[0] is None
            and held[2] == expiry
            and self._session_generations.get(server) == generation
        ):
            self._session_tokens.pop(server, None)
            self._session_notifications.pop(server, None)

    @staticmethod
    async def _send_session_notification(
        session: ClientSession,
        token: str,
        expires_at: Any,
    ) -> None:
        from mcp.types import JSONRPCNotification

        await session.send_notification(JSONRPCNotification(
            jsonrpc="2.0",
            method="notifications/atlas/session",
            params={"token": token, "expiresAt": expires_at},
        ))

    def _mirror_tool(self, server_name, server_cfg, defaults, session, remote_tool) -> Tool:
        timeout_s = float(defaults.get("call_timeout_s", 8))
        schema = _without_account_parameter(
            remote_tool.inputSchema,
            server_cfg.get("account_param"),
        )
        description = remote_tool.description or ""

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
            description=description[:512],
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
            if (
                server_cfg.get("session_channel") is True
                and _typed_error(result, clean, "t3_requires_dashboard")
            ):
                raise McpToolError(
                    "that needs the dashboard - T3 is never done by voice"
                )
            if (
                server_cfg.get("session_channel") is True
                and _session_required(result, clean)
            ):
                raise McpToolError("kb is locked - say: Atlas, unlock kb")
            message = _bounded_text(clean) or "MCP tool call failed"
            raise McpToolError(message)
        return clean

    def status(self) -> list[dict]:
        projected = []
        servers = self._config.get("servers", {})
        for name, value in self._status.items():
            item = dict(value)
            server_cfg = servers.get(name, {})
            if isinstance(server_cfg, Mapping) and server_cfg.get("session_channel") is True:
                item["session"] = self._session_status(name)
            projected.append(item)
        return projected

    def _session_status(self, server: str) -> str:
        held = self._session_tokens.get(server)
        if held is None:
            return "none"
        token, expires_at, expiry = held
        if expiry <= self._wall_clock():
            self._mark_session_expired(
                server,
                self._session_generations.get(server, 0),
                expiry,
            )
            return "expired"
        return "held" if token is not None else "expired"

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        async with self._session_lock:
            self._sessions.clear()
            self._session_tokens.clear()
            self._session_generations.clear()
            self._session_notifications.clear()
            for handle in self._session_expiry_handles.values():
                handle.cancel()
            self._session_expiry_handles.clear()
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


def _expiry_timestamp(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("invalid MCP session expiry")
    if isinstance(value, (int, float)):
        expiry = float(value)
        if expiry > 10_000_000_000:
            expiry /= 1_000
        if expiry > 0:
            return expiry
        raise ValueError("invalid MCP session expiry")
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("invalid MCP session expiry") from None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    raise ValueError("invalid MCP session expiry")


def _session_required(result: Any, clean_text: str) -> bool:
    return _typed_error(result, clean_text, "session_required")


def _typed_error(result: Any, clean_text: str, expected: str) -> bool:
    structured = getattr(result, "structuredContent", None)
    if structured is None:
        structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict) and any(
        structured.get(name) == expected
        for name in ("type", "code", "error")
    ):
        return True
    try:
        decoded = json.loads(clean_text)
    except (TypeError, ValueError):
        return False
    return isinstance(decoded, dict) and any(
        decoded.get(name) == expected
        for name in ("type", "code", "error")
    )


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
