"""Ereignis-Log des Waechters — ``event_log`` mit Ringpuffer-Grenze (§5).

Das Ereignis-Log ist die Sicht des Betreibers auf den Betrieb: Start und
Stopp, Weckvorgaenge, fehlgeschlagene Importe, verschickte Benachrichtigungen
(ARCHITECTURE §5 „SQLite (Waechter, Cache)", Admin-UI-Seite `/admin/logs`,
Phase 27). Es ist bewusst **kein zweites Logsystem**: jedes Ereignis geht
zusaetzlich als JSON-Zeile nach stderr (:mod:`shared.logging_setup`), damit
`docker logs` vollstaendig bleibt. Der Unterschied ist die Haltbarkeit — die
Datenbank ueberlebt einen Containerneustart, das Containerlog nicht
zwingend.

**Ringpuffer statt unbegrenztem Wachstum.** Der Waechter laeuft dauerhaft
auf einem SSD-Cache-Pool; ein Log, das jahrelang mitwaechst, ist dort ein
Platz- und ein Abfrageproblem. Nach jedem Schreiben bleiben deshalb nur die
:data:`EVENT_LOG_LIMIT` juengsten Eintraege stehen — exakt, nicht
naeherungsweise: geloescht wird alles ausserhalb der nach ``id``
absteigend sortierten Auswahl, damit auch Luecken in der Nummernfolge
(geloeschte Zeilen, ROLLBACK) nichts verschieben.

**Extra-Felder** landen als JSON-Text in einer eigenen Spalte, nicht als
weitere Spalten: welche Felder ein Ereignis mitbringt, entscheidet die
Phase, die es ausloest. Nicht serialisierbare Werte werden ueber ``str()``
abgebildet — dieselbe Regel wie im JSON-Log, damit `SecretStr` auch hier
maskiert bleibt.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from acoustid_watchdog.store import Database, utc_now

__all__ = [
    "EVENT_LOG_LIMIT",
    "Event",
    "EventLevel",
    "count_events",
    "log_event",
    "recent_events",
]

_LOG = logging.getLogger(__name__)

#: Wie viele Ereignisse hoechstens aufbewahrt werden. Grosszuegig genug fuer
#: mehrere Wochen Normalbetrieb (ein ruhiger Tag erzeugt eine Handvoll
#: Ereignisse) und klein genug, dass die Logansicht ohne Paginierung
#: auskommt.
EVENT_LOG_LIMIT: Final = 5_000


class EventLevel(StrEnum):
    """Level eines Ereignisses — dieselben Namen wie im stdlib-``logging``.

    So ist die Zuordnung zwischen Logzeile und Datenbankzeile eindeutig, und
    die Logansicht kann nach denselben Begriffen filtern, die auch in
    `docker logs` stehen.
    """

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"

    @property
    def display_name(self) -> str:
        return _LEVEL_DISPLAY[self]

    @property
    def logging_level(self) -> int:
        """Der Zahlenwert fuer :mod:`logging` (``INFO`` -> 20)."""
        return logging.getLevelNamesMapping()[self.value]


# Anzeigenamen der Logansicht (ARCHITECTURE §9: UI-Sprache Deutsch).
_LEVEL_DISPLAY: dict[EventLevel, str] = {
    EventLevel.DEBUG: "Debug",
    EventLevel.INFO: "Info",
    EventLevel.WARNING: "Warnung",
    EventLevel.ERROR: "Fehler",
    EventLevel.CRITICAL: "Kritisch",
}


@dataclass(frozen=True, slots=True)
class Event:
    """Eine Zeile aus ``event_log``."""

    id: int
    ts: str
    level: EventLevel
    source: str
    message: str
    extra: dict[str, Any]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Event:
        raw = row["extra"]
        return cls(
            id=int(row["id"]),
            ts=str(row["ts"]),
            level=EventLevel(row["level"]),
            source=str(row["source"]),
            message=str(row["message"]),
            extra=json.loads(raw) if raw else {},
        )

    def as_dict(self) -> dict[str, Any]:
        """Antwort-/Template-taugliche Form (Phase 24 „Letzte Ereignisse")."""
        return {
            "id": self.id,
            "ts": self.ts,
            "level": self.level.value,
            "level_display": self.level.display_name,
            "source": self.source,
            "message": self.message,
            "extra": self.extra,
        }


def log_event(
    db: Database,
    level: EventLevel,
    source: str,
    message: str,
    extra: dict[str, Any] | None = None,
    *,
    limit: int = EVENT_LOG_LIMIT,
) -> Event:
    """Schreibt ein Ereignis und haelt das Log auf ``limit`` Eintraege.

    Schreiben und Beschneiden liegen in **einer** Transaktion: es gibt
    keinen Moment, in dem das Log ueber der Grenze steht.

    Args:
        db: Offene Zustandsdatenbank.
        level: Ereignis-Level.
        source: Woher das Ereignis stammt (z. B. ``"watchdog"``,
            ``"scheduler"``, ``"importer"``) — Filterkriterium der
            Logansicht.
        message: Deutscher Klartext fuer die Admin-UI.
        extra: Zusatzfelder; werden als JSON abgelegt.
        limit: Ringpuffer-Grenze; ``0`` oder kleiner schaltet das
            Beschneiden ab (fuer Tests und Sonderfaelle).

    Returns:
        Das geschriebene Ereignis samt vergebener ``id``.
    """
    payload = json.dumps(extra, ensure_ascii=False, default=str) if extra else None
    ts = utc_now()

    with db.transaction() as tx:
        cursor = tx.execute(
            "INSERT INTO event_log (ts, level, source, message, extra) VALUES (?, ?, ?, ?, ?)",
            (ts, level.value, source, message, payload),
        )
        event_id = int(cursor.lastrowid or 0)
        _trim(tx, limit)

    # Zusaetzlich ins Containerlog. Die Anwendungsfelder tragen ein Praefix
    # und liegen gebuendelt unter `event_*` — die LogRecord-Standardnamen
    # (`message`, `levelname`, …) sind fuer `extra` gesperrt (LEARNINGS
    # „reservierte LogRecord-Feldnamen in extra").
    _LOG.log(
        level.logging_level,
        message,
        extra={"event_id": event_id, "event_source": source, "event_extra": extra or {}},
    )
    return Event(id=event_id, ts=ts, level=level, source=source, message=message, extra=extra or {})


def recent_events(
    db: Database,
    *,
    limit: int = 50,
    level: EventLevel | None = None,
    source: str | None = None,
) -> list[Event]:
    """Die juengsten Ereignisse, neueste zuerst.

    Args:
        db: Offene Zustandsdatenbank.
        limit: Hoechstzahl gelieferter Eintraege.
        level: Nur dieses Level (Filter der Logansicht).
        source: Nur diese Quelle.
    """
    clauses: list[str] = []
    values: list[Any] = []
    if level is not None:
        clauses.append("level = ?")
        values.append(level.value)
    if source is not None:
        clauses.append("source = ?")
        values.append(source)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    values.append(limit)

    with db.transaction() as tx:
        rows = tx.execute(
            f"SELECT id, ts, level, source, message, extra FROM event_log{where}"
            " ORDER BY id DESC LIMIT ?",
            values,
        ).fetchall()
    return [Event.from_row(row) for row in rows]


def count_events(db: Database) -> int:
    """Wie viele Ereignisse gerade gespeichert sind."""
    with db.transaction() as tx:
        row = tx.execute("SELECT COUNT(*) FROM event_log").fetchone()
    return int(row[0])


def _trim(tx: sqlite3.Connection, limit: int) -> None:
    """Alles ausserhalb der ``limit`` juengsten Eintraege loeschen.

    Bewusst ueber die sortierte Unterabfrage und nicht ueber
    ``id <= MAX(id) - limit``: die Nummernfolge darf Luecken haben
    (zurueckgerollte Transaktionen vergeben ``AUTOINCREMENT``-Nummern
    trotzdem), und dann waere die Rechnung um genau diese Luecken zu
    grosszuegig.
    """
    if limit <= 0:
        return
    tx.execute(
        "DELETE FROM event_log WHERE id NOT IN (SELECT id FROM event_log ORDER BY id DESC LIMIT ?)",
        (limit,),
    )
