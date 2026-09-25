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
"""

from __future__ import annotations

import ctypes
import threading
import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING

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
            button.bind("<Enter>", lambda _e, b=button: b.focus_set())
            self.buttons.append(button)
            if index < 9:
                root.bind(str(index + 1), lambda _e, name=profile.name: self.choose(name))
        ttk.Label(
            body, text=t("Arrows and Enter to choose, Esc to close"), style="Hint.TLabel",
            anchor="center",
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
        self._select(self.start)

    # ------------------------------------------------------------- clavier

    def _select(self, index: int) -> str:
        if self.buttons:
            self.buttons[index % len(self.buttons)].focus_set()
        return "break"

    def _move(self, step: int) -> str:
        """Selection suivante ou precedente, en bouclant aux extremites."""
        focused = self.root.focus_get()
        index = self.buttons.index(focused) if focused in self.buttons else self.start - step
        return self._select(index + step)

    def _invoke(self, _event) -> str:
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
