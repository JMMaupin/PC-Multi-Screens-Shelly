"""User interface translation.

The English text serves as the key: it stays readable in the code, and a
missing translation falls back to it instead of showing an identifier. No
catalog files to compile, no dependency -- the application has two
languages, not thirty.

Mixing is the only case to avoid: a window half in French, half in English
forces the reader to translate mentally at every glance. Switching language
therefore rebuilds the whole window.
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

# Primary language identifier Windows assigns to French.
LANG_FRENCH = 0x0C

_current = DEFAULT
_catalog: dict[str, str] = {}


def system_language() -> str:
    """Windows UI language, narrowed down to what we can translate."""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        langid = kernel32.GetUserDefaultUILanguage()
    except (OSError, AttributeError):
        return DEFAULT
    return "fr" if (langid & 0x3FF) == LANG_FRENCH else "en"


def resolve(setting: str) -> str:
    """Effective language for a given setting."""
    if setting == "system":
        return system_language()
    return setting if setting in LANGUAGES else DEFAULT


def set_language(setting: str) -> str:
    """Selects the current language and loads its catalog."""
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
    """Translates a text, and fills in the named values if there are any.

    The values go through `format` rather than an f-string: an f-string
    would be evaluated before translation, and the resulting string would
    then no longer work as a key.
    """
    translated = _catalog.get(text, text)
    if not fields:
        return translated
    try:
        return translated.format(**fields)
    except (KeyError, IndexError, ValueError):
        # Translation whose fields don't match: a correct English sentence
        # beats a broken French one.
        try:
            return text.format(**fields)
        except (KeyError, IndexError, ValueError):
            return text


def missing(texts: list[str]) -> list[str]:
    """Texts with no translation in the current catalog, for checking."""
    if not _catalog:
        return []
    return [text for text in texts if text not in _catalog]
