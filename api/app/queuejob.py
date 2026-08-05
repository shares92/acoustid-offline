"""Upstream-Warteschlange als One-Shot-Job: ``python -m acoustid_api.queuejob``.

    python -m acoustid_api.queuejob --report /config/jobs/queue-send.json
    python -m acoustid_api.queuejob --retry            # aufgegebene erneut

**Warum ein eigener Prozess.** Der Waechter ruft den Warteschlangenlauf im
taeglichen Zyklus auf — er darf ihn aber nicht selbst ausfuehren: er haelt
bewusst **keine** Verbindung zum Array (:mod:`acoustid_watchdog.service`,
Invariante §8.2), und ``drain_queue`` braucht eine Postgres-Verbindung.
E10 sieht dafuer genau diesen Weg vor: „Importer/Discogs/Crawler/Backup/
**Queue-Send** laufen als direkte Subprozesse des Waechters (Argumente,
returncode, Report ohne Umweg)."

Die Fachlogik selbst steht unveraendert in :mod:`acoustid_api.upstream`
(Phase 12): :func:`~acoustid_api.upstream.drain_queue` fuer den Regellauf,
:func:`~acoustid_api.upstream.retry_forward` fuer den manuellen
Wiederholungsversuch. Dieses Modul ist nur die Kommandozeile darum herum —
Report und Exit-Code, wie beim Importer.

**Der Report traegt die aufgegebenen Gruppen.** Das Ereignis
``upstream_forward_gave_up`` (§8.9) entsteht im API-Prozess und steht dort
im Log; der Waechter sieht es nie. Damit die Benachrichtigung aus M2.5
trotzdem die Felder des Ereignisses tragen kann (``local_track_id``,
``forward_attempts``, ``forward_error``), liest dieser Job sie nach dem
Lauf aus ``local_submission`` und schreibt sie in seinen Report.

Wie ueberall: strukturiertes Log auf **stderr**, Report auf stdout oder
per ``--report`` in eine Datei (atomar geschrieben, damit der Waechter nie
ein halbes Dokument liest).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from acoustid_api.service import ApiService
from acoustid_api.upstream import (
    MAX_FORWARD_ATTEMPTS,
    ForwardReport,
    drain_queue,
    retry_forward,
)
from shared.env import EnvError, EnvSettings
from shared.logging_setup import setup_logging

__all__ = ["REPORT_SCHEMA", "main"]

_LOG = logging.getLogger(__name__)

#: Dienstname im strukturierten Log.
SERVICE_NAME: Final = "acoustid-queue-send"

#: Version des Report-Formats — dieselbe Regel wie beim Importer: aendert
#: sich das Dokument unvertraeglich, steigt die Zahl.
REPORT_SCHEMA: Final = "musicmeta-offline/queue-send/1"

#: ``--report -`` schreibt auf stdout.
STDOUT: Final = "-"

#: Exit-Codes — die drei, die auch der Importer an diesen Stellen benutzt
#: (docs/importer-job.md): der Waechter liest sie einheitlich.
EXIT_OK: Final = 0
EXIT_ERROR: Final = 1
EXIT_USAGE: Final = 2


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m acoustid_api.queuejob",
        description=(
            "Arbeitet die Upstream-Warteschlange ab (§8.9). Im Modus off/local "
            "gibt es nichts zu tun; der Lauf endet dann mit Erfolg."
        ),
    )
    parser.add_argument(
        "--report",
        default=STDOUT,
        metavar="PFAD",
        help=f"Ergebnis-JSON hierhin schreiben ({STDOUT} = stdout, Default)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="hoechstens N Gruppen bearbeiten (Default: der Wert aus upstream.py)",
    )
    parser.add_argument(
        "--retry",
        action="store_true",
        help=(
            "aufgegebene Gruppen zuruecksetzen und erneut versuchen "
            "(der Knopf 'Upstream-Queue senden' der Admin-UI)"
        ),
    )
    return parser.parse_args(argv)


def _gave_up_details(connection: Any) -> dict[str, Any]:
    """Die Gruppen, die die Grenze aus §8.9 erreicht haben.

    Bewusst hier und nicht in :mod:`acoustid_api.store`: es ist eine reine
    Diagnose-Abfrage fuer den Report und kein Teil des Submit-Vertrags.
    """
    rows = connection.execute(
        "SELECT local_track_id, max(forward_attempts) AS attempts,"
        " min(forward_error) AS error"
        " FROM local_submission"
        " WHERE status = 'forward_failed' AND forward_attempts >= %s"
        " GROUP BY local_track_id ORDER BY local_track_id",
        (MAX_FORWARD_ATTEMPTS,),
    ).fetchall()
    return {
        "gave_up_track_ids": [int(row[0]) for row in rows],
        "forward_attempts": max((int(row[1]) for row in rows), default=0),
        # Ein Fehlertext steht stellvertretend fuer alle: sie sind bei einem
        # ausgefallenen Upstream ohnehin derselbe, und der volle Satz stuende
        # in einer Benachrichtigung nur im Weg.
        "forward_error": next((row[2] for row in rows if row[2]), None),
    }


def _document(
    *,
    result: str,
    exit_code: int,
    started_at: datetime,
    report: ForwardReport | None = None,
    details: dict[str, Any] | None = None,
    error: BaseException | None = None,
) -> dict[str, Any]:
    """Das Ergebnis-Dokument — es entsteht **immer**, auch im Fehlerfall."""
    finished_at = datetime.now(UTC)
    document: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "result": result,
        "exit_code": exit_code,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_s": round((finished_at - started_at).total_seconds(), 3),
        "attempted": report.attempted if report else 0,
        "forwarded": report.forwarded if report else 0,
        "failed": report.failed if report else 0,
        "gave_up": report.gave_up if report else 0,
        "skipped": report.skipped if report else 0,
        "gave_up_track_ids": [],
        "forward_attempts": 0,
        "forward_error": None,
        "error": None if error is None else {"type": type(error).__name__, "message": str(error)},
    }
    document.update(details or {})
    return document


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
        # Der Report ist zu wertvoll, um ihn an einem Schreibfehler zu
        # verlieren — dann eben auf stdout (Muster des Importers).
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
        # Ein kleiner Pool: dieser Job faehrt genau eine Verbindung.
        service = ApiService.from_env(settings, min_size=1, max_size=2)
    except Exception as error:
        _LOG.exception("Warteschlangenlauf nicht startbar")
        _emit(
            _document(
                result="usage_error", exit_code=EXIT_USAGE, started_at=started_at, error=error
            ),
            args.report,
        )
        return EXIT_USAGE

    if not service.config.acoustid.submit.upstream_enabled:
        # Kein Fehler: der Modus ist eine Betreiber-Entscheidung, und der
        # Zyklus soll deswegen nicht als gescheitert in der Historie stehen.
        _LOG.info(
            "Upstream-Weiterleitung ist abgeschaltet — nichts zu tun",
            extra={"submit_mode": service.config.acoustid.submit.mode.value},
        )
        service.close()
        _emit(_document(result="disabled", exit_code=EXIT_OK, started_at=started_at), args.report)
        return EXIT_OK

    limit = {"limit": args.limit} if args.limit else {}
    try:
        with service, service.pool.connection() as connection:
            report = (
                retry_forward(connection, service, **limit)
                if args.retry
                else drain_queue(connection, service, **limit)
            )
            details = _gave_up_details(connection)
    except Exception as error:
        _LOG.exception("Warteschlangenlauf gescheitert")
        _emit(
            _document(result="failed", exit_code=EXIT_ERROR, started_at=started_at, error=error),
            args.report,
        )
        return EXIT_ERROR

    _LOG.info(
        "Warteschlangenlauf beendet",
        extra={
            "attempted": report.attempted,
            "forwarded": report.forwarded,
            "failed": report.failed,
            "gave_up": report.gave_up,
        },
    )
    _emit(
        _document(
            result="ok",
            exit_code=EXIT_OK,
            started_at=started_at,
            report=report,
            details=details,
        ),
        args.report,
    )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
