"""Which Windows session drives the power strips.

Every account signed in runs its own instance -- its own icon, its own
profiles. But the power strips are shared: two instances commanding them
at once would fight, each applying its profiles and its sleep handling.
Only one drives them: the instance of the session shown on the PC's
screen, the console session. The others stay passive until theirs comes
to the front again (fast user switching, unlocking).

A session opened through Remote Desktop is not the console: its instance
stays passive, which is right -- it is not in front of these screens.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)

WM_WTSSESSION_CHANGE = 0x02B1
NOTIFY_FOR_THIS_SESSION = 0
NO_CONSOLE_SESSION = 0xFFFFFFFF
WTS_CURRENT_SERVER_HANDLE = None
WTS_USER_NAME = 5  # WTS_INFO_CLASS.WTSUserName

kernel32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
kernel32.ProcessIdToSessionId.restype = wintypes.BOOL
kernel32.WTSGetActiveConsoleSessionId.restype = wintypes.DWORD
wtsapi32.WTSRegisterSessionNotification.argtypes = [wintypes.HWND, wintypes.DWORD]
wtsapi32.WTSRegisterSessionNotification.restype = wintypes.BOOL
wtsapi32.WTSUnRegisterSessionNotification.argtypes = [wintypes.HWND]
wtsapi32.WTSUnRegisterSessionNotification.restype = wintypes.BOOL
wtsapi32.WTSQuerySessionInformationW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.c_int,
    ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.DWORD),
]
wtsapi32.WTSQuerySessionInformationW.restype = wintypes.BOOL
wtsapi32.WTSFreeMemory.argtypes = [ctypes.c_void_p]


def current_session_id() -> int:
    session = wintypes.DWORD()
    if not kernel32.ProcessIdToSessionId(kernel32.GetCurrentProcessId(), ctypes.byref(session)):
        return -1
    return int(session.value)


def console_session_id() -> int | None:
    """The session shown on the PC's screen; None while there is none,
    for the instant a switch between sessions takes."""
    session = kernel32.WTSGetActiveConsoleSessionId()
    return None if session == NO_CONSOLE_SESSION else int(session)


def is_console_session() -> bool:
    """True if this process runs in the session shown on the screen.

    With no console session at all -- the instant of a switch --, the
    answer is yes: better an instance that keeps driving for a second
    than none at all, which would miss a sleep.
    """
    console = console_session_id()
    return console is None or console == current_session_id()


def console_user() -> str:
    """Name of the account shown on the screen, empty if unknown."""
    console = console_session_id()
    if console is None:
        return ""
    buffer = wintypes.LPWSTR()
    size = wintypes.DWORD()
    if not wtsapi32.WTSQuerySessionInformationW(
        WTS_CURRENT_SERVER_HANDLE, console, WTS_USER_NAME,
        ctypes.byref(buffer), ctypes.byref(size),
    ):
        return ""
    try:
        return buffer.value or ""
    finally:
        wtsapi32.WTSFreeMemory(buffer)


def register(hwnd: int) -> bool:
    """Ask Windows to tell this window when sessions switch."""
    return bool(wtsapi32.WTSRegisterSessionNotification(hwnd, NOTIFY_FOR_THIS_SESSION))


def unregister(hwnd: int) -> None:
    wtsapi32.WTSUnRegisterSessionNotification(hwnd)
