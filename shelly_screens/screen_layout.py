"""Peut-on relever la disposition des ecrans maintenant ?

La disposition n'a de valeur que si chaque ecran y figure a sa place : un
ecran absent, et Windows decale les autres ; un ecran de trop, et le plan
dessine ce qui n'est pas la. Avant tout releve, on verifie donc que ce que
disent les prises et ce que voit Windows concordent exactement :

* chaque prise d'ecran est liee a un ecran, joignable, et allumee ;
* chaque ecran dont la prise est allumee est vu par Windows ;
* autant d'ecrans physiques detectes que de prises d'ecran allumees, plus
  les ecrans prouves hors prise -- ni plus, ni moins ;
* pas deux prises pour un meme ecran, pas d'ecrans en miroir.

Les ecrans virtuels et sans fil sont ecartes avant de compter : ils ne
s'eteignent pas avec une prise et ne sont pas des ecrans du bureau.

Un ecran branche au mur ne se reconnait a rien : seule l'epreuve de
l'assistant d'identification -- couper chaque prise et regarder qui
disparait -- prouve qu'il ne depend d'aucune. Tant qu'elle n'a pas ete
faite, un ecran physique sans prise est inconnu, et le releve attend.

Le module ne touche a rien : il rend la liste des problemes, vide quand le
releve peut avoir lieu.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import TYPE_CHECKING, Mapping

from .config import KIND_SCREEN
from .i18n import t

if TYPE_CHECKING:
    from .config import OutletConfig
    from .device import SwitchState
    from .win.monitors import DisplayOutput


def problems(
    outlets: list["OutletConfig"],
    states: Mapping[str, "SwitchState"],
    physical: set[str],
    outputs: Mapping[str, "DisplayOutput"],
    unswitched: set[str],
    links: Mapping[str, str] | None = None,
) -> list[str]:
    """Ce qui empeche de relever la disposition ; vide si rien ne s'y oppose.

    `physical` : cles des ecrans physiques actifs, virtuels ecartes.
    `outputs` : sorties actives, pour les noms et les miroirs.
    `unswitched` : ecrans prouves hors prise.
    `links` : associations prise -> ecran qui priment sur la configuration,
    pour l'assistant qui vient de les trouver sans les avoir encore ecrites.
    """
    links = links or {}

    def key_of(outlet: "OutletConfig") -> str:
        return links.get(outlet.ref, outlet.monitor_key)

    screen_outlets = [
        o for o in outlets
        if (o.kind == KIND_SCREEN or key_of(o)) and not o.host_pc
    ]
    names = {key_of(o): o.label for o in screen_outlets if key_of(o)}

    def name_of(key: str) -> str:
        output = outputs.get(key)
        return names.get(key) or (output.edid_name if output else "") or key

    found: list[str] = []
    lit = 0
    for outlet in screen_outlets:
        key = key_of(outlet)
        state = states.get(outlet.ref)
        if not key:
            found.append(t("{outlet} is a screen outlet not linked to a screen: "
                           "run Identify displays", outlet=outlet.label))
        elif state is None:
            found.append(t("{outlet}: state unknown, its device does not answer",
                           outlet=outlet.label))
        elif not state.output:
            if key in physical:
                found.append(t("{outlet} is off but Windows keeps its screen on the "
                               "desktop (ghost screen)", outlet=outlet.label))
            else:
                found.append(t("{outlet} is off", outlet=outlet.label))
        else:
            lit += 1
            if key not in physical:
                found.append(t("{outlet} is on but Windows does not see its screen",
                               outlet=outlet.label))

    linked = Counter(key_of(o) for o in screen_outlets if key_of(o))
    for key, count in linked.items():
        if count > 1:
            shared = [o.label for o in screen_outlets if key_of(o) == key]
            found.append(t("{outlets} are linked to the same screen",
                           outlets=", ".join(shared)))

    for key in sorted(physical - set(linked) - unswitched):
        found.append(t("Unknown screen connected ({screen}): run Identify displays",
                       screen=name_of(key)))

    # Le comptage resume tout : prises d'ecran allumees d'un cote, ecrans
    # physiques vus de l'autre. Il dit d'un coup d'oeil ce qui cloche.
    expected = lit + len(physical & unswitched)
    if len(physical) != expected:
        found.insert(0, t("{lit} screen outlet(s) on, {count} physical screen(s) detected",
                          lit=lit, count=len(physical)))

    by_source: dict[tuple, list[str]] = defaultdict(list)
    for output in outputs.values():
        if not output.virtual:
            by_source[output.source].append(output.key)
    for keys in by_source.values():
        if len(keys) > 1:
            found.append(t("{screens} are mirrored",
                           screens=", ".join(name_of(k) for k in sorted(keys))))
    return found


def ghosts(
    outlets: list["OutletConfig"],
    states: Mapping[str, "SwitchState"],
    physical: set[str],
) -> list[str]:
    """Prises d'ecran coupees dont Windows garde pourtant l'ecran : les fantomes.

    Un ecran branche en HDMI recoit du +5 V par le cable, de quoi maintenir
    sa detection et son EDID une fois son alimentation coupee. Windows le
    garde alors dans le bureau, eteint, et fenetres ou souris peuvent s'y
    perdre.
    """
    return [
        o.label for o in outlets
        if o.monitor_key and o.monitor_key in physical
        and o.ref in states and not states[o.ref].output
    ]
