"""Offline contracts for the read-only native Windows app mirror.

Every Win32 call is faked, so these run (and mean the same thing) on any host.
"""

import ctypes
import os
import struct
import sys
import threading
import zlib

import pytest

sys.path.insert(0, os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "console")))

import winapps


class FakeUser32:
    """One top-level window; each knob flips a single filter."""

    def __init__(self, *, style=winapps.WS_CAPTION, ex_style=0, class_name="ApplicationFrameWindow",
                 visible=True, iconic=False, root=None, rect=(10, 20, 330, 260), above=0, thread_id=99):
        self.style, self.ex_style, self.class_name = style, ex_style, class_name
        self.visible, self.iconic, self.root, self.rect = visible, iconic, root, rect
        self.above = above
        self.thread_id = thread_id
        self.capture_messages = []
        self.desktop_dc_requests = 0

    def IsWindowVisible(self, _hwnd):
        return self.visible

    def IsIconic(self, _hwnd):
        return self.iconic

    def GetWindowLongPtrW(self, _hwnd, index):
        return self.style if index == -16 else self.ex_style

    def GetWindowThreadProcessId(self, _hwnd, out_pid):
        if out_pid is not None:
            out_pid.value = 42
        return self.thread_id

    def GetWindowTextLengthW(self, _hwnd):
        return 5

    def GetWindowTextW(self, _hwnd, buffer, _length):
        buffer.value = "hello"
        return 5

    def GetClassNameW(self, _hwnd, buffer, _length):
        buffer.value = self.class_name
        return len(self.class_name)

    def GetWindowRect(self, _hwnd, out_rect):
        out_rect.left, out_rect.top, out_rect.right, out_rect.bottom = self.rect
        return True

    def EnumWindows(self, proc, _lparam):
        return proc(1234, 0)

    def GetAncestor(self, hwnd, _flag):
        return hwnd if self.root is None else self.root

    def GetWindow(self, hwnd, _command):
        return self.above if hwnd == 1234 else 0

    def GetWindowDC(self, _hwnd):
        return 8

    def GetDC(self, hwnd):
        assert hwnd == 0
        self.desktop_dc_requests += 1
        return 9

    def ReleaseDC(self, _hwnd, _dc):
        return 1

    def SendMessageTimeoutW(self, hwnd, message, _dc, flags, options, timeout, _result):
        self.capture_messages.append((hwnd, message, flags, options, timeout))
        return 0


class FakeGdi:
    def __init__(self):
        self.desktop_blits = 0

    def CreateCompatibleDC(self, _dc):
        return 10

    def CreateCompatibleBitmap(self, _dc, _width, _height):
        return 11

    def SelectObject(self, _dc, _bitmap):
        return 12

    def PatBlt(self, *_args):
        return True

    def BitBlt(self, *_args):
        self.desktop_blits += 1
        return True

    def GetDIBits(self, _dc, _bitmap, _start, height, pixels, _info, _usage):
        ctypes.memset(pixels, 0, 320 * 240 * 4)
        return height

    def DeleteObject(self, _bitmap):
        return True

    def DeleteDC(self, _dc):
        return True


class FakeKernel32:
    def __init__(self, session=1):
        self.session = session

    def OpenProcess(self, _access, _inherit, _pid):
        return 1

    def QueryFullProcessImageNameW(self, _handle, _flags, buffer, out_size):
        buffer.value = "C:/Some/App/Example.exe"
        out_size.value = len("C:/Some/App/Example.exe")
        return True

    def CloseHandle(self, _handle):
        return True

    def GetCurrentProcess(self):
        return 7

    def GetCurrentProcessId(self):
        return 4242

    def ProcessIdToSessionId(self, _pid, out_session):
        out_session._obj.value = self.session
        return True


class FakeDwm:
    def __init__(self, cloaked=0):
        self.cloaked = cloaked

    def DwmGetWindowAttribute(self, _hwnd, _attribute, out_value, _size):
        out_value._obj.value = self.cloaked
        return 0


class FakeAdvapi32:
    """`elevated=None` models Windows refusing the token query."""

    def __init__(self, elevated=False):
        self.elevated = elevated

    def OpenProcessToken(self, _process, _access, _out_token):
        return self.elevated is not None

    def GetTokenInformation(self, _token, _cls, out_info, _size, _out_returned):
        out_info._obj.value = int(bool(self.elevated))
        return True


@pytest.fixture
def host(monkeypatch):
    """Install hermetic fakes as if on Windows; tests swap individual pieces."""
    monkeypatch.setattr(winapps.sys, "platform", "win32")
    monkeypatch.setattr(winapps, "_user32", FakeUser32())
    monkeypatch.setattr(winapps, "_gdi32", object())
    monkeypatch.setattr(winapps, "_kernel32", FakeKernel32())
    monkeypatch.setattr(winapps, "_dwmapi", FakeDwm())
    monkeypatch.setattr(winapps, "_advapi32", FakeAdvapi32())
    return monkeypatch


def test_non_windows_hosts_report_native_availability(monkeypatch):
    monkeypatch.setattr(winapps.sys, "platform", "linux")
    result = winapps.winapps_windows()
    assert result["available"] is False and result["windows"] == []
    assert "Windows host" in result["reason"]


def test_non_windows_hosts_refuse_screenshots(monkeypatch):
    monkeypatch.setattr(winapps.sys, "platform", "linux")
    with pytest.raises(winapps.WinAppError, match="Windows host"):
        winapps.winapps_window_screenshot(1234)


def test_invalid_window_ids_are_refused(host):
    with pytest.raises(winapps.WinAppError, match="Invalid window id"):
        winapps.winapps_window_screenshot("not-a-window")


@pytest.mark.parametrize("window_id", ["0", "-5", str(2**32), str(2**64), str(2**80), "1e3", ""])
def test_out_of_range_window_ids_are_refused_before_ctypes(host, window_id):
    # An id ctypes cannot convert to an HWND used to escape as an unhandled error (HTTP 500).
    with pytest.raises(winapps.WinAppError, match="Invalid window id"):
        winapps.winapps_window_screenshot(window_id)


def test_a_captioned_window_is_listed_with_its_process(host):
    windows = winapps._win_windows()
    assert len(windows) == 1
    assert windows[0]["windowId"] == 1234
    assert windows[0]["app"] == "Example"
    assert windows[0]["title"] == "hello"
    assert windows[0]["className"] == "ApplicationFrameWindow"
    assert (windows[0]["width"], windows[0]["height"]) == (320, 240)
    assert windows[0]["elevated"] is False


@pytest.mark.parametrize("user32", [
    FakeUser32(style=0),                                     # no caption
    FakeUser32(ex_style=winapps.WS_EX_TOOLWINDOW),           # tool window
    FakeUser32(class_name="Shell_TrayWnd"),                  # known shell class
    FakeUser32(visible=False),                               # hidden
    FakeUser32(iconic=True),                                 # minimized
    FakeUser32(rect=(0, 0, 0, 0)),                           # empty rect
])
def test_shell_tool_hidden_and_minimized_windows_are_not_listed(host, user32):
    host.setattr(winapps, "_user32", user32)
    assert winapps._win_windows() == []


def test_cloaked_windows_are_not_listed(host):
    # Suspended UWP frames stay "visible" to Win32 but are not on screen.
    host.setattr(winapps, "_dwmapi", FakeDwm(cloaked=2))
    assert winapps._win_windows() == []


def test_elevation_is_reported_and_unknown_is_not_guessed(host):
    host.setattr(winapps, "_advapi32", FakeAdvapi32(elevated=True))
    assert winapps._win_windows()[0]["elevated"] is True
    host.setattr(winapps, "_advapi32", FakeAdvapi32(elevated=None))
    assert winapps._win_windows()[0]["elevated"] is None


def test_a_stale_window_id_is_refused(host):
    host.setattr(winapps, "_user32", FakeUser32(root=999))  # no longer its own root
    with pytest.raises(winapps.WinAppError, match="not on screen"):
        winapps.winapps_window_screenshot(1234)
    host.setattr(winapps, "_user32", FakeUser32(visible=False))
    with pytest.raises(winapps.WinAppError, match="not on screen"):
        winapps.winapps_window_screenshot(1234)


def test_window_stream_yields_frames_and_stops_when_the_window_disappears(host, monkeypatch):
    calls = []
    png = winapps._png(1, 1, bytes(4))

    def capture(window_id):
        calls.append(window_id)
        if len(calls) == 1:
            return png
        raise winapps.WinAppError("That window is not on screen anymore")

    monkeypatch.setattr(winapps, "winapps_window_screenshot", capture)
    monkeypatch.setattr(winapps.time, "sleep", lambda _seconds: None)
    assert list(winapps.winapps_window_stream(1234, 2.0)) == [png]
    assert calls == [1234, 1234]


@pytest.mark.parametrize("fps", [0, -1, 0.49, 8.01, float("nan"), float("inf")])
def test_window_stream_rejects_unbounded_frame_rates(host, fps):
    with pytest.raises(winapps.WinAppError, match="Frame rate"):
        next(winapps.winapps_window_stream(1234, fps))


def test_window_stream_refuses_a_stale_hwnd_before_the_first_frame(host, monkeypatch):
    def stale(_window_id):
        raise winapps.WinAppError("That window is not on screen anymore")

    monkeypatch.setattr(winapps, "winapps_window_screenshot", stale)
    with pytest.raises(winapps.WinAppError, match="not on screen"):
        next(winapps.winapps_window_stream(1234, 2.0))


def test_window_stream_stops_when_hwnd_is_reused_for_another_window(host, monkeypatch):
    png = winapps._png(1, 1, bytes(4))
    window = {"pid": 42, "app": "Example", "className": "Editor", "title": "first"}
    captures = []
    monkeypatch.setattr(winapps, "_validated_window", lambda _window_id: (1234, dict(window)))
    monkeypatch.setattr(winapps, "winapps_window_screenshot", lambda hwnd: captures.append(hwnd) or png)
    monkeypatch.setattr(winapps.time, "sleep", lambda _seconds: None)

    stream = winapps.winapps_window_stream(1234, 2.0)
    assert next(stream) == png
    window["pid"] = 43
    with pytest.raises(StopIteration):
        next(stream)
    assert captures == [1234]


def test_window_stream_stops_when_hwnd_is_reused_on_another_thread(host, monkeypatch):
    png = winapps._png(1, 1, bytes(4))
    window = {"pid": 42, "app": "Example", "className": "Editor", "title": "same"}
    user32 = winapps._user32
    captures = []
    monkeypatch.setattr(winapps, "_validated_window", lambda _window_id: (1234, dict(window)))
    monkeypatch.setattr(winapps, "winapps_window_screenshot", lambda hwnd: captures.append(hwnd) or png)
    monkeypatch.setattr(winapps.time, "sleep", lambda _seconds: None)

    stream = winapps.winapps_window_stream(1234, 2.0)
    assert next(stream) == png
    user32.thread_id += 1
    with pytest.raises(StopIteration):
        next(stream)
    assert captures == [1234]


def test_concurrent_window_streams_are_limited_and_release_their_slot(host, monkeypatch):
    png = winapps._png(1, 1, bytes(4))
    window = {"pid": 42, "app": "Example", "className": "Editor", "title": "first"}
    monkeypatch.setattr(winapps, "_STREAM_SLOTS", threading.BoundedSemaphore(1))
    monkeypatch.setattr(winapps, "_validated_window", lambda _window_id: (1234, window))
    monkeypatch.setattr(winapps, "winapps_window_screenshot", lambda _hwnd: png)
    monkeypatch.setattr(winapps.time, "sleep", lambda _seconds: None)

    active = winapps.winapps_window_stream(1234, 2.0)
    assert next(active) == png
    rejected = winapps.winapps_window_stream(1234, 2.0)
    with pytest.raises(winapps.WinAppError, match="maximum number") as caught:
        next(rejected)
    assert caught.value.status == 429
    active.close()

    resumed = winapps.winapps_window_stream(1234, 2.0)
    assert next(resumed) == png
    resumed.close()


def test_hung_window_capture_uses_a_bounded_print_and_desktop_fallback(host, monkeypatch):
    user32 = FakeUser32()
    gdi32 = FakeGdi()
    host.setattr(winapps, "_user32", user32)
    host.setattr(winapps, "_gdi32", gdi32)

    result = winapps.winapps_window_screenshot(1234)

    winapps.validate_png_frame(result)
    assert user32.capture_messages == [
        (1234, winapps.WM_PRINT,
         winapps.PRF_CHECKVISIBLE | winapps.PRF_NONCLIENT | winapps.PRF_CLIENT
         | winapps.PRF_ERASEBKGND | winapps.PRF_CHILDREN,
         winapps.SMTO_BLOCK | winapps.SMTO_ABORTIFHUNG, winapps.CAPTURE_MESSAGE_TIMEOUT_MS)
    ]
    assert user32.desktop_dc_requests == 1 and gdi32.desktop_blits == 1


def test_hung_covered_window_fails_without_copying_other_windows(host, monkeypatch):
    user32 = FakeUser32(above=5678)
    gdi32 = FakeGdi()
    host.setattr(winapps, "_user32", user32)
    host.setattr(winapps, "_gdi32", gdi32)

    with pytest.raises(winapps.WinAppError, match="covered") as caught:
        winapps.winapps_window_screenshot(1234)

    assert caught.value.status == 504
    assert user32.desktop_dc_requests == 0 and gdi32.desktop_blits == 0


def test_png_frame_validation_rejects_bad_crc_and_oversized_dimensions():
    png = bytearray(winapps._png(1, 1, bytes(4)))
    png[-5] ^= 0x01
    with pytest.raises(winapps.WinAppError, match="corrupt PNG"):
        winapps.validate_png_frame(bytes(png))

    oversized = bytearray(winapps._png(1, 1, bytes(4)))
    oversized[16:24] = struct.pack(">II", winapps.MAX_CAPTURE_PIXELS + 1, 1)
    oversized[29:33] = struct.pack(">I", zlib.crc32(b"IHDR" + oversized[16:29]))
    with pytest.raises(winapps.WinAppError, match="dimensions"):
        winapps.validate_png_frame(bytes(oversized))


def test_oversized_windows_are_refused_before_any_capture(host):
    host.setattr(winapps, "_user32", FakeUser32(rect=(0, 0, 20000, 20000)))
    with pytest.raises(winapps.WinAppError, match="too large"):
        winapps.winapps_window_screenshot(1234)


def _chunks(png):
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    position = 8
    while position < len(png):
        length, kind = struct.unpack(">I4s", png[position:position + 8])
        body = png[position + 8:position + 8 + length]
        (crc,) = struct.unpack(">I", png[position + 8 + length:position + 12 + length])
        assert crc == zlib.crc32(kind + body), f"bad CRC in {kind!r}"
        yield kind, body
        position += 12 + length


def test_png_encoder_round_trips_a_bgra_buffer():
    # 2x2, BGRA, alpha deliberately 0 as GDI often leaves it.
    bgra = bytes([
        10, 20, 30, 0,    40, 50, 60, 0,
        70, 80, 90, 0,    100, 110, 120, 0,
    ])
    chunks = list(_chunks(winapps._png(2, 2, bgra)))
    assert [kind for kind, _ in chunks] == [b"IHDR", b"IDAT", b"IEND"]
    assert struct.unpack(">IIBBBBB", chunks[0][1]) == (2, 2, 8, 6, 0, 0, 0)
    raw = zlib.decompress(chunks[1][1])
    stride = 1 + 2 * 4
    assert len(raw) == 2 * stride
    assert raw[0] == 0 and raw[stride] == 0  # filter type None on every row
    assert raw[1:stride] == bytes([30, 20, 10, 255, 60, 50, 40, 255])      # RGBA, opaque
    assert raw[stride + 1:] == bytes([90, 80, 70, 255, 120, 110, 100, 255])


def test_doctor_on_a_non_windows_host(monkeypatch):
    monkeypatch.setattr(winapps.sys, "platform", "linux")
    result = winapps.doctor()
    assert result["available"] is False and result["capture"] is False
    assert result["elevated"] is None and "Windows host" in result["reason"]


def test_doctor_reports_session_elevation_and_a_capture_probe(host):
    host.setattr(winapps, "_user32", type("U", (FakeUser32,), {"GetShellWindow": lambda self: 1})())
    host.setattr(winapps, "_advapi32", FakeAdvapi32(elevated=True))
    host.setattr(winapps, "winapps_window_screenshot", lambda _hwnd: b"\x89PNG\r\n\x1a\n")
    result = winapps.doctor()
    assert result["desktop"] is True and result["sessionId"] == 1
    assert result["elevated"] is True
    assert result["windows"] == 1 and result["capture"] is True and result["reason"] is None


def test_doctor_refuses_to_probe_without_an_interactive_desktop(host):
    host.setattr(winapps, "_kernel32", FakeKernel32(session=0))
    result = winapps.doctor()
    assert result["desktop"] is False and result["available"] is False
    assert "interactive desktop" in result["reason"]
