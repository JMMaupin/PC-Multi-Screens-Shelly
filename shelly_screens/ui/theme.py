"""Interface themes: system, light, dark.

Three possible choices, and `system` follows the Windows setting in real time.

Two points deserve an explanation.

The ttk theme used is `clam`, in all three cases. The native `vista` theme
looks nicer in light mode, but it draws its widgets with system images: their
backgrounds cannot be recolored, and a dark mode stays hopelessly light in
places. `clam` is fully controllable, at the cost of a few plainer
indicators -- a trade-off worth the consistency across the three themes.

The title bar does not belong to Tk but to the window manager. It is
switched through DwmSetWindowAttribute, otherwise a dark window would keep a
white strip on top.
"""

from __future__ import annotations

import ctypes
import tkinter as tk
import winreg
from ctypes import wintypes
from dataclasses import dataclass
from tkinter import ttk

MODES = ("system", "light", "dark")
PERSONALIZE_KEY = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
DWM_KEY = r"Software\Microsoft\Windows\DWM"
# Windows 11 / Windows 10 20H1 attribute; 19 on older versions.
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19


@dataclass(frozen=True)
class Palette:
    """Colors of a theme."""

    name: str
    dark: bool
    bg: str  # window background
    surface: str  # frames, lists, fields
    surface_alt: str  # alternating rows, headers
    border: str
    text: str
    text_muted: str  # hints and comments
    text_disabled: str
    accent: str  # selection, active tab
    accent_text: str  # text laid on the accent
    on: str  # outlet powered
    off: str  # outlet switched off
    warn: str  # device unreachable, bad signal
    caution: str  # in between: a merely good signal


LIGHT = Palette(
    name="light",
    dark=False,
    bg="#f3f3f3",
    surface="#ffffff",
    surface_alt="#eaeaea",
    border="#cfcfcf",
    text="#1a1a1a",
    text_muted="#5f5f5f",
    text_disabled="#a0a0a0",
    accent="#0067c0",
    accent_text="#ffffff",
    on="#128a3c",
    off="#8a8a8a",
    warn="#b3261e",
    caution="#b35c00",
)

DARK = Palette(
    name="dark",
    dark=True,
    bg="#1f1f1f",
    surface="#2a2a2a",
    surface_alt="#333333",
    border="#3f3f3f",
    text="#e9e9e9",
    text_muted="#a3a3a3",
    text_disabled="#6b6b6b",
    accent="#4cc2ff",
    accent_text="#05202e",
    on="#4ade80",
    off="#767676",
    warn="#ff7a6b",
    caution="#ffb347",
)


def system_prefers_dark() -> bool:
    """Read the apps light/dark setting from the registry."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PERSONALIZE_KEY) as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return int(value) == 0
    except (OSError, ValueError):
        return False  # when in doubt, light remains the Windows default


def system_accent() -> str | None:
    """Accent color chosen in Windows, if it can be read."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, DWM_KEY) as key:
            value, _ = winreg.QueryValueEx(key, "AccentColor")
    except (OSError, ValueError):
        return None
    # AccentColor is stored as AABBGGRR: the bytes must be reversed.
    blue = (int(value) >> 16) & 0xFF
    green = (int(value) >> 8) & 0xFF
    red = int(value) & 0xFF
    return f"#{red:02x}{green:02x}{blue:02x}"


# Minimum contrast required of a label laid on the accent. It stays below
# WCAG AA's 4.5:1: these are short UI texts on a flat fill, and strictly
# aiming for 4.5 would reject the Windows blue by a 0.001 margin.
MIN_TEXT_CONTRAST = 4.0


def relative_luminance(color: str) -> float:
    """WCAG relative luminance of a #rrggbb color.

    sRGB components are gamma-encoded: comparing them as-is skews the
    calculation and picks the wrong text color. So they are linearized
    before being weighted.
    """
    channels = []
    for index in (1, 3, 5):
        value = int(color[index : index + 2], 16) / 255
        channels.append(
            value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
        )
    red, green, blue = channels
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast_ratio(first: str, second: str) -> float:
    """WCAG contrast ratio between two colors, from 1 to 21."""
    light, dark = sorted((relative_luminance(first), relative_luminance(second)))
    return (dark + 0.05) / (light + 0.05)


def readable_on(color: str) -> str:
    """Text color to lay on this background: white by preference.

    White is the convention on a colored flat fill -- it is what Windows does
    on its accent. Switch to black only when white no longer holds the
    contrast, which happens on light accents.
    """
    if contrast_ratio("#ffffff", color) >= MIN_TEXT_CONTRAST:
        return "#ffffff"
    if contrast_ratio("#000000", color) >= MIN_TEXT_CONTRAST:
        return "#000000"
    return (
        "#000000"
        if contrast_ratio("#000000", color) > contrast_ratio("#ffffff", color)
        else "#ffffff"
    )


def _mix(color: str, target: str, ratio: float) -> str:
    """Blend two colors, `ratio` being the share of `target`."""
    out = []
    for index in (1, 3, 5):
        first = int(color[index : index + 2], 16)
        second = int(target[index : index + 2], 16)
        out.append(round(first * (1 - ratio) + second * ratio))
    return "#{:02x}{:02x}{:02x}".format(*out)


# Target luminance for the accent, so that text laid on it keeps a
# comfortable contrast: light on a dark background, deep on a light one.
# It is also what Windows 11 does, whose accents get lighter in dark
# mode.
ACCENT_TARGET_DARK = 0.45
ACCENT_TARGET_LIGHT = 0.22


def _best_text_contrast(color: str) -> float:
    return max(contrast_ratio("#ffffff", color), contrast_ratio("#000000", color))


def fit_accent(accent: str, dark: bool) -> str:
    """Fit the Windows accent to the background, without touching its hue.

    In light mode, leave it alone: the accent as Windows shows it is what the
    user recognizes, and it already stands out on a pale background. It is
    only corrected in the rare case where no text would be readable on it.

    In dark mode, it is different: the default blue has a luminance of 0.18
    and drowns on a 0.02 background. It is lightened until it stands out,
    as Windows 11 does with its accents in dark mode.
    """
    if not dark:
        if _best_text_contrast(accent) >= MIN_TEXT_CONTRAST:
            return accent
        return _approach(accent, "#000000", ACCENT_TARGET_LIGHT, dark=False)
    return _approach(accent, "#ffffff", ACCENT_TARGET_DARK, dark=True)


def _approach(accent: str, target: str, wanted: float, dark: bool) -> str:
    """Blend the accent toward `target` until the target luminance is reached."""
    if (relative_luminance(accent) >= wanted) if dark else (
        relative_luminance(accent) <= wanted
    ):
        return accent
    candidate = accent
    ratio = 0.05
    while ratio <= 1.0:
        candidate = _mix(accent, target, ratio)
        luminance = relative_luminance(candidate)
        if (luminance >= wanted) if dark else (luminance <= wanted):
            break
        ratio += 0.05
    return candidate


def resolve(mode: str) -> Palette:
    """Effective palette for a given mode, system accent included."""
    if mode not in MODES:
        mode = "system"
    dark = system_prefers_dark() if mode == "system" else (mode == "dark")
    base = DARK if dark else LIGHT
    accent = system_accent()
    if accent is None:
        return base
    accent = fit_accent(accent, dark)
    return Palette(
        **{
            **base.__dict__,
            "accent": accent,
            "accent_text": readable_on(accent),
        }
    )


# -------------------------------------------------------------------- title bar

_dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
_user32 = ctypes.WinDLL("user32", use_last_error=True)
GA_ROOT = 2


def apply_titlebar(window: tk.Misc, dark: bool) -> None:
    """Switch the title bar to light or dark.

    Tk only exposes its widget's handle: the window that carries the title
    bar is its root ancestor.
    """
    try:
        window.update_idletasks()  # the window must exist on the system side
        hwnd = _user32.GetAncestor(window.winfo_id(), GA_ROOT)
        if not hwnd:
            return
        value = ctypes.c_int(1 if dark else 0)
        for attribute in (
            DWMWA_USE_IMMERSIVE_DARK_MODE,
            DWMWA_USE_IMMERSIVE_DARK_MODE_OLD,
        ):
            result = _dwmapi.DwmSetWindowAttribute(
                wintypes.HWND(hwnd),
                wintypes.DWORD(attribute),
                ctypes.byref(value),
                ctypes.sizeof(value),
            )
            if result == 0:
                break
    except (tk.TclError, OSError, AttributeError):
        pass  # without a tinted title bar, the application remains usable


# ------------------------------------------------------------------ application

# Classic Tk widgets that ttk.Style does not reach: they must be recolored
# one by one, including on a live theme change.
_PLAIN_OPTIONS = {
    "Listbox": ("background", "foreground", "selectbackground", "selectforeground",
                "highlightbackground", "highlightcolor"),
    "Text": ("background", "foreground", "insertbackground", "highlightbackground"),
    "Canvas": ("background", "highlightbackground"),
    "Toplevel": ("background",),
    "Tk": ("background",),
    "Frame": ("background",),
    "Label": ("background", "foreground"),
}


def apply(root: tk.Misc, mode: str) -> Palette:
    """Apply a theme to a window and all its content."""
    palette = resolve(mode)
    style = ttk.Style(root)
    # `clam` is the only built-in theme that is fully colorable: see the header.
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    _configure_styles(style, palette)
    _style_combobox_popup(root, palette)
    _apply_plain_widgets(root, palette)
    apply_titlebar(root, palette.dark)
    return palette


def _configure_styles(style: ttk.Style, p: Palette) -> None:
    style.configure(
        ".",
        background=p.bg,
        foreground=p.text,
        fieldbackground=p.surface,
        bordercolor=p.border,
        lightcolor=p.bg,
        darkcolor=p.bg,
        troughcolor=p.surface_alt,
        selectbackground=p.accent,
        selectforeground=p.accent_text,
        focuscolor=p.accent,
        insertcolor=p.text,
    )
    style.map(".", foreground=[("disabled", p.text_disabled)])

    style.configure("TFrame", background=p.bg)
    style.configure("TLabel", background=p.bg, foreground=p.text)
    style.configure("Hint.TLabel", background=p.bg, foreground=p.text_muted)
    style.configure("Title.TLabel", background=p.bg, foreground=p.text,
                    font=("", 11, "bold"))
    style.configure("Section.TLabel", background=p.bg, foreground=p.text,
                    font=("", 9, "bold"))
    # A link: the accent color and an underline, as in a browser.
    style.configure("Link.TLabel", background=p.bg, foreground=p.accent,
                    font=("", 9, "underline"))
    style.configure("Banner.TLabel", background=p.bg, foreground=p.text,
                    font=("", 16, "bold"))
    style.configure("TLabelframe", background=p.bg, bordercolor=p.border)
    style.configure("TLabelframe.Label", background=p.bg, foreground=p.text_muted)

    # "Card" variants: a frame laid on a slightly distinct background reads
    # much better than a plain rule. Since ttk does not inherit the parent's
    # background, each widget type placed in a card needs its own variant
    # -- they are applied automatically by
    # `apply_card_styles`.
    style.configure("Card.TLabelframe", background=p.surface, bordercolor=p.border,
                    relief="solid", borderwidth=1)
    style.configure("Card.TLabelframe.Label", background=p.surface,
                    foreground=p.text_muted, font=("", 9, "bold"))
    style.configure("Card.TFrame", background=p.surface)
    # The banner announcing hardware changes that await an administrator.
    style.configure("Admin.TFrame", background=p.surface)
    style.configure("Admin.TLabel", background=p.surface, foreground=p.warn,
                    font=("", 9, "bold"))
    style.configure("Card.TLabel", background=p.surface, foreground=p.text)
    style.configure("Hint.Card.TLabel", background=p.surface, foreground=p.text_muted)
    style.configure("Section.Card.TLabel", background=p.surface, foreground=p.text,
                    font=("", 9, "bold"))
    for widget in ("Card.TCheckbutton", "Card.TRadiobutton"):
        style.configure(widget, background=p.surface, foreground=p.text,
                        indicatorbackground=p.bg, indicatorforeground=p.accent_text,
                        focusthickness=1, padding=(2, 3))
        style.map(
            widget,
            background=[("active", p.surface)],
            indicatorbackground=[("selected", p.accent), ("disabled", p.surface_alt),
                                 ("active", _mix(p.bg, p.accent, 0.3))],
            indicatorforeground=[("selected", p.accent_text)],
            foreground=[("disabled", p.text_disabled)],
        )

    style.configure(
        "TButton",
        background=p.surface_alt,
        foreground=p.text,
        bordercolor=p.border,
        lightcolor=p.surface_alt,
        darkcolor=p.surface_alt,
        focusthickness=1,
        padding=(10, 5),
    )
    style.map(
        "TButton",
        background=[("pressed", p.accent), ("active", _mix(p.surface_alt, p.accent, 0.25)),
                    ("disabled", p.bg)],
        foreground=[("pressed", p.accent_text), ("disabled", p.text_disabled)],
        bordercolor=[("focus", p.accent)],
    )

    for widget in ("TCheckbutton", "TRadiobutton"):
        style.configure(
            widget,
            background=p.bg,
            foreground=p.text,
            indicatorbackground=p.surface,
            indicatorforeground=p.accent_text,
            focusthickness=1,
            padding=(2, 3),
        )
        style.map(
            widget,
            background=[("active", p.bg)],
            indicatorbackground=[("selected", p.accent), ("disabled", p.surface_alt),
                                 ("active", _mix(p.surface, p.accent, 0.3))],
            indicatorforeground=[("selected", p.accent_text)],
            foreground=[("disabled", p.text_disabled)],
        )

    for widget in ("TEntry", "TSpinbox", "TCombobox"):
        style.configure(
            widget,
            fieldbackground=p.surface,
            foreground=p.text,
            bordercolor=p.border,
            lightcolor=p.border,
            darkcolor=p.border,
            arrowcolor=p.text,
            insertcolor=p.text,
            padding=4,
        )
        style.map(
            widget,
            fieldbackground=[("disabled", p.bg), ("readonly", p.surface_alt)],
            bordercolor=[("focus", p.accent)],
            foreground=[("disabled", p.text_disabled)],
        )

    style.configure("TNotebook", background=p.bg, bordercolor=p.border, tabmargins=(2, 4, 2, 0))
    style.configure(
        "TNotebook.Tab",
        background=p.surface_alt,
        foreground=p.text_muted,
        bordercolor=p.border,
        lightcolor=p.surface_alt,
        padding=(14, 6),
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", p.bg), ("active", _mix(p.surface_alt, p.accent, 0.18))],
        foreground=[("selected", p.text)],
        # The active tab must blend into the page it opens.
        lightcolor=[("selected", p.bg)],
        expand=[("selected", (1, 1, 1, 0))],
    )

    style.configure(
        "Treeview",
        background=p.surface,
        fieldbackground=p.surface,
        foreground=p.text,
        bordercolor=p.border,
        lightcolor=p.border,
        darkcolor=p.border,
        rowheight=22,
    )
    style.map(
        "Treeview",
        background=[("selected", p.accent)],
        foreground=[("selected", p.accent_text)],
    )
    style.configure(
        "Treeview.Heading",
        background=p.surface_alt,
        foreground=p.text,
        bordercolor=p.border,
        relief="flat",
        padding=(6, 4),
    )
    style.map(
        "Treeview.Heading",
        background=[("active", _mix(p.surface_alt, p.accent, 0.2))],
    )

    style.configure(
        "TProgressbar",
        background=p.accent,
        troughcolor=p.surface_alt,
        bordercolor=p.border,
        lightcolor=p.accent,
        darkcolor=p.accent,
    )
    # Sliders: without their own style, `clam` draws them light once
    # disabled, and the inert slider stood out more than the active one.
    style.configure(
        "Horizontal.TScale",
        background=p.accent,
        troughcolor=p.surface_alt,
        bordercolor=p.border,
        lightcolor=p.accent,
        darkcolor=p.accent,
    )
    style.map(
        "Horizontal.TScale",
        background=[("disabled", p.border), ("active", _mix(p.accent, p.text, 0.2))],
        lightcolor=[("disabled", p.border)],
        darkcolor=[("disabled", p.border)],
        troughcolor=[("disabled", p.bg)],
    )
    style.configure(
        "TScrollbar",
        background=p.surface_alt,
        troughcolor=p.bg,
        bordercolor=p.bg,
        arrowcolor=p.text_muted,
        lightcolor=p.surface_alt,
        darkcolor=p.surface_alt,
    )
    style.map("TScrollbar", background=[("active", _mix(p.surface_alt, p.accent, 0.3))])
    style.configure("TSeparator", background=p.border)


def _style_combobox_popup(root: tk.Misc, p: Palette) -> None:
    """Color the drop-down list of comboboxes.

    It is not a ttk widget but an internal Listbox created by Tk when it
    opens: ttk.Style does not reach it, and without this it stays white in
    the middle of a dark window.
    """
    for option, value in (
        ("*TCombobox*Listbox.background", p.surface),
        ("*TCombobox*Listbox.foreground", p.text),
        ("*TCombobox*Listbox.selectBackground", p.accent),
        ("*TCombobox*Listbox.selectForeground", p.accent_text),
    ):
        try:
            root.option_add(option, value)
        except tk.TclError:
            pass


def _apply_plain_widgets(widget: tk.Misc, p: Palette) -> None:
    """Recolor classic Tk widgets, walking down the widget tree."""
    colors = {
        "background": p.surface if widget.winfo_class() == "Listbox" else p.bg,
        "foreground": p.text,
        "selectbackground": p.accent,
        "selectforeground": p.accent_text,
        "highlightbackground": p.border,
        "highlightcolor": p.accent,
        "insertbackground": p.text,
    }
    wanted = _PLAIN_OPTIONS.get(widget.winfo_class())
    if wanted:
        for option in wanted:
            try:
                widget.configure(**{option: colors[option]})
            except (tk.TclError, KeyError):
                pass  # some options do not exist depending on the Tk version
    for child in widget.winfo_children():
        _apply_plain_widgets(child, p)


def refresh_plain_widgets(root: tk.Misc, palette: Palette) -> None:
    """Recolor classic widgets created after the theme was applied."""
    _apply_plain_widgets(root, palette)


# Base style of a widget -> its variant laid on a card.
_CARD_STYLES = {
    "TLabelframe": "Card.TLabelframe",
    "TFrame": "Card.TFrame",
    "TLabel": "Card.TLabel",
    "Hint.TLabel": "Hint.Card.TLabel",
    "Section.TLabel": "Section.Card.TLabel",
    "Title.TLabel": "Card.TLabel",
    "TCheckbutton": "Card.TCheckbutton",
    "TRadiobutton": "Card.TRadiobutton",
}
_TTK_DEFAULT_STYLE = {
    "TLabelframe": "TLabelframe",
    "TFrame": "TFrame",
    "TLabel": "TLabel",
    "TCheckbutton": "TCheckbutton",
    "TRadiobutton": "TRadiobutton",
}


def apply_card_styles(widget: tk.Misc, inside_card: bool = False) -> None:
    """Switch everything inside a frame to the "card" style.

    A ttk widget's background is not inherited from its parent: laying a
    frame on a distinct background means redeclaring every widget it holds.
    Rather than doing it by hand everywhere, the widget tree is walked once
    the theme is applied.
    """
    klass = widget.winfo_class()
    # A frame is itself the card: so it must get the style, whether nested
    # or not. Its descendants follow.
    is_card = klass == "TLabelframe"
    if (inside_card or is_card) and klass in _TTK_DEFAULT_STYLE:
        try:
            current = str(widget.cget("style")) or _TTK_DEFAULT_STYLE[klass]
            target = _CARD_STYLES.get(current)
            if target:
                widget.configure(style=target)
        except tk.TclError:
            pass
    for child in widget.winfo_children():
        apply_card_styles(child, inside_card or is_card)
