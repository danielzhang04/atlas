"""OPTIONAL live A/B probe: drives real utterances through the real model.

Uses worker.runtime.build() (the same composition worker.chat runs), so the
system prompt, tool schemas, and model call are all the real production Atlas
configuration. This spends real API budget against the subscription-only lane
(config/atlas.yaml fast_model) and must NEVER be run in CI or automated loops.
Run it by hand, after a prompt change, to see whether it moves model behavior
the way you intend.

SIDE EFFECTS -- exactly what is neutralised, and what is not:

  * worker.desktopcontrol is replaced in sys.modules BEFORE the registry is
    built, by a recording stub (``_recording_desktopcontrol``). Every desktop
    tool -- media_key, click, type_text, press_keys, press_delete,
    window_action, focus_window, list_windows -- therefore records instead of
    driving the real keyboard, mouse, or windows. An unstubbed attribute
    raises instead of falling through to the real module. ``normalize_chord``
    is the one function copied verbatim (pure string work, no OS call).
  * launch_work and cancel_work are replaced in the built registry
    (``_stub_side_effecting_tools``). NOTHING here can start a background
    Claude job -- that is paid work and a CLAUDE.md bottom-line violation if a
    probe script triggers it. The script aborts if either tool is missing from
    the registry, so a rename cannot silently re-arm them.
  * open_file and open_folder are replaced the same way whenever they are
    registered (they shell out to the real Explorer/shell handler through
    LocalFiles; they exist only when config/atlas.yaml sets file_roots).
  * open, focus, and close use tests.promptharness.Recorder via
    ``tool_overrides``, as before.

  Still REAL, deliberately, because they only read: find_file and read_file
  (read the configured file roots), work_status (reads the real job store),
  and the model call itself. MCP servers are never connected (mirrors
  ``worker.chat --no-mcp``), so MCP-backed tools like count_mail are absent
  from the schema set for this run.

Usage (from the Atlas repo root, shared venv active):
    python scripts/prompt_ab.py
    python scripts/prompt_ab.py "open spotify" "open my other chrome profile"

With no arguments it runs the fixed regression set below: utterances a prior
manual probe showed the AA2 prompt fix (BASE_SYSTEM tool-tiering language +
the honest, alias-listing `open` schema) converting from a hallucinated
"Music is playing" (no tool call at all) into a real open('spotify') call.

Requires the same environment worker.chat needs: ANTHROPIC_API_KEY reachable
(via ~/.atlas/env or the shell) and config/atlas.yaml's fast_model reachable.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
import sys
from types import ModuleType
from typing import Any

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

# Tools whose real implementation reaches outside this process. Each is
# swapped for a recorder in the built registry. A missing REQUIRED name aborts
# the run (a rename must never silently re-arm paid background work); the
# optional ones exist only when config/atlas.yaml sets file_roots.
_REQUIRED_STUBS = ("launch_work", "cancel_work")
_OPTIONAL_STUBS = ("open_file", "open_folder")


def _recording_desktopcontrol(recorder: Recorder) -> ModuleType:
    """A worker.desktopcontrol replacement that records instead of acting."""
    module = ModuleType("worker.desktopcontrol")

    class DesktopControlError(RuntimeError):
        pass

    def _record(name: str, result: Any = None):
        def stub(*args: Any, **kwargs: Any) -> Any:
            recorder.calls.append((f"desktopcontrol.{name}", args, kwargs))
            return result(*args) if callable(result) else result
        return stub

    def normalize_chord(chord: str) -> str:
        # Copied verbatim from worker.desktopcontrol: pure string work, no OS
        # call, and the delete-chord guard in tools.py depends on its exact
        # shape.
        if not isinstance(chord, str):
            return ""
        return "+".join(part.strip().casefold() for part in chord.split("+"))

    module.DesktopControlError = DesktopControlError
    module.normalize_chord = normalize_chord
    module.list_windows = _record("list_windows", lambda *_a: [])
    module.focus_window = _record("focus_window", {"focused": True})
    module.window_action = _record("window_action", {"stubbed": True})
    module.media_key = _record("media_key", {"stubbed": True})
    module.click = _record("click", {"stubbed": True})
    module.type_text = _record("type_text", {"stubbed": True})
    module.press_keys = _record("press_keys", {"stubbed": True})
    module.press_delete = _record("press_delete", {"stubbed": True})
    module.focused_window_identity = _record(
        "focused_window_identity", {"title": "stub", "pid": 0, "_handle": 0},
    )
    module.resolve_window = _record("resolve_window", None)
    module.focus_resolved_window = _record("focus_resolved_window", {"focused": True})
    module.find_window_by_process_path = _record("find_window_by_process_path", None)

    def __getattr__(name: str) -> Any:
        # Never fall through to the real module: an unstubbed desktop entry
        # point must fail loudly rather than press a key on Daniel's machine.
        recorder.calls.append(("desktopcontrol.UNSTUBBED", name))
        raise AttributeError(f"prompt_ab stub has no desktopcontrol.{name}")

    module.__getattr__ = __getattr__
    return module


def install_desktop_stub(recorder: Recorder) -> ModuleType:
    """Put the recording stub in sys.modules; must run BEFORE runtime.build."""
    module = _recording_desktopcontrol(recorder)
    sys.modules["worker.desktopcontrol"] = module
    return module


def stub_side_effecting_tools(registry: Any, recorder: Recorder) -> None:
    """Replace the registry's out-of-process tools with recorders."""
    registered = set(registry.names())
    missing = [name for name in _REQUIRED_STUBS if name not in registered]
    if missing:
        raise SystemExit(
            f"prompt_ab refuses to run: unstubbable tool(s) {', '.join(missing)} "
            "-- update _REQUIRED_STUBS before probing.",
        )
    for name in (*_REQUIRED_STUBS, *_OPTIONAL_STUBS):
        if name not in registered:
            continue
        tool = registry._tools[name]  # no public seam; in-process interception

        def run(arguments: dict, _name: str = name) -> Any:
            async def stubbed() -> dict:
                recorder.calls.append((f"tool:{_name}", dict(arguments)))
                return {"stubbed": _name, "arguments": dict(arguments)}
            return stubbed()

        registry._tools[name] = replace(
            tool, run=run, prepare=None, execute_prepared=None,
        )


async def _run_one(utterance: str) -> None:
    envload.load_private_environment()
    cfg = yaml.safe_load((ATLAS / "config" / "atlas.yaml").read_text(encoding="utf-8")) or {}
    recorder = Recorder()
    desktop_stub = install_desktop_stub(recorder)
    services = runtime.build(
        cfg,
        tool_overrides={
            "opener": recorder.opener,
            "profile_opener": recorder.profile_opener,
            "profile_focuser": recorder.profile_focuser,
            "profile_closer": recorder.profile_closer,
            "desktop": desktop_stub,
        },
    )
    stub_side_effecting_tools(services.registry, recorder)
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
