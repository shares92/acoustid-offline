"""Lauf-Historie des Waechters — ``update_run`` (ARCHITECTURE §5).

Eine Zeile je Import- oder Backup-Lauf: wann er begann, wann er endete, wie
viele Tagesdateien und Zeilen eingespielt wurden, wie er ausging und —
falls er scheiterte — woran. Aus dieser Tabelle speisen sich `/status`
(„letzter Update-Lauf" und „Datenstand", ARCHITECTURE §7), die Jobs-Seite
der Admin-UI (Phase 26) und die Kennzahlen (Phase 27).

**Warum der Datenstand hier steht und nicht in der Postgres.** Die letzte
eingespielte Delta-Sequenz kennt eigentlich `import_state` in der
AcoustID-Datenbank — die liegt aber auf dem Array und darf fuer eine
Statusabfrage nicht geweckt werden (Invariante §8.2). Der Waechter
schreibt den Wert deshalb beim Abschluss jedes Laufs aus dem
Importer-Report mit; `/status` liest ausschliesslich diese Kopie.

Gefuellt wird die Tabelle ab Phase 19 (Update-Zyklus) und Phase 21
(Backup). Phase 14 legt das Schema, die Zugriffe und ihre Semantik fest,
damit `/status` seinen Vertrag schon jetzt vollstaendig bedienen kann.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from acoustid_watchdog.store import Database, utc_now

__all__ = [
    "RunKind",
    "RunResult",
    "UpdateRun",
    "finish_run",
    "latest_data_sequence",
    "latest_run",
    "latest_successful_run",
    "running_runs",
    "start_run",
]


class RunKind(StrEnum):
    """Art eines Laufs (Spalte ``kind``)."""

    UPDATE = "update"
    BACKUP = "backup"

    @property
    def display_name(self) -> str:
        return _KIND_DISPLAY[self]


class RunResult(StrEnum):
    """Ausgang eines Laufs (Spalte ``result``; ``NULL`` = laeuft noch).

    ``aborted`` ist der geordnete Abbruch — Plattenplatz-Guard (§8.8) oder
    der Abbrechen-Knopf der Admin-UI (Phase 26); ``failed`` steht fuer alles
    Unfreiwillige.
    """

    SUCCESS = "success"
    FAILED = "failed"
    ABORTED = "aborted"

    @property
    def display_name(self) -> str:
        return _RESULT_DISPLAY[self]


_KIND_DISPLAY: dict[RunKind, str] = {
    RunKind.UPDATE: "Update",
    RunKind.BACKUP: "Backup",
}

_RESULT_DISPLAY: dict[RunResult, str] = {
    RunResult.SUCCESS: "erfolgreich",
    RunResult.FAILED: "fehlgeschlagen",
    RunResult.ABORTED: "abgebrochen",
}


@dataclass(frozen=True, slots=True)
class UpdateRun:
    """Eine Zeile aus ``update_run``."""

    id: int
    kind: RunKind
    started_at: str
    finished_at: str | None
    result: RunResult | None
    files_imported: int
    rows_imported: int
    last_sequence: str | None
    error: str | None
    report: dict[str, Any] | None

    @property
    def running(self) -> bool:
        """Laeuft noch — blockiert den Idle-Stopp (Invariante §8.5)."""
        return self.result is None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> UpdateRun:
        raw_report = row["report"]
        return cls(
            id=int(row["id"]),
            kind=RunKind(row["kind"]),
            started_at=str(row["started_at"]),
            finished_at=row["finished_at"],
            result=RunResult(row["result"]) if row["result"] else None,
            files_imported=int(row["files_imported"]),
            rows_imported=int(row["rows_imported"]),
            last_sequence=row["last_sequence"],
            error=row["error"],
            report=json.loads(raw_report) if raw_report else None,
        )

    def as_dict(self) -> dict[str, Any]:
        """Antwortform fuer `/status` und die Jobs-Seite.

        Der Importer-Report bleibt bewusst draussen: er ist umfangreich und
        gehoert in die Detailansicht (Phase 26), nicht in eine
        Statusantwort, die im Sekundentakt gepollt wird.
        """
        return {
            "id": self.id,
            "kind": self.kind.value,
            "kind_display": self.kind.display_name,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "running": self.running,
            "result": self.result.value if self.result else None,
            "result_display": self.result.display_name if self.result else None,
            "files_imported": self.files_imported,
            "rows_imported": self.rows_imported,
            "last_sequence": self.last_sequence,
            "error": self.error,
        }


_COLUMNS = (
    "id, kind, started_at, finished_at, result, files_imported, "
    "rows_imported, last_sequence, error, report"
)


def start_run(db: Database, kind: RunKind, *, started_at: str | None = None) -> int:
    """Legt einen laufenden Eintrag an und liefert seine ``id``.

    Der Eintrag entsteht **vor** der Arbeit, nicht danach: nur so ist ein
    Lauf, dessen Container abstuerzt, hinterher als angefangen erkennbar
    (und der naechste Zyklus wiederholt ihn, Invariante §8.4).
    """
    with db.transaction() as tx:
        cursor = tx.execute(
            "INSERT INTO update_run (kind, started_at) VALUES (?, ?)",
            (kind.value, started_at or utc_now()),
        )
        return int(cursor.lastrowid or 0)


def finish_run(
    db: Database,
    run_id: int,
    result: RunResult,
    *,
    files_imported: int = 0,
    rows_imported: int = 0,
    last_sequence: str | None = None,
    error: str | None = None,
    report: dict[str, Any] | None = None,
    finished_at: str | None = None,
) -> UpdateRun:
    """Schliesst einen Lauf ab und schreibt sein Ergebnis fort.

    Args:
        last_sequence: Zuletzt vollstaendig eingespielte Delta-Sequenz aus
            dem Importer-Report — der „Datenstand" aus ARCHITECTURE §7.
            Bleibt ``None``, wenn der Lauf nichts eingespielt hat; der
            zuvor erreichte Stand bleibt dann in seiner eigenen Zeile
            stehen und wird von :func:`latest_data_sequence` gefunden.
        report: Vollstaendiger Importer-Report; wird als JSON abgelegt.

    Raises:
        KeyError: Es gibt keinen Lauf mit dieser ``id``.
    """
    with db.transaction() as tx:
        cursor = tx.execute(
            "UPDATE update_run SET finished_at = ?, result = ?, files_imported = ?,"
            " rows_imported = ?, last_sequence = ?, error = ?, report = ?"
            " WHERE id = ?",
            (
                finished_at or utc_now(),
                result.value,
                files_imported,
                rows_imported,
                last_sequence,
                error,
                json.dumps(report, ensure_ascii=False, default=str) if report else None,
                run_id,
            ),
        )
        if cursor.rowcount == 0:
            raise KeyError(f"Kein Lauf mit der id {run_id}")
        row = tx.execute(f"SELECT {_COLUMNS} FROM update_run WHERE id = ?", (run_id,)).fetchone()
    return UpdateRun.from_row(row)


def latest_run(db: Database, kind: RunKind | None = None) -> UpdateRun | None:
    """Der zuletzt begonnene Lauf (optional nur dieser Art)."""
    where, values = ("WHERE kind = ? ", [kind.value]) if kind else ("", [])
    with db.transaction() as tx:
        row = tx.execute(
            f"SELECT {_COLUMNS} FROM update_run {where}ORDER BY id DESC LIMIT 1", values
        ).fetchone()
    return UpdateRun.from_row(row) if row else None


def latest_successful_run(db: Database, kind: RunKind | None = None) -> UpdateRun | None:
    """Der zuletzt begonnene **erfolgreiche** Lauf (optional nur dieser Art)."""
    clauses = ["result = ?"]
    values: list[Any] = [RunResult.SUCCESS.value]
    if kind is not None:
        clauses.append("kind = ?")
        values.append(kind.value)
    with db.transaction() as tx:
        row = tx.execute(
            f"SELECT {_COLUMNS} FROM update_run WHERE {' AND '.join(clauses)}"
            " ORDER BY id DESC LIMIT 1",
            values,
        ).fetchone()
    return UpdateRun.from_row(row) if row else None


def running_runs(db: Database) -> list[UpdateRun]:
    """Alle Laeufe ohne Ergebnis — die Sperre des Idle-Stopps (§8.5)."""
    with db.transaction() as tx:
        rows = tx.execute(
            f"SELECT {_COLUMNS} FROM update_run WHERE result IS NULL ORDER BY id"
        ).fetchall()
    return [UpdateRun.from_row(row) for row in rows]


def latest_data_sequence(db: Database) -> UpdateRun | None:
    """Der juengste Lauf, der einen Datenstand hinterlassen hat.

    Ein erfolgreicher Lauf ohne neue Tagesdateien schreibt keine Sequenz;
    der Datenstand von `/status` ist deshalb der letzte Lauf **mit**
    ``last_sequence``, nicht schlicht der letzte erfolgreiche.
    """
    with db.transaction() as tx:
        row = tx.execute(
            f"SELECT {_COLUMNS} FROM update_run"
            " WHERE kind = ? AND result = ? AND last_sequence IS NOT NULL"
            " ORDER BY id DESC LIMIT 1",
            (RunKind.UPDATE.value, RunResult.SUCCESS.value),
        ).fetchone()
    return UpdateRun.from_row(row) if row else None
