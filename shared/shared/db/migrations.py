"""Schlanker Migrations-Runner fuer die AcoustID-Postgres (ARCHITECTURE §5.2).

Bewusst kein Alembic: das Schema besteht aus sieben Tabellen ohne ORM, die
Migrationen sind reines SQL, und der Runner muss beim Start jedes Jobs und
Services laufen koennen, ohne ein zweites Konfigurationssystem mitzubringen.

**Dateien.** Jede Migration ist eine nummerierte SQL-Datei als Package-Data
unter ``shared/db/sql/<gruppe>/<nummer>_<name>.sql``. Die Nummer ist global
eindeutig und bestimmt die Reihenfolge; der Dateiname ist zugleich die in
``schema_migrations`` protokollierte Version.

**Gruppen.** ``core`` legt Tabellen und Primaerschluessel an, ``indexes``
alle Sekundaerindizes samt Partialindizes und der lz4-Kompression. Grund:
beim Bootstrap (Voll-Replay aller Tagesdeltas) werden die Sekundaerindizes
erst NACH dem Massenimport gebaut — ARCHITECTURE §5.2, Import-Regel 6::

    apply(conn, groups=["core"])  # vor dem Bootstrap-Import
    ...  # Massenimport
    apply(conn, groups=["indexes"])  # danach

Der Normalfall (laufender Betrieb, CI, Tests) ist ``apply(conn)`` — das
wendet alle Gruppen in einem Rutsch an. Die Nummerierung garantiert, dass
beide Wege in derselben Reihenfolge enden.

**Eigenschaften.**

* Jede Migration laeuft in ihrer eigenen Transaktion (DDL ist in Postgres
  transaktional): eine fehlerhafte Datei laesst das Schema unveraendert.
* Idempotent — was in ``schema_migrations`` steht, wird uebersprungen; ein
  zweiter Lauf ist ein No-Op.
* Nebenlaeufig sicher: jede Transaktion nimmt vorher einen
  Advisory-Lock, zwei gleichzeitig startende Container behindern sich nicht.
* Drift-Erkennung: zu jeder Version wird die SHA-256-Summe der Datei
  protokolliert. Wurde eine bereits angewendete Datei nachtraeglich
  geaendert, bricht der Lauf mit :class:`MigrationDriftError` ab, statt
  stillschweigend ein anderes Schema anzunehmen.
"""

from __future__ import annotations

import hashlib
import itertools
import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import cache
from importlib.resources import files
from typing import Final

import psycopg
from psycopg import Connection
from psycopg import pq as psycopg_pq

from shared.env import EnvSettings

__all__ = [
    "BOOKKEEPING_TABLE",
    "CORE",
    "GROUPS",
    "INDEXES",
    "Migration",
    "MigrationDriftError",
    "MigrationError",
    "MigrationReport",
    "applied_versions",
    "apply",
    "apply_from_env",
    "migrations",
    "normalise_groups",
    "pending",
]

_LOG = logging.getLogger(__name__)

#: Gruppe 1: Tabellen und Primaerschluessel.
CORE: Final = "core"
#: Gruppe 2: Sekundaerindizes (Bootstrap: erst nach dem Massenimport).
INDEXES: Final = "indexes"
#: Alle Gruppen in Anwendungsreihenfolge.
GROUPS: Final[tuple[str, ...]] = (CORE, INDEXES)

#: Tabelle, in der angewendete Migrationen protokolliert werden.
BOOKKEEPING_TABLE: Final = "schema_migrations"

# Session-uebergreifender Schluessel des Advisory-Locks; Ziffernfolge ohne
# Bedeutung ausser: sie gehoert diesem Projekt (ASCII "AOFFMIG").
_LOCK_KEY: Final = 0x414F46464D4947

_SQL_DIR: Final = "sql"
_FILE_PATTERN: Final = re.compile(r"^(?P<number>\d{4})_(?P<name>[a-z0-9_]+)\.sql$")

_BOOKKEEPING_DDL: Final = f"""
CREATE TABLE IF NOT EXISTS {BOOKKEEPING_TABLE} (
    version    text        PRIMARY KEY,
    group_name text        NOT NULL,
    checksum   text        NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


class MigrationError(Exception):
    """Migrationen sind nicht anwendbar (Aufruf, Dateien oder Verbindung)."""


class MigrationDriftError(MigrationError):
    """Eine bereits angewendete Migrationsdatei hat sich nachtraeglich geaendert."""


@dataclass(frozen=True, slots=True)
class Migration:
    """Eine SQL-Datei aus dem Package-Data-Verzeichnis."""

    #: Dateiname, z. B. ``0001_track.sql`` — die Version in `schema_migrations`.
    version: str
    #: ``core`` oder ``indexes``.
    group: str
    #: Global eindeutige Reihenfolgenummer (Praefix des Dateinamens).
    number: int
    #: Vollstaendiger Inhalt der Datei (ein oder mehrere Statements).
    statements: str
    #: SHA-256 des Inhalts, hex — Grundlage der Drift-Erkennung.
    checksum: str


@dataclass(frozen=True, slots=True)
class MigrationReport:
    """Ergebnis eines :func:`apply`-Laufs."""

    #: Angewendete Versionen in Anwendungsreihenfolge.
    applied: tuple[str, ...]
    #: Uebersprungene Versionen (standen bereits in `schema_migrations`).
    skipped: tuple[str, ...]
    #: Betrachtete Gruppen, in Anwendungsreihenfolge.
    groups: tuple[str, ...]

    @property
    def changed(self) -> bool:
        """`True`, wenn dieser Lauf das Schema veraendert hat."""
        return bool(self.applied)


def migrations(groups: Iterable[str] | None = None) -> tuple[Migration, ...]:
    """Alle Migrationen der gewaehlten Gruppen in Anwendungsreihenfolge.

    Args:
        groups: Gruppennamen; `None` (Default) heisst alle Gruppen.

    Raises:
        MigrationError: unbekannter Gruppenname oder defektes Package-Data.
    """
    selected = frozenset(normalise_groups(groups))
    return tuple(item for item in _load_migrations() if item.group in selected)


def normalise_groups(groups: Iterable[str] | None) -> tuple[str, ...]:
    """Gruppennamen pruefen, entdoppeln und in Anwendungsreihenfolge bringen.

    Raises:
        MigrationError: unbekannter Gruppenname.
    """
    if groups is None:
        return GROUPS
    requested = list(groups)
    unknown = sorted(set(requested) - set(GROUPS))
    if unknown:
        raise MigrationError(
            f"Unbekannte Migrationsgruppe(n): {', '.join(unknown)}; "
            f"bekannt sind: {', '.join(GROUPS)}"
        )
    return tuple(group for group in GROUPS if group in requested)


def apply(conn: Connection, *, groups: Iterable[str] | None = None) -> MigrationReport:
    """Wendet die ausstehenden Migrationen der gewaehlten Gruppen an.

    Jede Migration laeuft in einer eigenen Transaktion; bereits
    protokollierte Versionen werden uebersprungen. Der Aufruf ist damit
    idempotent und beim Start jedes Jobs/Services zumutbar.

    Args:
        conn: offene psycopg-Verbindung OHNE laufende Transaktion (sonst
            waeren die Einzeltransaktionen nur Savepoints und das Ergebnis
            haenge an einem Commit des Aufrufers).
        groups: Gruppen; `None` (Default) heisst alle — der Normalfall.
            Beim Bootstrap stattdessen erst ``["core"]``, nach dem
            Massenimport ``["indexes"]``.

    Returns:
        MigrationReport: was angewendet und was uebersprungen wurde.

    Raises:
        MigrationError: unbekannte Gruppe oder Verbindung mit offener
            Transaktion.
        MigrationDriftError: eine angewendete Datei wurde nachtraeglich
            geaendert.
        psycopg.Error: Fehler in einer Migration (deren Transaktion ist dann
            zurueckgerollt, vorherige Migrationen bleiben angewendet).
    """
    selected_groups = normalise_groups(groups)
    _require_no_open_transaction(conn)

    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
        conn.execute(_BOOKKEEPING_DDL)

    applied: list[str] = []
    skipped: list[str] = []
    for migration in migrations(selected_groups):
        if _apply_one(conn, migration):
            applied.append(migration.version)
        else:
            skipped.append(migration.version)

    _LOG.info(
        "Migrationen geprueft",
        extra={
            "migration_groups": list(selected_groups),
            "migration_applied": applied,
            "migration_skipped_count": len(skipped),
        },
    )
    return MigrationReport(applied=tuple(applied), skipped=tuple(skipped), groups=selected_groups)


def apply_from_env(
    *,
    groups: Iterable[str] | None = None,
    settings: EnvSettings | None = None,
) -> MigrationReport:
    """Wie :func:`apply`, oeffnet die Verbindung aber selbst aus der Umgebung.

    Der uebliche Aufruf beim Start eines Containers: die Zugangsdaten kommen
    aus den `AOFF_DB_*`-Variablen (`shared.env.EnvSettings.db_dsn`).

    Raises:
        shared.env.EnvError: `AOFF_DB_PASSWORD` fehlt oder die Umgebung ist
            ungueltig.
    """
    resolved = EnvSettings.from_env() if settings is None else settings
    with psycopg.connect(resolved.db_dsn().get_secret_value()) as conn:
        return apply(conn, groups=groups)


def applied_versions(conn: Connection) -> tuple[str, ...]:
    """Bereits protokollierte Versionen, aelteste zuerst.

    Liefert ein leeres Tupel, solange die Buchfuehrungstabelle fehlt.
    """
    if not _bookkeeping_exists(conn):
        return ()
    rows = conn.execute(f"SELECT version FROM {BOOKKEEPING_TABLE} ORDER BY version").fetchall()
    return tuple(row[0] for row in rows)


def pending(conn: Connection, *, groups: Iterable[str] | None = None) -> tuple[Migration, ...]:
    """Noch nicht angewendete Migrationen der gewaehlten Gruppen."""
    done = set(applied_versions(conn))
    return tuple(item for item in migrations(groups) if item.version not in done)


# --- Intern ----------------------------------------------------------------


def _apply_one(conn: Connection, migration: Migration) -> bool:
    """Wendet eine Migration in einer eigenen Transaktion an.

    Returns:
        bool: `True`, wenn sie angewendet wurde; `False`, wenn sie schon
        protokolliert war.
    """
    with conn.transaction():
        # Der Lock serialisiert gleichzeitig startende Container; die Pruefung
        # gehoert deshalb in dieselbe Transaktion wie die Anwendung.
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
        row = conn.execute(
            f"SELECT checksum FROM {BOOKKEEPING_TABLE} WHERE version = %s",
            (migration.version,),
        ).fetchone()
        if row is not None:
            if row[0] != migration.checksum:
                raise MigrationDriftError(
                    f"Migration {migration.version} wurde bereits angewendet, die Datei "
                    f"hat sich seitdem aber geaendert (protokolliert {row[0][:12]}…, "
                    f"jetzt {migration.checksum[:12]}…). Angewendete Migrationen sind "
                    "unveraenderlich — Aenderungen gehoeren in eine neue Datei."
                )
            return False

        # Ohne Parameter nutzt psycopg das einfache Query-Protokoll; die Datei
        # darf deshalb mehrere Statements enthalten.
        conn.execute(migration.statements)
        conn.execute(
            f"INSERT INTO {BOOKKEEPING_TABLE} (version, group_name, checksum) VALUES (%s, %s, %s)",
            (migration.version, migration.group, migration.checksum),
        )

    _LOG.info(
        "Migration angewendet",
        extra={"migration_version": migration.version, "migration_group": migration.group},
    )
    return True


def _require_no_open_transaction(conn: Connection) -> None:
    status = conn.pgconn.transaction_status
    if status != psycopg_pq.TransactionStatus.IDLE:
        raise MigrationError(
            "Die Verbindung hat eine offene Transaktion "
            f"(Status {psycopg_pq.TransactionStatus(status).name}). Migrationen "
            "brauchen je eine eigene Transaktion — bitte eine frische oder "
            "committete Verbindung uebergeben."
        )


def _bookkeeping_exists(conn: Connection) -> bool:
    row = conn.execute("SELECT to_regclass(%s)", (BOOKKEEPING_TABLE,)).fetchone()
    return row is not None and row[0] is not None


@cache
def _load_migrations() -> tuple[Migration, ...]:
    """Liest alle SQL-Dateien aus dem Package-Data (einmal je Prozess)."""
    root = files(__package__ or "shared.db").joinpath(_SQL_DIR)
    found: list[Migration] = []
    for group in GROUPS:
        directory = root.joinpath(group)
        names = sorted(entry.name for entry in directory.iterdir() if entry.name.endswith(".sql"))
        if not names:
            raise MigrationError(
                f"Gruppe {group!r} enthaelt keine SQL-Datei — fehlt das Package-Data "
                "(shared/db/sql/**/*.sql) in der Installation?"
            )
        for name in names:
            match = _FILE_PATTERN.match(name)
            if match is None:
                raise MigrationError(
                    f"Migrationsdatei {group}/{name} folgt nicht dem Muster "
                    "<vierstellige Nummer>_<name>.sql"
                )
            text = directory.joinpath(name).read_text(encoding="utf-8")
            found.append(
                Migration(
                    version=name,
                    group=group,
                    number=int(match["number"]),
                    statements=text,
                    checksum=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                )
            )
    _check_ordering(found)
    return tuple(found)


def _check_ordering(found: Sequence[Migration]) -> None:
    """Nummern global eindeutig und ueber die Gruppen hinweg aufsteigend.

    Nur so ergibt der Bootstrap-Weg (erst `core`, spaeter `indexes`) dieselbe
    Reihenfolge wie ein Lauf ueber alle Gruppen.
    """
    for previous, current in itertools.pairwise(found):
        if current.number <= previous.number:
            raise MigrationError(
                f"Migrationsnummern muessen ueber alle Gruppen hinweg aufsteigen: "
                f"{previous.group}/{previous.version} vor {current.group}/{current.version}"
            )
