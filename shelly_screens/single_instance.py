"""Only one instance at a time.

Two instances fight over the same configuration file: each keeps its own
copy in memory and writes it out whole on every save, so whichever writes
last wipes out the other's work. That is how a freshly measured
calibration vanished, replaced by an older copy.

The lock is a Windows named mutex: it belongs to the process and goes away
with it, even if the process is killed. A lock file, on the other hand,
would survive a hard stop and block every later launch.

Relaunching the application doesn't show a refusal: the running
instance's settings window opens. That is what one expects from a tray
program, whose main window is often closed.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)

# `Local\` limits the scope to the current Windows session: two accounts
# logged in side by side each keep their own instance -- their icon, their
# profiles. Which of them drives the shared power strips is decided
# elsewhere: the session on the screen (see `win.session`).
MUTEX_NAME = r"Local\ShellyScreens.SingleInstance"
ERROR_ALREADY_EXISTS = 183

kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND
user32.PostMessageW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]

_handle: wintypes.HANDLE | None = None


def acquire() -> bool:
    """Takes the lock. False if another instance already holds it."""
    global _handle
    handle = kernel32.CreateMutexW(None, True, MUTEX_NAME)
    if not handle:
        # If no lock can be taken, better to let it start than to block.
        return True
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False
    _handle = handle
    return True


def release() -> None:
    """Releases the lock. Windows would do it on exit anyway."""
    global _handle
    if _handle:
        kernel32.CloseHandle(_handle)
        _handle = None


def wake_existing(window_class: str, message: int) -> bool:
    """Asks the running instance to show itself."""
    hwnd = user32.FindWindowW(window_class, None)
    if not hwnd:
        return False
    user32.PostMessageW(hwnd, message, 0, 0)
    return True
