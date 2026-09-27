"""Build the Windows executable: dist\\ShellyPCScreens-<version>.exe.

Run through build.cmd, which prepares a dedicated virtual environment with
a pinned PyInstaller -- the application itself needs nothing beyond the
standard library.

One file, so that installing or updating is copying one file. The on-device
scripts and the icons travel inside it, at the same relative places as in
the sources: the code finds them without knowing it is frozen.

Signing: set SHELLY_SIGN_CERT to a .pfx file (and SHELLY_SIGN_PASSWORD if
it has one) and signtool.exe must be on the PATH. Without a certificate the
executable is left unsigned, and Windows SmartScreen warns on the first
launch of a downloaded copy.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from shelly_screens import __version__, product  # noqa: E402

BUILD = ROOT / "build"
DIST = ROOT / "dist"
ICONS = ROOT / "windows-icons"
TIMESTAMP_URL = "http://timestamp.digicert.com"


def version_resource() -> Path:
    """The "Details" tab of the file's properties."""
    numbers = ([int(part) for part in __version__.split(".")] + [0, 0, 0, 0])[:4]
    dotted = ".".join(str(n) for n in numbers)
    fields = {
        "CompanyName": product.AUTHOR,
        "FileDescription": product.APP_NAME,
        "FileVersion": __version__,
        "InternalName": "ShellyPCScreens",
        "LegalCopyright": product.AUTHOR,
        "OriginalFilename": "ShellyPCScreens.exe",
        "ProductName": product.APP_NAME,
        "ProductVersion": __version__,
    }
    strings = ",\n".join(f"          StringStruct('{k}', '{v}')" for k, v in fields.items())
    text = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={tuple(numbers)}, prodvers={tuple(numbers)},
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
{strings}
    ])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
# {dotted}
"""
    path = BUILD / "version_info.txt"
    path.write_text(text, encoding="utf-8")
    return path


def data_arguments() -> list[str]:
    pairs = [(ROOT / "shelly_screens" / "scripts", "shelly_screens/scripts")]
    # The icons the application draws from; not the large source artwork.
    pairs += [(icon, "windows-icons") for icon in sorted(ICONS.glob("icon*.png"))]
    pairs.append((ICONS / "icon.ico", "windows-icons"))
    arguments = []
    for source, target in pairs:
        arguments += ["--add-data", f"{source}{os.pathsep}{target}"]
    return arguments


def sign(exe: Path) -> None:
    certificate = os.environ.get("SHELLY_SIGN_CERT")
    if not certificate:
        print("Not signed: SHELLY_SIGN_CERT is not set (SmartScreen will warn).")
        return
    signtool = shutil.which("signtool.exe")
    if signtool is None:
        raise SystemExit("SHELLY_SIGN_CERT is set but signtool.exe is not on the PATH")
    command = [signtool, "sign", "/fd", "SHA256", "/tr", TIMESTAMP_URL, "/td", "SHA256",
               "/f", certificate]
    password = os.environ.get("SHELLY_SIGN_PASSWORD")
    if password:
        command += ["/p", password]
    subprocess.run(command + [str(exe)], check=True)
    print(f"Signed with {certificate}")


def main() -> None:
    import PyInstaller.__main__

    BUILD.mkdir(exist_ok=True)
    name = f"ShellyPCScreens-{__version__}"
    PyInstaller.__main__.run([
        "--noconfirm", "--clean", "--onefile", "--windowed",
        "--name", name,
        "--icon", str(ICONS / "icon.ico"),
        "--version-file", str(version_resource()),
        *data_arguments(),
        "--distpath", str(DIST),
        "--workpath", str(BUILD / "pyinstaller"),
        "--specpath", str(BUILD),
        str(ROOT / "main.py"),
    ])
    exe = DIST / f"{name}.exe"
    sign(exe)
    print(f"\nBuilt {exe} ({exe.stat().st_size / 1_048_576:.1f} MB)")


if __name__ == "__main__":
    main()
