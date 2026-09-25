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
import math
import re
import winreg
from ctypes import wintypes
from dataclasses import dataclass

from ..i18n import t
from .api import RECT, user32

try:
    shcore = ctypes.WinDLL("shcore", use_last_error=True)
    shcore.GetDpiForMonitor.argtypes = [
        wintypes.HMONITOR, ctypes.c_int,
        ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT),
    ]
    shcore.GetDpiForMonitor.restype = ctypes.c_long
except (OSError, AttributeError):
    shcore = None  # avant Windows 8.1 : pas d'echelle par ecran

MDT_EFFECTIVE_DPI = 0
BASE_DPI = 96  # l'echelle 100 %

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
    # Echelle reglee dans Windows (1.25 pour 125 %) : un ecran 4K a 150 %
    # offre l'espace de travail d'un 2560x1440.
    scale: float = 1.0

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


def _scale_of(handle) -> float:
    """Echelle d'un ecran, telle que reglee dans les parametres d'affichage."""
    if shcore is None:
        return 1.0
    dpi_x, dpi_y = wintypes.UINT(), wintypes.UINT()
    if shcore.GetDpiForMonitor(handle, MDT_EFFECTIVE_DPI, ctypes.byref(dpi_x),
                               ctypes.byref(dpi_y)) != 0 or not dpi_x.value:
        return 1.0
    return round(dpi_x.value / BASE_DPI, 2)


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
                    scale=_scale_of(handle),
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


# ------------------------------------------------------------ taille physique

ENUM_DISPLAY = r"SYSTEM\CurrentControlSet\Enum\DISPLAY"
# En deca, la « taille » est un rapport d'aspect deguise (16x9) ou une
# valeur de remplissage : aucun ecran de bureau n'est aussi petit.
MIN_DIAGONAL_IN = 5.0

_diagonals: dict[str, float] = {}


def parse_edid_diagonal(edid: bytes) -> float:
    """Diagonale en pouces annoncee par un EDID ; 0 s'il ne la donne pas.

    La norme VESA la porte a deux endroits : le premier descripteur de
    timing, en millimetres, et l'en-tete, en centimetres arrondis -- ou
    0x0, « non definie ». On prend le plus precis des deux qui soit
    renseigne. Largeur et hauteur ne servent qu'a en tirer la diagonale :
    certains ecrans les remplissent d'un gabarit qui n'a meme pas leur
    format (609x355 mm pour un 16:9), alors que la diagonale reste juste.
    """
    candidates = []
    if len(edid) >= 72 and (edid[54] or edid[55]):  # horloge non nulle : un timing
        block = edid[54:72]
        width = block[12] | (block[14] & 0xF0) << 4
        height = block[13] | (block[14] & 0x0F) << 8
        candidates.append(math.hypot(width, height) / 25.4)
    if len(edid) >= 23:
        candidates.append(math.hypot(edid[21], edid[22]) / 2.54)
    return next((d for d in candidates if d >= MIN_DIAGONAL_IN), 0.0)


def physical_diagonal(key: str) -> float:
    """Diagonale d'un ecran en pouces, lue dans son EDID ; 0 si inconnue.

    Windows garde l'EDID de chaque ecran dans le registre, sous l'instance
    que designe la cle (`GSM774B#UID8453` : materiel `GSM774B`, instance
    finissant par `UID8453`). Il y reste quand l'ecran est eteint, et se lit
    sans droits d'administrateur. Il ne change pas : on le lit une fois.
    """
    if key in _diagonals:
        return _diagonals[key]
    value = 0.0
    hardware, _, uid = key.partition("#")
    if hardware and uid.startswith("UID"):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                f"{ENUM_DISPLAY}\\{hardware}") as parent:
                index = 0
                while not value:
                    try:
                        instance = winreg.EnumKey(parent, index)
                    except OSError:
                        break
                    index += 1
                    if instance.split("&")[-1] != uid:
                        continue
                    try:
                        with winreg.OpenKey(
                            parent, instance + "\\Device Parameters"
                        ) as params:
                            edid = winreg.QueryValueEx(params, "EDID")[0]
                    except OSError:
                        continue
                    value = round(parse_edid_diagonal(bytes(edid)), 1)
        except OSError:
            pass
    _diagonals[key] = value
    return value


# ------------------------------------------------------------ sorties physiques

# Technologies de sortie de DISPLAYCONFIG_VIDEO_OUTPUT_TECHNOLOGY qui ne
# designent pas un ecran : un ecran virtuel (Parsec, Sunshine, spacedesk...)
# et un ecran sans fil. Un dock USB (INDIRECT_WIRED) est, lui, bien reel.
TECHNOLOGY_MIRACAST = 15
TECHNOLOGY_INDIRECT_VIRTUAL = 17
VIRTUAL_TECHNOLOGIES = {TECHNOLOGY_MIRACAST, TECHNOLOGY_INDIRECT_VIRTUAL}

QDC_ONLY_ACTIVE_PATHS = 2
DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME = 2


class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class _RATIONAL(ctypes.Structure):
    _fields_ = [("Numerator", ctypes.c_uint32), ("Denominator", ctypes.c_uint32)]


class _PATH_SOURCE(ctypes.Structure):
    _fields_ = [("adapterId", _LUID), ("id", ctypes.c_uint32),
                ("modeInfoIdx", ctypes.c_uint32), ("statusFlags", ctypes.c_uint32)]


class _PATH_TARGET(ctypes.Structure):
    _fields_ = [("adapterId", _LUID), ("id", ctypes.c_uint32),
                ("modeInfoIdx", ctypes.c_uint32), ("outputTechnology", ctypes.c_uint32),
                ("rotation", ctypes.c_uint32), ("scaling", ctypes.c_uint32),
                ("refreshRate", _RATIONAL), ("scanLineOrdering", ctypes.c_uint32),
                ("targetAvailable", wintypes.BOOL), ("statusFlags", ctypes.c_uint32)]


class _PATH_INFO(ctypes.Structure):
    _fields_ = [("sourceInfo", _PATH_SOURCE), ("targetInfo", _PATH_TARGET),
                ("flags", ctypes.c_uint32)]


class _MODE_INFO(ctypes.Structure):
    _fields_ = [("infoType", ctypes.c_uint32), ("id", ctypes.c_uint32),
                ("adapterId", _LUID), ("data", ctypes.c_byte * 48)]


class _DEVICE_INFO_HEADER(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("size", ctypes.c_uint32),
                ("adapterId", _LUID), ("id", ctypes.c_uint32)]


class _TARGET_DEVICE_NAME(ctypes.Structure):
    _fields_ = [("header", _DEVICE_INFO_HEADER), ("flags", ctypes.c_uint32),
                ("outputTechnology", ctypes.c_uint32),
                ("edidManufactureId", ctypes.c_uint16),
                ("edidProductCodeId", ctypes.c_uint16),
                ("connectorInstance", ctypes.c_uint32),
                ("monitorFriendlyDeviceName", ctypes.c_wchar * 64),
                ("monitorDevicePath", ctypes.c_wchar * 128)]


@dataclass(frozen=True)
class DisplayOutput:
    """Un ecran branche a une sortie active, vu par QueryDisplayConfig."""

    key: str  # meme cle que MonitorInfo
    technology: int  # HDMI, DisplayPort, virtuel...
    source: tuple[int, int, int]  # adaptateur et source : partagee = miroir
    edid_name: str  # le modele, lu dans l'EDID ("LG ULTRAGEAR")

    @property
    def virtual(self) -> bool:
        return self.technology in VIRTUAL_TECHNOLOGIES


def list_outputs() -> dict[str, DisplayOutput] | None:
    """Ecrans des sorties actives, par cle ; None si Windows ne repond pas.

    EnumDisplayMonitors ne dit ni si un ecran est virtuel, ni si deux
    ecrans en miroir se partagent la meme image : il n'en rend qu'un. La
    configuration d'affichage, elle, detaille chaque sortie.
    """
    try:
        paths_count, modes_count = ctypes.c_uint32(), ctypes.c_uint32()
        if user32.GetDisplayConfigBufferSizes(
            QDC_ONLY_ACTIVE_PATHS, ctypes.byref(paths_count), ctypes.byref(modes_count)
        ):
            return None
        paths = (_PATH_INFO * paths_count.value)()
        modes = (_MODE_INFO * modes_count.value)()
        if user32.QueryDisplayConfig(
            QDC_ONLY_ACTIVE_PATHS, ctypes.byref(paths_count), paths,
            ctypes.byref(modes_count), modes, None,
        ):
            return None
    except (AttributeError, OSError):
        return None  # avant Windows 7
    outputs: dict[str, DisplayOutput] = {}
    for path in paths[: paths_count.value]:
        target = path.targetInfo
        name = _TARGET_DEVICE_NAME()
        name.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME
        name.header.size = ctypes.sizeof(_TARGET_DEVICE_NAME)
        name.header.adapterId = target.adapterId
        name.header.id = target.id
        if user32.DisplayConfigGetDeviceInfo(ctypes.byref(name)):
            continue
        key = monitor_key(name.monitorDevicePath)
        if not key:
            continue
        source = path.sourceInfo
        outputs[key] = DisplayOutput(
            key=key,
            technology=int(target.outputTechnology),
            source=(int(source.adapterId.LowPart), int(source.adapterId.HighPart),
                    int(source.id)),
            edid_name=name.monitorFriendlyDeviceName.strip(),
        )
    return outputs


def physical_monitors(outputs: dict[str, DisplayOutput] | None) -> list[MonitorInfo]:
    """Ecrans actifs, hors ecrans virtuels et sans fil.

    Sans configuration d'affichage lisible, on les garde tous : mieux vaut
    un ecran virtuel de trop qu'un ecran reel ignore.
    """
    found = list_monitors()
    if outputs is None:
        return found
    return [m for m in found if not (m.key in outputs and outputs[m.key].virtual)]


def monitor_keys() -> set[str]:
    """Cles des ecrans actuellement actifs."""
    return {monitor.key for monitor in list_monitors()}


# Deux bords a moins de ce nombre de pixels sont consideres comme jointifs :
# Windows les aligne au pixel, mais une mise a l'echelle peut les decaler.
EDGE_TOLERANCE_PX = 16
# Un ecran du haut ou du bas dont le centre s'ecarte de celui de l'ecran
# principal de plus de cette fraction de sa largeur est dit decale.
OFFSET_RATIO = 0.1


def _names(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return t("{first} and {last}", first=", ".join(items[:-1]), last=items[-1])


def arrangement(
    screens: list[MonitorInfo], names: dict[str, str]
) -> list[tuple[MonitorInfo, str, str]]:
    """La disposition des ecrans telle que Windows la definit, en clair.

    Rend, pour chaque ecran, son nom et sa place : a gauche, au centre ou a
    droite de l'ecran principal, en haut ou en bas, decale d'un cote, et
    au-dessus -- ou au-dessous -- de quels autres ecrans. `names` donne le
    nom de la prise qui alimente chaque ecran, par cle ; a defaut, le nom
    du pilote et la definition.

    L'application ne stocke rien de tout cela : c'est Windows qui fait
    autorite, et on le relit a chaque fois.
    """
    primary = next((m for m in screens if m.is_primary), None)
    if primary is None:
        return []
    tol = EDGE_TOLERANCE_PX
    left, top, right, bottom = primary.rect

    def name_of(m: MonitorInfo) -> str:
        return names.get(m.key) or f"{m.friendly_name} {m.width}x{m.height}"

    def overlap_x(a: MonitorInfo, b: MonitorInfo) -> bool:
        return min(a.rect[2], b.rect[2]) - max(a.rect[0], b.rect[0]) > tol

    result = []
    for m in screens:
        if m is primary:
            result.append((m, name_of(m), t("centre, primary")))
            continue
        parts: list[str] = []
        if m.rect[3] <= top + tol:
            vertical = "top"
        elif m.rect[1] >= bottom - tol:
            vertical = "bottom"
        else:
            vertical = ""
        if m.rect[2] <= left + tol:
            parts.append(t("left"))
        elif m.rect[0] >= right - tol:
            parts.append(t("right"))
        elif vertical:
            # Au-dessus ou au-dessous de l'ecran principal : ce qui compte
            # alors, c'est le cote vers lequel il deborde.
            shift = (m.rect[0] + m.rect[2]) / 2 - (left + right) / 2
            parts.append(t("top") if vertical == "top" else t("bottom"))
            if shift > OFFSET_RATIO * primary.width:
                parts.append(t("shifted right"))
            elif shift < -OFFSET_RATIO * primary.width:
                parts.append(t("shifted left"))
            vertical = ""
        else:
            parts.append(t("centre"))
        if vertical:
            parts.append(t("top") if vertical == "top" else t("bottom"))
        # Les voisins du dessous ou du dessus, bord contre bord.
        beneath = [
            name_of(o) for o in screens
            if o is not m and abs(m.rect[3] - o.rect[1]) <= tol and overlap_x(m, o)
        ]
        above = [
            name_of(o) for o in screens
            if o is not m and abs(m.rect[1] - o.rect[3]) <= tol and overlap_x(m, o)
        ]
        if beneath:
            parts.append(t("above {names}", names=_names(beneath)))
        if above:
            parts.append(t("below {names}", names=_names(above)))
        result.append((m, name_of(m), ", ".join(parts)))
    return result
