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

    def create_confirmation_dialog(self, title, message):
        self.dialogs.append((title, message))
        return self.confirmation

    def load_html(self, html):
        self.loaded_html.append(html)


class FakeProcess:
    def __init__(self, output="", *, pid=4321, running=True) -> None:
        self.stdout = StringIO(output)
        self.pid = pid
        self.running = running
        self.waits = []

    def poll(self):
        return None if self.running else 0

    def wait(self, timeout=None):
        self.waits.append(timeout)
        self.running = False
        return 0


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
        terminate=lambda child: stop_calls.append(child),
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
    assert window_calls == [(
        "Atlas",
        "http://127.0.0.1:4360/#pair=one-use",
        {"width": 1100, "height": 760, "min_size": (800, 600)},
    )]
    assert len(window.events.closing.handlers) == 1
    assert len(window.events.closed.handlers) == 1
    assert start_calls == [(desktop._watch_child, (process, window, start_calls[0][1][2]))]
    assert stop_calls == [process]
    assert result == 0


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
    assert "running jobs will be stopped" in message


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


def test_expected_child_stop_does_not_replace_the_window():
    process = FakeProcess(running=True)
    window = FakeWindow()
    closing = Event()
    closing.set()

    desktop._watch_child(process, window, closing)

    assert window.loaded_html == []


def test_stop_child_uses_tree_kill_then_waits_up_to_ten_seconds():
    process = FakeProcess(pid=77)
    calls = []

    desktop.stop_child(process, killer=lambda command, **kwargs: calls.append((command, kwargs)))

    assert calls == [(["taskkill", "/T", "/PID", "77"], {"check": False})]
    assert process.waits == [10]


def test_stop_child_escalates_to_force_after_timeout():
    class StuckProcess(FakeProcess):
        def wait(self, timeout=None):
            self.waits.append(timeout)
            raise subprocess.TimeoutExpired("worker", timeout)

    process = StuckProcess(pid=88)
    calls = []

    desktop.stop_child(process, killer=lambda command, **kwargs: calls.append((command, kwargs)))

    assert calls == [
        (["taskkill", "/T", "/PID", "88"], {"check": False}),
        (["taskkill", "/T", "/PID", "88", "/F"], {"check": False}),
    ]
    assert process.waits == [10]


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
