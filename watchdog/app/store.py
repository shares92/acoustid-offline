"""Zustandsspeicher des Waechters: SQLite auf dem Cache-Pool (ARCHITECTURE §5).

Der Waechter haelt seinen gesamten Zustand neben dem Array — genau deshalb
kann die Admin-UI bei schlafendem Stack vollstaendig arbeiten und `/status`
antworten, ohne irgendetwas zu wecken (Invariante §8.2, DECISIONS
2026-07-25 „Config, Keys und Logs beim Waechter auf dem Cache").

Vier Tabellen, alle aus ARCHITECTURE §5 „SQLite (Waechter, Cache)":

===============  =========================================================
``api_key``      Key-Hash, Label, aktiv, erstellt, zuletzt benutzt. Das
                 Schema entsteht hier, benutzt wird es ab Phase 18/26.
``admin_user``   Genau ein Datensatz: Login + argon2-Hash
                 (:mod:`acoustid_watchdog.admin`).
``update_run``   Historie der Import- und Backup-Laeufe
                 (:mod:`acoustid_watchdog.runs`).
``event_log``    Ereignisse mit Level und Zeitstempel, **ringpuffer-artig
                 begrenzt** (:mod:`acoustid_watchdog.events`).
===============  =========================================================

Der Lookup-Cache aus §5 ist bewusst **nicht** dabei: er bekommt in Phase 17
seine eigene Ablage und soll die Zustandsdatenbank nicht mit
Massenschreibvorgaengen belasten.

**Migrationen ueber ``PRAGMA user_version``.** Kein zweiter Runner neben
:mod:`shared.db` — eine Einzeldatei-Datenbank braucht weder Advisory-Locks
noch Drift-Pruefung ueber mehrere Container. Jeder Schritt in
:data:`MIGRATIONS` laeuft in einer eigenen Transaktion; angewendete
Schritte werden uebersprungen, ein zweiter Aufruf ist ein No-Op.

**Nebenlaeufigkeit.** Der Dienst faehrt **eine** Verbindung hinter einem
Lock: die Routen sind ``async``, die Arbeit dahinter (wie im API-Dienst)
synchron im Threadpool. Eine Verbindung je Thread waere fuer die paar
Schreibvorgaenge je Minute unnoetig; WAL sorgt dafuer, dass ein spaeterer
Leser (Backup-Job, Phase 21) trotzdem danebengreifen kann.

**Zeitstempel** stehen als ISO-8601 in UTC mit ``Z`` in der Datenbank —
dasselbe Format wie im JSON-Log (:mod:`shared.logging_setup`), damit sich
Logzeile und Datenbankzeile ohne Umrechnung nebeneinanderlegen lassen.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Final, Self

__all__ = [
    "DB_FILENAME",
    "MIGRATIONS",
    "SCHEMA_VERSION",
    "Database",
    "StoreError",
    "utc_now",
]

_LOG = logging.getLogger(__name__)

#: Dateiname der Zustandsdatenbank im Datenverzeichnis (``MMO_DATA_DIR``).
#: Bewusst keine eigene ``MMO_``-Variable: der Pfad ist eine Eigenschaft des
#: Waechters, kein Bootstrap-Wert, den jemand anders kennen muesste.
DB_FILENAME: Final = "watchdog.sqlite3"

#: Schema-Schritte in Anwendungsreihenfolge. Der Index+1 ist die Version in
#: ``PRAGMA user_version``; neue Phasen haengen hinten an und aendern nie
#: einen bestehenden Schritt (sonst laufen alte Installationen auseinander).
MIGRATIONS: Final[tuple[tuple[str, ...], ...]] = (
    # --- 1: Grundschema (Phase 14) -----------------------------------------
    (
        # API-Keys des `apikey`-Modus. Der Key selbst wird NIE gespeichert,
        # nur sein Hash — angezeigt wird er einmalig beim Erzeugen
        # (ARCHITECTURE §9, Phase 26).
        """
        CREATE TABLE api_key (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            label        TEXT    NOT NULL,
            key_hash     TEXT    NOT NULL UNIQUE,
            active       INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
            created_at   TEXT    NOT NULL,
            last_used_at TEXT
        )
        """,
        # Der Proxy sucht im Anfragepfad ueber den Hash; nur aktive Keys
        # zaehlen (ARCHITECTURE §7 „Durchsetzungsort Auth & Rate-Limit").
        "CREATE INDEX api_key_active_idx ON api_key (active)",
        # Genau ein Admin-Benutzer (ARCHITECTURE §11 „keine Rollenverwaltung").
        # Der CHECK auf id = 1 macht das zur Schema-Eigenschaft statt zur
        # Verabredung.
        """
        CREATE TABLE admin_user (
            id            INTEGER PRIMARY KEY CHECK (id = 1),
            login         TEXT    NOT NULL,
            password_hash TEXT    NOT NULL,
            created_at    TEXT    NOT NULL,
            updated_at    TEXT    NOT NULL
        )
        """,
        # Historie der Laeufe. `kind` trennt Update- von Backup-Laeufen
        # (Phase 19 bzw. 21), `result` ist NULL, solange der Lauf laeuft.
        # `report` haelt den Importer-Report als JSON-Text — die
        # Auswertung gehoert der Phase, die ihn schreibt.
        """
        CREATE TABLE update_run (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            kind           TEXT    NOT NULL CHECK (kind IN ('update', 'backup')),
            started_at     TEXT    NOT NULL,
            finished_at    TEXT,
            result         TEXT    CHECK (result IN ('success', 'failed', 'aborted')),
            files_imported INTEGER NOT NULL DEFAULT 0,
            rows_imported  INTEGER NOT NULL DEFAULT 0,
            last_sequence  TEXT,
            error          TEXT,
            report         TEXT
        )
        """,
        # `/status` und das Dashboard fragen „letzter Lauf dieser Art" —
        # deshalb der zusammengesetzte Index.
        "CREATE INDEX update_run_kind_started_idx ON update_run (kind, started_at DESC)",
        # Ereignisse. Der Feldschnitt ist derselbe wie im JSON-Log
        # (`ts`, `level`, `source`, `message`, `extra`), damit Logzeile und
        # Datenbankzeile dasselbe sagen.
        """
        CREATE TABLE event_log (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            ts      TEXT NOT NULL,
            level   TEXT NOT NULL,
            source  TEXT NOT NULL,
            message TEXT NOT NULL,
            extra   TEXT
        )
        """,
        # Die Logansicht (Phase 27) filtert nach Level und Quelle und
        # sortiert immer nach Zeit.
        "CREATE INDEX event_log_ts_idx ON event_log (ts DESC)",
        "CREATE INDEX event_log_level_idx ON event_log (level)",
        "CREATE INDEX event_log_source_idx ON event_log (source)",
    ),
    # --- 2: Job-Typen der Scope-Erweiterung (M2.5) ---------------------------
    #
    # `kind` kannte bis hier genau zwei Werte (`update`, `backup`) — das war
    # der Stand, als es genau zwei Jobs geben sollte. Mit dem Scheduler
    # kommen die Job-Arten aus E10 dazu, und der taegliche Delta-Import
    # bekommt dabei einen Namen, der die Quelle nennt: `acoustid-delta`
    # statt `update`. Ab M3 stehen `discogs-dump`, `caa-crawl` und
    # `nachzuegler` daneben, und „update" waere dann keine Auskunft mehr,
    # sondern eine Frage.
    #
    # **SQLite kann keinen CHECK aendern** — die Tabelle wird neu gebaut und
    # der Bestand kopiert (der empfohlene Weg der SQLite-Doku). Bestehende
    # `update`-Zeilen wandern dabei auf den neuen Namen; in der Praxis gibt
    # es keine (gefuellt wird die Tabelle erst ab M2.5), aber ein
    # handgeschriebener Eintrag darf nicht am neuen CHECK haengen bleiben.
    (
        """
        CREATE TABLE update_run_new (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            kind           TEXT    NOT NULL CHECK (kind IN (
                               'acoustid-delta', 'discogs-dump', 'caa-crawl',
                               'nachzuegler', 'backup', 'queue-send')),
            started_at     TEXT    NOT NULL,
            finished_at    TEXT,
            result         TEXT    CHECK (result IN ('success', 'failed', 'aborted')),
            files_imported INTEGER NOT NULL DEFAULT 0,
            rows_imported  INTEGER NOT NULL DEFAULT 0,
            last_sequence  TEXT,
            error          TEXT,
            report         TEXT
        )
        """,
        """
        INSERT INTO update_run_new
            (id, kind, started_at, finished_at, result, files_imported,
             rows_imported, last_sequence, error, report)
        SELECT id,
               CASE kind WHEN 'update' THEN 'acoustid-delta' ELSE kind END,
               started_at, finished_at, result, files_imported,
               rows_imported, last_sequence, error, report
          FROM update_run
        """,
        "DROP TABLE update_run",
        "ALTER TABLE update_run_new RENAME TO update_run",
        # Der Index faellt mit der alten Tabelle und wird neu angelegt.
        "CREATE INDEX update_run_kind_started_idx ON update_run (kind, started_at DESC)",
    ),
)

#: Hoechste Schemaversion, die dieser Code kennt.
SCHEMA_VERSION: Final = len(MIGRATIONS)

#: Wartezeit, bevor ein blockierter Schreibvorgang aufgibt. Relevant erst,
#: wenn ein zweiter Prozess mitliest (Backup-Job, Phase 21).
_BUSY_TIMEOUT_MS: Final = 5_000


class StoreError(Exception):
    """Die Zustandsdatenbank ist nicht benutzbar."""


def utc_now() -> str:
    """Jetzt als ISO-8601 in UTC mit ``Z`` — das Format aller Zeitspalten."""
    return datetime.now(tz=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Database:
    """Eine Verbindung zur Zustandsdatenbank, hinter einem Lock.

    Benutzt wird ausschliesslich :meth:`transaction`; damit gibt es keinen
    Weg, versehentlich ohne Commit zu schreiben, und keinen, bei dem zwei
    Threads gleichzeitig auf derselben Verbindung sitzen.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._connection: sqlite3.Connection | None = None
        # Reentrant, damit eine Funktion mit offener Transaktion eine
        # andere aufrufen darf, die selbst eine oeffnet (z. B. „Lauf
        # abschliessen" + „Ereignis protokollieren").
        self._lock = threading.RLock()
        self._depth = 0

    # --- Lebenszyklus -------------------------------------------------------

    @classmethod
    def for_data_dir(cls, data_dir: str | Path) -> Self:
        """Die Datenbank am Vorgabeort innerhalb des Datenverzeichnisses."""
        return cls(Path(data_dir) / DB_FILENAME)

    def open(self) -> Self:
        """Oeffnet die Datei, setzt die PRAGMAs und wendet Migrationen an.

        Raises:
            StoreError: Datei nicht anlegbar oder Schema neuer als dieser
                Code (Downgrade).
        """
        if self._connection is not None:
            return self
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                self.path,
                # Der Zugriff ist ueber `self._lock` serialisiert; ohne
                # dieses Flag koennte der Threadpool die Verbindung nicht
                # benutzen.
                check_same_thread=False,
                # Transaktionsgrenzen setzt `transaction()` selbst.
                isolation_level=None,
            )
        except (OSError, sqlite3.Error) as exc:
            raise StoreError(f"{self.path}: Zustandsdatenbank nicht nutzbar: {exc}") from exc

        connection.row_factory = sqlite3.Row
        # WAL: Leser blockieren den Schreiber nicht — wichtig, sobald der
        # Backup-Job (Phase 21) die Datei nebenher sichert.
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        self._connection = connection

        try:
            self.migrate()
        except BaseException:
            self.close()
            raise
        return self

    def close(self) -> None:
        """Schliesst die Verbindung; ein zweiter Aufruf ist ein No-Op."""
        with self._lock:
            if self._connection is None:
                return
            self._connection.close()
            self._connection = None

    def __enter__(self) -> Self:
        return self.open()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # --- Zugriff ------------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Eine Transaktion auf der gemeinsamen Verbindung.

        Verschachtelte Aufrufe teilen sich die aeussere Transaktion (SQLite
        kennt keine echten verschachtelten Transaktionen); commitiert wird
        genau einmal, beim Verlassen der aeussersten Ebene.

        Raises:
            StoreError: Die Datenbank ist nicht geoeffnet.
        """
        with self._lock:
            connection = self._require_connection()
            if self._depth > 0:
                self._depth += 1
                try:
                    yield connection
                finally:
                    self._depth -= 1
                return

            connection.execute("BEGIN")
            self._depth = 1
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()
            finally:
                self._depth = 0

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise StoreError(f"{self.path}: Zustandsdatenbank ist nicht geoeffnet")
        return self._connection

    # --- Schema -------------------------------------------------------------

    @property
    def schema_version(self) -> int:
        """Aktuell angewendete Schemaversion (``PRAGMA user_version``)."""
        with self._lock:
            row = self._require_connection().execute("PRAGMA user_version").fetchone()
        return int(row[0])

    def migrate(self) -> int:
        """Wendet alle fehlenden Schema-Schritte an; liefert deren Anzahl.

        Raises:
            StoreError: Die Datenbank ist nicht geoeffnet, oder die Datei
                traegt eine hoehere Version als dieser Code kennt — ein
                Downgrade wuerde Daten verlieren.
        """
        with self._lock:
            # `schema_version` prueft mit, ob die Verbindung ueberhaupt steht.
            current = self.schema_version
            if current > SCHEMA_VERSION:
                raise StoreError(
                    f"{self.path}: Schemaversion {current} ist neuer als die bekannte "
                    f"Version {SCHEMA_VERSION} — kein Downgrade moeglich"
                )
            applied = 0
            for number, statements in enumerate(MIGRATIONS[current:], start=current + 1):
                with self.transaction() as tx:
                    for statement in statements:
                        tx.execute(statement)
                    # `PRAGMA user_version` nimmt keinen Parameter; die
                    # Zahl stammt aus der Aufzaehlung, nicht von aussen.
                    tx.execute(f"PRAGMA user_version = {number}")
                applied += 1
                _LOG.info(
                    "Schema der Zustandsdatenbank erweitert",
                    extra={"db_path": str(self.path), "schema_version": number},
                )
        return applied
