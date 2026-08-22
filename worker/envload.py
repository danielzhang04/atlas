"""Load Atlas's user-private environment without exposing values to logs or callers."""
from __future__ import annotations

import os
from pathlib import Path


def load_private_environment(path: Path | None = None) -> None:
    envfile = path or (Path.home() / ".atlas" / "env")
    if not envfile.is_file():
        return
    for line in envfile.read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and "\x00" not in key and "=" not in key:
            os.environ.setdefault(key, value.strip())


__all__ = ["load_private_environment"]
