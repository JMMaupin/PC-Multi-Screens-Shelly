"""Detecting PC activity from its power draw.

When the PC is off, no software runs on it to command the outlets. The
power strip therefore has to do it: an on-device script watches the PC's
power draw and switches the screens back on as soon as it picks up again.
This is what makes it possible to cut everything at shutdown, boot screen
included, without being left blind at the next power-on.

This module generates that script, installs it and keeps it up to date. It
also handles the relay between the application and the script: the list of
outlets to switch back on travels through the device's KVS, where each
value is limited to 255 characters -- hence a plain list of indexes into
the table the script carries.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from .config import AppConfig, OutletConfig, parse_ref
from .device import AUTH_USERNAME, ShellyError

if TYPE_CHECKING:
    from .controller import ScreenController

SCRIPT_NAME = "pc_sensing"
KVS_PROFILE_KEY = "scr_profile"
KVS_MAX_VALUE = 255
# Code returned by the firmware when the requested key does not exist.
KVS_KEY_NOT_FOUND = -105
# Pause between two commands sent by the script, in milliseconds. A burst
# that is too tight gets lost: the first commands go through, the rest are
# silently dropped. Remote orders go out over HTTP, slower than a local
# command.
#
# Widened from 600 to 1200 ms after two crashes of the power strip that
# hosts the script, both during a restore. Each remote order costs two
# round trips -- Digest authentication first requires a 401 refusal
# carrying the challenge -- and up to three attempts: enough to saturate
# the firmware when the application polls it at the same time. The screens
# come back in five seconds instead of two and a half, still before the
# Windows desktop.
COMMAND_GAP_MS = 1200
# Number of attempts per remote command before giving up.
COMMAND_TRIES = 3
# Maximum size of one code upload. The firmware rejects requests that are
# too large (HTTP 413): the code is therefore sent in chunks, the first
# replacing the content and the following ones appended to it.
CODE_CHUNK = 1024
TEMPLATE_PATH = Path(__file__).resolve().parent / "scripts" / "pc_sensing.js"
CONFIG_MARKER = "// --- CONFIG ---"


class SensingError(RuntimeError):
    """Sensing cannot be configured in the current state."""


@dataclass
class ScriptStatus:
    """What the device reports about the installed script."""

    installed: bool = False
    running: bool = False
    script_id: int = 0
    memory_used: int = 0
    error: str = ""

    def summary(self) -> str:
        if self.error:
            return self.error
        if not self.installed:
            return "not installed"
        return "running" if self.running else "installed but stopped"


def controlled_outlets(config: AppConfig) -> list[OutletConfig]:
    """Outlets the script is allowed to operate.

    The PC's outlet is obviously excluded -- it is what is being watched.
    Critical outlets too: a USB hub carrying the keyboard must stay powered
    at all times, otherwise it would not be enumerated in time to enter the
    BIOS.

    Finally, outlets whose "follows sleep" box is unticked stay out as
    well: an accessory meant to stay powered must not even appear in the
    on-device table, or the script would switch it off despite the stated
    intent.
    """
    return [o for o in config.outlets if o.cuts_on_sleep]


def outlet_index(config: AppConfig, ref: str) -> int:
    """Rank of an outlet in the on-device table, or -1."""
    for index, outlet in enumerate(controlled_outlets(config)):
        if outlet.ref == ref:
            return index
    return -1


def host_device_key(config: AppConfig) -> str:
    """Device carrying the PC's outlet, and therefore hosting the script."""
    if not config.sensing.pc_ref:
        raise SensingError("No outlet is marked as powering the PC")
    return parse_ref(config.sensing.pc_ref)[0]


def build_script_config(config: AppConfig) -> dict[str, Any]:
    """Assemble the configuration injected into the script."""
    sensing = config.sensing
    if not sensing.pc_ref:
        raise SensingError("No outlet is marked as powering the PC")
    host_key = host_device_key(config)
    pc_switch = parse_ref(sensing.pc_ref)[1]

    outlets: list[dict[str, Any]] = []
    for outlet in controlled_outlets(config):
        if outlet.device == host_key:
            # Output of the device running the script: direct call.
            outlets.append({"h": None, "i": outlet.switch_id})
        else:
            device = config.device(outlet.device)
            if device is None or not device.host:
                raise SensingError(
                    f"Device '{outlet.device}' has no known address; reconnect it first"
                )
            # The credentials travel in the URL, the only form the on-device
            # HTTP client accepts: neither the Basic header nor an `auth`
            # field is honoured -- both answer 401, whereas
            # `http://admin:pwd@host/` answers 200. Without this, a
            # password-protected power strip rejects every command from the
            # script, and its outlets stay frozen.
            userinfo = ""
            password = device.get_password()
            if password:
                userinfo = AUTH_USERNAME + ":" + quote(password, safe="")
            outlets.append(
                {"h": device.host, "i": outlet.switch_id, "u": userinfo}
            )

    boot = config.boot_screen_outlet()
    boot_index = outlet_index(config, boot.ref) if boot is not None else -1

    # Delays are converted into a number of readings here rather than in the
    # script: mJS only offers a subset of JavaScript, and there is no reason
    # to trust it with rounding.
    poll = max(0.5, float(sensing.poll_interval_s))
    on_ticks = max(1, math.ceil(float(sensing.on_delay_s) / poll))
    off_ticks = max(1, math.ceil(float(sensing.off_delay_s) / poll))

    return {
        "pc": pc_switch,
        "onW": round(float(sensing.on_threshold_w), 1),
        "offW": round(float(sensing.off_threshold_w), 1),
        "onTicks": on_ticks,
        "offTicks": off_ticks,
        "poll": round(poll, 1),
        "gap": COMMAND_GAP_MS,
        "tries": COMMAND_TRIES,
        "boot": boot_index,
        "key": KVS_PROFILE_KEY,
        "outlets": outlets,
    }


def render(config: AppConfig) -> str:
    """Produce the script code, configuration included."""
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    if CONFIG_MARKER not in template:
        raise SensingError("Script template is missing its configuration marker")
    payload = json.dumps(build_script_config(config), separators=(",", ":"))
    return template.replace(CONFIG_MARKER, f"let CFG = {payload};", 1)


def profile_indexes(config: AppConfig, profile_name: str) -> list[int]:
    """Indexes, in the on-device table, of the outlets powered by a profile."""
    profile = config.profile(profile_name)
    if profile is None:
        return []
    indexes = []
    for ref in profile.outlets_on:
        index = outlet_index(config, ref)
        if index >= 0:
            indexes.append(index)
    return sorted(set(indexes))


def encode_profile(indexes: list[int]) -> str:
    """Encode the list for the KVS, respecting its size limit."""
    payload = json.dumps(indexes, separators=(",", ":"))
    while len(payload) > KVS_MAX_VALUE and indexes:
        # Theoretical with our eight outlets, but better to truncate than
        # to have the write rejected and leave a stale value behind.
        indexes = indexes[:-1]
        payload = json.dumps(indexes, separators=(",", ":"))
    return payload


def _put_code(device, script_id: int, code: str) -> None:
    """Upload the code in chunks.

    A single upload exceeds what the firmware accepts (HTTP 413) as soon as
    the script reaches a few kilobytes. The first chunk replaces the
    content, the following ones are appended to it.
    """
    first = True
    for start in range(0, len(code), CODE_CHUNK):
        device.call(
            "Script.PutCode",
            {
                "id": script_id,
                "code": code[start : start + CODE_CHUNK],
                "append": not first,
            },
        )
        first = False


def _forget_key(device, key: str) -> None:
    """Delete a KVS key, whether it exists or not.

    The firmware refuses to delete a missing key (error -105). Yet that is
    the normal case on the first reading: absence is exactly what we want,
    not an anomaly to report.
    """
    try:
        device.call("KVS.Delete", {"key": key})
    except ShellyError as exc:
        if exc.code != KVS_KEY_NOT_FOUND:
            raise


def _find_script(device, name: str = SCRIPT_NAME) -> int:
    """Id of the script with this name on the device, or 0."""
    result = device.call("Script.List") or {}
    for entry in result.get("scripts", []):
        if entry.get("name") == name:
            return int(entry.get("id", 0))
    return 0


def install(controller: "ScreenController", config: AppConfig) -> ScriptStatus:
    """Install or update the script on the device carrying the PC."""
    code = render(config)  # fails early if the configuration is incomplete
    host_key = host_device_key(config)
    device = controller.device_for(host_key)

    script_id = _find_script(device)
    if not script_id:
        created = device.call("Script.Create", {"name": SCRIPT_NAME}) or {}
        script_id = int(created.get("id", 0))
        if not script_id:
            raise SensingError("The device refused to create the script")
    else:
        # A running script refuses to be rewritten.
        device.call("Script.Stop", {"id": script_id})

    _put_code(device, script_id, code)
    # `enable` restarts the script after a power cut, which is precisely
    # the case it has to cover.
    device.call("Script.SetConfig", {"id": script_id, "config": {"enable": True}})
    device.call("Script.Start", {"id": script_id})

    config.sensing.script_id = script_id
    config.sensing.enabled = True
    # What has just been installed is authoritative until the next change.
    config.sensing.installed_fingerprint = fingerprint(config)
    publish_profile(controller, config, config.settings.last_profile)
    return status(controller, config)


def _get_code(device, script_id: int) -> str:
    """Read back the installed code, in chunks as it was sent."""
    parts: list[str] = []
    offset = 0
    while True:
        chunk = device.call(
            "Script.GetCode", {"id": script_id, "offset": offset, "len": CODE_CHUNK}
        ) or {}
        data = str(chunk.get("data", ""))
        parts.append(data)
        offset += len(data)
        if not data or int(chunk.get("left", 0)) <= 0:
            break
    return "".join(parts)


def sync_installed(controller: "ScreenController", config: AppConfig) -> str:
    """Bring the on-device script back in line with the configuration.

    The script carries a frozen copy of the outlet table. Renaming a
    device, changing an outlet's kind or fixing the script itself leaves
    that copy stale, and the drift shows up nowhere: the indexes of the
    published profile then no longer designate the same outputs. Comparing
    it at every startup costs one read and avoids a whole night of
    inexplicable behaviour.
    """
    if not config.sensing.enabled or not config.sensing.pc_ref:
        return ""
    wanted = render(config)
    device = controller.device_for(host_device_key(config))
    script_id = _find_script(device)
    if not script_id:
        install(controller, config)
        return "installed"
    if _get_code(device, script_id) != wanted:
        install(controller, config)
        return "updated"
    info = device.call("Script.GetStatus", {"id": script_id}) or {}
    if not info.get("running"):
        # The code is right: no need to rewrite everything, restarting it
        # is enough. A stopped script no longer protects anything.
        device.call("Script.Start", {"id": script_id})
        return "restarted"
    return ""


def installed_matches(controller: "ScreenController", config: AppConfig) -> bool:
    """Does the code on the device match the configuration?

    Changing a threshold or a delay in the interface only writes the file:
    the device keeps the old values until a reinstall. Nothing said so, and
    one believed they were tuning a sensing that kept following stale
    instructions.
    """
    if not config.sensing.enabled or not config.sensing.pc_ref:
        return True
    try:
        device = controller.device_for(host_device_key(config))
        script_id = _find_script(device)
        if not script_id:
            return False
        return _get_code(device, script_id) == render(config)
    except Exception:  # noqa: BLE001 - an unreachable device is reported elsewhere
        return True


def fingerprint(config: AppConfig) -> str:
    """Fingerprint of the code the current configuration would produce."""
    return hashlib.sha256(render(config).encode("utf-8")).hexdigest()


def needs_update(config: AppConfig) -> bool:
    """Has the script on the device become outdated?

    Answered without touching the network: compare the fingerprint
    recorded at the last install with that of the code we would write
    now. Changing a threshold, an outlet kind or a password alters that
    code -- and the device would keep applying the old one without saying
    a word.
    """
    if not config.sensing.enabled or not config.sensing.pc_ref:
        return False
    if not config.sensing.installed_fingerprint:
        # Nothing recorded: an install predating this tracking, or a script
        # never installed. No crying wolf; the startup sync will settle
        # it.
        return False
    try:
        return fingerprint(config) != config.sensing.installed_fingerprint
    except SensingError:
        return False


def uninstall(controller: "ScreenController", config: AppConfig) -> None:
    """Stop and delete the script."""
    try:
        host_key = host_device_key(config)
        device = controller.device_for(host_key)
    except (SensingError, Exception):  # noqa: BLE001 - uninstalling must not fail
        config.sensing.enabled = False
        config.sensing.script_id = 0
        return
    script_id = _find_script(device) or config.sensing.script_id
    if script_id:
        try:
            device.call("Script.Stop", {"id": script_id})
            device.call("Script.Delete", {"id": script_id})
        except Exception:  # noqa: BLE001
            pass
    config.sensing.enabled = False
    config.sensing.script_id = 0


def status(controller: "ScreenController", config: AppConfig) -> ScriptStatus:
    """Ask the device for the script's state."""
    try:
        device = controller.device_for(host_device_key(config))
    except Exception as exc:  # noqa: BLE001
        return ScriptStatus(error=str(exc))
    try:
        script_id = _find_script(device)
        if not script_id:
            return ScriptStatus(installed=False)
        info = device.call("Script.GetStatus", {"id": script_id}) or {}
        return ScriptStatus(
            installed=True,
            running=bool(info.get("running")),
            script_id=script_id,
            memory_used=int(info.get("mem_used", 0)),
        )
    except Exception as exc:  # noqa: BLE001
        return ScriptStatus(error=str(exc))


def publish_profile(
    controller: "ScreenController", config: AppConfig, profile_name: str
) -> bool:
    """Store in the KVS the outlets the script will switch back on at boot.

    An empty profile is never published. Applying "All off" before shutting
    down the PC is a natural gesture, but it does not mean "at the next
    boot, a single screen": the last useful layout is kept instead.
    """
    if not config.sensing.enabled or not config.sensing.pc_ref:
        return False
    indexes = profile_indexes(config, profile_name)
    if not indexes:
        return False
    try:
        device = controller.device_for(host_device_key(config))
        device.call("KVS.Set", {"key": KVS_PROFILE_KEY, "value": encode_profile(indexes)})
        return True
    except Exception:  # noqa: BLE001 - a silent KVS must not block a profile
        return False


def read_published_profile(controller: "ScreenController", config: AppConfig) -> list[int]:
    """Read back what the script will find in the KVS.

    Indexes outside the current table are dropped: they come from a
    configuration that changed since publication, and letting them through
    would suggest a usable profile when it no longer designates anything.
    """
    try:
        device = controller.device_for(host_device_key(config))
        result = device.call("KVS.Get", {"key": KVS_PROFILE_KEY}) or {}
        stored = json.loads(result.get("value", "[]"))
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(stored, list):
        return []
    count = len(controlled_outlets(config))
    return [i for i in stored if isinstance(i, int) and 0 <= i < count]


# --------------------------------------------------------------- calibration

PROBE_NAME = "pc_probe"
KVS_PROBE_KEY = "scr_probe"
PROBE_TEMPLATE = Path(__file__).resolve().parent / "scripts" / "pc_probe.js"
PROBE_POLL_S = 5.0
PROBE_WRITE_EVERY = 12  # at most one write per minute, except on a new level
# Curve: timestamped ticks, recorded only when the power moves. A PC
# idling, or asleep for a whole night, then produces a single point --
# where regular sampling would have filled the memory with identical
# readings. A KVS value holds 255 characters, i.e. 84 ticks of three
# characters.
KVS_SERIES_KEY = "scr_series"
TICK_CHARS = 3
PROBE_SERIES_CHARS = 252  # multiple of TICK_CHARS
# Level difference from which a change deserves a tick. Three levels on
# the logarithmic scale amount to about 35 % variation: enough to ignore
# the fluctuations of a running PC, small enough to catch a transition to
# sleep.
PROBE_TICK_MIN_STEP = 3
# Maximum number of readings without a single tick. Past that, one is
# recorded anyway: without an anchor point, a long quiet period would
# become a plain line with no time scale.
PROBE_TICK_MAX_SILENCE = 180  # 180 x 5 s = 15 min
# Ceiling of the curve's logarithmic scale, in watts.
PROBE_SERIES_MAX_W = 400.0
SERIES_ALPHABET = (
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz-_"
)


@dataclass(frozen=True)
class Tick:
    """A power change, and how long it has lasted."""

    age_s: float  # seconds elapsed from this tick until now
    watts: float
# Histogram bounds, identical to those of the script.
PROBE_EDGES = [2, 5, 10, 20, 40, 80, 160]


@dataclass
class Levels:
    """Recorded power levels."""

    samples: int = 0
    lowest: float = 0.0
    highest: float = 0.0
    buckets: list[int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.buckets is None:
            self.buckets = [0] * (len(PROBE_EDGES) + 1)

    @property
    def duration_s(self) -> float:
        return self.samples * PROBE_POLL_S

    def bucket_bounds(self, index: int) -> tuple[float, float]:
        low = PROBE_EDGES[index - 1] if index > 0 else 0.0
        high = PROBE_EDGES[index] if index < len(PROBE_EDGES) else float("inf")
        return low, high

    def populated(self, min_share: float = 0.02) -> list[int]:
        """Buckets holding at least `min_share` of the readings.

        The threshold filters out transient values -- the ramp-up at
        startup, a one-off spike -- which are not levels.
        """
        if not self.samples:
            return []
        floor = max(1, int(self.samples * min_share))
        return [i for i, count in enumerate(self.buckets) if count >= floor]

    def split_levels(self) -> tuple[list[int], list[int]]:
        """Split the populated buckets into a low group and a high group.

        The cut is made at the widest gap. A PC does not produce two levels
        but four -- off, asleep, idle, under load -- and what matters is the
        gap between "off or asleep" and "on". Simply taking the highest
        bucket would pin the thresholds to load spikes, and would place
        them far too high.
        """
        slots = self.populated()
        if len(slots) < 2:
            return slots, []
        widest = 0
        cut = 0
        for index in range(len(slots) - 1):
            gap = slots[index + 1] - slots[index]
            if gap > widest:
                widest = gap
                cut = index
        if widest < 1:
            return slots, []
        return slots[: cut + 1], slots[cut + 1 :]

    def standby_ceiling(self) -> float:
        """Upper bound of what the PC draws when off or asleep.

        Upper edge of the last bucket in the low group, not the observed
        minimum: a threshold placed just above a one-off dip would be
        crossed by the slightest variation.
        """
        low_group, _ = self.split_levels()
        if not low_group:
            return 0.0
        _, high = self.bucket_bounds(low_group[-1])
        return self.highest if high == float("inf") else high

    def active_floor(self) -> float:
        """Lower bound of what the PC draws when running.

        Lower edge of the first bucket in the high group: the idle PC can
        drop that far, and the threshold must stay below it.
        """
        _, high_group = self.split_levels()
        if not high_group:
            return 0.0
        low, _ = self.bucket_bounds(high_group[0])
        return low

    def has_two_levels(self) -> bool:
        """True if a low level and a high level can be clearly told apart."""
        low_group, high_group = self.split_levels()
        return bool(low_group) and bool(high_group)


def suggest_thresholds(levels: Levels) -> tuple[float, float, str]:
    """Suggest the two thresholds from the recorded levels.

    Returns (on, off, warning). The thresholds go in the interval between
    the two levels, closer to the bottom than to the top: a PC in deep
    idle sometimes drops well below its usual draw, whereas a PC that is
    off does not creep up.
    """
    if not levels.has_two_levels():
        return (
            0.0,
            0.0,
            "Only one power level was seen. Let the PC run, sleep and shut "
            "down at least once before reading the measurement.",
        )
    floor = levels.standby_ceiling()
    ceiling = levels.active_floor()
    span = ceiling - floor
    if span <= 2.0:
        return (
            0.0,
            0.0,
            f"The gap between idle ({floor:.1f} W) and running ({ceiling:.1f} W) "
            "is too small to place a reliable threshold.",
        )
    on_threshold = round(floor + 0.45 * span, 1)
    off_threshold = round(floor + 0.25 * span, 1)
    warning = ""
    if span < 10.0:
        warning = (
            f"The gap is narrow ({span:.1f} W). Watch that the screens do not "
            "switch off while the PC is running."
        )
    return on_threshold, off_threshold, warning


def render_probe(config: AppConfig) -> str:
    """Power logger code, configuration included."""
    if not config.sensing.pc_ref:
        raise SensingError("No outlet is marked as powering the PC")
    template = PROBE_TEMPLATE.read_text(encoding="utf-8")
    payload = json.dumps(
        {
            "pc": parse_ref(config.sensing.pc_ref)[1],
            "poll": PROBE_POLL_S,
            "writeEvery": PROBE_WRITE_EVERY,
            "key": KVS_PROBE_KEY,
            "seriesKey": KVS_SERIES_KEY,
            "maxChars": PROBE_SERIES_CHARS,
            "minStep": PROBE_TICK_MIN_STEP,
            "maxSilence": PROBE_TICK_MAX_SILENCE,
            "maxW": PROBE_SERIES_MAX_W,
        },
        separators=(",", ":"),
    )
    return template.replace(CONFIG_MARKER, f"let CFG = {payload};", 1)


def install_probe(
    controller: "ScreenController", config: AppConfig, fresh: bool
) -> int:
    """Install the power logger; `fresh` erases what it had already recorded.

    Two opposite uses. Starting a measurement calls for a clean slate: a
    previous recording would skew the suggested levels. Updating the
    logger, on the contrary, must lose nothing: its ticks feed the power
    history, and the logger reads them back when it starts.
    """
    code = render_probe(config)
    device = controller.device_for(host_device_key(config))
    script_id = _find_script(device, PROBE_NAME)
    if not script_id:
        created = device.call("Script.Create", {"name": PROBE_NAME}) or {}
        script_id = int(created.get("id", 0))
    else:
        device.call("Script.Stop", {"id": script_id})
    _put_code(device, script_id, code)
    device.call("Script.SetConfig", {"id": script_id, "config": {"enable": True}})
    if fresh:
        _forget_key(device, KVS_PROBE_KEY)
        _forget_key(device, KVS_SERIES_KEY)
    device.call("Script.Start", {"id": script_id})
    return script_id


def start_probe(controller: "ScreenController", config: AppConfig) -> int:
    """Start a new measurement, on a clean slate."""
    return install_probe(controller, config, fresh=True)


def sync_probe(controller: "ScreenController", config: AppConfig) -> str:
    """Keep the power logger up to date and running, without erasing its ticks.

    It is no longer just a calibration tool: it is what sees the power
    draw while the PC sleeps, and the history depends on it. So it is
    installed if missing, replaced if its code has aged, and restarted if
    it has stopped.
    """
    if not config.sensing.enabled or not config.sensing.pc_ref:
        return ""
    device = controller.device_for(host_device_key(config))
    script_id = _find_script(device, PROBE_NAME)
    if not script_id:
        install_probe(controller, config, fresh=False)
        return "installed"
    if _get_code(device, script_id) != render_probe(config):
        install_probe(controller, config, fresh=False)
        return "updated"
    info = device.call("Script.GetStatus", {"id": script_id}) or {}
    if not info.get("running"):
        device.call("Script.Start", {"id": script_id})
        return "restarted"
    return ""


def read_probe_timeline(
    controller: "ScreenController", config: AppConfig
) -> list[tuple[float, float]]:
    """Power logger ticks, dated in Unix time, from oldest to newest.

    The logger publishes the time of its last tick; the others are derived
    from the gaps it encodes. Without that anchor -- an older logger, or a
    clock not yet synchronised --, the current time is used: the
    approximation is good on resume from sleep, where the last tick is
    precisely the wake-up one.
    """
    ticks = read_series(controller, config, strict=True)
    if not ticks:
        return []
    reference = None
    try:
        device = controller.device_for(host_device_key(config))
        raw = (device.call("KVS.Get", {"key": KVS_PROBE_KEY}) or {}).get("value")
        data = json.loads(raw) if isinstance(raw, str) and raw.startswith("{") else {}
        stamp = data.get("t")
        # A time before 2001 means an unsynchronised clock.
        if isinstance(stamp, (int, float)) and stamp > 1_000_000_000:
            reference = float(stamp)
    except Exception:  # noqa: BLE001 - the anchor is a bonus, not a requirement
        reference = None
    if reference is None:
        reference = time.time()
    return [(reference - tick.age_s, tick.watts) for tick in ticks]


def read_probe(controller: "ScreenController", config: AppConfig) -> Levels:
    """Read back the levels accumulated by the power logger.

    Reading right after starting is normal: the logger has not written yet,
    and the firmware then answers "unknown key". That is not an error to
    show, just a recording that is still empty.
    """
    device = controller.device_for(host_device_key(config))
    try:
        result = device.call("KVS.Get", {"key": KVS_PROBE_KEY}) or {}
    except ShellyError as exc:
        if exc.code == KVS_KEY_NOT_FOUND:
            return Levels()
        raise
    try:
        data = json.loads(result.get("value", "{}"))
    except (ValueError, TypeError):
        return Levels()
    return Levels(
        samples=int(data.get("n", 0)),
        lowest=float(data.get("mn", 0.0) or 0.0),
        highest=float(data.get("mx", 0.0) or 0.0),
        buckets=[int(x) for x in data.get("b", [])] or None,
    )


def stop_probe(controller: "ScreenController", config: AppConfig) -> None:
    """Stop and delete the power logger, keeping its last measurement."""
    try:
        device = controller.device_for(host_device_key(config))
    except Exception:  # noqa: BLE001
        return
    script_id = _find_script(device, PROBE_NAME)
    if script_id:
        try:
            device.call("Script.Stop", {"id": script_id})
            device.call("Script.Delete", {"id": script_id})
        except Exception:  # noqa: BLE001
            pass


def probe_running(controller: "ScreenController", config: AppConfig) -> bool:
    try:
        device = controller.device_for(host_device_key(config))
        script_id = _find_script(device, PROBE_NAME)
        if not script_id:
            return False
        info = device.call("Script.GetStatus", {"id": script_id}) or {}
        return bool(info.get("running"))
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------ guard

GUARD_NAME = "pc_guard"


def uninstall_guard(controller: "ScreenController", config: AppConfig, device_key: str) -> None:
    """Remove the guard from a given device."""
    try:
        device = controller.device_for(device_key)
    except Exception:  # noqa: BLE001
        return
    script_id = _find_script(device, GUARD_NAME)
    if not script_id:
        return
    try:
        device.call("Script.Stop", {"id": script_id})
        device.call("Script.Delete", {"id": script_id})
    except Exception:  # noqa: BLE001
        pass


def sync_guard(controller: "ScreenController", config: AppConfig) -> str:
    """Point the watch back at the outlet actually declared.

    The guard is no longer a separate script: it lives in `pc_sensing`.
    The device only runs three scripts at a time, and the third slot must
    stay free for the power-level logger. So the old `pc_guard` scripts are
    erased everywhere, then the driver script, which now carries the watch,
    is reinstalled.
    """
    for device_config in config.devices:
        uninstall_guard(controller, config, device_config.key)
    outlet = config.host_pc_outlet()
    if outlet is None:
        return "no outlet marked as powering the PC"
    if not config.sensing.enabled:
        return f"PC outlet is {outlet.ref}; install the script to watch it"
    install(controller, config)
    return f"watching {outlet.ref}"


def publish_current_state(controller: "ScreenController", config: AppConfig) -> list[int]:
    """Publish the outlets currently powered, when there is no profile.

    The script can only switch back on what the application left it. As
    long as no profile has been applied, it finds nothing and sticks to the
    boot screen -- the PC then comes back with a single screen, with
    nothing to explain why.

    Publishing the current state is the most sensible fallback: what is on
    now is most likely what one wants to find again.
    """
    if not config.sensing.enabled or not config.sensing.pc_ref:
        return []
    table = controlled_outlets(config)
    try:
        states = controller.read_outlets()
    except Exception:  # noqa: BLE001
        return []
    indexes = [
        index
        for index, outlet in enumerate(table)
        if states.get(outlet.ref) and states[outlet.ref].output
    ]
    if not indexes:
        return []
    try:
        device = controller.device_for(host_device_key(config))
        device.call("KVS.Set", {"key": KVS_PROFILE_KEY, "value": encode_profile(indexes)})
        return indexes
    except Exception:  # noqa: BLE001
        return []


def ensure_published(controller: "ScreenController", config: AppConfig) -> list[int]:
    """Make sure the script has something to switch back on.

    The last profile first, since it is the stated intent; otherwise the
    current state, which is better than an empty KVS.
    """
    if not config.sensing.enabled or not config.sensing.pc_ref:
        return []
    if read_published_profile(controller, config):
        return []
    name = config.settings.last_profile
    if name and profile_indexes(config, name):
        publish_profile(controller, config, name)
        return profile_indexes(config, name)
    return publish_current_state(controller, config)


# ------------------------------------------------------------------- curve

def decode_series(encoded: str) -> list[Tick]:
    """Recover the ticks from a series encoded by the power logger.

    Each tick takes three characters: the power level, then the time
    elapsed since the previous tick. Ages are then counted backwards from
    now, the last tick being the most recent.
    """
    span = math.log1p(PROBE_SERIES_MAX_W)
    raw: list[tuple[float, float]] = []
    # Slicing starts from the end. The series is a sliding queue whose head
    # gets trimmed, and an older logger may have left an incomplete tick
    # there: counting from the start would then shift every tick, and the
    # curve would no longer make any sense. The leading remainder is
    # dropped; the recent readings -- the only ones that matter -- stay
    # correct.
    offset = len(encoded) % TICK_CHARS
    for start in range(offset, len(encoded) - TICK_CHARS + 1, TICK_CHARS):
        level = SERIES_ALPHABET.find(encoded[start])
        high = SERIES_ALPHABET.find(encoded[start + 1])
        low = SERIES_ALPHABET.find(encoded[start + 2])
        if level < 0 or high < 0 or low < 0:
            continue  # unknown character: skip rather than shift
        watts = 0.0 if level == 0 else math.expm1(level / 63 * span)
        raw.append(((high * 64 + low) * PROBE_POLL_S, watts))

    # The encoded time is the gap between a tick and the previous one:
    # accumulate it from the end to get each tick's age.
    ticks: list[Tick] = []
    age = 0.0
    for gap, watts in reversed(raw):
        ticks.append(Tick(age_s=age, watts=watts))
        age += gap
    ticks.reverse()
    return ticks


def read_series(
    controller: "ScreenController", config: AppConfig, strict: bool = False
) -> list[Tick]:
    """Read back the accumulated ticks, from oldest to newest.

    By default, an unreachable device yields an empty list: the measurement
    curve simply waits for the next refresh. With `strict`, the failure
    propagates -- the history must tell "nothing to fetch" from "could not
    read", otherwise it gives up on data it would have had a minute later.
    """
    try:
        device = controller.device_for(host_device_key(config))
    except Exception:  # noqa: BLE001
        if strict:
            raise
        return []
    try:
        result = device.call("KVS.Get", {"key": KVS_SERIES_KEY}) or {}
    except ShellyError as exc:
        if exc.code == KVS_KEY_NOT_FOUND:
            return []
        raise
    except Exception:  # noqa: BLE001
        if strict:
            raise
        return []
    value = result.get("value")
    return decode_series(value) if isinstance(value, str) else []


