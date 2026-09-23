"""Historique de consommation par prise, conserve sur le disque.

Chaque prise suivie a son fichier, rempli par ajouts successifs et elague
de ses donnees les plus anciennes : une file dont la profondeur se regle en
jours, une semaine ou un mois selon ce qu'on veut pouvoir relire.

On n'enregistre pas chaque releve mais seulement ce qui apprend quelque
chose -- le principe des ticks. Un point s'ecrit quand la puissance bouge
nettement, et au moins une fois par minute pour que la courbe garde un
ancrage. Un PC au repos ou en veille coute donc quelques points par heure,
et un mois tient en quelques centaines de kilo-octets.

Deux sources alimentent un meme fichier. L'application releve la prise
toutes les cinq secondes tant qu'elle tourne -- c'est-a-dire tant que le PC
tourne. Pendant la veille ou l'arret, c'est le releveur embarque dans la
multiprise qui continue de mesurer : a la sortie de veille ou au lancement,
ses ticks comblent le trou. Seule la prise du PC beneficie de ce releveur ;
les autres prises n'auront que la partie vue par l'application.

Le fichier porte le nom de l'adresse MAC de la multiprise et du numero de
sortie, et non la cle de l'appareil : ces cles changent au gre des
renommages, la MAC jamais, et l'historique doit survivre aux deux.
"""

from __future__ import annotations

import os
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .config import AppConfig, OutletConfig
    from .controller import ScreenController
    from .device import SwitchState

# Un enregistrement : instant Unix en secondes, puissance en watts, origine.
# Neuf octets, sans en-tete : le fichier se lit par simple decoupage.
RECORD = struct.Struct("<IfB")

SOURCE_LIVE = 0  # releve par l'application
SOURCE_PROBE = 1  # recupere aupres du releveur embarque

# Au-dela de cet ecart, deux points ne sont plus relies : rien n'a ete
# mesure entre eux. L'application pose un ancrage chaque minute, le
# releveur embarque chaque quart d'heure ; chacun a donc sa tolerance.
MAX_GAP_S = {SOURCE_LIVE: 180.0, SOURCE_PROBE: 20 * 60.0}

ANCHOR_S = 60.0  # un point au moins par minute, meme si rien ne bouge
MIN_STEP_W = 1.0  # en deca, une variation n'est pas un evenement
MIN_STEP_RATIO = 0.03  # ... ni au-dessous de 3 % de la puissance courante
SYNC_EVERY_S = 60.0  # frequence des ecritures forcees jusqu'au disque
TRIM_EVERY_S = 3600.0  # frequence de l'elagage
DEFAULT_DAYS = 30


@dataclass(frozen=True)
class Sample:
    """Un point de l'historique : la puissance valable a partir de `t`."""

    t: float
    watts: float
    source: int = SOURCE_LIVE

    @property
    def max_gap(self) -> float:
        return MAX_GAP_S.get(self.source, MAX_GAP_S[SOURCE_LIVE])


def history_dir(config: "AppConfig") -> Path:
    """Dossier des historiques, a cote de la configuration."""
    return Path(config.path).parent / "history"


def outlet_key(config: "AppConfig", outlet: "OutletConfig") -> str:
    """Identifiant stable d'une prise : MAC de l'appareil et sortie."""
    device = config.device(outlet.device)
    mac = device.mac if device is not None and device.mac else outlet.device
    return f"{mac.upper()}-{outlet.switch_id}"


def history_path(config: "AppConfig", outlet: "OutletConfig") -> Path:
    return history_dir(config) / f"{outlet_key(config, outlet)}.bin"


def read_samples(path: Path, offset: int = 0) -> tuple[list[Sample], int, bool]:
    """Points a partir d'un decalage, le decalage suivant, et si le fichier
    a ete raccourci depuis -- un elagage -- auquel cas tout a ete relu.

    Un enregistrement incomplet en fin de fichier, en cours d'ecriture, est
    laisse pour la lecture suivante.
    """
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return [], 0, offset > 0
    reset = offset > size
    if reset:
        offset = 0
    with open(path, "rb") as handle:
        handle.seek(offset)
        data = handle.read()
    usable = len(data) - len(data) % RECORD.size
    samples = [
        Sample(float(t), float(w), int(s))
        for t, w, s in RECORD.iter_unpack(data[:usable])
    ]
    return samples, offset + usable, reset


def last_sample(path: Path) -> Sample | None:
    """Dernier point du fichier, sans le relire en entier."""
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return None
    usable = size - size % RECORD.size
    if usable == 0:
        return None
    with open(path, "rb") as handle:
        handle.seek(usable - RECORD.size)
        t, w, s = RECORD.unpack(handle.read(RECORD.size))
    return Sample(float(t), float(w), int(s))


class HistoryRecorder:
    """Alimente les historiques a partir des releves de l'application.

    Appele apres chaque lecture reussie des prises, il ne coute aucune
    requete supplementaire a la multiprise : il reutilise ce qui vient
    d'etre lu. Seule la recuperation des donnees de veille interroge
    l'appareil, une fois au lancement et une fois a chaque reveil.
    """

    def __init__(
        self,
        config: "AppConfig",
        controller: "ScreenController",
        log: Callable[[str], None],
    ) -> None:
        self.config = config
        self.controller = controller
        self._log = log
        self._lock = threading.Lock()
        self._last: dict[str, Sample] = {}
        self._recovery_pending = True
        self._last_sync = 0.0
        self._last_trim = 0.0

    # ------------------------------------------------------------ reglages

    @property
    def keep_seconds(self) -> float:
        days = getattr(self.config.settings, "history_days", DEFAULT_DAYS)
        return max(1, int(days)) * 86400.0

    def tracked(self) -> list["OutletConfig"]:
        """Prises suivies. Pour l'instant, la seule prise du PC."""
        outlet = self.config.host_pc_outlet()
        return [outlet] if outlet is not None else []

    # ------------------------------------------------------------ evenements

    def request_recovery(self) -> None:
        """Signale un trou a combler : lancement, ou sortie de veille.

        La recuperation elle-meme attend la prochaine lecture reussie :
        elle a lieu dans le meme fil, avant le premier releve direct, et
        les points s'ecrivent donc dans l'ordre chronologique.
        """
        self._recovery_pending = True

    def feed(self, states: dict[str, "SwitchState"]) -> None:
        """Retient ce qui merite de l'etre dans les releves qu'on vient de lire."""
        if not states:
            return
        now = time.time()
        with self._lock:
            if self._recovery_pending:
                self._recovery_pending = False
                self._recover(now)
            for outlet in self.tracked():
                state = states.get(outlet.ref)
                if state is None:
                    continue
                watts = float(state.apower) if state.output else 0.0
                self._consider(outlet, Sample(now, watts, SOURCE_LIVE))
            if now - self._last_trim >= TRIM_EVERY_S:
                self._last_trim = now
                self._trim(now)

    def sync(self) -> None:
        """Force l'ecriture jusqu'au disque : avant une veille ou un arret."""
        with self._lock:
            for outlet in self.tracked():
                path = history_path(self.config, outlet)
                if not path.exists():
                    continue
                try:
                    with open(path, "ab") as handle:
                        handle.flush()
                        os.fsync(handle.fileno())
                except OSError:
                    pass
            self._last_sync = time.time()

    # ------------------------------------------------------------ interne

    def _previous(self, outlet: "OutletConfig") -> Sample | None:
        key = outlet_key(self.config, outlet)
        if key not in self._last:
            previous = last_sample(history_path(self.config, outlet))
            if previous is not None:
                self._last[key] = previous
        return self._last.get(key)

    def _consider(self, outlet: "OutletConfig", sample: Sample) -> None:
        """Ecrit le point s'il apprend quelque chose, l'ignore sinon."""
        previous = self._previous(outlet)
        if previous is not None:
            quiet = sample.t - previous.t < ANCHOR_S
            step = max(MIN_STEP_W, MIN_STEP_RATIO * abs(previous.watts))
            negligible = abs(sample.watts - previous.watts) < step
            if quiet and negligible:
                return
        self._write(outlet, [sample])

    def _write(self, outlet: "OutletConfig", samples: list[Sample]) -> None:
        path = history_path(self.config, outlet)
        path.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        # Ouvert et referme a chaque ecriture : un fichier garde ouvert ne
        # pourrait pas etre remplace par l'elagage, Windows le refuse.
        with open(path, "ab") as handle:
            for sample in samples:
                handle.write(
                    RECORD.pack(int(sample.t), float(sample.watts), int(sample.source))
                )
            handle.flush()
            if now - self._last_sync >= SYNC_EVERY_S:
                os.fsync(handle.fileno())
                self._last_sync = now
        self._last[outlet_key(self.config, outlet)] = samples[-1]

    def _recover(self, now: float) -> None:
        """Comble avec les ticks du releveur le temps ou l'application dormait."""
        outlet = self.config.host_pc_outlet()
        if outlet is None or not self.config.sensing.enabled:
            return
        from . import sensing

        try:
            timeline = sensing.read_probe_timeline(self.controller, self.config)
        except Exception as exc:  # noqa: BLE001 - l'historique ne doit rien casser
            self._log(f"History: sleep data unavailable ({exc})")
            return
        previous = self._previous(outlet)
        start = previous.t + 1 if previous else now - self.keep_seconds
        recovered = [
            Sample(t, w, SOURCE_PROBE) for t, w in timeline if start < t < now - 1
        ]
        if recovered:
            self._write(outlet, recovered)
            self._log(f"History: {len(recovered)} point(s) recovered from the probe")

    def _trim(self, now: float) -> None:
        """Retire ce qui depasse la profondeur choisie."""
        limit = now - self.keep_seconds
        for outlet in self.tracked():
            path = history_path(self.config, outlet)
            samples, _, _ = read_samples(path)
            kept = [s for s in samples if s.t >= limit]
            if len(kept) == len(samples):
                continue
            temporary = path.with_suffix(".tmp")
            with open(temporary, "wb") as handle:
                for s in kept:
                    handle.write(RECORD.pack(int(s.t), float(s.watts), int(s.source)))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            self._log(f"History: {len(samples) - len(kept)} old point(s) trimmed")
