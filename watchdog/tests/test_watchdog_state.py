"""Zustandsmaschine: welcher Wechsel erlaubt ist — und welcher nicht (Phase 16).

Die Uebergangstabelle steht hier ein zweites Mal, ausgeschrieben und von
Hand: das ist der Punkt der Tests. Waere sie aus
:data:`~acoustid_watchdog.state.ALLOWED_TRANSITIONS` abgeleitet, prueften
sie nur, dass eine Tabelle sich selbst gleicht; so pruefen sie **die
Zusage** — jede der 25 Kombinationen ist einmal bewusst entschieden.
"""

from __future__ import annotations

import pytest

from acoustid_watchdog.state import (
    InvalidStackTransitionError,
    StackStateTracker,
    StackStatus,
)
from shared.models import StackState

SLEEPING = StackState.SLEEPING
STARTING = StackState.STARTING
READY = StackState.READY
STOPPING = StackState.STOPPING
ERROR = StackState.ERROR

#: Von -> erlaubte Ziele. Begruendung je Kante: Docstring von
#: :mod:`acoustid_watchdog.state`.
EXPECTED: dict[StackState, set[StackState]] = {
    SLEEPING: {STARTING, READY},
    STARTING: {READY, ERROR, SLEEPING},
    READY: {STARTING, STOPPING, SLEEPING},
    STOPPING: {SLEEPING, ERROR},
    ERROR: {STARTING, READY},
}

ALL_PAIRS = [(source, target) for source in EXPECTED for target in StackState]


def tracker(state: StackState) -> StackStateTracker:
    """Ein Tracker, der schon im gewuenschten Zustand steht."""
    return StackStateTracker(state)


# --- Die Tabelle ------------------------------------------------------------


@pytest.mark.parametrize(("source", "target"), ALL_PAIRS)
def test_every_pair_of_states_is_decided(source: StackState, target: StackState) -> None:
    """Alle 25 Kombinationen: erlaubt, verboten oder „derselbe Zustand"."""
    machine = tracker(source)
    allowed = target is source or target in EXPECTED[source]

    assert machine.allows(target) is allowed

    if not allowed:
        with pytest.raises(InvalidStackTransitionError):
            machine.to(target)
        assert machine.state is source
        return

    machine.to(target)
    assert machine.state is target


def test_a_forbidden_change_is_named_in_the_error() -> None:
    """Die Ausnahme sagt, was ging und was gegangen waere."""
    with pytest.raises(InvalidStackTransitionError) as raised:
        tracker(SLEEPING).to(STOPPING)

    assert raised.value.source is SLEEPING
    assert raised.value.target is STOPPING
    assert "sleeping -> stopping" in str(raised.value)
    assert "ready, starting" in str(raised.value)


def test_try_to_logs_instead_of_raising() -> None:
    """Der nachsichtige Weg des Pollers: er darf nichts umwerfen."""
    machine = tracker(SLEEPING)

    assert machine.try_to(STOPPING) is None
    assert machine.state is SLEEPING


# --- Der Lebenszyklus als Ganzes --------------------------------------------


def test_the_full_lifecycle_runs_through() -> None:
    """schlafend -> startet -> bereit -> stoppt -> schlafend."""
    machine = StackStateTracker.sleeping()

    for state in (STARTING, READY, STOPPING, SLEEPING):
        machine.to(state)

    assert machine.state is SLEEPING


def test_an_error_can_be_left_by_the_next_wake() -> None:
    """Der Fehlerzustand ist kein Endzustand (§7: naechster Weckversuch)."""
    machine = StackStateTracker.sleeping()
    machine.to(STARTING)
    machine.to(ERROR, detail="acoustid-index fehlt")

    machine.to(STARTING)
    machine.to(READY)

    assert machine.state is READY
    # Der Fehlertext gehoert zum Fehlerzustand und verschwindet mit ihm.
    assert machine.status.detail is None


# --- Momentaufnahme ---------------------------------------------------------


def test_same_state_keeps_since() -> None:
    """Sonst zeigte jede Pruefschleife „seit 0 Sekunden"."""
    machine = tracker(READY)
    before = machine.status

    assert machine.to(READY) == before
    assert machine.try_to(READY) is None
    assert machine.status.since == before.since


def test_a_new_detail_is_a_real_change() -> None:
    """Ein zweiter, anderer Fehler ist ein neuer Zustand — mit neuem Zeitpunkt."""
    machine = tracker(STARTING)
    first = machine.to(ERROR, detail="acoustid-db fehlt")
    second = machine.to(ERROR, detail="Docker antwortet nicht")

    assert second.detail == "Docker antwortet nicht"
    assert second.since >= first.since


def test_status_dict_carries_the_german_display_name() -> None:
    """Die Anzeige der Admin-UI kommt aus dem Modell (§9)."""
    machine = tracker(STARTING)
    assert machine.status.as_dict() == {
        "state": "starting",
        "state_display": "startet",
        "since": machine.status.since,
        "detail": None,
    }


# --- Anschluss ans Ereignis-Log ---------------------------------------------


def test_every_real_change_is_announced() -> None:
    seen: list[tuple[StackState, StackState]] = []

    machine = StackStateTracker.sleeping()
    machine.on_transition = lambda previous, current: seen.append((previous.state, current.state))

    machine.to(STARTING)
    machine.to(STARTING)  # kein Wechsel, keine Meldung
    machine.try_to(STOPPING)  # verboten, keine Meldung
    machine.to(READY)

    assert seen == [(SLEEPING, STARTING), (STARTING, READY)]


def test_a_broken_event_log_does_not_stop_the_machine() -> None:
    """Der Zustand ist die Wahrheit, das Ereignis nur seine Mitschrift."""

    def kaputt(previous: StackStatus, current: StackStatus) -> None:
        raise RuntimeError("Zustandsdatenbank geschlossen")

    machine = StackStateTracker(SLEEPING, on_transition=kaputt)

    assert machine.to(STARTING).state is STARTING
