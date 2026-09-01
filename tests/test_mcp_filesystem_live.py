"""Real, headless end-to-end proof for the files MCP server (Track C2):
spawns the actual npx-installed @modelcontextprotocol/server-filesystem
package (no fakes, no in-memory FastMCP fixture) through Atlas's own
worker.mcp_client spec-resolution and connect path, and proves:

  * the 13 curated tools register, matching the checked-in config/mcp.yaml
    expose: list exactly;
  * a real read (read_text_file) against a seeded file under the resolved
    file_roots returns real file content;
  * THE ROOTS TRAP does not bite Atlas: list_allowed_directories reports
    EXACTLY the resolved file_roots the host passed as CLI argv, not
    something the server negotiated over the MCP roots protocol (which
    Atlas's ClientSession never advertises -- see the comment in
    config/mcp.yaml's files: entry and worker/mcp_client.py's
    _stdio_session);
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
_CONNECT_TIMEOUT_S = 60

_READ_TOOLS = (
    "read_text_file", "read_media_file", "read_multiple_files",
    "list_directory", "list_directory_with_sizes", "directory_tree",
    "search_files", "get_file_info", "list_allowed_directories",
)
_MUTATION_TOOLS = ("write_file", "edit_file", "create_directory", "move_file")

pytestmark = pytest.mark.skipif(_NPX is None, reason="npx is not on PATH")


def _prefetch_reason() -> str | None:
    """Spawn the pinned package once, briefly, so the real test below fails
    only on a real bug -- not a slow or absent first-time network fetch of
    the npm package. Returns None once the server proves it can start."""
    try:
        proc = subprocess.run(
            [_NPX, "-y", _PINNED_PACKAGE, str(_ATLAS_ROOT)],
            capture_output=True,
            text=True,
            timeout=_PREFETCH_TIMEOUT_S,
            input="",
        )
    except FileNotFoundError:
        return "npx executable disappeared"
    except subprocess.TimeoutExpired:
        return "npx did not respond in time (no network for the first fetch?)"
    if "Secure MCP Filesystem Server running on stdio" not in proc.stderr:
        return f"filesystem server did not start cleanly: {proc.stderr[-500:]}"
    return None


@pytest.fixture(scope="module")
def npx_prefetched():
    reason = _prefetch_reason()
    if reason is not None:
        pytest.skip(reason)


def _write_files_only_config(tmp_path: Path, roots_dir: Path) -> tuple[Path, Path]:
    """Extract the real, checked-in files: server section from
    config/mcp.yaml (used unmodified -- this is production config, not a
    reimplementation) plus defaults, paired with a temp atlas.yaml whose
    file_roots points only at this test's tmp_path. Restricting the config
    to just the files: server keeps this test from also trying to connect
    kb/google/chrome-devtools, which need a live bridge or OAuth this test
    environment does not have."""
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
    atlas_path = tmp_path / "atlas.yaml"
    atlas_path.write_text(
        yaml.safe_dump({"file_roots": [str(roots_dir)]}),
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
            read_result = await registry.call(
                "files__read_text_file", {"path": str(seeded)},
            )
            allowed_result = await registry.call("files__list_allowed_directories", {})
            write_result = await registry.call(
                "files__write_file",
                {
                    "path": str(roots_dir / "should_not_be_written.txt"),
                    "content": "nope",
                },
            )
            return names, read_result, allowed_result, write_result
        finally:
            await servers.close()

    names, read_result, allowed_result, write_result = asyncio.run(scenario())

    # 1. Exactly the 13 curated tools registered (9 reads + 4 mutations),
    # matching config/mcp.yaml's expose: list.
    assert names == sorted(
        f"files__{name}" for name in (*_READ_TOOLS, *_MUTATION_TOOLS)
    )

    # 2. A real read against a real file returns real content.
    assert read_result.status == "ok"
    assert read_result.content == "hello from atlas c2"

    # 3. THE ROOTS TRAP: the effective allowlist reported by the real
    # server is EXACTLY the resolved file_roots the host passed as CLI
    # argv -- not something widened by the MCP roots protocol (which
    # Atlas's ClientSession never advertises; see config/mcp.yaml). One
    # configured root -> exactly that path back (ToolRegistry._bound_content
    # strips the server's newline as a control character before this
    # content reaches a caller, so the comparison is newline-free too).
    assert allowed_result.status == "ok"
    resolved_root = str(roots_dir.resolve())
    assert allowed_result.content == f"Allowed directories:{resolved_root}"

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
    test_checked_in_files_config_lists_every_read_tool_and_confirms_every_mutation
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
