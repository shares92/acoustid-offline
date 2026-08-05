"""Der Stack als Prozessgruppe: Reihenfolge, Gates, Residenz, Drift (M1b).

Was Compose frueher geschenkt hat (``depends_on: service_healthy``, „alle
Container stoppen"), steht seit dem Ein-Container-Umbau als Code da — und
wird hier festgehalten:

* **Startreihenfolge** Postgres -> Index -> API und Stopp rueckwaerts,
* **Bereitschaftsgates** je Prozess, hart nur fuer die Datenbank,
* **Residenz** des Suchindex (E12): er wird nie mitgestoppt,
* **Versions-Drift-Guard** (E14): kein Start auf fremdem Datenbestand.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from watchdog_stubs import (
    FakeSupervisor,
    ProcessState,
    controller,
    running_stack,
    sleeping_stack,
)

from acoustid_watchdog.control import ProcessControlError
from acoustid_watchdog.stack import (
    RESIDENT_PROCESSES,
    STACK_PROCESSES,
    ReadinessGate,
    check_postgres_version,
    default_gates,
)
from shared.env import EnvSettings


def _gate(name: str, answers: list[bool], **kwargs: object) -> ReadinessGate:
    """Ein Gate, das der Reihe nach die vorgegebenen Antworten gibt."""

    def check() -> bool:
        return answers.pop(0) if answers else True

    return ReadinessGate(name=name, check=check, timeout_s=0.2, **kwargs)  # type: ignore[arg-type]


# --- Reihenfolge ------------------------------------------------------------


def test_the_processes_start_in_the_documented_order() -> None:
    """Erst die Datenquellen, dann der Leser (ARCHITECTURE §3).

    Unter Compose sicherte das ``depends_on``; hier ist es die Reihenfolge
    dieser Liste — und sie ist keine Kosmetik: die API bricht ihren Start
    nach 30 s ohne Datenbank ab (``api/app/service.py``).
    """
    supervisor = sleeping_stack()
    stack = controller(supervisor)

    started = stack.start()

    assert started == ["db", "index", "api"]
    assert [name for method, name in supervisor.calls if method == "startProcess"] == started


def test_the_processes_stop_in_reverse_order() -> None:
    supervisor = running_stack()
    stack = controller(supervisor, processes=("db", "api"))

    stopped = stack.stop()

    assert stopped == ["api", "db"]


def test_a_running_process_is_not_started_again() -> None:
    """Idempotenz: ``ALREADY_STARTED`` ist kein Fehler, nur ein „lief schon"."""
    supervisor = running_stack()
    stack = controller(supervisor)

    assert stack.start() == []


def test_the_importer_is_no_wake_target() -> None:
    """Jobs laufen als Subprozesse des Waechters (E10), nicht ueber supervisord."""
    assert "importer" not in STACK_PROCESSES


# --- Residenz des Suchindex (E12) -------------------------------------------


def test_the_index_stays_up_when_the_stack_goes_to_sleep() -> None:
    """Der Idle-Stopp betrifft nur Postgres und API (E12).

    Bewusste Abweichung von v2 §1.2/§3: der Index-Start liest den
    kompletten Index per ``MAP_POPULATE``; auf dem SSD-Cache haelt er kein
    Array wach, und ein mitgestoppter Index waere mit
    ``wake.hold_timeout_s`` (90 s) nicht vereinbar.
    """
    supervisor = running_stack()
    stack = controller(supervisor)

    stopped = stack.stop()

    assert stopped == ["api", "db"]
    assert supervisor.programs["index"] is ProcessState.RUNNING
    assert supervisor.count("stopProcess", "index") == 0
    assert "index" in RESIDENT_PROCESSES


def test_a_sleeping_stack_is_not_running_although_the_index_is() -> None:
    """Der residente Index macht den Stack nicht „wach"."""
    supervisor = running_stack()
    stack = controller(supervisor)
    stack.stop()

    status = stack.inspect()

    assert status.running is False
    assert status.crashed == ()


# --- Zustand erheben --------------------------------------------------------


def test_sleeping_means_the_stoppable_processes_are_stopped() -> None:
    """Schlafen ist nicht „laeuft nicht" — der Unterschied ist R8.

    Der residente Index (E12) laeuft im Schlaf weiter; er darf die Anzeige
    nicht verhindern. Ein laufender **stoppbarer** Prozess dagegen schon:
    eine wache Postgres haelt das Array wach.
    """
    supervisor = running_stack()
    stack = controller(supervisor)

    assert stack.inspect().sleeping is False

    supervisor.stopProcess("api")
    # Postgres laeuft noch — das ist kein Schlaf, sondern ein Teilzustand.
    status = stack.inspect()
    assert status.sleeping is False
    assert status.running is False
    assert status.partial is True

    supervisor.stopProcess("db")
    status = stack.inspect()
    assert status.sleeping is True
    assert status.partial is False


def test_a_starting_process_is_not_sleeping() -> None:
    """``STARTING`` ist ein laufender Start (E15-Autorestart), kein Schlaf."""
    supervisor = sleeping_stack()
    supervisor.programs["db"] = ProcessState.STARTING
    stack = controller(supervisor)

    status = stack.inspect()

    assert status.sleeping is False
    assert status.running is False
    assert status.crashed == ()
    assert status.partial is True


def test_inspect_reports_a_crash_but_not_a_stop() -> None:
    supervisor = running_stack()
    stack = controller(supervisor)

    assert stack.inspect().crashed == ()

    supervisor.stopProcess("api")
    assert stack.inspect().crashed == ()

    supervisor.crash("db")
    assert stack.inspect().crashed == ("db",)


def test_a_missing_process_counts_as_crashed() -> None:
    """Ein Prozess, den supervisord nicht kennt, ist ein Image-Bug.

    Er darf nicht als Schlaf durchgehen — sonst zeigte der Waechter einen
    Gutzustand, waehrend das Image nicht zum Code passt.
    """
    supervisor = FakeSupervisor.sleeping(["db", "index"])  # api fehlt
    stack = controller(supervisor)

    status = stack.inspect()

    assert status.running is False
    assert status.crashed == ("api",)
    assert ("api", "MISSING") in status.states


def test_inspect_asks_once_for_everything() -> None:
    """Ein ``getAllProcessInfo`` statt n Einzelfragen (der Poller-Takt)."""
    supervisor = running_stack()
    stack = controller(supervisor)

    stack.inspect()

    assert supervisor.count("getAllProcessInfo") == 1
    assert supervisor.count("getProcessInfo") == 0


# --- Bereitschaftsgates -----------------------------------------------------


def test_a_gate_holds_the_start_until_the_process_answers() -> None:
    supervisor = sleeping_stack()
    asked: list[str] = []

    def db_check() -> bool:
        asked.append("db")
        return len(asked) >= 3

    stack = controller(
        supervisor,
        gates=[ReadinessGate(name="db", check=db_check, timeout_s=5, required=True)],
    )

    assert stack.start() == ["db", "index", "api"]
    assert len(asked) == 3


def test_a_required_gate_that_expires_fails_the_start() -> None:
    """Ohne Datenbank stirbt die API nach 30 s — sie darf gar nicht erst starten."""
    supervisor = sleeping_stack()
    stack = controller(supervisor, gates=[_gate("db", [False] * 1000, required=True)])

    with pytest.raises(ProcessControlError, match="nicht bereit"):
        stack.start()

    assert supervisor.count("startProcess", "index") == 0


def test_a_soft_gate_that_expires_lets_the_start_continue() -> None:
    """Der Index braucht Minuten (MAP_POPULATE) — das ist kein Startfehler.

    Die verbindliche Frist gehoert dem Weckvorgang und der wartenden
    Anfrage (``wake.hold_timeout_s``), nicht diesem Schritt.
    """
    supervisor = sleeping_stack()
    stack = controller(supervisor, gates=[_gate("index", [False] * 1000)])

    assert stack.start() == ["db", "index", "api"]


def test_a_required_gate_is_stellt_also_for_a_process_that_was_already_running() -> None:
    """„Laeuft" heisst nicht „nimmt Verbindungen an".

    Der Fall, den ein `continue` bei ALREADY_STARTED verschluckt haette: die
    Datenbank wurde von Hand gestartet (oder ein vorheriger Weckvorgang ist
    an ihrem Gate abgelaufen und hat sie laufend zurueckgelassen) und steckt
    noch in der Recovery. Ohne Gate startete die API dagegen und stuerbe
    nach 30 s — dreimal, bis `startretries` verbraucht sind.
    """
    supervisor = sleeping_stack()
    supervisor.programs["db"] = ProcessState.RUNNING  # laeuft schon
    stack = controller(supervisor, gates=[_gate("db", [False] * 1000, required=True)])

    with pytest.raises(ProcessControlError, match="nicht bereit"):
        stack.start()

    # Und vor allem: die API wurde nicht gestartet.
    assert supervisor.count("startProcess", "api") == 0
    assert supervisor.programs["api"] is ProcessState.STOPPED


def test_a_required_gate_of_a_running_process_lets_the_start_continue() -> None:
    """Antwortet die schon laufende Datenbank, geht es normal weiter."""
    supervisor = sleeping_stack()
    supervisor.programs["db"] = ProcessState.RUNNING
    asked: list[str] = []

    def db_check() -> bool:
        asked.append("db")
        return True

    stack = controller(
        supervisor,
        gates=[ReadinessGate(name="db", check=db_check, timeout_s=5, required=True)],
    )

    # `db` steht nicht in der Liste — gestartet hat ihn dieser Aufruf nicht.
    assert stack.start() == ["index", "api"]
    assert asked == ["db"]


def test_a_soft_gate_is_skipped_for_a_process_that_was_already_running() -> None:
    """Der residente Index laeuft seit dem Containerstart — vielleicht noch ladend.

    Auf ihn zu warten wuerde jeden Weckvorgang um Minuten verlaengern,
    obwohl Postgres und API laengst bereit waeren.
    """
    supervisor = sleeping_stack()
    supervisor.programs["index"] = ProcessState.RUNNING
    asked: list[str] = []

    stack = controller(
        supervisor,
        gates=[
            ReadinessGate(
                name="index",
                check=lambda: bool(asked.append("index")),  # antwortet nie True
                timeout_s=5,
            )
        ],
    )

    assert stack.start() == ["db", "api"]
    assert asked == []


def test_the_default_gates_cover_all_three_processes() -> None:
    """Jeder Stack-Prozess bekommt eine Bereitschaftsfrage — und nur die DB hart."""
    gates = {gate.name: gate for gate in default_gates(EnvSettings())}

    assert set(gates) == set(STACK_PROCESSES)
    assert gates["db"].required is True
    assert gates["index"].required is False
    assert gates["api"].required is False
    # Die Adressen kommen aus den Bootstrap-Werten, nicht aus Konstanten.
    assert EnvSettings().api_health_url in gates["api"].description


# --- Versions-Drift-Guard (E14) ---------------------------------------------


def test_no_drift_without_a_foreign_cluster(tmp_path: Path) -> None:
    (tmp_path / "18").mkdir()
    (tmp_path / "18" / "PG_VERSION").write_text("18\n", encoding="utf-8")

    assert check_postgres_version(tmp_path, 18) is None


def test_no_drift_for_a_missing_data_root(tmp_path: Path) -> None:
    """Ein fehlender Mount ist kein Drift — darueber klagt Postgres selbst."""
    assert check_postgres_version(tmp_path / "gibtsnicht", 18) is None


def test_an_empty_directory_is_no_drift(tmp_path: Path) -> None:
    """Erkannt wird ein **Bestand**, nicht ein Verzeichnisname."""
    (tmp_path / "17").mkdir()

    assert check_postgres_version(tmp_path, 18) is None


def test_a_foreign_major_is_reported(tmp_path: Path) -> None:
    (tmp_path / "17").mkdir()
    (tmp_path / "17" / "PG_VERSION").write_text("17\n", encoding="utf-8")

    drift = check_postgres_version(tmp_path, 18)

    assert drift is not None
    assert drift.found == (17,)
    assert "17" in str(drift) and "18" in str(drift)


def test_the_guard_stops_the_start_before_the_first_process(tmp_path: Path) -> None:
    """Kein ``startProcess``, wenn der Bestand nicht zum Image passt.

    Postgres 18 wuerde auf einem 17er-Verzeichnis ohnehin nicht starten —
    die Meldung stuende aber nur im Prozesslog, und der Stack ginge wortlos
    in ``fehler``.
    """
    supervisor = sleeping_stack()

    def guard() -> None:
        raise ProcessControlError("Datenbestand von PostgreSQL 17 gefunden")

    stack = controller(supervisor, version_guard=guard)

    with pytest.raises(ProcessControlError, match="PostgreSQL 17"):
        stack.start()

    assert supervisor.calls == []
