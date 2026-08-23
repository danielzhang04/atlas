"""Native Atlas desktop launcher behavior without real processes or windows."""
from __future__ import annotations

from io import StringIO
import json
from pathlib import Path
import subprocess
from threading import Event
from types import SimpleNamespace

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

    def create_confirmation_dialog(self, title, message):
        self.dialogs.append((title, message))
        return self.confirmation

    def load_html(self, html):
        self.loaded_html.append(html)

    def load_url(self, url):
        self.loaded_urls.append(url)


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


def test_run_spawns_console_worker_opens_exact_window_and_stops_once():
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
    assert window_calls == [(
        "Atlas",
        "http://127.0.0.1:4360/#pair=one-use",
        {"width": 1100, "height": 760, "min_size": (800, 600)},
    )]
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
    assert "reconnecting audio…" in window.loaded_html[0]
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


def test_unexpected_child_death_replaces_the_window_with_stopped_page():
    process = FakeProcess(running=True)
    window = FakeWindow()
    closing = Event()

    desktop._watch_child(process, window, closing)

    assert len(window.loaded_html) == 1
    assert "Atlas stopped" in window.loaded_html[0]


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
    assert "reconnecting audio…" in window.loaded_html[0]
    assert "Atlas stopped" in window.loaded_html[1]


def test_audio_restart_is_limited_to_once_per_thirty_seconds():
    process = FakeProcess(exit_code=desktop.RESTART_EXIT_CODE)
    replacement = FakeProcess(exit_code=desktop.RESTART_EXIT_CODE)
    window = FakeWindow()
    closing = Event()
    restarts = []
    times = iter([100.0, 120.0])

    def restart(exited):
        restarts.append(exited)
        return replacement

    desktop._watch_child(
        process,
        window,
        closing,
        restart,
        clock=lambda: next(times),
    )

    assert restarts == [process]
    assert len(window.loaded_html) == 2
    assert "reconnecting audio…" in window.loaded_html[0]
    assert "Atlas stopped" in window.loaded_html[1]


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
    assert "reconnecting audio…" in window.loaded_html[0]
    assert "reconnecting audio…" in window.loaded_html[1]
    assert "Atlas stopped" in window.loaded_html[2]


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
        killer=lambda command, **kwargs: calls.append((command, kwargs)),
    )

    assert calls == [
        (["taskkill", "/T", "/PID", "88"], {"check": False}),
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
        killer=lambda command, **kwargs: calls.append((command, kwargs)),
    )

    assert calls == [
        (["taskkill", "/T", "/PID", "99"], {"check": False}),
        (["taskkill", "/T", "/PID", "99", "/F"], {"check": False}),
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
