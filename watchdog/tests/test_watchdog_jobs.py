"""Jobs als Subprozesse: Runner, Zyklus, Trigger-API (M2.5, E10).

Der :class:`~acoustid_watchdog.jobs.JobRunner` wird gegen **echte**
Subprozesse geprueft — er ist genau die Naht zum Betriebssystem, und eine
Attrappe davor pruefte nichts. Die Jobs selbst sind dabei kurze
Python-Einzeiler, die einen Report schreiben und mit einem gewuenschten
Code enden: dieselbe Schnittstelle wie der Importer (docs/importer-job.md),
nur in Millisekunden.

Der :class:`~acoustid_watchdog.jobs.JobCycle` bekommt dagegen einen
eingesetzten Runner: dort geht es um den **Ablauf** (wecken, Historie,
Cache, schlafen legen), nicht mehr um Prozesse.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from watchdog_stubs import FakeSupervisor

from acoustid_watchdog.jobs import (
    INDEX_BUSY_FILENAME,
    JobCycle,
    JobManager,
    JobOutcome,
    JobRunner,
    job_command,
)
from acoustid_watchdog.notify import Notification, NotifyEvent
from acoustid_watchdog.runs import RunKind, RunResult, latest_run, running_runs
from acoustid_watchdog.service import WatchdogService
from shared.config import BackupConfig, Config, DiskConfig
from shared.models import StackState

# --- Werkzeuge --------------------------------------------------------------

#: Ein „Job", der einen Report schreibt und mit einem gewuenschten Code endet.
_JOB_SCRIPT = """
import json, pathlib, sys
report, code, payload = sys.argv[1], int(sys.argv[2]), sys.argv[3]
if payload != "-":
    pathlib.Path(report).write_text(payload, encoding="utf-8")
sys.exit(code)
"""


def _fake_job(report: Path, *, code: int = 0, payload: dict[str, Any] | None = None) -> list[str]:
    body = json.dumps(payload) if payload is not None else "-"
    return [sys.executable, "-c", _JOB_SCRIPT, str(report), str(code), body]


class FakeRunner:
    """Runner-Ersatz fuer die Zyklus-Tests — liefert ein vorgegebenes Ergebnis."""

    def __init__(self, outcomes: dict[str, JobOutcome] | None = None) -> None:
        self.outcomes = outcomes or {}
        self.commands: list[list[str]] = []
        self.default = JobOutcome(returncode=0, report={"result": "ok"})
        self.cancelled = False

    async def run(self, command, *, report: Path) -> JobOutcome:
        self.commands.append(list(command))
        # Der Modulname des Aufrufs entscheidet, welches Ergebnis kommt —
        # so kann ein Test Import und Warteschlangenlauf trennen.
        module = command[command.index("-m") + 1] if "-m" in command else ""
        return self.outcomes.get(module, self.default)

    async def cancel(self) -> bool:
        self.cancelled = True
        return True

    @property
    def running(self) -> bool:
        return False


def _cycle(
    service: WatchdogService,
    runner: FakeRunner | None = None,
    *,
    notifications: list[Notification] | None = None,
    min_free_gb: int = 0,
) -> tuple[JobCycle, FakeRunner]:
    """Zyklus mit eingesetztem Runner — und **abgeschaltetem** Plattenplatz-Guard.

    Der Vorgabewert aus §6 sind 100 GiB; ein Entwicklerrechner mit weniger
    freiem Platz wuerde sonst jeden Zyklus-Test in den Abbruch schicken,
    obwohl es um den Ablauf und nicht um die Platte geht. Die Tests **des
    Guards** setzen ihn ausdruecklich wieder an.
    """
    used = runner or FakeRunner()
    if notifications is not None:
        service.notify.send_background = notifications.append  # type: ignore[method-assign]
    _configure(service, disk=DiskConfig(min_free_gb=min_free_gb))
    return JobCycle(service, runner=used), used  # type: ignore[arg-type]


def _configure(service: WatchdogService, **changes: Any) -> Config:
    """Schreibt eine geaenderte Konfiguration und liefert sie zurueck."""
    config = service.config.model_copy(update=changes)
    service.config_store.save(config)
    return config


# --- Der Runner am echten Prozess -------------------------------------------


def test_a_job_report_is_read_back(tmp_path: Path) -> None:
    runner = JobRunner(cwd=tmp_path)
    report = tmp_path / "jobs" / "acoustid-delta.json"
    payload = {"result": "ok", "rows": 42, "files": {"imported": 3}, "days": {"last": "2026-07-22"}}

    outcome = asyncio.run(runner.run(_fake_job(report, payload=payload), report=report))

    assert outcome.ok is True
    assert outcome.result is RunResult.SUCCESS
    assert outcome.report == payload
    assert outcome.error_message is None


def test_a_failed_job_carries_its_error(tmp_path: Path) -> None:
    runner = JobRunner(cwd=tmp_path)
    report = tmp_path / "report.json"
    payload = {
        "result": "import_failed",
        "error": {"type": "ParseError", "message": "Zeile 128 ist kein JSON"},
    }

    outcome = asyncio.run(runner.run(_fake_job(report, code=6, payload=payload), report=report))

    assert outcome.ok is False
    assert outcome.result is RunResult.FAILED
    assert outcome.error_message == "ParseError: Zeile 128 ist kein JSON"


def test_a_disk_guard_abort_is_an_orderly_stop(tmp_path: Path) -> None:
    """Exit-Code 3 ist der Guard des Jobs — abgebrochen, nicht kaputt (§8.8)."""
    runner = JobRunner(cwd=tmp_path)
    report = tmp_path / "report.json"
    payload = {"result": "disk_guard", "error": {"type": "DiskSpaceError", "message": "zu voll"}}

    outcome = asyncio.run(runner.run(_fake_job(report, code=3, payload=payload), report=report))

    assert outcome.result is RunResult.ABORTED


def test_a_sigterm_exit_is_an_orderly_stop(tmp_path: Path) -> None:
    """Exit-Code 8: auf Wunsch beendet, Stand resumierbar."""
    runner = JobRunner(cwd=tmp_path)
    report = tmp_path / "report.json"
    outcome = asyncio.run(
        runner.run(_fake_job(report, code=8, payload={"result": "aborted"}), report=report)
    )
    assert outcome.result is RunResult.ABORTED


def test_a_missing_report_is_not_an_exception(tmp_path: Path) -> None:
    """Ein hart getoeteter Job hinterlaesst keinen — der Returncode bleibt."""
    runner = JobRunner(cwd=tmp_path)
    report = tmp_path / "report.json"

    outcome = asyncio.run(runner.run(_fake_job(report, code=1), report=report))

    assert outcome.report is None
    assert outcome.result is RunResult.FAILED
    assert outcome.error_message == "Exit-Code 1"


def test_a_stale_report_is_removed_before_the_run(tmp_path: Path) -> None:
    """Sonst laese der Waechter den Report des **vorherigen** Laufs."""
    runner = JobRunner(cwd=tmp_path)
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"result": "ok", "rows": 999}), encoding="utf-8")

    outcome = asyncio.run(runner.run(_fake_job(report, code=1), report=report))

    assert outcome.report is None


def test_a_broken_report_is_not_an_exception(tmp_path: Path) -> None:
    runner = JobRunner(cwd=tmp_path)
    report = tmp_path / "report.json"
    command = [sys.executable, "-c", f"open({str(report)!r}, 'w').write('kein json')"]

    outcome = asyncio.run(runner.run(command, report=report))

    assert outcome.report is None
    assert outcome.ok is True


def test_an_unstartable_command_is_reported(tmp_path: Path) -> None:
    runner = JobRunner(cwd=tmp_path)
    outcome = asyncio.run(
        runner.run([str(tmp_path / "gibt-es-nicht")], report=tmp_path / "report.json")
    )

    assert outcome.ok is False
    assert outcome.start_error is not None
    assert outcome.error_message == outcome.start_error


def test_cancel_terminates_a_running_job(tmp_path: Path) -> None:
    """Der Abbrechen-Knopf (M8): SIGTERM, und der Prozess ist wirklich weg."""
    runner = JobRunner(cwd=tmp_path, grace_s=10.0)
    command = [sys.executable, "-c", "import time; time.sleep(60)"]

    async def scenario() -> JobOutcome:
        task = asyncio.create_task(runner.run(command, report=tmp_path / "report.json"))
        # Warten, bis der Prozess wirklich laeuft.
        for _ in range(200):
            if runner.running:
                break
            await asyncio.sleep(0.01)
        assert await runner.cancel() is True
        return await task

    outcome = asyncio.run(scenario())
    assert outcome.ok is False
    assert runner.running is False


def test_cancel_without_a_job_says_so(tmp_path: Path) -> None:
    assert asyncio.run(JobRunner(cwd=tmp_path).cancel()) is False


# --- Die Kommandos ----------------------------------------------------------


def test_the_importer_is_called_with_a_report_path(
    tmp_path: Path, service: WatchdogService
) -> None:
    command = job_command(
        RunKind.ACOUSTID_DELTA,
        settings=service.settings,
        config=service.config,
        report=tmp_path / "r.json",
        python="/app/.venv/bin/python",
    )
    assert command[:4] == ["/app/.venv/bin/python", "-m", "acoustid_importer", "--report"]


def test_the_queue_job_lives_in_the_api_package(tmp_path: Path, service: WatchdogService) -> None:
    """Der Waechter darf die Datenbank nicht anfassen (§8.2) — der Subprozess schon."""
    command = job_command(
        RunKind.QUEUE_SEND,
        settings=service.settings,
        config=service.config,
        report=tmp_path / "r.json",
    )
    assert "acoustid_api.queuejob" in command


def test_the_backup_gets_its_target_and_the_cover_switch(
    tmp_path: Path, service: WatchdogService
) -> None:
    config = Config(backup=BackupConfig(dir=str(tmp_path / "backup"), include_covers=True))
    command = job_command(
        RunKind.BACKUP, settings=service.settings, config=config, report=tmp_path / "r.json"
    )
    assert "--target" in command and str(tmp_path / "backup") in command
    assert "--include-covers" in command


def test_the_cover_switch_stays_out_when_it_is_off(
    tmp_path: Path, service: WatchdogService
) -> None:
    config = Config(backup=BackupConfig(dir=str(tmp_path / "backup")))
    command = job_command(
        RunKind.BACKUP, settings=service.settings, config=config, report=tmp_path / "r.json"
    )
    assert "--include-covers" not in command


@pytest.mark.parametrize("kind", [RunKind.DISCOGS_DUMP, RunKind.CAA_CRAWL, RunKind.NACHZUEGLER])
def test_the_future_job_kinds_have_no_command_yet(
    kind: RunKind, tmp_path: Path, service: WatchdogService
) -> None:
    """Sie stehen im Schema, damit die Historie nicht auseinanderlaeuft (M3-M5)."""
    with pytest.raises(NotImplementedError, match=kind.value):
        job_command(
            kind, settings=service.settings, config=service.config, report=tmp_path / "r.json"
        )


# --- Der Zyklus -------------------------------------------------------------


def test_a_cycle_wakes_runs_and_sleeps(service: WatchdogService) -> None:
    """Die Definition of Done in einem Test: nach dem Lauf schlafen die Prozesse."""
    cycle, runner = _cycle(service)
    assert service.state.state is StackState.SLEEPING

    result = asyncio.run(cycle.run(RunKind.ACOUSTID_DELTA))

    assert result.ok is True
    assert result.woke_stack is True
    assert result.slept is True
    assert service.state.state is StackState.SLEEPING
    assert runner.commands  # der Job lief wirklich
    assert running_runs(service.db) == []


def test_the_history_gets_the_numbers_from_the_report(service: WatchdogService) -> None:
    outcome = JobOutcome(
        returncode=0,
        report={
            "result": "ok",
            "rows": 120_000,
            "files": {"imported": 7},
            "days": {"first": "2026-07-20", "last": "2026-07-22"},
        },
    )
    cycle, _runner = _cycle(service, FakeRunner({"acoustid_importer": outcome}))

    asyncio.run(cycle.run(RunKind.ACOUSTID_DELTA))

    run = latest_run(service.db, RunKind.ACOUSTID_DELTA)
    assert run is not None
    assert run.result is RunResult.SUCCESS
    assert run.files_imported == 7
    assert run.rows_imported == 120_000
    assert run.last_sequence == "2026-07-22"
    assert run.report is not None and run.report["rows"] == 120_000


def test_a_successful_import_empties_the_lookup_cache(
    service: WatchdogService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariante §8.6: nach dem Import ist jede gecachte Antwort veraltet."""
    reasons: list[str] = []
    monkeypatch.setattr(service, "invalidate_cache", lambda reason: reasons.append(reason) or 0)
    cycle, _runner = _cycle(service)

    asyncio.run(cycle.run(RunKind.ACOUSTID_DELTA))

    assert reasons == ["delta_import"]


def test_a_failed_import_keeps_the_cache_and_reports(service: WatchdogService) -> None:
    reasons: list[str] = []
    service.invalidate_cache = lambda reason: reasons.append(reason) or 0  # type: ignore[method-assign]
    notifications: list[Notification] = []
    outcome = JobOutcome(
        returncode=6, report={"result": "import_failed", "error": {"type": "X", "message": "y"}}
    )
    cycle, _runner = _cycle(
        service, FakeRunner({"acoustid_importer": outcome}), notifications=notifications
    )

    result = asyncio.run(cycle.run(RunKind.ACOUSTID_DELTA))

    assert result.ok is False
    assert reasons == []
    assert [item.event for item in notifications] == [NotifyEvent.IMPORT_FAILED]
    run = latest_run(service.db, RunKind.ACOUSTID_DELTA)
    assert run is not None and run.result is RunResult.FAILED
    # Auch ein gescheiterter Lauf legt den Stack wieder schlafen: er hat
    # ihn geweckt, also raeumt er ihn auch weg.
    assert result.slept is True


def test_too_little_disk_space_aborts_before_the_first_byte(
    service: WatchdogService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E11: geprueft wird jeder Schreibpfad, und zwar **vor** dem Wecken."""
    notifications: list[Notification] = []
    cycle, runner = _cycle(service, notifications=notifications)
    _configure(service, disk=DiskConfig(min_free_gb=100))

    class Tiny:
        total = 1000 * (1 << 30)
        free = 3 * (1 << 30)

    monkeypatch.setattr("acoustid_watchdog.diskspace.shutil.disk_usage", lambda _p: Tiny())

    result = asyncio.run(cycle.run(RunKind.ACOUSTID_DELTA))

    assert runner.commands == []
    assert service.state.state is StackState.SLEEPING
    run = latest_run(service.db, RunKind.ACOUSTID_DELTA)
    assert run is not None and run.result is RunResult.ABORTED
    assert "gefordert" in (run.error or "")
    assert [item.event for item in notifications] == [NotifyEvent.DISK_LOW]
    assert result.woke_stack is False


def test_the_guard_is_off_when_min_free_gb_is_zero(
    service: WatchdogService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(service, disk=DiskConfig(min_free_gb=0))

    class Tiny:
        total = 1000 * (1 << 30)
        free = 0

    monkeypatch.setattr("acoustid_watchdog.diskspace.shutil.disk_usage", lambda _p: Tiny())
    cycle, runner = _cycle(service)

    assert asyncio.run(cycle.run(RunKind.ACOUSTID_DELTA)).ok is True
    assert runner.commands


def test_a_stack_that_was_already_awake_stays_awake(service: WatchdogService) -> None:
    """Ein Job darf dem Betreiber den Stack nicht unter den Haenden wegstoppen."""

    async def scenario() -> Any:
        await service.wake.ensure_ready(timeout_s=5)
        cycle, _runner = _cycle(service)
        return await cycle.run(RunKind.ACOUSTID_DELTA)

    result = asyncio.run(scenario())

    assert result.woke_stack is False
    assert result.slept is False
    assert service.state.state is StackState.READY


def test_requests_during_the_run_keep_the_stack_awake(service: WatchdogService) -> None:
    """Sonst endete ein Lookup mitten im Satz, weil zufaellig ein Import fertig wurde."""

    class BusyRunner(FakeRunner):
        async def run(self, command, *, report: Path) -> JobOutcome:
            service.activity.touch()  # eine /v2/-Anfrage waehrend des Laufs
            return await super().run(command, report=report)

    cycle, _runner = _cycle(service, BusyRunner())
    result = asyncio.run(cycle.run(RunKind.ACOUSTID_DELTA))

    assert result.woke_stack is True
    assert result.slept is False
    assert service.state.state is StackState.READY


def test_a_stack_that_never_becomes_ready_fails_the_run(
    service: WatchdogService, supervisor: FakeSupervisor
) -> None:
    supervisor.fail_on.add("db")
    notifications: list[Notification] = []
    cycle, runner = _cycle(service, notifications=notifications)

    result = asyncio.run(cycle.run(RunKind.ACOUSTID_DELTA))

    assert result.ok is False
    assert runner.commands == []
    run = latest_run(service.db, RunKind.ACOUSTID_DELTA)
    assert run is not None and run.result is RunResult.FAILED
    assert "Stack nicht bereit" in (run.error or "")
    # Zwei Meldungen: der Weckvorgang meldet den Startfehler, der Zyklus
    # den gescheiterten Lauf.
    assert NotifyEvent.IMPORT_FAILED in {item.event for item in notifications}


# --- Zurueckgestellte Submits (Betreiber-Entscheid 2026-08-05) --------------


def test_the_import_holds_the_index_busy_marker(service: WatchdogService) -> None:
    """Waehrend des Delta-Imports wird die Indexierung zurueckgestellt.

    Sonst erhoehte ein Submit die Index-Version, und der Index-Feed des
    Importers braeche an seinem ``expected_version``-Guard ab — der Lauf
    endete als Fehler und kostete einen Tag Datenstand.
    """
    marker = service.settings.data_dir / INDEX_BUSY_FILENAME
    seen: list[bool] = []

    class WatchingRunner(FakeRunner):
        async def run(self, command, *, report: Path) -> JobOutcome:
            seen.append(marker.exists())
            return await super().run(command, report=report)

    cycle, _runner = _cycle(service, WatchingRunner())
    asyncio.run(cycle.run(RunKind.ACOUSTID_DELTA))

    assert seen[0] is True  # waehrend des Imports
    assert marker.exists() is False  # danach weg


def test_the_marker_is_removed_even_when_the_job_explodes(service: WatchdogService) -> None:
    """Bliebe sie liegen, waeren eigene Einreichungen fuer immer unauffindbar."""
    marker = service.settings.data_dir / INDEX_BUSY_FILENAME

    class ExplodingRunner(FakeRunner):
        async def run(self, command, *, report: Path) -> JobOutcome:
            raise RuntimeError("Runner kaputt")

    cycle, _runner = _cycle(service, ExplodingRunner())
    with pytest.raises(RuntimeError):
        asyncio.run(cycle.run(RunKind.ACOUSTID_DELTA))

    assert marker.exists() is False


def test_only_the_import_sets_the_marker(service: WatchdogService, tmp_path: Path) -> None:
    """Ein Backup schreibt nicht in den Suchindex — es stoert dort niemanden."""
    _configure(service, backup=BackupConfig(dir=str(tmp_path / "backup")))
    marker = service.settings.data_dir / INDEX_BUSY_FILENAME
    seen: list[bool] = []

    class WatchingRunner(FakeRunner):
        async def run(self, command, *, report: Path) -> JobOutcome:
            seen.append(marker.exists())
            return await super().run(command, report=report)

    cycle, _runner = _cycle(service, WatchingRunner())
    asyncio.run(cycle.run(RunKind.BACKUP))

    assert seen == [False]


def test_both_sides_use_the_same_marker_name() -> None:
    """Der Waechter setzt sie, der API-Dienst liest sie — ohne Paketabhaengigkeit."""
    from acoustid_api.submit import INDEX_BUSY_FILENAME as API_SIDE

    assert INDEX_BUSY_FILENAME == API_SIDE


# --- Der Warteschlangenlauf danach ------------------------------------------


def test_a_successful_import_is_followed_by_the_queue_run(service: WatchdogService) -> None:
    """§8.9: die Warteschlange wird im selben wachen Fenster abgearbeitet."""
    cycle, runner = _cycle(service)

    result = asyncio.run(cycle.run(RunKind.ACOUSTID_DELTA))

    assert [item.kind for item in result.followups] == [RunKind.QUEUE_SEND]
    modules = [command[command.index("-m") + 1] for command in runner.commands]
    assert modules == ["acoustid_importer", "acoustid_api.queuejob"]
    queue_run = latest_run(service.db, RunKind.QUEUE_SEND)
    assert queue_run is not None and queue_run.result is RunResult.SUCCESS


def test_a_failed_import_skips_the_queue_run(service: WatchdogService) -> None:
    outcome = JobOutcome(returncode=6, report={"result": "import_failed"})
    cycle, _runner = _cycle(service, FakeRunner({"acoustid_importer": outcome}))

    result = asyncio.run(cycle.run(RunKind.ACOUSTID_DELTA))

    assert result.followups == []
    assert latest_run(service.db, RunKind.QUEUE_SEND) is None


def test_given_up_submissions_are_reported(service: WatchdogService) -> None:
    """Das Ereignis aus Phase 12 kommt ueber den Report des Subprozesses an."""
    notifications: list[Notification] = []
    outcome = JobOutcome(
        returncode=0,
        report={
            "result": "ok",
            "gave_up": 2,
            "gave_up_track_ids": [17, 18],
            "forward_attempts": 7,
            "forward_error": "HTTP 500",
        },
    )
    cycle, _runner = _cycle(
        service, FakeRunner({"acoustid_api.queuejob": outcome}), notifications=notifications
    )

    asyncio.run(cycle.run(RunKind.ACOUSTID_DELTA))

    gave_up = [item for item in notifications if item.event is NotifyEvent.UPSTREAM_GAVE_UP]
    assert len(gave_up) == 1
    assert gave_up[0].fields["local_track_id"] == "17, 18"
    assert gave_up[0].fields["forward_attempts"] == 7
    assert gave_up[0].fields["forward_error"] == "HTTP 500"


def test_a_quiet_queue_run_reports_nothing(service: WatchdogService) -> None:
    notifications: list[Notification] = []
    cycle, _runner = _cycle(service, notifications=notifications)

    asyncio.run(cycle.run(RunKind.ACOUSTID_DELTA))

    assert notifications == []


# --- Die Sicherung ----------------------------------------------------------


def test_a_backup_run_is_its_own_history_entry(service: WatchdogService, tmp_path: Path) -> None:
    _configure(service, backup=BackupConfig(dir=str(tmp_path / "backup")))
    cycle, runner = _cycle(service)

    result = asyncio.run(cycle.run(RunKind.BACKUP))

    assert result.ok is True
    assert result.followups == []  # kein Warteschlangenlauf nach der Sicherung
    assert "acoustid_importer.backup" in runner.commands[0]
    run = latest_run(service.db, RunKind.BACKUP)
    assert run is not None and run.result is RunResult.SUCCESS


def test_a_running_backup_blocks_the_idle_stop(service: WatchdogService, tmp_path: Path) -> None:
    """Invariante §8.5 — die Sicherung meldet sich ueber `update_run` an."""
    _configure(service, backup=BackupConfig(dir=str(tmp_path / "backup")))
    seen: list[list[str]] = []

    class WatchingRunner(FakeRunner):
        async def run(self, command, *, report: Path) -> JobOutcome:
            seen.append(service.jobs.running_jobs())
            return await super().run(command, report=report)

    cycle, _runner = _cycle(service, WatchingRunner())
    asyncio.run(cycle.run(RunKind.BACKUP))

    assert seen and seen[0] and seen[0][0].startswith("Backup #")
    assert service.jobs.running_jobs() == []


# --- Die Trigger-API --------------------------------------------------------


def test_the_trigger_api_starts_a_run(service: WatchdogService) -> None:
    cycle, runner = _cycle(service)
    manager = JobManager(cycle)

    async def scenario() -> Any:
        assert manager.trigger(RunKind.ACOUSTID_DELTA, reason="manual") is True
        assert manager.running is True
        assert manager.current_kind is RunKind.ACOUSTID_DELTA
        return await manager.wait()

    result = asyncio.run(scenario())
    assert result is not None and result.ok is True
    assert manager.triggered == 1
    assert runner.commands


def test_only_one_job_runs_at_a_time(service: WatchdogService) -> None:
    """Zwei Importer nebeneinander kaemen sich in `import_state` ins Gehege."""
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowRunner(FakeRunner):
        async def run(self, command, *, report: Path) -> JobOutcome:
            started.set()
            await release.wait()
            return await super().run(command, report=report)

    cycle, _runner = _cycle(service, SlowRunner())
    manager = JobManager(cycle)

    async def scenario() -> None:
        assert manager.trigger(RunKind.ACOUSTID_DELTA) is True
        await started.wait()
        assert manager.trigger(RunKind.BACKUP) is False
        release.set()
        await manager.wait()

    asyncio.run(scenario())
    assert manager.triggered == 1


def test_waiting_without_a_job_returns_nothing(service: WatchdogService) -> None:
    cycle, _runner = _cycle(service)
    assert asyncio.run(JobManager(cycle).wait()) is None


def test_abandon_lets_the_subprocess_keep_its_signal(service: WatchdogService) -> None:
    """Beim Herunterfahren schickt der Waechter **kein** zweites SIGTERM.

    Das erste kommt von supervisord an die ganze Prozessgruppe
    (`stopasgroup=true`); ein zweites bedeutete im Importer „sofort
    beenden" — statt Exit-Code 8 gaebe es eine zurueckgerollte Transaktion.
    """
    release = asyncio.Event()

    class SlowRunner(FakeRunner):
        async def run(self, command, *, report: Path) -> JobOutcome:
            await release.wait()
            return await super().run(command, report=report)

    slow = SlowRunner()
    cycle, _runner = _cycle(service, slow)
    manager = JobManager(cycle)

    async def scenario() -> bool:
        manager.trigger(RunKind.ACOUSTID_DELTA)
        await asyncio.sleep(0)
        abandoned = manager.abandon()
        await asyncio.sleep(0)
        return abandoned

    assert asyncio.run(scenario()) is True
    assert slow.cancelled is False  # kein Signal an den Prozess


def test_cancel_stops_the_subprocess_first(service: WatchdogService) -> None:
    """Der Abbrechen-Knopf: erst der Prozess, dann die Aufgabe."""
    release = asyncio.Event()

    class SlowRunner(FakeRunner):
        async def run(self, command, *, report: Path) -> JobOutcome:
            await release.wait()
            return await super().run(command, report=report)

    slow = SlowRunner()
    cycle, _runner = _cycle(service, slow)
    manager = JobManager(cycle)

    async def scenario() -> bool:
        manager.trigger(RunKind.ACOUSTID_DELTA)
        await asyncio.sleep(0)
        stopped = await manager.cancel()
        release.set()
        await asyncio.sleep(0)
        return stopped

    assert asyncio.run(scenario()) is True
    assert slow.cancelled is True


def test_cancelling_nothing_says_so(service: WatchdogService) -> None:
    cycle, _runner = _cycle(service)
    manager = JobManager(cycle)
    assert asyncio.run(manager.cancel()) is False
    assert manager.abandon() is False
    assert manager.current_kind is None
