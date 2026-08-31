"""Install the checked-in Codex hook configuration for this worktree."""

from __future__ import annotations

import shutil
from pathlib import Path


def install_hooks(repo_root: Path | None = None) -> str:
    root = repo_root or Path(__file__).resolve().parents[1]
    source = root / "codex-hooks.json"
    destination = root / ".codex" / "hooks.json"
    destination.parent.mkdir(parents=True, exist_ok=True)

    if destination.is_file() and destination.read_bytes() == source.read_bytes():
        status = "unchanged"
    else:
        shutil.copyfile(source, destination)
        status = "installed"
    print(f"{status} .codex/hooks.json")
    return status


def main() -> int:
    install_hooks()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
