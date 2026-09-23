"""Modele de configuration et persistance sur disque.

La configuration tient dans un seul fichier JSON, lisible et editable a la
main. Elle decrit les appareils Shelly, le role de chaque prise, les profils
d'usage et quelques reglages de comportement.

Plusieurs appareils peuvent cohabiter -- une multiprise pour les ecrans, une
prise simple pour l'unite centrale, par exemple. Une prise se designe donc
par une reference `<cle appareil>:<numero de sortie>`, par exemple
`strip:0`. La cle est un alias court choisi a l'ajout de l'appareil ; elle
reste stable meme si l'adresse IP change, et c'est elle que les profils
referencent.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import secrets_store

CONFIG_VERSION = 2
# Par defaut la configuration vit a cote du code : l'outil est mono-poste et
# on veut pouvoir l'inspecter facilement. SHELLY_SCREENS_CONFIG permet de la
# deplacer (par exemple vers %APPDATA%).
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"

# Ce qu'une prise alimente. Distinct des roles de securite -- critique,
# ecran de demarrage, unite centrale -- qui disent comment la traiter :
# le type, lui, dit ce qui est au bout du fil.
KIND_UNKNOWN = ""
KIND_SCREEN = "screen"
KIND_ACCESSORY = "accessory"
KINDS = (KIND_UNKNOWN, KIND_SCREEN, KIND_ACCESSORY)
KIND_LABELS = {
    KIND_UNKNOWN: "Not set",  # tant qu'il vaut cela, aucun automatisme n'y touche
    KIND_SCREEN: "Screen",
    KIND_ACCESSORY: "Accessory",
}


# Prefixes lisibles par type d'appareil : avec deux multiprises, `strip` et
# `strip2` se lisent d'un coup d'oeil la ou `shellypstripg4aabbccddeeff` ne
# dit rien.
KEY_PREFIXES = {
    "powerstrip": "strip",
    "plugs": "plug",
    "plugus": "plug",
    "pluguk": "plug",
    "switch": "switch",
    "pro4pm": "pro",
}


def make_key(base: str, taken: set[str]) -> str:
    """Fabrique une cle d'appareil courte, lisible et unique."""
    slug = re.sub(r"[^a-z0-9]+", "", base.lower())
    slug = KEY_PREFIXES.get(slug, slug[:12]) or "device"
    if slug not in taken:
        return slug
    index = 2
    while f"{slug}{index}" in taken:
        index += 1
    return f"{slug}{index}"


@dataclass
class DeviceConfig:
    """Un appareil Shelly et comment le joindre."""

    key: str  # alias court, utilise dans les references de prise
    device_id: str = ""
    mac: str = ""
    # Hote utilise pour joindre l'appareil : adresse IP, ou nom mDNS quand la
    # resolution est passee par la. Reactualise a chaque connexion.
    host: str = ""
    # Adresse IPv4 correspondante, resolue a la connexion. Redondante avec
    # `host` quand celui-ci est deja une adresse, mais toujours renseignee :
    # c'est elle qu'on affiche et qu'on ouvre dans un navigateur.
    ip: str = ""
    name: str = ""  # libelle lisible, libre
    kind: str = ""  # application annoncee par l'appareil (PowerStrip, PlugS...)
    # Mot de passe de l'appareil, chiffre par DPAPI (voir secrets_store).
    # Ne jamais lire ce champ directement : passer par `get_password`.
    password: str | None = None
    switch_count: int = 0  # nombre de sorties constatees

    def get_password(self) -> str:
        """Mot de passe en clair, dechiffre a l'usage."""
        return secrets_store.unprotect(self.password or "")

    def set_password(self, plain: str) -> None:
        """Enregistre un mot de passe sous forme chiffree, ou l'efface."""
        self.password = secrets_store.protect(plain) if plain else None

    @property
    def has_password(self) -> bool:
        return bool(self.password)

    @property
    def label(self) -> str:
        return self.name or self.kind or self.key

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DeviceConfig":
        return cls(
            key=str(data.get("key", "")),
            device_id=str(data.get("device_id", "")),
            mac=str(data.get("mac", "")),
            host=str(data.get("host", "")),
            ip=str(data.get("ip", "")),
            name=str(data.get("name", "")),
            kind=str(data.get("kind", "")),
            password=data.get("password") or None,
            switch_count=int(data.get("switch_count", 0)),
        )


@dataclass
class OutletConfig:
    """Une sortie d'un appareil, et ce qu'elle alimente."""

    device: str  # cle de l'appareil
    switch_id: int  # numero de sortie sur cet appareil
    name: str = ""
    # Identifiant stable de l'ecran alimente par cette prise, tel que le
    # module win.monitors le calcule. Vide tant que l'association n'a pas
    # ete faite.
    monitor_key: str = ""
    # Une prise critique ne sera jamais coupee par un profil : garde-fou pour
    # ce qui ne doit pas s'eteindre (un dock, un NAS).
    critical: bool = False
    # L'ecran de demarrage : le garde-fou du demarrage, pas un ecran
    # privilegie. Quand le profil memorise est exploitable, cette prise
    # s'allume -- ou non -- avec les autres, comme n'importe laquelle. Elle
    # ne ressort que si ce profil manque ou ne vaut rien, pour que le PC ne
    # demarre jamais sans image.
    #
    # Sans detection de consommation, rien ne pourrait la rallumer : elle
    # reste alors sous tension a l'arret, faute de mieux.
    boot_screen: bool = False
    # Cette prise alimente l'unite centrale. Elle n'est jamais coupee, et sa
    # consommation dit si le PC tourne -- de quoi reproduire le comportement
    # d'une multiprise maitresse.
    host_pc: bool = False
    # Ce qui est branche : un ecran, ou un accessoire (concentrateur USB,
    # enceintes...). Les accessoires restent pilotables par les profils,
    # mais sortent du perimetre de l'assistant d'identification : les
    # couper ne fera disparaitre aucun ecran, et les tester ne serait que
    # du temps perdu et des coupures pour rien.
    kind: str = KIND_UNKNOWN
    # Cette prise suit-elle la veille du PC ? Les ecrans, oui : c'est tout
    # l'objet du montage. Les accessoires, seulement si on le demande --
    # couper un concentrateur USB ou des enceintes n'a rien d'evident, et
    # l'avoir fait d'office a deja surpris. Le choix se pose prise par
    # prise, puisqu'un hub inutile la nuit voisine avec un autre qui doit
    # rester eveille.
    cut_on_sleep: bool = True

    @property
    def ref(self) -> str:
        return f"{self.device}:{self.switch_id}"

    @property
    def label(self) -> str:
        return self.name or f"{self.device} {self.switch_id + 1}"

    @property
    def never_switch_off(self) -> bool:
        return self.critical or self.host_pc

    @property
    def cuts_on_sleep(self) -> bool:
        """Vrai si la veille du PC doit emporter cette prise.

        Les prises intouchables l'emportent sur la case : cocher la case
        d'une prise critique ou de celle du PC ne doit pas la rendre
        coupable pour autant.
        """
        return self.cut_on_sleep and not self.never_switch_off

    @property
    def is_screen(self) -> bool:
        """Vrai seulement si la prise est declaree comme portant un ecran.

        Le type doit etre pose explicitement : une prise non renseignee
        n'est pas traitee comme un ecran. Ce qu'on ignore, on n'y touche
        pas -- c'est la prise non classee qui se fait couper par megarde,
        jamais celle qu'on a pris le temps de declarer.
        """
        return self.kind == KIND_SCREEN and not self.host_pc

    @property
    def kind_label(self) -> str:
        if self.host_pc:
            return "PC"
        return KIND_LABELS.get(self.kind, self.kind)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OutletConfig":
        return cls(
            device=str(data["device"]),
            switch_id=int(data["switch_id"]),
            name=str(data.get("name", "")),
            monitor_key=str(data.get("monitor_key", "")),
            critical=bool(data.get("critical", False)),
            boot_screen=bool(data.get("boot_screen", False)),
            host_pc=bool(data.get("host_pc", False)),
            kind=str(data.get("kind", KIND_UNKNOWN)),
            # Absent des fichiers anterieurs : on reconduit le comportement
            # attendu pour les ecrans, et on cesse de couper les accessoires
            # que personne n'avait explicitement designes.
            cut_on_sleep=bool(
                data.get("cut_on_sleep", str(data.get("kind", KIND_UNKNOWN)) == KIND_SCREEN)
            ),
        )


def parse_ref(ref: str) -> tuple[str, int]:
    """Decoupe une reference `cle:sortie`."""
    device, _, switch = ref.rpartition(":")
    return device, int(switch)


@dataclass
class Profile:
    """Un usage : quelles prises sont alimentees, et ou vont les fenetres."""

    name: str
    # References de prises (`cle:sortie`) alimentees par ce profil.
    outlets_on: list[str] = field(default_factory=list)
    # Disposition des fenetres memorisee pour ce profil ; structure produite
    # et relue par win.layout (liste d'entrees serialisees).
    layout: list[dict[str, Any]] = field(default_factory=list)
    # Rang d'affichage dans le menu.
    order: int = 0

    def wants(self, ref: str) -> bool:
        return ref in self.outlets_on

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "outlets_on": sorted(set(self.outlets_on)),
            "order": self.order,
            "layout": self.layout,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Profile":
        return cls(
            name=str(data["name"]),
            outlets_on=[str(x) for x in data.get("outlets_on", [])],
            layout=list(data.get("layout", [])),
            order=int(data.get("order", 0)),
        )


@dataclass
class PowerSensing:
    """Detection de l'activite du PC par sa consommation.

    Quand le PC est eteint, aucun logiciel ne tourne pour commander les
    prises : c'est la multiprise elle-meme, via un script embarque, qui
    surveille la consommation de l'unite centrale et rallume les ecrans des
    qu'elle la voit repartir. Ces reglages sont ceux de ce script.

    Deux seuils plutot qu'un : entre les deux se trouve une zone morte ou
    l'etat courant se maintient, sans quoi une consommation oscillant autour
    d'une valeur unique ferait claquer le relais en boucle.
    """

    enabled: bool = False
    pc_ref: str = ""  # reference de la prise alimentant l'unite centrale
    on_threshold_w: float = 25.0  # au-dessus, le PC est considere actif
    off_threshold_w: float = 15.0  # en dessous, il est considere eteint
    on_delay_s: float = 3.0  # confirmation avant d'allumer : court
    # Confirmation avant de couper : long a dessein. Lors d'un redemarrage
    # de Windows le PC passe sous le seuil dix a quinze secondes, et couper
    # les ecrans a cet instant serait le pire moment.
    off_delay_s: float = 90.0
    poll_interval_s: float = 2.0
    script_id: int = 0  # identifiant du script installe, 0 si aucun
    # Empreinte du code reellement pose sur l'appareil. Comparee a celle
    # du code qu'on produirait maintenant, elle dit si un reglage a
    # change depuis -- un seuil, un type de prise, un mot de passe --
    # sans avoir ete transmis. Un drapeau qu'il faudrait lever a la main
    # finirait par etre oublie ; une empreinte ne s'oublie pas.
    installed_fingerprint: str = ""
    # Paliers releves par l'assistant de calibration, en watts.
    measured_idle_w: float = 0.0  # PC allume, au repos
    measured_sleep_w: float = 0.0  # PC en veille
    measured_off_w: float = 0.0  # PC eteint

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PowerSensing":
        defaults = cls()
        return cls(
            enabled=bool(data.get("enabled", defaults.enabled)),
            pc_ref=str(data.get("pc_ref", "")),
            installed_fingerprint=str(data.get("installed_fingerprint", "")),
            on_threshold_w=float(data.get("on_threshold_w", defaults.on_threshold_w)),
            off_threshold_w=float(data.get("off_threshold_w", defaults.off_threshold_w)),
            on_delay_s=float(data.get("on_delay_s", defaults.on_delay_s)),
            off_delay_s=float(data.get("off_delay_s", defaults.off_delay_s)),
            poll_interval_s=float(data.get("poll_interval_s", defaults.poll_interval_s)),
            script_id=int(data.get("script_id", 0)),
            measured_idle_w=float(data.get("measured_idle_w", 0.0)),
            measured_sleep_w=float(data.get("measured_sleep_w", 0.0)),
            measured_off_w=float(data.get("measured_off_w", 0.0)),
        )

    def thresholds_are_sane(self) -> str:
        """Message d'alerte si les seuils ne tiennent pas debout, sinon vide."""
        if self.off_threshold_w >= self.on_threshold_w:
            return "The off threshold must stay below the on threshold."
        if self.measured_idle_w and self.on_threshold_w >= self.measured_idle_w:
            return (
                f"The on threshold ({self.on_threshold_w:.0f} W) is above the "
                f"measured idle draw ({self.measured_idle_w:.0f} W): the PC "
                "would never be seen as running."
            )
        if self.measured_sleep_w and self.off_threshold_w <= self.measured_sleep_w:
            return (
                f"The off threshold ({self.off_threshold_w:.0f} W) is below the "
                f"measured sleep draw ({self.measured_sleep_w:.0f} W): the PC "
                "would never be seen as asleep."
            )
        return ""


@dataclass
class Settings:
    """Reglages de comportement."""

    # Couper toutes les prises non protegees quand le PC se met en veille.
    power_off_on_suspend: bool = True
    # Reappliquer le dernier profil au reveil.
    restore_on_resume: bool = True
    # Memoriser / restaurer la position des fenetres avec les profils.
    manage_window_layout: bool = True
    # Pause entre deux commandes de prise, pour ne pas noyer un appareil.
    switch_delay_ms: int = 250
    # Temps max d'attente de la prise en compte des ecrans par Windows.
    display_settle_timeout_s: float = 20.0
    # Profil applique en dernier, reapplique au reveil et au demarrage.
    last_profile: str = ""
    # Prises alimentees juste avant la mise en veille. Sans profil applique,
    # c'est la seule trace de ce qu'il faut rendre au reveil : le nom d'un
    # profil peut manquer, l'etat des prises, lui, existe toujours.
    resume_refs: list[str] = field(default_factory=list)
    # Reappliquer le dernier profil au lancement de l'application.
    apply_profile_on_start: bool = False
    # Theme de la fenetre de reglages : system, light ou dark.
    theme: str = "system"
    # Langue de l'interface : system, en ou fr.
    language: str = "system"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Settings":
        defaults = cls()
        return cls(
            power_off_on_suspend=bool(
                data.get("power_off_on_suspend", defaults.power_off_on_suspend)
            ),
            restore_on_resume=bool(data.get("restore_on_resume", defaults.restore_on_resume)),
            manage_window_layout=bool(
                data.get("manage_window_layout", defaults.manage_window_layout)
            ),
            switch_delay_ms=int(data.get("switch_delay_ms", defaults.switch_delay_ms)),
            display_settle_timeout_s=float(
                data.get("display_settle_timeout_s", defaults.display_settle_timeout_s)
            ),
            last_profile=str(data.get("last_profile", "")),
            resume_refs=[str(r) for r in data.get("resume_refs", [])],
            apply_profile_on_start=bool(
                data.get("apply_profile_on_start", defaults.apply_profile_on_start)
            ),
            theme=str(data.get("theme", defaults.theme)),
            language=str(data.get("language", defaults.language)),
        )


@dataclass
class AppConfig:
    """Racine de la configuration."""

    devices: list[DeviceConfig] = field(default_factory=list)
    outlets: list[OutletConfig] = field(default_factory=list)
    profiles: list[Profile] = field(default_factory=list)
    settings: Settings = field(default_factory=Settings)
    sensing: PowerSensing = field(default_factory=PowerSensing)
    path: Path = field(default=DEFAULT_CONFIG_PATH, compare=False, repr=False)

    # ------------------------------------------------------------- acces

    def device(self, key: str) -> DeviceConfig | None:
        for device in self.devices:
            if device.key == key:
                return device
        return None

    def outlet(self, ref: str) -> OutletConfig | None:
        for outlet in self.outlets:
            if outlet.ref == ref:
                return outlet
        return None

    def outlets_of(self, device_key: str) -> list[OutletConfig]:
        return [o for o in self.outlets if o.device == device_key]

    def profile(self, name: str) -> Profile | None:
        for profile in self.profiles:
            if profile.name == name:
                return profile
        return None

    def sorted_profiles(self) -> list[Profile]:
        return sorted(self.profiles, key=lambda p: (p.order, p.name.lower()))

    def refs(self) -> list[str]:
        return [outlet.ref for outlet in self.outlets]

    def ensure_outlets(self, device_key: str, count: int) -> None:
        """Complete la liste des prises pour couvrir un appareil reel."""
        known = {o.switch_id for o in self.outlets_of(device_key)}
        for switch_id in range(count):
            if switch_id not in known:
                self.outlets.append(OutletConfig(device=device_key, switch_id=switch_id))
        device = self.device(device_key)
        if device is not None:
            device.switch_count = max(device.switch_count, count)
        self.outlets.sort(key=lambda o: (self._device_order(o.device), o.switch_id))

    def _device_order(self, device_key: str) -> int:
        for index, device in enumerate(self.devices):
            if device.key == device_key:
                return index
        return len(self.devices)

    def add_device(self, device: DeviceConfig) -> DeviceConfig:
        """Ajoute un appareil, en lui donnant une cle libre si besoin."""
        if not device.key:
            device.key = make_key(device.kind or device.device_id, self.device_keys())
        elif self.device(device.key) is not None:
            device.key = make_key(device.key, self.device_keys())
        self.devices.append(device)
        return device

    def device_keys(self) -> set[str]:
        return {device.key for device in self.devices}

    def rename_device(self, old_key: str, new_key: str) -> bool:
        """Change la cle d'un appareil et propage aux prises et aux profils."""
        device = self.device(old_key)
        if device is None or not new_key or self.device(new_key) is not None:
            return False
        device.key = new_key
        for outlet in self.outlets:
            if outlet.device == old_key:
                outlet.device = new_key
        for profile in self.profiles:
            profile.outlets_on = [
                self._repoint(ref, old_key, new_key) for ref in profile.outlets_on
            ]
        # Les references hors profils comptent autant : laisser `pc_ref`
        # pointer sur l'ancienne cle rend la detection muette -- l'appareil
        # designe n'existe plus -- sans le moindre message, et `resume_refs`
        # perime rendrait un reveil incapable de restituer quoi que ce soit.
        self.sensing.pc_ref = self._repoint(self.sensing.pc_ref, old_key, new_key)
        self.settings.resume_refs = [
            self._repoint(ref, old_key, new_key)
            for ref in self.settings.resume_refs
        ]
        return True

    @staticmethod
    def _repoint(ref: str, old_key: str, new_key: str) -> str:
        """Reecrit une reference de prise apres un changement de cle."""
        if not ref:
            return ref
        device, switch = parse_ref(ref)
        return f"{new_key}:{switch}" if device == old_key else ref

    def remove_device(self, key: str) -> None:
        """Retire un appareil, ses prises, et les references qui y pointent."""
        self.devices = [d for d in self.devices if d.key != key]
        self.outlets = [o for o in self.outlets if o.device != key]
        remaining = set(self.refs())
        for profile in self.profiles:
            profile.outlets_on = [r for r in profile.outlets_on if r in remaining]

    def controllable_outlets(self) -> list[OutletConfig]:
        """Prises qu'un profil a le droit de manoeuvrer."""
        return [outlet for outlet in self.outlets if not outlet.never_switch_off]

    def screen_outlets(self) -> list[OutletConfig]:
        """Prises declarees comme portant un ecran."""
        return [outlet for outlet in self.outlets if outlet.is_screen]

    def unclassified_outlets(self) -> list[OutletConfig]:
        """Prises dont le type n'a pas ete renseigne.

        Elles ne sont manoeuvrees par aucun automatisme tant qu'on ignore
        ce qu'elles alimentent.
        """
        return [
            outlet
            for outlet in self.outlets
            if outlet.kind == KIND_UNKNOWN and not outlet.host_pc
        ]

    def boot_screen_outlet(self) -> OutletConfig | None:
        """La prise qui doit rester alimentee quand le PC s'eteint."""
        for outlet in self.outlets:
            if outlet.boot_screen:
                return outlet
        return None

    def host_pc_outlet(self) -> OutletConfig | None:
        """La prise qui alimente l'unite centrale, si elle est connue."""
        for outlet in self.outlets:
            if outlet.host_pc:
                return outlet
        return None

    def shutdown_refs_on(self) -> list[str]:
        """Prises a laisser alimentees a la veille ou a l'arret du PC.

        L'ecran de demarrage n'en fait partie que si rien ne peut le
        rallumer. Des lors que le script embarque surveille la
        consommation, il s'en charge au prochain allumage : le garder sous
        tension ne ferait que consommer pour rien, et contredirait le but
        meme de la detection -- tout couper a l'arret.
        """
        keep = [outlet.ref for outlet in self.outlets if not outlet.cuts_on_sleep]
        if not self.sensing.enabled:
            keep += [
                outlet.ref
                for outlet in self.outlets
                if outlet.boot_screen and outlet.ref not in keep
            ]
        return keep

    # -------------------------------------------------------- persistance

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": CONFIG_VERSION,
            "devices": [device.to_dict() for device in self.devices],
            "outlets": [outlet.to_dict() for outlet in self.outlets],
            "profiles": [profile.to_dict() for profile in self.sorted_profiles()],
            "settings": self.settings.to_dict(),
            "sensing": self.sensing.to_dict(),
        }

    def normalise_roles(self) -> None:
        """Garantit l'unicite des roles exclusifs.

        L'interface l'assure deja, mais un fichier edite a la main -- ou une
        configuration a demi migree -- pourrait porter deux ecrans de
        demarrage. On garde le premier de chaque role, car deux prises
        rivales rendraient le comportement a l'arret imprevisible.
        """
        # Une prise associee a un ecran en est un : on le deduit plutot que
        # de demander a nouveau ce qui est deja connu.
        for outlet in self.outlets:
            if not outlet.kind and outlet.monitor_key:
                outlet.kind = KIND_SCREEN

        for attribute in ("boot_screen", "host_pc"):
            seen = False
            for outlet in self.outlets:
                if getattr(outlet, attribute):
                    if seen:
                        setattr(outlet, attribute, False)
                    seen = True

        self._heal_refs()

    def _heal_refs(self) -> None:
        """Repose les references qui ne designent plus aucune prise.

        Une reference orpheline -- le plus souvent laissee par un changement
        de cle d'appareil -- ne se signale nulle part : la detection cherche
        un appareil absent et se tait, et la panne ne se decouvre qu'a la
        premiere veille. On repose donc `pc_ref` sur la prise qui porte
        effectivement le PC, et l'on ecarte les references de reveil
        devenues sans objet plutot que de tenter de les rallumer.
        """
        known = set(self.refs())
        if self.sensing.pc_ref and self.sensing.pc_ref not in known:
            host = self.host_pc_outlet()
            self.sensing.pc_ref = host.ref if host is not None else ""
        self.settings.resume_refs = [
            ref for ref in self.settings.resume_refs if ref in known
        ]

    @classmethod
    def from_dict(cls, data: dict[str, Any], path: Path) -> "AppConfig":
        data = migrate(data)
        config = cls(
            devices=[DeviceConfig.from_dict(item) for item in data.get("devices", [])],
            outlets=[OutletConfig.from_dict(item) for item in data.get("outlets", [])],
            profiles=[Profile.from_dict(item) for item in data.get("profiles", [])],
            settings=Settings.from_dict(data.get("settings", {})),
            sensing=PowerSensing.from_dict(data.get("sensing", {})),
            path=path,
        )
        config.normalise_roles()
        return config

    def save(self, path: Path | None = None) -> None:
        """Ecrit la configuration de facon atomique (fichier temporaire puis remplacement)."""
        target = Path(path or self.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.to_dict(), indent=2, ensure_ascii=False)
        handle, temp_name = tempfile.mkstemp(
            dir=str(target.parent), prefix=target.name, suffix=".tmp"
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.write("\n")
            os.replace(temp_name, target)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise


def migrate(data: dict[str, Any]) -> dict[str, Any]:
    """Amene une configuration ancienne au format courant.

    Version 1 : un seul appareil, decrit par un objet `device`, des prises
    numerotees `id`, et des profils referencant ces numeros. On lui invente
    une cle d'appareil et on reecrit les references en `cle:sortie`.
    """
    version = int(data.get("version", 1))
    if version >= CONFIG_VERSION:
        return data

    if version == 1:
        legacy_device = data.get("device") or {}
        key = make_key("PowerStrip", set())
        data = dict(data)
        data["devices"] = [
            {
                "key": key,
                "device_id": legacy_device.get("device_id", ""),
                "mac": legacy_device.get("mac", ""),
                "host": legacy_device.get("host", ""),
                "name": "",
                "kind": "PowerStrip",
                "password": legacy_device.get("password"),
                "switch_count": len(data.get("outlets", [])),
            }
        ]
        data.pop("device", None)
        data["outlets"] = [
            {
                "device": key,
                "switch_id": int(item.get("id", index)),
                "name": item.get("name", ""),
                "monitor_key": item.get("monitor_key", ""),
                "critical": item.get("critical", False),
                "boot_screen": item.get("boot_screen", False),
                "host_pc": False,
            }
            for index, item in enumerate(data.get("outlets", []))
        ]
        data["profiles"] = [
            {
                **profile,
                "outlets_on": [f"{key}:{int(i)}" for i in profile.get("outlets_on", [])],
            }
            for profile in data.get("profiles", [])
        ]
        data["version"] = 2

    return data


def config_path() -> Path:
    """Emplacement du fichier de configuration."""
    override = os.environ.get("SHELLY_SCREENS_CONFIG")
    return Path(override).expanduser() if override else DEFAULT_CONFIG_PATH


def load(path: Path | None = None) -> AppConfig:
    """Charge la configuration, ou renvoie une configuration vierge."""
    target = Path(path) if path else config_path()
    if not target.exists():
        return AppConfig(path=target)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Configuration illisible ({target}): {exc}") from exc
    return AppConfig.from_dict(data, path=target)


def default_profiles(refs: list[str]) -> list[Profile]:
    """Profils proposes au premier lancement, a ajuster ensuite."""
    first = refs[:1]
    half = refs[: max(1, len(refs) // 2)]
    return [
        Profile(name="All on", outlets_on=list(refs), order=0),
        Profile(name="Work", outlets_on=half, order=1),
        Profile(name="Focus", outlets_on=first, order=2),
        Profile(name="All off", outlets_on=[], order=3),
    ]
