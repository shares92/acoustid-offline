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

import httpx
import pytest
from watchdog_stubs import FakeDaemon, FakeProbe, docker_client

from acoustid_watchdog.docker import DockerClient
from acoustid_watchdog.events import EventLevel
from acoustid_watchdog.state import StackStateTracker
from acoustid_watchdog.wake import (
    STACK_CONTAINERS,
    ReadinessProbe,
    StackController,
    StackNotReadyError,
    WakeCoordinator,
)
from shared.models import StackState


def _coordinator(
    daemon: FakeDaemon,
    probe: FakeProbe,
    *,
    events: list[tuple[EventLevel, str]] | None = None,
) -> tuple[WakeCoordinator, StackStateTracker]:
    state = StackStateTracker.sleeping()
    coordinator = WakeCoordinator(
        StackController(docker_client(daemon)),
        probe,  # type: ignore[arg-type]
        state,
        log_event=(lambda level, message, extra=None: events.append((level, message)))
        if events is not None
        else None,
        poll_interval_s=0.01,
    )
    return coordinator, state


def _sleeping() -> FakeDaemon:
    return FakeDaemon(dict.fromkeys(STACK_CONTAINERS, False))


# --- Startreihenfolge -------------------------------------------------------


def test_controller_starts_in_the_documented_order() -> None:
    """Erst die Datenquellen, dann der Leser (ARCHITECTURE §6, §3)."""
    daemon = _sleeping()
    controller = StackController(docker_client(daemon))

    started = controller.start()

    assert started == ["acoustid-db", "acoustid-index", "acoustid-api"]
    assert [name for action, name in daemon.calls if action == "start"] == started


def test_controller_stops_in_reverse_order() -> None:
    daemon = FakeDaemon(dict.fromkeys(STACK_CONTAINERS, True))
    controller = StackController(docker_client(daemon))

    stopped = controller.stop()

    assert stopped == ["acoustid-api", "acoustid-index", "acoustid-db"]


def test_controller_leaves_the_importer_alone() -> None:
    """Der Importer ist ein One-Shot-Job des Schedulers, kein Weckziel."""
    assert "acoustid-importer" not in STACK_CONTAINERS


def test_all_running_asks_every_container() -> None:
    daemon = FakeDaemon(dict.fromkeys(STACK_CONTAINERS, True))
    controller = StackController(docker_client(daemon))

    assert controller.all_running() is True

    daemon.containers["acoustid-index"] = False
    assert controller.all_running() is False


# --- Wecken -----------------------------------------------------------------


def test_wake_starts_the_stack_and_waits_for_readiness() -> None:
    daemon = _sleeping()
    probe = FakeProbe(ready_after=2)
    events: list[tuple[EventLevel, str]] = []
    coordinator, state = _coordinator(daemon, probe, events=events)

    async def scenario() -> bool:
        return await coordinator.ensure_ready(timeout_s=5)

    assert asyncio.run(scenario()) is True
    assert daemon.all_running is True
    assert state.state is StackState.READY
    assert coordinator.wakes == 1
    assert probe.calls == 3
    assert [message for _, message in events] == [
        "Stack wird geweckt",
        "Stack ist bereit",
    ]


def test_ready_stack_is_not_woken_again() -> None:
    """Nach einem erfolgreichen Wecken kostet eine Anfrage nichts mehr."""
    daemon = _sleeping()
    coordinator, _ = _coordinator(daemon, FakeProbe())

    async def scenario() -> tuple[bool, bool]:
        first = await coordinator.ensure_ready(timeout_s=5)
        second = await coordinator.ensure_ready(timeout_s=5)
        return first, second

    first, second = asyncio.run(scenario())

    assert (first, second) == (True, False)
    assert coordinator.wakes == 1
    assert daemon.count("start") == len(STACK_CONTAINERS)


def test_concurrent_requests_trigger_exactly_one_wake() -> None:
    """Zehn Anfragen auf einen schlafenden Stack — ein Weckvorgang.

    Der Kern der Phase: sonst wuerde jede wartende Anfrage `docker start`
    erneut absetzen und die Zustandsanzeige flackern lassen.
    """
    daemon = _sleeping()
    probe = FakeProbe(ready_after=3)
    coordinator, state = _coordinator(daemon, probe)

    async def scenario() -> list[bool]:
        return await asyncio.gather(*(coordinator.ensure_ready(timeout_s=5) for _ in range(10)))

    results = asyncio.run(scenario())

    assert all(results)
    assert coordinator.wakes == 1
    assert daemon.count("start") == len(STACK_CONTAINERS)
    assert state.state is StackState.READY


def test_timeout_raises_with_retry_after() -> None:
    """Nach ``wake.hold_timeout_s`` gibt es 503 + Retry-After (§7)."""
    daemon = _sleeping()
    # Wird nie bereit.
    probe = FakeProbe(ready_after=10**6)
    events: list[tuple[EventLevel, str]] = []
    coordinator, state = _coordinator(daemon, probe, events=events)

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
    daemon = _sleeping()
    probe = FakeProbe(ready_after=10**6)  # wird nie bereit
    coordinator, _ = _coordinator(daemon, probe)

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
    """Fehlt ein Container, ist das ein Startfehler (§7) — mit Klartext."""
    daemon = FakeDaemon({"acoustid-db": False})  # index und api fehlen
    events: list[tuple[EventLevel, str]] = []
    coordinator, state = _coordinator(daemon, FakeProbe(), events=events)

    async def scenario() -> None:
        await coordinator.ensure_ready(timeout_s=5)

    with pytest.raises(StackNotReadyError, match="konnte nicht gestartet werden"):
        asyncio.run(scenario())

    assert state.state is StackState.ERROR
    assert state.status.detail is not None
    assert "acoustid-index" in state.status.detail
    assert (EventLevel.ERROR, "Stack-Start fehlgeschlagen") in events


def test_the_next_attempt_leads_out_of_the_error_state() -> None:
    """``error`` ist kein Endzustand (§7, Phase 16).

    Der Betreiber legt den fehlenden Container an — die naechste Anfrage
    muss den Stack wecken koennen, ohne dass der Waechter neu startet.
    """
    daemon = FakeDaemon({"acoustid-db": False, "acoustid-index": False})  # api fehlt
    events: list[tuple[EventLevel, str]] = []
    coordinator, state = _coordinator(daemon, FakeProbe(), events=events)

    async def scenario() -> None:
        with pytest.raises(StackNotReadyError):
            await coordinator.ensure_ready(timeout_s=5)
        assert state.state is StackState.ERROR

        daemon.containers["acoustid-api"] = False  # Container ist wieder da
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


def test_invalidate_forces_a_new_check() -> None:
    """Der Proxy verwirft die Bereitschaft, wenn die API wegbricht."""
    daemon = _sleeping()
    coordinator, _ = _coordinator(daemon, FakeProbe())

    async def scenario() -> bool:
        await coordinator.ensure_ready(timeout_s=5)
        coordinator.invalidate()
        return await coordinator.ensure_ready(timeout_s=5)

    assert asyncio.run(scenario()) is True
    assert coordinator.wakes == 2


# --- Zustand beim Start des Waechters ---------------------------------------


def test_refresh_finds_a_running_stack() -> None:
    """Der Zustand liegt nur im Speicher und wird beim Start neu erhoben."""
    daemon = FakeDaemon(dict.fromkeys(STACK_CONTAINERS, True))
    coordinator, state = _coordinator(daemon, FakeProbe())

    assert coordinator.refresh() is StackState.READY
    assert state.state is StackState.READY
    assert coordinator.ready is True


def test_refresh_finds_a_sleeping_stack() -> None:
    daemon = _sleeping()
    coordinator, _ = _coordinator(daemon, FakeProbe())

    assert coordinator.refresh() is StackState.SLEEPING
    assert coordinator.ready is False


def test_refresh_survives_an_unreachable_daemon() -> None:
    """Ohne Docker laeuft der Waechter weiter — sonst saehe niemand den Fehler."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    state = StackStateTracker.sleeping()
    coordinator = WakeCoordinator(
        StackController(DockerClient(client=httpx.Client(transport=httpx.MockTransport(handler)))),
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
        assert ReadinessProbe(client=client).ready() is expected


def test_probe_treats_a_dead_connection_as_not_ready() -> None:
    """Waehrend eines Starts ist „keine Antwort" der Normalfall, kein Fehler."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert ReadinessProbe(client=client).ready() is False
