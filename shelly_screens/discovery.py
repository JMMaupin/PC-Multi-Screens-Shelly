"""Localisation de la power strip sur le reseau local.

L'appareil est en DHCP : son adresse peut changer. On resout donc dans cet
ordre, du moins cher au plus cher :

1. l'adresse memorisee dans la configuration ;
2. le nom mDNS `<device-id>.local`, que Windows sait resoudre nativement ;
3. un balayage des sous-reseaux des cartes reseau actives.

Chaque candidat est valide en interrogeant /shelly, qui renvoie l'identite de
l'appareil sans authentification -- on verifie ainsi qu'on parle bien a NOTRE
multiprise et pas a un autre Shelly de la maison.
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
# Une premiere resolution mDNS interroge le reseau en multicast et attend
# la reponse : trois secondes ne sont pas rares, la ou une adresse deja
# connue repond en quelques dizaines de millisecondes. Avec le delai
# ordinaire, le nom etait abandonne avant d'avoir repondu et l'on
# retombait sur l'adresse memorisee -- justement celle qu'un bail DHCP
# rend caduque. Le systeme met ensuite le resultat en cache : ce delai ne
# se paie qu'une fois.
MDNS_TIMEOUT = 4.0
SCAN_TIMEOUT = 1.0
SCAN_WORKERS = 128
# Applications connues portant au moins une sortie commandable. La liste sert
# a trier les resultats d'un balayage : un capteur ou une passerelle n'a rien
# a faire dans la liste des prises. Elle n'est pas exhaustive -- un appareil
# absent d'ici reste ajoutable a la main, et un appareil deja connu est
# reconnu par son adresse MAC quelle que soit son application.
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
    """Ce que /shelly nous apprend d'un appareil joignable."""

    host: str
    device_id: str
    mac: str
    model: str
    app: str
    gen: int
    firmware: str
    auth_enabled: bool

    @property
    def mdns_name(self) -> str:
        return f"{self.device_id}.local"


def address_of(host: str) -> str:
    """Adresse IPv4 derriere un hote, qu'il soit deja une adresse ou un nom.

    On joint souvent l'appareil par son nom mDNS, plus stable que son bail
    DHCP. Pratique pour le programme, mais l'adresse reste ce qu'on veut
    lire pour ouvrir l'interface web ou reperer un changement de bail.
    """
    if not host:
        return ""
    try:
        return socket.gethostbyname(host)
    except (OSError, UnicodeError):
        return ""


def ipv4_host(host: str) -> str:
    """Rend un hote joignable en IPv4, en resolvant les noms si besoin.

    Ces appareils annoncent aussi des adresses IPv6, dont une lien-local
    `fe80::`. Python la choisit parfois en premier et echoue aussitot :
    une adresse lien-local exige un identifiant de portee que la resolution
    ne fournit pas, d'ou le `connect(): flowinfo must be 0-1048575` qui a
    fait echouer une reprise de veille. On tranche donc nous-memes, sur le
    seul protocole que ces appareils servent vraiment.

    En cas d'echec on rend le nom tel quel : mieux vaut laisser urllib
    tenter sa chance que refuser la connexion d'office.
    """
    if not host or host.replace(".", "").isdigit():
        return host
    try:
        return socket.gethostbyname(host)
    except (OSError, UnicodeError):
        return host


def probe(host: str, timeout: float = PROBE_TIMEOUT) -> DeviceIdentity | None:
    """Interroge /shelly ; renvoie l'identite si c'est bien un Shelly."""
    try:
        # Meme precaution que pour les appels RPC : on vise l'IPv4, mais
        # l'identite gardera le nom, qui seul survit a un bail DHCP.
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
    """Retrouve l'appareil, en preferant les pistes les moins couteuses."""
    # Le nom mDNS passe avant l'hote memorise, meme quand celui-ci repond
    # encore. Un bail DHCP se renouvelle sans prevenir, et l'adresse
    # retenue finit gravee ailleurs -- dans le script embarque, qui ne se
    # corrige pas tout seul et cesserait de commander les prises en
    # silence. Le nom, lui, suit l'appareil. L'hote memorise reste essaye
    # juste apres, pour les reseaux ou la resolution mDNS ne passe pas.
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
    """Balaye les sous-reseaux locaux a la recherche d'une power strip Shelly."""
    for identity in scan_network_all():
        if _matches(identity, expected_mac):
            return identity
    return None


def scan_network_all(every_shelly: bool = False) -> list[DeviceIdentity]:
    """Renvoie les appareils Shelly visibles sur les reseaux locaux.

    Par defaut seuls ceux susceptibles de porter des prises sont retenus ;
    `every_shelly` laisse tout passer, pour une recherche manuelle.
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
    """Decide si l'appareil trouve est celui qu'on cherche.

    Quand on connait son adresse MAC, elle tranche seule : l'appareil a deja
    ete adopte, son type importe peu. Sans MAC on cherche a l'aveugle, et on
    s'en tient alors aux applications susceptibles de porter des prises.
    """
    if expected_mac:
        return identity.mac.upper() == expected_mac.upper()
    return identity.app in SUPPORTED_APPS


def _candidate_addresses() -> set[str]:
    """Adresses a sonder : tous les hotes des sous-reseaux IPv4 prives locaux."""
    addresses: set[str] = set()
    for network in _local_networks():
        # Au-dela d'un /22 le balayage devient trop long pour un demarrage.
        if network.num_addresses > 1024:
            continue
        for address in network.hosts():
            addresses.add(str(address))
    return addresses


def _local_networks() -> list[ipaddress.IPv4Network]:
    """Sous-reseaux IPv4 prives auxquels ce PC est rattache."""
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
    """Couples (adresse, masque) des cartes reseau actives, via PowerShell."""
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
    """Repli minimal : l'adresse locale principale, supposee en /24."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe_socket:
            probe_socket.connect(("8.8.8.8", 80))
            return [(probe_socket.getsockname()[0], "24")]
    except OSError:
        return []
