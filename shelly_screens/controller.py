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
)
from .i18n import t
from .win import layout, monitors

# Margin left to the panel after Windows has announced the screen.
DISPLAY_GRACE_S = 1.2
# Polling period of the screen list.
POLL_INTERVAL_S = 0.4
# Gap between two layout readings that must agree.
LAYOUT_STABLE_S = 2.0

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
# Mains voltage never stays perfectly constant: it always wobbles by a
# tenth of a volt from one reading to the next. Several strictly identical
# readings are therefore not a measurement but a frozen value -- the
# firmware's metering channel has given out. The symptom is insidious: the
# device answers, the outlets obey, and only sleep detection reasons on a
# dead figure. It then no longer switches anything off, with nothing to
# flag it. Six reads, i.e. half a minute, are enough to tell a freeze from
# a coincidence.
FROZEN_METER_READS = 6
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
        """True if every configured device answers."""
        return bool(self.config.devices) and len(self._devices) == len(self.config.devices)

    def identity(self, key: str) -> discovery.DeviceIdentity | None:
        return self._identities.get(key)

    def connect_all(self, allow_scan: bool = True) -> dict[str, bool]:
        """Resolve every configured device; return their state by key."""
        results: dict[str, bool] = {}
        for device_config in list(self.config.devices):
            results[device_config.key] = (
                self.connect_device(device_config.key, allow_scan, force=True)
                is not None
            )
        return results

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

        identity = discovery.resolve(
            known_host=device_config.host or None,
            device_id=device_config.device_id or None,
            expected_mac=device_config.mac or None,
            allow_scan=allow_scan,
        )
        if identity is None:
            self._devices.pop(key, None)
            self._identities.pop(key, None)
            self._log(f"Device '{key}' not found on the local network")
            return None

        address = discovery.address_of(identity.host)
        changed = device_config.host != identity.host or device_config.ip != address
        device_config.host = identity.host
        device_config.ip = address or device_config.ip
        device_config.device_id = identity.device_id
        device_config.mac = identity.mac
        if not device_config.kind:
            device_config.kind = identity.app

        was_connected = key in self._devices
        self._identities[key] = identity
        self._devices[key] = ShellyDevice(
            identity.host,
            password=device_config.get_password() or None,
            protected=self.protected_switches(key),
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
        pc = self.config.host_pc_outlet()
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
        return states

    def _refresh_signal(self, key: str, now: float) -> None:
        """Re-read the Wi-Fi signal strength, without ever failing."""
        self._signal_due[key] = now + SIGNAL_REFRESH_S
        try:
            status = self._devices[key].call("Wifi.GetStatus") or {}
        except Exception:  # noqa: BLE001 - a nice-to-have reading, nothing more
            return
        rssi = status.get("rssi")
        if isinstance(rssi, (int, float)) and rssi:
            self._signal[key] = int(rssi)

    def wifi_signal(self, key: str) -> int | None:
        """Last known RSSI, in dBm, or None if it has not been read."""
        return self._signal.get(key)

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
        self._devices[key] = ShellyDevice(device_config.host, password=password or None)
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

    def set_outlet(self, ref: str, on: bool, keep_a_screen: bool = False) -> None:
        """Operate an outlet, honouring the safeguards.

        `keep_a_screen` refuses to switch off the last screen that is on. The
        identification assistant does without it: it switches each screen
        off in turn and back on right away, that is its very principle.
        """
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
        self._log(f"{ref} -> {'on' if on else 'off'}")

    def apply_profile(self, name: str) -> ApplyReport:
        """Apply a profile: the outlets, then the windows to rescue."""
        profile = self.config.profile(name)
        if profile is None:
            raise KeyError(f"Unknown profile: {name}")
        targets = {
            outlet.ref: (profile.wants(outlet.ref) or outlet.never_switch_off)
            for outlet in self.config.outlets
        }
        return self._apply_targets(
            profile_name=profile.name,
            targets=targets,
            profile=profile,
        )

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
        """
        keep_on = set(self.config.shutdown_refs_on())
        targets = {outlet.ref: (outlet.ref in keep_on) for outlet in self.config.outlets}
        # What is on now is what will need to be restored on wake. Note it
        # before switching off: afterwards, the information is gone.
        states = self.read_outlets()
        self.config.settings.resume_refs = [
            ref for ref, state in states.items() if state.output and ref not in keep_on
        ]
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
        still need to know what to restore on wake.
        """
        keep_on = set(self.config.shutdown_refs_on())
        states = self.read_outlets()
        refs = [
            ref for ref, state in states.items() if state.output and ref not in keep_on
        ]
        self.config.settings.resume_refs = refs
        self._save()
        return refs

    def resume(self) -> ApplyReport | None:
        """On wake, restore what was powered before sleep.

        The last profile first, since that is the expressed intent. Failing
        that, the state recorded just before the cut: without it, a user
        who had never applied a profile woke up in front of dark screens,
        with nothing to switch them back on.
        """
        name = self.config.settings.last_profile
        if name and self.config.profile(name) is not None:
            self._log(f"Resuming profile '{name}'")
            return self.apply_profile(name)

        refs = [r for r in self.config.settings.resume_refs if self.config.outlet(r)]
        if not refs:
            self._log("Nothing to restore on resume")
            return None
        self._log(f"No profile set, restoring the outlets that were on: {refs}")
        targets = {
            outlet.ref: (outlet.ref in refs or outlet.never_switch_off)
            for outlet in self.config.outlets
        }
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

            # 1. Switch on first, then let Windows discover the screens.
            expected_keys = self._expected_monitor_keys(targets)
            report.turned_on = self._switch_many(to_turn_on, True, report, urgent)
            if report.turned_on and not urgent:
                report.displays_waited_s = self._wait_for_displays(expected_keys)

            # 2. Switch off whatever remains to be switched off.
            report.turned_off = self._switch_many(to_turn_off, False, report, urgent)

            # 3. Bring back whatever was left outside every lit screen. The
            #    pause gives Windows time to remove the switched-off screens.
            if (profile is not None or rescue) and self.config.settings.rescue_offscreen_windows:
                if report.turned_off:
                    time.sleep(DISPLAY_GRACE_S)
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
        """Physical screens, outputs, and what prevents capturing their position."""
        outputs = monitors.list_outputs()
        physical = monitors.physical_monitors(outputs)
        found = screen_layout.problems(
            self.config.outlets,
            states,
            {m.key for m in physical},
            outputs or {},
            set(self.config.unswitched_screens) if unswitched is None else unswitched,
            links,
        )
        return physical, outputs or {}, found

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
        """
        if not self._lock.acquire(blocking=False):
            return False
        try:
            physical, outputs, found = self.check_screen_layout(states)
            self._note_ghosts(
                screen_layout.ghosts(self.config.outlets, states, {m.key for m in physical})
            )
            reading = tuple((m.key, m.rect, m.scale) for m in physical)
            stable = reading == self._layout_reading
            self._layout_reading = reading
            if found or not stable:
                return False
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
            if time.monotonic() >= deadline:
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
        screens = [o for o in self.config.outlets if o.monitor_key]
        if not screens:
            capture.message = t("No outlet is linked to a screen yet: "
                                "run Identify displays first.")
            self._note_capture([capture.message])
            return capture
        report = ApplyReport(profile="Screen layout")
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
            if capture.turned_on:
                say(t("Waiting for Windows to detect every screen..."))
                self._wait_for_displays({o.monitor_key for o in screens})

            say(t("Checking and capturing the layout..."))
            capture.ok, capture.message = self.capture_when_ready()
            if restore:
                self.undo_capture(capture)
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
            if turned_off and self.config.settings.rescue_offscreen_windows:
                time.sleep(DISPLAY_GRACE_S)
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

    def _switch_many(
        self, refs: list[str], on: bool, report: ApplyReport, urgent: bool = False
    ) -> list[str]:
        """Operate a series of outlets, noting failures without stopping everything.

        In urgent mode (sleep, shutdown) we go straight through without a
        pause: Windows only leaves a few moments before suspending the process.
        """
        done: list[str] = []
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
            if expected_keys and expected_keys.issubset(present):
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
            missing = expected_keys - monitors.monitor_keys()
            if missing:
                self._log(f"Displays still missing after {waited:.1f}s: {sorted(missing)}")
        return waited

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
