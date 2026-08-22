"""Host-fixed admission for contextual Atlas setup guides.

The browser chooses only a reviewed guide id. It cannot supply a prompt, command, path, tool, model,
or permission. The resulting work enters the same durable subscription queue as voice slow work.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .contracts import Request
from .frontdesk import FrontDesk, FrontDeskOutcome
from .jobstore import JobStore
from .subscription_worker import WorkerHealth


@dataclass(frozen=True, slots=True)
class SetupGuide:
    guide_id: str
    label: str
    context: str
    governing_files: tuple[str, ...]
    ready_when: str

    def instruction(self) -> str:
        files = ", ".join(self.governing_files)
        return (
            f"ATLAS GUIDED SETUP — {self.label}. This is a single-purpose contextual support run. "
            f"Current context: {self.context} Governing Atlas files: {files}. "
            "Give Daniel a concise ordered walkthrough, explain each choice in plain language, "
            "identify the exact readiness check, and call out any action that remains human-gated. "
            "Do not claim to read, edit, connect, authenticate, or execute anything: this reviewed "
            "guide profile has no machine or account authority. Do not request credentials or ask "
            f"Daniel to paste secrets. Completion condition: {self.ready_when}"
        )


GUIDES = {
    guide.guide_id: guide for guide in (
        SetupGuide(
            "voice", "Voice",
            "Atlas reported a voice, wake, microphone, speaker, or engagement configuration item.",
            ("config/atlas.yaml", "worker/app.py", "worker/wakeword.py"),
            "the live /state voice and output-device projections report the intended configuration",
        ),
        SetupGuide(
            "subscription", "Subscription worker",
            "The local subscription worker is unavailable, degraded, or stale.",
            ("worker/subscription_cli.py", "worker/subscription_supervisor.py", "config/atlas.yaml"),
            "the bounded /health projection is available with a fresh timestamp",
        ),
        SetupGuide(
            "desktop", "Desktop and local files",
            "Named desktop targets or reviewed local roots still need configuration.",
            ("config/atlas.yaml", "config/capabilities.yaml", "worker/localfiles.py"),
            "the capability catalog reports the named desktop/local-file source connected",
        ),
        SetupGuide(
            "browser", "Browser",
            "The trusted loopback browser bridge or its exact allowed origins need configuration.",
            ("config/atlas.yaml", "browser_bridge/", "worker/browser_transport.py"),
            "the capability catalog reports the browser source connected",
        ),
        SetupGuide(
            "google", "Google",
            "The external local Google credential broker is not configured or connected.",
            ("config/atlas.yaml", "worker/connectors.py", "config/capabilities.yaml"),
            "the capability catalog reports the selected Google sources connected",
        ),
        SetupGuide(
            "spotify", "Spotify",
            "A named Spotify desktop target or separately reviewed account capability is missing.",
            ("config/atlas.yaml", "config/capabilities.yaml", "worker/desktopapps.py"),
            "the capability catalog reports the intended Spotify capability connected",
        ),
    )
}


class GuidedSetupAdmission:
    def __init__(self, store: JobStore, health_provider: Callable[[], WorkerHealth]) -> None:
        if not isinstance(store, JobStore) or not callable(health_provider):
            raise TypeError("guided setup requires a JobStore and health provider")
        self._frontdesk = FrontDesk(store=store, health_provider=health_provider)

    def start(self, guide_id: str) -> FrontDeskOutcome:
        try:
            guide = GUIDES[guide_id]
        except (KeyError, TypeError):
            raise KeyError("unknown guided setup") from None
        request = Request(
            "atlas.guided_setup",
            target=guide.guide_id,
            steps=4,
            verification=True,
            risk="low",
        )
        return self._frontdesk.submit(
            request,
            public_payload={"summary": f"Configure {guide.label}"},
            raw_utterance=guide.instruction(),
        )


__all__ = ["GUIDES", "GuidedSetupAdmission", "SetupGuide"]
