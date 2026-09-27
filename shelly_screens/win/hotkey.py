"""Global keyboard shortcuts: description, parsing, availability check.

A global shortcut is reserved with Windows through `RegisterHotKey`. The
first program to reserve it keeps it: a second attempt fails, which is
precisely how we can tell whether it is free. We reserve it for an instant
and release it right away.

The actual reservation lives in the notification window
(`shell.TrayWindow`): Windows binds a shortcut to the thread that
registered it, and that thread's message loop is what receives `WM_HOTKEY`.

The shortcut is stored in the configuration in its readable form,
"Ctrl+Win+Alt+P", so that a `config.json` opened by hand makes sense.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
from ctypes import wintypes

from .api import user32

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
# Holding the key down sends a single event, not a burst.
MOD_NOREPEAT = 0x4000

# Display order, the one used on keyboards: Ctrl, Win, Alt, Shift.
MODIFIERS = (
    ("Ctrl", MOD_CONTROL),
    ("Win", MOD_WIN),
    ("Alt", MOD_ALT),
    ("Shift", MOD_SHIFT),
)

# Keys on offer: letters, digits, function keys. The virtual-key codes
# of letters and digits are their uppercase ASCII codes.
KEYS: dict[str, int] = {
    **{chr(code): code for code in range(ord("A"), ord("Z") + 1)},
    **{chr(code): code for code in range(ord("0"), ord("9") + 1)},
    **{f"F{number}": 0x6F + number for number in range(1, 13)},
}

# Identifier for the availability probe, distinct from the notification
# window's so we never accidentally take its own away from it.
_PROBE_ID = 0xB00F

user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.UnregisterHotKey.restype = wintypes.BOOL


@dataclass(frozen=True)
class Hotkey:
    """A combination: some modifiers and a key."""

    modifiers: int
    key: str

    @property
    def vk(self) -> int:
        return KEYS[self.key]

    @property
    def usable(self) -> bool:
        """Whether there is at least one real modifier.

        Without Ctrl, Alt or Win, the key would be stolen from every
        application: typing a "P" in a document would trigger the
        shortcut. Shift alone is not enough, for the same reason.
        """
        return bool(self.modifiers & (MOD_CONTROL | MOD_ALT | MOD_WIN))

    def __str__(self) -> str:
        names = [name for name, flag in MODIFIERS if self.modifiers & flag]
        return "+".join(names + [self.key])


def parse(text: str) -> Hotkey | None:
    """Parse "Ctrl+Win+Alt+P"; `None` if empty or unreadable."""
    parts = [part.strip() for part in (text or "").split("+") if part.strip()]
    if not parts:
        return None
    flags = {name.lower(): flag for name, flag in MODIFIERS}
    # A few common synonyms, for when the file is edited by hand.
    flags.update({"control": MOD_CONTROL, "windows": MOD_WIN, "super": MOD_WIN})
    modifiers = 0
    for part in parts[:-1]:
        flag = flags.get(part.lower())
        if flag is None:
            return None
        modifiers |= flag
    key = parts[-1].upper()
    if key not in KEYS:
        return None
    return Hotkey(modifiers, key)


def is_free(hotkey: Hotkey) -> bool:
    """True if no program already holds this shortcut.

    Reserved without a window, for the calling thread, then released right
    away. A shortcut this application already holds therefore reports
    "taken": it is up to the caller to recognise its own.

    Known limitation: a few Windows shortcuts (Win+L, for example) don't
    go through this mechanism. Reserving them succeeds, but Windows
    intercepts them before we do; no test can detect that.
    """
    if not user32.RegisterHotKey(None, _PROBE_ID, hotkey.modifiers | MOD_NOREPEAT, hotkey.vk):
        return False
    user32.UnregisterHotKey(None, _PROBE_ID)
    return True
