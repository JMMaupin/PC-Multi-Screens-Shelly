"""Detection de l'activite du PC par sa consommation.

Quand le PC est eteint, aucun logiciel ne tourne sur lui pour commander les
prises. C'est donc la multiprise qui doit s'en charger : un script embarque
surveille la consommation de l'unite centrale et rallume les ecrans des
qu'elle repart. C'est ce qui autorise a tout couper a l'arret, ecran de
demarrage compris, sans se retrouver aveugle au prochain allumage.

Ce module genere ce script, l'installe et le tient a jour. Il s'occupe aussi
du relais entre l'application et lui : la liste des prises a rallumer voyage
par le KVS de l'appareil, dont chaque valeur est limitee a 255 caracteres --
d'ou une simple liste d'index dans la table que le script embarque.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from .config import AppConfig, OutletConfig, parse_ref
from .device import AUTH_USERNAME, ShellyError

if TYPE_CHECKING:
    from .controller import ScreenController

SCRIPT_NAME = "pc_sensing"
KVS_PROFILE_KEY = "scr_profile"
KVS_MAX_VALUE = 255
# Code renvoye par le firmware quand la cle demandee n'existe pas.
KVS_KEY_NOT_FOUND = -105
# Pause entre deux commandes envoyees par le script, en millisecondes. Une
# rafale trop serree se perd : les premieres commandes passent, les
# suivantes sont abandonnees sans un mot. Les ordres a distance partent en
# HTTP, plus lents qu'une commande locale.
#
# Elargie de 600 a 1200 ms apres deux plantages de la multiprise qui porte
# le script, tous deux pendant une restitution. Chaque ordre distant coute
# deux allers-retours -- l'authentification Digest impose d'abord un refus
# 401 porteur du defi -- et jusqu'a trois tentatives : de quoi saturer le
# firmware quand l'application l'interroge en meme temps. Les ecrans
# reviennent en cinq secondes au lieu de deux et demie, toujours avant le
# bureau Windows.
COMMAND_GAP_MS = 1200
# Nombre d'essais par commande distante avant d'abandonner.
COMMAND_TRIES = 3
# Taille maximale d'un envoi de code. Le firmware refuse les requetes trop
# grosses (HTTP 413) : le code part donc par tranches, la premiere
# remplacant le contenu et les suivantes s'y ajoutant.
CODE_CHUNK = 1024
TEMPLATE_PATH = Path(__file__).resolve().parent / "scripts" / "pc_sensing.js"
CONFIG_MARKER = "// --- CONFIG ---"


class SensingError(RuntimeError):
    """La detection ne peut pas etre configuree en l'etat."""


@dataclass
class ScriptStatus:
    """Ce que l'appareil dit du script installe."""

    installed: bool = False
    running: bool = False
    script_id: int = 0
    memory_used: int = 0
    error: str = ""

    def summary(self) -> str:
        if self.error:
            return self.error
        if not self.installed:
            return "not installed"
        return "running" if self.running else "installed but stopped"


def controlled_outlets(config: AppConfig) -> list[OutletConfig]:
    """Prises que le script a le droit de manoeuvrer.

    La prise de l'unite centrale en est evidemment exclue -- elle est ce
    qu'on observe. Les prises critiques aussi : un concentrateur USB portant
    le clavier doit rester alimente en permanence, faute de quoi il ne serait
    pas enumere a temps pour entrer dans le BIOS.

    Restent enfin dehors les prises dont la case "suit la veille" est
    decochee : un accessoire qu'on veut garder sous tension ne doit meme
    pas figurer dans la table embarquee, sans quoi le script le couperait
    malgre l'intention exprimee.
    """
    return [o for o in config.outlets if o.cuts_on_sleep]


def outlet_index(config: AppConfig, ref: str) -> int:
    """Rang d'une prise dans la table embarquee, ou -1."""
    for index, outlet in enumerate(controlled_outlets(config)):
        if outlet.ref == ref:
            return index
    return -1


def host_device_key(config: AppConfig) -> str:
    """Appareil qui porte la prise du PC, et donc qui hebergera le script."""
    if not config.sensing.pc_ref:
        raise SensingError("No outlet is marked as powering the PC")
    return parse_ref(config.sensing.pc_ref)[0]


def build_script_config(config: AppConfig) -> dict[str, Any]:
    """Assemble la configuration injectee dans le script."""
    sensing = config.sensing
    if not sensing.pc_ref:
        raise SensingError("No outlet is marked as powering the PC")
    host_key = host_device_key(config)
    pc_switch = parse_ref(sensing.pc_ref)[1]

    outlets: list[dict[str, Any]] = []
    for outlet in controlled_outlets(config):
        if outlet.device == host_key:
            # Sortie de l'appareil qui execute le script : appel direct.
            outlets.append({"h": None, "i": outlet.switch_id})
        else:
            device = config.device(outlet.device)
            if device is None or not device.host:
                raise SensingError(
                    f"Device '{outlet.device}' has no known address; reconnect it first"
                )
            # Les identifiants voyagent dans l'URL, seule forme que le
            # client HTTP embarque accepte : ni l'en-tete Basic ni un champ
            # `auth` ne sont honores -- tous deux repondent 401, la ou
            # `http://admin:mdp@hote/` repond 200. Sans cela, une multiprise
            # protegee par mot de passe refuse toutes les commandes du
            # script, et ses prises restent figees.
            userinfo = ""
            password = device.get_password()
            if password:
                userinfo = AUTH_USERNAME + ":" + quote(password, safe="")
            outlets.append(
                {"h": device.host, "i": outlet.switch_id, "u": userinfo}
            )

    boot = config.boot_screen_outlet()
    boot_index = outlet_index(config, boot.ref) if boot is not None else -1

    # Les delais sont convertis en nombre de mesures ici plutot que dans le
    # script : mJS n'offre qu'un sous-ensemble de JavaScript, et il n'y a
    # aucune raison de lui confier un arrondi.
    poll = max(0.5, float(sensing.poll_interval_s))
    on_ticks = max(1, math.ceil(float(sensing.on_delay_s) / poll))
    off_ticks = max(1, math.ceil(float(sensing.off_delay_s) / poll))

    return {
        "pc": pc_switch,
        "onW": round(float(sensing.on_threshold_w), 1),
        "offW": round(float(sensing.off_threshold_w), 1),
        "onTicks": on_ticks,
        "offTicks": off_ticks,
        "poll": round(poll, 1),
        "gap": COMMAND_GAP_MS,
        "tries": COMMAND_TRIES,
        "boot": boot_index,
        "key": KVS_PROFILE_KEY,
        "outlets": outlets,
    }


def render(config: AppConfig) -> str:
    """Produit le code du script, configuration incluse."""
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    if CONFIG_MARKER not in template:
        raise SensingError("Script template is missing its configuration marker")
    payload = json.dumps(build_script_config(config), separators=(",", ":"))
    return template.replace(CONFIG_MARKER, f"let CFG = {payload};", 1)


def profile_indexes(config: AppConfig, profile_name: str) -> list[int]:
    """Index, dans la table embarquee, des prises alimentees par un profil."""
    profile = config.profile(profile_name)
    if profile is None:
        return []
    indexes = []
    for ref in profile.outlets_on:
        index = outlet_index(config, ref)
        if index >= 0:
            indexes.append(index)
    return sorted(set(indexes))


def encode_profile(indexes: list[int]) -> str:
    """Encode la liste pour le KVS, en respectant sa limite de taille."""
    payload = json.dumps(indexes, separators=(",", ":"))
    while len(payload) > KVS_MAX_VALUE and indexes:
        # Cas theorique avec nos huit prises, mais mieux vaut tronquer que
        # se faire refuser l'ecriture et laisser une valeur perimee.
        indexes = indexes[:-1]
        payload = json.dumps(indexes, separators=(",", ":"))
    return payload


def _put_code(device, script_id: int, code: str) -> None:
    """Televerse le code par tranches.

    Un envoi unique depasse ce que le firmware accepte (HTTP 413) des que
    le script atteint quelques kilo-octets. La premiere tranche remplace
    le contenu, les suivantes s'y ajoutent.
    """
    first = True
    for start in range(0, len(code), CODE_CHUNK):
        device.call(
            "Script.PutCode",
            {
                "id": script_id,
                "code": code[start : start + CODE_CHUNK],
                "append": not first,
            },
        )
        first = False


def _forget_key(device, key: str) -> None:
    """Efface une cle du KVS, qu'elle existe ou non.

    Le firmware refuse la suppression d'une cle absente (erreur -105).
    Or c'est le cas normal au premier releve : l'absence est justement ce
    que l'on veut obtenir, pas une anomalie a signaler.
    """
    try:
        device.call("KVS.Delete", {"key": key})
    except ShellyError as exc:
        if exc.code != KVS_KEY_NOT_FOUND:
            raise


def _find_script(device, name: str = SCRIPT_NAME) -> int:
    """Identifiant du script portant ce nom sur l'appareil, ou 0."""
    result = device.call("Script.List") or {}
    for entry in result.get("scripts", []):
        if entry.get("name") == name:
            return int(entry.get("id", 0))
    return 0


def install(controller: "ScreenController", config: AppConfig) -> ScriptStatus:
    """Installe ou met a jour le script sur l'appareil qui porte le PC."""
    code = render(config)  # echoue tot si la configuration est incomplete
    host_key = host_device_key(config)
    device = controller.device_for(host_key)

    script_id = _find_script(device)
    if not script_id:
        created = device.call("Script.Create", {"name": SCRIPT_NAME}) or {}
        script_id = int(created.get("id", 0))
        if not script_id:
            raise SensingError("The device refused to create the script")
    else:
        # Un script en cours d'execution refuse d'etre reecrit.
        device.call("Script.Stop", {"id": script_id})

    _put_code(device, script_id, code)
    # `enable` fait repartir le script apres une coupure de courant, ce qui
    # est justement le cas qu'il doit couvrir.
    device.call("Script.SetConfig", {"id": script_id, "config": {"enable": True}})
    device.call("Script.Start", {"id": script_id})

    config.sensing.script_id = script_id
    config.sensing.enabled = True
    # Ce qui vient d'etre pose fait foi jusqu'au prochain changement.
    config.sensing.installed_fingerprint = fingerprint(config)
    publish_profile(controller, config, config.settings.last_profile)
    return status(controller, config)


def _get_code(device, script_id: int) -> str:
    """Relit le code installe, par tranches comme il a ete envoye."""
    parts: list[str] = []
    offset = 0
    while True:
        chunk = device.call(
            "Script.GetCode", {"id": script_id, "offset": offset, "len": CODE_CHUNK}
        ) or {}
        data = str(chunk.get("data", ""))
        parts.append(data)
        offset += len(data)
        if not data or int(chunk.get("left", 0)) <= 0:
            break
    return "".join(parts)


def sync_installed(controller: "ScreenController", config: AppConfig) -> str:
    """Remet le script embarque en phase avec la configuration.

    Le script porte une copie figee de la table des prises. Renommer un
    appareil, changer le type d'une prise ou corriger le script lui-meme
    laisse cette copie perimee, et l'ecart ne se voit nulle part : les
    index du profil publie ne designent alors plus les memes sorties. Le
    comparer a chaque demarrage coute une lecture et evite une nuit
    entiere de comportement inexplicable.
    """
    if not config.sensing.enabled or not config.sensing.pc_ref:
        return ""
    wanted = render(config)
    device = controller.device_for(host_device_key(config))
    script_id = _find_script(device)
    if not script_id:
        install(controller, config)
        return "installed"
    if _get_code(device, script_id) != wanted:
        install(controller, config)
        return "updated"
    info = device.call("Script.GetStatus", {"id": script_id}) or {}
    if not info.get("running"):
        # Le code est bon : inutile de tout reecrire, il suffit de le
        # relancer. Un script arrete ne protege plus rien.
        device.call("Script.Start", {"id": script_id})
        return "restarted"
    return ""


def installed_matches(controller: "ScreenController", config: AppConfig) -> bool:
    """Le code pose sur l'appareil correspond-il a la configuration ?

    Changer un seuil ou un delai dans l'interface n'ecrit que le fichier :
    l'appareil garde les anciennes valeurs jusqu'a une reinstallation. Rien
    ne le disait, et l'on croyait regler une detection qui continuait de
    suivre des consignes perimees.
    """
    if not config.sensing.enabled or not config.sensing.pc_ref:
        return True
    try:
        device = controller.device_for(host_device_key(config))
        script_id = _find_script(device)
        if not script_id:
            return False
        return _get_code(device, script_id) == render(config)
    except Exception:  # noqa: BLE001 - un appareil injoignable se dit ailleurs
        return True


def fingerprint(config: AppConfig) -> str:
    """Empreinte du code que la configuration actuelle produirait."""
    return hashlib.sha256(render(config).encode("utf-8")).hexdigest()


def needs_update(config: AppConfig) -> bool:
    """Le script pose sur l'appareil est-il devenu obsolete ?

    Question posee sans toucher au reseau : on compare l'empreinte
    retenue lors de la derniere installation a celle du code qu'on
    ecrirait maintenant. Changer un seuil, un type de prise ou un mot de
    passe modifie ce code -- et l'appareil, lui, continuerait d'appliquer
    l'ancien sans rien en dire.
    """
    if not config.sensing.enabled or not config.sensing.pc_ref:
        return False
    if not config.sensing.installed_fingerprint:
        # Rien de retenu : installation anterieure a ce suivi, ou script
        # jamais pose. On ne crie pas au loup, la synchronisation au
        # demarrage tranchera.
        return False
    try:
        return fingerprint(config) != config.sensing.installed_fingerprint
    except SensingError:
        return False


def uninstall(controller: "ScreenController", config: AppConfig) -> None:
    """Arrete et supprime le script."""
    try:
        host_key = host_device_key(config)
        device = controller.device_for(host_key)
    except (SensingError, Exception):  # noqa: BLE001 - desinstaller ne doit pas echouer
        config.sensing.enabled = False
        config.sensing.script_id = 0
        return
    script_id = _find_script(device) or config.sensing.script_id
    if script_id:
        try:
            device.call("Script.Stop", {"id": script_id})
            device.call("Script.Delete", {"id": script_id})
        except Exception:  # noqa: BLE001
            pass
    config.sensing.enabled = False
    config.sensing.script_id = 0


def status(controller: "ScreenController", config: AppConfig) -> ScriptStatus:
    """Interroge l'appareil sur l'etat du script."""
    try:
        device = controller.device_for(host_device_key(config))
    except Exception as exc:  # noqa: BLE001
        return ScriptStatus(error=str(exc))
    try:
        script_id = _find_script(device)
        if not script_id:
            return ScriptStatus(installed=False)
        info = device.call("Script.GetStatus", {"id": script_id}) or {}
        return ScriptStatus(
            installed=True,
            running=bool(info.get("running")),
            script_id=script_id,
            memory_used=int(info.get("mem_used", 0)),
        )
    except Exception as exc:  # noqa: BLE001
        return ScriptStatus(error=str(exc))


def publish_profile(
    controller: "ScreenController", config: AppConfig, profile_name: str
) -> bool:
    """Depose dans le KVS les prises que le script rallumera au demarrage.

    Un profil vide n'est jamais publie. Appliquer « All off » avant
    d'eteindre le PC est un geste naturel, mais il ne veut pas dire « au
    prochain demarrage, un seul ecran » : on conserve alors la derniere
    disposition utile.
    """
    if not config.sensing.enabled or not config.sensing.pc_ref:
        return False
    indexes = profile_indexes(config, profile_name)
    if not indexes:
        return False
    try:
        device = controller.device_for(host_device_key(config))
        device.call("KVS.Set", {"key": KVS_PROFILE_KEY, "value": encode_profile(indexes)})
        return True
    except Exception:  # noqa: BLE001 - un KVS muet ne doit pas bloquer un profil
        return False


def read_published_profile(controller: "ScreenController", config: AppConfig) -> list[int]:
    """Relit ce que le script trouvera dans le KVS.

    Les index hors de la table courante sont ecartes : ils viennent d'une
    configuration qui a change depuis la publication, et les laisser
    passer ferait croire a un profil exploitable alors qu'il ne designe
    plus rien.
    """
    try:
        device = controller.device_for(host_device_key(config))
        result = device.call("KVS.Get", {"key": KVS_PROFILE_KEY}) or {}
        stored = json.loads(result.get("value", "[]"))
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(stored, list):
        return []
    count = len(controlled_outlets(config))
    return [i for i in stored if isinstance(i, int) and 0 <= i < count]


# --------------------------------------------------------------- calibration

PROBE_NAME = "pc_probe"
KVS_PROBE_KEY = "scr_probe"
PROBE_TEMPLATE = Path(__file__).resolve().parent / "scripts" / "pc_probe.js"
PROBE_POLL_S = 5.0
PROBE_WRITE_EVERY = 12  # une ecriture par minute au plus, hors nouveau palier
# Courbe : des ticks horodates, enregistres seulement quand la puissance
# bouge. Un PC au repos, ou en veille toute une nuit, ne produit alors
# qu'un point -- la ou un echantillonnage regulier aurait sature la
# memoire de mesures identiques. Une valeur du KVS tient 255 caracteres,
# soit 84 ticks de trois caracteres.
KVS_SERIES_KEY = "scr_series"
TICK_CHARS = 3
PROBE_SERIES_CHARS = 252  # multiple de TICK_CHARS
# Ecart de niveau a partir duquel un changement merite un tick. Trois
# niveaux sur l'echelle logarithmique valent environ 35 % de variation :
# assez pour ignorer les fluctuations d'un PC en marche, assez peu pour
# saisir un passage en veille.
PROBE_TICK_MIN_STEP = 3
# Nombre maximal de mesures sans le moindre tick. Passe ce delai, on en
# pose un quand meme : sans point d'ancrage, une longue periode calme
# deviendrait un simple trait sans echelle de temps.
PROBE_TICK_MAX_SILENCE = 180  # 180 x 5 s = 15 min
# Plafond de l'echelle logarithmique de la courbe, en watts.
PROBE_SERIES_MAX_W = 400.0
SERIES_ALPHABET = (
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz-_"
)


@dataclass(frozen=True)
class Tick:
    """Un changement de puissance, et depuis combien de temps il dure."""

    age_s: float  # secondes ecoulees depuis ce tick jusqu'a maintenant
    watts: float
# Bornes de l'histogramme, identiques a celles du script.
PROBE_EDGES = [2, 5, 10, 20, 40, 80, 160]


@dataclass
class Levels:
    """Paliers de consommation releves."""

    samples: int = 0
    lowest: float = 0.0
    highest: float = 0.0
    buckets: list[int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.buckets is None:
            self.buckets = [0] * (len(PROBE_EDGES) + 1)

    @property
    def duration_s(self) -> float:
        return self.samples * PROBE_POLL_S

    def bucket_bounds(self, index: int) -> tuple[float, float]:
        low = PROBE_EDGES[index - 1] if index > 0 else 0.0
        high = PROBE_EDGES[index] if index < len(PROBE_EDGES) else float("inf")
        return low, high

    def populated(self, min_share: float = 0.02) -> list[int]:
        """Tranches representant au moins `min_share` des mesures.

        Le seuil elimine les valeurs de passage -- la montee en charge au
        demarrage, une pointe ponctuelle -- qui ne sont pas des paliers.
        """
        if not self.samples:
            return []
        floor = max(1, int(self.samples * min_share))
        return [i for i, count in enumerate(self.buckets) if count >= floor]

    def split_levels(self) -> tuple[list[int], list[int]]:
        """Separe les tranches peuplees en groupe bas et groupe haut.

        La coupure se fait sur la plus grande discontinuite. Un PC ne
        produit pas deux paliers mais quatre -- eteint, en veille, au repos,
        en charge -- et c'est le vide entre « eteint ou en veille » et
        « allume » qui nous interesse. Prendre simplement la tranche la plus
        haute reviendrait a caler les seuils sur les pointes de charge, et
        les placerait bien trop haut.
        """
        slots = self.populated()
        if len(slots) < 2:
            return slots, []
        widest = 0
        cut = 0
        for index in range(len(slots) - 1):
            gap = slots[index + 1] - slots[index]
            if gap > widest:
                widest = gap
                cut = index
        if widest < 1:
            return slots, []
        return slots[: cut + 1], slots[cut + 1 :]

    def standby_ceiling(self) -> float:
        """Majorant de ce que consomme le PC eteint ou en veille.

        Borne haute de la derniere tranche du groupe bas, et non le minimum
        observe : un seuil place juste au-dessus d'un creux ponctuel se
        ferait franchir par la moindre variation.
        """
        low_group, _ = self.split_levels()
        if not low_group:
            return 0.0
        _, high = self.bucket_bounds(low_group[-1])
        return self.highest if high == float("inf") else high

    def active_floor(self) -> float:
        """Minorant de ce que consomme le PC en marche.

        Borne basse de la premiere tranche du groupe haut : le PC au repos
        peut descendre jusque-la, et le seuil doit rester en dessous.
        """
        _, high_group = self.split_levels()
        if not high_group:
            return 0.0
        low, _ = self.bucket_bounds(high_group[0])
        return low

    def has_two_levels(self) -> bool:
        """Vrai si l'on distingue bien un palier bas et un palier haut."""
        low_group, high_group = self.split_levels()
        return bool(low_group) and bool(high_group)


def suggest_thresholds(levels: Levels) -> tuple[float, float, str]:
    """Propose les deux seuils a partir des paliers releves.

    Renvoie (allumage, coupure, avertissement). Les seuils se placent dans
    l'intervalle separant les deux paliers, plus pres du bas que du haut :
    un PC au repos profond descend parfois bien en dessous de sa
    consommation habituelle, alors qu'un PC eteint ne remonte pas.
    """
    if not levels.has_two_levels():
        return (
            0.0,
            0.0,
            "Only one power level was seen. Let the PC run, sleep and shut "
            "down at least once before reading the measurement.",
        )
    floor = levels.standby_ceiling()
    ceiling = levels.active_floor()
    span = ceiling - floor
    if span <= 2.0:
        return (
            0.0,
            0.0,
            f"The gap between idle ({floor:.1f} W) and running ({ceiling:.1f} W) "
            "is too small to place a reliable threshold.",
        )
    on_threshold = round(floor + 0.45 * span, 1)
    off_threshold = round(floor + 0.25 * span, 1)
    warning = ""
    if span < 10.0:
        warning = (
            f"The gap is narrow ({span:.1f} W). Watch that the screens do not "
            "switch off while the PC is running."
        )
    return on_threshold, off_threshold, warning


def render_probe(config: AppConfig) -> str:
    """Code du releveur, configuration incluse."""
    if not config.sensing.pc_ref:
        raise SensingError("No outlet is marked as powering the PC")
    template = PROBE_TEMPLATE.read_text(encoding="utf-8")
    payload = json.dumps(
        {
            "pc": parse_ref(config.sensing.pc_ref)[1],
            "poll": PROBE_POLL_S,
            "writeEvery": PROBE_WRITE_EVERY,
            "key": KVS_PROBE_KEY,
            "seriesKey": KVS_SERIES_KEY,
            "maxChars": PROBE_SERIES_CHARS,
            "minStep": PROBE_TICK_MIN_STEP,
            "maxSilence": PROBE_TICK_MAX_SILENCE,
            "maxW": PROBE_SERIES_MAX_W,
        },
        separators=(",", ":"),
    )
    return template.replace(CONFIG_MARKER, f"let CFG = {payload};", 1)


def install_probe(
    controller: "ScreenController", config: AppConfig, fresh: bool
) -> int:
    """Pose le releveur ; `fresh` efface ce qu'il avait deja enregistre.

    Deux usages opposes. Lancer une mesure demande une ardoise vierge : un
    releve precedent fausserait les paliers proposes. Mettre le releveur a
    jour, au contraire, ne doit rien perdre : ses ticks alimentent
    l'historique de consommation, et le releveur les relit a son demarrage.
    """
    code = render_probe(config)
    device = controller.device_for(host_device_key(config))
    script_id = _find_script(device, PROBE_NAME)
    if not script_id:
        created = device.call("Script.Create", {"name": PROBE_NAME}) or {}
        script_id = int(created.get("id", 0))
    else:
        device.call("Script.Stop", {"id": script_id})
    _put_code(device, script_id, code)
    device.call("Script.SetConfig", {"id": script_id, "config": {"enable": True}})
    if fresh:
        _forget_key(device, KVS_PROBE_KEY)
        _forget_key(device, KVS_SERIES_KEY)
    device.call("Script.Start", {"id": script_id})
    return script_id


def start_probe(controller: "ScreenController", config: AppConfig) -> int:
    """Lance une nouvelle mesure, sur une ardoise vierge."""
    return install_probe(controller, config, fresh=True)


def sync_probe(controller: "ScreenController", config: AppConfig) -> str:
    """Tient le releveur a jour et en marche, sans effacer ses ticks.

    Il n'est plus un simple outil de calibration : c'est lui qui voit la
    consommation pendant que le PC dort, et l'historique en depend. On le
    pose donc s'il manque, on le remplace si son code a vieilli, et on le
    relance s'il s'est arrete.
    """
    if not config.sensing.enabled or not config.sensing.pc_ref:
        return ""
    device = controller.device_for(host_device_key(config))
    script_id = _find_script(device, PROBE_NAME)
    if not script_id:
        install_probe(controller, config, fresh=False)
        return "installed"
    if _get_code(device, script_id) != render_probe(config):
        install_probe(controller, config, fresh=False)
        return "updated"
    info = device.call("Script.GetStatus", {"id": script_id}) or {}
    if not info.get("running"):
        device.call("Script.Start", {"id": script_id})
        return "restarted"
    return ""


def read_probe_timeline(
    controller: "ScreenController", config: AppConfig
) -> list[tuple[float, float]]:
    """Ticks du releveur, dates en temps Unix, du plus ancien au plus recent.

    Le releveur publie l'instant de son dernier tick ; les autres s'en
    deduisent par les ecarts qu'il encode. Faute de ce repere -- releveur
    anterieur, ou horloge pas encore synchronisee --, on prend l'heure
    courante : l'approximation est bonne a la sortie de veille, ou le
    dernier tick est justement celui du reveil.
    """
    ticks = read_series(controller, config)
    if not ticks:
        return []
    reference = None
    try:
        device = controller.device_for(host_device_key(config))
        raw = (device.call("KVS.Get", {"key": KVS_PROBE_KEY}) or {}).get("value")
        data = json.loads(raw) if isinstance(raw, str) and raw.startswith("{") else {}
        stamp = data.get("t")
        # Un instant anterieur a 2001 est une horloge non synchronisee.
        if isinstance(stamp, (int, float)) and stamp > 1_000_000_000:
            reference = float(stamp)
    except Exception:  # noqa: BLE001 - le repere est un plus, pas une condition
        reference = None
    if reference is None:
        reference = time.time()
    return [(reference - tick.age_s, tick.watts) for tick in ticks]


def read_probe(controller: "ScreenController", config: AppConfig) -> Levels:
    """Relit les paliers accumules par le releveur.

    Relire juste apres le lancement est normal : le releveur n'a pas encore
    ecrit, et le firmware repond alors « cle inconnue ». Ce n'est pas une
    erreur a montrer, c'est un releve encore vide.
    """
    device = controller.device_for(host_device_key(config))
    try:
        result = device.call("KVS.Get", {"key": KVS_PROBE_KEY}) or {}
    except ShellyError as exc:
        if exc.code == KVS_KEY_NOT_FOUND:
            return Levels()
        raise
    try:
        data = json.loads(result.get("value", "{}"))
    except (ValueError, TypeError):
        return Levels()
    return Levels(
        samples=int(data.get("n", 0)),
        lowest=float(data.get("mn", 0.0) or 0.0),
        highest=float(data.get("mx", 0.0) or 0.0),
        buckets=[int(x) for x in data.get("b", [])] or None,
    )


def stop_probe(controller: "ScreenController", config: AppConfig) -> None:
    """Arrete et supprime le releveur, en gardant sa derniere mesure."""
    try:
        device = controller.device_for(host_device_key(config))
    except Exception:  # noqa: BLE001
        return
    script_id = _find_script(device, PROBE_NAME)
    if script_id:
        try:
            device.call("Script.Stop", {"id": script_id})
            device.call("Script.Delete", {"id": script_id})
        except Exception:  # noqa: BLE001
            pass


def probe_running(controller: "ScreenController", config: AppConfig) -> bool:
    try:
        device = controller.device_for(host_device_key(config))
        script_id = _find_script(device, PROBE_NAME)
        if not script_id:
            return False
        info = device.call("Script.GetStatus", {"id": script_id}) or {}
        return bool(info.get("running"))
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------ gardien

GUARD_NAME = "pc_guard"


def uninstall_guard(controller: "ScreenController", config: AppConfig, device_key: str) -> None:
    """Retire le gardien d'un appareil donne."""
    try:
        device = controller.device_for(device_key)
    except Exception:  # noqa: BLE001
        return
    script_id = _find_script(device, GUARD_NAME)
    if not script_id:
        return
    try:
        device.call("Script.Stop", {"id": script_id})
        device.call("Script.Delete", {"id": script_id})
    except Exception:  # noqa: BLE001
        pass


def sync_guard(controller: "ScreenController", config: AppConfig) -> str:
    """Remet la surveillance en face de la prise reellement declaree.

    Le gardien n'est plus un script separe : il vit dans `pc_sensing`.
    L'appareil n'execute que trois scripts a la fois, et le troisieme
    emplacement doit rester libre pour le releveur de paliers. On efface
    donc les anciens `pc_guard` partout, puis on reinstalle le pilote, qui
    embarque desormais la surveillance.
    """
    for device_config in config.devices:
        uninstall_guard(controller, config, device_config.key)
    outlet = config.host_pc_outlet()
    if outlet is None:
        return "no outlet marked as powering the PC"
    if not config.sensing.enabled:
        return f"PC outlet is {outlet.ref}; install the script to watch it"
    install(controller, config)
    return f"watching {outlet.ref}"


def publish_current_state(controller: "ScreenController", config: AppConfig) -> list[int]:
    """Publie les prises actuellement alimentees, a defaut de profil.

    Le script ne sait rallumer que ce que l'application lui a laisse. Tant
    qu'aucun profil n'a ete applique, il ne trouve rien et s'en tient a
    l'ecran de demarrage -- le PC repart alors avec un seul ecran, sans que
    rien ne l'explique.

    Publier l'etat du moment est le repli le plus sensé : ce qui est
    allume maintenant est vraisemblablement ce qu'on veut retrouver.
    """
    if not config.sensing.enabled or not config.sensing.pc_ref:
        return []
    table = controlled_outlets(config)
    try:
        states = controller.read_outlets()
    except Exception:  # noqa: BLE001
        return []
    indexes = [
        index
        for index, outlet in enumerate(table)
        if states.get(outlet.ref) and states[outlet.ref].output
    ]
    if not indexes:
        return []
    try:
        device = controller.device_for(host_device_key(config))
        device.call("KVS.Set", {"key": KVS_PROFILE_KEY, "value": encode_profile(indexes)})
        return indexes
    except Exception:  # noqa: BLE001
        return []


def ensure_published(controller: "ScreenController", config: AppConfig) -> list[int]:
    """Garantit que le script a quelque chose a rallumer.

    Le dernier profil d'abord, puisque c'est l'intention exprimee ; sinon
    l'etat courant, qui vaut mieux qu'un KVS vide.
    """
    if not config.sensing.enabled or not config.sensing.pc_ref:
        return []
    if read_published_profile(controller, config):
        return []
    name = config.settings.last_profile
    if name and profile_indexes(config, name):
        publish_profile(controller, config, name)
        return profile_indexes(config, name)
    return publish_current_state(controller, config)


# ------------------------------------------------------------------- courbe

def decode_series(encoded: str) -> list[Tick]:
    """Retrouve les ticks d'une suite encodee par le releveur.

    Chaque tick occupe trois caracteres : le niveau de puissance, puis le
    temps ecoule depuis le tick precedent. Les ages sont ensuite comptes
    a rebours depuis maintenant, le dernier tick etant le plus recent.
    """
    span = math.log1p(PROBE_SERIES_MAX_W)
    raw: list[tuple[float, float]] = []
    # Le decoupage part de la fin. La suite est une file glissante dont la
    # tete se fait rogner, et un releveur plus ancien a pu y laisser un
    # tick incomplet : compter depuis le debut decalerait alors tous les
    # ticks, et la courbe n'aurait plus aucun sens. Le residu de tete est
    # ecarte, les mesures recentes -- les seules qui comptent -- restent
    # justes.
    offset = len(encoded) % TICK_CHARS
    for start in range(offset, len(encoded) - TICK_CHARS + 1, TICK_CHARS):
        level = SERIES_ALPHABET.find(encoded[start])
        high = SERIES_ALPHABET.find(encoded[start + 1])
        low = SERIES_ALPHABET.find(encoded[start + 2])
        if level < 0 or high < 0 or low < 0:
            continue  # caractere inconnu : on saute plutot que de decaler
        watts = 0.0 if level == 0 else math.expm1(level / 63 * span)
        raw.append(((high * 64 + low) * PROBE_POLL_S, watts))

    # Le temps encode est celui qui separe un tick du precedent : on le
    # cumule depuis la fin pour obtenir l'age de chacun.
    ticks: list[Tick] = []
    age = 0.0
    for gap, watts in reversed(raw):
        ticks.append(Tick(age_s=age, watts=watts))
        age += gap
    ticks.reverse()
    return ticks


def read_series(controller: "ScreenController", config: AppConfig) -> list[Tick]:
    """Relit les ticks accumules, du plus ancien au plus recent."""
    try:
        device = controller.device_for(host_device_key(config))
    except Exception:  # noqa: BLE001
        return []
    try:
        result = device.call("KVS.Get", {"key": KVS_SERIES_KEY}) or {}
    except ShellyError as exc:
        if exc.code == KVS_KEY_NOT_FOUND:
            return []
        raise
    except Exception:  # noqa: BLE001
        return []
    value = result.get("value")
    return decode_series(value) if isinstance(value, str) else []


