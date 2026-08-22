"""Run one Atlas text turn against the production composition."""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys

import yaml

from worker import envload, runtime
from worker.jobstore import JobState

__all__ = ["main", "run"]

ATLAS = Path(__file__).resolve().parents[1]
_STARTING = {JobState.QUEUED, JobState.LAUNCHING}


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one Atlas text turn")
    parser.add_argument("utterance", help="the utterance to send to Atlas")
    parser.add_argument("--no-mcp", action="store_true", help="skip MCP connections")
    return parser.parse_args()


async def _wait_for_launch(services: runtime.Runtime) -> None:
    try:
        async with asyncio.timeout(30):
            while any(job.state in _STARTING for job in services.work.active()):
                await asyncio.sleep(0.05)
    except TimeoutError:
        return


async def run(utterance: str, *, no_mcp: bool = False) -> None:
    envload.load_private_environment()
    cfg = yaml.safe_load((ATLAS / "config" / "atlas.yaml").read_text(encoding="utf-8")) or {}
    services = runtime.build(cfg)
    stop_work = asyncio.Event()
    work_task = asyncio.create_task(services.work.run(stop_work))

    def on_tool(name, result) -> None:
        print(f"\ntool: {name} {result.status}", flush=True)

    services.brain.on_tool = on_tool
    try:
        if not no_mcp:
            await services.mcp.connect(services.registry)
        async for chunk in services.brain.respond(utterance):
            print(chunk, end="", flush=True)
        print(flush=True)
        await _wait_for_launch(services)
    finally:
        stop_work.set()
        await asyncio.gather(work_task, return_exceptions=True)
        await services.mcp.close()
        services.store.close()


def main() -> int:
    args = _arguments()
    asyncio.run(run(args.utterance, no_mcp=args.no_mcp))
    return 0


if __name__ == "__main__":
    sys.exit(main())
