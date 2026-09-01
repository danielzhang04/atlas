"""Bounded Win32 desktop control with host-side window resolution."""
from __future__ import annotations

from collections.abc import Iterable
import ctypes
from ctypes import wintypes
import ntpath
import os
import time
from typing import Any

__all__ = [
    "ALLOWED_CHORDS",
    "DELETE_CHORDS",
    "MEDIA_KEYS",
    "DesktopControlError",
    "click",
    "find_window_by_process_path",
    "focus_new_window",
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
    "visible_window_handles",
    "window_action",
    "windows_by_process_path",
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


def _prototype(library: Any, name: str, arguments: list[Any], result: Any) -> None:
    function = getattr(library, name)
    function.argtypes = arguments
    function.restype = result


def _native_user32() -> Any:
    if os.name != "nt":
        raise DesktopControlError("desktop control is available only on Windows")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
    enum_callback = callback_type(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _prototype(user32, "EnumWindows", [enum_callback, wintypes.LPARAM], wintypes.BOOL)
    _prototype(user32, "IsWindowVisible", [wintypes.HWND], wintypes.BOOL)
    _prototype(user32, "GetWindowTextLengthW", [wintypes.HWND], ctypes.c_int)
    _prototype(user32, "GetWindowTextW", [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int)
    _prototype(user32, "GetClassNameW", [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int)
    _prototype(user32, "GetWindowThreadProcessId", [wintypes.HWND, wintypes.LPDWORD], wintypes.DWORD)
    _prototype(user32, "GetWindowRect", [wintypes.HWND, ctypes.POINTER(_Rect)], wintypes.BOOL)
    _prototype(user32, "IsIconic", [wintypes.HWND], wintypes.BOOL)
    _prototype(user32, "IsZoomed", [wintypes.HWND], wintypes.BOOL)
    _prototype(user32, "GetForegroundWindow", [], wintypes.HWND)
    _prototype(user32, "SetForegroundWindow", [wintypes.HWND], wintypes.BOOL)
    _prototype(user32, "AttachThreadInput", [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL], wintypes.BOOL)
    _prototype(user32, "BringWindowToTop", [wintypes.HWND], wintypes.BOOL)
    _prototype(user32, "ShowWindow", [wintypes.HWND, ctypes.c_int], wintypes.BOOL)
    _prototype(user32, "SetWindowPos", [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ], wintypes.BOOL)
    _prototype(user32, "PostMessageW", [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ], wintypes.BOOL)
    _prototype(user32, "SystemParametersInfoW", [
        wintypes.UINT, wintypes.UINT, wintypes.LPVOID, wintypes.UINT,
    ], wintypes.BOOL)
    _prototype(user32, "SetCursorPos", [ctypes.c_int, ctypes.c_int], wintypes.BOOL)
    _prototype(user32, "SendInput", [wintypes.UINT, ctypes.POINTER(_Input), ctypes.c_int], wintypes.UINT)
    return user32


def _native_kernel32() -> Any:
    if os.name != "nt":
        raise DesktopControlError("desktop control is available only on Windows")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _prototype(kernel32, "OpenProcess", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE)
    _prototype(kernel32, "QueryFullProcessImageNameW", [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, wintypes.LPDWORD,
    ], wintypes.BOOL)
    _prototype(kernel32, "CloseHandle", [wintypes.HANDLE], wintypes.BOOL)
    _prototype(kernel32, "GetCurrentThreadId", [], wintypes.DWORD)
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
    """Focus a window the host resolved earlier, after proving it is still it.

    A stored record can be up to tools._LAST_OPENED_TTL_S (10 minutes) old,
    and an HWND is not a durable name: Windows RECYCLES handles, so ten
    minutes after Daniel's document window closed its number can belong to
    something else entirely. A dead handle already failed safe -- the focus
    call simply failed -- but a recycled one did not: it focused a stranger's
    window and reported the record's own stale title for it.

    So the handle is re-enumerated and matched back to the pid it was
    recorded with before anything is focused, and what gets focused (and
    reported) is the LIVE record. That way the identity Atlas speaks back is
    the window's identity now, not what it was called when it was stored.
    """
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
    live = next(
        (
            item for item in _window_records(user32=user32, kernel32=kernel32)
            if item["_handle"] == record["_handle"]
            and item["pid"] == record["pid"]
        ),
        None,
    )
    if live is None:
        # Closed, no longer visible, or the handle now belongs to another
        # process. All three are "this is not the window that was stored".
        raise DesktopControlError("window is no longer available")
    return _focus_record(live, user32=user32, kernel32=kernel32)


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
        if zone in {"left-half", "right-half"}:
            half_width = work_width // 2
            placement = (
                work.left + (half_width if zone == "right-half" else 0),
                work.top, half_width, work_height,
            )
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


def _emit_chord(
    chord: str,
    user32: Any,
    *,
    expected_hwnd: int | None = None,
    focus_error: str = "focused window changed; input not executed",
) -> dict:
    keys = [_vk(key) for key in chord.split("+")]
    inputs = [_keyboard_input(key) for key in keys]
    inputs.extend(_keyboard_input(key, flags=KEYEVENTF_KEYUP) for key in reversed(keys))
    _send(user32, inputs, expected_hwnd=expected_hwnd, focus_error=focus_error)
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
    return _emit_chord(
        normalized, user32 or _native_user32(),
        expected_hwnd=expected_hwnd,
        focus_error="focused window changed; delete not executed",
    )


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
    exclude_handles: Any = (),
    user32: Any | None = None,
    kernel32: Any | None = None,
) -> dict | None:
    """The first visible window of a process, optionally ignoring known ones.

    `exclude_handles` is a host-taken pre-spawn snapshot. It is what makes
    "the window this launch produced" distinguishable from "some window of
    that process that was already on screen" -- which matters most for
    explorer.exe, where every folder window shares one long-lived process, so
    an unfiltered lookup after opening a folder can hand back an unrelated
    Explorer window that happened to enumerate first.
    """
    found = windows_by_process_path(
        process_path, exclude_handles=exclude_handles,
        user32=user32, kernel32=kernel32,
    )
    return found[0] if found else None


def windows_by_process_path(
    process_path: str,
    *,
    exclude_handles: Any = (),
    user32: Any | None = None,
    kernel32: Any | None = None,
) -> list[dict]:
    """Every visible window of a process, so callers can see ambiguity.

    find_window_by_process_path answers "a window of this app", which is the
    right question when an app has one. It is the wrong question for
    explorer.exe, where every folder window shares one process: "the first
    one" is an arbitrary folder. Callers that must not guess ask for the
    whole list and refuse to act when it holds more than one.
    """
    if not isinstance(process_path, str) or not ntpath.isabs(process_path):
        raise DesktopControlError("process path must be absolute")
    excluded = _handle_set(exclude_handles)
    user32, kernel32 = _apis(user32, kernel32)
    wanted = _normalized_process_path(process_path)
    return [
        {
            "title": record["title"],
            "pid": record["pid"],
            "_handle": record["_handle"],
        }
        for record in _window_records(user32=user32, kernel32=kernel32)
        if record["_handle"] not in excluded
        and record["_process_path"]
        and _normalized_process_path(record["_process_path"]) == wanted
    ]


def _handle_set(handles: Any) -> frozenset[int]:
    """Host-supplied HWNDs only -- never a model argument (rule 12)."""
    if handles is None:
        return frozenset()
    if isinstance(handles, (str, bytes)) or not isinstance(handles, Iterable):
        raise DesktopControlError("invalid window handle set")
    collected = set()
    for handle in handles:
        if isinstance(handle, bool) or not isinstance(handle, int) or handle <= 0:
            raise DesktopControlError("invalid window handle set")
        collected.add(handle)
    return frozenset(collected)


def visible_window_handles(
    *,
    user32: Any | None = None,
    kernel32: Any | None = None,
) -> frozenset[int]:
    """Snapshot every visible top-level HWND, for a later new-window diff.

    Private by construction: the return value is a set of native handles, so
    it stays inside host code and is never serialized into a tool result.
    """
    user32, kernel32 = _apis(user32, kernel32)
    return frozenset(
        record["_handle"] for record in _window_records(user32=user32, kernel32=kernel32)
    )


def _process_path_set(paths: Any) -> frozenset[str]:
    """Host-resolved executable paths only -- never a model argument (rule 7)."""
    if isinstance(paths, (str, bytes)) or not isinstance(paths, Iterable):
        raise DesktopControlError("invalid expected process path set")
    collected = set()
    for path in paths:
        if not isinstance(path, str) or not ntpath.isabs(path):
            raise DesktopControlError("invalid expected process path set")
        collected.add(_normalized_process_path(path))
    return frozenset(collected)


def focus_new_window(
    before_hwnds: Any,
    timeout_s: float = 2.5,
    *,
    expected_process_paths: Any = None,
    interval_s: float = 0.15,
    user32: Any | None = None,
    kernel32: Any | None = None,
    clock: Any = time.monotonic,
    sleep: Any = time.sleep,
) -> dict | None:
    """Focus the ONE window this open produced, or do nothing at all.

    Opening something is asynchronous: os.startfile and a folder spawn both
    return long before a window exists, which is why "open" never used to end
    with the thing actually in front. This polls for the window that open
    produced and focuses it.

    "The window this open produced" needs BOTH halves, and new-since-snapshot
    is only the first. A new HWND is not evidence of causation: the desktop
    keeps producing windows Atlas did not ask for, and during the 2.5s poll a
    Teams call toast, a meeting reminder or an updater balloon is entirely
    ordinary. With only the freshness diff the toast was focused, the open
    reported focused: true about it, and the opened-record observer filed the
    TOAST's title and pid under the opened file's label -- so a later
    focus_last_opened raised a stranger's window and called it Daniel's
    document. `expected_process_paths` is the second half: the host resolves,
    ahead of the open, which executable is going to handle it (for a file,
    the registered association) and only windows belonging to that process
    are candidates.

    Identity is required, not preferred. When the caller cannot say which
    process to expect -- passes None, or an empty set -- this focuses NOTHING
    and returns None. The thing still opened; the host simply will not claim
    it put a window in front when it cannot tell which window that is.

    Exactly one, or nothing, applies on top of that. Two candidates means the
    host cannot tell which it caused -- a splash screen plus a document, or
    Daniel opening a second file of the same type at that moment -- and
    guessing would steal focus to an arbitrary window. Ambiguity does nothing
    and says so by returning None, which the caller reports as focused: false
    rather than swallowing. Foreign windows do not count toward that
    ambiguity: a toast arriving alongside the document must not be able to
    stop the document from being focused, only to fail to be focused itself.

    Returns the private window record (with `_handle`) so the host can
    remember what it focused; callers publish only host-shaped fields.
    """
    before = _handle_set(before_hwnds)
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or timeout_s < 0:
        raise DesktopControlError("invalid focus timeout")
    expected = (
        frozenset() if expected_process_paths is None
        else _process_path_set(expected_process_paths)
    )
    if not expected:
        # No identity to check against: nothing here can be attributed to
        # this open, so nothing is focused and nothing is recorded.
        return None
    user32, kernel32 = _apis(user32, kernel32)
    deadline = clock() + float(timeout_s)
    while True:
        records = _window_records(user32=user32, kernel32=kernel32)
        candidates = [
            record for record in records
            if record["_handle"] not in before
            and record["_process_path"]
            and _normalized_process_path(record["_process_path"]) in expected
        ]
        if len(candidates) > 1:
            # Ambiguous, and more polling only ever adds candidates.
            return None
        if candidates:
            record = candidates[0]
            _focus_record(record, user32=user32, kernel32=kernel32)
            return {
                "title": record["title"],
                "pid": record["pid"],
                "_handle": record["_handle"],
            }
        if clock() >= deadline:
            return None
        sleep(min(interval_s, max(0.0, deadline - clock())))
