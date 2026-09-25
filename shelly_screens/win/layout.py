"""Les fenetres restees sur un ecran qu'on vient de couper.

Quand un ecran perd son alimentation, Windows le retire du bureau et rapatrie
en principe ses fenetres -- mais pas toujours, et pas toutes : un moniteur
alimente par l'USB-C du PC reste enumere une fois sa prise coupee. On ramene
donc sur l'ecran allume le plus proche toute fenetre qu'on ne peut plus
attraper.

C'est tout. L'application ne memorise pas de disposition de fenetres : un
profil dit quels ecrans sont allumes, pas ce qu'on y fait, et sur un meme
profil se succedent des activites qui ont chacune la leur. La position des
ecrans, elle, est celle que Windows definit, lue a chaque fois.
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

# Une fenetre compte comme visible si l'on peut en attraper la barre de
# titre : une bande de cette hauteur en haut de la fenetre, dont au moins
# cette largeur tombe sur un ecran utilisable.
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
    """Une fenetre et sa place sur le bureau, dans l'instant."""

    hwnd: int
    title: str
    executable: str  # chemin complet, en minuscules
    show_cmd: int  # normal / minimisee / maximisee
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
                executable=_executable_of(hwnd),
                show_cmd=int(placement.showCmd),
                normal_rect=placement.rcNormalPosition.as_tuple(),
            )
        )
        return 1

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return entries


# ------------------------------------------------------------ fenetres perdues


def _overlap(a: Rect, b: Rect) -> tuple[int, int]:
    """Largeur et hauteur communes a deux rectangles (0 s'ils sont disjoints)."""
    width = min(a[2], b[2]) - max(a[0], b[0])
    height = min(a[3], b[3]) - max(a[1], b[1])
    return max(0, width), max(0, height)


def _grabbable(rect: Rect, areas: list[Rect]) -> bool:
    """Vrai si la barre de titre tombe, pour une part utile, sur une zone."""
    strip = (rect[0], rect[1], rect[2], rect[1] + TITLE_STRIP_PX)
    needed = min(MIN_GRAB_PX, max(1, rect[2] - rect[0]))
    for area in areas:
        width, height = _overlap(strip, area)
        if width >= needed and height > 0:
            return True
    return False


def _distance(point: tuple[int, int], area: Rect) -> float:
    """Distance d'un point a un rectangle, nulle s'il est dedans."""
    dx = max(area[0] - point[0], 0, point[0] - area[2])
    dy = max(area[1] - point[1], 0, point[1] - area[3])
    return (dx * dx + dy * dy) ** 0.5


def _fit(rect: Rect, area: Rect) -> Rect:
    """Le rectangle ramene dans la zone, au plus pres de sa place.

    La taille est gardee, reduite seulement si elle ne tient pas.
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
    """Ramene sur l'ecran utilisable le plus proche les fenetres hors de tous.

    `usable` : zones de travail des ecrans ou l'on peut poser une fenetre.
    `known` : ecrans reels, avant et apres le changement. Seule une fenetre
    qui en chevauchait un est ramenee : certains programmes garent des
    fenetres a -32000 a dessein, et les faire surgir serait une nuisance.
    `offset` : decalage des coordonnees « espace de travail » de
    `WINDOWPLACEMENT` vers celles de l'ecran -- non nul quand la barre des
    taches de l'ecran principal est en haut ou a gauche.

    Une fenetre reduite le reste, et reviendra au bon endroit quand on la
    rouvrira ; une fenetre agrandie l'est de nouveau, sur son nouvel ecran.
    """
    if not usable:
        return []
    dx, dy = offset
    moved: list[WindowEntry] = []
    minimized = (SW_SHOWMINIMIZED, SW_MINIMIZE, SW_SHOWMINNOACTIVE)
    for entry in capture():
        normal = _shift(entry.normal_rect, dx, dy)
        # La place « normale » n'est pas toujours celle qu'occupe la
        # fenetre : agrandie, ou collee a un bord (Aero Snap), elle est
        # ailleurs. On juge donc sur sa place reelle -- sauf reduite, ou
        # Windows la gare a -32000 et ou seule compte la place de retour.
        shown = normal
        if entry.show_cmd not in minimized:
            actual = RECT()
            if user32.GetWindowRect(entry.hwnd, ctypes.byref(actual)):
                shown = actual.as_tuple()
        if _grabbable(shown, usable):
            continue
        if not any(all(_overlap(shown, area)) for area in known):
            continue  # jamais sur un ecran : garee a dessein, on n'y touche pas
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
                # Deja agrandie, Windows la laisse ou elle est, meme avec
                # une nouvelle place normale : on la ramene d'abord a sa
                # taille normale, sur l'ecran vise, puis on l'y agrandit.
                placement.showCmd = SW_SHOWNOACTIVATE
                done = user32.SetWindowPlacement(entry.hwnd, ctypes.byref(placement))
                if done:
                    user32.ShowWindowAsync(entry.hwnd, SW_SHOWMAXIMIZED)
            else:
                # Une fenetre reduite le reste, sans surgir au premier plan.
                placement.showCmd = SW_SHOWMINNOACTIVE
                done = user32.SetWindowPlacement(entry.hwnd, ctypes.byref(placement))
        else:
            # Fenetre normale : on la deplace sans l'activer ni la faire
            # passer devant les autres -- qu'on la retrouve, sans plus.
            new = _fit(shown, target)
            done = user32.SetWindowPos(
                entry.hwnd, None, new[0], new[1], new[2] - new[0], new[3] - new[1],
                SWP_NOZORDER | SWP_NOACTIVATE | SWP_ASYNCWINDOWPOS,
            )
        if done:
            moved.append(entry)
    return moved
