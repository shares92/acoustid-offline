"""Der One-Shot-Job: Bootstrap und taeglicher Delta-Import in einem Ablauf.

Dies ist der Rumpf, den der Container startet und der Waechter spaeter
(Phase 19) anstoesst. Er verbindet die Bausteine der Phasen 4 bis 7 zu einem
Lauf mit definiertem Anfang, definiertem Ende und einem Ergebnis, das eine
Maschine lesen kann (:mod:`acoustid_importer.report`).

**Zwei Betriebsarten** (:class:`~acoustid_importer.report.JobMode`):

``update`` — der Normalfall
    Alle Migrationen anwenden, die offenen Tage laden und einspielen, neue
    Fingerprints in den Index geben. Kein Bulk-Modus.

``bootstrap`` — der Erst-Import (DECISIONS „Voll-Replay aller Tagesdeltas")
    Derselbe Weg, aber in der Reihenfolge aus Import-Regel 6:

    1. Migrationsgruppe ``core`` (Tabellen, PKs, lz4-Kompression),
    2. Massenimport im Bulk-Modus (:mod:`acoustid_importer.bulk`) mit
       Download-Prefetch (:mod:`acoustid_importer.prefetch`),
    3. Bulk-Modus verlassen, ``CHECKPOINT``,
    4. Migrationsgruppe ``indexes`` (Sekundaerindizes),
    5. **erst danach** der Index-Feed.

    Punkt 5 ist kein Detail: der Arbeitsvorrat des Feeds ist der
    Partialindex ``fingerprint_idx_unindexed``. Ohne ihn liest jeder Batch
    die ganze Tabelle — bei 100+ Mio. Zeilen ist das der Unterschied
    zwischen Minuten und Tagen.

**Probelauf.** ``end_date`` begrenzt den Lauf auf einen Zeitraum; der Report
enthaelt dann Durchsatz, DB- und Index-Zuwachs und die Hochrechnung auf den
Vollbestand (:func:`~acoustid_importer.report.project`). Genau dafuer ist
der Probelauf da (DECISIONS 2026-07-25, ARCHITECTURE §12 Punkt 11).

**Der Lauf bricht kontrolliert ab**, wenn der Plattenplatz-Guard anschlaegt
(§8.8) oder ein Signal kommt (``stop``). Weil jede Tagesdatei ihre eigene
Transaktion ist (§8.3), steht ``import_state`` danach exakt auf der letzten
vollstaendigen Datei — der naechste Lauf setzt dort fort (§8.4). Ein Report
entsteht in **jedem** Fall, auch im Fehlerfall.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg

from acoustid_importer import state as state_mod
from acoustid_importer.bulk import INDEX_BUILD_SETTINGS, bulk_session, flush_wal, session_settings
from acoustid_importer.dbimport import DEFAULT_BATCH_ROWS, import_file
from acoustid_importer.diskguard import (
    DEFAULT_EVERY_BYTES,
    DEFAULT_EVERY_FILES,
    DiskGuard,
    DiskSpace,
    UsageFn,
)
from acoustid_importer.download import DeltaDownloader
from acoustid_importer.errors import (
    BulkModeError,
    DbImportError,
    DiskSpaceError,
    DownloadError,
    GapError,
    ImporterError,
    IndexFeedError,
    ParseError,
)
from acoustid_importer.indexfeed import DEFAULT_BATCH_SIZE, IndexFeedReport, feed_index
from acoustid_importer.measure import DbSize, IndexSize, database_size, index_size
from acoustid_importer.prefetch import DEFAULT_AHEAD, Prefetcher
from acoustid_importer.report import (
    HISTORY_GZ_BYTES,
    JobMode,
    Projection,
    RunReport,
    RunResult,
    RunTally,
    build_report,
    project,
)
from acoustid_importer.streams import BASE_URL, FIRST_DAY, DeltaFile
from acoustid_importer.worklist import Gap
from shared.config import Config, ConfigError, load_config
from shared.db import CORE, INDEXES, MigrationError, apply
from shared.env import EnvError, EnvSettings
from shared.fpindex import FpIndexClient, FpIndexError

__all__ = [
    "RunOptions",
    "run",
]

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RunOptions:
    """Alles, was einen Lauf steuert — CLI-Argumente in Datenform."""

    mode: JobMode = JobMode.UPDATE
    #: Letzter einzuschliessender Kalendertag (Probelauf). ``None`` heisst:
    #: bis zum neuesten verfuegbaren Tag (gestern).
    end_date: date | None = None
    #: Hoechstens so viele Tagesdateien einspielen.
    max_files: int | None = None
    #: Hoechstens so viele Kalendertage einspielen.
    max_days: int | None = None
    #: Zeilen je ``executemany`` (LEARNINGS: wirkt auf den Speicher, nicht
    #: auf den Durchsatz).
    batch_rows: int = DEFAULT_BATCH_ROWS
    #: Wie viele Dateien der Downloader vorauslaeuft.
    prefetch_ahead: int = DEFAULT_AHEAD
    #: gzip-Pruefung nach dem Laden. ``None`` heisst: im Bootstrap aus (der
    #: Parser liest jede Datei ohnehin sofort danach), im Update an.
    verify_gzip: bool | None = None
    #: Eingespielte Tagesdateien behalten. Per Vorgabe werden sie geloescht —
    #: 414 GB Bootstrap-Dateien aufzuheben, waere teuer und nutzlos.
    keep_dumps: bool = False
    #: Ueberschreibt ``disk.min_free_gb`` aus der config.yaml.
    min_free_gb: int | None = None
    #: Platzpruefung nach je so vielen Dateien bzw. gz-Bytes.
    guard_every_files: int = DEFAULT_EVERY_FILES
    guard_every_bytes: int = DEFAULT_EVERY_BYTES
    #: Messfunktion des Guards. Ausschliesslich fuer Tests — ohne sie waere
    #: „Platte laeuft mitten im Lauf voll" nur mit einer echten vollen Platte
    #: pruefbar. ``None`` heisst ``shutil.disk_usage``.
    disk_usage: UsageFn | None = None
    #: Neue Fingerprints in den Suchindex geben.
    feed_index: bool = True
    #: Dokumente je ``_update``-Batch des Index-Feeds.
    index_batch_size: int = DEFAULT_BATCH_SIZE
    #: Datenverzeichnis des acoustid-index, falls es hier gemountet ist —
    #: nur dann ist seine Bytegroesse messbar.
    index_data_dir: Path | None = None
    #: Bezugsgroesse der Hochrechnung (gz-Byte des Vollbestands).
    total_gz_bytes: int = HISTORY_GZ_BYTES
    #: Migrationen anwenden. ``False`` nur, wenn ein anderer Weg das schon tut.
    migrate: bool = True
    #: Bulk-Modus erzwingen/verhindern; ``None`` heisst: im Bootstrap an.
    bulk: bool | None = None
    #: ``CHECKPOINT`` nach dem Massenimport.
    checkpoint: bool = True
    #: Luecken (Import-Regel 5) nur melden statt abzubrechen. Bewusst
    #: keine CLI-Option: ein automatisches Weiterlaufen ueber Luecken hinweg
    #: waere genau die Datenverfaelschung, die Regel 5 verhindert.
    allow_gaps: bool = False
    #: „Heute"; nie ``date.today()`` tief im Code (Testbarkeit).
    today: date | None = None
    #: Beginn der Historie; nur fuer Tests abweichend.
    first_day: date = FIRST_DAY
    #: Wurzel der Quelle; in Tests ein lokaler Server.
    base_url: str = BASE_URL

    @property
    def bootstrap(self) -> bool:
        return self.mode is JobMode.BOOTSTRAP

    @property
    def use_bulk(self) -> bool:
        """Bulk-Modus aktiv? Per Vorgabe genau im Bootstrap."""
        return self.bootstrap if self.bulk is None else self.bulk

    @property
    def gzip_check(self) -> bool:
        """gzip-Pruefung aktiv? Per Vorgabe im Bootstrap aus."""
        return (not self.bootstrap) if self.verify_gzip is None else self.verify_gzip

    def effective_today(self, *, now: date | None = None) -> date:
        """Der Tag, gegen den die Arbeitsliste rechnet.

        ``end_date`` ist der letzte **einzuschliessende** Tag; die
        Arbeitsliste denkt in „heute" und nimmt den Vortag als neuesten
        verfuegbaren Tag (:func:`~acoustid_importer.worklist.newest_available_day`).
        Beides zusammen: ``today = end_date + 1``, gedeckelt auf das echte
        Heute — ein Zeitraum in der Zukunft hat keine Dateien.
        """
        today = self.today or now or date.today()
        if self.end_date is None:
            return today
        return min(today, self.end_date + timedelta(days=1))


@dataclass(slots=True)
class _State:
    """Mitschrift eines laufenden Jobs (auch fuer den Fehlerfall vollstaendig)."""

    tally: RunTally = field(default_factory=RunTally)
    warnings: list[str] = field(default_factory=list)
    gaps: tuple[Gap, ...] = ()
    feed: IndexFeedReport | None = None
    measurements: dict[str, Any] = field(default_factory=dict)
    projection: Projection | None = None
    aborted: bool = False


def run(
    options: RunOptions | None = None,
    *,
    settings: EnvSettings | None = None,
    config: Config | None = None,
    stop: Callable[[], bool] | None = None,
) -> RunReport:
    """Fuehrt einen kompletten Lauf aus und liefert seinen Report.

    Wirft nicht: jeder Fehler wird zu einem Ergebnis (:class:`RunResult`) und
    steht im Report. Der Aufrufer macht daraus einen Exit-Code
    (:attr:`RunReport.exit_code`).

    Args:
        options: Steuerung des Laufs; per Vorgabe ein Update-Lauf.
        settings: Bootstrap-Umgebung; per Vorgabe aus den `MMO_`-Variablen.
        config: Laufzeit-Konfiguration; per Vorgabe aus ``MMO_CONFIG_PATH``.
        stop: Wird zwischen zwei Tagesdateien gefragt; ``True`` beendet den
            Lauf geordnet (Signalbehandlung des Containers).
    """
    opts = options or RunOptions()
    started_at = datetime.now(UTC)
    clock = time.monotonic()
    state = _State()
    error: BaseException | None = None
    result = RunResult.OK

    try:
        _execute(opts, state, settings=settings, config=config, stop=stop)
    except Exception as exc:  # jeder Fehler wird zu einem Ergebnis
        error = exc
        result = _classify(exc)
        _LOG.error(
            "Importer-Lauf gescheitert",
            extra={"job_result": result.value, "error_type": type(exc).__name__},
            exc_info=result is RunResult.FAILED,
        )
    else:
        if state.aborted:
            result = RunResult.ABORTED

    report = build_report(
        mode=opts.mode,
        result=result,
        tally=state.tally,
        started_at=started_at,
        duration_s=time.monotonic() - clock,
        gaps=state.gaps,
        index_feed=state.feed,
        measurements=state.measurements,
        projection=state.projection,
        error=error,
        warnings=state.warnings,
    )
    _LOG.info(report.summary(), extra={"job_result": report.result.value})
    return report


# --- Ablauf -----------------------------------------------------------------


def _execute(
    opts: RunOptions,
    state: _State,
    *,
    settings: EnvSettings | None,
    config: Config | None,
    stop: Callable[[], bool] | None,
) -> None:
    """Der eigentliche Lauf; Fehler gehen an :func:`run` zurueck."""
    env = settings or EnvSettings.from_env()
    conf = config or load_config(env.config_path)
    min_free_gb = opts.min_free_gb if opts.min_free_gb is not None else conf.disk.min_free_gb

    guard = DiskGuard(
        env.dump_dir,
        min_free_gb=min_free_gb,
        every_files=opts.guard_every_files,
        every_bytes=opts.guard_every_bytes,
        usage=opts.disk_usage,
    )
    # Invariante §8.8: vor dem Lauf, nicht erst wenn es zu spaet ist. Der
    # Messwert kommt danach getrennt in den Report — er interessiert auch,
    # wenn der Guard abgeschaltet ist (`min_free_gb: 0`).
    guard.check(what="Import")
    state.measurements["disk_before"] = _space_dict(guard.measure())

    with psycopg.connect(env.db_dsn().get_secret_value(), autocommit=True) as conn:
        if opts.migrate:
            migrated = apply(conn, groups=[CORE] if opts.bootstrap else None)
            _LOG.info(
                "Schema geprueft",
                extra={"migration_applied": list(migrated.applied), "job_mode": opts.mode.value},
            )
        db_before = database_size(conn)
        state.measurements["db_before"] = db_before.as_dict()

        files = _plan(conn, opts, state)
        _import_all(conn, env, opts, state, files=files, guard=guard, stop=stop)

        # Der Bulk-Modus ist hier bereits verlassen (er endet mit
        # `_import_all`): erst danach haltbar machen, dann die Indizes.
        if opts.use_bulk and opts.checkpoint and files:
            flush_wal(conn)

        if opts.bootstrap and opts.migrate and not state.aborted:
            # Erst jetzt die Sekundaerindizes — vor allem
            # `fingerprint_idx_unindexed`, den der Index-Feed braucht.
            with session_settings(conn, INDEX_BUILD_SETTINGS):
                migrated = apply(conn, groups=[INDEXES])
            _LOG.info(
                "Sekundaerindizes gebaut", extra={"migration_applied": list(migrated.applied)}
            )

        index_before, index_after = _feed(conn, env, conf, opts, state)
        db_after = database_size(conn)

    state.measurements["db_after"] = db_after.as_dict()
    state.measurements["index_before"] = index_before.as_dict() if index_before else None
    state.measurements["index_after"] = index_after.as_dict() if index_after else None
    # Abschlussmessung ohne Guard: zu wenig Platz ist jetzt kein Abbruchgrund
    # mehr, aber der Wert gehoert in den Report.
    state.measurements["disk_after"] = _space_dict(guard.measure())
    state.measurements["disk_checks"] = guard.checks
    state.projection = _projection(opts, state, db_before, db_after, index_before, index_after)


def _plan(conn: psycopg.Connection, opts: RunOptions, state: _State) -> tuple[DeltaFile, ...]:
    """Arbeitsliste aus ``import_state``, begrenzt durch die Optionen."""
    plan = state_mod.plan(conn, today=opts.effective_today(), first_day=opts.first_day)
    state.gaps = plan.gaps
    if plan.gaps:
        for gap in plan.gaps[:10]:
            _LOG.error("Fehlende Tagesdatei in der Vergangenheit", extra={"gap": str(gap)})
        if not opts.allow_gaps:
            # Import-Regel 5: Luecken werden gemeldet, nie automatisch
            # nachgeholt — ein alter Tag wuerde neuere Upsert-Staende
            # ueberschreiben.
            plan.raise_on_gaps()
        state.warnings.append(f"{len(plan.gaps)} fehlende Tagesdatei(en) uebergangen")

    files = _limit(plan.files, max_days=opts.max_days, max_files=opts.max_files)
    state.tally.files_planned = len(files)
    _LOG.info(
        "Arbeitsliste steht",
        extra={
            "files_planned": len(files),
            "files_open": len(plan.files),
            "gaps": len(plan.gaps),
            "first_day": files[0].day.isoformat() if files else None,
            "last_day": files[-1].day.isoformat() if files else None,
        },
    )
    return files


def _limit(
    files: Sequence[DeltaFile], *, max_days: int | None, max_files: int | None
) -> tuple[DeltaFile, ...]:
    """Begrenzt die Arbeitsliste — ganze Tage zuerst (pure).

    ``max_days`` schneidet an einer Tagesgrenze: ein halb eingespielter Tag
    waere zwar kein Fehler (jede Datei ist eine eigene Transaktion), aber die
    Messung des Probelaufs waere schief.
    """
    selected = list(files)
    if max_days is not None:
        days: list[date] = []
        for item in selected:
            if item.day not in days:
                days.append(item.day)
        keep = set(days[:max_days])
        selected = [item for item in selected if item.day in keep]
    if max_files is not None:
        selected = selected[:max_files]
    return tuple(selected)


def _import_all(
    conn: psycopg.Connection,
    env: EnvSettings,
    opts: RunOptions,
    state: _State,
    *,
    files: Sequence[DeltaFile],
    guard: DiskGuard,
    stop: Callable[[], bool] | None,
) -> None:
    """Laedt und importiert die Arbeitsliste — Datei fuer Datei, Prefetch voraus."""
    if not files:
        _LOG.info("Nichts zu tun — der Datenbestand ist aktuell")
        return

    downloader = DeltaDownloader(env.dump_dir, base_url=opts.base_url)
    with (
        downloader,
        bulk_session(conn, enabled=opts.use_bulk),
        Prefetcher(
            downloader,
            files,
            ahead=opts.prefetch_ahead,
            verify_gzip=opts.gzip_check,
        ) as prefetcher,
    ):
        for download in prefetcher:
            if stop is not None and stop():
                state.aborted = True
                _LOG.warning(
                    "Abbruch auf Wunsch — der Stand ist resumierbar",
                    extra={"delta_file": download.file.name},
                )
                break
            state.tally.add_download(download)
            result = import_file(
                conn,
                download.path,
                file=download.file,
                batch_rows=opts.batch_rows,
            )
            state.tally.add_import(result)
            if not opts.keep_dumps:
                # Die Datei ist committet; sie noch einmal zu brauchen,
                # hiesse sie ohnehin neu zu laden.
                download.path.unlink(missing_ok=True)
            guard.after_file(download.size)


def _feed(
    conn: psycopg.Connection,
    env: EnvSettings,
    conf: Config,
    opts: RunOptions,
    state: _State,
) -> tuple[IndexSize | None, IndexSize | None]:
    """Index-Feed samt Groessenmessung davor und danach."""
    if not opts.feed_index or state.aborted:
        return None, None
    with FpIndexClient.from_env(env) as client:
        before = index_size(client, data_dir=opts.index_data_dir)
        state.feed = feed_index(
            conn,
            client,
            max_hashes=conf.acoustid.index.query_hashes,
            batch_size=opts.index_batch_size,
        )
        after = index_size(client, data_dir=opts.index_data_dir)
    return before, after


def _projection(
    opts: RunOptions,
    state: _State,
    db_before: DbSize,
    db_after: DbSize,
    index_before: IndexSize | None,
    index_after: IndexSize | None,
) -> Projection:
    """Hochrechnung aus den Messwerten des Laufs."""
    return project(
        measured_gz_bytes=state.tally.gz_bytes,
        measured_duration_s=state.tally.import_duration_s,
        total_gz_bytes=opts.total_gz_bytes,
        measured_db_bytes=db_after.total_bytes - db_before.total_bytes,
        measured_index_documents=_delta(
            index_before.documents if index_before else None,
            index_after.documents if index_after else None,
        ),
        measured_index_bytes=_delta(
            index_before.bytes if index_before else None,
            index_after.bytes if index_after else None,
        ),
    )


# --- Innenleben -------------------------------------------------------------


def _classify(error: BaseException) -> RunResult:
    """Ordnet einen Fehler dem Ergebnis (und damit dem Exit-Code) zu."""
    match error:
        case DiskSpaceError():
            return RunResult.DISK_GUARD
        case GapError():
            return RunResult.GAPS
        case DownloadError():
            return RunResult.DOWNLOAD_FAILED
        case ParseError() | DbImportError() | BulkModeError() | MigrationError() | psycopg.Error():
            return RunResult.IMPORT_FAILED
        case IndexFeedError() | FpIndexError():
            return RunResult.INDEX_FEED_FAILED
        case EnvError() | ConfigError():
            return RunResult.USAGE_ERROR
        case ImporterError():
            return RunResult.FAILED
        case _:
            return RunResult.FAILED


def _space_dict(space: DiskSpace | None) -> dict[str, object] | None:
    return space.as_dict() if space is not None else None


def _delta(before: int | None, after: int | None) -> int | None:
    if before is None or after is None:
        return None
    return after - before
