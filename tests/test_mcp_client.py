"""MCP server discovery and bounded tool-call behavior."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import sys
from types import ModuleType
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Literal

from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import TextContent
import pytest


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

import worker.mcp_client as mcp_client
from worker.mcp_client import McpServers, load_mcp_config, policy_for
from worker.tools import ToolRegistry


class FakeRegistry:
    def __init__(self) -> None:
        self.tools = []

    def register(self, tool) -> None:
        self.tools.append(tool)


def test_stdio_session_discards_child_stderr(monkeypatch):
    class StopSession(Exception):
        pass

    class FakeStdioClient:
        async def __aenter__(self):
            raise StopSession

        async def __aexit__(self, *_args):
            return False

    seen = {}

    def fake_stdio_client(spec, *, errlog):
        seen["spec"] = spec
        seen["errlog"] = errlog
        return FakeStdioClient()

    monkeypatch.setattr(mcp_client, "stdio_client", fake_stdio_client)

    async def scenario():
        with pytest.raises(StopSession):
            async with mcp_client._stdio_session(
                "google",
                mcp_client.StdioServerParameters(command="node"),
            ):
                pass

    asyncio.run(scenario())
    assert seen["spec"].command == "node"
    assert seen["errlog"] is subprocess.DEVNULL


def test_default_stdio_session_records_pid_and_close_force_kills_tree(monkeypatch):
    events = []

    class FakeStdioClient:
        process = SimpleNamespace(pid=2468)

        async def __aenter__(self):
            events.append("stdio enter")
            return "read", "write"

        async def __aexit__(self, *_args):
            events.append("stdio exit")
            return False

    class FakeClientSession:
        def __init__(self, read_stream, write_stream):
            assert (read_stream, write_stream) == ("read", "write")

        async def __aenter__(self):
            events.append("session enter")
            return self

        async def __aexit__(self, *_args):
            events.append("session exit")
            return False

        async def initialize(self):
            events.append("initialize")

        async def list_tools(self):
            return SimpleNamespace(tools=[])

    def fake_stdio_client(spec, *, errlog):
        assert spec.command == "unused"
        assert errlog is subprocess.DEVNULL
        return FakeStdioClient()

    monkeypatch.setattr(mcp_client, "stdio_client", fake_stdio_client)
    monkeypatch.setattr(mcp_client, "ClientSession", FakeClientSession)

    async def scenario():
        kills = []
        servers = McpServers(
            {
                "servers": {"google": {"command": "unused"}},
                "defaults": {"connect_timeout_s": 1},
            },
            killer=lambda command, **kwargs: kills.append((command, kwargs)),
        )
        await servers.connect(FakeRegistry())
        await servers.close()
        await servers.close()
        return kills

    kills = asyncio.run(scenario())

    assert kills == [(
        ["taskkill", "/T", "/F", "/PID", "2468"],
        {"check": False},
    )]
    assert events == [
        "stdio enter",
        "session enter",
        "initialize",
        "session exit",
        "stdio exit",
    ]


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


def _account_server() -> FastMCP:
    server = FastMCP("test-account")

    @server.tool(description="Search Gmail messages.")
    def search_gmail_messages(
        query: str,
        user_google_email: str,
    ) -> list[TextContent]:
        return [
            TextContent(
                type="text",
                text=f"account={user_google_email};query={query};" + "x" * 5_000,
            ),
            TextContent(type="text", text="\nNext page token: complete-token"),
        ]

    return server


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


@pytest.mark.parametrize(
    ("is_error", "text"),
    [
        (True, "API error " + "x" * 5_000),
        (False, "Error calling tool 'draft_gmail_message': HttpError 400"),
    ],
)
def test_mirrored_tool_raises_bounded_runtime_error_for_mcp_errors(is_error, text):
    class FakeErrorSession:
        async def call_tool(self, *_args, **_kwargs):
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=text)],
                isError=is_error,
            )

    servers = McpServers({"servers": {}})
    remote_tool = SimpleNamespace(
        name="draft_gmail_message",
        description="Draft a Gmail message.",
        inputSchema={"type": "object", "properties": {}},
    )
    server_config = {"instant": ["draft_gmail_message"]}
    tool = servers._mirror_tool(
        "google",
        server_config,
        {},
        FakeErrorSession(),
        remote_tool,
    )

    with pytest.raises(RuntimeError) as raised:
        asyncio.run(tool.run({}))

    message = str(raised.value)
    assert message.startswith(text[:40])
    assert len(message) <= 4_096
    if len(text) > 4_096:
        assert message.endswith("…[truncated]")

    registry = ToolRegistry()
    registry.register(tool)
    result = asyncio.run(registry.call(tool.name, {}))
    assert result.status == "error"


def test_account_parameter_is_hidden_and_host_injected_for_mirrored_and_raw_calls():
    async def scenario():
        registry = FakeRegistry()
        servers = McpServers(
            {
                "servers": {
                    "google": {
                        "command": "unused",
                        "account_param": "user_google_email",
                    },
                },
                "defaults": {"connect_timeout_s": 1},
            },
            session_factory=_memory_factory(_account_server()),
            account_values={"user_google_email": "owner@example.test"},
        )
        await servers.connect(registry)
        tool = registry.tools[0]
        mirrored = await tool.run({
            "query": "in:inbox",
            "user_google_email": "attacker@example.test",
        })
        raw = await servers.call_raw(
            "google",
            "search_gmail_messages",
            {
                "query": "in:inbox",
                "user_google_email": "attacker@example.test",
            },
        )
        await servers.close()
        return tool, mirrored, raw

    tool, mirrored, raw = asyncio.run(scenario())

    properties = tool.input_schema["properties"]
    required = tool.input_schema.get("required", [])
    assert "user_google_email" not in properties
    assert "user_google_email" not in required
    assert mirrored.startswith("account=owner@example.test;query=in:inbox;")
    assert len(mirrored) == 4_096
    assert "attacker@example.test" not in mirrored
    assert raw.startswith("account=owner@example.test;query=in:inbox;")
    assert len(raw) > 5_000
    assert raw.endswith("Next page token: complete-token")
    assert "attacker@example.test" not in raw


def test_connect_runs_the_server_hook_after_successful_tool_registration():
    async def scenario():
        registry = FakeRegistry()
        observations = []
        servers = McpServers(
            {
                "servers": {"google": {"command": "unused"}},
                "defaults": {"connect_timeout_s": 1},
            },
            session_factory=_memory_factory(_server()),
        )
        await servers.connect(
            registry,
            on_server=lambda name, current: observations.append(
                (name, [tool.name for tool in current.tools]),
            ),
        )
        await servers.close()
        return observations

    observations = asyncio.run(scenario())

    assert observations == [(
        "google",
        [
            "google__get_events",
            "google__search_drive_files",
            "google__send_gmail_message",
        ],
    )] or observations == [(
        "google",
        [
            "google__get_events",
            "google__send_gmail_message",
            "google__search_drive_files",
        ],
    )]


def test_constructor_hook_is_used_when_connect_has_no_override():
    async def scenario():
        registry = FakeRegistry()
        called = []
        servers = McpServers(
            {
                "servers": {"google": {"command": "unused"}},
                "defaults": {"connect_timeout_s": 1},
            },
            session_factory=_memory_factory(_server()),
            on_server=lambda name, _registry: called.append(name),
        )
        await servers.connect(registry)
        await servers.close()
        return called

    assert asyncio.run(scenario()) == ["google"]


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


def test_cancelled_connection_kills_recorded_tree_and_closes_transport():
    events = []

    @asynccontextmanager
    async def factory(_server_name, _spec):
        events.append("entered")
        try:
            yield SimpleNamespace(
                list_tools=lambda: asyncio.sleep(30),
            )
        finally:
            events.append("closed")

    async def scenario():
        kills = []
        servers = McpServers(
            {
                "servers": {"slow": {"command": "unused"}},
                "defaults": {"connect_timeout_s": 60},
            },
            session_factory=factory,
            killer=lambda command, **kwargs: kills.append((command, kwargs)),
        )
        servers._server_pids["slow"] = 1357
        task = asyncio.create_task(servers.connect(FakeRegistry()))
        while events != ["entered"]:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await servers.close()
        return kills

    kills = asyncio.run(scenario())

    assert kills == [(
        ["taskkill", "/T", "/F", "/PID", "1357"],
        {"check": False},
    )]
    assert events == ["entered", "closed"]


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
