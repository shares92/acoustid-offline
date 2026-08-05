"""Kommandozeile des Importer-Containers (One-Shot-Job).

    python -m acoustid_importer                       # taeglicher Delta-Import
    python -m acoustid_importer --mode bootstrap      # Erst-Import (Voll-Replay)
    python -m acoustid_importer --mode bootstrap \\
        --end-date 2011-12-31 --report /data/probelauf.json   # Probelauf

Der Job schreibt sein Ergebnis als JSON — per Vorgabe auf stdout, mit
``--report PATH`` in eine Datei — und beendet sich mit einem definierten
Exit-Code (:class:`~acoustid_importer.report.ExitCode`). Beides ist der
Vertrag mit dem Waechter (Phase 19); Format und Codes stehen in
``docs/importer-job.md``.

Das strukturierte Log geht wie ueberall auf **stderr**, der Report auf
stdout — die beiden Stroeme lassen sich also getrennt weiterverarbeiten.

``SIGTERM`` und ``SIGINT`` (``docker stop``, Strg-C) beenden den Lauf
**geordnet**: die laufende Tagesdatei wird noch zu Ende gebracht, danach
endet der Job mit Exit-Code 8 und einem vollstaendigen Report. Ein zweites
Signal wirkt sofort — dann bricht die Datei-Transaktion ab und wird beim
naechsten Lauf wiederholt (§8.4).
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from types import FrameType

from acoustid_importer.dbimport import DEFAULT_BATCH_ROWS
from acoustid_importer.diskguard import DEFAULT_EVERY_BYTES, DEFAULT_EVERY_FILES
from acoustid_importer.indexfeed import DEFAULT_BATCH_SIZE
from acoustid_importer.job import RunOptions, run
from acoustid_importer.prefetch import DEFAULT_AHEAD
from acoustid_importer.report import HISTORY_GZ_BYTES, ExitCode, JobMode, RunReport
from shared.env import EnvError, EnvSettings
from shared.logging_setup import setup_logging

__all__ = ["main"]

_LOG = logging.getLogger(__name__)

#: Dienstname im strukturierten Log (wie ``acoustid-migrate`` in shared.db).
SERVICE_NAME = "acoustid-importer"

#: ``--report -`` schreibt auf stdout.
STDOUT = "-"


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m acoustid_importer",
        description=(
            "Delta-Importer von musicmeta-offline: laedt die Tagesdateien, "
            "spielt sie transaktional ein und uebergibt neue Fingerprints an "
            "den Suchindex. Ergebnis als JSON, Exit-Codes siehe docs/importer-job.md."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=[mode.value for mode in JobMode],
        default=JobMode.UPDATE.value,
        help=(
            "bootstrap = Erst-Import (Voll-Replay ab 2011-08-19, Bulk-Modus, "
            "Sekundaerindizes danach); update = taeglicher Lauf (Default)"
        ),
    )
    parser.add_argument(
        "--end-date",
        type=date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="letzter einzuschliessender Kalendertag — der Probelauf-Schalter",
    )
    parser.add_argument(
        "--max-days", type=int, metavar="N", help="hoechstens N Kalendertage einspielen"
    )
    parser.add_argument(
        "--max-files", type=int, metavar="N", help="hoechstens N Tagesdateien einspielen"
    )
    parser.add_argument(
        "--report",
        default=STDOUT,
        metavar="PFAD",
        help=f"Ergebnis-JSON hierhin schreiben ({STDOUT} = stdout, Default)",
    )
    parser.add_argument(
        "--batch-rows",
        type=int,
        default=DEFAULT_BATCH_ROWS,
        metavar="N",
        help=f"Zeilen je executemany (Default {DEFAULT_BATCH_ROWS}; wirkt auf den Speicher)",
    )
    parser.add_argument(
        "--prefetch",
        type=int,
        default=DEFAULT_AHEAD,
        metavar="N",
        help=f"so viele Dateien im Voraus laden (Default {DEFAULT_AHEAD}; kostet Plattenplatz)",
    )
    parser.add_argument(
        "--index-batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        metavar="N",
        help=f"Dokumente je Index-Batch (Default {DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument(
        "--no-index-feed",
        action="store_true",
        help="neue Fingerprints nicht an den Suchindex uebergeben",
    )
    parser.add_argument(
        "--index-dir",
        type=Path,
        metavar="PFAD",
        help="Datenverzeichnis des acoustid-index, falls hier gemountet (nur zum Messen)",
    )
    parser.add_argument(
        "--keep-dumps",
        action="store_true",
        help="eingespielte Tagesdateien behalten (Default: loeschen, sie sind committet)",
    )
    parser.add_argument(
        "--min-free-gb",
        type=int,
        metavar="N",
        help="ueberschreibt disk.min_free_gb aus der config.yaml (0 = Guard aus)",
    )
    parser.add_argument(
        "--guard-every-files",
        type=int,
        default=DEFAULT_EVERY_FILES,
        metavar="N",
        help=f"Platzpruefung nach je N Dateien (Default {DEFAULT_EVERY_FILES})",
    )
    parser.add_argument(
        "--guard-every-bytes",
        type=int,
        default=DEFAULT_EVERY_BYTES,
        metavar="N",
        help=f"Platzpruefung nach je N gz-Bytes (Default {DEFAULT_EVERY_BYTES})",
    )
    parser.add_argument(
        "--total-bytes",
        type=int,
        default=HISTORY_GZ_BYTES,
        metavar="N",
        help=f"Bezugsgroesse der Hochrechnung in gz-Byte (Default {HISTORY_GZ_BYTES})",
    )
    gzip_group = parser.add_mutually_exclusive_group()
    gzip_group.add_argument(
        "--verify-gzip",
        dest="verify_gzip",
        action="store_true",
        default=None,
        help="geladene Dateien zusaetzlich entpacken (Default: im Bootstrap aus, sonst an)",
    )
    gzip_group.add_argument(
        "--no-verify-gzip", dest="verify_gzip", action="store_false", help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--no-migrate",
        action="store_true",
        help="keine Migrationen anwenden (nur wenn ein anderer Weg das uebernimmt)",
    )
    parser.add_argument(
        "--no-checkpoint",
        action="store_true",
        help="kein CHECKPOINT nach dem Massenimport",
    )
    return parser.parse_args(argv)


def _options(args: argparse.Namespace) -> RunOptions:
    """Argumente -> :class:`RunOptions`.

    Raises:
        ValueError: Ein Zahlenwert ergibt keinen Lauf.
    """
    for name in ("max_days", "max_files", "batch_rows", "prefetch", "index_batch_size"):
        value = getattr(args, name)
        if value is not None and value < 1:
            raise ValueError(f"--{name.replace('_', '-')} muss mindestens 1 sein, war {value}")
    if args.min_free_gb is not None and args.min_free_gb < 0:
        raise ValueError("--min-free-gb darf nicht negativ sein")
    return RunOptions(
        mode=JobMode(args.mode),
        end_date=args.end_date,
        max_files=args.max_files,
        max_days=args.max_days,
        batch_rows=args.batch_rows,
        prefetch_ahead=args.prefetch,
        verify_gzip=args.verify_gzip,
        keep_dumps=args.keep_dumps,
        min_free_gb=args.min_free_gb,
        guard_every_files=args.guard_every_files,
        guard_every_bytes=args.guard_every_bytes,
        feed_index=not args.no_index_feed,
        index_batch_size=args.index_batch_size,
        index_data_dir=args.index_dir,
        total_gz_bytes=args.total_bytes,
        migrate=not args.no_migrate,
        checkpoint=not args.no_checkpoint,
    )


def _install_signal_handlers(flag: threading.Event) -> None:
    """SIGTERM/SIGINT setzen die Abbruchflagge; ein zweites Signal wirkt hart."""

    def handle(signum: int, _frame: FrameType | None) -> None:
        if flag.is_set():
            signal.signal(signum, signal.SIG_DFL)
            _LOG.warning(
                "Zweites Signal — der Lauf wird sofort beendet",
                extra={"signal": signal.Signals(signum).name},
            )
            signal.raise_signal(signum)
            return
        flag.set()
        _LOG.warning(
            "Signal empfangen — der Lauf endet nach der laufenden Tagesdatei",
            extra={"signal": signal.Signals(signum).name},
        )

    for number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(number, handle)


def _emit(report: RunReport, destination: str) -> None:
    """Schreibt den Report nach stdout oder in eine Datei."""
    if destination == STDOUT:
        print(report.to_json())
        return
    path = Path(destination)
    try:
        report.write(path)
    except OSError as error:
        # Der Report ist zu wertvoll, um ihn an einem Schreibfehler zu
        # verlieren — dann eben auf stdout.
        _LOG.error(
            "Report liess sich nicht schreiben — Ausgabe auf stdout",
            extra={"report_path": str(path), "reason": str(error)},
        )
        print(report.to_json())


def main(argv: Sequence[str] | None = None) -> int:
    """Einstiegspunkt; liefert den Exit-Code (siehe docs/importer-job.md)."""
    args = _parse_args(argv)
    try:
        settings = EnvSettings.from_env()
    except EnvError as error:
        print(f"Fehler: {error}", file=sys.stderr)
        return int(ExitCode.USAGE)
    setup_logging(SERVICE_NAME, settings.log_level)

    try:
        options = _options(args)
    except ValueError as error:
        print(f"Fehler: {error}", file=sys.stderr)
        return int(ExitCode.USAGE)

    stop = threading.Event()
    _install_signal_handlers(stop)

    report = run(options, settings=settings, stop=stop.is_set)
    _emit(report, args.report)
    return int(report.exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
