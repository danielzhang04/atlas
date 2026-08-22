"""Typed, allowlisted desktop app targets; no shell or arbitrary executable arguments."""
from __future__ import annotations

from dataclasses import dataclass
import ctypes
import os
from pathlib import Path
import re
import subprocess
from typing import Callable
from urllib.parse import urlsplit

__all__ = [
    "AppProfile",
    "DEFAULT_PROFILES",
    "DesktopAppError",
    "DesktopApps",
    "TargetAlias",
    "focus_profile",
    "native_launcher",
    "open_profile",
]

class DesktopAppError(RuntimeError): pass

@dataclass(frozen=True)
class TargetAlias:
    kind: str  # path | url | spotify_uri
    value: str | Path

@dataclass(frozen=True)
class AppProfile:
    id: str
    executable: str
    target_kind: str | None
    uri_template: str | None = None
    supports_focus: bool = True

DEFAULT_PROFILES = {
    "vscode": AppProfile("vscode", "Code.exe", "path", "vscode://file/{target}"),
    "wt": AppProfile("wt", "wt.exe", None),
    "chrome": AppProfile("chrome", "chrome.exe", "url", "{target}"),
    "spotify": AppProfile("spotify", "Spotify.exe", "spotify_uri", "{target}"),
    "file_explorer": AppProfile("file_explorer", "explorer.exe", "path", "{target}"),
}

# These identifiers are resolved through SHGetKnownFolderPath instead of inherited
# environment variables.  The latter are part of the launching process's input and
# therefore cannot establish a trusted executable root.
_KNOWN_FOLDER_IDS = {
    "local_app_data": (0xF1B32785, 0x6FBA, 0x4FCF, (0x9D, 0x55, 0x7B, 0x8E, 0x7F, 0x15, 0x70, 0x91)),
    "roaming_app_data": (0x3EB685DB, 0x65F9, 0x4CF6, (0xA0, 0x3A, 0xE3, 0xEF, 0x65, 0x72, 0x9F, 0x3D)),
    "program_files": (0x905E63B6, 0xC1BF, 0x494E, (0xB2, 0x9C, 0x65, 0xB7, 0x32, 0xD3, 0x2D, 0x21)),
    "program_files_x86": (0x7C5A40EF, 0xA0FB, 0x4BFC, (0x87, 0x4A, 0xC0, 0xF2, 0xE0, 0xB9, 0xFA, 0x8E)),
}
_LEGACY_FOLDER_IDS = {
    "local_app_data": 0x001C,
    "roaming_app_data": 0x001A,
    "program_files": 0x0026,
    "program_files_x86": 0x002A,
}
_EXPECTED_PUBLISHERS = {
    "Code.exe": "Microsoft Corporation",
    "wt.exe": "Microsoft Corporation",
    "chrome.exe": "Google LLC",
    "Spotify.exe": "Spotify AB",
    "explorer.exe": "Microsoft Windows",
}


class _Guid(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]

class DesktopApps:
    def __init__(self, aliases: dict[str, TargetAlias], *, profiles: dict[str, AppProfile] | None = None, launcher: Callable[[str, str | None], object], focuser: Callable[[str], object] | None = None) -> None:
        self._aliases = {name: self._canonical_target(target) for name, target in aliases.items()}
        self._profiles, self._launcher, self._focuser = dict(profiles or DEFAULT_PROFILES), launcher, focuser
    def open(self, app_id: str, target_alias: str | None = None) -> object:
        profile, target = self.validate_open(app_id, target_alias)
        argument = None if target is None else (profile.uri_template or "{target}").format(target=str(target.value).replace("\\", "/"))
        return self._launcher(profile.executable, argument)
    def validate_open(self, app_id: str, target_alias: str | None = None) -> tuple[AppProfile, TargetAlias | None]:
        profile, target = self._profile(app_id), (self._target(target_alias) if target_alias else None)
        if target is not None and target.kind != profile.target_kind: raise DesktopAppError(f"{profile.id} cannot open a {target.kind} alias")
        return profile, target
    def focus(self, app_id: str) -> object:
        profile = self.validate_focus(app_id)
        if not profile.supports_focus: raise DesktopAppError("app focus is unavailable")
        # Most named desktop applications are single-instance and focus their existing window
        # when invoked again. A platform-specific focuser can replace this behavior.
        if self._focuser is None: return self._launcher(profile.executable, None)
        return self._focuser(profile.id)
    def validate_focus(self, app_id: str) -> AppProfile:
        profile = self._profile(app_id)
        if not profile.supports_focus: raise DesktopAppError("app focus is unavailable")
        return profile
    def target(self, target_alias: str) -> TargetAlias: return self._target(target_alias)
    def target_kinds(self) -> dict[str, str]:
        """Return a non-sensitive projection for the model/UI capability catalog."""
        return {name: target.kind for name, target in sorted(self._aliases.items())}
    def _profile(self, app_id):
        try: return self._profiles[app_id]
        except KeyError as exc: raise DesktopAppError("app is not allowlisted") from exc
    def _target(self, alias):
        if not alias or alias not in self._aliases: raise DesktopAppError("target is not an approved alias")
        return self._aliases[alias]
    @staticmethod
    def _canonical_target(target: TargetAlias) -> TargetAlias:
        if not isinstance(target, TargetAlias): raise DesktopAppError("desktop aliases must declare a target type")
        if target.kind == "path": return TargetAlias("path", str(Path(target.value).resolve()))
        if target.kind == "url":
            value, parsed = str(target.value), urlsplit(str(target.value))
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password: raise DesktopAppError("URL alias must be http/https without credentials")
            return TargetAlias("url", value)
        if target.kind == "spotify_uri":
            value = str(target.value)
            if not value.startswith("spotify:") or any(c.isspace() for c in value) or len(value) > 500: raise DesktopAppError("Spotify alias must be a bounded spotify URI")
            return TargetAlias("spotify_uri", value)
        raise DesktopAppError("unsupported desktop target type")


def open_profile(app_id: str, url: str | None = None) -> object:
    aliases = {} if url is None else {"target": TargetAlias("url", url)}
    apps = DesktopApps(aliases, profiles=DEFAULT_PROFILES, launcher=native_launcher)
    return apps.open(app_id, "target" if url is not None else None)


def focus_profile(app_id: str) -> object:
    apps = DesktopApps({}, profiles=DEFAULT_PROFILES, launcher=native_launcher)
    return apps.focus(app_id)


def native_launcher(executable: str, argument: str | None) -> dict[str, object]:
    """Launch one already-allowlisted executable without a shell or inherited stdin."""
    resolved = _resolve_executable(executable)
    command = [resolved] + ([argument] if argument is not None else [])
    child_env = {key: os.environ[key] for key in (
        "SystemRoot", "WINDIR", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "TEMP", "TMP")
        if key in os.environ}
    try:
        process = subprocess.Popen(command, shell=False, stdin=subprocess.DEVNULL,
                                   close_fds=True, env=child_env)
    except OSError as exc:
        raise DesktopAppError("configured desktop application is unavailable") from exc
    return {"application": executable, "pid": process.pid, "targeted": argument is not None}


def _resolve_executable(executable: str) -> str:
    if os.name != "nt" or executable not in _EXPECTED_PUBLISHERS:
        raise DesktopAppError("configured desktop application is unavailable at an approved location")
    # Resolve each approved root independently. Windows can deny one known-folder lookup while
    # another valid installation root remains available; that must not disable every candidate.
    candidate_specs = {
        "Code.exe": [
            ("local_app_data", "Programs/Microsoft VS Code/Code.exe"),
            ("program_files", "Microsoft VS Code/Code.exe"),
        ],
        "wt.exe": [("local_app_data", "Microsoft/WindowsApps/wt.exe")],
        "chrome.exe": [
            ("program_files", "Google/Chrome/Application/chrome.exe"),
            ("program_files_x86", "Google/Chrome/Application/chrome.exe"),
            ("local_app_data", "Google/Chrome/Application/chrome.exe"),
        ],
        "Spotify.exe": [("roaming_app_data", "Spotify/Spotify.exe")],
        "explorer.exe": [("windows", "explorer.exe")],
    }[executable]
    expected_publisher = _EXPECTED_PUBLISHERS[executable]
    for root_name, relative in candidate_specs:
        try:
            root = _windows_directory() if root_name == "windows" else _known_folder_path(root_name)
        except DesktopAppError:
            continue
        item = root / relative
        if not item.is_file():
            continue
        resolved = item.resolve()
        if _verify_authenticode_publisher(resolved, expected_publisher):
            return str(resolved)
    raise DesktopAppError("configured desktop application is unavailable at an approved location")


def _known_folder_path(name: str) -> Path:
    """Resolve a Windows known folder without trusting the caller's environment."""
    if os.name != "nt" or name not in _KNOWN_FOLDER_IDS:
        raise DesktopAppError("Windows known-folder resolution is unavailable")
    data1, data2, data3, data4 = _KNOWN_FOLDER_IDS[name]
    folder_id = _Guid(data1, data2, data3, (ctypes.c_ubyte * 8)(*data4))
    value = ctypes.c_wchar_p()
    result = ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None,
                                                        ctypes.byref(value))
    if result != 0 or not value.value:
        # SHGetKnownFolderPath can fail for an individual folder under a restricted process.
        # The older Shell API is still OS-owned and avoids trusting inherited environment paths.
        buffer = ctypes.create_unicode_buffer(32_768)
        legacy = ctypes.windll.shell32.SHGetFolderPathW(
            None, _LEGACY_FOLDER_IDS[name], None, 0, buffer)
        if legacy != 0 or not buffer.value:
            raise DesktopAppError("Windows known-folder resolution failed")
        return Path(buffer.value)
    try:
        return Path(value.value)
    finally:
        ctypes.windll.ole32.CoTaskMemFree(value)


def _windows_directory() -> Path:
    """Resolve the OS Windows directory through the fixed Win32 API."""
    if os.name != "nt":
        raise DesktopAppError("Windows directory resolution is unavailable")
    size = ctypes.windll.kernel32.GetWindowsDirectoryW(None, 0)
    if not size:
        raise DesktopAppError("Windows directory resolution failed")
    buffer = ctypes.create_unicode_buffer(size + 1)
    if not ctypes.windll.kernel32.GetWindowsDirectoryW(buffer, len(buffer)):
        raise DesktopAppError("Windows directory resolution failed")
    return Path(buffer.value)


def _verify_authenticode_publisher(path: Path, expected_publisher: str) -> bool:
    """Accept only a valid signature whose certificate CN is the expected publisher.

    PowerShell is resolved beneath the Windows directory obtained above; its script and arguments
    are fixed.  The candidate path is passed as an argument, never interpolated into script text.
    """
    if os.name != "nt" or expected_publisher not in _EXPECTED_PUBLISHERS.values():
        return False
    powershell = _windows_directory() / "System32/WindowsPowerShell/v1.0/powershell.exe"
    if not powershell.is_file():
        return False
    script = (
        "$ErrorActionPreference='Stop'; "
        "$candidate=[Environment]::GetEnvironmentVariable('ATLAS_SIGNATURE_PATH','Process'); "
        "$signature=Get-AuthenticodeSignature -LiteralPath $candidate; "
        "if($signature.Status.ToString() -ne 'Valid'){exit 3}; "
        "[Console]::Out.Write($signature.SignerCertificate.Subject)"
    )
    child_env = {key: os.environ[key] for key in ("SystemRoot", "WINDIR") if key in os.environ}
    child_env["ATLAS_SIGNATURE_PATH"] = str(path)
    try:
        result = subprocess.run(
            [str(powershell.resolve()), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
             script],
            check=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=10, env=child_env,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    subject = result.stdout.strip()
    common_name = re.search(r"(?:^|,\s*)CN=([^,]+)", subject, flags=re.IGNORECASE)
    return bool(common_name and common_name.group(1).strip().casefold() == expected_publisher.casefold())
