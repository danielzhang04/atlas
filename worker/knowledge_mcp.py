"""Strict stdio MCP adapter for one authenticated Atlas read broker.

The child has no adapters or account credentials. It can only forward a bounded typed request to
the loopback endpoint injected by the Atlas host for its job. The endpoint remains the enforcement
boundary; this module repeats validation so malformed model calls fail before network dispatch.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import hmac
import http.client
import json
import math
import os
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlsplit
from uuid import UUID

from mcp.server.fastmcp import FastMCP

from .broker_ipc import MAX_IPC_REQUEST_BYTES, MAX_IPC_RESPONSE_BYTES
from .broker_ipc import BrokerIpcEndpoint
from .capability_runner import OBSERVABLE_READ_CAPABILITIES


_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_SAFE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,256}")
_RESPONSE_KEYS = {
    "version", "job_id", "request_id", "capability_id", "proposal_id", "parameters_hash",
    "content", "content_digest", "truncated",
}


class BrokerMcpError(RuntimeError):
    """Public, deliberately non-diagnostic failure from the private broker channel."""


@dataclass(frozen=True, slots=True)
class BrokerMcpLaunchConfig:
    """Host-only MCP process configuration; the token stays in the inherited environment."""

    endpoint: BrokerIpcEndpoint
    python_executable: Path
    package_root: Path

    def __post_init__(self) -> None:
        if not isinstance(self.endpoint, BrokerIpcEndpoint):
            raise TypeError("MCP launch requires a broker endpoint")
        python_executable = _resolved_file(self.python_executable, "MCP Python executable")
        package_root = _resolved_directory(self.package_root, "MCP package root")
        if not (package_root / "worker" / "knowledge_mcp.py").is_file():
            raise ValueError("MCP package root does not contain the knowledge adapter")
        object.__setattr__(self, "python_executable", python_executable)
        object.__setattr__(self, "package_root", package_root)

    @property
    def config_json(self) -> str:
        return json.dumps({
            "mcpServers": {
                "atlas_knowledge": {
                    "type": "stdio",
                    "command": str(self.python_executable),
                    "args": ["-B", "-m", "worker.knowledge_mcp"],
                },
            },
        }, sort_keys=True, separators=(",", ":"))

    def child_environment(self) -> Mapping[str, str]:
        values = dict(self.endpoint.mcp_environment())
        values["PYTHONPATH"] = str(self.package_root)
        return MappingProxyType(values)


@dataclass(frozen=True, slots=True)
class BrokerMcpClient:
    endpoint_url: str
    job_id: str
    allowed_capabilities: tuple[str, ...]
    token: str = field(repr=False)
    timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        try:
            UUID(self.job_id)
        except (TypeError, ValueError):
            raise ValueError("invalid MCP broker job id") from None
        if not isinstance(self.token, str) or _TOKEN.fullmatch(self.token) is None:
            raise ValueError("invalid MCP broker token")
        if (
            not isinstance(self.allowed_capabilities, tuple)
            or not self.allowed_capabilities
            or tuple(sorted(set(self.allowed_capabilities))) != self.allowed_capabilities
            or not set(self.allowed_capabilities).issubset(OBSERVABLE_READ_CAPABILITIES)
        ):
            raise ValueError("invalid MCP broker capability scope")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(float(self.timeout_seconds))
            or not 0.1 <= float(self.timeout_seconds) <= 30.0
        ):
            raise ValueError("invalid MCP broker timeout")
        _endpoint_parts(self.endpoint_url)

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> "BrokerMcpClient":
        source = os.environ if environment is None else environment
        if not isinstance(source, Mapping):
            raise TypeError("MCP environment must be a mapping")
        try:
            return cls(
                endpoint_url=source["ATLAS_BROKER_URL"],
                job_id=source["ATLAS_BROKER_JOB_ID"],
                allowed_capabilities=_parse_capability_scope(source["ATLAS_BROKER_CAPABILITIES"]),
                token=source["ATLAS_BROKER_TOKEN"],
            )
        except KeyError:
            raise ValueError("MCP broker environment is incomplete") from None

    def read(self, capability_id: str, parameters: Mapping[str, Any]) -> Mapping[str, Any]:
        if capability_id not in self.allowed_capabilities:
            raise BrokerMcpError("broker read rejected")
        if not isinstance(parameters, Mapping) or len(parameters) > 64:
            raise BrokerMcpError("broker read rejected")
        try:
            request = json.dumps(
                {"job_id": self.job_id, "capability_id": capability_id,
                 "parameters": dict(parameters)},
                sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError):
            raise BrokerMcpError("broker read rejected") from None
        if not 1 <= len(request) <= MAX_IPC_REQUEST_BYTES:
            raise BrokerMcpError("broker read rejected")

        parts, port = _endpoint_parts(self.endpoint_url)
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=self.timeout_seconds)
        try:
            connection.request(
                "POST", parts.path, body=request,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                    "Content-Length": str(len(request)),
                    "Accept": "application/json",
                    "Connection": "close",
                },
            )
            response = connection.getresponse()
            raw = response.read(MAX_IPC_RESPONSE_BYTES + 1)
            content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
        except (OSError, http.client.HTTPException):
            raise BrokerMcpError("broker read unavailable") from None
        finally:
            connection.close()
        if (
            response.status != 200
            or content_type != "application/json"
            or len(raw) > MAX_IPC_RESPONSE_BYTES
        ):
            raise BrokerMcpError("broker read rejected")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise BrokerMcpError("broker response rejected") from None
        return _validated_response(value, job_id=self.job_id, capability_id=capability_id)


def build_server(client: BrokerMcpClient) -> FastMCP:
    if not isinstance(client, BrokerMcpClient):
        raise TypeError("knowledge MCP requires a broker client")
    app = FastMCP("atlas-knowledge")

    def knowledge_read(capability_id: str, parameters: dict[str, Any]) -> dict[str, Any]:
        """Run one approved Atlas read capability with its typed JSON parameters."""
        return dict(client.read(capability_id, parameters))

    app.add_tool(
        knowledge_read,
        name="knowledge_read",
        description=(
            "Run one host-approved Atlas read through the authenticated job broker. "
            "Allowed capability_id values: " + ", ".join(client.allowed_capabilities)
        ),
    )
    return app


def main() -> None:
    build_server(BrokerMcpClient.from_environment()).run()


def _endpoint_parts(value: str):
    if not isinstance(value, str):
        raise ValueError("invalid MCP broker URL")
    parts = urlsplit(value)
    try:
        port = parts.port
    except ValueError:
        raise ValueError("invalid MCP broker URL") from None
    if (
        parts.scheme != "http"
        or parts.hostname != "127.0.0.1"
        or port is None
        or not 1 <= port <= 65_535
        or parts.netloc != f"127.0.0.1:{port}"
        or parts.path != "/v1/read"
        or parts.query
        or parts.fragment
        or parts.username is not None
        or parts.password is not None
    ):
        raise ValueError("invalid MCP broker URL")
    return parts, port


def _validated_response(value: Any, *, job_id: str, capability_id: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or set(value) != _RESPONSE_KEYS:
        raise BrokerMcpError("broker response rejected")
    if (
        value["version"] != 1
        or value["job_id"] != job_id
        or value["capability_id"] != capability_id
        or isinstance(value["request_id"], bool)
        or not isinstance(value["request_id"], int)
        or value["request_id"] < 1
        or not isinstance(value["proposal_id"], str)
        or _SAFE_ID.fullmatch(value["proposal_id"]) is None
        or not isinstance(value["parameters_hash"], str)
        or _DIGEST.fullmatch(value["parameters_hash"]) is None
        or not isinstance(value["content_digest"], str)
        or _DIGEST.fullmatch(value["content_digest"]) is None
        or not isinstance(value["truncated"], bool)
    ):
        raise BrokerMcpError("broker response rejected")
    try:
        canonical = json.dumps(
            value["content"], sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise BrokerMcpError("broker response rejected") from None
    if not hmac.compare_digest(sha256(canonical).hexdigest(), value["content_digest"]):
        raise BrokerMcpError("broker response rejected")
    return MappingProxyType(dict(value))


def _parse_capability_scope(value: str) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise ValueError("invalid MCP broker capability scope")
    capabilities = tuple(value.split(","))
    if (
        not capabilities
        or any(not item for item in capabilities)
        or tuple(sorted(set(capabilities))) != capabilities
        or not set(capabilities).issubset(OBSERVABLE_READ_CAPABILITIES)
    ):
        raise ValueError("invalid MCP broker capability scope")
    return capabilities


def _resolved_file(value: Path, label: str) -> Path:
    try:
        path = Path(value).resolve(strict=True)
    except (OSError, RuntimeError, TypeError):
        raise ValueError(f"{label} is unavailable") from None
    if not path.is_file() or any(char in str(path) for char in "\r\n\0"):
        raise ValueError(f"{label} is invalid")
    return path


def _resolved_directory(value: Path, label: str) -> Path:
    try:
        path = Path(value).resolve(strict=True)
    except (OSError, RuntimeError, TypeError):
        raise ValueError(f"{label} is unavailable") from None
    if not path.is_dir() or any(char in str(path) for char in "\r\n\0"):
        raise ValueError(f"{label} is invalid")
    return path


if __name__ == "__main__":
    main()


__all__ = [
    "BrokerMcpClient", "BrokerMcpError", "BrokerMcpLaunchConfig", "build_server", "main",
]
