from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager, suppress
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import subprocess
import time
from typing import Any, AsyncContextManager, TYPE_CHECKING
import unicodedata
import weakref

import yaml

from .jobobject import kill_process_tree
from .statusdetail import STATUS_DETAIL_RENDERERS, render_status_detail, status_detail_allowed
from .tools import McpToolError, Policy, Tool

if TYPE_CHECKING:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters
    from .tools import ToolRegistry


__all__ = ["McpServers", "McpSessionError", "load_mcp_config", "policy_for"]


_LOGGER = logging.getLogger(__name__)
_MAX_CONTENT = 4_096
_SESSION_EXPIRY_SKEW_S = 30.0
_RECIPIENT_ARGUMENTS = frozenset({"to", "cc", "bcc", "recipient", "attendees"})
_SELF_RECIPIENTS = frozenset({"me", "myself", "my email"})
_KB_BRIDGE_DEFAULTS = {
    "enabled": False,
    "mutations": False,
    "path": "C:/Users/danie/kb/dashboard/atlas-bridge",
    "origin": "http://127.0.0.1:5317",
}
_TRUNCATED = "…[truncated]"
_DEFAULT_NEVER_INSTANT = (
    "delete", "remove", "trash", "send", "purchase", "revoke", "permission", "share",
)
_DESCRIPTION_LIMIT = 512
# Retry-with-backoff for the per-server connect attempt (plan Track C3): the
# google outage was a uv cache eviction making cold `uvx workspace-mcp`
# resolution take 16.5s or wedge entirely, losing the race against the
# connect timeout -- with no retry, one bad attempt was terminal until the
# next full app restart. 3 attempts, ~2s/8s backoff between them, each
# attempt getting the *full* connect_timeout_s budget (not a shrinking
# remainder) so a slow-but-not-hung cold start still gets a fair shot on
# every attempt.
_DEFAULT_CONNECT_ATTEMPTS = 3
_DEFAULT_CONNECT_BACKOFF_S = (2.0, 8.0)
# Only timeout/spawn-class failures are retried -- the transient, process-
# level shapes behind the outage. Config-shaped failures (config_malformed,
# config_entry_missing, config_file_missing, config_unreadable,
# transport_unavailable, executable_missing, session_required, ...) are
# permanent for the life of this process; retrying them just spends the same
# backoff budget for the same outcome. Matched against the rendered detail
# text (the closed statusdetail vocabulary) rather than exception types,
# since that is the one place the retry/no-retry line is already drawn.
#
# The retryable set is derived from STATUS_DETAIL_RENDERERS' own compiled
# patterns at import time (not a hardcoded literal like "spawn failed"): a
# future rewording of the "timeout"/"spawn_failed" detail text moves this
# set with it automatically, so it can never silently desync from the
# vocabulary and stop retrying the very failure shapes the outage was.
_RETRYABLE_ERROR_DETAIL_KEYS = ("timeout", "spawn_failed")
_RETRYABLE_ERROR_PATTERNS = tuple(
    STATUS_DETAIL_RENDERERS["error"][key].pattern for key in _RETRYABLE_ERROR_DETAIL_KEYS
)


SessionFactory = Callable[
    [str, "StdioServerParameters"], AsyncContextManager["ClientSession"]
]
ServerHook = Callable[[str, "ToolRegistry"], None]
StateHook = Callable[[str, str, list[dict]], None]
ProcessTreeKiller = Callable[..., subprocess.CompletedProcess]
SleepFn = Callable[[float], Awaitable[None]]
_active_servers: weakref.ReferenceType | None = None


class McpSessionError(ValueError):
    """A short-lived MCP session was rejected before retention."""


class _ConnectExhausted(Exception):
    """Carries an already-classified terminal connect failure out of the
    per-server retry loop, so the outer handler doesn't have to reclassify
    it (and so a genuinely new exception raised after connection, e.g. from
    ``stop.wait()``, is never mistaken for a connect-phase failure)."""

    def __init__(self, error: str, state: str, detail: str) -> None:
        super().__init__(detail)
        self.error = error
        self.state = state
        self.detail = detail


def _connect_attempts(defaults: Mapping) -> int:
    value = defaults.get("connect_retries", _DEFAULT_CONNECT_ATTEMPTS)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("invalid MCP connect_retries")
    return value


def _connect_backoffs(defaults: Mapping) -> tuple[float, ...]:
    value = defaults.get("connect_retry_backoff_s", _DEFAULT_CONNECT_BACKOFF_S)
    if (
        not isinstance(value, (list, tuple))
        or not value
        or not all(
            isinstance(item, (int, float)) and not isinstance(item, bool) and item >= 0
            for item in value
        )
    ):
        raise ValueError("invalid MCP connect_retry_backoff_s")
    return tuple(float(item) for item in value)


def _is_retryable_failure(state: str, detail: str) -> bool:
    if state != "error":
        return False
    return any(pattern.fullmatch(detail) for pattern in _RETRYABLE_ERROR_PATTERNS)


def _exception_leaves(exc: BaseException) -> list[BaseException]:
    children = getattr(exc, "exceptions", None)
    if not isinstance(children, tuple):
        return [exc]
    leaves = []
    for child in children:
        if isinstance(child, BaseException):
            leaves.extend(_exception_leaves(child))
    return leaves or [exc]


def _failure_status(
    exc: BaseException,
    *,
    stage: str,
    command: Any,
    timeout_s: float,
) -> tuple[str, str]:
    leaves = _exception_leaves(exc)
    if isinstance(exc, TimeoutError) or any(isinstance(item, TimeoutError) for item in leaves):
        return "error", render_status_detail("error", "timeout", timeout_s=timeout_s)
    if stage == "resolving":
        if any(isinstance(item, ImportError) for item in leaves):
            return "error", render_status_detail("error", "transport_unavailable")
        if any(isinstance(item, FileNotFoundError) for item in leaves):
            return "not_configured", render_status_detail(
                "not_configured", "config_file_missing",
            )
        if any(isinstance(item, json.JSONDecodeError) for item in leaves):
            return "error", render_status_detail("error", "config_malformed")
        if any(isinstance(item, OSError) for item in leaves):
            return "error", render_status_detail("error", "config_unreadable")
        if any(isinstance(item, KeyError) for item in leaves):
            return "not_configured", render_status_detail(
                "not_configured", "config_entry_missing",
            )
        return "error", render_status_detail("error", "config_malformed")

    for item in leaves:
        message = str(item).casefold()
        if type(item).__name__ == "McpError" and any(
            marker in message
            for marker in (
                "session required", "authentication required", "unauthorized",
                "login required", "401",
            )
        ):
            return "error", render_status_detail("error", "session_required")
    for item in leaves:
        if type(item).__name__ == "McpError" and "connection closed" in str(item).casefold():
            return "error", render_status_detail("error", "closed_initialize")
    if stage == "listing":
        return "error", render_status_detail("error", "listing_failed")
    if any(isinstance(item, FileNotFoundError) for item in leaves):
        return "error", render_status_detail(
            "error", "executable_missing", executable=command,
        )
    return "error", render_status_detail("error", "spawn_failed")


_status_detail_allowed = status_detail_allowed


def active_mcp_servers() -> "McpServers | None":
    current = _active_servers() if _active_servers is not None else None
    return None if current is None or current._closed else current


def _load_mcp_transport():
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    return ClientSession, StdioServerParameters, stdio_client


class _PidTrackingStdio(AbstractAsyncContextManager):
    def __init__(
        self,
        spec: StdioServerParameters,
        on_pid: Callable[[int], None],
        *,
        exact_environment: bool = False,
        enter_lock: asyncio.Lock | None = None,
    ) -> None:
        _, _, stdio_client = _load_mcp_transport()
        self._context = stdio_client(spec, errlog=subprocess.DEVNULL)
        self._on_pid = on_pid
        self._exact_environment = exact_environment
        self._enter_lock = enter_lock or asyncio.Lock()

    async def __aenter__(self):
        async with self._enter_lock:
            restore_environment = None
            if self._exact_environment:
                from mcp.client import stdio as mcp_stdio

                restore_environment = mcp_stdio.get_default_environment
                mcp_stdio.get_default_environment = lambda: {}
            try:
                streams = await self._context.__aenter__()
            finally:
                if restore_environment is not None:
                    mcp_stdio.get_default_environment = restore_environment
        process = getattr(self._context, "process", None)
        generator = getattr(self._context, "gen", None)
        frame = getattr(generator, "ag_frame", None)
        if process is None and frame is not None:
            process = frame.f_locals.get("process")
        pid = getattr(process, "pid", None)
        if isinstance(pid, int) and pid > 0:
            self._on_pid(pid)
        return streams

    async def __aexit__(self, exc_type, exc_value, traceback):
        return await self._context.__aexit__(exc_type, exc_value, traceback)


def load_mcp_config(path: Path, *, atlas_path: Path | None = None) -> dict:
    value = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(value, dict):
        raise ValueError("MCP config must be a mapping")
    servers = value.get("servers", {})
    if not isinstance(servers, dict):
        raise ValueError("MCP servers must be a mapping")
    if any(
        isinstance(server, dict)
        and (
            "command" in server
            or "enabled_from" in server
            or "env_from" in server
        )
        for server in servers.values()
    ):
        atlas_file = Path(atlas_path) if atlas_path is not None else Path(path).with_name("atlas.yaml")
        atlas = yaml.safe_load(atlas_file.read_text(encoding="utf-8")) if atlas_file.exists() else {}
        if atlas is None:
            atlas = {}
        if not isinstance(atlas, dict):
            raise ValueError("Atlas config must be a mapping")
        value["servers"] = {
            name: _resolve_command_config(server, atlas)
            for name, server in servers.items()
        }
    return value


# Atlas config keys a server's argv may expand, and the enabled_from
# reference that gates the same server on that key resolving to at least one
# real directory. Two scopes, deliberately separate (BB-wave review, blocker
# 2): file_roots is Atlas's READ scope (what the built-in LocalFiles tools
# cover, kb included), file_write_roots is the narrower WRITE scope the
# files MCP server -- the only component with write tools -- is started
# with. Same resolver, same validation, same zero-resolved refusal for both;
# only the list differs.
_ROOTS_ARGV_TOKENS = {
    "{file_roots}": "file_roots",
    "{file_write_roots}": "file_write_roots",
}
_ROOTS_ENABLED_REFERENCES = {
    "file_roots.enabled": "file_roots",
    "file_write_roots.enabled": "file_write_roots",
}


def _validated_roots(atlas: Mapping, key: str) -> tuple[Any, ...]:
    """The exact validation worker/runtime.py applies to file_roots before
    building the built-in LocalFiles tools -- reused here, for either roots
    key, so a malformed entry fails the same way for every consumer of it."""
    from .localfiles import valid_file_root

    raw_roots = atlas.get(key, ())
    if (
        isinstance(raw_roots, (str, bytes))
        or not isinstance(raw_roots, (list, tuple))
        or not all(valid_file_root(root) for root in raw_roots)
    ):
        raise ValueError(f"invalid Atlas configuration: {key}")
    return tuple(raw_roots)


def _resolve_roots_argv(atlas: Mapping, key: str) -> list[str]:
    """Expand a roots list (config/atlas.yaml) into resolved, existing
    directory paths for an MCP server's argv, via the same resolver
    (worker/localfiles.py's resolve_file_roots, known: alias handling
    included) worker/runtime.py uses to build the built-in localfiles
    tools -- one place turns a configured root into real filesystem access,
    not two resolvers that could drift apart."""
    from .localfiles import resolve_file_roots

    return [str(root) for root in resolve_file_roots(_validated_roots(atlas, key))]


def _resolve_command_config(server_cfg: Any, atlas: Mapping) -> Any:
    if not isinstance(server_cfg, dict):
        return server_cfg
    if "command" not in server_cfg:
        return server_cfg
    command = server_cfg.get("command")
    if not isinstance(command, list):
        raise ValueError("invalid MCP command argv")
    if not command or not all(isinstance(item, str) and item for item in command):
        raise ValueError("invalid MCP command argv")
    bridge = dict(_KB_BRIDGE_DEFAULTS)
    configured = atlas.get("kb_bridge", {})
    if configured is not None:
        if not isinstance(configured, dict):
            raise ValueError("invalid Atlas kb_bridge config")
        bridge.update(configured)

    def setting(reference: Any) -> Any:
        # "file_roots.enabled"/"file_write_roots.enabled" are narrow special
        # cases, not a general namespace like kb_bridge.* -- the files MCP
        # server (Track C2) is gated by the exact same signal
        # worker/runtime.py already uses to decide whether the built-in
        # LocalFiles tools exist at all (`if raw_roots else None`), so there
        # is one place that grants filesystem access, not a second
        # independent toggle. This checks the RESOLVED directory count, not
        # just non-empty raw strings: a typo'd or unmounted root must read
        # as disabled here too (see the roots_token_used guard below for the
        # second, structural half of this fix).
        roots_key = _ROOTS_ENABLED_REFERENCES.get(reference) if isinstance(reference, str) else None
        if roots_key is not None:
            return bool(_resolve_roots_argv(atlas, roots_key))
        if not isinstance(reference, str) or not reference.startswith("kb_bridge."):
            raise ValueError("invalid MCP Atlas config reference")
        name = reference.removeprefix("kb_bridge.")
        if name not in bridge:
            raise ValueError("unknown MCP Atlas config reference")
        return bridge[name]

    resolved = dict(server_cfg)
    resolved_argv: list[str] = []
    roots_token_used = False
    # Accumulated across ALL roots tokens in the argv: any single token
    # resolving to zero directories disables the server, not just the last one.
    roots_token_empty = False
    for item in command:
        roots_key = _ROOTS_ARGV_TOKENS.get(item)
        if roots_key is not None:
            roots_token_used = True
            expanded = _resolve_roots_argv(atlas, roots_key)
            roots_token_empty = roots_token_empty or not expanded
            resolved_argv.extend(expanded)
        else:
            resolved_argv.append(item.replace("{kb_bridge.path}", str(bridge["path"])))
    if not resolved_argv:
        raise ValueError("invalid MCP command argv")
    resolved["command"] = resolved_argv[0]
    resolved["args"] = resolved_argv[1:]
    resolved["exact_environment"] = True
    if "enabled_from" not in resolved:
        raise ValueError("invalid MCP enabled_from")
    enabled_from = resolved.pop("enabled_from")
    enabled = setting(enabled_from)
    if not isinstance(enabled, bool):
        raise ValueError("invalid MCP enabled_from value")
    if roots_token_used and roots_token_empty:
        # Mirrors LocalFiles.__init__'s own `if not roots: raise ValueError`
        # guard: never let this server spawn with a roots token
        # ({file_roots} or {file_write_roots}) that
        # resolved to zero real directories, even if enabled_from itself
        # said True (e.g. a future config mistake that points enabled_from
        # at an unrelated flag). Without this, the argv still contains
        # [npx, -y, package] -- non-empty -- so the argv-empty check above
        # never fires, and today's pinned server happening to refuse to
        # start with zero directory args is not something Atlas may rely
        # on across version bumps. A typo'd or unmounted file_roots entry
        # must surface as not_configured, never as a healthy connect that
        # then fails every call with "Access denied" (adversarial review,
        # docs/plans/2026-08-31-atlas-bb-wave-plan.md Track C2, finding F1).
        enabled = False
        resolved["disabled_reason"] = "config_entry_missing"
    resolved["enabled"] = enabled
    env_from = resolved.pop("env_from", {})
    if not isinstance(env_from, dict) or not all(
        isinstance(name, str) and name and isinstance(reference, str)
        for name, reference in env_from.items()
    ):
        raise ValueError("invalid MCP command environment mapping")
    child_env = {
        name: _environment_value(setting(reference))
        for name, reference in env_from.items()
    }
    child_env["PATH"] = os.environ.get("PATH", os.defpath)
    child_env["SystemRoot"] = os.environ.get("SystemRoot", "C:/Windows")
    resolved["env"] = child_env
    return resolved


# not_configured detail keys a resolved server_cfg's disabled_reason may pick
# in place of the generic "disabled" -- a closed allowlist so an unexpected
# or param-requiring key (e.g. "signed_missing") can never reach
# render_status_detail and raise. See _resolve_command_config's
# roots_token_used guard for the one producer of "config_entry_missing"
# today.
_NOT_CONFIGURED_DISABLED_REASONS = frozenset({"disabled", "config_file_missing", "config_entry_missing"})


def _not_configured_detail_key(server_cfg: Any) -> str:
    reason = server_cfg.get("disabled_reason") if isinstance(server_cfg, Mapping) else None
    return reason if reason in _NOT_CONFIGURED_DISABLED_REASONS else "disabled"


def _environment_value(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, str):
        return value
    raise ValueError("invalid MCP command environment value")


def policy_for(server_cfg: Mapping, defaults: Mapping, tool_name: str) -> Policy:
    # Precedence: never_instant > instant > prefix heuristic. never_instant is
    # a name-substring backstop that forces confirm even over an explicit
    # instant: entry, so a mistaken or misleadingly-named mutation (e.g. a
    # "get_" tool that actually shares/deletes) cannot become instant by
    # config error. Matching is casefolded on both sides (pattern and tool
    # name) so it survives camelCase remote tool names too. (instant_when
    # sits above this too, but it is argument-conditional and applied later
    # as an `escalate` hook, not here.)
    normalized_name = tool_name.casefold()
    if any(pattern in normalized_name for pattern in _never_instant_patterns(defaults)):
        return "confirm"
    if "instant" in server_cfg:
        return "instant" if tool_name in server_cfg.get("instant", ()) else "confirm"
    prefixes = defaults.get("instant_prefixes", ())
    return "instant" if any(tool_name.startswith(prefix) for prefix in prefixes) else "confirm"


def _never_instant_patterns(defaults: Mapping) -> tuple[str, ...]:
    patterns = defaults.get("never_instant", _DEFAULT_NEVER_INSTANT)
    if (
        not isinstance(patterns, (list, tuple))
        or not patterns  # an empty list would silently disable the backstop
        or not all(isinstance(pattern, str) and pattern for pattern in patterns)
    ):
        raise ValueError("invalid MCP never_instant pattern list")
    return tuple(pattern.casefold() for pattern in patterns)


def _args_override(value: Any) -> list[str]:
    """Replace an entire ``from_claude_config``/``command`` argv with a
    version Atlas alone spawns (plan Track C3: pin the google server to a
    known-good ``workspace-mcp`` release so a `uv` cache eviction can't
    silently jump Atlas to an untested newer version, without ever editing
    the shared ``~/.claude.json`` entry Claude Code also uses).
    """
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ValueError("invalid MCP args_override")
    return list(value)


def _blocked_tools(server_cfg: Mapping) -> frozenset[str]:
    blocked = server_cfg.get("blocked", ())
    if not isinstance(blocked, (list, tuple)) or not all(
        isinstance(name, str) and name for name in blocked
    ):
        raise ValueError("invalid MCP blocked tool list")
    return frozenset(blocked)


def _exposed_tools(server_cfg: Mapping) -> frozenset[str] | None:
    """Return the allowed remote tool names, or None to mirror all of them.

    Absent `expose:` preserves today's behavior (mirror everything not
    blocked). When present, only names in this list are ever mirrored;
    combined with `blocked:` via AND, so blocked always wins on overlap.
    """
    if "expose" not in server_cfg:
        return None
    expose = server_cfg.get("expose")
    if not isinstance(expose, (list, tuple)) or not all(
        isinstance(name, str) and name for name in expose
    ):
        raise ValueError("invalid MCP expose tool list")
    return frozenset(expose)


_MISSING_EXPOSE_WARNING_LIMIT = 500


def _missing_exposed_tools(exposed: frozenset[str] | None, listed_tools: Any) -> tuple[str, ...]:
    """Names in expose: that the server did not actually offer this connect.

    Surfaces upstream renames/typos (an expose: entry silently mirroring
    nothing looks identical to an intentional cut otherwise).
    """
    if exposed is None:
        return ()
    listed_names = frozenset(getattr(tool, "name", None) for tool in listed_tools)
    return tuple(sorted(exposed - listed_names))


def _tool_description(server_cfg: Mapping, remote_tool_name: str, remote_description: str) -> str:
    describe = server_cfg.get("describe", {})
    if not isinstance(describe, Mapping):
        raise ValueError("invalid MCP describe map")
    if remote_tool_name in describe:
        override = describe[remote_tool_name]
        if not isinstance(override, str) or not override:
            raise ValueError("invalid MCP describe value")
        return override
    return remote_description


def _tool_content_bearing(server_cfg: Mapping, remote_tool_name: str) -> bool:
    """Whether this remote tool's output can taint the turn.

    Defaults TRUE for every MCP tool, and stays true for anything absent from
    the map -- fail closed. An unconfigured remote tool is assumed to return
    text Atlas did not author, which is the whole premise of the taint wall.
    Marking one false is a deliberate, reviewed statement that its output is
    host-authored: today the only such entry is the files server's
    list_allowed_directories, whose entire response is the CLI allowlist this
    host passed it on the command line.
    """
    configured = server_cfg.get("content_bearing", {})
    if not isinstance(configured, Mapping):
        raise ValueError("invalid MCP content_bearing map")
    if remote_tool_name not in configured:
        return True
    value = configured[remote_tool_name]
    if not isinstance(value, bool):
        raise ValueError("invalid MCP content_bearing value")
    return value


def _server_domain(server_cfg: Mapping) -> str | None:
    domain = server_cfg.get("domain")
    if domain is not None and (not isinstance(domain, str) or not domain):
        raise ValueError("invalid MCP domain")
    return domain


def _compile_instant_when(server_cfg: Mapping, remote_tool_name: str) -> Callable[[Mapping], bool] | None:
    """Compile instant_when rules for one tool into an escalate callable.

    instant_when: {tool_name: {arg_name: [allowed values, ...]}} is an
    ALLOWLIST, not a denylist: the call escalates instant -> confirm UNLESS
    every listed arg's call-time value is a string that case/whitespace-
    normalizes (``.strip().casefold()``) to one of its allowed values. A
    missing arg or a non-string value (list, dict, number, None, ...) also
    escalates -- fail closed rather than assuming the safe default applies.

    An allowlist (rather than a denylist of dangerous values) is required
    because the remote tool itself normalizes its argument the same way
    (e.g. workspace-mcp's manage_event does ``action.lower().strip()``), so
    a denylist of exact strings like "delete" would miss "Delete" or
    " delete " and fail open. Comparing both sides after the same
    normalization closes that gap regardless of what the disallowed values
    turn out to be.

    NOTE: no checked-in server configures instant_when today. Its one user
    was google's manage_event (instant for action: create), removed in the
    BB-wave review because an instant create can still carry attendees: and
    send_updates: -- third-party email on a model assertion, which rule 5
    forbids. That case needs an argument-PRESENCE predicate, which this
    value allowlist cannot express; the machinery is kept, and kept tested,
    for the next tool whose safe subset really is a set of argument values.
    """
    all_rules = server_cfg.get("instant_when", {})
    if not isinstance(all_rules, Mapping):
        raise ValueError("invalid MCP instant_when config")
    rules = all_rules.get(remote_tool_name)
    if rules is None:
        return None
    if not isinstance(rules, Mapping) or not rules:
        raise ValueError("invalid MCP instant_when rule")
    compiled: dict[str, frozenset[str]] = {}
    for arg_name, values in rules.items():
        if (
            not isinstance(arg_name, str) or not arg_name
            or not isinstance(values, (list, tuple)) or not values
            or not all(isinstance(value, str) and value for value in values)
        ):
            raise ValueError("invalid MCP instant_when rule")
        compiled[arg_name] = frozenset(value.strip().casefold() for value in values)

    def escalate(arguments: Mapping) -> bool:
        for arg_name, allowed in compiled.items():
            value = arguments.get(arg_name)
            if not isinstance(value, str) or value.strip().casefold() not in allowed:
                return True
        return False

    return escalate


@asynccontextmanager
async def _stdio_session(
    _server_name: str,
    spec: StdioServerParameters,
    *,
    on_pid: Callable[[int], None] = lambda _pid: None,
    exact_environment: bool = False,
    enter_lock: asyncio.Lock | None = None,
):
    ClientSession, _, _ = _load_mcp_transport()
    async with _PidTrackingStdio(
        spec,
        on_pid,
        exact_environment=exact_environment,
        enter_lock=enter_lock,
    ) as (read_stream, write_stream):
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
        on_server: ServerHook | None = None,
        on_state: StateHook | None = None,
        account_values: Mapping[str, str] | None = None,
        killer: ProcessTreeKiller = kill_process_tree,
        wall_clock: Callable[[], float] = time.time,
        sleep: SleepFn = asyncio.sleep,
    ):
        global _active_servers
        self._config = config
        self._claude_config_path = Path(claude_config_path)
        self._session_factory = session_factory or self._default_session
        self._on_server = on_server
        self._on_state = on_state
        self._account_values = dict(account_values or {})
        self._killer = killer
        self._wall_clock = wall_clock
        self._sleep = sleep
        self._stdio_enter_lock = asyncio.Lock()
        self._session_lock = asyncio.Lock()
        self._sessions: dict[str, ClientSession] = {}
        self._session_tokens: dict[str, tuple[str | None, Any, float]] = {}
        self._session_generations: dict[str, int] = {}
        self._session_notifications: dict[str, tuple[ClientSession, int]] = {}
        self._session_expiry_handles: dict[str, asyncio.TimerHandle] = {}
        self._call_settings: dict[str, tuple[Mapping, float]] = {}
        self._server_pids: dict[str, int] = {}
        self._server_tasks: dict[str, asyncio.Task[None]] = {}
        self._stop_events: dict[str, asyncio.Event] = {}
        self._server_tools: dict[str, dict[str, Tool]] = {}
        self._closed = False
        self._status_validation_logged = False
        servers = config.get("servers", {})
        self._status = {}
        for name, server_cfg in servers.items():
            disabled = (
                isinstance(server_cfg, Mapping)
                and server_cfg.get("enabled", True) is not True
            )
            self._status[name] = {
                "name": name,
                "connected": False,
                "tools": 0,
                "error": None,
                "state": "not_configured" if disabled else "connecting",
                "detail": render_status_detail(
                    "not_configured" if disabled else "connecting",
                    _not_configured_detail_key(server_cfg) if disabled else "pending",
                ),
            }
        if any(
            isinstance(server_cfg, Mapping)
            and server_cfg.get("session_channel") is True
            for server_cfg in servers.values()
        ):
            _active_servers = weakref.ref(self)

    def _default_session(
        self,
        server_name: str,
        spec: StdioServerParameters,
    ) -> AsyncContextManager[ClientSession]:
        server_cfg = self._config.get("servers", {}).get(server_name, {})
        return _stdio_session(
            server_name,
            spec,
            on_pid=lambda pid: self._server_pids.__setitem__(server_name, pid),
            exact_environment=(
                isinstance(server_cfg, Mapping)
                and server_cfg.get("exact_environment") is True
            ),
            enter_lock=self._stdio_enter_lock,
        )

    async def connect(
        self,
        registry: ToolRegistry,
        *,
        on_server: ServerHook | None = None,
    ) -> None:
        if self._closed:
            raise RuntimeError("MCP servers are closed")
        if any(not task.done() for task in self._server_tasks.values()):
            raise RuntimeError("MCP server connection already active")
        for name, task in tuple(self._server_tasks.items()):
            if task.done():
                self._server_tasks.pop(name, None)
                self._stop_events.pop(name, None)
        servers = self._config.get("servers", {})
        defaults = self._config.get("defaults", {})
        timeout_s = float(defaults.get("connect_timeout_s", 20))
        server_hook = self._compose_server_hooks(on_server)
        ready_events = []
        for name, server_cfg in servers.items():
            if isinstance(server_cfg, Mapping) and server_cfg.get("enabled", True) is not True:
                continue
            self._set_status(
                name,
                state="connecting",
                detail=render_status_detail("connecting", "pending"),
                connected=False,
                tools=0,
                error=None,
            )
            ready = asyncio.Event()
            stop = asyncio.Event()
            self._stop_events[name] = stop
            self._server_tasks[name] = asyncio.create_task(
                self._run_server(
                    name,
                    server_cfg,
                    defaults,
                    timeout_s,
                    registry,
                    server_hook,
                    ready,
                    stop,
                ),
                name=f"mcp-{name}",
            )
            ready_events.append(ready)
        try:
            await asyncio.gather(*(ready.wait() for ready in ready_events))
        except asyncio.CancelledError:
            await self._cancel_server_tasks()
            raise

    async def _run_server(
        self,
        name: str,
        server_cfg: Mapping,
        defaults: Mapping,
        timeout_s: float,
        registry: ToolRegistry,
        on_server: ServerHook | None,
        ready: asyncio.Event,
        stop: asyncio.Event,
    ) -> None:
        max_attempts = _connect_attempts(defaults)
        backoffs = _connect_backoffs(defaults)
        stack = AsyncExitStack()
        session = None
        stage = "resolving"
        command = None
        attempt = 1
        try:
            while True:
                try:
                    async with asyncio.timeout(timeout_s):
                        spec = self._resolve_spec(server_cfg)
                        command = spec.command
                        stage = "starting"
                        session = await stack.enter_async_context(self._session_factory(name, spec))
                        async with self._session_lock:
                            self._sessions[name] = session
                            generation = self._session_generations.get(name, 0)
                            await self._notify_held_session(name, session)
                        stage = "listing"
                        listed = await session.list_tools()
                        async with self._session_lock:
                            if self._session_generations.get(name, 0) != generation:
                                await self._notify_held_session(name, session)
                        blocked = _blocked_tools(server_cfg)
                        exposed = _exposed_tools(server_cfg)
                        missing = _missing_exposed_tools(exposed, listed.tools)
                        if missing:
                            _LOGGER.warning(
                                "MCP server %s expose: names not offered by the server: %s",
                                name,
                                ", ".join(missing)[:_MISSING_EXPOSE_WARNING_LIMIT],
                            )
                        mirrored = [
                            self._mirror_tool(name, server_cfg, defaults, session, tool)
                            for tool in listed.tools
                            if tool.name not in blocked and (exposed is None or tool.name in exposed)
                        ]
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    error = type(exc).__name__
                    state, detail = _failure_status(
                        exc,
                        stage=stage,
                        command=command if command is not None else server_cfg.get("command"),
                        timeout_s=timeout_s,
                    )
                    if attempt >= max_attempts or not _is_retryable_failure(state, detail):
                        raise _ConnectExhausted(error, state, detail) from exc
                    async with self._session_lock:
                        if self._sessions.get(name) is session:
                            self._sessions.pop(name, None)
                    session = None
                    with suppress(Exception):
                        await stack.aclose()
                    self._kill_server_tree(name)
                    backoff = backoffs[min(attempt - 1, len(backoffs) - 1)]
                    self._set_status(
                        name,
                        state="connecting",
                        detail=render_status_detail(
                            "connecting", "retrying",
                            attempt=attempt + 1, max_attempts=max_attempts,
                        ),
                        connected=False,
                        tools=0,
                        error=error,
                    )
                    _LOGGER.warning(
                        "MCP server %s connect attempt %d/%d failed (%s) -- retrying in %gs",
                        name, attempt, max_attempts, error, backoff,
                    )
                    await self._sleep(backoff)
                    stack = AsyncExitStack()
                    stage = "resolving"
                    command = None
                    attempt += 1
            self._call_settings[name] = (
                server_cfg,
                float(defaults.get("call_timeout_s", 8)),
            )
            tools_changed = self._replace_server_tools(name, registry, mirrored, None)
            self._set_status(
                name,
                state="connected",
                detail=render_status_detail("connected", "ready"),
                connected=True,
                tools=len(mirrored),
                error=None,
            )
            if tools_changed and on_server is not None:
                on_server(name, registry)
            ready.set()
            await stop.wait()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if isinstance(exc, _ConnectExhausted):
                error, state, detail = exc.error, exc.state, exc.detail
            else:
                error = type(exc).__name__
                state, detail = _failure_status(
                    exc,
                    stage=stage,
                    command=command if command is not None else server_cfg.get("command"),
                    timeout_s=timeout_s,
                )
            tools_changed = self._replace_server_tools(name, registry, [], None)
            self._set_status(
                name,
                state=state,
                detail=detail,
                connected=False,
                tools=0,
                error=error,
            )
            if tools_changed and on_server is not None:
                on_server(name, registry)
            _LOGGER.warning("MCP server %s connection failed: %s", name, error)
        finally:
            ready.set()
            self._call_settings.pop(name, None)
            current = self._status[name]
            tools_changed = self._replace_server_tools(name, registry, [], None)
            self._set_status(
                name,
                state=current["state"],
                detail=current["detail"],
                connected=False,
                tools=0,
                error=current["error"],
            )
            if tools_changed and on_server is not None:
                on_server(name, registry)
            async with self._session_lock:
                if self._sessions.get(name) is session:
                    self._sessions.pop(name, None)
                notified = self._session_notifications.get(name)
                if notified is not None and notified[0] is session:
                    self._session_notifications.pop(name, None)
            self._kill_server_tree(name)
            with suppress(Exception):
                await stack.aclose()

    def _compose_server_hooks(self, extra: ServerHook | None) -> ServerHook | None:
        hooks = []
        if self._on_server is not None:
            hooks.append(self._on_server)
        if extra is not None and extra is not self._on_server:
            hooks.append(extra)
        if not hooks:
            return None

        def composed(name: str, registry: ToolRegistry) -> None:
            for hook in hooks:
                try:
                    hook(name, registry)
                except Exception as exc:
                    _LOGGER.warning(
                        "MCP server %s hook failed: %s",
                        name,
                        type(exc).__name__,
                    )

        return composed

    def _replace_server_tools(
        self,
        name: str,
        registry: ToolRegistry,
        tools: list[Tool],
        on_server: ServerHook | None,
    ) -> bool:
        replacement = {tool.name: tool for tool in tools}
        if len(replacement) != len(tools):
            raise ValueError(f"duplicate MCP tool from server: {name}")
        previous = self._server_tools.get(name, {})
        previous_names = frozenset(previous)
        replacement_names = frozenset(replacement)

        for tool_name in previous:
            registry.unregister(tool_name)
        registered = []
        try:
            for tool in replacement.values():
                registry.register(tool)
                registered.append(tool.name)
        except Exception:
            for tool_name in registered:
                registry.unregister(tool_name)
            for tool in previous.values():
                registry.register(tool)
            raise

        if replacement:
            self._server_tools[name] = replacement
        else:
            self._server_tools.pop(name, None)
        if replacement_names != previous_names and on_server is not None:
            on_server(name, registry)
        return replacement_names != previous_names

    def _set_status(
        self,
        name: str,
        *,
        state: str,
        detail: str,
        connected: bool,
        tools: int,
        error: str | None,
    ) -> None:
        if not status_detail_allowed(state, detail):
            if "PYTEST_CURRENT_TEST" in os.environ:
                raise ValueError("invalid MCP status detail")
            if not self._status_validation_logged:
                _LOGGER.error("invalid MCP status pair normalized")
                self._status_validation_logged = True
            state = "error"
            detail = render_status_detail("error", "status_unavailable")
            connected = False
            tools = 0
        previous = self._status[name]["state"]
        self._status[name].update(
            state=state,
            detail=detail[:120],
            connected=connected,
            tools=tools,
            error=error,
        )
        if previous == state or self._on_state is None:
            return
        try:
            self._on_state(name, state, self.status())
        except Exception as exc:
            _LOGGER.warning("MCP server %s state hook failed: %s", name, type(exc).__name__)

    def _resolve_spec(self, server_cfg: Mapping) -> StdioServerParameters:
        source: Mapping = server_cfg
        config_name = server_cfg.get("from_claude_config")
        if config_name:
            claude_config = json.loads(self._claude_config_path.read_text(encoding="utf-8"))
            if not isinstance(claude_config, Mapping):
                raise ValueError("invalid MCP config")
            configured = claude_config.get("mcpServers")
            if not isinstance(configured, Mapping) or config_name not in configured:
                raise KeyError(config_name)
            source = configured[config_name]
        if not isinstance(source, Mapping) or not isinstance(source.get("command"), str):
            raise ValueError("invalid MCP server config")
        args = list(source.get("args", ()))
        if "args_override" in server_cfg:
            args = _args_override(server_cfg["args_override"])
        _, StdioServerParameters, _ = _load_mcp_transport()
        return StdioServerParameters(
            command=source["command"],
            args=args,
            env=dict(source["env"]) if source.get("env") is not None else None,
        )

    async def set_session(self, server: str, token: str, expires_at: Any) -> None:
        server_cfg = self._config.get("servers", {}).get(server)
        if not isinstance(server_cfg, Mapping) or server_cfg.get("session_channel") is not True:
            raise ValueError("MCP server has no session channel")
        if not isinstance(token, str) or not token:
            raise McpSessionError("invalid MCP session")
        try:
            expiry = _expiry_timestamp(expires_at)
        except ValueError as exc:
            raise McpSessionError(str(exc)) from None
        now = self._wall_clock()
        if expiry <= now + _SESSION_EXPIRY_SKEW_S:
            raise McpSessionError("MCP session expires too soon")
        async with self._session_lock:
            generation = self._session_generations.get(server, 0) + 1
            self._session_generations[server] = generation
            self._session_tokens[server] = (token, expires_at, expiry)
            self._schedule_session_expiry(server, generation, expiry)
            session = self._sessions.get(server)
            if session is not None:
                await self._notify_held_session(server, session)

    def session_origin(self, server: str) -> str | None:
        server_cfg = self._config.get("servers", {}).get(server)
        if (
            not isinstance(server_cfg, Mapping)
            or server_cfg.get("enabled", True) is not True
            or server_cfg.get("session_channel") is not True
        ):
            return None
        environment = server_cfg.get("env")
        origin = environment.get("ATLAS_KB_ORIGIN") if isinstance(environment, Mapping) else None
        return origin if isinstance(origin, str) and origin else None

    async def _notify_held_session(self, server: str, session: ClientSession) -> None:
        held = self._session_tokens.get(server)
        if held is None:
            return
        token, expires_at, expiry = held
        if token is None or expiry <= self._wall_clock():
            self._mark_session_expired(
                server,
                self._session_generations.get(server, 0),
                expiry,
            )
            return
        generation = self._session_generations.get(server, 0)
        notified = self._session_notifications.get(server)
        if notified is not None and notified[0] is session and notified[1] == generation:
            return
        await self._send_session_notification(session, token, expires_at)
        self._session_notifications[server] = (session, generation)

    def _schedule_session_expiry(self, server: str, generation: int, expiry: float) -> None:
        previous = self._session_expiry_handles.pop(server, None)
        if previous is not None:
            previous.cancel()
        delay = max(0.0, expiry - self._wall_clock())
        self._session_expiry_handles[server] = asyncio.get_running_loop().call_later(
            delay,
            self._expire_session,
            server,
            generation,
            expiry,
        )

    def _expire_session(self, server: str, generation: int, expiry: float) -> None:
        if self._session_generations.get(server) != generation:
            return
        remaining = expiry - self._wall_clock()
        if remaining > 0:
            self._session_expiry_handles[server] = asyncio.get_running_loop().call_later(
                remaining,
                self._expire_session,
                server,
                generation,
                expiry,
            )
            return
        self._mark_session_expired(server, generation, expiry)

    def _mark_session_expired(self, server: str, generation: int, expiry: float) -> None:
        held = self._session_tokens.get(server)
        if held is None or self._session_generations.get(server) != generation:
            return
        _, expires_at, held_expiry = held
        if held_expiry != expiry:
            return
        self._session_tokens[server] = (None, expires_at, expiry)
        handle = self._session_expiry_handles.pop(server, None)
        if handle is not None:
            handle.cancel()
        try:
            asyncio.get_running_loop().call_soon(
                self._erase_expired_session,
                server,
                generation,
                expiry,
            )
        except RuntimeError:
            pass

    def _erase_expired_session(self, server: str, generation: int, expiry: float) -> None:
        held = self._session_tokens.get(server)
        if (
            held is not None
            and held[0] is None
            and held[2] == expiry
            and self._session_generations.get(server) == generation
        ):
            self._session_tokens.pop(server, None)
            self._session_notifications.pop(server, None)

    @staticmethod
    async def _send_session_notification(
        session: ClientSession,
        token: str,
        expires_at: Any,
    ) -> None:
        from mcp.types import JSONRPCNotification

        await session.send_notification(JSONRPCNotification(
            jsonrpc="2.0",
            method="notifications/atlas/session",
            params={"token": token, "expiresAt": expires_at},
        ))

    def _mirror_tool(self, server_name, server_cfg, defaults, session, remote_tool) -> Tool:
        timeout_s = float(defaults.get("call_timeout_s", 8))
        schema = _without_account_parameter(
            remote_tool.inputSchema,
            server_cfg.get("account_param"),
        )
        description = _tool_description(server_cfg, remote_tool.name, remote_tool.description or "")

        async def run(arguments: dict) -> str:
            text = await self._call_session(
                session,
                server_cfg,
                timeout_s,
                remote_tool.name,
                arguments,
            )
            return _bounded_text(text)

        return Tool(
            name=f"{server_name}__{remote_tool.name}",
            description=description[:_DESCRIPTION_LIMIT],
            input_schema=schema,
            policy=policy_for(server_cfg, defaults, remote_tool.name),
            run=run,
            domain=_server_domain(server_cfg),
            content_bearing=_tool_content_bearing(server_cfg, remote_tool.name),
            escalate=_compile_instant_when(server_cfg, remote_tool.name),
        )

    async def call_raw(
        self,
        server: str,
        tool: str,
        arguments: Mapping[str, Any],
    ) -> str:
        session = self._sessions.get(server)
        settings = self._call_settings.get(server)
        if session is None or settings is None:
            raise RuntimeError(f"{server} not connected")
        server_cfg, timeout_s = settings
        return await self._call_session(
            session,
            server_cfg,
            timeout_s,
            tool,
            arguments,
        )

    async def _call_session(
        self,
        session: ClientSession,
        server_cfg: Mapping,
        timeout_s: float,
        tool: str,
        arguments: Mapping[str, Any],
    ) -> str:
        if tool in _blocked_tools(server_cfg):
            raise McpToolError("unknown MCP tool")
        call_arguments = dict(arguments)
        account_param = server_cfg.get("account_param")
        if account_param is not None:
            if not isinstance(account_param, str) or not account_param:
                raise RuntimeError("invalid MCP account parameter")
            account_value = self._account_values.get(account_param)
            if not isinstance(account_value, str) or not account_value:
                raise RuntimeError("MCP account is not configured")
            call_arguments = _normalize_recipients(call_arguments, account_value)
            call_arguments.pop(account_param, None)
            call_arguments[account_param] = account_value
        try:
            result = await session.call_tool(
                tool,
                arguments=call_arguments,
                read_timeout_seconds=timedelta(seconds=timeout_s),
            )
        except ValueError:
            # pydantic ValidationError subclasses ValueError; a malformed remote
            # payload must not surface remote text through the ValueError path.
            raise McpToolError("malformed MCP response") from None
        text = "".join(
            block.text
            for block in result.content
            if getattr(block, "type", None) == "text"
        )
        clean = _clean_text(text)
        is_error = bool(getattr(result, "isError", False))
        if is_error or clean.startswith("Error calling tool"):
            if (
                server_cfg.get("session_channel") is True
                and _typed_error(result, clean, "t3_requires_dashboard")
            ):
                raise McpToolError(
                    "that needs the dashboard - T3 is never done by voice"
                )
            if (
                server_cfg.get("session_channel") is True
                and _session_required(result, clean)
            ):
                raise McpToolError("kb is locked - say: Atlas, unlock kb")
            message = _bounded_text(clean) or "MCP tool call failed"
            raise McpToolError(message)
        return clean

    def status(self) -> list[dict]:
        projected = []
        servers = self._config.get("servers", {})
        for name, value in self._status.items():
            item = dict(value)
            server_cfg = servers.get(name, {})
            if isinstance(server_cfg, Mapping) and server_cfg.get("session_channel") is True:
                item["session"] = self._session_status(name)
            projected.append(item)
        return projected

    def _session_status(self, server: str) -> str:
        held = self._session_tokens.get(server)
        if held is None:
            return "none"
        token, expires_at, expiry = held
        if expiry <= self._wall_clock():
            self._mark_session_expired(
                server,
                self._session_generations.get(server, 0),
                expiry,
            )
            return "expired"
        return "held" if token is not None else "expired"

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        async with self._session_lock:
            self._sessions.clear()
            self._session_tokens.clear()
            self._session_generations.clear()
            self._session_notifications.clear()
            for handle in self._session_expiry_handles.values():
                handle.cancel()
            self._session_expiry_handles.clear()
        self._call_settings.clear()
        for name in tuple(self._server_tasks):
            self._kill_server_tree(name)
        for event in self._stop_events.values():
            event.set()
        tasks = list(self._server_tasks.values())
        if tasks:
            try:
                async with asyncio.timeout(10):
                    await asyncio.gather(*tasks, return_exceptions=True)
            except TimeoutError:
                await self._cancel_server_tasks()
        self._server_tasks.clear()
        self._stop_events.clear()
        for name in tuple(self._server_pids):
            self._kill_server_tree(name)
        self._server_pids.clear()

    async def _cancel_server_tasks(self) -> None:
        tasks = list(self._server_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _kill_server_tree(self, name: str) -> None:
        pid = self._server_pids.pop(name, None)
        if pid is None:
            return
        try:
            self._killer(pid, check=False)
        except Exception:
            _LOGGER.warning("MCP server %s process-tree cleanup failed", name)


def _bounded_text(value: str) -> str:
    clean = _clean_text(value)
    if len(clean) <= _MAX_CONTENT:
        return clean
    return clean[:_MAX_CONTENT - len(_TRUNCATED)] + _TRUNCATED


def _clean_text(value: str) -> str:
    return "".join(char for char in value
                   if char in "\n\t" or unicodedata.category(char) != "Cc")


def _expiry_timestamp(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("invalid MCP session expiry")
    if isinstance(value, (int, float)):
        expiry = float(value)
        if expiry > 10_000_000_000:
            expiry /= 1_000
        if expiry > 0:
            return expiry
        raise ValueError("invalid MCP session expiry")
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("invalid MCP session expiry") from None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    raise ValueError("invalid MCP session expiry")


def _session_required(result: Any, clean_text: str) -> bool:
    return _typed_error(result, clean_text, "session_required")


def _typed_error(result: Any, clean_text: str, expected: str) -> bool:
    structured = getattr(result, "structuredContent", None)
    if structured is None:
        structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict) and any(
        structured.get(name) == expected
        for name in ("type", "code", "error")
    ):
        return True
    try:
        decoded = json.loads(clean_text)
    except (TypeError, ValueError):
        return False
    return isinstance(decoded, dict) and any(
        decoded.get(name) == expected
        for name in ("type", "code", "error")
    )


def _normalize_recipients(arguments: Mapping[str, Any], account: str) -> dict[str, Any]:
    normalized = dict(arguments)
    local_part = account.partition("@")[0].strip().casefold()
    self_values = set(_SELF_RECIPIENTS)
    if local_part:
        self_values.add(local_part)
    for name, value in normalized.items():
        if name.casefold() not in _RECIPIENT_ARGUMENTS:
            continue
        if isinstance(value, str):
            if value.strip().casefold() in self_values:
                normalized[name] = account
            continue
        if isinstance(value, list):
            normalized[name] = [
                account
                if isinstance(item, str) and item.strip().casefold() in self_values
                else item
                for item in value
            ]
    return normalized


def _without_account_parameter(schema: dict, account_param: Any) -> dict:
    mirrored = deepcopy(schema)
    if not isinstance(account_param, str) or not account_param:
        return mirrored
    properties = mirrored.get("properties")
    if isinstance(properties, dict):
        properties.pop(account_param, None)
    required = mirrored.get("required")
    if isinstance(required, list):
        mirrored["required"] = [name for name in required if name != account_param]
    return mirrored
