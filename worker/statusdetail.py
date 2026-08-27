"""Closed, public-safe status detail vocabulary shared by health producers."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import re
from typing import Any, Pattern


__all__ = [
    "STATUS_DETAIL_RENDERERS",
    "render_status_detail",
    "safe_ascii_basename",
    "status_detail_allowed",
]


@dataclass(frozen=True, slots=True)
class DetailRenderer:
    render: Callable[..., str]
    pattern: Pattern[str]


def safe_ascii_basename(value: Any, *, fallback: str = "executable") -> str:
    """Return a bounded display basename without paths, argv, URLs, or Unicode."""
    if not isinstance(value, str):
        return fallback
    candidate = value.strip()
    cut_at = min(
        (index for marker in "?#" if (index := candidate.find(marker)) >= 0),
        default=len(candidate),
    )
    candidate = candidate[:cut_at].strip()
    if candidate[:1] in {'"', "'"}:
        quote = candidate[0]
        end = candidate.find(quote, 1)
        candidate = candidate[1:end if end >= 0 else None]
    else:
        executable = re.match(r"^(.*?\.exe)(?:\s+.*)?$", candidate, re.IGNORECASE)
        candidate = executable.group(1) if executable is not None else candidate.split(maxsplit=1)[0]
    basename = candidate.replace("\\", "/").rsplit("/", 1)[-1]
    safe = "".join(
        char
        for char in basename
        if char.isascii() and (char.isalnum() or char in "._+-")
    ).strip("._")
    return (safe or fallback)[:80]


def _fixed(value: str) -> DetailRenderer:
    return DetailRenderer(lambda: value, re.compile(re.escape(value)))


def _executable_missing(prefix: str) -> DetailRenderer:
    return DetailRenderer(
        lambda *, executable: f"{prefix}: {safe_ascii_basename(executable)}",
        re.compile(re.escape(prefix) + r": [A-Za-z0-9_+.-]{1,80}"),
    )


STATUS_DETAIL_RENDERERS: dict[str, dict[str, DetailRenderer]] = {
    "connecting": {
        "pending": _fixed("connection pending"),
    },
    "connected": {
        "ready": _fixed("ready"),
    },
    "configured": {
        "signed_found": _fixed("signed executable found"),
    },
    "not_configured": {
        "disabled": _fixed("disabled by configuration"),
        "config_file_missing": _fixed("config file missing"),
        "config_entry_missing": _fixed("config entry missing"),
        "signed_missing": _executable_missing("signed executable not found"),
    },
    "error": {
        "config_unreadable": _fixed("config unreadable"),
        "config_malformed": _fixed("config malformed"),
        "transport_unavailable": _fixed("transport unavailable"),
        "timeout": DetailRenderer(
            lambda *, timeout_s: f"handshake timeout after {timeout_s:g}s",
            re.compile(r"handshake timeout after [0-9]+(?:\.[0-9]+)?s"),
        ),
        "session_required": _fixed("session required"),
        "closed_initialize": _fixed("closed during initialize"),
        "listing_failed": _fixed("tool listing failed"),
        "executable_missing": _executable_missing("executable not found"),
        "spawn_failed": _fixed("spawn failed"),
        "profile_failed": _fixed("profile check failed"),
        "status_unavailable": _fixed("status unavailable"),
    },
}


def render_status_detail(state: str, key: str, **values: Any) -> str:
    detail = STATUS_DETAIL_RENDERERS[state][key].render(**values)
    if not status_detail_allowed(state, detail):
        raise ValueError("status detail renderer produced an invalid value")
    return detail


def status_detail_allowed(state: str, detail: str) -> bool:
    renderers = STATUS_DETAIL_RENDERERS.get(state)
    return (
        isinstance(detail, str)
        and detail.isascii()
        and 0 < len(detail) <= 120
        and renderers is not None
        and any(renderer.pattern.fullmatch(detail) for renderer in renderers.values())
    )
