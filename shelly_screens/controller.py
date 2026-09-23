"""Orchestration : appareils, prises, ecrans et fenetres.

Le controleur est la seule piece qui connait l'enchainement complet d'un
changement de profil. Il ne depend d'aucune interface graphique, de facon a
rester testable et pilotable depuis n'importe ou.

Plusieurs appareils Shelly cohabitent -- typiquement deux multiprises, ou une
multiprise et une prise simple pour l'unite centrale. Chacun est joint
independamment : un appareil injoignable n'empeche pas les autres de
repondre, et le rapport dit ce qui n'a pas pu etre fait.

L'ordre des operations n'est pas anodin :

1. memoriser la disposition des fenetres du profil que l'on quitte ;
2. ALLUMER d'abord les ecrans manquants, et attendre que Windows les voie.
   Allumer avant d'eteindre evite de se retrouver, ne serait-ce qu'un
   instant, sans aucun ecran -- et laisse a la dalle ses quelques secondes
   d'initialisation ;
3. eteindre ensuite ce qui doit l'etre ;
4. rejouer la disposition memorisee pour le profil demande.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace
from typing import Callable

from . import discovery
from . import sensing
from .config import AppConfig, DeviceConfig, Profile, parse_ref
from .device import (
    AuthenticationFailed,
    ProtectedOutlet,
    ShellyDevice,
    ShellyError,
    ShellyUnreachable,
    SwitchState,
)
from .win import layout, monitors

# Marge laissee a la dalle apres que Windows a annonce l'ecran.
DISPLAY_GRACE_S = 1.2
# Periode de scrutation de la liste des ecrans.
POLL_INTERVAL_S = 0.4

LogFn = Callable[[str], None]


@dataclass
class ApplyReport:
    """Resultat d'un changement de profil, pour l'affichage et les journaux."""

    profile: str
    turned_on: list[str] = field(default_factory=list)
    turned_off: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    displays_waited_s: float = 0.0
    windows_restored: int = 0
    windows_unmatched: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        parts = [f"Profile '{self.profile}'"]
        if self.turned_on:
            parts.append(f"on: {', '.join(sorted(self.turned_on))}")
        if self.turned_off:
            parts.append(f"off: {', '.join(sorted(self.turned_off))}")
        if not self.turned_on and not self.turned_off:
            parts.append("no change")
        if self.windows_restored:
            parts.append(f"{self.windows_restored} window(s) restored")
        if self.errors:
            parts.append(f"{len(self.errors)} error(s)")
        return " | ".join(parts)


class NotConnected(RuntimeError):
    """Aucun appareil joignable pour l'instant."""


class SensingRealmMissing(RuntimeError):
    """Impossible de poser un mot de passe sans connaitre l'identite de l'appareil."""

    def __init__(self, key: str) -> None:
        super().__init__(
            f"Device '{key}' has never answered: connect to it once before "
            "setting a password."
        )


# Une resolution complete coute une requete mDNS et un sondage HTTP. La
# refaire a chaque lecture ratee revient a punir un appareil deja en
# difficulte : c'est ainsi qu'on a compte dix resolutions en dix secondes
# pendant un reveil, au moment precis ou la multiprise saturait.
RESOLVE_COOLDOWN_S = 30.0
# Une tension secteur ne reste jamais parfaitement constante : elle
# oscille toujours d'un dixieme de volt d'une mesure a l'autre. Plusieurs
# releves rigoureusement identiques ne sont donc pas une mesure mais une
# valeur gelee -- la voie du firmware a lache. Le symptome est sournois :
# l'appareil repond, les prises obeissent, et seule la detection de veille
# raisonne sur un chiffre mort. Elle ne coupe alors plus rien, sans que
# rien ne le signale. Six lectures, soit une demi-minute, suffisent a
# distinguer le gel d'une coincidence.
FROZEN_METER_READS = 6
# Le signal se relit a part, et rarement. Une liaison Wi-Fi ne change pas
# d'un battement de cil, et chaque interrogation supplementaire pese sur
# un firmware dont on a appris ce soir la fragilite : une fois par minute
# suffit largement a voir une degradation s'installer.
SIGNAL_REFRESH_S = 60.0
# Apres un echec, on espace les interrogations au lieu de les maintenir.
# On revient au rythme normal des que l'appareil repond.
READ_BACKOFF_S = (0.0, 15.0, 30.0, 60.0)


class ScreenController:
    """Pilote un ou plusieurs appareils Shelly en fonction des profils."""

    def __init__(self, app_config: AppConfig, log: LogFn | None = None) -> None:
        self.config = app_config
        self._log: LogFn = log or (lambda message: None)
        self._devices: dict[str, ShellyDevice] = {}
        self._identities: dict[str, discovery.DeviceIdentity] = {}
        # Appareils qui repondent mais refusent le mot de passe. Distingues
        # des injoignables : insister ne sert a rien, et seule une
        # reinitialisation par les boutons permet d'en sortir.
        self.auth_failures: dict[str, str] = {}
        # Date de la derniere resolution tentee, par appareil : elle arme le
        # delai de garde qui empeche d'en enchainer une a chaque echec.
        self._last_resolve: dict[str, float] = {}
        # Nombre d'echecs de lecture consecutifs, et date avant laquelle il
        # est inutile de retenter. Un appareil qui peine recoit ainsi moins
        # de trafic, pas davantage.
        self._read_failures: dict[str, int] = {}
        self._retry_after: dict[str, float] = {}
        # Dernieres tensions relevees par prise, pour reperer une voie
        # de mesure qui ne bouge plus.
        self._meter_history: dict[str, list[float]] = {}
        # Dernier RSSI connu par appareil, et date de la prochaine
        # relecture. Absent tant qu'on n'a pas pu le lire.
        self._signal: dict[str, int] = {}
        self._signal_due: dict[str, float] = {}
        # Une seule sequence a la fois : un changement de profil manipule
        # l'alimentation et les fenetres, deux en parallele se marcheraient
        # dessus.
        self._lock = threading.RLock()

    # ------------------------------------------------------------ connexion

    @property
    def online_keys(self) -> set[str]:
        return set(self._devices)

    @property
    def connected(self) -> bool:
        """Vrai si au moins un appareil repond."""
        return bool(self._devices)

    @property
    def fully_connected(self) -> bool:
        """Vrai si tous les appareils configures repondent."""
        return bool(self.config.devices) and len(self._devices) == len(self.config.devices)

    def identity(self, key: str) -> discovery.DeviceIdentity | None:
        return self._identities.get(key)

    def connect_all(self, allow_scan: bool = True) -> dict[str, bool]:
        """Resout tous les appareils configures ; renvoie leur etat par cle."""
        results: dict[str, bool] = {}
        for device_config in list(self.config.devices):
            results[device_config.key] = (
                self.connect_device(device_config.key, allow_scan, force=True)
                is not None
            )
        return results

    def connect_device(
        self, key: str, allow_scan: bool = True, force: bool = False
    ) -> discovery.DeviceIdentity | None:
        """Retrouve un appareil et memorise son adresse.

        `force` passe outre le delai de garde : c'est ce que fait le bouton
        de reconnexion, ou un demarrage, ou un changement de mot de passe --
        des gestes voulus, qui ne doivent pas attendre.
        """
        device_config = self.config.device(key)
        if device_config is None:
            return None

        now = time.monotonic()
        if not force:
            last = self._last_resolve.get(key)
            if last is not None and now - last < RESOLVE_COOLDOWN_S:
                return self._identities.get(key)
        self._last_resolve[key] = now

        identity = discovery.resolve(
            known_host=device_config.host or None,
            device_id=device_config.device_id or None,
            expected_mac=device_config.mac or None,
            allow_scan=allow_scan,
        )
        if identity is None:
            self._devices.pop(key, None)
            self._identities.pop(key, None)
            self._log(f"Device '{key}' not found on the local network")
            return None

        address = discovery.address_of(identity.host)
        changed = device_config.host != identity.host or device_config.ip != address
        device_config.host = identity.host
        device_config.ip = address or device_config.ip
        device_config.device_id = identity.device_id
        device_config.mac = identity.mac
        if not device_config.kind:
            device_config.kind = identity.app

        was_connected = key in self._devices
        self._identities[key] = identity
        self._devices[key] = ShellyDevice(
            identity.host,
            password=device_config.get_password() or None,
            protected=self.protected_switches(key),
        )
        count = self._switch_count(key)
        if count:
            self.config.ensure_outlets(key, count)
        if not was_connected:
            # Un appareil qui apparait est peut-etre neuf, remis a zero ou
            # revenu d'un changement de firmware : ses sorties reprennent
            # alors le reglage d'usine, qui les ouvre toutes au demarrage --
            # la prise du PC comprise. On repose la garantie ici plutot qu'au
            # seul lancement de l'application, qui peut tourner depuis des
            # heures quand l'appareil, lui, vient de renaitre.
            self.enforce_power_on_state(key)
        if changed:
            self._save()
        # On ne journalise que ce qui apprend quelque chose : une premiere
        # connexion, ou une adresse qui a bouge. Repeter la meme ligne a
        # chaque appel noyait le journal -- cent onze lignes pour deux
        # appareils -- juste quand il fallait pouvoir le lire.
        if changed or not was_connected:
            where = (
                f"{identity.host} ({address})"
                if address and address != identity.host
                else identity.host
            )
            self._log(f"Device '{key}' connected at {where}")
        return identity

    def protected_switches(self, device_key: str) -> set[int]:
        """Sorties de cet appareil qu'aucune commande ne doit couper."""
        return {
            outlet.switch_id
            for outlet in self.config.outlets_of(device_key)
            if outlet.never_switch_off
        }

    def refresh_protection(self) -> None:
        """Repropage les protections vers les clients deja ouverts.

        A appeler des qu'un role change : un client cree avant le marquage
        garderait sinon l'ancienne liste, et la sortie du PC redeviendrait
        coupable.
        """
        for key, device in self._devices.items():
            device.protect(self.protected_switches(key))
            self.enforce_power_on_state(key)

    def enforce_power_on_state(self, device_key: str) -> list[str]:
        """Pose ce que chaque sortie doit faire quand l'appareil redemarre.

        Ce reglage vit dans la multiprise, hors de portee du script comme de
        l'application, et il decide seul du sort des sorties a chaque
        demarrage. Livre sur `off`, il coupe tout au moindre redemarrage --
        mise a jour du firmware, micro-coupure, chien de garde -- et la
        prise du PC avec : la machine s'arrete net, sans qu'aucune de nos
        protections ait eu son mot a dire. C'est ainsi qu'un redemarrage de
        la multiprise a coupe le PC en pleine session.

        La sortie du PC et les sorties critiques repartent donc allumees.
        Les autres reprennent leur etat anterieur : une multiprise d'ecrans
        qui redemarre pendant que le PC tourne doit rendre l'image, et le
        script ne la rallumerait pas -- il n'agit qu'aux changements d'etat
        du PC, et celui-ci n'a pas bouge.
        """
        device = self._devices.get(device_key)
        if device is None:
            return []
        changed: list[str] = []
        for outlet in self.config.outlets_of(device_key):
            wanted = "on" if outlet.never_switch_off else "restore_last"
            try:
                current = (device.call(
                    "Switch.GetConfig", {"id": outlet.switch_id}
                ) or {}).get("initial_state")
                if current == wanted:
                    continue
                # Un reglage, pas une commutation : la sortie ne bouge pas.
                device.call(
                    "Switch.SetConfig",
                    {"id": outlet.switch_id, "config": {"initial_state": wanted}},
                )
            except Exception as exc:  # noqa: BLE001 - ne jamais bloquer la connexion
                self._log(f"Could not set the power-on state of {outlet.ref}: {exc}")
                continue
            changed.append(f"{outlet.ref} {current} -> {wanted}")
        if changed:
            self._log("Power-on state corrected: " + ", ".join(changed))
        return changed

    def _switch_count(self, key: str) -> int:
        """Nombre de sorties reellement presentes sur un appareil."""
        device = self._devices.get(key)
        if device is None:
            return 0
        try:
            return device.count_switches()
        except (ShellyUnreachable, ShellyError):
            return 0

    def adopt(self, identity: discovery.DeviceIdentity, name: str = "") -> DeviceConfig:
        """Ajoute un appareil decouvert a la configuration."""
        existing = next(
            (d for d in self.config.devices if d.mac.upper() == identity.mac.upper()), None
        )
        if existing is not None:
            existing.host = identity.host
            self._save()
            return existing
        device_config = self.config.add_device(
            DeviceConfig(
                key="",
                device_id=identity.device_id,
                mac=identity.mac,
                host=identity.host,
                name=name,
                kind=identity.app,
            )
        )
        self.connect_device(device_config.key, allow_scan=False)
        self._save()
        self._log(f"Device '{device_config.key}' added ({identity.model} at {identity.host})")
        return device_config

    def forget(self, key: str) -> None:
        self._devices.pop(key, None)
        self._identities.pop(key, None)
        self.config.remove_device(key)
        self._save()
        self._log(f"Device '{key}' removed")

    def device_for(self, key: str) -> ShellyDevice:
        """Appareil joignable pour cette cle, avec une tentative de reconnexion."""
        device = self._devices.get(key)
        if device is not None:
            return device
        self.connect_device(key)
        # On verifie le client, pas la valeur de retour : pendant le delai
        # de garde, `connect_device` rend l'identite deja connue sans avoir
        # reconstruit quoi que ce soit. S'y fier ferait croire l'appareil
        # joignable alors qu'aucun client n'existe.
        device = self._devices.get(key)
        if device is None:
            raise NotConnected(f"Device '{key}' is not reachable")
        return device

    # -------------------------------------------------------------- lecture

    def read_outlets(self) -> dict[str, SwitchState]:
        """Etat courant de toutes les prises, indexe par reference.

        Un appareil muet est simplement absent du resultat : les autres
        restent lisibles, et l'appelant voit quelles prises manquent.
        """
        states: dict[str, SwitchState] = {}
        now = time.monotonic()
        for device_config in self.config.devices:
            key = device_config.key
            # Un appareil qui vient d'echouer se voit accorder un repit.
            # Le relancer toutes les cinq secondes revenait a l'accabler au
            # moment ou il tenait le moins debout, et c'est cette rafale qui
            # a precede ses deux plantages.
            if now < self._retry_after.get(key, 0.0):
                continue
            try:
                switches = self.device_for(key).get_all_switches()
            except AuthenticationFailed as exc:
                # Inutile de retenter : l'appareil repond, c'est le mot de
                # passe qui ne convient pas.
                self.auth_failures[key] = str(exc)
                continue
            except (NotConnected, ShellyUnreachable, ShellyError):
                # On ne reconstruit plus la connexion sur-le-champ : le
                # delai de garde de `connect_device` s'en chargera au
                # prochain tour, une fois l'appareil calme.
                self._devices.pop(key, None)
                attempt = self._read_failures.get(key, 0) + 1
                self._read_failures[key] = attempt
                delay = READ_BACKOFF_S[min(attempt, len(READ_BACKOFF_S) - 1)]
                self._retry_after[key] = now + delay
                if delay:
                    self._log(
                        f"Device '{key}' unreachable ({attempt}), next try in "
                        f"{delay:.0f} s"
                    )
                continue
            self._read_failures.pop(key, None)
            self._retry_after.pop(key, None)
            self.auth_failures.pop(key, None)
            for switch_id, state in switches.items():
                ref = f"{key}:{switch_id}"
                states[ref] = state
                self._note_meter(ref, state)
            # L'appareil vient de repondre : c'est le bon moment, et le
            # seul ou l'on est sur de ne pas le deranger pour rien.
            if now >= self._signal_due.get(key, 0.0):
                self._refresh_signal(key, now)
        return states

    def _refresh_signal(self, key: str, now: float) -> None:
        """Relit la puissance du signal Wi-Fi, sans jamais faire echouer."""
        self._signal_due[key] = now + SIGNAL_REFRESH_S
        try:
            status = self._devices[key].call("Wifi.GetStatus") or {}
        except Exception:  # noqa: BLE001 - une mesure de confort, pas plus
            return
        rssi = status.get("rssi")
        if isinstance(rssi, (int, float)) and rssi:
            self._signal[key] = int(rssi)

    def wifi_signal(self, key: str) -> int | None:
        """Dernier RSSI connu, en dBm, ou None s'il n'a pas ete lu."""
        return self._signal.get(key)

    def _note_meter(self, ref: str, state: SwitchState) -> None:
        """Retient la tension relevee, pour juger si la voie est vivante."""
        # Une prise coupee ne mesure rien : sa tension nulle et constante
        # ne dit pas que le firmware a lache.
        if not state.output or state.voltage <= 0:
            self._meter_history.pop(ref, None)
            return
        readings = self._meter_history.setdefault(ref, [])
        readings.append(state.voltage)
        del readings[:-FROZEN_METER_READS]

    def frozen_meters(self) -> set[str]:
        """Prises dont la mesure semble gelee."""
        return {
            ref
            for ref, readings in self._meter_history.items()
            if len(readings) >= FROZEN_METER_READS and len(set(readings)) == 1
        }

    def reboot_device(self, key: str) -> None:
        """Redemarre un appareil.

        Sans danger pour les sorties : leurs relais sont bistables et
        gardent leur position, et `initial_state` ramene de toute facon la
        prise du PC et les prises critiques sous tension.
        """
        device = self.device_for(key)
        try:
            device.call("Shelly.Reboot")
        except Exception:  # noqa: BLE001 - la reponse se perd avec la connexion
            pass
        # L'appareil part : on oublie tout ce qu'on croyait savoir de lui.
        self._devices.pop(key, None)
        self._last_resolve.pop(key, None)
        for ref in list(self._meter_history):
            if ref.startswith(f"{key}:"):
                self._meter_history.pop(ref, None)
        self._log(f"Device '{key}': restart requested")

    # ------------------------------------------------------------- actions

    def set_device_password(self, key: str, password: str) -> None:
        """Active, change ou retire le mot de passe d'un appareil.

        Une chaine vide retire l'authentification. Le mot de passe est
        memorise chiffre, et le client reconstruit pour l'utiliser aussitot.
        """
        device_config = self.config.device(key)
        if device_config is None:
            raise KeyError(f"Unknown device: {key}")
        realm = device_config.device_id or (self.identity(key).device_id if self.identity(key) else "")
        if not realm:
            raise SensingRealmMissing(key)
        self.device_for(key).set_password(realm, password)
        device_config.set_password(password)
        self._devices[key] = ShellyDevice(device_config.host, password=password or None)
        # L'identite est une photo prise a la connexion : sans cette mise a
        # jour, elle continuerait d'annoncer un appareil sans mot de passe
        # alors qu'on vient de lui en poser un. L'interface s'y fie pour
        # afficher l'etat reel, et afficherait donc le contraire.
        identity = self._identities.get(key)
        if identity is not None:
            self._identities[key] = replace(identity, auth_enabled=bool(password))
        self.auth_failures.pop(key, None)
        self._save()
        self._log(f"Device '{key}': password {'set' if password else 'removed'}")

    def set_outlet(self, ref: str, on: bool) -> None:
        """Manoeuvre une prise, en respectant les garde-fous."""
        outlet = self.config.outlet(ref)
        if outlet is not None and outlet.never_switch_off and not on:
            reason = "powers the PC" if outlet.host_pc else "is marked critical"
            raise PermissionError(f"{outlet.label} {reason} and cannot be switched off")
        key, switch_id = parse_ref(ref)
        self.device_for(key).set_switch(switch_id, on)
        self._log(f"{ref} -> {'on' if on else 'off'}")

    def apply_profile(self, name: str, restore_windows: bool | None = None) -> ApplyReport:
        """Applique un profil : alimentation puis disposition des fenetres."""
        profile = self.config.profile(name)
        if profile is None:
            raise KeyError(f"Unknown profile: {name}")
        targets = {
            outlet.ref: (profile.wants(outlet.ref) or outlet.never_switch_off)
            for outlet in self.config.outlets
        }
        return self._apply_targets(
            profile_name=profile.name,
            targets=targets,
            layout_profile=profile,
            restore_windows=restore_windows,
        )

    def prepare_for_suspend(self) -> ApplyReport:
        """Coupe les ecrans a la mise en veille, sauf ce qui doit rester.

        Pendant le POST et l'ecran de connexion, rien ne tourne sur le PC pour
        commander les prises : l'ecran de demarrage -- et le concentrateur USB
        qui porte le clavier, s'il est marque critique -- doivent donc rester
        alimentes, sans quoi le prochain demarrage se ferait a l'aveugle et
        sans saisie possible.
        """
        keep_on = set(self.config.shutdown_refs_on())
        targets = {outlet.ref: (outlet.ref in keep_on) for outlet in self.config.outlets}
        # Ce qui est allume maintenant est ce qu'il faudra rendre au reveil.
        # On le note avant de couper : apres, l'information a disparu.
        states = self.read_outlets()
        self.config.settings.resume_refs = [
            ref for ref, state in states.items() if state.output and ref not in keep_on
        ]
        self._remember_current_layout()
        return self._apply_targets(
            profile_name="Suspend",
            targets=targets,
            layout_profile=None,
            restore_windows=False,
            urgent=True,
        )

    def remember_for_resume(self) -> list[str]:
        """Note les prises alimentees, sans rien commander.

        Utile quand la coupure est laissee au script embarque : il faut
        tout de meme savoir quoi rendre au reveil.
        """
        keep_on = set(self.config.shutdown_refs_on())
        states = self.read_outlets()
        refs = [
            ref for ref, state in states.items() if state.output and ref not in keep_on
        ]
        self.config.settings.resume_refs = refs
        self._save()
        return refs

    def resume(self) -> ApplyReport | None:
        """Rend au reveil ce qui etait alimente avant la veille.

        Le dernier profil d'abord, puisque c'est l'intention exprimee. A
        defaut, l'etat releve juste avant la coupure : sans lui, un
        utilisateur qui n'a jamais applique de profil se reveillait devant
        des ecrans eteints, sans que rien ne les rallume.
        """
        name = self.config.settings.last_profile
        if name and self.config.profile(name) is not None:
            self._log(f"Resuming profile '{name}'")
            return self.apply_profile(name)

        refs = [r for r in self.config.settings.resume_refs if self.config.outlet(r)]
        if not refs:
            self._log("Nothing to restore on resume")
            return None
        self._log(f"No profile set, restoring the outlets that were on: {refs}")
        targets = {
            outlet.ref: (outlet.ref in refs or outlet.never_switch_off)
            for outlet in self.config.outlets
        }
        return self._apply_targets(
            profile_name="Resume",
            targets=targets,
            layout_profile=None,
            restore_windows=False,
        )

    # ----------------------------------------------------------- sequencage

    def _apply_targets(
        self,
        profile_name: str,
        targets: dict[str, bool],
        layout_profile: Profile | None,
        restore_windows: bool | None,
        urgent: bool = False,
    ) -> ApplyReport:
        manage_layout = (
            self.config.settings.manage_window_layout
            if restore_windows is None
            else restore_windows
        )
        report = ApplyReport(profile=profile_name)

        with self._lock:
            states = self.read_outlets()
            if not states:
                report.errors.append("No Shelly device is reachable")
                return report

            # 1. Memoriser la disposition avant de toucher a quoi que ce soit.
            if manage_layout and layout_profile is not None:
                self._remember_current_layout()

            # Une prise dont on ne connait pas l'etat n'est pas manoeuvree :
            # son appareil ne repond pas, insister ne ferait qu'attendre.
            to_turn_on = [
                ref for ref, want in targets.items() if want and _is_off(states, ref)
            ]
            to_turn_off = [
                ref for ref, want in targets.items() if not want and _is_on(states, ref)
            ]
            report.unchanged = [
                ref for ref in targets if ref not in to_turn_on and ref not in to_turn_off
            ]
            missing = [ref for ref in targets if ref not in states]
            if missing:
                report.errors.append(f"unreachable: {', '.join(sorted(missing))}")

            # 2. Allumer d'abord, puis laisser Windows decouvrir les ecrans.
            expected_keys = self._expected_monitor_keys(targets)
            report.turned_on = self._switch_many(to_turn_on, True, report, urgent)
            if report.turned_on and not urgent:
                report.displays_waited_s = self._wait_for_displays(expected_keys)

            # 3. Couper ce qui reste a couper.
            report.turned_off = self._switch_many(to_turn_off, False, report, urgent)

            # 4. Rejouer la disposition du profil demande. La pause laisse a
            #    Windows le temps de retirer les ecrans coupes avant qu'on ne
            #    replace les fenetres -- inutile s'il n'y a rien a replacer.
            will_restore = (
                manage_layout and layout_profile is not None and bool(layout_profile.layout)
            )
            if report.turned_off and will_restore:
                time.sleep(DISPLAY_GRACE_S)
            if will_restore:
                assert layout_profile is not None  # garanti par will_restore
                result = layout.restore(layout.deserialize(layout_profile.layout))
                report.windows_restored = result.restored
                report.windows_unmatched = len(result.unmatched)
                self._log(f"Layout: {result.summary()}")

            if layout_profile is not None:
                self.config.settings.last_profile = layout_profile.name
                # Le script embarque doit savoir quoi rallumer au prochain
                # demarrage du PC : c'est le seul moment ou l'application
                # peut le lui dire.
                sensing.publish_profile(self, self.config, layout_profile.name)
            self._save()

        self._log(report.summary())
        return report

    def _switch_many(
        self, refs: list[str], on: bool, report: ApplyReport, urgent: bool = False
    ) -> list[str]:
        """Manoeuvre une serie de prises, en notant les echecs sans tout stopper.

        En mode urgent (mise en veille, arret) on enchaine sans pause : Windows
        ne laisse que quelques instants avant de suspendre le processus.
        """
        done: list[str] = []
        delay = 0.0 if urgent else max(0.0, self.config.settings.switch_delay_ms / 1000.0)
        ordered = sorted(refs)
        for index, ref in enumerate(ordered):
            key, switch_id = parse_ref(ref)
            # Deuxieme verrou, avant meme d'appeler le client : une prise
            # protegee n'a rien a faire dans cette liste, et si elle y est
            # c'est qu'un appelant l'a mal construite.
            outlet = self.config.outlet(ref)
            if not on and outlet is not None and outlet.never_switch_off:
                message = f"{ref}: protected, refused ({outlet.label})"
                report.errors.append(message)
                self._log("REFUSED " + message)
                continue
            try:
                self.device_for(key).set_switch(switch_id, on)
                done.append(ref)
            except ProtectedOutlet as exc:
                report.errors.append(f"{ref}: {exc}")
                self._log(f"REFUSED {ref}: {exc}")
            except AuthenticationFailed as exc:
                self.auth_failures[key] = str(exc)
                report.errors.append(f"{ref}: {exc}")
            except (NotConnected, ShellyUnreachable, ShellyError) as exc:
                report.errors.append(f"{ref}: {exc}")
            if delay and index < len(ordered) - 1:
                time.sleep(delay)
        return done

    def _expected_monitor_keys(self, targets: dict[str, bool]) -> set[str]:
        """Ecrans qui devraient etre presents une fois le profil applique."""
        keys: set[str] = set()
        for ref, want in targets.items():
            outlet = self.config.outlet(ref)
            if want and outlet is not None and outlet.monitor_key:
                keys.add(outlet.monitor_key)
        return keys

    def _wait_for_displays(self, expected_keys: set[str]) -> float:
        """Attend que Windows ait pris en compte les ecrans rallumes.

        Si l'association prise/ecran n'est pas encore faite, on ne sait pas
        quoi attendre precisement : on se contente alors d'attendre que la
        liste des ecrans cesse de bouger.
        """
        timeout = self.config.settings.display_settle_timeout_s
        started = time.monotonic()
        previous: set[str] = monitors.monitor_keys()
        stable_since = started

        while time.monotonic() - started < timeout:
            time.sleep(POLL_INTERVAL_S)
            present = monitors.monitor_keys()
            if expected_keys and expected_keys.issubset(present):
                time.sleep(DISPLAY_GRACE_S)  # laisser la dalle finir de s'initialiser
                return time.monotonic() - started
            if present != previous:
                previous = present
                stable_since = time.monotonic()
            elif not expected_keys and time.monotonic() - stable_since > DISPLAY_GRACE_S:
                # Rien de precis a attendre et plus rien ne bouge : on continue.
                return time.monotonic() - started

        waited = time.monotonic() - started
        if expected_keys:
            missing = expected_keys - monitors.monitor_keys()
            if missing:
                self._log(f"Displays still missing after {waited:.1f}s: {sorted(missing)}")
        return waited

    # -------------------------------------------------------------- layout

    def _remember_current_layout(self) -> None:
        """Enregistre la disposition actuelle dans le profil en cours."""
        current_name = self.config.settings.last_profile
        profile = self.config.profile(current_name) if current_name else None
        if profile is None:
            return
        profile.layout = layout.serialize(layout.capture())
        self._log(f"Layout of '{profile.name}' memorised ({len(profile.layout)} window(s))")

    def capture_layout(self, profile_name: str) -> int:
        """Memorise explicitement la disposition actuelle dans un profil."""
        profile = self.config.profile(profile_name)
        if profile is None:
            raise KeyError(f"Unknown profile: {profile_name}")
        profile.layout = layout.serialize(layout.capture())
        self._save()
        self._log(f"Layout of '{profile.name}' captured ({len(profile.layout)} window(s))")
        return len(profile.layout)

    def clear_layout(self, profile_name: str) -> None:
        profile = self.config.profile(profile_name)
        if profile is not None:
            profile.layout = []
            self._save()

    # ----------------------------------------------------------------- io

    def _save(self) -> None:
        try:
            self.config.save()
        except OSError as exc:
            self._log(f"Cannot save configuration: {exc}")


def _is_on(states: dict[str, SwitchState], ref: str) -> bool:
    state = states.get(ref)
    return bool(state and state.output)


def _is_off(states: dict[str, SwitchState], ref: str) -> bool:
    """Faux si l'etat est inconnu : on ne commande pas a l'aveugle."""
    state = states.get(ref)
    return bool(state and not state.output)
