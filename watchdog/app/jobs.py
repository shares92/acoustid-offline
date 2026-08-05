"""Jobs als Subprozesse des Waechters (E10) — Ablauf eines ganzen Laufs.

**Warum Subprozesse und nicht ``[program:*]``.** Importer, Backup und
Warteschlangenlauf brauchen Per-Lauf-Argumente (``--report``,
``--target``), und die kann supervisord nicht uebergeben. E10 zieht daraus
die Grenze: *„Nur der Supervisor startet/stoppt **Dauerdienste**; Jobs sind
Kinder des Waechters."* Damit bekommt der Waechter Argumente, Returncode
und Report ohne Umweg — genau die drei Dinge, aus denen er ``update_run``
fuellt.

**Ein Zyklus, nicht nur ein Prozessstart** (:class:`JobCycle`). Ein
faelliger Job ist die einzige Gelegenheit, bei der die Instanz von selbst
aufwacht; deshalb haengt an ihm die ganze Kette:

1. **Lauf anlegen** — vor der Arbeit, damit ein abgestuerzter Container
   hinterher als angefangen erkennbar ist (§8.4) und der Idle-Stopp
   blockiert ist (§8.5, :class:`~acoustid_watchdog.lifecycle.DatabaseJobs`).
2. **Plattenplatz pruefen** — jeder Schreibpfad (E11,
   :mod:`acoustid_watchdog.diskspace`). Unterschreitung heisst Abbruch
   **vor** dem ersten Byte, mit Benachrichtigung.
3. **Wecken** — mit einer eigenen, grosszuegigen Frist: eine
   Postgres-Recovery am echten Bestand dauert laenger, als eine Anfrage
   warten darf.
4. **Job starten und ueberwachen** — Report und Returncode auswerten.
5. **Cache invalidieren** nach einem erfolgreichen Delta-Import
   (Invariante §8.6).
6. **Schlafen legen** — aber nur, wenn dieser Zyklus den Stack selbst
   geweckt hat und waehrenddessen niemand die Instanz benutzt hat.

**Der Stopp-Weg ist grosszuegig.** ``SIGTERM`` beendet den Importer
geordnet — aber erst **nach der laufenden Tagesdatei** (docs/importer-job.md
„Signale"). Eine knappe Frist machte aus dem geordneten Exit-Code 8 ein
``SIGKILL`` mit zurueckgerollter Transaktion. :data:`SIGTERM_GRACE_S` ist
deshalb in Minuten bemessen, nicht in Sekunden.

**Genau ein Job gleichzeitig** (:class:`JobManager`). Zwei Importer
nebeneinander wuerden dieselben Tagesdateien laden und sich in
``import_state`` ins Gehege kommen; zwei Backups schrieben in dasselbe
Verzeichnis. Der Manager ist zugleich die interne Trigger-API fuer
manuelle Laeufe — die Grundlage von `/admin/jobs` (M8).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from starlette.concurrency import run_in_threadpool

from acoustid_watchdog.diskspace import shortfalls, survey, write_paths
from acoustid_watchdog.events import EventLevel
from acoustid_watchdog.notify import disk_low, import_failed, upstream_gave_up
from acoustid_watchdog.runs import (
    RunKind,
    RunResult,
    UpdateRun,
    finish_run,
    open_run,
    start_run,
)
from acoustid_watchdog.store import utc_now
from acoustid_watchdog.wake import StackNotReadyError

if TYPE_CHECKING:  # nur fuer die Typannotation — sonst waere es ein Importzyklus
    from acoustid_watchdog.service import WatchdogService

__all__ = [
    "EVENT_SOURCE",
    "INDEX_BUSY_FILENAME",
    "INDEX_BUSY_MAX_AGE_S",
    "JOB_WAKE_TIMEOUT_S",
    "REPORT_DIRNAME",
    "SHUTDOWN_WAIT_S",
    "SIGTERM_GRACE_S",
    "CycleResult",
    "JobCycle",
    "JobManager",
    "JobOutcome",
    "JobRunner",
    "index_marker_expired",
    "job_command",
]

_LOG = logging.getLogger(__name__)

#: Quelle der Job-Ereignisse im ``event_log`` — eigene Quelle, damit die
#: Logansicht (M8) den Zyklus getrennt vom Weck-Geschehen zeigen kann.
EVENT_SOURCE: Final = "scheduler"

#: Wie lange ein Zyklus auf den wachen Stack wartet. Deutlich grosszuegiger
#: als ``wake.hold_timeout_s`` (90 s, §6): dort wartet ein **Client**, hier
#: ein Job, der ohnehin Stunden laufen darf — und eine Postgres-Recovery
#: nach einem harten Stopp kann Minuten dauern (supervisord.conf,
#: ``stopwaitsecs=300``). Bewusst kein §6-Schluessel: der Betreiber hat
#: keinen Grund, daran zu drehen (Muster aus DECISIONS 2026-08-01, Punkt 2).
JOB_WAKE_TIMEOUT_S: Final = 600.0

#: Frist zwischen ``SIGTERM`` und ``SIGKILL`` beim **manuellen** Abbruch
#: (Knopf „Abbrechen", M8). In Minuten und nicht in Sekunden: der Importer
#: beendet sich erst nach der laufenden Tagesdatei, und eine
#: Fingerprint-Datei kann mehrere GB gross sein (docs/importer-job.md
#: „Signale"). Wer hier knausert, tauscht den geordneten Exit-Code 8 gegen
#: eine zurueckgerollte Transaktion.
#:
#: **Die Zahl gehoert in eine Kette** (DECISIONS 2026-08-05, K2). Jede
#: Frist muss unter der naechstgroesseren liegen, sonst ist sie wirkungslos:
#:
#: ==========================================  =======  ======================
#: ``stop_grace_period`` (docker-compose.yml)   360 s   Deckel ueber allem
#: ``stopwaitsecs`` (``[program:watchdog]``)    300 s   danach SIGKILL an die
#:                                                      ganze Prozessgruppe
#: :data:`SIGTERM_GRACE_S` / :data:`SHUTDOWN_WAIT_S`  240 s   unsere beiden Wege
#: ==========================================  =======  ======================
#:
#: Ein Test haelt die Kette fest (``tests/test_repo_layout.py``); ohne ihn
#: hoben sich die Werte gegenseitig lautlos auf.
SIGTERM_GRACE_S: Final = 240.0

#: Wie lange der Lifespan beim Herunterfahren auf einen laufenden Job
#: wartet — **ohne** ihm selbst ein Signal zu schicken.
#:
#: Das Signal kommt von supervisord an die ganze Prozessgruppe
#: (``stopasgroup=true``); ein zweites bedeutete im Importer „sofort
#: beenden" (``acoustid_importer.__main__``). Warten muss der Waechter
#: trotzdem: endet **er** zuerst, wird der Job zum Waisen unter ``tini``
#: — supervisord sieht seinen Hauptprozess weg, raeumt die Gruppe nicht
#: mehr auf, und erst Docker killt nach ``stop_grace_period`` alles. Genau
#: dieser Waise haelt dann eine Busy-Marke und eine offene ``update_run``-
#: Zeile (F7/K1).
SHUTDOWN_WAIT_S: Final = 240.0

#: Unterverzeichnis im Datenverzeichnis, in dem die Reports der Jobs
#: liegen. Je Art eine Datei, beim naechsten Lauf ueberschrieben — der
#: Inhalt steht ohnehin in ``update_run.report``; die Datei ist die
#: Uebergabe zwischen Subprozess und Waechter (und beim Debuggen der
#: letzte Stand).
REPORT_DIRNAME: Final = "jobs"

#: Marke im Datenverzeichnis, mit der der Waechter einen laufenden
#: Delta-Import anzeigt (Betreiber-Entscheid 2026-08-05). Der API-Dienst
#: liest sie und **stellt die Indexierung eigener Einreichungen zurueck**:
#: sonst erhoehte ein Submit die Index-Version, und der Index-Feed des
#: Importers braeche an seinem ``expected_version``-Guard ab — der Lauf
#: endete als Fehler und kostete einen Tag Datenstand.
#:
#: Der Name steht auch in :data:`acoustid_api.submit.INDEX_BUSY_FILENAME`;
#: ein Test haelt beide aneinander. Bewusst kein Import: der Waechter
#: haengt nicht vom API-Paket ab (er brauchte sonst psycopg).
INDEX_BUSY_FILENAME: Final = "index-feed.busy"

#: Hoechstalter der Busy-Marke — danach gilt sie als **verwaist** (F7).
#:
#: Sie traegt ihren Setzzeitpunkt als Inhalt, und genau das rettet den Fall,
#: den ein ``finally`` nicht abdecken kann: stirbt der Waechter mit
#: ``SIGKILL``, laeuft kein ``finally``, und die Marke bliebe fuer immer
#: liegen — eigene Einreichungen waeren dauerhaft gespeichert, aber im
#: Index unauffindbar (und die Upstream-Quese staende mit still).
#:
#: **24 Stunden und nicht weniger:** ein Bootstrap-Feed laeuft Stunden bis
#: Tage (414 GB gz, §5.1). Ein knapperer Wert erklaerte einen ehrlich
#: laufenden Import fuer tot und oeffnete genau das Kollisionsfenster, das
#: die Marke schliessen soll. Ein taeglicher Delta-Lauf ist nach Minuten
#: fertig; wer laenger braucht, hat ohnehin ein Problem, das eine
#: Benachrichtigung wert ist.
INDEX_BUSY_MAX_AGE_S: Final = 24 * 3600.0


def index_marker_expired(marker: Path, *, max_age_s: float = INDEX_BUSY_MAX_AGE_S) -> bool:
    """Ist die Busy-Marke aelter als ihr Hoechstalter?

    Gelesen wird der **Inhalt** (der Setzzeitpunkt), nicht die mtime: die
    Datei liegt auf einem gemounteten Dateisystem, und eine Kopie oder ein
    ``touch`` waere dort keine Aussage ueber den Lauf. Ist der Inhalt
    unlesbar oder unverstaendlich, faellt die Antwort auf die mtime
    zurueck — und im Zweifel auf „nicht abgelaufen": eine faelschlich als
    tot erklaerte Marke ist der teurere Fehler.

    Returns:
        ``False``, wenn es die Marke gar nicht gibt.
    """
    try:
        raw = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    age_s = _marker_age_s(raw, marker)
    if age_s is None:
        return False
    return age_s > max_age_s


def _marker_age_s(raw: str, marker: Path) -> float | None:
    """Alter der Marke in Sekunden — ``None``, wenn nicht bestimmbar."""
    try:
        written = datetime.fromisoformat(raw)
    except ValueError:
        try:
            written = datetime.fromtimestamp(marker.stat().st_mtime, tz=UTC)
        except OSError:  # pragma: no cover - die Datei war eben noch da
            return None
    if written.tzinfo is None:  # pragma: no cover - `utc_now` schreibt immer eine Zone
        written = written.replace(tzinfo=UTC)
    return (datetime.now(UTC) - written).total_seconds()


@dataclass(frozen=True, slots=True)
class JobOutcome:
    """Was ein Subprozess hinterlassen hat."""

    #: Exit-Code des Prozesses (siehe docs/importer-job.md).
    returncode: int
    #: Der eingelesene Report; ``None``, wenn keiner geschrieben wurde
    #: (Prozess gar nicht gestartet, ``SIGKILL``, kaputte Datei).
    report: dict[str, Any] | None = None
    #: Der Lauf wurde von aussen abgebrochen (:meth:`JobRunner.cancel`).
    cancelled: bool = False
    #: Der Prozess liess sich nicht starten.
    start_error: str | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and self.start_error is None

    @property
    def result(self) -> RunResult:
        """Der Ausgang, wie er in ``update_run`` steht.

        ``aborted`` ist der **geordnete** Abbruch: der Plattenplatz-Guard
        des Jobs (Exit-Code 3) und der Abbrechen-Knopf (Exit-Code 8,
        ``SIGTERM``). Alles andere ist ``failed``.
        """
        if self.ok:
            return RunResult.SUCCESS
        if self.cancelled or self.report_result in ("aborted", "disk_guard"):
            return RunResult.ABORTED
        return RunResult.FAILED

    @property
    def report_result(self) -> str | None:
        """Das Feld ``result`` des Reports (``"ok"``, ``"gaps"``, …)."""
        return None if self.report is None else self.report.get("result")

    @property
    def error_message(self) -> str | None:
        """Der Fehlertext fuer ``update_run.error`` und die Meldung."""
        if self.start_error is not None:
            return self.start_error
        if self.report is not None:
            error = self.report.get("error")
            if isinstance(error, dict) and error.get("message"):
                return f"{error.get('type')}: {error['message']}"
            if self.report_result and self.report_result != "ok":
                return f"Ergebnis {self.report_result} (Exit-Code {self.returncode})"
        if self.ok:
            return None
        return f"Exit-Code {self.returncode}"


def job_command(
    kind: RunKind,
    *,
    settings: Any,
    config: Any,
    report: Path,
    python: str | None = None,
) -> list[str]:
    """Das Kommando eines Jobs — die Uebersetzung von Art nach Aufruf.

    Args:
        kind: Job-Art.
        settings: Bootstrap-Werte (``MMO_*``).
        config: Laufzeit-Konfiguration (fuer ``backup.*``).
        report: Pfad, in den der Job seinen Report schreibt.
        python: Interpreter; ohne Angabe der laufende
            (``sys.executable`` — im Image ``/app/.venv/bin/python``).

    Raises:
        NotImplementedError: Fuer die Job-Arten, deren Fachlogik erst mit
            M3-M5 entsteht. Sie stehen schon im Schema und im Enum, damit
            die Historie spaeter nicht auseinanderlaeuft — ein Lauf laesst
            sich daraus aber noch nicht bauen.
    """
    interpreter = python or sys.executable
    if kind is RunKind.ACOUSTID_DELTA:
        return [interpreter, "-m", "acoustid_importer", "--report", str(report)]
    if kind is RunKind.QUEUE_SEND:
        return [interpreter, "-m", "acoustid_api.queuejob", "--report", str(report)]
    if kind is RunKind.BACKUP:
        command = [
            interpreter,
            "-m",
            "acoustid_importer.backup",
            "--target",
            str(config.backup.dir),
            "--report",
            str(report),
        ]
        if config.backup.include_covers:
            command.append("--include-covers")
        return command
    raise NotImplementedError(f"Fuer {kind.value} gibt es noch keinen Job (M3-M5)")


class JobRunner:
    """Startet einen Job, wartet auf sein Ende und liest seinen Report.

    **stdout und stderr werden geerbt**, nicht abgefangen: der Job loggt
    strukturiert auf stderr, und geerbt landet das im Log des Waechters —
    also in ``/config/logs/watchdog.log`` **und** in ``docker logs`` (E16).
    Eine Pipe muesste der Waechter nebenher leerlesen, sonst blockierte der
    Job nach ein paar Megabyte Logausgabe. Die Fehlerauskunft kommt aus dem
    Report; der entsteht in **jedem** Fall, auch bei Abbruch und Fehler
    (docs/importer-job.md).
    """

    def __init__(self, *, cwd: Path, grace_s: float = SIGTERM_GRACE_S) -> None:
        """
        Args:
            cwd: Arbeitsverzeichnis des Kindprozesses. Ausdruecklich
                gesetzt und **nie** das Repo-Wurzelverzeichnis: dort
                verdeckt das Member-Verzeichnis ``shared/`` als
                Namespace-Paket den Editable-Import (LEARNINGS
                „Mehrere gleichnamige Python-Pakete kollidieren im venv").
            grace_s: Frist zwischen ``SIGTERM`` und ``SIGKILL``.
        """
        self.cwd = cwd
        self.grace_s = grace_s
        self._process: asyncio.subprocess.Process | None = None

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def run(self, command: Sequence[str], *, report: Path) -> JobOutcome:
        """Startet ``command`` und wartet, bis es fertig ist.

        Args:
            command: Vollstaendige Argumentliste (keine Shell).
            report: Datei, in die der Job seinen Report schreibt. Sie wird
                vorher entfernt, damit ein alter Report nicht als der
                dieses Laufs gelesen wird.
        """
        try:
            # Beides kann scheitern (schreibgeschuetztes oder volles
            # `/config`) — ungeschuetzt flog die Ausnahme am Aufrufer vorbei,
            # und der eben angelegte Lauf blieb fuer immer offen.
            report.parent.mkdir(parents=True, exist_ok=True)
            report.unlink(missing_ok=True)
        except OSError as error:
            _LOG.exception("Report-Verzeichnis nicht nutzbar", extra={"report_path": str(report)})
            return JobOutcome(returncode=-1, start_error=f"{type(error).__name__}: {error}")

        _LOG.info("Job wird gestartet", extra={"job_command": list(command), "cwd": str(self.cwd)})
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(self.cwd),
                # Die Umgebung wird geerbt: die `MMO_`-Variablen sind die
                # Zugaenge des Jobs (Pfade, Datenbank, Index).
                env=os.environ.copy(),
            )
        except OSError as error:
            _LOG.exception("Job liess sich nicht starten")
            return JobOutcome(returncode=-1, start_error=f"{type(error).__name__}: {error}")

        self._process = process
        try:
            returncode = await process.wait()
        finally:
            self._process = None
        _LOG.info("Job beendet", extra={"returncode": returncode})
        return JobOutcome(returncode=returncode, report=_read_report(report))

    async def cancel(self) -> bool:
        """Beendet einen laufenden Job — erst ``SIGTERM``, dann ``SIGKILL``.

        Returns:
            ``True``, wenn ein Prozess angesprochen wurde.
        """
        process = self._process
        if process is None or process.returncode is not None:
            return False
        _LOG.warning("Job wird beendet", extra={"pid": process.pid, "grace_s": self.grace_s})
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=self.grace_s)
        except TimeoutError:
            # Nach der grosszuegigen Frist bleibt nur der harte Weg. Der
            # Stand ist trotzdem resumierbar: eine Tagesdatei ist genau
            # eine Transaktion (§8.3/§8.4).
            _LOG.error(
                "Job hat die Frist nicht eingehalten — SIGKILL",
                extra={"pid": process.pid, "grace_s": self.grace_s},
            )
            process.kill()
            await process.wait()
        return True


def _read_report(path: Path) -> dict[str, Any] | None:
    """Liest den Report; jede Unlesbarkeit heisst „kein Report".

    Der Lauf ist an dieser Stelle vorbei — ein Traceback ueber eine
    kaputte JSON-Datei waere die schlechtere Auskunft als der Returncode,
    den wir ohnehin haben.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        _LOG.warning(
            "Job-Report nicht lesbar", extra={"report_path": str(path), "error": str(error)}
        )
        return None
    return data if isinstance(data, dict) else None


@dataclass(slots=True)
class CycleResult:
    """Was ein Zyklus getan hat — die Auskunft fuer Tests und Trigger-API."""

    kind: RunKind
    run: UpdateRun | None = None
    outcome: JobOutcome | None = None
    #: Der Zyklus hat den Stack selbst geweckt.
    woke_stack: bool = False
    #: Der Zyklus hat ihn danach wieder schlafen gelegt.
    slept: bool = False
    #: Aus dem Delta-Zyklus hervorgegangene Folgelaeufe (``queue-send``).
    followups: list[CycleResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.run is not None and self.run.result is RunResult.SUCCESS


class JobCycle:
    """Der vollstaendige Ablauf eines Jobs (Modul-Docstring, Schritte 1-6)."""

    def __init__(
        self,
        service: WatchdogService,
        *,
        runner: JobRunner | None = None,
        wake_timeout_s: float = JOB_WAKE_TIMEOUT_S,
    ) -> None:
        self._service = service
        self._runner = runner or JobRunner(cwd=service.settings.data_dir)
        self.wake_timeout_s = wake_timeout_s

    @property
    def runner(self) -> JobRunner:
        return self._runner

    @property
    def report_dir(self) -> Path:
        return self._service.settings.data_dir / REPORT_DIRNAME

    async def run(self, kind: RunKind, *, reason: str = "scheduler") -> CycleResult:
        """Faehrt einen ganzen Zyklus und liefert sein Ergebnis.

        **Jedes ``start_run`` bekommt garantiert sein ``finish_run``** — der
        Rumpf steht in einem ``try``, dessen ``finally`` einen noch offenen
        Lauf abschliesst. Eine offene Zeile heisst „laeuft noch"
        (:func:`~acoustid_watchdog.runs.running_runs`), und daran haengt die
        Job-Sperre des Idle-Stopps (§8.5): bliebe sie stehen, laege die
        Instanz dauerhaft wach, und `/status` zeigte einen Lauf, den es
        nicht gibt. Auch ein ``CancelledError`` (Abbruch-Knopf,
        Herunterfahren) kommt hier durch.
        """
        service = self._service
        result = CycleResult(kind=kind)
        config = service.config
        run_id = await run_in_threadpool(start_run, service.db, kind)
        service.log_event(
            EventLevel.INFO,
            f"{kind.display_name} gestartet",
            {"run_id": run_id, "reason": reason},
            source=EVENT_SOURCE,
        )
        try:
            await self._run_guarded(kind, reason, run_id, config, result)
        except asyncio.CancelledError:
            await self._close_open(
                run_id,
                kind,
                RunResult.ABORTED,
                "Lauf abgebrochen (Waechter beendet den Vorgang)",
                result,
            )
            raise
        except BaseException as error:
            await self._close_open(
                run_id, kind, RunResult.FAILED, f"{type(error).__name__}: {error}", result
            )
            raise
        else:
            # Der Regelfall hat den Lauf laengst abgeschlossen; das hier ist
            # das Netz fuer jeden Pfad, der es kuenftig vergisst.
            await self._close_open(
                run_id, kind, RunResult.FAILED, "Lauf ohne Ergebnis beendet", result
            )
        return result

    async def _run_guarded(
        self,
        kind: RunKind,
        reason: str,
        run_id: int,
        config: Any,
        result: CycleResult,
    ) -> None:
        """Der Rumpf des Zyklus — Schritte 2-6 des Modul-Docstrings."""
        service = self._service

        # --- Plattenplatz (E11) --------------------------------------------
        too_full = await run_in_threadpool(self._check_disk, config)
        if too_full:
            result.run = await self._abort_for_disk(run_id, kind, too_full)
            return

        # --- Wecken ---------------------------------------------------------
        # **Vor** dem Weckvorgang gemessen: eine Anfrage, die waehrend des
        # Weckens eintrifft, gehoert zum Nutzungsfenster — sie war sonst
        # unsichtbar (der Zaehler wurde erst danach gelesen).
        requests_before = service.activity.requests
        # Der Zaehler der **begonnenen** Weckvorgaenge ist die verlaessliche
        # Auskunft „haben wir ihn geweckt?": `wake.ready` waere es nicht —
        # ein Betreiberstart oder eine verworfene Bereitschaft
        # (`invalidate()`) verfaelschen es in beide Richtungen.
        wakes_before = service.wake.wakes
        try:
            await service.wake.ensure_ready(timeout_s=self.wake_timeout_s)
        except StackNotReadyError as error:
            result.run = await self._finish(
                run_id, kind, RunResult.FAILED, error=f"Stack nicht bereit: {error}"
            )
            service.notify.send_background(
                import_failed(
                    kind.display_name, result="stack_not_ready", error=str(error), run_id=run_id
                )
            )
            return
        result.woke_stack = service.wake.wakes > wakes_before

        # --- Der Job selbst -------------------------------------------------
        outcome = await self._execute(kind, config)
        result.outcome = outcome
        result.run = await self._finish_from_outcome(run_id, kind, outcome)

        if kind is RunKind.ACOUSTID_DELTA:
            await self._after_delta(reason, run_id, kind, outcome, result)
        elif not outcome.ok:
            service.notify.send_background(
                import_failed(
                    kind.display_name,
                    result=outcome.report_result or outcome.result.value,
                    error=outcome.error_message,
                    run_id=run_id,
                )
            )

        # --- Schlafen legen -------------------------------------------------
        result.slept = await self._sleep_again(result.woke_stack, requests_before)

    async def _after_delta(
        self,
        reason: str,
        run_id: int,
        kind: RunKind,
        outcome: JobOutcome,
        result: CycleResult,
    ) -> None:
        """Was nach dem Delta-Import folgt: Cache, Nachlauf, Meldung.

        **Der Nachlauf laeuft auch nach einem gescheiterten Import** (F9):
        die waehrend des Laufs zurueckgestellten Einreichungen
        (§8.12) haengen nicht am Ergebnis des Imports, und ohne den
        Nachlauf blieben sie bis zum naechsten Submit unsichtbar. Nicht
        nachgelaufen wird nach einem **Abbruch** — dort ist der Stack
        entweder gar nicht wach (Plattenplatz-Guard) oder der Betreiber
        wollte gerade, dass nichts mehr passiert.
        """
        service = self._service
        if outcome.ok:
            # Invariante §8.6: nach jedem erfolgreichen Delta-Import ist der
            # Lookup-Cache veraltet.
            await run_in_threadpool(service.invalidate_cache, "delta_import")
        else:
            service.notify.send_background(
                import_failed(
                    kind.display_name,
                    result=outcome.report_result or outcome.result.value,
                    error=outcome.error_message,
                    run_id=run_id,
                )
            )
        if outcome.result is RunResult.ABORTED:
            _LOG.info("Kein Nachlauf nach einem abgebrochenen Delta-Lauf")
            return
        if not service.wake.ready:
            # Nach einem Absturz des Stacks braucht der Nachlauf eine
            # Datenbank, die es gerade nicht gibt — er wuerde nur eine
            # zweite Fehlermeldung erzeugen.
            _LOG.info("Kein Nachlauf — der Stack ist nicht mehr bereit")
            return
        result.followups.append(await self._queue_send(reason))

    async def _close_open(
        self,
        run_id: int,
        kind: RunKind,
        outcome: RunResult,
        error: str,
        result: CycleResult,
    ) -> None:
        """Schliesst den Lauf ab, **falls** er noch offen ist.

        Der Regelweg hat ihn laengst abgeschlossen — dann passiert hier
        nichts, und das schon eingetragene Ergebnis bleibt stehen. Dieser
        Aufruf ist das Netz fuer Ausnahmen und Abbrueche; er darf selbst
        nichts werfen, denn eine Ausnahme hier verdeckte die eigentliche.
        """
        try:
            still_open = await run_in_threadpool(open_run, self._service.db, run_id)
            if still_open is None:
                return
            _LOG.warning(
                "Lauf ohne Ergebnis wird geschlossen",
                extra={"run_id": run_id, "job_kind": kind.value, "error": error},
            )
            result.run = await self._finish(run_id, kind, outcome, error=error)
        except Exception:  # pragma: no cover - defensiv
            _LOG.exception("Offener Lauf liess sich nicht schliessen", extra={"run_id": run_id})

    # --- Schritte -----------------------------------------------------------

    def _check_disk(self, config: Any) -> list[Any]:
        """Der Guard aus E11 — synchron, also im Threadpool aufgerufen."""
        paths = write_paths(self._service.settings, config)
        return shortfalls(survey(paths, min_free_gb=config.disk.min_free_gb))

    async def _abort_for_disk(self, run_id: int, kind: RunKind, too_full: list[Any]) -> UpdateRun:
        """Geordneter Abbruch **vor** dem ersten Byte (§8.8)."""
        worst = min(too_full, key=lambda space: space.free_bytes)
        message = "; ".join(str(space) for space in too_full)
        _LOG.error("Job abgebrochen, Plattenplatz zu knapp", extra={"detail": message})
        self._service.log_event(
            EventLevel.ERROR,
            "Lauf abgebrochen — Plattenplatz zu knapp",
            {"run_id": run_id, "paths": [space.as_dict() for space in too_full]},
            source=EVENT_SOURCE,
        )
        self._service.notify.send_background(
            disk_low(str(worst.path), free_gb=worst.free_gb, min_free_gb=worst.min_free_gb)
        )
        return await self._finish(run_id, kind, RunResult.ABORTED, error=message)

    async def _execute(self, kind: RunKind, config: Any) -> JobOutcome:
        report = self.report_dir / f"{kind.value}.json"
        try:
            command = job_command(
                kind, settings=self._service.settings, config=config, report=report
            )
        except NotImplementedError as error:
            return JobOutcome(returncode=-1, start_error=str(error))
        if kind is not RunKind.ACOUSTID_DELTA:
            return await self._runner.run(command, report=report)
        # Nur der Delta-Import schreibt in den Suchindex und braucht die
        # Marke (Betreiber-Entscheid 2026-08-05). Ein Backup oder ein
        # Warteschlangenlauf stoert dort niemanden.
        with self._index_busy():
            return await self._runner.run(command, report=report)

    @contextmanager
    def _index_busy(self) -> Iterator[None]:
        """Setzt die Marke fuer die Dauer des Laufs — und raeumt sie **immer** weg.

        Bliebe sie liegen, wuerde die Instanz eigene Einreichungen nie
        wieder indexieren: sie waeren gespeichert, aber unauffindbar. Das
        ``finally`` ist deshalb der eigentliche Punkt dieser Funktion; ein
        Schreibfehler beim Setzen wird dagegen nur geloggt, denn er kostet
        hoechstens einen Import (der naechste Zyklus wiederholt ihn).
        """
        marker = self._service.settings.data_dir / INDEX_BUSY_FILENAME
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(utc_now(), encoding="utf-8")
        except OSError:
            _LOG.warning(
                "Marke fuer den laufenden Import nicht schreibbar — eine gleichzeitige "
                "Einreichung kann den Index-Feed abbrechen lassen",
                extra={"marker_path": str(marker)},
            )
        try:
            yield
        finally:
            try:
                marker.unlink(missing_ok=True)
            except OSError:
                _LOG.exception(
                    "Marke fuer den laufenden Import liess sich nicht entfernen — "
                    "eigene Einreichungen bleiben unindexiert",
                    extra={"marker_path": str(marker)},
                )

    async def _queue_send(self, reason: str) -> CycleResult:
        """Der Warteschlangenlauf nach dem Delta-Import (§8.9).

        Er laeuft im selben wachen Fenster und als **eigener** Lauf in der
        Historie: sein Ergebnis sagt etwas anderes aus als das des Imports,
        und ein gescheiterter Upstream-Versand darf einen erfolgreichen
        Datenabgleich nicht rot faerben.

        Im Modus ``off``/``local`` endet der Job sofort mit Erfolg — der
        Modus ist eine Betreiber-Entscheidung und kein Fehler.
        """
        service = self._service
        kind = RunKind.QUEUE_SEND
        result = CycleResult(kind=kind)
        run_id = await run_in_threadpool(start_run, service.db, kind)
        outcome = await self._execute(kind, service.config)
        result.outcome = outcome
        result.run = await self._finish_from_outcome(run_id, kind, outcome)

        report = outcome.report or {}
        gave_up = report.get("gave_up_track_ids") or []
        if gave_up:
            # Das Ereignis `upstream_forward_gave_up` entsteht im
            # API-Prozess (Phase 12) und steht nur in dessen Log; ueber den
            # Report kommt es hier an — mit denselben Feldern.
            service.notify.send_background(
                upstream_gave_up(
                    local_track_ids=[int(value) for value in gave_up],
                    forward_attempts=int(report.get("forward_attempts") or 0),
                    forward_error=report.get("forward_error"),
                )
            )
        if not outcome.ok:
            service.notify.send_background(
                import_failed(
                    kind.display_name,
                    result=outcome.report_result or outcome.result.value,
                    error=outcome.error_message,
                    run_id=run_id,
                )
            )
        return result

    async def _sleep_again(self, woke_stack: bool, requests_before: int) -> bool:
        """Legt den Stack schlafen — wenn der Zyklus ihn selbst geweckt hat.

        Zwei Bedingungen, beide notwendig:

        * **Wir haben ihn geweckt.** War er vorher wach, gehoert er jemand
          anderem: der Betreiber kann ihn ueber die Admin-UI gestartet
          haben, und ein Job darf ihm den Stack nicht unter den Haenden
          wegstoppen.
        * **Niemand hat ihn benutzt.** Kam waehrend des Laufs eine
          ``/v2/``-Anfrage, uebernimmt der Idle-Stopp — er hat die Uhr
          dafuer (§8.5). Sonst endete ein Lookup mitten im Satz, weil
          zufaellig gerade ein Import fertig wurde.
        """
        service = self._service
        if not woke_stack:
            _LOG.info("Stack bleibt wach — er lief schon vor dem Lauf")
            return False
        if service.activity.requests != requests_before:
            _LOG.info("Stack bleibt wach — waehrend des Laufs kamen Anfragen")
            return False
        return await service.wake.stop_stack(reason="scheduler")

    # --- Historie -----------------------------------------------------------

    async def _finish(
        self, run_id: int, kind: RunKind, result: RunResult, *, error: str | None = None
    ) -> UpdateRun:
        run = await run_in_threadpool(finish_run, self._service.db, run_id, result, error=error)
        self._announce(kind, run)
        return run

    async def _finish_from_outcome(
        self, run_id: int, kind: RunKind, outcome: JobOutcome
    ) -> UpdateRun:
        """Schreibt die Zahlen des Reports in die Historie.

        ``files_imported``/``rows_imported``/``last_sequence`` kommen aus
        dem Importer-Report (docs/importer-job.md); ein Backup- oder
        Warteschlangenlauf hat sie nicht und laesst sie auf 0 bzw. ``None``.
        """
        report = outcome.report or {}
        files = report.get("files") or {}
        days = report.get("days") or {}
        run = await run_in_threadpool(
            finish_run,
            self._service.db,
            run_id,
            outcome.result,
            files_imported=int(files.get("imported") or 0),
            rows_imported=int(report.get("rows") or 0),
            last_sequence=days.get("last"),
            error=outcome.error_message,
            report=outcome.report,
        )
        self._announce(kind, run)
        return run

    def _announce(self, kind: RunKind, run: UpdateRun) -> None:
        level = EventLevel.INFO if run.result is RunResult.SUCCESS else EventLevel.ERROR
        self._service.log_event(
            level,
            f"{kind.display_name} {run.result.display_name if run.result else 'beendet'}",
            {
                "run_id": run.id,
                "result": run.result.value if run.result else None,
                "files_imported": run.files_imported,
                "rows_imported": run.rows_imported,
                "last_sequence": run.last_sequence,
                "duration_s": run.duration_s,
                "error": run.error,
            },
            source=EVENT_SOURCE,
        )


class JobManager:
    """Genau ein Job gleichzeitig — und die interne Trigger-API (M8).

    Der Manager ist absichtlich klein: er haelt die laufende Aufgabe, sagt
    Nein zu einer zweiten und kann die laufende abbrechen. Alles andere
    macht der :class:`JobCycle`.
    """

    def __init__(self, cycle: JobCycle) -> None:
        self.cycle = cycle
        self._task: asyncio.Task[CycleResult] | None = None
        self._kind: RunKind | None = None
        #: Wie viele Laeufe dieser Prozess angestossen hat (Kennzahl, Tests).
        self.triggered = 0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def current_kind(self) -> RunKind | None:
        """Die Art des laufenden Jobs; ``None``, wenn keiner laeuft."""
        return self._kind if self.running else None

    def trigger(self, kind: RunKind, *, reason: str = "manual") -> bool:
        """Stoesst einen Lauf an, ohne auf ihn zu warten.

        **Die Trigger-API** aus der M2.5-Aufgabenliste: der Scheduler
        benutzt sie fuer faellige Termine, `/admin/jobs` (M8) fuer den
        Knopf daneben.

        Returns:
            ``False``, wenn schon ein Job laeuft — zwei Importer
            nebeneinander wuerden sich in ``import_state`` ins Gehege
            kommen.
        """
        if self.running:
            _LOG.info(
                "Lauf abgelehnt, es laeuft schon einer",
                extra={"wanted": kind.value, "running": self._kind.value if self._kind else None},
            )
            return False
        self._kind = kind
        self.triggered += 1
        task = asyncio.create_task(self.cycle.run(kind, reason=reason), name=f"job-{kind.value}")
        task.add_done_callback(_swallow)
        self._task = task
        return True

    async def wait(self) -> CycleResult | None:
        """Wartet auf den laufenden Lauf (Tests, Herunterfahren)."""
        task = self._task
        if task is None:
            return None
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:  # pragma: no cover - nur beim Abbruch
            return None

    async def cancel(self) -> bool:
        """Bricht den laufenden Lauf ab (Knopf „Abbrechen", M8).

        Erst der Subprozess — mit der grosszuegigen Frist —, dann die
        Aufgabe. Umgekehrt bliebe ein verwaister Importer stehen, der
        weiter in die Datenbank schreibt.
        """
        if not self.running:
            return False
        stopped = await self.cycle.runner.cancel()
        task = self._task
        if task is not None and not task.done():
            task.cancel()
        return stopped

    async def shutdown(self, *, timeout_s: float = SHUTDOWN_WAIT_S) -> bool:
        """Wartet beim Herunterfahren auf den Job — **ohne** eigenes Signal.

        Der Weg beim Herunterfahren des Waechters, und er unterscheidet
        sich von :meth:`cancel` in beide Richtungen:

        * **Kein zweites Signal.** Der Waechter laeuft unter
          ``stopasgroup=true``/``killasgroup=true`` (supervisord.conf), sein
          ``SIGTERM`` erreicht also die ganze Prozessgruppe und damit auch
          den Job. Ein zweites bedeutet im Importer „sofort beenden"
          (``acoustid_importer.__main__``) — aus dem geordneten Exit-Code 8
          wuerde eine zurueckgerollte Transaktion.
        * **Aber warten.** Endet der Waechter zuerst, wird der Job zum
          Waisen unter ``tini``: supervisord sieht seinen Hauptprozess weg
          und raeumt die Gruppe nicht mehr auf. Der Waise haelt dann eine
          Busy-Marke und eine offene ``update_run``-Zeile — genau die
          beiden Reste, die K1 und F7 beschreiben.

        Nach ``timeout_s`` wird die Aufgabe trotzdem losgelassen; den Rest
        erledigt Docker mit ``stop_grace_period``.

        Returns:
            ``True``, wenn eine laufende Aufgabe abgewartet oder
            losgelassen wurde.
        """
        task = self._task
        if task is None or task.done():
            return False
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout_s)
        except TimeoutError:
            _LOG.warning(
                "Job laeuft beim Herunterfahren noch — er wird losgelassen",
                extra={"job_kind": self._kind.value if self._kind else None},
            )
            task.cancel()
        except Exception:
            # Der Zyklus hat sich beschwert; das steht laengst im Log und
            # in der Lauf-Historie.
            _LOG.debug("Job endete beim Herunterfahren mit einer Ausnahme")
        return True


def _swallow(task: asyncio.Task[Any]) -> None:
    """Holt die Ausnahme einer beendeten Job-Aufgabe ab.

    Ohne diesen Abholer meldet asyncio „exception was never retrieved" —
    der Fehler selbst steht laengst im Log und in der Lauf-Historie.
    """
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        _LOG.error("Job-Zyklus mit Ausnahme beendet", exc_info=error)
