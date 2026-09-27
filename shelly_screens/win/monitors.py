r"""Enumeration of the active screens and a stable identity for each one.

Cutting a screen's power makes it disappear from Windows: so we need to
know which screens are present at a given moment, and to recognise them
from one session to the next.

The catch is identification. The name `\\.\DISPLAY1` is just an
enumeration rank: it changes as soon as a screen turns on or off. The model
isn't enough either, since identical screens give the same string. So we
use the interface path returned by EnumDisplayDevices with the
EDD_GET_DEVICE_INTERFACE_NAME flag, which contains the UID of the graphics
card's physical output -- stable, and distinct for two screens of the same
model.
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
    shcore = None  # before Windows 8.1: no per-screen scaling

MDT_EFFECTIVE_DPI = 0
BASE_DPI = 96  # the 100 % scale

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
    """A currently active screen."""

    key: str  # stable identifier, see the module docstring
    device_name: str  # \.\DISPLAY1 -- only valid at this moment
    friendly_name: str  # what the driver reports ("Generic PnP Monitor"...)
    rect: tuple[int, int, int, int]  # full area, desktop coordinates
    work_rect: tuple[int, int, int, int]  # area excluding the taskbar
    is_primary: bool
    # Scale set in Windows (1.25 for 125 %): a 4K screen at 150 %
    # offers the workspace of a 2560x1440 one.
    scale: float = 1.0

    @property
    def width(self) -> int:
        return self.rect[2] - self.rect[0]

    @property
    def height(self) -> int:
        return self.rect[3] - self.rect[1]

    def describe(self) -> str:
        """Readable label, for menus and logs."""
        tag = " (primary)" if self.is_primary else ""
        return (
            f"{self.friendly_name} {self.width}x{self.height} "
            f"@ {self.rect[0]},{self.rect[1]}{tag}"
        )


def _scale_of(handle) -> float:
    """A screen's scale, as set in the display settings."""
    if shcore is None:
        return 1.0
    dpi_x, dpi_y = wintypes.UINT(), wintypes.UINT()
    if shcore.GetDpiForMonitor(handle, MDT_EFFECTIVE_DPI, ctypes.byref(dpi_x),
                               ctypes.byref(dpi_y)) != 0 or not dpi_x.value:
        return 1.0
    return round(dpi_x.value / BASE_DPI, 2)


def list_monitors() -> list[MonitorInfo]:
    """Active screens, sorted left to right, then top to bottom."""
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
        return 1  # continue the enumeration

    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(callback), 0)
    found.sort(key=lambda m: (m.rect[0], m.rect[1]))
    return found


def _identify(device_name: str) -> tuple[str, str]:
    """Readable name and stable key of a given display adapter."""
    device = DISPLAY_DEVICEW()
    device.cb = ctypes.sizeof(DISPLAY_DEVICEW)
    ok = user32.EnumDisplayDevicesW(
        device_name, 0, ctypes.byref(device), EDD_GET_DEVICE_INTERFACE_NAME
    )
    if not ok:
        # Without monitor information, fall back on the enumeration rank.
        # Less stable, but better than nothing.
        return ("Unknown display", f"device:{device_name}")
    friendly = device.DeviceString.strip() or "Display"
    return (friendly, monitor_key(device.DeviceID) or f"device:{device_name}")


def monitor_key(device_id: str) -> str:
    r"""Normalise a monitor interface path into a stable key.

    Windows returns, for example:
        \\?\DISPLAY#GSM5B09#5&2b1e0c4&0&UID4353#{e6f07b5f-...}

    We keep the hardware identifier and the UID of the graphics output,
    here `GSM5B09#UID4353`. The trailing class GUID is common to all
    monitors and the instance part varies with the PCI path: neither one
    helps tell two screens apart.

    Splitting is done on the `DISPLAY#` marker rather than with a regular
    expression: the path is riddled with backslashes, which a regex would
    force us to escape twice for nothing.
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


# ------------------------------------------------------------ physical size

ENUM_DISPLAY = r"SYSTEM\CurrentControlSet\Enum\DISPLAY"
# Below this, the "size" is a disguised aspect ratio (16x9) or a filler
# value: no desktop screen is that small.
MIN_DIAGONAL_IN = 5.0

_diagonals: dict[str, float] = {}


def parse_edid_diagonal(edid: bytes) -> float:
    """Diagonal in inches reported by an EDID; 0 if it doesn't give one.

    The VESA standard carries it in two places: the first timing
    descriptor, in millimetres, and the header, in rounded centimetres --
    or 0x0, "undefined". We take the more precise of the two that is
    filled in. Width and height only serve to derive the diagonal: some
    screens fill them with a template that doesn't even match their
    format (609x355 mm for a 16:9), while the diagonal stays correct.
    """
    candidates = []
    if len(edid) >= 72 and (edid[54] or edid[55]):  # non-zero clock: a timing
        block = edid[54:72]
        width = block[12] | (block[14] & 0xF0) << 4
        height = block[13] | (block[14] & 0x0F) << 8
        candidates.append(math.hypot(width, height) / 25.4)
    if len(edid) >= 23:
        candidates.append(math.hypot(edid[21], edid[22]) / 2.54)
    return next((d for d in candidates if d >= MIN_DIAGONAL_IN), 0.0)


def physical_diagonal(key: str) -> float:
    """A screen's diagonal in inches, read from its EDID; 0 if unknown.

    Windows keeps each screen's EDID in the registry, under the instance
    the key designates (`GSM774B#UID8453`: hardware `GSM774B`, instance
    ending in `UID8453`). It stays there while the screen is off, and can
    be read without administrator rights. It doesn't change: read it once.
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


# ------------------------------------------------------------ physical outputs

# DISPLAYCONFIG_VIDEO_OUTPUT_TECHNOLOGY output technologies that don't
# denote a physical screen: a virtual screen (Parsec, Sunshine, spacedesk...)
# and a wireless screen. A USB dock (INDIRECT_WIRED), however, is real.
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
    """A screen plugged into an active output, as seen by QueryDisplayConfig."""

    key: str  # same key as MonitorInfo
    technology: int  # HDMI, DisplayPort, virtual...
    source: tuple[int, int, int]  # adapter and source: shared = mirror
    edid_name: str  # the model, read from the EDID ("LG ULTRAGEAR")

    @property
    def virtual(self) -> bool:
        return self.technology in VIRTUAL_TECHNOLOGIES


def list_outputs() -> dict[str, DisplayOutput] | None:
    """Screens on the active outputs, by key; None if Windows doesn't answer.

    EnumDisplayMonitors tells neither whether a screen is virtual, nor
    whether two mirrored screens share the same image: it returns only one
    of them. The display configuration, on the other hand, details every
    output.
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
        return None  # before Windows 7
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
    """Active screens, excluding virtual and wireless ones.

    Without a readable display configuration, keep them all: better one
    virtual screen too many than a real screen ignored.
    """
    found = list_monitors()
    if outputs is None:
        return found
    return [m for m in found if not (m.key in outputs and outputs[m.key].virtual)]


def monitor_keys() -> set[str]:
    """Keys of the currently active screens."""
    return {monitor.key for monitor in list_monitors()}


# Two edges less than this many pixels apart are considered adjacent:
# Windows aligns them to the pixel, but scaling can shift them.
EDGE_TOLERANCE_PX = 16
# A top or bottom screen whose centre is further from the primary
# screen's centre than this fraction of its width is called shifted.
OFFSET_RATIO = 0.1


def _names(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return t("{first} and {last}", first=", ".join(items[:-1]), last=items[-1])


def arrangement(
    screens: list[MonitorInfo], names: dict[str, str]
) -> list[tuple[MonitorInfo, str, str]]:
    """The screen layout as Windows defines it, in plain words.

    Returns, for each screen, its name and its place: left of, centred on
    or right of the primary screen, at the top or the bottom, shifted to
    one side, and above -- or below -- which other screens. `names` gives
    the name of the outlet powering each screen, by key; failing that, the
    driver name and the resolution.

    The application stores none of this: Windows is the authority, and it
    is read again every time.
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
            # Above or below the primary screen: what matters then is the
            # side it overhangs.
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
        # The neighbours below or above, edge to edge.
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
