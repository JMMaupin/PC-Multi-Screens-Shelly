"""Lecture de PNG, sans dependance exterieure.

L'icone de la zone de notification se compose : le visuel de l'application,
surmonte d'une pastille qui dit l'etat des prises. Composer suppose de lire
les pixels, donc de decoder un PNG -- ce que la bibliotheque standard ne
fait pas.

Le decodeur couvre ce dont on a besoin et rien de plus : 8 bits par canal,
non entrelace, en niveaux de gris ou en couleurs, avec ou sans transparence.
Les images fournies avec l'application entrent dans ce cadre ; toute autre
leve une erreur explicite plutot que de produire une image fausse.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Nombre de canaux par type de couleur PNG.
CHANNELS = {0: 1, 2: 3, 4: 2, 6: 4}

Pixel = tuple[int, int, int, int]
Pixels = list[list[Pixel]]


class UnsupportedImage(ValueError):
    """Le fichier sort de ce que ce decodeur sait lire."""


def _paeth(left: int, up: int, corner: int) -> int:
    estimate = left + up - corner
    da, db, dc = abs(estimate - left), abs(estimate - up), abs(estimate - corner)
    if da <= db and da <= dc:
        return left
    return up if db <= dc else corner


def _unfilter(raw: bytes, width: int, height: int, stride: int) -> bytearray:
    """Annule les filtres par ligne du PNG."""
    out = bytearray()
    previous = bytearray(width * stride)
    position = 0
    for _row in range(height):
        method = raw[position]
        position += 1
        line = bytearray(raw[position : position + width * stride])
        position += width * stride
        if method == 1:  # Sub
            for i in range(stride, len(line)):
                line[i] = (line[i] + line[i - stride]) & 0xFF
        elif method == 2:  # Up
            for i in range(len(line)):
                line[i] = (line[i] + previous[i]) & 0xFF
        elif method == 3:  # Average
            for i in range(len(line)):
                left = line[i - stride] if i >= stride else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif method == 4:  # Paeth
            for i in range(len(line)):
                left = line[i - stride] if i >= stride else 0
                corner = previous[i - stride] if i >= stride else 0
                line[i] = (line[i] + _paeth(left, previous[i], corner)) & 0xFF
        elif method != 0:
            raise UnsupportedImage(f"unknown PNG filter {method}")
        out += line
        previous = line
    return out


def load_png(path: Path | str) -> tuple[int, int, Pixels]:
    """Renvoie (largeur, hauteur, pixels RVBA) d'un fichier PNG."""
    data = Path(path).read_bytes()
    if data[:8] != PNG_SIGNATURE:
        raise UnsupportedImage(f"{path}: not a PNG file")

    width = height = depth = color_type = 0
    idat = bytearray()
    position = 8
    while position < len(data):
        (length,) = struct.unpack(">I", data[position : position + 4])
        chunk = data[position + 4 : position + 8]
        payload = data[position + 8 : position + 8 + length]
        position += 12 + length  # longueur + type + donnees + CRC

        if chunk == b"IHDR":
            width, height, depth, color_type, _comp, _filt, interlace = struct.unpack(
                ">IIBBBBB", payload
            )
            if depth != 8:
                raise UnsupportedImage(f"{path}: {depth} bits per channel, expected 8")
            if interlace:
                raise UnsupportedImage(f"{path}: interlaced PNG")
            if color_type not in CHANNELS:
                raise UnsupportedImage(f"{path}: colour type {color_type}")
        elif chunk == b"IDAT":
            idat += payload
        elif chunk == b"IEND":
            break

    if not width or not idat:
        raise UnsupportedImage(f"{path}: no image data")

    stride = CHANNELS[color_type]
    flat = _unfilter(zlib.decompress(bytes(idat)), width, height, stride)

    pixels: Pixels = []
    index = 0
    for _y in range(height):
        row: list[Pixel] = []
        for _x in range(width):
            chunk_pixels = flat[index : index + stride]
            index += stride
            if color_type == 0:
                grey = chunk_pixels[0]
                row.append((grey, grey, grey, 255))
            elif color_type == 2:
                row.append((chunk_pixels[0], chunk_pixels[1], chunk_pixels[2], 255))
            elif color_type == 4:
                grey = chunk_pixels[0]
                row.append((grey, grey, grey, chunk_pixels[1]))
            else:
                row.append(
                    (chunk_pixels[0], chunk_pixels[1], chunk_pixels[2], chunk_pixels[3])
                )
        pixels.append(row)
    return width, height, pixels
