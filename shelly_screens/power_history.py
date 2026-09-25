"""Historique de consommation par prise, conserve dans une base SQLite.

Chaque prise suivie alimente une meme base, elaguee de ses donnees les plus
anciennes : une file dont la profondeur se regle en jours, une semaine ou un
mois selon ce qu'on veut pouvoir relire.

On n'enregistre pas chaque releve mais seulement ce qui apprend quelque
chose -- le principe des ticks. Un point s'ecrit quand la puissance bouge
nettement, et au moins une fois par minute pour que la courbe garde un
ancrage. Un PC au repos ou en veille coute donc quelques points par heure.

Toutes les prises configurees sont suivies, chacune sous son nom. Deux
sources alimentent la base. L'application releve les prises toutes les cinq
secondes tant qu'elle tourne -- c'est-a-dire tant que le PC tourne.
Pendant la veille ou l'arret, c'est le releveur embarque dans la multiprise
qui continue de mesurer : a la sortie de veille ou au lancement, ses ticks
comblent le trou. Seule la prise du PC beneficie de ce releveur. Les autres
n'ont que les releves directs : pendant la veille, les ecrans sont coupes
et seuls quelques concentrateurs USB consomment, ce qui ne vaut pas d'user
la memoire flash des multiprises a le noter.

Pourquoi SQLite. Un premier format, binaire et maison, etait compact mais
muet : ni signature, ni version, ni description, et illisible sans le code
qui l'avait ecrit. SQLite est un format normalise, inclus dans Python, que
lisent aussi bien un tableur, DB Browser for SQLite, Grafana ou n'importe
quel langage. La base se decrit elle-meme -- une table `info` dit ce que
contient chaque colonne, et une vue `sample_readable` donne l'heure locale
en clair --, et elle survit aux coupures grace a son journal.

Une prise y est designee par l'adresse MAC de la multiprise et son numero
de sortie, et non par la cle de l'appareil : ces cles changent au gre des
renommages, la MAC jamais, et l'historique doit survivre aux deux.
"""

from __future__ import annotations

import sqlite3
import struct
import threading
import time
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .config import AppConfig, OutletConfig
    from .controller import ScreenController
    from .device import SwitchState

DB_NAME = "power_history.sqlite3"
SCHEMA_VERSION = 1

SOURCE_LIVE = 0  # releve par l'application
SOURCE_PROBE = 1  # recupere aupres du releveur embarque

# Au-dela de cet ecart, deux points ne sont plus relies : rien n'a ete
# mesure entre eux. L'application pose un ancrage chaque minute, le
# releveur embarque chaque quart d'heure ; chacun a donc sa tolerance.
MAX_GAP_S = {SOURCE_LIVE: 180.0, SOURCE_PROBE: 20 * 60.0}

ANCHOR_S = 60.0  # un point au moins par minute, meme si rien ne bouge
MIN_STEP_W = 1.0  # en deca, une variation n'est pas un evenement
MIN_STEP_RATIO = 0.03  # ... ni au-dessous de 3 % de la puissance courante
TRIM_EVERY_S = 3600.0  # frequence de l'elagage
DEFAULT_DAYS = 30

# Premier format, binaire, lu une derniere fois pour la migration.
LEGACY_RECORD = struct.Struct("<IfB")

SCHEMA = """
CREATE TABLE IF NOT EXISTS info (
    name  TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outlet (
    id    INTEGER PRIMARY KEY,
    key   TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS sample (
    outlet_id INTEGER NOT NULL REFERENCES outlet(id),
    t         REAL    NOT NULL,
    watts     REAL    NOT NULL,
    source    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (outlet_id, t)
) WITHOUT ROWID;
CREATE VIEW IF NOT EXISTS sample_readable AS
    SELECT o.key                                   AS outlet,
           o.label                                 AS label,
           datetime(s.t, 'unixepoch', 'localtime') AS local_time,
           round(s.watts, 1)                       AS watts,
           CASE s.source WHEN 1 THEN 'probe' ELSE 'app' END AS source
    FROM sample AS s
    JOIN outlet AS o ON o.id = s.outlet_id;
"""

# Ce que la base dit d'elle-meme, a qui l'ouvre sans l'application.
INFO = {
    "schema": str(SCHEMA_VERSION),
    "sample.t": "Unix time in seconds, UTC",
    "sample.watts": "active power in watts",
    "sample.source": "0 = read by the application, "
                     "1 = recovered from the on-device probe while the PC slept",
    "outlet.key": "MAC address of the power strip, then the switch number",
    "sample_readable": "the same samples with local time in plain text",
}


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


def db_path(config: "AppConfig") -> Path:
    return history_dir(config) / DB_NAME


def outlet_key(config: "AppConfig", outlet: "OutletConfig") -> str:
    """Identifiant stable d'une prise : MAC de l'appareil et sortie."""
    device = config.device(outlet.device)
    mac = device.mac if device is not None and device.mac else outlet.device
    return f"{mac.upper()}-{outlet.switch_id}"


class HistoryStore:
    """La base : ajout, lecture, elagage.

    `readonly` ouvre une base existante sans jamais y ecrire, pour la
    fenetre de consultation ; elle lit pendant que l'enregistreur ecrit, ce
    que le journal WAL de SQLite permet sans qu'aucun n'attende l'autre.
    """

    def __init__(self, path: Path, readonly: bool = False) -> None:
        self.path = path
        self._ids: dict[str, int] = {}
        self._labels: dict[str, str] = {}
        if readonly:
            self._db = sqlite3.connect(str(path), timeout=10, check_same_thread=False)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        # Une connexion partagee par les fils de releve : c'est le verrou de
        # l'enregistreur qui les met en file, pas SQLite.
        self._db = sqlite3.connect(str(path), timeout=10, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        # Chaque ecriture va jusqu'au disque. Elles sont rares -- quelques
        # unes par minute --, et le PC peut etre debranche a tout instant.
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(SCHEMA)
        with self._db:
            self._db.executemany(
                "INSERT OR REPLACE INTO info(name, value) VALUES (?, ?)",
                INFO.items(),
            )
        self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        self._db.close()

    # ------------------------------------------------------------ prises

    def _find(self, key: str) -> int | None:
        cached = self._ids.get(key)
        if cached is not None:
            return cached
        try:
            row = self._db.execute(
                "SELECT id, label FROM outlet WHERE key = ?", (key,)
            ).fetchone()
        except sqlite3.OperationalError:
            return None  # base encore vide : rien n'a ete enregistre
        if row is not None:
            self._ids[key] = int(row[0])
            self._labels[key] = str(row[1])
        return self._ids.get(key)

    def _outlet_id(self, key: str, label: str) -> int:
        """Identifiant de la prise, creee au besoin, son nom tenu a jour.

        Le nom suit les renommages : c'est lui qu'on lit en ouvrant la base
        sans l'application, et une prise renommee doit s'y retrouver.
        """
        found = self._find(key)
        if found is None:
            with self._db:
                self._db.execute(
                    "INSERT OR IGNORE INTO outlet(key, label) VALUES (?, ?)",
                    (key, label),
                )
            return self._find(key)
        if label and self._labels.get(key) != label:
            with self._db:
                self._db.execute(
                    "UPDATE outlet SET label = ? WHERE id = ?", (label, found)
                )
            self._labels[key] = label
        return found

    def outlets(self) -> list[tuple[str, str]]:
        """Prises presentes dans la base : cle et nom."""
        try:
            return [
                (str(key), str(label))
                for key, label in self._db.execute(
                    "SELECT key, label FROM outlet ORDER BY id"
                )
            ]
        except sqlite3.OperationalError:
            return []

    # ------------------------------------------------------------ points

    def append(self, key: str, label: str, samples: list[Sample]) -> None:
        outlet_id = self._outlet_id(key, label)
        with self._db:
            # Un point deja present -- meme prise, meme instant -- est
            # ignore : une migration rejouee ne cree pas de doublon.
            self._db.executemany(
                "INSERT OR IGNORE INTO sample(outlet_id, t, watts, source) "
                "VALUES (?, ?, ?, ?)",
                [(outlet_id, s.t, s.watts, s.source) for s in samples],
            )

    def append_batch(self, entries: list[tuple[str, str, Sample]]) -> None:
        """Ecrit les points de plusieurs prises en une seule transaction.

        Un releve concerne toutes les prises a la fois : une transaction
        par prise multipliait d'autant les ecritures forcees sur le disque.
        """
        # Les prises nouvelles se creent avant : leur insertion validerait
        # sinon la transaction des points en cours de route.
        rows = [
            (self._outlet_id(key, label), s.t, s.watts, s.source)
            for key, label, s in entries
        ]
        with self._db:
            self._db.executemany(
                "INSERT OR IGNORE INTO sample(outlet_id, t, watts, source) "
                "VALUES (?, ?, ?, ?)",
                rows,
            )

    def last(self, key: str) -> Sample | None:
        outlet_id = self._find(key)
        if outlet_id is None:
            return None
        row = self._db.execute(
            "SELECT t, watts, source FROM sample WHERE outlet_id = ? "
            "ORDER BY t DESC LIMIT 1",
            (outlet_id,),
        ).fetchone()
        return Sample(row[0], row[1], row[2]) if row else None

    def read(
        self, key: str, after: float | None = None, since: float | None = None
    ) -> list[Sample]:
        """Points d'une prise, dans l'ordre du temps.

        `after` ne rend que les points posterieurs -- la lecture des
        nouveautes --, `since` borne le passe a la profondeur voulue.
        """
        outlet_id = self._find(key)
        if outlet_id is None:
            return []
        query = "SELECT t, watts, source FROM sample WHERE outlet_id = ?"
        params: list[float] = [outlet_id]
        if after is not None:
            query += " AND t > ?"
            params.append(after)
        if since is not None:
            query += " AND t >= ?"
            params.append(since)
        query += " ORDER BY t"
        return [Sample(t, w, s) for t, w, s in self._db.execute(query, params)]

    def trim(self, before: float) -> int:
        with self._db:
            cursor = self._db.execute("DELETE FROM sample WHERE t < ?", (before,))
        return cursor.rowcount

    def checkpoint(self) -> None:
        """Verse le journal dans la base : avant une veille ou un arret."""
        self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")


CSV_COLUMNS = ("local_time", "unix_time", "watts", "source", "duration_s")


def export_csv(
    path: Path, samples: list[Sample], end: float,
    delimiter: str = ",", decimal: str = ".",
) -> int:
    """Ecrit des points en CSV ; rend leur nombre.

    Par defaut, le CSV normalise (RFC 4180) : virgule entre les champs,
    point decimal, heure ISO 8601 avec son decalage. C'est ce que lisent
    tous les outils -- mais pas Excel en francais, qui attend des points-
    virgules et des virgules decimales, et entasse tout dans la premiere
    colonne sinon. Les separateurs se passent donc en parametre.

    `duration_s` dit combien de temps chaque valeur a tenu : jusqu'au point
    suivant, ou `end` pour le dernier, sans jamais franchir un trou de
    mesure. L'energie en watt-heures s'en deduit par une seule somme de
    produits, sans avoir a reconstituer la chronologie.
    """
    import csv
    from datetime import datetime

    standard = decimal == "."

    def number(value: float, digits: int) -> str:
        text = f"{value:.{digits}f}"
        return text if standard else text.replace(".", decimal)

    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, delimiter=delimiter)
        writer.writerow(CSV_COLUMNS)
        for index, sample in enumerate(samples):
            following = samples[index + 1].t if index + 1 < len(samples) else end
            duration = max(0.0, min(following, sample.t + sample.max_gap) - sample.t)
            # Un seul arrondi pour les deux colonnes de temps : l'heure ISO
            # tronque, le temps Unix arrondissait, et elles differaient d'une
            # seconde pour le meme instant.
            moment = round(sample.t)
            local = datetime.fromtimestamp(moment).astimezone()
            stamp = (
                local.isoformat(timespec="seconds")
                if standard
                else local.strftime("%Y-%m-%d %H:%M:%S")
            )
            writer.writerow((
                stamp,
                str(moment),
                number(sample.watts, 1),
                "probe" if sample.source == SOURCE_PROBE else "app",
                number(duration, 0),
            ))
    return len(samples)


def open_reader(config: "AppConfig") -> HistoryStore | None:
    """Base ouverte en lecture seule, ou None si rien n'a encore ete ecrit."""
    path = db_path(config)
    if not path.exists():
        return None
    return HistoryStore(path, readonly=True)


def migrate_legacy(
    store: HistoryStore, directory: Path, labels: dict[str, str],
    log: Callable[[str], None],
) -> int:
    """Reprend les fichiers du premier format binaire dans la base.

    Chaque fichier est renomme une fois repris, et non supprime : c'est la
    seule copie des mesures tant qu'on n'a pas verifie la base. La reprise
    est rejouable sans risque -- un point deja present est ignore --, ce qui
    compte : une ancienne version de l'application peut encore avoir ecrit
    un fichier entre deux lancements.
    """
    total = 0
    for legacy in sorted(directory.glob("*.bin")):
        data = legacy.read_bytes()
        usable = len(data) - len(data) % LEGACY_RECORD.size
        samples = [
            Sample(float(t), float(w), int(s))
            for t, w, s in LEGACY_RECORD.iter_unpack(data[:usable])
        ]
        if samples:
            store.append(legacy.stem, labels.get(legacy.stem, ""), samples)
        target = legacy.with_name(legacy.name + ".migrated")
        if target.exists():
            target = legacy.with_name(f"{legacy.name}.migrated-{int(time.time())}")
        legacy.rename(target)
        total += len(samples)
        log(f"History: {len(samples)} point(s) migrated from {legacy.name}")
    return total


class HistoryRecorder:
    """Alimente la base a partir des releves de l'application.

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
        self._store: HistoryStore | None = None
        self._last: dict[str, Sample] = {}
        self._recovery_pending = True
        self._recovery_warned = False
        self._last_trim = 0.0

    # ------------------------------------------------------------ reglages

    @property
    def keep_seconds(self) -> float:
        days = getattr(self.config.settings, "history_days", DEFAULT_DAYS)
        return max(1, int(days)) * 86400.0

    def tracked(self) -> list["OutletConfig"]:
        """Prises suivies : toutes celles de la configuration."""
        return list(self.config.outlets)

    def _labels(self) -> dict[str, str]:
        return {outlet_key(self.config, o): o.label for o in self.config.outlets}

    def _open(self) -> HistoryStore:
        """Ouvre la base a la premiere ecriture, en reprenant l'ancien format."""
        if self._store is None:
            self._store = HistoryStore(db_path(self.config))
            migrate_legacy(
                self._store, history_dir(self.config), self._labels(), self._log
            )
        return self._store

    # ------------------------------------------------------------ evenements

    def request_recovery(self) -> None:
        """Signale un trou a combler : lancement, ou sortie de veille.

        La recuperation elle-meme attend la prochaine lecture reussie :
        elle a lieu dans le meme fil, avant le premier releve direct, et
        les points s'ecrivent donc dans l'ordre chronologique.
        """
        self._recovery_pending = True
        self._recovery_warned = False

    def feed(self, states: dict[str, "SwitchState"]) -> None:
        """Retient ce qui merite de l'etre dans les releves qu'on vient de lire."""
        if not states:
            return
        now = time.time()
        with self._lock:
            self._open()
            # La recuperation attend que l'appareil du PC ait repondu. Au
            # reveil, l'autre multiprise revient souvent la premiere : tenter
            # sur sa seule reponse, c'etait interroger un releveur encore
            # injoignable, et perdre la nuit sans un mot.
            pc = self.config.host_pc_outlet()
            if self._recovery_pending and pc is not None and pc.ref in states:
                self._recovery_pending = not self._recover(now)
            # Une prise muette -- son appareil n'a pas repondu -- laisse un
            # trou, plutot qu'un zero qu'on n'a pas mesure.
            worth = []
            for outlet in self.tracked():
                state = states.get(outlet.ref)
                if state is None:
                    continue
                watts = float(state.apower) if state.output else 0.0
                sample = Sample(now, watts, SOURCE_LIVE)
                if self._worth_keeping(outlet, sample):
                    worth.append((outlet, sample))
            if worth:
                self._write_batch(worth)
            if now - self._last_trim >= TRIM_EVERY_S:
                self._last_trim = now
                removed = self._store.trim(now - self.keep_seconds)
                if removed:
                    self._log(f"History: {removed} old point(s) trimmed")

    def sync(self) -> None:
        """Verse le journal dans la base : avant une veille ou un arret."""
        with self._lock:
            if self._store is not None:
                try:
                    self._store.checkpoint()
                except sqlite3.Error:
                    pass

    # ------------------------------------------------------------ interne

    def _previous(self, outlet: "OutletConfig") -> Sample | None:
        key = outlet_key(self.config, outlet)
        if key not in self._last:
            previous = self._store.last(key)
            if previous is not None:
                self._last[key] = previous
        return self._last.get(key)

    def _worth_keeping(self, outlet: "OutletConfig", sample: Sample) -> bool:
        """Vrai si le point apprend quelque chose : un ecart, ou l'ancrage de la minute."""
        previous = self._previous(outlet)
        if previous is None:
            return True
        quiet = sample.t - previous.t < ANCHOR_S
        step = max(MIN_STEP_W, MIN_STEP_RATIO * abs(previous.watts))
        negligible = abs(sample.watts - previous.watts) < step
        return not (quiet and negligible)

    def _write_batch(self, entries: list[tuple["OutletConfig", Sample]]) -> None:
        keyed = [(outlet_key(self.config, o), o.label, s) for o, s in entries]
        self._store.append_batch(keyed)
        for key, _label, sample in keyed:
            self._last[key] = sample

    def _write(self, outlet: "OutletConfig", samples: list[Sample]) -> None:
        key = outlet_key(self.config, outlet)
        self._store.append(key, outlet.label, samples)
        # Des points recuperes peuvent etre anterieurs au dernier releve :
        # le plus recent reste la reference des ecarts.
        newest = max(samples, key=lambda sample: sample.t)
        known = self._last.get(key)
        if known is None or newest.t >= known.t:
            self._last[key] = newest

    def _recover(self, now: float) -> bool:
        """Comble avec les ticks du releveur les trous des releves directs.

        Rend faux si le releveur n'a pas pu etre lu : la tentative sera
        refaite a la lecture suivante.

        Chaque tick est juge sur place, et non par rapport au dernier point
        enregistre : quelques releves directs pris juste apres le reveil,
        avant que la recuperation n'aboutisse, suffisaient sinon a rejeter
        toute la nuit comme « anterieure ». Un tick est garde s'il tombe
        dans un trou -- aucun releve direct dans les trois minutes qui le
        precedent -- et s'il n'a pas deja ete recupere.
        """
        outlet = self.config.host_pc_outlet()
        if outlet is None or not self.config.sensing.enabled:
            return True
        from . import sensing

        try:
            timeline = sensing.read_probe_timeline(self.controller, self.config)
        except Exception as exc:  # noqa: BLE001 - l'historique ne doit rien casser
            if not self._recovery_warned:
                self._recovery_warned = True
                self._log(f"History: sleep data unavailable yet, will retry ({exc})")
            return False
        oldest = now - self.keep_seconds
        timeline = [(t, w) for t, w in timeline if oldest < t < now - 1]
        if not timeline:
            return True
        key = outlet_key(self.config, outlet)
        live_gap = MAX_GAP_S[SOURCE_LIVE]
        stored = self._store.read(key, since=timeline[0][0] - live_gap)
        live = [s.t for s in stored if s.source == SOURCE_LIVE]
        probed = [s.t for s in stored if s.source == SOURCE_PROBE]

        def covered(t: float) -> bool:
            index = bisect_right(live, t)
            if index and t - live[index - 1] <= live_gap:
                return True  # l'application mesurait deja
            # Deja recupere : les instants recalcules peuvent differer d'une
            # fraction de seconde d'une lecture a l'autre.
            index = bisect_left(probed, t - 2)
            return index < len(probed) and probed[index] <= t + 2

        recovered = [Sample(t, w, SOURCE_PROBE) for t, w in timeline if not covered(t)]
        if recovered:
            self._write(outlet, recovered)
            self._log(f"History: {len(recovered)} point(s) recovered from the probe")
        return True
