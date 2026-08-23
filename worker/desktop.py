"""Run Atlas as a native pywebview window with a child voice worker."""
from __future__ import annotations

import json
import os
from pathlib import Path
from queue import Empty, Queue
import subprocess
import sys
from threading import Event, Lock, Thread
from typing import Callable, TextIO
from urllib.parse import parse_qs, urlsplit, urlunsplit
from urllib.request import Request, urlopen

import webview


__all__ = [
    "ATLAS",
    "confirm_close",
    "main",
    "read_ui_url",
    "run",
    "stop_child",
]

ATLAS = Path(__file__).resolve().parents[1]
ACTIVE_JOB_STATES = {"queued", "launching", "running"}
STOPPED_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Atlas stopped</title>
  <style>
    body { background: #101319; color: #edf2f7; display: grid; font: 18px system-ui;
      margin: 0; min-height: 100vh; place-items: center; }
    main { max-width: 36rem; padding: 3rem; text-align: center; }
    h1 { font-size: 2rem; margin-bottom: .5rem; }
    p { color: #aeb8c5; line-height: 1.5; }
  </style>
</head>
<body><main><h1>Atlas stopped</h1><p>Close this window and open Atlas again to restart it.</p></main></body>
</html>"""


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


def read_ui_url(stream: TextIO) -> str | None:
    """Read until the worker emits a valid Atlas loopback bootstrap URL."""
    for line in stream:
        if not line.startswith("ATLAS_UI "):
            continue
        value = line.removeprefix("ATLAS_UI ").strip()
        if _is_ui_url(value):
            return value
    return None


def _jobs_url(ui_url: str) -> str:
    parsed = urlsplit(ui_url)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port is None:
        raise ValueError("invalid Atlas UI URL")
    return urlunsplit((parsed.scheme, parsed.netloc, "/jobs", "", ""))


def _active_job_titles(
    ui_url: str,
    *,
    opener: Callable = urlopen,
) -> list[str]:
    request = Request(_jobs_url(ui_url), headers={"accept": "application/json"})
    with opener(request, timeout=2.0) as response:
        body = response.read(65_537)
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


def confirm_close(
    window,
    ui_url: str,
    *,
    jobs_reader: Callable[[str], list[str]] = _active_job_titles,
) -> bool:
    """Allow close immediately when idle, otherwise ask once with active job titles."""
    try:
        titles = jobs_reader(ui_url)
    except Exception:
        message = (
            "Atlas could not check whether jobs are running. "
            "Any running jobs will be stopped.\n\nClose Atlas?"
        )
        return bool(window.create_confirmation_dialog("Close Atlas?", message))
    if not titles:
        return True
    job_lines = "\n".join(f"- {title}" for title in titles)
    message = (
        f"The following jobs are still running:\n\n{job_lines}\n\n"
        "These running jobs will be stopped.\n\nClose Atlas?"
    )
    return bool(window.create_confirmation_dialog("Close Atlas?", message))


def _confirm_window_close(proc, window, ui_url: str) -> bool:
    if proc.poll() is not None:
        return True
    return confirm_close(window, ui_url)


def _taskkill(command: list[str], *, check: bool) -> subprocess.CompletedProcess:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(command, check=check, creationflags=creationflags)


def stop_child(proc, *, killer: Callable = _taskkill) -> None:
    """Stop the worker process tree, escalating to a forced kill after ten seconds."""
    if proc.poll() is not None:
        return
    command = ["taskkill", "/T", "/PID", str(proc.pid)]
    try:
        killer(command, check=False)
    except OSError:
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            killer([*command, "/F"], check=False)
        except OSError:
            pass


def _worker_python() -> str:
    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe":
        return str(executable.with_name("python.exe"))
    return str(executable)


def _watch_child(proc, window, closing: Event) -> None:
    proc.wait()
    if not closing.is_set():
        window.load_html(STOPPED_HTML)


def _capture_ui_url(stream: TextIO, result: Queue[str | None]) -> None:
    url = read_ui_url(stream)
    result.put(url)
    if url is not None:
        for _line in stream:
            pass


def run(
    *,
    spawn: Callable = subprocess.Popen,
    window_factory: Callable = webview.create_window,
    start: Callable = webview.start,
    terminate: Callable = stop_child,
    wait_url_timeout_s: float = 90,
) -> int:
    """Launch the voice worker, host its paired UI, and tie its life to the window."""
    previous_utf8 = os.environ.get("PYTHONUTF8")
    os.environ["PYTHONUTF8"] = "1"
    try:
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
        )
    finally:
        if previous_utf8 is None:
            os.environ.pop("PYTHONUTF8", None)
        else:
            os.environ["PYTHONUTF8"] = previous_utf8
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
        terminate(proc)

    try:
        if proc.stdout is None:
            return 1
        result: Queue[str | None] = Queue(maxsize=1)
        Thread(target=_capture_ui_url, args=(proc.stdout, result), daemon=True).start()
        try:
            ui_url = result.get(timeout=wait_url_timeout_s)
        except Empty:
            return 1
        if ui_url is None:
            return 1
        window = window_factory(
            "Atlas",
            ui_url,
            width=1100,
            height=760,
            min_size=(800, 600),
        )
        if window is None:
            return 1
        window.events.closing += lambda: _confirm_window_close(proc, window, ui_url)
        window.events.closed += _stop_once
        start(_watch_child, (proc, window, closing))
        return 0
    finally:
        _stop_once()


def main() -> int:
    return run()


if __name__ == "__main__":
    sys.exit(main())
