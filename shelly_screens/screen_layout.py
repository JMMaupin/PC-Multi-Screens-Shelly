"""Can the screen layout be captured right now?

The layout is only worth something if every screen is in its place: one
screen missing, and Windows shifts the others; one screen too many, and the
map draws something that isn't there. Before any capture, we therefore
check that what the outlets report and what Windows sees match exactly:

* every screen outlet is linked to a screen, reachable, and on;
* every screen whose outlet is on is seen by Windows;
* as many physical screens detected as screen outlets on, plus the
  screens proven not to be on an outlet -- no more, no less;
* no two outlets for the same screen, no mirrored screens.

Virtual and wireless screens are set aside before counting: they don't
turn off with an outlet and aren't desktop screens.

A screen plugged into the wall can't be recognised by anything: only the
test run by the identification assistant -- cut each outlet and watch
which screen disappears -- proves it depends on none. Until that test has
been done, a physical screen without an outlet is unknown, and the
capture waits.

The module changes nothing: it returns the list of problems, empty when
the capture can go ahead.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import TYPE_CHECKING, Mapping

from .config import KIND_SCREEN
from .i18n import t

if TYPE_CHECKING:
    from .config import OutletConfig
    from .device import SwitchState
    from .win.monitors import DisplayOutput


def problems(
    outlets: list["OutletConfig"],
    states: Mapping[str, "SwitchState"],
    physical: set[str],
    outputs: Mapping[str, "DisplayOutput"],
    unswitched: set[str],
    links: Mapping[str, str] | None = None,
) -> list[str]:
    """What prevents capturing the layout; empty if nothing stands in the way.

    `physical`: keys of the active physical screens, virtual ones excluded.
    `outputs`: active outputs, for names and mirrors.
    `unswitched`: screens proven not to be on an outlet.
    `links`: outlet -> screen links that take precedence over the
    configuration, for the assistant that has just found them but not yet
    written them.
    """
    links = links or {}

    def key_of(outlet: "OutletConfig") -> str:
        return links.get(outlet.ref, outlet.monitor_key)

    screen_outlets = [
        o for o in outlets
        if (o.kind == KIND_SCREEN or key_of(o)) and not o.host_pc
    ]
    names = {key_of(o): o.label for o in screen_outlets if key_of(o)}

    def name_of(key: str) -> str:
        output = outputs.get(key)
        return names.get(key) or (output.edid_name if output else "") or key

    found: list[str] = []
    lit = 0
    for outlet in screen_outlets:
        key = key_of(outlet)
        state = states.get(outlet.ref)
        if not key:
            found.append(t("{outlet} is a screen outlet not linked to a screen: "
                           "run Identify displays", outlet=outlet.label))
        elif state is None:
            found.append(t("{outlet}: state unknown, its device does not answer",
                           outlet=outlet.label))
        elif not state.output:
            if key in physical:
                found.append(t("{outlet} is off but Windows keeps its screen on the "
                               "desktop (ghost screen)", outlet=outlet.label))
            else:
                found.append(t("{outlet} is off", outlet=outlet.label))
        else:
            lit += 1
            if key not in physical:
                found.append(t("{outlet} is on but Windows does not see its screen",
                               outlet=outlet.label))

    linked = Counter(key_of(o) for o in screen_outlets if key_of(o))
    for key, count in linked.items():
        if count > 1:
            shared = [o.label for o in screen_outlets if key_of(o) == key]
            found.append(t("{outlets} are linked to the same screen",
                           outlets=", ".join(shared)))

    for key in sorted(physical - set(linked) - unswitched):
        found.append(t("Unknown screen connected ({screen}): run Identify displays",
                       screen=name_of(key)))

    # The count sums it all up: screen outlets on one side, physical
    # screens seen on the other. It shows at a glance what is wrong.
    expected = lit + len(physical & unswitched)
    if len(physical) != expected:
        found.insert(0, t("{lit} screen outlet(s) on, {count} physical screen(s) detected",
                          lit=lit, count=len(physical)))

    by_source: dict[tuple, list[str]] = defaultdict(list)
    for output in outputs.values():
        if not output.virtual:
            by_source[output.source].append(output.key)
    for keys in by_source.values():
        if len(keys) > 1:
            found.append(t("{screens} are mirrored",
                           screens=", ".join(name_of(k) for k in sorted(keys))))
    return found


def ghosts(
    outlets: list["OutletConfig"],
    states: Mapping[str, "SwitchState"],
    physical: set[str],
) -> list[str]:
    """Screen outlets that are off yet whose screen Windows keeps: the ghosts.

    A screen connected over HDMI receives +5 V through the cable, enough to
    keep its detection and its EDID alive once its power is cut. Windows
    then keeps it on the desktop, dark, and windows or the mouse can get
    lost on it.
    """
    return [
        o.label for o in outlets
        if o.monitor_key and o.monitor_key in physical
        and o.ref in states and not states[o.ref].output
    ]
