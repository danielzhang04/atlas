import ctypes

import pytest

from worker import desktopcontrol


class FakeUser32:
    def __init__(self):
        self.windows = {
            10: {
                "visible": True,
                "title": "Report - Notepad",
                "class": "Notepad",
                "pid": 101,
                "rect": (100, 200, 900, 700),
                "iconic": False,
                "zoomed": False,
            },
            20: {
                "visible": True,
                "title": "Music",
                "class": "Chrome_WidgetWin_0",
                "pid": 202,
                "rect": (900, 100, 1500, 600),
                "iconic": True,
                "zoomed": False,
            },
            30: {
                "visible": False,
                "title": "Hidden",
                "class": "Hidden",
                "pid": 303,
                "rect": (0, 0, 1, 1),
                "iconic": False,
                "zoomed": False,
            },
        }
        self.foreground = 20
        self.foreground_results = [True]
        self.calls = []
        self.inputs = []

    def EnumWindows(self, callback, _param):
        for hwnd in self.windows:
            callback(hwnd, 0)
        return True

    def IsWindowVisible(self, hwnd):
        return self.windows[int(hwnd)]["visible"]

    def GetWindowTextLengthW(self, hwnd):
        return len(self.windows[int(hwnd)]["title"])

    def GetWindowTextW(self, hwnd, buffer, _size):
        buffer.value = self.windows[int(hwnd)]["title"]
        return len(buffer.value)

    def GetClassNameW(self, hwnd, buffer, _size):
        buffer.value = self.windows[int(hwnd)]["class"]
        return len(buffer.value)

    def GetWindowThreadProcessId(self, hwnd, pid_pointer):
        pid_pointer._obj.value = self.windows[int(hwnd)]["pid"]
        return int(hwnd) + 1000

    def GetWindowRect(self, hwnd, rect_pointer):
        rect = rect_pointer._obj
        rect.left, rect.top, rect.right, rect.bottom = self.windows[int(hwnd)]["rect"]
        return True

    def IsIconic(self, hwnd):
        return self.windows[int(hwnd)]["iconic"]

    def IsZoomed(self, hwnd):
        return self.windows[int(hwnd)]["zoomed"]

    def GetForegroundWindow(self):
        return self.foreground

    def SetForegroundWindow(self, hwnd):
        self.calls.append(("foreground", int(hwnd)))
        result = self.foreground_results.pop(0)
        if result:
            self.foreground = int(hwnd)
        return result

    def AttachThreadInput(self, source, target, attach):
        self.calls.append(("attach", int(source), int(target), bool(attach)))
        return True

    def BringWindowToTop(self, hwnd):
        self.calls.append(("top", int(hwnd)))
        return True

    def ShowWindow(self, hwnd, command):
        self.calls.append(("show", int(hwnd), int(command)))
        return True

    def SetWindowPos(self, hwnd, _after, x, y, width, height, flags):
        self.calls.append(
            ("position", int(hwnd), int(x), int(y), int(width), int(height), int(flags))
        )
        return True

    def PostMessageW(self, hwnd, message, wparam, lparam):
        self.calls.append(("post", int(hwnd), int(message), int(wparam), int(lparam)))
        return True

    def SystemParametersInfoW(self, action, _param, rect_pointer, _flags):
        assert action == desktopcontrol.SPI_GETWORKAREA
        rect = rect_pointer._obj
        rect.left, rect.top, rect.right, rect.bottom = (0, 40, 1920, 1080)
        return True

    def SetCursorPos(self, x, y):
        self.calls.append(("cursor", int(x), int(y)))
        return True

    def SendInput(self, count, inputs, _size):
        for index in range(int(count)):
            item = inputs[index]
            if item.type == desktopcontrol.INPUT_KEYBOARD:
                self.inputs.append(
                    ("key", int(item.ki.wVk), int(item.ki.wScan), int(item.ki.dwFlags))
                )
            else:
                self.inputs.append(("mouse", int(item.mi.dwFlags)))
        return count


class FakeKernel32:
    def __init__(self):
        self.paths = {101: "C:/Windows/notepad.exe", 202: "C:/Apps/Spotify.exe"}
        self.closed = []

    def OpenProcess(self, _access, _inherit, pid):
        return int(pid)

    def QueryFullProcessImageNameW(self, handle, _flags, buffer, size_pointer):
        value = self.paths[int(handle)]
        buffer.value = value
        size_pointer._obj.value = len(value)
        return True

    def CloseHandle(self, handle):
        self.closed.append(int(handle))
        return True

    def GetCurrentThreadId(self):
        return 77


def test_window_inventory_is_visible_host_shaped_and_has_no_handles():
    user32 = FakeUser32()
    kernel32 = FakeKernel32()

    inventory = desktopcontrol.list_windows(user32=user32, kernel32=kernel32)

    assert inventory == {"windows": [
        {
            "title": "Report - Notepad",
            "class": "Notepad",
            "pid": 101,
            "process": "notepad.exe",
            "bounds": {"x": 100, "y": 200, "width": 800, "height": 500},
            "state": "normal",
        },
        {
            "title": "Music",
            "class": "Chrome_WidgetWin_0",
            "pid": 202,
            "process": "Spotify.exe",
            "bounds": {"x": 900, "y": 100, "width": 600, "height": 500},
            "state": "minimized",
        },
    ], "total": 2, "truncated": False}
    assert all("handle" not in window for window in inventory["windows"])
    assert kernel32.closed == [101, 202]


def test_window_inventory_has_bounded_limit_and_truncation_metadata():
    user32 = FakeUser32()
    kernel32 = FakeKernel32()

    inventory = desktopcontrol.list_windows(limit=1, user32=user32, kernel32=kernel32)

    assert len(inventory["windows"]) == 1
    assert inventory["total"] == 2
    assert inventory["truncated"] is True
    with pytest.raises(desktopcontrol.DesktopControlError, match="limit"):
        desktopcontrol.list_windows(limit=101, user32=user32, kernel32=kernel32)


def test_resolution_uses_title_or_pid_and_rejects_missing_or_ambiguous_targets():
    user32 = FakeUser32()
    kernel32 = FakeKernel32()
    user32.windows[40] = dict(user32.windows[10], title="Report - Browser", pid=404)
    kernel32.paths[404] = "C:/Browser.exe"

    assert desktopcontrol.resolve_window(pid=202, user32=user32, kernel32=kernel32)["title"] == "Music"
    assert desktopcontrol.resolve_window(
        title="report - notepad", user32=user32, kernel32=kernel32
    )["pid"] == 101
    with pytest.raises(desktopcontrol.DesktopControlError, match="ambiguous"):
        desktopcontrol.resolve_window(title="report", user32=user32, kernel32=kernel32)
    with pytest.raises(desktopcontrol.DesktopControlError, match="title or pid"):
        desktopcontrol.resolve_window(user32=user32, kernel32=kernel32)


def test_focus_uses_attach_thread_input_fallback_without_exposing_a_handle():
    user32 = FakeUser32()
    user32.foreground_results = [False, True]
    kernel32 = FakeKernel32()

    result = desktopcontrol.focus_window(pid=101, user32=user32, kernel32=kernel32)

    assert result == {"focused": "Report - Notepad", "pid": 101}
    assert ("attach", 77, 1020, True) in user32.calls
    assert ("attach", 77, 1020, False) in user32.calls


def test_selected_window_identity_is_focused_without_resolving_ambiguous_pid():
    user32 = FakeUser32()
    kernel32 = FakeKernel32()
    user32.windows[40] = dict(
        user32.windows[10], title="Other document - Notepad",
    )

    selected = desktopcontrol.find_window_by_process_path(
        "C:/Windows/notepad.exe", user32=user32, kernel32=kernel32,
    )
    result = desktopcontrol.focus_resolved_window(
        selected, user32=user32, kernel32=kernel32,
    )

    assert result == {"focused": "Report - Notepad", "pid": 101}
    assert ("foreground", 10) in user32.calls


def test_a_stored_window_is_revalidated_before_it_is_focused():
    """DD-2/F4. An HWND is not a durable name -- Windows recycles them.

    A stored record can be ten minutes old (tools._LAST_OPENED_TTL_S). A dead
    handle already failed safe, because focusing it just failed. A RECYCLED
    one did not: the number now belonged to a different process, and focusing
    it put a stranger's window in front while reporting the stored record's
    own stale title for it.
    """
    user32 = FakeUser32()
    kernel32 = FakeKernel32()

    # Handle 10 is alive, but it now belongs to pid 101 -- not the pid 909
    # this record was stored with. Recycled, so it is refused.
    with pytest.raises(desktopcontrol.DesktopControlError, match="no longer available"):
        desktopcontrol.focus_resolved_window(
            {"_handle": 10, "title": "notes.txt - Notepad", "pid": 909},
            user32=user32, kernel32=kernel32,
        )
    # A handle that is simply gone is refused the same way, and neither
    # refusal touched the foreground.
    with pytest.raises(desktopcontrol.DesktopControlError, match="no longer available"):
        desktopcontrol.focus_resolved_window(
            {"_handle": 9999, "title": "notes.txt - Notepad", "pid": 101},
            user32=user32, kernel32=kernel32,
        )
    assert user32.calls == []


def test_a_revalidated_window_reports_its_live_title_not_the_stored_one():
    """What Atlas speaks back is the window's identity NOW.

    The stored title is a snapshot from up to ten minutes ago. A document
    renamed, or saved under another name, makes it stale -- and reporting a
    stale title as though it were current is the same class of untruth as
    focusing the wrong window.
    """
    user32 = FakeUser32()
    kernel32 = FakeKernel32()
    user32.windows[10]["title"] = "Report FINAL - Notepad"

    result = desktopcontrol.focus_resolved_window(
        {"_handle": 10, "title": "Report - Notepad", "pid": 101},
        user32=user32, kernel32=kernel32,
    )

    assert result == {"focused": "Report FINAL - Notepad", "pid": 101}


def test_window_actions_use_named_work_area_zones_resize_and_wm_close():
    user32 = FakeUser32()
    kernel32 = FakeKernel32()

    desktopcontrol.window_action(
        "move:left-half", title="Notepad", user32=user32, kernel32=kernel32
    )
    desktopcontrol.window_action(
        "resize", pid=101, width=640, height=480, user32=user32, kernel32=kernel32
    )
    desktopcontrol.window_action("close", pid=101, user32=user32, kernel32=kernel32)

    flags = desktopcontrol.SWP_NOZORDER | desktopcontrol.SWP_NOACTIVATE
    assert ("position", 10, 0, 40, 960, 1040, flags) in user32.calls
    assert ("position", 10, 100, 200, 640, 480, flags) in user32.calls
    assert ("post", 10, desktopcontrol.WM_CLOSE, 0, 0) in user32.calls


def test_media_click_typing_and_allowed_chords_emit_sendinput_events():
    user32 = FakeUser32()
    kernel32 = FakeKernel32()

    desktopcontrol.media_key("play_pause", user32=user32)
    desktopcontrol.click(7, 9, pid=101, user32=user32, kernel32=kernel32)
    desktopcontrol.type_text("Hi", user32=user32)
    desktopcontrol.press_keys("ctrl+s", user32=user32)

    assert ("cursor", 107, 209) in user32.calls
    assert ("mouse", desktopcontrol.MOUSEEVENTF_LEFTDOWN) in user32.inputs
    assert ("mouse", desktopcontrol.MOUSEEVENTF_LEFTUP) in user32.inputs
    assert ("key", desktopcontrol.VK_MEDIA_PLAY_PAUSE, 0, 0) in user32.inputs
    assert ("key", 0, ord("H"), desktopcontrol.KEYEVENTF_UNICODE) in user32.inputs
    assert ("key", desktopcontrol.VK_CONTROL, 0, 0) in user32.inputs
    assert ("foreground", 10) in user32.calls


def test_window_relative_click_refuses_minimized_and_out_of_bounds_targets():
    user32 = FakeUser32()
    kernel32 = FakeKernel32()

    with pytest.raises(desktopcontrol.DesktopControlError, match="minimized"):
        desktopcontrol.click(1, 1, pid=202, user32=user32, kernel32=kernel32)
    for x, y in [(-1, 0), (0, -1), (800, 0), (0, 500)]:
        with pytest.raises(desktopcontrol.DesktopControlError, match="outside"):
            desktopcontrol.click(x, y, pid=101, user32=user32, kernel32=kernel32)
    assert user32.inputs == []


def test_window_relative_click_aborts_if_exact_window_loses_focus_before_sendinput():
    class FocusChangingUser32(FakeUser32):
        def SetCursorPos(self, x, y):
            result = super().SetCursorPos(x, y)
            self.foreground = 20
            return result

    user32 = FocusChangingUser32()
    kernel32 = FakeKernel32()

    with pytest.raises(desktopcontrol.DesktopControlError, match="foreground"):
        desktopcontrol.click(7, 9, pid=101, user32=user32, kernel32=kernel32)

    assert user32.inputs == []


@pytest.mark.parametrize(
    "chord", ["delete", "ctrl+d", "ctrl+x", "shift+delete", "alt+f4", "win+r"],
)
def test_press_keys_refuses_delete_and_reserved_chords(chord):
    with pytest.raises(desktopcontrol.DesktopControlError, match="not allowed"):
        desktopcontrol.press_keys(chord, user32=FakeUser32())


def test_delete_chord_requires_exact_foreground_window_identity():
    user32 = FakeUser32()
    kernel32 = FakeKernel32()
    identity = desktopcontrol.focused_window_identity(
        user32=user32, kernel32=kernel32,
    )

    desktopcontrol.press_delete(
        "ctrl+x", expected_hwnd=identity["_handle"], user32=user32,
    )
    user32.foreground = 10
    with pytest.raises(desktopcontrol.DesktopControlError, match="changed"):
        desktopcontrol.press_delete(
            "delete", expected_hwnd=identity["_handle"], user32=user32,
        )

    assert sum(event[0] == "key" for event in user32.inputs) == 4


def test_focused_identity_and_process_path_lookup_remain_host_side():
    user32 = FakeUser32()
    kernel32 = FakeKernel32()

    identity = desktopcontrol.focused_window_identity(
        user32=user32, kernel32=kernel32,
    )
    match = desktopcontrol.find_window_by_process_path(
        "C:/Apps/Spotify.exe", user32=user32, kernel32=kernel32,
    )

    assert identity == {"title": "Music", "pid": 202, "_handle": 20}
    assert match == {"title": "Music", "pid": 202, "_handle": 20}
    assert desktopcontrol.find_window_by_process_path(
        "C:/Unsigned/Spotify.exe", user32=user32, kernel32=kernel32,
    ) is None
    assert desktopcontrol.normalize_chord(" CTRL + X ") == "ctrl+x"
    assert desktopcontrol.MEDIA_KEYS == frozenset({
        "play_pause", "next", "previous", "volume_up", "volume_down", "mute",
    })


def test_process_path_lookup_can_skip_windows_that_were_already_open():
    """The explorer case: one process, many windows, only one of them new."""
    user32 = FakeUser32()
    kernel32 = FakeKernel32()
    user32.windows[40] = dict(user32.windows[20], title="Playlists")
    kernel32.paths[202] = "C:/Apps/Spotify.exe"

    first = desktopcontrol.find_window_by_process_path(
        "C:/Apps/Spotify.exe", user32=user32, kernel32=kernel32,
    )
    fresh = desktopcontrol.find_window_by_process_path(
        "C:/Apps/Spotify.exe", exclude_handles={20},
        user32=user32, kernel32=kernel32,
    )

    assert first["_handle"] == 20
    assert fresh == {"title": "Playlists", "pid": 202, "_handle": 40}
    assert desktopcontrol.find_window_by_process_path(
        "C:/Apps/Spotify.exe", exclude_handles={20, 40},
        user32=user32, kernel32=kernel32,
    ) is None
    with pytest.raises(desktopcontrol.DesktopControlError, match="handle set"):
        desktopcontrol.find_window_by_process_path(
            "C:/Apps/Spotify.exe", exclude_handles={"20"},
            user32=user32, kernel32=kernel32,
        )


def test_visible_window_handles_snapshots_only_visible_top_level_windows():
    user32 = FakeUser32()
    kernel32 = FakeKernel32()

    assert desktopcontrol.visible_window_handles(
        user32=user32, kernel32=kernel32,
    ) == frozenset({10, 20})  # 30 is hidden


def test_focus_new_window_focuses_the_one_window_that_appeared():
    user32 = FakeUser32()
    kernel32 = FakeKernel32()
    before = desktopcontrol.visible_window_handles(user32=user32, kernel32=kernel32)
    slept = []

    def sleep(seconds):
        # The window only shows up on the second poll, which is the real
        # shape: startfile returns long before anything is on screen.
        slept.append(seconds)
        user32.windows[50] = dict(user32.windows[10], title="notes.txt", pid=101)

    focused = desktopcontrol.focus_new_window(
        before, 2.5, expected_process_paths=["C:/Windows/notepad.exe"],
        interval_s=0.15, user32=user32, kernel32=kernel32,
        clock=lambda: 0.0, sleep=sleep,
    )

    assert focused == {"title": "notes.txt", "pid": 101, "_handle": 50}
    assert slept == [0.15]
    assert ("foreground", 50) in user32.calls


def test_focus_new_window_ignores_a_foreign_window_that_appeared_meanwhile():
    """DD-2/F2. A toast from another process is not what the open produced.

    New-since-the-snapshot was never evidence of causation. During the 2.5s
    poll a Teams call toast, a reminder or an updater balloon is entirely
    ordinary -- and with only the freshness diff, that toast got focused,
    `open` reported focused: true about it, and the opened-record observer
    filed the TOAST's title and pid under the opened file's label.

    Here the foreign window is the ONLY new one, so there is nothing else to
    mistake it for: it must simply not be focused, and nothing may be
    returned for the host to record.
    """
    user32 = FakeUser32()
    kernel32 = FakeKernel32()
    before = desktopcontrol.visible_window_handles(user32=user32, kernel32=kernel32)
    # Teams pops a call toast a moment after os.startfile returned.
    user32.windows[70] = dict(user32.windows[10], title="Incoming call", pid=707)
    kernel32.paths[707] = "C:/Apps/Teams.exe"

    # timeout 0: one enumeration lap, then the honest give-up. The toast is
    # already on screen, so nothing here depends on polling.
    assert desktopcontrol.focus_new_window(
        before, 0, expected_process_paths=["C:/Windows/notepad.exe"],
        user32=user32, kernel32=kernel32,
        clock=lambda: 0.0, sleep=lambda _s: None,
    ) is None
    assert user32.calls == []


def test_focus_new_window_still_finds_the_document_behind_a_foreign_toast():
    """A stranger's window must not be able to block the real open either.

    Exactly-one is applied to the IDENTIFIED candidates, not to every new
    HWND on the desktop -- otherwise any background app that pops a toast
    during the poll would silently cost Daniel the focus he asked for.
    """
    user32 = FakeUser32()
    kernel32 = FakeKernel32()
    before = desktopcontrol.visible_window_handles(user32=user32, kernel32=kernel32)
    user32.windows[70] = dict(user32.windows[10], title="Incoming call", pid=707)
    kernel32.paths[707] = "C:/Apps/Teams.exe"
    user32.windows[50] = dict(user32.windows[10], title="notes.txt", pid=101)

    assert desktopcontrol.focus_new_window(
        before, 2.5, expected_process_paths=["C:/Windows/notepad.exe"],
        user32=user32, kernel32=kernel32,
        clock=lambda: 0.0, sleep=lambda _s: None,
    ) == {"title": "notes.txt", "pid": 101, "_handle": 50}
    assert ("foreground", 50) in user32.calls
    assert ("foreground", 70) not in user32.calls


def test_focus_new_window_does_nothing_when_identity_cannot_be_established():
    """No expected process means no attribution, so no focus and no record.

    An `os.startfile` open whose file type has no resolvable handler lands
    here. The file still opens; Atlas just declines to claim it put a window
    in front when it cannot tell which window that would be.
    """
    user32 = FakeUser32()
    kernel32 = FakeKernel32()
    before = desktopcontrol.visible_window_handles(user32=user32, kernel32=kernel32)
    user32.windows[50] = dict(user32.windows[10], title="notes.txt", pid=101)

    for unknown in (None, [], ()):
        assert desktopcontrol.focus_new_window(
            before, 2.5, expected_process_paths=unknown,
            user32=user32, kernel32=kernel32,
            clock=lambda: 0.0, sleep=lambda _s: None,
        ) is None
    assert user32.calls == []
    # And the expected-path set is host-shaped, like every handle set: an
    # executable the model could have named never reaches this.
    with pytest.raises(desktopcontrol.DesktopControlError, match="process path"):
        desktopcontrol.focus_new_window(
            before, 2.5, expected_process_paths="C:/Windows/notepad.exe",
            user32=user32, kernel32=kernel32,
        )
    with pytest.raises(desktopcontrol.DesktopControlError, match="process path"):
        desktopcontrol.focus_new_window(
            before, 2.5, expected_process_paths=["notepad.exe"],
            user32=user32, kernel32=kernel32,
        )


def test_focus_new_window_does_nothing_when_two_new_windows_appeared():
    """Ambiguity never guesses: two candidates means the host cannot tell.

    Guessing here would steal the foreground to an arbitrary window -- a
    splash screen, or something Daniel opened himself a moment earlier.
    """
    user32 = FakeUser32()
    kernel32 = FakeKernel32()
    before = desktopcontrol.visible_window_handles(user32=user32, kernel32=kernel32)
    user32.windows[50] = dict(user32.windows[10], title="notes.txt", pid=101)
    user32.windows[60] = dict(user32.windows[10], title="Splash", pid=101)

    assert desktopcontrol.focus_new_window(
        before, 2.5, expected_process_paths=["C:/Windows/notepad.exe"],
        user32=user32, kernel32=kernel32,
        clock=lambda: 0.0, sleep=lambda _s: None,
    ) is None
    assert user32.calls == []


def test_focus_new_window_gives_up_at_the_deadline_without_focusing_anything():
    user32 = FakeUser32()
    kernel32 = FakeKernel32()
    before = desktopcontrol.visible_window_handles(user32=user32, kernel32=kernel32)
    ticks = iter([0.0, 0.1, 0.2, 2.6, 2.6, 2.6])

    assert desktopcontrol.focus_new_window(
        before, 2.5, expected_process_paths=["C:/Windows/notepad.exe"],
        user32=user32, kernel32=kernel32,
        clock=lambda: next(ticks), sleep=lambda _s: None,
    ) is None
    assert user32.calls == []
    with pytest.raises(desktopcontrol.DesktopControlError, match="focus timeout"):
        desktopcontrol.focus_new_window(
            before, -1, expected_process_paths=["C:/Windows/notepad.exe"],
            user32=user32, kernel32=kernel32,
        )
    with pytest.raises(desktopcontrol.DesktopControlError, match="handle set"):
        desktopcontrol.focus_new_window(
            "10", 2.5, expected_process_paths=["C:/Windows/notepad.exe"],
            user32=user32, kernel32=kernel32,
        )
