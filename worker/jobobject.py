from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import os
import subprocess
from threading import Lock

__all__ = ["assign_current_process", "assign_process", "kill_process_tree"]
_LOGGER = logging.getLogger("atlas.jobobject")
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
_current_job_handle = None
_current_job_lock = Lock()


def kill_process_tree(
    pid: int,
    *,
    check: bool,
    force: bool = True,
    runner=None,
) -> subprocess.CompletedProcess:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ValueError("pid must be a positive integer")
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    taskkill = os.path.abspath(os.path.join(system_root, "System32", "taskkill.exe"))
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    run = runner or subprocess.run
    argv = [taskkill, "/PID", str(pid), "/T"]
    if force:
        argv.append("/F")
    return run(
        argv,
        check=check,
        creationflags=creationflags,
    )


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
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def assign_current_process(
    *,
    platform: str = os.name,
    create_job=None,
    set_information=None,
    assign_to_job=None,
    current_process=None,
    close_handle=None,
):
    global _current_job_handle
    if platform != "nt":
        _LOGGER.warning("Windows Job Objects are unavailable on this platform")
        return None
    with _current_job_lock:
        if _current_job_handle:
            return _current_job_handle
        try:
            if any(
                function is None
                for function in (
                    create_job,
                    set_information,
                    assign_to_job,
                    current_process,
                    close_handle,
                )
            ):
                kernel32 = _kernel32_functions()
                create_job = create_job or kernel32.CreateJobObjectW
                set_information = set_information or kernel32.SetInformationJobObject
                assign_to_job = assign_to_job or kernel32.AssignProcessToJobObject
                current_process = current_process or kernel32.GetCurrentProcess
                close_handle = close_handle or kernel32.CloseHandle
            process_handle = current_process()
            _current_job_handle = _assign_handle(
                process_handle,
                create_job=create_job,
                set_information=set_information,
                assign_to_job=assign_to_job,
                close_handle=close_handle,
            )
            return _current_job_handle
        except Exception:
            _LOGGER.warning("could not assign the current process to a kill-on-close Job Object")
            return None


def assign_process(
    handle_or_pid,
    *,
    platform: str = os.name,
    create_job=None,
    set_information=None,
    assign_to_job=None,
    open_process=None,
    close_handle=None,
):
    if platform != "nt":
        _LOGGER.warning("Windows Job Objects are unavailable on this platform")
        return None
    owned_process_handle = None
    try:
        if any(
            function is None
            for function in (
                create_job,
                set_information,
                assign_to_job,
                open_process,
                close_handle,
            )
        ):
            kernel32 = _kernel32_functions()
            create_job = create_job or kernel32.CreateJobObjectW
            set_information = set_information or kernel32.SetInformationJobObject
            assign_to_job = assign_to_job or kernel32.AssignProcessToJobObject
            open_process = open_process or kernel32.OpenProcess
            close_handle = close_handle or kernel32.CloseHandle
        process_handle = getattr(handle_or_pid, "_handle", None)
        if not process_handle:
            pid = getattr(handle_or_pid, "pid", handle_or_pid)
            owned_process_handle = open_process(
                _PROCESS_SET_QUOTA | _PROCESS_TERMINATE,
                False,
                int(pid),
            )
            if not owned_process_handle:
                raise OSError("OpenProcess failed")
            process_handle = owned_process_handle
        return _assign_handle(
            process_handle,
            create_job=create_job,
            set_information=set_information,
            assign_to_job=assign_to_job,
            close_handle=close_handle,
        )
    except Exception:
        _LOGGER.warning("could not assign the process to a kill-on-close Job Object")
        return None
    finally:
        if owned_process_handle:
            try:
                close_handle(owned_process_handle)
            except Exception:
                _LOGGER.warning("could not close a Windows process handle")


def _assign_handle(
    process_handle,
    *,
    create_job,
    set_information,
    assign_to_job,
    close_handle,
):
    job_handle = create_job(None, None)
    if not job_handle:
        raise OSError("CreateJobObjectW failed")
    try:
        information = _ExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = (
            _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | _JOB_OBJECT_LIMIT_BREAKAWAY_OK
        )
        configured = set_information(
            job_handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(information),
            ctypes.sizeof(information),
        )
        if not configured:
            raise OSError("SetInformationJobObject failed")
        if not assign_to_job(job_handle, process_handle):
            raise OSError("AssignProcessToJobObject failed")
        return job_handle
    except Exception:
        close_handle(job_handle)
        raise
