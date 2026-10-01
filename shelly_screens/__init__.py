"""Screen power control through one or more Shelly devices.

The version number is `major.minor`, with a third number for the smallest
fixes: 2.2, 2.2.1, 2.2.2, then 2.3.

The **major** changes when the existing configuration is no longer enough
as is -- a file format that evolves, a setting whose meaning changes, an
on-device script incompatible with the old one. In other words: when an
update requires checking something rather than just restarting.

The **minor** changes with each iteration that adds or changes behaviour.

The **third number** changes for a very minor fix -- a wording, a
misleading message, a threshold. It is incremented even for a detail,
because its role is not to summarise the extent of the work but to answer
a single question, asked on a day something breaks: *which version is
running in front of me?* A log that does not say which code it is about
wastes more time than it saves.

A third number, not a second decimal: versions compare number by number,
so "2.21" would read as newer than "2.3", and an update would be refused.
"""

__version__ = "2.3"
