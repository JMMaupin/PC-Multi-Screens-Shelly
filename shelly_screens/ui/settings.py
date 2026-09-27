"""Settings window: devices, outlets, profiles, behaviour.

Tkinter requires every call to come from the thread that created the root.
The window therefore runs in its own thread, with its own loop, and never
touches the tray icon's message loop directly. Only one window at a time,
otherwise two Tk roots would coexist in the same process.
"""

from __future__ import annotations

import threading
import time
import tkinter as tk
import webbrowser
from tkinter import messagebox, simpledialog, ttk
from typing import TYPE_CHECKING

from . import screen_map
from . import theme as theme_module
from .. import discovery, sensing
from .. import i18n
from ..i18n import t
from ..config import KIND_LABELS, KIND_SCREEN, KINDS, OutletConfig, Profile
from ..win import icon as icon_module
from .. import __version__, product, wifi_setup
from ..win import hotkey as hotkey_module
from ..win import monitors
from .. import device_leds, device_services

if TYPE_CHECKING:
    from ..app import Application

_state_lock = threading.Lock()
_is_open = False

# Above this power draw, an outlet is not considered to carry a screen,
# and the identification assistant refuses to touch it. A monitor, even a
# large one, rarely exceeds 60 W; a desktop tower draws more than 100.
# This is a physical safeguard: it does not depend on any labelling.
IDENTIFY_MAX_WATTS = 80.0
# Refresh of the power log status, as a number of 3-second cycles.
SENSING_REFRESH_TICKS = 5
# One refresh of the measurement every N 3-second cycles.
SENSING_REFRESH_TICKS = 5


def open_settings(application: "Application") -> None:
    """Open the settings window, unless it is already open."""
    global _is_open
    with _state_lock:
        if _is_open:
            return
        _is_open = True

    def run() -> None:
        global _is_open
        try:
            root = tk.Tk()
            SettingsWindow(root, application)
            root.mainloop()
        except Exception as exc:  # noqa: BLE001 - a failed UI must not kill the app
            application.log(f"Settings window failed: {exc}")
        finally:
            with _state_lock:
                _is_open = False

    threading.Thread(target=run, name="settings-ui", daemon=True).start()


class SettingsWindow:
    """Contents of the settings window."""

    def __init__(self, root: tk.Tk, application: "Application") -> None:
        self.root = root
        self.app = application
        self.config = application.config

        # Before building anything: widgets read their text only once.
        i18n.set_language(self.config.settings.language)
        icon_module.apply_to_window(root)
        root.title(t("Shelly Screens {version} - Settings", version=__version__))
        # The Devices view lines up 944 pixels of columns; any narrower and
        # the last ones get truncated with nothing to show it.
        root.geometry("1020x760")
        root.minsize(900, 640)

        # The theme must be set before the widgets are created: some of
        # them read their colours at construction time.
        self.palette = theme_module.apply(root, self.config.settings.theme)
        self._system_was_dark = theme_module.system_prefers_dark()
        self._sensing_ticks = 0

        notebook = self.notebook = ttk.Notebook(root)
        notebook.pack(fill="both", expand=True, padx=10, pady=(10, 0))

        self.devices_tab = ttk.Frame(notebook, padding=12)
        self.outlets_tab = ttk.Frame(notebook, padding=12)
        self.profiles_tab = ttk.Frame(notebook, padding=12)
        self.behaviour_tab = ttk.Frame(notebook, padding=12)
        self.sensing_tab = ttk.Frame(notebook, padding=12)
        self.about_tab = ttk.Frame(notebook, padding=24)
        notebook.add(self.devices_tab, text=t("Devices"))
        notebook.add(self.outlets_tab, text=t("Outlets"))
        notebook.add(self.profiles_tab, text=t("Profiles"))
        notebook.add(self.sensing_tab, text=t("PC power"))
        notebook.add(self.behaviour_tab, text=t("Behaviour"))
        notebook.add(self.about_tab, text=t("About"))
        # Without this call, Ctrl+Tab and Alt+letter do not switch tabs.
        notebook.enable_traversal()

        self.status = tk.StringVar(self.root, value="")
        ttk.Label(root, textvariable=self.status, anchor="w", padding=(12, 6)).pack(
            fill="x", side="bottom"
        )

        self._build_devices_tab()
        self._build_outlets_tab()
        self._build_profiles_tab()
        self._build_sensing_tab()
        self._build_behaviour_tab()
        self._build_about_tab()

        self.refresh()
        notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self._restyle()
        self._update_theme_hint()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        # Gentle refresh, to reflect changes made from the menu.
        self._schedule_refresh()

    # --------------------------------------------------------------- utilities

    def set_status(self, message: str) -> None:
        self.status.set(message)

    def _schedule_refresh(self) -> None:
        self.refresh_readings()
        # The layout may have been captured in the background: a profile
        # that turns everything on, a procedure started from the tray icon.
        ghosts = list(self.app.controller.ghost_screens)
        if self._capturing:
            pass  # the map is being built live: do not overwrite it
        elif self.config.screens != self._drawn_screens or ghosts != self._drawn_ghosts:
            self._draw_screen_map()
        else:
            self._update_layout_state()
        self._follow_system_theme()
        # The power log advances on its own on the device: without a periodic
        # callback, the UI would keep announcing "no measurement" while
        # samples pile up. It is spaced out more than the rest: it is a
        # network call, and the logger writes at most once per minute.
        self._sensing_ticks += 1
        if self._sensing_ticks >= SENSING_REFRESH_TICKS:
            self._sensing_ticks = 0
            self.refresh_sensing()
        self.root.after(3000, self._schedule_refresh)

    def _on_tab_changed(self, _event=None) -> None:
        """Rebuild the lists when arriving on a tab.

        The periodic callback only refreshes the values that move -- state,
        power, address. Everything else, outlet roles and kinds, devices
        added by a reconnection, a profile applied from the menu, only
        showed up after an explicit action in the window. Yet switching tabs
        is precisely the moment one comes to look: that is where up-to-date
        lists are expected.
        """
        # `refresh` covers everything, including the detection tab: no need
        # to add anything here, it would only query the device twice for
        # the same display.
        self.refresh()

    def _follow_system_theme(self) -> None:
        """In `system` mode, follow a Windows light/dark switch."""
        if self.config.settings.theme != "system":
            return
        now_dark = theme_module.system_prefers_dark()
        if now_dark != self._system_was_dark:
            self._system_was_dark = now_dark
            self.apply_theme()
            self._update_theme_hint()

    def apply_theme(self) -> None:
        """Apply the current theme to the window and its widgets."""
        self.palette = theme_module.apply(self.root, self.config.settings.theme)
        self._restyle()

    def _restyle(self) -> None:
        """Recolour what ttk.Style does not cover."""
        palette = self.palette
        self.screen_map.draw()
        theme_module.refresh_plain_widgets(self.root, palette)
        theme_module.apply_card_styles(self.root)
        self.profile_list.configure(
            background=palette.surface,
            foreground=palette.text,
            selectbackground=palette.accent,
            selectforeground=palette.accent_text,
            highlightthickness=1,
            highlightbackground=palette.border,
            borderwidth=0,
        )

    def _on_close(self) -> None:
        self._save()
        self.root.destroy()

    def _save(self) -> None:
        try:
            self.config.save()
        except OSError as exc:
            messagebox.showerror("Shelly Screens", f"Cannot save configuration:\n{exc}")

    # ------------------------------------------------------------- devices tab

    def _build_devices_tab(self) -> None:
        frame = self.devices_tab
        ttk.Label(
            frame,
            text=(
                t("Shelly devices driving the outlets. Two power strips give eight "
                "outlets; a single plug can be added later for the PC itself. "
                "The short key is what profiles refer to, so keep it readable. "
                "Click an IP address to open that device's web interface.")
            ),
            wraplength=760,
            justify="left",
        ).pack(anchor="w", pady=(0, 10))

        # Host and address are two separate columns: the device is often
        # reached by its mDNS name, more stable than its DHCP lease, but the
        # address is what one wants to read to open its web interface or
        # notice that it has changed.
        columns = ("kind", "host", "ip", "signal", "outlets", "auth", "state")
        self.device_tree = ttk.Treeview(frame, columns=columns, height=7)
        self.device_tree.heading("#0", text=t("Key / name"))
        self.device_tree.heading("kind", text=t("Model"))
        self.device_tree.heading("host", text=t("Reached via"))
        self.device_tree.heading("ip", text=t("IP address"))
        self.device_tree.heading("signal", text=t("Signal"))
        self.device_tree.heading("outlets", text=t("Outlets"))
        self.device_tree.heading("auth", text=t("Password"))
        self.device_tree.heading("state", text=t("Status"))
        self.device_tree.column("#0", width=160)
        self.device_tree.column("kind", width=115)
        self.device_tree.column("host", width=200)
        self.device_tree.column("ip", width=110)
        self.device_tree.column("signal", width=118)
        self.device_tree.column("outlets", width=58, anchor="center")
        # "Mot de passe" did not fit in the 78 pixels sized for "Password";
        # the room is taken from the mDNS name column, which had too much.
        self.device_tree.column("auth", width=100, anchor="center")
        self.device_tree.column("state", width=90)
        # The address cell behaves like a link. ttk.Treeview cannot style
        # a single cell -- no way to underline it without repainting the
        # whole row -- so we rely on the universal signal: the hand cursor
        # on hover.
        self.device_tree.bind("<Motion>", self._device_tree_hover)
        self.device_tree.bind("<Leave>", lambda _e: self.device_tree.configure(cursor=""))
        self.device_tree.bind("<Button-1>", self._device_tree_click, add="+")
        self.device_tree.pack(fill="both", expand=True)

        # Alert banner: hidden while all is well, it appears as soon as a
        # device rejects the password and leads to the recovery steps.
        self.auth_banner = ttk.Frame(frame)
        self.auth_alert = tk.StringVar(self.root, value="")
        ttk.Label(
            self.auth_banner,
            textvariable=self.auth_alert,
            wraplength=640,
            justify="left",
        ).pack(side="left", fill="x", expand=True)
        ttk.Button(
            self.auth_banner, text=t("Recovery steps"), command=self._show_reset_help
        ).pack(side="right")

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=12)
        ttk.Button(buttons, text=t("Add device..."), command=self._add_device).pack(side="left")
        for caption, command in (
            ("Name and key...", self._name_device),
            ("Password...", self._device_password),
            ("Services...", self._device_services),
            ("LEDs...", self._device_leds),
            ("Open web UI", self._open_web_ui),
            ("Reconnect", self._reconnect),
            ("Remove", self._remove_device),
        ):
            ttk.Button(buttons, text=t(caption), command=command).pack(
                side="left", padx=(8, 0)
            )

    def _signal_label(self, key: str) -> str:
        """Signal strength, along with what it is worth.

        A negative number in decibels only speaks to those used to it. The
        qualifier, on the other hand, reads at a glance -- and reading is
        what matters: a power strip at -79 dBm was on the verge of dropping
        out with nothing to announce it.
        """
        rssi = self.app.controller.wifi_signal(key)
        if rssi is None:
            return "-"
        return f"{rssi} dBm - {wifi_setup.signal_quality(rssi)}"

    def _auth_label(self, device) -> str:
        """Password state as reported by the device.

        The column used to show what the application had stored, which lies
        as soon as the device changes on its side: a factory reset removes
        its password without ours disappearing, and we believed we were
        protecting a wide-open device. So we show what it announces, and
        flag the mismatch rather than hide it.
        """
        identity = self.app.controller.identity(device.key)
        if identity is None:
            # Offline: we know nothing about it, only what we keep.
            return t("stored") if device.has_password else t("none")
        if identity.auth_enabled:
            return t("set") if device.has_password else t("unknown")
        return t("none, stored") if device.has_password else t("none")

    def _device_state_label(self, key: str, online: set[str]) -> str:
        """Readable state of a device, with authentication failure on its own."""
        if key in self.app.controller.auth_failures:
            return t("auth failed")
        return t("online") if key in online else t("offline")

    def _selected_device_key(self) -> str | None:
        selection = self.device_tree.selection()
        return selection[0] if selection else None

    def _ip_column_id(self) -> str:
        """Tk identifier of the address column (#1 is the first)."""
        return f"#{list(self.device_tree['columns']).index('ip') + 1}"

    def _ip_link_at(self, x: int, y: int) -> tuple[str, str] | None:
        """Key and address if the point targets a usable address cell."""
        if self.device_tree.identify_region(x, y) != "cell":
            return None
        if self.device_tree.identify_column(x) != self._ip_column_id():
            return None
        row = self.device_tree.identify_row(y)
        if not row:
            return None
        address = self.device_tree.set(row, "ip")
        if not address or address == "-":
            return None
        return row, address

    def _device_tree_hover(self, event) -> None:
        link = self._ip_link_at(event.x, event.y)
        self.device_tree.configure(cursor="hand2" if link else "")

    def _device_tree_click(self, event) -> None:
        link = self._ip_link_at(event.x, event.y)
        if link is None:
            return
        _key, address = link
        webbrowser.open(f"http://{address}/")
        self.set_status(f"Opening http://{address}/")

    def _add_device(self) -> None:
        AddDeviceDialog(self.root, self)

    def _name_device(self) -> None:
        key = self._selected_device_key()
        device = self.config.device(key) if key else None
        if device is None:
            self.set_status("Select a device first")
            return
        DeviceNamingDialog(self.root, self, device)

    def _device_services(self) -> None:
        key = self._selected_device_key()
        device = self.config.device(key) if key else None
        if device is None:
            self.set_status("Select a device first")
            return
        DeviceServicesDialog(self.root, self, device)

    def _device_leds(self) -> None:
        key = self._selected_device_key()
        device = self.config.device(key) if key else None
        if device is None:
            self.set_status("Select a device first")
            return
        DeviceLedsDialog(self.root, self, device)

    def _remove_device(self) -> None:
        key = self._selected_device_key()
        device = self.config.device(key) if key else None
        if device is None:
            return
        outlets = len(self.config.outlets_of(device.key))
        if not messagebox.askyesno(
            "Shelly Screens",
            f"Remove '{device.label}' and its {outlets} outlet(s)?\n\n"
            "Profiles referring to them will be updated.",
        ):
            return
        self.app.controller.forget(device.key)
        self.refresh()
        self.set_status(f"Device '{key}' removed")

    def _show_reset_help(self) -> None:
        PasswordDialog.show_reset_help(self.root)

    def _update_auth_banner(self) -> None:
        """Show or hide the authentication alert."""
        failures = self.app.controller.auth_failures
        if not failures:
            self.auth_banner.pack_forget()
            return
        names = ", ".join(sorted(failures))
        self.auth_alert.set(
            f"{names}: the device answers but refuses the stored password. "
            "Use « Password... » to enter the right one, or reset the device "
            "with its buttons if it is lost."
        )
        self.auth_banner.pack(fill="x", pady=(8, 0))

    def _device_password(self) -> None:
        key = self._selected_device_key()
        device = self.config.device(key) if key else None
        if device is None:
            self.set_status("Select a device first")
            return
        PasswordDialog(self.root, self, device)

    def _open_web_ui(self) -> None:
        """Open the selected device's web interface in the browser."""
        key = self._selected_device_key()
        device = self.config.device(key) if key else None
        if device is None:
            self.set_status("Select a device first")
            return
        target = device.ip or device.host
        if not target:
            self.set_status("No known address for this device")
            return
        webbrowser.open(f"http://{target}/")
        self.set_status(f"Opening http://{target}/")

    def _reconnect(self) -> None:
        self.set_status("Searching for devices...")
        self.app.reconnect()

    # ------------------------------------------------------------- outlets tab

    def _build_outlets_tab(self) -> None:
        frame = self.outlets_tab
        ttk.Label(
            frame,
            text=(
                t("Name each outlet and give it a role. Run the wizard once the "
                "screens are plugged in: it switches each outlet off in turn "
                "and watches which display Windows drops.")
            ),
            wraplength=760,
            justify="left",
        ).pack(anchor="w", pady=(0, 10))

        # An empty column separates the power from the screen. The number is
        # right-aligned, the label left-aligned: with nothing between them,
        # "105 W" and "not identified" touch and read as a single value.
        # ttk.Treeview cannot pad a cell, hence this column.
        columns = ("kind", "state", "power", "gap", "display", "flags")
        self.outlet_tree = ttk.Treeview(frame, columns=columns, height=9)
        self.outlet_tree.heading("#0", text=t("Outlet"))
        self.outlet_tree.heading("kind", text=t("Type"))
        self.outlet_tree.heading("state", text=t("State"))
        self.outlet_tree.heading("power", text=t("Power"))
        self.outlet_tree.heading("gap", text="")
        self.outlet_tree.heading("display", text=t("Display"))
        self.outlet_tree.heading("flags", text=t("Role"))
        self.outlet_tree.column("#0", width=200)
        self.outlet_tree.column("kind", width=85)
        self.outlet_tree.column("state", width=55, anchor="center")
        self.outlet_tree.column("power", width=65, anchor="e")
        self.outlet_tree.column("gap", width=18, minwidth=18, stretch=False)
        self.outlet_tree.column("display", width=235)
        self.outlet_tree.column("flags", width=125)
        self.outlet_tree.pack(fill="both", expand=True)
        self.outlet_tree.bind("<<TreeviewSelect>>", lambda _e: self._on_outlet_selected())

        editor = ttk.LabelFrame(frame, text=t("Selected outlet"), padding=10)
        editor.pack(fill="x", pady=10)

        ttk.Label(editor, text=t("Name")).grid(row=0, column=0, sticky="w")
        self.outlet_name = tk.StringVar(self.root)
        name_entry = ttk.Entry(editor, textvariable=self.outlet_name, width=26)
        name_entry.grid(row=0, column=1, sticky="w", padx=(8, 24))
        name_entry.bind("<FocusOut>", lambda _e: self._apply_outlet_edits())
        name_entry.bind("<Return>", lambda _e: self._apply_outlet_edits())

        ttk.Label(editor, text=t("Type")).grid(row=0, column=2, sticky="e", padx=(0, 8))
        self.outlet_kind = tk.StringVar(self.root)
        kind_box = ttk.Combobox(
            editor,
            textvariable=self.outlet_kind,
            values=[t(KIND_LABELS[k]) for k in KINDS],
            state="readonly",
            width=13,
        )
        kind_box.grid(row=0, column=3, sticky="w")
        kind_box.bind("<<ComboboxSelected>>", lambda _e: self._apply_outlet_edits())

        self.outlet_critical = tk.BooleanVar(self.root)
        ttk.Checkbutton(
            editor,
            text=t("Critical - never switched off"),
            variable=self.outlet_critical,
            command=self._apply_outlet_edits,
        ).grid(row=1, column=1, columnspan=3, sticky="w", pady=(8, 0))

        self.outlet_boot = tk.BooleanVar(self.root)
        ttk.Checkbutton(
            editor,
            text=t("Boot screen - fallback if the stored profile is unusable"),
            variable=self.outlet_boot,
            command=self._apply_outlet_edits,
        ).grid(row=2, column=1, columnspan=3, sticky="w", pady=(4, 0))

        self.outlet_host_pc = tk.BooleanVar(self.root)
        ttk.Checkbutton(
            editor,
            text=t("Powers the PC itself - never switched off"),
            variable=self.outlet_host_pc,
            command=self._apply_outlet_edits,
        ).grid(row=3, column=1, columnspan=3, sticky="w", pady=(4, 0))

        self.outlet_cut_on_sleep = tk.BooleanVar(self.root)
        ttk.Checkbutton(
            editor,
            text=t("Follows the PC - switched off while it sleeps"),
            variable=self.outlet_cut_on_sleep,
            command=self._apply_outlet_edits,
        ).grid(row=4, column=1, columnspan=3, sticky="w", pady=(4, 0))

        ttk.Label(
            editor,
            text=t("Screens follow the PC by default. Accessories do not: "
                   "tick this for a USB hub or speakers you want cut along "
                   "with the screens, and leave it clear for whatever must "
                   "stay powered through the night."),
            wraplength=560,
            justify="left",
            style="Hint.TLabel",
        ).grid(row=5, column=1, columnspan=3, sticky="w", pady=(2, 0))

        ttk.Label(
            editor,
            text=(
                t("Every display outlet must be set to « Screen »: the wizard "
                "only touches what has been declared, and leaves anything "
                "still « Not set » alone. "
                "Accessories - USB hubs, speakers - stay switchable by "
                "profiles but are left out of the display wizard: cutting "
                "them makes no screen disappear. A USB hub carrying your "
                "keyboard should also be marked critical: without it you "
                "could not enter the BIOS or type your PIN at the next boot.")
            ),
            wraplength=740,
            justify="left",
            style="Hint.TLabel",
        ).grid(row=6, column=0, columnspan=4, sticky="w", pady=(10, 0))

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        ttk.Button(buttons, text=t("Toggle outlet"), command=self._toggle_selected).pack(
            side="left"
        )
        ttk.Button(
            buttons, text=t("Identify displays..."), command=self._run_identify_wizard
        ).pack(side="left", padx=8)
        ttk.Button(buttons, text=t("Clear display link"), command=self._clear_display).pack(
            side="left"
        )

    def _selected_ref(self) -> str | None:
        selection = self.outlet_tree.selection()
        if not selection:
            return None
        # Device nodes are not outlets: they have no ":".
        return selection[0] if ":" in selection[0] else None

    def _on_outlet_selected(self) -> None:
        outlet = self.config.outlet(self._selected_ref() or "")
        if outlet is None:
            return
        self.outlet_name.set(outlet.name)
        self.outlet_kind.set(t(KIND_LABELS.get(outlet.kind, KIND_LABELS[""])))
        self.outlet_critical.set(outlet.critical)
        self.outlet_boot.set(outlet.boot_screen)
        self.outlet_host_pc.set(outlet.host_pc)
        self.outlet_cut_on_sleep.set(outlet.cut_on_sleep)

    def _apply_outlet_edits(self) -> None:
        outlet = self.config.outlet(self._selected_ref() or "")
        if outlet is None:
            return
        outlet.name = self.outlet_name.get().strip()
        previous_kind = outlet.kind
        chosen = self.outlet_kind.get()
        for value, label in KIND_LABELS.items():
            if t(label) == chosen:
                outlet.kind = value
                break
        outlet.critical = self.outlet_critical.get()
        outlet.cut_on_sleep = self.outlet_cut_on_sleep.get()
        # Declaring a screen means wanting it to follow sleep: that is the
        # whole point of the setup. Yet the checkbox kept the state inherited
        # from what the outlet carried before -- an accessory, so unticked --
        # and the screen stayed out of control with nothing to say so.
        became_screen = (
            outlet.kind == KIND_SCREEN and previous_kind != KIND_SCREEN
        )
        if became_screen and not outlet.cut_on_sleep:
            outlet.cut_on_sleep = True
            self.outlet_cut_on_sleep.set(True)
        # Only one boot screen and only one PC outlet, otherwise these roles
        # lose their meaning.
        if self.outlet_boot.get():
            for other in self.config.outlets:
                other.boot_screen = other.ref == outlet.ref
        else:
            outlet.boot_screen = False
        if self.outlet_host_pc.get():
            for other in self.config.outlets:
                other.host_pc = other.ref == outlet.ref
        else:
            outlet.host_pc = False
        self._save()

        # Clients already open keep their old list of protected outputs: it
        # must be handed to them again, otherwise the PC outlet would remain
        # switchable until the next reconnection.
        self.app.controller.refresh_protection()
        # And the on-device guard follows the role, from one device to another.
        _run_off_thread(
            self.root,
            lambda: sensing.sync_guard(self.app.controller, self.config),
            lambda result, error: self.set_status(
                f"Guard: {error}" if error else f"Guard: {result}"
            ),
        )
        self.refresh()

    def _toggle_selected(self) -> None:
        ref = self._selected_ref()
        if ref is None:
            self.set_status("Select an outlet first")
            return
        self.app.toggle_outlet(ref)
        self.set_status(f"Toggling {ref}...")

    def _clear_display(self) -> None:
        outlet = self.config.outlet(self._selected_ref() or "")
        if outlet is None:
            return
        outlet.monitor_key = ""
        self._save()
        self.refresh()

    # --------------------------------------------------------------- about tab

    def _build_about_tab(self) -> None:
        """Logo, name, version, author, validated devices, releases page.

        No frames here: a link placed on a card would keep the window's
        background. Plain section titles are enough.
        """
        frame = self.about_tab
        header = ttk.Frame(frame)
        header.pack(fill="x")
        self._about_logo = icon_module.load_photo(128, self.root)
        if self._about_logo is not None:
            ttk.Label(header, image=self._about_logo).pack(side="left", padx=(0, 20))
        identity = ttk.Frame(header)
        identity.pack(side="left", anchor="center")
        ttk.Label(identity, text="Shelly Screens", style="Banner.TLabel").pack(anchor="w")
        ttk.Label(identity, text=t("Version {version}", version=__version__)).pack(
            anchor="w", pady=(4, 0)
        )
        ttk.Label(identity, text=t("Author: {author}", author=product.AUTHOR)).pack(
            anchor="w"
        )

        ttk.Label(frame, text=t("Validated devices"), style="Section.TLabel").pack(
            anchor="w", pady=(28, 4)
        )
        for device in product.VALIDATED_DEVICES:
            row = ttk.Frame(frame)
            row.pack(anchor="w", padx=(12, 0))
            self._link(row, device.name, device.search_url).pack(side="left")
            ttk.Label(
                row, style="Hint.TLabel",
                text="   " + t("model {model}, firmware {firmware}",
                               model=device.model, firmware=device.firmware),
            ).pack(side="left")
        ttk.Label(
            frame, style="Hint.TLabel", wraplength=720, justify="left",
            text=t("Other Shelly devices with switchable outputs may work, but have "
                   "not been tested."),
        ).pack(anchor="w", padx=(12, 0), pady=(4, 0))

        ttk.Label(frame, text=t("Releases"), style="Section.TLabel").pack(
            anchor="w", pady=(24, 4)
        )
        row = ttk.Frame(frame)
        row.pack(anchor="w", padx=(12, 0))
        self._link(row, product.RELEASES_URL, product.RELEASES_URL).pack(side="left")
        ttk.Label(row, text="   " + t("(coming soon)"), style="Hint.TLabel").pack(
            side="left"
        )

    def _link(self, parent: tk.Misc, text: str, url: str) -> ttk.Label:
        """A label that opens an address in the browser."""
        label = ttk.Label(parent, text=text, style="Link.TLabel", cursor="hand2")
        label.bind("<Button-1>", lambda _e: webbrowser.open(url))
        return label

    # ------------------------------------------------------------ profiles tab

    def _build_profiles_tab(self) -> None:
        frame = self.profiles_tab
        left = ttk.Frame(frame)
        left.pack(side="left", fill="y", padx=(0, 12))

        ttk.Label(left, text=t("Profiles")).pack(anchor="w")
        self.profile_list = tk.Listbox(left, width=22, height=16, exportselection=False)
        # The list takes the full width of its column: filled vertically
        # only, it floated in the middle of a column widened by the buttons,
        # offset from its title.
        self.profile_list.pack(fill="both", expand=True)
        self.profile_list.bind("<<ListboxSelect>>", lambda _e: self._on_profile_selected())
        # Dragging a profile moves it in the list; the order is that of the
        # tray icon menu and of the keys in the hotkey window.
        self.profile_list.bind("<B1-Motion>", self._drag_profile)

        list_buttons = ttk.Frame(left)
        list_buttons.pack(fill="x", pady=6)
        # No fixed width: these buttons had been sized for "New", "Rename",
        # "Delete", and "Supprimer" ended up clipped to "Suppri". Each label
        # takes the room it needs, in every language.
        ttk.Button(list_buttons, text=t("New"), command=self._new_profile).pack(
            side="left"
        )
        rename = ttk.Button(list_buttons, text=t("Rename"), command=self._rename_profile)
        rename.pack(side="left", padx=3)
        delete = ttk.Button(list_buttons, text=t("Delete"), command=self._delete_profile)
        delete.pack(side="left")
        # Disabled when the built-in profile is selected.
        self._profile_edit_buttons = [rename, delete]
        self._profile_rows: list[str] = []

        move_buttons = ttk.Frame(left)
        move_buttons.pack(fill="x")
        self._move_up = ttk.Button(
            move_buttons, text=t("▲ Move up"), command=lambda: self._move_profile(-1)
        )
        self._move_up.pack(side="left")
        self._move_down = ttk.Button(
            move_buttons, text=t("▼ Move down"), command=lambda: self._move_profile(1)
        )
        self._move_down.pack(side="left", padx=3)

        right = ttk.Frame(frame)
        right.pack(side="left", fill="both", expand=True)

        # The screen map takes the bottom of the area, across its full width:
        # that is where it has room to be readable. It is reserved before the
        # top, otherwise it disappears when the window lacks height.
        map_box = ttk.LabelFrame(right, text=t("Screens"), padding=8)
        map_box.pack(side="bottom", fill="both", expand=True, pady=(4, 0))
        self.screen_map = screen_map.ScreenMap(
            map_box,
            palette=lambda: self.palette,
            on_toggle=self._toggle_from_map,
            empty_text=t("Screen positions are not known yet. Capture the layout: "
                         "every screen is switched on for a few seconds."),
        )
        # The button is reserved before the expanding map, otherwise it
        # disappears when the window lacks height.
        map_buttons = ttk.Frame(map_box)
        map_buttons.pack(side="bottom", fill="x", pady=(6, 0))
        ttk.Button(
            map_buttons, text=t("Capture layout..."), command=self._capture_layout
        ).pack(side="left")
        # Date of the capture, or reason for the last refusal: this tells
        # whether the map is up to date, and if not, why.
        self.layout_state = tk.StringVar(self.root, value="")
        ttk.Label(
            map_buttons, textvariable=self.layout_state, style="Hint.TLabel",
            wraplength=560, justify="left",
        ).pack(side="left", padx=(12, 0))
        self.screen_map.canvas.pack(fill="both", expand=True)
        self._drawn_screens: list = []
        self._capturing = False
        self._capture_step = ""
        self._drawn_ghosts: list[str] = []

        top = ttk.Frame(right)
        top.pack(side="top", fill="x")

        # The logo takes the space left free on the right. Without it, the
        # checkboxes stretched across the whole window width: a checkbox as
        # wide as a screen is harder to aim at than one snug against its
        # label, and the eye travels a needless distance between the caption
        # and the next checkbox.
        badge = ttk.Frame(top)
        badge.pack(side="right", fill="y", padx=(24, 0))
        self._profile_logo = icon_module.load_photo(96, self.root)
        if self._profile_logo is not None:
            ttk.Label(badge, image=self._profile_logo).pack(anchor="ne", pady=(6, 0))

        content = ttk.Frame(top)
        content.pack(side="left", fill="both")

        self.profile_title = tk.StringVar(self.root, value="No profile selected")
        ttk.Label(content, textvariable=self.profile_title, style="Title.TLabel").pack(
            anchor="w"
        )

        # The checkboxes are rebuilt on every configuration change: adding a
        # power strip adds outlets, and therefore checkboxes.
        self.outlets_box = ttk.LabelFrame(content, text=t("Powered outlets"), padding=10)
        self.outlets_box.pack(fill="x", pady=10)
        self.profile_outlet_vars: dict[str, tk.BooleanVar] = {}

        apply_row = ttk.Frame(content)
        apply_row.pack(fill="x", pady=(4, 8))
        ttk.Button(
            apply_row, text=t("Apply this profile now"), command=self._apply_profile_now
        ).pack(side="left")

    def _rebuild_profile_outlets(self) -> None:
        """Recreate the checkboxes, one per known outlet."""
        for child in self.outlets_box.winfo_children():
            child.destroy()
        self.profile_outlet_vars = {}
        self._profile_boxes: list[tuple[ttk.Checkbutton, str]] = []

        if not self.config.outlets:
            ttk.Label(self.outlets_box, text=t("No outlet yet - add a device first.")).pack(
                anchor="w"
            )
            return

        multi_device = len(self.config.devices) > 1
        current_device = None
        for outlet in self.config.outlets:
            if multi_device and outlet.device != current_device:
                current_device = outlet.device
                device = self.config.device(current_device)
                ttk.Label(
                    self.outlets_box,
                    text=device.label if device else current_device,
                    style="Section.TLabel",
                ).pack(anchor="w", pady=(6, 2))
            variable = tk.BooleanVar(self.root)
            self.profile_outlet_vars[outlet.ref] = variable
            suffix = ""
            if outlet.never_switch_off:
                suffix = "  " + t("(always on)")
            box = ttk.Checkbutton(
                self.outlets_box,
                text=f"{outlet.label}{suffix}",
                variable=variable,
                command=self._apply_profile_edits,
                state="disabled" if outlet.never_switch_off else "normal",
            )
            box.pack(anchor="w", padx=(12 if multi_device else 0, 0))
            self._profile_boxes.append((box, outlet.ref))

    def _selected_profile(self) -> Profile | None:
        # The list shows translated names: the profile is found by its row
        # index, not by the row's text.
        selection = self.profile_list.curselection()
        if not selection or selection[0] >= len(self._profile_rows):
            return None
        return self.config.profile(self._profile_rows[selection[0]])

    def _on_profile_selected(self) -> None:
        profile = self._selected_profile()
        if profile is None:
            return
        self.profile_title.set(
            f"{profile.label}  -  {t('built-in, every outlet on')}"
            if profile.builtin else profile.label
        )
        for ref, variable in self.profile_outlet_vars.items():
            outlet = self.config.outlet(ref)
            always_on = outlet is not None and outlet.never_switch_off
            variable.set(always_on or ref in profile.outlets_on)
        # The built-in profile cannot be edited: its checkboxes are disabled.
        for box, ref in self._profile_boxes:
            outlet = self.config.outlet(ref)
            locked = profile.builtin or (outlet is not None and outlet.never_switch_off)
            box.configure(state="disabled" if locked else "normal")
        for button in self._profile_edit_buttons:
            button.configure(state="disabled" if profile.builtin else "normal")
        self._update_move_buttons(profile)
        self._draw_screen_map()

    # ------------------------------------------------------------ order

    def _update_move_buttons(self, profile: Profile) -> None:
        """The built-in profile stays on top: neither it nor the others move past it."""
        ordered = self.config.user_profiles()
        position = next(
            (i for i, p in enumerate(ordered) if p.name == profile.name), None
        )
        first = profile.builtin or position == 0
        last = profile.builtin or position == len(ordered) - 1
        self._move_up.configure(state="disabled" if first else "normal")
        self._move_down.configure(state="disabled" if last else "normal")

    def _move_profile(self, step: int) -> None:
        profile = self._selected_profile()
        if profile is None or profile.builtin:
            return
        ordered = self.config.user_profiles()
        position = next(i for i, p in enumerate(ordered) if p.name == profile.name)
        self._place_profile(profile.name, position + step)

    def _place_profile(self, name: str, position: int) -> None:
        """Put a user profile at this rank and renumber the others."""
        ordered = self.config.user_profiles()
        moving = next((p for p in ordered if p.name == name), None)
        position = min(max(position, 0), len(ordered) - 1)
        if moving is None or ordered.index(moving) == position:
            return
        ordered.remove(moving)
        ordered.insert(position, moving)
        for rank, profile in enumerate(ordered):
            profile.order = rank
        self._save()
        self.refresh()
        self._select_profile(name)
        self.set_status(t("Profile order saved"))

    def _drag_profile(self, event) -> None:
        """While dragging: the profile follows the mouse, row by row."""
        profile = self._selected_profile()
        if profile is None or profile.builtin:
            return
        row = self.profile_list.nearest(event.y)
        # User profiles start at row 1, below the built-in profile.
        target = max(row, 1) - 1
        ordered = self.config.user_profiles()
        current = next(i for i, p in enumerate(ordered) if p.name == profile.name)
        if target != current:
            self._place_profile(profile.name, target)

    def _draw_screen_map(self) -> None:
        """Redraw the map from the checkboxes of the displayed profile."""
        selected = self._selected_profile()

        def lit(ref: str) -> bool | None:
            if selected is None:
                return None  # no profile displayed: nothing to say about outlets
            variable = self.profile_outlet_vars.get(ref)
            return variable is not None and variable.get()

        # A click on a screen edits the profile: not the built-in one.
        tiles = screen_map.build_tiles(
            self.config, lit,
            editable=selected is not None and not selected.builtin,
            ghosts=self.app.controller.ghost_screens,
        )
        self._drawn_screens = list(self.config.screens)
        self._drawn_ghosts = list(self.app.controller.ghost_screens)
        self.screen_map.show(tiles)
        self._update_layout_state()

    def _update_layout_state(self) -> None:
        """Capture date, failure of the last requested capture, ghost screens.

        Refusals from the continuous capture are not listed: that it cannot
        capture anything while screens are off is the rule, not a failure --
        the map simply keeps the last valid layout.
        """
        controller = self.app.controller
        lines = []
        failed = controller.capture_problems
        if failed and controller.capture_attempted_at > self.config.screens_captured_at:
            line = t("Not captured: {reason}", reason=failed[0])
            if len(failed) > 1:
                line += " " + t("(+{count} more)", count=len(failed) - 1)
            lines.append(line)
        elif self.config.screens_captured_at:
            lines.append(t("Captured {when} - {count} screen(s)",
                           when=time.strftime("%d/%m %H:%M",
                                              time.localtime(self.config.screens_captured_at)),
                           count=len(self.config.screens)))
        if controller.ghost_screens:
            lines.append(t("Ghost screen: {screens} switched off but kept on the "
                           "Windows desktop", screens=", ".join(controller.ghost_screens)))
        self.layout_state.set("\n".join(lines))

    def _capture_layout(self) -> None:
        """Establish the layout, showing it being built.

        The map is cleared, then each screen appears on it as Windows
        detects it. At the end, we say what was learned and ask whether to
        stay this way -- everything on -- or go back to the previous
        profile: going back automatically made it look as if nothing had
        happened.
        """
        linked = [o for o in self.config.outlets if o.monitor_key]
        if not linked:
            messagebox.showinfo(
                t("Capture screen layout"),
                t("Link each screen to its outlet first: Outlets tab, "
                  "Identify displays."),
                parent=self.root,
            )
            return
        dark = [
            o.label for o in linked
            if not (self.app.states.get(o.ref) and self.app.states[o.ref].output)
        ]
        text = t("The '{all_on}' profile will be applied and Windows will report "
                 "where each screen sits. You will see the layout build up, then "
                 "choose to keep '{all_on}' or go back to the previous profile.",
                 all_on=self.config.all_on_profile().label)
        if dark:
            text += "\n\n" + t("Will be switched on: {screens}", screens=", ".join(dark))
        if not messagebox.askyesno(t("Capture screen layout"), text, parent=self.root):
            return

        self._capturing = True
        self._capture_step = t("Switching the screens on...")
        self._draw_live_capture()

        def progress(step: str) -> None:
            self._capture_step = step

        def done(capture) -> None:
            try:
                self.root.after(0, lambda: self._capture_finished(capture))
            except (tk.TclError, RuntimeError):
                pass  # window closed in the meantime

        self.app.capture_screen_layout(on_done=done, progress=progress)

    def _draw_live_capture(self) -> None:
        """The map as Windows sees it right now, redrawn in a loop."""
        if not self._capturing:
            return
        names = {o.monitor_key: o.label for o in self.config.outlets if o.monitor_key}
        outputs = monitors.list_outputs() or {}
        tiles = []
        for m in monitors.physical_monitors(outputs):
            diagonal = monitors.physical_diagonal(m.key)
            output = outputs.get(m.key)
            tiles.append(screen_map.ScreenTile(
                rect=m.rect,
                title=names.get(m.key) or (output.edid_name if output else m.friendly_name),
                detail=screen_map.describe(
                    screen_map.STATE_ON, m.width, m.height, m.is_primary, m.scale, diagonal
                ),
                state=screen_map.STATE_ON,
                scale=m.scale, primary=m.is_primary, diagonal=diagonal,
            ))
        self.screen_map.show(tiles, empty_text=self._capture_step)
        self.layout_state.set(t("{step} {count} of {total} screen(s) detected",
                                step=self._capture_step, count=len(tiles),
                                total=len({o.monitor_key for o in self.config.outlets
                                           if o.monitor_key})))
        self.root.after(500, self._draw_live_capture)

    def _capture_finished(self, capture) -> None:
        """Say what was learned, then offer to stay or go back."""
        self._capturing = False
        self._draw_screen_map()
        self.set_status(capture.message)
        if not capture.turned_on:
            # Nothing was switched on for the occasion: there is nothing to choose.
            messagebox.showinfo(t("Capture screen layout"), capture.message,
                                parent=self.root)
            return
        all_on = self.config.all_on_profile()
        previous = self.config.profile(self.config.settings.last_profile)
        back = (t("Back to '{profile}'", profile=previous.label)
                if previous is not None else t("Back to previous state"))
        question = t("Keep '{all_on}', or go back?", all_on=all_on.label)
        if self._ask_stay_or_back(capture.message + "\n\n" + question,
                                  t("Keep '{all_on}'", all_on=all_on.label), back):
            self._when_idle(lambda: self.app.return_after_capture(capture))
        else:
            # Everything is on: "All on" is the current profile. Select it,
            # and the map shows what is on.
            self._when_idle(self.app.stay_after_capture)
            self._select_profile(all_on.name)

    def _ask_stay_or_back(self, message: str, stay_label: str, back_label: str) -> bool:
        """Small two-choice box; true to go back.

        Closing the box leaves things as they are: switching nothing is the
        safest answer to a question that was not settled.
        """
        window = tk.Toplevel(self.root)
        window.title(t("Capture screen layout"))
        window.transient(self.root)
        window.resizable(False, False)
        _theme_dialog(window, self.palette)
        choice = {"back": False}
        ttk.Label(window, text=message, wraplength=420, justify="left",
                  padding=16).pack(anchor="w")
        buttons = ttk.Frame(window, padding=(16, 0, 16, 14))
        buttons.pack(fill="x")

        def pick(back: bool) -> None:
            choice["back"] = back
            window.destroy()

        back_button = ttk.Button(buttons, text=back_label, command=lambda: pick(True))
        back_button.pack(side="right")
        ttk.Button(buttons, text=stay_label, command=lambda: pick(False)).pack(
            side="right", padx=(0, 8)
        )
        back_button.focus_set()
        window.grab_set()
        self.root.wait_window(window)
        return choice["back"]

    def _when_idle(self, action) -> None:
        """Run the action as soon as the application has no operation in progress."""
        if self.app.busy:
            self.root.after(300, lambda: self._when_idle(action))
        else:
            action()

    def _toggle_from_map(self, ref: str) -> None:
        """A click on a screen is worth a click on its outlet's checkbox."""
        variable = self.profile_outlet_vars.get(ref)
        if variable is None:
            return
        variable.set(not variable.get())
        self._apply_profile_edits()

    def _apply_profile_edits(self) -> None:
        profile = self._selected_profile()
        if profile is None or profile.builtin:
            return
        profile.outlets_on = [
            ref
            for ref, var in self.profile_outlet_vars.items()
            if var.get() and not (self.config.outlet(ref) or OutletConfig("", 0)).never_switch_off
        ]
        self._save()
        self._draw_screen_map()
        self.set_status(f"Profile '{profile.name}' updated")

    def _new_profile(self) -> None:
        name = simpledialog.askstring("New profile", "Profile name:", parent=self.root)
        if not name:
            return
        name = name.strip()
        if self.config.is_reserved_name(name):
            messagebox.showerror("Shelly Screens", t("'{name}' is the built-in profile.",
                                                      name=name))
            return
        if self.config.profile(name):
            messagebox.showerror("Shelly Screens", f"'{name}' already exists.")
            return
        order = max((p.order for p in self.config.profiles), default=-1) + 1
        self.config.profiles.append(Profile(name=name, outlets_on=[], order=order))
        self._save()
        self.refresh()
        self._select_profile(name)

    def _rename_profile(self) -> None:
        profile = self._selected_profile()
        if profile is None or profile.builtin:
            return
        name = simpledialog.askstring(
            "Rename profile", "New name:", initialvalue=profile.name, parent=self.root
        )
        if not name or name.strip() == profile.name:
            return
        name = name.strip()
        if self.config.is_reserved_name(name):
            messagebox.showerror("Shelly Screens", t("'{name}' is the built-in profile.",
                                                      name=name))
            return
        if self.config.profile(name):
            messagebox.showerror("Shelly Screens", f"'{name}' already exists.")
            return
        if self.config.settings.last_profile == profile.name:
            self.config.settings.last_profile = name
        profile.name = name
        self._save()
        self.refresh()
        self._select_profile(name)

    def _delete_profile(self) -> None:
        profile = self._selected_profile()
        if profile is None or profile.builtin:
            return
        if not messagebox.askyesno("Shelly Screens", f"Delete profile '{profile.name}'?"):
            return
        self.config.profiles.remove(profile)
        if self.config.settings.last_profile == profile.name:
            self.config.settings.last_profile = ""
        self._save()
        self.refresh()

    def _select_profile(self, name: str) -> None:
        for index, row in enumerate(self._profile_rows):
            if row == name:
                self.profile_list.selection_clear(0, "end")
                self.profile_list.selection_set(index)
                self._on_profile_selected()
                return

    def _apply_profile_now(self) -> None:
        profile = self._selected_profile()
        if profile is None:
            return
        self.app.apply_profile(profile.name)
        self.set_status(f"Applying '{profile.name}'...")

    # ------------------------------------------------- PC detection tab

    def _build_sensing_tab(self) -> None:
        frame = self.sensing_tab
        ttk.Label(
            frame,
            text=(
                t("With the PC plugged into a measured outlet, the power strip "
                "can switch the screens on by itself when it sees the PC draw "
                "current. That is what allows everything to be switched off at "
                "shutdown: no software runs on the PC during POST, but the "
                "strip keeps measuring.")
            ),
            wraplength=790,
            justify="left",
        ).pack(anchor="w", pady=(0, 8))

        self.sensing_state = tk.StringVar(self.root, value="")
        ttk.Label(frame, textvariable=self.sensing_state, wraplength=760,
                  justify="left").pack(anchor="w", pady=(0, 10))

        measure = ttk.LabelFrame(frame, text=t("Measurement"), padding=10)
        measure.pack(fill="x")
        ttk.Label(
            measure,
            text=(
                t("Start the measurement, then use the PC normally: let it idle, "
                "sleep it, shut it down, start it again. The strip records the "
                "levels on its own while the PC is off.")
            ),
            wraplength=730,
            justify="left",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(0, 8))
        self.probe_result = tk.StringVar(self.root, value=t("No measurement yet."))
        ttk.Label(measure, textvariable=self.probe_result, wraplength=730,
                  justify="left").pack(anchor="w", pady=(0, 8))
        probe_row = ttk.Frame(measure)
        probe_row.pack(fill="x")
        self.probe_button = ttk.Button(
            probe_row, text=t("Start measuring"), command=self._toggle_probe
        )
        self.probe_button.pack(side="left")
        ttk.Button(probe_row, text=t("Read now"), command=self._read_probe).pack(
            side="left", padx=8
        )
        self.suggest_button = ttk.Button(
            probe_row, text=t("Use suggested thresholds"), command=self._apply_suggestion
        )
        self.suggest_button.pack(side="left")
        ttk.Button(probe_row, text=t("Show curve..."), command=self._show_chart).pack(
            side="left", padx=8
        )

        limits = ttk.LabelFrame(frame, text=t("Thresholds and delays"), padding=10)
        limits.pack(fill="x", pady=10)
        self.var_on_w = tk.DoubleVar(self.root, value=self.config.sensing.on_threshold_w)
        self.var_off_w = tk.DoubleVar(self.root, value=self.config.sensing.off_threshold_w)
        self.var_on_s = tk.DoubleVar(self.root, value=self.config.sensing.on_delay_s)
        self.var_off_s = tk.DoubleVar(self.root, value=self.config.sensing.off_delay_s)
        rows = (
            (t("PC seen as running above"), self.var_on_w, "W", 0, 1000, 1),
            (t("PC seen as off below"), self.var_off_w, "W", 0, 1000, 1),
            (t("Confirm before switching on"), self.var_on_s, "s", 1, 60, 1),
            (t("Confirm before switching off"), self.var_off_s, "s", 5, 600, 5),
        )
        for index, (label, variable, unit, low, high, step) in enumerate(rows):
            ttk.Label(limits, text=label).grid(row=index, column=0, sticky="w", pady=2)
            spin = ttk.Spinbox(
                limits, from_=low, to=high, increment=step, textvariable=variable,
                width=8, command=self._apply_sensing_edits,
            )
            spin.grid(row=index, column=1, padx=8)
            # A Spinbox's `command` only responds to the arrows. A value typed
            # on the keyboard was therefore never committed: one believed a
            # threshold had changed, and nothing had moved.
            spin.bind("<FocusOut>", lambda _e: self._apply_sensing_edits())
            spin.bind("<Return>", lambda _e: self._apply_sensing_edits())
            ttk.Label(limits, text=unit).grid(row=index, column=2, sticky="w")

        ttk.Label(
            limits,
            text=(
                t("Two thresholds, not one: between them lies a dead band where "
                "the current state holds, so a fluctuating draw cannot make the "
                "relay chatter. The switch-off delay is deliberately long: "
                "during a Windows restart the PC drops below the threshold for "
                "ten to fifteen seconds, and cutting the screens right then "
                "would be the worst moment.")
            ),
            wraplength=730,
            justify="left",
            style="Hint.TLabel",
        ).grid(row=len(rows), column=0, columnspan=3, sticky="w", pady=(10, 0))

        self.sensing_warning = tk.StringVar(self.root, value="")
        ttk.Label(limits, textvariable=self.sensing_warning, wraplength=730,
                  justify="left").grid(row=len(rows) + 1, column=0, columnspan=3,
                                       sticky="w", pady=(6, 0))

        script_box = ttk.LabelFrame(frame, text=t("On-device script"), padding=10)
        script_box.pack(fill="x")
        self.script_state = tk.StringVar(self.root, value="")
        ttk.Label(script_box, textvariable=self.script_state, wraplength=730,
                  justify="left").pack(anchor="w", pady=(0, 8))
        script_row = ttk.Frame(script_box)
        script_row.pack(fill="x")
        ttk.Button(script_row, text=t("Install / update"), command=self._install_script).pack(
            side="left"
        )
        ttk.Button(script_row, text=t("Remove"), command=self._remove_script).pack(
            side="left", padx=8
        )

        history_box = ttk.LabelFrame(frame, text=t("Consumption history"), padding=10)
        history_box.pack(fill="x", pady=(10, 0))
        history_row = ttk.Frame(history_box)
        history_row.pack(fill="x")
        ttk.Label(history_row, text=t("Keep history for")).pack(side="left")
        self.var_history_days = tk.IntVar(self.root, value=self.config.settings.history_days)
        days = ttk.Spinbox(
            history_row, from_=1, to=365, increment=1, width=6,
            textvariable=self.var_history_days, command=self._apply_history_days,
        )
        days.pack(side="left", padx=8)
        # As with the thresholds: a value typed on the keyboard must be
        # committed when leaving the field, the arrows are not enough.
        days.bind("<FocusOut>", lambda _e: self._apply_history_days())
        days.bind("<Return>", lambda _e: self._apply_history_days())
        ttk.Label(history_row, text=t("days")).pack(side="left")
        ttk.Button(
            history_row, text=t("Open history..."), command=self._open_history
        ).pack(side="right")

    def _apply_history_days(self) -> None:
        try:
            days = int(self.var_history_days.get())
        except (tk.TclError, ValueError):
            return  # input in progress
        days = min(max(days, 1), 365)
        if days != self.config.settings.history_days:
            self.config.settings.history_days = days
            self._save()

    def _open_history(self) -> None:
        from .history_window import open_history

        open_history(self.app)

    def refresh_sensing(self) -> None:
        """Update the detection tab.

        The local part is immediate; the state of the script and of the power
        logger requires querying the device, so it runs in the background.
        """
        outlet = self.config.host_pc_outlet()
        if outlet is None:
            self.sensing_state.set(
                t(
                    "No outlet is marked as powering the PC. Set that role in "
                    "the Outlets tab first - nothing here can work without it."
                )
            )
        else:
            self.config.sensing.pc_ref = outlet.ref
            driven = ", ".join(o.label for o in sensing.controlled_outlets(self.config))
            boot = self.config.boot_screen_outlet()
            self.sensing_state.set(
                t(
                    "Watching {pc} ({ref}).  Boot screen: {boot}.  "
                    "Outlets driven by the script: {driven}.",
                    pc=outlet.label,
                    ref=outlet.ref,
                    boot=boot.label if boot else t("none set"),
                    driven=driven or t("none"),
                )
            )
        self.sensing_warning.set(self.config.sensing.thresholds_are_sane())

        if outlet is None:
            self.script_state.set(t("Unavailable until the PC outlet is set."))
            return

        def done(result, error):
            if error is not None:
                self.script_state.set(f"Cannot reach the device: {error}")
                return
            status, probing, levels, fresh = result
            # The KVS only holds indexes: map them back to names, the only
            # meaningful way to check what will come back at boot.
            table = sensing.controlled_outlets(self.config)
            names = [
                table[i].label for i in status[1] if 0 <= i < len(table)
            ]
            stored = ", ".join(names) if names else t("boot screen only")
            self.script_state.set(
                t(
                    "Script: {state}.  Restored at boot: {outlets}.",
                    state=t(status[0].summary()),
                    outlets=stored,
                )
            )
            if not fresh:
                self.script_state.set(
                    self.script_state.get() + "  "
                    + t("The device still runs the previous settings: use "
                        "« Install / update » to apply them.")
                )
            self.probe_button.configure(
                text=t("Stop measuring") if probing else t("Start measuring")
            )
            self._show_levels(levels, probing)

        def work():
            status = sensing.status(self.app.controller, self.config)
            stored = sensing.read_published_profile(self.app.controller, self.config)
            probing = sensing.probe_running(self.app.controller, self.config)
            levels = sensing.read_probe(self.app.controller, self.config)
            fresh = sensing.installed_matches(self.app.controller, self.config)
            return ((status, stored), probing, levels, fresh)

        _run_off_thread(self.root, work, done)

    def _apply_sensing_edits(self) -> None:
        sensing_config = self.config.sensing
        try:
            sensing_config.on_threshold_w = max(0.0, float(self.var_on_w.get()))
            sensing_config.off_threshold_w = max(0.0, float(self.var_off_w.get()))
            sensing_config.on_delay_s = max(1.0, float(self.var_on_s.get()))
            sensing_config.off_delay_s = max(5.0, float(self.var_off_s.get()))
        except (tk.TclError, ValueError):
            return  # input in progress
        self._save()
        self.refresh_sensing()

    def _toggle_probe(self) -> None:
        if not self._require_pc_outlet():
            return
        running = sensing.probe_running(self.app.controller, self.config)
        self.set_status("Stopping measurement..." if running else "Starting measurement...")

        def work():
            if running:
                sensing.stop_probe(self.app.controller, self.config)
                return "stopped"
            sensing.start_probe(self.app.controller, self.config)
            return "started"

        def done(result, error):
            if error is not None:
                messagebox.showerror("Shelly Screens", str(error))
            else:
                self.set_status(f"Measurement {result}")
            self.refresh_sensing()

        _run_off_thread(self.root, work, done)

    def _read_probe(self) -> None:
        if not self._require_pc_outlet():
            return

        def done(levels, error):
            if error is not None:
                self.probe_result.set(f"Cannot read the measurement: {error}")
                return
            self._show_levels(levels)

        _run_off_thread(
            self.root,
            lambda: sensing.read_probe(self.app.controller, self.config),
            done,
        )

    def _show_levels(self, levels, probing: bool = False) -> None:
        """Show the state of the power log, whether running or finished.

        The state is stated explicitly: a power log advances on its own on
        the device, and nothing would signal it if the UI merely showed the
        figures at the moment they are requested.
        """
        if not levels.samples:
            self.probe_result.set(
                t("Measurement running - no sample recorded yet.")
                if probing
                else t("No measurement yet.")
            )
            self._suggestion = None
            return
        minutes = levels.duration_s / 60.0
        on_w, off_w, warning = sensing.suggest_thresholds(levels)
        text = t(
            "{state} - {count} samples over {minutes} min, from {low} W to {high} W.",
            state=t("Measurement running") if probing else t("Measurement stopped"),
            count=levels.samples,
            minutes=f"{minutes:.0f}",
            low=f"{levels.lowest:.1f}",
            high=f"{levels.highest:.1f}",
        )
        if on_w:
            self._suggestion = (on_w, off_w)
            text += "\n" + t(
                "Off or asleep up to {standby} W, running from {active} W. "
                "Suggested: on above {on} W, off below {off} W.",
                standby=f"{levels.standby_ceiling():.0f}",
                active=f"{levels.active_floor():.0f}",
                on=f"{on_w:.0f}",
                off=f"{off_w:.0f}",
            )
        else:
            self._suggestion = None
        if warning:
            text += "\n" + t(warning)
        self.probe_result.set(text)

    def _show_chart(self) -> None:
        """Open the curve, where the thresholds are placed with the mouse."""
        if not self._require_pc_outlet():
            return
        from .power_chart import PowerChartDialog

        PowerChartDialog(self.root, self)

    def _apply_suggestion(self) -> None:
        suggestion = getattr(self, "_suggestion", None)
        if not suggestion:
            self.set_status("Read a measurement first")
            return
        on_w, off_w = suggestion
        self.var_on_w.set(on_w)
        self.var_off_w.set(off_w)
        self._apply_sensing_edits()
        self.set_status(f"Thresholds set to {on_w:.0f} / {off_w:.0f} W")

    def _require_pc_outlet(self) -> bool:
        outlet = self.config.host_pc_outlet()
        if outlet is None:
            messagebox.showinfo(
                "Shelly Screens",
                "First mark the outlet that powers the PC, in the Outlets tab.",
            )
            return False
        self.config.sensing.pc_ref = outlet.ref
        return True

    def _install_script(self) -> None:
        if not self._require_pc_outlet():
            return
        problem = self.config.sensing.thresholds_are_sane()
        if problem and not messagebox.askyesno(
            "Shelly Screens", f"{problem}\n\nInstall anyway?"
        ):
            return
        self.set_status("Installing the on-device script...")

        def done(status, error):
            if error is not None:
                messagebox.showerror("Shelly Screens", str(error))
            else:
                self.set_status(t("Script {state}", state=t(status.summary())))
                self._save()
            self.refresh_sensing()

        _run_off_thread(
            self.root,
            lambda: sensing.install(self.app.controller, self.config),
            done,
        )

    def _remove_script(self) -> None:
        def done(_result, error):
            if error is not None:
                messagebox.showerror("Shelly Screens", str(error))
            else:
                self.set_status("Script removed")
                self._save()
            self.refresh_sensing()

        _run_off_thread(
            self.root,
            lambda: sensing.uninstall(self.app.controller, self.config),
            done,
        )

    # ------------------------------------------------ behaviour tab

    def _build_behaviour_tab(self) -> None:
        frame = self.behaviour_tab
        settings = self.config.settings

        power_box = ttk.LabelFrame(frame, text=t("Sleep and shutdown"), padding=10)
        power_box.pack(fill="x")

        self.var_off_on_suspend = tk.BooleanVar(self.root, value=settings.power_off_on_suspend)
        ttk.Checkbutton(
            power_box,
            text=t("Switch outlets off when the PC sleeps or shuts down"),
            variable=self.var_off_on_suspend,
            command=self._apply_behaviour,
        ).pack(anchor="w")

        self.var_restore_on_resume = tk.BooleanVar(self.root, value=settings.restore_on_resume)
        ttk.Checkbutton(
            power_box,
            text=t("Re-apply the last profile on wake-up"),
            variable=self.var_restore_on_resume,
            command=self._apply_behaviour,
        ).pack(anchor="w")

        self.var_apply_on_start = tk.BooleanVar(self.root, value=settings.apply_profile_on_start)
        ttk.Checkbutton(
            power_box,
            text=t("Re-apply the last profile when this application starts"),
            variable=self.var_apply_on_start,
            command=self._apply_behaviour,
        ).pack(anchor="w")

        self.shutdown_summary = tk.StringVar(self.root, value="")
        ttk.Label(
            power_box,
            textvariable=self.shutdown_summary,
            wraplength=740,
            justify="left",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(8, 0))

        windows_box = ttk.LabelFrame(frame, text=t("Windows"), padding=10)
        windows_box.pack(fill="x", pady=12)
        self.var_rescue = tk.BooleanVar(self.root, value=settings.rescue_offscreen_windows)
        ttk.Checkbutton(
            windows_box,
            text=t("After a profile change, bring windows left outside every "
                   "lit screen back onto the nearest one"),
            variable=self.var_rescue,
            command=self._apply_behaviour,
        ).pack(anchor="w")

        self._build_hotkey_box(frame)

        appearance_box = ttk.LabelFrame(frame, text=t("Appearance"), padding=10)
        appearance_box.pack(fill="x", pady=(0, 12))
        self.var_theme = tk.StringVar(self.root, value=self.config.settings.theme)
        row = ttk.Frame(appearance_box)
        row.pack(anchor="w")
        for value, label in (
            ("system", "Follow Windows"),
            ("light", "Light"),
            ("dark", "Dark"),
        ):
            ttk.Radiobutton(
                row,
                text=t(label),
                value=value,
                variable=self.var_theme,
                command=self._change_theme,
            ).pack(side="left", padx=(0, 18))
        ttk.Label(appearance_box, text=t("Language")).pack(
            anchor="w", pady=(10, 2)
        )
        language_row = ttk.Frame(appearance_box)
        language_row.pack(anchor="w")
        self.var_language = tk.StringVar(self.root, value=self.config.settings.language)
        for value in i18n.LANGUAGES:
            ttk.Radiobutton(
                language_row,
                text=t(i18n.LANGUAGE_LABELS[value]),
                value=value,
                variable=self.var_language,
                command=self._change_language,
            ).pack(side="left", padx=(0, 18))

        self.theme_hint = tk.StringVar(self.root, value="")
        ttk.Label(
            appearance_box,
            textvariable=self.theme_hint,
            style="Hint.TLabel",
            wraplength=740,
            justify="left",
        ).pack(anchor="w", pady=(8, 0))

        timing_box = ttk.LabelFrame(frame, text=t("Timing"), padding=10)
        timing_box.pack(fill="x")
        ttk.Label(timing_box, text=t("Delay between outlet commands (ms)")).grid(
            row=0, column=0, sticky="w"
        )
        self.var_switch_delay = tk.IntVar(self.root, value=settings.switch_delay_ms)
        ttk.Spinbox(
            timing_box,
            from_=0,
            to=2000,
            increment=50,
            textvariable=self.var_switch_delay,
            width=8,
            command=self._apply_behaviour,
        ).grid(row=0, column=1, padx=8)

        ttk.Label(timing_box, text=t("Max wait for displays to appear (s)")).grid(
            row=1, column=0, sticky="w", pady=(6, 0)
        )
        self.var_settle = tk.DoubleVar(self.root, value=settings.display_settle_timeout_s)
        ttk.Spinbox(
            timing_box,
            from_=1,
            to=60,
            increment=1,
            textvariable=self.var_settle,
            width=8,
            command=self._apply_behaviour,
        ).grid(row=1, column=1, padx=8, pady=(6, 0))

    def _build_hotkey_box(self, frame) -> None:
        """Global hotkey that shows the profile buttons.

        Checkboxes and a list rather than keyboard entry: Tk does not see
        the Windows key as a modifier, and a combination that cannot be
        typed cannot be captured.
        """
        box = ttk.LabelFrame(frame, text=t("Profile shortcut"), padding=10)
        box.pack(fill="x", pady=(0, 12))
        current = hotkey_module.parse(self.config.settings.profile_hotkey)
        shown = current or hotkey_module.parse("Ctrl+Win+Alt+P")
        row = ttk.Frame(box)
        row.pack(anchor="w", fill="x")
        self.hotkey_flags: dict[int, tk.BooleanVar] = {}
        for name, flag in hotkey_module.MODIFIERS:
            variable = tk.BooleanVar(self.root, value=bool(shown.modifiers & flag))
            self.hotkey_flags[flag] = variable
            ttk.Checkbutton(
                row, text=name, variable=variable, command=self._check_hotkey
            ).pack(side="left", padx=(0, 10))
        ttk.Label(row, text="+").pack(side="left", padx=(0, 10))
        self.hotkey_key = tk.StringVar(self.root, value=shown.key)
        key_box = ttk.Combobox(
            row, textvariable=self.hotkey_key, values=list(hotkey_module.KEYS),
            state="readonly", width=5,
        )
        key_box.pack(side="left")
        key_box.bind("<<ComboboxSelected>>", lambda _e: self._check_hotkey())
        ttk.Button(row, text=t("Disable"), command=self._disable_hotkey).pack(
            side="right"
        )
        self.hotkey_apply = ttk.Button(row, text=t("Apply"), command=self._apply_hotkey)
        self.hotkey_apply.pack(side="right", padx=(0, 8))
        self.hotkey_status = tk.StringVar(self.root, value="")
        ttk.Label(
            box, textvariable=self.hotkey_status, style="Hint.TLabel",
            wraplength=740, justify="left",
        ).pack(anchor="w", pady=(8, 0))
        self._check_hotkey()

    def _chosen_hotkey(self) -> "hotkey_module.Hotkey":
        modifiers = 0
        for flag, variable in self.hotkey_flags.items():
            if variable.get():
                modifiers |= flag
        return hotkey_module.Hotkey(modifiers, self.hotkey_key.get())

    def _check_hotkey(self) -> None:
        """Tell, on every change, whether the combination can be used."""
        wanted = self._chosen_hotkey()
        active = self.app.tray.hotkey
        ready = False
        if not wanted.usable:
            message = t("Add Ctrl, Alt or Win: a shortcut without them would "
                        "take the key away from every other program.")
        elif wanted == active:
            message = t("Active: {hotkey} shows the profile buttons in the "
                        "middle of the main screen.", hotkey=str(wanted))
        elif hotkey_module.is_free(wanted):
            message = t("{hotkey} is available. Click Apply to use it.",
                        hotkey=str(wanted))
            ready = True
        else:
            message = t("{hotkey} is already used by another program or by "
                        "Windows.", hotkey=str(wanted))
        if active is None and wanted != active:
            message += "  " + t("No shortcut is active at the moment.")
        self.hotkey_status.set(message)
        self.hotkey_apply.state(["!disabled"] if ready else ["disabled"])

    def _apply_hotkey(self) -> None:
        wanted = self._chosen_hotkey()
        if not self.app.tray.set_hotkey(wanted):
            self._check_hotkey()
            self.hotkey_status.set(
                t("Windows refused {hotkey}: another program took it.",
                  hotkey=str(wanted))
            )
            return
        self.config.settings.profile_hotkey = str(wanted)
        self._save()
        self.app.log(f"Profile shortcut set to {wanted}")
        self._check_hotkey()

    def _disable_hotkey(self) -> None:
        self.app.tray.set_hotkey(None)
        self.config.settings.profile_hotkey = ""
        self._save()
        self.app.log("Profile shortcut disabled")
        self._check_hotkey()

    def _change_theme(self) -> None:
        """Switch theme, immediately and without reopening the window."""
        self.config.settings.theme = self.var_theme.get()
        self._save()
        self.apply_theme()
        self._update_theme_hint()
        self.set_status(f"Theme set to {self.config.settings.theme}")

    def _change_language(self) -> None:
        """Change the language and rebuild the window.

        Tk widgets read their text at construction time: translating them
        afterwards would require keeping a registry of each one. Reopening
        the window is simpler, and guarantees that no label stays in the
        old language -- a half-translated screen being worse than no
        translation at all.
        """
        chosen = self.var_language.get()
        if chosen == self.config.settings.language:
            return
        self.config.settings.language = chosen
        self._save()
        i18n.set_language(chosen)
        application = self.app
        self.root.destroy()
        # Let the window's thread finish before reopening.
        threading.Timer(0.4, lambda: open_settings(application)).start()

    def _update_theme_hint(self) -> None:
        palette = self.palette
        if self.config.settings.theme == "system":
            following = "dark" if palette.dark else "light"
            self.theme_hint.set(
                t(
                    "Windows is currently in {mode} mode, and this window "
                    "follows it. Accent colour {accent} comes from your "
                    "Windows settings.",
                    mode=t(following),
                    accent=palette.accent,
                )
            )
        else:
            self.theme_hint.set(
                t(
                    "Fixed {theme} theme. Accent colour {accent} comes from "
                    "your Windows settings.",
                    theme=t(palette.name),
                    accent=palette.accent,
                )
            )

    def _apply_behaviour(self) -> None:
        settings = self.config.settings
        settings.power_off_on_suspend = self.var_off_on_suspend.get()
        settings.restore_on_resume = self.var_restore_on_resume.get()
        settings.apply_profile_on_start = self.var_apply_on_start.get()
        settings.rescue_offscreen_windows = self.var_rescue.get()
        try:
            settings.switch_delay_ms = max(0, int(self.var_switch_delay.get()))
            settings.display_settle_timeout_s = max(1.0, float(self.var_settle.get()))
        except (tk.TclError, ValueError):
            pass  # input in progress, keep the previous value
        self._save()
        self.set_status("Settings saved")

    # ------------------------------------------------------------ refresh

    def refresh(self) -> None:
        """Rebuild the lists from the configuration."""
        online = self.app.controller.online_keys

        # --- devices
        selected_device = self._selected_device_key()
        self.device_tree.delete(*self.device_tree.get_children())
        for device in self.config.devices:
            identity = self.app.controller.identity(device.key)
            self.device_tree.insert(
                "",
                "end",
                iid=device.key,
                text=f"{device.key}" + (f"  -  {device.name}" if device.name else ""),
                values=(
                    identity.model if identity else device.kind,
                    device.host or "unknown",
                    device.ip or "-",
                    self._signal_label(device.key),
                    len(self.config.outlets_of(device.key)),
                    self._auth_label(device),
                    self._device_state_label(device.key, online),
                ),
            )
        if selected_device and self.device_tree.exists(selected_device):
            self.device_tree.selection_set(selected_device)
        self._update_auth_banner()

        # --- outlets, grouped by device as soon as there are several
        selected_ref = self._selected_ref()
        self.outlet_tree.delete(*self.outlet_tree.get_children())
        monitors_by_key = {m.key: m for m in monitors.list_monitors()}
        multi_device = len(self.config.devices) > 1
        for device in self.config.devices:
            outlets = self.config.outlets_of(device.key)
            if not outlets:
                continue
            parent = ""
            if multi_device:
                parent = f"dev-{device.key}"
                self.outlet_tree.insert(
                    "", "end", iid=parent, text=device.label, open=True,
                    values=("", "", "", "", "", ""),
                )
            for outlet in outlets:
                state = self.app.states.get(outlet.ref)
                monitor = monitors_by_key.get(outlet.monitor_key)
                if monitor is not None:
                    display = monitor.describe()
                elif outlet.monitor_key:
                    # Stale association: show it rather than hide it, even
                    # if the outlet has changed type since.
                    display = t("{key} (not connected)", key=outlet.monitor_key)
                elif outlet.is_screen:
                    display = t("not identified")
                else:
                    # The PC outlet, a USB hub or an undeclared outlet carry
                    # no screen: announcing that none is identified would
                    # suggest a forgotten setting, when there is nothing to
                    # set.
                    display = ""
                roles = []
                if outlet.host_pc:
                    roles.append(t("PC"))
                if outlet.critical:
                    roles.append(t("critical"))
                if outlet.boot_screen:
                    roles.append(t("boot"))
                # Only accessories show it: for a screen, following sleep
                # goes without saying and displaying it would tell nothing.
                if outlet.cuts_on_sleep and not outlet.is_screen:
                    roles.append(t("sleeps"))
                self.outlet_tree.insert(
                    parent,
                    "end",
                    iid=outlet.ref,
                    text=f"{outlet.switch_id + 1}. {outlet.label}",
                    values=(
                        t(outlet.kind_label),
                        t("on") if state and state.output else (t("off") if state else "-"),
                        f"{state.apower:.0f} W" if state else "-",
                        "",
                        display,
                        ", ".join(roles),
                    ),
                )
        if selected_ref and self.outlet_tree.exists(selected_ref):
            self.outlet_tree.selection_set(selected_ref)

        # --- profiles
        selected_profile = self._selected_profile()
        self._rebuild_profile_outlets()
        self.profile_list.delete(0, "end")
        profiles = self.config.sorted_profiles()
        self._profile_rows = [profile.name for profile in profiles]
        for profile in profiles:
            self.profile_list.insert("end", profile.label)
        if selected_profile is not None:
            self._select_profile(selected_profile.name)
        else:
            self._draw_screen_map()

        # --- summary of what stays on at shutdown
        kept = [
            self.config.outlet(ref).label  # type: ignore[union-attr]
            for ref in self.config.shutdown_refs_on()
            if self.config.outlet(ref) is not None
        ]
        self.refresh_sensing()
        self.shutdown_summary.set(
            t("Stays powered through sleep and shutdown: {outlets}",
              outlets=", ".join(kept))
            if kept
            else t(
                "Nothing stays powered through shutdown yet. Mark the outlet "
                "of your main screen as boot screen, and any USB hub carrying "
                "your keyboard as critical."
            )
        )

    def refresh_readings(self) -> None:
        """Update only the values that move, without rebuilding the list."""
        for outlet in self.config.outlets:
            state = self.app.states.get(outlet.ref)
            if not self.outlet_tree.exists(outlet.ref):
                continue
            self.outlet_tree.set(
                outlet.ref, "state", t("on") if state and state.output else (t("off") if state else "-")
            )
            self.outlet_tree.set(
                outlet.ref, "power", f"{state.apower:.0f} W" if state else "-"
            )
        online = self.app.controller.online_keys
        for device in self.config.devices:
            if self.device_tree.exists(device.key):
                self.device_tree.set(
                    device.key, "state", self._device_state_label(device.key, online)
                )
                self.device_tree.set(
                    device.key, "auth", self._auth_label(device)
                )
                self.device_tree.set(device.key, "host", device.host or "unknown")
                self.device_tree.set(device.key, "ip", device.ip or "-")
                self.device_tree.set(
                    device.key, "signal", self._signal_label(device.key)
                )
        self._update_auth_banner()

    # ------------------------------------------------ identification wizard

    def _run_identify_wizard(self) -> None:
        """Link each outlet to its screen, by watching Windows.

        The principle: with every outlet on, switch one off, see which screen
        Windows drops, then switch it back on. The other screens thus always
        stay on -- the wizard never pulls the rug out from under itself.
        """
        if not self.app.online:
            messagebox.showerror("Shelly Screens", "No Shelly device is reachable.")
            return
        online = self.app.controller.online_keys
        states = self.app.states

        # Three successive filters, from the most explicit to the most
        # physical. The last one does not depend on any labelling: an outlet
        # that draws a lot is not a screen, and cutting it would most likely
        # shut down the PC. It therefore protects even if the "Powers the
        # PC" role was not assigned, or was lost.
        candidates: list[OutletConfig] = []
        refused: list[str] = []
        for outlet in self.config.outlets:
            if outlet.device not in online:
                continue
            if outlet.never_switch_off:
                role = "powers the PC" if outlet.host_pc else "critical"
                refused.append(f"{outlet.label} ({outlet.ref}) - {role}")
                continue
            if not outlet.is_screen:
                # An accessory will make no screen disappear, and an outlet
                # with no type set must not be touched: we do not cut what we
                # do not know it powers.
                why = (
                    "accessory"
                    if outlet.kind
                    else "type not set - declare it as Screen to include it"
                )
                refused.append(f"{outlet.label} ({outlet.ref}) - {why}")
                continue
            state = states.get(outlet.ref)
            draw = state.apower if state else 0.0
            if draw > IDENTIFY_MAX_WATTS:
                refused.append(
                    f"{outlet.label} ({outlet.ref}) - draws {draw:.0f} W, over "
                    f"the {IDENTIFY_MAX_WATTS:.0f} W limit"
                )
                continue
            candidates.append(outlet)

        if not candidates:
            pending = [o.label for o in self.config.unclassified_outlets()]
            detail = (
                "\n\nOutlets still without a type: " + ", ".join(pending)
                if pending
                else ""
            )
            messagebox.showinfo(
                "Shelly Screens",
                "No outlet is declared as carrying a screen.\n\n"
                "Set the Type column to « Screen » on each display outlet "
                "first: the wizard only touches what has been declared." + detail,
            )
            return

        lines = [
            f"{len(candidates)} outlet(s) will be switched off and back on in "
            f"turn, about {len(candidates) * 12} seconds in total.",
            "",
            "WILL BE SWITCHED OFF:",
        ]
        for outlet in candidates:
            state = states.get(outlet.ref)
            draw = f"{state.apower:.0f} W" if state else "unknown"
            lines.append(f"    {outlet.label} ({outlet.ref}) - {draw}")
        if refused:
            lines += ["", "Left alone:"]
            lines += [f"    {entry}" for entry in refused]
        lines += ["", "Your screens will flicker. Start now?"]

        if not messagebox.askyesno("Identify displays", "\n".join(lines)):
            return
        IdentifyDialog(self.root, self, candidates)


def _theme_dialog(window: tk.Toplevel, palette: "theme_module.Palette") -> None:
    """Match a dialog box to the main window's theme.

    ttk styles are shared by the whole process, but a Toplevel's background
    and its title bar belong to it alone.
    """
    window.configure(background=palette.bg)
    theme_module.apply_titlebar(window, palette.dark)


class IdentifyDialog:
    """Small progress window driving the identification sequence."""

    SETTLE_TIMEOUT_S = 12.0
    POLL_S = 0.4

    def __init__(
        self, parent: tk.Tk, owner: SettingsWindow, outlets: list[OutletConfig]
    ) -> None:
        self.owner = owner
        self.app = owner.app
        self.outlets = outlets
        self.cancelled = False
        self.results: dict[str, str] = {}

        self.window = tk.Toplevel(parent)
        self.window.title(t("Identifying displays"))
        self.window.geometry("480x190")
        self.window.transient(parent)
        self.window.grab_set()
        self.window.protocol("WM_DELETE_WINDOW", self._cancel)
        _theme_dialog(self.window, owner.palette)

        self.message = tk.StringVar(self.window, value="Preparing...")
        ttk.Label(self.window, textvariable=self.message, wraplength=440, padding=14).pack(
            anchor="w"
        )
        self.progress = ttk.Progressbar(
            self.window, mode="determinate", maximum=len(outlets) + 1
        )
        self.progress.pack(fill="x", padx=14)
        ttk.Button(self.window, text=t("Cancel"), command=self._cancel).pack(pady=14)

        threading.Thread(target=self._run, name="identify", daemon=True).start()

    def _cancel(self) -> None:
        self.cancelled = True
        self.message.set("Cancelling, restoring outlets...")

    def _say(self, text: str, step: int | None = None) -> None:
        # Tkinter only likes its own thread: go back through the loop.
        def update() -> None:
            self.message.set(text)
            if step is not None:
                self.progress["value"] = step

        try:
            self.window.after(0, update)
        except tk.TclError:
            pass  # window already closed

    def _why_not_cut(self, outlet) -> str:
        """Reason not to cut this outlet now, otherwise empty.

        Re-read at each step: the role may have changed since the start, and
        the power draw tells the truth whatever the labelling.
        """
        current = self.app.config.outlet(outlet.ref)
        if current is None:
            return "outlet no longer configured"
        if current.never_switch_off:
            return "powers the PC" if current.host_pc else "marked critical"
        if not current.is_screen:
            return (
                "an accessory" if current.kind else "has no type set"
            ) + ", outside the screen scope"
        state = self.app.states.get(outlet.ref)
        if state is not None and state.apower > IDENTIFY_MAX_WATTS:
            return f"draws {state.apower:.0f} W, over the {IDENTIFY_MAX_WATTS:.0f} W limit"
        return ""

    def _wait_for_change(self, before: set[str], appearing: bool) -> set[str]:
        """Wait for a screen to disappear (or appear) and return the difference."""
        deadline = time.monotonic() + self.SETTLE_TIMEOUT_S
        while time.monotonic() < deadline:
            time.sleep(self.POLL_S)
            now = monitors.monitor_keys()
            difference = (now - before) if appearing else (before - now)
            if difference:
                time.sleep(0.8)  # let the configuration settle
                return difference
        return set()

    def _run(self) -> None:
        controller = self.app.controller
        try:
            initial = {ref: s.output for ref, s in controller.read_outlets().items()}
        except Exception as exc:  # noqa: BLE001
            self._say(f"Cannot read the devices: {exc}")
            return

        try:
            # 1. Switch everything on, to start from a complete desktop.
            self._say("Switching every outlet on...", 0)
            for outlet in self.outlets:
                if not initial.get(outlet.ref):
                    controller.set_outlet(outlet.ref, True)
                    time.sleep(0.3)
            # Switching on carries no risk; switching off does.
            time.sleep(4.0)  # let the panels initialise

            # 2. Cut each outlet in turn and watch which one goes away.
            for index, outlet in enumerate(self.outlets, start=1):
                if self.cancelled:
                    break
                # The list was fixed at the click, but the sequence lasts a
                # minute or two: re-validate just before cutting, against the
                # current state and not a stale snapshot.
                blocked = self._why_not_cut(outlet)
                if blocked:
                    self._say(f"{outlet.label}: skipped, {blocked}", index)
                    time.sleep(1.0)
                    continue

                before = monitors.monitor_keys()
                self._say(f"Testing {outlet.label}...", index)
                controller.set_outlet(outlet.ref, False)
                lost = self._wait_for_change(before, appearing=False)

                if len(lost) == 1:
                    key = next(iter(lost))
                    self.results[outlet.ref] = key
                    self._say(f"{outlet.label} -> {key}", index)
                elif len(lost) > 1:
                    self._say(f"{outlet.label}: several displays dropped, skipped", index)
                else:
                    self._say(f"{outlet.label}: no display dropped", index)

                controller.set_outlet(outlet.ref, True)
                if lost:
                    self._wait_for_change(before - lost, appearing=True)
                else:
                    time.sleep(1.5)

            # Everything is back on: this is the time to capture the layout.
            if not self.cancelled:
                self._capture_layout(controller)

            # 3. Return to the initial state.
            self._say("Restoring outlets...", len(self.outlets) + 1)
            for outlet in self.outlets:
                controller.set_outlet(outlet.ref, initial.get(outlet.ref, True))
                time.sleep(0.3)
        except Exception as exc:  # noqa: BLE001
            self._say(f"Identification failed: {exc}")
            time.sleep(2.0)

        self._finish()

    def _capture_layout(self, controller) -> None:
        """Capture the layout with the associations just found.

        It is also the only moment when we can prove that a screen depends
        on no outlet: if every screen outlet found its own, the remaining
        ones stayed on through every cut. They are recorded as such. The
        proof only holds if no screen outlet was skipped.
        """
        links = {
            o.ref: o.monitor_key for o in self.app.config.outlets if o.monitor_key
        }
        links.update(self.results)
        screen_refs = [
            o.ref for o in self.app.config.outlets
            if o.kind == KIND_SCREEN and not o.host_pc
        ]
        self.unswitched: set[str] | None = None
        if all(ref in links for ref in screen_refs):
            outputs = monitors.list_outputs()
            physical = {m.key for m in monitors.physical_monitors(outputs)}
            self.unswitched = physical - set(links.values())
        self._say(t("Capturing the screen layout..."))
        ok, message = controller.capture_when_ready(links, self.unswitched)
        self.layout_message = message

    def _deduce_last_pair(self) -> str | None:
        """Pair up the last remaining couple, once there is no ambiguity left.

        The measurement sometimes fails on a screen: two identical panels
        may flicker together, or Windows may be slow to drop the one just
        switched off, and the "a single screen disappeared" rule then
        rejects a result that is actually right. But if only one outlet is
        left without a screen and only one screen without an outlet, the
        pair is the only possible one: rerunning the whole sequence to find
        it would be absurd. We deduce it, and say so.
        """
        claimed = set(self.results.values())
        for outlet in self.app.config.outlets:
            if outlet.monitor_key and outlet.ref not in self.results:
                claimed.add(outlet.monitor_key)
        free_keys = [k for k in monitors.monitor_keys() if k not in claimed]
        free_outlets = [
            o.ref for o in self.outlets
            if o.ref not in self.results and not o.monitor_key
        ]
        if len(free_keys) != 1 or len(free_outlets) != 1:
            return None
        self.results[free_outlets[0]] = free_keys[0]
        return free_outlets[0]

    def _finish(self) -> None:
        deduced = None if self.cancelled else self._deduce_last_pair()

        def apply_results() -> None:
            for ref, key in self.results.items():
                outlet = self.app.config.outlet(ref)
                if outlet is not None:
                    outlet.monitor_key = key
            unswitched = getattr(self, "unswitched", None)
            if unswitched is not None:
                self.app.config.unswitched_screens = sorted(unswitched)
            if self.results or unswitched is not None:
                self.owner._save()
            self.owner.refresh()
            found = len(self.results)
            self.owner.set_status(
                f"{found} of {len(self.outlets)} outlet(s) matched to a display"
                + (f" - {self.layout_message}" if getattr(self, "layout_message", "") else "")
            )
            try:
                self.window.grab_release()
                self.window.destroy()
            except tk.TclError:
                pass
            if deduced is not None:
                outlet = self.app.config.outlet(deduced)
                label = outlet.label if outlet is not None else deduced
                messagebox.showinfo(
                    "Identify displays",
                    f"'{label}' was not measured: it was the only outlet left "
                    "without a display, and one display was left without an "
                    "outlet, so the pair was deduced. Clear its display link "
                    "if that guess looks wrong.",
                )
            elif found < len(self.outlets) and not self.cancelled:
                messagebox.showinfo(
                    "Identify displays",
                    f"{found} of {len(self.outlets)} outlets were matched.\n\n"
                    "An unmatched outlet either has no screen plugged in (a USB "
                    "hub, the PC itself), or its screen did not disconnect "
                    "quickly enough.",
                )

        try:
            self.window.after(0, apply_results)
        except tk.TclError:
            pass


class AddDeviceDialog:
    """Search the network for Shelly devices and allow adopting one."""

    def __init__(self, parent: tk.Tk, owner: SettingsWindow) -> None:
        self.owner = owner
        self.app = owner.app
        self.found: list[discovery.DeviceIdentity] = []

        self.window = tk.Toplevel(parent)
        self.window.title(t("Add a Shelly device"))
        self.window.geometry("720x520")
        self.window.minsize(660, 460)
        self.window.transient(parent)
        self.window.grab_set()
        _theme_dialog(self.window, owner.palette)

        ttk.Label(
            self.window,
            text=(
                t("Devices already configured are greyed out. Scanning the whole "
                "local network takes about twenty seconds; entering the address "
                "directly is instant.")
            ),
            wraplength=600,
            padding=12,
            justify="left",
        ).pack(anchor="w")

        manual = ttk.Frame(self.window, padding=(12, 0))
        manual.pack(fill="x")
        ttk.Label(manual, text=t("Address or mDNS name")).pack(side="left")
        self.host = tk.StringVar(self.window)
        entry = ttk.Entry(manual, textvariable=self.host, width=32)
        entry.pack(side="left", padx=8)
        entry.bind("<Return>", lambda _e: self._probe_host())
        ttk.Button(manual, text=t("Check"), command=self._probe_host).pack(side="left")

        # The bottom is reserved before the expanding tree. Tk distributes
        # space in packing order: an `expand=True` tree packed first takes
        # everything left, and the buttons packed afterwards get clipped as
        # soon as the window lacks height. They had been invisible since
        # day one.
        self.message = tk.StringVar(self.window, value="")
        buttons = ttk.Frame(self.window, padding=12)
        buttons.pack(fill="x", side="bottom")
        self.scan_button = ttk.Button(buttons, text=t("Scan network"), command=self._scan)
        self.scan_button.pack(side="left")
        ttk.Button(buttons, text=t("Add selected"), command=self._adopt).pack(side="left", padx=8)
        ttk.Button(
            buttons, text=t("First setup of a new device..."), command=self._first_setup
        ).pack(side="left")
        ttk.Button(buttons, text=t("Close"), command=self._close).pack(side="right")
        ttk.Label(
            self.window, textvariable=self.message, padding=(12, 0)
        ).pack(anchor="w", side="bottom")

        columns = ("model", "app", "id")
        self.tree = ttk.Treeview(self.window, columns=columns, height=10)
        self.tree.heading("#0", text=t("Address"))
        self.tree.heading("model", text=t("Model"))
        self.tree.heading("app", text=t("Type"))
        self.tree.heading("id", text=t("Device ID"))
        self.tree.column("#0", width=210)
        self.tree.column("model", width=115)
        self.tree.column("app", width=85)
        self.tree.column("id", width=230)
        self.tree.pack(fill="both", expand=True, padx=12, pady=12)
        # The scan does not start on its own: twenty seconds sweeping the
        # network is for the user to decide -- the typed address is often
        # enough. Only the first setup starts it, when it finishes.
        self._expected_mac = ""
        self._expected_ip = ""

    def _close(self) -> None:
        try:
            self.window.grab_release()
            self.window.destroy()
        except tk.TclError:
            pass

    def _known_macs(self) -> set[str]:
        return {d.mac.upper() for d in self.app.config.devices if d.mac}

    def _show(self, identities: list[discovery.DeviceIdentity]) -> None:
        self.found = identities
        self.tree.delete(*self.tree.get_children())
        known = self._known_macs()
        for index, identity in enumerate(identities):
            already = identity.mac.upper() in known
            self.tree.insert(
                "",
                "end",
                iid=str(index),
                text=identity.host + ("  (already added)" if already else ""),
                values=(identity.model, identity.app, identity.device_id),
                tags=("known",) if already else (),
            )
        self.tree.tag_configure("known", foreground=self.owner.palette.text_disabled)

    def _scan(self) -> None:
        self.scan_button.state(["disabled"])
        self.message.set(t("Scanning the local network..."))

        def worker() -> None:
            identities = discovery.scan_network_all(every_shelly=True)

            def done() -> None:
                self._show(identities)
                self.message.set(f"{len(identities)} Shelly device(s) found")
                self.scan_button.state(["!disabled"])
                if self._expected_mac:
                    self._select_expected()

            try:
                self.window.after(0, done)
            except tk.TclError:
                pass

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------ first setup

    def _first_setup(self) -> None:
        from .first_setup import FirstSetupDialog

        self.window.grab_release()  # the assistant takes over
        FirstSetupDialog(self.window, self.owner.palette, _theme_dialog, self._found_new)

    def _found_new(self, mac: str, ip: str) -> None:
        """The device has joined the Wi-Fi: look for it, and present it."""
        try:
            self.window.grab_set()
        except tk.TclError:
            return
        self._expected_mac = mac.replace(":", "").upper()
        self._expected_ip = ip
        self._scan()

    def _select_expected(self) -> None:
        """Select the device that was just set up.

        If the scan did not find it -- the network sometimes takes a few
        seconds to learn about it -- query it at the address it announced.
        """
        mac = self._expected_mac
        for index, identity in enumerate(self.found):
            if identity.mac.replace(":", "").upper() == mac:
                self.tree.selection_set(str(index))
                self.tree.see(str(index))
                self.message.set(t("New device found: check it, then Add selected."))
                self._expected_mac = ""
                return
        if self._expected_ip:
            self.host.set(self._expected_ip)
            self._expected_ip = ""
            self._probe_host(select_expected=True)
            return
        self._expected_mac = ""

    def _probe_host(self, select_expected: bool = False) -> None:
        host = self.host.get().strip()
        if not host:
            return
        self.message.set(f"Contacting {host}...")

        def worker() -> None:
            identity = discovery.probe(host)

            def done() -> None:
                if identity is None:
                    self.message.set(f"No Shelly device answered at {host}")
                else:
                    self._show([identity])
                    self.message.set(f"Found {identity.model} at {identity.host}")
                    if select_expected:
                        self._select_expected()

            try:
                self.window.after(0, done)
            except tk.TclError:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _adopt(self) -> None:
        selection = self.tree.selection()
        if not selection:
            self.message.set("Select a device first")
            return
        identity = self.found[int(selection[0])]
        if identity.mac.upper() in self._known_macs():
            self.message.set("This device is already configured")
            return
        device = self.app.controller.adopt(identity)
        self.owner.refresh()
        self.owner.set_status(f"Device '{device.key}' added ({identity.model})")
        self._close()


def _run_off_thread(window: tk.Misc, work, done) -> None:
    """Run a network call off the UI thread.

    Tkinter only tolerates its own thread: the result therefore goes back
    through the event loop with `after`.
    """

    def worker() -> None:
        try:
            result = work()
            error = None
        except Exception as exc:  # noqa: BLE001 - passed as-is to the UI
            result, error = None, exc
        try:
            window.after(0, lambda: done(result, error))
        except (tk.TclError, RuntimeError):
            # Window closed during the call. Tkinter reports this in two ways
            # depending on timing: `TclError` when the widget is destroyed,
            # `RuntimeError: main thread is not in main loop` when the whole
            # interpreter is gone. Only the first was caught, and the second
            # dumped a traceback into the log -- the very one we reread to
            # understand an incident.
            pass

    threading.Thread(target=worker, daemon=True).start()


# Reset procedure, checked against Shelly's knowledge base. The difference
# between five and ten seconds matters: releasing too early only performs a
# network reset, which turns the Wi-Fi access point back on -- an open one
# on this model.
FACTORY_RESET_STEPS = (
    "If the password is lost, only the buttons can unlock the device.\n\n"
    "1. Unplug the power strip, then plug it back in.\n"
    "2. Within the first 60 seconds, press buttons 1 and 4 together.\n"
    "3. Hold them for a full 10 seconds, then release.\n\n"
    "Releasing at around 5 seconds performs a network reset instead, which "
    "turns the built-in Wi-Fi access point back on - and it is an open one "
    "on this model. Hold the full 10 seconds.\n\n"
    "A factory reset erases everything: password, Wi-Fi credentials, "
    "scripts and outlet names. The device then has to be set up again from "
    "its own access point."
)


class DeviceNamingDialog:
    """Readable name and short key of a device, edited together.

    The two used to have two separate buttons, whereas they are changed in
    one go when discovering a device. Bringing them together above all
    avoids renaming one while forgetting the other, and ending up with a
    list where the key no longer says what the label announces.
    """

    def __init__(self, parent: tk.Tk, owner: SettingsWindow, device) -> None:
        self.owner = owner
        self.config = owner.config
        self.device = device
        self.original_key = device.key

        self.window = tk.Toplevel(parent)
        self.window.title(t("Name and key - {device}", device=device.label))
        self.window.geometry("620x330")
        self.window.minsize(560, 300)
        self.window.transient(parent)
        self.window.grab_set()
        _theme_dialog(self.window, owner.palette)

        ttk.Label(
            self.window,
            text=t("The label is yours to choose and appears in this window "
                   "only. The key is the short identifier that outlet "
                   "references and profiles are built on: changing it "
                   "rewrites every reference pointing at this device."),
            wraplength=570,
            justify="left",
            padding=14,
        ).pack(anchor="w")

        form = ttk.Frame(self.window, padding=14)
        form.pack(fill="x")
        ttk.Label(form, text=t("Label")).grid(row=0, column=0, sticky="w", pady=4)
        self.label_var = tk.StringVar(self.window, value=device.name)
        entry = ttk.Entry(form, textvariable=self.label_var, width=34)
        entry.grid(row=0, column=1, padx=8, sticky="w")
        ttk.Label(form, text=t("Key")).grid(row=1, column=0, sticky="w", pady=4)
        self.key_var = tk.StringVar(self.window, value=device.key)
        ttk.Entry(form, textvariable=self.key_var, width=18).grid(
            row=1, column=1, padx=8, sticky="w"
        )
        ttk.Label(
            form,
            text=t("letters and digits only"),
            style="Hint.TLabel",
        ).grid(row=2, column=1, padx=8, sticky="w")

        self.message = tk.StringVar(self.window, value="")
        ttk.Label(self.window, textvariable=self.message, wraplength=570,
                  justify="left", padding=(14, 4)).pack(anchor="w")

        buttons = ttk.Frame(self.window, padding=14)
        buttons.pack(fill="x", side="bottom")
        ttk.Button(buttons, text=t("Save"), command=self._save).pack(side="left")
        ttk.Button(buttons, text=t("Cancel"), command=self._close).pack(side="right")
        entry.focus_set()

    def _save(self) -> None:
        key = "".join(c for c in self.key_var.get().lower() if c.isalnum())
        if not key:
            self.message.set(t("The key must contain letters or digits."))
            return
        if key != self.original_key:
            if not self.config.rename_device(self.original_key, key):
                self.message.set(t("The key '{key}' is already taken.", key=key))
                return
        self.device.name = self.label_var.get().strip()
        self.owner._save()
        self.owner.refresh()
        if key != self.original_key:
            self.owner.set_status(f"Key '{self.original_key}' renamed to '{key}'")
        self._close()

    def _close(self) -> None:
        try:
            self.window.destroy()
        except tk.TclError:
            pass


class DeviceServicesDialog:
    """Optional services of a device, and what they cost.

    A Shelly leaves the factory with everything on. None of it is needed to
    drive the outlets, but each service keeps its network stack and its
    share of memory -- enough for the power strip carrying the scripts to
    come close to running dry and get reset by its watchdog.

    The dialog says what each service is really for before saying why it
    is not needed here: switching off what one does not understand is a
    bad habit, and what is useless today may be plugged in tomorrow.
    """

    def __init__(self, parent: tk.Tk, owner: SettingsWindow, device) -> None:
        self.owner = owner
        self.app = owner.app
        self.device = device
        self.vars: dict[str, tk.BooleanVar] = {}
        self.boxes: dict[str, ttk.Checkbutton] = {}
        self.notes: dict[str, tk.StringVar] = {}

        self.window = tk.Toplevel(parent)
        self.window.title(t("Services - {device}", device=device.label))
        # Seven services of three lines each: the window needs 900 pixels
        # of height. Below that, the footer stays visible -- it is reserved
        # first -- but the list gets squeezed.
        self.window.geometry("800x900")
        self.window.minsize(700, 560)
        self.window.transient(parent)
        _theme_dialog(self.window, owner.palette)

        ttk.Label(
            self.window,
            text=t("None of these services is needed to switch outlets: this "
                   "app talks to the device over its local API. Each one "
                   "still keeps a network stack and its share of memory "
                   "alive, and they are all on by default."),
            wraplength=730,
            justify="left",
            padding=14,
        ).pack(anchor="w")

        self.memory = tk.StringVar(self.window, value="")
        ttk.Label(self.window, textvariable=self.memory, padding=(14, 0),
                  style="Hint.TLabel").pack(anchor="w")

        # Same precaution as elsewhere: the window footer is reserved before
        # the expanding list, otherwise it disappears on a small screen.
        footer = ttk.Frame(self.window, padding=14)
        footer.pack(fill="x", side="bottom")
        self.reboot_button = ttk.Button(
            footer, text=t("Restart the device"), command=self._reboot
        )
        self.reboot_button.pack(side="left")
        ttk.Button(footer, text=t("Refresh"), command=self.reload).pack(side="left", padx=8)
        ttk.Button(footer, text=t("Close"), command=self._close).pack(side="right")

        body = ttk.Frame(self.window, padding=(14, 10))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        for row, service in enumerate(device_services.SERVICES):
            block = ttk.Frame(body)
            block.grid(row=row, column=0, sticky="ew", pady=(0, 9))
            block.columnconfigure(0, weight=1)
            variable = tk.BooleanVar(self.window)
            self.vars[service.key] = variable
            box = ttk.Checkbutton(
                block,
                text=t(service.label),
                variable=variable,
                command=lambda k=service.key: self._toggle(k),
            )
            box.grid(row=0, column=0, sticky="w")
            self.boxes[service.key] = box
            for line, wording in enumerate((service.purpose, service.verdict), start=1):
                ttk.Label(
                    block, text=t(wording), wraplength=700,
                    justify="left", style="Hint.TLabel",
                ).grid(row=line, column=0, sticky="w", padx=(22, 0))
            # Some firmwares do not expose the setting. A greyed-out empty
            # checkbox would suggest a service that is off and locked: we
            # say instead that the device does not allow touching it.
            note = tk.StringVar(self.window, value="")
            self.notes[service.key] = note
            ttk.Label(
                block, textvariable=note, wraplength=700,
                justify="left", style="Hint.TLabel",
            ).grid(row=3, column=0, sticky="w", padx=(22, 0))

        # Two absences that raise questions, and deserve better than silence.
        ttk.Label(
            self.window,
            text=t("A greyed row means this firmware does not carry that "
                   "service at all, not that it is switched off. BTHome "
                   "sensors have no row of their own: they ride on Bluetooth "
                   "and stay inert while it is off."),
            wraplength=730,
            justify="left",
            style="Hint.TLabel",
            padding=(14, 4),
        ).pack(anchor="w")

        self.message = tk.StringVar(self.window, value="")
        ttk.Label(self.window, textvariable=self.message, wraplength=730,
                  justify="left", padding=(14, 6)).pack(anchor="w")

        self.reload()

    # ------------------------------------------------------------- reading

    def reload(self) -> None:
        self.message.set(t("Reading the device..."))

        def work():
            handle = self.app.controller.device_for(self.device.key)
            return (
                device_services.read_states(handle),
                device_services.restart_required(handle),
                device_services.memory(handle),
            )

        def done(result, error) -> None:
            if error is not None:
                self.message.set(t("Cannot reach the device: {error}", error=error))
                return
            states, pending, (free, low, total) = result
            for key, value in states.items():
                # A service missing from the configuration cannot be set on
                # this firmware: better to grey out the checkbox than offer a
                # switch that controls nothing.
                self.vars[key].set(bool(value))
                self.boxes[key].configure(
                    state="disabled" if value is None else "normal"
                )
                self.notes[key].set(
                    t("This firmware does not expose the setting; check the "
                      "device web page.") if value is None else ""
                )
            self.memory.set(
                t("Free memory: {free} of {total} bytes, lowest since start "
                  "{low}.",
                  free=_grouped(free), total=_grouped(total), low=_grouped(low))
            )
            self.reboot_button.configure(state="normal" if pending else "disabled")
            self.message.set(
                t("Some changes need a restart to take effect.") if pending else ""
            )

        _run_off_thread(self.window, work, done)

    # ------------------------------------------------------------- actions

    def _toggle(self, key: str) -> None:
        wanted = self.vars[key].get()
        self.message.set(t("Applying..."))

        def work():
            handle = self.app.controller.device_for(self.device.key)
            return device_services.set_state(handle, key, wanted)

        def done(_pending, error) -> None:
            if error is not None:
                self.message.set(t("Failed: {error}", error=error))
                # The checkbox must reflect the device, not the intent.
                self.vars[key].set(not wanted)
                return
            self.reload()

        _run_off_thread(self.window, work, done)

    def _reboot(self) -> None:
        self.message.set(t("Restarting..."))

        def work():
            handle = self.app.controller.device_for(self.device.key)
            # The reply is lost with the connection: failure is expected.
            try:
                handle.call("Shelly.Reboot")
            except Exception:  # noqa: BLE001
                pass
            time.sleep(12.0)
            self.app.controller.connect_device(
                self.device.key, allow_scan=False, force=True
            )
            return True

        def done(_result, error) -> None:
            if error is not None:
                self.message.set(t("Failed: {error}", error=error))
                return
            self.owner.refresh()
            self.reload()

        _run_off_thread(self.window, work, done)

    def _close(self) -> None:
        try:
            self.window.destroy()
        except tk.TclError:
            pass


class DeviceLedsDialog:
    """Light rings, night mode and outlet buttons of a Power Strip.

    Shipped at full brightness, the rings light up a room in the dark. The
    dialog sets their mode and intensity, and above all night mode, which
    dims them on its own during the chosen hours.

    The buttons are here too: detaching an outlet's button prevents it from
    switching that outlet. The PC outlet's is detached by default, and the
    application reapplies it on every connection -- its checkbox stays
    ticked and greyed out.
    """

    MODES = (
        (device_leds.MODE_POWER, "Power: the colour follows the load"),
        (device_leds.MODE_SWITCH, "State: one colour when on, another when off"),
        (device_leds.MODE_OFF, "Off"),
    )

    def __init__(self, parent: tk.Tk, owner: SettingsWindow, device) -> None:
        self.owner = owner
        self.app = owner.app
        self.device = device
        self.on_rgb = (0, 100, 0)
        self.off_rgb = (100, 0, 0)
        # Devices whose rings are waiting for a restart: this one, and the
        # others when everything was applied at once.
        self.pending: set[str] = set()

        self.window = tk.Toplevel(parent)
        self.window.title(t("LEDs and buttons - {device}", device=device.label))
        self.window.geometry("780x700")
        self.window.minsize(700, 640)
        self.window.transient(parent)
        _theme_dialog(self.window, owner.palette)

        ttk.Label(
            self.window,
            text=t("Each outlet has a light ring and a push button. These "
                   "settings are stored in the device and apply at once. "
                   "If it asks for a restart, the button below does it "
                   "without switching any outlet."),
            wraplength=740,
            justify="left",
            padding=14,
        ).pack(anchor="w")

        # Window footer reserved before the body, as everywhere else.
        footer = ttk.Frame(self.window, padding=14)
        footer.pack(fill="x", side="bottom")
        self.reboot_button = ttk.Button(
            footer, text=t("Restart to apply"), command=self._reboot, state="disabled"
        )
        self.reboot_button.pack(side="left")
        ttk.Button(footer, text=t("Refresh"), command=self.reload).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(footer, text=t("Close"), command=self._close).pack(side="right")
        self.apply_all_button = ttk.Button(
            footer, text=t("Apply to all devices"), command=self._apply_all
        )
        self.apply_all_button.pack(side="right", padx=(0, 8))
        self.apply_button = ttk.Button(footer, text=t("Apply"), command=self._apply)
        self.apply_button.pack(side="right", padx=(0, 8))

        self.message = tk.StringVar(self.window, value="")
        ttk.Label(self.window, textvariable=self.message, wraplength=740,
                  justify="left", padding=(14, 4)).pack(side="bottom", anchor="w")

        body = ttk.Frame(self.window, padding=(14, 0))
        body.pack(fill="both", expand=True)

        # --- Rings
        ring = ttk.LabelFrame(body, text=t("Light rings"), padding=10)
        ring.pack(fill="x")
        ring.columnconfigure(1, weight=1)
        self.mode = tk.StringVar(self.window, value=device_leds.MODE_POWER)
        for row, (value, wording) in enumerate(self.MODES):
            ttk.Radiobutton(
                ring, text=t(wording), value=value, variable=self.mode,
                command=self._update_states,
            ).grid(row=row, column=0, columnspan=5, sticky="w", pady=1)
        self.brightness = tk.IntVar(self.window, value=100)
        self.on_brightness = tk.IntVar(self.window, value=100)
        self.off_brightness = tk.IntVar(self.window, value=100)
        self.power_row = self._slider(ring, 3, t("Brightness"), self.brightness)
        self.on_row = self._slider(ring, 4, t("When on"), self.on_brightness)
        self.off_row = self._slider(ring, 5, t("When off"), self.off_brightness)
        self.on_swatch = self._swatch(ring, 4, "on")
        self.off_swatch = self._swatch(ring, 5, "off")
        self.on_row += self.on_swatch
        self.off_row += self.off_swatch

        # --- Night mode
        night = ttk.LabelFrame(body, text=t("Night mode"), padding=10)
        night.pack(fill="x", pady=(12, 0))
        night.columnconfigure(1, weight=1)
        self.night_enabled = tk.BooleanVar(self.window, value=False)
        self.night_box = ttk.Checkbutton(
            night, text=t("Dim the rings between these times"),
            variable=self.night_enabled, command=self._update_states,
        )
        self.night_box.grid(row=0, column=0, columnspan=5, sticky="w")
        self.night_brightness = tk.IntVar(self.window, value=device_leds.NIGHT_BRIGHTNESS)
        self.night_row = self._slider(night, 1, t("Brightness"), self.night_brightness)
        hours = ttk.Frame(night)
        hours.grid(row=2, column=0, columnspan=5, sticky="w", pady=(6, 0))
        self.night_start = tk.StringVar(self.window, value=device_leds.NIGHT_START)
        self.night_end = tk.StringVar(self.window, value=device_leds.NIGHT_END)
        ttk.Label(hours, text=t("From")).pack(side="left")
        start = ttk.Entry(hours, textvariable=self.night_start, width=7)
        start.pack(side="left", padx=(6, 12))
        ttk.Label(hours, text=t("to")).pack(side="left")
        end = ttk.Entry(hours, textvariable=self.night_end, width=7)
        end.pack(side="left", padx=(6, 12))
        ttk.Label(hours, text=t("HH:MM, device clock"),
                  style="Hint.TLabel").pack(side="left")
        self.night_row += [start, end]

        # --- Buttons, filled on read: their number comes from the device.
        pushes = ttk.LabelFrame(body, text=t("Push buttons"), padding=10)
        pushes.pack(fill="x", pady=(12, 0))
        ttk.Label(
            pushes,
            text=t("A detached button no longer switches its outlet: only "
                   "this app does. Takes effect at once."),
            wraplength=700, justify="left", style="Hint.TLabel",
        ).pack(anchor="w")
        self.button_rows = ttk.Frame(pushes)
        self.button_rows.pack(fill="x", pady=(6, 0))

        self._update_states()
        self.reload()

    # ------------------------------------------------------------- widgets

    def _slider(self, parent, row: int, text: str, variable: tk.IntVar) -> list:
        """Label / slider / value row; returns its adjustable widgets."""
        label = ttk.Label(parent, text=text)
        label.grid(row=row, column=0, sticky="w", padx=(22, 12), pady=3)
        value = ttk.Label(parent, width=6, anchor="e")
        value.grid(row=row, column=2, sticky="e")

        def moved(_raw=None) -> None:
            # The slider returns decimals; the device wants integers.
            variable.set(int(round(variable.get())))
            value.configure(text=f"{variable.get()} %")

        scale = ttk.Scale(parent, from_=0, to=100, orient="horizontal",
                          variable=variable, command=moved)
        scale.grid(row=row, column=1, sticky="ew", pady=3)
        variable.trace_add("write", lambda *_: value.configure(
            text=f"{int(round(variable.get()))} %"))
        moved()
        return [label, scale]

    def _swatch(self, parent, row: int, state: str) -> list:
        """Colour swatch and a button to change it."""
        swatch = tk.Label(parent, width=3, relief="solid", borderwidth=1)
        swatch.grid(row=row, column=3, padx=(12, 6))
        button = ttk.Button(parent, text=t("Colour..."),
                            command=lambda: self._pick_colour(state))
        button.grid(row=row, column=4, sticky="w")
        return [swatch, button]

    def _paint_swatches(self) -> None:
        for (swatch, _button), rgb in (
            (self.on_swatch, self.on_rgb), (self.off_swatch, self.off_rgb)
        ):
            swatch.configure(background=_hex_colour(rgb))

    def _pick_colour(self, state: str) -> None:
        from tkinter import colorchooser

        current = self.on_rgb if state == "on" else self.off_rgb
        chosen, _hex = colorchooser.askcolor(
            color=_hex_colour(current), parent=self.window,
            title=t("Ring colour when on") if state == "on"
            else t("Ring colour when off"),
        )
        if chosen is None:
            return
        # The device counts its channels in percentages, not bytes.
        percent = tuple(int(round(channel * 100 / 255)) for channel in chosen)
        if state == "on":
            self.on_rgb = percent
        else:
            self.off_rgb = percent
        self._paint_swatches()

    def _update_states(self) -> None:
        """Enable only what matters in the chosen mode."""
        mode = self.mode.get()

        def enable(widgets, on: bool) -> None:
            for widget in widgets:
                if isinstance(widget, tk.Label):
                    continue  # the swatch stays visible, even when inactive
                if isinstance(widget, ttk.Label):
                    # A disabled label gets a light background in this
                    # theme: just fade it instead.
                    widget.configure(style="TLabel" if on else "Hint.TLabel")
                    continue
                widget.state(["!disabled"] if on else ["disabled"])

        enable(self.power_row, mode == device_leds.MODE_POWER)
        enable(self.on_row + self.off_row, mode == device_leds.MODE_SWITCH)
        rings_lit = mode != device_leds.MODE_OFF
        enable([self.night_box], rings_lit)
        enable(self.night_row, rings_lit and self.night_enabled.get())

    # ------------------------------------------------------------- reading

    def reload(self) -> None:
        self.message.set(t("Reading the device..."))

        def work():
            handle = self.app.controller.device_for(self.device.key)
            return device_leds.read(handle), device_services.restart_required(handle)

        def done(result, error) -> None:
            if error is not None:
                self.message.set(t("Cannot reach the device: {error}", error=error))
                return
            found, pending = result
            if found is None:
                self.message.set(t("This device has no light rings or buttons "
                                   "to set: it is not a Power Strip."))
                self.apply_button.state(["disabled"])
                return
            settings, buttons = found
            self.mode.set(settings.mode)
            self.brightness.set(settings.brightness)
            self.on_brightness.set(settings.on_brightness)
            self.off_brightness.set(settings.off_brightness)
            self.on_rgb, self.off_rgb = settings.on_rgb, settings.off_rgb
            self.night_enabled.set(settings.night_enabled)
            self.night_brightness.set(settings.night_brightness)
            self.night_start.set(settings.night_start)
            self.night_end.set(settings.night_end)
            self._paint_swatches()
            self._update_states()
            self._fill_buttons(buttons)
            if pending:
                self.pending.add(self.device.key)
            else:
                self.pending.discard(self.device.key)
            self._show_pending()

        _run_off_thread(self.window, work, done)

    def _fill_buttons(self, buttons: dict[int, str]) -> None:
        for child in self.button_rows.winfo_children():
            child.destroy()
        pc = self.app.config.host_pc_outlet()
        for switch_id in sorted(buttons):
            outlet = next(
                (o for o in self.app.config.outlets_of(self.device.key)
                 if o.switch_id == switch_id), None,
            )
            name = outlet.label if outlet is not None else f"{self.device.key}:{switch_id}"
            is_pc = (
                pc is not None and pc.device == self.device.key
                and pc.switch_id == switch_id
            )
            variable = tk.BooleanVar(
                self.window,
                value=buttons[switch_id] == device_leds.BUTTON_DETACHED
            )
            box = ttk.Checkbutton(
                self.button_rows,
                text=(t("{outlet}: detached - it powers the PC, the app keeps "
                        "it that way", outlet=name) if is_pc
                      else t("{outlet}: detached", outlet=name)),
                variable=variable,
                command=lambda s=switch_id, v=variable: self._toggle_button(s, v),
            )
            box.pack(anchor="w", pady=1)
            if is_pc:
                box.state(["disabled"])

    def _show_pending(self) -> None:
        if self.pending:
            self.reboot_button.state(["!disabled"])
            names = ", ".join(sorted(
                (self.app.config.device(key).label
                 if self.app.config.device(key) else key)
                for key in self.pending
            ))
            self.message.set(t("Restart needed for the rings to change: {devices}. "
                               "No outlet is switched by a restart.", devices=names))
        else:
            self.reboot_button.state(["disabled"])
            self.message.set("")

    # ------------------------------------------------------------- actions

    def _settings(self) -> "device_leds.LedSettings | None":
        """The entered settings, or `None` after saying what is wrong."""
        start, end = self.night_start.get().strip(), self.night_end.get().strip()
        for clock in (start, end):
            if not device_leds.valid_clock(clock):
                self.message.set(t("'{value}' is not a time: use HH:MM, for "
                                   "example 22:00.", value=clock))
                return None
        return device_leds.LedSettings(
            mode=self.mode.get(),
            brightness=int(self.brightness.get()),
            on_rgb=self.on_rgb,
            on_brightness=int(self.on_brightness.get()),
            off_rgb=self.off_rgb,
            off_brightness=int(self.off_brightness.get()),
            night_enabled=self.night_enabled.get(),
            night_brightness=int(self.night_brightness.get()),
            night_start=start,
            night_end=end,
        )

    def _apply(self) -> None:
        settings = self._settings()
        if settings is None:
            return
        self.message.set(t("Applying..."))

        def work():
            handle = self.app.controller.device_for(self.device.key)
            return device_leds.apply(handle, settings)

        def done(pending, error) -> None:
            if error is not None:
                self.message.set(t("Failed: {error}", error=error))
                return
            if pending:
                self.pending.add(self.device.key)
            self._show_pending()
            if not self.pending:
                self.message.set(t("Applied."))

        _run_off_thread(self.window, work, done)

    def _apply_all(self) -> None:
        """Same settings on every known Power Strip."""
        settings = self._settings()
        if settings is None:
            return
        self.message.set(t("Applying..."))
        keys = [device.key for device in self.app.config.devices]

        def work():
            pending, skipped, applied = [], [], 0
            for key in keys:
                try:
                    handle = self.app.controller.device_for(key)
                    if device_leds.read(handle) is None:
                        continue  # not a Power Strip: nothing to set
                    if device_leds.apply(handle, settings):
                        pending.append(key)
                    applied += 1
                except Exception as exc:  # noqa: BLE001 - carry on with the others
                    skipped.append(f"{key} ({exc})")
            return pending, skipped, applied

        def done(result, error) -> None:
            if error is not None:
                self.message.set(t("Failed: {error}", error=error))
                return
            pending, skipped, applied = result
            self.pending.update(pending)
            self._show_pending()
            if not self.pending:
                self.message.set(t("Applied to {count} device(s).", count=applied))
            if skipped:
                self.message.set(
                    self.message.get() + "  "
                    + t("Not reached: {devices}", devices=", ".join(skipped))
                )

        _run_off_thread(self.window, work, done)

    def _toggle_button(self, switch_id: int, variable: tk.BooleanVar) -> None:
        wanted = variable.get()
        self.message.set(t("Applying..."))

        def work():
            handle = self.app.controller.device_for(self.device.key)
            device_leds.set_button(handle, switch_id, detached=wanted)
            return True

        def done(_result, error) -> None:
            if error is not None:
                self.message.set(t("Failed: {error}", error=error))
                variable.set(not wanted)  # the checkbox follows the device
                return
            self._show_pending()

        _run_off_thread(self.window, work, done)

    def _reboot(self) -> None:
        """Restart the pending devices. No output toggles: latching relays,
        and the PC outlet comes back on anyway."""
        keys = sorted(self.pending)
        self.message.set(t("Restarting..."))
        self.reboot_button.state(["disabled"])

        def work():
            for key in keys:
                handle = self.app.controller.device_for(key)
                # The reply is lost with the connection: failure is expected.
                try:
                    handle.call("Shelly.Reboot")
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(12.0)
            for key in keys:
                self.app.controller.connect_device(key, allow_scan=False, force=True)
            return True

        def done(_result, error) -> None:
            if error is not None:
                self.message.set(t("Failed: {error}", error=error))
                self.reboot_button.state(["!disabled"])
                return
            self.pending.clear()
            self.owner.refresh()
            self.reload()

        _run_off_thread(self.window, work, done)

    def _close(self) -> None:
        try:
            self.window.destroy()
        except tk.TclError:
            pass


def _hex_colour(rgb) -> str:
    """Device colour (0-100 per channel) in Tk notation."""
    return "#" + "".join(
        f"{max(0, min(255, int(round(channel * 255 / 100)))):02x}" for channel in rgb
    )


def _grouped(value: int) -> str:
    """Byte count with a space every three digits."""
    return f"{value:,}".replace(",", " ")


class PasswordDialog:
    """Entry of a device's password, and setting it on the device."""

    def __init__(self, parent: tk.Tk, owner: SettingsWindow, device) -> None:
        self.owner = owner
        self.app = owner.app
        self.device = device

        self.window = tk.Toplevel(parent)
        self.window.title(f"Password - {device.label}")
        # Five buttons, including "Mot de passe perdu ?": at 680 pixels the
        # last one in got squeezed.
        self.window.geometry("780x430")
        self.window.minsize(760, 400)
        self.window.transient(parent)
        self.window.grab_set()
        _theme_dialog(self.window, owner.palette)

        ttk.Label(
            self.window,
            text=(
                t("Protecting the device stops anyone on the local network from "
                "commanding the outlets, running scripts on it or changing its "
                "Wi-Fi settings. The user name is always 'admin'; only the "
                "password can be chosen.")
            ),
            wraplength=640,
            justify="left",
            padding=14,
        ).pack(anchor="w")

        self.state = tk.StringVar(self.window, value="")
        ttk.Label(self.window, textvariable=self.state, wraplength=640,
                  justify="left", padding=(14, 0)).pack(anchor="w")

        form = ttk.Frame(self.window, padding=14)
        form.pack(fill="x")
        ttk.Label(form, text=t("Password")).grid(row=0, column=0, sticky="w", pady=3)
        self.first = tk.StringVar(self.window)
        ttk.Entry(form, textvariable=self.first, show="*", width=32).grid(
            row=0, column=1, padx=8
        )
        ttk.Label(form, text=t("Confirm")).grid(row=1, column=0, sticky="w", pady=3)
        self.second = tk.StringVar(self.window)
        ttk.Entry(form, textvariable=self.second, show="*", width=32).grid(
            row=1, column=1, padx=8
        )

        ttk.Label(
            self.window,
            text=(
                t("The password is stored encrypted with Windows DPAPI: the key "
                "comes from your Windows account, not from this program, and "
                "the stored value cannot be read by another account or on "
                "another machine.")
            ),
            wraplength=640,
            justify="left",
            style="Hint.TLabel",
            padding=(14, 0),
        ).pack(anchor="w")

        self.message = tk.StringVar(self.window, value="")
        ttk.Label(self.window, textvariable=self.message, wraplength=640,
                  justify="left", padding=(14, 8)).pack(anchor="w")

        buttons = ttk.Frame(self.window, padding=14)
        buttons.pack(fill="x", side="bottom")
        ttk.Button(buttons, text=t("Apply to device"), command=self._apply).pack(side="left")
        ttk.Button(buttons, text=t("Remember only"), command=self._remember).pack(
            side="left", padx=8
        )
        ttk.Button(buttons, text=t("Remove password"), command=self._remove).pack(side="left")
        ttk.Button(buttons, text=t("Close"), command=self._close).pack(side="right")
        ttk.Button(buttons, text=t("Lost password?"), command=self.show_reset_help).pack(
            side="right", padx=8
        )

        self._refresh_state()

    @staticmethod
    def show_reset_help(parent: tk.Misc | None = None) -> None:
        messagebox.showinfo("Factory reset", FACTORY_RESET_STEPS, parent=parent)

    def _close(self) -> None:
        try:
            self.window.grab_release()
            self.window.destroy()
        except tk.TclError:
            pass

    def _refresh_state(self) -> None:
        stored = (
            t("a password is stored")
            if self.device.has_password
            else t("no password stored")
        )
        failure = self.app.controller.auth_failures.get(self.device.key)
        if failure:
            self.state.set(
                t("The device refuses the current credentials ({failure}).\n"
                  "Enter the right password and use « Remember only », or reset "
                  "the device with its buttons.", failure=failure)
            )
        else:
            self.state.set(
                t("Device '{key}': {state}.", key=self.device.key, state=stored)
            )

    def _typed(self) -> str | None:
        """Typed password, after checking the confirmation."""
        first, second = self.first.get(), self.second.get()
        if not first:
            self.message.set("Enter a password first.")
            return None
        if first != second:
            self.message.set("The two entries differ.")
            return None
        if len(first) < 4:
            self.message.set("Too short to be worth setting.")
            return None
        return first

    def _busy(self, text: str) -> None:
        self.message.set(text)
        self.window.update_idletasks()

    def _apply(self) -> None:
        """Set the password on the device and store it."""
        password = self._typed()
        if password is None:
            return
        self._busy("Applying to the device...")

        def done(_result, error):
            if error is not None:
                self.message.set(f"Failed: {error}")
                # A failure at this stage often leaves the device unchanged,
                # but if the password was set without us being able to read
                # it back, only a hardware reset gets out of it.
                self.show_reset_help(self.window)
            else:
                self.message.set("Password set on the device and stored.")
                self.first.set("")
                self.second.set("")
            self.owner.refresh()
            self._refresh_state()

        _run_off_thread(
            self.window,
            lambda: self.app.controller.set_device_password(self.device.key, password),
            done,
        )

    def _remember(self) -> None:
        """Store a password already set on the device, without changing it."""
        password = self._typed()
        if password is None:
            return
        self.device.set_password(password)
        self.owner._save()
        self._busy("Stored. Checking against the device...")

        def done(_result, error):
            if error is not None:
                self.message.set(f"The device still refuses it: {error}")
            else:
                self.message.set("Accepted by the device.")
                self.first.set("")
                self.second.set("")
            self.owner.refresh()
            self._refresh_state()

        def work():
            self.app.controller._devices.pop(self.device.key, None)
            self.app.controller.connect_device(
                self.device.key, allow_scan=False, force=True
            )
            return self.app.controller.device_for(self.device.key).get_all_switches()

        _run_off_thread(self.window, work, done)

    def _remove(self) -> None:
        """Remove authentication from the device."""
        if not messagebox.askyesno(
            "Shelly Screens",
            "Remove the password from the device?\n\n"
            "Anyone on the local network will be able to command its outlets "
            "and run scripts on it again.",
            parent=self.window,
        ):
            return
        self._busy("Removing...")

        def done(_result, error):
            if error is not None:
                self.message.set(f"Failed: {error}")
                self.show_reset_help(self.window)
            else:
                self.message.set("Password removed from the device.")
            self.owner.refresh()
            self._refresh_state()

        _run_off_thread(
            self.window,
            lambda: self.app.controller.set_device_password(self.device.key, ""),
            done,
        )
