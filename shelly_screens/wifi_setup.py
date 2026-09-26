"""Premiere mise en service d'un Shelly : lui donner le Wi-Fi de la maison.

Un Shelly neuf -- ou remis a zero -- ouvre son propre point d'acces, sans
mot de passe, et se joint alors a une adresse fixe. On y lit son identite,
et on lui envoie le SSID et le mot de passe choisis. Puis on attend qu'il
annonce avoir obtenu une adresse : l'acceptation de la configuration ne
prouve pas qu'il se connectera -- un mot de passe faux est accepte sans
broncher.

On ne lui demande jamais de scanner les reseaux. Il n'a qu'une radio : pour
scanner, il quitte le canal de son point d'acces, et certains exemplaires
le ferment purement et simplement -- le PC perd alors l'appareil au milieu
de la mise en service, sans recours. La liste des reseaux vient donc du
scan du PC, pose a cote : une indication du signal, pas sa mesure exacte.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from . import discovery
from .device import ShellyDevice
from .i18n import t

AP_HOST = "192.168.33.1"  # adresse de l'appareil sur son propre point d'acces
# Silence du point d'acces au-dela duquel on le tient pour ferme.
AP_GONE_S = 20.0

# Au-dessus : excellent ; c'est ce qu'on recommande pour une multiprise qui
# pilote des ecrans -- a -79 dBm, l'une d'elles tenait au bord du decrochage.
RECOMMENDED_RSSI = -60

# Canaux de la bande 2,4 GHz : la seule que captent ces appareils.
MAX_24GHZ_CHANNEL = 14


def percent_to_dbm(percent: int) -> int:
    """Signal Windows en pourcentage vers des dBm, a la maniere de Windows.

    Windows etale lineairement -100 dBm (0 %) a -50 dBm (100 %) : la
    conversion inverse est exacte a l'arrondi pres.
    """
    return round(percent / 2 - 100)


def signal_level(rssi: int) -> str:
    """Le signal en trois niveaux, pour sa couleur : vert, orange, rouge."""
    if rssi >= RECOMMENDED_RSSI:
        return "excellent"
    if rssi >= -70:
        return "good"
    return "bad"  # moyen ou faible : a eviter l'un comme l'autre


def signal_quality(rssi: int) -> str:
    """Ce que vaut un signal, en un mot."""
    if rssi >= RECOMMENDED_RSSI:
        return t("excellent")
    if rssi >= -70:
        return t("good")
    if rssi >= -78:
        return t("fair")
    return t("weak")


@dataclass(frozen=True)
class SetupModel:
    """Un modele que l'assistant sait mettre en service."""

    name: str
    ap_prefix: str  # debut du nom du point d'acces ; la MAC suit
    ap_steps: str  # comment ouvrir le point d'acces, en anglais (traduit)


MODELS = (
    SetupModel(
        name="Shelly Power Strip 4 Gen4",
        ap_prefix="ShellyPStripG4-",
        ap_steps=(
            "Plug the power strip in. Press buttons 1 and 4 together and hold "
            "them for 5 seconds, then release. The four outlets blink red: the "
            "access point is open. Do not hold for 10 seconds: that would be a "
            "factory reset."
        ),
    ),
)


@dataclass(frozen=True)
class Network:
    """Un reseau que voit le PC, candidat pour l'appareil."""

    ssid: str
    rssi: int | None  # dBm en 2,4 GHz ; None si le PC ne l'a vu qu'en 5 GHz
    channel: int  # 0 si Windows ne l'a pas dit

    @property
    def seen_on_24ghz(self) -> bool:
        return self.rssi is not None


def candidate_networks() -> list[Network]:
    """Les reseaux proposables, les mieux captes en 2,4 GHz d'abord.

    La bande se juge borne par borne : c'est la meilleure borne 2,4 GHz
    d'un reseau qui compte. Un reseau que le PC n'a vu qu'en 5 GHz reste
    propose, en fin de liste : une box double bande emet souvent le meme
    nom sur les deux, et l'appareil dira s'il le trouve. Les points d'acces
    des Shelly sont ecartes -- on ne donne pas a un appareil le reseau d'un
    autre appareil.
    """
    from .win import wlan

    wlan.scan()
    best: dict[str, Network] = {}
    only_5ghz: dict[str, Network] = {}
    for ssid, percent, channel in wlan.visible_networks():
        if not ssid or ssid.startswith("Shelly"):
            continue
        if channel and channel > MAX_24GHZ_CHANNEL:
            only_5ghz.setdefault(ssid, Network(ssid, None, 0))
            continue
        network = Network(ssid, percent_to_dbm(percent), channel)
        if ssid not in best or network.rssi > best[ssid].rssi:
            best[ssid] = network
    found = sorted(best.values(), key=lambda n: -n.rssi)
    found += [n for name, n in sorted(only_5ghz.items()) if name not in best]
    return found


@dataclass(frozen=True)
class JoinResult:
    """Issue de la connexion de l'appareil au Wi-Fi choisi."""

    ok: bool
    status: str  # dernier etat annonce : connecting, connected, got ip...
    ip: str = ""
    rssi: int | None = None


class AccessPoint:
    """L'appareil, joint sur son propre point d'acces."""

    def __init__(self) -> None:
        # Un appareil neuf n'a pas de mot de passe.
        self.device = ShellyDevice(AP_HOST, timeout=5.0)

    def identify(self) -> discovery.DeviceIdentity | None:
        """Son identite, ou None si on ne le joint pas (encore)."""
        return discovery.probe(AP_HOST, timeout=2.0)

    def send(self, ssid: str, password: str) -> dict:
        """Lui envoie le Wi-Fi a rejoindre ; rend sa reponse."""
        return self.device.call("WiFi.SetConfig", {"config": {"sta": {
            "ssid": ssid, "pass": password, "enable": True,
        }}})

    def wait_joined(
        self,
        identity: "discovery.DeviceIdentity | None" = None,
        timeout: float = 60.0,
        rejoin_ap: Callable[[], None] | None = None,
        on_ap_gone: Callable[[], None] | None = None,
    ) -> JoinResult:
        """Attend qu'il annonce une adresse sur le Wi-Fi choisi.

        L'appareil n'a qu'une radio : en rejoignant le reseau, il quitte le
        canal de son point d'acces et le coupe un instant. Le PC le perd, et
        Windows ne s'y reconnecte pas de lui-meme. Deux temoins, menes de
        front :

        * le point d'acces, tant qu'il existe : `rejoin_ap` y ramene la
          carte Wi-Fi du PC a chaque silence. C'est le temoin le plus sur --
          il dit l'etat exact, « connecting » compris quand le mot de passe
          est faux ;
        * le reseau de la maison, ou l'appareil doit apparaitre sous son
          nom mDNS -- verifie a sa MAC. C'est le seul qui reste quand
          l'appareil finit par fermer son point d'acces.

        `on_ap_gone` est appele une fois, apres vingt secondes sans point
        d'acces : un PC en Wi-Fi seul doit alors retrouver son reseau pour
        voir l'appareil. Plus tot, on se priverait du premier temoin.
        """
        deadline = time.monotonic() + timeout
        status = "unknown"
        silent_since: float | None = None
        gone_called = False
        lan_host = f"{identity.device_id}.local" if identity and identity.device_id else ""
        while time.monotonic() < deadline:
            try:
                reply = self.device.call("WiFi.GetStatus")
                silent_since = None
                status = str(reply.get("status", status))
                if status == "got ip" and reply.get("sta_ip"):
                    return JoinResult(True, status, str(reply["sta_ip"]), reply.get("rssi"))
            except Exception:  # noqa: BLE001 - point d'acces deplace ou coupe
                now = time.monotonic()
                if silent_since is None:
                    silent_since = now
                if now - silent_since >= AP_GONE_S:
                    if not gone_called and on_ap_gone is not None:
                        gone_called = True
                        on_ap_gone()
                elif rejoin_ap is not None:
                    rejoin_ap()
            if silent_since is not None and lan_host:
                joined = self._find_on_network(lan_host, identity.mac)
                if joined is not None:
                    return joined
            time.sleep(1.5)
        return JoinResult(False, "access point closed" if silent_since is not None else status)

    @staticmethod
    def _find_on_network(host: str, mac: str) -> JoinResult | None:
        """L'appareil, joint sur le reseau de la maison -- et bien lui, a sa MAC."""
        found = discovery.probe(host, timeout=3.0)
        if found is None or found.mac.upper() != mac.upper():
            return None
        try:
            reply = ShellyDevice(host, timeout=3.0).call("WiFi.GetStatus")
            return JoinResult(True, str(reply.get("status", "got ip")),
                              str(reply.get("sta_ip") or discovery.address_of(host)),
                              reply.get("rssi"))
        except Exception:  # noqa: BLE001 - il repond a /shelly : c'est deja la preuve
            return JoinResult(True, "got ip", discovery.address_of(host))
