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
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

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
    """
    if not host:
        return ""
    try:
        return socket.gethostbyname(host)
    except (OSError, UnicodeError):
        return ""


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
    """
    if not host or host.replace(".", "").isdigit():
        return host
    try:
        return socket.gethostbyname(host)
    except (OSError, UnicodeError):
        return host


def probe(host: str, timeout: float = PROBE_TIMEOUT) -> DeviceIdentity | None:
    """Query /shelly; return the identity if it really is a Shelly."""
    try:
        # Same precaution as for RPC calls: we target IPv4, but the identity
        # keeps the name, which alone survives a DHCP lease.
        with urllib.request.urlopen(
            f"http://{ipv4_host(host)}/shelly", timeout=timeout
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
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
    """Sweep the local subnets looking for a Shelly power strip."""
    for identity in scan_network_all():
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


def _local_networks() -> list[ipaddress.IPv4Network]:
    """Private IPv4 subnets this PC is attached to."""
    networks: list[ipaddress.IPv4Network] = []
    for ip, netmask in _local_interfaces():
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


def _local_interfaces() -> list[tuple[str, str]]:
    """(address, mask) pairs of the active network adapters, via PowerShell."""
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
        return _fallback_interfaces()
    if isinstance(entries, dict):
        entries = [entries]
    result: list[tuple[str, str]] = []
    for entry in entries:
        ip = str(entry.get("IPAddress", ""))
        prefix = entry.get("PrefixLength")
        if ip and prefix is not None:
            result.append((ip, str(prefix)))
    return result or _fallback_interfaces()


def _fallback_interfaces() -> list[tuple[str, str]]:
    """Minimal fallback: the main local address, assumed to be a /24."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe_socket:
            probe_socket.connect(("8.8.8.8", 80))
            return [(probe_socket.getsockname()[0], "24")]
    except OSError:
        return []
