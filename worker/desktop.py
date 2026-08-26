"""Run Atlas as a native pywebview window with a child voice worker."""
from __future__ import annotations

import ctypes
from contextlib import suppress
from ctypes import wintypes
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
from queue import Empty, Queue
import secrets
import subprocess
import sys
from threading import Event, Lock, Thread
import time
from typing import Callable, TextIO
from urllib.parse import parse_qs, urlsplit, urlunsplit
from urllib.request import Request, urlopen

import webview

from worker import jobobject
__all__ = ["ATLAS", "confirm_close", "main", "read_ui_url", "run", "stop_child"]

ATLAS = Path(__file__).resolve().parents[1]
logger = logging.getLogger("atlas.desktop")
ACTIVE_JOB_STATES = {"queued", "launching", "running"}
ERROR_ALREADY_EXISTS = 183
MUTEX_NAME = "Local\\AtlasDesktop"
RESTART_EXIT_CODE = 21
RESTART_INTERVAL_S = 30.0
RESTART_BURST_WINDOW_S = 10.0 * 60.0
MAX_RESTARTS_PER_BURST = 3
APP_USER_MODEL_ID = "Atlas.Desktop"
ATLAS_ICON = ATLAS / "ui" / "atlas.ico"
GWL_STYLE = -16
IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE, WM_SETICON = 1, 0x0010, 0x0040, 0x0080
ICON_SMALL, ICON_BIG = 0, 1
SM_CXICON, SM_CYICON, SM_CXSMICON, SM_CYSMICON = 11, 12, 49, 50
WS_CAPTION, WS_THICKFRAME = 0x00C00000, 0x00040000
WS_MINIMIZEBOX, WS_MAXIMIZEBOX = 0x00020000, 0x00010000
WM_NCCALCSIZE, WM_NCHITTEST, WM_NCDESTROY = 0x0083, 0x0084, 0x0082
HTLEFT, HTRIGHT, HTTOP = 10, 11, 12
HTTOPLEFT, HTTOPRIGHT, HTBOTTOM = 13, 14, 15
HTBOTTOMLEFT, HTBOTTOMRIGHT = 16, 17
MONITOR_DEFAULTTONEAREST = 2
SWP_FRAMECHANGED, SWP_NOACTIVATE = 0x0020, 0x0010
SWP_NOMOVE, SWP_NOSIZE, SWP_NOZORDER = 0x0002, 0x0001, 0x0004
_native_window_hooks = {}
class _WindowRect(ctypes.Structure): _fields_ = [(name, ctypes.c_long) for name in ("left", "top", "right", "bottom")]
class _NCCalcSizeParams(ctypes.Structure): _fields_ = [("rects", _WindowRect * 3), ("window_pos", ctypes.c_void_p)]
class _MonitorInfo(ctypes.Structure): _fields_ = [("size", wintypes.DWORD), ("monitor", _WindowRect),
                                                  ("work", _WindowRect), ("flags", wintypes.DWORD)]
def _frameless_window_style(style: int) -> int:
    return (style | WS_THICKFRAME | WS_MAXIMIZEBOX | WS_MINIMIZEBOX) & ~WS_CAPTION
def _resize_hit_test(x: int, y: int, bounds, border: int = 8) -> int | None:
    left, top, right, bottom = bounds
    if not (left <= x < right and top <= y < bottom): return None
    horizontal = HTLEFT if x < left + border else HTRIGHT if x >= right - border else None
    vertical = HTTOP if y < top + border else HTBOTTOM if y >= bottom - border else None
    corners = {(HTLEFT, HTTOP): HTTOPLEFT, (HTRIGHT, HTTOP): HTTOPRIGHT,
               (HTLEFT, HTBOTTOM): HTBOTTOMLEFT, (HTRIGHT, HTBOTTOM): HTBOTTOMRIGHT}
    return corners.get((horizontal, vertical)) or horizontal or vertical
def _rect_tuple(rect): return rect.left, rect.top, rect.right, rect.bottom
def _client_rect(proposed, work_area, maximized: bool):
    if not maximized: return proposed
    left, top, right, bottom = proposed
    work_left, work_top, work_right, work_bottom = work_area
    return max(left, work_left), max(top, work_top), min(right, work_right), min(bottom, work_bottom)
def _window_long(call, args, set_last_error, get_last_error):
    set_last_error(0); result = call(*args); error = get_last_error()
    if result == 0 and error: raise OSError(error, "Windows window-style call failed")
    return result
def _exception_detail(error: Exception) -> str: return f"{type(error).__name__}: {' '.join(str(error).split())}"[:200]
def _user32_functions():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetWindowLongW.argtypes, user32.GetWindowLongW.restype = [wintypes.HWND, ctypes.c_int], ctypes.c_long
    user32.SetWindowLongW.argtypes, user32.SetWindowLongW.restype = [wintypes.HWND, ctypes.c_int, ctypes.c_long], ctypes.c_long
    user32.SetWindowPos.argtypes, user32.SetWindowPos.restype = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT], wintypes.BOOL
    user32.GetWindowRect.argtypes, user32.GetWindowRect.restype = [wintypes.HWND, ctypes.POINTER(_WindowRect)], wintypes.BOOL
    user32.MonitorFromWindow.argtypes, user32.MonitorFromWindow.restype = [wintypes.HWND, wintypes.DWORD], wintypes.HANDLE
    user32.GetMonitorInfoW.argtypes, user32.GetMonitorInfoW.restype = [wintypes.HANDLE, ctypes.POINTER(_MonitorInfo)], wintypes.BOOL
    user32.GetSystemMetrics.argtypes, user32.GetSystemMetrics.restype = [ctypes.c_int], ctypes.c_int
    user32.LoadImageW.argtypes, user32.LoadImageW.restype = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT], wintypes.HANDLE
    user32.SendMessageW.argtypes, user32.SendMessageW.restype = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], wintypes.LPARAM
    return user32
def _set_app_user_model_id(*, shell32=None) -> None:
    try:
        if (shell32 or ctypes.windll.shell32).SetCurrentProcessExplicitAppUserModelID(
                APP_USER_MODEL_ID) not in (None, 0):
            raise OSError("SetCurrentProcessExplicitAppUserModelID failed")
    except Exception:
        logger.warning("could not set the Atlas Windows application identity")
def _set_window_icon(window, *, user32=None, get_last_error=None) -> None:
    try:
        native = window.native
        if getattr(native, "InvokeRequired", False):
            from System import Action
            native.Invoke(Action(lambda: _set_window_icon(
                window, user32=user32, get_last_error=get_last_error)))
            return
        hwnd = int(native.Handle.ToInt64())
        user32 = user32 or _user32_functions()
        dimensions = ((ICON_SMALL, SM_CXSMICON, SM_CYSMICON),
                      (ICON_BIG, SM_CXICON, SM_CYICON))
        for icon_size, width_metric, height_metric in dimensions:
            width, height = user32.GetSystemMetrics(width_metric), user32.GetSystemMetrics(height_metric)
            hicon = user32.LoadImageW(None, str(ATLAS_ICON), IMAGE_ICON, width, height,
                                      LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if not hicon:
                raise OSError((get_last_error or ctypes.get_last_error)(), "LoadImageW failed")
            user32.SendMessageW(hwnd, WM_SETICON, icon_size, hicon)
        logger.info("window icon set")
    except Exception as error:
        logger.warning("could not set the Atlas Windows window icon: %s", _exception_detail(error))
def _native_window_hook(native, user32):
    from System import IntPtr
    from System.Windows.Forms import FormWindowState, NativeWindow
    hwnd = int(native.Handle.ToInt64())
    class AtlasNativeWindow(NativeWindow):
        def WndProc(self, message):
            if message.Msg == WM_NCCALCSIZE:
                if native.WindowState == FormWindowState.Maximized:
                    info = _MonitorInfo(size=ctypes.sizeof(_MonitorInfo))
                    monitor = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
                    if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                        extended = bool(message.WParam.ToInt64())
                        rect_type = _NCCalcSizeParams if extended else _WindowRect
                        proposed = ctypes.cast(message.LParam.ToInt64(), ctypes.POINTER(rect_type)).contents
                        rect = proposed.rects[0] if extended else proposed
                        rect.left, rect.top, rect.right, rect.bottom = _client_rect(
                            _rect_tuple(rect), _rect_tuple(info.work), True)
                message.Result = IntPtr.Zero
                return
            if message.Msg == WM_NCHITTEST and native.WindowState != FormWindowState.Maximized:
                rect = _WindowRect()
                if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    packed = message.LParam.ToInt64()
                    x, y = ctypes.c_short(packed & 0xffff).value, ctypes.c_short((packed >> 16) & 0xffff).value
                    hit = _resize_hit_test(x, y, _rect_tuple(rect))
                    if hit is not None:
                        message.Result = IntPtr(hit)
                        return
            if message.Msg == WM_NCDESTROY:
                try:
                    super().WndProc(message)
                finally:
                    self.ReleaseHandle()
                    if _native_window_hooks.get(hwnd) is self: _native_window_hooks.pop(hwnd)
                return
            super().WndProc(message)
    hook = AtlasNativeWindow(); hook.AssignHandle(native.Handle)
    return hook
def _configure_native_window(window, *, user32=None, hook_factory=_native_window_hook,
                             set_last_error=None, get_last_error=None) -> None:
    hook = hwnd = None
    try:
        native = window.native
        if getattr(native, "InvokeRequired", False):
            from System import Action
            native.Invoke(Action(lambda: _configure_native_window(
                window, user32=user32, hook_factory=hook_factory,
                                                                  set_last_error=set_last_error,
                                                                  get_last_error=get_last_error)))
            return
        hwnd = int(native.Handle.ToInt64())
        if user32 is None:
            user32 = _user32_functions()
        set_error, get_error = set_last_error or ctypes.set_last_error, get_last_error or ctypes.get_last_error
        style = _frameless_window_style(_window_long(user32.GetWindowLongW, (hwnd, GWL_STYLE), set_error, get_error))
        _window_long(user32.SetWindowLongW, (hwnd, GWL_STYLE, style), set_error, get_error)
        hook = hook_factory(native, user32)
        flags = SWP_FRAMECHANGED | SWP_NOACTIVATE | SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER
        if not user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, flags):
            raise OSError("SetWindowPos failed")
        previous = _native_window_hooks.get(hwnd)
        if previous is not None and previous is not hook: previous.ReleaseHandle()
        _native_window_hooks[hwnd] = hook
        logger.info("native window configured style=%#010x", style & 0xffffffff)
    except Exception as error:
        if hook is not None and _native_window_hooks.get(hwnd) is not hook:
            with suppress(Exception): hook.ReleaseHandle()
        logger.warning("could not configure the Atlas frameless Windows window: %s", _exception_detail(error))
def _status_html(title: str, message: str, *, heading: str | None = None) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    body {{ background: #101319; color: #edf2f7; display: grid; font: 18px system-ui;
      margin: 0; min-height: 100vh; place-items: center; }}
    main {{ max-width: 36rem; padding: 3rem; text-align: center; }}
    h1 {{ font-size: 2rem; margin-bottom: .5rem; }}
    p {{ color: #aeb8c5; line-height: 1.5; }}
  </style>
</head>
<body><main><h1>{heading or title}</h1><p>{message}</p></main></body>
</html>"""
STOPPED_HTML = _status_html("Atlas stopped", "Close this window and open Atlas again to restart it.")
RECONNECTING_HTML = _status_html("Atlas reconnecting audio", "Atlas is following your new system audio device.", heading="reconnecting audio\u2026")
def _configure_file_logging(local_app_data=None):
    root = local_app_data if local_app_data is not None else os.environ.get("LOCALAPPDATA")
    if not root:
        return None
    try:
        path = Path(root) / "Atlas" / "logs" / "desktop.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=256 * 1024, backupCount=2, encoding="utf-8")
    except Exception:
        return None
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    handler._atlas_desktop_file_handler = True
    for existing in list(logger.handlers):
        if getattr(existing, "_atlas_desktop_file_handler", False):
            logger.removeHandler(existing)
            existing.close()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return handler
def _log_worker_marker(line: str, count: int) -> bool:
    marker = next((item for item in ("Traceback", "Error", "Exception")
                   if line.startswith(item)), None)
    if marker is None:
        return False
    logger.error("worker marker category=%s count=%s", marker.casefold(), count)
    return True
class WindowApi:
    """Expose native window controls to the frameless Atlas page."""

    def __init__(self) -> None:
        self._window = None
        self._lock = Lock()

    def minimize(self) -> None:
        if self._window is not None:
            self._window.minimize()

    def toggle_maximize(self) -> None:
        window = self._window
        if window is None: return
        with self._lock:
            native = getattr(window, "native", None)
            if native is None: return
            try:
                from System import Action
                def toggle():
                    states = type(native.WindowState)
                    native.WindowState = (states.Normal if native.WindowState == states.Maximized
                                          else states.Maximized)
                native.Invoke(Action(toggle))
            except Exception:
                logger.warning("could not toggle the Atlas Windows window state")

    def request_close(self) -> None:
        if self._window is not None:
            self._window.destroy()
def _kernel32_functions():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.GetLastError.argtypes = []
    kernel32.GetLastError.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32
def _close_handle(handle, *, close_handle=None) -> None:
    if not handle or os.name != "nt":
        return
    try:
        closer = close_handle or _kernel32_functions().CloseHandle
        closer(handle)
    except Exception:
        logger.warning("could not close a Windows launcher handle")
def _create_instance_mutex(
    *, platform: str = os.name, create_mutex=None, get_last_error=None,
):
    if platform != "nt":
        return None, False
    try:
        if create_mutex is None or get_last_error is None:
            kernel32 = _kernel32_functions()
            create_mutex = create_mutex or kernel32.CreateMutexW
            get_last_error = get_last_error or kernel32.GetLastError
        handle = create_mutex(None, False, MUTEX_NAME)
        if not handle:
            raise OSError("CreateMutexW failed")
        return handle, get_last_error() == ERROR_ALREADY_EXISTS
    except Exception:
        logger.warning("could not create the Atlas single-instance mutex")
        return None, False
def _show_already_running() -> None:
    message = "Atlas is already running"
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.MessageBoxW(None, message, "Atlas", 0x40)
    except Exception:
        logger.warning(message)
def _is_ui_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        fragment = parse_qs(parsed.fragment, keep_blank_values=True)
        return (
            parsed.scheme == "http"
            and parsed.hostname == "127.0.0.1"
            and parsed.username is None
            and parsed.password is None
            and parsed.port is not None
            and parsed.path == "/"
            and not parsed.query
            and set(fragment) == {"pair"}
            and len(fragment["pair"]) == 1
            and bool(fragment["pair"][0])
        )
    except ValueError:
        return False
def _parse_ui_url_line(line: str) -> str | None:
    if not line.startswith("ATLAS_UI "):
        return None
    value = line.removeprefix("ATLAS_UI ").strip()
    return value if _is_ui_url(value) else None
def read_ui_url(stream: TextIO) -> str | None:
    """Read until the worker emits a valid Atlas loopback bootstrap URL."""
    for line in stream:
        value = _parse_ui_url_line(line)
        if value is not None:
            return value
    return None
def _jobs_url(ui_url: str) -> str:
    parsed = urlsplit(ui_url)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port is None:
        raise ValueError("invalid Atlas UI URL")
    return urlunsplit((parsed.scheme, parsed.netloc, "/jobs", "", ""))
def _shutdown_url(ui_url: str) -> str:
    parsed = urlsplit(ui_url)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port is None:
        raise ValueError("invalid Atlas UI URL")
    return urlunsplit((parsed.scheme, parsed.netloc, "/shutdown", "", ""))
def _loopback_request(
    url: str, *, method: str, headers: dict, body: bytes | None, timeout: float,
    max_bytes: int, opener: Callable,
) -> bytes:
    request = Request(url, data=body, method=method, headers=headers)
    with opener(request, timeout=timeout) as response:
        payload = response.read(max_bytes + 1)
    return payload
def _active_job_titles(ui_url: str, *, opener: Callable = urlopen) -> list[str]:
    body = _loopback_request(
        _jobs_url(ui_url), method="GET", headers={"accept": "application/json"},
        body=None, timeout=2.0, max_bytes=65_536, opener=opener)
    if len(body) > 65_536:
        raise ValueError("Atlas jobs response is too large")
    payload = json.loads(body.decode("utf-8"))
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list):
        raise ValueError("invalid Atlas jobs response")
    titles = []
    for job in jobs:
        if not isinstance(job, dict) or job.get("status") not in ACTIVE_JOB_STATES:
            continue
        title = job.get("title")
        if isinstance(title, str) and title.strip():
            titles.append(" ".join(title.split())[:200])
    return titles
def confirm_close(window, ui_url: str, *,
                  jobs_reader: Callable[[str], list[str]] = _active_job_titles) -> bool:
    """Allow close immediately when idle, otherwise ask once with active job titles."""
    try:
        titles = jobs_reader(ui_url)
    except Exception:
        message = (
            "Atlas could not check whether jobs are running. "
            "Any running jobs will be cancelled.\n\nClose Atlas?"
        )
        return bool(window.create_confirmation_dialog("Close Atlas?", message))
    if not titles:
        return True
    job_lines = "\n".join(f"- {title}" for title in titles)
    message = (
        f"The following jobs are still running:\n\n{job_lines}\n\n"
        "These jobs will be cancelled.\n\nClose Atlas?"
    )
    return bool(window.create_confirmation_dialog("Close Atlas?", message))
def _confirm_window_close(proc, window, ui_url: str) -> bool:
    if proc.poll() is not None:
        return True
    return confirm_close(window, ui_url)
def _request_shutdown(
    ui_url: str,
    shutdown_token: str,
    *,
    opener: Callable = urlopen,
) -> None:
    _loopback_request(
        _shutdown_url(ui_url), method="POST",
        headers={"X-Atlas-Shutdown": shutdown_token}, body=b"",
        timeout=16.0, max_bytes=65_536, opener=opener)
def stop_child(
    proc, ui_url: str | None = None, shutdown_token: str | None = None, *,
    shutdown_request: Callable[[str, str], None] = _request_shutdown,
    killer: Callable = jobobject.kill_process_tree,
) -> None:
    """Request shutdown, then escalate through tree kill and force after bounded waits."""
    if proc.poll() is not None:
        return
    if ui_url and shutdown_token:
        logger.info("shutdown requested")
        try:
            shutdown_request(ui_url, shutdown_token)
        except Exception:
            logger.warning("the worker did not accept the graceful shutdown request")
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        logger.warning("shutdown escalated")
        with suppress(OSError):
            killer(proc.pid, check=False, force=False)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            logger.warning("forced kill")
            with suppress(OSError):
                killer(proc.pid, check=False, force=True)

def _worker_python() -> str:
    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe":
        return str(executable.with_name("python.exe"))
    return str(executable)

def _watch_child(
    proc,
    window,
    closing: Event,
    restart=None,
    *,
    clock: Callable[[], float] = time.monotonic,
    restart_interval_s: float = RESTART_INTERVAL_S,
    wait=None,
) -> None:
    """Watch children, deferring close restarts and stopping on the third burst."""
    current = proc
    last_restart_at = None
    restart_requests: list[float] = []
    wait_for_delay = wait or closing.wait
    while True:
        exit_code = current.wait()
        logger.info("child exit code=%s", exit_code)
        if closing.is_set():
            return
        if exit_code != RESTART_EXIT_CODE or restart is None:
            window.load_html(STOPPED_HTML)
            return
        logger.info("restart requested")
        now = clock()
        restart_requests = [
            requested_at
            for requested_at in restart_requests
            if now - requested_at < RESTART_BURST_WINDOW_S
        ]
        restart_requests.append(now)
        if len(restart_requests) >= MAX_RESTARTS_PER_BURST:
            logger.critical("restart burst-limit reached")
            window.load_html(STOPPED_HTML)
            return
        window.load_html(RECONNECTING_HTML)
        if last_restart_at is not None and now - last_restart_at < restart_interval_s:
            delay = restart_interval_s - (now - last_restart_at)
            logger.warning("restart deferred for %.1f seconds", delay)
            if wait_for_delay(delay):
                return
            last_restart_at += restart_interval_s
        else:
            last_restart_at = now
        try:
            replacement = restart(current)
        except Exception:
            logger.error("restart failed")
            replacement = None
        if replacement is None:
            window.load_html(STOPPED_HTML)
            return
        current = replacement

def _capture_ui_url(stream: TextIO, result: Queue[str | None]) -> None:
    found = False
    marker_lines = 0
    for line in stream:
        value = _parse_ui_url_line(line) if not found else None
        if value is not None:
            found = True
            result.put(value)
            continue
        if marker_lines < 200 and _log_worker_marker(line, marker_lines + 1):
            marker_lines += 1
    if not found:
        result.put(None)

def _on_window_shown(proc, window, closing, restart) -> None:
    try:
        shown = window.events.shown.wait(timeout=30)
    except Exception as error:
        logger.warning("could not configure the Atlas native window: %s", _exception_detail(error))
    else:
        if not shown or window.native is None:
            logger.warning(
                "could not configure the Atlas native window: "
                "native window unavailable after waiting 30 seconds")
        else:
            try:
                from System import Action
                def configure():
                    _set_window_icon(window)
                    _configure_native_window(window)
                window.native.Invoke(Action(configure))
            except Exception as error:
                detail = _exception_detail(error)
                logger.warning("could not set the Atlas Windows window icon: %s", detail)
                logger.warning("could not configure the Atlas frameless Windows window: %s", detail)
    _watch_child(proc, window, closing, restart)
def run(
    *,
    spawn: Callable = subprocess.Popen,
    window_factory: Callable = webview.create_window,
    start: Callable = webview.start,
    terminate: Callable = stop_child,
    create_mutex: Callable = _create_instance_mutex,
    show_already_running: Callable = _show_already_running,
    assign_job: Callable = jobobject.assign_process,
    close_handle: Callable = _close_handle,
    token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32),
    wait_url_timeout_s: float = 90,
) -> int:
    """Launch the voice worker, host its paired UI, and tie its life to the window."""
    mutex_handle, already_running = create_mutex()
    if already_running:
        show_already_running()
        close_handle(mutex_handle)
        return 1
    child = {"proc": None, "job_handle": None, "ui_url": None}
    child_lock = Lock()
    stop_once = None
    shutdown_token = token_factory()
    child_env = os.environ.copy()
    child_env["PYTHONUTF8"] = "1"
    child_env["ATLAS_SHUTDOWN_TOKEN"] = shutdown_token

    def _spawn_child():
        proc = spawn(
            [_worker_python(), "-m", "worker.app", "console"],
            cwd=str(ATLAS),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            env=child_env,
        )
        logger.info("spawn child pid=%s", proc.pid)
        handle = assign_job(proc)
        if proc.stdout is None:
            return proc, handle, None
        result: Queue[str | None] = Queue(maxsize=1)
        Thread(
            target=_capture_ui_url,
            args=(proc.stdout, result),
            daemon=True,
        ).start()
        return proc, handle, result

    def _wait_for_ui_url(result) -> str | None:
        if result is None:
            logger.info("ui url received=false")
            return None
        try:
            url = result.get(timeout=wait_url_timeout_s)
        except Empty:
            logger.warning("ui url wait timeout")
            url = None
        logger.info("ui url received=%s", str(url is not None).lower())
        return url

    try:
        proc, job_handle, url_result = _spawn_child()
        with child_lock:
            child.update({"proc": proc, "job_handle": job_handle, "ui_url": None})
        closing = Event()
        stop_lock = Lock()
        stopped = False

        def _stop_once() -> None:
            nonlocal stopped
            with stop_lock:
                if stopped:
                    return
                stopped = True
            closing.set()
            with child_lock:
                current_proc = child["proc"]
                current_url = child["ui_url"]
            if current_proc is not None:
                terminate(current_proc, current_url, shutdown_token)

        stop_once = _stop_once
        ui_url = _wait_for_ui_url(url_result)
        if ui_url is None:
            return 1
        with child_lock:
            child["ui_url"] = ui_url
        window_api = WindowApi()
        window = window_factory(
            "Atlas",
            ui_url,
            width=1100,
            height=760,
            min_size=(800, 600),
            frameless=True,
            easy_drag=False,
            js_api=window_api,
            resizable=True,
        )
        if window is None:
            return 1
        window_api._window = window
        logger.info("window created")

        def _confirm_close_current() -> bool:
            with child_lock:
                current_proc = child["proc"]
                current_url = child["ui_url"]
            if current_proc is None or current_url is None:
                return True
            return _confirm_window_close(current_proc, window, current_url)

        def _restart_child(_exited_proc):
            replacement, replacement_handle, replacement_result = _spawn_child()
            with child_lock:
                old_handle = child["job_handle"]
                child.update({
                    "proc": replacement,
                    "job_handle": replacement_handle,
                    "ui_url": None,
                })
            close_handle(old_handle)
            if closing.is_set():
                terminate(replacement, None, shutdown_token)
                return None
            replacement_url = _wait_for_ui_url(replacement_result)
            if replacement_url is None:
                terminate(replacement, None, shutdown_token)
                with child_lock:
                    child["job_handle"] = None
                close_handle(replacement_handle)
                return None
            with child_lock:
                child["ui_url"] = replacement_url
            window.load_url(replacement_url)
            return replacement

        window.events.closing += _confirm_close_current
        window.events.closed += _stop_once
        start(_on_window_shown, (proc, window, closing, _restart_child))
        return 0
    finally:
        if stop_once is not None:
            stop_once()
        else:
            with child_lock:
                current_proc = child["proc"]
                current_url = child["ui_url"]
            if current_proc is not None:
                terminate(current_proc, current_url, shutdown_token)
        with child_lock:
            current_handle = child["job_handle"]
        close_handle(current_handle)
        close_handle(mutex_handle)

def main() -> int:
    _configure_file_logging()
    _set_app_user_model_id()
    return run()

if __name__ == "__main__":
    sys.exit(main())
