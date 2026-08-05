"""Idle-Stopp und Zustandsabgleich — die beiden Dauerlaeufer (Phase 16).

Wie in den Weck-Tests laeuft jedes Szenario ohne ``pytest-asyncio`` ueber
:func:`asyncio.run`. Die Zeit steht dabei still: der Idle-Stopp bekommt
eine **gestellte Uhr** (:class:`FakeClock`), sonst wuerde ein Test der
15-Minuten-Frist fuenfzehn Minuten dauern.

Geprueft wird gegen den echten :class:`WakeCoordinator` mit der
supervisord-Attrappe — die Stopps gehen also wirklich durch die
Fault-Uebersetzung des ``SupervisorClient`` und die Reihenfolge des
``ServiceGroupController``.
"""

from __future__ import annotations

import asyncio
import threading

import pytest
from watchdog_stubs import (
    FakeProbe,
    FakeSupervisor,
    ProcessState,
    controller,
    running_stack,
    sleeping_stack,
    supervisor_client,
)

from acoustid_watchdog.events import EventLevel
from acoustid_watchdog.lifecycle import (
    ActivityTracker,
    DatabaseJobs,
    IdleStopper,
    StatePoller,
)
from acoustid_watchdog.process import SupervisorClient
from acoustid_watchdog.runs import RunKind, RunResult, finish_run, start_run
from acoustid_watchdog.stack import STACK_PROCESSES, ServiceGroupController
from acoustid_watchdog.state import StackStateTracker
from acoustid_watchdog.store import Database
from acoustid_watchdog.wake import WakeCoordinator
from shared.config import Config
from shared.models import StackState

TIMEOUT_S = Config().idle.timeout_min * 60  # 15 min, ARCHITECTURE §6


class FakeClock:
    """Eine Uhr, die nur vorgeht, wenn der Test sie vorstellt."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeJobs:
    """Auskunft ueber laufende Jobs (ab Phase 19/21 echte Laeufe)."""

    def __init__(self, *jobs: str) -> None:
        self.jobs = list(jobs)

    def running_jobs(self) -> list[str]:
        return list(self.jobs)


class BlockingController(ServiceGroupController):
    """Ein Controller, dessen ``stop`` haengt, bis der Test ihn freigibt."""

    def __init__(self, supervisor: SupervisorClient) -> None:
        super().__init__(supervisor)
        self.entered = threading.Event()
        self.release = threading.Event()

    def stop(self) -> list[str]:
        self.entered.set()
        self.release.wait(timeout=5)
        return super().stop()


def ready_coordinator(
    supervisor: FakeSupervisor,
    *,
    probe: FakeProbe | None = None,
    events: list[tuple[EventLevel, str]] | None = None,
    stack: ServiceGroupController | None = None,
) -> tuple[WakeCoordinator, StackStateTracker]:
    """Koordinator auf einem laufenden Stack, Zustand bereits ``bereit``."""
    state = StackStateTracker.sleeping()
    coordinator = WakeCoordinator(
        stack if stack is not None else controller(supervisor),
        probe or FakeProbe(),  # type: ignore[arg-type]
        state,
        log_event=(lambda level, message, extra=None: events.append((level, message)))
        if events is not None
        else None,
        poll_interval_s=0.01,
    )
    if supervisor.all_running:
        # Derselbe Weg wie beim Start des Waechters: Zustand aus der Steuerung.
        coordinator.refresh()
    return coordinator, state


def stopper(
    coordinator: WakeCoordinator,
    state: StackStateTracker,
    activity: ActivityTracker,
    jobs: FakeJobs | None = None,
) -> IdleStopper:
    return IdleStopper(coordinator, state, activity, jobs or FakeJobs(), Config)


# --- Die Uhr ----------------------------------------------------------------


def test_activity_tracker_measures_the_gap() -> None:
    clock = FakeClock()
    activity = ActivityTracker(clock=clock)

    clock.advance(30)
    assert activity.idle_s == 30

    activity.touch()
    assert activity.idle_s == 0
    assert activity.requests == 1


def test_a_fresh_tracker_counts_as_just_used() -> None:
    """Sonst waere der erste Blick nach einem Neustart sofort faellig."""
    assert ActivityTracker().idle_s < 1


# --- Job-Auskunft -----------------------------------------------------------


def test_database_jobs_sees_running_runs(db: Database) -> None:
    """Die Schnittstelle, ueber die sich Phase 19/21 anmelden."""
    jobs = DatabaseJobs(db)
    assert jobs.running_jobs() == []

    run_id = start_run(db, RunKind.UPDATE)
    assert jobs.running_jobs() == [f"Update #{run_id}"]

    finish_run(db, run_id, RunResult.SUCCESS)
    assert jobs.running_jobs() == []


def test_database_jobs_sees_backups_too(db: Database) -> None:
    run_id = start_run(db, RunKind.BACKUP)
    assert DatabaseJobs(db).running_jobs() == [f"Backup #{run_id}"]


# --- Idle-Stopp -------------------------------------------------------------


def test_idle_stop_fires_after_the_timeout() -> None:
    """Der Kern der Phase: ohne Nutzung geht der Stack wieder schlafen."""
    supervisor = running_stack()
    events: list[tuple[EventLevel, str]] = []
    coordinator, state = ready_coordinator(supervisor, events=events)
    clock = FakeClock()
    activity = ActivityTracker(clock=clock)
    idle = stopper(coordinator, state, activity)

    assert state.state is StackState.READY
    clock.advance(TIMEOUT_S)

    assert asyncio.run(idle.check()) is True
    assert supervisor.all_running is False
    assert state.state is StackState.SLEEPING
    assert coordinator.ready is False
    assert coordinator.stops == 1
    # Gestoppt wird in umgekehrter Startreihenfolge — erst der Leser. Der
    # Suchindex fehlt: er bleibt resident (E12).
    assert [name for method, name in supervisor.calls if method == "stopProcess"] == ["api", "db"]
    assert supervisor.programs["index"] is ProcessState.RUNNING
    assert [message for _, message in events] == [
        "Stack wird schlafen gelegt",
        "Stack schlaeft",
    ]


def test_idle_stop_waits_for_the_full_timeout() -> None:
    supervisor = running_stack()
    coordinator, state = ready_coordinator(supervisor)
    clock = FakeClock()
    idle = stopper(coordinator, state, ActivityTracker(clock=clock))

    clock.advance(TIMEOUT_S - 1)

    assert asyncio.run(idle.check()) is False
    assert supervisor.all_running is True
    assert state.state is StackState.READY


def test_activity_postpones_the_idle_stop() -> None:
    """Jede ``/v2``-Anfrage schiebt den Auto-Stopp (§6 „Idle-Definition")."""
    supervisor = running_stack()
    coordinator, state = ready_coordinator(supervisor)
    clock = FakeClock()
    activity = ActivityTracker(clock=clock)
    idle = stopper(coordinator, state, activity)

    clock.advance(TIMEOUT_S - 60)
    activity.touch()  # eine Anfrage kurz vor Ablauf
    clock.advance(TIMEOUT_S - 60)

    assert asyncio.run(idle.check()) is False
    assert supervisor.all_running is True

    clock.advance(60)
    assert asyncio.run(idle.check()) is True
    assert supervisor.all_running is False


def test_a_running_job_blocks_the_stop(db: Database) -> None:
    """Invariante §8.5: kein Stopp, solange ein Import-/Backup-Job laeuft."""
    supervisor = running_stack()
    coordinator, state = ready_coordinator(supervisor)
    clock = FakeClock()
    activity = ActivityTracker(clock=clock)
    run_id = start_run(db, RunKind.UPDATE)
    idle = IdleStopper(coordinator, state, activity, DatabaseJobs(db), Config)

    clock.advance(TIMEOUT_S)
    assert asyncio.run(idle.check()) is False
    assert supervisor.all_running is True
    assert idle.blocked_by_jobs == 1

    # Der Job hat die Leerlaufuhr zurueckgesetzt: nach ihm bekommt der
    # Betreiber wieder das volle Fenster.
    finish_run(db, run_id, RunResult.SUCCESS)
    assert asyncio.run(idle.check()) is False
    assert supervisor.all_running is True

    clock.advance(TIMEOUT_S)
    assert asyncio.run(idle.check()) is True
    assert supervisor.all_running is False


def test_only_a_ready_stack_is_stopped() -> None:
    """Was schlaeft, startet oder im Fehler steht, wird nicht gestoppt."""
    for state_value in (StackState.SLEEPING, StackState.STARTING, StackState.ERROR):
        supervisor = running_stack()
        coordinator, state = ready_coordinator(supervisor)
        state.try_to(StackState.STARTING)
        if state_value is not StackState.STARTING:
            state.try_to(state_value)
        clock = FakeClock()
        idle = stopper(coordinator, state, ActivityTracker(clock=clock))

        clock.advance(TIMEOUT_S)

        assert asyncio.run(idle.check()) is False, state_value
        assert supervisor.all_running is True


def test_no_stop_while_a_wake_is_running() -> None:
    """Ein Weckvorgang und ein Stopp schliessen einander aus."""
    supervisor = sleeping_stack()
    probe = FakeProbe(ready_after=10**6)
    coordinator, state = ready_coordinator(supervisor, probe=probe)
    clock = FakeClock()
    idle = stopper(coordinator, state, ActivityTracker(clock=clock))
    clock.advance(TIMEOUT_S)

    async def scenario() -> bool:
        task = asyncio.create_task(coordinator.ensure_ready(timeout_s=5))
        await asyncio.sleep(0.05)  # Weckvorgang laeuft an
        state.try_to(StackState.READY)  # als waere er gerade fertig geworden
        stopped = await idle.check()
        task.cancel()
        return stopped

    assert asyncio.run(scenario()) is False
    assert coordinator.stops == 0


def test_a_failed_stop_becomes_an_error_state() -> None:
    supervisor = running_stack()
    events: list[tuple[EventLevel, str]] = []
    coordinator, state = ready_coordinator(supervisor, events=events)
    clock = FakeClock()
    idle = stopper(coordinator, state, ActivityTracker(clock=clock))

    supervisor.fail_on.add("api")
    clock.advance(TIMEOUT_S)

    assert asyncio.run(idle.check()) is False
    assert state.state is StackState.ERROR
    assert state.status.detail is not None
    assert "api" in state.status.detail
    assert (EventLevel.ERROR, "Stack-Stopp fehlgeschlagen") in events


def test_an_unreadable_configuration_falls_back_to_the_default() -> None:
    """Eine kaputte ``config.yaml`` darf den Betrieb nicht anhalten."""

    def kaputt() -> Config:
        raise RuntimeError("config.yaml unlesbar")

    supervisor = running_stack()
    coordinator, state = ready_coordinator(supervisor)
    idle = IdleStopper(coordinator, state, ActivityTracker(), FakeJobs(), kaputt)

    assert idle.timeout_s == TIMEOUT_S


def test_a_changed_timeout_takes_effect_without_a_restart() -> None:
    """``idle.timeout_min`` wird bei jeder Pruefung frisch gelesen."""
    supervisor = running_stack()
    coordinator, state = ready_coordinator(supervisor)
    clock = FakeClock()
    config = Config()
    idle = IdleStopper(coordinator, state, ActivityTracker(clock=clock), FakeJobs(), lambda: config)

    clock.advance(120)
    assert asyncio.run(idle.check()) is False

    config = config.model_copy(update={"idle": config.idle.model_copy(update={"timeout_min": 1})})
    assert asyncio.run(idle.check()) is True


# --- Eine Anfrage waehrend des Stopps ---------------------------------------


def test_a_request_during_stopping_waits_and_then_wakes() -> None:
    """Konservativ: wie schlafend — erst faellt der Stack, dann weckt sie ihn.

    Der Stopp wird nicht ueberholt (ein halb gestoppter Stack ist kein
    bedienbarer Stack, und `docker stop`/`docker start` kaemen sich ins
    Gehege); die Anfrage wird auch nicht abgewiesen, solange ihre Haltezeit
    reicht.
    """
    supervisor = running_stack()
    blocking = BlockingController(supervisor_client(supervisor))
    coordinator, state = ready_coordinator(supervisor, stack=blocking)
    clock = FakeClock()
    idle = stopper(coordinator, state, ActivityTracker(clock=clock))
    clock.advance(TIMEOUT_S)

    async def scenario() -> list[StackState]:
        seen: list[StackState] = []
        stopping = asyncio.create_task(idle.check())
        await asyncio.to_thread(blocking.entered.wait, 5)
        seen.append(state.state)  # stoppt

        request = asyncio.create_task(coordinator.ensure_ready(timeout_s=5))
        await asyncio.sleep(0.05)
        seen.append(state.state)  # immer noch stoppt — nicht ueberholt

        blocking.release.set()
        assert await stopping is True
        assert await request is True
        seen.append(state.state)
        return seen

    assert asyncio.run(scenario()) == [
        StackState.STOPPING,
        StackState.STOPPING,
        StackState.READY,
    ]
    assert supervisor.all_running is True
    assert coordinator.stops == 1
    assert coordinator.wakes == 1


# --- Zustandsabgleich -------------------------------------------------------


def test_poller_notices_a_stack_stopped_by_hand() -> None:
    """Die Phase-15-Luecke: `/status` sagt die Wahrheit, ohne Anfrage."""
    supervisor = running_stack()
    events: list[tuple[EventLevel, str]] = []
    coordinator, state = ready_coordinator(supervisor, events=events)
    poller = StatePoller(coordinator)

    assert state.state is StackState.READY
    # Von Hand gestoppt (supervisorctl) — **beide** stoppbaren Prozesse:
    # bliebe Postgres laufen, waere das kein Schlaf, sondern ein
    # Teilzustand (der Test darunter).
    supervisor.stopProcess("api")
    supervisor.stopProcess("db")

    assert asyncio.run(poller.check()) is StackState.SLEEPING
    assert state.state is StackState.SLEEPING
    # Und die Bereitschaft ist verworfen: die naechste Anfrage weckt sofort,
    # statt in eine tote API zu laufen.
    assert coordinator.ready is False
    assert (EventLevel.WARNING, "Stack wurde ausserhalb des Waechters gestoppt") in events


def test_the_first_request_after_a_manual_stop_wakes() -> None:
    """Der eigentliche Gewinn: keine 503 mehr fuer die erste Anfrage."""
    supervisor = running_stack()
    coordinator, _ = ready_coordinator(supervisor)
    poller = StatePoller(coordinator)

    for name in STACK_PROCESSES:
        supervisor.stopProcess(name)

    async def scenario() -> bool:
        await poller.check()
        return await coordinator.ensure_ready(timeout_s=5)

    assert asyncio.run(scenario()) is True
    assert supervisor.all_running is True
    assert coordinator.wakes == 1


def test_poller_never_reports_a_half_stopped_stack_as_sleeping() -> None:
    """Nur die API gestoppt — Postgres haelt das Array wach (R8).

    Der Poller ist die Stelle, an der diese Fehlanzeige entstanden waere:
    er fragt alle 15 s und schreibt, was er sieht. „Schlafend" waere hier
    die bequeme und falsche Antwort.
    """
    supervisor = running_stack()
    events: list[tuple[EventLevel, str]] = []
    coordinator, state = ready_coordinator(supervisor, events=events)
    poller = StatePoller(coordinator)

    supervisor.stopProcess("api")

    assert asyncio.run(poller.check()) is not StackState.SLEEPING
    assert state.state is StackState.READY
    assert coordinator.ready is False
    assert (EventLevel.WARNING, "Stack ist nur teilweise wach") in events


def test_poller_notices_a_stack_started_by_hand() -> None:
    supervisor = sleeping_stack()
    events: list[tuple[EventLevel, str]] = []
    coordinator, state = ready_coordinator(supervisor, events=events)

    assert state.state is StackState.SLEEPING
    for name in STACK_PROCESSES:
        supervisor.programs[name] = ProcessState.RUNNING

    assert asyncio.run(StatePoller(coordinator).check()) is StackState.READY
    assert coordinator.ready is True
    assert (EventLevel.INFO, "Stack wurde ausserhalb des Waechters gestartet") in events


def test_poller_keeps_quiet_while_a_wake_is_running() -> None:
    """Waehrend eines Vorgangs sind die Container in Bewegung."""
    supervisor = sleeping_stack()
    probe = FakeProbe(ready_after=10**6)
    coordinator, state = ready_coordinator(supervisor, probe=probe)
    poller = StatePoller(coordinator)

    async def scenario() -> StackState | None:
        task = asyncio.create_task(coordinator.ensure_ready(timeout_s=5))
        await asyncio.sleep(0.05)
        seen = await poller.check()
        task.cancel()
        return seen

    assert asyncio.run(scenario()) is None
    assert poller.checks == 0
    assert state.state is StackState.STARTING


def test_poller_leaves_a_half_started_stack_alone() -> None:
    """Laeuft alles, antwortet aber noch nichts: der Zustand bleibt stehen."""
    supervisor = running_stack()
    # Die Container laufen, die API antwortet noch nicht (Postgres-Recovery,
    # Index-mmap) — genau der Moment, in dem „schlafend" falsch waere.
    coordinator, state = ready_coordinator(supervisor, probe=FakeProbe(ready_after=10**6))
    state.try_to(StackState.STARTING)

    assert asyncio.run(StatePoller(coordinator).check()) is StackState.STARTING
    assert coordinator.ready is False


def test_poller_keeps_an_error_visible() -> None:
    """Ein Startfehler verschwindet nicht von selbst aus `/status`."""
    supervisor = sleeping_stack()
    coordinator, state = ready_coordinator(supervisor)
    state.to(StackState.STARTING)
    state.to(StackState.ERROR, detail="acoustid-index fehlt")

    assert asyncio.run(StatePoller(coordinator).check()) is StackState.ERROR
    assert state.status.detail == "acoustid-index fehlt"


def test_poller_recovers_from_an_error_when_the_stack_runs_again() -> None:
    supervisor = running_stack()
    coordinator, state = ready_coordinator(supervisor)
    state.to(StackState.STARTING)
    state.to(StackState.ERROR, detail="acoustid-index fehlt")

    assert asyncio.run(StatePoller(coordinator).check()) is StackState.READY
    assert state.status.detail is None


def test_poller_survives_an_unreachable_supervisor(tmp_path: pytest.TempPathFactory) -> None:
    """Ohne Steuerung laeuft der Waechter weiter — mit unveraenderter Anzeige."""
    state = StackStateTracker(StackState.READY)
    coordinator = WakeCoordinator(
        ServiceGroupController(SupervisorClient(f"{tmp_path}/nicht-da.sock")),
        FakeProbe(),  # type: ignore[arg-type]
        state,
    )

    assert asyncio.run(StatePoller(coordinator).check()) is StackState.READY


# --- Die Schleifen ----------------------------------------------------------


def test_both_loops_stop_on_cancellation() -> None:
    """Kein haengender Dauerlaeufer beim Herunterfahren."""
    supervisor = running_stack()
    coordinator, state = ready_coordinator(supervisor)
    idle = stopper(coordinator, state, ActivityTracker())
    poller = StatePoller(coordinator, interval_s=0.01)
    idle.interval_s = 0.01

    async def scenario() -> None:
        tasks = [asyncio.create_task(poller.run()), asyncio.create_task(idle.run())]
        await asyncio.sleep(0.05)
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert all(isinstance(result, asyncio.CancelledError) for result in results)

    asyncio.run(scenario())
    # Die Schleife hat wirklich gearbeitet, nicht nur geschlafen.
    assert poller.checks > 0


def test_a_failing_check_does_not_kill_the_loop() -> None:
    """Stirbt die Schleife, bliebe der Stack fuer immer wach."""
    supervisor = running_stack()
    coordinator, state = ready_coordinator(supervisor)
    idle = stopper(coordinator, state, ActivityTracker())
    idle.interval_s = 0.01
    calls: list[int] = []

    async def kaputt() -> bool:
        calls.append(1)
        raise RuntimeError("Zustandsdatenbank geschlossen")

    idle.check = kaputt  # type: ignore[method-assign]

    async def scenario() -> None:
        task = asyncio.create_task(idle.run())
        await asyncio.sleep(0.06)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert len(calls) > 1
