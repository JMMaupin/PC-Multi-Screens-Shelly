"""Declarations ctypes communes a la couche Windows.

Un point important y est regle une fois pour toutes : la conscience du DPI.
Sans elle, Windows virtualise les coordonnees des fenetres sur les ecrans
mis a l'echelle, et les positions relevees ne correspondent plus a la
realite. On passe donc le processus en Per-Monitor V2 des l'import.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

# ctypes ne connait pas ces types sur toutes les versions de Python.
LRESULT = ctypes.c_ssize_t
LONG_PTR = ctypes.c_ssize_t
UINT_PTR = ctypes.c_size_t
HICON = wintypes.HANDLE

DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.right, self.bottom)

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


def enable_dpi_awareness() -> None:
    """Passe le processus en Per-Monitor V2, avec repli sur les API anciennes."""
    try:
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
        if user32.SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2):
            return
    except AttributeError:
        pass
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        # 2 = PROCESS_PER_MONITOR_DPI_AWARE
        if shcore.SetProcessDpiAwareness(2) == 0:
            return
    except (OSError, AttributeError):
        pass
    try:
        user32.SetProcessDPIAware()
    except AttributeError:
        pass


enable_dpi_awareness()
