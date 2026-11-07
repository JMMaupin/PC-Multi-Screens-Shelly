"""Locating the power strip on the local network.

The device uses DHCP: its address may change. We therefore resolve in this
order, from cheapest to most expensive:

1. the address remembered in the configuration;
2. the mDNS name `<device-id>.local`, which Windows resolves natively;
3. a sweep of the subnets of the active network adapters.

Each candidate is validated by querying /shelly, which returns the device's
identity without authentication -- this checks that we are really talking to
OUR power strip and not to another Shelly in the house.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import subprocess
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .device import NETWORK_ERRORS

PROBE_TIMEOUT = 1.5
# A first mDNS resolution queries the network over multicast and waits for
# the answer: three seconds is not unusual, whereas an already known
# address answers within a few tens of milliseconds. With the ordinary
# timeout, the name was abandoned before it had answered and we fell back
# on the remembered address -- precisely the one a DHCP lease makes
# obsolete. The system then caches the result: this delay is only paid
# once.
MDNS_TIMEOUT = 4.0
SCAN_TIMEOUT = 1.0
SCAN_WORKERS = 128
# Known apps carrying at least one switchable output. The list is used to
# filter the results of a sweep: a sensor or a gateway has no place in the
# list of outlets. It is not exhaustive -- a device missing from here can
# still be added by hand, and an already known device is recognised by its
# MAC address whatever its app.
SUPPORTED_APPS = {
    "PowerStrip",
    # The same Power Strip 4 Gen4 under its newer firmware (seen in
    # October 2026: every strip of the setup switched to it).
    "PowerStripZB",
    "PlugS",
    "PlugUS",
    "PlugUK",
    "PlugIT",
    "Plus1PM",
    "Plus2PM",
    "Mini1PM",
    "Pro1PM",
    "Pro2PM",
    "Pro4PM",
    "Switch",
}
# Address of a Shelly on its own access point (`wifi_setup.AP_HOST`). Only
# the first setup may talk to it, by that literal address: a device adopted
# there would be lost as soon as the PC leaves its access point.
AP_ADDRESS = "192.168.33.1"
# The PC's networks change rarely (a cable, a VPN, the Wi-Fi of a device
# being set up), and reading them costs a PowerShell start, about a second:
# read again at most once a minute.
PC_NETWORKS_TTL_S = 60.0
_pc_networks_lock = threading.Lock()
_pc_networks: tuple[float, list[ipaddress.IPv4Network]] | None = None


@dataclass(frozen=True)
class DeviceIdentity:
    """What /shelly tells us about a reachable device."""

    host: str
    device_id: str
    mac: str
    model: str
    app: str
    gen: int
    firmware: str
    auth_enabled: bool


def address_of(host: str) -> str:
    """IPv4 address behind a host, whether it is already an address or a name.

    The device is often reached by its mDNS name, more stable than its DHCP
    lease. Handy for the program, but the address is still what one wants
    to read to open the web interface or spot a lease change.

    Only an address on the PC's network is returned, "" otherwise: a device
    whose access point is still on also announces its name there. Seen on
    2026-11-07 with a Plug M Gen3: its name resolved once to 192.168.33.1,
    and that address was recorded as the device's own.
    """
    for address in ipv4_addresses(host):
        if on_pc_network(address):
            return address
    return ""


def on_pc_network(address: str) -> bool:
    """Whether a device can be reached at `address` from this PC's own networks.

    True when the address falls within the mask of one of the PC's network
    adapters, and is not a Shelly's access point address. When the PC's
    networks cannot be read, nothing is refused: better a doubtful address
    than every device turned away.
    """
    try:
        ip = ipaddress.IPv4Address(address)
    except ValueError:
        return False
    if address == AP_ADDRESS:
        return False
    networks = pc_networks()
    if not networks:
        return True
    return any(ip in network for network in networks)


def pc_networks() -> list[ipaddress.IPv4Network]:
    """The PC's private IPv4 networks, as read from its adapters (cached)."""
    global _pc_networks
    with _pc_networks_lock:
        now = time.monotonic()
        if _pc_networks is None or now - _pc_networks[0] > PC_NETWORKS_TTL_S:
            # No guess here: the fallback assumes a /24, which would turn
            # away half of a /23 network such as this PC's.
            _pc_networks = (now, _local_networks(guess=False))
        return list(_pc_networks[1])


def ipv4_addresses(host: str) -> list[str]:
    """Every IPv4 address behind a host, in the order the system gives them."""
    if not host:
        return []
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return []
    addresses: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if address not in addresses:
            addresses.append(address)
    return addresses


def ipv4_host(host: str) -> str:
    """Make a host reachable over IPv4, resolving names if needed.

    These devices also announce IPv6 addresses, including an `fe80::`
    link-local one. Python sometimes picks it first and fails straight away:
    a link-local address requires a scope identifier that resolution does
    not provide, hence the `connect(): flowinfo must be 0-1048575` that
    made a resume from sleep fail. So we decide ourselves, on the only
    protocol these devices really serve.

    On failure we return the name as is: better to let urllib try its luck
    than to refuse the connection outright.

    An address on the PC's network is preferred (see `address_of`); failing
    one, the first address, as before. A literal address is used as is: the
    first setup reaches the device on its access point that way.
    """
    if not host or host.replace(".", "").isdigit():
        return host
    addresses = ipv4_addresses(host)
    if not addresses:
        return host
    return next((a for a in addresses if on_pc_network(a)), addresses[0])


def probe(host: str, timeout: float = PROBE_TIMEOUT) -> DeviceIdentity | None:
    """Query /shelly; return the identity if it really is a Shelly."""
    try:
        # Same precaution as for RPC calls: we target IPv4, but the identity
        # keeps the name, which alone survives a DHCP lease.
        with urllib.request.urlopen(
            f"http://{ipv4_host(host)}/shelly", timeout=timeout
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (*NETWORK_ERRORS, ValueError, json.JSONDecodeError):
        # The OverflowError among them is the one `ipv4_host` set out to
        # avoid: when the name does not resolve over IPv4, urllib still
        # tries it, and Windows may hand it an unusable IPv6 address. Missed
        # here, it aborted a whole resume from sleep (2026-10-25).
        return None
    if not isinstance(payload, dict) or "mac" not in payload or "app" not in payload:
        return None
    return DeviceIdentity(
        host=host,
        device_id=str(payload.get("id") or ""),
        mac=str(payload.get("mac") or ""),
        model=str(payload.get("model") or ""),
        app=str(payload.get("app") or ""),
        gen=int(payload.get("gen") or 0),
        firmware=str(payload.get("ver") or ""),
        auth_enabled=bool(payload.get("auth_en", False)),
    )


def resolve(
    known_host: str | None = None,
    device_id: str | None = None,
    expected_mac: str | None = None,
    allow_scan: bool = True,
) -> DeviceIdentity | None:
    """Find the device again, preferring the cheapest leads."""
    # The mDNS name comes before the remembered host, even when the latter
    # still answers. A DHCP lease renews without warning, and the stored
    # address ends up baked in elsewhere -- in the on-device script, which
    # does not fix itself and would silently stop controlling the outlets.
    # The name, on the other hand, follows the device. The remembered host
    # is still tried right after, for networks where mDNS resolution fails.
    candidates: list[str] = []
    if device_id:
        candidates.append(f"{device_id}.local")
    if known_host and known_host not in candidates:
        candidates.append(known_host)

    for candidate in candidates:
        patient = candidate.endswith(".local")
        identity = probe(candidate, MDNS_TIMEOUT if patient else PROBE_TIMEOUT)
        if identity and _matches(identity, expected_mac):
            return identity

    if not allow_scan:
        return None
    return scan_network(expected_mac=expected_mac)


def scan_network(expected_mac: str | None = None) -> DeviceIdentity | None:
    """Sweep the local subnets looking for a Shelly power strip.

    A known MAC settles the matter alone (see `_matches`): every Shelly is
    then looked at, whatever app it reports. Filtering on the app first
    missed a power strip whose firmware update had renamed it.
    """
    for identity in scan_network_all(every_shelly=bool(expected_mac)):
        if _matches(identity, expected_mac):
            return identity
    return None


def scan_network_all(every_shelly: bool = False) -> list[DeviceIdentity]:
    """Return the Shelly devices visible on the local networks.

    By default only those likely to carry outlets are kept;
    `every_shelly` lets everything through, for a manual search.
    """
    addresses = sorted(_candidate_addresses())
    if not addresses:
        return []
    found: list[DeviceIdentity] = []
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
        for identity in pool.map(lambda ip: probe(ip, SCAN_TIMEOUT), addresses):
            if identity and (every_shelly or identity.app in SUPPORTED_APPS):
                found.append(identity)
    return sorted(found, key=lambda d: d.host)


def _matches(identity: DeviceIdentity, expected_mac: str | None) -> bool:
    """Decide whether the device found is the one we are looking for.

    When its MAC address is known, it settles the matter alone: the device
    has already been adopted, its type matters little. Without a MAC we are
    searching blind, and then stick to apps likely to carry outlets.
    """
    if expected_mac:
        return identity.mac.upper() == expected_mac.upper()
    return identity.app in SUPPORTED_APPS


def _candidate_addresses() -> set[str]:
    """Addresses to probe: every host of the local private IPv4 subnets."""
    addresses: set[str] = set()
    for network in _local_networks():
        # Beyond a /22 the sweep takes too long for a startup.
        if network.num_addresses > 1024:
            continue
        for address in network.hosts():
            addresses.add(str(address))
    return addresses


def _local_networks(guess: bool = True) -> list[ipaddress.IPv4Network]:
    """Private IPv4 subnets this PC is attached to.

    `guess=False` gives up rather than fall back on a /24 around the main
    address when the adapters cannot be read.
    """
    networks: list[ipaddress.IPv4Network] = []
    for ip, netmask in _local_interfaces(guess):
        try:
            interface = ipaddress.IPv4Interface(f"{ip}/{netmask}")
        except ValueError:
            continue
        if not interface.ip.is_private or interface.ip.is_loopback:
            continue
        network = interface.network
        if network not in networks:
            networks.append(network)
    return networks


def _local_interfaces(guess: bool = True) -> list[tuple[str, str]]:
    """(address, mask) pairs of the active network adapters, via PowerShell."""
    fallback = _fallback_interfaces if guess else list
    command = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "Get-NetIPAddress -AddressFamily IPv4 | "
        "Select-Object IPAddress,PrefixLength | ConvertTo-Json -Compress",
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        entries = json.loads(completed.stdout or "[]")
    except (OSError, ValueError, subprocess.SubprocessError):
        return fallback()
    if isinstance(entries, dict):
        entries = [entries]
    result: list[tuple[str, str]] = []
    for entry in entries:
        ip = str(entry.get("IPAddress", ""))
        prefix = entry.get("PrefixLength")
        if ip and prefix is not None:
            result.append((ip, str(prefix)))
    return result or fallback()


def _fallback_interfaces() -> list[tuple[str, str]]:
    """Minimal fallback: the main local address, assumed to be a /24."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe_socket:
            probe_socket.connect(("8.8.8.8", 80))
            return [(probe_socket.getsockname()[0], "24")]
    except OSError:
        return []
