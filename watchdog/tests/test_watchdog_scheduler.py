"""Der Zeitplan: Faelligkeit, Nachholen, Wiederholung (M2.5).

Die Uhr ist hier immer eine Attrappe: ein Test, der auf 04:00 wartet, ist
keiner. Geprueft wird stattdessen die eigentliche Regel — *„seit dem
heutigen Termin lief noch keiner"* — gegen die Historie, also gegen genau
das, was einen Neustart ueberlebt.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from acoustid_watchdog.runs import RunKind, RunResult, finish_run, start_run
from acoustid_watchdog.scheduler import (
    SCHEDULE,
    ScheduledJob,
    Scheduler,
    next_due_at,
    utc_boundary,
)
from acoustid_watchdog.store import Database
from shared.config import (
    AcoustidConfig,
    AcoustidUpdateConfig,
    BackupConfig,
    Config,
)

#: Ein fester Tag in der Zeitzone der Maschine — die Termine aus §6 stehen
#: in lokaler Zeit, die Historie in UTC.
DAY = datetime(2026, 8, 5, tzinfo=UTC).astimezone()


def _at(hour: int, minute: int = 0) -> datetime:
    return DAY.replace(hour=hour, minute=minute, second=0, microsecond=0)


#: Vorgabezeitpunkt der Tests: nach dem Import-Termin, vor dem Backup-Termin.
AFTER_IMPORT_SLOT = DAY.replace(hour=4, minute=30, second=0, microsecond=0)


class FakeManager:
    """Trigger-API-Attrappe: merkt sich, was angestossen wurde."""

    def __init__(self, *, busy: bool = False, accept: bool = True) -> None:
        self.busy = busy
        self.accept = accept
        self.calls: list[tuple[RunKind, str]] = []

    @property
    def running(self) -> bool:
        return self.busy

    def trigger(self, kind: RunKind, *, reason: str = "manual") -> bool:
        self.calls.append((kind, reason))
        return self.accept


def _scheduler(
    db: Database,
    manager: FakeManager,
    *,
    config: Config | None = None,
    now: datetime = AFTER_IMPORT_SLOT,
    schedule: tuple[ScheduledJob, ...] = SCHEDULE,
) -> Scheduler:
    current = config if config is not None else Config()
    return Scheduler(
        db,
        manager,  # type: ignore[arg-type]
        lambda: current,
        schedule=schedule,
        clock=lambda: now,
    )


def _config(update_time: str = "04:00", *, backup_dir: str = "", backup_time: str = "04:45"):
    return Config(
        acoustid=AcoustidConfig(update=AcoustidUpdateConfig(time=update_time)),
        backup=BackupConfig(dir=backup_dir, time=backup_time),
    )


# --- Der Termin selbst ------------------------------------------------------


def test_the_daily_slot_keeps_the_timezone() -> None:
    slot = next_due_at(_at(23, 17), "04:00")
    assert (slot.hour, slot.minute, slot.second) == (4, 0, 0)
    assert slot.tzinfo == DAY.tzinfo


def test_the_boundary_is_the_format_of_the_history() -> None:
    """Verglichen wird auf der Zeichenkette — sie muss exakt passen."""
    boundary = utc_boundary(_at(4, 0))
    assert boundary.endswith("Z")
    assert "T" in boundary and boundary.count(":") == 2
    # Dasselbe Format wie `store.utc_now()`.
    import re

    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", boundary)


# --- Faelligkeit ------------------------------------------------------------


def test_before_the_slot_nothing_runs(db: Database) -> None:
    manager = FakeManager()
    scheduler = _scheduler(db, manager, config=_config("04:00"), now=_at(3, 59))

    assert asyncio.run(scheduler.check()) is None
    assert manager.calls == []


def test_at_the_slot_the_import_starts(db: Database) -> None:
    manager = FakeManager()
    scheduler = _scheduler(db, manager, config=_config("04:00"), now=_at(4, 0))

    assert asyncio.run(scheduler.check()) is RunKind.ACOUSTID_DELTA
    assert manager.calls == [(RunKind.ACOUSTID_DELTA, "scheduler")]
    assert scheduler.triggered == 1


def test_a_missed_slot_is_caught_up(db: Database) -> None:
    """Startet der Container um 06:00, laeuft der 04:00-Import sofort.

    Ein verspaeteter Datenabgleich ist besser als keiner — und der Zyklus
    legt den Stack anschliessend wieder schlafen.
    """
    manager = FakeManager()
    scheduler = _scheduler(db, manager, config=_config("04:00"), now=_at(6, 0))

    assert asyncio.run(scheduler.check()) is RunKind.ACOUSTID_DELTA


def test_a_run_since_the_slot_makes_it_not_due(db: Database) -> None:
    run_id = start_run(db, RunKind.ACOUSTID_DELTA, started_at=utc_boundary(_at(4, 1)))
    finish_run(db, run_id, RunResult.SUCCESS)
    manager = FakeManager()
    scheduler = _scheduler(db, manager, config=_config("04:00"), now=_at(4, 30))

    assert asyncio.run(scheduler.check()) is None


def test_a_run_before_the_slot_does_not_count(db: Database) -> None:
    """Ein manueller Lauf um 03:00 nimmt dem 04:00-Termin nichts weg."""
    run_id = start_run(db, RunKind.ACOUSTID_DELTA, started_at=utc_boundary(_at(3, 0)))
    finish_run(db, run_id, RunResult.SUCCESS)
    manager = FakeManager()
    scheduler = _scheduler(db, manager, config=_config("04:00"), now=_at(4, 5))

    assert asyncio.run(scheduler.check()) is RunKind.ACOUSTID_DELTA


def test_a_failed_run_is_not_repeated_the_same_day(db: Database) -> None:
    """Invariante §8.4: wiederholt wird beim **naechsten** Zyklus.

    Die haeufigsten Ursachen — kein Netz, volle Platte, Lueckenbefund —
    sind am selben Tag meist dieselben; ein Wiederholungslauf im
    Minutentakt hielte nur das Array wach.
    """
    run_id = start_run(db, RunKind.ACOUSTID_DELTA, started_at=utc_boundary(_at(4, 0)))
    finish_run(db, run_id, RunResult.FAILED, error="kein Netz")
    manager = FakeManager()

    assert asyncio.run(_scheduler(db, manager, config=_config(), now=_at(5, 0)).check()) is None
    assert asyncio.run(_scheduler(db, manager, config=_config(), now=_at(23, 0)).check()) is None


def test_an_aborted_run_also_uses_up_its_slot(db: Database) -> None:
    """Ein Abbruch zaehlt wie jeder andere Lauf — der Termin ist verbraucht.

    Der Plattenplatz-Guard bricht ab, weil kein Platz da ist (§8.8); der
    kommt nicht von selbst zurueck. Sofort erneut zu starten reparierte
    nichts und hielte nur das Array wach — dieselbe Begruendung wie beim
    Fehlschlag (§8.4).
    """
    run_id = start_run(db, RunKind.ACOUSTID_DELTA, started_at=utc_boundary(_at(4, 0)))
    finish_run(db, run_id, RunResult.ABORTED, error="Plattenplatz zu knapp")
    manager = FakeManager()

    assert asyncio.run(_scheduler(db, manager, config=_config(), now=_at(5, 0)).check()) is None


def test_a_new_slot_after_the_run_makes_it_due_again(db: Database) -> None:
    """Ein **neuer** Termin ist eine neue Gelegenheit — auch am selben Tag.

    Der Weg des Betreibers, der die Ursache beseitigt hat (Platz
    geschaffen, Guard gelockert) und nicht bis morgen warten will: er
    setzt die Uhrzeit neu. Entscheidend ist, dass der neue Termin **nach**
    dem letzten Lauf liegt — sonst ist er derselbe verbrauchte Termin.
    """
    run_id = start_run(db, RunKind.ACOUSTID_DELTA, started_at=utc_boundary(_at(4, 30)))
    finish_run(db, run_id, RunResult.ABORTED, error="Plattenplatz zu knapp")
    manager = FakeManager()

    # Ein Termin **vor** dem Lauf ist verbraucht — der Lauf liegt danach.
    before = _scheduler(db, manager, config=_config("04:15"), now=_at(5, 0))
    assert asyncio.run(before.check()) is None

    # Ein Termin **nach** dem Lauf ist wieder faellig.
    after = _scheduler(db, manager, config=_config("04:45"), now=_at(5, 0))
    assert asyncio.run(after.check()) is RunKind.ACOUSTID_DELTA


def test_the_next_day_repeats_the_failed_run(db: Database) -> None:
    """Und genau dann wird wiederholt — auf dem Stand, den `import_state` haelt."""
    run_id = start_run(db, RunKind.ACOUSTID_DELTA, started_at=utc_boundary(_at(4, 0)))
    finish_run(db, run_id, RunResult.FAILED, error="kein Netz")
    manager = FakeManager()
    tomorrow = _at(4, 0) + timedelta(days=1)

    scheduler = _scheduler(db, manager, config=_config(), now=tomorrow)
    assert asyncio.run(scheduler.check()) is RunKind.ACOUSTID_DELTA


def test_an_unfinished_run_also_counts_as_done_for_today(db: Database) -> None:
    """Ein Lauf ohne Ergebnis laeuft noch — er darf nicht doppelt starten."""
    start_run(db, RunKind.ACOUSTID_DELTA, started_at=utc_boundary(_at(4, 0)))
    manager = FakeManager()

    assert asyncio.run(_scheduler(db, manager, config=_config(), now=_at(4, 30)).check()) is None


# --- Die Sicherung ----------------------------------------------------------


def test_without_a_backup_directory_no_backup_runs(db: Database) -> None:
    """Leeres ``backup.dir`` heisst „aus" — die Regel des ganzen Schemas (§6)."""
    manager = FakeManager()
    scheduler = _scheduler(db, manager, config=_config(backup_dir=""), now=_at(23, 0))

    # Der Import ist laengst gelaufen, die Sicherung waere jetzt faellig.
    run_id = start_run(db, RunKind.ACOUSTID_DELTA, started_at=utc_boundary(_at(4, 0)))
    finish_run(db, run_id, RunResult.SUCCESS)

    assert asyncio.run(scheduler.check()) is None


def test_with_a_backup_directory_the_backup_runs(db: Database, tmp_path: Path) -> None:
    manager = FakeManager()
    config = _config(backup_dir=str(tmp_path / "backup"), backup_time="04:45")
    run_id = start_run(db, RunKind.ACOUSTID_DELTA, started_at=utc_boundary(_at(4, 0)))
    finish_run(db, run_id, RunResult.SUCCESS)
    scheduler = _scheduler(db, manager, config=config, now=_at(4, 50))

    assert asyncio.run(scheduler.check()) is RunKind.BACKUP


def test_the_import_has_precedence_over_the_backup(db: Database, tmp_path: Path) -> None:
    """Gesichert wird der neue Stand, nicht der alte — deshalb erst der Import."""
    manager = FakeManager()
    config = _config(backup_dir=str(tmp_path / "backup"))
    scheduler = _scheduler(db, manager, config=config, now=_at(6, 0))

    assert asyncio.run(scheduler.check()) is RunKind.ACOUSTID_DELTA
    assert [kind for kind, _reason in manager.calls] == [RunKind.ACOUSTID_DELTA]


# --- Zusammenspiel mit dem Manager ------------------------------------------


def test_a_running_job_defers_the_next_slot(db: Database, tmp_path: Path) -> None:
    """Ein Import darf laenger dauern als bis zum naechsten Termin."""
    manager = FakeManager(busy=True)
    config = _config(backup_dir=str(tmp_path / "backup"))
    scheduler = _scheduler(db, manager, config=config, now=_at(4, 50))

    assert asyncio.run(scheduler.check()) is None
    assert manager.calls == []


def test_a_refused_trigger_is_not_counted(db: Database) -> None:
    manager = FakeManager(accept=False)
    scheduler = _scheduler(db, manager, config=_config(), now=_at(4, 5))

    assert asyncio.run(scheduler.check()) is None
    assert manager.calls == [(RunKind.ACOUSTID_DELTA, "scheduler")]
    assert scheduler.triggered == 0


# --- Widrigkeiten -----------------------------------------------------------


def test_an_unreadable_configuration_falls_back_to_the_defaults(db: Database) -> None:
    """Eine kaputte config.yaml darf den Zeitplan nicht anhalten."""

    def broken() -> Config:
        raise RuntimeError("config.yaml unlesbar")

    manager = FakeManager()
    scheduler = Scheduler(db, manager, broken, clock=lambda: _at(4, 5))  # type: ignore[arg-type]

    # Der Vorgabewert ist 04:00, also ist der Termin erreicht.
    assert asyncio.run(scheduler.check()) is RunKind.ACOUSTID_DELTA


def test_a_broken_time_skips_the_slot_instead_of_stopping(db: Database) -> None:
    """Von Hand kaputtgemachte Uhrzeit: der Termin faellt aus, nicht der Waechter."""
    broken_job = ScheduledJob(
        kind=RunKind.ACOUSTID_DELTA, time_of_day=lambda _config: "keine Uhrzeit"
    )
    manager = FakeManager()
    scheduler = _scheduler(db, manager, now=_at(12, 0), schedule=(broken_job,))

    assert asyncio.run(scheduler.check()) is None
    assert manager.calls == []


def test_the_loop_survives_a_failing_check(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    """Eine Hintergrundschleife darf an nichts sterben — sonst liefe nie wieder etwas.

    Gewartet wird auf den **zweiten** Aufruf und nicht auf eine Uhr: eine
    Zeitschranke waere auf einer ausgelasteten Maschine ein Flake, und
    geprueft werden soll ja gerade, dass es ueberhaupt weitergeht.
    """
    manager = FakeManager()
    scheduler = _scheduler(db, manager)
    scheduler.interval_s = 0.01
    calls = 0
    second = asyncio.Event()

    async def explode() -> Any:
        nonlocal calls
        calls += 1
        if calls >= 2:
            second.set()
        raise RuntimeError("kaputt")

    monkeypatch.setattr(scheduler, "check", explode)

    async def scenario() -> None:
        task = asyncio.create_task(scheduler.run())
        try:
            await asyncio.wait_for(second.wait(), timeout=10)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(scenario())
    assert calls >= 2  # nach dem ersten Fehler lief sie weiter


# --- Der ausgelieferte Zeitplan ---------------------------------------------


def test_the_shipped_schedule_holds_the_two_documented_slots() -> None:
    """§6 kennt genau zwei Termine; `discogs.update.check_time` hat noch keinen Job."""
    assert [job.kind for job in SCHEDULE] == [RunKind.ACOUSTID_DELTA, RunKind.BACKUP]
    config = Config()
    assert SCHEDULE[0].time_of_day(config) == "04:00"
    assert SCHEDULE[1].time_of_day(config) == "04:45"
    assert SCHEDULE[0].enabled(config) is True
    assert SCHEDULE[1].enabled(config) is False  # backup.dir ist leer


def test_the_time_of_day_is_read_from_the_current_configuration() -> None:
    """Eine Aenderung in der Admin-UI wirkt ohne Neustart."""
    getter: Callable[[Config], str] = SCHEDULE[0].time_of_day
    assert getter(_config("02:15")) == "02:15"
