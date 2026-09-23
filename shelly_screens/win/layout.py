"""Capture et restauration de la disposition des fenetres.

Quand un ecran perd son alimentation, Windows le retire du bureau et rapatrie
en vrac les fenetres qui s'y trouvaient. Le retour de l'ecran ne les remet pas
en place. On memorise donc la disposition avant de couper, et on la rejoue
quand les ecrans concernes sont revenus.

Deux situations a distinguer pour retrouver une fenetre :

* dans la meme session, le handle (HWND) est encore valable -- on s'en sert
  directement, c'est exact et immediat ;
* apres un redemarrage, les handles ne valent plus rien. On rapproche alors
  les fenetres de leur trace enregistree par (executable, classe), puis par
  titre au sein de ce groupe.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Any

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

SW_SHOWNORMAL = 1

WPF_ASYNCWINDOWPLACEMENT = 0x0004


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
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = LONG_PTR
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowPlacement.argtypes = [wintypes.HWND, ctypes.POINTER(WINDOWPLACEMENT)]
user32.GetWindowPlacement.restype = wintypes.BOOL
user32.SetWindowPlacement.argtypes = [wintypes.HWND, ctypes.POINTER(WINDOWPLACEMENT)]
user32.SetWindowPlacement.restype = wintypes.BOOL
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
    """Une fenetre et sa place sur le bureau, telle qu'on la memorise."""

    hwnd: int  # valable seulement dans la session courante
    title: str
    class_name: str
    executable: str  # chemin complet, en minuscules
    show_cmd: int  # normal / minimisee / maximisee
    normal_rect: tuple[int, int, int, int]
    min_position: tuple[int, int]
    max_position: tuple[int, int]

    @property
    def process_name(self) -> str:
        return os.path.basename(self.executable)

    @property
    def identity(self) -> tuple[str, str]:
        """Ce qui identifie une fenetre entre deux sessions."""
        return (self.executable, self.class_name)

    def describe(self) -> str:
        title = self.title if len(self.title) <= 48 else self.title[:45] + "..."
        return f"{self.process_name} | {title}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "hwnd": self.hwnd,
            "title": self.title,
            "class_name": self.class_name,
            "executable": self.executable,
            "show_cmd": self.show_cmd,
            "normal_rect": list(self.normal_rect),
            "min_position": list(self.min_position),
            "max_position": list(self.max_position),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WindowEntry":
        return cls(
            hwnd=int(data.get("hwnd", 0)),
            title=str(data.get("title", "")),
            class_name=str(data.get("class_name", "")),
            executable=str(data.get("executable", "")),
            show_cmd=int(data.get("show_cmd", SW_SHOWNORMAL)),
            normal_rect=tuple(data.get("normal_rect", (0, 0, 0, 0))),
            min_position=tuple(data.get("min_position", (-1, -1))),
            max_position=tuple(data.get("max_position", (-1, -1))),
        )


def _text_of(hwnd: int, getter, size: int = 512) -> str:
    buffer = ctypes.create_unicode_buffer(size)
    length = getter(hwnd, buffer, size)
    return buffer[:length] if length > 0 else ""


def _executable_of(hwnd: int) -> str:
    """Chemin de l'executable proprietaire de la fenetre, en minuscules."""
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
    """Vrai pour les fenetres masquees par le gestionnaire de bureau (UWP en veille)."""
    cloaked = ctypes.c_int(0)
    result = dwmapi.DwmGetWindowAttribute(
        wintypes.HWND(hwnd),
        wintypes.DWORD(DWMWA_CLOAKED),
        ctypes.byref(cloaked),
        ctypes.sizeof(cloaked),
    )
    return result == 0 and cloaked.value != 0


def is_manageable(hwnd: int) -> bool:
    """Vrai si la fenetre est une vraie fenetre d'application deplacable."""
    if not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd):
        return False
    if user32.GetAncestor(hwnd, GA_ROOT) != hwnd:
        return False  # fenetre fille
    if user32.GetWindow(hwnd, GW_OWNER):
        return False  # boite de dialogue rattachee a une autre fenetre
    style = user32.GetWindowLongPtrW(hwnd, GWL_STYLE)
    if style & WS_CHILD:
        return False
    ex_style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    if ex_style & WS_EX_TOOLWINDOW:
        return False  # palette d'outils, pas une fenetre de premier plan
    if not _text_of(hwnd, user32.GetWindowTextW):
        return False  # sans titre, rien a replacer d'utile
    if _is_cloaked(hwnd):
        return False
    return True


def capture() -> list[WindowEntry]:
    """Releve la position de toutes les fenetres d'application visibles."""
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
                class_name=_text_of(hwnd, user32.GetClassNameW, 256),
                executable=_executable_of(hwnd),
                show_cmd=int(placement.showCmd),
                normal_rect=placement.rcNormalPosition.as_tuple(),
                min_position=(placement.ptMinPosition.x, placement.ptMinPosition.y),
                max_position=(placement.ptMaxPosition.x, placement.ptMaxPosition.y),
            )
        )
        return 1

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return entries


def serialize(entries: list[WindowEntry]) -> list[dict[str, Any]]:
    return [entry.to_dict() for entry in entries]


def deserialize(data: list[dict[str, Any]]) -> list[WindowEntry]:
    return [WindowEntry.from_dict(item) for item in data or []]


def apply_entry(entry: WindowEntry, hwnd: int) -> bool:
    """Repose une fenetre a l'endroit memorise."""
    placement = WINDOWPLACEMENT()
    placement.length = ctypes.sizeof(WINDOWPLACEMENT)
    if not user32.GetWindowPlacement(hwnd, ctypes.byref(placement)):
        return False
    # WPF_ASYNCWINDOWPLACEMENT evite de bloquer si l'application est occupee.
    placement.flags = WPF_ASYNCWINDOWPLACEMENT
    placement.showCmd = entry.show_cmd
    placement.ptMinPosition = POINT(entry.min_position[0], entry.min_position[1])
    placement.ptMaxPosition = POINT(entry.max_position[0], entry.max_position[1])
    placement.rcNormalPosition = RECT(*entry.normal_rect)
    return bool(user32.SetWindowPlacement(hwnd, ctypes.byref(placement)))


@dataclass
class RestoreReport:
    """Ce que la restauration a reellement pu faire."""

    restored: int = 0
    skipped: int = 0
    unmatched: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"{self.restored} window(s) restored"]
        if self.skipped:
            parts.append(f"{self.skipped} unchanged")
        if self.unmatched:
            parts.append(f"{len(self.unmatched)} not found")
        return ", ".join(parts)


def restore(entries: list[WindowEntry]) -> RestoreReport:
    """Rejoue une disposition memorisee sur les fenetres actuelles."""
    report = RestoreReport()
    if not entries:
        return report
    current = capture()
    for entry, hwnd in match_entries(entries, current):
        if hwnd is None:
            report.unmatched.append(entry.describe())
            continue
        if apply_entry(entry, hwnd):
            report.restored += 1
        else:
            report.skipped += 1
    return report


def match_entries(
    saved: list[WindowEntry], current: list[WindowEntry]
) -> list[tuple[WindowEntry, int | None]]:
    """Associe chaque fenetre memorisee a une fenetre actuelle, si possible.

    On procede du plus sur au plus approximatif : le handle exact d'abord,
    puis le titre au sein d'un meme (executable, classe), puis l'ordre
    d'apparition pour ce qu'il reste.
    """
    available = {entry.hwnd: entry for entry in current}
    results: list[tuple[WindowEntry, int | None]] = []
    pending: list[WindowEntry] = []

    # 1. Meme session : le handle est encore valable et designe la meme chose.
    for entry in saved:
        candidate = available.get(entry.hwnd)
        if candidate is not None and candidate.identity == entry.identity:
            results.append((entry, entry.hwnd))
            del available[entry.hwnd]
        else:
            pending.append(entry)

    if not pending:
        return results

    # 2. Regroupement par (executable, classe) parmi ce qui reste libre.
    groups: dict[tuple[str, str], list[WindowEntry]] = {}
    for entry in available.values():
        groups.setdefault(entry.identity, []).append(entry)

    leftovers: list[WindowEntry] = []
    for entry in pending:
        group = groups.get(entry.identity)
        if not group:
            leftovers.append(entry)
            continue
        # Titre identique : c'est tres probablement la meme fenetre.
        exact = next((c for c in group if c.title == entry.title), None)
        if exact is not None:
            group.remove(exact)
            results.append((entry, exact.hwnd))
        else:
            leftovers.append(entry)

    # 3. Dernier recours : apparier dans l'ordre ce qui partage l'identite.
    for entry in leftovers:
        group = groups.get(entry.identity)
        if group:
            candidate = group.pop(0)
            results.append((entry, candidate.hwnd))
        else:
            results.append((entry, None))

    return results
