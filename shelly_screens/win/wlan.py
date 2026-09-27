"""The PC's Wi-Fi adapter, driven through netsh: list, join, leave.

Used for a Shelly's first setup: we must join the access point the device
opens, long enough to give it the home Wi-Fi. netsh is present on every
Windows, and a profile limited to the current user doesn't require
administrator rights.

Its output is translated according to the Windows language. So we only
read what isn't -- the word SSID and the values --, and judge success not
from its messages but by reaching the device.
"""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import tempfile
import time
from ctypes import wintypes
from xml.sax.saxutils import escape

CREATE_NO_WINDOW = 0x08000000  # no flashing console window

# "SSID 3 : name" in the network list, "SSID : name" for the adapter.
_NETWORK_LINE = re.compile(r"^\s*SSID\s+\d+\s*:\s?(.*)$")
_SIGNAL_LINE = re.compile(r"^\s*Signal\s*:\s*(\d+)\s*%")
# The word changes with the Windows language; the number doesn't.
_CHANNEL_LINE = re.compile(r"^\s*(?:Channel|Canal|Kanal|Canale)\s*:\s*(\d+)", re.I)
_CONNECTED_LINE = re.compile(r"^\s*SSID\s*:\s?(.*)$")

_OPEN_PROFILE = """<?xml version="1.0"?>
<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">
  <name>{name}</name>
  <SSIDConfig><SSID><name>{name}</name></SSID></SSIDConfig>
  <connectionType>ESS</connectionType>
  <connectionMode>manual</connectionMode>
  <MSM><security><authEncryption>
    <authentication>open</authentication>
    <encryption>none</encryption>
    <useOneX>false</useOneX>
  </authEncryption></security></MSM>
</WLANProfile>
"""


def _netsh(*args: str, timeout: float = 20.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["netsh", "wlan", *args],
        capture_output=True, text=True, encoding="oem", errors="replace",
        timeout=timeout, creationflags=CREATE_NO_WINDOW,
    )


# --- forced scan, through the native Wi-Fi API: netsh can't request one


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


class _INTERFACE_INFO(ctypes.Structure):
    _fields_ = [("InterfaceGuid", _GUID),
                ("strInterfaceDescription", ctypes.c_wchar * 256),
                ("isState", ctypes.c_uint)]


class _INTERFACE_INFO_LIST(ctypes.Structure):
    _fields_ = [("dwNumberOfItems", wintypes.DWORD), ("dwIndex", wintypes.DWORD),
                ("InterfaceInfo", _INTERFACE_INFO * 1)]


SCAN_WAIT_S = 4.0  # a full channel scan takes three to four seconds


def scan() -> None:
    """Ask the adapter for a fresh scan, and wait for the result.

    While connected, Windows hardly refreshes its network list any more: it
    would then show only the 5 GHz access point of a dual-band network. The
    scan is done by the PC's adapter; it doesn't touch any device.
    """
    try:
        wlanapi = ctypes.WinDLL("wlanapi")
    except OSError:
        return
    handle, version = wintypes.HANDLE(), wintypes.DWORD()
    if wlanapi.WlanOpenHandle(2, None, ctypes.byref(version), ctypes.byref(handle)):
        return
    try:
        interfaces = ctypes.POINTER(_INTERFACE_INFO_LIST)()
        if wlanapi.WlanEnumInterfaces(handle, None, ctypes.byref(interfaces)):
            return
        try:
            count = interfaces.contents.dwNumberOfItems
            items = ctypes.cast(
                ctypes.addressof(interfaces.contents.InterfaceInfo),
                ctypes.POINTER(_INTERFACE_INFO * count),
            ).contents
            for item in items:
                wlanapi.WlanScan(handle, ctypes.byref(item.InterfaceGuid), None, None, None)
        finally:
            wlanapi.WlanFreeMemory(interfaces)
    finally:
        wlanapi.WlanCloseHandle(handle, None)
    time.sleep(SCAN_WAIT_S)


def visible_networks() -> list[tuple[str, int, int]]:
    """Access points the adapter sees: SSID, signal in percent, channel.

    One line per access point, not per network: a dual-band network carries
    the same name on 2.4 and 5 GHz, and keeping only its best access point
    made the 2.4 GHz band disappear -- the one that matters for a device
    that only picks up that band. From best signal to worst. The channel
    is 0 if Windows speaks a language whose word we don't know.

    Empty if the adapter is missing, turned off, or if Windows refuses the
    list -- since Windows 11 24H2, location permission is required.
    """
    try:
        output = _netsh("show", "networks", "mode=bssid").stdout
    except (OSError, subprocess.SubprocessError):
        return []
    bssids: dict[tuple[str, int], int] = {}
    ssid = None
    signal = 0
    for line in output.splitlines():
        match = _NETWORK_LINE.match(line)
        if match:
            ssid = match.group(1).strip()
            continue
        match = _SIGNAL_LINE.match(line)
        if match:
            signal = int(match.group(1))
            continue
        match = _CHANNEL_LINE.match(line)
        if match and ssid:
            # The channel follows each access point's signal: the pair is complete.
            key = (ssid, int(match.group(1)))
            bssids[key] = max(bssids.get(key, 0), signal)
    return sorted(
        ((name, sig, channel) for (name, channel), sig in bssids.items()),
        key=lambda item: -item[1],
    )


def connected_ssid() -> str | None:
    """The network the adapter is connected to, or None."""
    try:
        output = _netsh("show", "interfaces").stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in output.splitlines():
        match = _CONNECTED_LINE.match(line)
        if match and match.group(1).strip():
            return match.group(1).strip()
    return None


def join_open_network(ssid: str) -> bool:
    """Join an open network -- the access point of a new Shelly.

    Returns True if netsh accepted the request; the connection itself takes
    a few seconds, and it is confirmed by reaching the device.
    """
    handle, path = tempfile.mkstemp(suffix=".xml", prefix="shelly-ap-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as profile:
            profile.write(_OPEN_PROFILE.format(name=escape(ssid)))
        added = _netsh("add", "profile", f"filename={path}", "user=current")
        if added.returncode != 0:
            return False
        return _netsh("connect", f"name={ssid}", f"ssid={ssid}").returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def disconnect() -> None:
    """Disconnect the adapter, without deleting anything."""
    try:
        _netsh("disconnect")
    except (OSError, subprocess.SubprocessError):
        pass


def forget(ssid: str) -> None:
    """Leave this network and delete the profile created for it."""
    try:
        _netsh("disconnect")
        _netsh("delete", "profile", f"name={ssid}")
    except (OSError, subprocess.SubprocessError):
        pass


def reconnect(ssid: str) -> None:
    """Return to a network Windows already knows."""
    try:
        _netsh("connect", f"name={ssid}")
    except (OSError, subprocess.SubprocessError):
        pass
