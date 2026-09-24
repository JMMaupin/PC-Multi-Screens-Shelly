"""Raccourcis clavier globaux : description, lecture, test de disponibilite.

Un raccourci global se reserve aupres de Windows par `RegisterHotKey`. Le
premier programme qui le reserve le garde : un second essai echoue, ce qui
donne justement le moyen de savoir s'il est libre. On le reserve un instant,
on le rend aussitot.

La reservation effective, elle, vit dans la fenetre de notification
(`shell.TrayWindow`) : Windows lie un raccourci au thread qui l'a pose, et
c'est la boucle de messages de ce thread qui recoit `WM_HOTKEY`.

Le raccourci se range dans la configuration sous sa forme lisible,
« Ctrl+Win+Alt+P », pour qu'un `config.json` ouvert a la main se comprenne.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
from ctypes import wintypes

from .api import user32

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
# Maintenir la touche n'envoie qu'un seul evenement, pas une rafale.
MOD_NOREPEAT = 0x4000

# Ordre d'affichage, celui des claviers : Ctrl, Win, Alt, Shift.
MODIFIERS = (
    ("Ctrl", MOD_CONTROL),
    ("Win", MOD_WIN),
    ("Alt", MOD_ALT),
    ("Shift", MOD_SHIFT),
)

# Touches proposees : lettres, chiffres, touches de fonction. Les codes
# virtuels des lettres et des chiffres sont leur code ASCII majuscule.
KEYS: dict[str, int] = {
    **{chr(code): code for code in range(ord("A"), ord("Z") + 1)},
    **{chr(code): code for code in range(ord("0"), ord("9") + 1)},
    **{f"F{number}": 0x6F + number for number in range(1, 13)},
}

# Identifiant du test de disponibilite, distinct de celui de la fenetre
# de notification pour ne jamais lui retirer le sien par megarde.
_PROBE_ID = 0xB00F

user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.UnregisterHotKey.restype = wintypes.BOOL


@dataclass(frozen=True)
class Hotkey:
    """Une combinaison : des modificateurs et une touche."""

    modifiers: int
    key: str

    @property
    def vk(self) -> int:
        return KEYS[self.key]

    @property
    def usable(self) -> bool:
        """Faut-il au moins un vrai modificateur.

        Sans Ctrl, Alt ni Win, la touche serait volee a toutes les
        applications : taper un « P » dans un document declencherait le
        raccourci. Shift seul ne suffit pas, pour la meme raison.
        """
        return bool(self.modifiers & (MOD_CONTROL | MOD_ALT | MOD_WIN))

    def __str__(self) -> str:
        names = [name for name, flag in MODIFIERS if self.modifiers & flag]
        return "+".join(names + [self.key])


def parse(text: str) -> Hotkey | None:
    """Lit « Ctrl+Win+Alt+P » ; `None` si vide ou illisible."""
    parts = [part.strip() for part in (text or "").split("+") if part.strip()]
    if not parts:
        return None
    flags = {name.lower(): flag for name, flag in MODIFIERS}
    # Quelques synonymes courants, pour une saisie a la main du fichier.
    flags.update({"control": MOD_CONTROL, "windows": MOD_WIN, "super": MOD_WIN})
    modifiers = 0
    for part in parts[:-1]:
        flag = flags.get(part.lower())
        if flag is None:
            return None
        modifiers |= flag
    key = parts[-1].upper()
    if key not in KEYS:
        return None
    return Hotkey(modifiers, key)


def is_free(hotkey: Hotkey) -> bool:
    """Vrai si aucun programme ne tient deja ce raccourci.

    Reserve sans fenetre, pour le thread appelant, puis rendu aussitot. Un
    raccourci que cette application tient deja repond donc « occupe » : a
    l'appelant de reconnaitre le sien.

    Limite connue : quelques raccourcis de Windows (Win+L, par exemple) ne
    passent pas par ce mecanisme. Leur reservation reussit, mais Windows
    les intercepte avant nous ; aucun test ne peut le deceler.
    """
    if not user32.RegisterHotKey(None, _PROBE_ID, hotkey.modifiers | MOD_NOREPEAT, hotkey.vk):
        return False
    user32.UnregisterHotKey(None, _PROBE_ID)
    return True
