import ctypes
import os
from pathlib import Path

import pytest

from worker import desktopapps
from worker.desktopapps import DesktopAppError, DesktopApps, native_launcher


def test_default_profiles_match_configured_signed_desktop_apps():
    assert set(desktopapps.DEFAULT_PROFILES) == {
        "vscode", "wt", "chrome", "notepad", "spotify",
    }
    assert desktopapps.DEFAULT_PROFILES["vscode"].executable == "Code.exe"
    assert desktopapps.DEFAULT_PROFILES["wt"].executable == "wt.exe"
    assert desktopapps.DEFAULT_PROFILES["wt"].close_executable == "WindowsTerminal.exe"
    assert desktopapps.DEFAULT_PROFILES["chrome"].executable == "chrome.exe"
    assert desktopapps.DEFAULT_PROFILES["notepad"].executable == "notepad.exe"
    assert desktopapps.DEFAULT_PROFILES["spotify"].executable == "Spotify.exe"


def test_profile_status_uses_signed_resolution_and_closed_details():
    profiles = {
        "ready": desktopapps.AppProfile("ready", "ready.exe"),
        "missing": desktopapps.AppProfile(
            "missing", "C:/private/profile/tool.exe?token=secret --inspect",
        ),
        "broken": desktopapps.AppProfile("broken", "broken.exe"),
    }

    def resolver(executable):
        if executable == "ready.exe":
            return "C:/private/install/ready.exe"
        if executable.startswith("C:/private/profile/tool.exe"):
            raise DesktopAppError("private path must not escape")
        raise RuntimeError("private resolver detail")

    assert desktopapps.status(profiles=profiles, resolver=resolver) == [
        {"name": "ready", "state": "configured", "detail": "signed executable found"},
        {
            "name": "missing", "state": "not_configured",
            "detail": "signed executable not found: tool.exe",
        },
        {"name": "broken", "state": "error", "detail": "profile check failed"},
    ]


def test_status_snapshot_resolves_lazily_once_across_twenty_reads():
    calls = []
    snapshot = desktopapps.StatusSnapshot(
        profiles={"tool": desktopapps.AppProfile("tool", "tool.exe")},
        resolver=lambda executable: calls.append(executable) or "C:/signed/tool.exe",
        clock=lambda: "2026-08-27T12:00:00+00:00",
    )

    values = [snapshot.get() for _ in range(20)]

    assert calls == ["tool.exe"]
    assert all(value == values[0] for value in values)
    assert values[0] == {
        "apps": [{
            "name": "tool", "state": "configured",
            "detail": "signed executable found",
        }],
        "as_of": "2026-08-27T12:00:00+00:00",
    }


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


def test_close_profile_uses_allowlisted_image_without_forcing_termination(monkeypatch):
    captured = {}
    monkeypatch.setattr(desktopapps, "_taskkill_executable", lambda: "C:/Windows/taskkill.exe")

    class Result:
        returncode = 0

    def killer(command, **kwargs):
        captured.update(command=command, kwargs=kwargs)
        return Result()

    result = desktopapps.close_profile("vscode", killer=killer)

    assert captured["command"] == ["C:/Windows/taskkill.exe", "/IM", "Code.exe"]
    assert "/F" not in captured["command"]
    assert captured["kwargs"]["shell"] is False
    assert result == {"application": "vscode", "closed": True}


def test_close_terminal_targets_every_windows_terminal_window(monkeypatch):
    captured = {}
    monkeypatch.setattr(desktopapps, "_taskkill_executable", lambda: "taskkill.exe")

    class Result:
        returncode = 0

    def killer(command, **_kwargs):
        captured["command"] = command
        return Result()

    desktopapps.close_profile("wt", killer=killer)

    assert captured["command"] == ["taskkill.exe", "/IM", "WindowsTerminal.exe"]
    assert desktopapps._EXPECTED_PUBLISHERS["WindowsTerminal.exe"] == "Microsoft Corporation"


def test_notepad_profile_opens_and_closes_the_signed_windows_image(monkeypatch):
    captured = {}
    windows = Path("C:/Windows")
    monkeypatch.setattr(desktopapps, "_windows_directory", lambda: windows)
    monkeypatch.setattr(Path, "is_file", lambda self: self == windows / "System32/notepad.exe")
    monkeypatch.setattr(
        desktopapps,
        "_verify_authenticode_publisher",
        lambda path, publisher: captured.update(path=path, publisher=publisher) or True,
    )
    monkeypatch.setattr(desktopapps.os, "name", "nt")
    monkeypatch.setattr(desktopapps, "_taskkill_executable", lambda: "taskkill.exe")

    class Result:
        returncode = 0

    def killer(command, **_kwargs):
        captured["close_command"] = command
        return Result()

    resolved = desktopapps._resolve_executable("notepad.exe")
    desktopapps.close_profile("notepad", killer=killer)

    assert resolved == str((windows / "System32/notepad.exe").resolve())
    assert captured["publisher"] == "Microsoft Windows"
    assert captured["close_command"] == ["taskkill.exe", "/IM", "notepad.exe"]


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


def test_native_launcher_resolves_signed_path_before_focusing_exact_existing_window(
    monkeypatch,
):
    calls = []
    selected = {"title": "notes.txt - Notepad", "pid": 91, "_handle": 9001}
    monkeypatch.setattr(
        desktopapps,
        "_resolve_executable",
        lambda executable: calls.append(("resolve", executable)) or "C:/Windows/notepad.exe",
    )
    monkeypatch.setattr(
        desktopapps,
        "_visible_profile_window",
        lambda executable: calls.append(("inventory", executable)) or selected,
    )
    monkeypatch.setattr(
        desktopapps,
        "_focus_profile_window",
        lambda window: calls.append(("focus", window)),
    )

    result = native_launcher("notepad.exe", None)

    assert result == {
        "application": "notepad.exe",
        "pid": 91,
        "focused": True,
        "existing": True,
    }
    assert calls == [
        ("resolve", "notepad.exe"),
        ("inventory", "C:/Windows/notepad.exe"),
        ("focus", selected),
    ]


def test_profile_focus_carries_selected_hwnd_instead_of_resolving_pid(monkeypatch):
    selected = {"title": "one", "pid": 91, "_handle": 9001}
    calls = []

    class FakeDesktopControl:
        def focus_resolved_window(self, window):
            calls.append(window)
            return {"focused": window["title"], "pid": window["pid"]}

        def focus_window(self, **_target):
            pytest.fail("selected window must not be resolved again by pid")

    monkeypatch.setattr(desktopapps, "_desktopcontrol", lambda: FakeDesktopControl())

    assert desktopapps._focus_profile_window(selected) == {"focused": "one", "pid": 91}
    assert calls == [selected]


def test_native_launcher_falls_back_to_launch_when_window_inventory_fails(monkeypatch):
    from worker.desktopcontrol import DesktopControlError

    monkeypatch.setattr(
        desktopapps, "_resolve_executable", lambda _executable: "C:/Windows/notepad.exe",
    )
    monkeypatch.setattr(
        desktopapps,
        "_visible_profile_window",
        lambda _path: (_ for _ in ()).throw(DesktopControlError("inventory failed")),
    )

    class Proc:
        pid = 42

    monkeypatch.setattr(
        desktopapps.subprocess,
        "Popen",
        lambda *_args, **_kwargs: Proc(),
    )

    assert native_launcher("notepad.exe", None) == {
        "application": "notepad.exe", "pid": 42, "targeted": False,
    }


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


def test_spotify_resolver_uses_signed_windows_app_alias(tmp_path, monkeypatch):
    local = tmp_path / "local"
    candidate = local / "Microsoft/WindowsApps/Spotify.exe"
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

    assert desktopapps._resolve_executable("Spotify.exe") == str(candidate.resolve())
    assert calls == [(candidate.resolve(), "Spotify AB")]


def _appexeclink_buffer(
    *fields,
    tag=desktopapps._IO_REPARSE_TAG_APPEXECLINK,
    version=3,
    declared_length=None,
):
    payload = (
        version.to_bytes(4, "little")
        + "\x00".join(fields).encode("utf-16-le")
        + b"\x00\x00"
    )
    length = len(payload) if declared_length is None else declared_length
    return (
        tag.to_bytes(4, "little")
        + length.to_bytes(2, "little")
        + b"\x00\x00"
        + payload
    )


def test_app_execution_link_parser_accepts_only_well_formed_absolute_targets():
    well_formed = _appexeclink_buffer(
        "Microsoft.WindowsTerminal_8wekyb3d8bbwe",
        "Microsoft.WindowsTerminal_8wekyb3d8bbwe!App",
        "C:\\Program Files\\WindowsApps\\Microsoft.WindowsTerminal\\wt.exe",
        "0",
    )

    assert desktopapps._parse_app_execution_link(well_formed) == (
        "C:\\Program Files\\WindowsApps\\Microsoft.WindowsTerminal\\wt.exe"
    )
    assert desktopapps._reparse_tag(well_formed) == 0x8000001B
    # Wrong reparse tag: a symlink or junction is never an app-execution alias.
    assert desktopapps._parse_app_execution_link(
        _appexeclink_buffer("pkg", "pkg!App", "C:\\signed\\wt.exe", tag=0xA000000C),
    ) is None
    # Truncated payload, truncated header, and an odd-length UTF-16 tail.
    assert desktopapps._parse_app_execution_link(well_formed[:40]) is None
    assert desktopapps._parse_app_execution_link(well_formed[:6]) is None
    assert desktopapps._parse_app_execution_link(b"") is None
    even = _appexeclink_buffer("pkg", "pkg!App", "C:\\signed\\wt.exe")
    odd = even[:4] + (len(even) - 7).to_bytes(2, "little") + even[6:] + b"\x00"
    assert desktopapps._parse_app_execution_link(odd) is None
    assert desktopapps._parse_app_execution_link(
        _appexeclink_buffer("pkg", "pkg!App", "C:\\a\\wt.exe", declared_length=5),
    ) is None
    # Too few fields, an empty target, and a relative target are all unusable.
    assert desktopapps._parse_app_execution_link(_appexeclink_buffer("pkg", "pkg!App")) is None
    assert desktopapps._parse_app_execution_link(
        _appexeclink_buffer("pkg", "pkg!App", "   ", "0"),
    ) is None
    assert desktopapps._parse_app_execution_link(
        _appexeclink_buffer("pkg", "pkg!App", "wt.exe", "0"),
    ) is None
    assert desktopapps._reparse_tag(b"\x01\x02") is None


def _install_alias_roots(tmp_path, monkeypatch):
    """Point both the candidate roots and the admin-only alias roots at tmp_path."""
    program_files = tmp_path / "program-files"
    windows = tmp_path / "windows"
    local = tmp_path / "local"
    for path in (
        program_files / "WindowsApps",
        windows / "SystemApps",
        windows / "System32",
        local,
    ):
        path.mkdir(parents=True, exist_ok=True)
    roots = {
        "local_app_data": local,
        "roaming_app_data": tmp_path / "roaming",
        "program_files": program_files,
        "program_files_x86": tmp_path / "program-files-x86",
    }
    monkeypatch.setattr(desktopapps.os, "name", "nt")
    monkeypatch.setattr(desktopapps, "_windows_directory", lambda: windows)
    monkeypatch.setattr(
        desktopapps, "_known_folder_path", lambda name: roots.get(name, tmp_path / name),
    )
    return local, program_files / "WindowsApps", windows


def test_windows_terminal_alias_resolves_to_the_signed_store_image(tmp_path, monkeypatch):
    local, windows_apps, _windows = _install_alias_roots(tmp_path, monkeypatch)
    alias = local / "Microsoft/WindowsApps/wt.exe"
    alias.parent.mkdir(parents=True)
    alias.write_bytes(b"")
    target = windows_apps / "Microsoft.WindowsTerminal_1.24_x64/wt.exe"
    target.parent.mkdir(parents=True)
    target.write_text("signed store image", encoding="utf-8")
    calls = []

    monkeypatch.setattr(
        desktopapps,
        "_read_reparse_data",
        lambda path: _appexeclink_buffer(
            "Microsoft.WindowsTerminal_8wekyb3d8bbwe",
            "Microsoft.WindowsTerminal_8wekyb3d8bbwe!App",
            str(target),
            "0",
        ) if path == alias else None,
    )
    monkeypatch.setattr(
        desktopapps,
        "_verify_authenticode_publisher",
        lambda path, publisher: calls.append((path, publisher)) or True,
    )

    assert desktopapps._resolve_executable("wt.exe") == str(target.resolve())
    assert calls == [(target.resolve(), "Microsoft Corporation")]


def test_alias_target_is_rejected_when_unsigned_or_missing(tmp_path, monkeypatch):
    local, windows_apps, _windows = _install_alias_roots(tmp_path, monkeypatch)
    alias = local / "Microsoft/WindowsApps/wt.exe"
    alias.parent.mkdir(parents=True)
    alias.write_bytes(b"")
    unsigned = windows_apps / "Microsoft.WindowsTerminal_1.24_x64/wt.exe"
    unsigned.parent.mkdir(parents=True)
    unsigned.write_text("unsigned", encoding="utf-8")
    targets = [str(unsigned), str(windows_apps / "gone/wt.exe")]

    monkeypatch.setattr(
        desktopapps,
        "_read_reparse_data",
        lambda _path: _appexeclink_buffer("pkg", "pkg!App", targets[0], "0"),
    )
    monkeypatch.setattr(desktopapps, "_verify_authenticode_publisher", lambda *_args: False)

    with pytest.raises(DesktopAppError, match="approved location"):
        desktopapps._resolve_executable("wt.exe")

    checked = []
    monkeypatch.setattr(
        desktopapps,
        "_verify_authenticode_publisher",
        lambda path, publisher: checked.append(path) or True,
    )
    targets[0] = targets[1]

    with pytest.raises(DesktopAppError, match="approved location"):
        desktopapps._resolve_executable("wt.exe")
    assert checked == []


def test_malformed_alias_data_never_falls_back_to_the_unsigned_stub(tmp_path, monkeypatch):
    local = tmp_path / "local"
    alias = local / "Microsoft/WindowsApps/wt.exe"
    alias.parent.mkdir(parents=True)
    alias.write_bytes(b"")

    monkeypatch.setattr(desktopapps.os, "name", "nt")
    monkeypatch.setattr(desktopapps, "_known_folder_path", lambda _name: local)
    monkeypatch.setattr(
        desktopapps,
        "_read_reparse_data",
        lambda _path: _appexeclink_buffer("pkg", "pkg!App", "C:\\x\\wt.exe")[:12],
    )
    monkeypatch.setattr(
        desktopapps,
        "_verify_authenticode_publisher",
        lambda *_args: pytest.fail("a malformed alias must not reach the signature check"),
    )

    with pytest.raises(DesktopAppError, match="approved location"):
        desktopapps._resolve_executable("wt.exe")


def test_unrelated_reparse_tag_keeps_the_plain_candidate_path(tmp_path, monkeypatch):
    local = tmp_path / "local"
    candidate = local / "Microsoft/WindowsApps/wt.exe"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("cloud placeholder stand-in", encoding="utf-8")
    calls = []

    monkeypatch.setattr(desktopapps.os, "name", "nt")
    monkeypatch.setattr(desktopapps, "_known_folder_path", lambda _name: local)
    monkeypatch.setattr(
        desktopapps,
        "_read_reparse_data",
        lambda _path: _appexeclink_buffer("pkg", "pkg!App", "C:\\x\\wt.exe", tag=0x9000001A),
    )
    monkeypatch.setattr(
        desktopapps,
        "_verify_authenticode_publisher",
        lambda path, publisher: calls.append((path, publisher)) or True,
    )

    assert desktopapps._resolve_executable("wt.exe") == str(candidate.resolve())
    assert calls == [(candidate.resolve(), "Microsoft Corporation")]


def test_alias_target_outside_admin_roots_is_never_signature_checked(tmp_path, monkeypatch):
    local, _windows_apps, _windows = _install_alias_roots(tmp_path, monkeypatch)
    alias = local / "Microsoft/WindowsApps/wt.exe"
    alias.parent.mkdir(parents=True)
    alias.write_bytes(b"")
    elsewhere = tmp_path / "user-writable/wt.exe"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text("attacker payload", encoding="utf-8")
    hostile = [
        r"\\evil-server\share\payload.exe",
        r"\\127.0.0.1\C$\Program Files\WindowsApps\wt.exe",
        "\\\\?\\C:\\Program Files\\WindowsApps\\wt.exe",
        "\\\\?\\UNC\\evil-server\\share\\wt.exe",
        "\\\\.\\GLOBALROOT\\Device\\HarddiskVolume3\\wt.exe",
        str(elsewhere),
    ]
    target = [hostile[0]]

    monkeypatch.setattr(
        desktopapps,
        "_read_reparse_data",
        lambda _path: _appexeclink_buffer("pkg", "pkg!App", target[0], "0"),
    )
    monkeypatch.setattr(
        desktopapps,
        "_verify_authenticode_publisher",
        lambda *_args: pytest.fail("an uncontained alias target must never be signature checked"),
    )

    for value in hostile:
        target[0] = value
        assert desktopapps._contained_alias_target(value) is None
        with pytest.raises(DesktopAppError, match="approved location"):
            desktopapps._resolve_executable("wt.exe")


def test_alias_targets_are_accepted_under_each_admin_only_root(tmp_path, monkeypatch):
    _local, windows_apps, windows = _install_alias_roots(tmp_path, monkeypatch)
    accepted = [
        windows_apps / "Microsoft.WindowsTerminal_1.24_x64/wt.exe",
        windows / "SystemApps/Microsoft.Windows.Search_cw5n1h2txyewy/SearchApp.exe",
        # A Store alias may legitimately target a system launcher, e.g. MediaPlayer.exe.
        windows / "System32/SystemUWPLauncher.exe",
    ]
    for path in accepted:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("signed system image", encoding="utf-8")
        assert desktopapps._contained_alias_target(str(path)) == path.resolve()
        assert desktopapps._contained_alias_target(str(path).upper()) == path.resolve()


def test_alias_containment_fails_closed_without_resolvable_roots(tmp_path, monkeypatch):
    target = tmp_path / "program-files/WindowsApps/wt.exe"
    target.parent.mkdir(parents=True)
    target.write_text("image", encoding="utf-8")
    monkeypatch.setattr(desktopapps.os, "name", "nt")
    monkeypatch.setattr(desktopapps, "_alias_target_roots", tuple)

    assert desktopapps._contained_alias_target(str(target)) is None
    assert desktopapps._is_local_drive_path(Path(r"\\server\share\x.exe")) is False
    assert desktopapps._is_local_drive_path(Path("relative/x.exe")) is False
    assert desktopapps._is_local_drive_path(Path(r"C:\Windows\System32\x.exe")) is True


@pytest.mark.skipif(os.name != "nt", reason="reparse points are a Windows concept")
def test_read_reparse_data_reads_a_real_junction_and_closes_its_handle():
    junction = Path(desktopapps._windows_directory().anchor) / "Users/All Users"
    attributes = ctypes.windll.kernel32.GetFileAttributesW(str(junction))
    if attributes & 0xFFFFFFFF == 0xFFFFFFFF or not attributes & 0x0400:
        pytest.skip("no default junction available on this machine")

    def handles():
        count = ctypes.c_ulong(0)
        ctypes.windll.kernel32.GetProcessHandleCount(
            ctypes.c_void_p(ctypes.windll.kernel32.GetCurrentProcess()),
            ctypes.byref(count),
        )
        return count.value

    data = desktopapps._read_reparse_data(junction)

    assert data is not None
    assert len(data) >= 8
    # IO_REPARSE_TAG_MOUNT_POINT: a real reparse point that is not an app-execution alias.
    assert desktopapps._reparse_tag(data) == 0xA000000C
    assert desktopapps._reparse_tag(data) != desktopapps._IO_REPARSE_TAG_APPEXECLINK
    assert desktopapps._parse_app_execution_link(data) is None
    assert desktopapps._candidate_image(junction) is None

    before = handles()
    for _ in range(50):
        assert desktopapps._read_reparse_data(junction) is not None
    assert handles() - before < 10


def test_spotify_resolver_falls_back_to_the_roaming_desktop_install(tmp_path, monkeypatch):
    roots = {
        "local_app_data": tmp_path / "local",
        "roaming_app_data": tmp_path / "roaming",
    }
    for root in roots.values():
        root.mkdir()
    candidate = roots["roaming_app_data"] / "Spotify/Spotify.exe"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("test executable", encoding="utf-8")
    decoy = tmp_path / "attacker-controls-this/Spotify/Spotify.exe"
    decoy.parent.mkdir(parents=True)
    decoy.write_text("decoy", encoding="utf-8")
    asked = []
    calls = []

    monkeypatch.setattr(desktopapps.os, "name", "nt")
    monkeypatch.setattr(
        desktopapps, "_known_folder_path", lambda name: asked.append(name) or roots[name],
    )
    monkeypatch.setattr(
        desktopapps,
        "_verify_authenticode_publisher",
        lambda path, publisher: calls.append((path, publisher)) or True,
    )
    monkeypatch.setenv("APPDATA", str(tmp_path / "attacker-controls-this"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "attacker-controls-this"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "attacker-controls-this"))

    assert desktopapps._resolve_executable("Spotify.exe") == str(candidate.resolve())
    assert asked == ["local_app_data", "roaming_app_data"]
    assert calls == [(candidate.resolve(), "Spotify AB")]


def test_roaming_app_data_is_a_known_folder_with_a_legacy_fallback():
    assert desktopapps._KNOWN_FOLDER_IDS["roaming_app_data"] == (
        0x3EB685DB,
        0x65F9,
        0x4CF6,
        (0xA0, 0x3A, 0xE3, 0xEF, 0x65, 0x72, 0x9F, 0x3D),
    )
    assert desktopapps._LEGACY_FOLDER_IDS["roaming_app_data"] == 0x001A


def test_reparse_read_is_skipped_off_windows_and_for_plain_files(tmp_path, monkeypatch):
    plain = tmp_path / "Spotify.exe"
    plain.write_text("plain", encoding="utf-8")

    assert desktopapps._read_reparse_data(plain) is None
    assert desktopapps._read_reparse_data(tmp_path / "absent.exe") is None
    monkeypatch.setattr(desktopapps.os, "name", "posix")
    assert desktopapps._read_reparse_data(plain) is None


def test_private_folder_launcher_uses_signed_explorer_profile(monkeypatch):
    calls = []
    monkeypatch.setattr(
        desktopapps,
        "native_launcher",
        lambda executable, target: calls.append((executable, target)) or "opened",
    )

    assert "open_folder" not in desktopapps.__all__
    assert not hasattr(desktopapps, "open_folder")
    assert desktopapps._launch_folder("C:/allowed/folder") == "opened"
    assert calls == [("explorer.exe", "C:/allowed/folder")]


def test_explorer_resolver_uses_signed_system32_executable(tmp_path, monkeypatch):
    windows = tmp_path / "windows"
    candidate = windows / "System32/explorer.exe"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("test executable", encoding="utf-8")
    calls = []

    monkeypatch.setattr(desktopapps.os, "name", "nt")
    monkeypatch.setattr(desktopapps, "_windows_directory", lambda: windows)
    monkeypatch.setattr(
        desktopapps,
        "_verify_authenticode_publisher",
        lambda path, publisher: calls.append((path, publisher)) or True,
    )

    assert desktopapps._resolve_executable("explorer.exe") == str(candidate.resolve())
    assert calls == [(candidate.resolve(), "Microsoft Windows")]


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
