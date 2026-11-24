"""Native Windows app mirror: enumerate visible windows and capture one window.

The macOS Lab uses CoreGraphics through ctypes; this is the Windows counterpart.
It uses a bounded WM_PRINT request with a visibility-checked desktop fallback,
stays standard-library ctypes only, needs no elevated process, and never exposes
process command lines or performs input control.
"""

from __future__ import annotations

import ctypes
import math
import struct
import sys
import threading
import time
import zlib
from ctypes import wintypes
from pathlib import Path
from typing import Any, Iterator

PW_RENDERFULLCONTENT = 0x00000002
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
GA_ROOT = 2
WS_CAPTION = 0x00C00000
WS_EX_TOOLWINDOW = 0x00000080
DWMWA_CLOAKED = 14
TOKEN_QUERY = 0x0008
TOKEN_ELEVATION = 20  # TOKEN_INFORMATION_CLASS.TokenElevation
MAX_TITLE_CHARS = 512
MAX_WINDOW_CHARS = 200
# 8K at 32 bits is ~130 MB of pixels; anything larger is refused instead of exhausting memory.
MAX_CAPTURE_PIXELS = 7680 * 4320
MAX_PNG_FRAME_BYTES = MAX_CAPTURE_PIXELS * 4 + 1024 * 1024
MIN_STREAM_FPS = 0.5
MAX_STREAM_FPS = 8.0
DEFAULT_STREAM_FPS = 1.2
MAX_STREAM_SECONDS = 600
MAX_ACTIVE_STREAMS = 2
CAPTURE_MESSAGE_TIMEOUT_MS = 1500
WM_PRINT = 0x0317
PRF_CHECKVISIBLE = 0x00000001
PRF_NONCLIENT = 0x00000002
PRF_CLIENT = 0x00000004
PRF_ERASEBKGND = 0x00000008
PRF_CHILDREN = 0x00000010
SMTO_BLOCK = 0x00000001
SMTO_ABORTIFHUNG = 0x00000002
GW_HWNDPREV = 3
TOOL_CLASSES = {
    "Shell_TrayWnd",
    "Shell_SecondaryTrayWnd",
    "ThumbnailDeviceHelperWnd",
    "PseudoConsoleWindow",
}


class WinAppError(Exception):
    """A safe, user-presentable native window inventory/capture failure."""

    def __init__(self, message: str, *, status: int = 400):
        super().__init__(message)
        self.status = status


# Keep the Win32 data layout available to the offline tests on other hosts.
class _RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", ctypes.c_ubyte * 4)]


_ENUM_WINDOWS_PROC = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(
    wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


if sys.platform == "win32":
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    _user32.EnumWindows.argtypes = [_ENUM_WINDOWS_PROC, wintypes.LPARAM]
    _user32.EnumWindows.restype = wintypes.BOOL
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.IsWindowVisible.restype = wintypes.BOOL
    _user32.IsIconic.argtypes = [wintypes.HWND]
    _user32.IsIconic.restype = wintypes.BOOL
    _user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(_RECT)]
    _user32.GetWindowRect.restype = wintypes.BOOL
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    if not hasattr(_user32, "GetWindowLongPtrW"):  # 32-bit user32 exports only GetWindowLongW
        _user32.GetWindowLongPtrW = _user32.GetWindowLongW
    _user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.GetWindowTextLengthW.restype = ctypes.c_int
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
    _user32.GetWindowTextW.restype = ctypes.c_int
    _user32.GetClassNameW.argtypes = [wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
    _user32.GetClassNameW.restype = ctypes.c_int
    _user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    _user32.GetAncestor.restype = wintypes.HWND
    _user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    _user32.GetWindow.restype = wintypes.HWND
    _user32.GetDC.argtypes = [wintypes.HWND]
    _user32.GetDC.restype = wintypes.HDC
    _user32.GetWindowDC.argtypes = [wintypes.HWND]
    _user32.GetWindowDC.restype = wintypes.HDC
    _user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    _user32.ReleaseDC.restype = ctypes.c_int
    _user32.SendMessageTimeoutW.argtypes = [
        wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t,
        wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
    ]
    _user32.SendMessageTimeoutW.restype = ctypes.c_size_t
    if hasattr(_user32, "SetProcessDpiAwarenessContext"):  # Windows 10 1703+; older hosts use Shcore
        _user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        _user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL

    _gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    _gdi32.CreateCompatibleDC.restype = wintypes.HDC
    _gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    _gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    _gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HBITMAP]
    _gdi32.SelectObject.restype = wintypes.HBITMAP
    _gdi32.DeleteObject.argtypes = [wintypes.HBITMAP]
    _gdi32.DeleteObject.restype = wintypes.BOOL
    _gdi32.DeleteDC.argtypes = [wintypes.HDC]
    _gdi32.DeleteDC.restype = wintypes.BOOL
    _gdi32.GetDIBits.argtypes = [
        wintypes.HDC, wintypes.HBITMAP, ctypes.c_uint, ctypes.c_uint,
        ctypes.c_void_p, ctypes.POINTER(_BITMAPINFO), ctypes.c_uint,
    ]
    _gdi32.GetDIBits.restype = ctypes.c_int
    _gdi32.BitBlt.argtypes = [
        wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD,
    ]
    _gdi32.BitBlt.restype = wintypes.BOOL
    _gdi32.PatBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    _gdi32.PatBlt.restype = wintypes.BOOL

    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.c_wchar_p, ctypes.POINTER(wintypes.DWORD)
    ]
    _kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.GetCurrentProcess.argtypes = []
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.GetCurrentProcessId.argtypes = []
    _kernel32.GetCurrentProcessId.restype = wintypes.DWORD
    _kernel32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.ProcessIdToSessionId.restype = wintypes.BOOL
    _user32.GetShellWindow.argtypes = []
    _user32.GetShellWindow.restype = wintypes.HWND

    _dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    _dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long  # HRESULT

    _advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    _advapi32.OpenProcessToken.restype = wintypes.BOOL
    _advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)
    ]
    _advapi32.GetTokenInformation.restype = wintypes.BOOL
else:
    _user32 = _gdi32 = _kernel32 = _dwmapi = _advapi32 = None

_STREAM_SLOTS = threading.BoundedSemaphore(MAX_ACTIVE_STREAMS)


def _set_dpi_awareness() -> None:
    """Set per-monitor DPI awareness once, so coordinates stay physical pixels."""
    if sys.platform != "win32":
        return
    if _user32 is not None and hasattr(_user32, "SetProcessDpiAwarenessContext") and (
        _user32.SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
    ):
        return
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        # The Shcore API only knows PROCESS_PER_MONITOR_DPI_AWARE (2), i.e. v1 per-monitor awareness.
        shcore.SetProcessDpiAwareness(2)
    except (OSError, AttributeError):
        pass


_set_dpi_awareness()


def unavailable(reason: str) -> dict[str, Any]:
    return {"available": False, "reason": reason, "windows": []}


def _token_elevated(process: Any) -> bool | None:
    """Whether a process handle's token is elevated; None when Windows will not tell us.

    A standard-user console may be refused the token of an elevated process, so None is an
    honest "unknown" and never a silent False.
    """
    if _advapi32 is None:
        return None
    token = wintypes.HANDLE()
    if not _advapi32.OpenProcessToken(process, TOKEN_QUERY, ctypes.byref(token)):
        return None
    try:
        elevation = wintypes.DWORD()
        returned = wintypes.DWORD()
        if not _advapi32.GetTokenInformation(
            token, TOKEN_ELEVATION, ctypes.byref(elevation), ctypes.sizeof(elevation), ctypes.byref(returned)
        ):
            return None
        return bool(elevation.value)
    finally:
        _kernel32.CloseHandle(token)


def _process_info(pid: int) -> tuple[str, bool | None]:
    """The executable basename and elevation for one pid, without command-line data."""
    if _kernel32 is None:
        return "", None
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return "", None
    try:
        size = wintypes.DWORD(1024)
        path = ctypes.create_unicode_buffer(1024)
        if not _kernel32.QueryFullProcessImageNameW(handle, 0, path, size):
            return "", None
        return Path(path.value).stem, _token_elevated(handle)
    finally:
        _kernel32.CloseHandle(handle)


def _cloaked(hwnd: int) -> bool:
    """DWM-cloaked windows are 'visible' to Win32 but not on screen (suspended UWP, other desktops)."""
    if _dwmapi is None:
        return False
    cloaked = wintypes.DWORD()
    if _dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)) != 0:
        return False
    return bool(cloaked.value)


def _window(hwnd: int) -> dict[str, Any] | None:
    """One on-screen, non-iconic top-level window, or None for shell/tool/cloaked windows."""
    if _user32 is None or not _user32.IsWindowVisible(hwnd) or _user32.IsIconic(hwnd):
        return None
    if _cloaked(hwnd):
        return None
    style = int(_user32.GetWindowLongPtrW(hwnd, -16)) & 0xFFFFFFFF
    if not style & WS_CAPTION:
        return None
    ex_style = int(_user32.GetWindowLongPtrW(hwnd, -20)) & 0xFFFFFFFF
    if ex_style & WS_EX_TOOLWINDOW:
        return None
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, pid)
    if not pid.value:
        return None
    length = _user32.GetWindowTextLengthW(hwnd)
    title = ctypes.create_unicode_buffer(max(1, length + 1))
    if length:
        _user32.GetWindowTextW(hwnd, title, length + 1)
    class_name = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, class_name, 256)
    if not class_name.value or class_name.value in TOOL_CLASSES:
        return None
    rect = _RECT()
    if not _user32.GetWindowRect(hwnd, rect):
        return None
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return None
    app, elevated = _process_info(pid.value)
    if not app:
        return None
    return {
        "windowId": int(hwnd),
        "pid": int(pid.value),
        "app": app[:MAX_WINDOW_CHARS],
        "elevated": elevated,
        "title": title.value[:MAX_TITLE_CHARS],
        "className": class_name.value[:MAX_WINDOW_CHARS],
        "x": int(rect.left),
        "y": int(rect.top),
        "width": int(width),
        "height": int(height),
    }


def _win_windows() -> list[dict[str, Any]]:
    """Enumerate visible top-level windows with ctypes; no Screen Recording equivalent is needed."""
    if sys.platform != "win32":
        raise WinAppError("Native Windows window inventory needs a Windows host")
    windows: list[dict[str, Any]] = []
    seen: set[int] = set()

    def callback(hwnd: int, _lparam: int) -> bool:
        # Tool windows often have no visible owner, so accept only their own root window.
        if _user32 is not None and _user32.GetAncestor(hwnd, GA_ROOT) == hwnd:
            window = _window(hwnd)
            if window and window["windowId"] not in seen:
                seen.add(window["windowId"])
                windows.append(window)
        return True

    if not _user32.EnumWindows(_ENUM_WINDOWS_PROC(callback), 0):
        raise WinAppError("Windows window enumeration failed")
    windows.sort(key=lambda window: (window["app"].casefold(), window["title"].casefold(), window["windowId"]))
    return windows


def winapps_windows() -> dict[str, Any]:
    """The panel's read-only window inventory."""
    if sys.platform != "win32":
        return unavailable("Native Windows app mirror needs a Windows host")
    try:
        windows = _win_windows()
    except WinAppError as exc:
        return unavailable(str(exc))
    return {"available": True, "reason": None, "windows": windows}


def _png(width: int, height: int, bgra: bytes) -> bytes:
    """Encode a 32-bit BGRA buffer as an opaque RGBA PNG with only the standard library.

    GDI drawing into a 32-bit bitmap routinely leaves the alpha byte at 0, which a browser would
    show as an empty image, so a window capture is always written fully opaque.
    """
    rgba = bytearray(bgra)
    rgba[0::4] = bgra[2::4]  # slice assignment keeps the channel swap in C, not a per-pixel loop
    rgba[2::4] = bgra[0::4]
    rgba[3::4] = b"\xff" * (width * height)
    stride = width * 4
    raw = bytearray()
    for row in range(height):
        raw.append(0)  # filter type 0 (None)
        raw += rgba[row * stride:(row + 1) * stride]

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(raw), 3)) + chunk(b"IEND", b"")


def validate_png_frame(frame: bytes) -> None:
    """Reject malformed or oversized capture frames before they reach a stream client."""
    if not isinstance(frame, bytes) or len(frame) > MAX_PNG_FRAME_BYTES or len(frame) < 57:
        raise WinAppError("The Windows window capture returned an invalid PNG frame", status=502)
    if frame[:8] != b"\x89PNG\r\n\x1a\n":
        raise WinAppError("The Windows window capture returned an invalid PNG frame", status=502)
    position = 8
    width = height = 0
    seen_ihdr = seen_idat = seen_iend = False
    while position < len(frame):
        if position + 12 > len(frame):
            raise WinAppError("The Windows window capture returned an invalid PNG frame", status=502)
        length = struct.unpack(">I", frame[position:position + 4])[0]
        kind = frame[position + 4:position + 8]
        end = position + 12 + length
        if end > len(frame):
            raise WinAppError("The Windows window capture returned an invalid PNG frame", status=502)
        payload = frame[position + 8:position + 8 + length]
        checksum = struct.unpack(">I", frame[position + 8 + length:end])[0]
        if checksum != zlib.crc32(kind + payload):
            raise WinAppError("The Windows window capture returned a corrupt PNG frame", status=502)
        if not seen_ihdr:
            if kind != b"IHDR" or length != 13:
                raise WinAppError("The Windows window capture returned an invalid PNG header", status=502)
            width, height = struct.unpack(">II", payload[:8])
            if width <= 0 or height <= 0 or width * height > MAX_CAPTURE_PIXELS:
                raise WinAppError("The Windows window capture returned invalid PNG dimensions", status=502)
            seen_ihdr = True
        elif kind == b"IHDR":
            raise WinAppError("The Windows window capture returned an invalid PNG header", status=502)
        if kind == b"IDAT":
            seen_idat = True
        if kind == b"IEND":
            if length != 0 or end != len(frame):
                raise WinAppError("The Windows window capture returned an invalid PNG end marker", status=502)
            seen_iend = True
            break
        position = end
    if not (seen_ihdr and seen_idat and seen_iend):
        raise WinAppError("The Windows window capture returned an incomplete PNG frame", status=502)


def _window_unoccluded(hwnd: int, window: dict[str, Any]) -> bool:
    """Only allow desktop-pixel fallback when no higher window overlaps the target."""
    if _user32 is None or not hasattr(_user32, "GetWindow"):
        return False
    left, top = window["x"], window["y"]
    right, bottom = left + window["width"], top + window["height"]
    above = _user32.GetWindow(hwnd, GW_HWNDPREV)
    for _ in range(512):
        if not above:
            return True
        if _user32.IsWindowVisible(above) and not _user32.IsIconic(above):
            rect = _RECT()
            if _user32.GetWindowRect(above, rect):
                if max(left, rect.left) < min(right, rect.right) and max(top, rect.top) < min(bottom, rect.bottom):
                    return False
        above = _user32.GetWindow(above, GW_HWNDPREV)
    return not above


def _send_bounded_window_print(hwnd: int, memory_dc: Any) -> bool:
    """Ask the target to paint through a bounded WM_PRINT instead of blocking in PrintWindow."""
    result = ctypes.c_size_t()
    flags = PRF_CHECKVISIBLE | PRF_NONCLIENT | PRF_CLIENT | PRF_ERASEBKGND | PRF_CHILDREN
    return bool(_user32.SendMessageTimeoutW(
        hwnd, WM_PRINT, memory_dc, flags, SMTO_BLOCK | SMTO_ABORTIFHUNG,
        CAPTURE_MESSAGE_TIMEOUT_MS, ctypes.byref(result),
    ))


def _capture_desktop_fallback(memory_dc: Any, window: dict[str, Any]) -> bool:
    """Copy visible desktop pixels only when the target's full bounds are unobscured."""
    screen_dc = _user32.GetDC(0)
    if not screen_dc:
        return False
    try:
        return bool(_gdi32.BitBlt(
            memory_dc, 0, 0, window["width"], window["height"],
            screen_dc, window["x"], window["y"], 0x00CC0020 | 0x40000000,
        ))
    finally:
        _user32.ReleaseDC(0, screen_dc)


def _bitmap_pixels(memory_dc: Any, bitmap: Any, old_bitmap: Any, width: int, height: int) -> bytes:
    _gdi32.SelectObject(memory_dc, old_bitmap)
    bitmap_info = _BITMAPINFO()
    bitmap_info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    bitmap_info.bmiHeader.biWidth = width
    bitmap_info.bmiHeader.biHeight = -height
    bitmap_info.bmiHeader.biPlanes = 1
    bitmap_info.bmiHeader.biBitCount = 32
    bitmap_info.bmiHeader.biCompression = 0
    bitmap_info.bmiHeader.biSizeImage = width * height * 4
    pixels = (ctypes.c_ubyte * (width * height * 4))()
    rows = _gdi32.GetDIBits(memory_dc, bitmap, 0, height, pixels, ctypes.byref(bitmap_info), 0)
    if rows != height:
        raise WinAppError("Could not read the captured window pixels")
    return bytes(pixels)


def _validated_window(window_id: int | str) -> tuple[int, dict[str, Any]]:
    try:
        hwnd = int(window_id)
    except (TypeError, ValueError):
        raise WinAppError("Invalid window id") from None
    if not 0 < hwnd <= 0xFFFFFFFF:
        raise WinAppError("Invalid window id")
    window = _window(hwnd) if _user32.GetAncestor(hwnd, GA_ROOT) == hwnd else None
    if window is None:
        raise WinAppError("That window is not on screen anymore; refresh the mirror")
    if window["width"] * window["height"] > MAX_CAPTURE_PIXELS:
        raise WinAppError("That window is too large to capture")
    return hwnd, window


def _window_identity(hwnd: int, window: dict[str, Any]) -> tuple[Any, ...]:
    thread_id = _user32.GetWindowThreadProcessId(hwnd, None)
    return (thread_id, window["pid"], window["app"], window["className"], window["title"])


def winapps_window_screenshot(window_id: int) -> bytes:
    """Capture a window with a bounded app paint request and safe visible-pixel fallback."""
    if sys.platform != "win32" or _user32 is None or _gdi32 is None:
        raise WinAppError("Native Windows app mirror needs a Windows host")
    hwnd, window = _validated_window(window_id)
    width, height = window["width"], window["height"]

    screen_dc = _user32.GetWindowDC(hwnd)
    if not screen_dc:
        raise WinAppError("Could not open the window display context")
    memory_dc = bitmap = None
    try:
        memory_dc = _gdi32.CreateCompatibleDC(screen_dc)
        bitmap = _gdi32.CreateCompatibleBitmap(screen_dc, width, height)
        if not memory_dc or not bitmap:
            raise WinAppError("Could not create the window capture surface")
        old_bitmap = _gdi32.SelectObject(memory_dc, bitmap)
        if not old_bitmap:
            raise WinAppError("Could not select the window capture surface")
        _gdi32.PatBlt(memory_dc, 0, 0, width, height, 0x00000042)
        rendered = _send_bounded_window_print(hwnd, memory_dc)
        if not rendered:
            if not _window_unoccluded(hwnd, window):
                raise WinAppError(
                    "The window did not respond to capture before the timeout and is covered; uncover it and retry",
                    status=504,
                )
            if not _capture_desktop_fallback(memory_dc, window):
                raise WinAppError("The window capture timed out and desktop fallback failed", status=504)
        pixels = _bitmap_pixels(memory_dc, bitmap, old_bitmap, width, height)
    finally:
        if bitmap:
            if memory_dc and old_bitmap:
                _gdi32.SelectObject(memory_dc, old_bitmap)
            _gdi32.DeleteObject(bitmap)
        if memory_dc:
            _gdi32.DeleteDC(memory_dc)
        _user32.ReleaseDC(hwnd, screen_dc)
    result = _png(width, height, pixels)
    validate_png_frame(result)
    return result


def winapps_window_stream(window_id: int | str, fps: float) -> Iterator[bytes]:
    """Yield bounded PNG frames for one still-visible window.

    The first capture happens before the HTTP handler sends stream headers, so a stale
    HWND can still be reported as a normal client error. If it disappears later, the
    stream ends cleanly and the dashboard can fall back to individual captures.
    """
    if sys.platform != "win32" or _user32 is None or _gdi32 is None:
        raise WinAppError("Native Windows app mirror needs a Windows host", status=501)
    if not math.isfinite(fps) or not MIN_STREAM_FPS <= fps <= MAX_STREAM_FPS:
        raise WinAppError(f"Frame rate must be between {MIN_STREAM_FPS:g} and {MAX_STREAM_FPS:g} FPS")
    if not _STREAM_SLOTS.acquire(blocking=False):
        raise WinAppError("The Windows app mirror already has the maximum number of active streams",
                          status=429)
    try:
        hwnd, initial_window = _validated_window(window_id)
        identity = _window_identity(hwnd, initial_window)

        def capture_bound_frame() -> bytes:
            _current_hwnd, before = _validated_window(hwnd)
            if _window_identity(hwnd, before) != identity:
                raise WinAppError("The selected window changed; refresh the mirror")
            frame = winapps_window_screenshot(hwnd)
            _current_hwnd, after = _validated_window(hwnd)
            if _window_identity(hwnd, after) != identity:
                raise WinAppError("The selected window changed; refresh the mirror")
            validate_png_frame(frame)
            return frame

        deadline = time.monotonic() + MAX_STREAM_SECONDS
        yield capture_bound_frame()
        interval = 1.0 / fps
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(interval, remaining))
            if time.monotonic() >= deadline:
                return
            try:
                yield capture_bound_frame()
            except WinAppError:
                return
    finally:
        _STREAM_SLOTS.release()


def doctor() -> dict[str, Any]:
    """Desktop session, this console's elevation and one live capture probe.

    `elevated` is None when Windows would not report this process's own token. A standard-user
    console cannot capture elevated windows, so the state is reported instead of hidden.
    """
    checked_at = int(time.time())
    if sys.platform != "win32" or _user32 is None or _kernel32 is None:
        return {"available": False, "desktop": False, "sessionId": None, "elevated": None,
                "capture": False, "windows": 0, "checkedAt": checked_at,
                "reason": "Native Windows app mirror needs a Windows host"}
    session = wintypes.DWORD()
    _kernel32.ProcessIdToSessionId(_kernel32.GetCurrentProcessId(), ctypes.byref(session))
    # Session 0 and service contexts have no interactive desktop shell to mirror.
    desktop = session.value != 0 and bool(_user32.GetShellWindow())
    result: dict[str, Any] = {
        "available": False, "desktop": desktop, "sessionId": int(session.value),
        "elevated": _token_elevated(_kernel32.GetCurrentProcess()),
        "capture": False, "windows": 0, "checkedAt": checked_at, "reason": None,
    }
    if not desktop:
        result["reason"] = "No interactive desktop session is available to this console"
        return result
    try:
        windows = _win_windows()
    except WinAppError as exc:
        result["reason"] = str(exc)
        return result
    result["windows"] = len(windows)
    result["available"] = bool(windows)
    reason: str | None = None
    for window in windows[:8]:
        try:
            result["capture"] = winapps_window_screenshot(window["windowId"]).startswith(b"\x89PNG")
            break
        except WinAppError as exc:
            reason = str(exc)
    if not result["capture"]:
        result["reason"] = reason or "No on-screen window to probe"
    return result


def main(argv: list[str] | None = None) -> int:
    """Print the JSON inventory (or `--doctor` probe); useful for a quick native check."""
    import json
    if "--doctor" in (sys.argv[1:] if argv is None else argv):
        result = doctor()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["available"] and result["capture"] else 1
    result = winapps_windows()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["available"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
