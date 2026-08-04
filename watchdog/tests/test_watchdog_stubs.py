"""Tests der Attrappen selbst — hier: :class:`FakeSupervisor` (M1a).

Eine Attrappe, die falsch liegt, macht jeden Test wertlos, der auf ihr
steht. Bei :class:`~watchdog_stubs.FakeDaemon` war das Risiko klein: drei
Routen, deren Antworten (204/304/404) in der Engine-API-Doku stehen und die
der echte :class:`~acoustid_watchdog.docker.DockerClient` bei jedem Testlauf
mitdurchlaeuft. :class:`~watchdog_stubs.FakeSupervisor` hat diese Deckung
noch nicht — ihr Gegenpart (``SupervisorClient``) entsteht erst in M1b.
Bis dahin haelt dieses Modul fest, was die Attrappe verspricht:

* die Zustandsuebergaenge (``startProcess``/``stopProcess`` mit und ohne
  ``wait``, Absturz ohne Zutun),
* die Fault-Semantik, an der die Idempotenz haengt
  (``ALREADY_STARTED``/``NOT_RUNNING``) und die Abgrenzung zum echten
  Fehler (``BAD_NAME``/``FAILED``).

Sie ist damit auch die ausfuehrbare Beschreibung dessen, was der Client in
M1b vorfinden wird — die Zahlenwerte stammen aus ``supervisor.states`` und
``supervisor.xmlrpc.Faults``.
"""

from __future__ import annotations

import xmlrpc.client
from collections.abc import Callable

import pytest
from watchdog_stubs import (
    RUNNING_STATES,
    STOPPED_STATES,
    FakeSupervisor,
    Fault,
    ProcessState,
)

#: Die Dauerdienste des Ein-Container-Modells (HANDOFF v2 §5).
PROGRAMS = ("postgres", "index", "api")


def sleeping() -> FakeSupervisor:
    return FakeSupervisor.sleeping(PROGRAMS)


def running() -> FakeSupervisor:
    return FakeSupervisor.running(PROGRAMS)


# --- Zustandsuebergaenge ----------------------------------------------------


def test_start_moves_a_stopped_program_to_running() -> None:
    supervisor = sleeping()

    assert supervisor.startProcess("api") is True

    assert supervisor.programs["api"] is ProcessState.RUNNING
    assert supervisor.calls == [("startProcess", "api")]
    assert supervisor.starts["api"] == 1


def test_start_without_waiting_stops_at_starting() -> None:
    """``wait=False`` heisst „angestossen", nicht „laeuft"."""
    supervisor = sleeping()

    assert supervisor.startProcess("api", wait=False) is True

    assert supervisor.programs["api"] is ProcessState.STARTING
    assert supervisor.all_running is False


def test_stop_moves_a_running_program_to_stopped() -> None:
    supervisor = running()

    assert supervisor.stopProcess("api") is True

    assert supervisor.programs["api"] is ProcessState.STOPPED


def test_stop_without_waiting_stops_at_stopping() -> None:
    supervisor = running()

    assert supervisor.stopProcess("api", wait=False) is True

    assert supervisor.programs["api"] is ProcessState.STOPPING


def test_a_starting_program_counts_as_running_for_the_start_check() -> None:
    """``STARTING`` und ``BACKOFF`` sind fuer supervisord „laeuft schon"."""
    supervisor = sleeping()
    supervisor.programs["api"] = ProcessState.BACKOFF

    with pytest.raises(xmlrpc.client.Fault) as caught:
        supervisor.startProcess("api")

    assert caught.value.faultCode == Fault.ALREADY_STARTED


def test_a_crash_leaves_the_program_exited_without_anyone_stopping_it() -> None:
    """Der Fall, den es unter Docker so nicht gab (M0-Analyse §2.1).

    Ein laufender Prozess, der endet, ist ``EXITED`` — nicht ``FATAL``:
    die Kante ``RUNNING → FATAL`` gibt es in der Zustandsmaschine von
    supervisord nicht.
    """
    supervisor = running()

    supervisor.crash("api")

    assert supervisor.programs["api"] is ProcessState.EXITED
    assert supervisor.all_running is False
    # Ein Absturz ist kein Aufruf — sonst zaehlte jeder Test ihn mit.
    assert supervisor.calls == []


def test_only_a_failed_start_reaches_fatal() -> None:
    """``FATAL`` entsteht ausschliesslich ueber STARTING → BACKOFF → FATAL."""
    supervisor = sleeping()

    path = supervisor.start_failure("api", retries=2)

    assert path == [
        ProcessState.STARTING,
        ProcessState.BACKOFF,
        ProcessState.STARTING,
        ProcessState.BACKOFF,
        ProcessState.FATAL,
    ]
    assert supervisor.programs["api"] is ProcessState.FATAL
    assert supervisor.calls == []


@pytest.mark.parametrize("state", [ProcessState.EXITED, ProcessState.FATAL])
def test_a_dead_program_can_be_started_again(state: ProcessState) -> None:
    """``FATAL``/``EXITED`` sind Ruhezustaende, kein Endzustand."""
    supervisor = running()
    if state is ProcessState.EXITED:
        supervisor.crash("api")
    else:
        supervisor.start_failure("api")

    assert supervisor.startProcess("api") is True
    assert supervisor.programs["api"] is ProcessState.RUNNING
    assert supervisor.starts["api"] == 1


def test_the_two_state_groups_are_the_ones_supervisord_uses() -> None:
    """``STOPPING`` gehoert in keine der beiden — genau wie im Original."""
    assert {ProcessState.STARTING, ProcessState.RUNNING, ProcessState.BACKOFF} == RUNNING_STATES
    assert {
        ProcessState.STOPPED,
        ProcessState.EXITED,
        ProcessState.FATAL,
        ProcessState.UNKNOWN,
    } == STOPPED_STATES
    assert ProcessState.STOPPING not in RUNNING_STATES | STOPPED_STATES


# --- Fault-Semantik (die Idempotenz) ----------------------------------------


def test_starting_a_running_program_is_already_started() -> None:
    """Das Gegenstueck zu HTTP 304 der Engine-API — kein echter Fehler."""
    supervisor = running()

    with pytest.raises(xmlrpc.client.Fault) as caught:
        supervisor.startProcess("api")

    assert caught.value.faultCode == Fault.ALREADY_STARTED
    assert "api" in caught.value.faultString
    # Der Zustand bleibt, wie er war, und kein zweiter Start wird gezaehlt.
    assert supervisor.programs["api"] is ProcessState.RUNNING
    assert supervisor.starts["api"] == 0


def test_stopping_a_stopped_program_is_not_running() -> None:
    supervisor = sleeping()

    with pytest.raises(xmlrpc.client.Fault) as caught:
        supervisor.stopProcess("api")

    assert caught.value.faultCode == Fault.NOT_RUNNING
    assert supervisor.programs["api"] is ProcessState.STOPPED


def test_stopping_a_stopping_program_is_not_running_either() -> None:
    """``STOPPING`` zaehlt fuer ``stopProcess`` nicht als „laeuft"."""
    supervisor = running()
    supervisor.stopProcess("api", wait=False)

    with pytest.raises(xmlrpc.client.Fault) as caught:
        supervisor.stopProcess("api")

    assert caught.value.faultCode == Fault.NOT_RUNNING


@pytest.mark.parametrize(
    "call",
    [
        lambda supervisor: supervisor.getProcessInfo("erfunden"),
        lambda supervisor: supervisor.startProcess("erfunden"),
        lambda supervisor: supervisor.stopProcess("erfunden"),
        lambda supervisor: supervisor.signalProcess("erfunden", "TERM"),
    ],
    ids=["getProcessInfo", "startProcess", "stopProcess", "signalProcess"],
)
def test_an_unknown_program_is_bad_name_everywhere(
    call: Callable[[FakeSupervisor], object],
) -> None:
    """Im Betrieb ein Bug im Image, nie ein Betriebsfehler."""
    supervisor = sleeping()

    with pytest.raises(xmlrpc.client.Fault) as caught:
        call(supervisor)

    assert caught.value.faultCode == Fault.BAD_NAME


def test_a_failed_start_is_a_spawn_error_and_ends_in_fatal() -> None:
    """Der Fehler eines Weckvorgangs heisst ``SPAWN_ERROR``, nicht ``FAILED``.

    Und er ist nicht nur ein Fault: supervisord hat den Startversuch
    wirklich unternommen, also steht das Programm danach auf ``FATAL``.
    """
    supervisor = sleeping()
    supervisor.fail_on.add("api")

    with pytest.raises(xmlrpc.client.Fault) as caught:
        supervisor.startProcess("api")

    assert caught.value.faultCode == Fault.SPAWN_ERROR == 50
    assert supervisor.programs["api"] is ProcessState.FATAL
    assert supervisor.starts["api"] == 0, "ein gescheiterter Versuch ist kein Start"


def test_a_program_that_already_runs_never_reaches_the_spawn() -> None:
    """Reihenfolge des Originals: ``ALREADY_STARTED`` vor ``SPAWN_ERROR``."""
    supervisor = running()
    supervisor.fail_on.add("api")

    with pytest.raises(xmlrpc.client.Fault) as caught:
        supervisor.startProcess("api")

    assert caught.value.faultCode == Fault.ALREADY_STARTED
    assert supervisor.programs["api"] is ProcessState.RUNNING


def test_a_broken_supervisor_answers_other_calls_with_failed() -> None:
    """``fail_on`` ist das Gegenstueck zum HTTP 500 in ``FakeDaemon``."""
    supervisor = running()
    supervisor.fail_on.add("api")

    with pytest.raises(xmlrpc.client.Fault) as caught:
        supervisor.stopProcess("api")
    assert caught.value.faultCode == Fault.FAILED

    with pytest.raises(xmlrpc.client.Fault) as caught:
        supervisor.getProcessInfo("api")
    assert caught.value.faultCode == Fault.FAILED

    # Auch die Sammelabfrage bricht — supervisord antwortet ganz oder gar nicht.
    with pytest.raises(xmlrpc.client.Fault):
        supervisor.getAllProcessInfo()


def test_signal_leaves_the_state_alone() -> None:
    """Was ein Signal bewirkt, entscheidet der Prozess, nicht supervisord."""
    supervisor = running()

    assert supervisor.signalProcess("api", "TERM") is True

    assert supervisor.programs["api"] is ProcessState.RUNNING


# --- Auskunft ---------------------------------------------------------------


def test_process_info_carries_the_documented_fields() -> None:
    supervisor = running()

    info = supervisor.getProcessInfo("api")

    assert info["name"] == "api"
    assert info["state"] == int(ProcessState.RUNNING) == 20
    assert info["statename"] == "RUNNING"
    assert info["pid"] > 0


def test_a_fatal_program_reports_its_spawn_error() -> None:
    supervisor = running()
    supervisor.start_failure("api")

    info = supervisor.getProcessInfo("api")

    assert info["statename"] == "FATAL"
    assert info["state"] == 200
    assert info["pid"] == 0
    assert info["spawnerr"]


@pytest.mark.parametrize(
    ("state", "has_pid"),
    [
        (ProcessState.RUNNING, True),
        (ProcessState.STARTING, True),
        (ProcessState.STOPPING, True),
        (ProcessState.BACKOFF, False),
        (ProcessState.STOPPED, False),
        (ProcessState.EXITED, False),
        (ProcessState.FATAL, False),
    ],
)
def test_only_a_program_with_a_live_process_reports_a_pid(
    state: ProcessState, has_pid: bool
) -> None:
    """``BACKOFF`` ist der Sonderfall: „laeuft schon", aber der Prozess ist tot.

    Die PID haengt deshalb bewusst **nicht** an ``RUNNING_STATES`` — dort
    steht ``BACKOFF`` zu Recht (supervisord weist einen Start dann mit
    ``ALREADY_STARTED`` ab), aber zwischen zwei Versuchen laeuft nichts.
    """
    supervisor = running()
    supervisor.programs["api"] = state

    assert (supervisor.getProcessInfo("api")["pid"] > 0) is has_pid


def test_all_process_info_answers_for_every_program_in_one_call() -> None:
    """Ein Aufruf statt einer je Programm — der Weg des Zustands-Pollers."""
    supervisor = running()
    supervisor.crash("index")

    infos = supervisor.getAllProcessInfo()

    assert [info["name"] for info in infos] == list(PROGRAMS)
    assert {info["name"]: info["statename"] for info in infos} == {
        "postgres": "RUNNING",
        "index": "EXITED",
        "api": "RUNNING",
    }
    assert supervisor.count("getAllProcessInfo") == 1


def test_all_running_needs_every_program_really_running() -> None:
    supervisor = running()
    assert supervisor.all_running is True

    supervisor.programs["index"] = ProcessState.STARTING
    assert supervisor.all_running is False, "startet ist nicht laeuft"


def test_calls_are_recorded_in_order() -> None:
    supervisor = sleeping()
    for name in PROGRAMS:
        supervisor.startProcess(name)
    supervisor.getAllProcessInfo()

    assert supervisor.calls == [
        ("startProcess", "postgres"),
        ("startProcess", "index"),
        ("startProcess", "api"),
        ("getAllProcessInfo", "*"),
    ]
    assert supervisor.count("startProcess") == 3
    assert supervisor.count("startProcess", "api") == 1


def test_the_supervisor_namespace_points_at_the_attrappe_itself() -> None:
    """So laesst sie sich an die Stelle eines ``ServerProxy`` setzen."""
    supervisor = sleeping()

    assert supervisor.supervisor is supervisor
    assert supervisor.supervisor.getState()["statename"] == "RUNNING"


def test_states_is_a_copy_not_the_inner_dictionary() -> None:
    supervisor = running()

    snapshot = supervisor.states
    supervisor.crash("api")

    assert snapshot["api"] is ProcessState.RUNNING
