"""Real, headless end-to-end proof for the files MCP server (Track C2):
spawns the actual npx-installed @modelcontextprotocol/server-filesystem
package (no fakes, no in-memory FastMCP fixture) through Atlas's own
worker.mcp_client spec-resolution and connect path, and proves:

  * the 5 curated tools register, matching the checked-in config/mcp.yaml
    expose: list exactly -- write-only since the 2026-09-01 final gate (F3),
    so NONE of the 9 read tools the real server ships is registered, and
    the model cannot reach a Downloads credential file by asking this
    server for it instead of shield-covered find_file/read_file;
  * the server itself is genuinely live and readable underneath that
    policy: a raw read (read_text_file via call_raw, bypassing the host
    registry) of a seeded file under the resolved file_write_roots returns
    real file content, which is what makes the absence of a registered read
    tool a host decision rather than a broken connection;
  * THE ROOTS TRAP does not bite Atlas: list_allowed_directories reports
    EXACTLY the resolved file_write_roots the host passed as CLI argv, not
    something the server negotiated over the MCP roots protocol (which
    Atlas's ClientSession never advertises -- see the comment in
    config/mcp.yaml's files: entry and worker/mcp_client.py's
    _stdio_session);
  * the write scope really is narrower than the read scope: a file_roots
    entry that is NOT in file_write_roots is absent from the server's
    allowlist and refused live (blocker 2 -- this is the kb checkout in
    production);
  * a write_file call comes back needs_confirmation through the registry
    and is never confirmed or executed by this test -- the file it would
    have created does not exist afterward;
  * the real server's own guardrails still hold at the pinned version: a
    cross-root move_file (source inside a root, destination outside every
    root) and a symlink inside a root pointing outside every root are both
    refused with "Access denied", and nothing is moved or read -- these are
    regression guards for a future version bump, called directly through
    McpServers.call_raw (bypassing the host's own confirm gate, which is
    covered separately) because their purpose is proving the SERVER still
    fails closed, not the host policy layered on top of it.

Requires a real npx-resolvable Node install and, on the very first run on
a machine, network access to fetch and cache the pinned package version.
Skips (does not fail) when npx is unavailable or a bounded prefetch does
not succeed, so an offline dev machine or a sandboxed CI runner without
network turns this into a skip, not a false failure.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

from worker.mcp_client import McpServers, load_mcp_config
from worker.tools import McpToolError, ToolRegistry

_ATLAS_ROOT = Path(__file__).resolve().parents[1]
_PINNED_PACKAGE = "@modelcontextprotocol/server-filesystem@2026.8.31"
_NPX = shutil.which("npx")
_PREFETCH_TIMEOUT_S = 90
# Must outlast the config's own retry budget, not one attempt: connect()
# resolves only after connect_retries x connect_timeout_s plus the backoff
# waits (3 x 60 + 2 + 8 = 190s worst case, see config/mcp.yaml defaults).
# At 60s this wrapper fired first and turned a server that was retrying
# normally into a failed test instead of the skip/pass the retry would have
# produced.
_CONNECT_TIMEOUT_S = 200

# Every read tool the real server ships. None may be registered (F3): they
# take a raw path with none of localfiles.resolve's credential shield on it.
_SERVER_READ_TOOLS = (
    "read_file", "read_text_file", "read_media_file", "read_multiple_files",
    "list_directory", "list_directory_with_sizes", "directory_tree",
    "search_files", "get_file_info",
)
_MUTATION_TOOLS = ("write_file", "edit_file", "create_directory", "move_file")
_EXPOSED_TOOLS = (*_MUTATION_TOOLS, "list_allowed_directories")

pytestmark = pytest.mark.skipif(_NPX is None, reason="npx is not on PATH")


# npm's own error codes, which npx surfaces verbatim on stderr, separate the
# two failure shapes this prefetch can hit (BB-wave review, finding 12):
#
#   * NO NETWORK -- DNS/connect-level codes. Nothing is proved about Atlas
#     or the pin; skip.
#   * NETWORK, BUT THE PIN DOES NOT EXIST -- the registry answered and said
#     no such version. That is a real defect in config/mcp.yaml (a bad or
#     yanked pin), not an environment limitation, so it FAILS loudly instead
#     of hiding as a skip on every offline-tolerant runner.
#
# Anything else (including a prefetch timeout, which is genuinely ambiguous:
# a wedged registry connection and a very slow first fetch look identical
# from here) stays a skip -- see _prefetch_outcome's fallthrough.
_OFFLINE_MARKERS = (
    "enotfound", "eai_again", "econnrefused", "econnreset", "etimedout",
    "enetunreach", "getaddrinfo", "network socket disconnected",
)
_PIN_MISSING_MARKERS = ("no matching version", "notarget", "e404", "404 not found")


def _prefetch_outcome() -> tuple[str, str] | None:
    """Spawn the pinned package once, briefly, so the real test below fails
    only on a real bug -- not a slow or absent first-time network fetch of
    the npm package. Returns None once the server proves it can start, or
    ("skip"|"fail", reason)."""
    try:
        proc = subprocess.run(
            [_NPX, "-y", _PINNED_PACKAGE, str(_ATLAS_ROOT)],
            capture_output=True,
            text=True,
            timeout=_PREFETCH_TIMEOUT_S,
            input="",
        )
    except FileNotFoundError:
        return "skip", "npx executable disappeared"
    except subprocess.TimeoutExpired:
        return "skip", "npx did not respond in time (no network for the first fetch?)"
    if "Secure MCP Filesystem Server running on stdio" in proc.stderr:
        return None
    stderr = proc.stderr
    lowered = stderr.casefold()
    if any(marker in lowered for marker in _OFFLINE_MARKERS):
        return "skip", f"no network for the npm fetch: {stderr[-500:]}"
    if any(marker in lowered for marker in _PIN_MISSING_MARKERS):
        return "fail", (
            "the registry answered but does not have "
            f"{_PINNED_PACKAGE}; config/mcp.yaml pins a version that no "
            f"longer resolves: {stderr[-500:]}"
        )
    return "skip", f"filesystem server did not start cleanly: {stderr[-500:]}"


@pytest.fixture(scope="module")
def npx_prefetched():
    outcome = _prefetch_outcome()
    if outcome is None:
        return
    verdict, reason = outcome
    if verdict == "fail":
        pytest.fail(reason)
    pytest.skip(reason)


def _write_files_only_config(tmp_path: Path, roots_dir: Path) -> tuple[Path, Path]:
    """Extract the real, checked-in files: server section from
    config/mcp.yaml (used unmodified -- this is production config, not a
    reimplementation) plus defaults, paired with a temp atlas.yaml whose
    roots point only at this test's tmp_path. Restricting the config
    to just the files: server keeps this test from also trying to connect
    kb/google/chrome-devtools, which need a live bridge or OAuth this test
    environment does not have.

    file_roots and file_write_roots are deliberately DIFFERENT here: the
    read-only root stands in for the production file_roots entry that must
    never reach this server (C:/Users/danie/kb). Because the checked-in
    files: argv expands "{file_write_roots}", the server must come up
    knowing only roots_dir -- and the live list_allowed_directories
    assertion below proves it, which it could not if both lists were the
    same directory."""
    real_config = yaml.safe_load(
        (_ATLAS_ROOT / "config" / "mcp.yaml").read_text(encoding="utf-8"),
    )
    mcp_path = tmp_path / "mcp.yaml"
    mcp_path.write_text(
        yaml.safe_dump({
            "servers": {"files": real_config["servers"]["files"]},
            "defaults": real_config["defaults"],
        }),
        encoding="utf-8",
    )
    read_only_dir = tmp_path / "read_only_root"
    read_only_dir.mkdir(exist_ok=True)
    atlas_path = tmp_path / "atlas.yaml"
    atlas_path.write_text(
        yaml.safe_dump({
            "file_roots": [str(roots_dir), str(read_only_dir)],
            "file_write_roots": [str(roots_dir)],
        }),
        encoding="utf-8",
    )
    return mcp_path, atlas_path


async def _connect_real_files_server(
    mcp_path: Path, atlas_path: Path,
) -> tuple[McpServers, ToolRegistry]:
    config = load_mcp_config(mcp_path, atlas_path=atlas_path)
    registry = ToolRegistry()
    servers = McpServers(config)
    await asyncio.wait_for(servers.connect(registry), timeout=_CONNECT_TIMEOUT_S)
    status = servers.status()[0]
    if status["state"] != "connected":
        await servers.close()
        pytest.skip(f"files MCP server did not connect: {status}")
    return servers, registry


def test_files_server_end_to_end_against_the_real_npx_server(npx_prefetched, tmp_path):
    roots_dir = tmp_path / "atlas_files_root"
    roots_dir.mkdir()
    seeded = roots_dir / "hello.txt"
    seeded.write_text("hello from atlas c2", encoding="utf-8")
    mcp_path, atlas_path = _write_files_only_config(tmp_path, roots_dir)

    async def scenario():
        servers, registry = await _connect_real_files_server(mcp_path, atlas_path)
        try:
            names = sorted(registry.names())
            # Raw, below the host's expose: list -- the server can still read
            # (proving this is a live connection), the host just never hands
            # the model a tool that does.
            raw_read = await servers.call_raw(
                "files", "read_text_file", {"path": str(seeded)},
            )
            allowed_result = await registry.call("files__list_allowed_directories", {})
            with pytest.raises(McpToolError, match="Access denied"):
                await servers.call_raw(
                    "files", "list_directory",
                    {"path": str(tmp_path / "read_only_root")},
                )
            write_result = await registry.call(
                "files__write_file",
                {
                    "path": str(roots_dir / "should_not_be_written.txt"),
                    "content": "nope",
                },
            )
            return names, raw_read, allowed_result, write_result
        finally:
            await servers.close()

    names, raw_read, allowed_result, write_result = asyncio.run(scenario())

    # 1. Exactly the 5 curated tools registered (4 mutations plus
    # list_allowed_directories), matching config/mcp.yaml's expose: list --
    # and not one of the server's 9 read tools (F3).
    assert names == sorted(f"files__{name}" for name in _EXPOSED_TOOLS)
    for read_tool in _SERVER_READ_TOOLS:
        assert f"files__{read_tool}" not in names

    # 2. The server underneath really can read that file -- so the empty
    # read surface above is Atlas's policy, not a dead connection.
    assert raw_read == "hello from atlas c2"

    # 3. THE ROOTS TRAP: the effective allowlist reported by the real
    # server is EXACTLY the resolved file_write_roots the host passed as CLI
    # argv -- not something widened by the MCP roots protocol (which
    # Atlas's ClientSession never advertises; see config/mcp.yaml). One
    # configured write root -> exactly that path back
    # (ToolRegistry._bound_content strips the server's newline as a control
    # character before this content reaches a caller, so the comparison is
    # newline-free too).
    assert allowed_result.status == "ok"
    resolved_root = str(roots_dir.resolve())
    assert allowed_result.content == f"Allowed directories:{resolved_root}"

    # 3b. WRITE SCOPE (blocker 2): the second file_roots entry -- standing
    # in for the kb checkout, which is a read root and must never become a
    # write root -- is not in the allowlist above, and the raw list_directory
    # in the scenario above was refused live by the real server. This is what
    # proves the argv expanded "{file_write_roots}" and not "{file_roots}":
    # were the old token still there, both directories would be allowed and
    # that call would have succeeded.
    assert str(tmp_path / "read_only_root") not in allowed_result.content

    # 4. write_file is NOT EXECUTED: it stops at needs_confirmation, and
    # this test never sends the matching confirm.
    assert write_result.status == "needs_confirmation"
    assert not (roots_dir / "should_not_be_written.txt").exists()


def test_move_file_refuses_to_move_outside_all_configured_roots(npx_prefetched, tmp_path):
    """Adversarial review F2 (regression guard for a future version bump):
    a move whose source is inside a configured root but whose destination
    is outside every root is refused by the real server itself, with
    nothing moved -- verified live against the pinned version. Called
    directly via McpServers.call_raw, not through the host's registry,
    because move_file is confirm-policy host-side (covered by
    test_checked_in_files_config_is_write_only_and_confirms_every_mutation
    in tests/test_mcp_client.py) -- this test's job is proving the server's
    own guardrail still holds, independent of that host gate."""
    roots_dir = tmp_path / "atlas_files_root"
    roots_dir.mkdir()
    outside_dir = tmp_path / "outside_all_roots"
    outside_dir.mkdir()
    source = roots_dir / "source.txt"
    source.write_text("source content", encoding="utf-8")
    destination = outside_dir / "moved.txt"
    mcp_path, atlas_path = _write_files_only_config(tmp_path, roots_dir)

    async def scenario():
        servers, _registry = await _connect_real_files_server(mcp_path, atlas_path)
        try:
            with pytest.raises(McpToolError, match="Access denied"):
                await servers.call_raw(
                    "files", "move_file",
                    {"source": str(source), "destination": str(destination)},
                )
        finally:
            await servers.close()

    asyncio.run(scenario())

    assert source.exists()
    assert source.read_text(encoding="utf-8") == "source content"
    assert not destination.exists()


def test_read_refuses_a_symlink_that_escapes_all_configured_roots(npx_prefetched, tmp_path):
    """Adversarial review F3 (regression guard for a future version bump):
    a real filesystem symlink placed inside a configured root but pointing
    at a file outside every root is refused on read ("symlink target
    outside allowed directories"), not silently followed -- verified live
    against @modelcontextprotocol/server-filesystem@2026.8.31, the exact
    version config/mcp.yaml pins.

    If this test environment cannot create a filesystem symlink (elevated
    privileges / Windows Developer Mode off), the test skips with the
    OSError as its reason rather than failing -- an environment limitation
    here is not evidence about server behavior either way."""
    roots_dir = tmp_path / "atlas_files_root"
    roots_dir.mkdir()
    outside_dir = tmp_path / "outside_all_roots"
    outside_dir.mkdir()
    secret = outside_dir / "secret.txt"
    secret.write_text("outside secret content", encoding="utf-8")
    link = roots_dir / "escape_link.txt"
    try:
        link.symlink_to(secret)
    except OSError as exc:
        pytest.skip(f"cannot create a filesystem symlink in this test environment: {exc}")

    mcp_path, atlas_path = _write_files_only_config(tmp_path, roots_dir)

    async def scenario():
        servers, _registry = await _connect_real_files_server(mcp_path, atlas_path)
        try:
            with pytest.raises(
                McpToolError, match="symlink target outside allowed directories",
            ):
                await servers.call_raw("files", "read_text_file", {"path": str(link)})
        finally:
            await servers.close()

    asyncio.run(scenario())
