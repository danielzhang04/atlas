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
from urllib.error import HTTPError
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
WM_NCMOUSEMOVE, WM_NCLBUTTONDOWN = 0x00A0, 0x00A1
WM_NCLBUTTONUP, WM_NCLBUTTONDBLCLK = 0x00A2, 0x00A3
WM_NCMOUSELEAVE = 0x02A2
WM_SIZE, WM_ACTIVATE, WM_KILLFOCUS, WM_CANCELMODE = 0x0005, 0x0006, 0x0008, 0x001F
# Anything that ends the press gesture without a release over the same button: the pointer leaving
# the frame, the window losing activation or focus, a mode-cancel (a modal dialog, an Alt+Tab, a
# system drag), or the window changing size or state underneath the press.
NC_PRESS_CANCEL_MESSAGES = (WM_NCMOUSELEAVE, WM_SIZE, WM_ACTIVATE, WM_KILLFOCUS, WM_CANCELMODE)
HTCAPTION, HTMINBUTTON, HTMAXBUTTON, HTCLOSE = 2, 8, 9, 20
HTLEFT, HTRIGHT, HTTOP = 10, 11, 12
HTTOPLEFT, HTTOPRIGHT, HTBOTTOM = 13, 14, 15
HTBOTTOMLEFT, HTBOTTOMRIGHT = 16, 17
WM_SYSCOMMAND = 0x0112
SC_MINIMIZE, SC_MAXIMIZE, SC_CLOSE, SC_RESTORE = 0xF020, 0xF030, 0xF060, 0xF120
TME_LEAVE, TME_NONCLIENT = 0x0002, 0x0010
# The custom title bar's geometry, in CSS pixels, mirroring ui/styles.css. Every constant below is
# pinned against that stylesheet by tests/test_desktop.py so the two can never drift apart.
USER_DEFAULT_SCREEN_DPI = 96
TITLEBAR_HEIGHT_CSS_PX = 40           # :root { --header: 40px } sizes the .shell title bar row
TITLEBAR_BUTTON_WIDTH_CSS_PX = 44     # .window-control { width: 44px }, three of them, flush right
TITLEBAR_PADDING_LEFT_CSS_PX = 13     # .topbar { padding-left: .8rem } at the 16px root, rounded up
TITLEBAR_BRAND_WIDTH_CSS_PX = 160     # .brand { max-width: 160px } - a clickable button, never caption
TITLEBAR_NAV_WIDTH_CSS_PX = 280       # .topbar nav { max-width: 280px }, centred in the padded row
TITLEBAR_BUTTON_CODES = (HTCLOSE, HTMAXBUTTON, HTMINBUTTON)  # outermost first, right to left
NC_HOVER_NAMES = {HTMINBUTTON: "minimize", HTMAXBUTTON: "maximize", HTCLOSE: "close"}
NC_HOVER_STATES = frozenset({"", *NC_HOVER_NAMES.values()})
NC_HOVER_SCRIPT = "window.__atlasNcHover && window.__atlasNcHover('%s')"
NC_HOVER_QUEUE_LIMIT = 32
MONITOR_DEFAULTTONEAREST = 2
SWP_FRAMECHANGED, SWP_NOACTIVATE = 0x0020, 0x0010
SWP_NOMOVE, SWP_NOSIZE, SWP_NOZORDER = 0x0002, 0x0001, 0x0004
WEBVIEW_OCCLUSION_FEATURE = "CalculateNativeWinOcclusion"
WEBVIEW_DISABLE_FEATURES = "--disable-features="
WEBVIEW_BROWSER_ARGUMENTS = "--disable-features=ElasticOverscroll"
_native_window_hooks = {}
class _WindowRect(ctypes.Structure): _fields_ = [(name, ctypes.c_long) for name in ("left", "top", "right", "bottom")]
class _NCCalcSizeParams(ctypes.Structure): _fields_ = [("rects", _WindowRect * 3), ("window_pos", ctypes.c_void_p)]
class _MonitorInfo(ctypes.Structure): _fields_ = [("size", wintypes.DWORD), ("monitor", _WindowRect),
                                                  ("work", _WindowRect), ("flags", wintypes.DWORD)]
class _TrackMouseEvent(ctypes.Structure): _fields_ = [("size", wintypes.DWORD), ("flags", wintypes.DWORD),
                                                      ("track", wintypes.HWND), ("hover_time", wintypes.DWORD)]
class _Point(ctypes.Structure): _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
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
def _scaled_px(css_px: int, dpi: int) -> int:
    """Convert a CSS pixel length from ui/styles.css into this window's physical pixels.

    One factor covers the whole conversion because all three coordinate spaces agree, whatever DPI
    awareness the process ends up with. `GetDpiForWindow` answers in whatever space that awareness
    virtualises screen coordinates into - which is the space a WM_NCHITTEST LPARAM and
    `GetWindowRect` both speak - and pywebview's own logical-to-physical scale
    (`winforms.BrowserView._scale`) is that same DPI over 96, which is the device scale factor
    WebView2 lays CSS pixels out at. Today pywebview calls `SetProcessDPIAware()`, so that is the
    system DPI on every monitor and Windows stretches the window on a differently scaled display;
    under per-monitor awareness the same three spaces still move together. Multiplying a CSS length
    by dpi/96 therefore lands in the units of the hit-test point either way. Rounding is per length,
    so a zone edge can sit up to one physical pixel off the browser's own rounding - far inside the
    margin between the excluded zones and the content they cover.
    """
    return round(css_px * dpi / USER_DEFAULT_SCREEN_DPI)
def _window_dpi(hwnd, user32) -> int:
    """The window's DPI, falling back to 96 when Windows will not answer."""
    getter = getattr(user32, "GetDpiForWindow", None)
    if getter is None: return USER_DEFAULT_SCREEN_DPI
    try: dpi = int(getter(hwnd))
    except Exception: return USER_DEFAULT_SCREEN_DPI
    return dpi if dpi > 0 else USER_DEFAULT_SCREEN_DPI
def _titlebar_button_spans(width: int, dpi: int) -> list[tuple[int, int, int]]:
    """(hit code, left, right) client-x spans of the three window controls, outermost first."""
    button = _scaled_px(TITLEBAR_BUTTON_WIDTH_CSS_PX, dpi)
    return [(code, width - button * (index + 1), width - button * index)
            for index, code in enumerate(TITLEBAR_BUTTON_CODES)]
def _client_bounds(hwnd, user32):
    """The client area in screen coordinates - the exact box ui/styles.css lays out inside.

    Asking Windows beats deriving it. While restored this equals the window rect, because the
    WM_NCCALCSIZE handler returns the proposed rect untouched; while maximized the same handler
    clamps the client to the monitor work area and the window rect spills past it. Reading the
    client rect directly is right in both states and stays right if that clamp ever changes.
    """
    rect, origin = _WindowRect(), _Point()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)): return None
    if not user32.ClientToScreen(hwnd, ctypes.byref(origin)): return None
    return origin.x, origin.y, origin.x + rect.right, origin.y + rect.bottom
def _titlebar_hit_test(x: int, y: int, bounds, dpi: int, *, buttons_only: bool = False) -> int | None:
    """Nonclient code for a point over the custom title bar, or None to leave it to the page.

    `x`, `y` and `bounds` are screen coordinates, `bounds` being the client area. The brand button
    and the centred nav stay page-owned: they fall through as HTCLIENT, where the existing pywebview
    drag region and click handlers still run. `buttons_only` drops the caption strip and keeps just
    the three control rects - what a maximized window answers, so the Snap Layouts flyout still
    appears over its restore button while dragging stays on the page's own path.
    """
    left, top, right, bottom = bounds
    if not (left <= x < right and top <= y < bottom): return None
    offset_x, offset_y, width = x - left, y - top, right - left
    if offset_y >= _scaled_px(TITLEBAR_HEIGHT_CSS_PX, dpi): return None
    for code, start, end in _titlebar_button_spans(width, dpi):
        if start <= offset_x < end: return code
    if buttons_only: return None
    if offset_x < _scaled_px(TITLEBAR_PADDING_LEFT_CSS_PX + TITLEBAR_BRAND_WIDTH_CSS_PX, dpi): return None
    # The nav sits in the centre column of a `1fr auto 1fr` grid inside the padded row, so its
    # centre is (padding + width) / 2; both sides are doubled to stay in exact integers.
    nav_centre_doubled = _scaled_px(TITLEBAR_PADDING_LEFT_CSS_PX, dpi) + width
    if abs(2 * offset_x - nav_centre_doubled) < _scaled_px(TITLEBAR_NAV_WIDTH_CSS_PX, dpi): return None
    return HTCAPTION
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
    user32.GetClientRect.argtypes, user32.GetClientRect.restype = [wintypes.HWND, ctypes.POINTER(_WindowRect)], wintypes.BOOL
    user32.ClientToScreen.argtypes, user32.ClientToScreen.restype = [wintypes.HWND, ctypes.POINTER(_Point)], wintypes.BOOL
    user32.MonitorFromWindow.argtypes, user32.MonitorFromWindow.restype = [wintypes.HWND, wintypes.DWORD], wintypes.HANDLE
    user32.GetMonitorInfoW.argtypes, user32.GetMonitorInfoW.restype = [wintypes.HANDLE, ctypes.POINTER(_MonitorInfo)], wintypes.BOOL
    user32.GetSystemMetrics.argtypes, user32.GetSystemMetrics.restype = [ctypes.c_int], ctypes.c_int
    user32.LoadImageW.argtypes, user32.LoadImageW.restype = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT], wintypes.HANDLE
    user32.SendMessageW.argtypes, user32.SendMessageW.restype = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], wintypes.LPARAM
    user32.PostMessageW.argtypes, user32.PostMessageW.restype = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], wintypes.BOOL
    # Both are optional: the title bar degrades to page-owned hover, or to 96 DPI, without them,
    # so a missing export must never take the icon and frameless setup down with it.
    with suppress(AttributeError):
        user32.TrackMouseEvent.argtypes, user32.TrackMouseEvent.restype = [ctypes.POINTER(_TrackMouseEvent)], wintypes.BOOL
    with suppress(AttributeError):  # GetDpiForWindow needs Windows 10 1607 or newer
        user32.GetDpiForWindow.argtypes, user32.GetDpiForWindow.restype = [wintypes.HWND], wintypes.UINT
    return user32
def _track_nonclient_mouse_leave(hwnd, user32) -> bool:
    """Ask for one WM_NCMOUSELEAVE so the page can drop a stale nonclient hover."""
    tracker = getattr(user32, "TrackMouseEvent", None)
    if tracker is None: return False
    request = _TrackMouseEvent(size=ctypes.sizeof(_TrackMouseEvent),
                               flags=TME_LEAVE | TME_NONCLIENT, track=hwnd)
    try: return bool(tracker(ctypes.byref(request)))
    except Exception: return False
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
def _disable_page_zoom(window) -> None:
    """Pin the page at 100% zoom, because every hit-test region assumes it.

    The title-bar regions are computed from CSS pixel constants scaled by the window DPI. Page zoom
    is a second, independent multiplier on top of that: ctrl+scroll would move the real buttons
    while the host kept hit-testing the unzoomed rectangles, so the controls would drift out from
    under the cursor. pywebview turns zoom on unconditionally
    (`webview.platforms.edgechromium.EdgeChrome.on_webview_ready` sets
    `IsZoomControlEnabled = True`) and exposes no setting for it, so this reaches the same supported
    WebView2 property afterwards and turns it back off. Atlas is an app shell, not a browser page.

    `CoreWebView2` does not exist until initialisation finishes, which can be after the window is
    shown, so an uninitialised control is handled by subscribing to the completion event instead.
    pywebview subscribes first, so its handler - and its `True` - always runs before this one. Every
    step is feature-detected: on any mismatch one bounded INFO line is logged, page zoom stays live,
    and the only consequence is the drift above, which the live checklist covers.
    """
    def skip(reason: str) -> None:
        logger.info("page zoom control left enabled: %s", reason)
    try:
        webview_control = getattr(getattr(window.native, "browser", None), "webview", None)
        if webview_control is None:
            return skip("control")
        def pin(*_args) -> None:
            try:
                core = webview_control.CoreWebView2
                core.Settings.IsZoomControlEnabled = False
                webview_control.ZoomFactor = 1.0
                logger.info("page zoom control disabled")
            except Exception as error:
                skip(_exception_detail(error))
        if webview_control.CoreWebView2 is None:
            webview_control.CoreWebView2InitializationCompleted += pin
        else:
            pin()
    except Exception as error:
        skip(_exception_detail(error))
class _NcHoverNotifier:
    """Mirror nonclient title-bar hover onto the page, off the UI thread.

    Once the window controls answer WM_NCHITTEST with their own codes, WebView2 never sees a mouse
    move over them and CSS `:hover` stops firing, so the host has to say so. This is host-to-page
    only and carries one of four fixed words, so no `js_api` surface is added (rule 9).

    The work cannot happen inline in WndProc: pywebview's `evaluate_js` blocks on a semaphore that
    is released from the UI thread's synchronisation context, so calling it from WndProc - which is
    the UI thread - would deadlock the message pump. One lazily started daemon thread does the call
    instead, and only when the hovered region actually changes, so idle mouse movement is free.
    """

    def __init__(self, evaluate_js: Callable[[str], object], *, thread_factory=Thread) -> None:
        self._evaluate_js = evaluate_js
        self._thread_factory = thread_factory
        self._queue: Queue[str | None] = Queue(maxsize=NC_HOVER_QUEUE_LIMIT)
        self._lock = Lock()
        self._thread = None
        self._state = ""
        self._warned = False

    def notify(self, state: str | None) -> None:
        """Queue a hover state, or None once the window is gone, to stop the thread.

        The queue is bounded because `evaluate_js` has no timeout: if the WebView2 is torn down
        while a call is in flight - a `load_html` for the stopped or reconnecting page - the pump
        can wait forever, and an unbounded queue would then grow for the rest of the session.
        Whether the drop comes from a full queue or a thread that will not start, `_state` is rolled
        back so the next real change is still sent rather than swallowed as a repeat.
        """
        if state is not None and state not in NC_HOVER_STATES:
            state = ""
        with self._lock:
            if state == self._state or (state is None and self._thread is None):
                return
            previous, self._state = self._state, state
            try:
                if self._thread is None:
                    # Starting a thread can fail outright - a thread limit, or an interpreter
                    # already shutting down - and this runs on the WndProc path, where an escaping
                    # exception would cross back into the CLR. Hover is cosmetic, so a failure
                    # rolls the state back and drops the update instead of propagating.
                    worker = self._thread_factory(target=self._pump, daemon=True)
                    worker.start()
                    self._thread = worker
                self._queue.put_nowait(state)
            except Exception:
                self._state = previous
                if not self._warned:
                    self._warned = True
                    logger.warning("could not mirror Atlas title bar hover onto the page")

    def _pump(self) -> None:
        while True:
            state = self._queue.get()
            if state is None:
                return
            with suppress(Exception):
                self._evaluate_js(NC_HOVER_SCRIPT % state)
def _native_window_hook(native, user32, notify_hover=None):
    from System import IntPtr
    from System.Windows.Forms import FormWindowState, NativeWindow
    hwnd = int(native.Handle.ToInt64())
    hover = notify_hover if callable(notify_hover) else lambda _state: None
    mouse = {"pressed": None}
    def run_title_bar_command(code: int) -> None:
        """Post the button's system command so it runs after WndProc returns, never nested.

        Acting inline would be re-entrant. Closing is the worst case: pywebview's `closing` event is
        built with `should_lock=True`, so its handlers run on the calling thread, which would put
        the job query's loopback HTTP request and a modal confirmation dialog inside this
        WM_NCLBUTTONUP frame, and would then deliver WM_NCDESTROY - releasing this hook's handle -
        underneath a live WndProc frame for the same instance. A state change is milder but still
        re-enters this WndProc through WM_NCCALCSIZE. Posting hands each button to DefWindowProc's
        own system command from a clean message loop, exactly as the standard frame buttons do,
        which also keeps the close confirmation and the maximized work-area clamp on their normal
        paths. `WindowApi` reaches the same commands from the page thread through `Invoke`.
        """
        if code == HTMINBUTTON:
            command = SC_MINIMIZE
        elif code == HTCLOSE:
            command = SC_CLOSE
        else:
            command = (SC_RESTORE if native.WindowState == FormWindowState.Maximized
                       else SC_MAXIMIZE)
        try:
            if not user32.PostMessageW(hwnd, WM_SYSCOMMAND, command, 0):
                raise OSError("PostMessageW failed")
        except Exception as error:
            logger.warning("could not run an Atlas title bar window command: %s", _exception_detail(error))
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
            if message.Msg == WM_NCHITTEST:
                maximized = native.WindowState == FormWindowState.Maximized
                packed = message.LParam.ToInt64()
                x, y = ctypes.c_short(packed & 0xffff).value, ctypes.c_short((packed >> 16) & 0xffff).value
                hit = None
                if not maximized:  # a maximized window has no resize border to offer
                    rect = _WindowRect()
                    if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                        hit = _resize_hit_test(x, y, _rect_tuple(rect))
                if hit is None:
                    bounds = _client_bounds(hwnd, user32)
                    if bounds is not None:
                        hit = _titlebar_hit_test(x, y, bounds, _window_dpi(hwnd, user32),
                                                 buttons_only=maximized)
                if hit is not None:
                    message.Result = IntPtr(hit)
                    return
            if message.Msg == WM_NCMOUSEMOVE:
                hover(NC_HOVER_NAMES.get(message.WParam.ToInt64(), ""))
                # Re-armed on every move, not once per entry: anything that takes the mouse capture
                # cancels leave tracking, and DefWindowProc's caption move loop - the whole point of
                # this change - does exactly that. Requesting again is idempotent and cheap, whereas
                # a cancelled session would latch the last hover on until the next nonclient move.
                _track_nonclient_mouse_leave(hwnd, user32)
            elif message.Msg in NC_PRESS_CANCEL_MESSAGES:
                # The gesture can end without a release over the button, and a latched press would
                # otherwise fire on some unrelated later release.
                mouse["pressed"] = None
                if message.Msg == WM_NCMOUSELEAVE:
                    hover("")
            elif message.Msg == WM_NCLBUTTONDOWN:
                # DefWindowProc would track these against the standard frame buttons, which this
                # window does not have (WS_CAPTION is cleared), so it drives no SC_ command at all.
                # Swallow the press and act on the matching release instead, the way Windows does.
                code = message.WParam.ToInt64()
                if code in NC_HOVER_NAMES:
                    mouse["pressed"] = code
                    message.Result = IntPtr.Zero
                    return
            elif message.Msg == WM_NCLBUTTONDBLCLK:
                # The second click of a double-click arrives as DOWN, UP, DBLCLK, UP. Swallowing the
                # DBLCLK without latching makes the pair act once - two SC_CLOSE would otherwise
                # queue a second close behind the confirmation dialog. A double-click on the caption
                # is untouched and still reaches DefWindowProc's maximize toggle.
                if message.WParam.ToInt64() in NC_HOVER_NAMES:
                    message.Result = IntPtr.Zero
                    return
            elif message.Msg == WM_NCLBUTTONUP:
                code = message.WParam.ToInt64()
                pressed, mouse["pressed"] = mouse["pressed"], None
                if code in NC_HOVER_NAMES and code == pressed:
                    message.Result = IntPtr.Zero
                    hover("")
                    run_title_bar_command(code)
                    return
            if message.Msg == WM_NCDESTROY:
                try:
                    super().WndProc(message)
                finally:
                    self.ReleaseHandle()
                    hover(None)
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
        evaluate_js = getattr(window, "evaluate_js", None)
        hook = hook_factory(native, user32,
                            _NcHoverNotifier(evaluate_js).notify if callable(evaluate_js) else None)
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
KB_UNLOCK_SCRIPT = r"""(() => {
  const b64urlBytes = (value) => {
    const padded = value.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(value.length / 4) * 4, "=");
    return Uint8Array.from(atob(padded), (char) => char.charCodeAt(0));
  };
  const bytesB64url = (value) => {
    if (value === null || value === undefined) return null;
    let binary = "";
    new Uint8Array(value).forEach((byte) => { binary += String.fromCharCode(byte); });
    return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
  };
  (async () => {
    const optionsResponse = await fetch("/api/auth/assert/options", {method: "POST"});
    if (!optionsResponse.ok) throw new Error("unlock failed");
    const envelope = await optionsResponse.json();
    const publicKey = envelope.options;
    publicKey.challenge = b64urlBytes(publicKey.challenge);
    if (Array.isArray(publicKey.allowCredentials)) {
      publicKey.allowCredentials.forEach((item) => { item.id = b64urlBytes(item.id); });
    }
    const credential = await navigator.credentials.get({publicKey});
    const response = credential.response;
    const serialized = {
      id: credential.id,
      rawId: bytesB64url(credential.rawId),
      type: credential.type,
      response: {
        authenticatorData: bytesB64url(response.authenticatorData),
        clientDataJSON: bytesB64url(response.clientDataJSON),
        signature: bytesB64url(response.signature),
        userHandle: bytesB64url(response.userHandle),
      },
      clientExtensionResults: credential.getClientExtensionResults(),
    };
    const verifyResponse = await fetch("/api/auth/assert/verify", {
      method: "POST",
      headers: {"content-type": "application/json"},
      body: JSON.stringify({ceremonyId: envelope.ceremonyId, credential: serialized}),
    });
    if (!verifyResponse.ok) throw new Error("unlock failed");
    const session = await verifyResponse.json();
    if (
      !session || typeof session.token !== "string" || !session.token ||
      !(
        typeof session.expiresAt === "string" ||
        Number.isSafeInteger(session.expiresAt)
      )
    ) throw new Error("unlock failed");
    await window.pywebview.api.kb_session(session.token, session.expiresAt);
  })().catch(async () => {
    try { await window.pywebview.api.kb_session_failed(); } catch (_error) {}
  });
})();"""
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

    def __init__(self, unlock_kb: Callable[[], str] | None = None) -> None:
        self._window = None
        self._lock = Lock()
        self._unlock_kb = unlock_kb

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

    def unlock_kb(self) -> str:
        if self._unlock_kb is None:
            return "unlock cancelled"
        try:
            return self._unlock_kb()
        except Exception:
            return "unlock cancelled"


class KbSessionApi:
    """Receive one short-lived kb session from the dashboard-origin window."""

    def __init__(
        self,
        accept: Callable[[str, object], None],
        fail: Callable[[], None] = lambda: None,
    ) -> None:
        self._accept = accept
        self._fail = fail

    def kb_session(self, token: str, expires_at: object) -> None:
        self._accept(token, expires_at)

    def kb_session_failed(self) -> None:
        self._fail()


class KbUnlock:
    def __init__(
        self,
        origin: str,
        *,
        window_factory: Callable,
        session_sender: Callable[[str, object], None],
        context_probe: Callable[[str], str],
        timeout_s: float = 60.0,
    ) -> None:
        self._origin = origin.rstrip("/")
        self._window_factory = window_factory
        self._session_sender = session_sender
        self._context_probe = context_probe
        self._timeout_s = max(0.0, float(timeout_s))

    def unlock(self) -> str:
        try:
            mode = self._context_probe(self._origin)
        except Exception:
            return "unlock cancelled"
        if mode in {"legacy", "tailnet"}:
            return "kb has no login today - already usable"
        if mode != "win32-desktop":
            return "unlock cancelled"
        completed = Event()
        terminal_failure = Event()
        result: list[tuple[str, object]] = []

        def accept(token: str, expires_at: object) -> None:
            if (
                not result
                and isinstance(token, str)
                and bool(token)
                and isinstance(expires_at, (str, int))
                and not isinstance(expires_at, bool)
            ):
                result.append((token, expires_at))
            else:
                terminal_failure.set()
            completed.set()

        def fail() -> None:
            terminal_failure.set()
            completed.set()

        api = KbSessionApi(accept, fail)
        window = None
        try:
            window = self._window_factory(
                "Unlock kb",
                f"{self._origin}/",
                width=720,
                height=640,
                min_size=(560, 480),
                js_api=api,
                resizable=True,
            )
            if window is None:
                return "unlock cancelled"
            events = getattr(window, "events", None)
            if events is not None and getattr(events, "closed", None) is not None:
                window.events.closed += completed.set
            window.evaluate_js(KB_UNLOCK_SCRIPT)
            if not completed.wait(self._timeout_s) or not result:
                return "unlock failed" if terminal_failure.is_set() else "unlock cancelled"
            token, expires_at = result.pop()
            try:
                self._session_sender(token, expires_at)
            except Exception:
                return "unlock failed"
            return f"kb unlocked until {expires_at}"
        except Exception:
            return "unlock cancelled"
        finally:
            result.clear()
            if window is not None:
                with suppress(Exception):
                    window.destroy()
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
def _kb_session_url(ui_url: str) -> str:
    parsed = urlsplit(ui_url)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port is None:
        raise ValueError("invalid Atlas UI URL")
    return urlunsplit((parsed.scheme, parsed.netloc, "/kb/session", "", ""))
def _kb_config_url(ui_url: str) -> str:
    parsed = urlsplit(ui_url)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port is None:
        raise ValueError("invalid Atlas UI URL")
    return urlunsplit((parsed.scheme, parsed.netloc, "/kb/config", "", ""))
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
def _forward_kb_session(
    ui_url: str,
    shutdown_token: str,
    token: str,
    expires_at: object,
    *,
    opener: Callable = urlopen,
) -> None:
    parsed = urlsplit(ui_url)
    body = json.dumps(
        {"token": token, "expiresAt": expires_at},
        separators=(",", ":"),
    ).encode("utf-8")
    if len(body) > 8_192:
        raise ValueError("kb session payload is too large")
    response = _loopback_request(
        _kb_session_url(ui_url),
        method="POST",
        headers={
            "X-Atlas-Shutdown": shutdown_token,
            "Content-Type": "application/json",
            "Origin": urlunsplit((parsed.scheme, parsed.netloc, "", "", "")),
        },
        body=body,
        timeout=5.0,
        max_bytes=1_024,
        opener=opener,
    )
    payload = json.loads(response.decode("utf-8"))
    if payload != {"ok": True}:
        raise ValueError("kb session forwarding failed")
def _request_kb_origin(
    ui_url: str,
    shutdown_token: str,
    *,
    opener: Callable = urlopen,
) -> str | None:
    body = _loopback_request(
        _kb_config_url(ui_url),
        method="GET",
        headers={"X-Atlas-Shutdown": shutdown_token},
        body=None,
        timeout=3.0,
        max_bytes=4_096,
        opener=opener,
    )
    if len(body) > 4_096:
        raise ValueError("kb config response is too large")
    payload = json.loads(body.decode("utf-8"))
    origin = payload.get("origin") if isinstance(payload, dict) and payload.get("enabled") is True else None
    parsed = urlsplit(origin) if isinstance(origin, str) else None
    if (
        parsed is None
        or parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or (parsed.scheme == "http" and parsed.hostname != "127.0.0.1")
    ):
        return None
    return origin.rstrip("/")
def _probe_kb_auth_context(origin: str, *, opener: Callable = urlopen) -> str:
    request = Request(f"{origin.rstrip('/')}/api/auth/context", headers={"Accept": "application/json"})
    try:
        with opener(request, timeout=3.0) as response:
            body = response.read(4_097)
    except HTTPError as exc:
        if exc.code == 404:
            return "legacy"
        raise
    if len(body) > 4_096:
        raise ValueError("kb auth context is too large")
    payload = json.loads(body.decode("utf-8"))
    mode = payload.get("mode") if isinstance(payload, dict) else None
    return mode if mode in {"win32-desktop", "tailnet"} else "unknown"
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
                    _disable_page_zoom(window)
                window.native.Invoke(Action(configure))
            except Exception as error:
                detail = _exception_detail(error)
                logger.warning("could not set the Atlas Windows window icon: %s", detail)
                logger.warning("could not configure the Atlas frameless Windows window: %s", detail)
    _watch_child(proc, window, closing, restart)
def _merge_disable_features(arguments: str, feature: str) -> str | None:
    """Fold `feature` into one `--disable-features=` flag, or None when splitting would corrupt it.

    Chromium keeps only the last `--disable-features` argument, so every disabled feature has to
    travel in one comma-joined flag. A quoted argument cannot be split on whitespace, so a value
    carrying a double quote is refused rather than rewritten.
    """
    if '"' in arguments:
        return None
    values, others, position = [], [], None
    for part in arguments.split():
        if part.startswith(WEBVIEW_DISABLE_FEATURES):
            if position is None:
                position = len(others)
            for value in part[len(WEBVIEW_DISABLE_FEATURES):].split(","):
                if value and value not in values:
                    values.append(value)
        else:
            others.append(part)
    if feature not in values:
        values.append(feature)
    others.insert(len(others) if position is None else position,
                  WEBVIEW_DISABLE_FEATURES + ",".join(values))
    return " ".join(others)
def _patch_webview_occlusion() -> None:
    """Disable Windows native occlusion tracking for the WebView2 that renders the Atlas window.

    Targets pywebview 6.2.1, whose `webview.platforms.edgechromium.EdgeChrome.__init__` hardcodes
    `props.AdditionalBrowserArguments = '--disable-features=ElasticOverscroll'` and exposes no
    settings hook for extra arguments. Windows native occlusion tracking can wrongly treat the
    Atlas frameless custom-WndProc window as occluded and throttle its rendering, so
    `CalculateNativeWinOcclusion` is merged into that value: two separate `--disable-features`
    arguments do not combine in Chromium, the last one wins.

    The last line of that constructor calls `EnsureCoreWebView2Async`, which reads the argument
    string synchronously and copies it into the native environment options, so editing the
    creation properties after the constructor returns is a silent no-op. This instead rewrites the
    one string constant in the constructor's code object, which lands the merged value inside the
    constructor before that call, keeps the original signature, and adds no wrapper frame: a
    failure while building the browser still propagates exactly as it does today.

    Every step is feature-detected - the module, the class, its own Python constructor, and exactly
    one code constant equal to the known literal (CPython folds repeated equal literals into one
    constant, so every use of it moves together). On any mismatch, such as a future pywebview, one
    bounded INFO line is logged and the app behaves exactly as it does unpatched; nothing raises. A
    constructor already carrying the feature is left alone, so repeat calls are silent no-ops.

    Rule 11: importing this platform module costs 0.6-0.8s and about 41MB RSS (28 -> 69MB), and it
    grows `os.environ['Path']` by 245 characters with the WebView2 interop directories. That cost
    is moved, not added: `run` calls this immediately before `start`, which is where pywebview
    imports the same module anyway, after the child environment has been copied, so neither the
    already-running exit path nor any child process pays for it. Revert by deleting this function,
    `_merge_disable_features`, and the single call in `run`.
    """
    from importlib import import_module
    from types import FunctionType
    def skip(reason: str) -> None:
        logger.info("webview occlusion patch skipped: %s", reason)
    try:
        module = import_module("webview.platforms.edgechromium")
    except Exception as error:
        return skip(type(error).__name__)
    browser = getattr(module, "EdgeChrome", None)
    if not isinstance(browser, type):
        return skip("class")
    original = vars(browser).get("__init__")
    code = getattr(original, "__code__", None)
    if not isinstance(original, FunctionType) or code is None:
        return skip("constructor")
    constants = getattr(code, "co_consts", ())
    if any(isinstance(value, str) and WEBVIEW_OCCLUSION_FEATURE in value for value in constants):
        return
    if sum(1 for value in constants
           if isinstance(value, str) and value == WEBVIEW_BROWSER_ARGUMENTS) != 1:
        return skip("literal")
    merged = _merge_disable_features(WEBVIEW_BROWSER_ARGUMENTS, WEBVIEW_OCCLUSION_FEATURE)
    if merged is None:
        return skip("quoted")
    try:
        patched = FunctionType(
            code.replace(co_consts=tuple(
                merged if isinstance(value, str) and value == WEBVIEW_BROWSER_ARGUMENTS else value
                for value in constants)),
            original.__globals__, original.__name__, original.__defaults__, original.__closure__)
        patched.__kwdefaults__ = original.__kwdefaults__
        patched.__qualname__ = original.__qualname__
        patched.__doc__ = original.__doc__
        patched.__dict__.update(original.__dict__)
        browser.__init__ = patched
    except Exception as error:
        skip(type(error).__name__)
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
    patch_webview: Callable = _patch_webview_occlusion,
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
        def _send_kb_session(token: str, expires_at: object) -> None:
            with child_lock:
                current_url = child["ui_url"]
            if not isinstance(current_url, str):
                raise RuntimeError("Atlas worker is unavailable")
            _forward_kb_session(
                current_url,
                shutdown_token,
                token,
                expires_at,
            )

        def _unlock_kb() -> str:
            with child_lock:
                current_url = child["ui_url"]
            if not isinstance(current_url, str):
                return "unlock cancelled"
            try:
                kb_origin = _request_kb_origin(current_url, shutdown_token)
            except Exception:
                return "unlock cancelled"
            if kb_origin is None:
                return "unlock cancelled"
            return KbUnlock(
                kb_origin,
                window_factory=window_factory,
                session_sender=_send_kb_session,
                context_probe=_probe_kb_auth_context,
            ).unlock()

        window_api = WindowApi(_unlock_kb)
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
        patch_webview()
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
