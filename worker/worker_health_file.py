"""Bounded local health heartbeat shared by the voice and subscription processes."""
from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

from .subscription_worker import WorkerHealth, WorkerHealthStatus


DEFAULT_HEALTH_FILE = "%LOCALAPPDATA%/Atlas/subscription-health.json"
MAX_HEALTH_BYTES = 2_048


def health_path(value: str = DEFAULT_HEALTH_FILE) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("invalid worker health path")
    result = Path(os.path.expanduser(os.path.expandvars(value)))
    if not result.is_absolute():
        raise ValueError("worker health path must be absolute")
    return result.resolve()


def publish_health(path: Path, health: WorkerHealth) -> None:
    if not isinstance(health, WorkerHealth):
        raise TypeError("health must be WorkerHealth")
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({
        "version": 1,
        "status": health.status.value,
        "reason": health.reason,
        "worker_id": health.worker_id,
        "checked_at": health.checked_at,
    }, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(payload) > MAX_HEALTH_BYTES:
        raise ValueError("health payload exceeds limit")
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def read_health(path: Path) -> WorkerHealth:
    path = Path(path).resolve()
    try:
        raw = path.read_bytes()
    except OSError:
        return WorkerHealth(WorkerHealthStatus.UNAVAILABLE, "health_file_missing",
                            worker_id="atlas-subscription")
    if not raw or len(raw) > MAX_HEALTH_BYTES:
        return WorkerHealth(WorkerHealthStatus.UNAVAILABLE, "health_file_invalid",
                            worker_id="atlas-subscription")
    try:
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict) or set(value) != {
                "version", "status", "reason", "worker_id", "checked_at"} or value["version"] != 1:
            raise ValueError
        return WorkerHealth(
            WorkerHealthStatus(value["status"]), value["reason"], value["worker_id"],
            value["checked_at"],
        )
    except (KeyError, TypeError, ValueError, UnicodeDecodeError):
        return WorkerHealth(WorkerHealthStatus.UNAVAILABLE, "health_file_invalid",
                            worker_id="atlas-subscription")


__all__ = ["DEFAULT_HEALTH_FILE", "health_path", "publish_health", "read_health"]
