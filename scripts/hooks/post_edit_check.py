"""Return non-blocking, file-local diagnostics after an edit tool runs."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

TOTAL_TIMEOUT_SECONDS = 15.0
PER_TOOL_TIMEOUT_SECONDS = 8.0
PROCESS_CLEANUP_SECONDS = 2.0
MAX_EDITED_FILES = 20
MAX_YAML_BYTES = 256 * 1024
MAX_OUTPUT_CHARS = 2_000
TRUNCATION_SUFFIX = "... (truncated)"
SHARED_VENV_SCRIPTS = Path("C:/Users/danie/Atlas/.venv/Scripts")
PATCH_PATH = re.compile(
    r"^\*\*\* (?:Update|Add) File: (.+?)\s*$|^\*\*\* Move to: (.+?)\s*$",
    re.MULTILINE,
)
RUFF_DIAGNOSTIC = re.compile(r"^.+:\d+:\d+: (?:F\d+|E9\d+|B\d+)\b")


class ToolUnavailable(Exception):
    """A requested checker could not be launched."""


def _repo_root() -> Path:
    configured = os.environ.get("ATLAS_HOOK_REPO_ROOT")
    return Path(configured).resolve() if configured else Path(__file__).resolve().parents[2]


def _path_values(tool_input: Any):
    if not isinstance(tool_input, dict):
        return

    for key in ("file_path", "path"):
        value = tool_input.get(key)
        if isinstance(value, str):
            yield value
    for key in ("paths", "files"):
        value = tool_input.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    yield item


def _patch_paths(text: str) -> tuple[list[str], set[str]]:
    paths: list[str] = []
    moved_sources: set[str] = set()
    for match in PATCH_PATH.finditer(text):
        source, destination = match.groups()
        if source is not None:
            paths.append(source)
        elif destination is not None and paths:
            moved_sources.add(paths[-1])
            paths[-1] = destination
    return paths, moved_sources


def _edited_files(payload: Any, root: Path, deadline: float) -> list[Path]:
    patch_candidates: list[str] = []
    moved_sources: set[str] = set()
    tool_input = payload.get("tool_input") if isinstance(payload, dict) else None
    patch = tool_input if isinstance(tool_input, str) else None
    if isinstance(tool_input, dict) and isinstance(tool_input.get("patch"), str):
        patch = tool_input["patch"]
    if isinstance(patch, str) and time.monotonic() < deadline:
        paths, moved_sources = _patch_paths(patch)
        patch_candidates.extend(paths[:MAX_EDITED_FILES])

    candidates = list(patch_candidates)
    for candidate in _path_values(tool_input):
        if time.monotonic() >= deadline or len(candidates) >= MAX_EDITED_FILES:
            break
        if candidate not in moved_sources:
            candidates.append(candidate)

    files: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if time.monotonic() >= deadline:
            break
        path = Path(candidate)
        try:
            resolved = (root / path).resolve() if not path.is_absolute() else path.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        if resolved.is_file() and resolved not in seen:
            seen.add(resolved)
            files.append(resolved)
    return files


def _safe_path(root: Path, deadline: float) -> str:
    excluded = {os.path.normcase(str(root.resolve())), os.path.normcase(str(Path.cwd().resolve()))}
    entries: list[str] = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if time.monotonic() >= deadline:
            break
        if not entry:
            continue
        try:
            normalized = os.path.normcase(str(Path(entry).resolve()))
        except OSError:
            continue
        if normalized not in excluded:
            entries.append(entry)
    return os.pathsep.join(entries)


def _which_without_cwd(tool: str, search_path: str) -> str | None:
    if os.name != "nt":
        return shutil.which(tool, path=search_path)

    # Python follows NeedCurrentDirectoryForExePath on Windows even with an
    # explicit PATH. This documented opt-out prevents an implicit CWD lookup.
    variable = "NoDefaultCurrentDirectoryInExePath"
    previous = os.environ.get(variable)
    os.environ[variable] = "1"
    try:
        return shutil.which(tool, path=search_path)
    finally:
        if previous is None:
            os.environ.pop(variable, None)
        else:
            os.environ[variable] = previous


def _resolve_checker(tool: str, root: Path, deadline: float) -> str:
    locations = [root / ".venv" / "Scripts"]
    configured_venv = os.environ.get("ATLAS_VENV")
    if configured_venv:
        locations.append(Path(configured_venv) / "Scripts")
    locations.append(SHARED_VENV_SCRIPTS)

    executable_name = f"{tool}.exe" if os.name == "nt" else tool
    for directory in locations:
        if time.monotonic() >= deadline:
            raise ToolUnavailable(tool)
        candidate = directory / executable_name
        try:
            if candidate.is_file():
                return str(candidate.resolve())
        except OSError:
            continue

    if time.monotonic() >= deadline:
        raise ToolUnavailable(tool)
    executable = _which_without_cwd(tool, _safe_path(root, deadline))
    if executable is None:
        raise ToolUnavailable(tool)
    try:
        executable_path = Path(executable).resolve()
        if executable_path.parent in {root.resolve(), Path.cwd().resolve()}:
            raise ToolUnavailable(tool)
    except OSError as error:
        raise ToolUnavailable(tool) from error
    return str(executable_path)


def _kill_process_tree(process: subprocess.Popen[str], deadline: float) -> None:
    remaining = max(0.1, min(PROCESS_CLEANUP_SECONDS, deadline - time.monotonic()))
    if os.name == "nt":
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        taskkill = os.path.abspath(os.path.join(system_root, "System32", "taskkill.exe"))
        try:
            subprocess.run(
                [taskkill, "/PID", str(process.pid), "/T", "/F"],
                check=False,
                capture_output=True,
                timeout=remaining,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass
        return

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        try:
            process.kill()
        except OSError:
            pass


def _run(command: list[str], root: Path, deadline: float) -> subprocess.CompletedProcess[str] | None:
    executable = _resolve_checker(command[0], root, deadline)
    invocation = [executable, *command[1:]]
    if os.name == "nt" and Path(executable).suffix.lower() in {".bat", ".cmd"}:
        if any(re.search(r"[&|<>^()%!]", argument) for argument in invocation):
            return None
        invocation = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", *invocation]

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    popen_kwargs: dict[str, Any] = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kwargs["start_new_session"] = True
    try:
        process = subprocess.Popen(
            invocation,
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **popen_kwargs,
        )
    except OSError:
        return None

    try:
        stdout, stderr = process.communicate(timeout=min(PER_TOOL_TIMEOUT_SECONDS, remaining))
    except subprocess.TimeoutExpired:
        _kill_process_tree(process, deadline)
        try:
            process.communicate(timeout=max(0.1, min(0.5, deadline - time.monotonic())))
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass
        return None
    return subprocess.CompletedProcess(invocation, process.returncode, stdout, stderr)


def _ruff_findings(target: Path, root: Path, deadline: float) -> list[str]:
    result = _run(
        ["ruff", "check", "--select", "F,E9,B", "--output-format", "concise", str(target)],
        root,
        deadline,
    )
    if result is None:
        return []
    return [line for line in result.stdout.splitlines() if RUFF_DIAGNOSTIC.match(line)]


def _same_file(left: str, right: Path) -> bool:
    try:
        return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(str(right.resolve()))
    except OSError:
        return False


def _pyright_findings(target: Path, root: Path, deadline: float) -> list[str]:
    result = _run(["pyright", "--level", "error", "--outputjson", str(target)], root, deadline)
    if result is None or time.monotonic() >= deadline:
        return []
    try:
        report = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return []

    findings: list[str] = []
    for diagnostic in report.get("generalDiagnostics", []):
        if time.monotonic() >= deadline:
            break
        if not isinstance(diagnostic, dict):
            continue
        if diagnostic.get("severity") != "error" or not _same_file(str(diagnostic.get("file", "")), target):
            continue
        start = diagnostic.get("range", {}).get("start", {})
        line = int(start.get("line", 0)) + 1
        column = int(start.get("character", 0)) + 1
        message = " ".join(str(diagnostic.get("message", "")).split())
        findings.append(f"{target}:{line}:{column}: pyright: {message}")
    return findings


def _node_findings(target: Path, root: Path, deadline: float) -> list[str]:
    result = _run(["node", "--check", str(target)], root, deadline)
    if result is None or result.returncode == 0:
        return []
    lines = result.stderr.splitlines()
    message = next((line.strip() for line in lines if "SyntaxError" in line), "JavaScript syntax error")
    location = next((line.strip() for line in lines if line.strip().startswith(str(target))), str(target))
    return [f"{location}: {message}"]


def _yaml_findings(target: Path, deadline: float) -> list[str]:
    try:
        if time.monotonic() >= deadline or target.stat().st_size > MAX_YAML_BYTES:
            return []
    except OSError:
        return []
    try:
        import yaml
    except ImportError:
        return []

    if time.monotonic() >= deadline:
        return []
    try:
        with target.open("r", encoding="utf-8") as stream:
            yaml.safe_load(stream)
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        line = getattr(mark, "line", 0) + 1
        column = getattr(mark, "column", 0) + 1
        problem = getattr(error, "problem", "invalid YAML")
        return [f"{target}:{line}:{column}: yaml: {problem}"]
    except OSError:
        return []
    if time.monotonic() >= deadline:
        return []
    return []


def _findings(files: list[Path], root: Path, deadline: float) -> list[str]:
    findings: list[str] = []
    for target in files:
        if time.monotonic() >= deadline:
            break
        suffix = target.suffix.lower()
        if suffix == ".py":
            findings.extend(_ruff_findings(target, root, deadline))
            if time.monotonic() < deadline:
                findings.extend(_pyright_findings(target, root, deadline))
        elif suffix in {".js", ".mjs"}:
            findings.extend(_node_findings(target, root, deadline))
        elif suffix in {".yaml", ".yml"}:
            findings.extend(_yaml_findings(target, deadline))
    return findings


def _encoded_output(context: str) -> str:
    def encode(value: str) -> str:
        return json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": value,
                }
            },
            separators=(",", ":"),
        )

    encoded = encode(context)
    if len(encoded) + 1 <= MAX_OUTPUT_CHARS:
        return encoded

    keep = max(0, MAX_OUTPUT_CHARS - 1 - len(encoded) + len(context) - len(TRUNCATION_SUFFIX))
    context = context[:keep] + TRUNCATION_SUFFIX
    encoded = encode(context)
    while len(encoded) + 1 > MAX_OUTPUT_CHARS and keep > 0:
        keep -= 1
        context = context[:keep] + TRUNCATION_SUFFIX
        encoded = encode(context)
    return encoded


def main() -> int:
    deadline = time.monotonic() + TOTAL_TIMEOUT_SECONDS
    try:
        payload = json.loads(sys.stdin.read())
        if time.monotonic() >= deadline:
            findings = []
        else:
            root = _repo_root()
            files = _edited_files(payload, root, deadline)
            findings = _findings(files, root, deadline)
    except Exception:
        findings = []

    print(_encoded_output("\n".join(findings)) if findings else "{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
