"""Native Atlas desktop launcher behavior without real processes or windows."""
from __future__ import annotations

from io import StringIO
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from queue import Queue
import subprocess
from threading import Event
from types import SimpleNamespace

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
    assert watch_function is desktop._watch_child
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


def test_native_window_api_toggles_work_area_and_restores_saved_bounds(monkeypatch):
    work_area = SimpleNamespace(X=0, Y=0, Width=1920, Height=1040)
    screen = SimpleNamespace(frame=work_area)
    window = FakeWindow()
    api = desktop.WindowApi()
    api._window = window
    monkeypatch.setattr(desktop, "webview", SimpleNamespace(screens=[screen]))

    api.toggle_maximize()
    api.toggle_maximize()

    assert window.resize_calls == [(1920, 1040), (1100, 760)]
    assert window.move_calls == [(0, 0), (100, 120)]


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
    assert "WindowStyle = 7" in script
