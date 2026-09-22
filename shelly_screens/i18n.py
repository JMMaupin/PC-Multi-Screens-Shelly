"""Traduction de l'interface.

Le texte anglais sert de cle : il reste lisible dans le code, et une
traduction manquante retombe dessus au lieu d'afficher un identifiant. Pas
de fichiers de catalogue a compiler, pas de dependance -- l'application en
compte deux langues, pas trente.

Le melange est le seul cas a eviter : une fenetre mi-francaise mi-anglaise
oblige a traduire mentalement a chaque coup d'oeil. Changer de langue
reconstruit donc la fenetre entiere.
"""

from __future__ import annotations

import ctypes

LANGUAGES = ("system", "en", "fr")
LANGUAGE_LABELS = {
    "system": "Follow Windows",
    "en": "English",
    "fr": "Francais",
}
DEFAULT = "en"

# Identifiant de langue principale attribue au francais par Windows.
LANG_FRENCH = 0x0C

_current = DEFAULT
_catalog: dict[str, str] = {}


def system_language() -> str:
    """Langue de l'interface de Windows, ramenee a ce qu'on sait traduire."""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        langid = kernel32.GetUserDefaultUILanguage()
    except (OSError, AttributeError):
        return DEFAULT
    return "fr" if (langid & 0x3FF) == LANG_FRENCH else "en"


def resolve(setting: str) -> str:
    """Langue effective pour un reglage donne."""
    if setting == "system":
        return system_language()
    return setting if setting in LANGUAGES else DEFAULT


def set_language(setting: str) -> str:
    """Choisit la langue courante et charge son catalogue."""
    global _current, _catalog
    _current = resolve(setting)
    if _current == "fr":
        from .locale_fr import CATALOG

        _catalog = CATALOG
    else:
        _catalog = {}
    return _current


def current() -> str:
    return _current


def t(text: str, **fields: object) -> str:
    """Traduit un texte, et y insere les valeurs nommees s'il y en a.

    Les valeurs passent par `format` plutot que par une f-string : une
    f-string serait evaluee avant la traduction, et la chaine traduite ne
    servirait alors plus de cle.
    """
    translated = _catalog.get(text, text)
    if not fields:
        return translated
    try:
        return translated.format(**fields)
    except (KeyError, IndexError, ValueError):
        # Traduction dont les champs ne correspondent pas : mieux vaut la
        # phrase anglaise juste qu'une francaise cassee.
        try:
            return text.format(**fields)
        except (KeyError, IndexError, ValueError):
            return text


def missing(texts: list[str]) -> list[str]:
    """Textes sans traduction dans le catalogue courant, pour verification."""
    if not _catalog:
        return []
    return [text for text in texts if text not in _catalog]
