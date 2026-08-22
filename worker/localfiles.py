"""Fail-closed placeholder for Atlas local-file capabilities.

Atlas cannot currently make pathname validation, file identity, and atomic replacement one
indivisible operation on Windows. Keeping the previous path-based adapter reachable would allow
a reparse-point swap between validation and use. The capability vocabulary remains in the
catalog, but no local-file adapter may be constructed until a strong root-confinement backend is
implemented and reviewed.
"""
from __future__ import annotations

from pathlib import Path

from worker.actionbroker import ActionBroker


LOCAL_FILES_UNAVAILABLE = (
    "local file capabilities require a strong Windows root-confinement backend; "
    "local file access is disabled"
)


class LocalFileError(RuntimeError):
    pass


class LocalFilesUnavailable(LocalFileError):
    pass


class FileConflict(LocalFileError):
    """Compatibility name for callers that classify reviewed-version conflicts."""


class LocalFiles:
    """Unavailable adapter retained only as an explicit fail-closed construction boundary."""

    def __init__(self, roots: dict[str, str | Path], broker: ActionBroker, **_kwargs) -> None:
        # Fail before validating or opening any configured path and before retaining the broker.
        # This also prevents direct construction from bypassing trusted runtime assembly.
        raise LocalFilesUnavailable(LOCAL_FILES_UNAVAILABLE)
