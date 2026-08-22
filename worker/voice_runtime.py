"""Standalone production composition for Atlas voice admission.

This is the only bridge from the live voice process to the durable fast/slow work plane.
It deliberately knows nothing about kb, MCP tools, or model-driven execution.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any, Callable, Mapping

from .frontdesk import FrontDesk
from .jobstore import JobStore
from .payload_codec import PayloadCodec, WindowsCurrentUserDPAPICodec
from .subscription_worker import WorkerHealth, WorkerHealthStatus
from .turn_interpreter import TurnInterpreter
from .voice_frontdesk import VoiceFrontDesk
from .worker_health_file import DEFAULT_HEALTH_FILE, health_path, read_health


DEFAULT_JOB_STORE = "%LOCALAPPDATA%/Atlas/jobs.sqlite3"
ATLAS_ROOT = Path(__file__).resolve().parents[1]
PERSONA_PATH = ATLAS_ROOT / "config" / "persona.md"


def job_events_projection(store: JobStore, job_id: str) -> list[dict[str, Any]]:
    result = []
    for event in store.events(job_id):
        item: dict[str, Any] = {
            "sequence": event.sequence,
            "kind": event.kind.value,
            "state": event.state.value,
            "timestamp": event.timestamp,
        }
        for field in ("code", "summary", "reason", "worker_id"):
            value = event.public_payload.get(field)
            if isinstance(value, str):
                item[field] = value
        result.append(item)
    return result


@dataclass(slots=True)
class VoiceRuntime:
    """Resources owned by one voice-worker process."""

    desk: VoiceFrontDesk
    store: JobStore

    def close(self) -> None:
        self.store.close()

    def jobs_projection(self) -> list[dict[str, Any]]:
        result = []
        for job in self.store.recent_jobs(50):
            item = {
                "id": job.job_id,
                "status": job.state.value,
                "lane": job.lane.value,
                "operation": job.request.operation,
                "updated_at": str(job.updated_at),
            }
            for field in ("code", "proposal_id", "summary"):
                value = job.public_payload.get(field)
                if isinstance(value, str):
                    item[field] = value
            if job.public_payload.get("result_available") is True:
                item["result_available"] = True
            result.append(item)
        return result

    def job_events_projection(self, job_id: str) -> list[dict[str, Any]]:
        return job_events_projection(self.store, job_id)


def _expanded_store_path(value: str) -> Path:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("invalid Atlas job-store path")
    expanded = os.path.expanduser(os.path.expandvars(value.strip()))
    path = Path(expanded)
    if not path.is_absolute():
        raise ValueError("Atlas job-store path must be absolute")
    return path.resolve()


def build_voice_runtime(
    cfg: Mapping[str, Any],
    *,
    structured_client: Any,
    payload_codec: PayloadCodec,
    worker_health: WorkerHealth | None = None,
    health_provider: Callable[[], WorkerHealth] | None = None,
    persona: str | None = None,
) -> VoiceRuntime:
    """Assemble the interpreter and durable front desk from injected trusted seams."""
    if not isinstance(cfg, Mapping):
        raise TypeError("Atlas config must be a mapping")
    store_path = _expanded_store_path(str(cfg.get("job_store_path", DEFAULT_JOB_STORE)))
    store = JobStore(store_path, payload_codec=payload_codec)
    try:
        if persona is None:
            try:
                persona = PERSONA_PATH.read_text(encoding="utf-8")
            except OSError:
                persona = ""
        interpreter = TurnInterpreter(
            structured_client,
            model=str(cfg.get("fast_model", "claude-haiku-4-5")),
            timeout=float(cfg.get("interpreter_timeout_s", 1.5)),
            max_tokens=int(cfg.get("interpreter_max_tokens", 256)),
            persona=persona,
        )
        health = worker_health or WorkerHealth(
            WorkerHealthStatus.UNAVAILABLE,
            "subscription_not_activated",
            worker_id="atlas-subscription",
        )
        return VoiceRuntime(
            VoiceFrontDesk(
                interpreter,
                FrontDesk(store=store, worker_health=health, health_provider=health_provider),
            ),
            store,
        )
    except Exception:
        store.close()
        raise


def build_production_voice_runtime(cfg: Mapping[str, Any]) -> VoiceRuntime:
    """Build the Windows production runtime.

    Heavy work remains unavailable until a separately supervised subscription worker publishes
    a fresh health attestation.  The voice process never guesses at subscription authorization.
    """
    import anthropic

    client = anthropic.AsyncAnthropic()
    heartbeat = health_path(str(cfg.get("subscription_health_path", DEFAULT_HEALTH_FILE)))
    return build_voice_runtime(
        cfg,
        structured_client=client.messages,
        payload_codec=WindowsCurrentUserDPAPICodec(),
        health_provider=lambda: read_health(heartbeat),
    )


__all__ = ["VoiceRuntime", "build_voice_runtime", "build_production_voice_runtime",
           "DEFAULT_JOB_STORE", "job_events_projection"]
