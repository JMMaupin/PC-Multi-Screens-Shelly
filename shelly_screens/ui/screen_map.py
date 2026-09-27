"""Screen map, with screens arranged the way Windows places them on the desktop.

A profile reads better as a drawing than as a list of checkboxes: you see
at a glance which screens stay on, and where. Each screen bears the name of
the outlet that powers it; clicking a screen switches its outlet on or off
in the profile, like the matching checkbox.

Each screen is drawn at its physical size: the diagonal its EDID reports,
in the proportions of its resolution. A 27-inch 4K and a 27-inch QHD have
the same size on the map, as on the desk. A screen whose EDID says nothing
keeps its effective size -- its resolution divided by the scale set in
Windows --, converted at 96 dots per inch, the density Windows assumes at
100 %.

Desktop coordinates, however, are in pixels: once each screen is brought
back to its real size, they no longer fit together. The map is therefore
rebuilt step by step, starting from the primary screen, following the
edges the screens share.
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

STATE_ON = "on"  # powered by the profile
STATE_OFF = "off"  # switched off by the profile
STATE_FIXED = "fixed"  # protected outlet: always powered
STATE_UNMANAGED = "unmanaged"  # no associated outlet
STATE_GHOST = "ghost"  # switched off, but Windows keeps the screen on the desktop

MARGIN_PX = 14
GAP_PX = 3  # space between two adjacent screens, to tell them apart
EDGE_TOLERANCE_PX = 16  # two edges closer than this are adjacent
REFERENCE_MM_PER_PX = 25.4 / 96  # one pixel at 100 %, according to Windows

Box = tuple[float, float, float, float]


@dataclass(frozen=True)
class ScreenTile:
    """A screen to draw."""

    rect: tuple[int, int, int, int]  # desktop coordinates
    title: str
    detail: str
    state: str
    ref: str | None = None  # outlet toggled by a click, if there is one
    scale: float = 1.0  # scale set in Windows
    primary: bool = False
    diagonal: float = 0.0  # inches, according to the EDID; 0 if unknown


def mm_per_pixel(tile: ScreenTile) -> float:
    """Real size of one pixel of this screen, in millimeters."""
    width = tile.rect[2] - tile.rect[0]
    height = tile.rect[3] - tile.rect[1]
    if tile.diagonal > 0 and width > 0 and height > 0:
        return tile.diagonal * 25.4 / math.hypot(width, height)
    return REFERENCE_MM_PER_PX / (tile.scale or 1.0)


def physical_layout(tiles: list[ScreenTile]) -> list[Box]:
    """Place each screen at its real size, in millimeters, without breaking contacts.

    Start from the primary screen and lay its neighbors one by one: a
    screen stuck to the right of another starts where that one ends, and
    its offset along the shared edge is converted using the pixels of the
    screen already placed. A screen that touches no other -- Windows does
    not allow it, but a hand-edited configuration does -- keeps its place.
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
            # Offsets along the shared edge, in the placed screen's pixels.
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
    """The map, drawn on a Canvas."""

    def __init__(
        self,
        parent: tk.Misc,
        palette: Callable[[], Palette],
        on_toggle: Callable[[str], None],
        empty_text: str,
        compact: bool = False,
        height: int = 150,
    ) -> None:
        # `compact`: the name only, without the detail line -- for a small
        # map, where it would not fit.
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
        """Draw these screens; `empty_text` replaces the empty-map message."""
        self._tiles = tiles
        self._showing_empty = empty_text
        self.draw()

    # ------------------------------------------------------------ drawing

    def draw(self) -> None:
        canvas = self.canvas
        palette = self._palette()
        canvas.delete("all")
        # The map sits in a frame, whose background is the card background.
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
        # The map is centered in the area.
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
        # A screen that is on is filled and outlined in green; a switched-off
        # screen is just a dashed outline, on the map background.
        if lit:
            fill, outline, width, dash = palette.surface_alt, palette.on, 3, ()
            title_colour, detail_colour = palette.text, palette.text_muted
        elif tile.state == STATE_OFF:
            fill, outline, width, dash = palette.surface, palette.off, 1, (4, 3)
            title_colour = detail_colour = palette.text_muted
        elif tile.state == STATE_GHOST:
            # Switched off, but still on the desktop: it must be visible.
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

    # ------------------------------------------------------------ mouse

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
    """The line under the name: diagonal, resolution, scale, role, state."""
    # Non-breaking space: "100 %" must not be split at the end of a line.
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
    """The remembered screens, ready to draw.

    `lit(ref)` tells whether a screen's outlet is on -- None: show it as
    on, for lack of knowing. `editable` makes the screens clickable. Shared
    by the settings map and the shortcut window's map: same drawing, same
    rules.
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
