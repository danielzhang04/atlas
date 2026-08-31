"""OPTIONAL live A/B probe: drives real utterances through the real model.

Uses worker.runtime.build() (the same composition worker.chat runs) with
recorders swapped in for open/focus/close/launch_work/cancel_work so nothing
actually launches on the desktop -- but the system prompt, tool schemas, and
model call are all the real production Atlas configuration. This spends real
API budget against the subscription-only lane (config/atlas.yaml fast_model)
and must NEVER be run in CI or automated loops. Run it by hand, after a
prompt change, to see whether it moves model behavior the way you intend.

Usage (from the Atlas repo root, shared venv active):
    python scripts/prompt_ab.py
    python scripts/prompt_ab.py "open spotify" "open my other chrome profile"

With no arguments it runs the fixed regression set below: utterances a prior
manual probe showed the AA2 prompt fix (BASE_SYSTEM tool-tiering language +
the honest, alias-listing `open` schema) converting from a hallucinated
"Music is playing" (no tool call at all) into a real open('spotify') call.

Requires the same environment worker.chat needs: ANTHROPIC_API_KEY reachable
(via ~/.atlas/env or the shell) and config/atlas.yaml's fast_model reachable.
Does not connect MCP servers (mirrors `worker.chat --no-mcp`), so MCP-backed
tools like count_mail are absent from the schema set for this run.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys

ATLAS = Path(__file__).resolve().parents[1]
if str(ATLAS) not in sys.path:
    sys.path.insert(0, str(ATLAS))

import yaml

from tests.promptharness import Recorder
from worker import envload, runtime

DEFAULT_UTTERANCES = (
    "open spotify",
    "put on some music",
    "open my other chrome profile",
)


async def _run_one(utterance: str) -> None:
    envload.load_private_environment()
    cfg = yaml.safe_load((ATLAS / "config" / "atlas.yaml").read_text(encoding="utf-8")) or {}
    recorder = Recorder()
    services = runtime.build(
        cfg,
        tool_overrides={
            "opener": recorder.opener,
            "profile_opener": recorder.profile_opener,
            "profile_focuser": recorder.profile_focuser,
            "profile_closer": recorder.profile_closer,
        },
    )
    tool_calls: list[tuple[str, str]] = []
    services.brain.on_tool = lambda name, result: tool_calls.append((name, result.status))

    print(f"\n=== {utterance!r} ===")
    reply_chunks: list[str] = []
    async for chunk in services.brain.respond(utterance):
        reply_chunks.append(chunk)
    print("reply:", "".join(reply_chunks).strip())
    print("tool calls:", tool_calls or "(none)")
    print("recorded side effects:", recorder.calls or "(none)")
    services.store.close()


async def _main(utterances: tuple[str, ...]) -> None:
    for utterance in utterances:
        await _run_one(utterance)


if __name__ == "__main__":
    asyncio.run(_main(tuple(sys.argv[1:]) or DEFAULT_UTTERANCES))
