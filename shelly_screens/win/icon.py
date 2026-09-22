"""Icone de la zone de notification.

L'icone est celle de l'application, surmontee d'une pastille qui dit l'etat
des prises : verte quand des ecrans sont alimentes, grise quand tout est
coupe, orange quand un appareil manque a l'appel, rouge quand plus rien ne
repond.

Le detail chiffre -- combien de prises, combien de watts -- tient dans
l'infobulle et dans le menu. A la taille reelle d'affichage, seize pixels
de cote, une pastille se lit d'un coup d'oeil la ou un decompte ne se
lirait pas du tout.

Le fichier .ico produit contient trois tailles, chacune dessinee a partir
du visuel de la bonne definition : laisser Windows reduire une seule image
donnerait un rendu trouble.
"""

from __future__ import annotations

import struct
import tempfile
from pathlib import Path

from .images import Pixels, load_png

# Le jeu d'icones vit a la racine du projet, tel que le generateur
# `icongen_windows.py` le produit : on recopie son dossier sans le
# reorganiser, pour qu'une regeneration se resume a un remplacement.
ASSETS = Path(__file__).resolve().parent.parent.parent / "windows-icons"
# Tailles composees pour la zone de notification. Les intermediaires
# couvrent les ecrans a 125 %, 150 % et 250 %, ou Windows reclame 20, 24
# et 40 pixels plutot que 16 ou 32.
SIZES = (16, 20, 24, 32, 40, 48)
SOURCES = {size: ASSETS / f"icon-{size}.png" for size in SIZES}
APP_ICON = ASSETS / "icon.ico"
LARGE_PNG = ASSETS / "icon-256.png"

# Etats possibles et couleur de leur pastille.
STATUS_COLORS = {
    "on": (60, 200, 90, 255),  # au moins une prise alimentee
    "off": (150, 150, 156, 255),  # tout coupe, mais tout repond
    "warning": (230, 155, 60, 255),  # un appareil manque ou refuse le mot de passe
    "offline": (220, 90, 70, 255),  # plus rien ne repond
}
BADGE_RING = (18, 20, 24, 255)  # cerne sombre, pour detacher du fond

# Numero de generation du dessin. Il entre dans le nom du fichier mis en
# cache : sans lui, une icone produite par une version anterieure serait
# reprise telle quelle, le fichier portant deja le bon nom.
RENDER_VERSION = 4

_cache: dict[tuple[int, str], Pixels] = {}


def _base(size: int) -> Pixels:
    """Visuel de l'application a la taille demandee."""
    key = (size, "base")
    if key not in _cache:
        _, _, pixels = load_png(SOURCES[size])
        _cache[key] = pixels
    return _cache[key]


def _blend(under: tuple[int, int, int, int], over: tuple[int, int, int, int]):
    """Compose `over` sur `under`, en tenant compte de la transparence."""
    alpha = over[3] / 255
    if alpha >= 1:
        return over
    if alpha <= 0:
        return under
    return (
        round(over[0] * alpha + under[0] * (1 - alpha)),
        round(over[1] * alpha + under[1] * (1 - alpha)),
        round(over[2] * alpha + under[2] * (1 - alpha)),
        max(under[3], over[3]),
    )


def compose(size: int, status: str) -> Pixels:
    """Visuel de l'application, pastille d'etat comprise."""
    color = STATUS_COLORS.get(status, STATUS_COLORS["off"])
    pixels = [list(row) for row in _base(size)]

    # Pastille au quart inferieur droit, proportionnelle a la taille.
    radius = max(2.5, size * 0.21)
    centre = size - radius - max(1.0, size * 0.04)
    ring = max(1.0, size * 0.05)

    for y in range(size):
        for x in range(size):
            distance = ((x + 0.5 - centre) ** 2 + (y + 0.5 - centre) ** 2) ** 0.5
            if distance <= radius - ring:
                pixels[y][x] = _blend(pixels[y][x], color)
            elif distance <= radius:
                # Bord adouci : l'anti-crenelage evite l'escalier a 16 pixels.
                edge = min(1.0, radius - distance + 1.0)
                pixels[y][x] = _blend(
                    pixels[y][x], (*BADGE_RING[:3], int(255 * max(0.0, edge)))
                )
    return pixels


def _image_entry(pixels: Pixels, size: int) -> bytes:
    """Une image d'un fichier ICO : en-tete DIB, pixels, masque."""
    body = bytearray()
    # Les lignes d'un DIB sont stockees de bas en haut, en BGRA.
    for y in reversed(range(size)):
        for x in range(size):
            r, g, b, a = pixels[y][x]
            body += bytes((b, g, r, a))
    # Masque AND : inutile en 32 bits, mais le format l'attend.
    mask_row = ((size + 31) // 32) * 4
    body += bytes(mask_row * size)

    header = struct.pack(
        "<IiiHHIIiiII",
        40,  # biSize
        size,  # biWidth
        size * 2,  # biHeight : image + masque
        1,  # biPlanes
        32,  # biBitCount
        0,  # biCompression = BI_RGB
        len(body),
        0, 0, 0, 0,
    )
    return header + bytes(body)


def build_ico(status: str) -> bytes:
    """Construit un fichier ICO multi-taille pour un etat donne."""
    images = [(size, _image_entry(compose(size, status), size)) for size in SIZES]

    directory = struct.pack("<HHH", 0, 1, len(images))
    entry_size = struct.calcsize("<BBBBHHII")
    offset = len(directory) + entry_size * len(images)

    entries = bytearray()
    for size, image in images:
        entries += struct.pack(
            "<BBBBHHII", size, size, 0, 0, 1, 32, len(image), offset
        )
        offset += len(image)
    return bytes(directory) + bytes(entries) + b"".join(img for _s, img in images)


def write_ico(status: str, path: Path | None = None) -> Path:
    """Ecrit l'icone d'un etat sur disque et renvoie son chemin.

    Les fichiers sont reutilises d'une fois sur l'autre : il n'y a que
    quatre etats possibles, autant ne pas reecrire a chaque rafraichissement.
    """
    if path is None:
        directory = Path(tempfile.gettempdir()) / "shelly-screens"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"tray-v{RENDER_VERSION}-{status}.ico"
        if path.exists():
            return path
    path.write_bytes(build_ico(status))
    return path


def status_for(
    outlet_states: list[bool], online: bool, complete: bool = True
) -> str:
    """Traduit l'etat de l'installation en couleur de pastille."""
    if not online:
        return "offline"
    if not complete:
        return "warning"
    return "on" if any(outlet_states) else "off"


def apply_to_window(window) -> None:
    """Pose l'icone de l'application sur une fenetre Tk et ses filles.

    `iconbitmap(default=...)` vaut pour toutes les fenetres du processus et
    donne la meilleure definition sous Windows, l'icone etant choisie dans
    le fichier .ico selon le contexte. On garde `iconphoto` en secours, pour
    le cas ou le .ico ne serait pas lisible.
    """
    import tkinter as tk

    try:
        window.iconbitmap(default=str(APP_ICON))
        return
    except tk.TclError:
        pass
    try:
        photo = tk.PhotoImage(file=str(LARGE_PNG))
        # La reference doit survivre a l'appel, sinon Tk libere l'image.
        window._app_icon = photo  # type: ignore[attr-defined]
        window.iconphoto(True, photo)
    except tk.TclError:
        pass  # sans icone, la fenetre reste utilisable
