"""Ce que l'application dit d'elle-meme : auteur, liens, appareils valides.

Rassemble en un seul endroit ce que montrent l'onglet About et, plus tard,
l'executable et sa page de publication : une information ecrite deux fois
finit toujours par diverger.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote_plus

AUTHOR = "JMMaupin"

REPOSITORY_URL = "https://github.com/JMMaupin/PC-Multi-Screens-Shelly"
# La page des versions publiees, ou se trouvera l'executable.
RELEASES_URL = f"{REPOSITORY_URL}/releases"


@dataclass(frozen=True)
class ValidatedDevice:
    """Un appareil sur lequel l'application a ete eprouvee.

    Le firmware compte autant que le modele : c'est sur cette version que
    le comportement a ete verifie, et une autre peut le changer.
    """

    name: str
    model: str  # code du modele, tel que l'appareil l'annonce
    firmware: str

    @property
    def search_url(self) -> str:
        """Une recherche du produit : aucune page de fabricant n'est perenne."""
        return f"https://www.google.com/search?q={quote_plus(self.name)}"


VALIDATED_DEVICES = (
    ValidatedDevice("Shelly Power Strip 4 Gen4", "S4PL-00416EU", "2.0.1-beta3"),
)
