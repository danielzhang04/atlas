"""MCP server discovery and bounded tool-call behavior."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import sys
from types import ModuleType
from typing import Any, Awaitable, Callable, Literal

from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import TextContent


try:
    import worker.tools  # noqa: F401
except ModuleNotFoundError:
    tools_module = ModuleType("worker.tools")

    @dataclass(frozen=True, slots=True)
    class Tool:
        name: str
        description: str
        input_schema: dict
        run: Callable[[dict], Awaitable[Any]]
        policy: Literal["instant", "confirm"] = "instant"

    tools_module.Tool = Tool
    tools_module.Policy = Literal["instant", "confirm"]
    sys.modules["worker.tools"] = tools_module

from worker.mcp_client import McpServers, load_mcp_config, policy_for


class FakeRegistry:
    def __init__(self) -> None:
        self.tools = []

    def register(self, tool) -> None:
        self.tools.append(tool)


def _server() -> FastMCP:
    server = FastMCP("test-google")

    @server.tool(description="Read calendar events.")
    def get_events(calendar: str = "primary") -> list[TextContent]:
        return [
            TextContent(type="text", text="first\x00"),
            TextContent(type="text", text="x" * 5_000 + "\x1f"),
        ]

    @server.tool(description="Send a Gmail message.")
    def send_gmail_message(to: str, subject: str) -> str:
        return f"sent {subject} to {to}"

    @server.tool(description="Search Drive files.")
    def search_drive_files(query: str) -> str:
        return f"found {query}"

    return server


def _memory_factory(server: FastMCP, exits: list[str] | None = None):
    @asynccontextmanager
    async def factory(_server_name, _spec):
        try:
            async with create_connected_server_and_client_session(server) as session:
                yield session
        finally:
            if exits is not None:
                exits.append("closed")

    return factory


def test_connect_registers_schema_policy_and_bounded_concatenated_text():
    async def scenario():
        registry = FakeRegistry()
        servers = McpServers(
            {
                "servers": {
                    "google": {
                        "command": "unused",
                        "instant": ["get_events", "search_drive_files"],
                    },
                },
                "defaults": {"instant_prefixes": ["get_"], "connect_timeout_s": 1},
            },
            session_factory=_memory_factory(_server()),
        )
        await servers.connect(registry)
        tools = {tool.name: tool for tool in registry.tools}
        content = await tools["google__get_events"].run({"calendar": "primary"})
        status = servers.status()
        await servers.close()
        return tools, content, status

    tools, content, status = asyncio.run(scenario())
    assert set(tools) == {
        "google__get_events",
        "google__send_gmail_message",
        "google__search_drive_files",
    }
    assert tools["google__get_events"].description == "Read calendar events."
    assert tools["google__get_events"].input_schema["type"] == "object"
    assert "calendar" in tools["google__get_events"].input_schema["properties"]
    assert tools["google__get_events"].policy == "instant"
    assert tools["google__search_drive_files"].policy == "instant"
    assert tools["google__send_gmail_message"].policy == "confirm"
    assert content.startswith("firstx")
    assert len(content) == 4_096
    assert content.endswith("…[truncated]")
    assert "\x00" not in content and "\x1f" not in content
    assert status == [{"name": "google", "connected": True, "tools": 3, "error": None}]


def test_default_prefix_policy_applies_only_without_explicit_instant_list():
    defaults = {"instant_prefixes": ["get_", "list_", "search_", "query_", "read_", "check_"]}
    assert policy_for({}, defaults, "search_drive_files") == "instant"
    assert policy_for({}, defaults, "send_gmail_message") == "confirm"
    assert policy_for({"instant": ["get_events"]}, defaults, "search_drive_files") == "confirm"
    assert policy_for({"instant": ["get_events"]}, defaults, "get_events") == "instant"


def test_one_factory_failure_is_class_only_and_does_not_block_other_server(caplog):
    async def scenario():
        good_factory = _memory_factory(_server())

        def factory(server_name, spec):
            if server_name == "broken":
                raise RuntimeError("must-not-appear-secret")
            return good_factory(server_name, spec)

        registry = FakeRegistry()
        servers = McpServers(
            {
                "servers": {
                    "broken": {"command": "bad"},
                    "google": {"command": "unused", "instant": ["get_events"]},
                },
                "defaults": {"connect_timeout_s": 1},
            },
            session_factory=factory,
        )
        await servers.connect(registry)
        result = servers.status(), [tool.name for tool in registry.tools]
        await servers.close()
        return result

    (status, names) = asyncio.run(scenario())
    assert status == [
        {"name": "broken", "connected": False, "tools": 0, "error": "RuntimeError"},
        {"name": "google", "connected": True, "tools": 3, "error": None},
    ]
    assert names == [
        "google__get_events",
        "google__search_drive_files",
        "google__send_gmail_message",
    ] or names == [
        "google__get_events",
        "google__send_gmail_message",
        "google__search_drive_files",
    ]
    assert "RuntimeError" in caplog.text
    assert "must-not-appear-secret" not in caplog.text


def test_servers_connect_concurrently():
    async def scenario():
        entered = set()
        both_entered = asyncio.Event()
        apps = {"one": _server(), "two": _server()}

        @asynccontextmanager
        async def factory(server_name, _spec):
            entered.add(server_name)
            if len(entered) == 2:
                both_entered.set()
            await asyncio.wait_for(both_entered.wait(), timeout=0.2)
            async with create_connected_server_and_client_session(apps[server_name]) as session:
                yield session

        registry = FakeRegistry()
        servers = McpServers(
            {
                "servers": {"one": {"command": "unused"}, "two": {"command": "unused"}},
                "defaults": {"connect_timeout_s": 1},
            },
            session_factory=factory,
        )
        await servers.connect(registry)
        status = servers.status()
        await servers.close()
        return status

    assert asyncio.run(scenario()) == [
        {"name": "one", "connected": True, "tools": 3, "error": None},
        {"name": "two", "connected": True, "tools": 3, "error": None},
    ]


def test_connect_timeout_is_reported_by_class_name():
    @asynccontextmanager
    async def factory(_server_name, _spec):
        await asyncio.sleep(1)
        yield None

    async def scenario():
        servers = McpServers(
            {
                "servers": {"slow": {"command": "unused"}},
                "defaults": {"connect_timeout_s": 0.01},
            },
            session_factory=factory,
        )
        await servers.connect(FakeRegistry())
        status = servers.status()
        await servers.close()
        return status

    assert asyncio.run(scenario()) == [
        {"name": "slow", "connected": False, "tools": 0, "error": "TimeoutError"},
    ]


def test_claude_config_resolves_child_spec_without_leaking_env(tmp_path, caplog):
    secret = "test-secret-value"
    claude_config = tmp_path / "claude.json"
    claude_config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "google-workspace": {
                        "command": "node",
                        "args": ["server.js"],
                        "env": {"ACCESS_TOKEN": secret},
                    },
                },
            },
        ),
        encoding="utf-8",
    )
    seen = []

    def factory(_server_name, spec):
        seen.append(spec)
        raise LookupError(f"failed while env contained {secret}")

    async def scenario():
        servers = McpServers(
            {"servers": {"google": {"from_claude_config": "google-workspace"}}},
            claude_config_path=claude_config,
            session_factory=factory,
        )
        await servers.connect(FakeRegistry())
        status = servers.status()
        await servers.close()
        await servers.close()
        return status

    status = asyncio.run(scenario())
    assert seen[0].command == "node"
    assert seen[0].args == ["server.js"]
    assert seen[0].env == {"ACCESS_TOKEN": secret}
    assert status == [{"name": "google", "connected": False, "tools": 0, "error": "LookupError"}]
    assert secret not in caplog.text
    assert secret not in repr(status)


def test_close_is_idempotent():
    async def scenario():
        exits = []
        servers = McpServers(
            {"servers": {"google": {"command": "unused"}}},
            session_factory=_memory_factory(_server(), exits),
        )
        await servers.connect(FakeRegistry())
        await servers.close()
        await servers.close()
        return exits

    assert asyncio.run(scenario()) == ["closed"]


def test_load_mcp_config_reads_mapping(tmp_path):
    path = tmp_path / "mcp.yaml"
    path.write_text("servers:\n  google:\n    command: node\n", encoding="utf-8")
    assert load_mcp_config(path) == {"servers": {"google": {"command": "node"}}}
