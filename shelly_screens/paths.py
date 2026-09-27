"""Where the application keeps its files.

The application drives hardware shared by every account on the PC, so it
is installed for all users, and its data is split the same way:

- `%ProgramData%\\Shelly PC Screens\\` holds what belongs to the machine:
  `machine.json` (devices, outlets, roles, sensing -- written by an
  administrator), `state\\state.json` (what the application records by
  itself: last known addresses, outlets to restore on resume, screen
  positions), the power history and the logs;
- `%APPDATA%\\Shelly PC Screens\\user.json` holds what belongs to each
  account: its profiles and its preferences.

The program itself never lives next to its data: `Program Files` is
read-only for a normal user.

`SHELLY_SCREENS_DATA` points everything at one folder instead -- a sandbox
for development or tests, with the account's file in a `user` subfolder.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

APP_DIR_NAME = "Shelly PC Screens"
DATA_OVERRIDE_ENV = "SHELLY_SCREENS_DATA"
MACHINE_FILE_NAME = "machine.json"

# Before version 2.0, everything lived next to the code. Read once, to
# import an existing installation; never written again.
SOURCE_ROOT = Path(__file__).resolve().parent.parent
LEGACY_CONFIG = SOURCE_ROOT / "config.json"
LEGACY_HISTORY = SOURCE_ROOT / "history"


@dataclass(frozen=True)
class DataPaths:
    """The two data folders, and every file derived from them."""

    machine_dir: Path
    user_dir: Path

    @property
    def machine_file(self) -> Path:
        return self.machine_dir / MACHINE_FILE_NAME

    @property
    def state_dir(self) -> Path:
        """Writable by every account, unlike the folder above it.

        Each save goes through a temporary file next to its target: a
        state file sitting next to `machine.json` would need a folder where
        anyone may create files -- and then replace `machine.json` too.
        """
        return self.machine_dir / "state"

    @property
    def state_file(self) -> Path:
        return self.state_dir / "state.json"

    @property
    def quit_flag(self) -> Path:
        """Dropped by the installer to ask every running instance, in every
        session, to exit before the executable is replaced."""
        return self.state_dir / "quit-request"

    @property
    def user_file(self) -> Path:
        return self.user_dir / "user.json"

    @property
    def history_dir(self) -> Path:
        return self.machine_dir / "history"

    @property
    def log_dir(self) -> Path:
        return self.machine_dir / "logs"

    @property
    def log_file(self) -> Path:
        """One log per account: two sessions writing and rotating the same
        file would interleave their lines and lose some at rotation."""
        return self.log_dir / f"shelly-screens-{_account_slug()}.log"


# Where the executable is installed, for every account.
EXE_NAME = "ShellyPCScreens.exe"


def install_dir() -> Path:
    program_files = os.environ.get("ProgramW6432") or os.environ.get("ProgramFiles")
    return Path(program_files or r"C:\Program Files") / APP_DIR_NAME


def installed_exe() -> Path:
    return install_dir() / EXE_NAME


def is_frozen() -> bool:
    """True when running as the executable, False from the sources."""
    return bool(getattr(sys, "frozen", False))


def running_installed_copy() -> bool:
    """True if this process is the installed executable itself."""
    if not is_frozen():
        return False
    try:
        return Path(sys.executable).resolve() == installed_exe().resolve()
    except OSError:
        return False


def default() -> DataPaths:
    """The folders in use: the standard ones, or the override."""
    override = os.environ.get(DATA_OVERRIDE_ENV)
    if override:
        root = Path(override).expanduser()
        return DataPaths(machine_dir=root, user_dir=root / "user")
    program_data = Path(os.environ.get("ProgramData") or r"C:\ProgramData")
    app_data = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    return DataPaths(
        machine_dir=program_data / APP_DIR_NAME,
        user_dir=app_data / APP_DIR_NAME,
    )


def _account_slug() -> str:
    """The account name, reduced to what a file name tolerates."""
    name = os.environ.get("USERNAME") or Path.home().name or "user"
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name) or "user"
