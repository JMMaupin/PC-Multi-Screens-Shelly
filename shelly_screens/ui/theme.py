"""Themes de l'interface : systeme, clair, sombre.

Trois choix possibles, et `system` suit le reglage de Windows en temps reel.

Deux points meritent une explication.

Le theme ttk retenu est `clam`, dans les trois cas. Le theme natif `vista`
est plus joli en clair, mais il dessine ses widgets avec les images du
systeme : ses fonds ne se colorent pas, et un mode sombre y reste
irremediablement clair par endroits. `clam` est entierement pilotable, au
prix de quelques indicateurs plus sobres -- un echange qui vaut la coherence
entre les trois themes.

La barre de titre, elle, n'appartient pas a Tk mais au gestionnaire de
fenetres. On la fait basculer par DwmSetWindowAttribute, sans quoi une
fenetre sombre garderait un bandeau blanc.
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
# Attribut Windows 11 / Windows 10 20H1 ; 19 sur les versions plus anciennes.
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19


@dataclass(frozen=True)
class Palette:
    """Couleurs d'un theme."""

    name: str
    dark: bool
    bg: str  # fond de fenetre
    surface: str  # cadres, listes, champs
    surface_alt: str  # lignes alternees, en-tetes
    border: str
    text: str
    text_muted: str  # aides et commentaires
    text_disabled: str
    accent: str  # selection, onglet actif
    accent_text: str  # texte pose sur l'accent
    on: str  # prise alimentee
    off: str  # prise coupee
    warn: str  # appareil injoignable


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
)


def system_prefers_dark() -> bool:
    """Lit le reglage clair/sombre des applications dans le registre."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PERSONALIZE_KEY) as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return int(value) == 0
    except (OSError, ValueError):
        return False  # en cas de doute, le clair reste le defaut de Windows


def system_accent() -> str | None:
    """Couleur d'accentuation choisie dans Windows, si elle est lisible."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, DWM_KEY) as key:
            value, _ = winreg.QueryValueEx(key, "AccentColor")
    except (OSError, ValueError):
        return None
    # AccentColor est stocke en AABBGGRR : il faut inverser les octets.
    blue = (int(value) >> 16) & 0xFF
    green = (int(value) >> 8) & 0xFF
    red = int(value) & 0xFF
    return f"#{red:02x}{green:02x}{blue:02x}"


# Contraste minimal exige d'un libelle pose sur l'accent. On reste en deca
# des 4,5:1 de WCAG AA : il s'agit de textes d'interface courts sur un aplat,
# et viser 4,5 strictement rejetterait le bleu Windows pour 0,001 de marge.
MIN_TEXT_CONTRAST = 4.0


def relative_luminance(color: str) -> float:
    """Luminance relative WCAG d'une couleur #rrggbb.

    Les composantes sRGB sont encodees en gamma : les comparer telles quelles
    fausse le calcul et fait choisir la mauvaise couleur de texte. On les
    linearise donc avant de les ponderer.
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
    """Rapport de contraste WCAG entre deux couleurs, de 1 a 21."""
    light, dark = sorted((relative_luminance(first), relative_luminance(second)))
    return (dark + 0.05) / (light + 0.05)


def readable_on(color: str) -> str:
    """Couleur de texte a poser sur ce fond : blanc de preference.

    Le blanc est la convention sur un aplat colore -- c'est ce que fait
    Windows sur son accent. On ne bascule au noir que lorsque le blanc ne
    tient plus le contraste, ce qui arrive sur les accents clairs.
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
    """Melange deux couleurs, `ratio` etant la part de `target`."""
    out = []
    for index in (1, 3, 5):
        first = int(color[index : index + 2], 16)
        second = int(target[index : index + 2], 16)
        out.append(round(first * (1 - ratio) + second * ratio))
    return "#{:02x}{:02x}{:02x}".format(*out)


# Luminance visee pour l'accent, de facon qu'un texte pose dessus garde un
# contraste confortable : clair sur fond sombre, soutenu sur fond clair.
# C'est aussi ce que fait Windows 11, dont les accents s'eclaircissent en
# mode sombre.
ACCENT_TARGET_DARK = 0.45
ACCENT_TARGET_LIGHT = 0.22


def _best_text_contrast(color: str) -> float:
    return max(contrast_ratio("#ffffff", color), contrast_ratio("#000000", color))


def fit_accent(accent: str, dark: bool) -> str:
    """Adapte l'accent de Windows au fond, sans toucher a sa teinte.

    En clair, on n'y touche pas : l'accent tel que Windows l'affiche est ce
    que l'utilisateur reconnait, et il ressort deja sur un fond pale. On ne
    le corrige que dans le cas rare ou aucun texte n'y serait lisible.

    En sombre, c'est different : le bleu par defaut a une luminance de 0,18
    et se noie sur un fond a 0,02. On l'eclaircit jusqu'a ce qu'il ressorte,
    comme le fait Windows 11 avec ses accents en mode sombre.
    """
    if not dark:
        if _best_text_contrast(accent) >= MIN_TEXT_CONTRAST:
            return accent
        return _approach(accent, "#000000", ACCENT_TARGET_LIGHT, dark=False)
    return _approach(accent, "#ffffff", ACCENT_TARGET_DARK, dark=True)


def _approach(accent: str, target: str, wanted: float, dark: bool) -> str:
    """Melange l'accent vers `target` jusqu'a la luminance visee."""
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
    """Palette effective pour un mode donne, accent systeme compris."""
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


# --------------------------------------------------------------- barre de titre

_dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
_user32 = ctypes.WinDLL("user32", use_last_error=True)
GA_ROOT = 2


def apply_titlebar(window: tk.Misc, dark: bool) -> None:
    """Fait basculer la barre de titre en clair ou en sombre.

    Tk n'expose que le handle de son widget : la fenetre qui porte la barre de
    titre est son ancetre racine.
    """
    try:
        window.update_idletasks()  # la fenetre doit exister cote systeme
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
        pass  # sans barre de titre teintee, l'application reste utilisable


# ------------------------------------------------------------------ application

# Widgets Tk classiques que ttk.Style ne touche pas : il faut les recolorer
# un par un, y compris lors d'un changement de theme a chaud.
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
    """Applique un theme a une fenetre et a tout son contenu."""
    palette = resolve(mode)
    style = ttk.Style(root)
    # `clam` est le seul theme integre entierement colorable : voir l'entete.
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
    style.configure("TLabelframe", background=p.bg, bordercolor=p.border)
    style.configure("TLabelframe.Label", background=p.bg, foreground=p.text_muted)

    # Variantes « carte » : un cadre pose sur un fond legerement distinct se
    # lit bien mieux qu'un simple filet. ttk n'heritant pas le fond du
    # parent, chaque type de widget place dans une carte a besoin de sa
    # declinaison -- elles sont appliquees automatiquement par
    # `apply_card_styles`.
    style.configure("Card.TLabelframe", background=p.surface, bordercolor=p.border,
                    relief="solid", borderwidth=1)
    style.configure("Card.TLabelframe.Label", background=p.surface,
                    foreground=p.text_muted, font=("", 9, "bold"))
    style.configure("Card.TFrame", background=p.surface)
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
        # L'onglet actif doit se fondre dans la page qu'il ouvre.
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
    # Curseurs : sans style propre, `clam` les dessine en clair une fois
    # desactives, et le curseur inerte ressortait plus que l'actif.
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
    """Colore la liste deroulante des listes de choix.

    Elle n'est pas un widget ttk mais une Listbox interne creee par Tk au
    moment de l'ouverture : ttk.Style ne l'atteint pas, et sans cela elle
    reste blanche au milieu d'une fenetre sombre.
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
    """Recolore les widgets Tk classiques, en descendant l'arborescence."""
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
                pass  # certaines options n'existent pas selon la version de Tk
    for child in widget.winfo_children():
        _apply_plain_widgets(child, p)


def refresh_plain_widgets(root: tk.Misc, palette: Palette) -> None:
    """Recolore les widgets classiques crees apres l'application du theme."""
    _apply_plain_widgets(root, palette)


# Style de base d'un widget -> sa declinaison posee sur une carte.
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
    """Bascule en style « carte » tout ce qui se trouve dans un cadre.

    Le fond d'un widget ttk ne s'herite pas de son parent : poser un cadre
    sur un fond distinct impose de redeclarer chaque widget qu'il contient.
    Plutot que de le faire a la main partout, on parcourt l'arborescence une
    fois le theme applique.
    """
    klass = widget.winfo_class()
    # Un cadre est lui-meme la carte : il doit donc recevoir le style, qu'il
    # soit imbrique ou non. Ses descendants suivent.
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
