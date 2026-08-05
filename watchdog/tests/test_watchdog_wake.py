"""Wake-on-request: Startreihenfolge, Haltezeit, ein Weckvorgang (Phase 15).

Die Tests laufen ohne ``pytest-asyncio``: jedes Szenario ist eine
Koroutine, die ueber :func:`asyncio.run` gestartet wird. Das haelt die
Abhaengigkeiten des Workspace unveraendert und macht sichtbar, wo eine
Nebenlaeufigkeit wirklich gebraucht wird — naemlich genau in dem Test, der
gleichzeitige Anfragen prueft.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx
import pytest
from watchdog_stubs import (
    FakeProbe,
    FakeSupervisor,
    ProcessState,
    controller,
    running_stack,
    sleeping_stack,
)

from acoustid_watchdog.control import GroupStatus, ProcessControlError, ProcessGroupController
from acoustid_watchdog.events import EventLevel
from acoustid_watchdog.notify import Notification, NotifyEvent
from acoustid_watchdog.process import SupervisorClient, SupervisorError
from acoustid_watchdog.stack import STACK_PROCESSES, ServiceGroupController
from acoustid_watchdog.state import StackStateTracker
from acoustid_watchdog.wake import (
    ReadinessProbe,
    StackNotReadyError,
    WakeCoordinator,
)
from shared.env import EnvSettings
from shared.models import StackState

#: Adresse des internen Healthchecks — seit M1a ein Bootstrap-Wert
#: (``MMO_API_HEALTH_URL``) und keine Modulkonstante des Waechters mehr.
HEALTH_URL = EnvSettings().api_health_url


def _coordinator(
    supervisor: FakeSupervisor,
    probe: FakeProbe,
    *,
    events: list[tuple[EventLevel, str]] | None = None,
    state: StackStateTracker | None = None,
    notifications: list[Notification] | None = None,
) -> tuple[WakeCoordinator, StackStateTracker]:
    tracker = state if state is not None else StackStateTracker.sleeping()
    coordinator = WakeCoordinator(
        controller(supervisor),
        probe,  # type: ignore[arg-type]
        tracker,
        log_event=(lambda level, message, extra=None: events.append((level, message)))
        if events is not None
        else None,
        notify=notifications.append if notifications is not None else None,
        poll_interval_s=0.01,
    )
    return coordinator, tracker


# --- Wecken -----------------------------------------------------------------


def test_wake_starts_the_stack_and_waits_for_readiness() -> None:
    supervisor = sleeping_stack()
    probe = FakeProbe(ready_after=2)
    events: list[tuple[EventLevel, str]] = []
    coordinator, state = _coordinator(supervisor, probe, events=events)

    async def scenario() -> bool:
        return await coordinator.ensure_ready(timeout_s=5)

    assert asyncio.run(scenario()) is True
    assert supervisor.all_running is True
    assert state.state is StackState.READY
    assert coordinator.wakes == 1
    assert probe.calls == 3
    assert [message for _, message in events] == [
        "Stack wird geweckt",
        "Stack ist bereit",
    ]


def test_ready_stack_is_not_woken_again() -> None:
    """Nach einem erfolgreichen Wecken kostet eine Anfrage nichts mehr."""
    supervisor = sleeping_stack()
    coordinator, _ = _coordinator(supervisor, FakeProbe())

    async def scenario() -> tuple[bool, bool]:
        first = await coordinator.ensure_ready(timeout_s=5)
        second = await coordinator.ensure_ready(timeout_s=5)
        return first, second

    first, second = asyncio.run(scenario())

    assert (first, second) == (True, False)
    assert coordinator.wakes == 1
    assert supervisor.count("startProcess") == len(STACK_PROCESSES)


def test_concurrent_requests_trigger_exactly_one_wake() -> None:
    """Zehn Anfragen auf einen schlafenden Stack — ein Weckvorgang.

    Der Kern der Phase: sonst wuerde jede wartende Anfrage `docker start`
    erneut absetzen und die Zustandsanzeige flackern lassen.
    """
    supervisor = sleeping_stack()
    probe = FakeProbe(ready_after=3)
    coordinator, state = _coordinator(supervisor, probe)

    async def scenario() -> list[bool]:
        return await asyncio.gather(*(coordinator.ensure_ready(timeout_s=5) for _ in range(10)))

    results = asyncio.run(scenario())

    assert all(results)
    assert coordinator.wakes == 1
    assert supervisor.count("startProcess") == len(STACK_PROCESSES)
    assert state.state is StackState.READY


def test_timeout_raises_with_retry_after() -> None:
    """Nach ``wake.hold_timeout_s`` gibt es 503 + Retry-After (§7)."""
    supervisor = sleeping_stack()
    # Wird nie bereit.
    probe = FakeProbe(ready_after=10**6)
    events: list[tuple[EventLevel, str]] = []
    coordinator, state = _coordinator(supervisor, probe, events=events)

    async def scenario() -> StackNotReadyError:
        with pytest.raises(StackNotReadyError) as raised:
            await coordinator.ensure_ready(timeout_s=0.05)
        # Der Weckvorgang laeuft nach dem Abbruch der Anfrage weiter
        # (``shield``) und endet mit seiner eigenen Frist — hier abwarten,
        # damit das Ereignis noch geschrieben wird.
        await asyncio.sleep(0.2)
        return raised.value

    error = asyncio.run(scenario())

    assert error.retry_after_s == 30
    # Kein Fehlerzustand: der Stack startet vermutlich weiter, nur eben
    # laenger, als eine Anfrage warten darf.
    assert state.state is StackState.STARTING
    assert (EventLevel.WARNING, "Stack war nicht rechtzeitig bereit") in events


def test_a_later_request_gets_its_own_full_hold_time() -> None:
    """Die Frist des Vorgangs waechst mit dem geduldigsten Wartenden.

    Die zweite der beiden Phase-15-Luecken, geschlossen in Phase 16: vorher
    erbte der Weckvorgang die Frist der **ersten** Anfrage und endete mit
    ihr — ein spaeter Dazugekommener sah seine 503 lange vor Ablauf seiner
    eigenen Haltezeit.
    """
    supervisor = sleeping_stack()
    probe = FakeProbe(ready_after=10**6)  # wird nie bereit
    coordinator, _ = _coordinator(supervisor, probe)

    async def scenario() -> float:
        first = asyncio.create_task(coordinator.ensure_ready(timeout_s=0.05))
        await asyncio.sleep(0.02)
        started_at = time.monotonic()
        with pytest.raises(StackNotReadyError):
            await coordinator.ensure_ready(timeout_s=0.5)
        waited = time.monotonic() - started_at
        with pytest.raises(StackNotReadyError):
            await first
        return waited

    # Der zweite Wartende bekommt seine volle halbe Sekunde, nicht die
    # Restzeit des ersten.
    assert asyncio.run(scenario()) >= 0.4
    assert coordinator.wakes == 1


def test_start_failure_becomes_an_error_state() -> None:
    """Ein gescheiterter Startversuch ist ein Startfehler (§7) — mit Klartext.

    Der Weg, den supervisord wirklich geht: der Prozess wird gespawnt,
    ueberlebt ``startsecs`` nicht, landet nach den Wiederholungen in
    ``FATAL``, und der Aufruf endet mit ``SPAWN_ERROR``
    (``FakeSupervisor.start_failure``, DECISIONS 2026-08-04 M1a).
    """
    supervisor = sleeping_stack()
    supervisor.fail_on.add("index")
    events: list[tuple[EventLevel, str]] = []
    coordinator, state = _coordinator(supervisor, FakeProbe(), events=events)

    async def scenario() -> None:
        await coordinator.ensure_ready(timeout_s=5)

    with pytest.raises(StackNotReadyError, match="konnte nicht gestartet werden"):
        asyncio.run(scenario())

    assert state.state is StackState.ERROR
    assert state.status.detail is not None
    assert "SPAWN_ERROR" in state.status.detail
    assert (EventLevel.ERROR, "Stack-Start fehlgeschlagen") in events
    # Der Prozess davor lief trotzdem an — der Start ist sequenziell, und
    # was schon steht, wird nicht zurueckgenommen.
    assert supervisor.programs["db"] is ProcessState.RUNNING
    assert supervisor.programs["index"] is ProcessState.FATAL
    # Und der Nachfolger wurde nie versucht.
    assert supervisor.count("startProcess", "api") == 0


def test_an_unknown_process_is_an_image_bug() -> None:
    """Ein Prozess, den supervisord nicht kennt, ist kein Betriebsfehler.

    Unter Docker war das der fehlende Container (HTTP 404); hier ist es
    ``BAD_NAME``. Beides endet im selben Fehlerzustand — die Meldung nennt
    aber den Namen, damit im Log steht, dass Image und Code auseinander
    laufen.
    """
    supervisor = FakeSupervisor.sleeping(["db"])  # index und api fehlen
    coordinator, state = _coordinator(supervisor, FakeProbe())

    async def scenario() -> None:
        await coordinator.ensure_ready(timeout_s=5)

    with pytest.raises(StackNotReadyError, match="konnte nicht gestartet werden"):
        asyncio.run(scenario())

    assert state.state is StackState.ERROR
    assert state.status.detail is not None
    assert "index" in state.status.detail


def test_the_next_attempt_leads_out_of_the_error_state() -> None:
    """``error`` ist kein Endzustand (§7, Phase 16).

    Der Betreiber behebt die Ursache — die naechste Anfrage muss den Stack
    wecken koennen, ohne dass der Waechter neu startet.
    """
    supervisor = sleeping_stack()
    supervisor.fail_on.add("api")
    events: list[tuple[EventLevel, str]] = []
    coordinator, state = _coordinator(supervisor, FakeProbe(), events=events)

    async def scenario() -> None:
        with pytest.raises(StackNotReadyError):
            await coordinator.ensure_ready(timeout_s=5)
        assert state.state is StackState.ERROR

        supervisor.fail_on.clear()  # Ursache behoben
        supervisor.programs["api"] = ProcessState.STOPPED
        await coordinator.ensure_ready(timeout_s=5)

    asyncio.run(scenario())

    assert state.state is StackState.READY
    assert state.status.detail is None
    assert coordinator.wakes == 2
    assert [message for _, message in events] == [
        "Stack wird geweckt",
        "Stack-Start fehlgeschlagen",
        "Stack wird geweckt",
        "Stack ist bereit",
    ]


# --- Die Naht: Protokoll und Fehlerbasis (M1a) ------------------------------


class _BrokenController:
    """Eine Steuerung ohne jede Technik dahinter — sie scheitert nur.

    Der Beleg dafuer, dass der Koordinator wirklich am Protokoll haengt und
    nicht an einer bestimmten Klasse: diese hier erbt von nichts.
    """

    def __init__(self, message: str = "Steuerung antwortet nicht") -> None:
        self.message = message
        self.calls: list[str] = []

    def _fail(self, what: str) -> None:
        self.calls.append(what)
        raise ProcessControlError(self.message)

    def start(self) -> list[str]:
        self._fail("start")
        return []

    def stop(self) -> list[str]:
        self._fail("stop")
        return []

    def inspect(self) -> GroupStatus:
        self._fail("inspect")
        return GroupStatus(running=False)


def test_the_supervisor_controller_fulfils_the_protocol() -> None:
    """Der Adapter-Tausch aus M1b haengt genau an dieser Zusage."""
    assert isinstance(controller(sleeping_stack()), ProcessGroupController)
    assert isinstance(_BrokenController(), ProcessGroupController)


def test_supervisor_errors_are_process_control_errors() -> None:
    """``SupervisorError`` ist die supervisord-Auspraegung der Basis."""
    assert issubclass(SupervisorError, ProcessControlError)


def test_a_start_failure_of_any_controller_becomes_an_error_state() -> None:
    """Der Koordinator faengt die Basis, nicht den Supervisor-Fehler.

    Gleiches Verhalten wie bei einem gescheiterten Start
    (:func:`test_start_failure_becomes_an_error_state`) — nur kommt der
    Fehler hier aus einer Steuerung, die supervisord nie gesehen hat.
    """
    broken = _BrokenController("Supervisor-Socket antwortet nicht")
    state = StackStateTracker.sleeping()
    events: list[tuple[EventLevel, str]] = []
    coordinator = WakeCoordinator(
        broken,
        FakeProbe(),  # type: ignore[arg-type]
        state,
        log_event=lambda level, message, extra=None: events.append((level, message)),
        poll_interval_s=0.01,
    )

    async def scenario() -> None:
        await coordinator.ensure_ready(timeout_s=5)

    with pytest.raises(StackNotReadyError, match="konnte nicht gestartet werden"):
        asyncio.run(scenario())

    assert state.state is StackState.ERROR
    assert state.status.detail == "Supervisor-Socket antwortet nicht"
    assert (EventLevel.ERROR, "Stack-Start fehlgeschlagen") in events


def test_a_start_failure_is_reported_to_the_operator() -> None:
    """M2.5: der Stack bleibt im Fehlerzustand stehen — die Meldung geht raus.

    Ohne sie saehe der Betreiber den Zustand nur an den 503-Antworten. Der
    Weg ist bewusst der **nicht blockierende**
    (:meth:`~acoustid_watchdog.notify.Notifier.send_background`): der
    Weckvorgang laeuft in der Ereignisschleife, beide Kanaele sind
    synchron.
    """
    supervisor = sleeping_stack()
    supervisor.fail_on.add("db")
    notifications: list[Notification] = []
    coordinator, state = _coordinator(supervisor, FakeProbe(), notifications=notifications)

    async def scenario() -> None:
        await coordinator.ensure_ready(timeout_s=5)

    with pytest.raises(StackNotReadyError):
        asyncio.run(scenario())

    assert state.state is StackState.ERROR
    assert [item.event for item in notifications] == [NotifyEvent.STACK_START_FAILED]
    assert "db" in notifications[0].fields["grund"]


def test_a_successful_wake_reports_nothing() -> None:
    """Der Normalfall meldet sich nicht — sonst waere die Meldung wertlos."""
    notifications: list[Notification] = []
    coordinator, _state = _coordinator(sleeping_stack(), FakeProbe(), notifications=notifications)

    async def scenario() -> None:
        await coordinator.ensure_ready(timeout_s=5)
        await coordinator.stop_stack(reason="idle")

    asyncio.run(scenario())
    assert notifications == []


def test_a_broken_notify_hook_does_not_break_the_wake() -> None:
    """Die Ausnahme, die gemeldet werden soll, ist die wichtigere."""
    supervisor = sleeping_stack()
    supervisor.fail_on.add("db")
    state = StackStateTracker.sleeping()

    def explode(_notification: Notification) -> None:
        raise RuntimeError("Benachrichtigung kaputt")

    coordinator = WakeCoordinator(
        controller(supervisor),
        FakeProbe(),  # type: ignore[arg-type]
        state,
        notify=explode,
        poll_interval_s=0.01,
    )

    async def scenario() -> None:
        await coordinator.ensure_ready(timeout_s=5)

    with pytest.raises(StackNotReadyError, match="konnte nicht gestartet werden"):
        asyncio.run(scenario())
    assert state.state is StackState.ERROR


def test_a_failing_controller_leaves_the_display_alone_on_observe() -> None:
    """Nachfragen scheitert lautlos — der Waechter muss weiterlaufen."""
    broken = _BrokenController()
    state = StackStateTracker(StackState.READY)
    coordinator = WakeCoordinator(
        broken,
        FakeProbe(),  # type: ignore[arg-type]
        state,
    )

    assert coordinator.observe() is StackState.READY
    assert broken.calls == ["inspect"]


# --- Absturz statt Schlaf: die neue Kante ready -> error (M1b) ---------------


def test_a_crash_while_ready_becomes_an_error_state() -> None:
    """Ein Prozess faellt im Betrieb weg — das ist kein Schlaf (R8).

    Der Kern der neuen Zustandskante. Unter Docker war „laeuft nicht"
    eindeutig gutartig; unter supervisord unterscheidet sich ``STOPPED``
    (Idle-Stopp) von ``EXITED`` (der Prozess ist von selbst weg). Ohne
    diese Unterscheidung saehe der Betreiber einen Gutzustand.
    """
    supervisor = running_stack()
    events: list[tuple[EventLevel, str]] = []
    coordinator, state = _coordinator(
        supervisor,
        FakeProbe(),
        events=events,
        state=StackStateTracker(StackState.READY),
    )

    supervisor.crash("db")

    assert coordinator.observe() is StackState.ERROR
    assert state.status.detail is not None
    assert "db" in state.status.detail
    assert coordinator.ready is False


def test_a_stopped_process_stays_a_sleeping_stack() -> None:
    """Die Gegenprobe: gestoppt ist gestoppt, nicht abgestuerzt.

    Ohne sie waere die Kante oben ein Fehlalarm bei jedem Idle-Stopp — der
    haeufigste Betriebsvorgang dieses Projekts.
    """
    supervisor = running_stack()
    coordinator, state = _coordinator(
        supervisor, FakeProbe(), state=StackStateTracker(StackState.READY)
    )

    supervisor.stopProcess("db")
    supervisor.stopProcess("api")

    assert coordinator.observe() is StackState.SLEEPING
    assert state.status.detail is None


def test_a_half_running_stack_is_never_reported_as_sleeping() -> None:
    """Die gefaehrlichste Fehlanzeige des Projekts (R8).

    Die API ist gestoppt, Postgres laeuft weiter — und haelt damit das
    Array wach. „Schlafend" saehe aus wie der Gutzustand, und niemand
    haette einen Grund nachzusehen. Der gefuehrte Zustand bleibt deshalb
    stehen, und die Bereitschaft ist verworfen.
    """
    supervisor = running_stack()
    events: list[tuple[EventLevel, str]] = []
    coordinator, state = _coordinator(
        supervisor,
        FakeProbe(),
        events=events,
        state=StackStateTracker(StackState.READY),
    )

    supervisor.stopProcess("api")

    assert coordinator.observe() is StackState.READY
    assert state.state is not StackState.SLEEPING
    assert coordinator.ready is False
    assert (EventLevel.WARNING, "Stack ist nur teilweise wach") in events


def test_a_restarting_process_is_not_reported_as_sleeping() -> None:
    """Waehrend `autorestart=unexpected` greift, schlaeft nichts (E15).

    supervisord hat den abgestuerzten Prozess neu gespawnt; er steht in
    ``STARTING`` und ist damit weder „laeuft" noch „abgestuerzt". Genau
    diese Luecke haette ihn als Schlaf gelesen.
    """
    supervisor = running_stack()
    coordinator, state = _coordinator(
        supervisor, FakeProbe(), state=StackStateTracker(StackState.READY)
    )

    supervisor.programs["db"] = ProcessState.STARTING

    assert coordinator.observe() is not StackState.SLEEPING
    assert state.state is StackState.READY


def test_a_partial_state_is_reported_once() -> None:
    """Auch hier gilt: gemeldet wird die Aenderung, nicht der Zustand."""
    supervisor = running_stack()
    events: list[tuple[EventLevel, str]] = []
    coordinator, _state = _coordinator(
        supervisor,
        FakeProbe(),
        events=events,
        state=StackStateTracker(StackState.READY),
    )

    supervisor.stopProcess("api")
    for _ in range(5):
        coordinator.observe()

    assert [message for _, message in events].count("Stack ist nur teilweise wach") == 1


def test_a_crash_while_sleeping_is_reported_without_changing_the_state() -> None:
    """Aus ``schlafend`` gibt es keine Kante in den Fehler — gemeldet wird trotzdem.

    Der residente Index (E12) laeuft auch im Schlaf. Stuerzt er ab,
    waehrend Postgres und API gestoppt sind, waere ``fehler`` ein Wechsel,
    den die Uebergangstabelle nicht kennt. Der Befund darf deswegen nicht
    verschwinden.
    """
    supervisor = sleeping_stack()
    supervisor.programs["index"] = ProcessState.RUNNING
    events: list[tuple[EventLevel, str]] = []
    coordinator, _state = _coordinator(supervisor, FakeProbe(), events=events)

    supervisor.crash("index")

    assert coordinator.observe() is StackState.SLEEPING
    assert (EventLevel.WARNING, "Prozess unerwartet beendet") in events


def test_a_persistent_crash_is_reported_once() -> None:
    """Der Poller fragt alle 15 s — der Ringpuffer haelt 5000 Eintraege.

    Ein dauerhaft toter Prozess wuerde ihn an einem Nachmittag leeren.
    Gemeldet wird deshalb die **Aenderung**, nicht der Zustand.
    """
    supervisor = sleeping_stack()
    supervisor.programs["index"] = ProcessState.RUNNING
    events: list[tuple[EventLevel, str]] = []
    coordinator, _ = _coordinator(supervisor, FakeProbe(), events=events)

    supervisor.crash("index")
    for _ in range(5):
        coordinator.observe()

    assert [message for _, message in events].count("Prozess unerwartet beendet") == 1


def test_a_recovered_process_leaves_the_error_state() -> None:
    """``autorestart=unexpected`` heilt den Absturz — die Anzeige folgt (E15)."""
    supervisor = running_stack()
    coordinator, state = _coordinator(
        supervisor, FakeProbe(), state=StackStateTracker(StackState.READY)
    )

    supervisor.crash("db")
    assert coordinator.observe() is StackState.ERROR

    # supervisord startet den Prozess neu (autorestart=unexpected).
    supervisor.programs["db"] = ProcessState.RUNNING

    assert coordinator.observe() is StackState.READY
    assert state.status.detail is None


def test_invalidate_forces_a_new_check() -> None:
    """Der Proxy verwirft die Bereitschaft, wenn die API wegbricht."""
    supervisor = sleeping_stack()
    coordinator, _ = _coordinator(supervisor, FakeProbe())

    async def scenario() -> bool:
        await coordinator.ensure_ready(timeout_s=5)
        coordinator.invalidate()
        return await coordinator.ensure_ready(timeout_s=5)

    assert asyncio.run(scenario()) is True
    assert coordinator.wakes == 2


# --- Zustand beim Start des Waechters ---------------------------------------


def test_refresh_finds_a_running_stack() -> None:
    """Der Zustand liegt nur im Speicher und wird beim Start neu erhoben."""
    supervisor = running_stack()
    coordinator, state = _coordinator(supervisor, FakeProbe())

    assert coordinator.refresh() is StackState.READY
    assert state.state is StackState.READY
    assert coordinator.ready is True


def test_refresh_finds_a_sleeping_stack() -> None:
    supervisor = sleeping_stack()
    coordinator, _ = _coordinator(supervisor, FakeProbe())

    assert coordinator.refresh() is StackState.SLEEPING
    assert coordinator.ready is False


def test_refresh_survives_an_unreachable_supervisor(tmp_path: Path) -> None:
    """Ohne Steuerung laeuft der Waechter weiter — sonst saehe niemand den Fehler.

    Bewusst mit dem **echten** Client auf einen Socket, den es nicht gibt:
    so laeuft die Uebersetzung des Transportfehlers in
    ``SupervisorUnavailableError`` wirklich mit durch.
    """
    supervisor = SupervisorClient(str(tmp_path / "nicht-da.sock"))
    state = StackStateTracker.sleeping()
    coordinator = WakeCoordinator(
        ServiceGroupController(supervisor),
        FakeProbe(),  # type: ignore[arg-type]
        state,
    )

    assert coordinator.refresh() is StackState.SLEEPING
    assert coordinator.ready is False


# --- Bereitschaftsfrage -----------------------------------------------------


def test_probe_accepts_only_http_200() -> None:
    for status, expected in ((200, True), (503, False), (500, False), (404, False)):
        client = httpx.Client(
            transport=httpx.MockTransport(lambda request, code=status: httpx.Response(code))
        )
        assert ReadinessProbe(HEALTH_URL, client=client).ready() is expected


def test_probe_treats_a_dead_connection_as_not_ready() -> None:
    """Waehrend eines Starts ist „keine Antwort" der Normalfall, kein Fehler."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert ReadinessProbe(HEALTH_URL, client=client).ready() is False
