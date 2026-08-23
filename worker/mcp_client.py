"""Connect configured MCP servers and mirror their tools into the Atlas registry."""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from copy import deepcopy
from datetime import timedelta
import json
import logging
from pathlib import Path
import subprocess
from typing import Any, AsyncContextManager, TYPE_CHECKING
import unicodedata

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
import yaml

from .tools import Policy, Tool

if TYPE_CHECKING:
    from .tools import ToolRegistry


__all__ = ["McpServers", "load_mcp_config", "policy_for"]


_LOGGER = logging.getLogger(__name__)
_MAX_CONTENT = 4_096
_TRUNCATED = "…[truncated]"


SessionFactory = Callable[
    [str, StdioServerParameters], AsyncContextManager[ClientSession]
]
ServerHook = Callable[[str, "ToolRegistry"], None]


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


@asynccontextmanager
async def _stdio_session(
    _server_name: str, spec: StdioServerParameters
):
    async with stdio_client(spec, errlog=subprocess.DEVNULL) as (read_stream, write_stream):
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
    ):
        self._config = config
        self._claude_config_path = Path(claude_config_path)
        self._session_factory = session_factory or _stdio_session
        self._on_server = on_server
        self._account_values = dict(account_values or {})
        self._stacks: dict[str, AsyncExitStack] = {}
        self._sessions: dict[str, ClientSession] = {}
        self._call_settings: dict[str, tuple[Mapping, float]] = {}
        self._closed = False
        servers = config.get("servers", {})
        self._status = {
            name: {"name": name, "connected": False, "tools": 0, "error": None}
            for name in servers
        }

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
        await asyncio.gather(*(
            self._connect_one(name, server_cfg, defaults, timeout_s, registry, server_hook)
            for name, server_cfg in servers.items()
        ))

    async def _connect_one(
        self,
        name: str,
        server_cfg: Mapping,
        defaults: Mapping,
        timeout_s: float,
        registry: ToolRegistry,
        on_server: ServerHook | None,
    ) -> None:
        stack = AsyncExitStack()
        try:
            async with asyncio.timeout(timeout_s):
                spec = self._resolve_spec(server_cfg)
                session = await stack.enter_async_context(self._session_factory(name, spec))
                listed = await session.list_tools()
                mirrored = [
                    self._mirror_tool(name, server_cfg, defaults, session, tool)
                    for tool in listed.tools
                ]
                for tool in mirrored:
                    registry.register(tool)
            self._stacks[name] = stack
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
        except Exception as exc:
            with suppress(Exception):
                await stack.aclose()
            error = type(exc).__name__
            self._status[name].update(connected=False, tools=0, error=error)
            _LOGGER.warning("MCP server %s connection failed: %s", name, error)

    def _resolve_spec(self, server_cfg: Mapping) -> StdioServerParameters:
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
        """Call one connected server tool without the mirrored content bound."""
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
        call_arguments = dict(arguments)
        account_param = server_cfg.get("account_param")
        if account_param is not None:
            if not isinstance(account_param, str) or not account_param:
                raise RuntimeError("invalid MCP account parameter")
            account_value = self._account_values.get(account_param)
            if not isinstance(account_value, str) or not account_value:
                raise RuntimeError("MCP account is not configured")
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
        return _clean_text(text)

    def status(self) -> list[dict]:
        return [dict(value) for value in self._status.values()]

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._sessions.clear()
        self._call_settings.clear()
        stacks = list(reversed(self._stacks.values()))
        self._stacks.clear()
        for stack in stacks:
            with suppress(Exception):
                await stack.aclose()


def _bounded_text(value: str) -> str:
    clean = _clean_text(value)
    if len(clean) <= _MAX_CONTENT:
        return clean
    return clean[:_MAX_CONTENT - len(_TRUNCATED)] + _TRUNCATED


def _clean_text(value: str) -> str:
    return "".join(
        char
        for char in value
        if char in "\n\t" or unicodedata.category(char) != "Cc"
    )


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
