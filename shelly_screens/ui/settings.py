"""Fenetre de reglages : appareils, prises, profils, comportement.

Tkinter exige que tous ses appels viennent du thread qui a cree la racine.
La fenetre tourne donc dans son propre thread, avec sa propre boucle, et ne
touche jamais directement a la boucle de messages de l'icone. Une seule
fenetre a la fois, sans quoi deux racines Tk cohabiteraient dans le meme
processus.
"""

from __future__ import annotations

import threading
import time
import tkinter as tk
import webbrowser
from tkinter import messagebox, simpledialog, ttk
from typing import TYPE_CHECKING

from . import theme as theme_module
from .. import discovery, sensing
from .. import i18n
from ..i18n import t
from ..config import KIND_LABELS, KIND_SCREEN, KINDS, OutletConfig, Profile
from ..win import icon as icon_module
from .. import __version__
from ..win import monitors
from .. import device_services

if TYPE_CHECKING:
    from ..app import Application

_state_lock = threading.Lock()
_is_open = False

# Au-dela de cette puissance, une prise n'est pas tenue pour porter un
# ecran, et l'assistant d'identification refuse d'y toucher. Un moniteur,
# meme grand, depasse rarement 60 W ; une unite centrale en consomme plus
# de 100. C'est une securite physique : elle ne depend d'aucun marquage.
IDENTIFY_MAX_WATTS = 80.0
# Rafraichissement de l'etat du releve, en nombre de cycles de 3 secondes.
SENSING_REFRESH_TICKS = 5
# Un rafraichissement de la mesure tous les N cycles de 3 secondes.
SENSING_REFRESH_TICKS = 5


def open_settings(application: "Application") -> None:
    """Ouvre la fenetre de reglages, si elle ne l'est pas deja."""
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
        except Exception as exc:  # noqa: BLE001 - une UI ratee ne doit pas tuer l'appli
            application.log(f"Settings window failed: {exc}")
        finally:
            with _state_lock:
                _is_open = False

    threading.Thread(target=run, name="settings-ui", daemon=True).start()


class SettingsWindow:
    """Contenu de la fenetre de reglages."""

    def __init__(self, root: tk.Tk, application: "Application") -> None:
        self.root = root
        self.app = application
        self.config = application.config

        # Avant toute construction : les widgets lisent leur texte une fois.
        i18n.set_language(self.config.settings.language)
        icon_module.apply_to_window(root)
        root.title(t("Shelly Screens {version} - Settings", version=__version__))
        root.geometry("860x720")
        root.minsize(760, 600)

        # Le theme doit etre pose avant la creation des widgets : certains
        # lisent leurs couleurs a la construction.
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
        notebook.add(self.devices_tab, text=t("Devices"))
        notebook.add(self.outlets_tab, text=t("Outlets"))
        notebook.add(self.profiles_tab, text=t("Profiles"))
        notebook.add(self.sensing_tab, text=t("PC power"))
        notebook.add(self.behaviour_tab, text=t("Behaviour"))
        # Sans cet appel, Ctrl+Tab et Alt+lettre ne changent pas d'onglet.
        notebook.enable_traversal()

        self.status = tk.StringVar(value="")
        ttk.Label(root, textvariable=self.status, anchor="w", padding=(12, 6)).pack(
            fill="x", side="bottom"
        )

        self._build_devices_tab()
        self._build_outlets_tab()
        self._build_profiles_tab()
        self._build_sensing_tab()
        self._build_behaviour_tab()

        self.refresh()
        notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self._restyle()
        self._update_theme_hint()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        # Rafraichissement doux, pour refleter les changements venus du menu.
        self._schedule_refresh()

    # ------------------------------------------------------------- utilitaires

    def set_status(self, message: str) -> None:
        self.status.set(message)

    def _schedule_refresh(self) -> None:
        self.refresh_readings()
        self._follow_system_theme()
        # Le releve avance tout seul sur l'appareil : sans un rappel
        # periodique, l'interface continuerait d'annoncer « aucune mesure »
        # pendant que les echantillons s'accumulent. On espace davantage
        # que le reste : c'est un appel reseau, et le releve ecrit au plus
        # une fois par minute.
        self._sensing_ticks += 1
        if self._sensing_ticks >= SENSING_REFRESH_TICKS:
            self._sensing_ticks = 0
            self.refresh_sensing()
        self.root.after(3000, self._schedule_refresh)

    def _on_tab_changed(self, _event=None) -> None:
        """Reconstruit les listes en arrivant sur un onglet.

        Le rappel periodique ne repose que les valeurs qui bougent -- etat,
        puissance, adresse. Tout le reste, roles et types des prises,
        appareils ajoutes par une reconnexion, profil applique depuis le
        menu, n'apparaissait qu'apres une action explicite dans la fenetre.
        Or changer d'onglet est precisement le moment ou l'on vient
        regarder : c'est la qu'on attend des listes a jour.
        """
        # `refresh` couvre tout, y compris l'onglet de detection : inutile
        # d'y ajouter quoi que ce soit, on ne ferait qu'interroger
        # l'appareil deux fois pour le meme affichage.
        self.refresh()

    def _follow_system_theme(self) -> None:
        """En mode `system`, suivre un basculement clair/sombre de Windows."""
        if self.config.settings.theme != "system":
            return
        now_dark = theme_module.system_prefers_dark()
        if now_dark != self._system_was_dark:
            self._system_was_dark = now_dark
            self.apply_theme()
            self._update_theme_hint()

    def apply_theme(self) -> None:
        """Applique le theme courant a la fenetre et a ses widgets."""
        self.palette = theme_module.apply(self.root, self.config.settings.theme)
        self._restyle()

    def _restyle(self) -> None:
        """Recolore ce que ttk.Style ne couvre pas."""
        palette = self.palette
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

    # ---------------------------------------------------------- onglet appareils

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

        # Hote et adresse sont deux colonnes distinctes : on joint souvent
        # l'appareil par son nom mDNS, plus stable que son bail DHCP, mais
        # c'est l'adresse qu'on veut lire pour ouvrir son interface web ou
        # reperer qu'elle a change.
        columns = ("kind", "host", "ip", "outlets", "auth", "state")
        self.device_tree = ttk.Treeview(frame, columns=columns, height=7)
        self.device_tree.heading("#0", text=t("Key / name"))
        self.device_tree.heading("kind", text=t("Model"))
        self.device_tree.heading("host", text=t("Reached via"))
        self.device_tree.heading("ip", text=t("IP address"))
        self.device_tree.heading("outlets", text=t("Outlets"))
        self.device_tree.heading("auth", text=t("Password"))
        self.device_tree.heading("state", text=t("Status"))
        self.device_tree.column("#0", width=160)
        self.device_tree.column("kind", width=115)
        self.device_tree.column("host", width=215)
        self.device_tree.column("ip", width=110)
        self.device_tree.column("outlets", width=58, anchor="center")
        self.device_tree.column("auth", width=78, anchor="center")
        self.device_tree.column("state", width=90)
        # La cellule d'adresse se comporte comme un lien. ttk.Treeview ne
        # sait pas styler une cellule isolee -- impossible de la souligner
        # sans repeindre toute la ligne -- alors on s'en remet au signal
        # universel : le curseur en main au survol.
        self.device_tree.bind("<Motion>", self._device_tree_hover)
        self.device_tree.bind("<Leave>", lambda _e: self.device_tree.configure(cursor=""))
        self.device_tree.bind("<Button-1>", self._device_tree_click, add="+")
        self.device_tree.pack(fill="both", expand=True)

        # Bandeau d'alerte : masque tant que tout va bien, il apparait des
        # qu'un appareil refuse le mot de passe et mene a la marche a suivre.
        self.auth_banner = ttk.Frame(frame)
        self.auth_alert = tk.StringVar(value="")
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
        ttk.Button(buttons, text=t("Name and key..."), command=self._name_device).pack(
            side="left", padx=8
        )
        ttk.Button(buttons, text=t("Password..."), command=self._device_password).pack(
            side="left", padx=8
        )
        ttk.Button(buttons, text=t("Services..."), command=self._device_services).pack(
            side="left"
        )
        ttk.Button(buttons, text=t("Open web UI"), command=self._open_web_ui).pack(
            side="left", padx=8
        )
        ttk.Button(buttons, text=t("Reconnect"), command=self._reconnect).pack(side="left")
        ttk.Button(buttons, text=t("Remove"), command=self._remove_device).pack(
            side="left", padx=8
        )

    def _auth_label(self, device) -> str:
        """Etat du mot de passe tel que l'appareil le rapporte.

        La colonne montrait ce que l'application avait memorise, ce qui
        ment des que l'appareil change de son cote : une remise a zero lui
        retire son mot de passe sans que le notre disparaisse, et l'on
        croyait proteger un appareil grand ouvert. On affiche donc ce qu'il
        annonce, et l'on signale le desaccord plutot que de le taire.
        """
        identity = self.app.controller.identity(device.key)
        if identity is None:
            # Hors ligne : on ne sait rien de lui, seulement ce qu'on garde.
            return t("stored") if device.has_password else t("none")
        if identity.auth_enabled:
            return t("set") if device.has_password else t("unknown")
        return t("none, stored") if device.has_password else t("none")

    def _device_state_label(self, key: str, online: set[str]) -> str:
        """Etat lisible d'un appareil, l'echec d'authentification en propre."""
        if key in self.app.controller.auth_failures:
            return t("auth failed")
        return t("online") if key in online else t("offline")

    def _selected_device_key(self) -> str | None:
        selection = self.device_tree.selection()
        return selection[0] if selection else None

    def _ip_column_id(self) -> str:
        """Identifiant Tk de la colonne d'adresse (#1 est la premiere)."""
        return f"#{list(self.device_tree['columns']).index('ip') + 1}"

    def _ip_link_at(self, x: int, y: int) -> tuple[str, str] | None:
        """Cle et adresse si le point vise une cellule d'adresse utilisable."""
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
        """Affiche ou masque l'alerte d'authentification."""
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
        """Ouvre l'interface web de l'appareil selectionne dans le navigateur."""
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

    # ------------------------------------------------------------- onglet prises

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

        # Une colonne vide separe la puissance de l'ecran. Le nombre est cale
        # a droite, le libelle a gauche : sans rien entre eux, « 105 W » et
        # « non identifie » se touchent et se lisent comme une seule valeur.
        # ttk.Treeview ne sait pas espacer une cellule, d'ou cette colonne.
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
        self.outlet_name = tk.StringVar()
        name_entry = ttk.Entry(editor, textvariable=self.outlet_name, width=26)
        name_entry.grid(row=0, column=1, sticky="w", padx=(8, 24))
        name_entry.bind("<FocusOut>", lambda _e: self._apply_outlet_edits())
        name_entry.bind("<Return>", lambda _e: self._apply_outlet_edits())

        ttk.Label(editor, text=t("Type")).grid(row=0, column=2, sticky="e", padx=(0, 8))
        self.outlet_kind = tk.StringVar()
        kind_box = ttk.Combobox(
            editor,
            textvariable=self.outlet_kind,
            values=[KIND_LABELS[k] for k in KINDS],
            state="readonly",
            width=13,
        )
        kind_box.grid(row=0, column=3, sticky="w")
        kind_box.bind("<<ComboboxSelected>>", lambda _e: self._apply_outlet_edits())

        self.outlet_critical = tk.BooleanVar()
        ttk.Checkbutton(
            editor,
            text=t("Critical - never switched off"),
            variable=self.outlet_critical,
            command=self._apply_outlet_edits,
        ).grid(row=1, column=1, columnspan=3, sticky="w", pady=(8, 0))

        self.outlet_boot = tk.BooleanVar()
        ttk.Checkbutton(
            editor,
            text=t("Boot screen - fallback if the stored profile is unusable"),
            variable=self.outlet_boot,
            command=self._apply_outlet_edits,
        ).grid(row=2, column=1, columnspan=3, sticky="w", pady=(4, 0))

        self.outlet_host_pc = tk.BooleanVar()
        ttk.Checkbutton(
            editor,
            text=t("Powers the PC itself - never switched off"),
            variable=self.outlet_host_pc,
            command=self._apply_outlet_edits,
        ).grid(row=3, column=1, columnspan=3, sticky="w", pady=(4, 0))

        self.outlet_cut_on_sleep = tk.BooleanVar()
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
        # Les noeuds d'appareil ne sont pas des prises : ils n'ont pas de ":".
        return selection[0] if ":" in selection[0] else None

    def _on_outlet_selected(self) -> None:
        outlet = self.config.outlet(self._selected_ref() or "")
        if outlet is None:
            return
        self.outlet_name.set(outlet.name)
        self.outlet_kind.set(KIND_LABELS.get(outlet.kind, KIND_LABELS[""]))
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
            if label == chosen:
                outlet.kind = value
                break
        outlet.critical = self.outlet_critical.get()
        outlet.cut_on_sleep = self.outlet_cut_on_sleep.get()
        # Declarer un ecran, c'est vouloir qu'il suive la veille : c'est tout
        # l'objet du montage. La case gardait pourtant l'etat herite de ce
        # que la prise portait avant -- un accessoire, donc decochee -- et
        # l'ecran restait hors du pilotage sans que rien ne le dise.
        became_screen = (
            outlet.kind == KIND_SCREEN and previous_kind != KIND_SCREEN
        )
        if became_screen and not outlet.cut_on_sleep:
            outlet.cut_on_sleep = True
            self.outlet_cut_on_sleep.set(True)
        # Un seul ecran de demarrage et une seule prise PC, sinon ces roles
        # perdent leur sens.
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

        # Les clients deja ouverts gardent leur ancienne liste de sorties
        # protegees : il faut la leur repasser, sinon la prise du PC
        # resterait coupable jusqu'a la prochaine reconnexion.
        self.app.controller.refresh_protection()
        # Et le gardien embarque suit le role, d'un appareil a l'autre.
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

    # ------------------------------------------------------------ onglet profils

    def _build_profiles_tab(self) -> None:
        frame = self.profiles_tab
        left = ttk.Frame(frame)
        left.pack(side="left", fill="y", padx=(0, 12))

        ttk.Label(left, text=t("Profiles")).pack(anchor="w")
        self.profile_list = tk.Listbox(left, width=22, height=16, exportselection=False)
        self.profile_list.pack(fill="y", expand=True)
        self.profile_list.bind("<<ListboxSelect>>", lambda _e: self._on_profile_selected())

        list_buttons = ttk.Frame(left)
        list_buttons.pack(fill="x", pady=6)
        ttk.Button(list_buttons, text=t("New"), width=6, command=self._new_profile).pack(
            side="left"
        )
        ttk.Button(list_buttons, text=t("Rename"), width=8, command=self._rename_profile).pack(
            side="left", padx=3
        )
        ttk.Button(list_buttons, text=t("Delete"), width=7, command=self._delete_profile).pack(
            side="left"
        )

        right = ttk.Frame(frame)
        right.pack(side="left", fill="both", expand=True)

        self.profile_title = tk.StringVar(value="No profile selected")
        ttk.Label(right, textvariable=self.profile_title, style="Title.TLabel").pack(
            anchor="w"
        )

        # Les cases sont reconstruites a chaque changement de configuration :
        # ajouter une multiprise ajoute des prises, et donc des cases.
        self.outlets_box = ttk.LabelFrame(right, text=t("Powered outlets"), padding=10)
        self.outlets_box.pack(fill="x", pady=10)
        self.profile_outlet_vars: dict[str, tk.BooleanVar] = {}

        layout_box = ttk.LabelFrame(right, text=t("Window layout"), padding=10)
        layout_box.pack(fill="x")
        self.layout_info = tk.StringVar(value="")
        ttk.Label(layout_box, textvariable=self.layout_info, wraplength=440).pack(
            anchor="w", pady=(0, 8)
        )
        layout_buttons = ttk.Frame(layout_box)
        layout_buttons.pack(fill="x")
        ttk.Button(
            layout_buttons, text=t("Save current layout"), command=self._save_layout
        ).pack(side="left")
        ttk.Button(layout_buttons, text=t("Restore now"), command=self._restore_layout).pack(
            side="left", padx=6
        )
        ttk.Button(layout_buttons, text=t("Clear"), command=self._clear_layout).pack(side="left")

        apply_row = ttk.Frame(right)
        apply_row.pack(fill="x", pady=14)
        ttk.Button(
            apply_row, text=t("Apply this profile now"), command=self._apply_profile_now
        ).pack(side="left")

    def _rebuild_profile_outlets(self) -> None:
        """Recree les cases a cocher, une par prise connue."""
        for child in self.outlets_box.winfo_children():
            child.destroy()
        self.profile_outlet_vars = {}

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
            variable = tk.BooleanVar()
            self.profile_outlet_vars[outlet.ref] = variable
            suffix = ""
            if outlet.never_switch_off:
                suffix = "  (always on)"
            ttk.Checkbutton(
                self.outlets_box,
                text=f"{outlet.label}{suffix}",
                variable=variable,
                command=self._apply_profile_edits,
                state="disabled" if outlet.never_switch_off else "normal",
            ).pack(anchor="w", padx=(12 if multi_device else 0, 0))

    def _selected_profile(self) -> Profile | None:
        selection = self.profile_list.curselection()
        if not selection:
            return None
        return self.config.profile(self.profile_list.get(selection[0]))

    def _on_profile_selected(self) -> None:
        profile = self._selected_profile()
        if profile is None:
            return
        self.profile_title.set(profile.name)
        for ref, variable in self.profile_outlet_vars.items():
            outlet = self.config.outlet(ref)
            always_on = outlet is not None and outlet.never_switch_off
            variable.set(always_on or ref in profile.outlets_on)
        count = len(profile.layout)
        self.layout_info.set(
            f"{count} window(s) memorised."
            if count
            else "No layout memorised yet. Arrange your windows, then save."
        )

    def _apply_profile_edits(self) -> None:
        profile = self._selected_profile()
        if profile is None:
            return
        profile.outlets_on = [
            ref
            for ref, var in self.profile_outlet_vars.items()
            if var.get() and not (self.config.outlet(ref) or OutletConfig("", 0)).never_switch_off
        ]
        self._save()
        self.set_status(f"Profile '{profile.name}' updated")

    def _new_profile(self) -> None:
        name = simpledialog.askstring("New profile", "Profile name:", parent=self.root)
        if not name:
            return
        name = name.strip()
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
        if profile is None:
            return
        name = simpledialog.askstring(
            "Rename profile", "New name:", initialvalue=profile.name, parent=self.root
        )
        if not name or name.strip() == profile.name:
            return
        name = name.strip()
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
        if profile is None:
            return
        if not messagebox.askyesno("Shelly Screens", f"Delete profile '{profile.name}'?"):
            return
        self.config.profiles.remove(profile)
        if self.config.settings.last_profile == profile.name:
            self.config.settings.last_profile = ""
        self._save()
        self.refresh()

    def _select_profile(self, name: str) -> None:
        for index in range(self.profile_list.size()):
            if self.profile_list.get(index) == name:
                self.profile_list.selection_clear(0, "end")
                self.profile_list.selection_set(index)
                self._on_profile_selected()
                return

    def _save_layout(self) -> None:
        profile = self._selected_profile()
        if profile is None:
            return
        count = self.app.controller.capture_layout(profile.name)
        self.set_status(f"{count} window(s) memorised for '{profile.name}'")
        self._on_profile_selected()

    def _restore_layout(self) -> None:
        profile = self._selected_profile()
        if profile is None or not profile.layout:
            self.set_status("Nothing to restore")
            return
        from ..win import layout as layout_module

        result = layout_module.restore(layout_module.deserialize(profile.layout))
        self.set_status(result.summary())

    def _clear_layout(self) -> None:
        profile = self._selected_profile()
        if profile is None:
            return
        self.app.controller.clear_layout(profile.name)
        self._on_profile_selected()
        self.set_status(f"Layout cleared for '{profile.name}'")

    def _apply_profile_now(self) -> None:
        profile = self._selected_profile()
        if profile is None:
            return
        self.app.apply_profile(profile.name)
        self.set_status(f"Applying '{profile.name}'...")

    # ------------------------------------------------- onglet detection PC

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

        self.sensing_state = tk.StringVar(value="")
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
        self.probe_result = tk.StringVar(value=t("No measurement yet."))
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
        self.var_on_w = tk.DoubleVar(value=self.config.sensing.on_threshold_w)
        self.var_off_w = tk.DoubleVar(value=self.config.sensing.off_threshold_w)
        self.var_on_s = tk.DoubleVar(value=self.config.sensing.on_delay_s)
        self.var_off_s = tk.DoubleVar(value=self.config.sensing.off_delay_s)
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
            # Le `command` d'un Spinbox ne repond qu'aux fleches. Une valeur
            # tapee au clavier n'etait donc jamais validee : on croyait avoir
            # change un seuil, et rien n'avait bouge.
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

        self.sensing_warning = tk.StringVar(value="")
        ttk.Label(limits, textvariable=self.sensing_warning, wraplength=730,
                  justify="left").grid(row=len(rows) + 1, column=0, columnspan=3,
                                       sticky="w", pady=(6, 0))

        script_box = ttk.LabelFrame(frame, text=t("On-device script"), padding=10)
        script_box.pack(fill="x")
        self.script_state = tk.StringVar(value="")
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

    def refresh_sensing(self) -> None:
        """Met a jour l'onglet de detection.

        La partie locale est immediate ; l'etat du script et du releveur
        demande d'interroger l'appareil, et part donc en tache de fond.
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
            # Le KVS ne contient que des index : on les retraduit en noms,
            # seuls parlants pour verifier ce qui reviendra au demarrage.
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
            return  # saisie en cours
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
        """Affiche l'etat du releve, qu'il soit en cours ou termine.

        L'etat est dit explicitement : un releve avance tout seul sur
        l'appareil, et rien ne le signalerait si l'interface se contentait
        d'afficher les chiffres au moment ou on les demande.
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
        """Ouvre la courbe, ou les seuils se placent a la souris."""
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

    # ------------------------------------------------ onglet comportement

    def _build_behaviour_tab(self) -> None:
        frame = self.behaviour_tab
        settings = self.config.settings

        power_box = ttk.LabelFrame(frame, text=t("Sleep and shutdown"), padding=10)
        power_box.pack(fill="x")

        self.var_off_on_suspend = tk.BooleanVar(value=settings.power_off_on_suspend)
        ttk.Checkbutton(
            power_box,
            text=t("Switch outlets off when the PC sleeps or shuts down"),
            variable=self.var_off_on_suspend,
            command=self._apply_behaviour,
        ).pack(anchor="w")

        self.var_restore_on_resume = tk.BooleanVar(value=settings.restore_on_resume)
        ttk.Checkbutton(
            power_box,
            text=t("Re-apply the last profile on wake-up"),
            variable=self.var_restore_on_resume,
            command=self._apply_behaviour,
        ).pack(anchor="w")

        self.var_apply_on_start = tk.BooleanVar(value=settings.apply_profile_on_start)
        ttk.Checkbutton(
            power_box,
            text=t("Re-apply the last profile when this application starts"),
            variable=self.var_apply_on_start,
            command=self._apply_behaviour,
        ).pack(anchor="w")

        self.shutdown_summary = tk.StringVar(value="")
        ttk.Label(
            power_box,
            textvariable=self.shutdown_summary,
            wraplength=740,
            justify="left",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(8, 0))

        layout_box = ttk.LabelFrame(frame, text=t("Window layout"), padding=10)
        layout_box.pack(fill="x", pady=12)
        self.var_manage_layout = tk.BooleanVar(value=settings.manage_window_layout)
        ttk.Checkbutton(
            layout_box,
            text=t("Memorise and restore window positions with profiles"),
            variable=self.var_manage_layout,
            command=self._apply_behaviour,
        ).pack(anchor="w")

        appearance_box = ttk.LabelFrame(frame, text=t("Appearance"), padding=10)
        appearance_box.pack(fill="x", pady=(0, 12))
        self.var_theme = tk.StringVar(value=self.config.settings.theme)
        row = ttk.Frame(appearance_box)
        row.pack(anchor="w")
        for value, label in (
            ("system", "Follow Windows"),
            ("light", "Light"),
            ("dark", "Dark"),
        ):
            ttk.Radiobutton(
                row,
                text=label,
                value=value,
                variable=self.var_theme,
                command=self._change_theme,
            ).pack(side="left", padx=(0, 18))
        ttk.Label(appearance_box, text=t("Language")).pack(
            anchor="w", pady=(10, 2)
        )
        language_row = ttk.Frame(appearance_box)
        language_row.pack(anchor="w")
        self.var_language = tk.StringVar(value=self.config.settings.language)
        for value in i18n.LANGUAGES:
            ttk.Radiobutton(
                language_row,
                text=t(i18n.LANGUAGE_LABELS[value]),
                value=value,
                variable=self.var_language,
                command=self._change_language,
            ).pack(side="left", padx=(0, 18))

        self.theme_hint = tk.StringVar(value="")
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
        self.var_switch_delay = tk.IntVar(value=settings.switch_delay_ms)
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
        self.var_settle = tk.DoubleVar(value=settings.display_settle_timeout_s)
        ttk.Spinbox(
            timing_box,
            from_=1,
            to=60,
            increment=1,
            textvariable=self.var_settle,
            width=8,
            command=self._apply_behaviour,
        ).grid(row=1, column=1, padx=8, pady=(6, 0))

    def _change_theme(self) -> None:
        """Bascule de theme, immediatement et sans rouvrir la fenetre."""
        self.config.settings.theme = self.var_theme.get()
        self._save()
        self.apply_theme()
        self._update_theme_hint()
        self.set_status(f"Theme set to {self.config.settings.theme}")

    def _change_language(self) -> None:
        """Change la langue et reconstruit la fenetre.

        Les widgets Tk lisent leur texte a la construction : les traduire
        apres coup demanderait de tenir un registre de chacun. Rouvrir la
        fenetre est plus simple, et garantit qu'aucun libelle ne reste dans
        l'ancienne langue -- un ecran a moitie traduit etant pire que pas
        de traduction du tout.
        """
        chosen = self.var_language.get()
        if chosen == self.config.settings.language:
            return
        self.config.settings.language = chosen
        self._save()
        i18n.set_language(chosen)
        application = self.app
        self.root.destroy()
        # Laisser le thread de la fenetre se terminer avant de rouvrir.
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
        settings.manage_window_layout = self.var_manage_layout.get()
        try:
            settings.switch_delay_ms = max(0, int(self.var_switch_delay.get()))
            settings.display_settle_timeout_s = max(1.0, float(self.var_settle.get()))
        except (tk.TclError, ValueError):
            pass  # saisie en cours, on garde la valeur precedente
        self._save()
        self.set_status("Settings saved")

    # ------------------------------------------------------------ rafraichissement

    def refresh(self) -> None:
        """Reconstruit les listes a partir de la configuration."""
        online = self.app.controller.online_keys

        # --- appareils
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
                    len(self.config.outlets_of(device.key)),
                    self._auth_label(device),
                    self._device_state_label(device.key, online),
                ),
            )
        if selected_device and self.device_tree.exists(selected_device):
            self.device_tree.selection_set(selected_device)
        self._update_auth_banner()

        # --- prises, groupees par appareil des qu'il y en a plusieurs
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
                    # Association devenue caduque : on la montre plutot que
                    # de la taire, meme si la prise a change de type depuis.
                    display = t("{key} (not connected)", key=outlet.monitor_key)
                elif outlet.is_screen:
                    display = t("not identified")
                else:
                    # La prise du PC, un concentrateur USB ou une prise non
                    # declaree ne portent aucun ecran : annoncer qu'aucun
                    # n'est identifie laisserait croire a un reglage oublie,
                    # alors qu'il n'y a rien a regler.
                    display = ""
                roles = []
                if outlet.host_pc:
                    roles.append(t("PC"))
                if outlet.critical:
                    roles.append(t("critical"))
                if outlet.boot_screen:
                    roles.append(t("boot"))
                # Seuls les accessoires le portent : pour un ecran, suivre
                # la veille va de soi et l'afficher n'apprendrait rien.
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

        # --- profils
        selected_profile = self._selected_profile()
        self._rebuild_profile_outlets()
        self.profile_list.delete(0, "end")
        for profile in self.config.sorted_profiles():
            self.profile_list.insert("end", profile.name)
        if selected_profile is not None:
            self._select_profile(selected_profile.name)

        # --- resume de ce qui reste allume a l'arret
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
        """Met a jour les seules valeurs qui bougent, sans reconstruire la liste."""
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
        self._update_auth_banner()

    # ------------------------------------------------ assistant d'identification

    def _run_identify_wizard(self) -> None:
        """Associe chaque prise a son ecran, en observant Windows.

        Le principe : toutes les prises allumees, on en coupe une, on regarde
        quel ecran Windows retire, puis on la rallume. Il reste ainsi toujours
        les autres ecrans allumes -- l'assistant ne se coupe jamais l'herbe
        sous le pied.
        """
        if not self.app.online:
            messagebox.showerror("Shelly Screens", "No Shelly device is reachable.")
            return
        online = self.app.controller.online_keys
        states = self.app.states

        # Trois filtres successifs, du plus explicite au plus physique. Le
        # dernier ne depend d'aucun marquage : une prise qui tire beaucoup
        # n'est pas un ecran, et la couper reviendrait sans doute a arreter
        # l'unite centrale. Il protege donc meme si le role « Powers the
        # PC » n'a pas ete attribue, ou a ete perdu.
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
                # Un accessoire ne fera disparaitre aucun ecran, et une
                # prise non renseignee ne doit rien subir : on ne coupe pas
                # ce dont on ignore ce qu'il alimente.
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
    """Accorde une boite de dialogue au theme de la fenetre principale.

    Les styles ttk sont partages par tout le processus, mais le fond d'un
    Toplevel et sa barre de titre lui appartiennent en propre.
    """
    window.configure(background=palette.bg)
    theme_module.apply_titlebar(window, palette.dark)


class IdentifyDialog:
    """Petite fenetre de progression pilotant la sequence d'identification."""

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

        self.message = tk.StringVar(value="Preparing...")
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
        # Tkinter n'aime que son propre thread : on repasse par la boucle.
        def update() -> None:
            self.message.set(text)
            if step is not None:
                self.progress["value"] = step

        try:
            self.window.after(0, update)
        except tk.TclError:
            pass  # fenetre deja fermee

    def _why_not_cut(self, outlet) -> str:
        """Raison de ne pas couper cette prise maintenant, sinon vide.

        Relu a chaque etape : le role a pu changer depuis le lancement, et
        la consommation, elle, dit la verite quel que soit le marquage.
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
        """Attend qu'un ecran disparaisse (ou apparaisse) et renvoie l'ecart."""
        deadline = time.monotonic() + self.SETTLE_TIMEOUT_S
        while time.monotonic() < deadline:
            time.sleep(self.POLL_S)
            now = monitors.monitor_keys()
            difference = (now - before) if appearing else (before - now)
            if difference:
                time.sleep(0.8)  # laisser la configuration se stabiliser
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
            # 1. Tout allumer, pour partir d'un bureau complet.
            self._say("Switching every outlet on...", 0)
            for outlet in self.outlets:
                if not initial.get(outlet.ref):
                    controller.set_outlet(outlet.ref, True)
                    time.sleep(0.3)
            # L'allumage ne fait courir aucun risque ; la coupure, si.
            time.sleep(4.0)  # laisser les dalles s'initialiser

            # 2. Couper chaque prise a tour de role et regarder qui s'en va.
            for index, outlet in enumerate(self.outlets, start=1):
                if self.cancelled:
                    break
                # La liste a ete arretee au clic, mais la sequence dure une
                # minute ou deux : on revalide juste avant de couper, sur
                # l'etat courant et non sur une photo perimee.
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

            # 3. Revenir a l'etat de depart.
            self._say("Restoring outlets...", len(self.outlets) + 1)
            for outlet in self.outlets:
                controller.set_outlet(outlet.ref, initial.get(outlet.ref, True))
                time.sleep(0.3)
        except Exception as exc:  # noqa: BLE001
            self._say(f"Identification failed: {exc}")
            time.sleep(2.0)

        self._finish()

    def _deduce_last_pair(self) -> str | None:
        """Apparie le dernier couple restant, quand il n'y a plus d'ambiguite.

        La mesure echoue parfois sur un ecran : deux dalles identiques
        peuvent broncher ensemble, ou Windows tarder a retirer celle qu'on
        vient d'eteindre, et la regle « un seul ecran disparu » rejette
        alors un resultat pourtant juste. Mais s'il ne reste qu'une prise
        sans ecran et qu'un seul ecran sans prise, le couple est le seul
        possible : refaire toute la sequence pour le retrouver serait
        absurde. On le deduit, et on le dit.
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
            if self.results:
                self.owner._save()
            self.owner.refresh()
            found = len(self.results)
            self.owner.set_status(
                f"{found} of {len(self.outlets)} outlet(s) matched to a display"
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
    """Recherche les Shelly du reseau et permet d'en adopter un."""

    def __init__(self, parent: tk.Tk, owner: SettingsWindow) -> None:
        self.owner = owner
        self.app = owner.app
        self.found: list[discovery.DeviceIdentity] = []

        self.window = tk.Toplevel(parent)
        self.window.title(t("Add a Shelly device"))
        self.window.geometry("640x420")
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
        self.host = tk.StringVar()
        entry = ttk.Entry(manual, textvariable=self.host, width=32)
        entry.pack(side="left", padx=8)
        entry.bind("<Return>", lambda _e: self._probe_host())
        ttk.Button(manual, text=t("Check"), command=self._probe_host).pack(side="left")

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

        self.message = tk.StringVar(value="")
        ttk.Label(self.window, textvariable=self.message, padding=(12, 0)).pack(anchor="w")

        buttons = ttk.Frame(self.window, padding=12)
        buttons.pack(fill="x")
        self.scan_button = ttk.Button(buttons, text=t("Scan network"), command=self._scan)
        self.scan_button.pack(side="left")
        ttk.Button(buttons, text=t("Add selected"), command=self._adopt).pack(side="left", padx=8)
        ttk.Button(buttons, text=t("Close"), command=self._close).pack(side="right")

        self._scan()

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
        self.message.set("Scanning the local network...")

        def worker() -> None:
            identities = discovery.scan_network_all(every_shelly=True)

            def done() -> None:
                self._show(identities)
                self.message.set(f"{len(identities)} Shelly device(s) found")
                self.scan_button.state(["!disabled"])

            try:
                self.window.after(0, done)
            except tk.TclError:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _probe_host(self) -> None:
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
    """Execute un appel reseau hors du thread de l'interface.

    Tkinter ne tolere que son propre thread : le resultat repasse donc par
    la boucle d'evenements avec `after`.
    """

    def worker() -> None:
        try:
            result = work()
            error = None
        except Exception as exc:  # noqa: BLE001 - remonte tel quel a l'interface
            result, error = None, exc
        try:
            window.after(0, lambda: done(result, error))
        except (tk.TclError, RuntimeError):
            # Fenetre fermee pendant l'appel. Tkinter signale le cas de deux
            # facons selon l'instant : `TclError` quand le widget est detruit,
            # `RuntimeError: main thread is not in main loop` quand c'est
            # l'interpreteur entier qui est parti. Seule la premiere etait
            # rattrapee, et la seconde deversait une trace dans le journal --
            # celui-la meme qu'on relit pour comprendre un incident.
            pass

    threading.Thread(target=worker, daemon=True).start()


# Procedure de reinitialisation, verifiee dans la base de connaissances de
# Shelly. La distinction entre cinq et dix secondes compte : relacher trop
# tot ne fait qu'une remise a zero reseau, qui rallume le point d'acces WiFi
# -- ouvert sur ce modele.
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
    """Nom lisible et cle courte d'un appareil, edites ensemble.

    Les deux allaient par deux boutons distincts, alors qu'on les change
    d'un meme mouvement en decouvrant un appareil. Les reunir evite surtout
    de renommer l'un en oubliant l'autre, et de se retrouver devant une
    liste ou la cle ne dit plus ce que l'etiquette annonce.
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
        self.label_var = tk.StringVar(value=device.name)
        entry = ttk.Entry(form, textvariable=self.label_var, width=34)
        entry.grid(row=0, column=1, padx=8, sticky="w")
        ttk.Label(form, text=t("Key")).grid(row=1, column=0, sticky="w", pady=4)
        self.key_var = tk.StringVar(value=device.key)
        ttk.Entry(form, textvariable=self.key_var, width=18).grid(
            row=1, column=1, padx=8, sticky="w"
        )
        ttk.Label(
            form,
            text=t("letters and digits only"),
            style="Hint.TLabel",
        ).grid(row=2, column=1, padx=8, sticky="w")

        self.message = tk.StringVar(value="")
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
    """Services optionnels d'un appareil, et ce qu'ils coutent.

    Un Shelly sort d'usine avec tout allume. Rien de cela ne sert au
    pilotage des prises, mais chaque service garde sa pile reseau et sa part
    de memoire -- assez pour que la multiprise qui porte les scripts frole
    la panne seche et se fasse reinitialiser par son chien de garde.

    Le dialogue dit a quoi chaque service sert vraiment avant de dire
    pourquoi il ne sert pas ici : couper ce qu'on ne comprend pas est une
    mauvaise habitude, et l'on branchera peut-etre demain ce qui est
    inutile aujourd'hui.
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
        self.window.geometry("780x660")
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

        self.memory = tk.StringVar(value="")
        ttk.Label(self.window, textvariable=self.memory, padding=(14, 0),
                  style="Hint.TLabel").pack(anchor="w")

        body = ttk.Frame(self.window, padding=(14, 10))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        for row, service in enumerate(device_services.SERVICES):
            block = ttk.Frame(body)
            block.grid(row=row, column=0, sticky="ew", pady=(0, 9))
            block.columnconfigure(0, weight=1)
            variable = tk.BooleanVar()
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
            # Certains firmwares n'exposent pas le reglage. Une case grise
            # et vide laisserait croire a un service eteint et verrouille :
            # on dit plutot que l'appareil ne permet pas d'y toucher.
            note = tk.StringVar(value="")
            self.notes[service.key] = note
            ttk.Label(
                block, textvariable=note, wraplength=700,
                justify="left", style="Hint.TLabel",
            ).grid(row=3, column=0, sticky="w", padx=(22, 0))

        # Deux absences qui interrogent, et meritent mieux qu'un silence.
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

        self.message = tk.StringVar(value="")
        ttk.Label(self.window, textvariable=self.message, wraplength=730,
                  justify="left", padding=(14, 6)).pack(anchor="w")

        footer = ttk.Frame(self.window, padding=14)
        footer.pack(fill="x", side="bottom")
        self.reboot_button = ttk.Button(
            footer, text=t("Restart the device"), command=self._reboot
        )
        self.reboot_button.pack(side="left")
        ttk.Button(footer, text=t("Refresh"), command=self.reload).pack(side="left", padx=8)
        ttk.Button(footer, text=t("Close"), command=self._close).pack(side="right")

        self.reload()

    # ------------------------------------------------------------- lecture

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
                # Un service absent de la configuration ne se laisse pas
                # regler sur ce firmware : mieux vaut griser la case que
                # proposer un interrupteur qui ne commande rien.
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
                # La case doit refleter l'appareil, pas l'intention.
                self.vars[key].set(not wanted)
                return
            self.reload()

        _run_off_thread(self.window, work, done)

    def _reboot(self) -> None:
        self.message.set(t("Restarting..."))

        def work():
            handle = self.app.controller.device_for(self.device.key)
            # La reponse se perd avec la connexion : l'echec est attendu.
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


def _grouped(value: int) -> str:
    """Nombre d'octets avec des espaces tous les trois chiffres."""
    return f"{value:,}".replace(",", " ")


class PasswordDialog:
    """Saisie du mot de passe d'un appareil, et pose sur l'appareil."""

    def __init__(self, parent: tk.Tk, owner: SettingsWindow, device) -> None:
        self.owner = owner
        self.app = owner.app
        self.device = device

        self.window = tk.Toplevel(parent)
        self.window.title(f"Password - {device.label}")
        self.window.geometry("680x430")
        self.window.minsize(640, 400)
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

        self.state = tk.StringVar(value="")
        ttk.Label(self.window, textvariable=self.state, wraplength=640,
                  justify="left", padding=(14, 0)).pack(anchor="w")

        form = ttk.Frame(self.window, padding=14)
        form.pack(fill="x")
        ttk.Label(form, text=t("Password")).grid(row=0, column=0, sticky="w", pady=3)
        self.first = tk.StringVar()
        ttk.Entry(form, textvariable=self.first, show="*", width=32).grid(
            row=0, column=1, padx=8
        )
        ttk.Label(form, text=t("Confirm")).grid(row=1, column=0, sticky="w", pady=3)
        self.second = tk.StringVar()
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

        self.message = tk.StringVar(value="")
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
        stored = "a password is stored" if self.device.has_password else "no password stored"
        failure = self.app.controller.auth_failures.get(self.device.key)
        if failure:
            self.state.set(
                f"The device refuses the current credentials ({failure}).\n"
                "Enter the right password and use « Remember only », or reset "
                "the device with its buttons."
            )
        else:
            self.state.set(f"Device '{self.device.key}': {stored}.")

    def _typed(self) -> str | None:
        """Mot de passe saisi, apres verification de la confirmation."""
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
        """Pose le mot de passe sur l'appareil et le memorise."""
        password = self._typed()
        if password is None:
            return
        self._busy("Applying to the device...")

        def done(_result, error):
            if error is not None:
                self.message.set(f"Failed: {error}")
                # Un echec a ce stade laisse souvent l'appareil inchange,
                # mais si le mot de passe a ete pose sans qu'on puisse le
                # relire, seule la remise a zero materielle en sort.
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
        """Memorise un mot de passe deja pose sur l'appareil, sans le changer."""
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
        """Retire l'authentification de l'appareil."""
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
