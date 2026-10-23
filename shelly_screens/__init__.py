"""Screen power control through one or more Shelly devices.

The version number always has three parts, `major.minor.fix`: 3.0.0,
3.0.1, 3.0.2, then 3.1.0 ("3.0" and "3.0.0" are the same version).

The **major** changes when the existing configuration is no longer enough
as is -- a file format that evolves, a setting whose meaning changes, an
on-device script incompatible with the old one. In other words: when an
update requires checking something rather than just restarting.

The **minor** changes when a feature is added: something one can do that
one could not before.

The **fix** number changes for every correction, behaviour included -- a
screen not recognised, a wait too long, a wording, a threshold. It is
incremented even for a detail, because its role is not to summarise the
extent of the work but to answer a single question, asked on a day
something breaks: *which version is running in front of me?* A log that
does not say which code it is about wastes more time than it saves.

A third number, not a second decimal: versions compare number by number,
so "2.21" would read as newer than "2.3", and an update would be refused.
"""

__version__ = "3.1.1"


def version_tuple(text: str) -> tuple[int, ...]:
    """Comparable form of a version; "2.0" and "2.0.0" are the same."""
    parts = []
    for piece in str(text).split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    parts += [0] * (4 - len(parts))
    return tuple(parts[:4])
