import pytest

from worker import desktopapps
from worker.desktopapps import DesktopAppError, DesktopApps, TargetAlias, native_launcher


def test_code_and_windows_terminal_profiles_are_allowlisted():
    code = desktopapps.DEFAULT_PROFILES["vscode"]
    terminal = desktopapps.DEFAULT_PROFILES["wt"]

    assert (code.id, code.executable, code.target_kind) == ("vscode", "Code.exe", "path")
    assert (terminal.id, terminal.executable, terminal.target_kind) == ("wt", "wt.exe", None)


def test_profile_helpers_build_default_desktop_apps_and_delegate(monkeypatch):
    calls = []
    launcher = object()

    class FakeDesktopApps:
        def __init__(self, aliases, *, profiles, launcher):
            calls.append(("init", aliases, profiles, launcher))

        def open(self, app_id, target_alias=None):
            calls.append(("open", app_id, target_alias))
            return "opened"

        def focus(self, app_id):
            calls.append(("focus", app_id))
            return "focused"

    monkeypatch.setattr(desktopapps, "DesktopApps", FakeDesktopApps)
    monkeypatch.setattr(desktopapps, "native_launcher", launcher)

    assert desktopapps.open_profile("vscode") == "opened"
    assert desktopapps.focus_profile("wt") == "focused"
    assert calls == [
        ("init", {}, desktopapps.DEFAULT_PROFILES, launcher),
        ("open", "vscode", None),
        ("init", {}, desktopapps.DEFAULT_PROFILES, launcher),
        ("focus", "wt"),
    ]


def test_profile_helper_wraps_an_optional_url_as_a_typed_alias(monkeypatch):
    calls = []

    class FakeDesktopApps:
        def __init__(self, aliases, *, profiles, launcher):
            calls.append((aliases, profiles, launcher))

        def open(self, app_id, target_alias=None):
            calls.append((app_id, target_alias))
            return "opened"

    monkeypatch.setattr(desktopapps, "DesktopApps", FakeDesktopApps)

    assert desktopapps.open_profile("chrome", "https://example.com/") == "opened"
    assert calls[0][0] == {"target": TargetAlias("url", "https://example.com/")}
    assert calls[1] == ("chrome", "target")


def test_fixed_profiles_accept_only_typed_compatible_aliases(tmp_path):
    workspace = tmp_path / "workspace"; workspace.mkdir()
    calls = []
    apps = DesktopApps({"workspace": TargetAlias("path", workspace), "docs": TargetAlias("url", "https://docs.google.com/document/d/1"), "focus": TargetAlias("spotify_uri", "spotify:playlist:abc")}, launcher=lambda exe, arg: calls.append((exe, arg)), focuser=lambda app: app)
    apps.open("vscode", "workspace"); apps.open("file_explorer", "workspace"); apps.open("chrome", "docs"); apps.open("spotify", "focus")
    assert calls[0][0] == "Code.exe" and calls[0][1].startswith("vscode://file/")
    assert calls[2] == ("chrome.exe", "https://docs.google.com/document/d/1")
    assert calls[3] == ("Spotify.exe", "spotify:playlist:abc") and apps.focus("spotify") == "spotify"
    with pytest.raises(DesktopAppError): apps.open("chrome", "workspace")
    with pytest.raises(DesktopAppError): apps.open("powershell", "workspace")


def test_aliases_are_typed_and_validate_url_spotify_and_unknown_target(tmp_path):
    workspace = tmp_path / "workspace"; workspace.mkdir()
    with pytest.raises(DesktopAppError): DesktopApps({"bad": workspace}, launcher=lambda *_: None)
    with pytest.raises(DesktopAppError): DesktopApps({"bad": TargetAlias("url", "file:///x")}, launcher=lambda *_: None)
    with pytest.raises(DesktopAppError): DesktopApps({"bad": TargetAlias("spotify_uri", "https://spotify.com/x")}, launcher=lambda *_: None)
    apps = DesktopApps({"workspace": TargetAlias("path", workspace)}, launcher=lambda *_: None)
    with pytest.raises(DesktopAppError): apps.open("vscode", "C:\\arbitrary")


def test_native_launcher_uses_resolved_executable_without_shell(monkeypatch):
    captured = {}
    monkeypatch.setattr("worker.desktopapps._resolve_executable", lambda name: "C:/fixed/Code.exe")
    class Proc:
        pid = 42
    def popen(command, **kwargs):
        captured.update(command=command, kwargs=kwargs)
        return Proc()
    monkeypatch.setattr("worker.desktopapps.subprocess.Popen", popen)
    result = native_launcher("Code.exe", "vscode://file/C:/notes")
    assert captured["command"] == ["C:/fixed/Code.exe", "vscode://file/C:/notes"]
    assert captured["kwargs"]["shell"] is False
    assert "PATH" not in captured["kwargs"]["env"]
    assert result == {"application": "Code.exe", "pid": 42, "targeted": True}


def test_resolver_uses_known_folders_not_inherited_environment_and_checks_publisher(tmp_path, monkeypatch):
    roots = {
        "local_app_data": tmp_path / "local",
        "roaming_app_data": tmp_path / "roaming",
        "program_files": tmp_path / "program-files",
        "program_files_x86": tmp_path / "program-files-x86",
    }
    for root in roots.values():
        root.mkdir()
    windows = tmp_path / "windows"
    windows.mkdir()
    candidate = roots["local_app_data"] / "Programs/Microsoft VS Code/Code.exe"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("test executable", encoding="utf-8")
    calls = []

    monkeypatch.setattr(desktopapps.os, "name", "nt")
    monkeypatch.setattr(desktopapps, "_known_folder_path", lambda name: roots[name])
    monkeypatch.setattr(desktopapps, "_windows_directory", lambda: windows)
    monkeypatch.setattr(desktopapps, "_verify_authenticode_publisher",
                        lambda path, publisher: calls.append((path, publisher)) or True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "attacker-controls-this"))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "attacker-controls-this-too"))

    assert desktopapps._resolve_executable("Code.exe") == str(candidate.resolve())
    assert calls == [(candidate.resolve(), "Microsoft Corporation")]


def test_windows_terminal_resolver_uses_windows_apps_and_checks_publisher(tmp_path, monkeypatch):
    local = tmp_path / "local"
    candidate = local / "Microsoft/WindowsApps/wt.exe"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("test executable", encoding="utf-8")
    calls = []

    monkeypatch.setattr(desktopapps.os, "name", "nt")
    monkeypatch.setattr(desktopapps, "_known_folder_path", lambda name: local)
    monkeypatch.setattr(desktopapps, "_verify_authenticode_publisher",
                        lambda path, publisher: calls.append((path, publisher)) or True)

    assert desktopapps._resolve_executable("wt.exe") == str(candidate.resolve())
    assert calls == [(candidate.resolve(), "Microsoft Corporation")]


def test_resolver_fails_closed_when_expected_publisher_does_not_match(tmp_path, monkeypatch):
    roots = {
        "local_app_data": tmp_path / "local",
        "roaming_app_data": tmp_path / "roaming",
        "program_files": tmp_path / "program-files",
        "program_files_x86": tmp_path / "program-files-x86",
    }
    for root in roots.values():
        root.mkdir()
    windows = tmp_path / "windows"
    windows.mkdir()
    candidate = roots["local_app_data"] / "Programs/Microsoft VS Code/Code.exe"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("test executable", encoding="utf-8")
    monkeypatch.setattr(desktopapps.os, "name", "nt")
    monkeypatch.setattr(desktopapps, "_known_folder_path", lambda name: roots[name])
    monkeypatch.setattr(desktopapps, "_windows_directory", lambda: windows)
    monkeypatch.setattr(desktopapps, "_verify_authenticode_publisher", lambda *_: False)

    with pytest.raises(DesktopAppError, match="approved location"):
        desktopapps._resolve_executable("Code.exe")


def test_authenticode_verification_uses_fixed_windows_powershell_and_exact_publisher(tmp_path, monkeypatch):
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
    assert "-NoProfile" in captured["command"] and str(target) not in captured["command"]
    assert captured["kwargs"]["env"]["ATLAS_SIGNATURE_PATH"] == str(target)
    assert "PATH" not in captured["kwargs"]["env"]
    assert captured["kwargs"]["stdin"] is desktopapps.subprocess.DEVNULL

    Result.stdout = "CN=Not Google, O=Attacker"
    assert desktopapps._verify_authenticode_publisher(target, "Google LLC") is False
