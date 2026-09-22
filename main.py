"""Point d'entree de Shelly Screens.

Usage courant -- aucune console, c'est le mode normal :
    le raccourci cree par install-startup.ps1, qui vise directement
    pythonw.exe. Ajouter -Desktop pour en poser un sur le Bureau.

Diagnostic -- console ouverte, journaux a l'ecran :
    run-console.cmd          ou    python main.py --verbose

Dans les deux cas, tout est aussi ecrit dans shelly-screens.log, a cote de
ce fichier. Sans console, `print` ne leve pas d'erreur mais n'ecrit nulle
part : le journal est alors le seul temoin.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from shelly_screens.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(verbose="--verbose" in sys.argv or "-v" in sys.argv))
