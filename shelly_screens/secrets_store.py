"""Storage of the device password.

A plaintext password in a configuration file is readable by anything
running on the machine -- and the file quickly ends up in a backup or a
folder copy. So it is encrypted with DPAPI, the Windows service designed
for this.

The key is the machine's, not an account's: the power strips are shared by
every account on the PC, and each account's instance must be able to
command them. The ciphertext can therefore be decrypted by any account on
this machine, and by nothing elsewhere.

This is not a vault: a program running on the machine can ask Windows to
decrypt. It protects against the file being copied elsewhere, not against
a local user or malware already in place -- and no scheme could, since
every account's instance needs the password to switch its screens.

Before version 2.0, ciphertexts were tied to one account. `unprotect`
still reads them for that account, which is how the import of an older
installation re-encrypts them with the machine key.
"""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes

crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# Application-specific entropy: a ciphertext produced here can't be
# decrypted from another program, even under the same account.
ENTROPY = b"shelly-screens/v1"
CRYPTPROTECT_UI_FORBIDDEN = 0x01
CRYPTPROTECT_LOCAL_MACHINE = 0x04
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
    """Keeps the buffer alive as long as the structure pointing to it."""

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
    """Encrypts a secret with the machine key; returns a string that can be
    stored as is."""
    if not secret:
        return ""
    source = _Blob(secret.encode("utf-8"))
    entropy = _Blob(ENTROPY)
    out = DATA_BLOB()
    ok = crypt32.CryptProtectData(
        ctypes.byref(source.value), "Shelly Screens", ctypes.byref(entropy.value),
        None, None, CRYPTPROTECT_UI_FORBIDDEN | CRYPTPROTECT_LOCAL_MACHINE,
        ctypes.byref(out),
    )
    if not ok:
        raise OSError(ctypes.get_last_error(), "CryptProtectData failed")
    return PREFIX + base64.b64encode(_read(out)).decode("ascii")


def unprotect(stored: str) -> str:
    """Decrypts a value produced by `protect`.

    A value without the prefix is returned as is: it is a password written
    by hand into the file, or a leftover from an earlier version that
    stored them in plaintext.
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
        # Ciphertext produced on another machine, or tied to another
        # account by a version older than 2.0.
        return ""
    return _read(out).decode("utf-8", errors="replace")


