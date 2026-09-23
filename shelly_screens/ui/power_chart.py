"""Courbe de consommation, pour placer les seuils a l'oeil.

Choisir un seuil sur deux nombres est un pari ; le choisir sur la courbe
montre tout de suite si la marge est confortable ou si l'on frole un
palier. Les deux seuils sont donc des lignes que l'on attrape a la souris.

L'axe des puissances est logarithmique. Une echelle lineaire ecraserait
contre zero tout ce qui compte : la veille et l'arret se jouent entre un
demi-watt et cinq watts, quand la marche depasse la centaine.
"""

from __future__ import annotations

import math
import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING

from .. import sensing
from ..i18n import t

if TYPE_CHECKING:
    from .settings import SettingsWindow

# Marges autour du trace, en pixels.
LEFT = 62
RIGHT = 18
TOP = 16
BOTTOM = 34
# Bornes de l'echelle verticale, en watts.
MIN_W = 0.1
MAX_W = sensing.PROBE_SERIES_MAX_W
# Distance a laquelle une ligne de seuil se laisse attraper.
GRAB_PX = 7


def _ago(age_s: float) -> str:
    """Age lisible : des heures au-dela de quatre-vingt-dix minutes."""
    minutes = age_s / 60.0
    if minutes < 90:
        return t("-{min} min", min=f"{minutes:.0f}")
    return t("-{hours} h", hours=f"{minutes / 60:.1f}")


class PowerChartDialog:
    """Fenetre de trace, avec seuils reglables a la souris."""

    def __init__(self, parent: tk.Tk, owner: "SettingsWindow") -> None:
        self.owner = owner
        self.app = owner.app
        self.config = owner.config
        self.series: list[sensing.Tick] = []
        self.dragging: str | None = None

        sensing_config = self.config.sensing
        self.on_w = float(sensing_config.on_threshold_w)
        self.off_w = float(sensing_config.off_threshold_w)

        self.window = tk.Toplevel(parent)
        self.window.title(t("Power over time"))
        self.window.geometry("860x520")
        self.window.minsize(640, 420)
        self.window.transient(parent)

        from .settings import _theme_dialog

        _theme_dialog(self.window, owner.palette)
        self.palette = owner.palette

        ttk.Label(
            self.window,
            text=t(
                "Drag either line to set a threshold. The upper one marks the "
                "PC as running, the lower one as off; between them nothing "
                "changes, which is what keeps the relays from chattering."
            ),
            wraplength=820,
            justify="left",
            padding=12,
        ).pack(anchor="w")

        self.canvas = tk.Canvas(
            self.window, background=self.palette.surface, highlightthickness=0
        )
        self.canvas.pack(fill="both", expand=True, padx=12)
        self.canvas.bind("<Configure>", lambda _e: self.draw())
        self.canvas.bind("<Button-1>", self._grab)
        self.canvas.bind("<B1-Motion>", self._move)
        self.canvas.bind("<ButtonRelease-1>", lambda _e: self._drop())
        self.canvas.bind("<Motion>", self._hover)

        self.status = tk.StringVar(value="")
        ttk.Label(self.window, textvariable=self.status, padding=(12, 6)).pack(anchor="w")

        buttons = ttk.Frame(self.window, padding=12)
        buttons.pack(fill="x", side="bottom")
        ttk.Button(buttons, text=t("Refresh"), command=self.reload).pack(side="left")
        ttk.Button(buttons, text=t("Apply these thresholds"), command=self._apply).pack(
            side="left", padx=8
        )
        ttk.Button(buttons, text=t("Close"), command=self._close).pack(side="right")

        self.reload()

    # ------------------------------------------------------------- donnees

    def reload(self) -> None:
        """Relit la courbe sur l'appareil, sans figer l'interface."""
        self.status.set(t("Reading the measurement..."))

        def work():
            return sensing.read_series(self.app.controller, self.config)

        def done(series, error) -> None:
            if error is not None:
                self.status.set(t("Cannot read the measurement: {error}", error=error))
                return
            self.series = series or []
            self.draw()

        from .settings import _run_off_thread

        _run_off_thread(self.window, work, done)

    # --------------------------------------------------------------- trace

    def _plot_area(self) -> tuple[int, int, int, int]:
        width = max(self.canvas.winfo_width(), 200)
        height = max(self.canvas.winfo_height(), 150)
        return LEFT, TOP, width - RIGHT, height - BOTTOM

    def _y_of(self, watts: float) -> float:
        """Ordonnee d'une puissance, sur une echelle logarithmique."""
        _x0, y0, _x1, y1 = self._plot_area()
        value = max(MIN_W, min(MAX_W, watts))
        span = math.log(MAX_W / MIN_W)
        ratio = math.log(value / MIN_W) / span
        return y1 - ratio * (y1 - y0)

    def _watts_of(self, y: float) -> float:
        """Puissance correspondant a une ordonnee."""
        _x0, y0, _x1, y1 = self._plot_area()
        ratio = (y1 - y) / max(1.0, (y1 - y0))
        ratio = max(0.0, min(1.0, ratio))
        return MIN_W * math.exp(ratio * math.log(MAX_W / MIN_W))

    def draw(self) -> None:
        """Retrace tout : grille, courbe, seuils."""
        canvas = self.canvas
        palette = self.palette
        canvas.delete("all")
        x0, y0, x1, y1 = self._plot_area()
        if x1 <= x0 or y1 <= y0:
            return

        canvas.create_rectangle(
            x0, y0, x1, y1, outline=palette.border, fill=palette.bg
        )

        # Graduations choisies pour l'echelle logarithmique : elles se
        # resserrent la ou la lecture compte, sous une dizaine de watts.
        for watts in (0.5, 1, 2, 5, 10, 20, 50, 100, 200, 400):
            y = self._y_of(watts)
            if not (y0 <= y <= y1):
                continue
            canvas.create_line(x0, y, x1, y, fill=palette.border)
            canvas.create_text(
                x0 - 8, y, anchor="e", fill=palette.text_muted,
                text=f"{watts:g} W", font=("", 8),
            )

        if not self.series:
            canvas.create_text(
                (x0 + x1) / 2, (y0 + y1) / 2, fill=palette.text_muted,
                text=t("No measurement yet."),
            )
            self._draw_thresholds()
            return

        # Axe du temps : de gauche (le plus ancien) a droite (maintenant).
        # Les ticks ne sont pas regulierement espaces -- ils marquent les
        # changements -- donc l'abscisse suit l'age reel, pas le rang.
        oldest = max(tick.age_s for tick in self.series)

        # Un seul tick, ou plusieurs au meme instant : la puissance n'a pas
        # bouge depuis le debut de la mesure. Rapporte a un axe de largeur
        # nulle, tout se tasserait sur le bord droit et la fenetre
        # paraitrait vide -- alors que la mesure fonctionne et dit
        # precisement que rien ne varie. On l'ecrit en toutes lettres.
        if oldest <= 0:
            watts = self.series[-1].watts
            y = self._y_of(watts)
            canvas.create_line(x0, y, x1, y, fill=palette.accent, width=2)
            canvas.create_oval(
                x1 - 3.5, y - 3.5, x1 + 3.5, y + 3.5,
                fill=palette.accent, outline=palette.accent,
            )
            self._draw_thresholds()
            self.status.set(
                t(
                    "Power steady at {watts} W since the measurement started. "
                    "A point is added when it moves, or every {minutes} min.",
                    watts=f"{watts:.1f}",
                    minutes=f"{sensing.PROBE_TICK_MAX_SILENCE * sensing.PROBE_POLL_S / 60:.0f}",
                )
            )
            return

        span = max(oldest, 1.0)

        def x_of(age_s: float) -> float:
            return x1 - (x1 - x0) * (age_s / span)

        for fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
            age = span * fraction
            # Centres sur leur graduation, les libelles des deux bords
            # debordaient du cadre : « maintenant » s'y trouvait coupe en
            # « maintena ». On les accroche vers l'interieur.
            ancrage = "e" if fraction == 0.0 else ("w" if fraction == 1.0 else "center")
            canvas.create_text(
                x_of(age), y1 + 12, fill=palette.text_muted, font=("", 8),
                text=t("now") if age < 60 else _ago(age), anchor=ancrage,
            )

        # Trace en escalier : entre deux ticks la puissance n'a pas bouge,
        # c'est tout le sens d'un enregistrement par evenement. Une ligne
        # oblique laisserait croire a une transition progressive.
        points: list[float] = []
        previous_y = None
        for tick in self.series:
            x = x_of(tick.age_s)
            y = self._y_of(tick.watts)
            if previous_y is not None:
                points.append(x)
                points.append(previous_y)
            points.append(x)
            points.append(y)
            previous_y = y
        if previous_y is not None:
            points.append(x1)
            points.append(previous_y)

        if len(points) >= 4:
            canvas.create_line(*points, fill=palette.accent, width=2)
        for tick in self.series:
            x, y = x_of(tick.age_s), self._y_of(tick.watts)
            canvas.create_oval(
                x - 2.5, y - 2.5, x + 2.5, y + 2.5,
                fill=palette.accent, outline=palette.accent,
            )

        self._draw_thresholds()
        watts = [tick.watts for tick in self.series]
        self.status.set(
            t(
                "{count} ticks over {span}, from {low} W to {high} W.",
                count=len(self.series),
                span=_ago(oldest).lstrip("-"),
                low=f"{min(watts):.1f}",
                high=f"{max(watts):.1f}",
            )
        )

    def _draw_thresholds(self) -> None:
        """Les deux lignes reglables, et la zone morte entre elles."""
        canvas = self.canvas
        palette = self.palette
        x0, _y0, x1, _y1 = self._plot_area()
        y_on = self._y_of(self.on_w)
        y_off = self._y_of(self.off_w)

        # La zone morte est le coeur du reglage : l'etat courant s'y
        # maintient, et c'est elle qui empeche le relais de claquer.
        canvas.create_rectangle(
            x0 + 1, min(y_on, y_off), x1 - 1, max(y_on, y_off),
            fill=palette.surface_alt, outline="", stipple="gray25", tags="band",
        )
        for name, y, watts, colour in (
            ("on", y_on, self.on_w, palette.on),
            ("off", y_off, self.off_w, palette.warn),
        ):
            canvas.create_line(x0, y, x1, y, fill=colour, width=2, tags=name)
            canvas.create_text(
                x1 - 6, y - 9, anchor="e", fill=colour, font=("", 9, "bold"),
                text=t("{label}: {watts} W",
                       label=t("running above") if name == "on" else t("off below"),
                       watts=f"{watts:.1f}"),
                tags=name,
            )

    # ------------------------------------------------------------ souris

    def _nearest(self, y: float) -> str | None:
        """Nom de la ligne sous le curseur, s'il y en a une."""
        if abs(y - self._y_of(self.on_w)) <= GRAB_PX:
            return "on"
        if abs(y - self._y_of(self.off_w)) <= GRAB_PX:
            return "off"
        return None

    def _hover(self, event) -> None:
        self.canvas.configure(cursor="sb_v_double_arrow" if self._nearest(event.y) else "")

    def _grab(self, event) -> None:
        self.dragging = self._nearest(event.y)

    def _move(self, event) -> None:
        if self.dragging is None:
            return
        watts = round(self._watts_of(event.y), 1)
        # Les deux seuils ne se croisent pas : sans ecart, la zone morte
        # disparaitrait et le relais se remettrait a claquer.
        if self.dragging == "on":
            self.on_w = max(watts, self.off_w + 1.0)
        else:
            self.off_w = min(watts, self.on_w - 1.0)
        self.draw()

    def _drop(self) -> None:
        self.dragging = None
        self.canvas.configure(cursor="")

    # ------------------------------------------------------------ actions

    def _apply(self) -> None:
        self.config.sensing.on_threshold_w = round(self.on_w, 1)
        self.config.sensing.off_threshold_w = round(self.off_w, 1)
        self.owner._save()
        self.owner.refresh_sensing()
        self.status.set(
            t(
                "Thresholds set to {on} / {off} W. Reinstall the script to "
                "apply them on the device.",
                on=f"{self.on_w:.1f}",
                off=f"{self.off_w:.1f}",
            )
        )

    def _close(self) -> None:
        try:
            self.window.destroy()
        except tk.TclError:
            pass
