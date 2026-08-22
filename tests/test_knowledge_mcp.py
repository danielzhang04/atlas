import json

import pytest

from worker.broker_ipc import BrokerIpcServer
from worker.capability_runner import BrokeredReadObservation
from worker.knowledge_mcp import (
    BrokerMcpClient, BrokerMcpError, BrokerMcpLaunchConfig, build_server,
)


JOB_ID = "3f75564b-cad1-4b9e-9e79-4f15013b43c2"
TOKEN = "a" * 43


class RecordingDispatcher:
    def __init__(self):
        self.calls = []

    def dispatch_observed(self, call):
        self.calls.append(call)
        content = json.dumps({"items": [{"title": "private result"}]}, separators=(",", ":"),
                             sort_keys=True)
        import hashlib
        return BrokeredReadObservation(
            call.capability_id, "proposal-1", "b" * 64, content,
            hashlib.sha256(content.encode("utf-8")).hexdigest(), False,
        )


def test_mcp_client_round_trips_one_authenticated_broker_read():
    dispatcher = RecordingDispatcher()
    server = BrokerIpcServer(
        dispatcher, job_id=JOB_ID, token_factory=lambda: TOKEN,
        allowed_capabilities=frozenset({"google.drive.list"}),
    )
    endpoint = server.start()
    try:
        client = BrokerMcpClient.from_environment(endpoint.mcp_environment())
        result = client.read("google.drive.list", {"query": "project atlas"})
    finally:
        server.close()
    assert result["content"] == {"items": [{"title": "private result"}]}
    assert result["content_digest"]
    assert dispatcher.calls[0].capability_id == "google.drive.list"
    assert dict(dispatcher.calls[0].parameters) == {"query": "project atlas"}
    assert TOKEN not in repr(client) and TOKEN not in repr(result)


@pytest.mark.parametrize("url", [
    "https://127.0.0.1:1234/v1/read",
    "http://localhost:1234/v1/read",
    "http://127.0.0.1:1234/v1/read?next=http://example.com",
    "http://user@127.0.0.1:1234/v1/read",
    "http://127.0.0.1:1234/other",
])
def test_mcp_client_rejects_every_nonexact_loopback_endpoint(url):
    with pytest.raises(ValueError, match="invalid MCP broker URL"):
        BrokerMcpClient(url, JOB_ID, ("google.drive.read",), TOKEN)


def test_mcp_client_rejects_mutations_before_network_dispatch():
    client = BrokerMcpClient(
        "http://127.0.0.1:9/v1/read", JOB_ID, ("google.drive.read",), TOKEN,
        timeout_seconds=0.1,
    )
    with pytest.raises(BrokerMcpError, match="broker read rejected"):
        client.read("google.gmail.send", {"draft_id": "unsafe"})


def test_mcp_server_exposes_one_broker_tool_with_bounded_schema():
    client = BrokerMcpClient(
        "http://127.0.0.1:9/v1/read", JOB_ID, ("google.drive.read",), TOKEN,
        timeout_seconds=0.1,
    )
    app = build_server(client)
    tools = app._tool_manager.list_tools()
    assert [tool.name for tool in tools] == ["knowledge_read"]
    schema = tools[0].parameters
    assert set(schema["required"]) == {"capability_id", "parameters"}
    assert set(schema["properties"]) == {"capability_id", "parameters"}
    assert "google.drive.read" in tools[0].description


def test_mcp_environment_is_complete_and_errors_do_not_echo_values():
    with pytest.raises(ValueError, match="environment is incomplete") as missing:
        BrokerMcpClient.from_environment({"ATLAS_BROKER_URL": "secret-url"})
    assert "secret-url" not in str(missing.value)


def test_launch_config_keeps_bearer_token_out_of_mcp_json(tmp_path):
    package_root = tmp_path / "atlas"
    adapter = package_root / "worker" / "knowledge_mcp.py"
    adapter.parent.mkdir(parents=True)
    adapter.write_text("# test adapter", encoding="utf-8")
    python = tmp_path / "python.exe"
    python.write_text("test", encoding="utf-8")
    dispatcher = RecordingDispatcher()
    server = BrokerIpcServer(
        dispatcher, job_id=JOB_ID, token_factory=lambda: TOKEN,
        allowed_capabilities=frozenset({"google.drive.list"}),
    )
    endpoint = server.start()
    try:
        config = BrokerMcpLaunchConfig(endpoint, python, package_root)
        decoded = json.loads(config.config_json)
        environment = config.child_environment()
    finally:
        server.close()
    assert decoded["mcpServers"]["atlas_knowledge"]["args"] == [
        "-B", "-m", "worker.knowledge_mcp",
    ]
    assert TOKEN not in config.config_json and TOKEN not in repr(config)
    assert environment["ATLAS_BROKER_TOKEN"] == TOKEN
    assert environment["PYTHONPATH"] == str(package_root.resolve())
