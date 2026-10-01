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
import struct
import winreg
from ctypes import wintypes
from dataclasses import dataclass
from typing import Sequence

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
DISPLAY_DEVICE_ACTIVE = 0x00000001  # for a monitor: the one its output shows on
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
    """Readable name and stable key of the screen a display output shows on.

    An output may list several monitors: those it has driven since boot
    stay there, inactive. Once a ghost is taken off the desktop and its
    output handed to another screen, the ghost even comes first. Only the
    active one is the screen actually shown; the first one is a fallback.
    """
    found = None
    index = 0
    while True:
        device = DISPLAY_DEVICEW()
        device.cb = ctypes.sizeof(DISPLAY_DEVICEW)
        if not user32.EnumDisplayDevicesW(
            device_name, index, ctypes.byref(device), EDD_GET_DEVICE_INTERFACE_NAME
        ):
            break
        if found is None or device.StateFlags & DISPLAY_DEVICE_ACTIVE:
            found = device
        if device.StateFlags & DISPLAY_DEVICE_ACTIVE:
            break
        index += 1
    if found is None:
        # Without monitor information, fall back on the enumeration rank.
        # Less stable, but better than nothing.
        return ("Unknown display", f"device:{device_name}")
    friendly = found.DeviceString.strip() or "Display"
    return (friendly, monitor_key(found.DeviceID) or f"device:{device_name}")


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

QDC_ALL_PATHS = 1
QDC_ONLY_ACTIVE_PATHS = 2
QDC_DATABASE_CURRENT = 4
DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME = 2
DISPLAYCONFIG_MODE_INFO_TYPE_SOURCE = 1
DISPLAYCONFIG_PATH_ACTIVE = 0x1
DISPLAYCONFIG_PATH_MODE_IDX_INVALID = 0xFFFFFFFF
SDC_TOPOLOGY_EXTEND = 0x4
SDC_USE_SUPPLIED_DISPLAY_CONFIG = 0x20
SDC_VALIDATE = 0x40
SDC_APPLY = 0x80
SDC_ALLOW_CHANGES = 0x400
ERROR_ACCESS_DENIED = 5
ERROR_INVALID_PARAMETER = 87


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


user32.SetDisplayConfig.argtypes = [
    ctypes.c_uint32, ctypes.POINTER(_PATH_INFO), ctypes.c_uint32,
    ctypes.POINTER(_MODE_INFO), ctypes.c_uint32,
]
user32.SetDisplayConfig.restype = ctypes.c_long


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


@dataclass
class _DisplayConfig:
    """A display configuration: its paths (one per lit output), and their modes."""

    paths: list[_PATH_INFO]
    modes: list[_MODE_INFO]
    keys: list[str]  # the screen on each path, "" if unknown
    names: list[str]  # its model, read from the EDID


def _raw_query(flags: int) -> tuple[list[_PATH_INFO], list[_MODE_INFO]] | None:
    """Paths and modes from QueryDisplayConfig; None if Windows doesn't answer."""
    try:
        paths_count, modes_count = ctypes.c_uint32(), ctypes.c_uint32()
        if user32.GetDisplayConfigBufferSizes(
            flags, ctypes.byref(paths_count), ctypes.byref(modes_count)
        ):
            return None
        paths = (_PATH_INFO * paths_count.value)()
        modes = (_MODE_INFO * modes_count.value)()
        topology = ctypes.c_uint32()
        if user32.QueryDisplayConfig(
            flags, ctypes.byref(paths_count), paths, ctypes.byref(modes_count), modes,
            ctypes.byref(topology) if flags & QDC_DATABASE_CURRENT else None,
        ):
            return None
    except (AttributeError, OSError):
        return None  # before Windows 7
    return list(paths[: paths_count.value]), list(modes[: modes_count.value])


def _query(flags: int) -> _DisplayConfig | None:
    """The active display configuration, or the one Windows saved for the
    screens connected (QDC_DATABASE_CURRENT); None if Windows doesn't answer.
    """
    found = _raw_query(flags)
    if found is None:
        return None
    paths, modes = found
    kept = [path for path in paths if path.flags & DISPLAYCONFIG_PATH_ACTIVE]
    names = [_target_name(path.targetInfo) for path in kept]
    return _DisplayConfig(
        paths=kept,
        modes=modes,
        keys=[key for key, _name in names],
        names=[name for _key, name in names],
    )


def _target_name(target: _PATH_TARGET) -> tuple[str, str]:
    """Stable key and EDID model of the screen on an output; ("", "") if unknown."""
    name = _TARGET_DEVICE_NAME()
    name.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME
    name.header.size = ctypes.sizeof(_TARGET_DEVICE_NAME)
    name.header.adapterId = target.adapterId
    name.header.id = target.id
    if user32.DisplayConfigGetDeviceInfo(ctypes.byref(name)):
        return "", ""
    return monitor_key(name.monitorDevicePath), name.monitorFriendlyDeviceName.strip()


def list_outputs() -> dict[str, DisplayOutput] | None:
    """Screens on the active outputs, by key; None if Windows doesn't answer.

    EnumDisplayMonitors tells neither whether a screen is virtual, nor
    whether two mirrored screens share the same image: it returns only one
    of them. The display configuration, on the other hand, details every
    output.
    """
    config = _query(QDC_ONLY_ACTIVE_PATHS)
    if config is None:
        return None
    outputs: dict[str, DisplayOutput] = {}
    for path, key, edid_name in zip(config.paths, config.keys, config.names):
        if not key:
            continue
        source = path.sourceInfo
        outputs[key] = DisplayOutput(
            key=key,
            technology=int(path.targetInfo.outputTechnology),
            source=(int(source.adapterId.LowPart), int(source.adapterId.HighPart),
                    int(source.id)),
            edid_name=edid_name,
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


# ------------------------------------------------------------ ghost screens


class DisplayConfigError(OSError):
    """Windows refused a display configuration."""

    def __init__(self, code: int, what: str = "") -> None:
        if code == ERROR_ACCESS_DENIED:
            reason = "access denied (locked screen, or another session on the console)"
        else:
            reason = f"error {code}"
        super().__init__(f"{what}: {reason}" if what else reason)
        self.code = code


def connected_keys() -> set[str] | None:
    """Screens Windows sees plugged in, on the desktop or not; None if unknown.

    A ghost is among them: its cable keeps its detection alive. A screen
    whose power cut also cut its detection -- most DisplayPort screens --
    is not, and cannot be put back on the desktop before it returns.
    """
    found = _raw_query(QDC_ALL_PATHS)
    if found is None:
        return None
    keys: set[str] = set()
    seen: set[tuple[int, int, int]] = set()
    for path in found[0]:
        target = path.targetInfo
        ident = (int(target.adapterId.LowPart), int(target.adapterId.HighPart), int(target.id))
        if not target.targetAvailable or ident in seen:
            continue
        seen.add(ident)
        key, _name = _target_name(target)
        if key:
            keys.add(key)
    return keys


def desktop_screens() -> dict[str, bool] | None:
    """Screens on the desktop, as the display configuration has them: for
    each key, whether it is the primary screen. None if Windows doesn't answer.

    Read from the same source as `detach` and `reattach`, unlike
    `list_monitors`: right after a change, the two may not agree yet.
    """
    config = _query(QDC_ONLY_ACTIVE_PATHS)
    if config is None:
        return None
    screens: dict[str, bool] = {}
    for path, key in zip(config.paths, config.keys):
        if key:
            screens[key] = screens.get(key, False) or _is_primary(config, path)
    return screens


def _source_position(mode: _MODE_INFO) -> tuple[int, int] | None:
    """Top-left corner on the desktop of a source mode; None for another mode."""
    if mode.infoType != DISPLAYCONFIG_MODE_INFO_TYPE_SOURCE:
        return None
    # DISPLAYCONFIG_SOURCE_MODE: width, height, pixel format, then the position.
    _width, _height, _format, x, y = struct.unpack_from("<IIIii", bytes(mode.data))
    return x, y


def _move_source(mode: _MODE_INFO, dx: int, dy: int) -> None:
    """Shift a source mode on the desktop."""
    position = _source_position(mode)
    if position is None:
        return
    data = bytearray(bytes(mode.data))
    struct.pack_into("<ii", data, 12, position[0] + dx, position[1] + dy)
    ctypes.memmove(ctypes.addressof(mode.data), bytes(data), len(data))


def _position_of(config: _DisplayConfig, path: _PATH_INFO) -> tuple[int, int] | None:
    index = path.sourceInfo.modeInfoIdx
    if index >= len(config.modes):
        return None
    return _source_position(config.modes[index])


def _is_primary(config: _DisplayConfig, path: _PATH_INFO) -> bool:
    """The primary screen is the one whose source sits at 0,0."""
    return _position_of(config, path) == (0, 0)


def _apply_without(
    config: _DisplayConfig, off: set[str], prefer: Sequence[str] = (), validate: bool = False
) -> int:
    """Apply a configuration minus the screens `off`; Windows' error code, 0 if done.

    The modes are renumbered: only those of the outputs kept are passed on.
    If the primary screen is among those going, another one takes its
    place -- the first of `prefer` that stays, failing that any of them --:
    every screen is shifted so that it lands at 0,0, which is how Windows
    names its primary screen. Left to itself, Windows had picked a dark
    ghost, taskbar and windows included. A desktop with no screen left is
    refused without asking Windows.
    """
    kept = [(p, key) for p, key in zip(config.paths, config.keys) if key not in off]
    if not kept:
        return ERROR_INVALID_PARAMETER
    shift = (0, 0)
    if not any(_is_primary(config, p) for p, _key in kept):
        placed = {key: _position_of(config, p) for p, key in kept}
        heir = next((k for k in prefer if placed.get(k) is not None), None)
        heir = heir or next((k for k, at in placed.items() if at is not None), None)
        if heir is None:
            return ERROR_INVALID_PARAMETER
        shift = placed[heir]
    paths = (_PATH_INFO * len(kept))(*(p for p, _key in kept))
    used: list[_MODE_INFO] = []
    renumbered: dict[int, int] = {}
    for path in paths:
        for end in (path.sourceInfo, path.targetInfo):
            index = end.modeInfoIdx
            if index == DISPLAYCONFIG_PATH_MODE_IDX_INVALID or index >= len(config.modes):
                end.modeInfoIdx = DISPLAYCONFIG_PATH_MODE_IDX_INVALID
                continue
            if index not in renumbered:
                renumbered[index] = len(used)
                used.append(config.modes[index])
            end.modeInfoIdx = renumbered[index]
    modes = (_MODE_INFO * len(used))(*used)
    if shift != (0, 0):
        for mode in modes:
            _move_source(mode, -shift[0], -shift[1])
    # Never SDC_SAVE_TO_DATABASE: the change stays out of what Windows
    # restores at the next boot or hot plug.
    flags = SDC_USE_SUPPLIED_DISPLAY_CONFIG | SDC_ALLOW_CHANGES
    flags |= SDC_VALIDATE if validate else SDC_APPLY
    return int(user32.SetDisplayConfig(len(kept), paths, len(used), modes, flags))


def detach(keys: set[str], prefer: Sequence[str] = ()) -> set[str]:
    """Take screens off the Windows desktop; return those taken off.

    What "Disconnect this display" does in the display settings: the
    screen stays connected but no longer holds any part of the desktop,
    and Windows moves its windows onto the others. The change is not
    saved: after a restart, or a screen plugged in, Windows brings back
    the desktop as it was saved, these screens included. `prefer` orders
    the screens that may take over as primary, should it be going.
    """
    config = _query(QDC_ONLY_ACTIVE_PATHS)
    if config is None:
        raise DisplayConfigError(0, "display configuration unreadable")
    wanted = keys & set(config.keys)
    if not wanted:
        return set()
    code = _apply_without(config, wanted, prefer)
    if code:
        raise DisplayConfigError(code, "taking off " + ", ".join(sorted(wanted)))
    # Windows said yes; check that it did it.
    return wanted - set(desktop_screens() or {})


def reattach(keys: set[str], keep_off: set[str], prefer: Sequence[str] = ()) -> set[str]:
    """Put screens back on the desktop; return those back.

    The configuration Windows saved for the screens connected is applied
    again -- positions, resolutions, primary screen as the user arranged
    them --, minus the screens that must stay off (`keep_off`). If it no
    longer holds a screen to bring back, because the layout was saved in
    the meantime without it, every connected screen is extended, as Win+P
    > Extend does, and those to keep off are taken off again. `prefer`
    orders the screens that may take over as primary, should the saved
    primary be among those kept off.
    """
    keys = keys & (connected_keys() or set())
    if not keys:
        return set()
    saved = _query(QDC_DATABASE_CURRENT)
    code = 0
    if saved is not None and keys & set(saved.keys):
        code = _apply_without(saved, keep_off, prefer)
    active = set(desktop_screens() or {})
    if not keys <= active:
        code = int(user32.SetDisplayConfig(0, None, 0, None, SDC_APPLY | SDC_TOPOLOGY_EXTEND))
        active = set(desktop_screens() or {})
        if keep_off & active:
            try:
                detach(keep_off & active, prefer)
            except DisplayConfigError:
                pass  # what is wanted back is back; the next pass retries the rest
            active = set(desktop_screens() or {})
    back = keys & active
    if not back and code:
        raise DisplayConfigError(code, "putting back " + ", ".join(sorted(keys)))
    return back


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
