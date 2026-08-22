"""Connect configured MCP servers and mirror their tools into the Atlas registry."""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from datetime import timedelta
import json
import logging
from pathlib import Path
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
    async with stdio_client(spec) as (read_stream, write_stream):
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
    ):
        self._config = config
        self._claude_config_path = Path(claude_config_path)
        self._session_factory = session_factory or _stdio_session
        self._stacks: dict[str, AsyncExitStack] = {}
        self._closed = False
        servers = config.get("servers", {})
        self._status = {
            name: {"name": name, "connected": False, "tools": 0, "error": None}
            for name in servers
        }

    async def connect(self, registry: ToolRegistry) -> None:
        if self._closed:
            raise RuntimeError("MCP servers are closed")
        servers = self._config.get("servers", {})
        defaults = self._config.get("defaults", {})
        timeout_s = float(defaults.get("connect_timeout_s", 20))
        await asyncio.gather(*(
            self._connect_one(name, server_cfg, defaults, timeout_s, registry)
            for name, server_cfg in servers.items()
        ))

    async def _connect_one(
        self,
        name: str,
        server_cfg: Mapping,
        defaults: Mapping,
        timeout_s: float,
        registry: ToolRegistry,
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
            self._status[name].update(connected=True, tools=len(mirrored), error=None)
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

        async def run(arguments: dict) -> str:
            result = await session.call_tool(
                remote_tool.name,
                arguments=arguments,
                read_timeout_seconds=timedelta(seconds=timeout_s),
            )
            text = "".join(
                block.text for block in result.content
                if getattr(block, "type", None) == "text"
            )
            return _bounded_text(text)

        return Tool(
            name=f"{server_name}__{remote_tool.name}",
            description=(remote_tool.description or "")[:512],
            input_schema=remote_tool.inputSchema,
            policy=policy_for(server_cfg, defaults, remote_tool.name),
            run=run,
        )

    def status(self) -> list[dict]:
        return [dict(value) for value in self._status.values()]

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        stacks = list(reversed(self._stacks.values()))
        self._stacks.clear()
        for stack in stacks:
            with suppress(Exception):
                await stack.aclose()


def _bounded_text(value: str) -> str:
    clean = "".join(char for char in value if char in "\n\t" or unicodedata.category(char) != "Cc")
    if len(clean) <= _MAX_CONTENT:
        return clean
    return clean[:_MAX_CONTENT - len(_TRUNCATED)] + _TRUNCATED
