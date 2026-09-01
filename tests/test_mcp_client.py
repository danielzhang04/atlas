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
from typing import Any, Awaitable, Callable, Literal, Mapping

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
        prepare: Any = None
        execute_prepared: Any = None
        domain: str | None = None
        escalate: Callable[[Mapping], bool] | None = None

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
    assert status == [{
        "name": "google", "connected": True, "tools": 3, "error": None,
        "state": "connected", "detail": "ready",
    }]


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
    assert server["domain"] == "browser"
    assert set(server["blocked"]) == {
        "get_network_request",
        "list_network_requests",
    }
    assert set(server["expose"]) == {
        "navigate_page", "take_snapshot", "click", "fill",
        "take_screenshot", "list_pages", "wait_for",
    }
    for name in ("list_pages", "take_snapshot", "take_screenshot"):
        assert policy_for(server, defaults, name) == "instant"
    for name in ("navigate_page", "click", "fill", "wait_for"):
        assert policy_for(server, defaults, name) == "confirm"


def test_checked_in_google_config_curates_a_core_set_with_tiered_mutations():
    config = load_mcp_config(Path(__file__).parents[1] / "config" / "mcp.yaml")
    server = config["servers"]["google"]
    defaults = config["defaults"]

    assert server["domain"] == "google"
    # Pins the Track C3 version pin itself: a version bump or a --tools
    # typo in the checked-in config fails this test rather than silently
    # spawning a different workspace-mcp release or tool set.
    assert server["args_override"] == [
        "workspace-mcp==1.25.2", "--tools", "gmail", "drive", "calendar",
    ]
    assert set(server["expose"]) == {
        "search_gmail_messages", "get_gmail_message_content", "get_gmail_thread_content",
        "list_gmail_labels", "get_events", "list_calendars", "query_freebusy",
        "search_drive_files", "list_drive_items", "get_drive_file_content",
        "send_gmail_message", "draft_gmail_message",
        "get_drive_shareable_link", "manage_event",
    }
    for name in (
        "search_gmail_messages", "get_gmail_message_content", "get_gmail_thread_content",
        "list_gmail_labels", "get_events", "list_calendars", "query_freebusy",
        "search_drive_files", "list_drive_items", "get_drive_file_content",
    ):
        assert policy_for(server, defaults, name) == "instant"
    # Named in the plan as tiered mutations, both now confirm. A name-lying
    # read (get_drive_shareable_link creates a sharing grant) stays confirm
    # outright. manage_event was instant for action: create via instant_when
    # until the BB-wave review (blocker 1) pointed out that an instant create
    # still accepts attendees:/send_updates:, i.e. sends real email to third
    # parties on a model assertion -- rule 5. EVERY calendar mutation
    # confirms now, and there is no instant_when rule for it at all; a
    # future instant create needs an argument-PRESENCE predicate, which the
    # value-allowlist instant_when compiles today cannot express.
    assert policy_for(server, defaults, "get_drive_shareable_link") == "confirm"
    assert policy_for(server, defaults, "manage_event") == "confirm"
    assert "manage_event" not in server["instant"]
    assert "instant_when" not in server
    # The escalate machinery itself stays in place and tested (it is general,
    # and is the right shape for the next tool whose safe subset is a set of
    # argument values) -- it just has no rules configured. Every instant
    # google tool therefore compiles to no escalate hook.
    for name in server["instant"]:
        assert mcp_client._compile_instant_when(server, name) is None
    # Product decision: send_gmail_message/draft_gmail_message are exposed
    # (everyday send/draft is the point) but not in instant: -- and
    # never_instant's "send" pattern independently forces confirm on
    # send_gmail_message even if it were ever added there by mistake.
    assert "send_gmail_message" not in server["instant"]
    assert "draft_gmail_message" not in server["instant"]
    assert policy_for(server, defaults, "send_gmail_message") == "confirm"
    assert policy_for(server, defaults, "draft_gmail_message") == "confirm"


def test_checked_in_files_config_lists_every_read_tool_and_confirms_every_mutation():
    """Official @modelcontextprotocol/server-filesystem (Track C2): 8 reads
    instant, the 4 mutations (write_file, edit_file, create_directory,
    move_file) confirm by omission from instant: -- there is no delete tool
    on this server at all, a structural guardrail rather than a policy
    choice made here."""
    config = load_mcp_config(Path(__file__).parents[1] / "config" / "mcp.yaml")
    server = config["servers"]["files"]
    defaults = config["defaults"]

    assert server["domain"] == "files"
    # enabled_from is consumed during spec-resolution (see
    # _resolve_command_config); the checked-in atlas.yaml's non-empty
    # file_write_roots resolves it to True here. The reference itself and its
    # empty/malformed-roots behavior are covered directly by
    # test_file_roots_enabled_reference_tracks_whether_file_roots_is_non_empty
    # and test_file_roots_reference_rejects_malformed_file_roots_config.
    assert server["enabled"] is True
    read_tools = {
        "read_text_file", "read_multiple_files",
        "list_directory", "list_directory_with_sizes", "directory_tree",
        "search_files", "get_file_info", "list_allowed_directories",
    }
    mutation_tools = {"write_file", "edit_file", "create_directory", "move_file"}
    assert set(server["expose"]) == read_tools | mutation_tools
    assert set(server["instant"]) == read_tools
    assert "read_file" not in server["expose"]  # deprecated alias of read_text_file
    # BB-wave review, finding 11: read_media_file returns base64 image/audio
    # through the same 4096-char _MAX_CONTENT bound as every other MCP
    # result, so it can only ever deliver a truncated, unusable fragment.
    assert "read_media_file" not in server["expose"]
    assert "delete" not in {t.lower() for t in server["expose"]}
    for name in read_tools:
        assert policy_for(server, defaults, name) == "instant"
    for name in mutation_tools:
        # Neither in instant: (checked above) nor caught by instant_prefixes
        # (write_/edit_/create_/move_ are not among get_/list_/search_/
        # query_/read_/check_) -- confirmed by omission, not by
        # never_instant (none of write_file/edit_file/create_directory/
        # move_file contain a never_instant substring either).
        assert not any(
            name.startswith(prefix) for prefix in defaults["instant_prefixes"]
        )
        assert policy_for(server, defaults, name) == "confirm"
    # Every mutation's description states the confirm gate; the two read
    # overrides (BB-wave review, finding 11) state the limits that make a
    # naive call useless -- read_text_file's 4KB truncation, and that
    # search_files' plain paths are not the handles open_file/open_folder
    # need after a tainting read.
    assert set(server["describe"]) == mutation_tools | {"read_text_file", "search_files"}
    for name in mutation_tools:
        description = server["describe"][name].lower()
        assert "confirm" in description or "yes" in description
    assert "4kb" in server["describe"]["read_text_file"].lower()
    assert "launch_work" in server["describe"]["read_text_file"]
    assert "find_file" in server["describe"]["search_files"]
    assert "handle" in server["describe"]["search_files"].lower()

    # Blocker 2 (write scope): this server -- the only component with write
    # tools -- is started with file_write_roots, NOT file_roots, so the kb
    # checkout stays a read root reachable only through the built-in
    # LocalFiles tools. The argv token and the enabled_from reference must
    # both name the write list, or one of the two silently reintroduces kb.
    raw = yaml.safe_load(
        (Path(__file__).parents[1] / "config" / "mcp.yaml").read_text(encoding="utf-8"),
    )["servers"]["files"]
    assert "{file_write_roots}" in raw["command"]
    assert "{file_roots}" not in raw["command"]
    assert raw["enabled_from"] == "file_write_roots.enabled"
    atlas = yaml.safe_load(
        (Path(__file__).parents[1] / "config" / "atlas.yaml").read_text(encoding="utf-8"),
    )
    assert atlas["file_write_roots"] == ["known:Desktop", "known:Documents", "known:Downloads"]
    # The write list is a strict subset of the read list, and kb is in the
    # difference -- the whole point of the split.
    assert set(atlas["file_write_roots"]) < set(atlas["file_roots"])
    assert not any("kb" in root.casefold() for root in atlas["file_write_roots"])
    assert any("kb" in root.casefold() for root in atlas["file_roots"])
    # The resolved argv the server actually spawns with contains no kb path.
    assert not any("kb" in argument.casefold() for argument in server["args"])


def test_checked_in_config_registered_tool_count_is_at_or_under_budget(tmp_path):
    """Net prompt surface: kb (32, a commented constant -- kb tools require a
    live bridge connection this test does not make; see
    handoffs/2026-08-27-atlas-xwave.md) + google (38->14) +
    chrome-devtools (27->7) + files (12, Track C2 minus read_media_file --
    BB-wave review finding 11) + built-in host tools (constructed here via
    the real builtin()/register_count_mail() registration path, not
    guessed) = 84. This is OVER the 72 C0 set
    (docs/plans/2026-08-31-atlas-bb-wave-plan.md Track C0), itself already
    2-3x the ~30-50 model-accuracy threshold the survey named -- the plan's
    Track C2 row prioritizes the files domain for this wave over holding
    72, and flags 84 for a per-turn tool-projection follow-up (config/
    mcp.yaml's top-of-file comment carries the same note). This test locks
    in the actual number so a future change to it is a deliberate, visible
    diff rather than a silent drift.
    """
    from worker.localfiles import LocalFiles
    from worker.tools import ToolRegistry, builtin, register_count_mail

    config = load_mcp_config(Path(__file__).parents[1] / "config" / "mcp.yaml")
    google_expose = config["servers"]["google"]["expose"]
    chrome_expose = config["servers"]["chrome-devtools"]["expose"]
    files_expose = config["servers"]["files"]["expose"]
    assert len(google_expose) == 14
    assert len(chrome_expose) == 7
    assert len(files_expose) == 12

    class _FakeJob:
        job_id = "job"
        title = "t"
        state = "done"
        created_at = "now"

    class _FakeWork:
        def launch(self, _title, _brief):
            return _FakeJob()

        def active(self):
            return []

        def recent(self, _n):
            return []

        def cancel(self, _job_id):
            return _FakeJob()

    async def _search(_arguments):
        return "Found 0 messages matching x"

    builtin_registry = ToolRegistry()
    files = LocalFiles([str(tmp_path)], opener=lambda _path: None)
    builtin(builtin_registry, {}, _FakeWork(), files=files)
    register_count_mail(builtin_registry, _search)
    builtin_count = len(builtin_registry.schemas())

    KB_COUNT = 32  # commented constant: see docstring above

    total = (
        KB_COUNT + len(google_expose) + len(chrome_expose)
        + len(files_expose) + builtin_count
    )
    # Track C2 (files) pushes the curated total from 72 to 84 -- over the
    # C0 budget, flagged rather than hidden; see this test's docstring.
    assert builtin_count == 19
    assert total == 84
    assert total < 116  # still net negative against the pre-C0 baseline


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
                # connect_retries: 1: this test is about a single broken
                # server not blocking a healthy one, not about retry.
                "defaults": {"connect_timeout_s": 1, "connect_retries": 1},
            },
            session_factory=factory,
        )
        await servers.connect(registry)
        result = servers.status(), [tool.name for tool in registry.tools]
        await servers.close()
        return result

    (status, names) = asyncio.run(scenario())
    assert status == [
        {
            "name": "broken", "connected": False, "tools": 0,
            "error": "RuntimeError", "state": "error", "detail": "spawn failed",
        },
        {
            "name": "google", "connected": True, "tools": 3, "error": None,
            "state": "connected", "detail": "ready",
        },
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
        {
            "name": "one", "connected": True, "tools": 3, "error": None,
            "state": "connected", "detail": "ready",
        },
        {
            "name": "two", "connected": True, "tools": 3, "error": None,
            "state": "connected", "detail": "ready",
        },
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
                # connect_retries: 1 isolates the single-attempt timeout
                # classification this test is about from the retry-with-
                # backoff behavior covered by its own tests.
                "defaults": {"connect_timeout_s": 0.01, "connect_retries": 1},
            },
            session_factory=factory,
        )
        await servers.connect(FakeRegistry())
        status = servers.status()
        await servers.close()
        return status

    assert asyncio.run(scenario()) == [
        {
            "name": "slow", "connected": False, "tools": 0,
            "error": "TimeoutError", "state": "error",
            "detail": "handshake timeout after 0.01s",
        },
    ]


def test_status_distinguishes_connecting_and_disabled_configuration():
    servers = McpServers({
        "servers": {
            "google": {"command": "node"},
            "kb": {"command": "node", "enabled": False, "session_channel": True},
        },
    })

    assert servers.status() == [
        {
            "name": "google", "connected": False, "tools": 0, "error": None,
            "state": "connecting", "detail": "connection pending",
        },
        {
            "name": "kb", "connected": False, "tools": 0, "error": None,
            "state": "not_configured", "detail": "disabled by configuration",
            "session": "none",
        },
    ]


@pytest.mark.parametrize(
    ("failure", "expected_error", "expected_detail"),
    [
        (FileNotFoundError("private path"), "FileNotFoundError", "executable not found: missing.exe"),
        (OSError("private spawn detail"), "OSError", "spawn failed"),
        (
            ExceptionGroup(
                "private group detail",
                [type("McpError", (Exception,), {})("Connection closed")],
            ),
            "ExceptionGroup",
            "closed during initialize",
        ),
        (
            type("McpError", (Exception,), {})("Authentication required: private URL"),
            "McpError",
            "session required",
        ),
    ],
)
def test_connection_failures_have_closed_bounded_details(failure, expected_error, expected_detail):
    def factory(_server_name, _spec):
        raise failure

    async def scenario():
        servers = McpServers(
            {
                "servers": {"broken": {"command": "C:/private/path/missing.exe?token=secret"}},
                # connect_retries: 1: this test is about single-attempt
                # detail classification (spawn_failed is otherwise retried).
                "defaults": {"connect_retries": 1},
            },
            session_factory=factory,
        )
        await servers.connect(FakeRegistry())
        result = servers.status()
        await servers.close()
        return result

    assert asyncio.run(scenario()) == [{
        "name": "broken", "connected": False, "tools": 0,
        "error": expected_error, "state": "error", "detail": expected_detail,
    }]
    assert len(expected_detail) <= 120
    assert "private" not in expected_detail and "secret" not in expected_detail


def test_missing_claude_config_entry_is_not_configured(tmp_path):
    claude_config = tmp_path / "claude.json"
    claude_config.write_text('{"mcpServers": {}}', encoding="utf-8")

    async def scenario():
        servers = McpServers(
            {"servers": {"google": {"from_claude_config": "google-workspace"}}},
            claude_config_path=claude_config,
        )
        await servers.connect(FakeRegistry())
        result = servers.status()
        await servers.close()
        return result

    assert asyncio.run(scenario()) == [{
        "name": "google", "connected": False, "tools": 0, "error": "KeyError",
        "state": "not_configured", "detail": "config entry missing",
    }]


def test_missing_claude_config_file_is_not_configured(tmp_path):
    async def scenario():
        servers = McpServers(
            {"servers": {"google": {"from_claude_config": "google-workspace"}}},
            claude_config_path=tmp_path / "missing.json",
        )
        await servers.connect(FakeRegistry())
        result = servers.status()
        await servers.close()
        return result

    assert asyncio.run(scenario()) == [{
        "name": "google", "connected": False, "tools": 0,
        "error": "FileNotFoundError", "state": "not_configured",
        "detail": "config file missing",
    }]


def test_malformed_claude_config_is_an_error(tmp_path):
    claude_config = tmp_path / "claude.json"
    claude_config.write_text("{malformed", encoding="utf-8")

    async def scenario():
        servers = McpServers(
            {"servers": {"google": {"from_claude_config": "google-workspace"}}},
            claude_config_path=claude_config,
        )
        await servers.connect(FakeRegistry())
        result = servers.status()
        await servers.close()
        return result

    assert asyncio.run(scenario()) == [{
        "name": "google", "connected": False, "tools": 0,
        "error": "JSONDecodeError", "state": "error",
        "detail": "config malformed",
    }]


def test_unreadable_claude_config_is_an_error(tmp_path, monkeypatch):
    claude_config = tmp_path / "claude.json"
    claude_config.write_text("{}", encoding="utf-8")
    original_read_text = Path.read_text

    def deny_read(path, *args, **kwargs):
        if path == claude_config:
            raise PermissionError("private path")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny_read)

    async def scenario():
        servers = McpServers(
            {"servers": {"google": {"from_claude_config": "google-workspace"}}},
            claude_config_path=claude_config,
        )
        await servers.connect(FakeRegistry())
        result = servers.status()
        await servers.close()
        return result

    assert asyncio.run(scenario()) == [{
        "name": "google", "connected": False, "tools": 0,
        "error": "PermissionError", "state": "error",
        "detail": "config unreadable",
    }]


def test_transport_import_failure_is_an_error(monkeypatch):
    def unavailable():
        raise ImportError("private import detail")

    monkeypatch.setattr(mcp_client, "_load_mcp_transport", unavailable)

    async def scenario():
        servers = McpServers({"servers": {"google": {"command": "node"}}})
        await servers.connect(FakeRegistry())
        result = servers.status()
        await servers.close()
        return result

    assert asyncio.run(scenario()) == [{
        "name": "google", "connected": False, "tools": 0,
        "error": "ImportError", "state": "error",
        "detail": "transport unavailable",
    }]


def test_status_detail_vocabulary_rejects_unknown_pairs():
    servers = McpServers({"servers": {"google": {"command": "node"}}})

    with pytest.raises(ValueError, match="invalid MCP status detail"):
        servers._set_status(
            "google",
            state="error",
            detail="private arbitrary detail",
            connected=False,
            tools=0,
            error="RuntimeError",
        )

    expected = {
        ("connecting", "connection pending"),
        ("connecting", "retrying (attempt 2 of 3)"),
        ("connected", "ready"),
        ("configured", "signed executable found"),
        ("not_configured", "disabled by configuration"),
        ("not_configured", "config file missing"),
        ("not_configured", "config entry missing"),
        ("not_configured", "signed executable not found: tool.exe"),
        ("error", "config unreadable"),
        ("error", "config malformed"),
        ("error", "transport unavailable"),
        ("error", "handshake timeout after 0.01s"),
        ("error", "session required"),
        ("error", "closed during initialize"),
        ("error", "tool listing failed"),
        ("error", "executable not found: tool.exe"),
        ("error", "spawn failed"),
        ("error", "profile check failed"),
        ("error", "status unavailable"),
    }
    assert all(mcp_client._status_detail_allowed(*pair) for pair in expected)


def test_tool_listing_failure_is_reported_without_exception_text():
    class FailedListing:
        async def list_tools(self):
            raise RuntimeError("private child output")

    @asynccontextmanager
    async def factory(_server_name, _spec):
        yield FailedListing()

    async def scenario():
        servers = McpServers(
            {"servers": {"google": {"command": "node"}}},
            session_factory=factory,
        )
        await servers.connect(FakeRegistry())
        result = servers.status()
        await servers.close()
        return result

    assert asyncio.run(scenario()) == [{
        "name": "google", "connected": False, "tools": 0, "error": "RuntimeError",
        "state": "error", "detail": "tool listing failed",
    }]


def test_status_transition_hook_ignores_polls_and_reconnect_replaces_detail():
    """A fresh top-level connect() call (e.g. a future manual "reconnect")
    is a different thing from the automatic per-attempt retry-with-backoff
    covered elsewhere -- connect_retries: 1 isolates that here."""
    attempts = [OSError("first private failure"), FileNotFoundError("second private failure")]
    transitions = []

    def factory(_server_name, _spec):
        raise attempts.pop(0)

    async def scenario():
        servers = McpServers(
            {
                "servers": {"google": {"command": "C:/private/tool.exe"}},
                "defaults": {"connect_retries": 1},
            },
            session_factory=factory,
            on_state=lambda name, state, snapshot: transitions.append((name, state, snapshot)),
        )
        await servers.connect(FakeRegistry())
        servers.status()
        servers.status()
        await servers.connect(FakeRegistry())
        result = servers.status()
        await servers.close()
        return result

    assert asyncio.run(scenario()) == [{
        "name": "google", "connected": False, "tools": 0,
        "error": "FileNotFoundError", "state": "error",
        "detail": "executable not found: tool.exe",
    }]
    assert [(name, state) for name, state, _snapshot in transitions] == [
        ("google", "error"),
        ("google", "connecting"),
        ("google", "error"),
    ]


# --- connect retry-with-backoff (plan Track C3): -------------------------

def test_connect_retries_timeout_then_succeeds_with_observed_backoff():
    calls = []
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    good_factory = _memory_factory(_server())

    @asynccontextmanager
    async def factory(server_name, spec):
        calls.append(server_name)
        if len(calls) <= 2:
            raise TimeoutError("simulated cold-cache resolution")
        async with good_factory(server_name, spec) as session:
            yield session

    async def scenario():
        registry = FakeRegistry()
        servers = McpServers(
            {
                "servers": {"google": {"command": "unused"}},
                "defaults": {"connect_timeout_s": 1},
            },
            session_factory=factory,
            sleep=fake_sleep,
        )
        await servers.connect(registry)
        status = servers.status()
        await servers.close()
        return status

    status = asyncio.run(scenario())

    assert status == [{
        "name": "google", "connected": True, "tools": 3, "error": None,
        "state": "connected", "detail": "ready",
    }]
    assert len(calls) == 3
    assert sleeps == [2.0, 8.0]


def test_connect_retries_spawn_failure_then_succeeds_with_observed_backoff():
    """Mirrors the timeout retry test above but for the other retryable
    class -- a generic exception with no special-cased FileNotFoundError/
    TimeoutError/McpError shape, which the real _failure_status pipeline
    falls through to classifying as "spawn failed". Exercises retry
    end-to-end through the actual classifier and renderer (not a hardcoded
    "spawn failed" literal) so a change to that fallback path or to the
    vocabulary's wording is caught here if it stops being retried."""
    calls = []
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    good_factory = _memory_factory(_server())

    @asynccontextmanager
    async def factory(server_name, spec):
        calls.append(server_name)
        if len(calls) <= 2:
            raise RuntimeError("simulated spawn failure")
        async with good_factory(server_name, spec) as session:
            yield session

    async def scenario():
        registry = FakeRegistry()
        servers = McpServers(
            {
                "servers": {"google": {"command": "unused"}},
                "defaults": {"connect_timeout_s": 1},
            },
            session_factory=factory,
            sleep=fake_sleep,
        )
        await servers.connect(registry)
        status = servers.status()
        await servers.close()
        return status

    status = asyncio.run(scenario())

    assert status == [{
        "name": "google", "connected": True, "tools": 3, "error": None,
        "state": "connected", "detail": "ready",
    }]
    assert len(calls) == 3
    assert sleeps == [2.0, 8.0]


def test_retry_exhausts_after_configured_attempts_and_state_is_visible_between_attempts():
    observed = []
    holder: dict = {}

    async def fake_sleep(_seconds):
        observed.append(holder["servers"].status()[0])

    def factory(_server_name, _spec):
        raise TimeoutError("simulated cold-cache resolution")

    async def scenario():
        servers = McpServers(
            {
                "servers": {"google": {"command": "unused"}},
                "defaults": {"connect_timeout_s": 1, "connect_retries": 2},
            },
            session_factory=factory,
            sleep=fake_sleep,
        )
        holder["servers"] = servers
        await servers.connect(FakeRegistry())
        final = servers.status()
        await servers.close()
        return final

    final = asyncio.run(scenario())

    # After attempt 1/2 fails, state stays "connecting" (visible via status())
    # with a "retrying" detail naming the *next* attempt about to run --
    # the on_state hook does not fire for this because state didn't change.
    assert observed == [{
        "name": "google", "connected": False, "tools": 0,
        "error": "TimeoutError", "state": "connecting",
        "detail": "retrying (attempt 2 of 2)",
    }]
    assert final == [{
        "name": "google", "connected": False, "tools": 0,
        "error": "TimeoutError", "state": "error",
        "detail": "handshake timeout after 1s",
    }]


def test_config_entry_missing_does_not_retry(tmp_path):
    claude_config = tmp_path / "claude.json"
    claude_config.write_text('{"mcpServers": {}}', encoding="utf-8")
    calls = []

    def factory(_server_name, _spec):
        calls.append(True)
        raise AssertionError("session_factory must not run for a config error")

    async def fake_sleep(_seconds):
        raise AssertionError("must not back off / retry a config error")

    async def scenario():
        servers = McpServers(
            {"servers": {"google": {"from_claude_config": "google-workspace"}}},
            claude_config_path=claude_config,
            session_factory=factory,
            sleep=fake_sleep,
        )
        await servers.connect(FakeRegistry())
        status = servers.status()
        await servers.close()
        return status

    status = asyncio.run(scenario())

    assert status == [{
        "name": "google", "connected": False, "tools": 0, "error": "KeyError",
        "state": "not_configured", "detail": "config entry missing",
    }]
    assert calls == []


def test_config_malformed_does_not_retry(tmp_path):
    claude_config = tmp_path / "claude.json"
    claude_config.write_text("{malformed", encoding="utf-8")

    async def fake_sleep(_seconds):
        raise AssertionError("must not back off / retry a config error")

    async def scenario():
        servers = McpServers(
            {"servers": {"google": {"from_claude_config": "google-workspace"}}},
            claude_config_path=claude_config,
            sleep=fake_sleep,
        )
        await servers.connect(FakeRegistry())
        status = servers.status()
        await servers.close()
        return status

    status = asyncio.run(scenario())

    assert status == [{
        "name": "google", "connected": False, "tools": 0,
        "error": "JSONDecodeError", "state": "error",
        "detail": "config malformed",
    }]


def test_connect_attempts_and_backoffs_default_when_unconfigured():
    assert mcp_client._connect_attempts({}) == 3
    assert mcp_client._connect_backoffs({}) == (2.0, 8.0)


def test_connect_retries_rejects_invalid_values():
    with pytest.raises(ValueError, match="connect_retries"):
        mcp_client._connect_attempts({"connect_retries": 0})
    with pytest.raises(ValueError, match="connect_retries"):
        mcp_client._connect_attempts({"connect_retries": True})
    with pytest.raises(ValueError, match="connect_retries"):
        mcp_client._connect_attempts({"connect_retries": "3"})


def test_connect_retry_backoff_rejects_invalid_values():
    with pytest.raises(ValueError, match="connect_retry_backoff_s"):
        mcp_client._connect_backoffs({"connect_retry_backoff_s": []})
    with pytest.raises(ValueError, match="connect_retry_backoff_s"):
        mcp_client._connect_backoffs({"connect_retry_backoff_s": [2, -1]})
    with pytest.raises(ValueError, match="connect_retry_backoff_s"):
        mcp_client._connect_backoffs({"connect_retry_backoff_s": "2,8"})


def test_is_retryable_failure_matches_only_timeout_and_spawn_failed():
    assert mcp_client._is_retryable_failure("error", "spawn failed") is True
    assert mcp_client._is_retryable_failure("error", "handshake timeout after 60s") is True
    assert mcp_client._is_retryable_failure("error", "config malformed") is False
    assert mcp_client._is_retryable_failure("error", "config entry missing") is False
    assert mcp_client._is_retryable_failure("error", "executable not found: tool.exe") is False
    assert mcp_client._is_retryable_failure("not_configured", "config entry missing") is False


def test_is_retryable_failure_is_derived_from_the_real_vocabulary_renderers():
    """_is_retryable_failure must not hardcode a copy of the rendered text
    -- it is checked here against worker.statusdetail's own render function,
    not a literal, so a future rewording of the "timeout"/"spawn_failed"
    detail strings cannot silently desync the retry set from the
    vocabulary (the failure mode the C3 review flagged)."""
    assert mcp_client._is_retryable_failure(
        "error", mcp_client.render_status_detail("error", "spawn_failed"),
    ) is True
    assert mcp_client._is_retryable_failure(
        "error", mcp_client.render_status_detail("error", "timeout", timeout_s=42),
    ) is True
    assert mcp_client._is_retryable_failure(
        "error", mcp_client.render_status_detail("error", "config_malformed"),
    ) is False
    assert mcp_client._is_retryable_failure(
        "error", mcp_client.render_status_detail("error", "executable_missing", executable="x.exe"),
    ) is False


# --- args_override / package pin (plan Track C3): -------------------------

def test_args_override_replaces_from_claude_config_argv_without_spawning(tmp_path):
    claude_config = tmp_path / "claude.json"
    claude_config.write_text(
        json.dumps({
            "mcpServers": {
                "google-workspace": {
                    "command": "uvx",
                    "args": ["workspace-mcp", "--tools", "gmail", "drive", "calendar"],
                    "env": {"USER_GOOGLE_EMAIL": "daniel.zhang.t1@gmail.com"},
                },
            },
        }),
        encoding="utf-8",
    )
    seen = []

    def factory(_server_name, spec):
        seen.append(spec)
        raise LookupError("must stop before any real spawn")

    async def scenario():
        servers = McpServers(
            {
                "servers": {
                    "google": {
                        "from_claude_config": "google-workspace",
                        "args_override": [
                            "workspace-mcp==1.25.2", "--tools", "gmail", "drive", "calendar",
                        ],
                    },
                },
                "defaults": {"connect_retries": 1},
            },
            claude_config_path=claude_config,
            session_factory=factory,
        )
        await servers.connect(FakeRegistry())
        await servers.close()

    asyncio.run(scenario())

    assert seen[0].command == "uvx"
    assert seen[0].args == ["workspace-mcp==1.25.2", "--tools", "gmail", "drive", "calendar"]
    assert seen[0].env == {"USER_GOOGLE_EMAIL": "daniel.zhang.t1@gmail.com"}


def test_args_override_rejects_a_malformed_list():
    with pytest.raises(ValueError, match="args_override"):
        mcp_client._args_override(["ok", 7])
    with pytest.raises(ValueError, match="args_override"):
        mcp_client._args_override("not-a-list")


def test_cancellation_during_backoff_kills_tree_and_exits_cleanly():
    """The existing connect-cancellation test below covers cancellation
    while suspended entering stdio; this covers the other suspension point
    the retry loop introduces -- ``await self._sleep(backoff)`` between
    attempts. A pid is set right at the point of cancellation (simulating a
    still-tracked child at that suspension point) so the assertion proves
    the unconditional `finally` cleanup runs, not just the per-attempt
    cleanup that already ran before the backoff await."""
    events = []
    entered_backoff = asyncio.Event()

    def factory(_server_name, _spec):
        raise TimeoutError("simulated cold-cache resolution")

    async def fake_sleep(_seconds):
        events.append("backoff entered")
        entered_backoff.set()
        await asyncio.Event().wait()  # never set -- must be cancelled to resume

    async def scenario():
        kills = []
        servers = McpServers(
            {
                "servers": {"google": {"command": "unused"}},
                "defaults": {"connect_timeout_s": 1},
            },
            session_factory=factory,
            sleep=fake_sleep,
            killer=lambda pid, **kwargs: kills.append((pid, kwargs)),
        )
        task = asyncio.create_task(servers.connect(FakeRegistry()))
        await entered_backoff.wait()
        servers._server_pids["google"] = 4242
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # A subsequent close() must not raise and must not find an orphan
        # task still tracked.
        await servers.close()
        return kills, task.done(), dict(servers._server_tasks)

    kills, done, remaining_tasks = asyncio.run(scenario())

    assert kills == [(4242, {"check": False})]
    assert events == ["backoff entered"]
    assert done is True
    assert remaining_tasks == {}


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


def test_double_connect_keeps_one_live_task_and_one_child_kill_path():
    events = []

    async def scenario():
        kills = []
        servers = McpServers(
            {"servers": {"google": {"command": "unused"}}},
            session_factory=_memory_factory(_server(), events),
            killer=lambda pid, **kwargs: kills.append((pid, kwargs)),
        )
        await servers.connect(FakeRegistry())
        first_task = servers._server_tasks["google"]
        servers._server_pids["google"] = 2468
        with pytest.raises(RuntimeError, match="connection already active"):
            await servers.connect(FakeRegistry())
        assert servers._server_tasks == {"google": first_task}
        assert not first_task.done()
        await servers.close()
        return kills, first_task.done()

    kills, done = asyncio.run(scenario())

    assert kills == [(2468, {"check": False})]
    assert events == ["closed"]
    assert done is True


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
            {
                "servers": {"google": {"from_claude_config": "google-workspace"}},
                # connect_retries: 1: this test is about the resolved spec
                # and env leak-safety, not about retry.
                "defaults": {"connect_retries": 1},
            },
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
    assert status == [{
        "name": "google", "connected": False, "tools": 0, "error": "LookupError",
        "state": "error", "detail": "spawn failed",
    }]
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
            # connect_retries: 1: this test is about the server task ending
            # and retaining disconnected status, not about retry.
            {"servers": {"broken": {"command": "bad"}}, "defaults": {"connect_retries": 1}},
            session_factory=factory,
        )
        await servers.connect(FakeRegistry())
        server_task = servers._server_tasks["broken"]
        status = servers.status()
        await servers.close()
        return status, server_task.done()

    status, done = asyncio.run(scenario())

    assert status == [
        {
            "name": "broken", "connected": False, "tools": 0,
            "error": "LookupError", "state": "error", "detail": "spawn failed",
        },
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
            "state": "not_configured",
            "detail": "disabled by configuration",
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


# --- files: {file_roots} spec-resolution (Track C2) ----------------------


def _files_mcp_yaml(key: str = "file_roots") -> str:
    """A minimal command: server using one of the two roots keys. Both go
    through identical machinery (see _ROOTS_ARGV_TOKENS /
    _ROOTS_ENABLED_REFERENCES), which is why every behavior below is
    asserted for each key rather than for a hardcoded one."""
    return (
        "servers:\n"
        "  files:\n"
        f'    command: [npx, "-y", "pkg", "{{{key}}}"]\n'
        f"    enabled_from: {key}.enabled\n"
    )


def test_any_empty_roots_token_disables_a_server_using_both_tokens(tmp_path):
    """Final-review nit: the zero-resolved guard accumulates across ALL roots
    tokens in one argv -- with two tokens, an empty first token must disable
    the server even when the second resolves fine (and vice versa), not just
    whichever token happened to be processed last."""
    real = tmp_path / "real"
    real.mkdir()
    missing = tmp_path / "does_not_exist"
    for first, second in (("file_roots", "file_write_roots"),
                          ("file_write_roots", "file_roots")):
        mcp_path = tmp_path / "mcp.yaml"
        mcp_path.write_text(
            "servers:\n"
            "  files:\n"
            f'    command: [npx, "-y", "pkg", "{{{first}}}", "{{{second}}}"]\n'
            f"    enabled_from: {second}.enabled\n",
            encoding="utf-8",
        )
        atlas_path = tmp_path / "atlas.yaml"
        atlas_path.write_text(
            f"{first}:\n  - {missing}\n{second}:\n  - {real}\n",
            encoding="utf-8",
        )
        config = load_mcp_config(mcp_path, atlas_path=atlas_path)
        server = config["servers"]["files"]
        assert server["enabled"] is False, f"empty {first} did not disable"
        assert server["disabled_reason"] == "config_entry_missing"


def test_file_roots_argv_token_expands_to_existing_directories_and_skips_missing_ones(
    tmp_path,
):
    """{file_roots} in a command: server's argv resolves through the exact
    same code path worker/runtime.py uses to build the built-in LocalFiles
    tools (worker/localfiles.py's resolve_file_roots), so a directory that
    doesn't exist is silently dropped from BOTH surfaces identically, not
    just from one of them -- one allowlist, not two that could drift."""
    kept = tmp_path / "kept"
    kept.mkdir()
    missing = tmp_path / "missing"

    mcp_path = tmp_path / "mcp.yaml"
    atlas_path = tmp_path / "atlas.yaml"
    mcp_path.write_text(_files_mcp_yaml(), encoding="utf-8")
    atlas_path.write_text(
        f"file_roots:\n  - {kept}\n  - {missing}\n",
        encoding="utf-8",
    )

    config = load_mcp_config(mcp_path, atlas_path=atlas_path)
    server = config["servers"]["files"]

    assert server["command"] == "npx"
    assert server["args"] == ["-y", "pkg", str(kept.resolve())]
    assert server["enabled"] is True
    assert server["exact_environment"] is True


def test_file_roots_argv_token_calls_the_same_resolver_localfiles_tools_use(
    tmp_path, monkeypatch,
):
    """Proves the wiring, not just the outcome: the token expansion goes
    through worker.localfiles.resolve_file_roots (patched here to a spy),
    the identical function worker/runtime.py uses for the built-in
    find_file/open_file/read_file tools -- confirming there is one resolver
    behind both surfaces rather than a second implementation that merely
    happens to agree with it today.

    The spy is called twice with identical args: once expanding the argv's
    {file_roots} token, and once more for enabled_from's file_roots.enabled
    reference (adversarial review F1 -- enabled now reflects the RESOLVED
    root count, not just the raw config strings, so it independently
    re-resolves rather than trusting the argv expansion's result)."""
    import worker.localfiles as localfiles_module

    calls = []

    def spy(roots):
        calls.append(tuple(roots))
        return (tmp_path,)

    monkeypatch.setattr(localfiles_module, "resolve_file_roots", spy)

    mcp_path = tmp_path / "mcp.yaml"
    atlas_path = tmp_path / "atlas.yaml"
    mcp_path.write_text(_files_mcp_yaml(), encoding="utf-8")
    atlas_path.write_text("file_roots: [known:Desktop, C:/extra]\n", encoding="utf-8")

    config = load_mcp_config(mcp_path, atlas_path=atlas_path)
    server = config["servers"]["files"]

    assert calls == [("known:Desktop", "C:/extra")] * 2
    assert server["args"] == ["-y", "pkg", str(tmp_path)]
    assert server["enabled"] is True


def test_file_roots_enabled_reference_tracks_whether_file_roots_is_non_empty(tmp_path):
    """The files server's enabled_from mirrors worker/runtime.py's own
    `files = LocalFiles(raw_roots) if raw_roots else None` gate: an empty
    or absent file_roots disables the MCP server exactly like it disables
    the built-in localfiles tools, rather than needing a second config
    flag someone could forget to keep in sync."""
    mcp_path = tmp_path / "mcp.yaml"
    mcp_path.write_text(_files_mcp_yaml(), encoding="utf-8")

    empty_atlas = tmp_path / "empty.yaml"
    empty_atlas.write_text("{}\n", encoding="utf-8")
    disabled = load_mcp_config(mcp_path, atlas_path=empty_atlas)
    assert disabled["servers"]["files"]["enabled"] is False

    kept = tmp_path / "kept"
    kept.mkdir()
    nonempty_atlas = tmp_path / "nonempty.yaml"
    nonempty_atlas.write_text(f"file_roots: [{kept}]\n", encoding="utf-8")
    enabled = load_mcp_config(mcp_path, atlas_path=nonempty_atlas)
    assert enabled["servers"]["files"]["enabled"] is True


def test_file_roots_reference_rejects_malformed_file_roots_config(tmp_path):
    """Same validation, and the same error text, worker/runtime.py raises
    for a malformed file_roots -- one failure mode for the one config key,
    not two independently-worded ones."""
    mcp_path = tmp_path / "mcp.yaml"
    mcp_path.write_text(_files_mcp_yaml(), encoding="utf-8")
    atlas_path = tmp_path / "atlas.yaml"
    atlas_path.write_text("file_roots: not-a-list\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid Atlas configuration: file_roots"):
        load_mcp_config(mcp_path, atlas_path=atlas_path)


def test_file_roots_that_resolve_to_zero_directories_stay_not_configured(tmp_path):
    """Adversarial review F1: a typo'd or unmounted file_roots entry is a
    non-empty RAW list that resolves to zero real directories. Before this
    fix, enabled_from only checked the raw strings, so this connected as
    "healthy" even though the argv ended up as just [npx, -y, pkg] -- no
    root args at all -- and every call would then fail "Access denied".
    Gating on the RESOLVED count (mirroring LocalFiles.__init__'s own
    `if not roots: raise ValueError` guard) makes this surface as
    not_configured / "config entry missing" up front, and -- the
    structural half of the fix -- forces enabled False even if some future
    config mistake pointed enabled_from at an unrelated flag that reads
    True, so the server is never spawned with a rootless argv."""
    mcp_path = tmp_path / "mcp.yaml"
    mcp_path.write_text(_files_mcp_yaml(), encoding="utf-8")
    atlas_path = tmp_path / "atlas.yaml"
    missing = tmp_path / "does_not_exist"
    atlas_path.write_text(f"file_roots:\n  - {missing}\n", encoding="utf-8")

    config = load_mcp_config(mcp_path, atlas_path=atlas_path)
    server = config["servers"]["files"]

    assert server["enabled"] is False
    assert server["disabled_reason"] == "config_entry_missing"
    assert server["args"] == ["-y", "pkg"]  # zero roots in the argv

    def factory(_name, _spec):
        raise AssertionError("files server was spawned with zero resolved roots")

    async def scenario():
        registry = FakeRegistry()
        servers = McpServers(config, session_factory=factory)
        await servers.connect(registry)
        status = servers.status()
        await servers.close()
        return status

    status = asyncio.run(scenario())
    assert status == [{
        "name": "files",
        "connected": False,
        "tools": 0,
        "error": None,
        "state": "not_configured",
        "detail": "config entry missing",
    }]


def test_file_write_roots_token_expands_only_the_write_list_never_the_read_list(tmp_path):
    """Blocker 2: the files MCP server is the only component with write
    tools, and the server takes ONE allowlist for reads and writes alike.
    So its argv expands {file_write_roots}, and a directory that is a read
    root only -- kb in production -- must not appear in it. Both keys are
    configured here with different directories precisely so a regression
    back to {file_roots} cannot pass."""
    write_root = tmp_path / "documents"
    write_root.mkdir()
    read_only_root = tmp_path / "kb_stand_in"
    read_only_root.mkdir()

    mcp_path = tmp_path / "mcp.yaml"
    mcp_path.write_text(_files_mcp_yaml("file_write_roots"), encoding="utf-8")
    atlas_path = tmp_path / "atlas.yaml"
    atlas_path.write_text(
        f"file_roots: [{write_root}, {read_only_root}]\n"
        f"file_write_roots: [{write_root}]\n",
        encoding="utf-8",
    )

    server = load_mcp_config(mcp_path, atlas_path=atlas_path)["servers"]["files"]

    assert server["args"] == ["-y", "pkg", str(write_root.resolve())]
    assert str(read_only_root.resolve()) not in server["args"]
    assert server["enabled"] is True


def test_file_write_roots_that_resolve_to_zero_directories_stay_not_configured(tmp_path):
    """The same zero-resolved refusal as {file_roots} (adversarial review
    F1), for the write token: a typo'd or unmounted file_write_roots entry
    is a non-empty raw list that resolves to nothing, and must surface as
    not_configured rather than spawning a rootless server that fails every
    call with "Access denied". A populated file_roots alongside it must not
    rescue it -- that would be exactly the read/write conflation blocker 2
    removed."""
    mcp_path = tmp_path / "mcp.yaml"
    mcp_path.write_text(_files_mcp_yaml("file_write_roots"), encoding="utf-8")
    real = tmp_path / "real_read_root"
    real.mkdir()
    atlas_path = tmp_path / "atlas.yaml"
    atlas_path.write_text(
        f"file_roots: [{real}]\n"
        f"file_write_roots: [{tmp_path / 'does_not_exist'}]\n",
        encoding="utf-8",
    )

    config = load_mcp_config(mcp_path, atlas_path=atlas_path)
    server = config["servers"]["files"]

    assert server["enabled"] is False
    assert server["disabled_reason"] == "config_entry_missing"
    assert server["args"] == ["-y", "pkg"]  # zero roots in the argv

    def factory(_name, _spec):
        raise AssertionError("files server was spawned with zero resolved write roots")

    async def scenario():
        registry = FakeRegistry()
        servers = McpServers(config, session_factory=factory)
        await servers.connect(registry)
        status = servers.status()
        await servers.close()
        return status

    assert asyncio.run(scenario()) == [{
        "name": "files",
        "connected": False,
        "tools": 0,
        "error": None,
        "state": "not_configured",
        "detail": "config entry missing",
    }]


def test_file_write_roots_enabled_reference_and_malformed_config(tmp_path):
    """enabled_from: file_write_roots.enabled behaves exactly like its
    file_roots twin -- absent/empty disables, malformed raises with the
    key's own name so the error says which list to fix."""
    mcp_path = tmp_path / "mcp.yaml"
    mcp_path.write_text(_files_mcp_yaml("file_write_roots"), encoding="utf-8")

    # A perfectly good read list does not enable the write server.
    read_only = tmp_path / "read_only"
    read_only.mkdir()
    reads_only_atlas = tmp_path / "reads_only.yaml"
    reads_only_atlas.write_text(f"file_roots: [{read_only}]\n", encoding="utf-8")
    disabled = load_mcp_config(mcp_path, atlas_path=reads_only_atlas)
    assert disabled["servers"]["files"]["enabled"] is False

    kept = tmp_path / "kept"
    kept.mkdir()
    nonempty_atlas = tmp_path / "nonempty.yaml"
    nonempty_atlas.write_text(f"file_write_roots: [{kept}]\n", encoding="utf-8")
    assert load_mcp_config(mcp_path, atlas_path=nonempty_atlas)["servers"]["files"]["enabled"] is True

    malformed_atlas = tmp_path / "malformed.yaml"
    malformed_atlas.write_text("file_write_roots: not-a-list\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid Atlas configuration: file_write_roots"):
        load_mcp_config(mcp_path, atlas_path=malformed_atlas)


def test_file_roots_with_one_resolvable_root_spawns_with_exactly_that_root(tmp_path):
    """Companion to the zero-resolved-roots case above: a mix of one real
    directory and one missing one still enables and spawns, with argv
    containing only the directory that actually resolved."""
    mcp_path = tmp_path / "mcp.yaml"
    mcp_path.write_text(_files_mcp_yaml(), encoding="utf-8")
    atlas_path = tmp_path / "atlas.yaml"
    kept = tmp_path / "kept"
    kept.mkdir()
    missing = tmp_path / "does_not_exist"
    atlas_path.write_text(
        f"file_roots:\n  - {kept}\n  - {missing}\n", encoding="utf-8",
    )
    config = load_mcp_config(mcp_path, atlas_path=atlas_path)
    server = config["servers"]["files"]
    assert server["enabled"] is True
    assert "disabled_reason" not in server

    spawned = []

    @asynccontextmanager
    async def factory(name, spec):
        spawned.append((name, spec))
        yield SimpleNamespace(list_tools=lambda: asyncio.sleep(
            0, result=SimpleNamespace(tools=[]),
        ))

    async def scenario():
        servers = McpServers(config, session_factory=factory)
        await servers.connect(FakeRegistry())
        await servers.close()

    asyncio.run(scenario())
    assert len(spawned) == 1
    name, spec = spawned[0]
    assert name == "files"
    assert spec.args == ["-y", "pkg", str(kept.resolve())]


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
        "state": "connected",
        "detail": "ready",
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
    # BB-wave review, finding 10: kb_deployment_inspect and
    # kb_asset_pull_inspect were listed as instant reads but the bridge does
    # not offer them (an instant: name the server never sends is inert
    # decoration); kb_grades, a read it does offer, was missing.
    read_tools = {
        "kb_capabilities", "kb_agents_list", "kb_agent_get",
        "kb_workflows_list", "kb_workflow_get", "kb_runs_list", "kb_run_get",
        "kb_run_events", "kb_run_watch", "kb_inbox_list", "kb_schedules_list",
        "kb_grades", "kb_repo_tree",
        "kb_repo_file", "kb_repo_history", "kb_repo_search",
        "kb_analytics_snapshot", "kb_trace_list", "kb_trace_get",
        "kb_terminal_list",
    }
    mutation_tools = {
        "kb_agent_create", "kb_agent_update", "kb_workflow_create",
        "kb_workflow_update", "kb_workflow_launch", "kb_agent_launch",
        "kb_human_respond", "kb_review_dispatch", "kb_schedule_create",
        "kb_schedule_set_armed", "kb_schedule_delete", "kb_run_control",
    }

    assert set(server["instant"]) == read_tools
    assert not (read_tools & mutation_tools)
    # Exactly the 32 tools the bridge offers (enumerated from the built
    # atlas-bridge server on 2026-08-31; Atlas must not import or depend on
    # a kb checkout, so the set is pinned here rather than read live). An
    # instant: entry for a tool the server never sends is silently inert,
    # which is how the two removed names survived -- kb_deployment_action,
    # kb_asset_pull_action and kb_terminal_close, listed here before as
    # mutations, do not exist either and are gone for the same reason.
    assert len(read_tools | mutation_tools) == 32
    assert "kb_deployment_inspect" not in server["instant"]
    assert "kb_asset_pull_inspect" not in server["instant"]
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


# --- expose: -----------------------------------------------------------

def test_exposed_tools_returns_none_when_absent_and_a_frozenset_when_present():
    assert mcp_client._exposed_tools({}) is None
    assert mcp_client._exposed_tools({"expose": ["a", "b"]}) == frozenset({"a", "b"})


def test_exposed_tools_rejects_a_malformed_list():
    with pytest.raises(ValueError, match="expose"):
        mcp_client._exposed_tools({"expose": ["a", 1]})
    with pytest.raises(ValueError, match="expose"):
        mcp_client._exposed_tools({"expose": "not-a-list"})


def test_expose_and_blocked_overlap_blocked_always_wins():
    class FakeSession:
        async def list_tools(self):
            return SimpleNamespace(tools=[
                SimpleNamespace(
                    name=name, description=f"{name} tool",
                    inputSchema={"type": "object", "properties": {}},
                )
                for name in ("kept", "overlapping", "unexposed")
            ])

    @asynccontextmanager
    async def factory(_server_name, _spec):
        yield FakeSession()

    async def scenario():
        registry = ToolRegistry()
        servers = McpServers(
            {
                "servers": {
                    "demo": {
                        "command": "unused",
                        "expose": ["kept", "overlapping"],
                        "blocked": ["overlapping"],
                        "instant": ["kept", "overlapping"],
                    },
                },
                "defaults": {"connect_timeout_s": 1},
            },
            session_factory=factory,
        )
        await servers.connect(registry)
        registered = registry.names()
        await servers.close()
        return registered

    assert asyncio.run(scenario()) == ["demo__kept"]


# --- never_instant: ------------------------------------------------------

def test_never_instant_forces_confirm_over_an_explicit_instant_listing():
    server_cfg = {"instant": ["delete_thing", "get_thing"]}
    defaults = {}
    assert policy_for(server_cfg, defaults, "get_thing") == "instant"
    assert policy_for(server_cfg, defaults, "delete_thing") == "confirm"


def test_never_instant_default_list_covers_the_documented_patterns():
    server_cfg = {
        "instant": [
            "purchase_item", "revoke_access", "trash_note", "share_file",
            "remove_item", "send_message", "set_permission",
        ],
    }
    defaults = {}
    for name in server_cfg["instant"]:
        assert policy_for(server_cfg, defaults, name) == "confirm"


def test_never_instant_matching_is_case_insensitive_on_both_sides():
    server_cfg = {"instant": ["DeleteFile", "sendEmail", "listFiles"]}
    defaults = {}
    assert policy_for(server_cfg, defaults, "DeleteFile") == "confirm"
    assert policy_for(server_cfg, defaults, "sendEmail") == "confirm"
    assert policy_for(server_cfg, defaults, "listFiles") == "instant"


def test_never_instant_config_patterns_are_also_casefolded():
    server_cfg = {"instant": ["DELETE_THING"]}
    defaults = {"never_instant": ["Delete"]}
    assert policy_for(server_cfg, defaults, "DELETE_THING") == "confirm"


def test_never_instant_rejects_an_empty_pattern_list():
    """An empty list would silently disable the destructive-action backstop
    -- reject it rather than let it slip through as "no restrictions"."""
    with pytest.raises(ValueError, match="never_instant"):
        policy_for({"instant": ["delete_thing"]}, {"never_instant": []}, "delete_thing")


def test_never_instant_can_be_narrowed_by_a_non_empty_replacement_list():
    server_cfg = {"instant": ["delete_thing", "purchase_item"]}
    defaults = {"never_instant": ["purchase"]}
    assert policy_for(server_cfg, defaults, "delete_thing") == "instant"
    assert policy_for(server_cfg, defaults, "purchase_item") == "confirm"


def test_never_instant_rejects_a_malformed_pattern_list():
    with pytest.raises(ValueError, match="never_instant"):
        policy_for({}, {"never_instant": ["ok", 7]}, "get_thing")
    with pytest.raises(ValueError, match="never_instant"):
        policy_for({}, {"never_instant": "not-a-list"}, "get_thing")


# --- instant_when: -----------------------------------------------------

def test_instant_when_is_an_allowlist_that_escalates_unless_the_value_matches():
    escalate = mcp_client._compile_instant_when(
        {"instant_when": {"manage_event": {"action": ["create"]}}},
        "manage_event",
    )
    assert escalate is not None
    assert escalate({"action": "create"}) is False
    assert escalate({"action": "delete"}) is True
    assert escalate({"action": "update"}) is True


@pytest.mark.parametrize("value", ["delete", "Delete", " delete ", "DELETE", "  DeLeTe"])
def test_instant_when_escalates_case_and_whitespace_variants_of_a_disallowed_value(value):
    escalate = mcp_client._compile_instant_when(
        {"instant_when": {"manage_event": {"action": ["create"]}}},
        "manage_event",
    )
    assert escalate({"action": value}) is True


@pytest.mark.parametrize("value", ["create", " Create ", "CREATE", "CreAte"])
def test_instant_when_allows_case_and_whitespace_variants_of_an_allowed_value(value):
    escalate = mcp_client._compile_instant_when(
        {"instant_when": {"manage_event": {"action": ["create"]}}},
        "manage_event",
    )
    assert escalate({"action": value}) is False


@pytest.mark.parametrize("arguments", [
    {},                                # missing entirely
    {"action": ["create"]},            # list, not a string
    {"action": {"value": "create"}},   # dict, not a string
    {"action": 7},                     # int, not a string
    {"action": None},                  # None, not a string
])
def test_instant_when_escalates_on_missing_or_non_string_values(arguments):
    escalate = mcp_client._compile_instant_when(
        {"instant_when": {"manage_event": {"action": ["create"]}}},
        "manage_event",
    )
    assert escalate(arguments) is True


def test_instant_when_absent_for_a_tool_yields_no_escalate_hook():
    assert mcp_client._compile_instant_when(
        {"instant_when": {"other_tool": {"x": ["y"]}}}, "manage_event",
    ) is None
    assert mcp_client._compile_instant_when({}, "manage_event") is None


@pytest.mark.parametrize("rules", [
    {"manage_event": {}},
    {"manage_event": {"action": []}},
    {"manage_event": {7: ["create"]}},
    {"manage_event": "create"},
    {"manage_event": {"action": [7]}},
    {"manage_event": {"action": [""]}},
])
def test_instant_when_rejects_malformed_rules(rules):
    with pytest.raises(ValueError, match="instant_when"):
        mcp_client._compile_instant_when({"instant_when": rules}, "manage_event")


def test_instant_when_rejects_a_non_mapping_top_level_config():
    with pytest.raises(ValueError, match="instant_when"):
        mcp_client._compile_instant_when({"instant_when": ["not", "a", "map"]}, "manage_event")


# --- describe: ---------------------------------------------------------

def test_describe_overrides_the_mirrored_description():
    assert mcp_client._tool_description(
        {"describe": {"manage_event": "Host-authored description."}},
        "manage_event", "Remote description.",
    ) == "Host-authored description."
    assert mcp_client._tool_description(
        {}, "manage_event", "Remote description.",
    ) == "Remote description."


def test_describe_override_is_truncated_at_the_same_512_char_bound():
    servers = McpServers({"servers": {}})
    tool = servers._mirror_tool(
        "demo",
        {"describe": {"widget": "x" * 600}},
        {},
        SimpleNamespace(),
        SimpleNamespace(
            name="widget", description="short",
            inputSchema={"type": "object", "properties": {}},
        ),
    )
    assert len(tool.description) == 512
    assert tool.description == "x" * 512


def test_describe_rejects_a_non_string_empty_or_malformed_map():
    with pytest.raises(ValueError, match="describe"):
        mcp_client._tool_description({"describe": {"widget": ""}}, "widget", "remote")
    with pytest.raises(ValueError, match="describe"):
        mcp_client._tool_description({"describe": {"widget": 7}}, "widget", "remote")
    with pytest.raises(ValueError, match="describe"):
        mcp_client._tool_description({"describe": ["not", "a", "map"]}, "widget", "remote")


# --- transform: -------------------------------------------------------

def test_transform_absent_returns_none():
    assert mcp_client._tool_transform({}, "widget") is None


def test_transform_resolves_a_known_name_to_the_named_transformer():
    assert (
        mcp_client._tool_transform({"transform": {"widget": "local_time"}}, "widget")
        is mcp_client._local_time_transform
    )
    # A tool not named in the map is untouched even when transform: is present.
    assert mcp_client._tool_transform({"transform": {"other": "local_time"}}, "widget") is None


def test_transform_rejects_an_unknown_transformer_name():
    with pytest.raises(ValueError, match="transform"):
        mcp_client._tool_transform({"transform": {"widget": "not_a_real_transformer"}}, "widget")


def test_transform_rejects_a_non_string_value():
    with pytest.raises(ValueError, match="transform"):
        mcp_client._tool_transform({"transform": {"widget": 7}}, "widget")


def test_transform_rejects_a_non_mapping_top_level_config():
    with pytest.raises(ValueError, match="transform"):
        mcp_client._tool_transform({"transform": ["not", "a", "map"]}, "widget")


def test_transform_is_applied_on_the_mirrored_run_before_bounded_text():
    class FakeSession:
        async def call_tool(self, *_args, **_kwargs):
            return SimpleNamespace(
                content=[SimpleNamespace(
                    type="text",
                    text="Subject: hi\nDate: Mon, 31 Aug 2026 16:55:00 -0700\nFrom: a@b.test",
                )],
                isError=False,
            )

    servers = McpServers({"servers": {}})
    tool = servers._mirror_tool(
        "google",
        {"transform": {"widget": "local_time"}},
        {},
        FakeSession(),
        SimpleNamespace(
            name="widget", description="d",
            inputSchema={"type": "object", "properties": {}},
        ),
    )

    result = asyncio.run(tool.run({}))

    assert "-0700" not in result
    assert "Date: Mon, 31 Aug 2026" in result
    assert "Subject: hi" in result
    assert "From: a@b.test" in result


def _date_server() -> FastMCP:
    server = FastMCP("test-date")

    @server.tool(description="Search Gmail messages.")
    def search_gmail_messages(query: str) -> str:
        return f"Found 1 messages matching '{query}':\nDate: Mon, 31 Aug 2026 16:55:00 -0700"

    return server


def test_transform_applies_only_on_the_mirrored_path_call_raw_stays_untouched():
    async def scenario():
        registry = FakeRegistry()
        servers = McpServers(
            {
                "servers": {
                    "google": {
                        "command": "unused",
                        "instant": ["search_gmail_messages"],
                        "transform": {"search_gmail_messages": "local_time"},
                    },
                },
                "defaults": {"connect_timeout_s": 1},
            },
            session_factory=_memory_factory(_date_server()),
        )
        await servers.connect(registry)
        tool = registry.tools[0]
        mirrored = await tool.run({"query": "x"})
        raw = await servers.call_raw("google", "search_gmail_messages", {"query": "x"})
        await servers.close()
        return mirrored, raw

    mirrored, raw = asyncio.run(scenario())

    assert raw == "Found 1 messages matching 'x':\nDate: Mon, 31 Aug 2026 16:55:00 -0700"
    assert "-0700" not in mirrored
    assert mirrored != raw


def test_local_time_transform_rewrites_a_foreign_offset_date_line_to_local():
    from email.utils import parsedate_to_datetime

    raw_date = "Mon, 31 Aug 2026 16:55:00 -0700"
    expected_local = parsedate_to_datetime(raw_date).astimezone().strftime("%a, %d %b %Y %I:%M %p")
    text = f"Subject: hi\nDate: {raw_date}\nFrom: a@b.test\n"

    result = mcp_client._local_time_transform(text)

    assert f"Date: {expected_local} " in result
    assert raw_date not in result
    assert "Subject: hi" in result
    assert "From: a@b.test" in result


def test_local_time_transform_passes_unparseable_date_lines_through_byte_identical():
    text = "Date: not a real date at all\nOther-Header: kept\n"
    assert mcp_client._local_time_transform(text) == text


def test_local_time_transform_leaves_a_date_with_no_offset_unchanged():
    # No numeric/named offset to convert from -- ambiguous, so left as-is.
    text = "Date: Mon, 31 Aug 2026 16:55:00\n"
    assert mcp_client._local_time_transform(text) == text


def test_local_time_transform_is_idempotent():
    text = "Date: Mon, 31 Aug 2026 16:55:00 -0700\n"
    once = mcp_client._local_time_transform(text)
    twice = mcp_client._local_time_transform(once)
    assert twice == once


def test_local_time_transform_rewrites_every_date_line_in_a_multi_message_thread_dump():
    # get_gmail_thread_content's real shape (gmail/gmail_tools.py:_format_
    # thread_content) repeats "=== Message N ===\nFrom: ...\nDate: ..." once
    # per message in the thread -- every Date: line must be rewritten, not
    # just the first one a naive .sub(count=1) or non-global replace would
    # catch.
    from email.utils import parsedate_to_datetime

    raw_dates = [
        "Mon, 31 Aug 2026 16:55:00 -0700",
        "Tue, 01 Sep 2026 09:10:00 +0900",
        "Tue, 01 Sep 2026 03:00:00 +0000",
    ]
    expected = [
        parsedate_to_datetime(raw).astimezone().strftime("%a, %d %b %Y %I:%M %p")
        for raw in raw_dates
    ]
    text = "Thread ID: t1\nSubject: hi\nMessages: 3\n\n" + "\n\n".join(
        f"=== Message {i} ===\nFrom: sender{i}@example.com\nDate: {raw}\nTo: daniel@example.com"
        for i, raw in enumerate(raw_dates, 1)
    )

    result = mcp_client._local_time_transform(text)

    for raw, exp in zip(raw_dates, expected):
        assert raw not in result
        assert f"Date: {exp} " in result
    # Message count and structure around the Date: lines are untouched.
    assert result.count("=== Message") == 3
    assert "From: sender1@example.com" in result
    assert "From: sender2@example.com" in result
    assert "From: sender3@example.com" in result


# --- domain: ---------------------------------------------------------------

def test_domain_is_stored_on_mirrored_tools_when_configured():
    servers = McpServers({"servers": {}})
    tool = servers._mirror_tool(
        "demo", {"domain": "google"}, {}, SimpleNamespace(),
        SimpleNamespace(name="widget", description="d", inputSchema={"type": "object", "properties": {}}),
    )
    assert tool.domain == "google"


def test_domain_absent_leaves_mirrored_tool_domain_none():
    servers = McpServers({"servers": {}})
    tool = servers._mirror_tool(
        "demo", {}, {}, SimpleNamespace(),
        SimpleNamespace(name="widget", description="d", inputSchema={"type": "object", "properties": {}}),
    )
    assert tool.domain is None


def test_domain_rejects_a_non_string_or_empty_value():
    with pytest.raises(ValueError, match="domain"):
        mcp_client._server_domain({"domain": 7})
    with pytest.raises(ValueError, match="domain"):
        mcp_client._server_domain({"domain": ""})


# --- function-proof: all five keys through a real connect ------------------

def _c0_curation_server() -> FastMCP:
    """A fake remote server shaped like the real one this config targets:
    the mutation tool's argument is named `action` (workspace-mcp 1.25.2's
    real manage_event parameter, gcalendar/calendar_tools.py:1316), not the
    `operation` name the config/fixture were mistakenly fitted to before
    the F2/F3 fix."""
    server = FastMCP("test-c0")

    @server.tool(description="Read calendar events.")
    def get_events(calendar: str = "primary") -> str:
        return "events"

    @server.tool(description="List calendars.")
    def list_calendars() -> str:
        return "calendars"

    @server.tool(description="Delete a calendar event.")
    def delete_event(event_id: str) -> str:
        return f"deleted {event_id}"

    @server.tool(description="Create, update, or delete an event.")
    def manage_event(action: str, event_id: str = "") -> str:
        return f"{action} {event_id}"

    @server.tool(description="Secret internal tool that must never be exposed.")
    def secret_tool() -> str:
        return "secret"

    @server.tool(description="A network-inspection tool that stays blocked.")
    def blocked_tool() -> str:
        return "blocked"

    return server


def test_function_proof_registry_ends_up_with_exactly_the_curated_surface():
    """Spins the same fake-MCP-server fixture used elsewhere in this file
    (FastMCP + create_connected_server_and_client_session) with 6 tools,
    shaped like the real upstream server (manage_event's argument is named
    `action`), and a config exercising all five new keys (expose,
    never_instant, instant_when, describe, domain) at once, connects for
    real through McpServers, and checks the registry lands on exactly the
    curated set with the right policies, escalate hook, host-authored
    description, and domain label."""
    async def scenario():
        registry = ToolRegistry()
        servers = McpServers(
            {
                "servers": {
                    "google": {
                        "command": "unused",
                        "domain": "google",
                        "expose": ["get_events", "list_calendars", "delete_event", "manage_event"],
                        "blocked": ["blocked_tool"],
                        "instant": ["get_events", "list_calendars", "delete_event", "manage_event"],
                        "instant_when": {
                            "manage_event": {"action": ["create"]},
                        },
                        "describe": {
                            "manage_event": "Create, update, or delete a calendar event (host-authored).",
                        },
                    },
                },
                "defaults": {
                    "instant_prefixes": ["get_", "list_"],
                    "never_instant": [
                        "delete", "remove", "trash", "send", "purchase",
                        "revoke", "permission", "share",
                    ],
                    "connect_timeout_s": 1,
                },
            },
            session_factory=_memory_factory(_c0_curation_server()),
        )
        await servers.connect(registry)
        tools = dict(registry._tools)
        await servers.close()
        return tools

    tools = asyncio.run(scenario())

    # Exactly the exposed subset: secret_tool (never exposed) and
    # blocked_tool (exposed AND blocked -- blocked wins) are both absent.
    assert set(tools) == {
        "google__get_events", "google__list_calendars",
        "google__delete_event", "google__manage_event",
    }
    assert tools["google__get_events"].policy == "instant"
    assert tools["google__get_events"].description == "Read calendar events."
    assert tools["google__list_calendars"].policy == "instant"
    # never_instant's "delete" pattern forces confirm even though
    # delete_event was named in this server's instant: list.
    assert tools["google__delete_event"].policy == "confirm"
    # manage_event stays instant at the base policy; escalation to confirm
    # is argument-conditional via instant_when's allowlist, not baked into
    # the policy. It escalates for anything except an exact (normalized)
    # "create", including values that only differ by case/whitespace, and
    # for a missing or non-string action.
    assert tools["google__manage_event"].policy == "instant"
    assert tools["google__manage_event"].escalate is not None
    assert tools["google__manage_event"].escalate({"action": "create"}) is False
    assert tools["google__manage_event"].escalate({"action": " Create "}) is False
    assert tools["google__manage_event"].escalate({"action": "delete"}) is True
    assert tools["google__manage_event"].escalate({"action": "Delete"}) is True
    assert tools["google__manage_event"].escalate({}) is True
    assert tools["google__manage_event"].description == (
        "Create, update, or delete a calendar event (host-authored)."
    )
    assert all(tool.domain == "google" for tool in tools.values())


def test_missing_exposed_tools_logs_one_bounded_warning_with_names_only(caplog):
    class FakeSession:
        async def list_tools(self):
            return SimpleNamespace(tools=[
                SimpleNamespace(
                    name="kept", description="kept tool",
                    inputSchema={"type": "object", "properties": {}},
                ),
            ])

    @asynccontextmanager
    async def factory(_server_name, _spec):
        yield FakeSession()

    async def scenario():
        registry = ToolRegistry()
        servers = McpServers(
            {
                "servers": {
                    "demo": {
                        "command": "unused",
                        "expose": ["kept", "renamed_tool", "another_missing_tool"],
                    },
                },
                "defaults": {"connect_timeout_s": 1},
            },
            session_factory=factory,
        )
        with caplog.at_level("WARNING"):
            await servers.connect(registry)
        await servers.close()

    asyncio.run(scenario())

    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "demo" in message
    assert "renamed_tool" in message
    assert "another_missing_tool" in message
    assert "kept" not in message  # only the missing names are listed, not the mirrored ones


def test_missing_exposed_tools_says_nothing_when_everything_is_offered(caplog):
    async def scenario():
        registry = ToolRegistry()
        servers = McpServers(
            {
                "servers": {
                    "google": {"command": "unused", "instant": ["get_events"]},
                },
                "defaults": {"connect_timeout_s": 1},
            },
            session_factory=_memory_factory(_server()),
        )
        with caplog.at_level("WARNING"):
            await servers.connect(registry)
        await servers.close()

    asyncio.run(scenario())

    assert not any("expose" in record.getMessage() for record in caplog.records)


def test_missing_exposed_tools_helper_is_bounded_and_names_only():
    listed = [
        SimpleNamespace(name="kept", description="d", inputSchema={"type": "object", "properties": {}}),
    ]
    assert mcp_client._missing_exposed_tools(None, listed) == ()
    assert mcp_client._missing_exposed_tools(frozenset({"kept"}), listed) == ()
    assert mcp_client._missing_exposed_tools(
        frozenset({"kept", "gone", "also_gone"}), listed,
    ) == ("also_gone", "gone")
