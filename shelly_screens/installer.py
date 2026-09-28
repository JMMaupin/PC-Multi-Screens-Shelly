"""Installing, updating and removing Shelly PC Screens.

A single executable does everything, like the self-installing programs one
downloads: started from anywhere else than its installation folder, it
offers to install itself, or to update the installed copy; started from
there, it is the application.

The installation is for every account: the application drives screens
shared by all of them. It therefore goes to `Program Files`, which takes an
administrator -- asked for once, through UAC, by a second copy of the
executable that does the privileged part and exits. The application itself
never runs elevated.

What the elevated part does:
- asks the running instances, in every session, to exit (then stops those
  that do not);
- copies the executable, through a temporary name, so that an interrupted
  copy never leaves half an executable behind;
- prepares the data folder in ProgramData and its permissions: the
  hardware configuration read-only for ordinary accounts, the state, the
  history and the logs writable by all;
- registers the program with Windows: "Installed apps" entry, start at
  sign-in for every account, Start menu shortcut.

What stays with the account that launched it, before and after: importing
an installation from before version 2.0 (its passwords can only be
decrypted by that account), removing its old shortcuts, and starting the
installed application -- not elevated.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
import winreg
from pathlib import Path

from . import __version__, paths, product

log = logging.getLogger("shelly_screens")

INSTALL_ARGUMENT = "--install"
UNINSTALL_ARGUMENT = "--uninstall"
SILENT_ARGUMENT = "--silent"
PURGE_ARGUMENT = "--purge"
# Processes the elevated part must not stop: the copy that asked for it.
SPARE_ARGUMENT = "--spare-pids"

UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\ShellyPCScreens"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = product.APP_NAME
# Always the 64-bit registry: the entry must be where Windows looks for it.
REGISTRY_VIEW = winreg.KEY_WOW64_64KEY

# Well-known accounts, by SID: their names are translated on a French Windows.
ADMINISTRATORS = "*S-1-5-32-544"
SYSTEM = "*S-1-5-18"
USERS = "*S-1-5-32-545"

# The shortcut the old `install-startup.ps1` created, before version 2.0.
LEGACY_LINK_NAME = "Shelly Screens.lnk"

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
STOP_GRACE_S = 12.0


class SetupError(Exception):
    """A step of the installation failed; the message says which."""


# ------------------------------------------------------------------ versions


def version_tuple(text: str) -> tuple[int, ...]:
    """Comparable form of a version; "2.0" and "2.0.0" are the same."""
    parts = []
    for piece in str(text).split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    parts += [0] * (4 - len(parts))
    return tuple(parts[:4])


def installed_version() -> str | None:
    """Version registered with Windows, None if not installed."""
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, UNINSTALL_KEY, 0, winreg.KEY_READ | REGISTRY_VIEW
        ) as key:
            value, _kind = winreg.QueryValueEx(key, "DisplayVersion")
    except OSError:
        return None
    return str(value) if paths.installed_exe().exists() else None


# ------------------------------------------------------------ small helpers


def _powershell(script: str) -> str:
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
    )
    return result.stdout


def _ps_quote(text: str) -> str:
    """A PowerShell single-quoted literal."""
    return "'" + str(text).replace("'", "''") + "'"


def _icacls(*arguments: str) -> None:
    result = subprocess.run(
        ["icacls.exe", *arguments, "/Q"],
        capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
    )
    if result.returncode != 0:
        raise SetupError(f"icacls {' '.join(arguments)}: {result.stdout or result.stderr}")


def _create_link(link: Path, target: Path, arguments: str = "") -> None:
    script = (
        f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut({_ps_quote(link)}); "
        f"$s.TargetPath = {_ps_quote(target)}; "
        f"$s.Arguments = {_ps_quote(arguments)}; "
        f"$s.WorkingDirectory = {_ps_quote(target.parent)}; "
        f"$s.Description = {_ps_quote(product.APP_NAME)}; "
        f"$s.IconLocation = {_ps_quote(str(target) + ',0')}; "
        "$s.Save()"
    )
    _powershell(script)


def _link_arguments(link: Path) -> str:
    return _powershell(
        f"(New-Object -ComObject WScript.Shell).CreateShortcut({_ps_quote(link)}).Arguments"
    ).strip()


def _known_folder(name: str) -> Path | None:
    folder = _powershell(f"[Environment]::GetFolderPath({_ps_quote(name)})").strip()
    return Path(folder) if folder else None


def start_menu_link() -> Path:
    program_data = Path(os.environ.get("ProgramData") or r"C:\ProgramData")
    return (
        program_data / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        / f"{product.APP_NAME}.lnk"
    )


# ------------------------------------------------------ running instances


def _running_instances(spare: set[int]) -> list[int]:
    """Processes of the installed executable, and instances run from the
    sources (`pythonw main.py`), which hold the same data."""
    exe = _ps_quote(paths.installed_exe())
    script = (
        "Get-CimInstance Win32_Process | Where-Object { "
        f"($_.ExecutablePath -and $_.ExecutablePath -ieq {exe}) -or "
        "($_.Name -like 'python*' -and $_.CommandLine -like '*Shelly*main.py*') "
        "} | ForEach-Object { $_.ProcessId }"
    )
    found = [int(pid) for pid in _powershell(script).split() if pid.isdigit()]
    return [pid for pid in found if pid not in spare]


def stop_instances(data: paths.DataPaths, spare: set[int]) -> None:
    """Ask every instance to exit, then stop those still there.

    The request is a file every instance checks on its periodic tick: it
    reaches the other sessions too. An instance that exits by itself saves
    its history first; one from before version 2.0 does not know the
    request, and is stopped after the grace period.
    """
    spare = spare | {os.getpid(), os.getppid()}
    if not _running_instances(spare):
        return
    data.state_dir.mkdir(parents=True, exist_ok=True)
    data.quit_flag.write_text(str(time.time()), encoding="utf-8")
    deadline = time.monotonic() + STOP_GRACE_S
    while time.monotonic() < deadline and _running_instances(spare):
        time.sleep(1.0)
    for pid in _running_instances(spare):
        log.info("Stopping process %s, which did not exit by itself", pid)
        subprocess.run(
            ["taskkill.exe", "/F", "/PID", str(pid)],
            capture_output=True, creationflags=CREATE_NO_WINDOW,
        )
    time.sleep(0.5)


# -------------------------------------------------------- elevated: install


def install(source: Path, spare: set[int]) -> None:
    """The privileged part of an installation or an update."""
    data = paths.default()
    stop_instances(data, spare)
    try:
        target = paths.installed_exe()
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() != target.resolve():
            staging = target.with_name(target.name + ".new")
            shutil.copy2(source, staging)
            # The old executable may take a moment to be released by the
            # processes that were just stopped.
            for _attempt in range(30):
                try:
                    os.replace(staging, target)
                    break
                except PermissionError:
                    time.sleep(0.5)
            else:
                raise SetupError(f"{target} is still in use")
        _prepare_data_folder(data)
        _register(target)
        _create_link(start_menu_link(), target)
    finally:
        data.quit_flag.unlink(missing_ok=True)
    log.info("%s %s installed in %s", product.APP_NAME, __version__, target.parent)


def _prepare_data_folder(data: paths.DataPaths) -> None:
    """Create the data folders and give each its permissions.

    The root, where `machine.json` lives, is read-only for ordinary
    accounts: only an administrator changes the hardware configuration.
    The state, the history and the logs are written by every account's
    instance, so they are writable by all. Files an account created before
    the installation -- by running the sources -- are handed over to the
    administrators, or their creator would keep the right to change them.
    """
    for folder in (data.machine_dir, data.state_dir, data.history_dir, data.log_dir):
        folder.mkdir(parents=True, exist_ok=True)
    root = str(data.machine_dir)
    _icacls(root, "/setowner", ADMINISTRATORS, "/T", "/C")
    _icacls(root, "/reset", "/T", "/C")
    _icacls(
        root, "/inheritance:r", "/grant:r",
        f"{ADMINISTRATORS}:(OI)(CI)F", f"{SYSTEM}:(OI)(CI)F", f"{USERS}:(OI)(CI)RX",
    )
    for folder in (data.state_dir, data.history_dir, data.log_dir):
        _icacls(str(folder), "/grant", f"{USERS}:(OI)(CI)M", "/T", "/C")


def _register(target: Path) -> None:
    """The "Installed apps" entry, and the start at sign-in for everyone."""
    command = f'"{target}"'
    values = {
        "DisplayName": (winreg.REG_SZ, product.APP_NAME),
        "DisplayVersion": (winreg.REG_SZ, __version__),
        "Publisher": (winreg.REG_SZ, product.AUTHOR),
        "DisplayIcon": (winreg.REG_SZ, f"{command},0"),
        "InstallLocation": (winreg.REG_SZ, str(target.parent)),
        "UninstallString": (winreg.REG_SZ, f"{command} {UNINSTALL_ARGUMENT}"),
        "QuietUninstallString": (
            winreg.REG_SZ, f"{command} {UNINSTALL_ARGUMENT} {SILENT_ARGUMENT}"
        ),
        "URLInfoAbout": (winreg.REG_SZ, product.WEBSITE_URL),
        "HelpLink": (winreg.REG_SZ, product.RELEASES_URL),
        "EstimatedSize": (winreg.REG_DWORD, max(1, target.stat().st_size // 1024)),
        "NoModify": (winreg.REG_DWORD, 1),
        "NoRepair": (winreg.REG_DWORD, 1),
    }
    access = winreg.KEY_WRITE | REGISTRY_VIEW
    with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, UNINSTALL_KEY, 0, access) as key:
        for name, (kind, value) in values.items():
            winreg.SetValueEx(key, name, 0, kind, value)
    with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, RUN_KEY, 0, access) as key:
        winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, command)


# ------------------------------------------------------ elevated: uninstall


def uninstall(purge: bool, spare: set[int]) -> None:
    """The privileged part of a removal.

    The executable cannot delete itself while it runs: a detached helper
    removes the folder once every process has let go of it.
    """
    data = paths.default()
    stop_instances(data, spare)
    access = winreg.KEY_WRITE | REGISTRY_VIEW
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, RUN_KEY, 0, access) as key:
            winreg.DeleteValue(key, RUN_VALUE)
    except OSError:
        pass
    try:
        winreg.DeleteKeyEx(winreg.HKEY_LOCAL_MACHINE, UNINSTALL_KEY, REGISTRY_VIEW)
    except OSError:
        pass
    start_menu_link().unlink(missing_ok=True)
    data.quit_flag.unlink(missing_ok=True)
    if purge:
        shutil.rmtree(data.machine_dir, ignore_errors=True)
    _remove_later(paths.install_dir())
    log.info("%s uninstalled%s", product.APP_NAME, " with its data" if purge else "")


def _remove_later(folder: Path) -> None:
    # Retried for ten minutes: the copy that asked for the removal runs
    # from this folder, and keeps its executable busy until its closing
    # message is dismissed.
    script = (
        "Start-Sleep -Seconds 2; "
        "for ($i = 0; $i -lt 600 -and (Test-Path -LiteralPath $d); $i++) { "
        "Remove-Item -LiteralPath $d -Recurse -Force -ErrorAction SilentlyContinue; "
        "Start-Sleep -Seconds 1 }"
    )
    subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
         "-Command", f"$d = {_ps_quote(folder)}; {script}"],
        creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS,
        close_fds=True,
    )


# ---------------------------------------------- the launching account's part


def _legacy_links() -> list[Path]:
    links = []
    for name in ("Startup", "Desktop"):
        folder = _known_folder(name)
        if folder is not None and (folder / LEGACY_LINK_NAME).exists():
            links.append(folder / LEGACY_LINK_NAME)
    return links


def import_legacy_installation() -> Path | None:
    """Import the configuration of an installation from before 2.0.

    Its shortcut says where it lives: `pythonw "<folder>\\main.py"`, the
    configuration next to it. Done by the launching account, the only one
    able to decrypt its passwords, and only when there is no machine
    configuration yet.
    """
    from . import config as config_module

    data = paths.default()
    if data.machine_file.exists():
        return None
    for link in _legacy_links():
        main = Path(_link_arguments(link).strip().strip('"'))
        legacy = main.parent / "config.json"
        if main.name == "main.py" and legacy.exists():
            config_module.import_legacy(legacy, data)
            return legacy
    return None


def remove_legacy_links() -> None:
    """The old shortcuts would start the sources next to the installed copy."""
    for link in _legacy_links():
        if "main.py" in _link_arguments(link):
            link.unlink(missing_ok=True)
            log.info("Old shortcut removed: %s", link)


def start_installed() -> None:
    """Start the installed application, as the account that launched setup."""
    subprocess.Popen(
        [str(paths.installed_exe())],
        cwd=str(paths.install_dir()),
        creationflags=DETACHED_PROCESS,
        close_fds=True,
    )


def spare_argument() -> list[str]:
    """Arguments telling the elevated copy which processes to leave alone."""
    return [SPARE_ARGUMENT, f"{os.getpid()},{os.getppid()}"]


def parse_spare(argv: list[str]) -> set[int]:
    if SPARE_ARGUMENT not in argv:
        return set()
    index = argv.index(SPARE_ARGUMENT)
    try:
        return {int(pid) for pid in argv[index + 1].split(",") if pid.strip().isdigit()}
    except IndexError:
        return set()


def own_executable() -> Path:
    return Path(sys.executable)
