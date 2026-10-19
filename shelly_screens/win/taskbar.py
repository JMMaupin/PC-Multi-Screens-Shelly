"""The taskbar's auto-hide: read it, and put it back.

Windows sometimes drops "Automatically hide the taskbar" when screens come
and go -- a profile change, a ghost taken off the desktop. The setting is
read and written through the shell's appbar messages, the same ones the
Settings app ends up sending: `ABM_GETSTATE` costs nothing, so it can be
checked at every refresh, and `ABM_SETSTATE` applies at once, without
restarting Explorer.

The setting belongs to the account: every taskbar of the session follows
it, the secondary ones included.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from .api import RECT, UINT_PTR, shell32, user32

ABM_GETSTATE = 0x00000004
ABM_SETSTATE = 0x0000000A
ABS_AUTOHIDE = 0x0000001
# Always on since Windows 7: it can no longer be turned off, but it is
# still reported, and sent back as is.
ABS_ALWAYSONTOP = 0x0000002


class APPBARDATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uCallbackMessage", wintypes.UINT),
        ("uEdge", wintypes.UINT),
        ("rc", RECT),
        ("lParam", wintypes.LPARAM),
    ]


_SHAppBarMessage = shell32.SHAppBarMessage
_SHAppBarMessage.argtypes = [wintypes.DWORD, ctypes.POINTER(APPBARDATA)]
_SHAppBarMessage.restype = UINT_PTR

user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND


def _data() -> APPBARDATA:
    data = APPBARDATA()
    data.cbSize = ctypes.sizeof(APPBARDATA)
    # The primary taskbar. Without Explorer -- a crash, a restart under
    # way -- there is no taskbar to set, and `auto_hide` says so.
    data.hWnd = user32.FindWindowW("Shell_TrayWnd", None)
    return data


def auto_hide() -> bool | None:
    """True if the taskbar hides itself; None without a taskbar."""
    data = _data()
    if not data.hWnd:
        return None
    return bool(_SHAppBarMessage(ABM_GETSTATE, ctypes.byref(data)) & ABS_AUTOHIDE)


def set_auto_hide(on: bool) -> bool:
    """Turn auto-hide on or off; true if the taskbar now says so."""
    data = _data()
    if not data.hWnd:
        return False
    data.lParam = (ABS_AUTOHIDE if on else 0) | ABS_ALWAYSONTOP
    _SHAppBarMessage(ABM_SETSTATE, ctypes.byref(data))
    return auto_hide() == on
