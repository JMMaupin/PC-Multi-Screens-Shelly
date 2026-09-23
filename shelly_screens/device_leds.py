"""Anneaux lumineux et boutons des prises d'une Power Strip.

Chaque prise porte un anneau RGB et un bouton, regles par un seul composant
du firmware, `POWERSTRIP_UI`. Livres a pleine luminosite, les anneaux
eclairent une piece dans le noir ; le bouton, lui, commute la prise au
moindre appui -- y compris celle du PC.

Deux constats faits sur l'appareil guident ce module :

- `POWERSTRIP_UI.SetConfig` accepte une configuration partielle : on
  n'envoie que ce qui change, le reste demeure ;
- les reglages s'appliquent a chaud. Seule la toute premiere activation
  du mode nuit a reclame un redemarrage (`restart_required`) ; on suit donc
  ce que l'appareil annonce plutot que de le supposer. Un redemarrage ne
  fait basculer aucune sortie : les relais sont bistables.

Comme `device_services`, ce module decrit, lit et ecrit, sans rien decider.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .device import ShellyError

COMPONENT = "POWERSTRIP_UI"
MODE_POWER = "power"  # la couleur suit la puissance consommee
MODE_SWITCH = "switch"  # une couleur allumee, une autre eteinte
MODE_OFF = "off"
MODES = (MODE_POWER, MODE_SWITCH, MODE_OFF)
BUTTON_MOMENTARY = "momentary"  # l'appui commute la prise
BUTTON_DETACHED = "detached"  # l'appui ne commande plus rien

# Reglage de nuit propose par defaut : assez pour reperer une prise, pas
# assez pour eclairer la piece.
NIGHT_BRIGHTNESS = 5
NIGHT_START = "22:00"
NIGHT_END = "07:00"

# Seule cle de couleurs acceptee : elle vaut pour toutes les prises.
COLOURS_KEY = "switch:0"

_CLOCK = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


@dataclass(frozen=True)
class LedSettings:
    """Reglage des anneaux, identique pour toutes les prises d'un appareil.

    Le firmware n'a qu'un jeu de couleurs, range sous `switch:0` et valable
    pour toutes les prises : il refuse toute autre cle (erreur -103). Les
    couleurs sont en pourcentages (0-100 par canal), comme il les attend.
    """

    mode: str
    brightness: int  # mode puissance
    on_rgb: tuple[int, int, int]
    on_brightness: int
    off_rgb: tuple[int, int, int]
    off_brightness: int
    night_enabled: bool
    night_brightness: int
    night_start: str
    night_end: str


def valid_clock(value: str) -> bool:
    """Vrai pour une heure « HH:MM » que l'appareil acceptera."""
    return bool(_CLOCK.match(value))


def read(device) -> tuple[LedSettings, dict[int, str]] | None:
    """Reglage des anneaux et mode de chaque bouton, `None` hors Power Strip.

    Un autre modele de Shelly ignore la methode : on le distingue d'une
    panne, pour que l'interface dise « non disponible » plutot qu'une erreur.
    """
    try:
        config: dict[str, Any] = device.call(f"{COMPONENT}.GetConfig") or {}
    except ShellyError:
        return None
    leds = config.get("leds") or {}
    colors = leds.get("colors") or {}
    first = colors.get(COLOURS_KEY) or {}
    night = leds.get("night_mode") or {}
    between = list(night.get("active_between") or [])
    if len(between) != 2:
        between = [NIGHT_START, NIGHT_END]

    def rgb(state: str, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
        values = (first.get(state) or {}).get("rgb") or fallback
        return tuple(int(round(v)) for v in values[:3])  # type: ignore[return-value]

    def level(value: Any) -> int:
        return max(0, min(100, int(round(float(value)))))

    settings = LedSettings(
        mode=leds.get("mode", MODE_POWER),
        brightness=level((colors.get("power") or {}).get("brightness", 100)),
        on_rgb=rgb("on", (0, 100, 0)),
        on_brightness=level((first.get("on") or {}).get("brightness", 100)),
        off_rgb=rgb("off", (100, 0, 0)),
        off_brightness=level((first.get("off") or {}).get("brightness", 100)),
        night_enabled=bool(night.get("enable", False)),
        # Mode nuit jamais regle : l'appareil annonce 100 %, ce qui ne
        # vaut pas proposition. On suggere plutot le reglage par defaut.
        night_brightness=(
            level(night.get("brightness", NIGHT_BRIGHTNESS))
            if night.get("enable") or night.get("active_between")
            else NIGHT_BRIGHTNESS
        ),
        night_start=between[0],
        night_end=between[1],
    )
    buttons = {
        int(key.split(":")[1]): (value or {}).get("in_mode", BUTTON_MOMENTARY)
        for key, value in (config.get("controls") or {}).items()
        if key.startswith("switch:")
    }
    return settings, buttons


def apply(device, settings: LedSettings) -> bool:
    """Envoie le reglage des anneaux ; dit si un redemarrage est necessaire."""
    if settings.mode not in MODES:
        raise ValueError(f"Unknown LED mode '{settings.mode}'")
    for clock in (settings.night_start, settings.night_end):
        if not valid_clock(clock):
            raise ValueError(f"Invalid time '{clock}', expected HH:MM")
    colors = {
        "power": {"brightness": settings.brightness},
        COLOURS_KEY: {
            "on": {"rgb": list(settings.on_rgb), "brightness": settings.on_brightness},
            "off": {"rgb": list(settings.off_rgb), "brightness": settings.off_brightness},
        },
    }
    leds = {
        "mode": settings.mode,
        "colors": colors,
        "night_mode": {
            "enable": settings.night_enabled,
            "brightness": settings.night_brightness,
            "active_between": [settings.night_start, settings.night_end],
        },
    }
    result = device.call(f"{COMPONENT}.SetConfig", {"config": {"leds": leds}}) or {}
    return bool(result.get("restart_required", False))


def set_button(device, switch_id: int, detached: bool) -> None:
    """Detache ou rattache le bouton d'une prise. Effet immediat."""
    mode = BUTTON_DETACHED if detached else BUTTON_MOMENTARY
    device.call(
        f"{COMPONENT}.SetConfig",
        {"config": {"controls": {f"switch:{switch_id}": {"in_mode": mode}}}},
    )

