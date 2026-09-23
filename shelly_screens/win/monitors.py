r"""Enumeration des ecrans actifs et identification stable de chacun.

Couper l'alimentation d'un ecran le fait disparaitre de Windows : on a donc
besoin de savoir quels ecrans sont presents a un instant donne, et de les
reconnaitre d'une session a l'autre.

Le piege est l'identification. Le nom `\\.\DISPLAY1` est un simple rang
d'enumeration : il change des qu'un ecran s'allume ou s'eteint. Le modele ne
suffit pas non plus, des ecrans identiques donnant la meme chaine. On utilise
donc le chemin d'interface renvoye par EnumDisplayDevices avec le drapeau
EDD_GET_DEVICE_INTERFACE_NAME, qui contient l'UID de la sortie physique de la
carte graphique -- stable, et distinct pour deux ecrans du meme modele.
"""

from __future__ import annotations

import ctypes
import re
from ctypes import wintypes
from dataclasses import dataclass

from .api import RECT, user32

EDD_GET_DEVICE_INTERFACE_NAME = 0x00000001
MONITORINFOF_PRIMARY = 0x00000001
CCHDEVICENAME = 32


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * CCHDEVICENAME),
    ]


class DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("DeviceName", wintypes.WCHAR * 32),
        ("DeviceString", wintypes.WCHAR * 128),
        ("StateFlags", wintypes.DWORD),
        ("DeviceID", wintypes.WCHAR * 128),
        ("DeviceKey", wintypes.WCHAR * 128),
    ]


MONITORENUMPROC = ctypes.WINFUNCTYPE(
    wintypes.BOOL,
    wintypes.HMONITOR,
    wintypes.HDC,
    ctypes.POINTER(RECT),
    wintypes.LPARAM,
)

user32.EnumDisplayMonitors.argtypes = [
    wintypes.HDC,
    ctypes.POINTER(RECT),
    MONITORENUMPROC,
    wintypes.LPARAM,
]
user32.EnumDisplayMonitors.restype = wintypes.BOOL
user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFOEXW)]
user32.GetMonitorInfoW.restype = wintypes.BOOL
user32.EnumDisplayDevicesW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.POINTER(DISPLAY_DEVICEW),
    wintypes.DWORD,
]
user32.EnumDisplayDevicesW.restype = wintypes.BOOL
user32.MonitorFromPoint.argtypes = [ctypes.c_void_p, wintypes.DWORD]
user32.MonitorFromPoint.restype = wintypes.HMONITOR


@dataclass(frozen=True)
class MonitorInfo:
    """Un ecran actuellement actif."""

    key: str  # identifiant stable, voir le module docstring
    device_name: str  # \.\DISPLAY1 -- valable seulement dans l'instant
    friendly_name: str  # ce que le pilote annonce ("Generic PnP Monitor"...)
    rect: tuple[int, int, int, int]  # zone totale, coordonnees du bureau
    work_rect: tuple[int, int, int, int]  # zone hors barre des taches
    is_primary: bool

    @property
    def width(self) -> int:
        return self.rect[2] - self.rect[0]

    @property
    def height(self) -> int:
        return self.rect[3] - self.rect[1]

    def describe(self) -> str:
        """Libelle lisible, pour les menus et les journaux."""
        tag = " (primary)" if self.is_primary else ""
        return (
            f"{self.friendly_name} {self.width}x{self.height} "
            f"@ {self.rect[0]},{self.rect[1]}{tag}"
        )


def list_monitors() -> list[MonitorInfo]:
    """Ecrans actifs, tries de gauche a droite puis de haut en bas."""
    found: list[MonitorInfo] = []

    def callback(handle, _hdc, _rect, _param) -> int:
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            device_name = info.szDevice
            friendly, key = _identify(device_name)
            found.append(
                MonitorInfo(
                    key=key,
                    device_name=device_name,
                    friendly_name=friendly,
                    rect=info.rcMonitor.as_tuple(),
                    work_rect=info.rcWork.as_tuple(),
                    is_primary=bool(info.dwFlags & MONITORINFOF_PRIMARY),
                )
            )
        return 1  # continuer l'enumeration

    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(callback), 0)
    found.sort(key=lambda m: (m.rect[0], m.rect[1]))
    return found


def _identify(device_name: str) -> tuple[str, str]:
    """Nom lisible et cle stable d'un adaptateur d'affichage donne."""
    device = DISPLAY_DEVICEW()
    device.cb = ctypes.sizeof(DISPLAY_DEVICEW)
    ok = user32.EnumDisplayDevicesW(
        device_name, 0, ctypes.byref(device), EDD_GET_DEVICE_INTERFACE_NAME
    )
    if not ok:
        # Sans information moniteur, on se rabat sur le rang d'enumeration.
        # Moins stable, mais mieux que rien.
        return ("Unknown display", f"device:{device_name}")
    friendly = device.DeviceString.strip() or "Display"
    return (friendly, monitor_key(device.DeviceID) or f"device:{device_name}")


def monitor_key(device_id: str) -> str:
    r"""Normalise un chemin d'interface moniteur en cle stable.

    Windows renvoie par exemple :
        \\?\DISPLAY#GSM5B09#5&2b1e0c4&0&UID4353#{e6f07b5f-...}

    On en garde l'identifiant materiel et l'UID de la sortie graphique, soit
    ici `GSM5B09#UID4353`. Le GUID de classe final est commun a tous les
    moniteurs et la partie instance varie selon le chemin PCI : ni l'un ni
    l'autre n'aide a distinguer deux ecrans.

    Le decoupage se fait sur le marqueur `DISPLAY#` plutot qu'avec une
    expression reguliere : le chemin est truffe d'antislashes, qu'une regex
    obligerait a echapper deux fois pour rien.
    """
    if not device_id:
        return ""
    cleaned = device_id.strip()
    marker = "DISPLAY#"
    start = cleaned.find(marker)
    if start < 0:
        return cleaned
    parts = cleaned[start + len(marker) :].split("#")
    hardware_id = parts[0]
    instance = parts[1] if len(parts) > 1 else ""
    uid_match = re.search(r"UID(\d+)", instance)
    suffix = f"UID{uid_match.group(1)}" if uid_match else instance
    return f"{hardware_id}#{suffix}" if suffix else hardware_id


def monitor_keys() -> set[str]:
    """Cles des ecrans actuellement actifs."""
    return {monitor.key for monitor in list_monitors()}


