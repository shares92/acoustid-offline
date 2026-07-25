"""Buchfuehrung des resumierbaren Imports: Tabelle ``import_state``.

Hier liegt die Bruecke zwischen der reinen Arbeitslisten-Rechnung
(:mod:`acoustid_importer.worklist`) und der Datenbank: welche Tagesdateien
sind vollstaendig eingespielt, welche fehlen noch, und wo hat ein
abgebrochener Lauf aufgehoert (ARCHITECTURE §5.2, Invariante §8.4).

**Eine Zeile je Strom und Kalendertag.** Angelegt wird sie von
:func:`begin`, abgeschlossen von :func:`finish` — beides **innerhalb**
derselben Transaktion wie der eigentliche Import der Datei
(:mod:`acoustid_importer.dbimport`). Bricht die Datei ab, verschwindet die
Zeile mit dem Rollback; der naechste Lauf sieht den Tag wieder als offen.

**Erledigt heisst ``finished_at IS NOT NULL``.** Nur solche Zeilen zaehlen
als importiert. Da Anlegen und Abschliessen in einer Transaktion liegen,
kann eine committete Zeile im Normalfall gar nicht halbfertig sein — die
Pruefung steht trotzdem ueberall, damit der Bootstrap-Bulk-Modus (Phase 8)
den Zustand spaeter auch mehrstufig fuehren darf, ohne dass die
Lueckenpruefung falsche Antworten gibt.

**Zeitstempel.** ``started_at`` ist ``now()``, also der Beginn der
Datei-Transaktion; ``finished_at`` setzt :func:`finish` bewusst auf
``clock_timestamp()`` (die echte Uhr). Nur so ist die Differenz die
tatsaechliche Importdauer der Datei und nicht konstant null — Phase 8
braucht sie fuer das Ergebnis-Reporting.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Final

from psycopg import Connection

from acoustid_importer.streams import FIRST_DAY, IMPORT_ORDER, DeltaFile, Stream
from acoustid_importer.worklist import ImportPlan, plan_from_state

__all__ = [
    "TABLE",
    "FileState",
    "begin",
    "finish",
    "imported_days",
    "is_done",
    "plan",
    "state_for",
]

_LOG = logging.getLogger(__name__)

#: Name der Buchfuehrungstabelle (ARCHITECTURE §5.2).
TABLE: Final = "import_state"

_SELECT_DONE: Final = f"SELECT stream, day FROM {TABLE} WHERE finished_at IS NOT NULL"

_SELECT_ONE: Final = f"""
SELECT stream, day, file_name, file_size, row_count, started_at, finished_at
  FROM {TABLE}
 WHERE stream = %s AND day = %s
"""

_BEGIN: Final = f"""
INSERT INTO {TABLE} (stream, day, file_name, file_size, row_count, started_at, finished_at)
    VALUES (%s, %s, %s, %s, NULL, now(), NULL)
    ON CONFLICT (stream, day) DO UPDATE
       SET file_name = EXCLUDED.file_name,
           file_size = EXCLUDED.file_size,
           row_count = NULL,
           started_at = now(),
           finished_at = NULL
"""

_FINISH: Final = f"""
UPDATE {TABLE}
   SET row_count = %s, finished_at = clock_timestamp()
 WHERE stream = %s AND day = %s
"""


@dataclass(frozen=True, slots=True)
class FileState:
    """Eine Zeile aus ``import_state``."""

    stream: Stream
    day: date
    file_name: str
    file_size: int | None
    row_count: int | None
    started_at: datetime
    finished_at: datetime | None

    @property
    def file(self) -> DeltaFile:
        """Die Tagesdatei, um die es geht."""
        return DeltaFile(self.day, self.stream)

    @property
    def done(self) -> bool:
        """Vollstaendig eingespielt?"""
        return self.finished_at is not None

    @property
    def duration_s(self) -> float | None:
        """Dauer des Imports in Sekunden, solange er abgeschlossen ist."""
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()


def imported_days(conn: Connection) -> dict[Stream, frozenset[date]]:
    """Je Strom die vollstaendig importierten Kalendertage.

    Genau die Sicht, die :func:`acoustid_importer.worklist.plan_from_state`
    erwartet. Zeilen mit unbekanntem ``stream`` (aelterer oder neuerer Stand
    des Schemas) werden mit einer Warnung uebergangen, statt den Lauf
    abzubrechen — dieselbe Regel wie bei unbekannten Konfigurationsschluesseln.
    """
    days: dict[Stream, set[date]] = {stream: set() for stream in IMPORT_ORDER}
    unknown: dict[str, int] = {}
    for name, day in conn.execute(_SELECT_DONE).fetchall():
        try:
            stream = Stream(name)
        except ValueError:
            unknown[name] = unknown.get(name, 0) + 1
            continue
        days[stream].add(day)
    for name, count in sorted(unknown.items()):
        _LOG.warning(
            "Unbekannter Strom in import_state wird uebergangen",
            extra={"stream": name, "rows": count},
        )
    return {stream: frozenset(found) for stream, found in days.items()}


def plan(conn: Connection, *, today: date, first_day: date = FIRST_DAY) -> ImportPlan:
    """Arbeitsliste und Lueckenpruefung aus dem Zustand der Datenbank.

    Das ist die Anbindung der Phase-6-Logik an die Buchfuehrung: die
    Rechnung selbst bleibt pure Funktion, hier kommt nur der Zustand dazu.

    Args:
        conn: Verbindung zur AcoustID-Postgres.
        today: Der laufende Tag — immer explizit (nie ``date.today()`` tief
            im Code, sonst sind die Randfaelle nicht testbar).
        first_day: Beginn der Historie; nur fuer Tests abweichend.
    """
    return plan_from_state(imported_days(conn), today=today, first_day=first_day)


def state_for(conn: Connection, file: DeltaFile) -> FileState | None:
    """Die Buchfuehrungszeile einer Tagesdatei, falls es sie gibt."""
    row = conn.execute(_SELECT_ONE, (file.stream.value, file.day)).fetchone()
    if row is None:
        return None
    name, day, file_name, file_size, row_count, started_at, finished_at = row
    return FileState(
        stream=Stream(name),
        day=day,
        file_name=file_name,
        file_size=file_size,
        row_count=row_count,
        started_at=started_at,
        finished_at=finished_at,
    )


def is_done(conn: Connection, file: DeltaFile) -> bool:
    """Ist diese Tagesdatei vollstaendig eingespielt?"""
    row = conn.execute(
        f"SELECT 1 FROM {TABLE} WHERE stream = %s AND day = %s AND finished_at IS NOT NULL",
        (file.stream.value, file.day),
    ).fetchone()
    return row is not None


def begin(conn: Connection, file: DeltaFile, *, file_name: str, file_size: int | None) -> None:
    """Vermerkt den Beginn eines Datei-Imports.

    Ein vorhandener Eintrag wird zurueckgesetzt (``row_count`` und
    ``finished_at`` auf NULL): der Tag wird gerade neu eingespielt und ist
    bis zum :func:`finish` derselben Transaktion nicht erledigt.
    """
    conn.execute(_BEGIN, (file.stream.value, file.day, file_name, file_size))


def finish(conn: Connection, file: DeltaFile, *, row_count: int) -> None:
    """Schliesst den Datei-Import ab — der Tag gilt danach als erledigt."""
    conn.execute(_FINISH, (row_count, file.stream.value, file.day))
