"""Application : icone de notification, menu, et reaction aux evenements.

Le thread principal ne fait qu'une chose : pomper les messages Windows. Tout
ce qui parle au reseau part dans un thread de travail, sans quoi le menu se
figerait a chaque appel aux appareils.

Une exception assumee : la mise en veille et l'arret. Windows attend la
reponse du programme avant de suspendre la machine, donc la coupure s'y fait
de facon synchrone -- et sans temporisation, d'ou le mode urgent du
controleur.
"""

from __future__ import annotations

import os
import threading
import time

from . import config as config_module
from . import logging_setup
from . import single_instance
from .i18n import set_language, t
from .config import AppConfig
from .controller import ApplyReport, NotConnected, ScreenController
from .device import SwitchState
from .win import icon as icon_module
from .win import monitors
from .win.shell import WM_SHOW_SETTINGS, MenuItem, TrayWindow

from . import __version__

APP_NAME = "Shelly Screens"
REFRESH_INTERVAL_MS = 5000


class Application:
    """Assemble le controleur, l'icone et les evenements systeme."""

    def __init__(self, app_config: AppConfig) -> None:
        self.config = app_config
        self.controller = ScreenController(app_config, log=self.log)
        self.tray = TrayWindow(
            tooltip=APP_NAME,
            on_suspend=self._on_suspend,
            on_resume=self._on_resume,
            on_shutdown=self._on_shutdown,
            on_display_change=self._on_display_change,
            on_tick=self._on_tick,
            on_activate=self._on_activate,
            build_menu=self._build_menu,
            tick_interval_ms=REFRESH_INTERVAL_MS,
        )
        # Le journal s'installe ici s'il ne l'a pas deja ete : un logger sans
        # gestionnaire avalerait tout en silence, precisement ce qu'on veut
        # eviter quand il n'y a pas de console.
        self._logger = logging_setup.setup()
        self.states: dict[str, SwitchState] = {}
        self.online = False
        self.busy = ""  # intitule de l'operation en cours, vide sinon
        self._busy_lock = threading.Lock()
        self._refreshing = False
        # Appareils dont le refus d'authentification a deja ete signale :
        # le rafraichissement est periodique, une bulle toutes les cinq
        # secondes serait insupportable.
        self._auth_warned: set[str] = set()

    # ------------------------------------------------------------------ log

    def log(self, message: str) -> None:
        """Trace une action. Sous pythonw il n'y a pas de console : tout part
        dans le fichier journal, seul temoin de ce que fait l'application."""
        self._logger.info(message)

    # -------------------------------------------------------------- demarrage

    def start(self) -> None:
        self.log(f"{APP_NAME} {__version__} starting")
        self.tray.create()
        self._update_icon()
        # La premiere connexion peut demander un balayage reseau : en tache de
        # fond, pour que l'icone apparaisse tout de suite.
        threading.Thread(target=self._initial_connect, daemon=True).start()
        self.tray.run()
        self.log(f"{APP_NAME} stopped")

    def _initial_connect(self) -> None:
        try:
            if not self.config.devices:
                self.log("No device configured yet - open Settings to add one")
                self._update_icon()
                return
            self.controller.connect_all()
            # Les sorties protegees sont posees a la connexion ; on les
            # repasse ici au cas ou la configuration aurait change entre
            # deux lancements.
            self.controller.refresh_protection()
            self._refresh_states()
            # Sans profil memorise, le script embarque ne rallumerait que
            # l'ecran de demarrage : on lui laisse au moins l'etat courant.
            from . import sensing

            changed = sensing.sync_installed(self.controller, self.config)
            if changed:
                self.log(f"On-device script {changed}")
            published = sensing.ensure_published(self.controller, self.config)
            if published:
                self.log(f"Published outlets for the on-device script: {published}")
            if self.config.settings.apply_profile_on_start:
                name = self.config.settings.last_profile
                if name and self.config.profile(name):
                    self.log(f"Applying profile '{name}' at startup")
                    self._apply_profile_sync(name)
        except Exception as exc:  # noqa: BLE001 - un demarrage rate ne doit pas tuer l'appli
            self.log(f"Startup error: {exc}")

    def stop(self) -> None:
        self.tray.stop()

    # ------------------------------------------------------------------ etat

    def _refresh_states(self) -> None:
        """Relit l'etat des prises et met l'icone a jour."""
        try:
            self.states = self.controller.read_outlets()
            self.online = bool(self.states)
        except (NotConnected, OSError) as exc:
            self.online = False
            self.log(f"Refresh failed: {exc}")
        self._warn_about_auth_failures()
        self._update_icon()

    def _warn_about_auth_failures(self) -> None:
        """Previent une seule fois par appareil qui refuse le mot de passe."""
        failures = set(self.controller.auth_failures)
        for key in sorted(failures - self._auth_warned):
            self.log(f"Device '{key}' refuses the stored password")
            self.tray.notify(
                APP_NAME,
                f"{key}: wrong or missing password. Open Settings > Devices "
                "to fix it, or reset the device with its buttons.",
            )
        # Un appareil redevenu accessible pourra reavertir plus tard.
        self._auth_warned = failures

    def _outlet_states(self) -> list[bool]:
        """Etat des prises, dans l'ordre de la configuration."""
        return [
            bool(self.states.get(outlet.ref) and self.states[outlet.ref].output)
            for outlet in self.config.outlets
        ]

    def _update_icon(self) -> None:
        """Repose l'icone : le visuel de l'application, pastille d'etat comprise.

        Le decompte des prises n'est plus dessine mais dit par l'infobulle :
        a seize pixels de cote, une pastille se lit, un decompte non.
        """
        status = icon_module.status_for(
            self._outlet_states(),
            online=self.online,
            complete=self.controller.fully_connected
            and not self.controller.auth_failures,
        )
        path = icon_module.write_ico(status)
        self.tray.set_icon(str(path), self._tooltip())

    def _tooltip(self) -> str:
        if not self.config.devices:
            return f"{APP_NAME} - no device configured"
        if not self.online:
            return f"{APP_NAME} - no device reachable"
        states = self._outlet_states()
        total = sum(state.apower for state in self.states.values())
        profile = self.config.settings.last_profile or "no profile"
        suffix = ""
        if not self.controller.fully_connected:
            missing = len(self.config.devices) - len(self.controller.online_keys)
            suffix = f" - {missing} device(s) offline"
        return (
            f"{APP_NAME} - {profile} - {sum(states)}/{len(states)} on "
            f"- {total:.0f} W{suffix}"
        )

    # --------------------------------------------------------------- actions

    def _run_async(self, label: str, function) -> None:
        """Lance une operation en tache de fond, une seule a la fois."""
        with self._busy_lock:
            if self.busy:
                self.log(f"Ignored '{label}': '{self.busy}' still running")
                return
            self.busy = label

        def worker() -> None:
            try:
                function()
            except Exception as exc:  # noqa: BLE001 - remonter sans tuer le thread
                self.log(f"{label} failed: {exc}")
                self.tray.notify(APP_NAME, f"{label} failed: {exc}")
            finally:
                with self._busy_lock:
                    self.busy = ""
                self._refresh_states()

        threading.Thread(target=worker, name=label, daemon=True).start()

    def apply_profile(self, name: str) -> None:
        self._run_async(f"Apply '{name}'", lambda: self._apply_profile_sync(name))

    def _apply_profile_sync(self, name: str) -> ApplyReport:
        report = self.controller.apply_profile(name)
        if report.errors:
            self.tray.notify(APP_NAME, "; ".join(report.errors[:2]))
        return report

    def toggle_outlet(self, ref: str) -> None:
        state = self.states.get(ref)
        target = not (state and state.output)
        outlet = self.config.outlet(ref)
        label = f"{outlet.label if outlet else ref} {'on' if target else 'off'}"
        self._run_async(label, lambda: self.controller.set_outlet(ref, target))

    def capture_layout(self, profile_name: str) -> None:
        count = self.controller.capture_layout(profile_name)
        self.tray.notify(APP_NAME, f"{count} window(s) memorised for '{profile_name}'")

    def refresh_now(self) -> None:
        self._run_async("Refresh", lambda: None)

    def reconnect(self) -> None:
        self._run_async("Reconnect", lambda: self.controller.connect_all(allow_scan=True))

    # ------------------------------------------------------------------ menu

    def _build_menu(self) -> list[MenuItem]:
        items: list[MenuItem] = []

        if not self.config.devices:
            items.append(MenuItem.info(t("No device configured")))
            items.append(MenuItem(t("Add a device..."), action=self._open_settings))
            items.append(MenuItem.sep())
            items.append(MenuItem(t("Quit"), action=self.stop))
            return items

        if self.online:
            states = self._outlet_states()
            total = sum(state.apower for state in self.states.values())
            items.append(
                MenuItem.info(f"{sum(states)}/{len(states)} outlets on - {total:.0f} W")
            )
            if not self.controller.fully_connected:
                offline = [
                    d.key for d in self.config.devices if d.key not in self.controller.online_keys
                ]
                items.append(MenuItem.info(f"Offline: {', '.join(offline)}"))
                items.append(MenuItem(t("Reconnect"), action=self.reconnect))
            if self.controller.auth_failures:
                refused = ", ".join(sorted(self.controller.auth_failures))
                items.append(MenuItem.info(f"Password refused: {refused}"))
                items.append(MenuItem(t("Fix password..."), action=self._open_settings))
        else:
            items.append(MenuItem.info(t("No device reachable")))
            items.append(MenuItem(t("Reconnect"), action=self.reconnect))

        frozen = self.controller.frozen_meters()
        if frozen:
            noms = ", ".join(sorted(
                self.config.outlet(r).label
                for r in frozen if self.config.outlet(r) is not None
            ))
            items.append(
                MenuItem.info(t("Frozen measurement: {outlets}", outlets=noms))
            )
            items.append(
                MenuItem(t("Restart the device"), action=self.restart_frozen)
            )

        if self.script_out_of_date:
            # Le plus visible des emplacements : le menu s'ouvre d'un
            # clic droit, sans savoir ou chercher. Un ecart entre les
            # reglages et le script pose est indevinable autrement.
            items.append(MenuItem.info(t("On-device script is out of date")))
            items.append(
                MenuItem(t("Update it now"), action=self.update_script)
            )

        if self.busy:
            items.append(MenuItem.info(f"Busy: {self.busy}"))

        items.append(MenuItem.sep())
        items.extend(self._profile_items())
        items.append(MenuItem.sep())
        items.append(MenuItem(t("Outlets"), submenu=self._outlet_items()))
        items.append(MenuItem(t("Layout"), submenu=self._layout_items()))
        items.append(MenuItem.sep())
        items.append(MenuItem(t("Settings..."), action=self._open_settings))
        items.append(MenuItem(t("Refresh"), action=self.refresh_now))
        items.append(MenuItem(t("Open log file"), action=self.open_log))
        items.append(MenuItem(t("Quit"), action=self.stop))
        return items

    @property
    def script_out_of_date(self) -> bool:
        """Les reglages ont-ils change depuis la derniere installation ?

        Calcule localement, sans reseau : on peut donc le demander a
        chaque construction du menu sans rien couter a l'appareil.
        """
        from . import sensing

        try:
            return sensing.needs_update(self.config)
        except Exception:  # noqa: BLE001 - un doute ne doit rien casser
            return False

    def update_script(self) -> None:
        """Repose le script embarque avec les reglages courants."""
        from . import sensing

        def worker() -> None:
            status = sensing.install(self.controller, self.config)
            # `install` retient la nouvelle empreinte : il faut l'ecrire,
            # sans quoi l'avertissement reapparaitrait au prochain demarrage.
            self.config.save()
            self.log(f"On-device script updated: {status.summary()}")

        self._run_async("Updating script", worker)

    def restart_frozen(self) -> None:
        """Redemarre les appareils dont une voie de mesure est gelee.

        C'est le seul remede connu a ce defaut du firmware, et il est sans
        danger : relais bistables, et `initial_state` ramene la prise du PC
        sous tension quoi qu'il arrive.
        """
        cles = {
            ref.split(":")[0] for ref in self.controller.frozen_meters()
        }

        def worker() -> None:
            for cle in sorted(cles):
                self.controller.reboot_device(cle)
            time.sleep(15.0)
            self.controller.connect_all(allow_scan=False)

        self._run_async("Restarting device", worker)

    def open_log(self) -> None:
        """Ouvre le journal dans l'editeur associe.

        Sans console, c'est le seul moyen de voir ce que fait l'application.
        """
        path = logging_setup.log_path()
        try:
            os.startfile(str(path))  # noqa: S606 - ouverture par l'editeur du systeme
        except OSError as exc:
            self.log(f"Cannot open the log file ({path}): {exc}")

    def _profile_items(self) -> list[MenuItem]:
        profiles = self.config.sorted_profiles()
        if not profiles:
            return [MenuItem.info("No profile configured")]
        current = self.config.settings.last_profile
        items: list[MenuItem] = []
        for profile in profiles:
            count = len(profile.outlets_on)
            detail = f"{count} outlet(s)" if count else "all off"
            items.append(
                MenuItem(
                    label=f"{profile.name}  ({detail})",
                    action=lambda name=profile.name: self.apply_profile(name),
                    checked=profile.name == current,
                    enabled=self.online and not self.busy,
                )
            )
        return items

    def _outlet_items(self) -> list[MenuItem]:
        """Prises, regroupees par appareil quand il y en a plusieurs."""
        if len(self.config.devices) <= 1:
            return self._outlet_entries(self.config.outlets)
        items: list[MenuItem] = []
        for device in self.config.devices:
            outlets = self.config.outlets_of(device.key)
            if not outlets:
                continue
            reachable = device.key in self.controller.online_keys
            label = device.label if reachable else f"{device.label} (offline)"
            items.append(MenuItem(label, submenu=self._outlet_entries(outlets)))
        return items or [MenuItem.info("No outlet")]

    def _outlet_entries(self, outlets: list) -> list[MenuItem]:
        items: list[MenuItem] = []
        for outlet in outlets:
            state = self.states.get(outlet.ref)
            power = f" - {state.apower:.0f} W" if state and state.output else ""
            tags = []
            if outlet.host_pc:
                tags.append("PC")
            if outlet.critical:
                tags.append("critical")
            if outlet.boot_screen:
                tags.append("boot")
            suffix = f" [{', '.join(tags)}]" if tags else ""
            items.append(
                MenuItem(
                    label=f"{outlet.label}{suffix}{power}",
                    action=lambda r=outlet.ref: self.toggle_outlet(r),
                    checked=bool(state and state.output),
                    enabled=(
                        state is not None and not self.busy and not outlet.never_switch_off
                    ),
                )
            )
        return items

    def _layout_items(self) -> list[MenuItem]:
        current = self.config.settings.last_profile
        items: list[MenuItem] = []
        profile = self.config.profile(current) if current else None
        if profile is not None:
            items.append(
                MenuItem(
                    f"Save window layout to '{profile.name}'",
                    action=lambda name=profile.name: self.capture_layout(name),
                )
            )
            items.append(MenuItem.info(f"{len(profile.layout)} window(s) memorised"))
        else:
            items.append(MenuItem.info("No active profile"))
        items.append(MenuItem.sep())
        for monitor in monitors.list_monitors():
            items.append(MenuItem.info(monitor.describe()))
        return items

    # ------------------------------------------------------- evenements systeme

    def _on_tick(self) -> None:
        """Rafraichissement periodique, sans empiler les requetes."""
        if self._refreshing or self.busy or not self.config.devices:
            return
        self._refreshing = True

        def worker() -> None:
            try:
                self._refresh_states()
            finally:
                self._refreshing = False

        threading.Thread(target=worker, daemon=True).start()

    def _on_activate(self) -> None:
        """Clic gauche sur l'icone : ouvrir la fenetre de reglages."""
        self._open_settings()

    def _on_display_change(self) -> None:
        self.log("Display configuration changed")

    def _on_suspend(self) -> None:
        """Mise en veille : couper, mais garder ce qui doit rester allume.

        Synchrone a dessein -- Windows suspend le processus des que l'on rend
        la main, et une coupure lancee en tache de fond n'aurait pas le temps
        d'aboutir.
        """
        if not self.config.settings.power_off_on_suspend or not self.config.devices:
            return
        if self.config.sensing.enabled:
            # Le script embarque coupe deja, apres son delai de
            # confirmation. Couper ici en plus ferait claquer les relais a
            # l'instant meme de la mise en veille, sans rien apporter : on
            # se contente de noter ce qu'il faudra rendre au reveil.
            try:
                self.controller.remember_for_resume()
            except Exception as exc:  # noqa: BLE001
                self.log(f"Could not record the pre-sleep state: {exc}")
            self.log("Suspending: leaving the outlets to the on-device script")
            return
        self.log("Suspending: switching screens off")
        try:
            report = self.controller.prepare_for_suspend()
            self.log(report.summary())
        except Exception as exc:  # noqa: BLE001 - ne jamais bloquer la veille
            self.log(f"Suspend handling failed: {exc}")

    def _on_resume(self) -> None:
        """Reveil : reappliquer le dernier profil."""
        if not self.config.settings.restore_on_resume:
            self._on_tick()
            return
        self.log("Resuming")

        def worker() -> None:
            # Le reseau met un instant a revenir apres le reveil : sans cette
            # pause, les premieres requetes echoueraient a coup sur.
            time.sleep(3.0)
            self.controller.connect_all(allow_scan=False)
            try:
                self.controller.resume()
            except Exception as exc:  # noqa: BLE001
                self.log(f"Resume failed: {exc}")
            self._refresh_states()

        self._run_async("Resume", worker)

    def _on_shutdown(self) -> None:
        """Arret ou redemarrage : meme traitement que la veille."""
        self._on_suspend()

    # ------------------------------------------------------------- reglages

    def _open_settings(self) -> None:
        from .ui.settings import open_settings

        open_settings(self)


def main(verbose: bool = False) -> int:
    # Le journal se met en place avant tout le reste : sans lui, une erreur
    # au chargement de la configuration disparaitrait sans laisser de trace.
    logger = logging_setup.setup(verbose=verbose)

    # Une seule instance : deux programmes qui ecrivent le meme fichier de
    # configuration se marchent dessus, et le dernier a enregistrer efface
    # le travail de l'autre.
    if not single_instance.acquire():
        shown = single_instance.wake_existing(
            TrayWindow.CLASS_NAME, WM_SHOW_SETTINGS
        )
        logger.info(
            "Already running; %s",
            "asked the running instance to show its settings"
            if shown
            else "could not find its window",
        )
        return 0

    app_config = config_module.load()
    set_language(app_config.settings.language)
    application = Application(app_config)
    try:
        application.start()
    finally:
        single_instance.release()
    return 0
