"""Une seule instance a la fois.

Deux instances se disputent le meme fichier de configuration : chacune
garde la sienne en memoire et l'ecrit entiere a chaque enregistrement, si
bien que la derniere a ecrire efface le travail de l'autre. C'est ainsi
qu'une calibration fraichement relevee a disparu, remplacee par une copie
plus ancienne.

Le verrou est un mutex nomme de Windows : il appartient au processus et
disparait avec lui, meme si celui-ci est tue. Un fichier verrou, lui,
resterait apres un arret brutal et bloquerait tout lancement ulterieur.

Relancer l'application n'affiche pas un refus : la fenetre de reglages de
l'instance en place s'ouvre. C'est ce qu'on attend d'un programme a icone,
dont la fenetre principale est souvent fermee.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)

# `Local\` limite la portee a la session Windows ouverte : deux comptes
# connectes en parallele gardent chacun leur instance.
MUTEX_NAME = r"Local\ShellyScreens.SingleInstance"
ERROR_ALREADY_EXISTS = 183

kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND
user32.PostMessageW.argtypes = [
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]

_handle: wintypes.HANDLE | None = None


def acquire() -> bool:
    """Prend le verrou. Faux si une autre instance le detient deja."""
    global _handle
    handle = kernel32.CreateMutexW(None, True, MUTEX_NAME)
    if not handle:
        # Sans verrou possible, mieux vaut laisser demarrer que bloquer.
        return True
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False
    _handle = handle
    return True


def release() -> None:
    """Rend le verrou. Windows le ferait de toute facon a la sortie."""
    global _handle
    if _handle:
        kernel32.CloseHandle(_handle)
        _handle = None


def wake_existing(window_class: str, message: int) -> bool:
    """Demande a l'instance en place de se montrer."""
    hwnd = user32.FindWindowW(window_class, None)
    if not hwnd:
        return False
    user32.PostMessageW(hwnd, message, 0, 0)
    return True
