"""Entry point of Shelly PC Screens.

Everyday use -- no console, this is the normal mode:
    the shortcut created by install-startup.ps1, which targets pythonw.exe
    directly. Add -Desktop to also place one on the Desktop.

Diagnostics -- console open, logs on screen:
    run-console.cmd          or    python main.py --verbose

In both cases, everything is also written to the log, in
%ProgramData%\\Shelly PC Screens\\logs. Without a console, `print` raises no
error but writes nowhere: the log is then the only witness.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from shelly_screens.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
