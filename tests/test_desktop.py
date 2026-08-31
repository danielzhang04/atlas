"""Native Atlas desktop launcher behavior without real processes or windows."""
from __future__ import annotations

from io import StringIO
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from queue import Queue
import subprocess
import sys
from threading import Event, Thread
from types import ModuleType, SimpleNamespace

import pytest

from worker import app, desktop
from worker.stateserver import PairingAuthorizer


class FakeEvent:
    def __init__(self) -> None:
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self):
        return [handler() for handler in self.handlers]


class FakeWindow:
    def __init__(self, *, confirmation=True) -> None:
        self.events = SimpleNamespace(closing=FakeEvent(), closed=FakeEvent())
        self.confirmation = confirmation
        self.dialogs = []
        self.loaded_html = []
        self.loaded_urls = []
        self.minimize_calls = 0
        self.destroy_calls = 0
        self.resize_calls = []
        self.move_calls = []
        self.x = 100
        self.y = 120
        self.width = 1100
        self.height = 760
        self.screen = None

    def create_confirmation_dialog(self, title, message):
        self.dialogs.append((title, message))
        return self.confirmation

    def load_html(self, html):
        self.loaded_html.append(html)

    def load_url(self, url):
        self.loaded_urls.append(url)

    def minimize(self):
        self.minimize_calls += 1

    def destroy(self):
        self.destroy_calls += 1
        if all(result is not False for result in self.events.closing.fire()):
            self.events.closed.fire()

    def resize(self, width, height):
        self.resize_calls.append((width, height))
        self.width = width
        self.height = height

    def move(self, x, y):
        self.move_calls.append((x, y))
        self.x = x
        self.y = y


class FakeProcess:
    def __init__(self, output="", *, pid=4321, running=True, exit_code=0) -> None:
        self.stdout = StringIO(output)
        self.pid = pid
        self._handle = 8765
        self.running = running
        self.exit_code = exit_code
        self.waits = []

    def poll(self):
        return None if self.running else self.exit_code

    def wait(self, timeout=None):
        self.waits.append(timeout)
        self.running = False
        return self.exit_code


class RecursiveAttributeGraph:
    def __dir__(self):
        return ["empty"]

    def __getattr__(self, name):
        if name == "empty":
            return RecursiveAttributeGraph()
        raise AttributeError(name)


def _pywebview_api_walk(obj, seen=None, visits=None):
    # pywebview reflects over every public API attribute and recursively walks
    # non-callable objects with __module__, so window handles must stay private.
    seen = set() if seen is None else seen
    visits = [] if visits is None else visits
    for name in dir(obj):
        if len(visits) >= 200:
            break
        if name.startswith("_"):
            continue
        visits.append(name)
        value = getattr(obj, name)
        if callable(value) or not hasattr(value, "__module__"):
            continue
        if id(value) in seen:
            continue
        seen.add(id(value))
        _pywebview_api_walk(value, seen, visits)
    return len(visits)


@pytest.fixture
def desktop_log():
    output = StringIO()
    handler = logging.StreamHandler(output)
    old_level = desktop.logger.level
    desktop.logger.addHandler(handler)
    desktop.logger.setLevel(logging.INFO)
    try:
        yield output
    finally:
        desktop.logger.removeHandler(handler)
        desktop.logger.setLevel(old_level)


def test_window_api_exposes_only_methods():
    api = desktop.WindowApi()
    api._window = RecursiveAttributeGraph()

    assert all(
        callable(getattr(api, name))
        for name in dir(api)
        if not name.startswith("_")
    )


def test_pywebview_injection_walk_terminates():
    api = desktop.WindowApi()
    api._window = RecursiveAttributeGraph()

    assert _pywebview_api_walk(api) < 200


def test_kb_session_api_exposes_only_methods_and_reflection_walk_terminates():
    api = desktop.KbSessionApi(lambda _token, _expires_at: None)

    assert all(
        callable(getattr(api, name))
        for name in dir(api)
        if not name.startswith("_")
    )
    assert _pywebview_api_walk(api) < 200


def test_kb_unlock_window_forwards_session_and_closes_without_logging(desktop_log):
    sent = []
    windows = []
    bearer = "private-operator-bearer"

    class AuthWindow:
        def __init__(self, api):
            self.api = api
            self.scripts = []
            self.destroy_calls = 0

        def evaluate_js(self, script):
            self.scripts.append(script)
            self.api.kb_session(bearer, "2099-01-01T00:00:00Z")

        def destroy(self):
            self.destroy_calls += 1

    def window_factory(title, url, **kwargs):
        assert title == "Unlock kb"
        assert url == "http://127.0.0.1:5317/"
        window = AuthWindow(kwargs["js_api"])
        windows.append(window)
        return window

    unlock = desktop.KbUnlock(
        "http://127.0.0.1:5317",
        window_factory=window_factory,
        session_sender=lambda token, expires_at: sent.append((token, expires_at)),
        context_probe=lambda _origin: "win32-desktop",
    )

    result = unlock.unlock()

    assert result == "kb unlocked until 2099-01-01T00:00:00Z"
    assert sent == [(bearer, "2099-01-01T00:00:00Z")]
    assert windows[0].destroy_calls == 1
    script = windows[0].scripts[0]
    assert "/api/auth/assert/options" in script
    assert "navigator.credentials.get" in script
    assert "/api/auth/assert/verify" in script
    assert "typeof session.token" in script
    assert "Number.isSafeInteger(session.expiresAt)" in script
    assert "kb_session" in script
    assert bearer not in script
    assert bearer not in desktop_log.getvalue()


def test_kb_unlock_timeout_and_legacy_daemon_are_content_free():
    windows = []
    sent = []

    class AuthWindow:
        def __init__(self):
            self.destroy_calls = 0

        def evaluate_js(self, _script):
            return None

        def destroy(self):
            self.destroy_calls += 1

    def window_factory(*_args, **_kwargs):
        window = AuthWindow()
        windows.append(window)
        return window

    timeout = desktop.KbUnlock(
        "http://127.0.0.1:5317",
        window_factory=window_factory,
        session_sender=lambda *args: sent.append(args),
        context_probe=lambda _origin: "win32-desktop",
        timeout_s=0,
    )
    assert timeout.unlock() == "unlock cancelled"
    assert windows[0].destroy_calls == 1
    assert sent == []

    legacy = desktop.KbUnlock(
        "http://127.0.0.1:5317",
        window_factory=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("legacy daemon must not open an auth window")
        ),
        session_sender=lambda *_args: None,
        context_probe=lambda _origin: "legacy",
    )
    assert legacy.unlock() == "kb has no login today - already usable"


def test_kb_unlock_invalid_verify_shape_and_forwarding_failure_are_terminal():
    class InvalidWindow:
        def __init__(self, api):
            self.api = api
            self.events = SimpleNamespace(closed=FakeEvent())

        def evaluate_js(self, _script):
            self.api.kb_session("private", 1.5)

        def destroy(self):
            return None

    invalid = desktop.KbUnlock(
        "http://127.0.0.1:5317",
        window_factory=lambda *_args, **kwargs: InvalidWindow(kwargs["js_api"]),
        session_sender=lambda *_args: None,
        context_probe=lambda _origin: "win32-desktop",
        timeout_s=0.05,
    )
    assert invalid.unlock() == "unlock failed"

    class ValidWindow(InvalidWindow):
        def evaluate_js(self, _script):
            self.api.kb_session("private", "2099-01-01T00:00:00Z")

    rejected = desktop.KbUnlock(
        "http://127.0.0.1:5317",
        window_factory=lambda *_args, **kwargs: ValidWindow(kwargs["js_api"]),
        session_sender=lambda *_args: (_ for _ in ()).throw(ValueError("rejected")),
        context_probe=lambda _origin: "win32-desktop",
    )
    assert rejected.unlock() == "unlock failed"


def test_kb_unlock_manual_close_cancels_without_waiting_for_timeout():
    created = Event()
    windows = []
    outcome = []

    class ClosingWindow:
        def __init__(self):
            self.events = SimpleNamespace(closed=FakeEvent())

        def evaluate_js(self, _script):
            created.set()

        def destroy(self):
            return None

    def factory(*_args, **_kwargs):
        window = ClosingWindow()
        windows.append(window)
        return window

    unlock = desktop.KbUnlock(
        "http://127.0.0.1:5317",
        window_factory=factory,
        session_sender=lambda *_args: None,
        context_probe=lambda _origin: "win32-desktop",
        timeout_s=0.5,
    )
    thread = Thread(target=lambda: outcome.append(unlock.unlock()))
    thread.start()
    assert created.wait(0.2)
    windows[0].events.closed.fire()
    thread.join(0.1)
    cancelled_immediately = not thread.is_alive()
    thread.join(1.0)

    assert cancelled_immediately is True
    assert outcome == ["unlock cancelled"]


def test_kb_unlock_utterances_route_as_a_reflex():
    from worker import router

    assert router.route("atlas, unlock kb", {}) == ("reflex", "unlock_kb")
    assert router.route("unlock the dashboard", {}) == ("reflex", "unlock_kb")


def test_kb_origin_comes_from_the_private_worker_channel():
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, limit):
            requests.append(("read", limit))
            return b'{"enabled":true,"origin":"http://127.0.0.1:5317"}'

    def opener(request, *, timeout):
        requests.append((request.full_url, dict(request.header_items()), timeout))
        return Response()

    origin = desktop._request_kb_origin(
        "http://127.0.0.1:4360/#pair=one-use",
        "launcher-token",
        opener=opener,
    )

    assert origin == "http://127.0.0.1:5317"
    url, headers, timeout = requests[0]
    assert url == "http://127.0.0.1:4360/kb/config"
    assert headers["X-atlas-shutdown"] == "launcher-token"
    assert timeout == 3.0
    assert requests[1] == ("read", 4_097)


def test_set_app_user_model_id_calls_shell_once_and_swallows_failure(desktop_log):
    calls = []
    shell32 = SimpleNamespace(
        SetCurrentProcessExplicitAppUserModelID=calls.append,
    )

    desktop._set_app_user_model_id(shell32=shell32)

    assert calls == ["Atlas.Desktop"]

    def fail(_app_id):
        raise OSError("shell unavailable")

    desktop._set_app_user_model_id(
        shell32=SimpleNamespace(SetCurrentProcessExplicitAppUserModelID=fail),
    )
    desktop._set_app_user_model_id(shell32=SimpleNamespace(
        SetCurrentProcessExplicitAppUserModelID=lambda _app_id: -1,
    ))

    assert desktop_log.getvalue().count(
        "could not set the Atlas Windows application identity") == 2


def test_set_window_icon_loads_and_sends_small_and_big_icons(desktop_log):
    calls = []
    native = SimpleNamespace(Handle=SimpleNamespace(ToInt64=lambda: 456))
    user32 = SimpleNamespace(
        GetSystemMetrics=lambda metric: calls.append(("metric", metric)) or metric + 1,
        LoadImageW=lambda *args: calls.append(("load", *args)) or 1000 + len(calls),
        SendMessageW=lambda *args: calls.append(("send", *args)),
    )

    desktop._set_window_icon(SimpleNamespace(native=native), user32=user32)

    loads = [call for call in calls if call[0] == "load"]
    sends = [call for call in calls if call[0] == "send"]
    assert [call[2] for call in loads] == [str(desktop.ATLAS_ICON)] * 2
    assert [call[3:] for call in loads] == [
        (desktop.IMAGE_ICON, desktop.SM_CXSMICON + 1, desktop.SM_CYSMICON + 1,
         desktop.LR_LOADFROMFILE | desktop.LR_DEFAULTSIZE),
        (desktop.IMAGE_ICON, desktop.SM_CXICON + 1, desktop.SM_CYICON + 1,
         desktop.LR_LOADFROMFILE | desktop.LR_DEFAULTSIZE),
    ]
    assert [(call[1], call[2], call[3]) for call in sends] == [
        (456, desktop.WM_SETICON, desktop.ICON_SMALL),
        (456, desktop.WM_SETICON, desktop.ICON_BIG),
    ]
    assert "window icon set" in desktop_log.getvalue()


def test_set_window_icon_logs_bounded_exception_detail(desktop_log):
    native = SimpleNamespace(Handle=SimpleNamespace(ToInt64=lambda: 456))
    user32 = SimpleNamespace(
        GetSystemMetrics=lambda _metric: 16,
        LoadImageW=lambda *_args: (_ for _ in ()).throw(
            RuntimeError("icon load exploded" + "x" * 300)),
    )

    desktop._set_window_icon(SimpleNamespace(native=native), user32=user32)

    message = desktop_log.getvalue()
    assert "RuntimeError: icon load exploded" in message
    assert "x" * 201 not in message


def test_configure_native_window_sets_style_before_refreshing_frame(desktop_log):
    desktop._native_window_hooks.clear()
    calls = []
    error = [99]
    handle = SimpleNamespace(ToInt64=lambda: 456)
    native = SimpleNamespace(Handle=handle)
    old_style = desktop.WS_CAPTION | 0x08000000
    user32 = SimpleNamespace(
        GetWindowLongW=lambda hwnd, index: calls.append(("get", hwnd, index)) or
        error.__setitem__(0, 5) or old_style,
        SetWindowLongW=lambda hwnd, index, style: calls.append(
            ("set", hwnd, index, style)) or error.__setitem__(0, 6) or old_style,
        SetWindowPos=lambda *args: calls.append(("position", *args)) or True,
    )
    hook = object()

    desktop._configure_native_window(
        SimpleNamespace(native=native),
        user32=user32,
        hook_factory=lambda form, functions: calls.append(
            ("hook", form, functions)) or hook,
        set_last_error=lambda value: error.__setitem__(0, value),
        get_last_error=lambda: error[0],
    )

    expected_style = desktop._frameless_window_style(old_style)
    assert calls[:3] == [
        ("get", 456, desktop.GWL_STYLE),
        ("set", 456, desktop.GWL_STYLE, expected_style),
        ("hook", native, user32),
    ]
    assert calls[3][0:7] == ("position", 456, None, 0, 0, 0, 0)
    assert calls[3][7] & desktop.SWP_FRAMECHANGED
    assert desktop._native_window_hooks.pop(456) is hook
    assert f"native window configured style={expected_style:#010x}" in desktop_log.getvalue()


def test_configure_native_window_invokes_style_and_hook_on_ui_thread(monkeypatch):
    desktop._native_window_hooks.clear()
    calls = []

    class Native:
        invoking = False
        Handle = SimpleNamespace(ToInt64=lambda: 456)

        @property
        def InvokeRequired(self):
            return not self.invoking

        def Invoke(self, action):
            calls.append("invoke")
            self.invoking = True
            try:
                action()
            finally:
                self.invoking = False

    native = Native()

    def on_ui_thread(name, result):
        def call(*_args):
            assert native.invoking
            calls.append(name)
            return result
        return call

    monkeypatch.setitem(sys.modules, "System", SimpleNamespace(Action=lambda action: action))
    user32 = SimpleNamespace(
        GetWindowLongW=on_ui_thread("get", desktop.WS_CAPTION),
        SetWindowLongW=on_ui_thread("set", desktop.WS_CAPTION),
        SetWindowPos=on_ui_thread("position", True),
    )
    hook = object()

    desktop._configure_native_window(
        SimpleNamespace(native=native), user32=user32,
        hook_factory=lambda *_args: on_ui_thread("hook", hook)(),
    )

    assert calls == ["invoke", "get", "set", "hook", "position"]
    assert desktop._native_window_hooks.pop(456) is hook


def test_configure_native_window_logs_assign_handle_exception_detail(desktop_log):
    user32 = SimpleNamespace(
        GetWindowLongW=lambda *_args: desktop.WS_CAPTION,
        SetWindowLongW=lambda *_args: desktop.WS_CAPTION,
        SetWindowPos=lambda *_args: True,
    )

    desktop._configure_native_window(
        SimpleNamespace(native=SimpleNamespace(
            Handle=SimpleNamespace(ToInt64=lambda: 456))),
        user32=user32,
        hook_factory=lambda *_args: (_ for _ in ()).throw(
            RuntimeError("AssignHandle failed live")),
    )

    assert "RuntimeError: AssignHandle failed live" in desktop_log.getvalue()


@pytest.mark.parametrize("failing_call", ["get", "set"])
def test_configure_native_window_aborts_when_window_style_call_sets_last_error(
        failing_call, desktop_log):
    calls = []
    error = [0]
    old_style = desktop.WS_CAPTION | 0x08000000

    def get_window_long(*_args):
        calls.append("get")
        if failing_call == "get":
            error[0] = 5
            return 0
        return old_style

    def set_window_long(*_args):
        calls.append("set")
        if failing_call == "set":
            error[0] = 5
            return 0
        return old_style

    user32 = SimpleNamespace(
        GetWindowLongW=get_window_long,
        SetWindowLongW=set_window_long,
        SetWindowPos=lambda *_args: calls.append("position") or True,
    )
    desktop._configure_native_window(
        SimpleNamespace(native=SimpleNamespace(
            Handle=SimpleNamespace(ToInt64=lambda: 456))),
        user32=user32,
        hook_factory=lambda *_args: calls.append("hook") or object(),
        set_last_error=lambda value: error.__setitem__(0, value),
        get_last_error=lambda: error[0],
    )

    assert calls == (["get"] if failing_call == "get" else ["get", "set"])
    assert "could not configure the Atlas frameless Windows window" in desktop_log.getvalue()


def test_frameless_window_style_enables_resize_and_system_commands():
    unrelated_style = 0x08000000
    style = desktop.WS_CAPTION | unrelated_style

    updated = desktop._frameless_window_style(style)

    assert updated & desktop.WS_THICKFRAME
    assert updated & desktop.WS_MAXIMIZEBOX
    assert updated & desktop.WS_MINIMIZEBOX
    assert updated & desktop.WS_CAPTION == 0
    assert updated & unrelated_style


@pytest.mark.parametrize(
    ("point", "expected"),
    [
        ((100, 200), desktop.HTTOPLEFT),
        ((899, 200), desktop.HTTOPRIGHT),
        ((100, 799), desktop.HTBOTTOMLEFT),
        ((899, 799), desktop.HTBOTTOMRIGHT),
        ((100, 500), desktop.HTLEFT),
        ((107, 500), desktop.HTLEFT),
        ((108, 500), None),
        ((891, 500), None),
        ((892, 500), desktop.HTRIGHT),
        ((899, 500), desktop.HTRIGHT),
        ((500, 200), desktop.HTTOP),
        ((500, 799), desktop.HTBOTTOM),
        ((500, 500), None),
    ],
)
def test_resize_hit_test_uses_an_eight_pixel_window_border(point, expected):
    assert desktop._resize_hit_test(*point, (100, 200, 900, 800)) == expected


@pytest.mark.parametrize(
    ("proposed", "maximized", "expected"),
    [
        ((100, 100, 900, 700), False, (100, 100, 900, 700)),
        ((-8, -8, 1928, 1088), True, (0, 0, 1920, 1040)),
        ((0, 0, 960, 1040), False, (0, 0, 960, 1040)),
    ],
    ids=["normal", "maximized", "snapped"],
)
def test_client_rect_clamps_only_maximized_windows(proposed, maximized, expected):
    assert desktop._client_rect(proposed, (0, 0, 1920, 1040), maximized) == expected


class _FakeIntPtr:
    def __init__(self, value):
        self.value = value


_FakeIntPtr.Zero = _FakeIntPtr(0)


class _FakeNativeWindow:
    def __init__(self):
        self.assigned = None
        self.forwarded = []
        self.released = False

    def AssignHandle(self, handle):
        self.assigned = handle

    def ReleaseHandle(self):
        self.released = True

    def WndProc(self, message):
        self.forwarded.append(message.Msg)


class _FakeFormWindowState:
    Normal = object()
    Maximized = object()


def _install_fake_system(monkeypatch):
    monkeypatch.setitem(sys.modules, "System", SimpleNamespace(IntPtr=_FakeIntPtr))
    monkeypatch.setitem(sys.modules, "System.Windows.Forms", SimpleNamespace(
        FormWindowState=_FakeFormWindowState,
        NativeWindow=_FakeNativeWindow,
    ))


def test_native_hook_converts_managed_handle_before_typed_hit_test_call(monkeypatch):
    _install_fake_system(monkeypatch)
    observed_handles = []
    prototype = desktop.ctypes.CFUNCTYPE(
        desktop.wintypes.BOOL,
        desktop.wintypes.HWND,
        desktop.ctypes.POINTER(desktop._WindowRect),
    )

    @prototype
    def get_window_rect(hwnd, rect_pointer):
        observed_handles.append(hwnd)
        rect = rect_pointer.contents
        rect.left, rect.top, rect.right, rect.bottom = 100, 200, 900, 800
        return True

    managed_handle = SimpleNamespace(ToInt64=lambda: 456)
    native = SimpleNamespace(
        Handle=managed_handle,
        WindowState=_FakeFormWindowState.Normal,
    )
    hook = desktop._native_window_hook(
        native, SimpleNamespace(GetWindowRect=get_window_rect))
    packed_point = (500 << 16) | 101
    message = SimpleNamespace(
        Msg=desktop.WM_NCHITTEST,
        LParam=SimpleNamespace(ToInt64=lambda: packed_point),
        Result=None,
    )

    hook.WndProc(message)

    assert observed_handles == [456]
    assert message.Result.value == desktop.HTLEFT


def test_native_hook_releases_and_drops_itself_on_destroy(monkeypatch):
    _install_fake_system(monkeypatch)
    hwnd = 789
    native = SimpleNamespace(
        Handle=SimpleNamespace(ToInt64=lambda: hwnd),
        WindowState=_FakeFormWindowState.Normal,
    )
    hook = desktop._native_window_hook(native, SimpleNamespace())
    desktop._native_window_hooks[hwnd] = hook

    hook.WndProc(SimpleNamespace(Msg=desktop.WM_NCDESTROY))

    assert hook.released is True
    assert hook.forwarded == [desktop.WM_NCDESTROY]
    assert hwnd not in desktop._native_window_hooks


def test_configure_native_window_replaces_the_existing_hook_for_an_hwnd():
    desktop._native_window_hooks.clear()
    calls = []
    first = SimpleNamespace(ReleaseHandle=lambda: calls.append("release-first"))
    second = SimpleNamespace(ReleaseHandle=lambda: calls.append("release-second"))
    hooks = iter([first, second])
    native = SimpleNamespace(Handle=SimpleNamespace(ToInt64=lambda: 456))
    user32 = SimpleNamespace(
        GetWindowLongW=lambda *_args: desktop.WS_CAPTION,
        SetWindowLongW=lambda *_args: desktop.WS_CAPTION,
        SetWindowPos=lambda *_args: True,
    )

    for _ in range(2):
        desktop._configure_native_window(
            SimpleNamespace(native=native), user32=user32,
            hook_factory=lambda *_args: next(hooks))

    assert calls == ["release-first"]
    assert desktop._native_window_hooks.pop(456) is second


def test_window_shown_configures_native_window_on_ui_thread_before_watching(monkeypatch):
    calls = []

    class Native:
        invoking = False

        def Invoke(self, action):
            calls.append(("invoke", window))
            self.invoking = True
            try:
                action()
            finally:
                self.invoking = False

    window = SimpleNamespace(native=None)

    class Shown:
        def wait(self, *, timeout):
            calls.append(("wait", timeout))
            window.native = Native()
            return True

    window.events = SimpleNamespace(shown=Shown())
    monkeypatch.setitem(sys.modules, "System", SimpleNamespace(Action=lambda action: action))
    monkeypatch.setattr(
        desktop, "_set_window_icon",
        lambda value: calls.append(("icon", value, value.native.invoking)))
    monkeypatch.setattr(
        desktop, "_configure_native_window",
        lambda value: calls.append(("native", value, value.native.invoking)),
    )
    monkeypatch.setattr(
        desktop, "_watch_child",
        lambda proc, window, closing, restart: calls.append(
            ("watch", proc, window, closing, restart)),
    )
    proc, closing, restart = object(), object(), object()

    desktop._on_window_shown(proc, window, closing, restart)

    assert calls == [
        ("wait", 30),
        ("invoke", window),
        ("icon", window, True),
        ("native", window, True),
        ("watch", proc, window, closing, restart),
    ]


def test_window_shown_timeout_logs_once_and_still_watches_child(monkeypatch, desktop_log):
    calls = []
    shown = SimpleNamespace(wait=lambda *, timeout: calls.append(("wait", timeout)) or False)
    window = SimpleNamespace(native=None, events=SimpleNamespace(shown=shown))
    monkeypatch.setattr(
        desktop, "_watch_child",
        lambda proc, window, closing, restart: calls.append(
            ("watch", proc, window, closing, restart)),
    )
    proc, closing, restart = object(), object(), object()

    desktop._on_window_shown(proc, window, closing, restart)

    assert calls == [
        ("wait", 30),
        ("watch", proc, window, closing, restart),
    ]
    assert desktop_log.getvalue().count("could not configure the Atlas native window") == 1


def test_main_sets_identity_before_run(monkeypatch):
    calls = []
    monkeypatch.setattr(desktop, "_configure_file_logging", lambda: calls.append("logging"))
    monkeypatch.setattr(desktop, "_set_app_user_model_id", lambda: calls.append("identity"))
    monkeypatch.setattr(desktop, "run", lambda: calls.append("run") or 17)

    assert desktop.main() == 17
    assert calls == ["logging", "identity", "run"]


def test_read_ui_url_ignores_noise_and_accepts_only_a_loopback_fragment_url():
    stream = StringIO(
        "worker starting\n"
        "ATLAS_UI https://example.com/#pair=wrong\n"
        "ATLAS_UI http://127.0.0.1:4360/#pair=one-use\n"
    )

    assert desktop.read_ui_url(stream) == "http://127.0.0.1:4360/#pair=one-use"
    assert desktop.read_ui_url(StringIO("worker stopped\n")) is None


def test_active_job_titles_filters_terminal_jobs_and_never_sends_the_fragment():
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit):
            return json.dumps({"jobs": [
                {"title": "  Active   analysis  ", "status": "running"},
                {"title": "Finished", "status": "succeeded"},
                {"title": "Waiting", "status": "queued"},
            ]}).encode()

    def opener(request, *, timeout):
        requests.append((request.full_url, timeout))
        return Response()

    titles = desktop._active_job_titles(
        "http://127.0.0.1:4360/#pair=one-use",
        opener=opener,
    )

    assert titles == ["Active analysis", "Waiting"]
    assert requests == [("http://127.0.0.1:4360/jobs", 2.0)]


def test_app_emits_the_bootstrap_url_for_the_desktop_launcher(capsys):
    authorizer = PairingAuthorizer(token="one-use")

    result = app._emit_ui_url(authorizer, 4360)

    assert result == "http://127.0.0.1:4360/#pair=one-use"
    assert capsys.readouterr().out == f"ATLAS_UI {result}\n"


def test_run_spawns_console_worker_opens_exact_window_and_stops_once(desktop_log):
    process = FakeProcess("ATLAS_UI http://127.0.0.1:4360/#pair=one-use\n")
    spawn_calls = []
    window_calls = []
    start_calls = []
    stop_calls = []
    assigned = []
    closed_handles = []
    window = FakeWindow()

    def spawn(command, **kwargs):
        spawn_calls.append((command, kwargs))
        return process

    def window_factory(title, url, **kwargs):
        window_calls.append((title, url, kwargs))
        return window

    def start(func, args):
        start_calls.append((func, args))
        window.events.closed.fire()

    result = desktop.run(
        spawn=spawn,
        window_factory=window_factory,
        start=start,
        terminate=lambda child, url, token: stop_calls.append((child, url, token)),
        create_mutex=lambda: (111, False),
        assign_job=lambda child: assigned.append(child) or 222,
        close_handle=closed_handles.append,
        patch_webview=lambda: None,
        token_factory=lambda: "shutdown-token",
    )

    command, options = spawn_calls[0]
    assert Path(command[0]).name.lower() == "python.exe"
    assert command[1:] == ["-m", "worker.app", "console"]
    assert options["cwd"] == str(desktop.ATLAS)
    assert options["stdout"] is subprocess.PIPE
    assert options["stderr"] is subprocess.STDOUT
    assert options["text"] is True
    assert options["encoding"] == "utf-8"
    assert options["errors"] == "replace"
    assert options["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert options["env"]["PYTHONUTF8"] == "1"
    assert options["env"]["ATLAS_SHUTDOWN_TOKEN"] == "shutdown-token"
    assert len(window_calls) == 1
    title, url, window_options = window_calls[0]
    assert title == "Atlas"
    assert url == "http://127.0.0.1:4360/#pair=one-use"
    assert window_options["width"] == 1100
    assert window_options["height"] == 760
    assert window_options["min_size"] == (800, 600)
    assert window_options["frameless"] is True
    assert window_options["easy_drag"] is False
    assert window_options["resizable"] is True
    assert isinstance(window_options["js_api"], desktop.WindowApi)
    assert len(window.events.closing.handlers) == 1
    assert len(window.events.closed.handlers) == 1
    assert len(start_calls) == 1
    watch_function, watch_args = start_calls[0]
    assert watch_function is desktop._on_window_shown
    assert watch_args[:2] == (process, window)
    assert isinstance(watch_args[2], Event)
    assert callable(watch_args[3])
    assert assigned == [process]
    assert stop_calls == [(
        process,
        "http://127.0.0.1:4360/#pair=one-use",
        "shutdown-token",
    )]
    assert closed_handles == [222, 111]
    assert result == 0
    assert "spawn child pid=4321" in desktop_log.getvalue()
    assert "ui url received=true" in desktop_log.getvalue()
    assert "window created" in desktop_log.getvalue()


def test_run_patches_webview_exactly_once_before_starting_the_gui_loop():
    # F2: the patch_webview() call in run() had no integration coverage --
    # deleting it, or moving it after start(), left the whole suite green. The
    # occlusion patch must be installed BEFORE webview.start takes over the
    # thread, or the first window is created unpatched.
    process = FakeProcess("ATLAS_UI http://127.0.0.1:4360/#pair=one-use\n")
    window = FakeWindow()
    order = []

    def start(_function, _args):
        order.append("start")
        window.events.closed.fire()

    result = desktop.run(
        spawn=lambda _command, **_kwargs: process,
        window_factory=lambda _title, _url, **_kwargs: window,
        start=start,
        terminate=lambda _child, _url, _token: None,
        create_mutex=lambda: (111, False),
        assign_job=lambda _child: 222,
        close_handle=lambda _handle: None,
        patch_webview=lambda: order.append("patch"),
        token_factory=lambda: "shutdown-token",
    )

    assert result == 0
    assert order == ["patch", "start"]


def test_native_window_api_minimizes_and_requests_the_graceful_close(monkeypatch):
    process = FakeProcess("ATLAS_UI http://127.0.0.1:4360/#pair=one-use\n")
    window = FakeWindow()
    api_calls = []
    stop_calls = []

    def confirm(process_arg, window_arg, url_arg):
        api_calls.append((process_arg, window_arg, url_arg))
        return True

    def window_factory(_title, _url, **kwargs):
        api = kwargs["js_api"]
        assert kwargs["frameless"] is True
        assert api._window is None
        window.api = api
        return window

    def start(_function, _args):
        assert window.api._window is window
        window.api.minimize()
        window.api.request_close()

    monkeypatch.setattr(desktop, "_confirm_window_close", confirm)

    result = desktop.run(
        spawn=lambda _command, **_kwargs: process,
        window_factory=window_factory,
        start=start,
        terminate=lambda child, url, token: stop_calls.append((child, url, token)),
        create_mutex=lambda: (111, False),
        assign_job=lambda _child: 222,
        close_handle=lambda _handle: None,
        patch_webview=lambda: None,
        token_factory=lambda: "shutdown-token",
    )

    assert result == 0
    assert window.minimize_calls == 1
    assert window.destroy_calls == 1
    assert api_calls == [(
        process,
        window,
        "http://127.0.0.1:4360/#pair=one-use",
    )]
    assert stop_calls == [(
        process,
        "http://127.0.0.1:4360/#pair=one-use",
        "shutdown-token",
    )]


def test_native_window_api_toggles_native_maximized_state_through_invoke(monkeypatch):
    class WindowState:
        pass

    WindowState.Normal = WindowState()
    WindowState.Maximized = WindowState()

    calls = []

    class Native:
        def __init__(self):
            self._state = WindowState.Normal
            self.invoking = False

        @property
        def WindowState(self):
            return self._state

        @WindowState.setter
        def WindowState(self, value):
            assert self.invoking
            calls.append(("state", value))
            self._state = value

        def Invoke(self, action):
            calls.append(("invoke", action))
            self.invoking = True
            try:
                action()
            finally:
                self.invoking = False

    monkeypatch.setitem(sys.modules, "System", SimpleNamespace(Action=lambda action: action))
    window = FakeWindow()
    window.native = Native()
    api = desktop.WindowApi()
    api._window = window

    api.toggle_maximize()
    assert window.native.WindowState is WindowState.Maximized
    api.toggle_maximize()

    assert window.native.WindowState is WindowState.Normal
    assert [call[0] for call in calls] == ["invoke", "state", "invoke", "state"]


def test_native_window_api_logs_invoke_failure(monkeypatch, desktop_log):
    monkeypatch.setitem(sys.modules, "System", SimpleNamespace(Action=lambda action: action))
    native = SimpleNamespace(
        WindowState=SimpleNamespace(),
        Invoke=lambda _action: (_ for _ in ()).throw(OSError("invoke failed")),
    )
    api = desktop.WindowApi()
    api._window = SimpleNamespace(native=native)

    api.toggle_maximize()

    assert "could not toggle the Atlas Windows window state" in desktop_log.getvalue()


def test_run_replaces_exit_21_child_and_loads_new_worker_url_in_same_window():
    first = FakeProcess(
        "ATLAS_UI http://127.0.0.1:4360/#pair=first\n",
        pid=1001,
        exit_code=desktop.RESTART_EXIT_CODE,
    )
    second = FakeProcess(
        "ATLAS_UI http://127.0.0.1:4361/#pair=second\n",
        pid=1002,
        exit_code=0,
    )
    processes = iter([first, second])
    handles = iter([201, 202])
    assigned = []
    closed_handles = []
    terminated = []
    window = FakeWindow()

    def spawn(_command, **_kwargs):
        return next(processes)

    def start(function, args):
        function(*args)

    result = desktop.run(
        spawn=spawn,
        window_factory=lambda _title, _url, **_kwargs: window,
        start=start,
        terminate=lambda child, url, token: terminated.append((child, url, token)),
        create_mutex=lambda: (111, False),
        assign_job=lambda child: assigned.append(child) or next(handles),
        close_handle=closed_handles.append,
        patch_webview=lambda: None,
        token_factory=lambda: "shutdown-token",
    )

    assert result == 0
    assert assigned == [first, second]
    assert "reconnecting audio\u2026" in window.loaded_html[0]
    assert "Atlas stopped" in window.loaded_html[1]
    assert window.loaded_urls == ["http://127.0.0.1:4361/#pair=second"]
    assert terminated == [(
        second,
        "http://127.0.0.1:4361/#pair=second",
        "shutdown-token",
    )]
    assert closed_handles == [201, 202, 111]


def test_existing_desktop_mutex_reports_already_running_without_spawning():
    spawn_calls = []
    messages = []
    closed_handles = []

    result = desktop.run(
        spawn=lambda *_args, **_kwargs: spawn_calls.append(True),
        create_mutex=lambda: (333, True),
        show_already_running=lambda: messages.append("Atlas is already running"),
        close_handle=closed_handles.append,
    )

    assert result == 1
    assert spawn_calls == []
    assert messages == ["Atlas is already running"]
    assert closed_handles == [333]


def test_instance_mutex_uses_the_required_local_name_and_last_error():
    calls = []

    handle, already_running = desktop._create_instance_mutex(
        platform="nt",
        create_mutex=lambda security, owner, name: (
            calls.append((security, owner, name)) or 444
        ),
        get_last_error=lambda: desktop.ERROR_ALREADY_EXISTS,
    )

    assert handle == 444
    assert already_running is True
    assert calls == [(None, False, "Local\\AtlasDesktop")]


def test_close_confirmation_lists_active_jobs_and_can_veto_close():
    window = FakeWindow(confirmation=False)

    allowed = desktop.confirm_close(
        window,
        "http://127.0.0.1:4360/#pair=one-use",
        jobs_reader=lambda _url: ["Quarterly analysis", "Draft launch brief"],
    )

    assert allowed is False
    assert len(window.dialogs) == 1
    title, message = window.dialogs[0]
    assert title == "Close Atlas?"
    assert "Quarterly analysis" in message
    assert "Draft launch brief" in message
    assert "jobs will be cancelled" in message


def test_close_without_active_jobs_needs_no_confirmation():
    window = FakeWindow()

    assert desktop.confirm_close(window, "http://127.0.0.1:4360/", jobs_reader=lambda _url: [])
    assert window.dialogs == []


def test_close_after_child_death_needs_no_confirmation():
    process = FakeProcess(running=False)
    window = FakeWindow()

    assert desktop._confirm_window_close(process, window, "http://127.0.0.1:4360/")
    assert window.dialogs == []


def test_unexpected_child_death_replaces_the_window_with_stopped_page(desktop_log):
    process = FakeProcess(running=True)
    window = FakeWindow()
    closing = Event()

    desktop._watch_child(process, window, closing)

    assert len(window.loaded_html) == 1
    assert "Atlas stopped" in window.loaded_html[0]
    assert "child exit code=0" in desktop_log.getvalue()


def test_audio_restart_exit_restarts_in_place_with_reconnecting_page():
    process = FakeProcess(exit_code=desktop.RESTART_EXIT_CODE)
    replacement = FakeProcess(exit_code=0)
    window = FakeWindow()
    closing = Event()
    restarts = []

    def restart(exited):
        restarts.append(exited)
        return replacement

    desktop._watch_child(process, window, closing, restart)

    assert restarts == [process]
    assert "reconnecting audio\u2026" in window.loaded_html[0]
    assert "Atlas stopped" in window.loaded_html[1]


def test_second_audio_restart_inside_thirty_seconds_is_deferred_not_stopped():
    process = FakeProcess(exit_code=desktop.RESTART_EXIT_CODE)
    replacement = FakeProcess(exit_code=desktop.RESTART_EXIT_CODE)
    final = FakeProcess(exit_code=0)
    window = FakeWindow()
    closing = Event()
    restarts = []
    waits = []
    times = iter([100.0, 120.0])
    replacements = iter([replacement, final])

    def restart(exited):
        restarts.append(exited)
        return next(replacements)

    desktop._watch_child(
        process,
        window,
        closing,
        restart,
        clock=lambda: next(times),
        wait=lambda delay: waits.append(delay) or False,
    )

    assert restarts == [process, replacement]
    assert waits == [10.0]
    assert len(window.loaded_html) == 3
    assert "reconnecting audio\u2026" in window.loaded_html[0]
    assert "reconnecting audio\u2026" in window.loaded_html[1]
    assert "Atlas stopped" in window.loaded_html[2]


def test_audio_restart_is_allowed_again_after_thirty_seconds():
    first = FakeProcess(exit_code=desktop.RESTART_EXIT_CODE)
    second = FakeProcess(exit_code=desktop.RESTART_EXIT_CODE)
    third = FakeProcess(exit_code=0)
    window = FakeWindow()
    closing = Event()
    replacements = iter([second, third])
    restarts = []
    times = iter([100.0, 130.0])

    def restart(exited):
        restarts.append(exited)
        return next(replacements)

    desktop._watch_child(
        first,
        window,
        closing,
        restart,
        clock=lambda: next(times),
    )

    assert restarts == [first, second]
    assert len(window.loaded_html) == 3
    assert "reconnecting audio\u2026" in window.loaded_html[0]
    assert "reconnecting audio\u2026" in window.loaded_html[1]
    assert "Atlas stopped" in window.loaded_html[2]


def test_third_audio_restart_inside_ten_minutes_shows_stopped_page():
    first = FakeProcess(exit_code=desktop.RESTART_EXIT_CODE)
    second = FakeProcess(exit_code=desktop.RESTART_EXIT_CODE)
    third = FakeProcess(exit_code=desktop.RESTART_EXIT_CODE)
    window = FakeWindow()
    closing = Event()
    replacements = iter([second, third])
    restarts = []
    times = iter([100.0, 140.0, 180.0])

    def restart(exited):
        restarts.append(exited)
        return next(replacements)

    desktop._watch_child(
        first,
        window,
        closing,
        restart,
        clock=lambda: next(times),
    )

    assert restarts == [first, second]
    assert "Atlas stopped" in window.loaded_html[-1]


def test_audio_restart_burst_count_expires_after_ten_minutes():
    first = FakeProcess(exit_code=desktop.RESTART_EXIT_CODE)
    second = FakeProcess(exit_code=desktop.RESTART_EXIT_CODE)
    third = FakeProcess(exit_code=desktop.RESTART_EXIT_CODE)
    final = FakeProcess(exit_code=0)
    window = FakeWindow()
    closing = Event()
    replacements = iter([second, third, final])
    restarts = []
    times = iter([100.0, 140.0, 701.0])

    def restart(exited):
        restarts.append(exited)
        return next(replacements)

    desktop._watch_child(
        first,
        window,
        closing,
        restart,
        clock=lambda: next(times),
    )

    assert restarts == [first, second, third]
    assert "Atlas stopped" in window.loaded_html[-1]


def test_expected_child_stop_does_not_replace_the_window():
    process = FakeProcess(running=True)
    window = FakeWindow()
    closing = Event()
    closing.set()

    desktop._watch_child(process, window, closing)

    assert window.loaded_html == []


def test_shutdown_request_uses_token_header_and_never_sends_pairing_fragment():
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, limit):
            requests.append(("read", limit))
            return b'{"ok": true}'

    def opener(request, *, timeout):
        requests.append((request.full_url, dict(request.header_items()), timeout))
        return Response()

    desktop._request_shutdown(
        "http://127.0.0.1:4360/#pair=one-use",
        "shutdown-token",
        opener=opener,
    )

    url, headers, timeout = requests[0]
    assert url == "http://127.0.0.1:4360/shutdown"
    assert headers["X-atlas-shutdown"] == "shutdown-token"
    assert timeout == 16.0
    assert requests[1] == ("read", 65_537)


def test_status_pages_preserve_visible_text():
    assert "<title>Atlas stopped</title>" in desktop.STOPPED_HTML
    assert "<h1>Atlas stopped</h1>" in desktop.STOPPED_HTML
    assert (
        "<p>Close this window and open Atlas again to restart it.</p>"
        in desktop.STOPPED_HTML
    )
    assert "<title>Atlas reconnecting audio</title>" in desktop.RECONNECTING_HTML
    assert "<h1>reconnecting audio\u2026</h1>" in desktop.RECONNECTING_HTML
    assert (
        "<p>Atlas is following your new system audio device.</p>"
        in desktop.RECONNECTING_HTML
    )


def test_desktop_file_log_is_bounded_and_rotating(tmp_path):
    handler = desktop._configure_file_logging(tmp_path)
    try:
        assert isinstance(handler, RotatingFileHandler)
        assert handler.maxBytes == 256 * 1024
        assert handler.backupCount == 2
        assert handler.level == logging.INFO
        assert Path(handler.baseFilename) == tmp_path / "Atlas" / "logs" / "desktop.log"
    finally:
        desktop.logger.removeHandler(handler)
        handler.close()


def test_desktop_file_log_handler_permission_failure_continues_without_file(
    tmp_path, monkeypatch,
):
    def deny_handler(*_args, **_kwargs):
        raise PermissionError("logging denied")

    monkeypatch.setattr(desktop, "RotatingFileHandler", deny_handler)
    monkeypatch.setattr(desktop, "run", lambda: 17)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    assert desktop.main() == 17
    assert not list(tmp_path.rglob("desktop.log"))


def test_desktop_file_logging_replaces_existing_atlas_handler(tmp_path):
    first = desktop._configure_file_logging(tmp_path)
    second = desktop._configure_file_logging(tmp_path)
    try:
        tagged = [
            handler for handler in desktop.logger.handlers
            if getattr(handler, "_atlas_desktop_file_handler", False)
        ]
        assert tagged == [second]
        assert first.stream is None
    finally:
        desktop.logger.removeHandler(second)
        second.close()


def test_worker_marker_log_persists_only_host_shaped_category_and_count(tmp_path):
    opaque_environment = "PRIVATE_SETTING=opaque-environment-value"
    prompt = "prompt: reveal the operator's private request"
    boundary_token = "boundary-spanning-secret"
    handler = desktop._configure_file_logging(tmp_path)
    result = Queue(maxsize=1)
    try:
        desktop._capture_ui_url(
            StringIO(
                "ATLAS_UI http://127.0.0.1:4360/#pair=one-use\n"
                f"Traceback {opaque_environment}\n"
                f"Error {prompt}\n"
                f"Exception {'x' * 385}{boundary_token}\n"
            ),
            result,
        )
        handler.flush()
        payload = Path(handler.baseFilename).read_bytes()
    finally:
        desktop.logger.removeHandler(handler)
        handler.close()

    assert result.get_nowait() is not None
    assert b"worker marker category=traceback count=1" in payload
    assert b"worker marker category=error count=2" in payload
    assert b"worker marker category=exception count=3" in payload
    assert opaque_environment.encode("ascii") not in payload
    assert prompt.encode("ascii") not in payload
    assert boundary_token.encode("ascii") not in payload
    assert b"x" * 385 not in payload


def test_worker_marker_log_caps_each_child_at_two_hundred_lines(desktop_log):
    result = Queue(maxsize=1)
    desktop._capture_ui_url(
        StringIO(
            "ATLAS_UI http://127.0.0.1:4360/#pair=one-use\n"
            + "".join(f"Error diagnostic {number}\n" for number in range(205))
        ),
        result,
    )

    assert result.get_nowait() is not None
    assert desktop_log.getvalue().count("worker marker category=error") == 200
    assert "count=200" in desktop_log.getvalue()
    assert "count=201" not in desktop_log.getvalue()


def test_bootstrap_line_parser_is_shared_by_stream_readers(monkeypatch):
    ui_url = "http://127.0.0.1:4360/#pair=one-use"
    calls = []

    def parse(line):
        calls.append(line)
        return ui_url

    monkeypatch.setattr(desktop, "_parse_ui_url_line", parse)
    assert desktop.read_ui_url(StringIO("candidate\n")) == ui_url

    result = Queue(maxsize=1)
    desktop._capture_ui_url(StringIO("candidate\n"), result)
    assert result.get_nowait() == ui_url
    assert calls == ["candidate\n", "candidate\n"]


def test_run_logs_ui_url_wait_timeout_without_worker_output(desktop_log):
    release = Event()

    class BlockingStream:
        def __iter__(self):
            return self

        def __next__(self):
            release.wait(1)
            raise StopIteration

    process = FakeProcess(pid=9191)
    process.stdout = BlockingStream()
    try:
        result = desktop.run(
            spawn=lambda _command, **_kwargs: process,
            terminate=lambda *_args: None,
            create_mutex=lambda: (111, False),
            assign_job=lambda _child: 222,
            close_handle=lambda _handle: None,
            wait_url_timeout_s=0,
        )
    finally:
        release.set()

    assert result == 1
    assert "spawn child pid=9191" in desktop_log.getvalue()
    assert "ui url received=false" in desktop_log.getvalue()
    assert "ui url wait timeout" in desktop_log.getvalue()


def test_stop_child_requests_shutdown_then_waits_up_to_twenty_seconds():
    process = FakeProcess(pid=77)
    shutdown_calls = []
    kill_calls = []

    desktop.stop_child(
        process,
        "http://127.0.0.1:4360/#pair=one-use",
        "shutdown-token",
        shutdown_request=lambda url, token: shutdown_calls.append((url, token)),
        killer=lambda command, **kwargs: kill_calls.append((command, kwargs)),
    )

    assert shutdown_calls == [(
        "http://127.0.0.1:4360/#pair=one-use",
        "shutdown-token",
    )]
    assert kill_calls == []
    assert process.waits == [20]


def test_stop_child_uses_tree_kill_after_graceful_timeout():
    class SlowProcess(FakeProcess):
        def wait(self, timeout=None):
            self.waits.append(timeout)
            if timeout == 20:
                raise subprocess.TimeoutExpired("worker", timeout)
            self.running = False
            return 0

    process = SlowProcess(pid=88)
    calls = []

    desktop.stop_child(
        process,
        killer=lambda pid, **kwargs: calls.append((pid, kwargs)),
    )

    assert calls == [
        (88, {"check": False, "force": False}),
    ]
    assert process.waits == [20, 10]


def test_stop_child_forces_tree_after_another_ten_seconds():
    class StuckProcess(FakeProcess):
        def wait(self, timeout=None):
            self.waits.append(timeout)
            raise subprocess.TimeoutExpired("worker", timeout)

    process = StuckProcess(pid=99)
    calls = []

    desktop.stop_child(
        process,
        killer=lambda pid, **kwargs: calls.append((pid, kwargs)),
    )

    assert calls == [
        (99, {"check": False, "force": False}),
        (99, {"check": False, "force": True}),
    ]
    assert process.waits == [20, 10]


def test_stop_child_is_a_noop_after_child_exit():
    process = FakeProcess(running=False)
    calls = []

    desktop.stop_child(process, killer=lambda command, **kwargs: calls.append(command))

    assert calls == []


def test_shortcut_installer_targets_pythonw_desktop_module():
    script = (desktop.ATLAS / "scripts" / "install_shortcut.ps1").read_text(encoding="utf-8")

    assert "pythonw.exe" in script
    assert '-m worker.desktop' in script
    assert "Atlas.lnk" in script
    assert 'GetFolderPath("Desktop")' in script
    assert "IconLocation" in script
    assert "System.AppUserModel.ID" in script
    assert 'SetAppUserModelId($shortcutPath, "Atlas.Desktop")' in script
    assert 'new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3")' in script
    assert "PropertyId = 5" in script
    assert "WindowStyle = 7" in script


class FakeCreationProperties:
    AdditionalBrowserArguments = ""


class FakeWebView2Control:
    def __init__(self, props) -> None:
        self.CreationProperties = props


EDGECHROMIUM = "webview.platforms.edgechromium"


class EdgeChromeTemplate:
    """Shaped like pywebview 6.2.1: the browser arguments are one constant in the constructor."""

    def __init__(self, form, window, cache_dir):
        self.constructed = (form, window, cache_dir)
        props = FakeCreationProperties()
        props.AdditionalBrowserArguments = "--disable-features=ElasticOverscroll"
        self.webview = FakeWebView2Control(props)


class RenamedLiteralTemplate:
    def __init__(self, form, window, cache_dir):
        self.constructed = (form, window, cache_dir)
        props = FakeCreationProperties()
        props.AdditionalBrowserArguments = "--disable-features=Overscroll"
        self.webview = FakeWebView2Control(props)


class FailingTemplate:
    def __init__(self, form, window, cache_dir):
        props = FakeCreationProperties()
        props.AdditionalBrowserArguments = "--disable-features=ElasticOverscroll"
        self.webview = FakeWebView2Control(props)
        raise RuntimeError("browser unavailable")


def _fake_edgechromium(monkeypatch, template=EdgeChromeTemplate, *, attribute="EdgeChrome",
                       metaclass=type):
    """Install a fake edgechromium module holding a throwaway copy of `template`."""
    module = ModuleType(EDGECHROMIUM)
    setattr(module, attribute, metaclass("EdgeChrome", (), dict(vars(template))))
    monkeypatch.setitem(sys.modules, EDGECHROMIUM, module)
    return module


def test_merge_disable_features_appends_to_the_single_existing_flag():
    merged = desktop._merge_disable_features(
        "--disable-features=ElasticOverscroll", "CalculateNativeWinOcclusion")

    assert merged == "--disable-features=ElasticOverscroll,CalculateNativeWinOcclusion"


def test_merge_disable_features_keeps_other_arguments_and_collapses_duplicate_flags():
    merged = desktop._merge_disable_features(
        "--disable-features=ElasticOverscroll --allow-file-access-from-files "
        "--remote-debugging-port=9222 --disable-features=Translate",
        "CalculateNativeWinOcclusion",
    )

    assert merged == (
        "--disable-features=ElasticOverscroll,Translate,CalculateNativeWinOcclusion "
        "--allow-file-access-from-files --remote-debugging-port=9222"
    )


def test_merge_disable_features_adds_the_flag_when_none_is_present():
    merged = desktop._merge_disable_features(
        "--allow-file-access-from-files", "CalculateNativeWinOcclusion")

    assert merged == (
        "--allow-file-access-from-files --disable-features=CalculateNativeWinOcclusion")


def test_merge_disable_features_refuses_a_quoted_value_instead_of_corrupting_it():
    quoted = '--disable-features=ElasticOverscroll --user-agent="Atlas Voice"'

    assert desktop._merge_disable_features(quoted, "CalculateNativeWinOcclusion") is None


def test_webview_occlusion_patch_merges_the_flag_into_the_constructor_constant(
    monkeypatch, desktop_log,
):
    module = _fake_edgechromium(monkeypatch)

    desktop._patch_webview_occlusion()
    browser = module.EdgeChrome("form", "window", "cache")

    assert browser.webview.CreationProperties.AdditionalBrowserArguments == (
        "--disable-features=ElasticOverscroll,CalculateNativeWinOcclusion")
    assert browser.constructed == ("form", "window", "cache")
    assert desktop_log.getvalue() == ""


def test_webview_occlusion_patch_keeps_the_constructor_signature_and_identity(
    monkeypatch, desktop_log,
):
    module = _fake_edgechromium(monkeypatch)
    original = module.EdgeChrome.__init__

    desktop._patch_webview_occlusion()
    patched = module.EdgeChrome.__init__

    assert patched is not original
    assert patched.__name__ == original.__name__
    assert patched.__qualname__ == original.__qualname__
    assert patched.__defaults__ == original.__defaults__
    assert patched.__code__.co_argcount == original.__code__.co_argcount
    assert patched.__code__.co_varnames == original.__code__.co_varnames
    assert desktop_log.getvalue() == ""


def test_webview_occlusion_patch_is_idempotent(monkeypatch, desktop_log):
    module = _fake_edgechromium(monkeypatch)

    desktop._patch_webview_occlusion()
    patched = module.EdgeChrome.__init__
    desktop._patch_webview_occlusion()
    desktop._patch_webview_occlusion()
    browser = module.EdgeChrome("form", "window", "cache")

    assert module.EdgeChrome.__init__ is patched
    assert browser.webview.CreationProperties.AdditionalBrowserArguments == (
        "--disable-features=ElasticOverscroll,CalculateNativeWinOcclusion")
    assert desktop_log.getvalue() == ""


def test_webview_occlusion_patch_lets_a_failing_constructor_propagate(monkeypatch, desktop_log):
    module = _fake_edgechromium(monkeypatch, FailingTemplate)

    desktop._patch_webview_occlusion()

    with pytest.raises(RuntimeError, match="browser unavailable"):
        module.EdgeChrome("form", "window", "cache")
    assert desktop_log.getvalue() == ""


def test_webview_occlusion_patch_skips_a_constructor_without_the_known_literal(
    monkeypatch, desktop_log,
):
    module = _fake_edgechromium(monkeypatch, RenamedLiteralTemplate)

    desktop._patch_webview_occlusion()
    browser = module.EdgeChrome("form", "window", "cache")

    assert browser.webview.CreationProperties.AdditionalBrowserArguments == (
        "--disable-features=Overscroll")
    assert desktop_log.getvalue() == "webview occlusion patch skipped: literal\n"


def test_webview_occlusion_patch_skips_a_missing_module(monkeypatch, desktop_log):
    monkeypatch.setitem(sys.modules, EDGECHROMIUM, None)

    desktop._patch_webview_occlusion()

    assert desktop_log.getvalue() == "webview occlusion patch skipped: ModuleNotFoundError\n"


def test_webview_occlusion_patch_skips_a_renamed_class(monkeypatch, desktop_log):
    _fake_edgechromium(monkeypatch, attribute="EdgeChromium")

    desktop._patch_webview_occlusion()

    assert desktop_log.getvalue() == "webview occlusion patch skipped: class\n"


def test_webview_occlusion_patch_skips_a_class_without_its_own_constructor(
    monkeypatch, desktop_log,
):
    module = ModuleType(EDGECHROMIUM)
    module.EdgeChrome = type("EdgeChrome", (), {})
    monkeypatch.setitem(sys.modules, EDGECHROMIUM, module)

    desktop._patch_webview_occlusion()

    assert desktop_log.getvalue() == "webview occlusion patch skipped: constructor\n"


def test_webview_occlusion_patch_skips_a_constructor_that_is_not_python(monkeypatch, desktop_log):
    module = ModuleType(EDGECHROMIUM)
    module.EdgeChrome = type("EdgeChrome", (), {"__init__": object.__init__})
    monkeypatch.setitem(sys.modules, EDGECHROMIUM, module)

    desktop._patch_webview_occlusion()

    assert desktop_log.getvalue() == "webview occlusion patch skipped: constructor\n"


def test_webview_occlusion_patch_reports_an_unassignable_constructor(monkeypatch, desktop_log):
    class FrozenClass(type):
        def __setattr__(cls, name, value):
            raise TypeError("read-only class")

    _fake_edgechromium(monkeypatch, metaclass=FrozenClass)

    desktop._patch_webview_occlusion()

    assert desktop_log.getvalue() == "webview occlusion patch skipped: TypeError\n"


def test_installed_pywebview_constructor_still_hardcodes_the_known_literal():
    # Canary: parses the installed pywebview without importing it, so an upgrade that moves or
    # renames this literal fails here instead of silently skipping the occlusion patch.
    import ast

    import webview

    source = (Path(webview.__file__).parent / "platforms" / "edgechromium.py").read_text(
        encoding="utf-8")
    classes = [node for node in ast.parse(source).body
               if isinstance(node, ast.ClassDef) and node.name == "EdgeChrome"]
    assert len(classes) == 1
    constructors = [node for node in classes[0].body
                    if isinstance(node, ast.FunctionDef) and node.name == "__init__"]
    assert len(constructors) == 1
    assignments = [
        node for node in ast.walk(constructors[0])
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Attribute)
                and target.attr == "AdditionalBrowserArguments" for target in node.targets)
        and isinstance(node.value, ast.Constant)
        and node.value.value == desktop.WEBVIEW_BROWSER_ARGUMENTS
    ]
    assert len(assignments) == 1


def test_webview_occlusion_patch_rewrites_the_installed_pywebview_constructor():
    # The only test that imports the real platform module (pythonnet, WinForms and the WebView2
    # interop assemblies, about 0.7s and 41MB of RSS). No window is created.
    from importlib import import_module

    module = import_module("webview.platforms.edgechromium")
    original = vars(module.EdgeChrome).get("__init__")
    try:
        desktop._patch_webview_occlusion()
        constants = vars(module.EdgeChrome)["__init__"].__code__.co_consts

        assert "--disable-features=ElasticOverscroll,CalculateNativeWinOcclusion" in constants
        assert desktop.WEBVIEW_BROWSER_ARGUMENTS not in constants
    finally:
        module.EdgeChrome.__init__ = original
