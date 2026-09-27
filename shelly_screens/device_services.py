"""Optional services of a Shelly device, and what they cost.

A Shelly leaves the factory with half a dozen services enabled, designed
to cover every conceivable use. None of them is needed here: the app
drives the outlets through the local API, and nothing else.

Yet each one keeps its network stack alive and takes its share of memory.
On the power strip that runs the scripts, the measurement was clear --
disabling Matter and the Cloud raised the minimum free memory from 88 KB to
156 KB, after two restarts triggered by the firmware watchdog. What serves
no purpose can therefore do harm.

This module decides nothing: it describes, reads and writes. The choice is
left to the user, who alone knows what they will plug in tomorrow.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Service:
    """An optional service, and what it takes to present it honestly."""

    key: str  # component name in the Shelly API
    label: str  # displayed title
    purpose: str  # what it is for, in general
    verdict: str  # why it is not needed here
    # Some services refuse to be turned off on some firmwares. We find
    # out by reading back rather than assuming it.


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
    """State of each service, `None` when the device knows nothing of it.

    A component missing from the configuration does not exist on this
    model, or cannot be configured: we tell it apart from a service that is
    merely off, so as not to offer a switch that controls nothing.
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
    """Enable or disable a service; tell whether a restart is needed.

    The method name is derived from the component: `matter` becomes
    `Matter.SetConfig`, `ble` becomes `BLE.SetConfig`. Capitalisation does
    not follow a single rule, hence this table.
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
    """True if some settings are waiting for a restart to take effect."""
    status = device.call("Sys.GetStatus") or {}
    return bool(status.get("restart_required", False))


def memory(device) -> tuple[int, int, int]:
    """Free memory, lowest point reached and total size, in bytes.

    The minimum is the telling figure: it shows whether the device came
    close to running dry, and it is what goes up when the load is lightened.
    """
    status = device.call("Sys.GetStatus") or {}
    return (
        int(status.get("ram_free", 0)),
        int(status.get("ram_min_free", 0)),
        int(status.get("ram_size", 0)),
    )
