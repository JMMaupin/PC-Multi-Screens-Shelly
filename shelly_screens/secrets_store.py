"""Stockage du mot de passe de l'appareil.

Un mot de passe en clair dans un fichier de configuration est lisible par
tout ce qui tourne sous la session -- et le fichier se retrouve vite dans
une sauvegarde ou une copie de dossier. On le chiffre donc avec DPAPI, le
service de Windows prevu pour cela : la cle derive du compte utilisateur,
et le chiffre n'est dechiffrable que par ce compte, sur cette machine.

Ce n'est pas un coffre-fort : un programme lance sous la meme session peut
demander a Windows de dechiffrer. Cela protege du fichier recopie ailleurs
ou lu par un autre compte, pas d'un logiciel malveillant deja en place.
"""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes

crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# Entropie propre a l'application : un chiffre produit ici ne se dechiffre
# pas depuis un autre programme, meme sous le meme compte.
ENTROPY = b"shelly-screens/v1"
CRYPTPROTECT_UI_FORBIDDEN = 0x01
PREFIX = "dpapi:"


class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


crypt32.CryptProtectData.argtypes = [
    ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR, ctypes.POINTER(DATA_BLOB),
    ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
]
crypt32.CryptProtectData.restype = wintypes.BOOL
crypt32.CryptUnprotectData.argtypes = [
    ctypes.POINTER(DATA_BLOB), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(DATA_BLOB),
    ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
]
crypt32.CryptUnprotectData.restype = wintypes.BOOL
kernel32.LocalFree.argtypes = [ctypes.c_void_p]


class _Blob:
    """Garde le tampon en vie aussi longtemps que la structure qui le vise."""

    def __init__(self, data: bytes) -> None:
        self._buffer = ctypes.create_string_buffer(data, len(data))
        self.value = DATA_BLOB(
            len(data), ctypes.cast(self._buffer, ctypes.POINTER(ctypes.c_byte))
        )


def _read(blob: DATA_BLOB) -> bytes:
    data = ctypes.string_at(blob.pbData, blob.cbData)
    kernel32.LocalFree(blob.pbData)
    return data


def protect(secret: str) -> str:
    """Chiffre un secret ; renvoie une chaine stockable telle quelle."""
    if not secret:
        return ""
    source = _Blob(secret.encode("utf-8"))
    entropy = _Blob(ENTROPY)
    out = DATA_BLOB()
    ok = crypt32.CryptProtectData(
        ctypes.byref(source.value), "Shelly Screens", ctypes.byref(entropy.value),
        None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out),
    )
    if not ok:
        raise OSError(ctypes.get_last_error(), "CryptProtectData failed")
    return PREFIX + base64.b64encode(_read(out)).decode("ascii")


def unprotect(stored: str) -> str:
    """Dechiffre une valeur produite par `protect`.

    Une valeur sans prefixe est rendue telle quelle : c'est un mot de passe
    ecrit a la main dans le fichier, ou l'heritage d'une version anterieure
    qui les stockait en clair.
    """
    if not stored:
        return ""
    if not stored.startswith(PREFIX):
        return stored
    try:
        raw = base64.b64decode(stored[len(PREFIX):])
    except (ValueError, TypeError):
        return ""
    source = _Blob(raw)
    entropy = _Blob(ENTROPY)
    out = DATA_BLOB()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(source.value), None, ctypes.byref(entropy.value),
        None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out),
    )
    if not ok:
        # Chiffre produit par un autre compte ou une autre machine.
        return ""
    return _read(out).decode("utf-8", errors="replace")


