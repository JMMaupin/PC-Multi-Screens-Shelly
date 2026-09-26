"""Assistant de premiere mise en service d'un appareil neuf.

Cinq etapes, dans l'ordre ou on les vit devant l'appareil :

1. le modele ;
2. ouvrir son point d'acces, aux boutons ;
3. y connecter le PC -- l'assistant le fait lui-meme s'il le peut ;
4. choisir le Wi-Fi de la maison et saisir son mot de passe ;
5. l'envoyer, attendre que l'appareil annonce une adresse, rendre au PC
   son Wi-Fi, puis chercher l'appareil sur le reseau.

A aucun moment on ne demande a l'appareil de scanner les reseaux : certains
exemplaires ferment leur point d'acces pour le faire, et la mise en service
s'arrete net. Voir wifi_setup.
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING, Callable

from .. import wifi_setup
from ..i18n import t
from ..win import wlan

if TYPE_CHECKING:
    from ..discovery import DeviceIdentity

POLL_MS = 2000  # recherche du point d'acces, a l'etape 3


class FirstSetupDialog:
    """La fenetre de l'assistant, une page par etape."""

    def __init__(
        self,
        parent: tk.Misc,
        palette,
        theme_dialog: Callable,
        on_done: Callable[[str, str], None],
    ) -> None:
        """`on_done(mac, ip)` : l'appareil a rejoint le Wi-Fi ; a l'appelant de le chercher."""
        self.on_done = on_done
        self.palette = palette
        self.model = wifi_setup.MODELS[0]
        self.ap = wifi_setup.AccessPoint()
        self.identity: "DeviceIdentity | None" = None
        self.joined_ap: str | None = None  # AP rejoint par l'assistant, a quitter
        self.ap_ssid: str | None = None  # AP de l'appareil, auquel revenir s'il se coupe
        # Le Wi-Fi du PC avant nous, a lui rendre a la fin -- sauf si c'est
        # deja le point d'acces d'un Shelly : on y reviendrait pour rien.
        previous = wlan.connected_ssid()
        self.previous_ssid = None if (previous or "").startswith("Shelly") else previous
        self.polling = False
        self.closed = False

        self.window = tk.Toplevel(parent)
        self.window.title(t("First setup of a new device"))
        self.window.geometry("640x520")
        self.window.minsize(600, 480)
        self.window.transient(parent)
        self.window.grab_set()
        self.window.protocol("WM_DELETE_WINDOW", self._cancel)
        theme_dialog(self.window, palette)

        self.title = tk.StringVar(self.window)
        ttk.Label(self.window, textvariable=self.title, style="Title.TLabel",
                  padding=(16, 14, 16, 4)).pack(anchor="w")
        # Le bas se reserve avant la page extensible.
        buttons = ttk.Frame(self.window, padding=12)
        buttons.pack(side="bottom", fill="x")
        self.next_button = ttk.Button(buttons, text=t("Next"), command=self._next)
        self.next_button.pack(side="right")
        self.back_button = ttk.Button(buttons, text=t("Back"), command=self._back)
        self.back_button.pack(side="right", padx=8)
        ttk.Button(buttons, text=t("Cancel"), command=self._cancel).pack(side="left")
        self.page = ttk.Frame(self.window, padding=(16, 4))
        self.page.pack(fill="both", expand=True)

        self.step = 0
        self.steps = [self._page_model, self._page_ap, self._page_connect,
                      self._page_network, self._page_send]
        self._show()

    # ------------------------------------------------------------ navigation

    def _show(self) -> None:
        self.polling = False
        for child in self.page.winfo_children():
            child.destroy()
        self.back_button.configure(state="normal" if 0 < self.step < 4 else "disabled")
        self.next_button.configure(state="normal", text=t("Next"))
        self.steps[self.step]()

    def _next(self) -> None:
        if self.step < len(self.steps) - 1:
            self.step += 1
            self._show()

    def _back(self) -> None:
        if self.step > 0:
            self.step -= 1
            self._show()

    def _text(self, text: str) -> None:
        ttk.Label(self.page, text=text, wraplength=580, justify="left").pack(
            anchor="w", pady=(0, 10)
        )

    def _after(self, delay: int, action) -> None:
        try:
            self.window.after(delay, action)
        except (tk.TclError, RuntimeError):
            pass

    def _off_thread(self, work, done) -> None:
        """Appel lent hors de l'interface ; `done(resultat)` y revient."""
        def worker() -> None:
            try:
                result = work()
            except Exception as exc:  # noqa: BLE001 - rendu tel quel a la page
                result = exc
            self._after(0, lambda: None if self.closed else done(result))
        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------ 1. modele

    def _page_model(self) -> None:
        self.title.set(t("1. Which device?"))
        self._text(t("Choose the model to set up. Only validated models are offered."))
        self.model_var = tk.StringVar(self.window, value=self.model.name)
        for model in wifi_setup.MODELS:
            ttk.Radiobutton(self.page, text=model.name, value=model.name,
                            variable=self.model_var).pack(anchor="w", padx=12)

    # ------------------------------------------------------------ 2. point d'acces

    def _page_ap(self) -> None:
        self.model = next(m for m in wifi_setup.MODELS if m.name == self.model_var.get())
        self.title.set(t("2. Open the device's access point"))
        self._text(t(self.model.ap_steps))
        self._text(t("Its Wi-Fi network then appears, named {prefix}... followed "
                     "by its MAC address.", prefix=self.model.ap_prefix))

    # ------------------------------------------------------------ 3. connexion du PC

    def _page_connect(self) -> None:
        self.title.set(t("3. Connect this PC to it"))
        self._text(t("Select the device's network and connect. You can also connect "
                     "from the Windows Wi-Fi menu: the assistant notices it by itself."))
        row = ttk.Frame(self.page)
        row.pack(fill="x")
        self.ap_list = tk.Listbox(row, height=5, exportselection=False)
        self.ap_list.pack(side="left", fill="x", expand=True)
        side = ttk.Frame(row)
        side.pack(side="left", padx=(8, 0), anchor="n")
        ttk.Button(side, text=t("Refresh"), command=self._list_aps).pack(fill="x")
        ttk.Button(side, text=t("Connect"), command=self._join_ap).pack(fill="x", pady=6)
        self.connect_state = tk.StringVar(self.window, value=t("Looking for the device..."))
        ttk.Label(self.page, textvariable=self.connect_state, wraplength=580,
                  justify="left").pack(anchor="w", pady=(12, 0))
        self.next_button.configure(state="disabled")
        self._list_aps()
        self.polling = True
        self._poll_device()

    def _list_aps(self) -> None:
        def done(networks) -> None:
            if isinstance(networks, Exception) or not hasattr(self, "ap_list"):
                return
            try:
                self.ap_list.delete(0, "end")
                listed = set()
                for ssid, percent, _channel in networks:
                    if ssid.startswith(self.model.ap_prefix) and ssid not in listed:
                        listed.add(ssid)
                        self.ap_list.insert("end", f"{ssid}   ({percent} %)")
                if self.ap_list.size():
                    self.ap_list.selection_set(0)
            except tk.TclError:
                pass  # page deja quittee
        self._off_thread(lambda: (wlan.scan(), wlan.visible_networks())[1], done)

    def _join_ap(self) -> None:
        selection = self.ap_list.curselection()
        if not selection:
            self.connect_state.set(t("No access point selected. Refresh once the "
                                     "outlets blink red."))
            return
        ssid = self.ap_list.get(selection[0]).split("   (")[0]
        self.connect_state.set(t("Connecting to {ssid}...", ssid=ssid))

        def done(ok) -> None:
            if ok is True:
                self.joined_ap = ssid
            else:
                self.connect_state.set(t("Windows refused the connection. Connect "
                                         "from its Wi-Fi menu instead."))
        self._off_thread(lambda: wlan.join_open_network(ssid), done)

    def _poll_device(self) -> None:
        """Tant que la page est ouverte : l'appareil repond-il a son adresse ?"""
        if not self.polling or self.closed:
            return

        def done(identity) -> None:
            if not self.polling:
                return
            if isinstance(identity, Exception) or identity is None:
                self._after(POLL_MS, self._poll_device)
                return
            self.identity = identity
            # Le nom exact du point d'acces : celui auquel le PC est connecte,
            # a defaut celui que forment le prefixe et la MAC.
            self._off_thread(wlan.connected_ssid, self._remember_ap)
            self.connect_state.set(t(
                "Connected to {model}, MAC {mac}, firmware {firmware}.",
                model=identity.model, mac=identity.mac, firmware=identity.firmware,
            ))
            self.next_button.configure(state="normal")
        self._off_thread(self.ap.identify, done)

    def _remember_ap(self, ssid) -> None:
        if isinstance(ssid, str) and ssid.startswith(self.model.ap_prefix):
            self.ap_ssid = ssid
        elif self.identity is not None:
            self.ap_ssid = f"{self.model.ap_prefix}{self.identity.mac.upper()}"

    # ------------------------------------------------------------ 4. reseau

    def _page_network(self) -> None:
        self.title.set(t("4. Choose the Wi-Fi network"))
        self._text(t(
            "Networks seen by this PC, on 2.4 GHz: the only band the device uses. "
            "The signal is measured by the PC, so place it close to where the "
            "device will stay. Prefer {limit} dBm or better (excellent): below "
            "-70 dBm the connection may drop.", limit=wifi_setup.RECOMMENDED_RSSI,
        ))
        columns = ("signal", "quality", "channel")
        self.networks = ttk.Treeview(self.page, columns=columns, height=6)
        self.networks.heading("#0", text=t("Network (SSID)"))
        self.networks.heading("signal", text=t("Signal"))
        self.networks.heading("quality", text=t("Quality"))
        self.networks.heading("channel", text=t("Channel"))
        self.networks.column("#0", width=250)
        for column, width in (("signal", 90), ("quality", 110), ("channel", 70)):
            self.networks.column(column, width=width, anchor="center")
        # Le code couleur du signal : vert excellent, orange bon, rouge mauvais.
        self.networks.tag_configure("excellent", foreground=self.palette.on)
        self.networks.tag_configure("good", foreground=self.palette.caution)
        self.networks.tag_configure("bad", foreground=self.palette.warn)
        self.networks.tag_configure("unseen", foreground=self.palette.text_muted)
        self.networks.pack(fill="x")
        self.networks.bind("<<TreeviewSelect>>", self._on_network)
        ttk.Button(self.page, text=t("Rescan"), command=self._scan_networks).pack(
            anchor="e", pady=(4, 0))

        form = ttk.Frame(self.page)
        form.pack(fill="x", pady=(12, 0))
        ttk.Label(form, text=t("Network (SSID)")).grid(row=0, column=0, sticky="w")
        self.ssid = getattr(self, "ssid", None) or tk.StringVar(self.window)
        ttk.Entry(form, textvariable=self.ssid, width=34).grid(
            row=0, column=1, sticky="w", padx=8, pady=2)
        ttk.Label(form, text=t("Password")).grid(row=1, column=0, sticky="w")
        self.password = getattr(self, "password", None) or tk.StringVar(self.window)
        self.password_entry = ttk.Entry(form, textvariable=self.password, width=34, show="•")
        self.password_entry.grid(row=1, column=1, sticky="w", padx=8, pady=2)
        self.show_password = tk.BooleanVar(self.window, value=False)
        ttk.Checkbutton(
            form, text=t("Show"), variable=self.show_password,
            command=lambda: self.password_entry.configure(
                show="" if self.show_password.get() else "•"),
        ).grid(row=1, column=2, sticky="w")
        self.network_hint = tk.StringVar(self.window, value="")
        ttk.Label(self.page, textvariable=self.network_hint, style="Hint.TLabel",
                  wraplength=580, justify="left").pack(anchor="w", pady=(6, 0))
        self.next_button.configure(text=t("Send to the device"))
        self._scan_networks()

    def _scan_networks(self) -> None:
        """Scan frais du PC -- quelques secondes -- puis la liste."""
        self.network_list = []
        self.networks.delete(*self.networks.get_children())
        self.network_hint.set(t("Scanning the Wi-Fi networks from this PC..."))
        self._off_thread(wifi_setup.candidate_networks, self._show_networks)

    def _show_networks(self, networks) -> None:
        if isinstance(networks, Exception):
            networks = []
        self.network_list = networks
        self.network_hint.set("")
        for index, network in enumerate(networks):
            if network.seen_on_24ghz:
                values = (f"{network.rssi} dBm", wifi_setup.signal_quality(network.rssi),
                          network.channel or "-")
                tags = (wifi_setup.signal_level(network.rssi),)
            else:
                values = ("-", t("5 GHz only"), "-")
                tags = ("unseen",)
            self.networks.insert("", "end", iid=str(index), text=network.ssid,
                                 values=values, tags=tags)
        if not networks:
            self.network_hint.set(t("This PC sees no 2.4 GHz network: type the name "
                                    "of yours."))

    def _on_network(self, _event) -> None:
        selection = self.networks.selection()
        if not selection:
            return
        network = self.network_list[int(selection[0])]
        self.ssid.set(network.ssid)
        if not network.seen_on_24ghz:
            self.network_hint.set(t(
                "This PC only sees this network on 5 GHz, which the device cannot "
                "use. A dual-band router often uses the same name on 2.4 GHz: try "
                "it, the device will tell."))
        elif network.rssi < wifi_setup.RECOMMENDED_RSSI:
            self.network_hint.set(t(
                "Signal below {limit} dBm: the device may lose the connection. "
                "Consider a closer access point.", limit=wifi_setup.RECOMMENDED_RSSI))
        else:
            self.network_hint.set("")

    # ------------------------------------------------------------ 5. envoi

    def _page_send(self) -> None:
        ssid, password = self.ssid.get().strip(), self.password.get()
        if not ssid:
            self.step -= 1
            self._show()
            self.network_hint.set(t("Choose or type a network first."))
            return
        self.title.set(t("5. Connecting the device"))
        self.progress = tk.StringVar(self.window, value="")
        ttk.Label(self.page, textvariable=self.progress, wraplength=580,
                  justify="left").pack(anchor="w")
        self.lines: list[str] = []
        self.next_button.configure(state="disabled", text=t("Finish"))
        self.back_button.configure(state="disabled")
        self._say(t("Sending the Wi-Fi settings for {ssid}...", ssid=ssid))

        def work():
            reply = self.ap.send(ssid, password)
            self._after(0, lambda: self._say(t("Settings accepted by the device.")))
            self._after(0, lambda: self._say(t("Waiting for the device to join {ssid}...",
                                               ssid=ssid)))
            return reply, self.ap.wait_joined(
                self.identity, rejoin_ap=self._rejoin_ap, on_ap_gone=self._ap_gone
            )

        self._off_thread(work, lambda result: self._sent(ssid, result))

    def _say(self, line: str) -> None:
        self.lines.append(line)
        self.progress.set("\n".join(self.lines))

    def _rejoin_ap(self) -> None:
        """Ramene la carte Wi-Fi du PC sur le point d'acces de l'appareil.

        En rejoignant le reseau, l'appareil coupe son point d'acces un
        instant, et Windows ne s'y reconnecte pas de lui-meme. Appele depuis
        le fil de l'attente, a chaque silence.
        """
        if self.ap_ssid and wlan.connected_ssid() != self.ap_ssid:
            if not getattr(self, "_rejoin_said", False):
                self._rejoin_said = True
                self._after(0, lambda: self._say(t(
                    "The device's access point dropped: reconnecting this PC to it...")))
            wlan.reconnect(self.ap_ssid)

    def _ap_gone(self) -> None:
        """Le point d'acces ne revient pas : on cherche l'appareil sur le reseau.

        Un PC en Wi-Fi seul doit d'abord retrouver le sien pour le voir.
        """
        self._after(0, lambda: self._say(t(
            "The device closed its access point: looking for it on your network...")))
        if self.joined_ap or self.previous_ssid:
            self._leave_ap()

    def _sent(self, ssid: str, result) -> None:
        if isinstance(result, Exception):
            self._say(t("The device did not accept the settings: {error}", error=result))
            self._retry()
            return
        _reply, joined = result
        if not joined.ok:
            if joined.status == "access point closed":
                self._say(t("The device closed its access point but was not found on "
                            "your network. It may still be joining: scan the network "
                            "in a moment. Otherwise, check the password."))
            else:
                self._say(t("The device did not join {ssid} (last status: {status}). "
                            "Check the password, then try again.",
                            ssid=ssid, status=joined.status))
            self._retry()
            return
        quality = (t(", signal {rssi} dBm ({quality})", rssi=joined.rssi,
                     quality=wifi_setup.signal_quality(joined.rssi))
                   if joined.rssi is not None else "")
        self._say(t("Joined {ssid}: address {ip}{quality}.",
                    ssid=ssid, ip=joined.ip, quality=quality))
        self._say(t("Giving this PC its Wi-Fi back..."))
        mac = self.identity.mac if self.identity else ""

        def restore() -> None:
            self._leave_ap()
            self._after(0, lambda: self._finish(mac, joined.ip))
        threading.Thread(target=restore, daemon=True).start()

    def _retry(self) -> None:
        """Retour a l'etape du reseau : le mot de passe est souvent en cause."""
        self.back_button.configure(state="normal")
        self.step = 4  # « Retour » ramene a l'etape 4

    def _finish(self, mac: str, ip: str) -> None:
        self._say(t("Done. Looking for the device on your network..."))
        self.next_button.configure(state="normal", command=lambda: self._close(mac, ip))
        self._after(1500, lambda: self._close(mac, ip))

    # ------------------------------------------------------------ fermeture

    def _leave_ap(self) -> None:
        """Quitte le point d'acces et rend au PC le Wi-Fi d'avant.

        Le profil Windows n'est efface que si l'assistant l'a cree : un
        point d'acces rejoint a la main garde le sien.
        """
        if self.joined_ap:
            wlan.forget(self.joined_ap)
            self.joined_ap = None
        elif (wlan.connected_ssid() or "").startswith(self.model.ap_prefix):
            wlan.disconnect()
        if self.previous_ssid:
            wlan.reconnect(self.previous_ssid)

    def _close(self, mac: str = "", ip: str = "") -> None:
        if self.closed:
            return
        self.closed = True
        self.polling = False
        try:
            self.window.grab_release()
            self.window.destroy()
        except tk.TclError:
            pass
        if ip:
            self.on_done(mac, ip)

    def _cancel(self) -> None:
        threading.Thread(target=self._leave_ap, daemon=True).start()
        self._close()
