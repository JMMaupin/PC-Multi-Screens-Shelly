"""Windows left behind on a screen that was just switched off.

When a screen loses power, Windows removes it from the desktop and is
supposed to bring its windows back -- but not always, and not all of them:
a monitor powered through the PC's USB-C stays enumerated once its outlet
is off. So any window that can no longer be grabbed is brought back to the
nearest screen that is on.

That's all. The application doesn't remember any window layout: a profile
says which screens are on, not what is done on them, and a single profile
hosts successive activities that each have their own. The position of the
screens is the one Windows defines, read every time.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass

from .api import POINT, RECT, LONG_PTR, kernel32, user32

dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

GW_OWNER = 4
GA_ROOT = 2
GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_CHILD = 0x40000000
WS_EX_TOOLWINDOW = 0x00000080
DWMWA_CLOAKED = 14
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

SW_SHOWMINIMIZED = 2
SW_SHOWMAXIMIZED = 3
SW_SHOWNOACTIVATE = 4
SW_MINIMIZE = 6
SW_SHOWMINNOACTIVE = 7

WPF_ASYNCWINDOWPLACEMENT = 0x0004

SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_ASYNCWINDOWPOS = 0x4000

# A window counts as visible if its title bar can be grabbed: a strip of
# this height at the top of the window, at least this much of whose width
# falls on a usable screen.
TITLE_STRIP_PX = 32
MIN_GRAB_PX = 80

Rect = tuple[int, int, int, int]


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.UINT),
        ("flags", wintypes.UINT),
        ("showCmd", wintypes.UINT),
        ("ptMinPosition", POINT),
        ("ptMaxPosition", POINT),
        ("rcNormalPosition", RECT),
    ]


WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = LONG_PTR
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowPlacement.argtypes = [wintypes.HWND, ctypes.POINTER(WINDOWPLACEMENT)]
user32.GetWindowPlacement.restype = wintypes.BOOL
user32.SetWindowPlacement.argtypes = [wintypes.HWND, ctypes.POINTER(WINDOWPLACEMENT)]
user32.SetWindowPlacement.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, wintypes.UINT,
]
user32.SetWindowPos.restype = wintypes.BOOL
user32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindowAsync.restype = wintypes.BOOL
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL


@dataclass
class WindowEntry:
    """A window and its place on the desktop, at this moment."""

    hwnd: int
    title: str
    executable: str  # full path, lowercase
    show_cmd: int  # normal / minimised / maximised
    normal_rect: tuple[int, int, int, int]

    @property
    def process_name(self) -> str:
        return os.path.basename(self.executable)

    def describe(self) -> str:
        title = self.title if len(self.title) <= 48 else self.title[:45] + "..."
        return f"{self.process_name} | {title}"


def _text_of(hwnd: int, getter, size: int = 512) -> str:
    buffer = ctypes.create_unicode_buffer(size)
    length = getter(hwnd, buffer, size)
    return buffer[:length] if length > 0 else ""


def _executable_of(hwnd: int) -> str:
    """Path of the executable that owns the window, lowercase."""
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value.lower()
        return ""
    finally:
        kernel32.CloseHandle(handle)


def _is_cloaked(hwnd: int) -> bool:
    """True for windows cloaked by the desktop window manager (suspended UWP)."""
    cloaked = ctypes.c_int(0)
    result = dwmapi.DwmGetWindowAttribute(
        wintypes.HWND(hwnd),
        wintypes.DWORD(DWMWA_CLOAKED),
        ctypes.byref(cloaked),
        ctypes.sizeof(cloaked),
    )
    return result == 0 and cloaked.value != 0


def is_manageable(hwnd: int) -> bool:
    """True if the window is a real, movable application window."""
    if not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd):
        return False
    if user32.GetAncestor(hwnd, GA_ROOT) != hwnd:
        return False  # child window
    if user32.GetWindow(hwnd, GW_OWNER):
        return False  # dialog box owned by another window
    style = user32.GetWindowLongPtrW(hwnd, GWL_STYLE)
    if style & WS_CHILD:
        return False
    ex_style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    if ex_style & WS_EX_TOOLWINDOW:
        return False  # tool palette, not a top-level window
    if not _text_of(hwnd, user32.GetWindowTextW):
        return False  # no title, nothing useful to reposition
    if _is_cloaked(hwnd):
        return False
    return True


def capture() -> list[WindowEntry]:
    """Record the position of every visible application window."""
    entries: list[WindowEntry] = []

    def callback(hwnd, _param) -> int:
        if not is_manageable(hwnd):
            return 1
        placement = WINDOWPLACEMENT()
        placement.length = ctypes.sizeof(WINDOWPLACEMENT)
        if not user32.GetWindowPlacement(hwnd, ctypes.byref(placement)):
            return 1
        entries.append(
            WindowEntry(
                hwnd=int(hwnd),
                title=_text_of(hwnd, user32.GetWindowTextW),
                executable=_executable_of(hwnd),
                show_cmd=int(placement.showCmd),
                normal_rect=placement.rcNormalPosition.as_tuple(),
            )
        )
        return 1

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return entries


# ------------------------------------------------------------ lost windows


def _overlap(a: Rect, b: Rect) -> tuple[int, int]:
    """Width and height shared by two rectangles (0 if they are disjoint)."""
    width = min(a[2], b[2]) - max(a[0], b[0])
    height = min(a[3], b[3]) - max(a[1], b[1])
    return max(0, width), max(0, height)


def _grabbable(rect: Rect, areas: list[Rect]) -> bool:
    """True if a useful part of the title bar falls on one of the areas."""
    strip = (rect[0], rect[1], rect[2], rect[1] + TITLE_STRIP_PX)
    needed = min(MIN_GRAB_PX, max(1, rect[2] - rect[0]))
    for area in areas:
        width, height = _overlap(strip, area)
        if width >= needed and height > 0:
            return True
    return False


def _distance(point: tuple[int, int], area: Rect) -> float:
    """Distance from a point to a rectangle, zero if it is inside."""
    dx = max(area[0] - point[0], 0, point[0] - area[2])
    dy = max(area[1] - point[1], 0, point[1] - area[3])
    return (dx * dx + dy * dy) ** 0.5


def _fit(rect: Rect, area: Rect) -> Rect:
    """The rectangle brought into the area, as close as possible to its place.

    The size is kept, reduced only if it doesn't fit.
    """
    width = min(rect[2] - rect[0], area[2] - area[0])
    height = min(rect[3] - rect[1], area[3] - area[1])
    left = min(max(rect[0], area[0]), area[2] - width)
    top = min(max(rect[1], area[1]), area[3] - height)
    return left, top, left + width, top + height


def _shift(rect: Rect, dx: int, dy: int) -> Rect:
    return rect[0] + dx, rect[1] + dy, rect[2] + dx, rect[3] + dy


def rescue_offscreen(
    usable: list[Rect], known: list[Rect], offset: tuple[int, int]
) -> list[WindowEntry]:
    """Bring windows that are off every screen back to the nearest usable one.

    `usable`: work areas of the screens where a window can be placed.
    `known`: real screens, before and after the change. Only a window that
    overlapped one of them is brought back: some programs park windows at
    -32000 on purpose, and making them pop up would be a nuisance.
    `offset`: shift from the "workspace" coordinates of `WINDOWPLACEMENT`
    to screen coordinates -- non-zero when the primary screen's taskbar is
    at the top or on the left.

    A minimised window stays minimised, and will come back in the right
    place when reopened; a maximised window is maximised again, on its new
    screen.
    """
    if not usable:
        return []
    dx, dy = offset
    moved: list[WindowEntry] = []
    minimized = (SW_SHOWMINIMIZED, SW_MINIMIZE, SW_SHOWMINNOACTIVE)
    for entry in capture():
        normal = _shift(entry.normal_rect, dx, dy)
        # The "normal" place is not always where the window actually is:
        # maximised, or snapped to an edge (Aero Snap), it is elsewhere.
        # So we judge by its real place -- except when minimised, where
        # Windows parks it at -32000 and only the restore place matters.
        shown = normal
        if entry.show_cmd not in minimized:
            actual = RECT()
            if user32.GetWindowRect(entry.hwnd, ctypes.byref(actual)):
                shown = actual.as_tuple()
        if _grabbable(shown, usable):
            continue
        if not any(all(_overlap(shown, area)) for area in known):
            continue  # never on a screen: parked on purpose, leave it alone
        centre = ((shown[0] + shown[2]) // 2, (shown[1] + shown[3]) // 2)
        target = min(usable, key=lambda area: _distance(centre, area))
        if entry.show_cmd in minimized or entry.show_cmd == SW_SHOWMAXIMIZED:
            new = _fit(normal, target)
            placement = WINDOWPLACEMENT()
            placement.length = ctypes.sizeof(WINDOWPLACEMENT)
            if not user32.GetWindowPlacement(entry.hwnd, ctypes.byref(placement)):
                continue
            placement.flags = WPF_ASYNCWINDOWPLACEMENT
            placement.rcNormalPosition = RECT(*_shift(new, -dx, -dy))
            if entry.show_cmd == SW_SHOWMAXIMIZED:
                # Already maximised, Windows leaves it where it is, even with
                # a new normal place: first restore it to its normal size
                # on the target screen, then maximise it there.
                placement.showCmd = SW_SHOWNOACTIVATE
                done = user32.SetWindowPlacement(entry.hwnd, ctypes.byref(placement))
                if done:
                    user32.ShowWindowAsync(entry.hwnd, SW_SHOWMAXIMIZED)
            else:
                # A minimised window stays minimised, without popping to the front.
                placement.showCmd = SW_SHOWMINNOACTIVE
                done = user32.SetWindowPlacement(entry.hwnd, ctypes.byref(placement))
        else:
            # Normal window: move it without activating it or bringing it
            # in front of the others -- just so it can be found, nothing more.
            new = _fit(shown, target)
            done = user32.SetWindowPos(
                entry.hwnd, None, new[0], new[1], new[2] - new[0], new[3] - new[1],
                SWP_NOZORDER | SWP_NOACTIVATE | SWP_ASYNCWINDOWPOS,
            )
        if done:
            moved.append(entry)
    return moved
