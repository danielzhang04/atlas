"""MCP server discovery and bounded tool-call behavior."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import json
import os
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
import yaml


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

    class McpToolError(RuntimeError):
        pass

    tools_module.McpToolError = McpToolError
    tools_module.Tool = Tool
    tools_module.Policy = Literal["instant", "confirm"]
    sys.modules["worker.tools"] = tools_module

import worker.mcp_client as mcp_client
from worker.mcp_client import McpServers, load_mcp_config, policy_for
from worker.tools import McpToolError
from worker.tools import ToolRegistry


class FakeRegistry:
    def __init__(self) -> None:
        self.tools = []

    def register(self, tool) -> None:
        self.tools.append(tool)

    def unregister(self, name: str) -> bool:
        remaining = [tool for tool in self.tools if tool.name != name]
        removed = len(remaining) != len(self.tools)
        self.tools = remaining
        return removed


def test_import_does_not_load_external_mcp_package():
    root = Path(__file__).resolve().parents[1]
    code = (
        "import sys; "
        f"sys.path.insert(0, {str(root)!r}); "
        "import worker.mcp_client; "
        "assert 'mcp' not in sys.modules, sorted(name for name in sys.modules if name.startswith('mcp'))"
    )

    completed = subprocess.run(
        [sys.executable, "-I", "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


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

    monkeypatch.setattr(
        mcp_client,
        "_load_mcp_transport",
        lambda: (None, SimpleNamespace, fake_stdio_client),
    )

    async def scenario():
        with pytest.raises(StopSession):
            async with mcp_client._stdio_session(
                "google",
                SimpleNamespace(command="node"),
            ):
                pass

    asyncio.run(scenario())
    assert seen["spec"].command == "node"
    assert seen["errlog"] is subprocess.DEVNULL


def test_explicit_command_transport_suppresses_mcp_default_environment(monkeypatch):
    from mcp.client import stdio as mcp_stdio

    seen = {}

    class FakeStdioClient:
        async def __aenter__(self):
            seen["environment"] = {
                **mcp_stdio.get_default_environment(),
                **seen["spec"].env,
            }
            return "read", "write"

        async def __aexit__(self, *_args):
            return False

    def fake_stdio_client(spec, *, errlog):
        seen["spec"] = spec
        seen["errlog"] = errlog
        return FakeStdioClient()

    monkeypatch.setattr(
        mcp_client,
        "_load_mcp_transport",
        lambda: (None, SimpleNamespace, fake_stdio_client),
    )
    monkeypatch.setattr(
        mcp_stdio,
        "get_default_environment",
        lambda: {"MUST_NOT_ESCAPE": "private", "PATH": "broad"},
    )

    async def scenario():
        spec = SimpleNamespace(env={"PATH": "minimal", "SystemRoot": "C:/Windows"})
        async with mcp_client._PidTrackingStdio(
            spec,
            lambda _pid: None,
            exact_environment=True,
            enter_lock=asyncio.Lock(),
        ):
            pass

    asyncio.run(scenario())

    assert seen["environment"] == {
        "PATH": "minimal",
        "SystemRoot": "C:/Windows",
    }
    assert not any(
        value in seen["environment"].values()
        for name, value in os.environ.items()
        if name not in {"PATH", "SystemRoot"}
    )
    assert seen["errlog"] is subprocess.DEVNULL


def test_default_session_only_uses_exact_environment_for_command_servers(monkeypatch):
    from mcp.client import stdio as mcp_stdio

    environments = []

    class FakeStdioClient:
        def __init__(self, spec):
            self._spec = spec

        async def __aenter__(self):
            environments.append(
                {
                    **mcp_stdio.get_default_environment(),
                    **self._spec.env,
                }
            )
            return "read", "write"

        async def __aexit__(self, *_args):
            return False

    class FakeClientSession:
        def __init__(self, _read_stream, _write_stream):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def initialize(self):
            pass

    def fake_stdio_client(spec, *, errlog):
        assert errlog is subprocess.DEVNULL
        return FakeStdioClient(spec)

    monkeypatch.setattr(
        mcp_client,
        "_load_mcp_transport",
        lambda: (FakeClientSession, SimpleNamespace, fake_stdio_client),
    )
    monkeypatch.setattr(
        mcp_stdio,
        "get_default_environment",
        lambda: {"PATH": "mcp-default", "APPDATA": "profile"},
    )

    async def scenario():
        servers = McpServers(
            {
                "servers": {
                    "google": {"from_claude_config": "google-workspace"},
                    "kb": {"command": "node", "exact_environment": True},
                },
            }
        )
        async with servers._default_session(
            "google",
            SimpleNamespace(env={"GOOGLE_FEATURE": "enabled"}),
        ):
            pass
        async with servers._default_session(
            "kb",
            SimpleNamespace(env={"PATH": "minimal", "SystemRoot": "C:/Windows"}),
        ):
            pass

    asyncio.run(scenario())

    assert environments == [
        {
            "PATH": "mcp-default",
            "APPDATA": "profile",
            "GOOGLE_FEATURE": "enabled",
        },
        {
            "PATH": "minimal",
            "SystemRoot": "C:/Windows",
        },
    ]


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

    monkeypatch.setattr(
        mcp_client,
        "_load_mcp_transport",
        lambda: (FakeClientSession, SimpleNamespace, fake_stdio_client),
    )

    async def scenario():
        kills = []
        servers = McpServers(
            {
                "servers": {"google": {"command": "unused"}},
                "defaults": {"connect_timeout_s": 1},
            },
            killer=lambda pid, **kwargs: kills.append((pid, kwargs)),
        )
        await servers.connect(FakeRegistry())
        await servers.close()
        await servers.close()
        return kills

    kills = asyncio.run(scenario())

    assert kills == [(
        2468,
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
def test_mirrored_tool_raises_bounded_mcp_error_for_mcp_errors(is_error, text):
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

    with pytest.raises(McpToolError) as raised:
        asyncio.run(tool.run({}))

    message = str(raised.value)
    assert message.startswith(text[:40])
    assert len(message) <= 300

    registry = ToolRegistry()
    registry.register(tool)
    result = asyncio.run(registry.call(tool.name, {}))
    assert result.status == "error"
    assert result.content == message


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


def test_account_scoped_calls_normalize_self_recipient_aliases_item_wise():
    seen = []

    class FakeSession:
        async def call_tool(self, tool, *, arguments, read_timeout_seconds):
            seen.append((tool, arguments, read_timeout_seconds))
            return SimpleNamespace(content=[], isError=False)

    async def scenario():
        servers = McpServers(
            {"servers": {}},
            account_values={"user_google_email": "daniel@example.test"},
        )
        server_config = {"account_param": "user_google_email"}
        await servers._call_session(
            FakeSession(),
            server_config,
            8,
            "draft_gmail_message",
            {
                "to": " me ",
                "cc": "friend@example.test",
                "bcc": ["MYSELF", "other@example.test", " Daniel "],
                "recipient": "MY EMAIL",
                "attendees": ["my email", 7],
                "subject": "hello",
            },
        )

    asyncio.run(scenario())

    arguments = seen[0][1]
    assert arguments["to"] == "daniel@example.test"
    assert arguments["cc"] == "friend@example.test"
    assert arguments["bcc"] == [
        "daniel@example.test",
        "other@example.test",
        "daniel@example.test",
    ]
    assert arguments["recipient"] == "daniel@example.test"
    assert arguments["attendees"] == ["daniel@example.test", 7]
    assert arguments["subject"] == "hello"


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
        connected = list(observations)
        await servers.close()
        return connected, observations, [tool.name for tool in registry.tools]

    connected, observations, remaining = asyncio.run(scenario())

    assert connected == [(
        "google",
        [
            "google__get_events",
            "google__search_drive_files",
            "google__send_gmail_message",
        ],
    )] or connected == [(
        "google",
        [
            "google__get_events",
            "google__send_gmail_message",
            "google__search_drive_files",
        ],
    )]
    assert observations[-1] == ("google", [])
    assert remaining == []


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
        connected = list(called)
        await servers.close()
        return connected, called

    connected, called = asyncio.run(scenario())
    assert connected == ["google"]
    assert called == ["google", "google"]


def test_constructor_hook_runs_before_connect_hook():
    async def scenario():
        registry = FakeRegistry()
        called = []
        servers = McpServers(
            {
                "servers": {"google": {"command": "unused"}},
                "defaults": {"connect_timeout_s": 1},
            },
            session_factory=_memory_factory(_server()),
            on_server=lambda _name, _registry: called.append("configured"),
        )
        await servers.connect(
            registry,
            on_server=lambda _name, _registry: called.append("settle"),
        )
        connected = list(called)
        await servers.close()
        return connected

    assert asyncio.run(scenario()) == ["configured", "settle"]


def test_reconnect_replaces_server_tools_and_fires_one_rebuild():
    registry = ToolRegistry()
    servers = McpServers({"servers": {}})
    session = SimpleNamespace()

    def mirrored(remote_name):
        return servers._mirror_tool(
            "demo",
            {"instant": [remote_name]},
            {},
            session,
            SimpleNamespace(
                name=remote_name,
                description=remote_name,
                inputSchema={"type": "object", "properties": {}},
            ),
        )

    rebuilds = []
    hook = lambda name, current: rebuilds.append((name, tuple(current.names())))
    servers._replace_server_tools(
        "demo",
        registry,
        [mirrored("kept"), mirrored("removed")],
        hook,
    )
    rebuilds.clear()

    servers._replace_server_tools("demo", registry, [mirrored("kept")], hook)

    assert registry.names() == ["demo__kept"]
    assert rebuilds == [("demo", ("demo__kept",))]


def test_default_prefix_policy_applies_only_without_explicit_instant_list():
    defaults = {"instant_prefixes": ["get_", "list_", "search_", "query_", "read_", "check_"]}
    assert policy_for({}, defaults, "search_drive_files") == "instant"
    assert policy_for({}, defaults, "send_gmail_message") == "confirm"
    assert policy_for({"instant": ["get_events"]}, defaults, "search_drive_files") == "confirm"
    assert policy_for({"instant": ["get_events"]}, defaults, "get_events") == "instant"


def test_checked_in_chrome_devtools_config_has_explicit_safe_policy():
    config = load_mcp_config(Path(__file__).parents[1] / "config" / "mcp.yaml")
    server = config["servers"]["chrome-devtools"]
    defaults = config["defaults"]

    assert server["from_claude_config"] == "chrome-devtools"
    assert set(server["blocked"]) == {
        "get_network_request",
        "list_network_requests",
    }
    for name in ("list_pages", "get_console_message", "take_snapshot", "take_screenshot"):
        assert policy_for(server, defaults, name) == "instant"
    for name in ("navigate_page", "click", "fill", "evaluate_script"):
        assert policy_for(server, defaults, name) == "confirm"


def test_blocked_server_tools_are_never_registered_or_callable():
    calls = []

    class FakeSession:
        async def list_tools(self):
            return SimpleNamespace(tools=[
                SimpleNamespace(
                    name=name,
                    description=f"Fake {name} tool.",
                    inputSchema={"type": "object", "properties": {}},
                )
                for name in (
                    "list_pages",
                    "list_network_requests",
                    "get_network_request",
                )
            ])

        async def call_tool(self, name, **_kwargs):
            calls.append(name)
            return SimpleNamespace(
                content=[TextContent(
                    type="text",
                    text="Authorization: Bearer xyz",
                )],
                isError=False,
            )

    @asynccontextmanager
    async def factory(_server_name, _spec):
        yield FakeSession()

    async def scenario():
        registry = ToolRegistry()
        servers = McpServers(
            {
                "servers": {
                    "chrome-devtools": {
                        "command": "unused",
                        "instant": ["list_pages"],
                        "blocked": [
                            "get_network_request",
                            "list_network_requests",
                        ],
                    },
                },
                "defaults": {"connect_timeout_s": 1},
            },
            session_factory=factory,
        )
        await servers.connect(registry)
        registered = registry.names()
        schemas = registry.schemas()
        direct = await registry.call("chrome-devtools__get_network_request", {})
        with pytest.raises(McpToolError, match="unknown MCP tool"):
            await servers.call_raw("chrome-devtools", "get_network_request", {})
        await servers.close()
        return registered, schemas, direct

    registered, schemas, direct = asyncio.run(scenario())

    assert registered == ["chrome-devtools__list_pages"]
    assert [schema["name"] for schema in schemas] == registered
    assert direct.status == "error"
    assert direct.content == "unknown tool"
    assert calls == []


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
            killer=lambda pid, **kwargs: kills.append((pid, kwargs)),
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
        1357,
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


def test_connected_server_session_enters_and_exits_in_its_dedicated_task():
    async def scenario():
        task_ids = []
        exits = []

        @asynccontextmanager
        async def factory(_server_name, _spec):
            task_ids.append(id(asyncio.current_task()))
            try:
                async with create_connected_server_and_client_session(_server()) as session:
                    yield session
            finally:
                task_ids.append(id(asyncio.current_task()))
                exits.append("closed")

        servers = McpServers(
            {"servers": {"google": {"command": "unused"}}},
            session_factory=factory,
        )
        await servers.connect(FakeRegistry())
        server_task = servers._server_tasks["google"]
        await servers.close()
        return task_ids, exits, server_task.done()

    task_ids, exits, done = asyncio.run(scenario())

    assert task_ids[0] == task_ids[1]
    assert exits == ["closed"]
    assert done is True


def test_failed_server_task_ends_and_retains_disconnected_status():
    async def scenario():
        def factory(_server_name, _spec):
            raise LookupError("unavailable")

        servers = McpServers(
            {"servers": {"broken": {"command": "bad"}}},
            session_factory=factory,
        )
        await servers.connect(FakeRegistry())
        server_task = servers._server_tasks["broken"]
        status = servers.status()
        await servers.close()
        return status, server_task.done()

    status, done = asyncio.run(scenario())

    assert status == [
        {"name": "broken", "connected": False, "tools": 0, "error": "LookupError"},
    ]
    assert done is True


def test_load_mcp_config_rejects_scalar_command(tmp_path):
    path = tmp_path / "mcp.yaml"
    path.write_text("servers:\n  google:\n    command: node\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid MCP command argv"):
        load_mcp_config(path)


def test_command_config_requires_enabled_from(tmp_path):
    path = tmp_path / "mcp.yaml"
    path.write_text("servers:\n  kb:\n    command: [node]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid MCP enabled_from"):
        load_mcp_config(path)


def test_command_config_enabled_from_must_resolve_to_bool(tmp_path):
    mcp_path = tmp_path / "mcp.yaml"
    atlas_path = tmp_path / "atlas.yaml"
    mcp_path.write_text(
        "servers:\n  kb:\n    command: [node]\n"
        "    enabled_from: kb_bridge.enabled\n",
        encoding="utf-8",
    )
    atlas_path.write_text(
        'kb_bridge:\n  enabled: "yes"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="invalid MCP enabled_from value"):
        load_mcp_config(mcp_path, atlas_path=atlas_path)


def test_command_config_is_dormant_by_default_and_resolves_a_minimal_environment(
    tmp_path,
    monkeypatch,
):
    mcp_path = tmp_path / "mcp.yaml"
    atlas_path = tmp_path / "atlas.yaml"
    mcp_path.write_text(
        """servers:
  kb:
    command: [node, "{kb_bridge.path}/dist/server.js"]
    enabled_from: kb_bridge.enabled
    session_channel: true
    env_from:
      ATLAS_KB_BRIDGE_ENABLED: kb_bridge.enabled
      ATLAS_KB_MUTATIONS_ENABLED: kb_bridge.mutations
      ATLAS_KB_ORIGIN: kb_bridge.origin
""",
        encoding="utf-8",
    )
    atlas_path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("PATH", "minimal-path")
    monkeypatch.setenv("SystemRoot", "C:/Windows")
    monkeypatch.setenv("MUST_NOT_ESCAPE", "private")

    config = load_mcp_config(mcp_path, atlas_path=atlas_path)
    calls = []

    def factory(_name, _spec):
        calls.append(True)
        raise AssertionError("disabled command server was spawned")

    async def scenario():
        servers = McpServers(config, session_factory=factory)
        await servers.connect(FakeRegistry())
        status = servers.status()
        await servers.close()
        return status

    assert asyncio.run(scenario()) == [
        {
            "name": "kb",
            "connected": False,
            "tools": 0,
            "error": None,
            "session": "none",
        },
    ]
    assert calls == []

    atlas_path.write_text(
        """kb_bridge:
  enabled: true
  mutations: true
  path: C:/bridge
  origin: http://127.0.0.1:5317
""",
        encoding="utf-8",
    )
    enabled = load_mcp_config(mcp_path, atlas_path=atlas_path)
    spawned = []

    @asynccontextmanager
    async def enabled_factory(name, spec):
        spawned.append((name, spec))
        yield SimpleNamespace(list_tools=lambda: asyncio.sleep(
            0,
            result=SimpleNamespace(tools=[]),
        ))

    async def enabled_scenario():
        enabled_servers = McpServers(enabled, session_factory=enabled_factory)
        await enabled_servers.connect(FakeRegistry())
        await enabled_servers.close()

    asyncio.run(enabled_scenario())
    assert len(spawned) == 1
    name, spec = spawned[0]
    assert name == "kb"
    assert spec.command == "node"
    assert spec.args == ["C:/bridge/dist/server.js"]
    assert spec.env == {
        "ATLAS_KB_BRIDGE_ENABLED": "1",
        "ATLAS_KB_MUTATIONS_ENABLED": "1",
        "ATLAS_KB_ORIGIN": "http://127.0.0.1:5317",
        "PATH": "minimal-path",
        "SystemRoot": "C:/Windows",
    }


def test_kb_session_is_notified_after_initialize_and_on_refresh_without_logging(caplog):
    token = "operator-bearer-must-not-be-logged"
    refreshed = "refreshed-bearer-must-not-be-logged"
    notifications = []

    class FakeSession:
        async def list_tools(self):
            return SimpleNamespace(tools=[])

        async def send_notification(self, notification):
            notifications.append(notification)

    @asynccontextmanager
    async def factory(_server_name, _spec):
        yield FakeSession()

    async def scenario():
        servers = McpServers(
            {
                "servers": {
                    "kb": {
                        "command": "unused",
                        "session_channel": True,
                    },
                },
                "defaults": {"connect_timeout_s": 1},
            },
            session_factory=factory,
        )
        await servers.set_session("kb", token, "2099-01-01T00:00:00Z")
        await servers.connect(FakeRegistry())
        await servers.set_session("kb", refreshed, "2099-01-01T00:05:00Z")
        status = servers.status()
        await servers.close()
        return status

    status = asyncio.run(scenario())
    assert [item.method for item in notifications] == [
        "notifications/atlas/session",
        "notifications/atlas/session",
    ]
    assert [item.params for item in notifications] == [
        {"token": token, "expiresAt": "2099-01-01T00:00:00Z"},
        {"token": refreshed, "expiresAt": "2099-01-01T00:05:00Z"},
    ]
    assert status == [{
        "name": "kb",
        "connected": True,
        "tools": 0,
        "error": None,
        "session": "held",
    }]
    assert token not in caplog.text
    assert refreshed not in caplog.text
    assert token not in repr(status)
    assert refreshed not in repr(status)


def test_session_set_during_list_tools_is_notified_exactly_once():
    notifications = []

    class DelayedSession:
        def __init__(self):
            self.listing = asyncio.Event()
            self.release = asyncio.Event()

        async def list_tools(self):
            self.listing.set()
            await self.release.wait()
            return SimpleNamespace(tools=[])

        async def send_notification(self, notification):
            notifications.append(notification)

    session = DelayedSession()

    @asynccontextmanager
    async def factory(_server_name, _spec):
        yield session

    async def scenario():
        servers = McpServers(
            {
                "servers": {
                    "kb": {"command": "unused", "session_channel": True},
                },
                "defaults": {"connect_timeout_s": 1},
            },
            session_factory=factory,
        )
        connecting = asyncio.create_task(servers.connect(FakeRegistry()))
        await session.listing.wait()
        await servers.set_session("kb", "private", "2099-01-01T00:00:00Z")
        session.release.set()
        await connecting
        await servers.close()

    asyncio.run(scenario())
    assert len(notifications) == 1
    assert notifications[0].params == {
        "token": "private",
        "expiresAt": "2099-01-01T00:00:00Z",
    }


def test_kb_session_required_error_is_mapped_to_bounded_unlock_instruction():
    class FakeErrorSession:
        async def call_tool(self, *_args, **_kwargs):
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text='{"type":"session_required"}')],
                structuredContent={"type": "session_required"},
                isError=True,
            )

    servers = McpServers({"servers": {}})
    with pytest.raises(
        McpToolError,
        match="^kb is locked - say: Atlas, unlock kb$",
    ):
        asyncio.run(servers._call_session(
            FakeErrorSession(), {"session_channel": True}, 8, "kb_runs_list", {},
        ))


def test_non_session_server_keeps_generic_session_required_error():
    class FakeGoogleSession:
        async def call_tool(self, *_args, **_kwargs):
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text='{"type":"session_required"}')],
                structuredContent={"type": "session_required"},
                isError=True,
            )

    servers = McpServers({"servers": {}})
    with pytest.raises(McpToolError) as raised:
        asyncio.run(servers._call_session(
            FakeGoogleSession(), {}, 8, "google_search", {},
        ))
    assert str(raised.value) == '{"type":"session_required"}'


def test_t3_requires_dashboard_error_has_bounded_voice_mapping():
    class FakeKbSession:
        async def call_tool(self, *_args, **_kwargs):
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text="bridge refused response")],
                structuredContent={"type": "t3_requires_dashboard"},
                isError=True,
            )

    servers = McpServers({"servers": {}})
    with pytest.raises(McpToolError) as raised:
        asyncio.run(servers._call_session(
            FakeKbSession(), {"session_channel": True}, 8, "kb_human_respond", {},
        ))
    assert str(raised.value) == "that needs the dashboard - T3 is never done by voice"


def test_kb_session_rejects_expiry_inside_clock_skew_without_retaining_token():
    now = [1_000.0]
    servers = McpServers(
        {
            "servers": {
                "kb": {"command": "unused", "session_channel": True},
            },
        },
        wall_clock=lambda: now[0],
    )

    with pytest.raises(ValueError, match="MCP session expires too soon"):
        asyncio.run(servers.set_session("kb", "private", 1_030.0))
    assert servers.status()[0]["session"] == "none"
    assert servers._session_tokens == {}


def test_kb_health_flips_expired_then_erases_session_with_fake_clock():
    async def scenario():
        now = [1_000.0]
        servers = McpServers(
            {
                "servers": {
                    "kb": {"command": "unused", "session_channel": True},
                },
            },
            wall_clock=lambda: now[0],
        )
        await servers.set_session("kb", "private", 1_031.0)
        assert servers.status()[0]["session"] == "held"
        now[0] = 1_031.0
        expired = servers.status()
        assert expired[0]["session"] == "expired"
        assert servers._session_tokens["kb"][0] is None
        await asyncio.sleep(0)
        erased = servers.status()
        await servers.close()
        return expired, erased

    expired, erased = asyncio.run(scenario())
    assert "private" not in repr(expired)
    assert erased[0]["session"] == "none"


def test_checked_in_kb_config_lists_every_read_tool_and_confirms_every_other_tool():
    config_dir = Path(__file__).parents[1] / "config"
    atlas_config = yaml.safe_load(
        (config_dir / "atlas.yaml").read_text(encoding="utf-8"),
    )
    bridge_path = atlas_config["kb_bridge"]["path"]
    config = load_mcp_config(config_dir / "mcp.yaml")
    server = config["servers"]["kb"]
    defaults = config["defaults"]
    read_tools = {
        "kb_capabilities", "kb_agents_list", "kb_agent_get",
        "kb_workflows_list", "kb_workflow_get", "kb_runs_list", "kb_run_get",
        "kb_run_events", "kb_run_watch", "kb_inbox_list", "kb_schedules_list",
        "kb_deployment_inspect", "kb_asset_pull_inspect", "kb_repo_tree",
        "kb_repo_file", "kb_repo_history", "kb_repo_search",
        "kb_analytics_snapshot", "kb_trace_list", "kb_trace_get",
        "kb_terminal_list",
    }
    mutation_tools = {
        "kb_agent_create", "kb_agent_update", "kb_workflow_create",
        "kb_workflow_update", "kb_workflow_launch", "kb_agent_launch",
        "kb_human_respond", "kb_review_dispatch", "kb_schedule_create",
        "kb_schedule_set_armed", "kb_schedule_delete", "kb_deployment_action",
        "kb_asset_pull_action", "kb_terminal_close", "kb_run_control",
    }

    assert set(server["instant"]) == read_tools
    assert all(policy_for(server, defaults, name) == "instant" for name in read_tools)
    assert all(policy_for(server, defaults, name) == "confirm" for name in mutation_tools)
    assert policy_for(server, defaults, "kb_future_mutation") == "confirm"
    assert [server["command"], *server["args"]] == [
        "node", f"{bridge_path}/dist/server.js",
    ]
    assert "\\" not in bridge_path
    assert bridge_path[0].isalpha() and bridge_path[1:3] == ":/"
    assert bridge_path.endswith("/atlas-bridge")
    assert server["enabled"] is True
    assert set(server["env"]) == {
        "ATLAS_KB_BRIDGE_ENABLED",
        "ATLAS_KB_MUTATIONS_ENABLED",
        "ATLAS_KB_ORIGIN",
        "PATH",
        "SystemRoot",
    }


def test_kb_tool_descriptions_do_not_claim_to_enforce_t3():
    servers = McpServers({"servers": {}})
    tool = servers._mirror_tool(
        "kb",
        {"instant": []},
        {},
        SimpleNamespace(),
        SimpleNamespace(
            name="kb_run_control",
            description="MUTATION Control a run.",
            inputSchema={"type": "object", "properties": {}},
        ),
    )

    assert "T3 approvals" not in tool.description
    assert tool.policy == "confirm"
