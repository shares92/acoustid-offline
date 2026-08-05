"""Ergebnis eines Importer-Laufs: Exit-Codes und maschinenlesbarer Report.

Der Importer ist ein **One-Shot-Job**: er laeuft, sagt in einer Zahl, wie es
ausging, und beschreibt in einem JSON-Dokument, was passiert ist. Beides
zusammen ist die Schnittstelle zum Waechter, der daraus in Phase 19 die
Tabelle ``update_run`` fuellt (ARCHITECTURE §5, „SQLite (Waechter)").

**Exit-Codes** (:class:`ExitCode`) trennen die Faelle, die der Waechter
unterschiedlich behandeln muss: ein Plattenplatz-Abbruch (§8.8) ist etwas
anderes als ein Netzfehler, und beide sind etwas anderes als eine Luecke in
der Historie (Import-Regel 5), die niemand automatisch reparieren darf.
Jeder Code hat genau ein Ergebnis (:class:`RunResult`) und umgekehrt.

**Der Report** (:class:`RunReport`) ist ein flaches, versioniertes
JSON-Dokument (:data:`REPORT_SCHEMA`). Er entsteht **immer** — auch im
Fehlerfall, auch beim Abbruch durch ein Signal. Wo er hingeschrieben wird,
entscheidet der Aufrufer (stdout oder Datei, siehe
:mod:`acoustid_importer.__main__`); das Format ist in ``docs/importer-job.md``
dokumentiert.

**Die Hochrechnung** (:func:`project`) ist der Kern des Probelaufs: aus
gemessenem Durchsatz und gemessenem Platzverbrauch eines begrenzten
Zeitraums wird der Vollbestand (:data:`HISTORY_GZ_BYTES`) hochgerechnet.
Bewusst eine pure Funktion — sie laesst sich mit den Messwerten eines
echten Laufs auch nachtraeglich neu rechnen, wenn sich die Bezugsgroesse
aendert (die Historie waechst taeglich weiter).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:  # nur fuer die Typannotation — hier soll kein psycopg laden
    from acoustid_importer.dbimport import FileImport
    from acoustid_importer.download import Download
    from acoustid_importer.indexfeed import IndexFeedReport
    from acoustid_importer.worklist import Gap

__all__ = [
    "HISTORY_GZ_BYTES",
    "REPORT_SCHEMA",
    "RESULT_BY_EXIT_CODE",
    "ErrorInfo",
    "ExitCode",
    "FileCounts",
    "JobMode",
    "Projection",
    "RunReport",
    "RunResult",
    "RunTally",
    "build_report",
    "project",
]

#: Version des Report-Formats. Aendert sich das Dokument unvertraeglich,
#: steigt die Zahl — der Waechter kann dann sauber ablehnen.
REPORT_SCHEMA: Final = "acoustid-offline/importer-run/1"

#: Volumen der gesamten Historie in gz-Byte (ARCHITECTURE §5.1, Stand
#: 2026-07-25: 38.178 Dateien, 414 GB, SI). Bezugsgroesse der Hochrechnung;
#: die Historie waechst um ~58 MB/Tag, deshalb ist der Wert ueberschreibbar.
HISTORY_GZ_BYTES: Final = 414 * 10**9


class JobMode(StrEnum):
    """Betriebsart des Laufs."""

    #: Erst-Import: Voll-Replay ab 2011-08-19, Bulk-Modus, Indizes nachziehen.
    BOOTSTRAP = "bootstrap"
    #: Taeglicher Delta-Import im laufenden Betrieb.
    UPDATE = "update"


class ExitCode(IntEnum):
    """Exit-Codes des Jobs — der Vertrag mit dem Waechter (Phase 19)."""

    #: Alles eingespielt (auch: nichts zu tun).
    OK = 0
    #: Unerwarteter Fehler — ein Fall, den der Job nicht vorgesehen hat.
    ERROR = 1
    #: Aufruf- oder Umgebungsfehler (CLI-Argumente, `MMO_`-Variablen, Config).
    USAGE = 2
    #: Plattenplatz-Guard hat abgebrochen (Invariante §8.8).
    DISK_GUARD = 3
    #: Eine Tagesdatei kam nicht sauber vom Server.
    DOWNLOAD = 4
    #: Luecke in der Historie (Import-Regel 5) — nie automatisch reparieren.
    GAPS = 5
    #: Import in die Postgres gescheitert (inkl. Parse-Fehler).
    IMPORT = 6
    #: Index-Feed gescheitert.
    INDEX_FEED = 7
    #: Auf Wunsch beendet (SIGTERM/SIGINT); der Stand ist resumierbar.
    ABORTED = 8


class RunResult(StrEnum):
    """Ergebnis eines Laufs; die Textform steht im Report."""

    OK = "ok"
    ABORTED = "aborted"
    DISK_GUARD = "disk_guard"
    GAPS = "gaps"
    DOWNLOAD_FAILED = "download_failed"
    IMPORT_FAILED = "import_failed"
    INDEX_FEED_FAILED = "index_feed_failed"
    USAGE_ERROR = "usage_error"
    FAILED = "failed"

    @property
    def exit_code(self) -> ExitCode:
        """Der zugehoerige Exit-Code."""
        return _EXIT_CODES[self]

    @property
    def ok(self) -> bool:
        """Lief der Job durch?"""
        return self is RunResult.OK


_EXIT_CODES: Final[Mapping[RunResult, ExitCode]] = {
    RunResult.OK: ExitCode.OK,
    RunResult.ABORTED: ExitCode.ABORTED,
    RunResult.DISK_GUARD: ExitCode.DISK_GUARD,
    RunResult.GAPS: ExitCode.GAPS,
    RunResult.DOWNLOAD_FAILED: ExitCode.DOWNLOAD,
    RunResult.IMPORT_FAILED: ExitCode.IMPORT,
    RunResult.INDEX_FEED_FAILED: ExitCode.INDEX_FEED,
    RunResult.USAGE_ERROR: ExitCode.USAGE,
    RunResult.FAILED: ExitCode.ERROR,
}

#: Umkehrung — jeder Code gehoert zu genau einem Ergebnis.
RESULT_BY_EXIT_CODE: Final[Mapping[ExitCode, RunResult]] = {
    code: result for result, code in _EXIT_CODES.items()
}


@dataclass(frozen=True, slots=True)
class ErrorInfo:
    """Der Fehler, der den Lauf beendet hat."""

    #: Klassenname der Ausnahme (z. B. ``DiskSpaceError``).
    type: str
    message: str

    @classmethod
    def of(cls, error: BaseException) -> ErrorInfo:
        return cls(type=type(error).__name__, message=str(error))

    def as_dict(self) -> dict[str, Any]:
        return {"type": self.type, "message": self.message}


@dataclass(frozen=True, slots=True)
class FileCounts:
    """Dateizahlen eines Laufs."""

    #: Dateien laut Arbeitsliste (nach Begrenzung durch ``--end-date`` u. Ae.).
    planned: int = 0
    #: Tatsaechlich eingespielte Dateien.
    imported: int = 0
    #: Laut ``import_state`` bereits erledigt und deshalb uebersprungen.
    skipped: int = 0
    #: Frisch geladen (der Rest lag schon im Arbeitsverzeichnis).
    downloaded: int = 0
    #: Aus einem ``.part`` fortgesetzt.
    resumed: int = 0
    #: Leere Tagesdateien — regulaer, keine Luecke (§5.1).
    empty: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "planned": self.planned,
            "imported": self.imported,
            "skipped": self.skipped,
            "downloaded": self.downloaded,
            "resumed": self.resumed,
            "empty": self.empty,
        }


@dataclass(frozen=True, slots=True)
class Projection:
    """Hochrechnung des Probelaufs auf den Vollbestand."""

    #: Bezugsgroesse (:data:`HISTORY_GZ_BYTES` oder ein eigener Wert).
    total_gz_bytes: int
    #: Im Lauf verarbeitete gz-Bytes.
    measured_gz_bytes: int
    #: Dauer des reinen Imports (ohne Migrationen und Index-Feed).
    measured_duration_s: float
    #: Anteil des Vollbestands, den der Lauf abgedeckt hat (0 bis 1).
    coverage: float
    #: Gemessener Durchsatz in gz-Byte je Sekunde.
    throughput_gz_bytes_s: float | None
    #: Hochgerechnete Dauer des Vollimports in Sekunden.
    estimated_total_duration_s: float | None
    #: Zuwachs der Datenbank im Lauf.
    measured_db_bytes: int | None
    #: Hochgerechnete Groesse der Datenbank nach dem Vollimport.
    estimated_db_bytes: int | None
    #: Zuwachs der Dokumentzahl im Suchindex.
    measured_index_documents: int | None
    #: Hochgerechnete Dokumentzahl nach dem Vollimport.
    estimated_index_documents: int | None
    #: Zuwachs des Index-Datenverzeichnisses (nur wenn gemountet).
    measured_index_bytes: int | None = None
    #: Hochgerechnete Groesse des Index-Datenverzeichnisses.
    estimated_index_bytes: int | None = None

    @property
    def estimated_total_hours(self) -> float | None:
        """Hochgerechnete Dauer in Stunden — die Zahl fuer die Bootstrap-Doku."""
        if self.estimated_total_duration_s is None:
            return None
        return self.estimated_total_duration_s / 3600

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_gz_bytes": self.total_gz_bytes,
            "measured_gz_bytes": self.measured_gz_bytes,
            "measured_duration_s": round(self.measured_duration_s, 3),
            "coverage": round(self.coverage, 9),
            "throughput_gz_bytes_s": _round(self.throughput_gz_bytes_s, 1),
            "estimated_total_duration_s": _round(self.estimated_total_duration_s, 0),
            "estimated_total_hours": _round(self.estimated_total_hours, 2),
            "measured_db_bytes": self.measured_db_bytes,
            "estimated_db_bytes": self.estimated_db_bytes,
            "measured_index_documents": self.measured_index_documents,
            "estimated_index_documents": self.estimated_index_documents,
            "measured_index_bytes": self.measured_index_bytes,
            "estimated_index_bytes": self.estimated_index_bytes,
        }


def project(
    *,
    measured_gz_bytes: int,
    measured_duration_s: float,
    total_gz_bytes: int = HISTORY_GZ_BYTES,
    measured_db_bytes: int | None = None,
    measured_index_documents: int | None = None,
    measured_index_bytes: int | None = None,
) -> Projection:
    """Rechnet die Messwerte linear auf den Vollbestand hoch (pure).

    Die Annahme ist bewusst einfach: Dauer und Platzbedarf wachsen linear
    mit den verarbeiteten gz-Byte. Das ist fuer den Fingerprint-Strom (94 %
    des Volumens) belegbar gut und fuer die Planung genau genug — es geht um
    die Groessenordnung „ein Tag" gegen „eine Woche", nicht um Minuten.

    Wo eine Bezugsgroesse fehlt (kein Byte verarbeitet, keine Zeit vergangen,
    keine DB-Messung), bleibt das jeweilige Ergebnis ``None`` statt einer
    erfundenen Zahl.

    Args:
        measured_gz_bytes: Im Lauf verarbeitete gz-Bytes.
        measured_duration_s: Dauer des Imports in Sekunden.
        total_gz_bytes: Bezugsgroesse des Vollbestands.
        measured_db_bytes: Zuwachs der Datenbank im Lauf.
        measured_index_documents: Zuwachs der Dokumentzahl im Index.
        measured_index_bytes: Zuwachs des Index-Datenverzeichnisses.

    Raises:
        ValueError: ``total_gz_bytes`` ist kleiner als 1.
    """
    if total_gz_bytes < 1:
        raise ValueError(f"total_gz_bytes muss mindestens 1 sein, war {total_gz_bytes}")

    coverage = measured_gz_bytes / total_gz_bytes
    factor = (total_gz_bytes / measured_gz_bytes) if measured_gz_bytes > 0 else None
    throughput = (
        measured_gz_bytes / measured_duration_s
        if measured_gz_bytes > 0 and measured_duration_s > 0
        else None
    )
    return Projection(
        total_gz_bytes=total_gz_bytes,
        measured_gz_bytes=measured_gz_bytes,
        measured_duration_s=measured_duration_s,
        coverage=coverage,
        throughput_gz_bytes_s=throughput,
        estimated_total_duration_s=(
            total_gz_bytes / throughput if throughput is not None else None
        ),
        measured_db_bytes=measured_db_bytes,
        estimated_db_bytes=_scaled(measured_db_bytes, factor),
        measured_index_documents=measured_index_documents,
        estimated_index_documents=_scaled(measured_index_documents, factor),
        measured_index_bytes=measured_index_bytes,
        estimated_index_bytes=_scaled(measured_index_bytes, factor),
    )


@dataclass(slots=True)
class RunTally:
    """Mitschrift eines laufenden Jobs — die Rohdaten des Reports.

    Bewusst veraenderlich: der Job zaehlt hier mit, waehrend er laeuft, und
    baut daraus am Ende den unveraenderlichen :class:`RunReport` (auch im
    Fehlerfall, deshalb muss der Zwischenstand jederzeit vollstaendig sein).
    """

    files_planned: int = 0
    imported: int = 0
    skipped: int = 0
    empty: int = 0
    downloaded: int = 0
    resumed: int = 0
    rows: int = 0
    #: Strom -> eingespielte Zeilen.
    rows_by_stream: dict[str, int] = field(default_factory=dict)
    #: Strom -> eingespielte Dateien.
    files_by_stream: dict[str, int] = field(default_factory=dict)
    #: gz-Bytes der eingespielten Dateien (Bezugsgroesse der Hochrechnung).
    gz_bytes: int = 0
    #: gz-Bytes, die tatsaechlich ueber die Leitung kamen.
    downloaded_bytes: int = 0
    #: Reine Importdauer (Summe der Datei-Transaktionen).
    import_duration_s: float = 0.0
    first_day: date | None = None
    last_day: date | None = None
    #: Zeilen, die erst in der anderen Escaping-Lesart lesbar waren (§5.1).
    escaping_fallbacks: int = 0
    #: Unbekannte Felder aus dem Sanity-Check (§12.8).
    unknown_fields: set[str] = field(default_factory=set)

    def add_download(self, download: Download) -> None:
        """Zaehlt ein Ergebnis des Downloaders mit."""
        self.downloaded_bytes += 0 if download.reused else download.size
        if not download.reused:
            self.downloaded += 1
        if download.resumed:
            self.resumed += 1

    def add_import(self, result: FileImport) -> None:
        """Zaehlt ein Ergebnis von :func:`~acoustid_importer.dbimport.import_file` mit."""
        stream = result.file.stream.value
        if result.skipped:
            self.skipped += 1
            return
        self.imported += 1
        self.rows += result.rows
        self.rows_by_stream[stream] = self.rows_by_stream.get(stream, 0) + result.rows
        self.files_by_stream[stream] = self.files_by_stream.get(stream, 0) + 1
        self.gz_bytes += result.file_size
        self.import_duration_s += result.duration_s
        self.escaping_fallbacks += result.escaping_fallbacks
        self.unknown_fields.update(result.unknown_fields)
        if result.empty:
            self.empty += 1
        day = result.file.day
        self.first_day = day if self.first_day is None else min(self.first_day, day)
        self.last_day = day if self.last_day is None else max(self.last_day, day)

    @property
    def counts(self) -> FileCounts:
        """Der Dateizaehler-Block des Reports."""
        return FileCounts(
            planned=self.files_planned,
            imported=self.imported,
            skipped=self.skipped,
            downloaded=self.downloaded,
            resumed=self.resumed,
            empty=self.empty,
        )


@dataclass(frozen=True, slots=True)
class RunReport:
    """Das Ergebnis-Dokument eines Laufs (Format: ``docs/importer-job.md``)."""

    mode: JobMode
    result: RunResult
    started_at: datetime
    finished_at: datetime
    duration_s: float
    files: FileCounts = field(default_factory=FileCounts)
    rows: int = 0
    rows_by_stream: Mapping[str, int] = field(default_factory=dict)
    files_by_stream: Mapping[str, int] = field(default_factory=dict)
    gz_bytes: int = 0
    downloaded_bytes: int = 0
    import_duration_s: float = 0.0
    first_day: date | None = None
    last_day: date | None = None
    #: Fehlende Tagesdateien der Vergangenheit (Import-Regel 5).
    gaps: tuple[str, ...] = ()
    escaping_fallbacks: int = 0
    unknown_fields: tuple[str, ...] = ()
    #: Zusammenfassung des Index-Feeds; ``None``, wenn er nicht lief.
    index_feed: Mapping[str, Any] | None = None
    #: Messwerte (DB, Index, freier Platz) — Roh-Mapping, damit der Report
    #: waechst, ohne dass das Schema bricht.
    measurements: Mapping[str, Any] = field(default_factory=dict)
    projection: Projection | None = None
    error: ErrorInfo | None = None
    warnings: tuple[str, ...] = ()

    @property
    def exit_code(self) -> ExitCode:
        """Exit-Code des Prozesses."""
        return self.result.exit_code

    def as_dict(self) -> dict[str, Any]:
        """Maschinenlesbare Form — genau das, was als JSON herauskommt."""
        return {
            "schema": REPORT_SCHEMA,
            "mode": self.mode.value,
            "result": self.result.value,
            "exit_code": int(self.exit_code),
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "duration_s": round(self.duration_s, 3),
            "import_duration_s": round(self.import_duration_s, 3),
            "files": self.files.as_dict(),
            "rows": self.rows,
            "rows_by_stream": dict(self.rows_by_stream),
            "files_by_stream": dict(self.files_by_stream),
            "gz_bytes": self.gz_bytes,
            "downloaded_bytes": self.downloaded_bytes,
            "days": {
                "first": self.first_day.isoformat() if self.first_day else None,
                "last": self.last_day.isoformat() if self.last_day else None,
            },
            "gaps": list(self.gaps),
            "escaping_fallbacks": self.escaping_fallbacks,
            "unknown_fields": list(self.unknown_fields),
            "index_feed": dict(self.index_feed) if self.index_feed is not None else None,
            "measurements": dict(self.measurements),
            "projection": self.projection.as_dict() if self.projection is not None else None,
            "error": self.error.as_dict() if self.error is not None else None,
            "warnings": list(self.warnings),
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        """JSON-Text des Reports (UTF-8, unmaskiert)."""
        return json.dumps(self.as_dict(), indent=indent, ensure_ascii=False)

    def write(self, path: Path) -> Path:
        """Schreibt den Report atomar nach ``path``.

        Atomar, weil der Waechter die Datei liest, sobald der Prozess endet —
        eine halb geschriebene Datei waere ein Fehlalarm.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".part")
        tmp.write_text(self.to_json() + "\n", encoding="utf-8")
        tmp.replace(path)
        return path

    def summary(self) -> str:
        """Eine Zeile fuer Menschen — das, was im Log auffallen soll."""
        parts = [
            f"{self.mode.value}: {self.result.value}",
            f"{self.files.imported} Dateien",
            f"{self.rows} Zeilen",
            f"{self.gz_bytes / 10**6:.1f} MB gz",
            f"{self.duration_s:.1f} s",
        ]
        if self.files.skipped:
            parts.append(f"{self.files.skipped} uebersprungen")
        if self.gaps:
            parts.append(f"{len(self.gaps)} Luecken")
        if self.projection is not None and self.projection.estimated_total_hours is not None:
            parts.append(f"Hochrechnung Vollimport ~{self.projection.estimated_total_hours:.1f} h")
        if self.error is not None:
            parts.append(f"Fehler: {self.error.message}")
        return "; ".join(parts)


def build_report(
    *,
    mode: JobMode,
    result: RunResult,
    tally: RunTally,
    started_at: datetime,
    duration_s: float,
    gaps: Iterable[Gap] = (),
    index_feed: IndexFeedReport | None = None,
    measurements: Mapping[str, Any] | None = None,
    projection: Projection | None = None,
    error: BaseException | None = None,
    warnings: Iterable[str] = (),
    finished_at: datetime | None = None,
) -> RunReport:
    """Setzt den Report aus Mitschrift und Ergebnis zusammen."""
    return RunReport(
        mode=mode,
        result=result,
        started_at=started_at,
        finished_at=finished_at or datetime.now(UTC),
        duration_s=duration_s,
        files=tally.counts,
        rows=tally.rows,
        rows_by_stream=dict(tally.rows_by_stream),
        files_by_stream=dict(tally.files_by_stream),
        gz_bytes=tally.gz_bytes,
        downloaded_bytes=tally.downloaded_bytes,
        import_duration_s=tally.import_duration_s,
        first_day=tally.first_day,
        last_day=tally.last_day,
        gaps=tuple(gap.file.name for gap in gaps),
        escaping_fallbacks=tally.escaping_fallbacks,
        unknown_fields=tuple(sorted(tally.unknown_fields)),
        index_feed=_feed_dict(index_feed),
        measurements=dict(measurements or {}),
        projection=projection,
        error=ErrorInfo.of(error) if error is not None else None,
        warnings=tuple(warnings),
    )


# --- Innenleben -------------------------------------------------------------


def _feed_dict(report: IndexFeedReport | None) -> dict[str, Any] | None:
    if report is None:
        return None
    return {
        "documents": report.documents,
        "batches": report.batches,
        "scanned": report.scanned,
        "incomplete": report.incomplete,
        "empty_queries": report.empty_queries,
        "last_id": report.last_id,
        "version": report.version,
        "duration_s": round(report.duration_s, 3),
        "exhausted": report.exhausted,
    }


def _round(value: float | None, digits: int) -> float | int | None:
    if value is None:
        return None
    return round(value, digits) if digits > 0 else round(value)


def _scaled(value: int | None, factor: float | None) -> int | None:
    if value is None or factor is None:
        return None
    return round(value * factor)
