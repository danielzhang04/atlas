"""Bounded Win32 desktop control with host-side window resolution."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import ntpath
import os
from typing import Any

__all__ = [
    "ALLOWED_CHORDS",
    "DELETE_CHORDS",
    "MEDIA_KEYS",
    "DesktopControlError",
    "click",
    "find_window_by_process_path",
    "focus_resolved_window",
    "focus_window",
    "focused_window_identity",
    "list_windows",
    "media_key",
    "normalize_chord",
    "press_delete",
    "press_keys",
    "resolve_window",
    "type_text",
    "window_action",
]

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SPI_GETWORKAREA = 0x0030
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SW_MINIMIZE = 6
SW_MAXIMIZE = 3
SW_RESTORE = 9
VK_BACK = 0x08
VK_TAB = 0x09
VK_RETURN = 0x0D
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_ESCAPE = 0x1B
VK_SPACE = 0x20
VK_PRIOR = 0x21
VK_NEXT = 0x22
VK_END = 0x23
VK_HOME = 0x24
VK_LEFT = 0x25
VK_UP = 0x26
VK_RIGHT = 0x27
VK_DOWN = 0x28
VK_DELETE = 0x2E
VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_PLAY_PAUSE = 0xB3
VK_VOLUME_MUTE = 0xAD
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF
WM_CLOSE = 0x0010

DELETE_CHORDS = frozenset({"delete", "ctrl+d", "ctrl+x", "shift+delete"})
ALLOWED_CHORDS = frozenset({
    "alt+tab",
    "backspace",
    "ctrl+a",
    "ctrl+c",
    "ctrl+f",
    "ctrl+l",
    "ctrl+p",
    "ctrl+s",
    "ctrl+t",
    "ctrl+v",
    "ctrl+w",
    "ctrl+y",
    "ctrl+z",
    "down",
    "end",
    "enter",
    "escape",
    "home",
    "left",
    "pagedown",
    "pageup",
    "right",
    "space",
    "tab",
    "up",
})

_MEDIA_KEY_CODES = {
    "play_pause": VK_MEDIA_PLAY_PAUSE,
    "next": VK_MEDIA_NEXT_TRACK,
    "previous": VK_MEDIA_PREV_TRACK,
    "volume_up": VK_VOLUME_UP,
    "volume_down": VK_VOLUME_DOWN,
    "mute": VK_VOLUME_MUTE,
}
MEDIA_KEYS = frozenset(_MEDIA_KEY_CODES)
_NAMED_KEYS = {
    "backspace": VK_BACK,
    "tab": VK_TAB,
    "enter": VK_RETURN,
    "shift": VK_SHIFT,
    "ctrl": VK_CONTROL,
    "alt": VK_MENU,
    "escape": VK_ESCAPE,
    "space": VK_SPACE,
    "pageup": VK_PRIOR,
    "pagedown": VK_NEXT,
    "end": VK_END,
    "home": VK_HOME,
    "left": VK_LEFT,
    "up": VK_UP,
    "right": VK_RIGHT,
    "down": VK_DOWN,
    "delete": VK_DELETE,
}


class DesktopControlError(RuntimeError):
    pass


class _Rect(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class _MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _KeyboardInput(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _HardwareInput(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _InputUnion(ctypes.Union):
    _fields_ = [("mi", _MouseInput), ("ki", _KeyboardInput), ("hi", _HardwareInput)]


class _Input(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = [("type", wintypes.DWORD), ("value", _InputUnion)]


def _native_user32() -> Any:
    if os.name != "nt":
        raise DesktopControlError("desktop control is available only on Windows")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
    enum_callback = callback_type(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [enum_callback, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, wintypes.LPDWORD]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(_Rect)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.IsZoomed.argtypes = [wintypes.HWND]
    user32.IsZoomed.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.PostMessageW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ]
    user32.PostMessageW.restype = wintypes.BOOL
    user32.SystemParametersInfoW.argtypes = [
        wintypes.UINT, wintypes.UINT, wintypes.LPVOID, wintypes.UINT,
    ]
    user32.SystemParametersInfoW.restype = wintypes.BOOL
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    user32.SetCursorPos.restype = wintypes.BOOL
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_Input), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    return user32


def _native_kernel32() -> Any:
    if os.name != "nt":
        raise DesktopControlError("desktop control is available only on Windows")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, wintypes.LPDWORD,
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetCurrentThreadId.argtypes = []
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    return kernel32


def _apis(user32: Any | None, kernel32: Any | None = None) -> tuple[Any, Any]:
    return user32 or _native_user32(), kernel32 or _native_kernel32()


def _window_text(user32: Any, hwnd: int) -> str:
    size = max(1, int(user32.GetWindowTextLengthW(hwnd)) + 1)
    buffer = ctypes.create_unicode_buffer(size)
    user32.GetWindowTextW(hwnd, buffer, size)
    return buffer.value


def _class_name(user32: Any, hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, len(buffer))
    return buffer.value


def _process_image_path(kernel32: Any, pid: int) -> str:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(32_768)
        size = wintypes.DWORD(len(buffer))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return ""
        return buffer.value
    finally:
        kernel32.CloseHandle(handle)


def _window_records(*, user32: Any, kernel32: Any) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    def collect(hwnd: int, _param: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        rect = _Rect()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        state = (
            "minimized" if user32.IsIconic(hwnd)
            else "maximized" if user32.IsZoomed(hwnd)
            else "normal"
        )
        process_path = _process_image_path(kernel32, int(pid.value))
        records.append({
            "_handle": int(hwnd),
            "_process_path": process_path,
            "title": _window_text(user32, hwnd),
            "class": _class_name(user32, hwnd),
            "pid": int(pid.value),
            "process": ntpath.basename(process_path),
            "bounds": {
                "x": int(rect.left),
                "y": int(rect.top),
                "width": int(rect.right - rect.left),
                "height": int(rect.bottom - rect.top),
            },
            "state": state,
        })
        return True

    callback_type = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
    callback = callback_type(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(collect)
    if not user32.EnumWindows(callback, 0):
        raise DesktopControlError("window inventory failed")
    return records


def _public(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if not key.startswith("_")}


def list_windows(
    *,
    limit: int = 40,
    user32: Any | None = None,
    kernel32: Any | None = None,
) -> dict[str, Any]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise DesktopControlError("window limit must be between 1 and 100")
    user32, kernel32 = _apis(user32, kernel32)
    records = _window_records(user32=user32, kernel32=kernel32)
    windows = [_public(record) for record in records[:limit]]
    return {
        "windows": windows,
        "total": len(records),
        "truncated": len(windows) < len(records),
    }


def _resolve_record(
    *,
    title: str | None,
    pid: int | None,
    user32: Any,
    kernel32: Any,
) -> dict[str, Any]:
    if (title is None) == (pid is None):
        raise DesktopControlError("provide exactly one window title or pid")
    records = _window_records(user32=user32, kernel32=kernel32)
    if pid is not None:
        matches = [record for record in records if record["pid"] == pid]
    else:
        if not isinstance(title, str) or not title.strip():
            raise DesktopControlError("invalid window title")
        wanted = title.strip().casefold()
        exact = [record for record in records if record["title"].casefold() == wanted]
        matches = exact or [record for record in records if wanted in record["title"].casefold()]
    if not matches:
        raise DesktopControlError("window not found")
    if len(matches) != 1:
        raise DesktopControlError("window target is ambiguous")
    return matches[0]


def resolve_window(
    *,
    title: str | None = None,
    pid: int | None = None,
    user32: Any | None = None,
    kernel32: Any | None = None,
) -> dict:
    user32, kernel32 = _apis(user32, kernel32)
    return _public(_resolve_record(
        title=title, pid=pid, user32=user32, kernel32=kernel32,
    ))


def focus_window(
    *,
    title: str | None = None,
    pid: int | None = None,
    user32: Any | None = None,
    kernel32: Any | None = None,
) -> dict:
    user32, kernel32 = _apis(user32, kernel32)
    record = _resolve_record(title=title, pid=pid, user32=user32, kernel32=kernel32)
    return _focus_record(record, user32=user32, kernel32=kernel32)


def focus_resolved_window(
    record: dict[str, Any],
    *,
    user32: Any | None = None,
    kernel32: Any | None = None,
) -> dict:
    user32, kernel32 = _apis(user32, kernel32)
    if (
        not isinstance(record, dict)
        or isinstance(record.get("_handle"), bool)
        or not isinstance(record.get("_handle"), int)
        or record["_handle"] <= 0
        or not isinstance(record.get("title"), str)
        or isinstance(record.get("pid"), bool)
        or not isinstance(record.get("pid"), int)
    ):
        raise DesktopControlError("invalid resolved window identity")
    return _focus_record(record, user32=user32, kernel32=kernel32)


def _focus_record(record: dict[str, Any], *, user32: Any, kernel32: Any) -> dict:
    hwnd = record["_handle"]
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    if not user32.SetForegroundWindow(hwnd):
        foreground = user32.GetForegroundWindow()
        foreground_pid = wintypes.DWORD()
        foreground_thread = user32.GetWindowThreadProcessId(
            foreground, ctypes.byref(foreground_pid),
        )
        current_thread = kernel32.GetCurrentThreadId()
        attached = bool(
            foreground_thread
            and user32.AttachThreadInput(current_thread, foreground_thread, True)
        )
        try:
            user32.BringWindowToTop(hwnd)
            if not user32.SetForegroundWindow(hwnd):
                raise DesktopControlError("window could not be focused")
        finally:
            if attached:
                user32.AttachThreadInput(current_thread, foreground_thread, False)
    if int(user32.GetForegroundWindow() or 0) != hwnd:
        raise DesktopControlError("window could not be focused")
    return {"focused": record["title"], "pid": record["pid"]}


def _work_area(user32: Any) -> _Rect:
    rect = _Rect()
    if not user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
        raise DesktopControlError("monitor work area is unavailable")
    return rect


def window_action(
    action: str,
    *,
    title: str | None = None,
    pid: int | None = None,
    x: int | None = None,
    y: int | None = None,
    width: int | None = None,
    height: int | None = None,
    user32: Any | None = None,
    kernel32: Any | None = None,
) -> dict:
    user32, kernel32 = _apis(user32, kernel32)
    record = _resolve_record(title=title, pid=pid, user32=user32, kernel32=kernel32)
    hwnd = record["_handle"]
    normalized = action.strip().casefold() if isinstance(action, str) else ""
    show_commands = {
        "minimize": SW_MINIMIZE,
        "maximize": SW_MAXIMIZE,
        "restore": SW_RESTORE,
    }
    if normalized in show_commands:
        user32.ShowWindow(hwnd, show_commands[normalized])
    elif normalized == "close":
        if not user32.PostMessageW(hwnd, WM_CLOSE, 0, 0):
            raise DesktopControlError("window close request failed")
    elif normalized == "resize":
        if not all(isinstance(value, int) and value > 0 for value in (width, height)):
            raise DesktopControlError("resize requires positive width and height")
        bounds = record["bounds"]
        if not user32.SetWindowPos(
            hwnd, 0, bounds["x"], bounds["y"], width, height,
            SWP_NOZORDER | SWP_NOACTIVATE,
        ):
            raise DesktopControlError("window resize failed")
    elif normalized == "move":
        if not all(isinstance(value, int) for value in (x, y)):
            raise DesktopControlError("move requires x and y")
        bounds = record["bounds"]
        if not user32.SetWindowPos(
            hwnd, 0, x, y, bounds["width"], bounds["height"],
            SWP_NOZORDER | SWP_NOACTIVATE,
        ):
            raise DesktopControlError("window move failed")
    elif normalized.startswith("move:"):
        zone = normalized.removeprefix("move:")
        work = _work_area(user32)
        work_width = int(work.right - work.left)
        work_height = int(work.bottom - work.top)
        if zone == "left-half":
            placement = (work.left, work.top, work_width // 2, work_height)
        elif zone == "right-half":
            placement = (work.left + work_width // 2, work.top, work_width // 2, work_height)
        elif zone == "center":
            bounds = record["bounds"]
            window_width = min(bounds["width"], work_width)
            window_height = min(bounds["height"], work_height)
            placement = (
                work.left + (work_width - window_width) // 2,
                work.top + (work_height - window_height) // 2,
                window_width,
                window_height,
            )
        else:
            raise DesktopControlError("unknown window zone")
        if not user32.SetWindowPos(
            hwnd, 0, *placement, SWP_NOZORDER | SWP_NOACTIVATE,
        ):
            raise DesktopControlError("window move failed")
    else:
        raise DesktopControlError("unknown window action")
    return {"action": normalized, "title": record["title"], "pid": record["pid"]}


def _keyboard_input(vk: int, *, scan: int = 0, flags: int = 0) -> _Input:
    return _Input(
        type=INPUT_KEYBOARD,
        ki=_KeyboardInput(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0),
    )


def _mouse_input(flags: int) -> _Input:
    return _Input(
        type=INPUT_MOUSE,
        mi=_MouseInput(dx=0, dy=0, mouseData=0, dwFlags=flags, time=0, dwExtraInfo=0),
    )


def _send(
    user32: Any,
    inputs: list[_Input],
    *,
    expected_hwnd: int | None = None,
    focus_error: str = "focused window changed; input not executed",
) -> None:
    array = (_Input * len(inputs))(*inputs)
    if expected_hwnd is not None and int(user32.GetForegroundWindow() or 0) != expected_hwnd:
        raise DesktopControlError(focus_error)
    if int(user32.SendInput(len(array), array, ctypes.sizeof(_Input))) != len(array):
        raise DesktopControlError("desktop input was not accepted")


def media_key(key: str, *, user32: Any | None = None) -> dict:
    user32 = user32 or _native_user32()
    normalized = key.strip().casefold() if isinstance(key, str) else ""
    try:
        vk = _MEDIA_KEY_CODES[normalized]
    except KeyError as exc:
        raise DesktopControlError("media key is not allowed") from exc
    _send(user32, [_keyboard_input(vk), _keyboard_input(vk, flags=KEYEVENTF_KEYUP)])
    return {"pressed": normalized}


def click(
    x: int,
    y: int,
    *,
    title: str | None = None,
    pid: int | None = None,
    user32: Any | None = None,
    kernel32: Any | None = None,
) -> dict:
    if not isinstance(x, int) or not isinstance(y, int):
        raise DesktopControlError("click coordinates must be integers")
    user32, kernel32 = _apis(user32, kernel32)
    expected_hwnd = None
    if title is not None or pid is not None:
        record = _resolve_record(title=title, pid=pid, user32=user32, kernel32=kernel32)
        if record["state"] == "minimized":
            raise DesktopControlError("window-relative click target is minimized")
        bounds = record["bounds"]
        if not (0 <= x < bounds["width"] and 0 <= y < bounds["height"]):
            raise DesktopControlError("window-relative click is outside the target")
        _focus_record(record, user32=user32, kernel32=kernel32)
        expected_hwnd = record["_handle"]
        x += record["bounds"]["x"]
        y += record["bounds"]["y"]
    if not user32.SetCursorPos(x, y):
        raise DesktopControlError("cursor could not be positioned")
    _send(
        user32,
        [_mouse_input(MOUSEEVENTF_LEFTDOWN), _mouse_input(MOUSEEVENTF_LEFTUP)],
        expected_hwnd=expected_hwnd,
        focus_error="window-relative click target is not foreground",
    )
    return {"clicked": {"x": x, "y": y}}


def type_text(text: str, *, user32: Any | None = None) -> dict:
    if not isinstance(text, str) or not text or len(text) > 4000:
        raise DesktopControlError("text must contain 1 to 4000 characters")
    user32 = user32 or _native_user32()
    inputs = []
    encoded = text.encode("utf-16-le")
    for index in range(0, len(encoded), 2):
        unit = int.from_bytes(encoded[index:index + 2], "little")
        inputs.extend([
            _keyboard_input(0, scan=unit, flags=KEYEVENTF_UNICODE),
            _keyboard_input(0, scan=unit, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
        ])
    _send(user32, inputs)
    return {"typed": len(text)}


def normalize_chord(chord: str) -> str:
    if not isinstance(chord, str):
        return ""
    return "+".join(part.strip().casefold() for part in chord.split("+"))


def _vk(key: str) -> int:
    if key in _NAMED_KEYS:
        return _NAMED_KEYS[key]
    if len(key) == 1 and key.isascii() and key.isalnum():
        return ord(key.upper())
    raise DesktopControlError("key chord is not allowed")


def _emit_chord(chord: str, user32: Any) -> dict:
    keys = [_vk(key) for key in chord.split("+")]
    inputs = [_keyboard_input(key) for key in keys]
    inputs.extend(_keyboard_input(key, flags=KEYEVENTF_KEYUP) for key in reversed(keys))
    _send(user32, inputs)
    return {"pressed": chord}


def press_keys(chord: str, *, user32: Any | None = None) -> dict:
    normalized = normalize_chord(chord)
    if normalized not in ALLOWED_CHORDS:
        raise DesktopControlError("key chord is not allowed")
    return _emit_chord(normalized, user32 or _native_user32())


def press_delete(
    chord: str,
    *,
    expected_hwnd: int,
    user32: Any | None = None,
) -> dict:
    normalized = normalize_chord(chord)
    if normalized not in DELETE_CHORDS:
        raise DesktopControlError("delete chord is not allowed")
    if isinstance(expected_hwnd, bool) or not isinstance(expected_hwnd, int) or expected_hwnd <= 0:
        raise DesktopControlError("invalid pending window identity")
    user32 = user32 or _native_user32()
    keys = [_vk(key) for key in normalized.split("+")]
    inputs = [_keyboard_input(key) for key in keys]
    inputs.extend(_keyboard_input(key, flags=KEYEVENTF_KEYUP) for key in reversed(keys))
    _send(
        user32,
        inputs,
        expected_hwnd=expected_hwnd,
        focus_error="focused window changed; delete not executed",
    )
    return {"pressed": normalized}


def focused_window_identity(
    *,
    user32: Any | None = None,
    kernel32: Any | None = None,
) -> dict[str, Any]:
    user32, _kernel32 = _apis(user32, kernel32)
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        raise DesktopControlError("no foreground window")
    title = _window_text(user32, hwnd)
    if not title:
        raise DesktopControlError("foreground window has no title")
    if len(title) > 512:
        raise DesktopControlError("foreground window title is too long")
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return {"title": title, "pid": int(pid.value), "_handle": int(hwnd)}


def _normalized_process_path(path: str) -> str:
    normalized = ntpath.normpath(path)
    if normalized.startswith("\\\\?\\"):
        normalized = normalized[4:]
    return ntpath.normcase(normalized)


def find_window_by_process_path(
    process_path: str,
    *,
    user32: Any | None = None,
    kernel32: Any | None = None,
) -> dict | None:
    if not isinstance(process_path, str) or not ntpath.isabs(process_path):
        raise DesktopControlError("process path must be absolute")
    user32, kernel32 = _apis(user32, kernel32)
    wanted = _normalized_process_path(process_path)
    for record in _window_records(user32=user32, kernel32=kernel32):
        if record["_process_path"] and _normalized_process_path(record["_process_path"]) == wanted:
            return {
                "title": record["title"],
                "pid": record["pid"],
                "_handle": record["_handle"],
            }
    return None
