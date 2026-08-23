"""Run Atlas as a native pywebview window with a child voice worker."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import logging
import os
from pathlib import Path
from queue import Empty, Queue
import secrets
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
    "create_kill_on_close_job",
    "main",
    "read_ui_url",
    "run",
    "stop_child",
]

ATLAS = Path(__file__).resolve().parents[1]
logger = logging.getLogger("atlas.desktop")
ACTIVE_JOB_STATES = {"queued", "launching", "running"}
ERROR_ALREADY_EXISTS = 183
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
MUTEX_NAME = "Local\\AtlasDesktop"
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


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _kernel32_functions():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
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


def create_kill_on_close_job(
    proc,
    *,
    platform: str = os.name,
    create_job=None,
    set_information=None,
    assign_process=None,
    close_handle=None,
):
    """Assign the worker to a job whose tree dies with the launcher handle."""
    if platform != "nt":
        logger.warning("Windows Job Objects are unavailable on this platform")
        return None
    try:
        if create_job is None or set_information is None or assign_process is None:
            kernel32 = _kernel32_functions()
            create_job = create_job or kernel32.CreateJobObjectW
            set_information = set_information or kernel32.SetInformationJobObject
            assign_process = assign_process or kernel32.AssignProcessToJobObject
            close_handle = close_handle or kernel32.CloseHandle
        job_handle = create_job(None, None)
        if not job_handle:
            raise OSError("CreateJobObjectW failed")
        information = _ExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        configured = set_information(
            job_handle,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(information),
            ctypes.sizeof(information),
        )
        if not configured:
            raise OSError("SetInformationJobObject failed")
        process_handle = getattr(proc, "_handle", None)
        if not process_handle or not assign_process(job_handle, process_handle):
            raise OSError("AssignProcessToJobObject failed")
        return job_handle
    except Exception:
        if "job_handle" in locals() and job_handle:
            try:
                (close_handle or _kernel32_functions().CloseHandle)(job_handle)
            except Exception:
                pass
        logger.warning("could not protect the worker with a kill-on-close Job Object")
        return None


def _create_instance_mutex(
    *,
    platform: str = os.name,
    create_mutex=None,
    get_last_error=None,
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


def _shutdown_url(ui_url: str) -> str:
    parsed = urlsplit(ui_url)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port is None:
        raise ValueError("invalid Atlas UI URL")
    return urlunsplit((parsed.scheme, parsed.netloc, "/shutdown", "", ""))


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


def _taskkill(command: list[str], *, check: bool) -> subprocess.CompletedProcess:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(command, check=check, creationflags=creationflags)


def _request_shutdown(
    ui_url: str,
    shutdown_token: str,
    *,
    opener: Callable = urlopen,
) -> None:
    request = Request(
        _shutdown_url(ui_url),
        data=b"",
        method="POST",
        headers={"X-Atlas-Shutdown": shutdown_token},
    )
    with opener(request, timeout=16.0) as response:
        response.read(65_537)


def stop_child(
    proc,
    ui_url: str | None = None,
    shutdown_token: str | None = None,
    *,
    shutdown_request: Callable[[str, str], None] = _request_shutdown,
    killer: Callable = _taskkill,
) -> None:
    """Request shutdown, then escalate through tree kill and force after bounded waits."""
    if proc.poll() is not None:
        return
    if ui_url and shutdown_token:
        try:
            shutdown_request(ui_url, shutdown_token)
        except Exception:
            logger.warning("the worker did not accept the graceful shutdown request")
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
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
    create_mutex: Callable = _create_instance_mutex,
    show_already_running: Callable = _show_already_running,
    assign_job: Callable = create_kill_on_close_job,
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
    proc = None
    job_handle = None
    ui_url = None
    stop_once = None
    shutdown_token = token_factory()
    try:
        child_env = os.environ.copy()
        child_env["PYTHONUTF8"] = "1"
        child_env["ATLAS_SHUTDOWN_TOKEN"] = shutdown_token
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
        job_handle = assign_job(proc)
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
            terminate(proc, ui_url, shutdown_token)

        stop_once = _stop_once

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
        if stop_once is not None:
            stop_once()
        elif proc is not None:
            terminate(proc, ui_url, shutdown_token)
        close_handle(job_handle)
        close_handle(mutex_handle)


def main() -> int:
    return run()


if __name__ == "__main__":
    sys.exit(main())
