from __future__ import annotations

from dataclasses import dataclass
import logging
import math
import os
from pathlib import Path
import threading
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
logger = logging.getLogger("atlas.runtime")


class _LazyAnthropicClient:
    def __init__(self, factory=None) -> None:
        self._factory = factory
        self._client = None
        self._lock = threading.Lock()
        self._warm_thread = None

    def warm(self) -> None:
        with self._lock:
            if self._client is not None or self._warm_thread is not None:
                return
            self._warm_thread = threading.Thread(
                target=self._warm,
                name="atlas-anthropic-warmup",
                daemon=True,
            )
            self._warm_thread.start()

    def _client_instance(self):
        if self._client is not None:
            return self._client
        with self._lock:
            if self._client is not None:
                return self._client
            factory = self._factory
            if factory is None:
                from anthropic import AsyncAnthropic

                factory = AsyncAnthropic
            self._client = factory()
            return self._client

    def _warm(self) -> None:
        try:
            self._client_instance()
        except Exception as exc:
            logger.warning("Anthropic client warmup failed (type=%s)", type(exc).__name__)

    @property
    def messages(self):
        return self._client_instance().messages


@dataclass(frozen=True, slots=True)
class Runtime:
    registry: ToolRegistry
    mcp: McpServers
    work: WorkManager
    brain: Brain
    store: JobStore

    def warm_model_client(self) -> None:
        warm = getattr(self.brain.client, "warm", None)
        if warm is not None:
            warm()


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
    model = _required_text(cfg, "fast_model")
    store_path = _required_text(cfg, "job_store_path")
    workspace_path = _required_text(cfg, "work_workspace_path")
    max_tokens = cfg.get("max_tokens", 400)
    timeout_s = cfg.get("turn_timeout_s", 12.0)
    ceiling_s = cfg.get("turn_ceiling_s", 30.0)
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
        raise ValueError("invalid Atlas configuration: max_tokens")
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or timeout_s <= 0:
        raise ValueError("invalid Atlas configuration: turn_timeout_s")
    if (isinstance(ceiling_s, bool) or not isinstance(ceiling_s, (int, float))
            or not math.isfinite(ceiling_s) or ceiling_s <= 0 or ceiling_s < timeout_s):
        raise ValueError("invalid Atlas configuration: turn_ceiling_s")
    raw_roots = cfg.get("file_roots", ())
    if (isinstance(raw_roots, (str, bytes)) or not isinstance(raw_roots, (list, tuple))
            or not all(isinstance(root, str) and root.strip() for root in raw_roots)):
        raise ValueError("invalid Atlas configuration: file_roots")

    store = JobStore(store_path if store_path == ":memory:" else Path(
        os.path.expanduser(os.path.expandvars(store_path))))
    registry = ToolRegistry()
    files = LocalFiles(raw_roots) if raw_roots else None
    work = WorkManager(
        store,
        launcher or ClaudeLauncher(),
        Path(os.path.expanduser(os.path.expandvars(workspace_path))),
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

    brain_holder: list[Brain] = []

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

    def on_state(_name: str, _state: str, snapshot: list[dict]) -> None:
        if brain_holder:
            brain_holder[0].refresh_capabilities(snapshot)

    mcp = McpServers(
        load_mcp_config(ATLAS / "config" / "mcp.yaml"),
        on_server=on_server,
        on_state=on_state,
        account_values={"user_google_email": google_account},
        **mcp_kwargs,
    )
    if client is None:
        client = _LazyAnthropicClient()
    persona = (ATLAS / "config" / "persona.md").read_text(encoding="utf-8")
    brain = Brain(
        client,
        registry,
        model=model,
        persona=persona,
        max_tokens=max_tokens,
        turn_timeout_s=float(timeout_s),
        turn_ceiling_s=float(ceiling_s),
        mcp_status=mcp.status(),
    )
    brain_holder.append(brain)
    return Runtime(registry, mcp, work, brain, store)
