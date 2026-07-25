"""Transaktionaler Import der Tagesdateien (§5.2 Regeln 1 bis 4, §8.3 und §8.4).

**Eine Tagesdatei ist genau eine Transaktion** — inklusive der
Fortschreibung von ``import_state``. Entweder die Datei ist vollstaendig
drin und als erledigt vermerkt, oder es ist, als haette sie nie
stattgefunden. Daraus folgt beides zugleich:

* **Stale statt Sperre** (Invariante §8.3): waehrend des Imports antworten
  Lookups weiter aus dem alten Bestand; ein Wartungsfenster gibt es nicht.
* **Resume** (Invariante §8.4): nach einem Abbruch steht ``import_state``
  auf der letzten *vollstaendig* eingespielten Datei. Der naechste Lauf
  fragt denselben Zustand ab (:func:`acoustid_importer.state.plan`) und
  macht dort weiter — ohne Duplikate, weil jede Zeile ein Upsert ist, und
  ohne Luecken, weil die abgebrochene Datei wieder offen ist.

**Ablauf je Datei.** ``DeltaReader`` liefert die Records zeilenweise
(Import-Regel 4: direkter JSON-Parse, kein COPY-FROM-Staging), sie werden
in Bloecken von :data:`DEFAULT_BATCH_ROWS` Zeilen per ``executemany`` gegen
das Upsert-Statement des Stroms geschrieben
(:mod:`acoustid_importer.upserts`). Ein Parse-Fehler ist ein harter Abbruch
der Datei-Transaktion — er kommt als Ausnahme aus dem Reader, das
``with conn.transaction()`` rollt zurueck.

**Reihenfolge.** Diese Modul importiert genau die Dateien, die ihm gegeben
werden, in der gegebenen Reihenfolge. Dass die Reihenfolge Import-Regel 1
entspricht (Tage chronologisch, im Tag ``track`` -> ``meta`` ->
``fingerprint`` + ``track_fingerprint`` -> ``track_mbid`` -> ``track_meta``
-> ``track_puid``), stellt die Arbeitsliste sicher — sie ist die einzige
Quelle der Reihenfolge, hier wird nicht nachsortiert.

Beispiel::

    plan = state.plan(conn, today=date.today())
    plan.raise_on_gaps()
    for result in import_files(conn, (downloader.path_for(f), f) for f in plan.files):
        print(result.file.name, result.rows)
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date
from itertools import batched
from pathlib import Path
from typing import Any, Final

from psycopg import Connection
from psycopg import pq as psycopg_pq

from acoustid_importer import state
from acoustid_importer.errors import DbImportError
from acoustid_importer.parser import DeltaReader, Escaping
from acoustid_importer.streams import DeltaFile
from acoustid_importer.upserts import Upsert, upsert_for

__all__ = [
    "DEFAULT_BATCH_ROWS",
    "FileImport",
    "import_file",
    "import_files",
]

_LOG = logging.getLogger(__name__)

#: Zeilen je ``executemany``. Gross genug, damit der Aufwand je Block nicht
#: ins Gewicht faellt, klein genug, dass ein Block des Fingerprint-Stroms
#: (~1300 int32 je Zeile) im Speicher unauffaellig bleibt.
DEFAULT_BATCH_ROWS: Final = 1000


@dataclass(frozen=True, slots=True)
class FileImport:
    """Ergebnis eines :func:`import_file`."""

    file: DeltaFile
    path: Path
    #: Eingespielte Records — der Wert, der in ``import_state.row_count`` steht.
    rows: int
    #: Gelesene Zeilen der entpackten Datei (inkl. Leerzeilen).
    lines: int
    #: Zahl der ``executemany``-Bloecke.
    batches: int
    #: Groesse der gz-Datei in Byte.
    file_size: int
    duration_s: float
    #: Datei war laut ``import_state`` schon erledigt und wurde uebersprungen.
    skipped: bool = False
    #: Zeilen, die erst in der anderen Escaping-Lesart lesbar waren (§5.1).
    escaping_fallbacks: int = 0
    #: Unbekannte Felder mit ihrer Trefferzahl (§12.8).
    unknown_fields: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        """Leere Tagesdatei — regulaer, keine Luecke (§5.1)."""
        return self.rows == 0


def import_file(
    conn: Connection,
    path: Path,
    *,
    file: DeltaFile | None = None,
    batch_rows: int = DEFAULT_BATCH_ROWS,
    escaping: Escaping = Escaping.AUTO,
    skip_done: bool = True,
) -> FileImport:
    """Spielt genau eine Tagesdatei in einer Transaktion ein.

    Args:
        conn: Verbindung zur AcoustID-Postgres **ohne offene Transaktion**.
            Mit offener Transaktion waere das ``with conn.transaction()``
            hier nur ein Savepoint, und „eine Datei = eine Transaktion"
            haenge am Commit des Aufrufers.
        path: Die gz-Datei im Arbeitsverzeichnis.
        file: Tag und Strom; per Vorgabe aus dem Dateinamen.
        batch_rows: Zeilen je ``executemany``.
        escaping: Lesart der Zeilen; per Vorgabe nach Datei-Epoche (§5.1).
        skip_done: Bereits erledigte Dateien ueberspringen. Das ist der
            Resume-Fall; mit ``False`` wird die Datei erneut eingespielt
            (idempotent, weil jede Zeile ein Upsert ist).

    Returns:
        :class:`FileImport` mit Zeilenzahlen und Dauer.

    Raises:
        DbImportError: Die Verbindung hat eine offene Transaktion, oder ein
            Record verletzt eine Zusicherung des Datenmodells.
        ParseError: Eine Zeile ist unlesbar (Import-Regel 4) — die
            Transaktion ist dann zurueckgerollt.
        ValueError: ``batch_rows`` kleiner als 1 oder der Dateiname passt
            nicht zum Schema.
        psycopg.Error: Fehler der Datenbank; ebenfalls mit Rollback.
    """
    if batch_rows < 1:
        raise ValueError(f"batch_rows muss mindestens 1 sein, war {batch_rows}")
    delta = DeltaFile.from_name(path.name) if file is None else file
    _require_no_open_transaction(conn)

    upsert = upsert_for(delta.stream)
    file_size = path.stat().st_size
    reader = DeltaReader(path, stream=delta.stream, day=delta.day, escaping=escaping)
    started = time.monotonic()

    with conn.transaction():
        if skip_done and state.is_done(conn, delta):
            _LOG.debug(
                "Tagesdatei ist laut import_state erledigt — uebersprungen",
                extra={"delta_file": delta.name, "stream": delta.stream.value},
            )
            return FileImport(
                file=delta,
                path=path,
                rows=0,
                lines=0,
                batches=0,
                file_size=file_size,
                duration_s=time.monotonic() - started,
                skipped=True,
            )

        state.begin(conn, delta, file_name=path.name, file_size=file_size)
        rows = 0
        batches = 0
        with conn.cursor() as cursor:
            for block in batched(_rows(reader, upsert, delta.day), batch_rows, strict=False):
                cursor.executemany(upsert.statement, block)
                rows += len(block)
                batches += 1
        state.finish(conn, delta, row_count=rows)

    duration_s = time.monotonic() - started
    stats = reader.stats
    _LOG.info(
        "Tagesdatei eingespielt",
        extra={
            "delta_file": delta.name,
            "stream": delta.stream.value,
            "day": delta.day.isoformat(),
            "rows": rows,
            "batches": batches,
            "duration_s": round(duration_s, 3),
            "escaping_fallbacks": stats.escaping_fallbacks,
        },
    )
    return FileImport(
        file=delta,
        path=path,
        rows=rows,
        lines=stats.lines,
        batches=batches,
        file_size=file_size,
        duration_s=duration_s,
        escaping_fallbacks=stats.escaping_fallbacks,
        unknown_fields=tuple(sorted(stats.unknown_fields)),
    )


def import_files(
    conn: Connection,
    files: Iterable[Path | tuple[Path, DeltaFile]],
    *,
    batch_rows: int = DEFAULT_BATCH_ROWS,
    escaping: Escaping = Escaping.AUTO,
    skip_done: bool = True,
) -> Iterator[FileImport]:
    """Spielt mehrere Tagesdateien der Reihe nach ein — je eine Transaktion.

    Bewusst ein Iterator: der Aufrufer sieht jedes Ergebnis sofort (Log,
    Fortschrittsanzeige) und kann jederzeit abbrechen; alles bereits
    Eingespielte bleibt committet. Ein Fehler bricht die Schleife ab —
    genau die Stelle, an der ``import_state`` stehen bleibt und der
    naechste Lauf wieder ansetzt.

    Args:
        files: Pfade, wahlweise mit der zugehoerigen :class:`DeltaFile`
            (sonst wird sie aus dem Dateinamen abgeleitet). Die Reihenfolge
            wird **nicht** angetastet — sie kommt aus der Arbeitsliste.
    """
    for item in files:
        path, delta = item if isinstance(item, tuple) else (item, None)
        yield import_file(
            conn,
            path,
            file=delta,
            batch_rows=batch_rows,
            escaping=escaping,
            skip_done=skip_done,
        )


# --- Innenleben -------------------------------------------------------------


def _rows(reader: DeltaReader, upsert: Upsert, src_day: date) -> Iterator[tuple[Any, ...]]:
    """Records -> Parameterlisten des Upserts.

    Die Aufloesungen (``reader``, ``upsert.read``) stehen bewusst vor der
    Schleife: der Fingerprint-Strom laeuft hier milliardenfach durch.
    """
    read = upsert.read
    for record in reader:
        yield (*read(record), src_day)


def _require_no_open_transaction(conn: Connection) -> None:
    """Sichert zu, dass ``with conn.transaction()`` wirklich eine wird.

    Innerhalb einer bereits offenen Transaktion waere sie nur ein
    Savepoint — die Datei waere dann nicht mehr die Einheit des Commits,
    und ein Abbruch koennte halb eingespielte Tage sichtbar machen.
    """
    status = conn.pgconn.transaction_status
    if status != psycopg_pq.TransactionStatus.IDLE:
        raise DbImportError(
            "Die Verbindung hat eine offene Transaktion "
            f"(Status {psycopg_pq.TransactionStatus(status).name}). Eine Tagesdatei "
            "muss eine eigene Transaktion bekommen (ARCHITECTURE §8.3) — bitte eine "
            "frische oder committete Verbindung uebergeben."
        )
