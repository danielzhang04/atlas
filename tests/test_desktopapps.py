import pytest

from worker import desktopapps
from worker.desktopapps import DesktopAppError, DesktopApps, native_launcher


def test_default_profiles_match_configured_signed_desktop_apps():
    assert set(desktopapps.DEFAULT_PROFILES) == {"vscode", "wt", "chrome"}
    assert desktopapps.DEFAULT_PROFILES["vscode"].executable == "Code.exe"
    assert desktopapps.DEFAULT_PROFILES["wt"].executable == "wt.exe"
    assert desktopapps.DEFAULT_PROFILES["chrome"].executable == "chrome.exe"


def test_open_and_focus_delegate_only_allowlisted_profiles():
    launches = []
    focuses = []
    apps = DesktopApps(
        launcher=lambda executable, url: launches.append((executable, url)) or "opened",
        focuser=lambda app_id: focuses.append(app_id) or "focused",
    )

    assert apps.open("vscode") == "opened"
    assert apps.focus("wt") == "focused"
    assert launches == [("Code.exe", None)]
    assert focuses == ["wt"]
    with pytest.raises(DesktopAppError, match="allowlisted"):
        apps.open("powershell")


def test_only_chrome_accepts_an_optional_https_url():
    calls = []
    apps = DesktopApps(launcher=lambda executable, url: calls.append((executable, url)))

    apps.open("chrome", "https://example.com/path")

    assert calls == [("chrome.exe", "https://example.com/path")]
    for app_id, url in (
        ("chrome", "http://example.com/"),
        ("chrome", "https://user:password@example.com/"),
        ("vscode", "https://example.com/"),
    ):
        with pytest.raises(DesktopAppError, match="only Chrome"):
            apps.open(app_id, url)


def test_profile_helpers_delegate_open_and_focus(monkeypatch):
    calls = []

    class FakeDesktopApps:
        def __init__(self, *, profiles, launcher):
            calls.append(("init", profiles, launcher))

        def open(self, app_id, url=None):
            calls.append(("open", app_id, url))
            return "opened"

        def focus(self, app_id):
            calls.append(("focus", app_id))
            return "focused"

    launcher = object()
    monkeypatch.setattr(desktopapps, "DesktopApps", FakeDesktopApps)
    monkeypatch.setattr(desktopapps, "native_launcher", launcher)

    assert desktopapps.open_profile("chrome", "https://example.com/") == "opened"
    assert desktopapps.focus_profile("wt") == "focused"
    assert calls == [
        ("init", desktopapps.DEFAULT_PROFILES, launcher),
        ("open", "chrome", "https://example.com/"),
        ("init", desktopapps.DEFAULT_PROFILES, launcher),
        ("focus", "wt"),
    ]


def test_native_launcher_uses_resolved_executable_without_shell(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "worker.desktopapps._resolve_executable",
        lambda _name: "C:/fixed/chrome.exe",
    )

    class Proc:
        pid = 42

    def popen(command, **kwargs):
        captured.update(command=command, kwargs=kwargs)
        return Proc()

    monkeypatch.setattr("worker.desktopapps.subprocess.Popen", popen)

    result = native_launcher("chrome.exe", "https://example.com/")

    assert captured["command"] == ["C:/fixed/chrome.exe", "https://example.com/"]
    assert captured["kwargs"]["shell"] is False
    assert "PATH" not in captured["kwargs"]["env"]
    assert result == {"application": "chrome.exe", "pid": 42, "targeted": True}


def test_resolver_uses_known_folders_not_inherited_environment_and_checks_publisher(
    tmp_path,
    monkeypatch,
):
    roots = {
        "local_app_data": tmp_path / "local",
        "program_files": tmp_path / "program-files",
        "program_files_x86": tmp_path / "program-files-x86",
    }
    for root in roots.values():
        root.mkdir()
    candidate = roots["local_app_data"] / "Programs/Microsoft VS Code/Code.exe"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("test executable", encoding="utf-8")
    calls = []

    monkeypatch.setattr(desktopapps.os, "name", "nt")
    monkeypatch.setattr(desktopapps, "_known_folder_path", lambda name: roots[name])
    monkeypatch.setattr(
        desktopapps,
        "_verify_authenticode_publisher",
        lambda path, publisher: calls.append((path, publisher)) or True,
    )
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "attacker-controls-this"))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "attacker-controls-this-too"))

    assert desktopapps._resolve_executable("Code.exe") == str(candidate.resolve())
    assert calls == [(candidate.resolve(), "Microsoft Corporation")]


def test_windows_terminal_resolver_uses_windows_apps_and_checks_publisher(
    tmp_path,
    monkeypatch,
):
    local = tmp_path / "local"
    candidate = local / "Microsoft/WindowsApps/wt.exe"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("test executable", encoding="utf-8")
    calls = []

    monkeypatch.setattr(desktopapps.os, "name", "nt")
    monkeypatch.setattr(desktopapps, "_known_folder_path", lambda _name: local)
    monkeypatch.setattr(
        desktopapps,
        "_verify_authenticode_publisher",
        lambda path, publisher: calls.append((path, publisher)) or True,
    )

    assert desktopapps._resolve_executable("wt.exe") == str(candidate.resolve())
    assert calls == [(candidate.resolve(), "Microsoft Corporation")]


def test_resolver_fails_closed_when_expected_publisher_does_not_match(
    tmp_path,
    monkeypatch,
):
    roots = {
        "local_app_data": tmp_path / "local",
        "program_files": tmp_path / "program-files",
        "program_files_x86": tmp_path / "program-files-x86",
    }
    for root in roots.values():
        root.mkdir()
    candidate = roots["local_app_data"] / "Programs/Microsoft VS Code/Code.exe"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("test executable", encoding="utf-8")
    monkeypatch.setattr(desktopapps.os, "name", "nt")
    monkeypatch.setattr(desktopapps, "_known_folder_path", lambda name: roots[name])
    monkeypatch.setattr(desktopapps, "_verify_authenticode_publisher", lambda *_args: False)

    with pytest.raises(DesktopAppError, match="approved location"):
        desktopapps._resolve_executable("Code.exe")


def test_authenticode_verification_uses_fixed_powershell_and_exact_publisher(
    tmp_path,
    monkeypatch,
):
    windows = tmp_path / "windows"
    powershell = windows / "System32/WindowsPowerShell/v1.0/powershell.exe"
    powershell.parent.mkdir(parents=True)
    powershell.write_text("trusted tool stand-in", encoding="utf-8")
    target = tmp_path / "chrome.exe"
    target.write_text("candidate", encoding="utf-8")
    captured = {}

    class Result:
        returncode = 0
        stdout = "CN=Google LLC, O=Google LLC"

    def run(command, **kwargs):
        captured.update(command=command, kwargs=kwargs)
        return Result()

    monkeypatch.setattr(desktopapps.os, "name", "nt")
    monkeypatch.setattr(desktopapps, "_windows_directory", lambda: windows)
    monkeypatch.setattr(desktopapps.subprocess, "run", run)

    assert desktopapps._verify_authenticode_publisher(target, "Google LLC") is True
    assert captured["command"][0] == str(powershell.resolve())
    assert "-NoProfile" in captured["command"]
    assert str(target) not in captured["command"]
    assert captured["kwargs"]["env"]["ATLAS_SIGNATURE_PATH"] == str(target)
    assert "PATH" not in captured["kwargs"]["env"]
    assert captured["kwargs"]["stdin"] is desktopapps.subprocess.DEVNULL

    Result.stdout = "CN=Not Google, O=Attacker"
    assert desktopapps._verify_authenticode_publisher(target, "Google LLC") is False
