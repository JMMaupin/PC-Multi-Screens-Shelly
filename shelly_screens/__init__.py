"""Screen power control through one or more Shelly devices.

The version number is two numbers and nothing more.

The **major** changes when the existing configuration is no longer enough
as is -- a file format that evolves, a setting whose meaning changes, an
on-device script incompatible with the old one. In other words: when an
update requires checking something rather than just restarting.

The **minor** changes on every iteration: fix, addition, robustness
measure. It is incremented even for a detail, because its role is not to
summarise the extent of the work but to answer a single question, asked on
a day something breaks: *which version is running in front of me?* A log
that does not say which code it is about wastes more time than it saves.
"""

__version__ = "1.15"
