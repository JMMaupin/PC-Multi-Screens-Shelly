"""Configuration model and on-disk persistence.

The configuration fits in a single JSON file, readable and editable by
hand. It describes the Shelly devices, the role of each outlet, the usage
profiles and a few behaviour settings.

Several devices can coexist -- a power strip for the screens, a single
outlet for the PC, for example. An outlet is therefore designated by a
reference `<device key>:<output number>`, for example `strip:0`. The key
is a short alias chosen when the device is added; it stays stable even if
the IP address changes, and it is what profiles reference.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import secrets_store
from .i18n import t

CONFIG_VERSION = 2

# The built-in profile: every outlet on. This name is its key -- in
# `last_profile`, in the on-device script --, stable whatever the
# language; it is displayed translated ("Tous en marche").
ALL_ON_PROFILE = "All on"
# By default the configuration lives next to the code: the tool is
# single-machine and we want it easy to inspect. SHELLY_SCREENS_CONFIG
# moves it elsewhere (to %APPDATA%, for example).
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"

# What an outlet powers. Distinct from the safety roles -- critical,
# boot screen, PC -- which say how to treat it: the kind says what is
# at the end of the cord.
KIND_UNKNOWN = ""
KIND_SCREEN = "screen"
KIND_ACCESSORY = "accessory"
KINDS = (KIND_UNKNOWN, KIND_SCREEN, KIND_ACCESSORY)
KIND_LABELS = {
    KIND_UNKNOWN: "Not set",  # while it has this value, no automation touches it
    KIND_SCREEN: "Screen",
    KIND_ACCESSORY: "Accessory",
}


# Readable prefixes per device type: with two power strips, `strip` and
# `strip2` read at a glance where `shellypstripg4aabbccddeeff` says
# nothing.
KEY_PREFIXES = {
    "powerstrip": "strip",
    "plugs": "plug",
    "plugus": "plug",
    "pluguk": "plug",
    "switch": "switch",
    "pro4pm": "pro",
}


def make_key(base: str, taken: set[str]) -> str:
    """Build a short, readable and unique device key."""
    slug = re.sub(r"[^a-z0-9]+", "", base.lower())
    slug = KEY_PREFIXES.get(slug, slug[:12]) or "device"
    if slug not in taken:
        return slug
    index = 2
    while f"{slug}{index}" in taken:
        index += 1
    return f"{slug}{index}"


@dataclass
class DeviceConfig:
    """A Shelly device and how to reach it."""

    key: str  # short alias, used in outlet references
    device_id: str = ""
    mac: str = ""
    # Host used to reach the device: IP address, or mDNS name when
    # resolution went that way. Refreshed on every connection.
    host: str = ""
    # Matching IPv4 address, resolved on connection. Redundant with
    # `host` when that is already an address, but always filled in:
    # it is what we display and open in a browser.
    ip: str = ""
    name: str = ""  # human-readable label, free-form
    kind: str = ""  # application reported by the device (PowerStrip, PlugS...)
    # Device password, encrypted with DPAPI (see secrets_store).
    # Never read this field directly: go through `get_password`.
    password: str | None = None
    switch_count: int = 0  # number of outputs found

    def get_password(self) -> str:
        """Plain-text password, decrypted on use."""
        return secrets_store.unprotect(self.password or "")

    def set_password(self, plain: str) -> None:
        """Store a password in encrypted form, or clear it."""
        self.password = secrets_store.protect(plain) if plain else None

    @property
    def has_password(self) -> bool:
        return bool(self.password)

    @property
    def label(self) -> str:
        return self.name or self.kind or self.key

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DeviceConfig":
        return cls(
            key=str(data.get("key", "")),
            device_id=str(data.get("device_id", "")),
            mac=str(data.get("mac", "")),
            host=str(data.get("host", "")),
            ip=str(data.get("ip", "")),
            name=str(data.get("name", "")),
            kind=str(data.get("kind", "")),
            password=data.get("password") or None,
            switch_count=int(data.get("switch_count", 0)),
        )


@dataclass
class OutletConfig:
    """One output of a device, and what it powers."""

    device: str  # device key
    switch_id: int  # output number on that device
    name: str = ""
    # Stable identifier of the screen powered by this outlet, as computed
    # by the win.monitors module. Empty until the association has been
    # made.
    monitor_key: str = ""
    # A critical outlet is never switched off by a profile: a safeguard
    # for what must not go dark (a dock, a NAS).
    critical: bool = False
    # The boot screen: the boot-time safeguard, not a favoured screen.
    # When the stored profile is usable, this outlet switches on -- or
    # not -- with the others, like any other. It only comes into play if
    # that profile is missing or worthless, so that the PC never boots
    # without a picture.
    #
    # Without power sensing, nothing could switch it back on: it then
    # stays powered at shutdown, for lack of anything better.
    boot_screen: bool = False
    # This outlet powers the PC. It is never switched off, and its
    # power draw tells whether the PC is running -- enough to reproduce
    # the behaviour of a master/slave power strip.
    host_pc: bool = False
    # What is plugged in: a screen, or an accessory (USB hub, speakers...).
    # Accessories remain controllable by profiles, but fall outside the
    # scope of the identification assistant: switching them off will not
    # make any screen disappear, and testing them would only waste time
    # and cause pointless power cuts.
    kind: str = KIND_UNKNOWN
    # Does this outlet follow the PC's sleep? Screens do: that is the
    # whole point of the setup. Accessories only when asked -- switching
    # off a USB hub or speakers is far from obvious, and doing it by
    # default has already caught people out. The choice is made outlet
    # by outlet, since a hub that is useless at night can sit next to
    # another that must stay awake.
    cut_on_sleep: bool = True

    @property
    def ref(self) -> str:
        return f"{self.device}:{self.switch_id}"

    @property
    def label(self) -> str:
        return self.name or f"{self.device} {self.switch_id + 1}"

    @property
    def never_switch_off(self) -> bool:
        return self.critical or self.host_pc

    @property
    def cuts_on_sleep(self) -> bool:
        """True if the PC going to sleep should take this outlet with it.

        Untouchable outlets win over the checkbox: ticking the box on a
        critical outlet or on the PC's outlet must not make it cuttable
        for all that.
        """
        return self.cut_on_sleep and not self.never_switch_off

    @property
    def is_screen(self) -> bool:
        """True only if the outlet is declared as powering a screen.

        The kind must be set explicitly: an outlet left blank is not
        treated as a screen. What we do not know, we do not touch -- it
        is the unclassified outlet that gets switched off by mistake,
        never the one someone took the time to declare.
        """
        return self.kind == KIND_SCREEN and not self.host_pc

    @property
    def kind_label(self) -> str:
        if self.host_pc:
            return "PC"
        return KIND_LABELS.get(self.kind, self.kind)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OutletConfig":
        return cls(
            device=str(data["device"]),
            switch_id=int(data["switch_id"]),
            name=str(data.get("name", "")),
            monitor_key=str(data.get("monitor_key", "")),
            critical=bool(data.get("critical", False)),
            boot_screen=bool(data.get("boot_screen", False)),
            host_pc=bool(data.get("host_pc", False)),
            kind=str(data.get("kind", KIND_UNKNOWN)),
            # Missing from older files: keep the expected behaviour for
            # screens, and stop switching off accessories that nobody had
            # explicitly designated.
            cut_on_sleep=bool(
                data.get("cut_on_sleep", str(data.get("kind", KIND_UNKNOWN)) == KIND_SCREEN)
            ),
        )


@dataclass
class ScreenPosition:
    """A screen and its place on the desktop, as Windows defines it.

    Windows forgets a screen as soon as its outlet is cut, and may then
    shift the others. To know where a switched-off screen sits, its place
    must therefore have been recorded while everything was on.
    """

    key: str  # stable screen identifier, see win.monitors
    rect: tuple[int, int, int, int]  # left, top, right, bottom; physical pixels
    primary: bool = False
    name: str = ""  # what the driver reports, when no outlet is associated
    scale: float = 1.0  # scale set in Windows: 1.25 for 125 %
    diagonal: float = 0.0  # inches, read from the screen's EDID; 0 if unknown

    @property
    def width(self) -> int:
        return self.rect[2] - self.rect[0]

    @property
    def height(self) -> int:
        return self.rect[3] - self.rect[1]

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "rect": list(self.rect),
            "primary": self.primary,
            "name": self.name,
            "scale": self.scale,
            "diagonal": self.diagonal,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScreenPosition":
        rect = [int(v) for v in data.get("rect", (0, 0, 0, 0))][:4]
        return cls(
            key=str(data.get("key", "")),
            rect=tuple(rect + [0] * (4 - len(rect))),  # type: ignore[arg-type]
            primary=bool(data.get("primary", False)),
            name=str(data.get("name", "")),
            scale=float(data.get("scale", 1.0)) or 1.0,
            diagonal=float(data.get("diagonal", 0.0)),
        )


def parse_ref(ref: str) -> tuple[str, int]:
    """Split a `key:output` reference."""
    device, _, switch = ref.rpartition(":")
    return device, int(switch)


@dataclass
class Profile:
    """A screen profile: which outlets are powered.

    Nothing more. A profile says which screens are on, not what is done on
    them: unrelated activities follow one another on the same profile --
    CAD, trading, development --, each with its own windows. Storing a
    window layout per profile therefore made no sense; an old
    configuration that carries one loses it on the next save.
    """

    name: str
    # Outlet references (`key:output`) powered by this profile.
    outlets_on: list[str] = field(default_factory=list)
    # Display rank in the menu.
    order: int = 0

    # Built-in profile: computed, never saved, neither editable nor deletable.
    builtin: bool = field(default=False, compare=False)

    def wants(self, ref: str) -> bool:
        return ref in self.outlets_on

    @property
    def label(self) -> str:
        """Displayed name: translated for the built-in profile, as is otherwise."""
        return t(self.name) if self.builtin else self.name

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "outlets_on": sorted(set(self.outlets_on)),
            "order": self.order,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Profile":
        return cls(
            name=str(data["name"]),
            outlets_on=[str(x) for x in data.get("outlets_on", [])],
            order=int(data.get("order", 0)),
        )


@dataclass
class PowerSensing:
    """Detecting PC activity from its power draw.

    When the PC is off, no software runs to command the outlets: it is the
    power strip itself, through an on-device script, that watches the PC's
    power draw and switches the screens back on as soon as it sees it pick
    up again. These settings are that script's settings.

    Two thresholds rather than one: between them lies a dead band where the
    current state holds, otherwise a power draw hovering around a single
    value would make the relay chatter endlessly.
    """

    enabled: bool = False
    pc_ref: str = ""  # reference of the outlet powering the PC
    on_threshold_w: float = 25.0  # above it, the PC is considered running
    off_threshold_w: float = 15.0  # below it, it is considered off
    on_delay_s: float = 3.0  # confirmation before switching on: short
    # Confirmation before switching off: long on purpose. During a Windows
    # restart the PC drops below the threshold for ten to fifteen seconds,
    # and cutting the screens at that instant would be the worst timing.
    off_delay_s: float = 90.0
    poll_interval_s: float = 2.0
    script_id: int = 0  # id of the installed script, 0 if none
    # Fingerprint of the code actually installed on the device. Compared
    # with that of the code we would produce now, it tells whether a
    # setting has changed since -- a threshold, an outlet kind, a password
    # -- without having been sent. A flag that had to be raised by hand
    # would end up forgotten; a fingerprint cannot be forgotten.
    installed_fingerprint: str = ""
    # Levels recorded by the calibration assistant, in watts.
    measured_idle_w: float = 0.0  # PC on, idle
    measured_sleep_w: float = 0.0  # PC asleep
    measured_off_w: float = 0.0  # PC off

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PowerSensing":
        defaults = cls()
        return cls(
            enabled=bool(data.get("enabled", defaults.enabled)),
            pc_ref=str(data.get("pc_ref", "")),
            installed_fingerprint=str(data.get("installed_fingerprint", "")),
            on_threshold_w=float(data.get("on_threshold_w", defaults.on_threshold_w)),
            off_threshold_w=float(data.get("off_threshold_w", defaults.off_threshold_w)),
            on_delay_s=float(data.get("on_delay_s", defaults.on_delay_s)),
            off_delay_s=float(data.get("off_delay_s", defaults.off_delay_s)),
            poll_interval_s=float(data.get("poll_interval_s", defaults.poll_interval_s)),
            script_id=int(data.get("script_id", 0)),
            measured_idle_w=float(data.get("measured_idle_w", 0.0)),
            measured_sleep_w=float(data.get("measured_sleep_w", 0.0)),
            measured_off_w=float(data.get("measured_off_w", 0.0)),
        )

    def thresholds_are_sane(self) -> str:
        """Warning message if the thresholds do not make sense, otherwise empty."""
        if self.off_threshold_w >= self.on_threshold_w:
            return "The off threshold must stay below the on threshold."
        if self.measured_idle_w and self.on_threshold_w >= self.measured_idle_w:
            return (
                f"The on threshold ({self.on_threshold_w:.0f} W) is above the "
                f"measured idle draw ({self.measured_idle_w:.0f} W): the PC "
                "would never be seen as running."
            )
        if self.measured_sleep_w and self.off_threshold_w <= self.measured_sleep_w:
            return (
                f"The off threshold ({self.off_threshold_w:.0f} W) is below the "
                f"measured sleep draw ({self.measured_sleep_w:.0f} W): the PC "
                "would never be seen as asleep."
            )
        return ""


@dataclass
class Settings:
    """Behaviour settings."""

    # Switch off every unprotected outlet when the PC goes to sleep.
    power_off_on_suspend: bool = True
    # Reapply the last profile on resume.
    restore_on_resume: bool = True
    # Pause between two outlet commands, so as not to flood a device.
    switch_delay_ms: int = 250
    # Maximum time to wait for Windows to pick up the screens.
    display_settle_timeout_s: float = 20.0
    # Last applied profile, reapplied on resume and at startup.
    last_profile: str = ""
    # Outlets powered just before going to sleep. Without an applied
    # profile, this is the only record of what to restore on resume: a
    # profile name may be missing, the outlet state always exists.
    resume_refs: list[str] = field(default_factory=list)
    # Reapply the last profile when the application starts.
    apply_profile_on_start: bool = False
    # Theme of the settings window: system, light or dark.
    theme: str = "system"
    # Interface language: system, en or fr.
    language: str = "system"
    # Depth of the power history, in days. Beyond that, the oldest
    # points are pruned: a queue, not an archive.
    history_days: int = 30
    # Global hotkey that opens the profile picker, in its readable
    # form ("Ctrl+Win+Alt+P"). Empty: no hotkey.
    profile_hotkey: str = "Ctrl+Win+Alt+P"
    # After a profile change, bring windows left outside every screen
    # back onto the nearest screen that is on.
    rescue_offscreen_windows: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Settings":
        defaults = cls()
        return cls(
            power_off_on_suspend=bool(
                data.get("power_off_on_suspend", defaults.power_off_on_suspend)
            ),
            restore_on_resume=bool(data.get("restore_on_resume", defaults.restore_on_resume)),
            switch_delay_ms=int(data.get("switch_delay_ms", defaults.switch_delay_ms)),
            display_settle_timeout_s=float(
                data.get("display_settle_timeout_s", defaults.display_settle_timeout_s)
            ),
            last_profile=str(data.get("last_profile", "")),
            resume_refs=[str(r) for r in data.get("resume_refs", [])],
            apply_profile_on_start=bool(
                data.get("apply_profile_on_start", defaults.apply_profile_on_start)
            ),
            theme=str(data.get("theme", defaults.theme)),
            language=str(data.get("language", defaults.language)),
            history_days=max(1, int(data.get("history_days", defaults.history_days))),
            profile_hotkey=str(data.get("profile_hotkey", defaults.profile_hotkey)),
            rescue_offscreen_windows=bool(
                data.get("rescue_offscreen_windows", defaults.rescue_offscreen_windows)
            ),
        )


@dataclass
class AppConfig:
    """Root of the configuration."""

    devices: list[DeviceConfig] = field(default_factory=list)
    outlets: list[OutletConfig] = field(default_factory=list)
    profiles: list[Profile] = field(default_factory=list)
    settings: Settings = field(default_factory=Settings)
    sensing: PowerSensing = field(default_factory=PowerSensing)
    # Position of the screens on the desktop, recorded while they were all on.
    screens: list[ScreenPosition] = field(default_factory=list)
    # Time of the last layout capture (Unix time), 0 if none.
    screens_captured_at: float = 0.0
    # Screens proven to be on no outlet: they stayed on while the
    # assistant cut every screen outlet. Only this test proves it -- a
    # wall-powered screen cannot be told apart any other way.
    unswitched_screens: list[str] = field(default_factory=list)
    path: Path = field(default=DEFAULT_CONFIG_PATH, compare=False, repr=False)

    # ------------------------------------------------------------- access

    def device(self, key: str) -> DeviceConfig | None:
        for device in self.devices:
            if device.key == key:
                return device
        return None

    def outlet(self, ref: str) -> OutletConfig | None:
        for outlet in self.outlets:
            if outlet.ref == ref:
                return outlet
        return None

    def outlets_of(self, device_key: str) -> list[OutletConfig]:
        return [o for o in self.outlets if o.device == device_key]

    def all_on_profile(self) -> Profile:
        """The built-in profile, recomputed: it follows added or removed outlets."""
        return Profile(
            name=ALL_ON_PROFILE, outlets_on=self.refs(), order=-1, builtin=True
        )

    @staticmethod
    def is_reserved_name(name: str) -> bool:
        """True for the built-in profile's name, in the UI language or in English."""
        folded = name.strip().casefold()
        return folded in (ALL_ON_PROFILE.casefold(), t(ALL_ON_PROFILE).casefold())

    def profile(self, name: str) -> Profile | None:
        if name == ALL_ON_PROFILE:
            return self.all_on_profile()
        for profile in self.profiles:
            if profile.name == name:
                return profile
        return None

    def sorted_profiles(self) -> list[Profile]:
        """The built-in profile first, then the user's in their order."""
        return [self.all_on_profile()] + self.user_profiles()

    def user_profiles(self) -> list[Profile]:
        return sorted(self.profiles, key=lambda p: (p.order, p.name.lower()))

    def refs(self) -> list[str]:
        return [outlet.ref for outlet in self.outlets]

    def ensure_outlets(self, device_key: str, count: int) -> None:
        """Fill in the outlet list to cover an actual device."""
        known = {o.switch_id for o in self.outlets_of(device_key)}
        for switch_id in range(count):
            if switch_id not in known:
                self.outlets.append(OutletConfig(device=device_key, switch_id=switch_id))
        device = self.device(device_key)
        if device is not None:
            device.switch_count = max(device.switch_count, count)
        self.outlets.sort(key=lambda o: (self._device_order(o.device), o.switch_id))

    def _device_order(self, device_key: str) -> int:
        for index, device in enumerate(self.devices):
            if device.key == device_key:
                return index
        return len(self.devices)

    def add_device(self, device: DeviceConfig) -> DeviceConfig:
        """Add a device, giving it a free key if needed."""
        if not device.key:
            device.key = make_key(device.kind or device.device_id, self.device_keys())
        elif self.device(device.key) is not None:
            device.key = make_key(device.key, self.device_keys())
        self.devices.append(device)
        return device

    def device_keys(self) -> set[str]:
        return {device.key for device in self.devices}

    def rename_device(self, old_key: str, new_key: str) -> bool:
        """Change a device's key and propagate it to outlets and profiles."""
        device = self.device(old_key)
        if device is None or not new_key or self.device(new_key) is not None:
            return False
        device.key = new_key
        for outlet in self.outlets:
            if outlet.device == old_key:
                outlet.device = new_key
        for profile in self.profiles:
            profile.outlets_on = [
                self._repoint(ref, old_key, new_key) for ref in profile.outlets_on
            ]
        # References outside profiles matter just as much: leaving `pc_ref`
        # pointing at the old key silences sensing -- the designated device
        # no longer exists -- without the slightest message, and a stale
        # `resume_refs` would leave a resume unable to restore anything.
        self.sensing.pc_ref = self._repoint(self.sensing.pc_ref, old_key, new_key)
        self.settings.resume_refs = [
            self._repoint(ref, old_key, new_key)
            for ref in self.settings.resume_refs
        ]
        return True

    @staticmethod
    def _repoint(ref: str, old_key: str, new_key: str) -> str:
        """Rewrite an outlet reference after a key change."""
        if not ref:
            return ref
        device, switch = parse_ref(ref)
        return f"{new_key}:{switch}" if device == old_key else ref

    def remove_device(self, key: str) -> None:
        """Remove a device, its outlets, and the references pointing to them."""
        self.devices = [d for d in self.devices if d.key != key]
        self.outlets = [o for o in self.outlets if o.device != key]
        remaining = set(self.refs())
        for profile in self.profiles:
            profile.outlets_on = [r for r in profile.outlets_on if r in remaining]

    def unclassified_outlets(self) -> list[OutletConfig]:
        """Outlets whose kind has not been set.

        No automation operates them as long as we do not know what they
        power.
        """
        return [
            outlet
            for outlet in self.outlets
            if outlet.kind == KIND_UNKNOWN and not outlet.host_pc
        ]

    def boot_screen_outlet(self) -> OutletConfig | None:
        """The outlet that must stay powered when the PC shuts down."""
        for outlet in self.outlets:
            if outlet.boot_screen:
                return outlet
        return None

    def leaves_a_screen(self, powered: set[str]) -> bool:
        """True if these powered outlets leave at least one screen on.

        Nothing to protect without a declared screen outlet, nor when a
        screen depends on no outlet: that one stays on whatever is cut.
        """
        screens = [outlet.ref for outlet in self.outlets if outlet.is_screen]
        if not screens or self.unswitched_screens:
            return True
        return any(ref in powered for ref in screens)

    def fallback_screen(self, candidates: set[str]) -> OutletConfig | None:
        """The screen to keep when none would stay on.

        The boot screen first, since it is already designated as the one
        that must never be missing; failing that, the first declared
        screen. `candidates` rules out outlets we could not command.
        """
        screens = [o for o in self.outlets if o.is_screen and o.ref in candidates]
        boot = self.boot_screen_outlet()
        if boot is not None and boot in screens:
            return boot
        return screens[0] if screens else None

    def host_pc_outlet(self) -> OutletConfig | None:
        """The outlet powering the PC, if known."""
        for outlet in self.outlets:
            if outlet.host_pc:
                return outlet
        return None

    def shutdown_refs_on(self) -> list[str]:
        """Outlets to leave powered when the PC sleeps or shuts down.

        The boot screen is only among them if nothing can switch it back
        on. As soon as the on-device script watches the power draw, it
        takes care of it at the next power-on: keeping it powered would
        only waste energy, and would defeat the very purpose of sensing
        -- cutting everything at shutdown.
        """
        keep = [outlet.ref for outlet in self.outlets if not outlet.cuts_on_sleep]
        if not self.sensing.enabled:
            keep += [
                outlet.ref
                for outlet in self.outlets
                if outlet.boot_screen and outlet.ref not in keep
            ]
        return keep

    # -------------------------------------------------------- persistence

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": CONFIG_VERSION,
            "devices": [device.to_dict() for device in self.devices],
            "outlets": [outlet.to_dict() for outlet in self.outlets],
            "profiles": [profile.to_dict() for profile in self.user_profiles()],
            "settings": self.settings.to_dict(),
            "sensing": self.sensing.to_dict(),
            "screens": [screen.to_dict() for screen in self.screens],
            "screens_captured_at": self.screens_captured_at,
            "unswitched_screens": sorted(set(self.unswitched_screens)),
        }

    def normalise_roles(self) -> None:
        """Guarantee that exclusive roles are unique.

        The interface already ensures it, but a hand-edited file -- or a
        half-migrated configuration -- could carry two boot screens. The
        first of each role is kept, because two rival outlets would make
        the shutdown behaviour unpredictable.
        """
        # An outlet associated with a screen is one: infer it rather than
        # asking again for what is already known.
        for outlet in self.outlets:
            if not outlet.kind and outlet.monitor_key:
                outlet.kind = KIND_SCREEN

        for attribute in ("boot_screen", "host_pc"):
            seen = False
            for outlet in self.outlets:
                if getattr(outlet, attribute):
                    if seen:
                        setattr(outlet, attribute, False)
                    seen = True

        self._heal_refs()

    def _heal_refs(self) -> None:
        """Repoint references that no longer designate any outlet.

        An orphan reference -- most often left behind by a device key
        change -- is reported nowhere: sensing looks for a missing device
        and goes quiet, and the failure is only discovered at the first
        sleep. So `pc_ref` is repointed to the outlet that actually powers
        the PC, and resume references that no longer mean anything are
        dropped rather than trying to switch them back on.
        """
        known = set(self.refs())
        if self.sensing.pc_ref and self.sensing.pc_ref not in known:
            host = self.host_pc_outlet()
            self.sensing.pc_ref = host.ref if host is not None else ""
        self.settings.resume_refs = [
            ref for ref in self.settings.resume_refs if ref in known
        ]

    @classmethod
    def from_dict(cls, data: dict[str, Any], path: Path) -> "AppConfig":
        data = migrate(data)
        config = cls(
            devices=[DeviceConfig.from_dict(item) for item in data.get("devices", [])],
            outlets=[OutletConfig.from_dict(item) for item in data.get("outlets", [])],
            profiles=[Profile.from_dict(item) for item in data.get("profiles", [])],
            settings=Settings.from_dict(data.get("settings", {})),
            sensing=PowerSensing.from_dict(data.get("sensing", {})),
            screens=[ScreenPosition.from_dict(item) for item in data.get("screens", [])],
            screens_captured_at=float(data.get("screens_captured_at", 0.0)),
            unswitched_screens=[str(k) for k in data.get("unswitched_screens", [])],
            path=path,
        )
        config.normalise_roles()
        config._adopt_all_on()
        return config

    def _adopt_all_on(self) -> None:
        """Make room for the built-in profile in an older configuration.

        A profile saved under that name would hide it. If it already turns
        on every screen, it is redundant and goes away; otherwise it is a
        user choice, and it is kept under another name.
        """
        screens = {
            o.ref for o in self.outlets
            if (o.kind == KIND_SCREEN or o.monitor_key) and not o.never_switch_off
        }
        for profile in list(self.profiles):
            if not self.is_reserved_name(profile.name):
                continue
            if screens <= set(profile.outlets_on):
                self.profiles.remove(profile)
            else:
                name = f"{ALL_ON_PROFILE} (custom)"
                if self.settings.last_profile == profile.name:
                    self.settings.last_profile = name
                profile.name = name

    def save(self, path: Path | None = None) -> None:
        """Write the configuration atomically and durably.

        The previous version, if readable, first goes to
        `config.json.bak`: `load` falls back on it if the main file is
        damaged.
        """
        target = Path(path or self.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n"
        previous = _read_valid(target)
        if previous is not None and previous != payload:
            _write_durable(backup_path(target), previous)
        _write_durable(target, payload)


def backup_path(target: Path) -> Path:
    """Backup copy: the version preceding the last save."""
    return target.with_name(target.name + ".bak")


def _read_valid(target: Path) -> str | None:
    """File contents if they parse as JSON, otherwise None."""
    try:
        text = target.read_text(encoding="utf-8")
        json.loads(text)
    except (OSError, ValueError):
        return None
    return text


def _write_durable(target: Path, text: str) -> None:
    """Temporary file, `fsync`, then replace.

    Without `fsync`, the rename can reach the disk before the data: a
    power cut during a sleep once left a `config.json` of the right size,
    but filled with null bytes.
    """
    handle, temp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=target.name, suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, target)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def migrate(data: dict[str, Any]) -> dict[str, Any]:
    """Bring an old configuration up to the current format.

    Version 1: a single device, described by a `device` object, outlets
    numbered by `id`, and profiles referencing those numbers. A device key
    is invented for it and references are rewritten as `key:output`.
    """
    version = int(data.get("version", 1))
    if version >= CONFIG_VERSION:
        return data

    if version == 1:
        legacy_device = data.get("device") or {}
        key = make_key("PowerStrip", set())
        data = dict(data)
        data["devices"] = [
            {
                "key": key,
                "device_id": legacy_device.get("device_id", ""),
                "mac": legacy_device.get("mac", ""),
                "host": legacy_device.get("host", ""),
                "name": "",
                "kind": "PowerStrip",
                "password": legacy_device.get("password"),
                "switch_count": len(data.get("outlets", [])),
            }
        ]
        data.pop("device", None)
        data["outlets"] = [
            {
                "device": key,
                "switch_id": int(item.get("id", index)),
                "name": item.get("name", ""),
                "monitor_key": item.get("monitor_key", ""),
                "critical": item.get("critical", False),
                "boot_screen": item.get("boot_screen", False),
                "host_pc": False,
            }
            for index, item in enumerate(data.get("outlets", []))
        ]
        data["profiles"] = [
            {
                **profile,
                "outlets_on": [f"{key}:{int(i)}" for i in profile.get("outlets_on", [])],
            }
            for profile in data.get("profiles", [])
        ]
        data["version"] = 2

    return data


def config_path() -> Path:
    """Location of the configuration file."""
    override = os.environ.get("SHELLY_SCREENS_CONFIG")
    return Path(override).expanduser() if override else DEFAULT_CONFIG_PATH


def load(path: Path | None = None) -> AppConfig:
    """Load the configuration, or return a blank one."""
    target = Path(path) if path else config_path()
    if not target.exists():
        return AppConfig(path=target)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # Damaged file: start again from the backup copy. The main file
        # will be rewritten on the next save; that save will not touch
        # the backup, since the original no longer parses.
        backup = _read_valid(backup_path(target))
        if backup is None:
            raise RuntimeError(f"Unreadable configuration ({target}): {exc}") from exc
        logging.getLogger("shelly_screens").warning(
            "Configuration unreadable (%s), restored from %s",
            exc,
            backup_path(target).name,
        )
        data = json.loads(backup)
    return AppConfig.from_dict(data, path=target)


