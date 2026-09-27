"""First setup of a Shelly: give it the home Wi-Fi.

A new Shelly -- or one reset to factory settings -- opens its own access
point, with no password, and can then be reached at a fixed address. We
read its identity there, and send it the chosen SSID and password. Then we
wait for it to announce it has obtained an address: accepting the
configuration doesn't prove it will connect -- a wrong password is
accepted without a murmur.

We never ask it to scan for networks. It has only one radio: to scan, it
leaves its access point's channel, and some units simply close it -- the
PC then loses the device in the middle of the setup, with no way back.
The list of networks therefore comes from the PC's own scan, sitting next
to it: an indication of the signal, not an exact measurement.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from . import discovery
from .device import ShellyDevice
from .i18n import t

AP_HOST = "192.168.33.1"  # device address on its own access point
# Access point silence beyond which we consider it closed.
AP_GONE_S = 20.0

# Above this: excellent; it is what we recommend for a power strip driving
# screens -- at -79 dBm, one of them was on the verge of dropping out.
RECOMMENDED_RSSI = -60

# Channels of the 2.4 GHz band: the only one these devices can receive.
MAX_24GHZ_CHANNEL = 14


def percent_to_dbm(percent: int) -> int:
    """Windows signal percentage to dBm, the way Windows does it.

    Windows maps -100 dBm (0 %) to -50 dBm (100 %) linearly: the reverse
    conversion is exact up to rounding.
    """
    return round(percent / 2 - 100)


def signal_level(rssi: int) -> str:
    """The signal in three levels, for its colour: green, orange, red."""
    if rssi >= RECOMMENDED_RSSI:
        return "excellent"
    if rssi >= -70:
        return "good"
    return "bad"  # fair or weak: both to be avoided


def signal_quality(rssi: int) -> str:
    """How good a signal is, in one word."""
    if rssi >= RECOMMENDED_RSSI:
        return t("excellent")
    if rssi >= -70:
        return t("good")
    if rssi >= -78:
        return t("fair")
    return t("weak")


@dataclass(frozen=True)
class SetupModel:
    """A model the assistant knows how to set up."""

    name: str
    ap_prefix: str  # start of the access point name; the MAC follows
    ap_steps: str  # how to open the access point, in English (translated)


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
    """A network the PC can see, a candidate for the device."""

    ssid: str
    rssi: int | None  # dBm on 2.4 GHz; None if the PC only saw it on 5 GHz
    channel: int  # 0 if Windows didn't say

    @property
    def seen_on_24ghz(self) -> bool:
        return self.rssi is not None


def candidate_networks() -> list[Network]:
    """The networks that can be offered, best received on 2.4 GHz first.

    The band is judged access point by access point: what counts is a
    network's best 2.4 GHz access point. A network the PC only saw on 5 GHz
    is still offered, at the end of the list: a dual-band router often
    broadcasts the same name on both, and the device will tell whether it
    finds it. Shelly access points are left out -- we don't give a device
    another device's network.
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
    """Outcome of the device connecting to the chosen Wi-Fi."""

    ok: bool
    status: str  # last reported state: connecting, connected, got ip...
    ip: str = ""
    rssi: int | None = None


class AccessPoint:
    """The device, reached on its own access point."""

    def __init__(self) -> None:
        # A new device has no password.
        self.device = ShellyDevice(AP_HOST, timeout=5.0)

    def identify(self) -> discovery.DeviceIdentity | None:
        """Its identity, or None if it can't be reached (yet)."""
        return discovery.probe(AP_HOST, timeout=2.0)

    def send(self, ssid: str, password: str) -> dict:
        """Sends it the Wi-Fi to join; returns its reply."""
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
        """Waits for it to announce an address on the chosen Wi-Fi.

        The device has only one radio: when joining the network, it leaves
        its access point's channel and shuts it down for a moment. The PC
        loses it, and Windows doesn't reconnect to it on its own. Two
        witnesses, watched side by side:

        * the access point, as long as it exists: `rejoin_ap` brings the
          PC's Wi-Fi adapter back to it after every silence. It is the most
          reliable witness -- it gives the exact state, including
          "connecting" when the password is wrong;
        * the home network, where the device should appear under its mDNS
          name -- checked against its MAC. It is the only one left when the
          device ends up closing its access point.

        `on_ap_gone` is called once, after twenty seconds without an access
        point: a Wi-Fi-only PC must then get back onto its network to see
        the device. Any earlier, and we would lose the first witness.
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
            except Exception:  # noqa: BLE001 - access point moved or shut down
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
        """The device, reached on the home network -- and really it, by its MAC."""
        found = discovery.probe(host, timeout=3.0)
        if found is None or found.mac.upper() != mac.upper():
            return None
        try:
            reply = ShellyDevice(host, timeout=3.0).call("WiFi.GetStatus")
            return JoinResult(True, str(reply.get("status", "got ip")),
                              str(reply.get("sta_ip") or discovery.address_of(host)),
                              reply.get("rssi"))
        except Exception:  # noqa: BLE001 - it answers /shelly: that is proof enough
            return JoinResult(True, "got ip", discovery.address_of(host))
