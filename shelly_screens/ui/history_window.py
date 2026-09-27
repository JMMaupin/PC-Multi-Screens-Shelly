"""Window for browsing the power consumption history.

A separate window, opened from the tray icon menu as well as from the
settings: you come here to read a day, a week, a month, without having to
go through the configuration tabs.

Navigation follows the conventions of stock-market charts. The mouse wheel
zooms around the pointer, dragging scrolls, and the Live button brings you
back to the present -- the window then follows new measurements by itself.
As soon as you move away to look back at the past, it freezes where chosen.

The vertical scale is logarithmic by default, but fitted to what is
visible. Over a range without sleep, between 90 and 250 watts, a fixed
0.5 to 400 W scale would squash the curve into a third of the height; here
it is spread over all of it. A linear toggle stays within reach, to judge
first-hand which one reads best.

A period without any measurement stays a gap in the curve. Joining both
edges would suggest a consumption that was never observed.

Each outlet has its curve; a selector switches from one to another. The
PC's outlet opens first, and the window then remembers the last choice.
"""

from __future__ import annotations

import math
import threading
import time
import tkinter as tk
from bisect import bisect_left, bisect_right
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import TYPE_CHECKING

from .. import power_history
from ..i18n import t
from ..win import icon as icon_module
from . import theme as theme_module

if TYPE_CHECKING:
    from ..app import Application
    from ..config import OutletConfig

# Plot margins, in pixels.
LEFT = 70
RIGHT = 18
TOP = 14
BOTTOM = 34

REFRESH_MS = 5000  # the pace of the application's readings
MIN_SPAN_S = 120.0  # below this, only steps are visible
LOG_FLOOR_W = 0.5  # floor of the log scale: zero has no place on it
ZOOM_STEP = 1.25  # factor per mouse wheel notch

# One-click spans, in seconds.
PRESETS = (
    ("1 h", 3600),
    ("6 h", 6 * 3600),
    ("24 h", 86400),
    ("7 d", 7 * 86400),
    ("30 d", 30 * 86400),
)

# Horizontal gridline steps, from finest to widest.
TIME_STEPS = (
    60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200,
    86400, 172800, 604800,
)

DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

_state_lock = threading.Lock()
_is_open = False
_current: "HistoryWindow | None" = None
_last_key: str | None = None  # the outlet viewed last


def open_history(application: "Application") -> None:
    """Open the window, or bring the already open one to the foreground."""
    global _is_open
    with _state_lock:
        if _is_open:
            window = _current
            if window is not None:
                try:
                    window.root.after(0, window.raise_window)
                except Exception:  # noqa: BLE001 - window already closing
                    pass
            return
        _is_open = True

    def run() -> None:
        global _is_open, _current
        try:
            root = tk.Tk()
            _current = HistoryWindow(root, application)
            root.mainloop()
        except Exception as exc:  # noqa: BLE001 - a failed UI must not kill the app
            application.log(f"History window failed: {exc}")
        finally:
            with _state_lock:
                _is_open = False
                _current = None

    threading.Thread(target=run, name="history-ui", daemon=True).start()


def _duration(seconds: float) -> str:
    """Readable duration, rounded to the minute."""
    minutes = int(round(seconds / 60))
    if minutes < 60:
        return t("{m} min", m=minutes)
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return t("{h} h {m:02d}", h=hours, m=minutes)
    days, hours = divmod(hours, 24)
    return t("{d} d {h} h", d=days, h=hours)


def _stamp(moment: float, seconds: bool = False) -> str:
    """Local date and time, with the weekday translated."""
    local = time.localtime(moment)
    day = t(DAY_NAMES[local.tm_wday])
    clock = time.strftime("%H:%M:%S" if seconds else "%H:%M", local)
    return f"{day} {local.tm_mday:02d}/{local.tm_mon:02d} {clock}"


def _regional_separators() -> tuple[str, str]:
    """List and decimal separators from the Windows regional settings.

    That is what Excel applies when opening a CSV: reusing them as-is is the
    only way to get columns and numbers read correctly from a simple
    double-click.
    """
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Control Panel\International"
        ) as key:
            delimiter = str(winreg.QueryValueEx(key, "sList")[0]) or ";"
            decimal = str(winreg.QueryValueEx(key, "sDecimal")[0]) or ","
    except OSError:
        return ";", ","
    if delimiter == decimal:
        return ";", ","  # inconsistent setting: fall back to the French convention
    return delimiter, decimal


class HistoryWindow:
    """The chart, its toolbar and its readouts."""

    def __init__(self, root: tk.Tk, application: "Application") -> None:
        self.root = root
        self.app = application
        self.config = application.config

        icon_module.apply_to_window(root)
        self.palette = theme_module.apply(root, self.config.settings.theme)
        root.geometry("1100x620")
        root.minsize(760, 460)

        self.samples: list[power_history.Sample] = []
        self.times: list[float] = []
        self.store: power_history.HistoryStore | None = None
        self.choices = self._list_outlets()
        self.key: str | None = None
        self.label = ""
        self.outlet: "OutletConfig | None" = None
        self._choose(self._initial_key())
        self.span = 6 * 3600.0
        self.end = time.time()
        self.follow = True
        self.log_scale = tk.BooleanVar(self.root, value=True)
        self.drag: tuple[int, float] | None = None
        self.cursor_x: int | None = None
        self.view: tuple[float, float, int, int, int, int] | None = None
        self.y_range = (LOG_FLOOR_W, 400.0)

        self._build()
        self._reload()
        self.draw()
        root.after(REFRESH_MS, self._refresh)

    # ------------------------------------------------------------ interface

    def _build(self) -> None:
        palette = self.palette
        bar = ttk.Frame(self.root, padding=(12, 10, 12, 4))
        bar.pack(fill="x")

        ttk.Label(bar, text=t("Outlet")).pack(side="left", padx=(0, 6))
        self.outlet_box = ttk.Combobox(
            bar, state="readonly", width=24,
            values=[name for _key, name, _outlet in self.choices],
        )
        keys = [key for key, _name, _outlet in self.choices]
        if self.key in keys:
            self.outlet_box.current(keys.index(self.key))
        self.outlet_box.bind("<<ComboboxSelected>>", self._on_outlet_selected)
        self.outlet_box.pack(side="left")
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=10)

        ttk.Label(bar, text=t("Span")).pack(side="left", padx=(0, 6))
        for label, seconds in PRESETS:
            ttk.Button(
                bar, text=t(label), width=0,
                command=lambda s=seconds: self._set_span(s),
            ).pack(side="left", padx=1)

        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Button(bar, text="◀", width=0, command=self._back).pack(side="left", padx=1)
        ttk.Button(bar, text="▶", width=0, command=self._forward).pack(side="left", padx=1)
        ttk.Button(bar, text=t("Live"), width=0, command=self._go_live).pack(
            side="left", padx=(6, 0)
        )

        scale = ttk.Frame(bar)
        scale.pack(side="right")
        ttk.Label(scale, text=t("Scale")).pack(side="left", padx=(0, 6))
        ttk.Radiobutton(
            scale, text=t("Log"), value=True, variable=self.log_scale,
            command=self.draw,
        ).pack(side="left")
        ttk.Radiobutton(
            scale, text=t("Linear"), value=False, variable=self.log_scale,
            command=self.draw,
        ).pack(side="left", padx=(8, 0))

        export = ttk.Menubutton(bar, text=t("Export"))
        menu = tk.Menu(export, tearoff=False)
        menu.add_command(
            label=t("CSV, standard (comma, decimal point)..."),
            command=lambda: self._export(regional=False),
        )
        delimiter, decimal = _regional_separators()
        menu.add_command(
            label=t("CSV for Excel, regional settings ({delimiter} and {decimal})...",
                    delimiter=delimiter, decimal=decimal),
            command=lambda: self._export(regional=True),
        )
        export["menu"] = menu
        export.pack(side="right", padx=(0, 18))

        # The bottom row is reserved before the expanding chart, otherwise
        # it disappears when the window lacks height.
        bottom = ttk.Frame(self.root, padding=(12, 2, 12, 10))
        bottom.pack(fill="x", side="bottom")
        self.mode = tk.StringVar(self.root, value="")
        self.mode_label = ttk.Label(bottom, textvariable=self.mode)
        self.mode_label.pack(side="left")
        self.readout = tk.StringVar(self.root, value="")
        ttk.Label(bottom, textvariable=self.readout).pack(side="right")

        self.stats = tk.StringVar(self.root, value="")
        ttk.Label(
            self.root, textvariable=self.stats, padding=(12, 0)
        ).pack(fill="x", side="bottom")
        ttk.Label(
            self.root,
            text=t("Mouse wheel to zoom, drag to scroll, double-click to go "
                   "back to live."),
            style="Hint.TLabel",
            padding=(12, 0, 12, 4),
        ).pack(fill="x", side="bottom")

        self.canvas = tk.Canvas(
            self.root, background=palette.surface, highlightthickness=0
        )
        self.canvas.pack(fill="both", expand=True, padx=12, pady=4)
        self.canvas.bind("<Configure>", lambda _e: self.draw())
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Double-Button-1>", lambda _e: self._go_live())
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", self._on_leave)

        self.root.bind("<Left>", lambda _e: self._back())
        self.root.bind("<Right>", lambda _e: self._forward())
        self.root.bind("<End>", lambda _e: self._go_live())
        for key in ("<plus>", "<KP_Add>"):
            self.root.bind(key, lambda _e: self._zoom(1 / ZOOM_STEP, None))
        for key in ("<minus>", "<KP_Subtract>"):
            self.root.bind(key, lambda _e: self._zoom(ZOOM_STEP, None))

    def raise_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    # ------------------------------------------------------------ outlets

    def _list_outlets(self) -> list[tuple[str, str, "OutletConfig | None"]]:
        """Offered outlets: key, displayed name, configuration.

        Configured outlets first, in their order; then those the database
        still knows but the configuration has dropped -- their history stays
        readable until pruning removes it.
        """
        choices: list[tuple[str, str, "OutletConfig | None"]] = [
            (power_history.outlet_key(self.config, o), o.label, o)
            for o in self.config.outlets
        ]
        known = {key for key, _name, _outlet in choices}
        store = self._reader()
        if store is not None:
            for key, label in store.outlets():
                if key not in known:
                    name = t("{outlet} (removed)", outlet=label or key)
                    choices.append((key, name, None))
        # Two outlets with the same name are told apart by their reference.
        names = [name for _key, name, _outlet in choices]
        return [
            (key, f"{name} ({outlet.ref})", outlet)
            if outlet is not None and names.count(name) > 1
            else (key, name, outlet)
            for key, name, outlet in choices
        ]

    def _initial_key(self) -> str | None:
        """The last outlet viewed, failing that the PC's, failing that the first."""
        keys = [key for key, _name, _outlet in self.choices]
        if _last_key in keys:
            return _last_key
        pc = self.config.host_pc_outlet()
        if pc is not None:
            return power_history.outlet_key(self.config, pc)
        return keys[0] if keys else None

    def _choose(self, key: str | None) -> None:
        global _last_key
        self.key = key
        self.label, self.outlet = "", None
        for choice_key, name, outlet in self.choices:
            if choice_key == key:
                self.label, self.outlet = name, outlet
        _last_key = key
        self.root.title(
            t("Consumption history - {outlet}", outlet=self.label or "-")
        )

    def _on_outlet_selected(self, _event) -> None:
        index = self.outlet_box.current()
        if not 0 <= index < len(self.choices):
            return
        self._choose(self.choices[index][0])
        self._reload()
        self.draw()

    # ------------------------------------------------------------ data

    def _reader(self) -> "power_history.HistoryStore | None":
        """The database, opened on the first read that finds it."""
        if self.store is None:
            self.store = power_history.open_reader(self.config)
        return self.store

    def _reload(self) -> None:
        self.samples, self.times = [], []
        self._read_more()

    def _read_more(self) -> bool:
        """Read what was added since last time."""
        store = self._reader()
        if store is None or self.key is None:
            return False
        oldest = time.time() - self._max_span()
        # What pruning removed from the database also leaves memory.
        cut = bisect_left(self.times, oldest)
        if cut:
            del self.samples[:cut]
            del self.times[:cut]
        new = store.read(
            self.key, after=self.times[-1] if self.times else None, since=oldest
        )
        if not new:
            return bool(cut)
        in_order = not self.samples or new[0].t >= self.samples[-1].t
        self.samples.extend(new)
        if not in_order:
            # Points arriving out of order -- rare -- require a sort.
            self.samples.sort(key=lambda s: s.t)
            self.times = [s.t for s in self.samples]
        else:
            self.times.extend(s.t for s in new)
        return True

    def _refresh(self) -> None:
        try:
            changed = self._read_more()
            if self.follow or changed:
                self.draw()
        except Exception as exc:  # noqa: BLE001 - a failed refresh is not fatal
            self.app.log(f"History refresh failed: {exc}")
        self.root.after(REFRESH_MS, self._refresh)

    def _value_at(self, moment: float, now: float) -> power_history.Sample | None:
        """The point in effect at this instant, if there is one."""
        index = bisect_right(self.times, moment) - 1
        if index < 0:
            return None
        sample = self.samples[index]
        following = self.times[index + 1] if index + 1 < len(self.times) else now
        if moment > min(following, sample.t + sample.max_gap):
            return None
        return sample

    # ------------------------------------------------------------ navigation

    def _max_span(self) -> float:
        return max(1, int(self.config.settings.history_days)) * 86400.0

    def _effective_end(self) -> float:
        return time.time() if self.follow else self.end

    def _clamp(self) -> None:
        now = time.time()
        self.span = min(max(self.span, MIN_SPAN_S), self._max_span())
        if self.follow:
            self.end = now
            return
        oldest = self.times[0] if self.times else now - self.span
        # Do not let the window drift entirely into the void before the
        # first point.
        self.end = max(self.end, oldest + self.span * 0.1)
        if self.end >= now - self.span * 0.01:
            self.end = now
            self.follow = True

    def _set_span(self, seconds: float) -> None:
        self.span = float(seconds)
        self._clamp()
        self.draw()

    def _back(self) -> None:
        self.end = self._effective_end() - self.span / 2
        self.follow = False
        self._clamp()
        self.draw()

    def _forward(self) -> None:
        self.end = self._effective_end() + self.span / 2
        self.follow = False
        self._clamp()
        self.draw()

    def _go_live(self) -> None:
        self.follow = True
        self._clamp()
        self.draw()

    def _zoom(self, factor: float, x: int | None) -> None:
        """Zoom around the pointer; when following live, around the present."""
        if self.view is None:
            return
        start, _end, x0, _y0, x1, _y1 = self.view
        new_span = min(max(self.span * factor, MIN_SPAN_S), self._max_span())
        if self.follow or x is None:
            self.span = new_span
        else:
            fraction = min(max((x - x0) / max(1, x1 - x0), 0.0), 1.0)
            anchor = start + fraction * self.span
            self.end = anchor + (1 - fraction) * new_span
            self.span = new_span
        self._clamp()
        self.draw()

    def _on_wheel(self, event) -> None:
        self._zoom(1 / ZOOM_STEP if event.delta > 0 else ZOOM_STEP, event.x)

    def _on_press(self, event) -> None:
        self.drag = (event.x, self._effective_end())

    def _on_drag(self, event) -> None:
        if self.drag is None or self.view is None:
            return
        x_start, end_start = self.drag
        _s, _e, x0, _y0, x1, _y1 = self.view
        shift = (event.x - x_start) / max(1, x1 - x0) * self.span
        self.follow = False
        self.end = end_start - shift
        self._clamp()
        self.cursor_x = event.x
        self.draw()

    def _on_release(self, _event) -> None:
        self.drag = None

    def _on_motion(self, event) -> None:
        if self.drag is not None:
            return
        self.cursor_x = event.x
        self._draw_cursor()

    def _on_leave(self, _event) -> None:
        self.cursor_x = None
        self._draw_cursor()

    # ------------------------------------------------------------ plot

    def _y(self, watts: float) -> float:
        _s, _e, _x0, y0, _x1, y1 = self.view
        low, high = self.y_range
        if self.log_scale.get():
            value = max(watts, low)
            ratio = math.log(value / low) / math.log(high / low)
        else:
            ratio = (watts - low) / (high - low)
        ratio = min(max(ratio, 0.0), 1.0)
        return y1 - ratio * (y1 - y0)

    def _columns(self, start: float, end: float, count: int, now: float):
        """Minimum and maximum power in each pixel column.

        A month of measurements holds far more points than the screen has
        columns: each column is summarized by its range rather than drawing
        thousands of overlapping segments. When zoomed in tight, each point
        instead covers several columns, and the same calculation naturally
        draws its steps.
        """
        columns: list[list[float] | None] = [None] * count
        if count <= 0 or not self.samples:
            return columns
        scale = count / (end - start)
        first = max(bisect_right(self.times, start) - 1, 0)
        last = min(bisect_right(self.times, end), len(self.samples))
        for index in range(first, last):
            sample = self.samples[index]
            following = self.times[index + 1] if index + 1 < len(self.times) else now
            valid_until = min(following, sample.t + sample.max_gap)
            a = max(sample.t, start)
            b = min(valid_until, end)
            if b <= a:
                if not start <= sample.t <= end:
                    continue
                a = b = sample.t
            first_col = min(max(int((a - start) * scale), 0), count - 1)
            last_col = min(max(int(math.ceil((b - start) * scale)), first_col + 1), count)
            watts = sample.watts
            for column in range(first_col, last_col):
                cell = columns[column]
                if cell is None:
                    columns[column] = [watts, watts]
                elif watts < cell[0]:
                    cell[0] = watts
                elif watts > cell[1]:
                    cell[1] = watts
        return columns

    def _fit_y(self, values: list[float]) -> tuple[float, float]:
        """Vertical range, fitted to what is visible."""
        if self.log_scale.get():
            if not values:
                return LOG_FLOOR_W, 400.0
            positive = [v for v in values if v > 0]
            low = max(LOG_FLOOR_W, min(positive) * 0.8) if positive else LOG_FLOOR_W
            if len(positive) < len(values):
                low = LOG_FLOOR_W  # a visible zero goes down to the floor
            high = max(max(values) * 1.25, low * 4)
            return low, high
        high = max(max(values) * 1.1, 1.0) if values else 100.0
        return 0.0, high

    def draw(self) -> None:
        canvas = self.canvas
        palette = self.palette
        canvas.delete("all")
        width = max(canvas.winfo_width(), 300)
        height = max(canvas.winfo_height(), 200)
        x0, y0, x1, y1 = LEFT, TOP, width - RIGHT, height - BOTTOM
        if x1 - x0 < 60 or y1 - y0 < 60:
            return
        now = time.time()
        self._clamp()
        end = self._effective_end()
        start = end - self.span
        self.view = (start, end, x0, y0, x1, y1)

        columns = self._columns(start, end, x1 - x0, now)
        values = [v for cell in columns if cell for v in cell]
        self.y_range = self._fit_y(values)

        canvas.create_rectangle(x0, y0, x1, y1, outline=palette.border, fill=palette.bg)
        self._draw_y_grid(x0, y0, x1, y1)
        self._draw_x_grid(start, end, x0, y0, x1, y1)
        self._draw_thresholds(x0, x1)

        for column, cell in enumerate(columns):
            if cell is None:
                continue
            x = x0 + column + 0.5
            canvas.create_line(
                x, self._y(cell[1]) - 1, x, self._y(cell[0]) + 1,
                fill=palette.accent, width=2,
            )

        if self.key is None:
            message = t("No outlet configured.")
        elif not self.samples:
            message = t("No data recorded yet.")
        elif not values:
            message = t("No data in this range.")
        else:
            message = ""
        if message:
            canvas.create_text(
                (x0 + x1) / 2, (y0 + y1) / 2, text=message, fill=palette.text_muted
            )

        self._update_stats(start, end, now)
        self._update_mode(end)
        self._draw_cursor()

    def _draw_y_grid(self, x0: int, y0: int, x1: int, y1: int) -> None:
        canvas = self.canvas
        palette = self.palette
        low, high = self.y_range
        if self.log_scale.get():
            ticks = []
            for mantissas in ((1, 1.5, 2, 3, 5, 7), (1, 2, 5), (1,)):
                ticks = [
                    m * 10 ** d
                    for d in range(math.floor(math.log10(low)) - 1, math.ceil(math.log10(high)) + 1)
                    for m in mantissas
                    if low <= m * 10 ** d <= high
                ]
                if len(ticks) <= 9:
                    break
        else:
            raw = (high - low) / 5
            magnitude = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
            # The round step closest to the target, not the first one that
            # exceeds it: over 0 - 286 W, the latter gave 100 W, i.e. only
            # three gridlines; the closest gives 50 W.
            step = min(
                (m * magnitude for m in (1, 2, 2.5, 5, 10)),
                key=lambda s: abs(math.log(s / raw)),
            )
            ticks = [i * step for i in range(0, int(high / step) + 1) if i * step >= low]
        for watts in ticks:
            y = self._y(watts)
            canvas.create_line(x0, y, x1, y, fill=palette.border)
            label = f"{watts:g} W" if watts < 10 else f"{watts:.0f} W"
            canvas.create_text(
                x0 - 8, y, anchor="e", fill=palette.text_muted, text=label, font=("", 8)
            )

    def _draw_x_grid(self, start, end, x0, y0, x1, y1) -> None:
        canvas = self.canvas
        palette = self.palette
        span = end - start
        step = next((s for s in TIME_STEPS if span / s <= 8), TIME_STEPS[-1])
        # Gridlines fall on round local times, and midnight on a gridline:
        # that is what the eye looks for to get its bearings.
        shift = time.localtime(start).tm_gmtoff
        tick = math.ceil((start + shift) / step) * step - shift
        while tick <= end:
            x = x0 + (tick - start) / span * (x1 - x0)
            local = time.localtime(tick)
            midnight = local.tm_hour == 0 and local.tm_min == 0
            canvas.create_line(
                x, y0, x, y1, fill=palette.text_muted if midnight else palette.border,
                dash=() if midnight else (2, 4),
            )
            if step >= 86400 or midnight:
                label = f"{t(DAY_NAMES[local.tm_wday])} {local.tm_mday:02d}/{local.tm_mon:02d}"
            else:
                label = time.strftime("%H:%M", local)
            canvas.create_text(
                x, y1 + 12, text=label, fill=palette.text_muted, font=("", 8)
            )
            tick += step

    def _draw_thresholds(self, x0: int, x1: int) -> None:
        """The detection thresholds, to spot sleep at a glance."""
        sensing = self.config.sensing
        # The thresholds only apply to the outlet whose consumption tells
        # whether the PC is running.
        if not sensing.enabled or self.outlet is None or not self.outlet.host_pc:
            return
        low, high = self.y_range
        for watts, colour, label in (
            (sensing.on_threshold_w, self.palette.on, t("running above")),
            (sensing.off_threshold_w, self.palette.warn, t("off below")),
        ):
            if not low <= watts <= high:
                continue
            y = self._y(watts)
            self.canvas.create_line(x0, y, x1, y, fill=colour, dash=(6, 4))
            self.canvas.create_text(
                x1 - 6, y - 8, anchor="e", fill=colour, font=("", 8),
                text=f"{label} {watts:g} W",
            )

    def _draw_cursor(self) -> None:
        canvas = self.canvas
        canvas.delete("cursor")
        if self.view is None or self.cursor_x is None:
            self.readout.set("")
            return
        start, end, x0, y0, x1, y1 = self.view
        if not x0 <= self.cursor_x <= x1:
            self.readout.set("")
            return
        moment = start + (self.cursor_x - x0) / (x1 - x0) * (end - start)
        canvas.create_line(
            self.cursor_x, y0, self.cursor_x, y1,
            fill=self.palette.text_muted, dash=(2, 3), tags="cursor",
        )
        # The time on the time axis, under the pointer. Beyond one day
        # displayed, the time alone no longer tells which day it is.
        clock = (
            _stamp(moment, seconds=True)
            if end - start > 86400
            else time.strftime("%H:%M:%S", time.localtime(moment))
        )
        self._tag(self.cursor_x, y1 + 12, clock, "center", x0, x1,
                  self.palette.surface_alt, self.palette.text)
        sample = self._value_at(moment, time.time())
        if sample is None:
            self._tag(self.cursor_x + 12, (y0 + y1) / 2, t("no data"), "w", x0, x1,
                      self.palette.surface_alt, self.palette.text_muted)
            self.readout.set(f"{_stamp(moment, seconds=True)}   {t('no data')}")
            return
        y = self._y(sample.watts)
        # A horizontal guide to the power axis, to read the value on the
        # scale as well as in the label.
        canvas.create_line(
            x0, y, self.cursor_x, y,
            fill=self.palette.text_muted, dash=(2, 3), tags="cursor",
        )
        canvas.create_oval(
            self.cursor_x - 5, y - 5, self.cursor_x + 5, y + 5,
            fill=self.palette.bg, outline=self.palette.accent, width=2,
            tags="cursor",
        )
        asleep = sample.source == power_history.SOURCE_PROBE
        value = f"{sample.watts:.1f} W"
        if asleep:
            value += "  " + t("asleep")
        # The label sticks to the point, on the side where there is room.
        # Dark background with an accent border: a background the color of
        # the curve blended into it on hover, and the label became unreadable.
        self._tag(self.cursor_x + 12, y, value, "w", x0, x1,
                  self.palette.bg, self.palette.text, bold=True,
                  outline=self.palette.accent)
        origin = "   " + t("(on-device probe, PC asleep)") if asleep else ""
        self.readout.set(
            f"{_stamp(moment, seconds=True)}   {sample.watts:.1f} W{origin}"
        )

    def _tag(self, x, y, text, anchor, left, right, fill, colour, bold=False,
             outline=""):
        """Label on a solid background, flipped if it would overflow the frame."""
        canvas = self.canvas
        font = ("", 10, "bold") if bold else ("", 9)
        item = canvas.create_text(
            x, y, text=text, anchor=anchor, fill=colour, font=font, tags="cursor"
        )
        x_a, y_a, x_b, y_b = canvas.bbox(item)
        # Too close to the right edge: move it to the other side of the pointer.
        if anchor == "w" and x_b > right - 4:
            canvas.coords(item, 2 * self.cursor_x - x, y)
            canvas.itemconfigure(item, anchor="e")
            x_a, y_a, x_b, y_b = canvas.bbox(item)
        # Centered on the pointer, it must not overflow on either side.
        if anchor == "center":
            shift = max(0, left - x_a) - max(0, x_b - right)
            if shift:
                canvas.move(item, shift, 0)
                x_a, y_a, x_b, y_b = canvas.bbox(item)
        box = canvas.create_rectangle(
            x_a - 6, y_a - 3, x_b + 6, y_b + 3, fill=fill, outline=outline,
            tags="cursor",
        )
        canvas.tag_raise(item, box)

    def _export(self, regional: bool) -> None:
        """Export the points of the displayed period to CSV."""
        if self.view is None:
            return
        start, end = self.view[0], self.view[1]
        first = bisect_left(self.times, start)
        last = bisect_right(self.times, end)
        chosen = self.samples[first:last]
        if not chosen:
            messagebox.showinfo(
                t("Export"), t("No data in this range."), parent=self.root
            )
            return
        following = self.times[last] if last < len(self.times) else time.time()
        label = "".join(c for c in self.label if c.isalnum())
        name = time.strftime("consumption_{label}_%Y-%m-%d_%H%M", time.localtime(start))
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title=t("Export"),
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
            initialfile=name.replace("{label}", label or "outlet") + ".csv",
        )
        if not path:
            return
        delimiter, decimal = _regional_separators() if regional else (",", ".")
        try:
            count = power_history.export_csv(
                Path(path), chosen, end=min(following, time.time()),
                delimiter=delimiter, decimal=decimal,
            )
        except OSError as exc:
            messagebox.showerror(t("Export"), str(exc), parent=self.root)
            return
        self.readout.set(
            t("{count} point(s) exported to {name}", count=count, name=Path(path).name)
        )

    # ------------------------------------------------------------ readouts

    def _update_stats(self, start: float, end: float, now: float) -> None:
        """Minimum, average, maximum and energy over what is visible.

        The average is weighted by duration: a ten-second spike does not
        weigh as much as an hour of sleep. Energy only counts the time
        actually measured, and the coverage says so.
        """
        energy = 0.0
        covered = 0.0
        low = high = None
        if self.samples:
            first = max(bisect_right(self.times, start) - 1, 0)
            last = min(bisect_right(self.times, end), len(self.samples))
            for index in range(first, last):
                sample = self.samples[index]
                following = self.times[index + 1] if index + 1 < len(self.times) else now
                a = max(sample.t, start)
                b = min(following, sample.t + sample.max_gap, end)
                if b <= a:
                    continue
                duration = b - a
                energy += sample.watts * duration
                covered += duration
                low = sample.watts if low is None else min(low, sample.watts)
                high = sample.watts if high is None else max(high, sample.watts)
        if covered <= 0:
            self.stats.set("")
            return
        self.stats.set(
            t("Min {low} W   average {avg} W   max {high} W   energy {kwh} kWh   "
              "measured {covered} of {span}",
              low=f"{low:.1f}", avg=f"{energy / covered:.1f}", high=f"{high:.1f}",
              kwh=f"{energy / 3_600_000:.3f}", covered=_duration(covered),
              span=_duration(end - start))
        )

    def _update_mode(self, end: float) -> None:
        if self.follow:
            self.mode.set("● " + t("Live"))
            self.mode_label.configure(foreground=self.palette.on)
        else:
            self.mode.set(t("History, up to {when}", when=_stamp(end)))
            self.mode_label.configure(foreground=self.palette.text_muted)
