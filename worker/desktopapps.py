"""Open or focus signed, allowlisted desktop application profiles."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import ctypes
from datetime import datetime, timezone
import importlib
import os
from pathlib import Path
import re
import subprocess
import threading
from typing import Any
from urllib.parse import urlsplit

from .statusdetail import render_status_detail

__all__ = [
    "AppProfile",
    "DEFAULT_PROFILES",
    "DesktopAppError",
    "DesktopApps",
    "StatusSnapshot",
    "close_profile",
    "focus_profile",
    "known_folder_path",
    "native_launcher",
    "open_profile",
    "status",
]


class DesktopAppError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AppProfile:
    id: str
    executable: str
    close_executable: str | None = None


DEFAULT_PROFILES = {
    "vscode": AppProfile("vscode", "Code.exe"),
    "wt": AppProfile("wt", "wt.exe", "WindowsTerminal.exe"),
    "chrome": AppProfile("chrome", "chrome.exe"),
    "notepad": AppProfile("notepad", "notepad.exe"),
    "spotify": AppProfile("spotify", "Spotify.exe"),
}

# These identifiers are resolved through SHGetKnownFolderPath instead of inherited
# environment variables. The latter cannot establish a trusted executable root.
_KNOWN_FOLDER_IDS = {
    "desktop": (
        0xB4BFCC3A,
        0xDB2C,
        0x424C,
        (0xB0, 0x29, 0x7F, 0xE9, 0x9A, 0x87, 0xC6, 0x41),
    ),
    "documents": (
        0xFDD39AD0,
        0x238F,
        0x46AF,
        (0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7),
    ),
    "downloads": (
        0x374DE290,
        0x123F,
        0x4565,
        (0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B),
    ),
    "local_app_data": (
        0xF1B32785,
        0x6FBA,
        0x4FCF,
        (0x9D, 0x55, 0x7B, 0x8E, 0x7F, 0x15, 0x70, 0x91),
    ),
    "program_files": (
        0x905E63B6,
        0xC1BF,
        0x494E,
        (0xB2, 0x9C, 0x65, 0xB7, 0x32, 0xD3, 0x2D, 0x21),
    ),
    "program_files_x86": (
        0x7C5A40EF,
        0xA0FB,
        0x4BFC,
        (0x87, 0x4A, 0xC0, 0xF2, 0xE0, 0xB9, 0xFA, 0x8E),
    ),
}
_LEGACY_FOLDER_IDS = {
    "local_app_data": 0x001C,
    "program_files": 0x0026,
    "program_files_x86": 0x002A,
}
_EXPECTED_PUBLISHERS = {
    "Code.exe": "Microsoft Corporation",
    "wt.exe": "Microsoft Corporation",
    "WindowsTerminal.exe": "Microsoft Corporation",
    "chrome.exe": "Google LLC",
    "notepad.exe": "Microsoft Windows",
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
    def __init__(
        self,
        *,
        profiles: dict[str, AppProfile] | None = None,
        launcher: Callable[[str, str | None], object],
        focuser: Callable[[str], object] | None = None,
    ) -> None:
        self._profiles = dict(profiles or DEFAULT_PROFILES)
        self._launcher = launcher
        self._focuser = focuser

    def open(self, app_id: str, url: str | None = None) -> object:
        profile = self._profile(app_id)
        if url is not None:
            if profile.id != "chrome" or not _direct_https(url):
                raise DesktopAppError("only Chrome can open an HTTPS URL")
        return self._launcher(profile.executable, url)

    def focus(self, app_id: str) -> object:
        profile = self._profile(app_id)
        if self._focuser is None:
            return self._launcher(profile.executable, None)
        return self._focuser(profile.id)

    def _profile(self, app_id: str) -> AppProfile:
        try:
            return self._profiles[app_id]
        except KeyError as exc:
            raise DesktopAppError("app is not allowlisted") from exc


def status(
    *,
    profiles: dict[str, AppProfile] | None = None,
    resolver: Callable[[str], str] | None = None,
) -> list[dict[str, str]]:
    """Report signed executable availability without exposing resolved paths."""
    configured = profiles or DEFAULT_PROFILES
    resolve = resolver or _resolve_executable
    result = []
    for name, profile in configured.items():
        try:
            resolve(profile.executable)
        except DesktopAppError:
            result.append({
                "name": name,
                "state": "not_configured",
                "detail": render_status_detail(
                    "not_configured", "signed_missing", executable=profile.executable,
                ),
            })
        except Exception:
            result.append({
                "name": name,
                "state": "error",
                "detail": render_status_detail("error", "profile_failed"),
            })
        else:
            result.append({
                "name": name,
                "state": "configured",
                "detail": render_status_detail("configured", "signed_found"),
            })
    return result


class StatusSnapshot:
    """Lazily cache signed desktop status and refresh it only off the poll path."""

    def __init__(
        self,
        *,
        profiles: dict[str, AppProfile] | None = None,
        resolver: Callable[[str], str] | None = None,
        clock: Callable[[], datetime | str] = lambda: datetime.now(timezone.utc),
        refresh_interval_s: float = 600.0,
    ) -> None:
        self._profiles = profiles
        self._resolver = resolver
        self._clock = clock
        self._refresh_interval_s = refresh_interval_s
        self._lock = threading.Lock()
        self._apps: list[dict[str, str]] | None = None
        self._as_of: str | None = None

    def refresh(self) -> dict[str, Any]:
        apps = status(profiles=self._profiles, resolver=self._resolver)
        now = self._clock()
        as_of = now.isoformat() if isinstance(now, datetime) else str(now)
        with self._lock:
            self._apps = apps
            self._as_of = as_of[:64]
            return self._copy_locked()

    def get(self) -> dict[str, Any]:
        with self._lock:
            if self._apps is not None:
                return self._copy_locked()
            apps = status(profiles=self._profiles, resolver=self._resolver)
            now = self._clock()
            self._apps = apps
            self._as_of = (now.isoformat() if isinstance(now, datetime) else str(now))[:64]
            return self._copy_locked()

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self._refresh_interval_s)
            await asyncio.to_thread(self.refresh)

    def _copy_locked(self) -> dict[str, Any]:
        return {
            "apps": [dict(item) for item in self._apps or ()],
            "as_of": self._as_of,
        }


def _direct_https(value: str) -> bool:
    parsed = urlsplit(value)
    return (
        parsed.scheme == "https"
        and bool(parsed.netloc)
        and parsed.username is None
        and parsed.password is None
    )


def open_profile(app_id: str, url: str | None = None) -> object:
    apps = DesktopApps(profiles=DEFAULT_PROFILES, launcher=native_launcher)
    return apps.open(app_id, url)


def focus_profile(app_id: str) -> object:
    apps = DesktopApps(profiles=DEFAULT_PROFILES, launcher=native_launcher)
    return apps.focus(app_id)


def _launch_folder(path: str) -> object:
    """Open a host-validated directory with the signed system Explorer profile."""
    return native_launcher("explorer.exe", path)


def close_profile(app_id: str, *, killer: Callable[..., object] = subprocess.run
                  ) -> dict[str, object]:
    """Request a graceful close for every window of an allowlisted app."""
    try:
        profile = DEFAULT_PROFILES[app_id]
    except KeyError as exc:
        raise DesktopAppError("app is not allowlisted") from exc
    try:
        result = killer(
            [
                _taskkill_executable(),
                "/IM",
                profile.close_executable or profile.executable,
            ],
            check=False,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, shell=False, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DesktopAppError("desktop application could not be closed") from exc
    if getattr(result, "returncode", 1) != 0:
        raise DesktopAppError("desktop application could not be closed")
    return {"application": app_id, "closed": True}


def _taskkill_executable() -> str:
    taskkill = _windows_directory() / "System32/taskkill.exe"
    if not taskkill.is_file():
        raise DesktopAppError("Windows task termination is unavailable")
    return str(taskkill.resolve())


def native_launcher(executable: str, url: str | None) -> dict[str, object]:
    """Launch one already-allowlisted executable without a shell or inherited stdin."""
    if executable not in _EXPECTED_PUBLISHERS:
        raise DesktopAppError("desktop application is not an approved profile")
    resolved = _resolve_executable(executable)
    existing = None
    if url is None:
        try:
            existing = _visible_profile_window(resolved)
        except _desktopcontrol().DesktopControlError:
            existing = None
    if existing is not None:
        _focus_profile_window(existing)
        return {
            "application": executable,
            "pid": existing["pid"],
            "focused": True,
            "existing": True,
        }
    command = [resolved] + ([url] if url is not None else [])
    child_env = {
        key: os.environ[key]
        for key in (
            "SystemRoot",
            "WINDIR",
            "USERPROFILE",
            "LOCALAPPDATA",
            "APPDATA",
            "TEMP",
            "TMP",
        )
        if key in os.environ
    }
    try:
        process = subprocess.Popen(
            command,
            shell=False,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            env=child_env,
        )
    except OSError as exc:
        raise DesktopAppError("configured desktop application is unavailable") from exc
    return {
        "application": executable,
        "pid": process.pid,
        "targeted": url is not None,
    }


def _desktopcontrol():
    return importlib.import_module("worker.desktopcontrol")


def _visible_profile_window(executable_path: str) -> dict | None:
    return _desktopcontrol().find_window_by_process_path(executable_path)


def _focus_profile_window(window: dict) -> object:
    return _desktopcontrol().focus_resolved_window(window)


def _resolve_executable(executable: str) -> str:
    if os.name != "nt" or executable not in _EXPECTED_PUBLISHERS:
        raise DesktopAppError(
            "configured desktop application is unavailable at an approved location"
        )
    candidate_specs = {
        "Code.exe": [
            ("local_app_data", "Programs/Microsoft VS Code/Code.exe"),
            ("program_files", "Microsoft VS Code/Code.exe"),
        ],
        "wt.exe": [("local_app_data", "Microsoft/WindowsApps/wt.exe")],
        "notepad.exe": [("windows", "System32/notepad.exe")],
        "Spotify.exe": [("local_app_data", "Microsoft/WindowsApps/Spotify.exe")],
        "explorer.exe": [("windows", "System32/explorer.exe")],
        "chrome.exe": [
            ("program_files", "Google/Chrome/Application/chrome.exe"),
            ("program_files_x86", "Google/Chrome/Application/chrome.exe"),
            ("local_app_data", "Google/Chrome/Application/chrome.exe"),
        ],
    }[executable]
    expected_publisher = _EXPECTED_PUBLISHERS[executable]
    for root_name, relative in candidate_specs:
        try:
            root = (
                _windows_directory()
                if root_name == "windows"
                else _known_folder_path(root_name)
            )
        except DesktopAppError:
            continue
        item = root / relative
        if not item.is_file():
            continue
        resolved = item.resolve()
        if _verify_authenticode_publisher(resolved, expected_publisher):
            return str(resolved)
    raise DesktopAppError(
        "configured desktop application is unavailable at an approved location"
    )


def known_folder_path(name: str) -> Path:
    """Resolve a Windows known folder without trusting the caller's environment."""
    normalized_name = name.casefold()
    if os.name != "nt" or normalized_name not in _KNOWN_FOLDER_IDS:
        raise DesktopAppError("Windows known-folder resolution is unavailable")
    data1, data2, data3, data4 = _KNOWN_FOLDER_IDS[normalized_name]
    folder_id = _Guid(
        data1,
        data2,
        data3,
        (ctypes.c_ubyte * 8)(*data4),
    )
    value = ctypes.c_wchar_p()
    result = ctypes.windll.shell32.SHGetKnownFolderPath(
        ctypes.byref(folder_id),
        0,
        None,
        ctypes.byref(value),
    )
    if result != 0 or not value.value:
        legacy_id = _LEGACY_FOLDER_IDS.get(normalized_name)
        if legacy_id is None:
            raise DesktopAppError("Windows known-folder resolution failed")
        buffer = ctypes.create_unicode_buffer(32_768)
        legacy = ctypes.windll.shell32.SHGetFolderPathW(
            None,
            legacy_id,
            None,
            0,
            buffer,
        )
        if legacy != 0 or not buffer.value:
            raise DesktopAppError("Windows known-folder resolution failed")
        return Path(buffer.value)
    try:
        return Path(value.value)
    finally:
        ctypes.windll.ole32.CoTaskMemFree(value)


def _known_folder_path(name: str) -> Path:
    """Keep the private desktop-app resolver seam used by existing callers."""
    return known_folder_path(name)


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
    """Accept only a valid signature from the expected certificate publisher."""
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
    child_env = {
        key: os.environ[key]
        for key in ("SystemRoot", "WINDIR")
        if key in os.environ
    }
    child_env["ATLAS_SIGNATURE_PATH"] = str(path)
    try:
        result = subprocess.run(
            [
                str(powershell.resolve()),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            env=child_env,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    subject = result.stdout.strip()
    common_name = re.search(
        r"(?:^|,\s*)CN=([^,]+)",
        subject,
        flags=re.IGNORECASE,
    )
    return bool(
        common_name
        and common_name.group(1).strip().casefold() == expected_publisher.casefold()
    )
