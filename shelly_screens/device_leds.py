"""Light rings and buttons on a Power Strip's outlets.

Each outlet carries an RGB ring and a button, both set by a single firmware
component, `POWERSTRIP_UI`. Shipped at full brightness, the rings light up
a room in the dark; the button, for its part, toggles the outlet at the
slightest press -- including the PC's.

Two findings made on the device shape this module:

- `POWERSTRIP_UI.SetConfig` accepts a partial configuration: we only send
  what changes, the rest stays as is;
- settings apply live. Only the very first activation of night mode asked
  for a restart (`restart_required`); so we follow what the device reports
  rather than assuming it. A restart does not flip any output: the relays
  are bistable.

Like `device_services`, this module describes, reads and writes, without
deciding anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .device import ShellyError

COMPONENT = "POWERSTRIP_UI"
MODE_POWER = "power"  # the colour follows the power drawn
MODE_SWITCH = "switch"  # one colour when on, another when off
MODE_OFF = "off"
MODES = (MODE_POWER, MODE_SWITCH, MODE_OFF)
BUTTON_MOMENTARY = "momentary"  # a press toggles the outlet
BUTTON_DETACHED = "detached"  # a press no longer controls anything

# Night setting offered by default: enough to spot an outlet, not enough to
# light up the room.
NIGHT_BRIGHTNESS = 5
NIGHT_START = "22:00"
NIGHT_END = "07:00"

# The only colour key accepted: it applies to every outlet.
COLOURS_KEY = "switch:0"

_CLOCK = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


@dataclass(frozen=True)
class LedSettings:
    """Ring settings, identical for every outlet of a device.

    The firmware has only one set of colours, stored under `switch:0` and
    valid for every outlet: it rejects any other key (error -103). Colours
    are percentages (0-100 per channel), as it expects them.
    """

    mode: str
    brightness: int  # power mode
    on_rgb: tuple[int, int, int]
    on_brightness: int
    off_rgb: tuple[int, int, int]
    off_brightness: int
    night_enabled: bool
    night_brightness: int
    night_start: str
    night_end: str


def valid_clock(value: str) -> bool:
    """True for an "HH:MM" time that the device will accept."""
    return bool(_CLOCK.match(value))


def read(device) -> tuple[LedSettings, dict[int, str]] | None:
    """Ring settings and each button's mode, `None` if not a Power Strip.

    Another Shelly model ignores the method: we tell that apart from a
    failure, so the interface says "not available" rather than an error.
    """
    try:
        config: dict[str, Any] = device.call(f"{COMPONENT}.GetConfig") or {}
    except ShellyError:
        return None
    leds = config.get("leds") or {}
    colors = leds.get("colors") or {}
    first = colors.get(COLOURS_KEY) or {}
    night = leds.get("night_mode") or {}
    between = list(night.get("active_between") or [])
    if len(between) != 2:
        between = [NIGHT_START, NIGHT_END]

    def rgb(state: str, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
        values = (first.get(state) or {}).get("rgb") or fallback
        return tuple(int(round(v)) for v in values[:3])  # type: ignore[return-value]

    def level(value: Any) -> int:
        return max(0, min(100, int(round(float(value)))))

    settings = LedSettings(
        mode=leds.get("mode", MODE_POWER),
        brightness=level((colors.get("power") or {}).get("brightness", 100)),
        on_rgb=rgb("on", (0, 100, 0)),
        on_brightness=level((first.get("on") or {}).get("brightness", 100)),
        off_rgb=rgb("off", (100, 0, 0)),
        off_brightness=level((first.get("off") or {}).get("brightness", 100)),
        night_enabled=bool(night.get("enable", False)),
        # Night mode never set: the device reports 100 %, which is no
        # suggestion at all. We offer the default setting instead.
        night_brightness=(
            level(night.get("brightness", NIGHT_BRIGHTNESS))
            if night.get("enable") or night.get("active_between")
            else NIGHT_BRIGHTNESS
        ),
        night_start=between[0],
        night_end=between[1],
    )
    buttons = {
        int(key.split(":")[1]): (value or {}).get("in_mode", BUTTON_MOMENTARY)
        for key, value in (config.get("controls") or {}).items()
        if key.startswith("switch:")
    }
    return settings, buttons


def apply(device, settings: LedSettings) -> bool:
    """Send the ring settings; tell whether a restart is needed."""
    if settings.mode not in MODES:
        raise ValueError(f"Unknown LED mode '{settings.mode}'")
    for clock in (settings.night_start, settings.night_end):
        if not valid_clock(clock):
            raise ValueError(f"Invalid time '{clock}', expected HH:MM")
    colors = {
        "power": {"brightness": settings.brightness},
        COLOURS_KEY: {
            "on": {"rgb": list(settings.on_rgb), "brightness": settings.on_brightness},
            "off": {"rgb": list(settings.off_rgb), "brightness": settings.off_brightness},
        },
    }
    leds = {
        "mode": settings.mode,
        "colors": colors,
        "night_mode": {
            "enable": settings.night_enabled,
            "brightness": settings.night_brightness,
            "active_between": [settings.night_start, settings.night_end],
        },
    }
    result = device.call(f"{COMPONENT}.SetConfig", {"config": {"leds": leds}}) or {}
    return bool(result.get("restart_required", False))


def set_button(device, switch_id: int, detached: bool) -> None:
    """Detach or reattach an outlet's button. Takes effect immediately."""
    mode = BUTTON_DETACHED if detached else BUTTON_MOMENTARY
    device.call(
        f"{COMPONENT}.SetConfig",
        {"config": {"controls": {f"switch:{switch_id}": {"in_mode": mode}}}},
    )

