"""Application: tray icon, menu, and reactions to events.

The main thread does only one thing: pump Windows messages. Anything that
talks to the network goes to a worker thread, otherwise the menu would
freeze on every call to the devices.

One deliberate exception: sleep and shutdown. Windows waits for the
program's reply before suspending the machine, so the power-off happens
synchronously there -- and without delay, hence the controller's urgent
mode.
"""

from __future__ import annotations

import os
import sys
import threading
import time

from . import config as config_module
from . import installer
from . import logging_setup
from . import machine_admin
from . import paths
from . import power_history
from . import single_instance
from .i18n import set_language, t
from .config import ALL_ON_PROFILE, AppConfig
from .controller import ApplyReport, NotConnected, ScreenController
from .device import SwitchState
from .win import hotkey as hotkey_module
from .win import icon as icon_module
from .win import monitors
from .win import session as session_module
from .win import taskbar
from .win.shell import WM_SHOW_SETTINGS, MenuItem, TrayWindow

from . import __version__, product

APP_NAME = product.APP_NAME
REFRESH_INTERVAL_MS = 5000
# After Windows refused to hide the taskbar again, wait this long before
# trying anew: insisting at every refresh would only fill the log.
TASKBAR_RETRY_S = 300.0
# How often the on-device script's reach to the other power strips is
# checked (see `sensing.unreachable_remotes`), and how soon it is tried
# again when the device hosting the script did not answer.
SCRIPT_REACH_EVERY_S = 3600.0
SCRIPT_REACH_RETRY_S = 300.0
# A power strip carrying a PC outlet is restarted by itself once its metering
# gives out (see `_restart_faulty_meters`): no sooner than this after the
# last restart, and no more than so many times a day. A freeze that comes
# back that fast is something else, for a person to look at.
METER_RESTART_GAP_S = 1800.0
METER_RESTARTS_PER_DAY = 3


class Application:
    """Ties together the controller, the icon and the system events."""

    def __init__(self, app_config: AppConfig) -> None:
        self.config = app_config
        self.controller = ScreenController(app_config, log=self.log)
        # Consumption history: it feeds on the readings the application
        # already takes, without asking anything more of the device.
        self.history = power_history.HistoryRecorder(
            app_config, self.controller, self.log
        )
        self.tray = TrayWindow(
            tooltip=APP_NAME,
            on_suspend=self._on_suspend,
            on_resume=self._on_resume,
            on_shutdown=self._on_shutdown,
            on_display_change=self._on_display_change,
            on_tick=self._on_tick,
            on_activate=self._on_activate,
            on_hotkey=self._on_hotkey,
            on_session_change=self._check_control,
            build_menu=self._build_menu,
            tick_interval_ms=REFRESH_INTERVAL_MS,
        )
        # Only the session on the screen drives the devices; the others
        # keep their icon but leave the power strips alone.
        self.in_control = self._may_drive()
        self.controller.drives_devices = self.in_control
        self.config.writes_state = self.in_control
        # A request to exit older than this instance is a leftover.
        self._started_at = time.time()
        # The log is set up here if it hasn't been already: a logger with no
        # handler would silently swallow everything, precisely what we want
        # to avoid when there is no console.
        self._logger = logging_setup.setup()
        self.states: dict[str, SwitchState] = {}
        self.online = False
        self.busy = ""  # name of the operation in progress, empty otherwise
        self._busy_lock = threading.Lock()
        self._refreshing = False
        # Devices whose authentication refusal has already been reported:
        # refreshing is periodic, a balloon every five seconds would be
        # unbearable.
        self._auth_warned: set[str] = set()
        # Before this time, a taskbar left visible is not corrected again.
        self._taskbar_retry_at = 0.0
        # Set when the PC has just been located somewhere: the scripts of
        # that location are brought up to date at the next refresh.
        self._location_changed = False
        self.controller.on_location = self._on_location
        # Power strips the on-device script cannot reach, with the reason;
        # checked from the first refresh on, then every hour, and again as
        # soon as another script is installed.
        self.script_unreachable: dict[str, str] = {}
        self._script_reach_due = 0.0
        self._script_reach_checked = ""  # fingerprint of the script checked
        # Automatic restarts of a device whose metering gave out: when, by
        # device, and the devices already announced as beyond that remedy.
        self._meter_restarts: dict[str, list[float]] = {}
        self._meter_given_up: set[str] = set()

    # ------------------------------------------------------------------ log

    def log(self, message: str) -> None:
        """Logs an action. Under pythonw there is no console: everything goes
        to the log file, the only witness of what the application does."""
        self._logger.info(message)

    # ---------------------------------------------------------------- startup

    def start(self) -> None:
        self.log(f"{APP_NAME} {__version__} starting")
        self.tray.create()
        self._update_icon()
        self._install_hotkey()
        if self.in_control:
            # The first connection may require a network scan: in the
            # background, so the icon appears right away.
            threading.Thread(target=self._initial_connect, daemon=True).start()
        elif self.config.newer_version:
            self.log(
                f"Configuration written by version {self.config.newer_version}, newer than "
                "this one: this copy leaves it and the devices alone"
            )
            # Said at once, not only when someone opens the menu: this copy
            # was most likely started by Windows, unseen.
            threading.Timer(3.0, self._explain_passive).start()
        else:
            self.log("Another session is on the screen: this one leaves the devices alone")
        self.tray.run()
        self.log(f"{APP_NAME} stopped")

    # --------------------------------------------------------------- sessions

    def _check_control(self) -> None:
        """Follow which session is on the screen, and hand over control.

        Called when Windows reports a session switch, and on every tick in
        case a notification went missing: a missed handover would leave two
        instances driving the power strips, or none.
        """
        now = self._may_drive()
        if now == self.in_control:
            return
        self.in_control = now
        self.controller.drives_devices = now
        self.config.writes_state = now
        if now:
            self.log("This session is on the screen: it drives the devices again")
            # The session that drove until now noted addresses, screen
            # positions and outlets to restore: start from what it wrote.
            self.config.reload_shared()
            self.controller.forget_unknown()
            # Nothing is switched: the screens stay as the other account
            # left them, until this one picks a profile.
            threading.Thread(
                target=self._initial_connect, kwargs={"at_startup": False}, daemon=True
            ).start()
        elif self.config.newer_version:
            self.log(
                f"Configuration written by version {self.config.newer_version}, newer than "
                "this one: this copy leaves it and the devices alone from now on"
            )
            self._explain_passive()
        else:
            self.log(f"Session switch: {self._driver_label()} now drives the devices")
            self.history.sync()
            self.states = {}
            self.online = False
        self._update_icon()

    def _may_drive(self) -> bool:
        """This copy drives the devices: its session is on the screen, and it
        is not older than the configuration it would write (see
        `AppConfig.newer_version`)."""
        return session_module.is_console_session() and not self.config.newer_version

    def _quit_requested(self) -> bool:
        try:
            return self.config.paths.quit_flag.stat().st_mtime > self._started_at
        except OSError:
            return False

    def _driver_label(self) -> str:
        """Who drives the devices, when it is not this session."""
        user = session_module.console_user()
        return t("{user}'s session", user=user) if user else t("another session")

    def _explain_passive(self) -> None:
        if self.config.newer_version:
            self.tray.notify(
                APP_NAME,
                t("This configuration was saved by version {newer}; this copy is {version}. "
                  "To protect it, this copy changes nothing and drives no device: install "
                  "version {newer} or later.",
                  newer=self.config.newer_version, version=__version__),
            )
            return
        self.tray.notify(
            APP_NAME,
            t("The power strips are driven by {driver}, on the screen right now. "
              "Switch to it to change profiles or settings.", driver=self._driver_label()),
        )

    def _initial_connect(self, at_startup: bool = True) -> None:
        try:
            if not self.config.devices:
                self.log("No device configured yet - open Settings to add one")
                self._update_icon()
                return
            self.controller.connect_all()
            # Protected outputs are set on connection; we apply them again
            # here in case the configuration changed between two launches.
            self.controller.refresh_protection()
            # Locates the PC, and then brings that location's scripts up to
            # date (see `_sync_location`).
            self._refresh_states()
            if at_startup and self.config.settings.apply_profile_on_start:
                name = self.config.settings.last_profile
                if name and self.config.profile(name):
                    self.log(f"Applying profile '{name}' at startup")
                    self._apply_profile_sync(name)
        except Exception as exc:  # noqa: BLE001 - a failed startup must not kill the app
            self.log(f"Startup error: {exc}")

    def stop(self) -> None:
        self.history.sync()
        # A ghost screen taken off the desktop would stay so once its outlet
        # is back on: nothing would be left to put it back.
        try:
            self.controller.release_screens()
        except Exception as exc:  # noqa: BLE001 - quitting must go through
            self.log(f"Screens not put back on the desktop: {exc}")
        self.tray.stop()

    # ----------------------------------------------------------------- state

    def _on_location(self, _key: str | None) -> None:
        """The PC has been located somewhere else: from the reading thread."""
        self._location_changed = True

    def _sync_location(self) -> None:
        """Bring the scripts of the location the PC is at up to date.

        Each location's PC outlet carries its own: the first time the PC is
        used somewhere, they are installed there.
        """
        if self.config.location is None:
            return
        from . import sensing

        try:
            changed = sensing.sync_installed(self.controller, self.config)
            if changed:
                self.log(f"On-device script {changed}")
            # The probe sees the consumption while the PC sleeps: the
            # history depends on it, so it must be installed and up to date.
            probe = sensing.sync_probe(self.controller, self.config)
            if probe:
                self.log(f"On-device probe {probe}")
            # Without a stored profile, the on-device script would only turn
            # the boot screen back on: we leave it at least the current state.
            published = sensing.ensure_published(self.controller, self.config)
            if published:
                self.log(f"Published outlets for the on-device script: {published}")
            self.config.save()
        except Exception as exc:  # noqa: BLE001 - the outlets stay driven without it
            self.log(f"On-device scripts not brought up to date: {exc}")

    def _refresh_states(self) -> None:
        """Rereads the outlet states and updates the icon."""
        try:
            self.states = self.controller.read_outlets()
            self.online = bool(self.states)
            if self._location_changed:
                self._location_changed = False
                self._sync_location()
            try:
                self.history.feed(self.states)
            except Exception as exc:  # noqa: BLE001 - never at the expense of control
                self.log(f"History not recorded: {exc}")
            # Ghosts first: the layout is judged on the outlet states just
            # read, and on the screens as they will stay.
            self._settle_ghosts()
            self._remember_screens()
            self._check_script_reach()
            self._restart_faulty_meters()
        except (NotConnected, OSError) as exc:
            self.online = False
            self.log(f"Refresh failed: {exc}")
        self._warn_about_auth_failures()
        # After the ghosts: taking one off the desktop is one of the screen
        # changes that make Windows drop the auto-hide.
        self.keep_taskbar_hidden()
        self._update_icon()

    def keep_taskbar_hidden(self, force: bool = False) -> None:
        """Put the taskbar's auto-hide back if Windows dropped it.

        Off the message thread: the shell answers through Explorer, and a
        hung Explorer must not freeze the tray icon. `force` ignores the
        pause after a refusal -- the box has just been ticked.
        """
        if not self.config.settings.keep_taskbar_autohide:
            return
        if not force and time.monotonic() < self._taskbar_retry_at:
            return
        try:
            if taskbar.auto_hide() is not False:
                return  # hidden already, or no taskbar to set
            if taskbar.set_auto_hide(True):
                self._taskbar_retry_at = 0.0
                self.log("Taskbar auto-hide was off: turned back on")
            else:
                self._taskbar_retry_at = time.monotonic() + TASKBAR_RETRY_S
                self.log("Taskbar auto-hide is off, and Windows did not take it back")
        except OSError as exc:
            self._taskbar_retry_at = time.monotonic() + TASKBAR_RETRY_S
            self.log(f"Taskbar auto-hide not checked: {exc}")

    def _warn_about_auth_failures(self) -> None:
        """Warns only once per device that refuses the password."""
        failures = set(self.controller.auth_failures)
        for key in sorted(failures - self._auth_warned):
            self.log(f"Device '{key}' refuses the stored password")
            self.tray.notify(
                APP_NAME,
                f"{key}: wrong or missing password. Open Settings > Devices "
                "to fix it, or reset the device with its buttons.",
            )
        # A device that became reachable again may warn again later.
        self._auth_warned = failures

    def _check_script_reach(self) -> None:
        """Make sure the on-device script reaches the other power strips.

        It switches their screens off once the PC sleeps or shuts down, and
        nothing else can by then. When it fails, the screens stay on all
        night, and nothing says so: the device is the only one to know.
        Hence the check -- at the first refresh, then hourly -- and a
        balloon the first time a power strip drops out of its reach.
        """
        if self.config.location is None or self.busy:
            return  # an operation in progress has the devices first
        now = time.monotonic()
        installed = self.config.sensing.installed_fingerprint
        if now < self._script_reach_due and installed == self._script_reach_checked:
            return
        self._script_reach_checked = installed
        from . import sensing

        try:
            failures = sensing.unreachable_remotes(self.controller, self.config)
        except Exception:  # noqa: BLE001 - its device is silent, and shown as such
            self._script_reach_due = now + SCRIPT_REACH_RETRY_S
            return
        self._script_reach_due = now + SCRIPT_REACH_EVERY_S
        lost = sorted(failures.keys() - self.script_unreachable.keys())
        for key in lost:
            self.log(f"On-device script cannot reach '{key}': {failures[key]}")
        for key in sorted(self.script_unreachable.keys() - failures.keys()):
            self.log(f"On-device script reaches '{key}' again")
        self.script_unreachable = failures
        if lost:
            self.tray.notify(
                APP_NAME,
                t("The power strip that watches the PC cannot reach {devices}: "
                  "their screens will stay on while the PC sleeps or is off.",
                  devices=", ".join(lost)),
            )

    def _restart_faulty_meters(self) -> None:
        """Restart a power strip carrying a PC outlet once its metering gives out.

        Everything rests on that measurement: the on-device script switches
        the screens on when it sees the PC draw, off when it stops, and the
        location is recognised by it. Frozen, it held a standby 1 W while
        the PC drew 150 (2026-10-29): no screen came on at boot. This
        application runs on the PC, so the PC is on; a restart is the only
        known remedy, and a harmless one -- latching relays, checked again
        that day: the PC's outlet did not move. Bounded all the same (see
        `METER_RESTART_GAP_S`), and announced.
        """
        if self.busy or not self.in_control:
            return
        pc_devices ={o.device for o in self.config.outlets if o.host_pc}
        faulty = self.controller.faulty_meter_devices() & pc_devices
        if not faulty:
            self._meter_given_up.clear()
            return
        now = time.monotonic()
        due = []
        for key in sorted(faulty):
            recent = [
                at for at in self._meter_restarts.get(key, []) if now - at < 86400.0
            ]
            self._meter_restarts[key] = recent
            if recent and now - recent[-1] < METER_RESTART_GAP_S:
                continue
            if len(recent) >= METER_RESTARTS_PER_DAY:
                if key not in self._meter_given_up:
                    self._meter_given_up.add(key)
                    self.log(f"Device '{key}': metering still faulty after "
                             f"{len(recent)} restarts today, left as is")
                    self.tray.notify(APP_NAME, t(
                        "The measurement of {device} keeps failing, even after {count} "
                        "restarts today: its screens no longer follow the PC. Unplug "
                        "it for a few seconds, or check its firmware.",
                        device=key, count=len(recent)))
                continue
            due.append(key)
        if not due:
            return
        refs = ", ".join(sorted(
            r for r in self.controller.faulty_meters() if r.split(":")[0] in due
        ))

        def worker() -> None:
            for key in due:
                self.controller.reboot_device(key)
            time.sleep(15.0)
            self.controller.connect_all(allow_scan=False)
            # Measured again, the PC may well be somewhere else.
            self.controller.unsettle_location()

        if self._run_async("Restarting device", worker):
            for key in due:
                self._meter_restarts[key].append(now)
            self.log(f"Metering faulty ({refs}): restarting {', '.join(due)}")
            self.tray.notify(APP_NAME, t(
                "The measurement of {devices} had frozen: restarting it. "
                "Its outlets stay as they are.", devices=", ".join(due)))

    def _outlet_states(self) -> list[bool]:
        """Outlet states, in configuration order."""
        return [
            bool(self.states.get(outlet.ref) and self.states[outlet.ref].output)
            for outlet in self.config.outlets
        ]

    def _update_icon(self) -> None:
        """Resets the icon: the application's artwork, status dot included.

        The outlet count is no longer drawn but given by the tooltip: at
        sixteen pixels square, a dot is readable, a count is not.
        """
        if not self.in_control:
            status = "passive"
        elif self.config.devices and self.config.location is None:
            status = "away"
        else:
            status = icon_module.status_for(
                self._outlet_states(),
                online=self.online,
                complete=self.controller.fully_connected
                and not self.controller.auth_failures,
            )
        path = icon_module.write_ico(status)
        self.tray.set_icon(str(path), self._tooltip())

    def _tooltip(self) -> str:
        if not self.in_control:
            if self.config.newer_version:
                return f"{APP_NAME} {__version__} - " + t(
                    "older than its configuration ({newer}): changes nothing",
                    newer=self.config.newer_version,
                )
            return f"{APP_NAME} - " + t("driven by {driver}", driver=self._driver_label())
        if not self.config.devices:
            return f"{APP_NAME} - no device configured"
        if self.config.location is None:
            return f"{APP_NAME} - " + t("at no known location")
        if not self.online:
            return f"{APP_NAME} - no device reachable"
        states = self._outlet_states()
        total = sum(state.apower for state in self.states.values())
        profile = self.config.settings.last_profile or "no profile"
        if self.config.has_several_locations():
            profile = f"{self.config.location_label(self.config.location)} - {profile}"
        suffix = ""
        if not self.controller.fully_connected:
            missing = len(self.controller.missing_here())
            suffix = f" - {missing} device(s) offline"
        return (
            f"{APP_NAME} - {profile} - {sum(states)}/{len(states)} on "
            f"- {total:.0f} W{suffix}"
        )

    # --------------------------------------------------------------- actions

    def _run_async(self, label: str, function) -> bool:
        """Runs an operation in the background, one at a time; false if refused."""
        if not self.in_control:
            self.log(f"Ignored '{label}': another session drives the devices")
            self._explain_passive()
            return False
        with self._busy_lock:
            if self.busy:
                self.log(f"Ignored '{label}': '{self.busy}' still running")
                return False
            self.busy = label

        def worker() -> None:
            try:
                function()
            except Exception as exc:  # noqa: BLE001 - report without killing the thread
                self.log(f"{label} failed: {exc}")
                self.tray.notify(APP_NAME, f"{label} failed: {exc}")
            finally:
                with self._busy_lock:
                    self.busy = ""
                self._refresh_states()

        threading.Thread(target=worker, name=label, daemon=True).start()
        return True

    def apply_profile(self, name: str) -> None:
        self._run_async(f"Apply '{name}'", lambda: self._apply_profile_sync(name))

    def _apply_profile_sync(self, name: str) -> ApplyReport:
        report = self.controller.apply_profile(name)
        if report.errors:
            self.tray.notify(APP_NAME, "; ".join(report.errors[:2]))
        return report

    def toggle_outlet(self, ref: str) -> None:
        state = self.states.get(ref)
        self.set_outlet(ref, not (state and state.output))

    def set_outlet(self, ref: str, on: bool) -> bool:
        """Switches one outlet in the background; false if not started."""
        outlet = self.config.outlet(ref)
        label = f"{outlet.label if outlet else ref} {'on' if on else 'off'}"
        return self._run_async(
            label, lambda: self.controller.set_outlet(ref, on, keep_a_screen=True)
        )

    def refresh_now(self) -> None:
        self._run_async("Refresh", lambda: None)

    def reconnect(self) -> None:
        self._run_async("Reconnect", lambda: self.controller.connect_all(allow_scan=True))

    # ------------------------------------------------------------------ menu

    def _build_menu(self) -> list[MenuItem]:
        items: list[MenuItem] = []

        if not self.in_control:
            # Reading is harmless, driving is not: the history stays, the
            # profiles and outlets go.
            if self.config.newer_version:
                items.append(MenuItem.info(t(
                    "Version {version}, older than its configuration ({newer}): changes nothing",
                    version=__version__, newer=self.config.newer_version,
                )))
            else:
                items.append(MenuItem.info(t("Driven by {driver}", driver=self._driver_label())))
            items.append(MenuItem.sep())
            items.append(
                MenuItem(t("Consumption history..."), action=self._open_history)
            )
            items.append(MenuItem(t("Open log file"), action=self.open_log))
            items.append(MenuItem(t("Quit"), action=self.stop))
            return items

        if not self.config.devices:
            items.append(MenuItem.info(t("No device configured")))
            items.append(MenuItem(t("Add a device..."), action=self._open_settings))
            items.append(MenuItem.sep())
            items.append(MenuItem(t("Quit"), action=self.stop))
            return items

        if self.online:
            states = self._outlet_states()
            total = sum(state.apower for state in self.states.values())
            items.append(
                MenuItem.info(f"{sum(states)}/{len(states)} outlets on - {total:.0f} W")
            )
            if not self.controller.fully_connected:
                offline = self.controller.missing_here()
                items.append(MenuItem.info(f"Offline: {', '.join(offline)}"))
                items.append(MenuItem(t("Reconnect"), action=self.reconnect))
            if self.controller.auth_failures:
                refused = ", ".join(sorted(self.controller.auth_failures))
                items.append(MenuItem.info(f"Password refused: {refused}"))
                items.append(MenuItem(t("Fix password..."), action=self._open_settings))
        else:
            items.append(MenuItem.info(t("No device reachable")))
            items.append(MenuItem(t("Reconnect"), action=self.reconnect))

        frozen = self.controller.faulty_meters()
        if frozen:
            names = ", ".join(sorted(
                self.config.outlet(r).label
                for r in frozen if self.config.outlet(r) is not None
            ))
            items.append(
                MenuItem.info(t("Frozen measurement: {outlets}", outlets=names))
            )
            items.append(
                MenuItem(t("Restart the device"), action=self.restart_frozen)
            )

        if self.script_out_of_date:
            # The most visible spot: the menu opens with a right-click,
            # without having to know where to look. A mismatch between the
            # settings and the installed script is impossible to guess
            # otherwise.
            items.append(MenuItem.info(t("On-device script is out of date")))
            items.append(
                MenuItem(t("Update it now"), action=self.update_script)
            )
        if self.script_unreachable:
            items.append(MenuItem.info(t(
                "On-device script cannot reach {devices}",
                devices=", ".join(sorted(self.script_unreachable)),
            )))

        if self.busy:
            items.append(MenuItem.info(f"Busy: {self.busy}"))

        items.append(MenuItem.sep())
        if self.config.location is None:
            # No profile anywhere: the outlets in reach belong to a place the
            # PC is not at. Switching one by hand stays possible below.
            items.append(MenuItem.info(t("At no known location: profiles are off")))
        else:
            if self.config.has_several_locations():
                items.append(MenuItem.info(
                    t("At {location}", location=self.config.location_label(self.config.location))
                ))
            items.extend(self._profile_items())
        items.append(MenuItem.sep())
        items.append(MenuItem(t("Outlets"), submenu=self._outlet_items()))
        items.append(MenuItem(t("Screens"), submenu=self._screen_items()))
        items.append(MenuItem.sep())
        items.append(
            MenuItem(t("Consumption history..."), action=self._open_history)
        )
        items.append(MenuItem(t("Settings..."), action=self._open_settings))
        items.append(MenuItem(t("Refresh"), action=self.refresh_now))
        items.append(MenuItem(t("Open log file"), action=self.open_log))
        items.append(MenuItem(t("Quit"), action=self.stop))
        return items

    @property
    def script_out_of_date(self) -> bool:
        """Have the settings changed since the last installation?

        Computed locally, without the network: it can therefore be asked on
        every menu build without costing the device anything.
        """
        from . import sensing

        try:
            return sensing.needs_update(self.config)
        except Exception:  # noqa: BLE001 - a doubt must not break anything
            return False

    def update_script(self) -> None:
        """Reinstalls the on-device script with the current settings."""
        from . import sensing

        def worker() -> None:
            status = sensing.install(self.controller, self.config)
            # `install` records the new fingerprint: it must be saved,
            # otherwise the warning would come back at the next startup.
            self.config.save()
            self.log(f"On-device script updated: {status.summary()}")

        self._run_async("Updating script", worker)

    def restart_frozen(self) -> None:
        """Restarts the devices with a frozen measurement channel.

        It is the only known remedy for this firmware defect, and it is
        harmless: latching relays, and `initial_state` brings the PC's
        outlet back on no matter what.
        """
        keys = self.controller.faulty_meter_devices()

        def worker() -> None:
            for key in sorted(keys):
                self.controller.reboot_device(key)
            time.sleep(15.0)
            self.controller.connect_all(allow_scan=False)
            self.controller.unsettle_location()

        self._run_async("Restarting device", worker)

    def open_log(self) -> None:
        """Opens the log file in the associated editor.

        Without a console, it is the only way to see what the application does.
        """
        path = logging_setup.log_path()
        try:
            os.startfile(str(path))  # noqa: S606 - opened by the system's editor
        except OSError as exc:
            self.log(f"Cannot open the log file ({path}): {exc}")

    def _profile_items(self) -> list[MenuItem]:
        profiles = self.config.sorted_profiles(here_only=True)
        if not profiles:
            return [MenuItem.info("No profile configured")]
        current = self.config.settings.last_profile
        items: list[MenuItem] = []
        for profile in profiles:
            count = len(self.config.profile_screens(profile))
            detail = (t("{count} screen(s)", count=count) if count
                      else t("screens off"))
            items.append(
                MenuItem(
                    label=f"{profile.label}  ({detail})",
                    action=lambda name=profile.name: self.apply_profile(name),
                    checked=profile.name == current,
                    enabled=self.online and not self.busy,
                )
            )
        return items

    def _outlet_items(self) -> list[MenuItem]:
        """Outlets, grouped by device when there are several."""
        if len(self.config.devices) <= 1:
            return self._outlet_entries(self.config.outlets)
        items: list[MenuItem] = []
        for device in self.config.devices:
            outlets = self.config.outlets_of(device.key)
            if not outlets:
                continue
            reachable = device.key in self.controller.online_keys
            label = device.label if reachable else f"{device.label} (offline)"
            items.append(MenuItem(label, submenu=self._outlet_entries(outlets)))
        return items or [MenuItem.info("No outlet")]

    def _outlet_entries(self, outlets: list) -> list[MenuItem]:
        items: list[MenuItem] = []
        powered = {ref for ref, state in self.states.items() if state.output}
        for outlet in outlets:
            state = self.states.get(outlet.ref)
            # The last screen on can't be turned off from here: the menu
            # would vanish with it, and nothing would let it be turned back on.
            last_screen = (
                outlet.is_screen
                and outlet.ref in powered
                and not self.config.leaves_a_screen(powered - {outlet.ref})
            )
            power = f" - {state.apower:.0f} W" if state and state.output else ""
            tags = []
            if outlet.host_pc:
                tags.append("PC")
            if outlet.critical:
                tags.append("critical")
            if outlet.boot_screen:
                tags.append("boot")
            suffix = f" [{', '.join(tags)}]" if tags else ""
            items.append(
                MenuItem(
                    label=f"{outlet.label}{suffix}{power}",
                    action=lambda r=outlet.ref: self.toggle_outlet(r),
                    checked=bool(state and state.output),
                    enabled=(
                        state is not None
                        and not self.busy
                        and not outlet.never_switch_off
                        and not last_screen
                    ),
                )
            )
        return items

    def _screen_items(self) -> list[MenuItem]:
        """The screens that are on and their position, as Windows defines it."""
        names = {o.monitor_key: o.label for o in self.config.outlets if o.monitor_key}
        placed = monitors.arrangement(monitors.list_monitors(), names)
        items = [MenuItem.info(f"{name} — {where}") for _m, name, where in placed]
        if not items:
            items = [MenuItem.info(t("No screen detected"))]
        items.append(MenuItem.sep())
        items.append(
            MenuItem(t("Capture screen layout"), action=self.capture_screen_layout)
        )
        return items

    # ----------------------------------------------------------- system events

    def _on_tick(self) -> None:
        """Periodic refresh, without piling up requests."""
        if self._quit_requested():
            # An update or a removal is under way, possibly started from
            # another session: exit cleanly, history saved, so the
            # executable can be replaced.
            self.log("The installer asked this instance to exit")
            self.stop()
            return
        self._check_control()
        if self.config.shared_files_changed():
            # Another session saved: an administrator's hardware change, or
            # the state noted by the session that drives the devices.
            self.config.reload_shared()
            self.controller.forget_unknown()
        if not self.in_control:
            return
        if self._refreshing or self.busy or not self.config.devices:
            return
        self._refreshing = True

        def worker() -> None:
            try:
                self._refresh_states()
            finally:
                self._refreshing = False

        threading.Thread(target=worker, daemon=True).start()

    def _on_activate(self) -> None:
        """Left click on the icon: open the settings window."""
        self._open_settings()

    def _on_display_change(self) -> None:
        if not self.in_control:
            return  # the screens show another session: not ours to note
        self.log("Display configuration changed")
        self._remember_screens()

    def capture_screen_layout(self, on_done=None, progress=None) -> None:
        """Captures the screen layout, in the background.

        From the icon menu, without `on_done`, everything goes back
        immediately: there is no window in which to ask the question. With
        `on_done(capture)`, the screens stay on and the caller offers to
        stay that way or to go back (`return_after_capture`). Both callbacks
        come from the task's thread: it is up to the caller to switch back
        to its own.
        """
        def run() -> None:
            capture = self.controller.capture_screen_layout(
                restore=on_done is None, progress=progress
            )
            if on_done is None:
                self.tray.notify(APP_NAME, capture.message)
            else:
                on_done(capture)

        self._run_async("Capture screen layout", run)

    def apply_outlets(self, targets: dict[str, bool]) -> None:
        """Applies a selection of outlets outside any profile, in the background."""
        self._run_async("Screen selection", lambda: self.controller.apply_outlets(targets))

    def stay_after_capture(self) -> None:
        """Keeps everything on: "All on" becomes the current profile."""
        self._run_async(
            "Keep 'All on'", lambda: self.controller.adopt_profile(ALL_ON_PROFILE)
        )

    def return_after_capture(self, capture) -> None:
        """Goes back to the pre-capture profile, or failing that the prior state.

        Reapply the profile rather than turn the lit outlets off again one
        by one: it is what the user chose to get back, windows included.
        """
        name = self.config.settings.last_profile
        if name and self.config.profile(name) is not None:
            self.apply_profile(name)
        else:
            self._run_async("Restore outlets", lambda: self.controller.undo_capture(capture))

    def _settle_ghosts(self) -> None:
        """Keeps ghost screens off the desktop, without ever getting in the way."""
        if self.busy:
            return  # the operation in progress settles them itself
        try:
            self.controller.settle_ghosts()
        except Exception as exc:  # noqa: BLE001 - the screens stay as Windows has them
            self.log(f"Ghost screens not handled: {exc}")

    def _remember_screens(self) -> None:
        """Records the screens' positions, without ever getting in the way."""
        if self.busy:
            return  # an operation is in progress: the state read is already stale
        try:
            self.controller.remember_screens(self.states)
        except Exception as exc:  # noqa: BLE001 - a failed capture is not fatal
            self.log(f"Screen positions not read: {exc}")

    def _on_suspend(self) -> None:
        """Going to sleep: switch off, but keep what must stay on.

        Synchronous on purpose -- Windows suspends the process as soon as we
        return, and a power-off started in the background wouldn't have
        time to complete.
        """
        # History first: Windows suspends the process as soon as we return,
        # and whatever isn't on disk would be lost if the PC didn't wake
        # up.
        self.history.sync()
        # Every session hears about the sleep; only the one driving acts,
        # or the relays would be commanded twice at the worst moment.
        if not self.in_control:
            return
        if not self.config.settings.power_off_on_suspend or not self.config.devices:
            return
        if self.config.sensing.enabled:
            # The on-device script already switches off, after its
            # confirmation delay. Switching off here as well would click the
            # relays at the very moment of going to sleep, for no benefit:
            # we just record what will need to be restored on wake.
            try:
                self.controller.remember_for_resume()
            except Exception as exc:  # noqa: BLE001
                self.log(f"Could not record the pre-sleep state: {exc}")
            self.log("Suspending: leaving the outlets to the on-device script")
            return
        self.log("Suspending: switching screens off")
        try:
            report = self.controller.prepare_for_suspend()
            self.log(report.summary())
        except Exception as exc:  # noqa: BLE001 - never block sleep
            self.log(f"Suspend handling failed: {exc}")

    def _on_resume(self) -> None:
        """Wake: reapply the last profile."""
        # The session on the screen may have changed while the PC slept, and
        # the PC may have travelled while hibernated.
        self._check_control()
        self.controller.unsettle_location()
        if not self.in_control:
            return
        # During sleep, only the on-device probe measured: its ticks will
        # fill the gap on the next read.
        self.history.request_recovery()
        if not self.config.settings.restore_on_resume:
            self._on_tick()
            return
        self.log("Resuming")

        def worker() -> None:
            # The network takes a moment to come back after wake: without
            # this pause, the first requests would be sure to fail.
            time.sleep(3.0)
            self.controller.connect_all(allow_scan=False)
            try:
                self.controller.resume()
            except Exception as exc:  # noqa: BLE001
                self.log(f"Resume failed: {exc}")
            self._refresh_states()

        self._run_async("Resume", worker)

    def _on_shutdown(self) -> None:
        """Shutdown or restart: same handling as sleep."""
        self._on_suspend()

    # ------------------------------------------------------------- settings

    def _install_hotkey(self) -> None:
        """Registers the profile shortcut at startup, and says if it's missing.

        A shortcut already taken elsewhere goes unnoticed: you press it,
        nothing happens, and you blame the application. Hence the balloon,
        once.
        """
        text = self.config.settings.profile_hotkey
        wanted = hotkey_module.parse(text)
        if wanted is None:
            if text:
                self.log(f"Ignored unreadable profile shortcut '{text}'")
            return
        if self.tray.set_hotkey(wanted):
            self.log(f"Profile shortcut {wanted} active")
            return
        self.log(f"Profile shortcut {wanted} is already used by another program")
        self.tray.notify(
            APP_NAME,
            t("The shortcut {hotkey} is already used by another program. "
              "Choose another one in Settings > Behaviour.", hotkey=str(wanted)),
        )

    def _on_hotkey(self) -> None:
        from .ui.profile_picker import toggle_picker

        if not self.in_control:
            self._explain_passive()
            return
        toggle_picker(self)

    def _open_history(self) -> None:
        from .ui.history_window import open_history

        open_history(self)

    def _open_settings(self) -> None:
        from .ui.settings import open_settings

        # The settings window commands the devices at every turn -- the
        # identification assistant, the script, the password: it belongs to
        # the session that drives them.
        if not self.in_control:
            self._explain_passive()
            return
        open_settings(self)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # The elevated copy started to save the hardware configuration does
    # that and nothing else: no log, no icon, no single-instance lock --
    # the instance that asked for it is holding that lock.
    if machine_admin.COMMIT_ARGUMENT in argv:
        index = argv.index(machine_admin.COMMIT_ARGUMENT)
        try:
            source, machine_dir = argv[index + 1], argv[index + 2]
        except IndexError:
            return machine_admin.EXIT_INVALID
        return machine_admin.commit_from_command_line(source, machine_dir)

    if installer.INSTALL_ARGUMENT in argv or installer.UNINSTALL_ARGUMENT in argv:
        return _setup_command(argv)

    # The executable started from anywhere but its installation folder --
    # a download, a USB stick -- offers to install or update itself first.
    if paths.is_frozen() and not paths.running_installed_copy():
        outcome = _offer_setup()
        if outcome is not None:
            return outcome

    verbose = "--verbose" in argv or "-v" in argv
    # The log is set up before anything else: without it, an error while
    # loading the configuration would vanish without a trace.
    logger = logging_setup.setup(verbose=verbose)

    # A single instance: two programs writing the same configuration file
    # step on each other, and the last one to save wipes out the other's
    # work.
    if not single_instance.acquire():
        shown = single_instance.wake_existing(
            TrayWindow.CLASS_NAME, WM_SHOW_SETTINGS
        )
        logger.info(
            "Already running; %s",
            "asked the running instance to show its settings"
            if shown
            else "could not find its window",
        )
        return 0

    app_config = config_module.load()
    set_language(app_config.settings.language)
    application = Application(app_config)
    try:
        application.start()
    finally:
        single_instance.release()
    return 0


# ----------------------------------------------------------------- setup


def _offer_setup() -> int | None:
    """Ask what to do with a non-installed executable; None to just run it."""
    from .ui import setup_dialog

    choice = setup_dialog.ask_setup(installer.installed_version())
    if choice == "run":
        return None
    if choice == "open":
        installer.start_installed()
        return 0
    if choice == "install":
        return _install_from_launcher(silent=False)
    return 0


def _install_from_launcher(silent: bool) -> int:
    """Install or update, from the account that launched the executable.

    This account imports an older installation first -- only it can
    decrypt its passwords --, hands the privileged part to an elevated
    copy, then removes its old shortcuts and starts the installed
    application, not elevated.
    """
    from .ui import setup_dialog
    from .win import elevation

    logger = logging_setup.setup()
    try:
        legacy = installer.import_legacy_installation()
        if legacy is not None:
            logger.info("Imported the configuration of %s", legacy)
    except Exception:  # noqa: BLE001 - the installation goes on without it
        logger.exception("Could not import the previous installation")

    try:
        code = elevation.run_elevated(
            [installer.INSTALL_ARGUMENT, *installer.spare_argument()], timeout_s=300
        )
    except elevation.ElevationCancelled:
        logger.info("Installation cancelled at the UAC prompt")
        return 1
    except (OSError, TimeoutError) as exc:
        code = -1
        logger.error("Installation could not run: %s", exc)
    if code != 0:
        if not silent:
            setup_dialog.show_message(
                t("The installation failed (code {code}). The log is in {folder}.",
                  code=code, folder=paths.default().log_dir),
                error=True,
            )
        return code or 1

    installer.remove_legacy_links()
    installer.start_installed()
    return 0


def _setup_command(argv: list[str]) -> int:
    """`--install` and `--uninstall`, elevated or not yet."""
    import shutil

    from .ui import setup_dialog
    from .win import elevation

    logger = logging_setup.setup()
    silent = installer.SILENT_ARGUMENT in argv
    spare = installer.parse_spare(argv)
    elevated = elevation.is_elevated()

    if installer.INSTALL_ARGUMENT in argv:
        if not elevated:
            return _install_from_launcher(silent=silent)
        try:
            installer.install(installer.own_executable(), spare)
        except Exception:  # noqa: BLE001 - reported through the exit code and the log
            logger.exception("Installation failed")
            return 1
        return 0

    purge = installer.PURGE_ARGUMENT in argv
    if not silent:
        go, purge = setup_dialog.confirm_uninstall()
        if not go:
            return 0
    if elevated:
        try:
            installer.uninstall(purge, spare)
        except Exception:  # noqa: BLE001
            logger.exception("Uninstallation failed")
            return 1
        return 0

    arguments = [installer.UNINSTALL_ARGUMENT, installer.SILENT_ARGUMENT]
    if purge:
        arguments.append(installer.PURGE_ARGUMENT)
    try:
        code = elevation.run_elevated(arguments + installer.spare_argument(), timeout_s=300)
    except elevation.ElevationCancelled:
        return 1
    if code == 0 and purge:
        # This account's own preferences; other accounts keep theirs.
        shutil.rmtree(paths.default().user_dir, ignore_errors=True)
    if code == 0 and not silent:
        setup_dialog.show_message(t("{app} has been uninstalled.", app=APP_NAME))
    return code
