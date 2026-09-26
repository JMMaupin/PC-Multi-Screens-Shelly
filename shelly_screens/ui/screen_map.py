"""Plan des ecrans, disposes comme Windows les place sur le bureau.

Un profil se lit mieux sur un dessin que dans une liste de cases : on voit
d'un coup d'oeil quels ecrans restent allumes, et ou. Chaque ecran porte le
nom de la prise qui l'alimente ; un clic sur un ecran allume ou coupe sa
prise dans le profil, comme la case correspondante.

Chaque ecran est dessine a sa taille physique : la diagonale que son EDID
annonce, dans les proportions de sa definition. Un 27 pouces 4K et un
27 pouces QHD ont la meme taille sur le plan, comme sur le bureau. Un ecran
dont l'EDID ne dit rien garde sa taille effective -- sa definition divisee
par l'echelle reglee dans Windows --, convertie a 96 points par pouce, la
densite que Windows suppose a 100 %.

Les coordonnees du bureau, elles, sont en pixels : une fois chaque ecran
ramene a sa taille reelle, elles ne se recollent plus. Le plan est donc
reconstruit de proche en proche, a partir de l'ecran principal, en suivant
les bords que les ecrans partagent.
"""

from __future__ import annotations

import math
import tkinter as tk
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from ..i18n import t
from .theme import Palette

if TYPE_CHECKING:
    from ..config import AppConfig

STATE_ON = "on"  # alimente par le profil
STATE_OFF = "off"  # coupe par le profil
STATE_FIXED = "fixed"  # prise protegee : toujours alimente
STATE_UNMANAGED = "unmanaged"  # aucune prise associee
STATE_GHOST = "ghost"  # coupe, mais Windows garde l'ecran sur le bureau

MARGIN_PX = 14
GAP_PX = 3  # espace entre deux ecrans jointifs, pour les distinguer
EDGE_TOLERANCE_PX = 16  # deux bords plus proches sont jointifs
REFERENCE_MM_PER_PX = 25.4 / 96  # un pixel a 100 %, selon Windows

Box = tuple[float, float, float, float]


@dataclass(frozen=True)
class ScreenTile:
    """Un ecran a dessiner."""

    rect: tuple[int, int, int, int]  # coordonnees du bureau
    title: str
    detail: str
    state: str
    ref: str | None = None  # prise a basculer d'un clic, s'il y en a une
    scale: float = 1.0  # echelle reglee dans Windows
    primary: bool = False
    diagonal: float = 0.0  # pouces, selon l'EDID ; 0 si inconnue


def mm_per_pixel(tile: ScreenTile) -> float:
    """Taille reelle d'un pixel de cet ecran, en millimetres."""
    width = tile.rect[2] - tile.rect[0]
    height = tile.rect[3] - tile.rect[1]
    if tile.diagonal > 0 and width > 0 and height > 0:
        return tile.diagonal * 25.4 / math.hypot(width, height)
    return REFERENCE_MM_PER_PX / (tile.scale or 1.0)


def physical_layout(tiles: list[ScreenTile]) -> list[Box]:
    """Place chaque ecran a sa taille reelle, en millimetres, sans rompre les contacts.

    On part de l'ecran principal et l'on pose ses voisins un a un : un
    ecran colle a droite d'un autre commence la ou celui-ci finit, et son
    decalage le long du bord commun se convertit dans les pixels de l'ecran
    deja pose. Un ecran qui ne touche aucun autre -- Windows ne le permet
    pas, mais une configuration editee a la main si -- garde sa place.
    """
    tol = EDGE_TOLERANCE_PX
    factors = [mm_per_pixel(tile) for tile in tiles]

    def size(index: int) -> tuple[float, float]:
        rect = tiles[index].rect
        return (rect[2] - rect[0]) * factors[index], (rect[3] - rect[1]) * factors[index]

    def overlap(a0, a1, b0, b1) -> bool:
        return min(a1, b1) - max(a0, b0) > tol

    placed: dict[int, Box] = {}
    start = next((i for i, tile in enumerate(tiles) if tile.primary), 0)
    width, height = size(start)
    x, y = tiles[start].rect[0] * factors[start], tiles[start].rect[1] * factors[start]
    placed[start] = (x, y, x + width, y + height)
    queue = [start]
    while queue:
        index = queue.pop(0)
        a = tiles[index].rect
        ax0, ay0, ax1, ay1 = placed[index]
        a_factor = factors[index]
        for other, tile in enumerate(tiles):
            if other in placed:
                continue
            b = tile.rect
            width, height = size(other)
            # Decalages le long du bord commun, dans les pixels de l'ecran pose.
            shift_y = ay0 + (b[1] - a[1]) * a_factor
            shift_x = ax0 + (b[0] - a[0]) * a_factor
            if abs(b[0] - a[2]) <= tol and overlap(a[1], a[3], b[1], b[3]):
                box = (ax1, shift_y, ax1 + width, shift_y + height)
            elif abs(b[2] - a[0]) <= tol and overlap(a[1], a[3], b[1], b[3]):
                box = (ax0 - width, shift_y, ax0, shift_y + height)
            elif abs(b[1] - a[3]) <= tol and overlap(a[0], a[2], b[0], b[2]):
                box = (shift_x, ay1, shift_x + width, ay1 + height)
            elif abs(b[3] - a[1]) <= tol and overlap(a[0], a[2], b[0], b[2]):
                box = (shift_x, ay0 - height, shift_x + width, ay0)
            else:
                continue
            placed[other] = box
            queue.append(other)
    for index, tile in enumerate(tiles):
        if index not in placed:
            width, height = size(index)
            x, y = tile.rect[0] * factors[index], tile.rect[1] * factors[index]
            placed[index] = (x, y, x + width, y + height)
    return [placed[i] for i in range(len(tiles))]


class ScreenMap:
    """Le plan, dessine sur un Canvas."""

    def __init__(
        self,
        parent: tk.Misc,
        palette: Callable[[], Palette],
        on_toggle: Callable[[str], None],
        empty_text: str,
        compact: bool = False,
        height: int = 150,
    ) -> None:
        # `compact` : le nom seul, sans la ligne de details -- pour un plan
        # reduit, ou elle ne tiendrait pas.
        self._compact = compact
        self._palette = palette
        self._on_toggle = on_toggle
        self._empty_text = empty_text
        self._tiles: list[ScreenTile] = []
        self._hits: list[tuple[tuple[float, float, float, float], str]] = []
        self.canvas = tk.Canvas(parent, highlightthickness=0, height=height)
        self.canvas.bind("<Configure>", lambda _e: self.draw())
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Motion>", self._on_motion)

    def show(self, tiles: list[ScreenTile], empty_text: str | None = None) -> None:
        """Dessine ces ecrans ; `empty_text` remplace le message du plan vide."""
        self._tiles = tiles
        self._showing_empty = empty_text
        self.draw()

    # ------------------------------------------------------------ trace

    def draw(self) -> None:
        canvas = self.canvas
        palette = self._palette()
        canvas.delete("all")
        # Le plan est pose dans un cadre, dont le fond est celui des cartes.
        canvas.configure(background=palette.surface)
        self._hits = []
        width = max(canvas.winfo_width(), 100)
        height = max(canvas.winfo_height(), 80)
        if not self._tiles:
            canvas.create_text(
                width / 2, height / 2,
                text=getattr(self, "_showing_empty", None) or self._empty_text,
                fill=palette.text_muted, width=width - 2 * MARGIN_PX, justify="center",
            )
            return

        boxes = physical_layout(self._tiles)
        left = min(box[0] for box in boxes)
        top = min(box[1] for box in boxes)
        right = max(box[2] for box in boxes)
        bottom = max(box[3] for box in boxes)
        scale = min(
            (width - 2 * MARGIN_PX) / max(1, right - left),
            (height - 2 * MARGIN_PX) / max(1, bottom - top),
        )
        # Le plan est centre dans la zone.
        origin_x = (width - (right - left) * scale) / 2
        origin_y = (height - (bottom - top) * scale) / 2

        for tile, box in zip(self._tiles, boxes):
            x0 = origin_x + (box[0] - left) * scale + GAP_PX
            y0 = origin_y + (box[1] - top) * scale + GAP_PX
            x1 = origin_x + (box[2] - left) * scale - GAP_PX
            y1 = origin_y + (box[3] - top) * scale - GAP_PX
            self._draw_tile(tile, (x0, y0, x1, y1), palette)
            if tile.ref is not None:
                self._hits.append(((x0, y0, x1, y1), tile.ref))

    def _draw_tile(self, tile: ScreenTile, box, palette: Palette) -> None:
        canvas = self.canvas
        x0, y0, x1, y1 = box
        lit = tile.state in (STATE_ON, STATE_FIXED)
        # Un ecran allume est plein et cerne de vert ; un ecran coupe n'est
        # plus qu'un contour en pointille, sur le fond du plan.
        if lit:
            fill, outline, width, dash = palette.surface_alt, palette.on, 3, ()
            title_colour, detail_colour = palette.text, palette.text_muted
        elif tile.state == STATE_OFF:
            fill, outline, width, dash = palette.surface, palette.off, 1, (4, 3)
            title_colour = detail_colour = palette.text_muted
        elif tile.state == STATE_GHOST:
            # Coupe, mais toujours sur le bureau : il faut que ca se voie.
            fill, outline, width, dash = palette.surface, palette.warn, 2, (4, 3)
            title_colour, detail_colour = palette.text_muted, palette.warn
        else:
            fill, outline, width, dash = palette.surface_alt, palette.border, 1, ()
            title_colour = detail_colour = palette.text_muted
        canvas.create_rectangle(
            x0, y0, x1, y1, fill=fill, outline=outline, width=width, dash=dash
        )
        centre_x = (x0 + x1) / 2
        centre_y = (y0 + y1) / 2
        wrap = max(20, x1 - x0 - 10)
        if self._compact:
            canvas.create_text(
                centre_x, centre_y, text=tile.title, fill=title_colour,
                font=("", 8, "bold"), width=wrap, justify="center",
            )
            return
        canvas.create_text(
            centre_x, centre_y - 2, anchor="s", text=tile.title, fill=title_colour,
            font=("", 10, "bold"), width=wrap, justify="center",
        )
        canvas.create_text(
            centre_x, centre_y + 2, anchor="n", text=tile.detail, fill=detail_colour,
            font=("", 8), width=wrap, justify="center",
        )

    # ------------------------------------------------------------ souris

    def _ref_at(self, x: float, y: float) -> str | None:
        for (x0, y0, x1, y1), ref in self._hits:
            if x0 <= x <= x1 and y0 <= y <= y1:
                return ref
        return None

    def _on_click(self, event) -> None:
        ref = self._ref_at(event.x, event.y)
        if ref is not None:
            self._on_toggle(ref)

    def _on_motion(self, event) -> None:
        clickable = self._ref_at(event.x, event.y) is not None
        self.canvas.configure(cursor="hand2" if clickable else "")


def describe(
    state: str, width: int, height: int, primary: bool, scale: float = 1.0,
    diagonal: float = 0.0,
) -> str:
    """La ligne sous le nom : diagonale, definition, echelle, role, etat."""
    # Espace insecable : « 100 % » ne doit pas se couper en fin de ligne.
    parts = [f"{width}x{height}", f"{round(scale * 100)}\u00a0%"]
    if diagonal > 0:
        parts.insert(0, f'{diagonal:.1f}"')
    if primary:
        parts.append(t("primary"))
    if state == STATE_OFF:
        parts.append(t("switched off"))
    elif state == STATE_GHOST:
        parts.append(t("ghost"))
    elif state == STATE_FIXED:
        parts.append(t("always on"))
    elif state == STATE_UNMANAGED:
        parts.append(t("not on an outlet"))
    return "  ·  ".join(parts)


def build_tiles(
    config: "AppConfig",
    lit: Callable[[str], bool | None],
    editable: bool,
    ghosts: tuple[str, ...] | list[str] = (),
) -> list[ScreenTile]:
    """Les ecrans memorises, prets a dessiner.

    `lit(ref)` dit si la prise d'un ecran est allumee -- None : a montrer
    allume, faute de savoir. `editable` rend les ecrans cliquables. Commun
    au plan des reglages et a celui de la fenetre du raccourci : meme
    dessin, memes regles.
    """
    by_key = {o.monitor_key: o for o in config.outlets if o.monitor_key}
    tiles = []
    for screen in config.screens:
        outlet = by_key.get(screen.key)
        ref = None
        if outlet is None:
            state = STATE_UNMANAGED
            title = screen.name or t("Screen")
        elif outlet.never_switch_off:
            state, title = STATE_FIXED, outlet.label
        else:
            on = lit(outlet.ref)
            state = STATE_OFF if on is False else STATE_ON
            if on is False and outlet.label in ghosts:
                state = STATE_GHOST
            title = outlet.label
            ref = outlet.ref if editable else None
        tiles.append(ScreenTile(
            rect=screen.rect,
            title=title,
            detail=describe(state, screen.width, screen.height, screen.primary,
                            screen.scale, screen.diagonal),
            state=state,
            ref=ref,
            scale=screen.scale,
            primary=screen.primary,
            diagonal=screen.diagonal,
        ))
    return tiles
