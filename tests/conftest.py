import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class _NoDesktopControl:
    """A desktop with no windows, for every test that did not ask for one.

    Opening a file or folder now ends by focusing the window it produced.
    That is the point of the feature and it is exercised deliberately in the
    tests that care -- but left live everywhere else it would make any test
    that opens something poll the REAL desktop and, worse, hand the
    foreground to whatever window happened to appear while the suite ran.

    Substituted at the lazy `_desktopcontrol()` accessor rather than at each
    wrapper, so the host's own fail-soft wrappers still run for real and stay
    under test. A test that wants different behavior patches the same
    accessor (or a wrapper) itself, and its patch wins.
    """

    from worker.desktopcontrol import DesktopControlError  # the real class

    @staticmethod
    def visible_window_handles():
        return frozenset()

    @staticmethod
    def windows_by_process_path(_path, **_kwargs):
        return []

    @staticmethod
    def find_window_by_process_path(_path, **_kwargs):
        return None

    @staticmethod
    def focus_new_window(_before, *_args, **_kwargs):
        return None


@pytest.fixture(autouse=True)
def no_real_window_focus(monkeypatch):
    for module in ("worker.desktopapps", "worker.localfiles"):
        monkeypatch.setattr(f"{module}._desktopcontrol", _NoDesktopControl)
    # Zero, not a stubbed-out poller: the real _await_profile_window still
    # runs, it just does one lap and gives up, so no test spends 2.5s waiting
    # for a window that a faked Popen was never going to create.
    monkeypatch.setattr("worker.desktopapps._LAUNCH_WINDOW_WAIT_S", 0.0)
