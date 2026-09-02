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
from .localfiles import LocalFiles, valid_file_root
from .mcp_client import McpServers, load_mcp_config
from .tools import ToolRegistry, builtin, load_apps, register_count_mail
from .transcript import TranscriptStore
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
    # DD-4. None whenever persistence.enabled is false, which is the shipped
    # state -- so "the flag is off" and "there is no conversation store in
    # this process" are the same fact, not two that could disagree.
    transcript: TranscriptStore | None = None

    def warm_model_client(self) -> None:
        warm = getattr(self.brain.client, "warm", None)
        if warm is not None:
            warm()


def _required_text(cfg: dict[str, Any], name: str) -> str:
    value = cfg.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"invalid Atlas configuration: {name}")
    return value.strip()


def _positive_int(
    section: dict[str, Any], name: str, fallback: int, floor: int = 1,
) -> int:
    """Read one persistence bound, raising rather than silently substituting.

    The store clamps its own arguments defensively, so a bad value here would
    otherwise be absorbed in silence -- and a retention or size cap that
    quietly became something other than what the amendment says is exactly the
    kind of drift the amendment exists to prevent.

    `floor` exists for max_rows (re-review LOW-F). One row is a legal positive
    integer and a nonsensical store: an exchange is TWO rows, so a cap below
    that evicts every write as it lands and leaves a store that reports itself
    enabled while retaining nothing. A fat-fingered config should say so, not
    silently keep an empty file.
    """
    value = section.get(name, fallback)
    if isinstance(value, bool) or not isinstance(value, int) or value < floor:
        raise ValueError(f"invalid Atlas configuration: persistence.{name}")
    return value


def _transcript_store(
    cfg: dict[str, Any], tool_names: Callable[[], list[str]],
) -> TranscriptStore | None:
    """Build the conversation store, or None while the feature ships dark.

    `enabled is True` rather than `is not False`: traces default ON and so
    read the config that way, but persistence is the first thing Atlas writes
    that holds what was SAID, and it stays off until config says otherwise in
    so many words. A missing section, a null, a "yes" -- all of them are off.
    """
    section = cfg.get("persistence")
    if not isinstance(section, dict) or section.get("enabled") is not True:
        return None
    return TranscriptStore(
        section.get("path"),
        retention_days=_positive_int(section, "retention_days", 30),
        # Four rows = two exchanges, the least that can be a conversation.
        max_rows=_positive_int(section, "max_rows", 20_000, floor=4),
        max_content_bytes=_positive_int(section, "max_content_bytes", 4 * 1024 * 1024),
        seed_token_budget=_positive_int(section, "seed_token_budget", 1_500),
        seed_max_turns=_positive_int(section, "seed_max_turns", 20),
        seed_max_age_hours=_positive_int(section, "seed_max_age_hours", 24),
        tool_names=tool_names,
    )


def build(
    cfg: dict[str, Any],
    *,
    client: Any = None,
    launcher: ClaudeLauncher | None = None,
    session_factory=None,
    paired_url: Callable[[], str | None] | None = None,
    tool_overrides: dict[str, Any] | None = None,
) -> Runtime:
    model = _required_text(cfg, "fast_model")
    store_path = _required_text(cfg, "job_store_path")
    workspace_path = _required_text(cfg, "work_workspace_path")
    max_tokens = cfg.get("max_tokens", 500)
    timeout_s = cfg.get("turn_timeout_s", 12.0)
    ceiling_s = cfg.get("turn_ceiling_s", 30.0)
    pricing = cfg.get("pricing") if isinstance(cfg.get("pricing"), dict) else {}
    cache_ttl = pricing.get("cache_ttl", "5m")
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
        raise ValueError("invalid Atlas configuration: max_tokens")
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or timeout_s <= 0:
        raise ValueError("invalid Atlas configuration: turn_timeout_s")
    if (isinstance(ceiling_s, bool) or not isinstance(ceiling_s, (int, float))
            or not math.isfinite(ceiling_s) or ceiling_s <= 0 or ceiling_s < timeout_s):
        raise ValueError("invalid Atlas configuration: turn_ceiling_s")
    raw_roots = cfg.get("file_roots", ())
    if (isinstance(raw_roots, (str, bytes)) or not isinstance(raw_roots, (list, tuple))
            or not all(valid_file_root(root) for root in raw_roots)):
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
    # Built before builtin() because search_transcript needs it, and given
    # registry.names as a live callable rather than a snapshot -- MCP tools
    # register minutes later and must still count as known names.
    transcript = _transcript_store(cfg, registry.names)
    builtin(registry, load_apps(ATLAS / "config" / "apps.yaml"), work,
            paired_url=paired_url, files=files, transcript=transcript,
            **(tool_overrides or {}))
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
        cache_ttl=cache_ttl,
        transcript_store=transcript,
    )
    brain_holder.append(brain)
    if transcript is not None:
        # Boot, in the one place both entrypoints (worker/app.py and the
        # worker/chat.py text lane) go through. Sweep BEFORE seeding, so a
        # tail that retention should already have dropped can never be the
        # thing the first live turn is answering.
        transcript.sweep()
        brain.seed_prior_session(transcript.seed_text())
    return Runtime(registry, mcp, work, brain, store, transcript)
