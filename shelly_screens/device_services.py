"""Services optionnels d'un appareil Shelly, et ce qu'ils coutent.

Un Shelly sort d'usine avec une demi-douzaine de services actifs, pensés
pour couvrir tous les usages imaginables. Aucun n'est necessaire ici :
l'application pilote les prises par l'API locale, et rien d'autre.

Chacun garde pourtant sa pile reseau vivante et sa part de memoire. Sur la
multiprise qui porte les scripts, la mesure a ete nette -- desactiver
Matter et le Cloud a fait remonter la memoire libre minimale de 88 Ko a
156 Ko, apres deux redemarrages provoques par le chien de garde du
firmware. Ce qui ne sert pas peut donc nuire.

Ce module ne decide rien : il decrit, lit et ecrit. Le choix reste a
l'utilisateur, qui seul sait ce qu'il branchera demain.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Service:
    """Un service optionnel, et de quoi le presenter honnetement."""

    key: str  # nom du composant dans l'API Shelly
    label: str  # intitule affiche
    purpose: str  # a quoi il sert, en general
    verdict: str  # pourquoi il ne sert pas ici
    # Certains services refusent d'etre coupes sur certains firmwares.
    # On le constate a la relecture plutot que de le supposer.


SERVICES: tuple[Service, ...] = (
    Service(
        key="matter",
        label="Matter",
        purpose=(
            "Smart-home standard, so Apple Home, Google Home or Alexa can "
            "switch the outlets."
        ),
        verdict=(
            "Unused here: this app drives the outlets over the local API. "
            "Matter still keeps an IPv6 stack and a permanent mDNS "
            "announcement running for nothing."
        ),
    ),
    Service(
        key="cloud",
        label="Shelly Cloud",
        purpose=(
            "Permanent outbound link to Shelly's servers, so their phone app "
            "can reach the device from anywhere."
        ),
        verdict=(
            "Unused here: everything happens on your own network. The link "
            "is also a way in that nothing on your side controls."
        ),
    ),
    Service(
        key="ble",
        label="Bluetooth",
        purpose=(
            "Local radio, used to set the device up and to read Shelly BLE "
            "sensors."
        ),
        verdict=(
            "Unused for switching, but it is the only way to reconfigure "
            "Wi-Fi on a device that has dropped off the network. Turning it "
            "off leaves a factory reset as the only rescue."
        ),
    ),
    Service(
        key="zigbee",
        label="Zigbee",
        purpose=(
            "Radio for Zigbee networks, so a hub can switch the outlets. It "
            "only exists on the Zigbee firmware variant."
        ),
        verdict=(
            "Unused here, and worst of all when it cannot join a network: it "
            "keeps retrying, which is exactly the kind of load that trips the "
            "firmware watchdog. On the standard firmware the row stays greyed."
        ),
    ),
    Service(
        key="mqtt",
        label="MQTT",
        purpose=(
            "Publishes readings to a message broker, for home-automation "
            "systems that subscribe to them."
        ),
        verdict="Unused here: nothing subscribes, and there is no broker.",
    ),
    Service(
        key="knx",
        label="KNX",
        purpose="Building-automation bus, used in wired installations.",
        verdict="Unused here: there is no KNX bus on this network.",
    ),
    Service(
        key="ws",
        label="Outbound WebSocket",
        purpose="Pushes status to a server you run, without being asked.",
        verdict="Unused here: this app queries the device itself.",
    ),
)


def read_states(device) -> dict[str, bool | None]:
    """Etat de chaque service, `None` quand l'appareil n'en sait rien.

    Un composant absent de la configuration n'existe pas sur ce modele, ou
    ne se laisse pas regler : on le distingue d'un service simplement
    eteint, pour ne pas proposer un interrupteur qui ne commande rien.
    """
    config: dict[str, Any] = device.call("Shelly.GetConfig") or {}
    states: dict[str, bool | None] = {}
    for service in SERVICES:
        section = config.get(service.key)
        if not isinstance(section, dict) or "enable" not in section:
            states[service.key] = None
            continue
        states[service.key] = bool(section["enable"])
    return states


def set_state(device, key: str, enabled: bool) -> bool:
    """Active ou coupe un service ; dit si un redemarrage est necessaire.

    Le nom de la methode se deduit du composant : `matter` devient
    `Matter.SetConfig`, `ble` devient `BLE.SetConfig`. Les majuscules ne
    suivent pas une regle unique, d'ou cette table.
    """
    methods = {
        "matter": "Matter.SetConfig",
        "cloud": "Cloud.SetConfig",
        "ble": "BLE.SetConfig",
        "zigbee": "Zigbee.SetConfig",
        "mqtt": "MQTT.SetConfig",
        "knx": "KNX.SetConfig",
        "ws": "Ws.SetConfig",
    }
    method = methods.get(key)
    if method is None:
        raise ValueError(f"Unknown service '{key}'")
    result = device.call(method, {"config": {"enable": bool(enabled)}}) or {}
    return bool(result.get("restart_required", False))


def restart_required(device) -> bool:
    """Vrai si des reglages attendent un redemarrage pour prendre effet."""
    status = device.call("Sys.GetStatus") or {}
    return bool(status.get("restart_required", False))


def memory(device) -> tuple[int, int, int]:
    """Memoire libre, minimum atteint et taille totale, en octets.

    Le minimum est le chiffre parlant : c'est lui qui dit si l'appareil a
    frole la panne seche, et c'est lui qui remonte quand on allege.
    """
    status = device.call("Sys.GetStatus") or {}
    return (
        int(status.get("ram_free", 0)),
        int(status.get("ram_min_free", 0)),
        int(status.get("ram_size", 0)),
    )
