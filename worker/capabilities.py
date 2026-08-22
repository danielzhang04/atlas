"""Declarative Atlas capability catalog.

Capabilities describe *what may be proposed*, not a permission to perform it.  The
catalog is intentionally data-only: credentials, URLs, filesystem roots, and account
identifiers belong in user-private runtime configuration, never this file.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import yaml


VALID_RISKS = frozenset({"T1", "T2", "T3"})
VALID_CONFIRMATIONS = frozenset({"none", "proposal", "trusted_action"})


@dataclass(frozen=True)
class Capability:
    id: str
    domain: str
    operation: str
    risk: str
    availability: str
    required_connection: str | None
    confirmation: str
    description: str = ""

    @property
    def is_mutating(self) -> bool:
        return self.operation in {"write", "send", "share", "delete", "execute", "interact"}


def _as_capability(data: Mapping[str, object]) -> Capability:
    required = ("id", "domain", "operation", "risk", "availability", "confirmation")
    missing = [key for key in required if not data.get(key)]
    if missing:
        raise ValueError(f"capability missing required fields: {', '.join(missing)}")
    item = Capability(
        id=str(data["id"]), domain=str(data["domain"]), operation=str(data["operation"]),
        risk=str(data["risk"]), availability=str(data["availability"]),
        required_connection=(str(data["required_connection"])
                             if data.get("required_connection") else None),
        confirmation=str(data["confirmation"]), description=str(data.get("description", "")),
    )
    if item.risk not in VALID_RISKS:
        raise ValueError(f"{item.id}: invalid risk {item.risk}")
    if item.confirmation not in VALID_CONFIRMATIONS:
        raise ValueError(f"{item.id}: invalid confirmation policy {item.confirmation}")
    if item.is_mutating and item.confirmation != "trusted_action":
        raise ValueError(f"{item.id}: mutating capability requires trusted_action confirmation")
    return item


def load_catalog(path: str | Path) -> dict[str, Capability]:
    """Load and validate a catalog; duplicate capability ids are rejected."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    entries = raw.get("capabilities", raw)
    if not isinstance(entries, list):
        raise ValueError("capabilities catalog must contain a list")
    catalog: dict[str, Capability] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise ValueError("capability entry must be a mapping")
        capability = _as_capability(entry)
        if capability.id in catalog:
            raise ValueError(f"duplicate capability id: {capability.id}")
        catalog[capability.id] = capability
    return catalog
