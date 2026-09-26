"""Petite fenetre de choix des profils, appelee par le raccourci global.

Un bouton par profil ; un clic applique le profil, sans confirmation, et
la fenetre se ferme. Elle s'ouvre au premier plan, au centre de l'ecran
principal -- celui que Windows designe comme tel, ou apparaissent aussi
ses propres dialogues.

Pensee pour le clavier autant que pour la souris : les fleches deplacent
la selection, Entree applique, les chiffres 1 a 9 appliquent directement
les neuf premiers profils, Echap ferme, et le raccourci presse une seconde
fois aussi. Le survol a la souris deplace la meme selection : une seule
surbrillance, jamais deux qui se contredisent. Elle se ferme d'elle-meme quand on clique ailleurs :
un choix rapide qui trainerait a l'ecran deviendrait un encombrement.

Sous les boutons, le plan des ecrans, dessine comme dans les reglages. A
l'ouverture, il montre l'etat reel ; des que la selection bouge, il montre
ce que donnerait le profil selectionne. Un clic sur un ecran part de ce qui
est affiche et bascule son etat prevu, sans rien commuter : c'est Entree,
ou le bouton Appliquer, qui met la selection en oeuvre. C'est toujours une
configuration ponctuelle, hors profils : les clics ne modifient aucun
profil, et aucun profil ne devient « en cours ». Changer de selection
abandonne les clics : le plan montre toujours ce qui arrivera si l'on valide.
"""

from __future__ import annotations

import ctypes
import threading
import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING

from . import screen_map
from . import theme as theme_module
from ..i18n import t
from ..win import icon as icon_module
from ..win import monitors

if TYPE_CHECKING:
    from ..app import Application

_state_lock = threading.Lock()
_is_open = False
_current: "ProfilePicker | None" = None

# Largeur des boutons, en caracteres : assez pour un nom de profil
# ordinaire, sans que la fenetre s'etale.
BUTTON_WIDTH = 26
MAP_HEIGHT = 120  # le plan des ecrans, sous les boutons


def toggle_picker(application: "Application") -> None:
    """Ouvre la fenetre, ou la ferme si elle l'est deja.

    Appele depuis le thread des messages Windows : ne doit rien bloquer.
    """
    global _is_open
    with _state_lock:
        if _is_open:
            picker = _current
            if picker is not None:
                try:
                    picker.root.after(0, picker.close)
                except Exception:  # noqa: BLE001 - fenetre en cours de fermeture
                    pass
            return
        _is_open = True

    def run() -> None:
        global _is_open, _current
        try:
            root = tk.Tk()
            _current = ProfilePicker(root, application)
            root.mainloop()
        except Exception as exc:  # noqa: BLE001 - une UI ratee ne doit pas tuer l'appli
            application.log(f"Profile picker failed: {exc}")
        finally:
            with _state_lock:
                _is_open = False
                _current = None

    threading.Thread(target=run, name="picker-ui", daemon=True).start()


class ProfilePicker:
    """Les boutons des profils, et rien d'autre."""

    def __init__(self, root: tk.Tk, application: "Application") -> None:
        self.root = root
        self.app = application
        self.closing = False
        config = application.config

        root.withdraw()  # construite hors champ, montree une fois placee
        root.title(t("Profiles"))
        icon_module.apply_to_window(root)
        root.resizable(False, False)
        # Fenetre d'outil : pas de bouton dans la barre des taches pour une
        # boite qui ne vit que quelques secondes.
        root.attributes("-toolwindow", True)
        palette = theme_module.apply(root, config.settings.theme)
        self.palette = palette
        root.configure(background=palette.bg)
        theme_module.apply_titlebar(root, palette.dark)

        style = ttk.Style(root)
        # La selection prend l'accent, comme dans un menu : c'est elle que
        # l'on suit des yeux en naviguant aux fleches. Le focus passe avant
        # le survol, pour que le bouton choisi reste marque sous la souris.
        hover = theme_module._mix(palette.surface_alt, palette.accent, 0.25)
        style.map(
            "Picker.TButton",
            background=[("focus", palette.accent), ("active", hover)],
            foreground=[("focus", palette.accent_text)],
            lightcolor=[("focus", palette.accent)],
            darkcolor=[("focus", palette.accent)],
        )

        body = ttk.Frame(root, padding=14)
        body.pack(fill="both", expand=True)
        profiles = config.sorted_profiles()
        self.profiles = profiles
        current = config.settings.last_profile
        self.buttons: list[ttk.Button] = []
        # Le profil en cours : la selection de depart, et une coche. Il
        # reste cliquable -- le reappliquer sert, apres un ecran rallume a
        # la main.
        self.start = 0
        if not profiles:
            ttk.Label(body, text=t("No profile configured")).pack(padx=20, pady=10)
        for index, profile in enumerate(profiles):
            label = f"{index + 1}   {profile.label}" if index < 9 else f"     {profile.label}"
            if profile.name == current:
                label += "   ✓"
                self.start = index
            button = ttk.Button(
                body,
                text=label,
                width=BUTTON_WIDTH,
                style="Picker.TButton",
                command=lambda name=profile.name: self.choose(name),
            )
            button.pack(fill="x", pady=3)
            button.bind("<Enter>", lambda _e, i=index: self._hover(i))
            self.buttons.append(button)
            if index < 9:
                root.bind(str(index + 1), lambda _e, name=profile.name: self.choose(name))
        self._build_map(body)
        self.hint = tk.StringVar(root, value=t("Arrows and Enter to choose, Esc to close"))
        ttk.Label(
            body, textvariable=self.hint, style="Hint.TLabel", anchor="center",
        ).pack(fill="x", pady=(8, 0))

        root.bind("<Escape>", lambda _e: self.close())
        for key, step in (("<Up>", -1), ("<Left>", -1), ("<Down>", 1), ("<Right>", 1)):
            root.bind(key, lambda _e, step=step: self._move(step))
        root.bind("<Home>", lambda _e: self._select(0))
        root.bind("<End>", lambda _e: self._select(len(self.buttons) - 1))
        root.bind("<Return>", self._invoke)
        root.bind("<KP_Enter>", self._invoke)
        root.bind("<FocusOut>", self._focus_out)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._show()

    # ------------------------------------------------------------- affichage

    def _show(self) -> None:
        """Centre sur l'ecran principal, puis passe au premier plan."""
        root = self.root
        root.update_idletasks()
        width, height = root.winfo_reqwidth(), root.winfo_reqheight()
        left, top, right, bottom = _primary_work_area(root)
        x = left + (right - left - width) // 2
        y = top + (bottom - top - height) // 2
        root.geometry(f"+{x}+{y}")
        root.deiconify()
        root.attributes("-topmost", True)
        root.lift()
        root.update_idletasks()
        # Tk seul ne suffit pas : Windows refuse le premier plan a qui ne
        # l'a pas demande. L'appui sur le raccourci nous en donne le droit,
        # encore faut-il le reclamer pour la bonne fenetre -- le cadre que
        # Windows connait, pas le widget interieur de Tk.
        try:
            ctypes.windll.user32.SetForegroundWindow(int(root.wm_frame(), 16))
        except (ValueError, OSError):
            pass
        root.focus_force()
        # A l'ouverture, le plan garde l'etat reel : la previsualisation ne
        # commence qu'au premier mouvement de la selection.
        self._select(self.start, preview=False)

    # ------------------------------------------------------------- clavier

    def _select(self, index: int, preview: bool = True) -> str:
        if self.buttons:
            index %= len(self.buttons)
            self.buttons[index].focus_set()
            if preview:
                self._preview(self.profiles[index])
        return "break"

    def _hover(self, index: int) -> None:
        """Le survol deplace la selection -- sans effacer les clics s'il n'y a
        pas de changement : repasser sur le bouton deja choisi ne dit rien."""
        if self.root.focus_get() is not self.buttons[index]:
            self._select(index)

    def _move(self, step: int) -> str:
        """Selection suivante ou precedente, en bouclant aux extremites."""
        focused = self.root.focus_get()
        index = self.buttons.index(focused) if focused in self.buttons else self.start - step
        return self._select(index + step)

    def _invoke(self, _event) -> str:
        # Des ecrans ont ete bascules sur le plan : Entree valide ce choix-la.
        if self._changed():
            self.apply_selection()
            return "break"
        focused = self.root.focus_get()
        if focused in self.buttons:
            focused.invoke()
        return "break"

    def _focus_out(self, _event) -> None:
        # Le focus passe d'un bouton a l'autre sans quitter la fenetre ; on
        # ne ferme que s'il a quitte l'application.
        self.root.after(150, self._close_if_inactive)

    def _close_if_inactive(self) -> None:
        if self.closing:
            return
        try:
            if self.root.focus_get() is None:
                self.close()
        except (tk.TclError, KeyError):
            # `focus_get` echoue quand le focus est sur une fenetre d'un
            # autre programme : la fenetre n'est plus active.
            self.close()

    # --------------------------------------------------------------- plan

    def _build_map(self, body: ttk.Frame) -> None:
        """Le plan des ecrans et son bouton Appliquer, s'il y a une disposition."""
        config = self.app.config
        # L'etat reel des prises d'ecran : le point de depart des clics.
        self.actual = {
            o.ref: bool(self.app.states[o.ref].output)
            for o in config.outlets
            if o.monitor_key and o.ref in self.app.states
        }
        self.pending = dict(self.actual)
        # Vrai une fois des ecrans bascules a la main : Entree valide alors
        # ces clics, et non le profil selectionne.
        self.edited = False
        self.map = None
        self.apply_button = None
        if not config.screens:
            return
        self.map = screen_map.ScreenMap(
            body, palette=lambda: self.palette, on_toggle=self._toggle,
            empty_text="", compact=True, height=MAP_HEIGHT,
        )
        self.map.canvas.pack(fill="x", pady=(10, 4))
        self.apply_button = ttk.Button(
            body, text=t("Apply"), command=self.apply_selection, state="disabled"
        )
        self.apply_button.pack(fill="x")
        self._draw_map()

    def _draw_map(self) -> None:
        if self.map is None:
            return
        self.map.show(screen_map.build_tiles(
            self.app.config, lambda ref: self.pending.get(ref), editable=True,
        ))

    def _preview(self, profile) -> None:
        """Montre ce que donnerait ce profil ; abandonne les clics en cours."""
        if self.map is None:
            return
        config = self.app.config
        self.pending = {
            ref: profile.wants(ref) or config.outlet(ref).never_switch_off
            for ref in self.actual
        }
        self.edited = False
        self._refresh_controls()

    def _toggle(self, ref: str) -> None:
        """Bascule l'etat prevu d'un ecran : rien n'est encore commute."""
        if ref not in self.pending:
            return  # appareil muet : on ne sait pas ce qu'on changerait
        self.pending[ref] = not self.pending[ref]
        self.edited = True
        self._refresh_controls()

    def _refresh_controls(self) -> None:
        changed = self._changed()
        self.apply_button.configure(state="normal" if changed else "disabled")
        self.hint.set(
            t("Enter or Apply to switch the screens, Esc to cancel")
            if changed else t("Arrows and Enter to choose, Esc to close")
        )
        self._draw_map()

    def _changed(self) -> bool:
        """Des clics a valider, et qui changeraient quelque chose."""
        return self.edited and self.pending != self.actual

    def apply_selection(self) -> None:
        """Met en oeuvre les ecrans bascules sur le plan, puis ferme.

        Une configuration ponctuelle, meme si elle ressemble a un profil :
        choisir sur le plan, c'est vouloir autre chose que les profils. On
        ne commute que les ecrans qui changent ; le reste ne bouge pas.
        """
        if self.closing or not self._changed():
            return
        config = self.app.config
        changes = {ref: on for ref, on in self.pending.items() if on != self.actual[ref]}
        self.app.log(
            "Screen selection from the keyboard shortcut: "
            + ", ".join(f"{config.outlet(r).label} {'on' if on else 'off'}"
                        for r, on in changes.items())
        )
        self.app.apply_outlets(changes)
        self.close()

    # --------------------------------------------------------------- actions

    def choose(self, name: str) -> None:
        """Applique le profil et ferme, sans rien demander."""
        if self.closing:
            return
        self.app.log(f"Profile '{name}' chosen from the keyboard shortcut")
        # L'application se charge du reseau dans son propre thread : la
        # fenetre peut se fermer sans attendre les appareils.
        self.app.apply_profile(name)
        self.close()

    def close(self) -> None:
        self.closing = True
        try:
            self.root.destroy()
        except tk.TclError:
            pass


def _primary_work_area(root: tk.Misc) -> tuple[int, int, int, int]:
    """Zone utile de l'ecran principal, barre des taches exclue."""
    for monitor in monitors.list_monitors():
        if monitor.is_primary:
            return monitor.work_rect
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()
