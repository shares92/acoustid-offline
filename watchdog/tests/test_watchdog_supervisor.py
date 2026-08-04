"""``SupervisorClient`` — gegen die Attrappe und gegen ein **echtes** supervisord.

Der Ersatz fuer ``test_watchdog_docker.py`` und zugleich die Antwort auf
das Learning „Attrappen fremder Systeme sind Paritaets-Code": eine Attrappe
definiert, wogegen alle spaeteren Tests laufen — ihre eigenen Tests helfen
nicht, weil sie aus derselben Lektuere stammen. Diese Datei hat deshalb
zwei Haelften:

1. **Semantik gegen :class:`~watchdog_stubs.FakeSupervisor`** — schnell,
   deterministisch, deckt jeden Fehlerpfad ab.
2. **Kontrakt gegen ein echtes ``supervisord``** (Marker ``supervisor``) —
   startet einen eigenen Daemon als Subprozess mit Wegwerf-Konfiguration
   und Dummy-Programmen. Kein Docker noetig, keine Sekunde laenger als
   noetig. Er prueft genau die Zusagen, an denen die Attrappe haengt:
   Fault-Codes, Zustandsuebergaenge, Stopp-Verhalten.

**Was der Kontrakt-Test belegt** (und die Attrappe nur behauptet):

===============================  ==========================================
Idempotenz                       doppelter Start -> ``ALREADY_STARTED``,
                                 doppelter Stopp -> ``NOT_RUNNING``
Image-Bug                        unbekanntes Programm -> ``BAD_NAME``,
                                 fehlendes Kommando -> ``NO_FILE``
Startfehler                      Prozess ueberlebt ``startsecs`` nicht ->
                                 ``SPAWN_ERROR``/``ABNORMAL_TERMINATION``
Absturz im Betrieb               ``RUNNING`` -> ``EXITED`` (nicht ``FATAL``)
Stopp-Frist                      TERM-ignorierender Prozess wird nach
                                 ``stopwaitsecs`` mit ``SIGKILL`` beendet;
                                 der Aufruf kehrt erst danach zurueck
===============================  ==========================================

Laufen lassen::

    uv run pytest watchdog/tests/test_watchdog_supervisor.py

Ist ``supervisord`` nicht installiert, wird die zweite Haelfte **mit
Begruendung** abgewaehlt (wie die Integrationstests); in der CI ist es ueber
die Dev-Abhaengigkeiten da.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from watchdog_stubs import FakeSupervisor, ProcessState, running_stack, sleeping_stack

from acoustid_watchdog.control import ProcessControlError
from acoustid_watchdog.process import (
    RUNNING_STATES,
    SUPERVISOR_SOCKET,
    Fault,
    ProcessInfo,
    SupervisorClient,
    SupervisorError,
    SupervisorUnavailableError,
    UnknownProcessError,
)

PROGRAMS = ("db", "index", "api")


def client(supervisor: FakeSupervisor) -> SupervisorClient:
    return SupervisorClient(proxy=supervisor)


# --- Semantik gegen die Attrappe --------------------------------------------


def test_start_reports_whether_it_did_something() -> None:
    """Die Idempotenz-Zusage: ``True`` = gestartet, ``False`` = lief schon.

    Das Gegenstueck zu HTTP 204/304 der Docker-Engine-API — nur kommt „lief
    schon" hier als Fault und nicht als Statuscode.
    """
    supervisor = sleeping_stack(PROGRAMS)
    supervisor_client = client(supervisor)

    assert supervisor_client.start("db") is True
    assert supervisor_client.start("db") is False


def test_stop_reports_whether_it_did_something() -> None:
    supervisor = running_stack(PROGRAMS)
    supervisor_client = client(supervisor)

    assert supervisor_client.stop("db") is True
    assert supervisor_client.stop("db") is False


def test_an_unknown_program_is_an_image_bug() -> None:
    """``BAD_NAME`` ist kein Betriebsfehler — die Namen stehen im Image."""
    supervisor_client = client(sleeping_stack(PROGRAMS))

    with pytest.raises(UnknownProcessError, match="BAD_NAME"):
        supervisor_client.start("gibtsnicht")
    with pytest.raises(UnknownProcessError):
        supervisor_client.inspect("gibtsnicht")


def test_a_failed_start_is_an_error() -> None:
    """``SPAWN_ERROR`` ist **der** Fehler eines Weckvorgangs."""
    supervisor = sleeping_stack(PROGRAMS)
    supervisor.fail_on.add("db")
    supervisor_client = client(supervisor)

    with pytest.raises(SupervisorError, match="SPAWN_ERROR"):
        supervisor_client.start("db")

    assert supervisor.programs["db"] is ProcessState.FATAL


def test_every_error_is_a_process_control_error() -> None:
    """Die Weck-Logik faengt nur die Basis — alles muss darunter passen."""
    for error in (SupervisorError, SupervisorUnavailableError, UnknownProcessError):
        assert issubclass(error, ProcessControlError)


def test_signal_is_idempotent_for_a_stopped_process() -> None:
    """Ein gestoppter Prozess ist kein Fehler, sondern schon das Ziel."""
    supervisor = sleeping_stack(PROGRAMS)
    supervisor_client = client(supervisor)

    assert supervisor_client.signal("db", "TERM") is False

    supervisor_client.start("db")
    assert supervisor_client.signal("db", "TERM") is True


def test_states_answers_for_all_programs_at_once() -> None:
    supervisor = running_stack(PROGRAMS)
    supervisor.crash("api")

    states = client(supervisor).states()

    assert set(states) == set(PROGRAMS)
    assert states["db"].running is True
    assert states["api"].crashed is True
    assert supervisor.count("getAllProcessInfo") == 1


def test_a_stopped_process_is_not_crashed() -> None:
    """Der Unterschied, an dem die Kante ``ready→error`` haengt."""
    supervisor = running_stack(PROGRAMS)
    supervisor.stopProcess("db")

    info = client(supervisor).inspect("db")

    assert info.running is False
    assert info.crashed is False


def test_an_unreachable_socket_is_its_own_error(tmp_path: Path) -> None:
    """„Steuerung antwortet nicht" ist kein Fehler *des Stacks*."""
    supervisor_client = SupervisorClient(str(tmp_path / "nicht-da.sock"))

    with pytest.raises(SupervisorUnavailableError):
        supervisor_client.states()


def test_an_unknown_state_becomes_unknown_instead_of_crashing() -> None:
    """Ein Zustand, den wir nicht kennen, haelt den Waechter nicht an."""
    info = ProcessInfo.from_payload({"name": "db", "state": 12345, "statename": "WAT"})

    assert info.state is ProcessState.UNKNOWN
    assert info.crashed is True


def test_a_malformed_answer_is_an_error() -> None:
    with pytest.raises(SupervisorError):
        ProcessInfo.from_payload({"kein": "prozess"})


def test_the_socket_path_is_short_enough_for_af_unix() -> None:
    """AF_UNIX-Namen sind auf ~104 Byte begrenzt (empirisch auf macOS).

    Ein Pfad unter ``/config`` waere auf manchen Wirten schon zu lang — und
    der Fehler kaeme erst zur Laufzeit, beim ersten Weckversuch.
    """
    assert len(SUPERVISOR_SOCKET) < 100


# --- Kontrakt gegen ein echtes supervisord ----------------------------------

#: Wegwerf-Konfiguration: vier Dummy-Programme, die jeden Pfad abbilden,
#: den der Client kennt. ``stopwaitsecs=2`` beim widerspenstigen Prozess,
#: damit der Nachweis „SIGKILL nach der Frist" zwei Sekunden kostet und
#: nicht fuenf Minuten wie im Betrieb.
_CONF = """
[supervisord]
nodaemon=true
logfile={run}/supervisord.log
pidfile={run}/supervisord.pid
childlogdir={run}
loglevel=debug

[unix_http_server]
file={socket}
chmod=0700

[rpcinterface:supervisor]
supervisor.rpcinterface_factory = supervisor.rpcinterface:make_main_rpcinterface

[supervisorctl]
serverurl=unix://{socket}

[program:sleeper]
command=/bin/sh -c 'while true; do sleep 1; done'
autostart=false
autorestart=unexpected
startretries=1
startsecs=1

[program:stubborn]
command=/bin/sh -c 'trap "" TERM; while true; do sleep 1; done'
autostart=false
autorestart=false
startretries=1
startsecs=1
stopwaitsecs=2

[program:mortal]
command=/bin/sh -c 'while true; do sleep 1; done'
autostart=false
autorestart=false
startretries=1
startsecs=1

[program:dying]
command=/bin/sh -c 'exit 3'
autostart=false
autorestart=false
startretries=1
startsecs=1

[program:nosuchfile]
command=/nicht/vorhanden/programm
autostart=false
autorestart=false
startretries=1
"""


@pytest.fixture(scope="module")
def real_supervisor() -> Iterator[SupervisorClient]:
    """Ein echtes ``supervisord`` als Subprozess, mit eigenem Wegwerf-Socket.

    Der Socket liegt bewusst **nicht** unter ``tmp_path``: AF_UNIX-Namen
    sind auf ~104 Byte begrenzt, und die pytest-Verzeichnisse von macOS
    (``/private/var/folders/...``) reissen die Grenze (empirisch beim Bau
    dieser Phase). Also ein kurzes eigenes Verzeichnis.
    """
    binary = shutil.which("supervisord")
    if binary is None:
        pytest.skip("supervisord ist nicht installiert (uv sync --all-packages)")

    run_dir = Path(tempfile.mkdtemp(prefix="sv-", dir="/tmp"))
    socket_path = run_dir / "s.sock"
    config = run_dir / "supervisord.conf"
    config.write_text(_CONF.format(run=run_dir, socket=socket_path), encoding="utf-8")

    process = subprocess.Popen(
        [binary, "-c", str(config)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    supervisor_client = SupervisorClient(str(socket_path), timeout_s=30)
    try:
        deadline = time.monotonic() + 30
        while True:
            if process.poll() is not None:
                raise AssertionError(f"supervisord startete nicht: {process.communicate()[1]}")
            try:
                supervisor_client.states()
                break
            except SupervisorUnavailableError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)
        yield supervisor_client
    finally:
        supervisor_client.close()
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensiv
            process.kill()
        shutil.rmtree(run_dir, ignore_errors=True)


@pytest.mark.supervisor
def test_contract_start_and_stop_are_idempotent(real_supervisor: SupervisorClient) -> None:
    """Die Zusage, an der die ganze Weck-Logik haengt — am Original."""
    assert real_supervisor.start("sleeper") is True
    assert real_supervisor.start("sleeper") is False
    assert real_supervisor.inspect("sleeper").running is True

    assert real_supervisor.stop("sleeper") is True
    assert real_supervisor.stop("sleeper") is False


@pytest.mark.supervisor
def test_contract_a_stopped_process_is_not_crashed(real_supervisor: SupervisorClient) -> None:
    """``STOPPED`` nach ``stopProcess`` — der Gutzustand des Idle-Stopps."""
    real_supervisor.start("sleeper")
    real_supervisor.stop("sleeper")

    info = real_supervisor.inspect("sleeper")

    assert info.state is ProcessState.STOPPED
    assert info.crashed is False


@pytest.mark.supervisor
def test_contract_a_killed_process_becomes_exited(real_supervisor: SupervisorClient) -> None:
    """Absturz im Betrieb ist ``RUNNING`` -> ``EXITED``, nie direkt ``FATAL``.

    Genau diese Zusage der Attrappe war im M1a-Zweitreview ein Treue-Fehler
    (DECISIONS 2026-08-04) — hier steht sie am Original. Gemessen wird an
    ``mortal`` (``autorestart=false``), damit der Endzustand stehen bleibt;
    dass ``autorestart=unexpected`` ihn wieder heilt, prueft der Test
    darunter.
    """
    real_supervisor.start("mortal")
    real_supervisor.signal("mortal", "KILL")

    info = _await_state(real_supervisor, "mortal", ProcessState.EXITED)

    assert info.state is ProcessState.EXITED
    assert info.crashed is True
    assert info.pid == 0


@pytest.mark.supervisor
def test_contract_autorestart_unexpected_heals_a_crash(real_supervisor: SupervisorClient) -> None:
    """E15, Richtung 2: ein Absturz wird geheilt — der Idle-Stopp nicht (s. u.)."""
    real_supervisor.start("sleeper")
    first = real_supervisor.inspect("sleeper").pid
    real_supervisor.signal("sleeper", "KILL")

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        info = real_supervisor.inspect("sleeper")
        if info.running and info.pid != first:
            break
        time.sleep(0.1)

    assert info.running is True
    assert info.pid != first
    real_supervisor.stop("sleeper")


def _await_state(
    supervisor_client: SupervisorClient,
    name: str,
    wanted: ProcessState,
    *,
    timeout_s: float = 20.0,
) -> ProcessInfo:
    """Wartet, bis ein Programm den gewuenschten Zustand erreicht."""
    deadline = time.monotonic() + timeout_s
    info = supervisor_client.inspect(name)
    while info.state is not wanted and time.monotonic() < deadline:
        time.sleep(0.05)
        info = supervisor_client.inspect(name)
    return info


@pytest.mark.supervisor
def test_contract_a_stopped_process_stays_stopped(real_supervisor: SupervisorClient) -> None:
    """E15, Richtung 1: kein Idle-Stopp-Loop.

    ``autorestart=unexpected`` startet **nichts** neu, was per
    ``stopProcess`` gestoppt wurde — sonst machte supervisord jeden
    Idle-Stopp sofort rueckgaengig.
    """
    real_supervisor.start("sleeper")
    real_supervisor.stop("sleeper")

    time.sleep(2)

    assert real_supervisor.inspect("sleeper").state is ProcessState.STOPPED


@pytest.mark.supervisor
def test_contract_an_unknown_program_is_bad_name(real_supervisor: SupervisorClient) -> None:
    with pytest.raises(UnknownProcessError, match="BAD_NAME"):
        real_supervisor.start("gibtsnicht")


@pytest.mark.supervisor
def test_contract_a_missing_command_is_an_image_bug(real_supervisor: SupervisorClient) -> None:
    """``NO_FILE`` — supervisord prueft das Kommando **vor** allem anderen.

    Fuer den Waechter ist es dasselbe wie ``BAD_NAME``: das Image passt
    nicht zum Code.
    """
    with pytest.raises(UnknownProcessError, match="NO_FILE"):
        real_supervisor.start("nosuchfile")


@pytest.mark.supervisor
def test_contract_a_dying_process_is_a_start_failure(real_supervisor: SupervisorClient) -> None:
    """Ein Prozess, der ``startsecs`` nicht ueberlebt, ist ein Startfehler."""
    with pytest.raises(SupervisorError) as raised:
        real_supervisor.start("dying")

    assert "SPAWN_ERROR" in str(raised.value) or "ABNORMAL_TERMINATION" in str(raised.value)
    assert not isinstance(raised.value, UnknownProcessError)


@pytest.mark.supervisor
def test_contract_stop_waits_for_stopwaitsecs_then_kills(
    real_supervisor: SupervisorClient,
) -> None:
    """Ein TERM-ignorierender Prozess kostet genau ``stopwaitsecs`` (hier 2 s).

    Der Nachweis fuer die Leseschranke des Clients: ``stopProcess`` kehrt
    erst zurueck, wenn der Prozess wirklich weg ist. Mit Postgres
    (``stopwaitsecs=300``) waere eine kleinere Schranke ein Fehler, waehrend
    der Stopp noch geordnet laeuft.
    """
    real_supervisor.start("stubborn")

    started_at = time.monotonic()
    assert real_supervisor.stop("stubborn") is True
    took = time.monotonic() - started_at

    assert took >= 2.0, f"Stopp kehrte nach {took:.2f}s zurueck — vor der Frist"
    assert took < 20.0
    assert real_supervisor.inspect("stubborn").state is ProcessState.STOPPED
    print(f"\nstopwaitsecs-Nachweis: SIGKILL nach {took:.2f}s (Frist 2 s)")


def test_our_numbers_are_the_originals() -> None:
    """Fault-Codes und Zustandswerte gegen ``supervisor`` selbst.

    Der schaerfste Paritaets-Test und der billigste: die Zahlen stehen nicht
    in einer Doku, sondern im Quelltext des Originals — und der liegt als
    Dev-Abhaengigkeit im venv. Ein Zahlendreher (der M1a-Fall: ``FAILED``
    statt ``SPAWN_ERROR``) faellt hier auf, ohne dass ein Daemon laufen muss.
    """
    supervisor_states = pytest.importorskip("supervisor.states")
    supervisor_xmlrpc = pytest.importorskip("supervisor.xmlrpc")

    for state in ProcessState:
        assert getattr(supervisor_states.ProcessStates, state.name) == int(state), state.name
    for fault in Fault:
        assert getattr(supervisor_xmlrpc.Faults, fault.name) == int(fault), fault.name
    # Und die Menge „gilt als laufend", an der die Idempotenz haengt.
    assert {int(state) for state in RUNNING_STATES} == set(supervisor_states.RUNNING_STATES)
