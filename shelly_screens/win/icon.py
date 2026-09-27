"""Notification area icon.

The icon is the application's own, topped with a badge showing the state
of the outlets: green when screens are powered, grey when everything is
off, orange when a device is missing, red when nothing responds any more.

The detailed figures -- how many outlets, how many watts -- live in the
tooltip and the menu. At the actual display size, sixteen pixels square,
a badge reads at a glance where a count would not be readable at all.

The generated .ico file holds three sizes, each drawn from the artwork at
the matching resolution: letting Windows shrink a single image would give
a blurry result.
"""

from __future__ import annotations

import struct
import tempfile
from pathlib import Path

from .images import Pixels, load_png

# The icon set lives at the project root, as the `icongen_windows.py`
# generator produces it: its folder is copied without being
# reorganised, so that regenerating boils down to a replacement.
ASSETS = Path(__file__).resolve().parent.parent.parent / "windows-icons"
# Sizes composed for the notification area. The intermediate ones
# cover screens at 125 %, 150 % and 250 %, where Windows asks for 20, 24
# and 40 pixels rather than 16 or 32.
SIZES = (16, 20, 24, 32, 40, 48)
SOURCES = {size: ASSETS / f"icon-{size}.png" for size in SIZES}
APP_ICON = ASSETS / "icon.ico"
LARGE_PNG = ASSETS / "icon-256.png"

# Possible states and the colour of their badge.
STATUS_COLORS = {
    "on": (60, 200, 90, 255),  # at least one outlet powered
    "off": (150, 150, 156, 255),  # everything off, but everything responds
    "warning": (230, 155, 60, 255),  # a device is missing or rejects the password
    "offline": (220, 90, 70, 255),  # nothing responds any more
}
BADGE_RING = (18, 20, 24, 255)  # dark ring, to stand out from the background

# Rendering generation number. It is part of the cached file name:
# without it, an icon produced by an earlier version would be reused
# as is, since the file already has the right name.
RENDER_VERSION = 4

_cache: dict[tuple[int, str], Pixels] = {}


def _base(size: int) -> Pixels:
    """Application artwork at the requested size."""
    key = (size, "base")
    if key not in _cache:
        _, _, pixels = load_png(SOURCES[size])
        _cache[key] = pixels
    return _cache[key]


def _blend(under: tuple[int, int, int, int], over: tuple[int, int, int, int]):
    """Composite `over` onto `under`, taking transparency into account."""
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
    """Application artwork, status badge included."""
    color = STATUS_COLORS.get(status, STATUS_COLORS["off"])
    pixels = [list(row) for row in _base(size)]

    # Badge in the lower-right quarter, proportional to the size.
    radius = max(2.5, size * 0.21)
    centre = size - radius - max(1.0, size * 0.04)
    ring = max(1.0, size * 0.05)

    for y in range(size):
        for x in range(size):
            distance = ((x + 0.5 - centre) ** 2 + (y + 0.5 - centre) ** 2) ** 0.5
            if distance <= radius - ring:
                pixels[y][x] = _blend(pixels[y][x], color)
            elif distance <= radius:
                # Softened edge: anti-aliasing avoids the staircase at 16 pixels.
                edge = min(1.0, radius - distance + 1.0)
                pixels[y][x] = _blend(
                    pixels[y][x], (*BADGE_RING[:3], int(255 * max(0.0, edge)))
                )
    return pixels


def _image_entry(pixels: Pixels, size: int) -> bytes:
    """One image of an ICO file: DIB header, pixels, mask."""
    body = bytearray()
    # DIB rows are stored bottom-up, in BGRA.
    for y in reversed(range(size)):
        for x in range(size):
            r, g, b, a = pixels[y][x]
            body += bytes((b, g, r, a))
    # AND mask: useless at 32 bits, but the format expects it.
    mask_row = ((size + 31) // 32) * 4
    body += bytes(mask_row * size)

    header = struct.pack(
        "<IiiHHIIiiII",
        40,  # biSize
        size,  # biWidth
        size * 2,  # biHeight: image + mask
        1,  # biPlanes
        32,  # biBitCount
        0,  # biCompression = BI_RGB
        len(body),
        0, 0, 0, 0,
    )
    return header + bytes(body)


def build_ico(status: str) -> bytes:
    """Build a multi-size ICO file for a given state."""
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
    """Write the icon for a state to disk and return its path.

    Files are reused from one run to the next: there are only four
    possible states, no point rewriting them on every refresh.
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
    """Translate the installation's state into a badge colour."""
    if not online:
        return "offline"
    if not complete:
        return "warning"
    return "on" if any(outlet_states) else "off"


def load_photo(size: int = 96, master=None):
    """Tk image of the logo at the requested size, or None if it is missing.

    Return None rather than raise: a missing logo is a cosmetic flaw,
    it must never prevent a window from opening.

    `master` is the window that will display it: each window of the
    application has its own Tk interpreter, and an image created in
    another one cannot be found there.
    """
    import tkinter as tk

    # The icon set holds more sizes than SIZES declares: the latter
    # only lists those in the .ico. So we look for the file, and
    # fall back on the 256 one reduced by an integer factor -- Tk cannot
    # interpolate, but dividing by two or four stays sharp.
    source = ASSETS / f"icon-{size}.png"
    try:
        if source.exists():
            return tk.PhotoImage(master=master, file=str(source))
        if not LARGE_PNG.exists():
            return None
        image = tk.PhotoImage(master=master, file=str(LARGE_PNG))
        factor = max(1, round(256 / max(1, size)))
        return image.subsample(factor, factor) if factor > 1 else image
    except Exception:  # noqa: BLE001 - Tk without PNG support, unreadable file
        return None


def apply_to_window(window) -> None:
    """Set the application icon on a Tk window and its children.

    `iconbitmap(default=...)` applies to every window of the process and
    gives the best resolution on Windows, since the icon is picked from
    the .ico file according to context. `iconphoto` is kept as a fallback,
    in case the .ico is not readable.
    """
    import tkinter as tk

    try:
        window.iconbitmap(default=str(APP_ICON))
        return
    except tk.TclError:
        pass
    try:
        photo = tk.PhotoImage(master=window, file=str(LARGE_PNG))
        # The reference must outlive the call, otherwise Tk frees the image.
        window._app_icon = photo  # type: ignore[attr-defined]
        window.iconphoto(True, photo)
    except tk.TclError:
        pass  # without an icon, the window remains usable
