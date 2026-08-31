"""Shared recorder-swap harness for prompt-regression checks.

Builds the exact production tool registry -- same tool names, descriptions,
and input schemas the real desktop app sends the model, from the real
``config/apps.yaml`` and ``config/persona.md`` -- but with every
side-effecting builtin (``open``, ``focus``, ``close``, ``launch_work``,
``cancel_work``) swapped for a :class:`Recorder` that captures the call
instead of touching the desktop or launching a background worker.

Two callers:
  - ``tests/test_prompt_regression.py``: offline assertions on the built
    system text and tool schemas only (tier text present, alias list present
    and sorted, description bounded). No model calls; safe for CI.
  - ``scripts/prompt_ab.py``: an OPTIONAL live script (see its own docstring)
    that drives the *real* model against a fixed set of utterances with
    these same recorders wired in, and reports which tool call (if any) came
    back. Not run in CI -- it spends real API budget.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from worker.brain import BASE_SYSTEM
from worker.tools import ToolRegistry, builtin, load_apps

__all__ = [
    "ATLAS",
    "Recorder",
    "build_recording_registry",
    "build_system_text",
]

ATLAS = Path(__file__).resolve().parents[1]


@dataclass
class Recorder:
    """Captures builtin side effects instead of executing them."""

    calls: list[tuple[Any, ...]] = field(default_factory=list)

    def opener(self, url: str) -> None:
        self.calls.append(("open_web", url))

    def profile_opener(self, exe: str, url: str | None) -> dict:
        self.calls.append(("open_desktop", exe, url))
        return {"application": exe, "focused": False, "existing": False}

    def profile_focuser(self, exe: str) -> dict:
        self.calls.append(("focus", exe))
        return {"focused": exe}

    def profile_closer(self, exe: str) -> dict:
        self.calls.append(("close", exe))
        return {"closed": exe}

    def launch(self, title: str, brief: str) -> SimpleNamespace:
        self.calls.append(("launch_work", title, brief))
        return SimpleNamespace(job_id="job-harness", title=title)

    def active(self) -> list:
        return []

    def recent(self, _n: int) -> list:
        return []

    def cancel(self, job_id: str) -> SimpleNamespace:
        self.calls.append(("cancel_work", job_id))
        return SimpleNamespace(job_id=job_id, title="", state=SimpleNamespace(value="cancelled"))


def build_recording_registry(
    *, apps_path: Path | None = None,
) -> tuple[ToolRegistry, Recorder]:
    """Build the real tool registry (real aliases, real schemas); record side effects."""
    apps = load_apps(apps_path or (ATLAS / "config" / "apps.yaml"))
    recorder = Recorder()
    registry = ToolRegistry()
    builtin(
        registry,
        apps,
        recorder,
        opener=recorder.opener,
        profile_opener=recorder.profile_opener,
        profile_focuser=recorder.profile_focuser,
        profile_closer=recorder.profile_closer,
    )
    return registry, recorder


def build_system_text(*, persona_path: Path | None = None) -> str:
    """Reproduce the exact system text Brain sends: BASE_SYSTEM + persona.md."""
    persona = (persona_path or (ATLAS / "config" / "persona.md")).read_text(encoding="utf-8")
    text = BASE_SYSTEM
    if persona.strip():
        text += "\n\nVoice and personality:\n" + persona.strip()
    return text
