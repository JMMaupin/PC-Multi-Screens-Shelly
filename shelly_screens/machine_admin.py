"""Saving the hardware configuration, with administrator rights when needed.

`machine.json` is writable by administrators only, so that an ordinary
account cannot, by mistake, remove a device or untick the role that
protects the PC's outlet. The application does not run elevated: when the
file refuses a write, a second copy of the program is started through UAC
just to install the prepared content, then exits.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from .win import elevation

if TYPE_CHECKING:
    from .config import AppConfig

COMMIT_ARGUMENT = "--commit-machine-config"

EXIT_OK = 0
EXIT_INVALID = 2  # the prepared file is not a machine configuration
EXIT_WRITE_FAILED = 3  # even elevated, the file could not be written


class SaveCancelled(Exception):
    """The user declined the UAC prompt: nothing was saved."""


class SaveFailed(Exception):
    """The elevated copy ran but could not save."""


def save(config: "AppConfig", owner: int | None = None) -> None:
    """Write the hardware configuration, asking for elevation if needed.

    Blocks while the UAC prompt is up: call it from a worker thread.
    Raises SaveCancelled or SaveFailed; returns normally once saved.
    """
    config.save()  # direct write: enough whenever this account may write
    if not config.machine_pending():
        return

    handle, temp_name = tempfile.mkstemp(prefix="shelly-machine-", suffix=".json")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(config.machine_dict(), stream, indent=2, ensure_ascii=False)
        try:
            code = elevation.run_elevated(
                [COMMIT_ARGUMENT, temp_name, str(config.paths.machine_dir)], owner=owner
            )
        except elevation.ElevationCancelled as exc:
            raise SaveCancelled() from exc
        except (OSError, TimeoutError) as exc:
            raise SaveFailed(str(exc)) from exc
    finally:
        Path(temp_name).unlink(missing_ok=True)

    if code != EXIT_OK or config.machine_pending():
        raise SaveFailed(f"the elevated save ended with code {code}")
    # Written by the elevated copy, not by this process: without this, the
    # change of the file's date would pass for another session's write.
    config.mark_synced()


def commit_from_command_line(source: str, machine_dir: str) -> int:
    """Entry point of the elevated copy: install the prepared file, exit."""
    from .config import write_machine_file

    try:
        write_machine_file(Path(source), Path(machine_dir))
    except (ValueError, KeyError, TypeError):
        return EXIT_INVALID
    except OSError:
        return EXIT_WRITE_FAILED
    return EXIT_OK
