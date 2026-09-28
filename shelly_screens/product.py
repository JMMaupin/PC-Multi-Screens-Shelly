"""What the app says about itself: author, links, validated devices.

Gathers in one place what the About tab shows and, later, the executable
and its release page: information written twice always ends up diverging.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote_plus

APP_NAME = "Shelly PC Screens"
AUTHOR = "JMMaupin"

REPOSITORY_URL = "https://github.com/JMMaupin/PC-Multi-Screens-Shelly"
# The illustrated website: overview, features, step-by-step setup guide.
WEBSITE_URL = "https://jmmaupin.github.io/PC-Multi-Screens-Shelly/"
# The published releases page, where the executable is downloaded.
RELEASES_URL = f"{REPOSITORY_URL}/releases"


@dataclass(frozen=True)
class ValidatedDevice:
    """A device the app has been tested on.

    The firmware matters as much as the model: this is the version on which
    the behaviour was verified, and another one may change it.
    """

    name: str
    model: str  # model code, as the device reports it
    firmware: str

    @property
    def search_url(self) -> str:
        """A search for the product: no manufacturer page lasts forever."""
        return f"https://www.google.com/search?q={quote_plus(self.name)}"


VALIDATED_DEVICES = (
    ValidatedDevice("Shelly Power Strip 4 Gen4", "S4PL-00416EU", "2.0.1-beta3"),
)
