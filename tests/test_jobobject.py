"""Windows Job Object assignment without creating real processes."""
from __future__ import annotations

import ctypes

import pytest

from worker import jobobject


class FakeProcess:
    _handle = 8765
    pid = 4321


def _fakes(calls):
    def set_information(handle, info_class, pointer, size):
        information = ctypes.cast(
            pointer,
            ctypes.POINTER(jobobject._ExtendedLimitInformation),
        ).contents
        calls.append((
            "configure",
            handle,
            info_class,
            information.BasicLimitInformation.LimitFlags,
            size,
        ))
        return True

    return {
        "create_job": lambda security, name: (
            calls.append(("create", security, name)) or 555
        ),
        "set_information": set_information,
        "assign_to_job": lambda job, process: (
            calls.append(("assign", job, process)) or True
        ),
        "close_handle": lambda handle: calls.append(("close", handle)),
    }


def test_assign_process_creates_a_kill_on_close_job_for_a_borrowed_handle():
    calls = []
    fakes = _fakes(calls)

    handle = jobobject.assign_process(
        FakeProcess(),
        platform="nt",
        open_process=lambda *_args: pytest.fail("borrowed handle should be used"),
        **fakes,
    )

    assert handle == 555
    assert calls[0] == ("create", None, None)
    assert calls[1][0:4] == (
        "configure",
        555,
        jobobject._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
        jobobject._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    )
    assert calls[2] == ("assign", 555, 8765)


def test_assign_process_opens_and_closes_a_pid_handle():
    calls = []
    fakes = _fakes(calls)

    handle = jobobject.assign_process(
        4321,
        platform="nt",
        open_process=lambda access, inherit, pid: (
            calls.append(("open", access, inherit, pid)) or 999
        ),
        **fakes,
    )

    assert handle == 555
    assert calls[0][0] == "open"
    assert calls[0][2:] == (False, 4321)
    assert calls[-1] == ("close", 999)


def test_assign_current_process_retains_one_job_handle(monkeypatch):
    calls = []
    fakes = _fakes(calls)
    monkeypatch.setattr(jobobject, "_current_job_handle", None)

    first = jobobject.assign_current_process(
        platform="nt",
        current_process=lambda: 777,
        **fakes,
    )
    second = jobobject.assign_current_process(
        platform="nt",
        current_process=lambda: pytest.fail("job should be reused"),
        **fakes,
    )

    assert first == 555
    assert second == 555
    assert calls[-1] == ("assign", 555, 777)


def test_job_object_failure_logs_warning_and_continues(caplog):
    with caplog.at_level("WARNING", logger="atlas.jobobject"):
        handle = jobobject.assign_process(
            FakeProcess(),
            platform="nt",
            create_job=lambda *_args: 0,
            set_information=lambda *_args: True,
            assign_to_job=lambda *_args: True,
            open_process=lambda *_args: 999,
            close_handle=lambda _handle: None,
        )

    assert handle is None
    assert "kill-on-close Job Object" in caplog.text


@pytest.mark.parametrize(
    "assign",
    [
        lambda: jobobject.assign_current_process(platform="posix"),
        lambda: jobobject.assign_process(1234, platform="posix"),
    ],
)
def test_non_windows_job_object_calls_warn_and_continue(caplog, assign):
    with caplog.at_level("WARNING", logger="atlas.jobobject"):
        handle = assign()

    assert handle is None
    assert "Job Objects are unavailable" in caplog.text
