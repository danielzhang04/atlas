"""Run the Atlas command center without microphone or voice services."""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
import webbrowser

import yaml

from worker.claude_launcher import ClaudeLauncher
from worker.jobstore import JobStore
from worker.mcp_client import McpServers, load_mcp_config
from worker import state, stateserver
from worker.work import WorkManager

__all__ = ["ATLAS", "main", "serve"]

ATLAS = Path(__file__).resolve().parents[1]


def _open_pairing_window(url: str) -> bool:
    return bool(webbrowser.open(url, new=1))


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Atlas standalone loopback UI")
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="loopback port (default: atlas.yaml state_port)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="serve without opening a browser window",
    )
    return parser.parse_args()


def _expanded_path(value: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(value)))


class _EmptyRegistry:
    def register(self, _tool) -> None:
        return None


async def serve(port: int | None = None, *, open_browser: bool = True) -> None:
    cfg = yaml.safe_load((ATLAS / "config" / "atlas.yaml").read_text(encoding="utf-8")) or {}
    store = JobStore(_expanded_path(str(cfg["job_store_path"])))
    launcher = ClaudeLauncher()
    work = WorkManager(store, launcher, _expanded_path(str(cfg["work_workspace_path"])))
    mcp = McpServers(load_mcp_config(ATLAS / "config" / "mcp.yaml"))
    publisher = state.StatePublisher(voice=cfg.get("active_voice"))
    authorizer = stateserver.PairingAuthorizer()
    serving_port = int(port if port is not None else cfg.get("state_port", 4360))
    stop_work = asyncio.Event()
    work_task = asyncio.create_task(work.run(stop_work))
    mcp_task = asyncio.create_task(mcp.connect(_EmptyRegistry()))
    try:
        server = await stateserver.start(
            publisher,
            serving_port,
            authorizer=authorizer,
            job_provider=lambda: [
                job.to_public() for job in [*work.active(), *work.recent(50)]
            ],
            job_event_provider=store.events,
            result_provider=store.result,
            cancel_provider=work.cancel,
            mcp_provider=mcp.status,
            health_provider=lambda: {"claude": launcher.available, "mcp": mcp.status()},
        )
        bootstrap_url = stateserver.pairing_url(authorizer, server.port)
        if bootstrap_url is None:
            raise RuntimeError("Atlas UI pairing is unavailable")
    except Exception:
        stop_work.set()
        await asyncio.gather(work_task, return_exceptions=True)
        await asyncio.gather(mcp_task, return_exceptions=True)
        await mcp.close()
        store.close()
        raise
    print(f"Atlas UI: http://{stateserver.HOST}:{server.port}/", flush=True)
    opened = await asyncio.to_thread(_open_pairing_window, bootstrap_url) if open_browser else False
    if open_browser and not opened:
        print("Browser launch failed; Atlas remains available at the URL above.", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        stop_work.set()
        await asyncio.gather(work_task, return_exceptions=True)
        await asyncio.gather(mcp_task, return_exceptions=True)
        await mcp.close()
        await server.stop()
        store.close()


def main() -> None:
    args = _arguments()
    try:
        asyncio.run(serve(args.port, open_browser=not args.no_browser))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
