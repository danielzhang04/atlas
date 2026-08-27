import ctypes
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "hooks" / "post_edit_check.py"
INSTALLER = ROOT / "scripts" / "install_codex_hooks.py"
CODEX_HOOKS = ROOT / "codex-hooks.json"
SHARED_PYTHON = "C:/Users/danie/Atlas/.venv/Scripts/python.exe"


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def install_python_tool_shims(tmp_path: Path) -> str:
    tool_dir = tmp_path / "tools"
    tool_dir.mkdir()
    launcher = f'@echo off\r\n"{sys.executable}" "%~dp0\\%~n0_tool.py" %*\r\n'
    checker = """import json
import re
import sys
from pathlib import Path

target = Path(sys.argv[-1]).resolve()
source = target.read_text(encoding="utf-8")
diagnostics = []
for line_number, line in enumerate(source.splitlines(), 1):
    for match in re.finditer(r"missing(?:_name|_\\d+)", line):
        diagnostics.append((line_number, match.start() + 1, match.group(0)))

if Path(sys.argv[0]).stem.startswith("ruff"):
    selected = None
    if "--select" in sys.argv:
        selected = sys.argv[sys.argv.index("--select") + 1]
    for line_number, column, name in diagnostics:
        print(f"{target}:{line_number}:{column}: F821 Undefined name `{name}`")
    if "import sys\\nimport os" in source and selected != "F,E9,B":
        print(f"{target}:1:1: I001 Import block is un-sorted or un-formatted")
else:
    print(json.dumps({
        "generalDiagnostics": [
            {
                "file": str(target),
                "severity": "error",
                "message": f'"{name}" is not defined',
                "range": {"start": {"line": line_number - 1, "character": column - 1}},
            }
            for line_number, column, name in diagnostics
        ]
    }))
"""
    for name in ("ruff", "pyright"):
        (tool_dir / f"{name}.cmd").write_text(launcher, encoding="ascii")
        (tool_dir / f"{name}_tool.py").write_text(checker, encoding="ascii")
    return str(tool_dir)


def run_hook(repo_root: Path, payload: dict, *, path: str | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["ATLAS_HOOK_REPO_ROOT"] = str(repo_root)
    if path is not None:
        env["PATH"] = path
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=repo_root,
        env=env,
        timeout=25,
        check=False,
    )


def context_from(result: subprocess.CompletedProcess[str]) -> str:
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]


def process_is_running(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and exit_code.value == 259
    finally:
        kernel32.CloseHandle(handle)


def test_python_findings_include_ruff_and_matching_pyright_error() -> None:
    with tempfile.TemporaryDirectory(prefix="atlas-post-edit-") as directory:
        root = Path(directory)
        target = root / "broken.py"
        target.write_text("answer: int = missing_name\n", encoding="utf-8")

        result = run_hook(root, {"tool_input": {"file_path": str(target)}})

        context = context_from(result)
        assert "F821" in context
        assert '"missing_name" is not defined' in context


def test_clean_python_prints_empty_object(tmp_path: Path) -> None:
    target = tmp_path / "clean.py"
    target.write_text("answer = 42\n", encoding="utf-8")

    result = run_hook(
        tmp_path,
        {"tool_input": {"path": str(target)}},
        path=install_python_tool_shims(tmp_path),
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "{}"
    assert result.stderr == ""


def test_unsorted_imports_do_not_report_style_findings(tmp_path: Path) -> None:
    target = tmp_path / "clean_unsorted.py"
    target.write_text(
        "import sys\nimport os\n\nanswer = (os.name, sys.version_info)\n",
        encoding="utf-8",
    )

    result = run_hook(
        tmp_path,
        {"tool_input": {"file_path": str(target)}},
        path=install_python_tool_shims(tmp_path),
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "{}"
    assert result.stderr == ""


def test_javascript_syntax_error_is_reported(tmp_path: Path) -> None:
    target = tmp_path / "broken.js"
    target.write_text("const answer = ;\n", encoding="utf-8")

    result = run_hook(tmp_path, {"tool_input": {"files": [str(target)]}})

    assert "SyntaxError" in context_from(result)


def test_findings_are_capped(tmp_path: Path) -> None:
    target = tmp_path / "many_errors.py"
    source = "\n".join(f"value_{number} = missing_{number}" for number in range(100))
    target.write_text(source, encoding="utf-8")

    result = run_hook(
        tmp_path,
        {"tool_input": {"paths": [str(target)]}},
        path=install_python_tool_shims(tmp_path),
    )

    context = context_from(result)
    assert len(context) <= 2_000
    assert len(result.stdout) <= 2_000
    assert context.endswith("... (truncated)")


def test_path_outside_root_is_ignored(tmp_path: Path, tmp_path_factory) -> None:
    outside = tmp_path_factory.mktemp("outside") / "broken.py"
    outside.write_text("answer = missing_name\n", encoding="utf-8")

    result = run_hook(tmp_path, {"tool_input": {"file_path": str(outside)}})

    assert result.returncode == 0
    assert result.stdout.strip() == "{}"


def test_missing_tools_are_silent(tmp_path: Path) -> None:
    target = tmp_path / "broken.js"
    target.write_text("const answer = ;\n", encoding="utf-8")
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()

    result = run_hook(tmp_path, {"tool_input": {"file_path": str(target)}}, path=str(empty_path))

    assert result.returncode == 0
    assert result.stdout.strip() == "{}"
    assert result.stderr == ""


def test_apply_patch_payload_extracts_path(tmp_path: Path) -> None:
    target = tmp_path / "patched.py"
    target.write_text("answer = missing_name\n", encoding="utf-8")
    patch = "*** Begin Patch\n*** Update File: patched.py\n*** End Patch"

    result = run_hook(
        tmp_path,
        {"tool_input": patch},
        path=install_python_tool_shims(tmp_path),
    )

    assert "F821" in context_from(result)


def test_apply_patch_move_prefers_destination_path(tmp_path: Path) -> None:
    source = tmp_path / "before.py"
    source.write_text("answer = 42\n", encoding="utf-8")
    destination = tmp_path / "after.py"
    destination.write_text("answer = missing_name\n", encoding="utf-8")
    patch = (
        "*** Begin Patch\n"
        "*** Update File: before.py\n"
        "*** Move to: after.py\n"
        "*** End Patch"
    )

    result = run_hook(
        tmp_path,
        {"tool_input": {"patch": patch}},
        path=install_python_tool_shims(tmp_path),
    )

    context = context_from(result)
    assert "F821" in context
    assert "after.py" in context
    assert "before.py" not in context


def test_repo_root_path_entry_cannot_shadow_ruff(tmp_path: Path) -> None:
    target = tmp_path / "clean.py"
    target.write_text("answer = 42\n", encoding="utf-8")
    marker = tmp_path / "ruff-ran.txt"
    (tmp_path / "ruff.cmd").write_text(
        f'@echo off\r\necho shadowed>"{marker}"\r\n',
        encoding="ascii",
    )

    result = run_hook(
        tmp_path,
        {"tool_input": {"file_path": str(target)}},
        path=str(tmp_path),
    )

    assert result.returncode == 0
    assert not marker.exists()


def test_path_fallback_excludes_cwd_when_shared_checker_is_absent(tmp_path: Path, monkeypatch) -> None:
    module = load_module(SCRIPT, "post_edit_check_path_test")
    marker = tmp_path / "ruff-ran.txt"
    (tmp_path / "ruff.cmd").write_text(
        f'@echo off\r\necho shadowed>"{marker}"\r\n',
        encoding="ascii",
    )
    monkeypatch.setattr(module, "SHARED_VENV_SCRIPTS", tmp_path / "missing-shared-venv")
    monkeypatch.delenv("ATLAS_VENV", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    with pytest.raises(module.ToolUnavailable):
        module._resolve_checker("ruff", tmp_path, time.monotonic() + 1.0)

    assert not marker.exists()


def test_checker_resolution_prefers_repo_then_atlas_then_shared(tmp_path: Path, monkeypatch) -> None:
    module = load_module(SCRIPT, "post_edit_check_resolution_order_test")
    repo_scripts = tmp_path / ".venv" / "Scripts"
    atlas_venv = tmp_path / "atlas-venv"
    atlas_scripts = atlas_venv / "Scripts"
    shared_scripts = tmp_path / "shared-venv" / "Scripts"
    for scripts in (repo_scripts, atlas_scripts, shared_scripts):
        scripts.mkdir(parents=True)
        (scripts / "ruff.exe").write_bytes(b"stub")
    monkeypatch.setenv("ATLAS_VENV", str(atlas_venv))
    monkeypatch.setattr(module, "SHARED_VENV_SCRIPTS", shared_scripts)

    deadline = time.monotonic() + 1.0
    assert Path(module._resolve_checker("ruff", tmp_path, deadline)).parent == repo_scripts
    (repo_scripts / "ruff.exe").unlink()
    assert Path(module._resolve_checker("ruff", tmp_path, deadline)).parent == atlas_scripts
    (atlas_scripts / "ruff.exe").unlink()
    assert Path(module._resolve_checker("ruff", tmp_path, deadline)).parent == shared_scripts


def test_only_first_twenty_paths_are_considered(tmp_path: Path) -> None:
    paths = []
    for number in range(20):
        target = tmp_path / f"ignored-{number}.txt"
        target.write_text("not checked\n", encoding="utf-8")
        paths.append(str(target))
    overflow = tmp_path / "overflow.py"
    overflow.write_text("answer = missing_name\n", encoding="utf-8")
    paths.append(str(overflow))

    result = run_hook(
        tmp_path,
        {"tool_input": {"paths": paths}},
        path=install_python_tool_shims(tmp_path),
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "{}"


def test_oversized_yaml_is_skipped(tmp_path: Path) -> None:
    target = tmp_path / "oversized.yaml"
    target.write_text("[unterminated\n" + ("#" * (256 * 1024)), encoding="utf-8")

    result = run_hook(tmp_path, {"tool_input": {"file_path": str(target)}})

    assert result.returncode == 0
    assert result.stdout.strip() == "{}"


def test_checker_timeout_kills_spawned_child(tmp_path: Path, monkeypatch) -> None:
    module = load_module(SCRIPT, "post_edit_check_timeout_test")
    pid_file = tmp_path / "child.pid"
    checker = tmp_path / "checker.ps1"
    checker.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        "Set-Location $env:SystemRoot\n"
        f"$child = Start-Process -FilePath '{sys.executable}' "
        "-ArgumentList @('-c', 'import time; time.sleep(60)') "
        "-PassThru -WindowStyle Hidden\n"
        "Set-Content -LiteralPath $env:ATLAS_CHILD_PID_FILE "
        "-Value \"$PID,$($child.Id)\" -Encoding ascii\n"
        "Start-Sleep -Seconds 60\n",
        encoding="ascii",
    )
    monkeypatch.setenv("ATLAS_HOOK_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("ATLAS_CHILD_PID_FILE", str(pid_file))
    powershell_dir = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0"
    monkeypatch.setenv("PATH", str(powershell_dir))
    monkeypatch.setattr(module, "PER_TOOL_TIMEOUT_SECONDS", 0.5)

    checker_pid = None
    child_pid = None
    try:
        started = time.monotonic()
        result = module._run(
            ["powershell", "-NoProfile", "-File", str(checker)],
            tmp_path,
            started + 3.0,
        )
        elapsed = time.monotonic() - started
        checker_pid, child_pid = (
            int(value) for value in pid_file.read_text(encoding="ascii").split(",")
        )

        assert result is None
        assert elapsed < 3.0
        stopped_by = time.monotonic() + 1.0
        while process_is_running(child_pid) and time.monotonic() < stopped_by:
            time.sleep(0.02)
        assert not process_is_running(child_pid)
    finally:
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
        for pid in (checker_pid, child_pid):
            if pid is not None and process_is_running(pid):
                subprocess.run(
                    [str(taskkill), "/PID", str(pid), "/T", "/F"],
                    check=False,
                    capture_output=True,
                )


def test_codex_hook_source_config_parses_and_matches() -> None:
    config = json.loads(CODEX_HOOKS.read_text(encoding="utf-8"))

    assert config == {
        "hooks": {
            "PostToolUse": [
                {
                    "matcher": "apply_patch|Edit|Write",
                    "hooks": [
                        {
                            "type": "command",
                            "command": (
                                f'"{SHARED_PYTHON}" "scripts/hooks/post_edit_check.py"'
                            ),
                            "timeout": 20,
                        }
                    ],
                }
            ]
        }
    }


def test_codex_hook_installer_is_idempotent(tmp_path: Path, capsys) -> None:
    installer = load_module(INSTALLER, "install_codex_hooks_test")
    source = tmp_path / "codex-hooks.json"
    source.write_text('{"hooks": {}}\n', encoding="ascii")

    assert installer.install_hooks(tmp_path) == "installed"
    assert (tmp_path / ".codex" / "hooks.json").read_bytes() == source.read_bytes()
    assert installer.install_hooks(tmp_path) == "unchanged"
    assert capsys.readouterr().out.splitlines() == ["installed .codex/hooks.json", "unchanged .codex/hooks.json"]


def test_claude_hook_uses_shared_venv_and_readme_documents_worktrees() -> None:
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    command = settings["hooks"]["PostToolUse"][0]["hooks"][0]["command"]

    assert command.startswith(f'"{SHARED_PYTHON}" ')
    assert "../../Atlas/.venv" not in command
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "worktrees share" in readme.lower()
