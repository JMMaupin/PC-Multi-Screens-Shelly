"""Per-outlet power consumption history, kept in an SQLite database.

Every tracked outlet feeds the same database, trimmed of its oldest data:
a queue whose depth is set in days, a week or a month depending on how far
back one wants to be able to look.

We don't record every reading, only what teaches us something -- the tick
principle. A point is written when the power changes noticeably, and at
least once a minute so the curve keeps an anchor. An idle or sleeping PC
therefore costs only a few points per hour.

All configured outlets are tracked, each under its own name. Two sources
feed the database. The application reads the outlets every five seconds
while it runs -- that is, while the PC runs. During sleep or shutdown, the
logger embedded in the power strip keeps measuring: on resume or at
launch, its ticks fill the gap. Only the PC's outlet benefits from this
logger. The others only have direct readings: during sleep, the screens
are cut off and only a few USB hubs draw power, which isn't worth wearing
out the power strips' flash memory to record.

Why SQLite. A first format, binary and home-made, was compact but mute:
no signature, no version, no description, and unreadable without the code
that had written it. SQLite is a standardised format, bundled with Python,
readable by a spreadsheet, DB Browser for SQLite, Grafana or any language
alike. The database describes itself -- an `info` table says what each
column holds, and a `sample_readable` view gives the local time in plain
text --, and it survives power cuts thanks to its journal.

An outlet is identified there by the power strip's MAC address and its
output number, not by the device key: those keys change with renames, the
MAC never does, and the history must survive both.
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

SOURCE_LIVE = 0  # read by the application
SOURCE_PROBE = 1  # recovered from the on-device probe

# Beyond this gap, two points are no longer joined: nothing was measured
# between them. The application sets an anchor every minute, the on-device
# probe every quarter of an hour; each therefore has its own tolerance.
MAX_GAP_S = {SOURCE_LIVE: 180.0, SOURCE_PROBE: 20 * 60.0}

ANCHOR_S = 60.0  # at least one point per minute, even if nothing changes
MIN_STEP_W = 1.0  # below this, a variation is not an event
MIN_STEP_RATIO = 0.03  # ... nor below 3 % of the current power
TRIM_EVERY_S = 3600.0  # trimming frequency
DEFAULT_DAYS = 30

# First format, binary, read one last time for the migration.
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

# What the database says about itself, to whoever opens it without the application.
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
    """A history point: the power valid from `t` onwards."""

    t: float
    watts: float
    source: int = SOURCE_LIVE

    @property
    def max_gap(self) -> float:
        return MAX_GAP_S.get(self.source, MAX_GAP_S[SOURCE_LIVE])


def history_dir(config: "AppConfig") -> Path:
    """History folder: the machine's, shared by every account."""
    return config.paths.history_dir


def db_path(config: "AppConfig") -> Path:
    return history_dir(config) / DB_NAME


def outlet_key(config: "AppConfig", outlet: "OutletConfig") -> str:
    """Stable identifier of an outlet: device MAC and output."""
    device = config.device(outlet.device)
    mac = device.mac if device is not None and device.mac else outlet.device
    return f"{mac.upper()}-{outlet.switch_id}"


class HistoryStore:
    """The database: append, read, trim.

    `readonly` opens an existing database without ever writing to it, for
    the viewing window; it reads while the recorder writes, which SQLite's
    WAL journal allows without either one waiting for the other.
    """

    def __init__(self, path: Path, readonly: bool = False) -> None:
        self.path = path
        self._ids: dict[str, int] = {}
        self._labels: dict[str, str] = {}
        if readonly:
            self._db = sqlite3.connect(str(path), timeout=10, check_same_thread=False)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        # One connection shared by the polling threads: it is the recorder's
        # lock that queues them, not SQLite.
        self._db = sqlite3.connect(str(path), timeout=10, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        # Every write goes all the way to disk. They are rare -- a few per
        # minute --, and the PC can be unplugged at any moment.
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

    # ------------------------------------------------------------ outlets

    def _find(self, key: str) -> int | None:
        cached = self._ids.get(key)
        if cached is not None:
            return cached
        try:
            row = self._db.execute(
                "SELECT id, label FROM outlet WHERE key = ?", (key,)
            ).fetchone()
        except sqlite3.OperationalError:
            return None  # database still empty: nothing recorded yet
        if row is not None:
            self._ids[key] = int(row[0])
            self._labels[key] = str(row[1])
        return self._ids.get(key)

    def _outlet_id(self, key: str, label: str) -> int:
        """Outlet identifier, created if needed, its name kept up to date.

        The name follows renames: it is what one reads when opening the
        database without the application, and a renamed outlet must still
        be found there.
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
        """Outlets present in the database: key and name."""
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
            # A point already present -- same outlet, same instant -- is
            # ignored: a replayed migration creates no duplicate.
            self._db.executemany(
                "INSERT OR IGNORE INTO sample(outlet_id, t, watts, source) "
                "VALUES (?, ?, ?, ?)",
                [(outlet_id, s.t, s.watts, s.source) for s in samples],
            )

    def append_batch(self, entries: list[tuple[str, str, Sample]]) -> None:
        """Writes the points of several outlets in a single transaction.

        A reading covers all outlets at once: one transaction per outlet
        multiplied the forced disk writes by as much.
        """
        # New outlets are created first: otherwise their insertion would
        # commit the points' transaction halfway through.
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
        """Points of an outlet, in chronological order.

        `after` returns only later points -- reading what's new --, `since`
        limits the past to the desired depth.
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
        """Flushes the journal into the database: before sleep or shutdown."""
        self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")


CSV_COLUMNS = ("local_time", "unix_time", "watts", "source", "duration_s")


def export_csv(
    path: Path, samples: list[Sample], end: float,
    delimiter: str = ",", decimal: str = ".",
) -> int:
    """Writes points as CSV; returns their count.

    By default, standard CSV (RFC 4180): comma between fields, decimal
    point, ISO 8601 time with its offset. That is what every tool reads --
    but not French-language Excel, which expects semicolons and decimal
    commas, and otherwise crams everything into the first column. The
    separators are therefore passed as parameters.

    `duration_s` says how long each value held: until the next point, or
    `end` for the last one, without ever crossing a measurement gap. The
    energy in watt-hours follows from a single sum of products, without
    having to rebuild the timeline.
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
            # A single rounding for both time columns: the ISO time truncated,
            # the Unix time rounded, and they differed by one second for the
            # same instant.
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
    """Database opened read-only, or None if nothing has been written yet."""
    path = db_path(config)
    if not path.exists():
        return None
    return HistoryStore(path, readonly=True)


def migrate_legacy(
    store: HistoryStore, directory: Path, labels: dict[str, str],
    log: Callable[[str], None],
) -> int:
    """Imports the files of the first binary format into the database.

    Each file is renamed once imported, not deleted: it is the only copy of
    the measurements until the database has been checked. The import can be
    replayed safely -- a point already present is ignored --, which matters:
    an old version of the application may still have written a file
    between two launches.
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
    """Feeds the database from the application's readings.

    Called after every successful read of the outlets, it costs the power
    strip no extra request: it reuses what has just been read. Only the
    recovery of the sleep data queries the device, once at launch and once
    on every resume.
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

    # ------------------------------------------------------------ settings

    @property
    def keep_seconds(self) -> float:
        days = getattr(self.config.settings, "history_days", DEFAULT_DAYS)
        return max(1, int(days)) * 86400.0

    def tracked(self) -> list["OutletConfig"]:
        """Tracked outlets: all those in the configuration."""
        return list(self.config.outlets)

    def _labels(self) -> dict[str, str]:
        return {outlet_key(self.config, o): o.label for o in self.config.outlets}

    def _open(self) -> HistoryStore:
        """Opens the database on the first write, importing the old format."""
        if self._store is None:
            self._store = HistoryStore(db_path(self.config))
            migrate_legacy(
                self._store, history_dir(self.config), self._labels(), self._log
            )
        return self._store

    # ------------------------------------------------------------ events

    def request_recovery(self) -> None:
        """Flags a gap to fill: launch, or resume from sleep.

        The recovery itself waits for the next successful read: it happens
        in the same thread, before the first direct reading, so the points
        are written in chronological order.
        """
        self._recovery_pending = True
        self._recovery_warned = False

    def feed(self, states: dict[str, "SwitchState"]) -> None:
        """Keeps what is worth keeping from the readings just taken."""
        if not states:
            return
        now = time.time()
        with self._lock:
            self._open()
            # Recovery waits until the PC's device has answered. On resume,
            # the other power strip often comes back first: trying on its
            # answer alone meant querying a probe still unreachable, and
            # losing the night without a word.
            pc = self.config.host_pc_outlet()
            if self._recovery_pending and pc is not None and pc.ref in states:
                self._recovery_pending = not self._recover(now)
            # A silent outlet -- its device didn't answer -- leaves a gap,
            # rather than a zero that was never measured.
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
        """Flushes the journal into the database: before sleep or shutdown."""
        with self._lock:
            if self._store is not None:
                try:
                    self._store.checkpoint()
                except sqlite3.Error:
                    pass

    # ------------------------------------------------------------ internal

    def _previous(self, outlet: "OutletConfig") -> Sample | None:
        key = outlet_key(self.config, outlet)
        if key not in self._last:
            previous = self._store.last(key)
            if previous is not None:
                self._last[key] = previous
        return self._last.get(key)

    def _worth_keeping(self, outlet: "OutletConfig", sample: Sample) -> bool:
        """True if the point teaches something: a change, or the minute's anchor."""
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
        # Recovered points may predate the last reading: the most recent one
        # stays the reference for changes.
        newest = max(samples, key=lambda sample: sample.t)
        known = self._last.get(key)
        if known is None or newest.t >= known.t:
            self._last[key] = newest

    def _recover(self, now: float) -> bool:
        """Fills the gaps in the direct readings with the probe's ticks.

        Returns False if the probe could not be read: the attempt will be
        repeated on the next read.

        Each tick is judged on its own, not against the last recorded
        point: otherwise a few direct readings taken right after resume,
        before recovery succeeded, were enough to reject the whole night as
        "earlier". A tick is kept if it falls into a gap -- no direct
        reading in the three minutes before it -- and if it hasn't already
        been recovered.
        """
        outlet = self.config.host_pc_outlet()
        if outlet is None or not self.config.sensing.enabled:
            return True
        from . import sensing

        try:
            timeline = sensing.read_probe_timeline(self.controller, self.config)
        except Exception as exc:  # noqa: BLE001 - history must never break anything
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
                return True  # the application was already measuring
            # Already recovered: recomputed instants may differ by a
            # fraction of a second from one read to the next.
            index = bisect_left(probed, t - 2)
            return index < len(probed) and probed[index] <= t + 2

        recovered = [Sample(t, w, SOURCE_PROBE) for t, w in timeline if not covered(t)]
        if recovered:
            self._write(outlet, recovered)
            self._log(f"History: {len(recovered)} point(s) recovered from the probe")
        return True
