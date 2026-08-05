"""Backup-Job: ``python -m acoustid_importer.backup`` (K9, ARCHITECTURE §6).

    python -m acoustid_importer.backup --target /backup \\
        --report /config/jobs/backup.json

Gesichert wird genau das, was sich **nicht wiederherstellen laesst**:

=====================  =====================================================
``local_submission``   Die eigenen Einreichungen. Sie stehen nirgends sonst
                       — der Delta-Strom kennt sie nicht, und upstream
                       liegen sie bestenfalls unter fremden IDs (§5.2).
``watchdog.sqlite3``   API-Keys, Admin-Login, Lauf-Historie, Ereignis-Log.
``config.yaml``        Alle Laufzeit-Einstellungen samt Zugaengen (§6).
=====================  =====================================================

**Nicht** gesichert wird der Rest, und zwar mit Absicht: der
AcoustID-Bestand kommt aus den Tagesdeltas, der Suchindex aus dem Bestand,
und der **Lookup-Cache gehoert ausdruecklich nicht ins Backup** — er haelt
nichts, was sich nicht neu berechnen liesse, und wuerde die Sicherung um
Hunderte Megabyte aufblaehen (M2.5-Aufgabenliste, DECISIONS 2026-07-25
„Backup nur fuer lokale Unikate").

``backup.include_covers`` (Default ``false``, v2 §6.12) steht schon im
Schema; die Cover-Ablage entsteht erst mit M4. Bis dahin vermerkt der Job
den Schalter im Manifest und meldet als Warnung, dass er noch nichts tut.

**Ein Verzeichnis je Lauf**, atomar: geschrieben wird nach
``<ziel>/backup-<YYYYmmdd-HHMMSS>.part``, und erst der fertige Satz wird
umbenannt. Ein abgebrochener Lauf hinterlaesst damit kein Verzeichnis, das
wie eine gueltige Sicherung aussieht.

**Aufbewahrung ist Sache des Betreibers** (docs/backup-restore.md): der
Job loescht nie etwas. Eine automatische Rotation koennte im Fehlerfall
die letzte gute Sicherung mitnehmen — das ist genau die Sorte
Automatismus, die man nachts nicht will.

Wie beim Importer: strukturiertes Log auf **stderr**, Report auf stdout
oder per ``--report`` in eine Datei; Exit-Code und Report sind der Vertrag
mit dem Waechter (E10).
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from shared.env import EnvError, EnvSettings
from shared.logging_setup import setup_logging

__all__ = ["REPORT_SCHEMA", "STATE_DB_FILENAME", "main", "run_backup"]

_LOG = logging.getLogger(__name__)

#: Dienstname im strukturierten Log.
SERVICE_NAME: Final = "acoustid-backup"

#: Version des Report- und Manifest-Formats.
REPORT_SCHEMA: Final = "musicmeta-offline/backup/1"

#: Dateiname der Wächter-Zustandsdatenbank im Datenverzeichnis. Bewusst
#: hier wiederholt und nicht aus ``acoustid_watchdog`` importiert: der
#: Importer haengt nicht vom Waechter-Paket ab (und soll es nicht). Ein
#: Test haelt beide Namen aneinander.
STATE_DB_FILENAME: Final = "watchdog.sqlite3"

#: Der Lookup-Cache — hier nur, um ihn ausdruecklich **auszuschliessen**.
#: Ein Name in einer Sperrliste ist nachweisbar; ein weggelassener nicht.
EXCLUDED_FILENAMES: Final = ("lookup-cache.sqlite3",)

#: Tabelle mit den lokalen Unikaten (§5.2).
SUBMISSION_TABLE: Final = "local_submission"

#: ``--report -`` schreibt auf stdout.
STDOUT: Final = "-"

#: Exit-Codes wie beim Importer (docs/importer-job.md), damit der Waechter
#: sie einheitlich liest.
EXIT_OK: Final = 0
EXIT_ERROR: Final = 1
EXIT_USAGE: Final = 2


# --- Einzelschritte ---------------------------------------------------------


def _submission_columns(connection: Any) -> list[str]:
    """Spalten von ``local_submission`` in Tabellenreihenfolge.

    Der Restore-Weg braucht die Liste ausdruecklich
    (``COPY local_submission (…) FROM …``): ohne sie waere ein Backup nach
    einer spaeteren Schema-Erweiterung nicht mehr einspielbar, und zwar
    ohne Fehlermeldung — die Spalten wuerden nur verschoben.
    """
    rows = connection.execute(
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_name = %s AND table_schema = current_schema()"
        " ORDER BY ordinal_position",
        (SUBMISSION_TABLE,),
    ).fetchall()
    return [str(row[0]) for row in rows]


def _sequence_values(connection: Any) -> dict[str, int]:
    """Stand der beiden Sequenzen hinter ``local_submission``.

    Ohne sie vergaebe eine wiederhergestellte Instanz Doc-IDs, die es
    schon gibt — und der Suchindex bekaeme zwei Fingerprints unter
    derselben Nummer (§5.3, reservierter Bereich [2^31, 2^32-1]).
    """
    values: dict[str, int] = {}
    for name in ("local_submission_track_id_seq",):
        row = connection.execute(f"SELECT last_value FROM {name}").fetchone()
        values[name] = int(row[0])
    row = connection.execute(
        "SELECT pg_get_serial_sequence(%s, 'id')", (SUBMISSION_TABLE,)
    ).fetchone()
    identity = row[0]
    if identity:
        last = connection.execute(f"SELECT last_value FROM {identity}").fetchone()
        values[str(identity).split(".")[-1]] = int(last[0])
    return values


def _dump_submissions(connection: Any, target: Path) -> dict[str, Any]:
    """Schreibt ``local_submission`` als gzip-komprimierten COPY-Text.

    COPY-Text und nicht CSV oder JSON: er ist das Format, das
    ``COPY … FROM`` ohne Umweg wieder einliest — inklusive der
    ``integer[]``-Vektoren, die als ``{1,2,3}`` heil durch beide
    Richtungen gehen.

    Returns:
        Den Manifest-Block zu dieser Datei.
    """
    exists = connection.execute("SELECT to_regclass(%s)", (SUBMISSION_TABLE,)).fetchone()[0]
    if exists is None:
        # Eine frische Instanz vor dem Bootstrap hat die Tabelle noch nicht.
        # Das ist kein Fehler — ein „fehlgeschlagenes Backup" bei jeder
        # neuen Instanz waere ein Fehlalarm, der die Meldung entwertet.
        _LOG.warning("Tabelle local_submission existiert nicht — nichts zu sichern")
        return {"file": None, "rows": None, "columns": [], "sequences": {}, "present": False}

    columns = _submission_columns(connection)
    column_list = ", ".join(columns)
    rows = 0
    with (
        gzip.open(target, "wb") as archive,
        connection.cursor() as cursor,
        cursor.copy(
            f"COPY (SELECT {column_list} FROM {SUBMISSION_TABLE} ORDER BY id) TO STDOUT"
        ) as copy,
    ):
        for block in copy:
            archive.write(block)
            rows += bytes(block).count(b"\n")
    return {
        "file": target.name,
        "rows": rows,
        "columns": columns,
        "sequences": _sequence_values(connection),
        "present": True,
    }


def _copy_sqlite(source: Path, target: Path) -> dict[str, Any]:
    """Kopiert die Zustandsdatenbank mit der Online-Backup-API von SQLite.

    Eine blosse Dateikopie waere im WAL-Modus nicht konsistent: der
    Waechter schreibt weiter, waehrend der Job liest. ``Connection.backup``
    liefert einen in sich stimmigen Stand, ohne den Schreiber anzuhalten.
    """
    if not source.is_file():
        _LOG.warning("Zustandsdatenbank nicht gefunden", extra={"db_path": str(source)})
        return {"file": None, "present": False}
    with (
        sqlite3.connect(f"file:{source}?mode=ro", uri=True) as origin,
        sqlite3.connect(target) as copy,
    ):
        origin.backup(copy)
    origin.close()
    copy.close()
    return {"file": target.name, "bytes": target.stat().st_size, "present": True}


def _copy_config(source: Path, target: Path) -> dict[str, Any]:
    """Kopiert die ``config.yaml`` samt Dateirechten (sie enthaelt Secrets)."""
    if not source.is_file():
        _LOG.warning("Konfiguration nicht gefunden", extra={"config_path": str(source)})
        return {"file": None, "present": False}
    shutil.copy2(source, target)
    # 0640 wie das Original (§6): die Sicherung enthaelt dieselben Secrets
    # im Klartext und darf nicht offener liegen als die Quelle.
    target.chmod(0o640)
    return {"file": target.name, "bytes": target.stat().st_size, "present": True}


# --- Der Lauf ---------------------------------------------------------------


def run_backup(
    settings: EnvSettings,
    target_dir: Path,
    *,
    include_covers: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fuehrt eine Sicherung durch und liefert den Report.

    Args:
        settings: Bootstrap-Werte (Datenverzeichnis, DB-Zugang).
        target_dir: ``backup.dir`` — das Wurzelverzeichnis der Sicherungen.
        include_covers: ``backup.include_covers``; noch ohne Wirkung (M4).
        now: Zeitpunkt fuer den Verzeichnisnamen (Tests).

    Raises:
        Exception: Jeder Fehler beim Sichern; der Aufrufer macht daraus
            Report und Exit-Code. Das halbfertige ``.part``-Verzeichnis
            bleibt dabei liegen und wird beim naechsten Lauf ersetzt — es
            traegt den Namen nicht, den ein Restore sucht.
    """
    # psycopg wird bewusst erst hier importiert (Modulimport bleibt billig,
    # dieselbe Regel wie im shared-Paket).
    import psycopg

    started_at = now or datetime.now(UTC)
    stamp = started_at.strftime("%Y%m%d-%H%M%S")
    final_dir = target_dir / f"backup-{stamp}"
    work_dir = target_dir / f"backup-{stamp}.part"
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)

    warnings: list[str] = []
    if include_covers:
        warnings.append(
            "backup.include_covers ist gesetzt, die Cover-Ablage entsteht aber erst mit M4 — "
            "es wurden keine Bilder gesichert"
        )
        _LOG.warning(warnings[-1])

    with psycopg.connect(settings.db_dsn().get_secret_value(), autocommit=True) as connection:
        submissions = _dump_submissions(connection, work_dir / "local_submission.copy.gz")
    state = _copy_sqlite(settings.data_dir / STATE_DB_FILENAME, work_dir / STATE_DB_FILENAME)
    config = _copy_config(settings.config_path, work_dir / "config.yaml")

    manifest = {
        "schema": REPORT_SCHEMA,
        "created_at": started_at.isoformat(),
        "table": SUBMISSION_TABLE,
        "local_submission": submissions,
        "state_db": state,
        "config": config,
        "include_covers": include_covers,
        "excluded": list(EXCLUDED_FILENAMES),
        "restore": "docs/backup-restore.md",
    }
    (work_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8"
    )

    if final_dir.exists():  # pragma: no cover - nur bei zwei Laeufen in derselben Sekunde
        shutil.rmtree(final_dir)
    work_dir.rename(final_dir)

    finished_at = datetime.now(UTC)
    total_bytes = sum(item.stat().st_size for item in final_dir.iterdir() if item.is_file())
    _LOG.info(
        "Sicherung abgeschlossen",
        extra={
            "backup_dir": str(final_dir),
            "rows": submissions["rows"],
            "total_bytes": total_bytes,
        },
    )
    return {
        "schema": REPORT_SCHEMA,
        "result": "ok",
        "exit_code": EXIT_OK,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_s": round((finished_at - started_at).total_seconds(), 3),
        "directory": str(final_dir),
        "total_bytes": total_bytes,
        "rows": submissions["rows"],
        "manifest": manifest,
        "warnings": warnings,
        "error": None,
    }


# --- Kommandozeile ----------------------------------------------------------


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m acoustid_importer.backup",
        description=(
            "Sichert local_submission, die Waechter-SQLite und die config.yaml "
            "(K9). Der Lookup-Cache gehoert ausdruecklich nicht dazu."
        ),
    )
    parser.add_argument(
        "--target",
        required=True,
        metavar="PFAD",
        help="Zielverzeichnis der Sicherungen (backup.dir aus der config.yaml)",
    )
    parser.add_argument(
        "--report",
        default=STDOUT,
        metavar="PFAD",
        help=f"Ergebnis-JSON hierhin schreiben ({STDOUT} = stdout, Default)",
    )
    parser.add_argument(
        "--include-covers",
        action="store_true",
        help="backup.include_covers — noch ohne Wirkung (die Cover-Ablage kommt mit M4)",
    )
    return parser.parse_args(argv)


def _emit(document: dict[str, Any], destination: str) -> None:
    """Schreibt den Report nach stdout oder atomar in eine Datei."""
    text = json.dumps(document, indent=2, ensure_ascii=False, default=str) + "\n"
    if destination == STDOUT:
        print(text, end="")
        return
    path = Path(destination)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle_fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.")
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        Path(tmp_name).replace(path)
    except OSError as error:
        _LOG.error(
            "Report liess sich nicht schreiben — Ausgabe auf stdout",
            extra={"report_path": str(path), "reason": str(error)},
        )
        print(text, end="")


def main(argv: Sequence[str] | None = None) -> int:
    """Einstiegspunkt; liefert den Exit-Code."""
    args = _parse_args(argv)
    started_at = datetime.now(UTC)
    try:
        settings = EnvSettings.from_env()
    except EnvError as error:
        print(f"Fehler: {error}", file=sys.stderr)
        return EXIT_USAGE
    setup_logging(SERVICE_NAME, settings.log_level)

    try:
        document = run_backup(
            settings, Path(args.target), include_covers=args.include_covers, now=started_at
        )
    except Exception as error:
        _LOG.exception("Sicherung gescheitert")
        finished_at = datetime.now(UTC)
        _emit(
            {
                "schema": REPORT_SCHEMA,
                "result": "failed",
                "exit_code": EXIT_ERROR,
                "started_at": started_at.isoformat(),
                "finished_at": finished_at.isoformat(),
                "duration_s": round((finished_at - started_at).total_seconds(), 3),
                "directory": None,
                "total_bytes": 0,
                "rows": None,
                "warnings": [],
                "error": {"type": type(error).__name__, "message": str(error)},
            },
            args.report,
        )
        return EXIT_ERROR

    _emit(document, args.report)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
