"""Compose the Atlas tool, model, MCP, and background-work lanes."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any, Callable

from .brain import Brain
from .claude_launcher import ClaudeLauncher
from .jobstore import JobStore
from .localfiles import LocalFiles
from .mcp_client import McpServers, load_mcp_config
from .tools import ToolRegistry, builtin, load_apps, register_count_mail
from .work import WorkManager

__all__ = ["Runtime", "build"]

ATLAS = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class Runtime:
    registry: ToolRegistry
    mcp: McpServers
    work: WorkManager
    brain: Brain
    store: JobStore


def _path(value: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(value)))


def _required_text(cfg: dict[str, Any], name: str) -> str:
    value = cfg.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"invalid Atlas configuration: {name}")
    return value.strip()


def build(
    cfg: dict[str, Any],
    *,
    client: Any = None,
    launcher: ClaudeLauncher | None = None,
    session_factory=None,
    paired_url: Callable[[], str | None] | None = None,
) -> Runtime:
    """Build services without connecting MCP or starting background work."""
    model = _required_text(cfg, "fast_model")
    store_path = _required_text(cfg, "job_store_path")
    workspace_path = _required_text(cfg, "work_workspace_path")
    max_tokens = cfg.get("max_tokens", 400)
    timeout_s = cfg.get("turn_timeout_s", 12.0)
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
        raise ValueError("invalid Atlas configuration: max_tokens")
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or timeout_s <= 0:
        raise ValueError("invalid Atlas configuration: turn_timeout_s")
    raw_roots = cfg.get("file_roots", ())
    if (isinstance(raw_roots, (str, bytes)) or not isinstance(raw_roots, (list, tuple))
            or not all(isinstance(root, str) and root.strip() for root in raw_roots)):
        raise ValueError("invalid Atlas configuration: file_roots")

    store = JobStore(store_path if store_path == ":memory:" else _path(store_path))
    registry = ToolRegistry()
    files = LocalFiles(raw_roots) if raw_roots else None
    work = WorkManager(
        store,
        launcher or ClaudeLauncher(),
        _path(workspace_path),
        folders=files.folders if files is not None else {},
    )
    builtin(registry, load_apps(ATLAS / "config" / "apps.yaml"), work,
            paired_url=paired_url, files=files)
    mcp_kwargs = {"session_factory": session_factory} if session_factory is not None else {}
    google_account = _required_text(cfg, "google_account")

    async def google_not_connected(_arguments: dict) -> str:
        raise RuntimeError("google not connected")

    current_search = [google_not_connected]

    async def search(arguments: dict) -> str:
        return await current_search[0](arguments)

    register_count_mail(registry, search)

    def on_server(name: str, _current_registry: ToolRegistry) -> None:
        if name != "google":
            return

        async def connected_search(arguments: dict) -> str:
            return await mcp.call_raw(
                "google",
                "search_gmail_messages",
                arguments,
            )

        current_search[0] = connected_search

    mcp = McpServers(
        load_mcp_config(ATLAS / "config" / "mcp.yaml"),
        on_server=on_server,
        account_values={"user_google_email": google_account},
        **mcp_kwargs,
    )
    if client is None:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic()
    persona = (ATLAS / "config" / "persona.md").read_text(encoding="utf-8")
    brain = Brain(
        client,
        registry,
        model=model,
        persona=persona,
        max_tokens=max_tokens,
        turn_timeout_s=float(timeout_s),
    )
    return Runtime(registry, mcp, work, brain, store)
