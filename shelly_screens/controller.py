"""Orchestration: devices, outlets, screens and windows.

The controller is the only piece that knows the complete sequence of a
profile change. It depends on no graphical interface, so that it stays
testable and can be driven from anywhere.

Several Shelly devices coexist -- typically two power strips, or a power
strip and a single outlet for the tower. Each one is reached independently:
an unreachable device does not prevent the others from answering, and the
report says what could not be done.

The order of operations is not trivial:

1. remember the window layout of the profile being left;
2. switch the missing screens ON first, and wait for Windows to see them.
   Switching on before switching off avoids ending up, even for a moment,
   with no screen at all -- and gives the panel its few seconds of
   initialisation;
3. then switch off what needs to be;
4. replay the layout remembered for the requested profile.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Callable

from . import device_leds, discovery
from . import screen_layout, sensing
from .config import AppConfig, DeviceConfig, Profile, ScreenPosition, parse_ref
from .device import (
    AuthenticationFailed,
    ProtectedOutlet,
    ShellyDevice,
    ShellyError,
    ShellyUnreachable,
    SwitchState,
    normalise_mac,
)
from .i18n import t
from .win import layout, monitors

# Margin left to the panel after Windows has announced the screen.
DISPLAY_GRACE_S = 1.2
# Polling period of the screen list.
POLL_INTERVAL_S = 0.4
# Gap between two layout readings that must agree.
LAYOUT_STABLE_S = 2.0
# A screen that would not come back on the desktop is tried again after
# this long, not on every refresh.
REATTACH_RETRY_S = 60.0
# Taking the same ghost off more often than this, within this window,
# means something keeps putting it back: windows would jump from screen
# to screen at every refresh. It is then left alone until the window passes.
DETACH_MAX = 10
DETACH_WINDOW_S = 600.0
# Least draw accepted for the PC's outlet while this application runs,
# when the off threshold is set even lower (see `host_pc_floor`).
HOST_PC_MIN_W = 5.0

LogFn = Callable[[str], None]


@dataclass
class ApplyReport:
    """Outcome of a profile change, for display and logs."""

    profile: str
    turned_on: list[str] = field(default_factory=list)
    turned_off: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    displays_waited_s: float = 0.0
    windows_rescued: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        parts = [f"Profile '{self.profile}'"]
        if self.turned_on:
            parts.append(f"on: {', '.join(sorted(self.turned_on))}")
        if self.turned_off:
            parts.append(f"off: {', '.join(sorted(self.turned_off))}")
        if not self.turned_on and not self.turned_off:
            parts.append("no change")
        if self.windows_rescued:
            parts.append(f"{self.windows_rescued} window(s) brought back on screen")
        if self.errors:
            parts.append(f"{len(self.errors)} error(s)")
        return " | ".join(parts)


@dataclass
class LayoutCapture:
    """Outcome of a layout capture, and what is needed to roll it back."""

    ok: bool
    message: str
    # Outlet states before the capture, and the outlets it switched on.
    states_before: dict[str, bool] = field(default_factory=dict)
    turned_on: list[str] = field(default_factory=list)


class NotConnected(RuntimeError):
    """No device reachable for now."""


class NotInControl(RuntimeError):
    """Another Windows session drives the devices."""

    def __init__(self) -> None:
        super().__init__("Another Windows session drives the devices")


class SensingRealmMissing(RuntimeError):
    """Cannot set a password without knowing the device's identity."""

    def __init__(self, key: str) -> None:
        super().__init__(
            f"Device '{key}' has never answered: connect to it once before "
            "setting a password."
        )


# A full resolution costs an mDNS query and an HTTP probe. Redoing it on
# every failed read amounts to punishing a device already in trouble: that
# is how ten resolutions were counted in ten seconds during a wake, at the
# very moment the power strip was saturating.
RESOLVE_COOLDOWN_S = 30.0
# Mains voltage never stays constant for long: it wobbles by a tenth of a
# volt over minutes. A long run of strictly identical readings is therefore
# not a measurement but a frozen value -- the firmware's metering channel
# has given out. The symptom is insidious: the device answers, the outlets
# obey, and only sleep detection reasons on a dead figure. It then no longer
# switches anything off, with nothing to flag it.
#
# Five minutes of reads, not half a minute: on a quiet grid the voltage
# holds to the tenth for three to five reads in a row, and six identical
# ones out of twelve outlets watched came up by chance -- a false alarm
# offering to restart a healthy device. A real freeze lasts until the
# device restarts, hours later: waiting five minutes loses nothing.
FROZEN_METER_READS = 60
# The signal is read separately, and rarely. A Wi-Fi link does not change
# in the blink of an eye, and every extra query weighs on a firmware whose
# fragility we learned about tonight: once a minute is plenty to see a
# degradation setting in.
SIGNAL_REFRESH_S = 60.0
# After a failure, queries are spaced out instead of kept up. We return to
# the normal pace as soon as the device answers.
READ_BACKOFF_S = (0.0, 15.0, 30.0, 60.0)


class ScreenController:
    """Drives one or more Shelly devices according to the profiles."""

    def __init__(self, app_config: AppConfig, log: LogFn | None = None) -> None:
        self.config = app_config
        self._log: LogFn = log or (lambda message: None)
        self._devices: dict[str, ShellyDevice] = {}
        self._identities: dict[str, discovery.DeviceIdentity] = {}
        # Devices that answer but reject the password. Kept apart from the
        # unreachable ones: insisting is useless, and only a reset via the
        # buttons gets out of it.
        self.auth_failures: dict[str, str] = {}
        # Time of the last attempted resolution, per device: it arms the
        # cooldown that prevents chaining one on every failure.
        self._last_resolve: dict[str, float] = {}
        # Number of consecutive read failures, and the time before which
        # retrying is pointless. A struggling device thus receives less
        # traffic, not more.
        self._read_failures: dict[str, int] = {}
        self._retry_after: dict[str, float] = {}
        # Latest voltages read per outlet, to spot a metering channel that
        # no longer moves.
        self._meter_history: dict[str, list[float]] = {}
        # Last known RSSI per device, and the time of the next re-read.
        # Absent until it could be read.
        self._signal: dict[str, int] = {}
        # Devices being searched for and connected right now.
        self._connecting: set[str] = set()
        # Wi-Fi network each device is on, and the one it is set to prefer
        # (its `sta`): on the other one, it has fallen back to its backup.
        self._network: dict[str, str] = {}
        self._preferred_network: dict[str, str] = {}
        self._signal_due: dict[str, float] = {}
        # One sequence at a time: a profile change handles power and
        # windows, two in parallel would trip over each other.
        self._lock = threading.RLock()
        # Last screen reading, to judge their stability, and reason for the
        # last refusal to capture the layout.
        self._layout_reading: tuple | None = None
        # Result of the last requested capture: its problems (empty if it
        # succeeded) and its time. Continuous capture does not touch it: its
        # refusals are the rule as soon as a screen is off, not failures.
        self.capture_problems: list[str] = []
        self.capture_attempted_at = 0.0
        # Outlets switched off whose screen remains on the Windows desktop.
        self.ghost_screens: list[str] = []
        # Last known output of each outlet, by reference: read from the
        # devices, then updated by every switching. Absent when unknown.
        self._outputs: dict[str, bool] = {}
        # Last reading of each reachable outlet, power included: what the
        # PC role is checked against.
        self.last_states: dict[str, SwitchState] = {}
        # True once the outlets on before a sleep have been noted, until the
        # wake restores them.
        self._noted_for_resume = False
        # True once the PC has been located: a PC without a battery cannot
        # change places while it runs, so the location holds until the next
        # wake. Until then, every reading tries again.
        self._location_settled = False
        self._location_note = ""
        # Called with the new location key -- None: at none -- whenever it
        # changes, from the thread that read the outlets.
        self.on_location: Callable[[str | None], None] | None = None
        # True while the identification assistant runs: it cuts each outlet
        # in turn and watches the screens, ghosts staying listed included.
        # Taking them off the desktop then would distort its measurement.
        self.identifying = False
        # Times each ghost was taken off the desktop, and when a screen that
        # would not come back may be tried again.
        self._detach_times: dict[str, list[float]] = {}
        self._reattach_retry: dict[str, float] = {}
        # Last display message logged, so as not to repeat it on every refresh.
        self._display_note = ""
        # Display changes go one at a time, under their own lock rather
        # than the sequence lock: quitting, from the main thread, must not
        # wait for a profile being applied. Nor for a thread changing the
        # display, which may be waiting for the main thread's windows to
        # take note of the change -- whether Windows waits for them is not
        # documented, so the main thread never takes this lock blocking.
        self._display_lock = threading.RLock()
        # Devices are searched for in parallel; adopting what was found is
        # done one at a time.
        self._connect_lock = threading.RLock()
        # False while another Windows session drives the devices. The
        # application stops asking anything of them then; this flag is the
        # last line: whatever path gets here, no outlet is switched.
        self.drives_devices = True

    # ------------------------------------------------------------ connection

    @property
    def online_keys(self) -> set[str]:
        return set(self._devices)

    @property
    def connected(self) -> bool:
        """True if at least one device answers."""
        return bool(self._devices)

    @property
    def fully_connected(self) -> bool:
        """True if every device of the location the PC is at answers."""
        return bool(self.config.devices) and not self.missing_here()

    def missing_here(self) -> list[str]:
        """Devices that do not answer where the PC is: every one at no location.

        Another location's devices out of reach are expected: the PC is not
        there. Counting them would leave the icon orange for a whole trip.
        """
        here = self.config.location
        return [
            device.key for device in self.config.devices
            if device.key not in self._devices
            and (here is None or self.config.location_of(device.key) == here)
        ]

    def _elsewhere(self, key: str) -> bool:
        """True for a device at another location than the settled one."""
        return (
            self._location_settled
            and self.config.location is not None
            and self.config.location_of(key) != self.config.location
        )

    def identity(self, key: str) -> discovery.DeviceIdentity | None:
        return self._identities.get(key)

    def connect_all(self, allow_scan: bool = True) -> dict[str, bool]:
        """Resolve every configured device at once; return their state by key.

        In parallel: a device out of reach costs seconds -- an mDNS name
        that does not resolve takes three, before any timeout applies, then
        a sweep of the network. One after the other, a PC away from its desk
        waited for each of the desk's power strips before reaching the one
        beside it, at every start and every wake.
        """
        keys = [device.key for device in self.config.devices]
        if not keys:
            return {}
        with ThreadPoolExecutor(max_workers=len(keys)) as pool:
            found = pool.map(
                lambda key: self.connect_device(key, allow_scan, force=True) is not None,
                keys,
            )
            return dict(zip(keys, found))

    def connect_device(
        self, key: str, allow_scan: bool = True, force: bool = False
    ) -> discovery.DeviceIdentity | None:
        """Find a device and remember its address.

        `force` overrides the cooldown: that is what the reconnect button
        does, or a startup, or a password change -- deliberate actions,
        which must not wait.
        """
        device_config = self.config.device(key)
        if device_config is None:
            return None

        now = time.monotonic()
        if not force:
            last = self._last_resolve.get(key)
            if last is not None and now - last < RESOLVE_COOLDOWN_S:
                return self._identities.get(key)
        self._last_resolve[key] = now
        # Readings leave it alone meanwhile (see `read_outlets`): a failure
        # counted while it is being found would hold it back once found.
        self._connecting.add(key)
        try:
            return self._connect(key, device_config, allow_scan)
        finally:
            self._connecting.discard(key)

    def _connect(
        self, key: str, device_config: DeviceConfig, allow_scan: bool
    ) -> discovery.DeviceIdentity | None:
        """The search and the connection proper (see `connect_device`)."""
        identity, address = self._direct_identity(device_config)
        if identity is None:
            identity = discovery.resolve(
                known_host=device_config.host or None,
                device_id=device_config.device_id or None,
                expected_mac=device_config.mac or None,
                allow_scan=allow_scan,
            )
            address = discovery.address_of(identity.host) if identity is not None else ""
        fixed = self._fixed_address(device_config, identity, address)
        # The search above runs in parallel (see `connect_all`); what follows
        # changes the configuration and saves it: one device at a time.
        with self._connect_lock:
            if identity is None:
                self._devices.pop(key, None)
                self._identities.pop(key, None)
                self._log(f"Device '{key}' not found on the local network")
                return None

            fixed_changed = device_config.fixed_ip != bool(fixed)
            changed = (
                device_config.host != identity.host or device_config.ip != address
                or fixed_changed
            )
            device_config.host = identity.host
            device_config.ip = address or device_config.ip
            device_config.fixed_ip = bool(fixed)
            device_config.device_id = identity.device_id
            device_config.mac = identity.mac
            if not device_config.kind:
                device_config.kind = identity.app

            was_connected = key in self._devices
            self._identities[key] = identity
            self._devices[key] = self._client(key, fixed)
            self._preferred_network.pop(key, None)
            # Found: the respite a failed reading imposed no longer applies,
            # it is read at once.
            self._retry_after.pop(key, None)
            self._read_failures.pop(key, None)
            if fixed_changed:
                self._log(
                    f"Device '{key}': fixed address {fixed}, reached directly" if fixed
                    else f"Device '{key}': address not fixed, reached by its name"
                )
            count = self._switch_count(key)
            if count:
                self.config.ensure_outlets(key, count)
            if not was_connected:
                # A device that shows up may be new, reset, or back from a
                # firmware change: its outputs then revert to the factory
                # setting, which turns them all on at boot -- the PC's outlet
                # included. We reapply the guarantee here rather than only at
                # app launch, since the app may have been running for hours when
                # the device has just come back to life.
                self.enforce_power_on_state(key)
                self.enforce_button_lock(key)
            if changed:
                self._save()
            # Only log what tells us something: a first connection, or an
            # address that moved. Repeating the same line on every call drowned
            # the log -- a hundred and eleven lines for two devices -- right when
            # it needed to be readable.
            if changed or not was_connected:
                where = (
                    f"{identity.host} ({address})"
                    if address and address != identity.host
                    else identity.host
                )
                self._log(f"Device '{key}' connected at {where}")
            return identity

    def _direct_identity(
        self, device_config: DeviceConfig
    ) -> tuple[discovery.DeviceIdentity | None, str]:
        """The device asked at its last known address, before any name or sweep.

        Kept only if the MAC that answers is its own: on DHCP, the address
        may have gone to another device, and the usual search -- name first,
        then a sweep -- takes over. Asked first even when its address is not
        known to be fixed: a name that no longer resolves and a sweep that
        missed it left a power strip unfound for hours while its address
        answered in forty milliseconds. (None, "") if it is not there.
        """
        if not (device_config.ip and device_config.mac):
            return None, ""
        found = discovery.probe(device_config.ip)
        if found is None or normalise_mac(found.mac) != normalise_mac(device_config.mac):
            return None, ""
        # The identity keeps the name: it alone would follow the device back
        # to DHCP, and it is what the on-device script is given.
        return replace(found, host=device_config.host or found.host), device_config.ip

    def _fixed_address(
        self,
        device_config: DeviceConfig,
        identity: discovery.DeviceIdentity | None,
        address: str,
    ) -> str:
        """`address` if the device keeps it fixed, asked at that address; "" otherwise.

        See `ShellyDevice.fixed_address`. A device that does not say --
        silent, protected by a password we lack -- stays reached by its name.
        """
        if identity is None or not address:
            return ""
        client = ShellyDevice(
            identity.host, password=device_config.get_password() or None,
            address=address, mac=identity.mac,
        )
        try:
            fixed = client.fixed_address()
        except (ShellyUnreachable, AuthenticationFailed, ShellyError):
            return ""
        return fixed if fixed == address else ""

    def _client(self, key: str, address: str = "") -> ShellyDevice:
        """The client of a device: its password, its protected outputs, its fixed address."""
        device_config = self.config.device(key)
        return ShellyDevice(
            device_config.host,
            password=device_config.get_password() or None,
            protected=self.protected_switches(key),
            address=address,
            mac=device_config.mac,
        )

    def protected_switches(self, device_key: str) -> set[int]:
        """Outputs of this device that no command may switch off."""
        return {
            outlet.switch_id
            for outlet in self.config.outlets_of(device_key)
            if outlet.never_switch_off
        }

    def refresh_protection(self) -> None:
        """Propagate the protections again to the clients already open.

        To be called as soon as a role changes: a client created before the
        marking would otherwise keep the old list, and the PC's output would
        become switchable off again.
        """
        for key, device in self._devices.items():
            device.protect(self.protected_switches(key))
            self.enforce_power_on_state(key)
            self.enforce_button_lock(key)

    def enforce_power_on_state(self, device_key: str) -> list[str]:
        """Set what each output must do when the device restarts.

        This setting lives in the power strip, out of reach of both the
        script and the app, and it alone decides the fate of the outputs at
        every boot. Shipped as `off`, it cuts everything at the slightest
        restart -- firmware update, brief power cut, watchdog -- and the PC's
        outlet with it: the machine stops dead, without any of our
        protections having a say. That is how a restart of the power strip
        cut the PC in the middle of a session.

        The PC's output and the critical outputs therefore come back on.
        The others return to their previous state: a screen power strip
        that restarts while the PC is running must give the picture back,
        and the script would not switch it back on -- it only acts on PC
        state changes, and the PC has not moved.
        """
        device = self._devices.get(device_key)
        if device is None:
            return []
        changed: list[str] = []
        for outlet in self.config.outlets_of(device_key):
            wanted = "on" if outlet.never_switch_off else "restore_last"
            try:
                current = (device.call(
                    "Switch.GetConfig", {"id": outlet.switch_id}
                ) or {}).get("initial_state")
                if current == wanted:
                    continue
                # A setting, not a switching: the output does not move.
                device.call(
                    "Switch.SetConfig",
                    {"id": outlet.switch_id, "config": {"initial_state": wanted}},
                )
            except Exception as exc:  # noqa: BLE001 - never block the connection
                self._log(f"Could not set the power-on state of {outlet.ref}: {exc}")
                continue
            changed.append(f"{outlet.ref} {current} -> {wanted}")
        if changed:
            self._log("Power-on state corrected: " + ", ".join(changed))
        return changed

    def enforce_button_lock(self, device_key: str) -> bool:
        """Detach the physical button of the PC's outlet; true if it was needed.

        A Power Strip's button toggles its outlet at the slightest press,
        bypassing all of our protections: a sweep of the broom, a cable
        being tidied, and the PC dies on the spot. Detached, the button no
        longer controls anything -- the outlet is driven by the app only.

        Like the power-on state, this setting lives in the device and is
        lost on a reset: we reapply it on every connection. A model other
        than the Power Strip lacks the component, and is not concerned.
        """
        device = self._devices.get(device_key)
        # The PC's outlet at this device's location, wherever the PC is now.
        pc = self.config.host_pc_at(self.config.location_of(device_key))
        if device is None or pc is None or pc.device != device_key:
            return False
        try:
            found = device_leds.read(device)
            if found is None:
                return False
            _settings, buttons = found
            if buttons.get(pc.switch_id) == device_leds.BUTTON_DETACHED:
                return False
            device_leds.set_button(device, pc.switch_id, detached=True)
        except Exception as exc:  # noqa: BLE001 - never block the connection
            self._log(f"Could not detach the button of {pc.ref}: {exc}")
            return False
        self._log(f"Physical button of {pc.ref} detached: it can no longer switch the PC off")
        return True

    def _switch_count(self, key: str) -> int:
        """Number of outputs actually present on a device."""
        device = self._devices.get(key)
        if device is None:
            return 0
        try:
            return device.count_switches()
        except (ShellyUnreachable, ShellyError):
            return 0

    def adopt(self, identity: discovery.DeviceIdentity, name: str = "") -> DeviceConfig:
        """Add a discovered device to the configuration."""
        existing = next(
            (d for d in self.config.devices if d.mac.upper() == identity.mac.upper()), None
        )
        if existing is not None:
            existing.host = identity.host
            self._save()
            return existing
        device_config = self.config.add_device(
            DeviceConfig(
                key="",
                device_id=identity.device_id,
                mac=identity.mac,
                host=identity.host,
                name=name,
                kind=identity.app,
            )
        )
        self.connect_device(device_config.key, allow_scan=False)
        self._save()
        self._log(f"Device '{device_config.key}' added ({identity.model} at {identity.host})")
        return device_config

    def forget_unknown(self) -> None:
        """Drop the clients of devices the configuration no longer lists.

        After unsaved hardware changes are discarded, a device added
        meanwhile disappears from the configuration: its client must go
        with it, or the application would keep commanding it.
        """
        known = self.config.device_keys()
        for key in list(self._devices):
            if key not in known:
                self._devices.pop(key, None)
                self._identities.pop(key, None)

    def forget(self, key: str) -> None:
        self._devices.pop(key, None)
        self._identities.pop(key, None)
        self.config.remove_device(key)
        self._save()
        self._log(f"Device '{key}' removed")

    def device_for(self, key: str) -> ShellyDevice:
        """Reachable device for this key, with one reconnection attempt."""
        device = self._devices.get(key)
        if device is not None:
            return device
        self.connect_device(key)
        # Check the client, not the return value: during the cooldown,
        # `connect_device` returns the already known identity without having
        # rebuilt anything. Relying on it would make the device look
        # reachable while no client exists.
        device = self._devices.get(key)
        if device is None:
            raise NotConnected(f"Device '{key}' is not reachable")
        return device

    # --------------------------------------------------------------- reading

    def read_outlets(self) -> dict[str, SwitchState]:
        """Current state of every outlet, indexed by reference.

        A silent device is simply absent from the result: the others remain
        readable, and the caller sees which outlets are missing.
        """
        states: dict[str, SwitchState] = {}
        now = time.monotonic()
        for device_config in self.config.devices:
            key = device_config.key
            # Once the PC is located, a silent device at another location is
            # left alone until the next wake: it is out of reach because the
            # PC is not there, and searching for it every minute -- a name
            # that does not resolve, a sweep of the network -- would only
            # slow down every reading.
            if key not in self._devices and self._elsewhere(key):
                continue
            # Being connected right now (at start, on wake): the connection
            # will tell. Reading it as well counted a failure, and its
            # respite then delayed the first reading once it was found.
            if key in self._connecting:
                continue
            # A device that has just failed is granted a respite. Polling it
            # again every five seconds amounted to overwhelming it at the
            # moment it was least able to stand, and that burst is what
            # preceded its two crashes.
            if now < self._retry_after.get(key, 0.0):
                continue
            try:
                switches = self.device_for(key).get_all_switches()
            except AuthenticationFailed as exc:
                # No point retrying: the device answers, it is the password
                # that does not fit.
                self.auth_failures[key] = str(exc)
                continue
            except (NotConnected, ShellyUnreachable, ShellyError):
                # The connection is no longer rebuilt on the spot: the
                # cooldown of `connect_device` will handle it on the next
                # round, once the device has calmed down.
                self._devices.pop(key, None)
                attempt = self._read_failures.get(key, 0) + 1
                self._read_failures[key] = attempt
                delay = READ_BACKOFF_S[min(attempt, len(READ_BACKOFF_S) - 1)]
                self._retry_after[key] = now + delay
                if delay:
                    self._log(
                        f"Device '{key}' unreachable ({attempt}), next try in "
                        f"{delay:.0f} s"
                    )
                continue
            self._read_failures.pop(key, None)
            self._retry_after.pop(key, None)
            self.auth_failures.pop(key, None)
            for switch_id, state in switches.items():
                ref = f"{key}:{switch_id}"
                states[ref] = state
                self._note_meter(ref, state)
            # The device has just answered: this is the right moment, and
            # the only one where we are sure not to bother it for nothing.
            if now >= self._signal_due.get(key, 0.0):
                self._refresh_signal(key, now)
        self._outputs = {ref: state.output for ref, state in states.items()}
        self.last_states = dict(states)
        self._locate()
        return states

    def output(self, ref: str) -> bool | None:
        """Last known output of an outlet, switchings included; None if unknown."""
        return self._outputs.get(ref)

    # -------------------------------------------------------------- location

    def unsettle_location(self) -> None:
        """Work out the location again at the next reading.

        On wake -- the PC may have travelled while hibernated --, and when
        a PC outlet or a device's location changes.
        """
        self._location_settled = False

    def _locate(self) -> None:
        """Find where the PC is, unless already settled."""
        if self._location_settled:
            return
        key, why = self._find_location(self.last_states)
        pc_devices = {
            pc.device for pc in map(self.config.host_pc_at, self.config.location_keys())
            if pc is not None
        }
        if key is None and self._connecting & pc_devices:
            # A PC outlet's device is still being found (at start, on
            # wake): "at no location" would only be its absence so far.
            return
        self._location_settled = key is not None
        # Logged when the conclusion changes, not when a reading moves by
        # a watt: while the PC is away, this runs at every refresh.
        note = f"{key}|{why.partition(':')[0]}"
        changed = key != self.config.location
        self.config.select_location(key)
        if changed:
            # A capture refused elsewhere, or at no location, says nothing
            # of the layout here.
            self.capture_problems = []
        if note != self._location_note:
            self._location_note = note
            if key is not None:
                self._log(f"Location: {self.config.location_label(key)} ({why})")
            else:
                self._log(f"At no known location: {why}")
        if changed and self.on_location is not None:
            self.on_location(key)

    def _find_location(self, states: dict[str, SwitchState]) -> tuple[str | None, str]:
        """The location the PC is at, and why; None when it is at none.

        The PC is where its outlet draws power: a running PC cannot be
        anywhere else. Its outlet elsewhere draws nothing, or does not
        answer at all -- the power strip is out of reach.

        Two outlets drawing like a PC -- another machine plugged in at the
        other desk -- are told apart by the screens Windows sees; failing
        that, the application stays at none rather than drive the wrong
        place. A location without a PC outlet cannot be recognised that way:
        it is chosen only if no PC outlet draws, and it is the only one
        whose devices answer.
        """
        floor = self.host_pc_floor()
        readings: dict[str, str] = {}
        drawing = []
        for location in self.config.locations:
            pc = self.config.host_pc_at(location.key)
            if pc is None:
                continue
            state = states.get(pc.ref)
            readings[location.key] = (
                f"{pc.ref} {state.apower:.0f} W" if state else f"{pc.ref} unreachable"
            )
            if state is not None and state.apower >= floor:
                drawing.append(location.key)
        if len(drawing) == 1:
            return drawing[0], f"PC outlet {readings[drawing[0]]}"
        if len(drawing) > 1:
            seen = monitors.connected_keys() or set()
            watched = [
                key for key in drawing
                if any(o.monitor_key in seen for o in self.config.outlets_at(key) if o.monitor_key)
            ]
            if len(watched) == 1:
                return watched[0], "its screens are the ones Windows sees"
            return None, "several PC outlets draw power: " + ", ".join(readings.values())
        answering = [
            location.key for location in self.config.locations
            if self.config.host_pc_at(location.key) is None
            and any(o.ref in states for o in self.config.outlets_at(location.key))
        ]
        if len(answering) == 1:
            return answering[0], "its devices answer, and it has no PC outlet"
        if readings:
            return None, "no PC outlet draws power: " + ", ".join(readings.values())
        return None, "no device answers"

    def _refresh_signal(self, key: str, now: float) -> None:
        """Re-read the Wi-Fi signal strength and network, without ever failing.

        The configured networks change only by hand: they are read once per
        connection, not every minute.
        """
        self._signal_due[key] = now + SIGNAL_REFRESH_S
        try:
            device = self._devices[key]
            status = device.call("Wifi.GetStatus") or {}
            if key not in self._preferred_network:
                config = device.call("WiFi.GetConfig") or {}
                preferred = config.get("sta") or {}
                self._preferred_network[key] = (
                    str(preferred.get("ssid") or "") if preferred.get("enable") else ""
                )
        except Exception:  # noqa: BLE001 - a nice-to-have reading, nothing more
            return
        rssi = status.get("rssi")
        if isinstance(rssi, (int, float)) and rssi:
            self._signal[key] = int(rssi)
        network = str(status.get("ssid") or "")
        if network:
            self._network[key] = network

    def wifi_signal(self, key: str) -> int | None:
        """Last known RSSI, in dBm, or None if it has not been read."""
        return self._signal.get(key)

    def wifi_network(self, key: str) -> tuple[str, bool] | None:
        """Wi-Fi network a device is on, and whether it is its backup; None if unknown.

        A device that lost its preferred network falls back to its backup
        and stays there, even once the first one is back: a power strip
        sat at -74 dBm on a distant router that way, slowing every reading.
        """
        network = self._network.get(key)
        if not network:
            return None
        preferred = self._preferred_network.get(key, "")
        return network, bool(preferred) and network != preferred

    def _note_meter(self, ref: str, state: SwitchState) -> None:
        """Remember the voltage read, to judge whether the channel is alive."""
        # An outlet that is off measures nothing: its constant zero voltage
        # does not mean the firmware has given out.
        if not state.output or state.voltage <= 0:
            self._meter_history.pop(ref, None)
            return
        readings = self._meter_history.setdefault(ref, [])
        readings.append(state.voltage)
        del readings[:-FROZEN_METER_READS]

    def frozen_meters(self) -> set[str]:
        """Outlets whose metering seems frozen."""
        return {
            ref
            for ref, readings in self._meter_history.items()
            if len(readings) >= FROZEN_METER_READS and len(set(readings)) == 1
        }

    def reboot_device(self, key: str) -> None:
        """Restart a device.

        Harmless for the outputs: their relays are bistable and keep their
        position, and `initial_state` brings the PC's outlet and the
        critical outlets back on anyway.
        """
        device = self.device_for(key)
        try:
            device.call("Shelly.Reboot")
        except Exception:  # noqa: BLE001 - the response is lost with the connection
            pass
        # The device is going away: forget everything we thought we knew about it.
        self._devices.pop(key, None)
        self._last_resolve.pop(key, None)
        for ref in list(self._meter_history):
            if ref.startswith(f"{key}:"):
                self._meter_history.pop(ref, None)
        self._log(f"Device '{key}': restart requested")

    # ------------------------------------------------------------- actions

    def set_device_password(self, key: str, password: str) -> None:
        """Enable, change or remove a device's password.

        An empty string removes authentication. The password is stored
        encrypted, and the client is rebuilt to use it right away.
        """
        device_config = self.config.device(key)
        if device_config is None:
            raise KeyError(f"Unknown device: {key}")
        realm = device_config.device_id or (self.identity(key).device_id if self.identity(key) else "")
        if not realm:
            raise SensingRealmMissing(key)
        self.device_for(key).set_password(realm, password)
        device_config.set_password(password)
        # Rebuilt like any other: its protected outputs included.
        self._devices[key] = self._client(
            key, device_config.ip if device_config.fixed_ip else ""
        )
        # The identity is a snapshot taken at connection time: without this
        # update, it would keep announcing a device without a password when
        # one has just been set. The interface relies on it to show the
        # real state, and would therefore show the opposite.
        identity = self._identities.get(key)
        if identity is not None:
            self._identities[key] = replace(identity, auth_enabled=bool(password))
        self.auth_failures.pop(key, None)
        self._save()
        self._log(f"Device '{key}': password {'set' if password else 'removed'}")

    def host_pc_floor(self) -> float:
        """Least draw of the PC's outlet while the PC runs this application.

        The off threshold: below it, the on-device script judges the PC off
        at its first reading, and switches off everything it drives.
        """
        return max(HOST_PC_MIN_W, self.config.sensing.off_threshold_w)

    def host_pc_refusal(self, ref: str) -> str:
        """Why this outlet cannot take the PC role now; empty if it can.

        A physical check, independent of any labelling: this application
        runs on the PC, so the PC is on and its outlet draws power. An
        outlet drawing less than a running PC is not the one -- a tick on
        the wrong row, most likely. Giving it the role takes the protection
        away from the real one: that is how a misplaced tick once cut the
        PC. An outlet that cannot be read is refused too: what we cannot
        check, we do not hand the PC's protection to.
        """
        outlet = self.config.outlet(ref)
        if outlet is None:
            return t("Unknown outlet: {ref}", ref=ref)
        state = self.last_states.get(ref)
        if state is None:
            return t("{outlet} cannot be read right now, and only its power draw "
                     "can show that it powers this PC. Reconnect its device and "
                     "try again.", outlet=outlet.label)
        floor = self.host_pc_floor()
        if state.apower < floor:
            return t("{outlet} draws {watts} W. This PC is running, so its outlet "
                     "draws at least {floor} W: {outlet} cannot be the one.",
                     outlet=outlet.label, watts=f"{state.apower:.0f}",
                     floor=f"{floor:.0f}")
        return ""

    def assign_host_pc(self, location: str, ref: str | None) -> list[str]:
        """Give the PC role at a location to this outlet, or to none.

        Each location has its own PC outlet. Refused (PermissionError) when
        the outlet does not draw like a running PC. The outlet losing the
        role keeps a protection, unless it is known to draw less than a
        running PC: it becomes critical, so nothing switches it off until
        someone unticks that knowingly. Returns the outlets kept critical
        that way.
        """
        if ref is not None:
            reason = self.host_pc_refusal(ref)
            if reason:
                self._log(f"PC role refused for {ref}: it does not draw like a running PC")
                raise PermissionError(reason)
            location = self.config.outlet_location(ref)
        kept: list[str] = []
        previous = self.config.host_pc_at(location)
        if previous is not None and previous.ref != ref:
            state = self.last_states.get(previous.ref)
            if (state is None or state.apower >= self.host_pc_floor()) and not previous.critical:
                previous.critical = True
                kept.append(previous.ref)
        for outlet in self.config.outlets_at(location):
            outlet.host_pc = outlet.ref == ref
        # The PC outlets say where the PC is: work it out again.
        self.config.select_location(self.config.location)
        self.unsettle_location()
        self._save()
        self._log(
            f"PC role: {previous.ref if previous else 'none'} -> {ref or 'none'}"
            + (f"; kept critical: {', '.join(kept)}" if kept else "")
        )
        # Clients already open keep their old list of protected outputs.
        self.refresh_protection()
        return kept

    def set_outlet(self, ref: str, on: bool, keep_a_screen: bool = False) -> None:
        """Operate an outlet, honouring the safeguards.

        `keep_a_screen` refuses to switch off the last screen that is on. The
        identification assistant does without it: it switches each screen
        off in turn and back on right away, that is its very principle.
        """
        if not self.drives_devices:
            raise NotInControl()
        outlet = self.config.outlet(ref)
        if outlet is not None and outlet.never_switch_off and not on:
            reason = "powers the PC" if outlet.host_pc else "is marked critical"
            raise PermissionError(f"{outlet.label} {reason} and cannot be switched off")
        if keep_a_screen and not on and outlet is not None and outlet.is_screen:
            powered = {r for r, s in self.read_outlets().items() if s.output and r != ref}
            if not self.config.leaves_a_screen(powered):
                raise PermissionError(
                    f"{outlet.label} is the last screen on and cannot be switched off"
                )
        key, switch_id = parse_ref(ref)
        self.device_for(key).set_switch(switch_id, on)
        self._outputs[ref] = on
        self._log(f"{ref} -> {'on' if on else 'off'}")
        if outlet is not None and outlet.monitor_key:
            if not on and not self.identifying:
                time.sleep(DISPLAY_GRACE_S)  # let Windows drop the screens that do go
            self.settle_ghosts()

    def apply_profile(self, name: str) -> ApplyReport:
        """Apply a profile: the screens, then the windows to rescue."""
        profile = self.config.profile(name)
        if profile is None:
            raise KeyError(f"Unknown profile: {name}")
        if self.config.location is not None and not self.config.profile_fits(profile):
            # Another desk's profile: applied here, it would switch off every
            # screen of this one, since it lists none of them.
            report = ApplyReport(profile=profile.name)
            report.errors.append(
                f"profile '{profile.name}' has no screen at "
                f"{self.config.location_label(self.config.location)}"
            )
            self._log(report.summary())
            return report
        return self._apply_targets(
            profile_name=profile.name,
            targets=self._profile_targets(profile),
            profile=profile,
        )

    def _profile_targets(self, profile: Profile) -> dict[str, bool]:
        """What a profile commands: the screens, and nothing else.

        An accessory stays as it is whatever the screen profile -- a lamp or
        speakers going dark because one picked "Gaming" was the complaint --,
        and so does an outlet whose type is not set: what we do not know, we
        do not touch. Before this rule, a profile also cut every accessory
        it did not list. Only the screens of the location the PC is at.
        """
        return {
            outlet.ref: (profile.wants(outlet.ref) or outlet.never_switch_off)
            for outlet in self.config.here_outlets()
            if outlet.is_screen
        }

    def apply_outlets(self, targets: dict[str, bool]) -> ApplyReport:
        """Apply a one-off configuration, outside of profiles.

        No profile is modified or kept as the current profile.

        Same sequence as a profile -- switch on first, switch off next,
        bring back stray windows --, but no profile is "current" any more:
        on wake, the outlets will be restored as they were before sleep,
        rather than to a profile that was left.
        """
        for ref in list(targets):
            outlet = self.config.outlet(ref)
            if outlet is not None and outlet.never_switch_off:
                targets[ref] = True
        report = self._apply_targets(
            profile_name="Custom", targets=targets, profile=None, rescue=True
        )
        self.config.settings.last_profile = ""
        self._save()
        return report

    def prepare_for_suspend(self) -> ApplyReport:
        """Switch the screens off on sleep, except what must stay on.

        During POST and the sign-in screen, nothing runs on the PC to
        control the outlets: the boot screen -- and the USB hub carrying the
        keyboard, if it is marked critical -- must therefore stay powered,
        otherwise the next boot would happen blind and with no way to type.

        Only at the location the PC is at: elsewhere, the outlets belong to
        whoever is there, and its own script handles them.
        """
        keep_on = set(self.config.shutdown_refs_on())
        targets = {outlet.ref: (outlet.ref in keep_on) for outlet in self.config.here_outlets()}
        # What is on now is what will need to be restored on wake. Note it
        # before switching off: afterwards, the information is gone.
        states = self.read_outlets()
        self.config.settings.resume_refs = [
            ref for ref, state in states.items()
            if state.output and ref not in keep_on and self.config.is_here(ref)
        ]
        self._noted_for_resume = True
        return self._apply_targets(
            profile_name="Suspend",
            targets=targets,
            profile=None,
            urgent=True,
            # On sleep, switching everything off is precisely the goal.
            keep_a_screen=False,
        )

    def remember_for_resume(self) -> list[str]:
        """Note the powered outlets, without commanding anything.

        Useful when the switching off is left to the on-device script: we
        still need to know what to restore on wake. The script is told as
        well: an accessory switched by hand since the last profile must come
        back at the next power-on as it is now.
        """
        keep_on = set(self.config.shutdown_refs_on())
        states = self.read_outlets()
        refs = [
            ref for ref, state in states.items()
            if state.output and ref not in keep_on and self.config.is_here(ref)
        ]
        self.config.settings.resume_refs = refs
        self._noted_for_resume = True
        self._save()
        try:
            sensing.publish_for_boot(self, self.config)
        except Exception as exc:  # noqa: BLE001 - never hold up the sleep
            self._log(f"Outlets to restore not published: {exc}")
        return refs

    def resume(self) -> ApplyReport | None:
        """On wake, restore what was powered before sleep.

        Screens: the last profile first, since that is the expressed intent.
        Failing that, the screens on just before the cut: without them, a
        user who had never applied a profile woke up in front of dark
        screens, with nothing to switch them back on.

        The other outlets the sleep switched off come back as they were,
        whatever the profile: it does not decide for them. Those it left
        alone are not touched -- the fallback used to switch off every
        outlet it had not noted, an accessory kept on through the night
        included.

        The PC may have travelled while hibernated: it is located again
        first, and nothing is restored if it is at no known location.
        """
        self.read_outlets()
        if self.config.location is None:
            self._log("Resume: the PC is at no known location, nothing restored")
            self._noted_for_resume = False
            return None
        refs = [r for r in self.config.settings.resume_refs if self.config.is_here(r)]
        # Only if this sleep was noted: a list left over from an earlier one
        # would switch back on an accessory switched off by hand since.
        noted, self._noted_for_resume = self._noted_for_resume, False
        targets = {
            ref: True for ref in refs
            if noted and not self.config.outlet(ref).is_screen  # type: ignore[union-attr]
        }
        name = self.config.settings.last_profile
        profile = self.config.profile(name) if name else None
        if profile is not None:
            self._log(f"Resuming profile '{name}'")
            targets.update(self._profile_targets(profile))
            return self._apply_targets(
                profile_name=profile.name, targets=targets, profile=profile
            )

        if not refs:
            self._log("Nothing to restore on resume")
            return None
        self._log(f"No profile set, restoring the outlets that were on: {refs}")
        targets.update({
            outlet.ref: (outlet.ref in refs or outlet.never_switch_off)
            for outlet in self.config.here_outlets()
            if outlet.is_screen
        })
        return self._apply_targets(
            profile_name="Resume",
            targets=targets,
            profile=None,
        )

    # ----------------------------------------------------------- sequencing

    def _apply_targets(
        self,
        profile_name: str,
        targets: dict[str, bool],
        profile: Profile | None,
        urgent: bool = False,
        rescue: bool = False,
        keep_a_screen: bool = True,
    ) -> ApplyReport:
        report = ApplyReport(profile=profile_name)

        with self._lock:
            states = self.read_outlets()
            if not states:
                report.errors.append("No Shelly device is reachable")
                return report
            # The last word, whatever the caller: nothing is switched at a
            # place the PC is not at -- another desk's screens, the suitcase's.
            if self.config.location is None:
                report.errors.append("the PC is at no known location: nothing switched")
                self._log(report.summary())
                return report
            targets = {ref: want for ref, want in targets.items() if self.config.is_here(ref)}

            if keep_a_screen:
                self._keep_a_screen(targets, states)

            # The screens before the change: a window that was on one of
            # them and is no longer on any screen is lost, not parked on purpose.
            screens_before = [m.rect for m in monitors.list_monitors()]

            # An outlet whose state is unknown is not operated: its device
            # does not answer, insisting would only mean waiting.
            to_turn_on = [
                ref for ref, want in targets.items() if want and _is_off(states, ref)
            ]
            to_turn_off = [
                ref for ref, want in targets.items() if not want and _is_on(states, ref)
            ]
            report.unchanged = [
                ref for ref in targets if ref not in to_turn_on and ref not in to_turn_off
            ]
            missing = [ref for ref in targets if ref not in states]
            if missing:
                report.errors.append(f"unreachable: {', '.join(sorted(missing))}")

            # 1. Switch on first, then let Windows discover the screens. A
            #    ghost taken off the desktop does not come back by itself:
            #    its detection never went away, Windows saw nothing change.
            expected_keys = self._expected_monitor_keys(targets)
            report.turned_on = self._switch_many(to_turn_on, True, report, urgent)
            if not urgent:
                self.settle_ghosts()
            if report.turned_on and not urgent:
                report.displays_waited_s = self._wait_for_displays(expected_keys)

            # 2. Switch off whatever remains to be switched off.
            report.turned_off = self._switch_many(to_turn_off, False, report, urgent)

            # 3. Take off the desktop the screens Windows keeps although
            #    switched off, then bring back whatever was left outside
            #    every lit screen. The pause gives Windows time to remove
            #    the switched-off screens that do go.
            if report.turned_off and not urgent:
                time.sleep(DISPLAY_GRACE_S)
            if not urgent:
                self.settle_ghosts()
            if (profile is not None or rescue) and self.config.settings.rescue_offscreen_windows:
                report.windows_rescued = self._rescue_windows(targets, screens_before)

            if profile is not None:
                self.config.settings.last_profile = profile.name
                # The on-device script must know what to switch back on at
                # the next PC boot: this is the only moment the app can
                # tell it.
                sensing.publish_profile(self, self.config, profile.name)
            self._save()

        self._log(report.summary())
        return report

    def _keep_a_screen(self, targets: dict[str, bool], states: dict[str, SwitchState]) -> None:
        """Keep one screen on if the command would leave none.

        Outside of sleep, switching off every screen leaves the PC running
        but with no picture: nothing then allows going back from the
        desktop. The safety net is set here, and not in each interface, to
        cover profiles, choices on the plan and resumes on wake all at once.
        """
        powered = {ref for ref, state in states.items() if targets.get(ref, state.output)}
        if self.config.leaves_a_screen(powered):
            return
        # Only outlets whose state is known can be commanded.
        keep = self.config.fallback_screen(set(states))
        if keep is None:
            return
        targets[keep.ref] = True
        self._log(f"No screen would stay on: keeping {keep.label} on")

    # ------------------------------------------------------------ screens

    def check_screen_layout(
        self,
        states: dict[str, SwitchState],
        links: dict[str, str] | None = None,
        unswitched: set[str] | None = None,
    ) -> tuple[list[monitors.MonitorInfo], dict, list[str]]:
        """Physical screens, outputs, and what prevents capturing their position.

        Screens plugged into another output are followed first
        (`relink_moved_screens`), unless the identification assistant
        runs: it is establishing the links itself.
        """
        outputs = monitors.list_outputs()
        physical = monitors.physical_monitors(outputs)
        if links is None and not self.identifying:
            self.relink_moved_screens(states, {m.key for m in physical})
        found = screen_layout.problems(
            # The screens of this location: another desk's are expected off.
            self.config.here_outlets(),
            states,
            {m.key for m in physical},
            outputs or {},
            set(self.config.unswitched_screens) if unswitched is None else unswitched,
            links,
        )
        return physical, outputs or {}, found

    def relink_moved_screens(
        self, states: dict[str, SwitchState], physical: set[str]
    ) -> list[str]:
        """Follow the screens plugged into another output of the graphics card.

        Moving the PC means unplugging its cables, and they rarely go back
        into the same outputs: the screens then come back under other keys
        (see win.monitors), and their links and recorded positions point
        at screens Windows no longer shows. When the deduction leaves no
        doubt (screen_layout.match_moved), they follow at once. Writing the
        new links waits for an administrator like any hardware change;
        until then, the same deduction is made again after each start.

        Returns the labels of the outlets whose screen moved.
        """
        moves = screen_layout.moved_screens(
            self.config.here_outlets(), states, physical,
            set(self.config.unswitched_screens), monitors.edid_serial,
        )
        if not moves:
            return []
        relinked = self.config.rename_screens(moves)
        self._log(
            "Screen plugged into another output, link updated: "
            + ", ".join(f"{old} -> {new}" for old, new in sorted(moves.items()))
            + (f" ({', '.join(relinked)})" if relinked else "")
        )
        self._save()
        return relinked

    def _record_screens(
        self, physical: list[monitors.MonitorInfo], outputs: dict, touch: bool
    ) -> bool:
        """Write the layout; true if it changed.

        `touch` timestamps the capture even without a change: it was
        requested, and the date then says the layout has been checked.
        """
        snapshot = [
            ScreenPosition(
                key=m.key, rect=m.rect, primary=m.is_primary,
                name=(outputs[m.key].edid_name if m.key in outputs else "")
                or m.friendly_name,
                scale=m.scale, diagonal=monitors.physical_diagonal(m.key),
            )
            for m in physical
        ]
        changed = snapshot != self.config.screens
        if not changed and not touch:
            return False
        self.config.screens = snapshot
        self.config.screens_captured_at = time.time()
        self._save()
        if changed:
            self._log(
                "Screen positions memorised: "
                + ", ".join(f"{s.key} @ {s.rect[0]},{s.rect[1]}" for s in snapshot)
            )
        return changed

    def _note_capture(self, found: list[str]) -> None:
        """Remember the outcome of a requested capture, for the interface."""
        self.capture_problems = found
        self.capture_attempted_at = time.time()

    def _note_ghosts(self, ghosts: list[str]) -> None:
        """Remember ghost screens; only log their appearance."""
        appeared = [g for g in ghosts if g not in self.ghost_screens]
        if appeared:
            self._log(
                "Ghost screen: outlet off but still on the Windows desktop: "
                + ", ".join(appeared)
            )
        self.ghost_screens = ghosts

    def remember_screens(self, states: dict[str, SwitchState]) -> bool:
        """Passive capture: keep the layout when everything agrees and holds.

        Called on every refresh. Nothing must stand in the way (see
        screen_layout) and the reading must be identical to the previous
        one: when a screen comes back, Windows rearranges the desktop in
        several steps, and a reading taken halfway would be wrong.

        It steps aside for any operation in progress: a profile being
        applied, a requested capture. Otherwise it would judge the outlet
        state read before the operation against the screens after it. Its
        refusals are not logged: with screens off, they are the rule.

        Nothing is recorded at no known location: the screens there -- a
        hotel television -- belong to no layout worth keeping.
        """
        if self.config.location is None:
            return False
        if not self._lock.acquire(blocking=False):
            return False
        try:
            physical, outputs, found = self.check_screen_layout(states)
            self._note_ghosts(
                screen_layout.ghosts(self.config.here_outlets(), states, {m.key for m in physical})
            )
            reading = tuple((m.key, m.rect, m.scale) for m in physical)
            stable = reading == self._layout_reading
            self._layout_reading = reading
            if found or not stable:
                return False
            # Everything agrees now: what stood in the way of the last
            # requested capture is gone. Left on screen, its reason would
            # contradict a map that is right.
            self.capture_problems = []
            return self._record_screens(physical, outputs, touch=False)
        finally:
            self._lock.release()

    def capture_when_ready(
        self,
        links: dict[str, str] | None = None,
        unswitched: set[str] | None = None,
    ) -> tuple[bool, str]:
        """Wait until everything agrees and settles, then capture the layout.

        Re-reads outlets and screens every two seconds, up to the screen
        wait timeout: two identical, problem-free readings are required.
        Beyond that, return the reason for the refusal.
        """
        deadline = time.monotonic() + self.config.settings.display_settle_timeout_s
        previous = None
        while True:
            states = self.read_outlets()
            physical, outputs, found = self.check_screen_layout(states, links, unswitched)
            reading = tuple((m.key, m.rect, m.scale) for m in physical)
            if not found and reading == previous:
                break
            # A first clean reading is always given its confirmation, even
            # past the deadline: when the devices are slow to answer, one
            # reading alone can outlast it, and the capture then failed on
            # a "still arranging" that nothing showed.
            if time.monotonic() >= deadline and (found or previous is not None):
                if not found:
                    found = [t("Windows is still arranging the screens")]
                self._note_capture(found)
                return False, t("Layout not captured: {reasons}",
                                reasons="; ".join(found))
            previous = None if found else reading
            time.sleep(LAYOUT_STABLE_S)
        self._note_capture([])
        self._record_screens(physical, outputs, touch=True)
        return True, t("Screen layout captured ({count} screens)", count=len(physical))

    def capture_screen_layout(
        self,
        restore: bool = True,
        progress: Callable[[str], None] | None = None,
    ) -> "LayoutCapture":
        """Switch to "All on" and capture the screen layout.

        The layout can only be read with every screen on: this is the
        procedure that establishes it on demand. It switches on what the
        built-in profile switches on -- every outlet --, without yet keeping
        it as the current profile: one may want to go back to the previous
        one (`undo_capture`) or stay that way (`adopt_profile`).

        `restore` then switches off again what was switched on for the
        occasion. Without it, the screens stay on and the caller decides:
        the interface asks whether to stay that way or go back to the
        previous profile, via `undo_capture`. `progress` receives the steps,
        to display them.
        """
        say = progress or (lambda _text: None)
        capture = LayoutCapture(False, "")
        if self.config.location is None:
            capture.message = t("The PC is at no known location: there is no layout to capture.")
            self._note_capture([capture.message])
            return capture
        screens = [o for o in self.config.here_outlets() if o.monitor_key]
        if not screens:
            capture.message = t("No outlet is linked to a screen yet: "
                                "run Identify displays first.")
            self._note_capture([capture.message])
            return capture
        report = ApplyReport(profile="Screen layout")
        keys_before = {o.ref: o.monitor_key for o in screens}
        with self._lock:
            states = self.read_outlets()
            capture.states_before = {ref: state.output for ref, state in states.items()}
            unreachable = [o.label for o in screens if o.ref not in states]
            if unreachable:
                reason = t("unreachable: {outlets}", outlets=", ".join(unreachable))
                self._note_capture([reason])
                capture.message = t("Layout not captured: {reasons}", reasons=reason)
                return capture
            to_turn_on = [
                ref for ref in self.config.all_on_profile().outlets_on
                if ref in states and not states[ref].output
            ]
            if to_turn_on:
                say(t("Switching the screens on..."))
            capture.turned_on = self._switch_many(to_turn_on, True, report)
            # Every screen must be on the desktop to be measured, a ghost
            # taken off earlier included.
            self.settle_ghosts()
            if capture.turned_on:
                say(t("Waiting for Windows to detect every screen..."))
                self._wait_for_displays({o.monitor_key for o in screens})

            say(t("Checking and capturing the layout..."))
            capture.ok, capture.message = self.capture_when_ready()
            if restore:
                self.undo_capture(capture)
        moved = [o.label for o in screens if o.monitor_key != keys_before[o.ref]]
        if moved:
            capture.message += "\n" + t(
                "Plugged into another output of the graphics card, link updated: {outlets}",
                outlets=", ".join(moved),
            )
        if report.errors:
            capture.ok = False
            capture.message += " " + "; ".join(report.errors)
        self._log(f"Screen layout: {capture.message}")
        return capture

    def adopt_profile(self, name: str) -> None:
        """Keep as current a profile whose outlets are already in place.

        After a capture one chooses to keep: everything is on, so "All on"
        is the current one -- for the menu, for wake, and for the on-device
        script that switches the screens back on at boot.
        """
        if self.config.profile(name) is None:
            return
        self.config.settings.last_profile = name
        sensing.publish_profile(self, self.config, name)
        self._save()
        self._log(f"Profile '{name}' kept as the current one")

    def undo_capture(self, capture: "LayoutCapture") -> None:
        """Switch off again what the capture switched on, and bring back stray windows."""
        if not capture.turned_on:
            return
        report = ApplyReport(profile="Screen layout")
        with self._lock:
            screens_before = [m.rect for m in monitors.list_monitors()]
            turned_off = self._switch_many(capture.turned_on, False, report)
            if turned_off:
                time.sleep(DISPLAY_GRACE_S)
                self.settle_ghosts()
                if self.config.settings.rescue_offscreen_windows:
                    self._rescue_windows(capture.states_before, screens_before)
        capture.turned_on = []

    def _rescue_windows(self, targets: dict[str, bool], screens_before: list) -> int:
        """Bring lost windows back onto the nearest lit screen.

        A screen Windows still sees is not necessarily on: a monitor powered
        by the PC's USB-C stays enumerated once its outlet is switched off,
        and a window placed on it is as lost as if the screen had
        disappeared. Screens whose outlet is switched off by the profile
        therefore do not count as usable -- unless none were left.
        """
        current = monitors.list_monitors()
        dark = set()
        for ref, want in targets.items():
            outlet = self.config.outlet(ref)
            if not want and outlet is not None and outlet.monitor_key:
                dark.add(outlet.monitor_key)
        usable = [m for m in current if m.key not in dark] or current
        primary = next((m for m in current if m.is_primary), None)
        offset = (
            (primary.work_rect[0] - primary.rect[0], primary.work_rect[1] - primary.rect[1])
            if primary is not None
            else (0, 0)
        )
        moved = layout.rescue_offscreen(
            [m.work_rect for m in usable],
            screens_before + [m.rect for m in current],
            offset,
        )
        if moved:
            self._log(
                f"Brought back {len(moved)} window(s) left outside the screens: "
                + ", ".join(entry.describe() for entry in moved)
            )
        return len(moved)

    # --------------------------------------------------------- ghost screens

    def settle_ghosts(self) -> None:
        """Keep ghost screens off the Windows desktop; put them back once lit.

        A screen on HDMI whose outlet is cut stays on the desktop (see
        screen_layout.ghosts): windows open on it, the mouse gets lost on
        it. It is taken off the desktop, as "Disconnect this display"
        would, and put back as soon as its outlet is on again -- switched
        by a profile, the menu, the device's own app or its button.

        When the primary screen goes dark on DisplayPort, it leaves Windows
        altogether, and Windows picks another primary screen among those
        still connected: the ghost, as likely as not. The ghost then hands
        the role over to a lit screen as it leaves (see `_heirs`). Should
        Windows have kept nothing but ghosts, a lit screen is brought onto
        the desktop first.

        Nothing is saved in Windows' configuration: should the PC or the
        application stop now, Windows restores the desktop as it was saved,
        the screen included. Only screens whose outlet state is known are
        taken off, never the last lit one, and none while the
        identification assistant runs.
        """
        if not self.drives_devices:
            return
        with self._display_lock:
            settings = self.config.settings
            desktop = monitors.desktop_screens()
            if desktop is None:
                return  # Windows does not answer: next pass
            detached = set(settings.detached_screens)
            lit = self._screens_lit()
            now = time.monotonic()

            # 1. Put back what is lit again or no longer linked to an
            #    outlet -- everything, once the option is turned off.
            back = {
                key for key in detached - set(desktop)
                if (not settings.detach_ghost_screens or lit.get(key, True))
                and now >= self._reattach_retry.get(key, 0.0)
            }
            if back:
                heirs = self._heirs(back | self._lit_among(desktop, lit))
                returned = self._reattach(back, detached - back - set(desktop), heirs, now)
                detached -= returned
                desktop = monitors.desktop_screens() or desktop

            # 2. Take off the ghosts: outlet off, screen on the desktop --
            #    still, or again, when Windows restored its saved layout on
            #    another screen's arrival.
            if settings.detach_ghost_screens and not self.identifying:
                ghosts = {key for key in desktop if lit.get(key) is False}
                if ghosts and not self._lit_among(desktop, lit):
                    # Only ghosts on the desktop: bring a lit screen first.
                    waiting = {
                        key for key, on in lit.items()
                        if on and key not in desktop
                        and now >= self._reattach_retry.get(key, 0.0)
                    }
                    if waiting:
                        self._reattach(waiting, ghosts, self._heirs(waiting), now)
                        desktop = monitors.desktop_screens() or desktop
                        # The ghosts kept off on the way are noted, to come back with their outlet.
                        gone = ghosts - set(desktop)
                        if gone:
                            self._log("Ghost screen taken off the Windows desktop: "
                                      + self._screen_names(gone))
                        detached |= gone
                        ghosts = {key for key in desktop if lit.get(key) is False}
                heirs = self._heirs(self._lit_among(desktop, lit))
                if ghosts and not heirs:
                    self._note_display(
                        f"Ghost screen {self._screen_names(ghosts)} left on the desktop: "
                        "no other screen there is known to be lit"
                    )
                    ghosts = set()
                ghosts = {key for key in ghosts if self._may_detach(key, now)}
                if ghosts:
                    try:
                        taken = monitors.detach(ghosts, heirs)
                    except monitors.DisplayConfigError as exc:
                        taken = set()
                        self._note_display(
                            f"Could not take ghost screen {self._screen_names(ghosts)} "
                            f"off the desktop: {exc}"
                        )
                    if taken:
                        self._display_note = ""
                        primary = next(
                            (k for k, first in (monitors.desktop_screens() or {}).items() if first),
                            "",
                        )
                        handed = any(desktop.get(key) for key in taken) and primary
                        self._log(
                            "Ghost screen taken off the Windows desktop: "
                            + self._screen_names(taken)
                            + (f" (primary screen now {self._screen_names({primary})})"
                               if handed else "")
                        )
                        for key in taken:
                            self._detach_times.setdefault(key, []).append(now)
                    elif ghosts:
                        self._note_display(
                            f"Windows kept ghost screen {self._screen_names(ghosts)} on the desktop"
                        )
                    detached |= taken

            # 3. What stays noted: off the desktop, yet still plugged in. A
            #    screen whose detection went away with its power comes back
            #    by itself when it returns; Windows needs no help there.
            connected = monitors.connected_keys()
            desktop = monitors.desktop_screens() or {}
            remaining = sorted(
                key for key in detached
                if key not in desktop and (connected is None or key in connected)
            )
            if remaining != settings.detached_screens:
                settings.detached_screens = remaining
                self._save()

    def _reattach(
        self, keys: set[str], keep_off: set[str], heirs: list[str], now: float
    ) -> set[str]:
        """Put screens back on the desktop, log it, and space out the retries."""
        try:
            returned = monitors.reattach(keys, keep_off, heirs)
        except monitors.DisplayConfigError as exc:
            returned = set()
            self._note_display(
                f"Could not put {self._screen_names(keys)} back on the desktop: {exc}"
            )
        if returned:
            self._display_note = ""
            self._log(f"Back on the Windows desktop: {self._screen_names(returned)}")
        for key in keys - returned:
            self._reattach_retry[key] = now + REATTACH_RETRY_S
        return returned

    @staticmethod
    def _lit_among(desktop: dict[str, bool], lit: dict[str, bool | None]) -> set[str]:
        """Screens of the desktop that are lit; one on no outlet always is."""
        return {key for key in desktop if lit.get(key, True) is True}

    def _heirs(self, keys: set[str]) -> list[str]:
        """Who may become the primary screen, best first, among `keys`.

        The primary screen of the captured layout first: it is the one the
        user chose. Then the boot screen, meant to be always there -- the
        one the user also picked by hand, the day Windows had chosen a ghost.
        """
        order = [s.key for s in self.config.screens if s.primary]
        boot = self.config.boot_screen_outlet()
        if boot is not None and boot.monitor_key:
            order.append(boot.monitor_key)
        order += sorted(keys)
        return [key for key in dict.fromkeys(order) if key in keys]

    def release_screens(self) -> None:
        """Put back every screen taken off the desktop: the application stops.

        Without it, a screen taken off would stay so once its outlet is on
        again, until Win+P > Extend -- the application would no longer be
        there to do it.
        """
        detached = set(self.config.settings.detached_screens)
        if not detached or not self.drives_devices:
            return
        # Called from the main thread: no waiting (see `_display_lock`). The
        # list stays noted, and the next start puts them back.
        if not self._display_lock.acquire(blocking=False):
            self._log(f"Left off the desktop until the next start: {self._screen_names(detached)}")
            return
        try:
            returned = monitors.reattach(detached, set())
        except monitors.DisplayConfigError as exc:
            self._log(f"Could not put {self._screen_names(detached)} back on the desktop: {exc}")
            return
        finally:
            self._display_lock.release()
        if returned:
            self._log(f"Back on the Windows desktop: {self._screen_names(returned)}")
        self.config.settings.detached_screens = sorted(detached - returned)
        self._save()

    def detached_labels(self) -> list[str]:
        """Names of the screens taken off the desktop, for the interface."""
        return [
            self._screen_names({key}) for key in self.config.settings.detached_screens
        ]

    def _screens_lit(self) -> dict[str, bool | None]:
        """For each screen linked to an outlet: is the outlet on? None if unknown.

        Two outlets linked to the same screen -- a configuration error the
        layout check reports -- light it as soon as one of them is on.
        """
        lit: dict[str, bool | None] = {}
        for outlet in self.config.outlets:
            key = outlet.monitor_key
            if not key:
                continue
            state = self._outputs.get(outlet.ref)
            if state or lit.get(key):
                lit[key] = True
            elif state is None or (key in lit and lit[key] is None):
                lit[key] = None
            else:
                lit[key] = False
        return lit

    def _may_detach(self, key: str, now: float) -> bool:
        """False if this ghost was taken off too often lately (see DETACH_MAX)."""
        recent = [t for t in self._detach_times.get(key, []) if now - t < DETACH_WINDOW_S]
        self._detach_times[key] = recent
        if len(recent) < DETACH_MAX:
            return True
        self._note_display(
            f"Windows keeps putting ghost screen {self._screen_names({key})} back on "
            f"the desktop: left there for now"
        )
        return False

    def _screen_names(self, keys: set[str]) -> str:
        """Readable names of screens: the outlet powering them, failing that their key."""
        names = {o.monitor_key: o.label for o in self.config.outlets if o.monitor_key}
        return ", ".join(sorted(names.get(key, key) for key in keys))

    def _note_display(self, message: str) -> None:
        """Log a display message, but not again on every refresh."""
        if message != self._display_note:
            self._display_note = message
            self._log(message)

    def _switch_many(
        self, refs: list[str], on: bool, report: ApplyReport, urgent: bool = False
    ) -> list[str]:
        """Operate a series of outlets, noting failures without stopping everything.

        In urgent mode (sleep, shutdown) we go straight through without a
        pause: Windows only leaves a few moments before suspending the process.
        """
        done: list[str] = []
        if not self.drives_devices:
            if refs:
                report.errors.append("another Windows session drives the devices")
                self._log("REFUSED: another Windows session drives the devices")
            return done
        delay = 0.0 if urgent else max(0.0, self.config.settings.switch_delay_ms / 1000.0)
        ordered = sorted(refs)
        for index, ref in enumerate(ordered):
            key, switch_id = parse_ref(ref)
            # Second lock, before even calling the client: a protected
            # outlet has no business being in this list, and if it is there
            # a caller built it wrong.
            outlet = self.config.outlet(ref)
            if not on and outlet is not None and outlet.never_switch_off:
                message = f"{ref}: protected, refused ({outlet.label})"
                report.errors.append(message)
                self._log("REFUSED " + message)
                continue
            try:
                self.device_for(key).set_switch(switch_id, on)
                self._outputs[ref] = on
                done.append(ref)
            except ProtectedOutlet as exc:
                report.errors.append(f"{ref}: {exc}")
                self._log(f"REFUSED {ref}: {exc}")
            except AuthenticationFailed as exc:
                self.auth_failures[key] = str(exc)
                report.errors.append(f"{ref}: {exc}")
            except (NotConnected, ShellyUnreachable, ShellyError) as exc:
                report.errors.append(f"{ref}: {exc}")
            if delay and index < len(ordered) - 1:
                time.sleep(delay)
        return done

    def _expected_monitor_keys(self, targets: dict[str, bool]) -> set[str]:
        """Screens that should be present once the profile is applied."""
        keys: set[str] = set()
        for ref, want in targets.items():
            outlet = self.config.outlet(ref)
            if want and outlet is not None and outlet.monitor_key:
                keys.add(outlet.monitor_key)
        return keys

    def _wait_for_displays(self, expected_keys: set[str]) -> float:
        """Wait for Windows to take the screens switched back on into account.

        If the outlet/screen association is not made yet, we do not know
        exactly what to wait for: we then simply wait for the screen list
        to stop changing.
        """
        timeout = self.config.settings.display_settle_timeout_s
        started = time.monotonic()
        previous: set[str] = monitors.monitor_keys()
        stable_since = started

        while time.monotonic() - started < timeout:
            time.sleep(POLL_INTERVAL_S)
            present = monitors.monitor_keys()
            if expected_keys and not self._missing_displays(expected_keys, present):
                time.sleep(DISPLAY_GRACE_S)  # let the panel finish initialising
                return time.monotonic() - started
            if present != previous:
                previous = present
                stable_since = time.monotonic()
            elif not expected_keys and time.monotonic() - stable_since > DISPLAY_GRACE_S:
                # Nothing specific to wait for and nothing moves any more: carry on.
                return time.monotonic() - started

        waited = time.monotonic() - started
        if expected_keys:
            missing = self._missing_displays(expected_keys, monitors.monitor_keys())
            if missing:
                self._log(f"Displays still missing after {waited:.1f}s: {sorted(missing)}")
        return waited

    def _missing_displays(self, expected: set[str], present: set[str]) -> set[str]:
        """Expected screens not seen, a screen back on another output counting as seen.

        Its link only follows at the next layout check
        (`relink_moved_screens`): waiting for its old key would last until
        the timeout for nothing.
        """
        missing = expected - present
        if missing:
            known = {o.monitor_key for o in self.config.here_outlets() if o.monitor_key}
            known |= set(self.config.unswitched_screens)
            missing -= set(screen_layout.match_moved(missing, present - known,
                                                     monitors.edid_serial))
        return missing

    # ----------------------------------------------------------------- io

    def _save(self) -> None:
        try:
            self.config.save()
        except OSError as exc:
            self._log(f"Cannot save configuration: {exc}")


def _is_on(states: dict[str, SwitchState], ref: str) -> bool:
    state = states.get(ref)
    return bool(state and state.output)


def _is_off(states: dict[str, SwitchState], ref: str) -> bool:
    """False if the state is unknown: we do not command blindly."""
    state = states.get(ref)
    return bool(state and not state.output)
