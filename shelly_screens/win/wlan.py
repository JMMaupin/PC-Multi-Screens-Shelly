"""La carte Wi-Fi du PC, pilotee par netsh : lister, rejoindre, quitter.

Sert a la premiere mise en service d'un Shelly : il faut rejoindre le point
d'acces que l'appareil ouvre, le temps de lui donner le Wi-Fi de la maison.
netsh est present sur tout Windows, et un profil limite a l'utilisateur
courant ne demande pas de droits d'administrateur.

Sa sortie est traduite selon la langue de Windows. On n'en lit donc que ce
qui ne l'est pas -- le mot SSID et les valeurs --, et l'on juge du succes
non sur ses messages mais en joignant l'appareil.
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

CREATE_NO_WINDOW = 0x08000000  # pas de console qui clignote

# « SSID 3 : nom » dans la liste des reseaux, « SSID : nom » pour la carte.
_NETWORK_LINE = re.compile(r"^\s*SSID\s+\d+\s*:\s?(.*)$")
_SIGNAL_LINE = re.compile(r"^\s*Signal\s*:\s*(\d+)\s*%")
# Le mot change avec la langue de Windows ; le numero, non.
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


# --- scan force, par l'API Wi-Fi native : netsh ne sait pas le demander


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


SCAN_WAIT_S = 4.0  # un scan complet des canaux prend trois a quatre secondes


def scan() -> None:
    """Demande a la carte un scan frais, et en attend le resultat.

    Connecte, Windows ne rafraichit presque plus sa liste des reseaux : il
    ne montrait alors d'un reseau double bande que sa borne 5 GHz. Le scan
    est fait par la carte du PC ; il ne touche a aucun appareil.
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
    """Bornes que voit la carte : SSID, signal en pourcentage, canal.

    Une ligne par borne, et non par reseau : un reseau double bande porte
    le meme nom en 2,4 et en 5 GHz, et ne garder que sa meilleure borne
    faisait disparaitre la bande 2,4 GHz -- celle qui compte pour un
    appareil qui ne capte qu'elle. Du meilleur signal au pire. Le canal
    vaut 0 si Windows parle une langue dont on ignore le mot.

    Vide si la carte manque, est coupee, ou si Windows refuse la liste --
    depuis Windows 11 24H2, il faut l'autorisation de localisation.
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
            # Le canal suit le signal de chaque borne : la paire est complete.
            key = (ssid, int(match.group(1)))
            bssids[key] = max(bssids.get(key, 0), signal)
    return sorted(
        ((name, sig, channel) for (name, channel), sig in bssids.items()),
        key=lambda item: -item[1],
    )


def connected_ssid() -> str | None:
    """Le reseau auquel la carte est connectee, ou None."""
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
    """Rejoint un reseau ouvert -- le point d'acces d'un Shelly neuf.

    Rend vrai si netsh a accepte la demande ; la connexion elle-meme prend
    quelques secondes, et c'est en joignant l'appareil qu'on la constate.
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
    """Deconnecte la carte, sans rien effacer."""
    try:
        _netsh("disconnect")
    except (OSError, subprocess.SubprocessError):
        pass


def forget(ssid: str) -> None:
    """Quitte ce reseau et efface le profil cree pour lui."""
    try:
        _netsh("disconnect")
        _netsh("delete", "profile", f"name={ssid}")
    except (OSError, subprocess.SubprocessError):
        pass


def reconnect(ssid: str) -> None:
    """Revient a un reseau deja connu de Windows."""
    try:
        _netsh("connect", f"name={ssid}")
    except (OSError, subprocess.SubprocessError):
        pass
