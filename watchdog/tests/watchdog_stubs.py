"""Attrappen der Waechter-Tests (Phase 15, umgebaut in M1b).

Der Waechter spricht mit zwei Gegenstellen: der Prozess-Steuerung
(supervisord, XML-RPC ueber einen Unix-Socket) und dem API-Dienst (HTTP).
Beide werden hier ersetzt — aber bewusst **nicht** durch Attrappen der
Client-Klassen: der echte :class:`~acoustid_watchdog.process.
SupervisorClient` und die echte :class:`~acoustid_watchdog.proxy.
ReverseProxy` laufen durch jeden Test mit, damit Fault-Uebersetzung,
Statusauswertung und Kopfzeilen-Behandlung wirklich geprueft werden.

* :class:`FakeSupervisor` — die Gegenstelle von supervisord. Sie spricht
  die Original-Methodennamen, damit sie an die Stelle eines
  ``xmlrpc.client.ServerProxy`` treten kann.
* :class:`RecordingProxyTransport` / :func:`probe` — der API-Dienst auf
  einem ``httpx.MockTransport``.

**Die Zustands- und Fehlerwerte kommen aus dem Produktionsmodul**
(:mod:`acoustid_watchdog.process`) und werden hier **nicht** noch einmal
aufgeschrieben. Eine Attrappe mit eigener Kopie derselben Zahlen ist eine
Divergenz, die auf ihr Auftreten wartet (LEARNINGS „Attrappen fremder
Systeme sind Paritaets-Code"). Wogegen die Werte selbst stimmen, haelt der
Kontrakt-Test gegen ein **echtes** supervisord fest
(``test_watchdog_supervisor.py``).

Bewusst ein eigenes Modul und nicht die conftest.py: pytest laedt alle
`conftest`-Module unter demselben Namen, ein ``from conftest import …``
wuerde je nach Sammelreihenfolge im falschen Paket landen (gleiche
Begruendung wie in ``api/tests/stubs.py``).
"""

from __future__ import annotations

import json
import time
import xmlrpc.client
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, Final, Self

import httpx

from acoustid_watchdog.process import (
    RUNNING_STATES,
    Fault,
    ProcessState,
    SupervisorClient,
)
from acoustid_watchdog.stack import STACK_PROCESSES, ServiceGroupController
from acoustid_watchdog.wake import ReadinessProbe
from shared.env import EnvSettings

__all__ = [
    "RUNNING_STATES",
    "STOPPED_STATES",
    "FakeProbe",
    "FakeSupervisor",
    "Fault",
    "ProcessState",
    "RecordingProxyTransport",
    "controller",
    "probe",
    "running_stack",
    "sleeping_stack",
    "streamed",
    "supervisor_client",
]


def streamed(
    status_code: int, body: bytes = b"", headers: dict[str, str] | None = None
) -> httpx.Response:
    """Antwort, wie sie ein **echter** Transport liefert: als Strom.

    ``httpx.Response(content=…)`` liest den Rumpf sofort ein und markiert den
    Strom als verbraucht — der Proxy koennte ihn dann nicht mehr
    weiterstreamen. Die HTTP-Transporte von httpx geben immer einen
    ungelesenen Strom zurueck; diese Attrappe tut dasselbe, damit der
    Streaming-Pfad des Proxys wirklich durchlaufen wird.
    """
    return httpx.Response(
        status_code,
        stream=httpx.ByteStream(body),
        headers={"content-length": str(len(body)), **(headers or {})},
    )


#: Zustaende, in denen ein Prozess endgueltig steht. ``STOPPING`` fehlt hier
#: mit Absicht — es ist weder das eine noch das andere, und ``stopProcess``
#: beantwortet es (wie das Original) trotzdem mit ``NOT_RUNNING``.
STOPPED_STATES: Final = frozenset(
    {ProcessState.STOPPED, ProcessState.EXITED, ProcessState.FATAL, ProcessState.UNKNOWN}
)


class FakeSupervisor:
    """Ein supervisord aus einem Woerterbuch von Prozesszustaenden.

    Zustands-Woerterbuch, :attr:`calls`, :attr:`fail_on` und
    :attr:`all_running` — derselbe Zuschnitt, den in v1 die
    Docker-Daemon-Attrappe hatte, aber in der Sprache von supervisord.

    Sie versteht die Methoden, die der
    :class:`~acoustid_watchdog.process.SupervisorClient` braucht, und zwar
    unter ihren echten Namen::

        getProcessInfo(name)         Zustand eines Programms
        getAllProcessInfo()          Zustand aller Programme (der Poller)
        startProcess(name, wait)     starten
        stopProcess(name, wait)      stoppen
        signalProcess(name, signal)  Signal schicken
        getState()                   Zustand von supervisord selbst

    **Warum die Original-Namen** (und nicht ``start``/``stop``): so laesst
    sich die Attrappe an die Stelle eines ``xmlrpc.client.ServerProxy``
    setzen — :attr:`supervisor` liefert sich selbst zurueck, damit auch der
    uebliche Aufruf ``proxy.supervisor.startProcess(…)`` durchgeht. Der
    echte Client kann dann ohne Sonderweg getestet werden.

    **Fehler sind ``xmlrpc.client.Fault``**, wie sie ueber die Leitung
    kaemen — kein eigener Ausnahmetyp. Genau daran haengt die Idempotenz:
    ``ALREADY_STARTED``/``NOT_RUNNING`` sind gutartig und werden vom Client
    zu ``False`` (§ „lief schon"), alles andere ist ein echter Fehler.
    """

    def __init__(self, programs: Mapping[str, ProcessState] | None = None) -> None:
        #: Programmname -> Zustand. Fehlt ein Name, antwortet supervisord
        #: mit ``BAD_NAME`` (wie fuer ein nie konfiguriertes Programm).
        self.programs: dict[str, ProcessState] = dict(programs or {})
        #: ``(methode, name)`` in Aufrufreihenfolge.
        self.calls: list[tuple[str, str]] = []
        #: Programme, deren Aufrufe mit ``FAILED`` beantwortet werden —
        #: das Gegenstueck zum HTTP 500 in :class:`FakeDaemon`.
        self.fail_on: set[str] = set()
        #: Wie oft ein Programm gestartet wurde (Autorestart-Szenarien).
        self.starts: dict[str, int] = dict.fromkeys(self.programs, 0)

    # --- Aufbauhilfen -------------------------------------------------------

    @classmethod
    def sleeping(cls, names: Iterable[str]) -> Self:
        """Alle Programme gestoppt — der schlafende Stack."""
        return cls(dict.fromkeys(names, ProcessState.STOPPED))

    @classmethod
    def running(cls, names: Iterable[str]) -> Self:
        """Alle Programme laufen — der wache Stack."""
        return cls(dict.fromkeys(names, ProcessState.RUNNING))

    @property
    def supervisor(self) -> Self:
        """Der ``supervisor``-Namensraum von XML-RPC — hier wir selbst."""
        return self

    # --- Methoden von supervisord (Original-Namen) --------------------------

    def getState(self) -> dict[str, Any]:  # noqa: N802 - XML-RPC-Name
        """Zustand von supervisord selbst; hier immer ``RUNNING``."""
        self.calls.append(("getState", ""))
        return {"statecode": 1, "statename": "RUNNING"}

    def getProcessInfo(self, name: str) -> dict[str, Any]:  # noqa: N802 - XML-RPC-Name
        """Zustand eines Programms.

        Raises:
            xmlrpc.client.Fault: ``BAD_NAME``, wenn es das Programm nicht
                gibt; ``FAILED``, wenn es in :attr:`fail_on` steht.
        """
        self.calls.append(("getProcessInfo", name))
        return self._info(self._state_of(name), name)

    def getAllProcessInfo(self) -> list[dict[str, Any]]:  # noqa: N802 - XML-RPC-Name
        """Zustand aller Programme — der Weg des Zustands-Pollers.

        Ein Aufruf statt einer je Programm: das ist der Grund, warum der
        Poller in M1b nicht mehr n-mal fragen muss.

        Raises:
            xmlrpc.client.Fault: ``FAILED``, wenn irgendein Programm in
                :attr:`fail_on` steht — supervisord antwortet auf diese
                Frage entweder ganz oder gar nicht.
        """
        self.calls.append(("getAllProcessInfo", "*"))
        for name in self.programs:
            self._guard_failure(name)
        return [self._info(state, name) for name, state in self.programs.items()]

    def startProcess(self, name: str, wait: bool = True) -> bool:  # noqa: N802 - XML-RPC-Name
        """Startet ein Programm.

        Args:
            name: Programmname aus der supervisord-Konfiguration.
            wait: Auf ``RUNNING`` warten (``startsecs``). Ohne Warten
                bleibt das Programm in ``STARTING`` stehen.

        Returns:
            Immer ``True`` — supervisord kennt keinen anderen Erfolg. Dass
            das Programm schon lief, kommt als Fault, nicht als Rueckgabe.

        Raises:
            xmlrpc.client.Fault: ``BAD_NAME`` (unbekanntes Programm),
                ``ALREADY_STARTED`` (laeuft/startet/versucht es gerade)
                oder ``SPAWN_ERROR`` (Start gescheitert, :attr:`fail_on`).

        Die Reihenfolge ist die des Originals: erst der Name, dann „laeuft
        schon", und erst danach wird wirklich gespawnt — nur dort kann ein
        Start scheitern.
        """
        self.calls.append(("startProcess", name))
        state = self._require_known(name)
        if state in RUNNING_STATES:
            raise xmlrpc.client.Fault(Fault.ALREADY_STARTED, f"ALREADY_STARTED: {name}")
        if name in self.fail_on:
            # Wie im Original: der Startversuch laeuft wirklich, scheitert
            # und laesst das Programm in FATAL zurueck — der Fault kommt
            # danach. `starts` zaehlt ihn nicht: es war kein Start.
            self.start_failure(name)
            raise xmlrpc.client.Fault(Fault.SPAWN_ERROR, f"SPAWN_ERROR: {name}")
        self.programs[name] = ProcessState.RUNNING if wait else ProcessState.STARTING
        self.starts[name] = self.starts.get(name, 0) + 1
        return True

    def stopProcess(self, name: str, wait: bool = True) -> bool:  # noqa: N802 - XML-RPC-Name
        """Stoppt ein Programm.

        Args:
            name: Programmname aus der supervisord-Konfiguration.
            wait: Auf das Ende warten (``stopwaitsecs``). Ohne Warten
                bleibt das Programm in ``STOPPING`` stehen.

        Returns:
            Immer ``True``; „stand schon" kommt als ``NOT_RUNNING``-Fault.

        Raises:
            xmlrpc.client.Fault: ``BAD_NAME``, ``NOT_RUNNING`` oder
                ``FAILED``.
        """
        self.calls.append(("stopProcess", name))
        state = self._state_of(name)
        if state not in RUNNING_STATES:
            raise xmlrpc.client.Fault(Fault.NOT_RUNNING, f"NOT_RUNNING: {name}")
        self.programs[name] = ProcessState.STOPPED if wait else ProcessState.STOPPING
        return True

    def signalProcess(self, name: str, signal: str | int) -> bool:  # noqa: N802 - XML-RPC-Name
        """Schickt einem laufenden Programm ein Signal.

        Der Weg, auf dem ein Job in M2.5 sein ``SIGTERM`` bekommt. Der
        Zustand aendert sich dadurch **nicht** — was das Signal bewirkt,
        entscheidet der Prozess; ein Test setzt das Ergebnis per
        :meth:`crash` oder :meth:`stopProcess`.

        Raises:
            xmlrpc.client.Fault: ``BAD_NAME``, ``NOT_RUNNING`` oder
                ``FAILED``.
        """
        self.calls.append(("signalProcess", name))
        state = self._state_of(name)
        if state not in RUNNING_STATES:
            raise xmlrpc.client.Fault(Fault.NOT_RUNNING, f"NOT_RUNNING: {name}")
        return True

    # --- Steuerung aus dem Test ---------------------------------------------

    def crash(self, name: str) -> None:
        """Laesst ein laufendes Programm abstuerzen — ohne Zutun von aussen.

        Der Fall, den es unter Docker so nicht gab und der in M1b die neue
        Kante ``ready→error`` begruendet: der Prozess ist weg, aber der
        Waechter hat ihn nicht schlafen gelegt. Zaehlt nicht als Aufruf —
        es ist keiner.

        Das Ziel ist ``EXITED`` und nicht ``FATAL``: die Zustandsmaschine
        von supervisord kennt keine Kante ``RUNNING → FATAL``. Ein
        laufender Prozess, der endet, ist **immer** ``EXITED``; ``FATAL``
        erreicht nur, wer den Start nicht schafft (:meth:`start_failure`).
        """
        if name not in self.programs:
            raise KeyError(name)
        self.programs[name] = ProcessState.EXITED

    def start_failure(self, name: str, *, retries: int = 1) -> list[ProcessState]:
        """Laesst einen Startversuch endgueltig scheitern.

        Geht den Weg, den supervisord wirklich geht:
        ``STARTING → BACKOFF`` je Versuch und ``FATAL``, wenn
        ``startretries`` verbraucht sind. Der einzige Weg zu ``FATAL`` —
        deshalb hat :meth:`crash` kein Zustandsargument.

        Args:
            name: Programmname.
            retries: Wie viele Startversuche scheitern (``startretries``).

        Returns:
            Die durchlaufenen Zustaende in Reihenfolge — damit ein Test
            belegen kann, dass der Weg und nicht nur das Ziel stimmt.
        """
        if name not in self.programs:
            raise KeyError(name)
        path: list[ProcessState] = []
        for _ in range(retries):
            for state in (ProcessState.STARTING, ProcessState.BACKOFF):
                self.programs[name] = state
                path.append(state)
        self.programs[name] = ProcessState.FATAL
        path.append(ProcessState.FATAL)
        return path

    # --- Auswertung ---------------------------------------------------------

    def count(self, method: str, name: str | None = None) -> int:
        """Wie oft wurde ``method`` (optional fuer ``name``) aufgerufen?"""
        return sum(
            1
            for call_method, call_name in self.calls
            if call_method == method and (name is None or call_name == name)
        )

    @property
    def all_running(self) -> bool:
        """Laufen alle Programme wirklich (``RUNNING``, nicht nur „nicht aus")?"""
        return bool(self.programs) and all(
            state is ProcessState.RUNNING for state in self.programs.values()
        )

    @property
    def states(self) -> dict[str, ProcessState]:
        """Der Zustand aller Programme als einfaches Woerterbuch."""
        return dict(self.programs)

    # --- Innenleben ---------------------------------------------------------

    def _require_known(self, name: str) -> ProcessState:
        """Zustand eines Programms; unbekannte Namen sind ``BAD_NAME``."""
        if name not in self.programs:
            raise xmlrpc.client.Fault(Fault.BAD_NAME, f"BAD_NAME: {name}")
        return self.programs[name]

    def _state_of(self, name: str) -> ProcessState:
        """Wie :meth:`_require_known`, mit ``FAILED``-Sperre.

        Fuer alles ausser dem Start: dort ist der passende Fault
        ``SPAWN_ERROR``, und er faellt erst nach der Idempotenz-Pruefung.
        """
        state = self._require_known(name)
        self._guard_failure(name)
        return state

    def _guard_failure(self, name: str) -> None:
        if name in self.fail_on:
            raise xmlrpc.client.Fault(Fault.FAILED, f"FAILED: {name}")

    def _info(self, state: ProcessState, name: str) -> dict[str, Any]:
        """Die Felder, die ``getProcessInfo`` liefert (Auszug des Originals)."""
        now = int(time.time())
        # Bewusst NICHT ueber ``RUNNING_STATES``: dort gehoert ``BACKOFF``
        # dazu (supervisord zaehlt es als „laeuft schon"), aber der Prozess
        # ist dabei tot — er wartet auf den naechsten Versuch. Eine PID hat
        # nur, wo wirklich einer laeuft.
        alive = state in (ProcessState.STARTING, ProcessState.RUNNING, ProcessState.STOPPING)
        return {
            "name": name,
            "group": name,
            "state": int(state),
            "statename": state.name,
            "pid": 4242 if alive else 0,
            "start": now if state is not ProcessState.STOPPED else 0,
            "stop": 0 if alive else now,
            "now": now,
            "spawnerr": "" if state is not ProcessState.FATAL else "zu oft gescheitert",
            "exitstatus": 0 if state is not ProcessState.EXITED else 1,
            "description": state.name.lower(),
        }


def supervisor_client(supervisor: FakeSupervisor) -> SupervisorClient:
    """Echter :class:`SupervisorClient` auf einer Attrappen-Gegenstelle.

    Der Grund fuer die Original-Methodennamen in :class:`FakeSupervisor`:
    so laeuft die **echte** Fault-Uebersetzung des Clients durch jeden Test
    mit, statt in den Tests nachgebaut zu werden.
    """
    return SupervisorClient(proxy=supervisor)


def controller(
    supervisor: FakeSupervisor,
    *,
    processes: Sequence[str] = STACK_PROCESSES,
    gates: Sequence[Any] = (),
    version_guard: Callable[[], None] | None = None,
) -> ServiceGroupController:
    """Echte Prozessgruppen-Steuerung auf einer Attrappen-Gegenstelle.

    **Ohne Gates** per Vorgabe: die Bereitschaftsfragen des Betriebs
    sprechen mit Postgres und HTTP-Diensten, die es im Unit-Test nicht
    gibt. Wer sie pruefen will, gibt eigene mit (``ReadinessGate`` mit einer
    Attrappen-Frage) — so bleibt jeder Test bei einer Sache.
    """
    return ServiceGroupController(
        supervisor_client(supervisor),
        processes=processes,
        gates=gates,
        version_guard=version_guard,
    )


def sleeping_stack(processes: Sequence[str] = STACK_PROCESSES) -> FakeSupervisor:
    """Alle Stack-Prozesse gestoppt — der schlafende Ausgangszustand."""
    return FakeSupervisor.sleeping(processes)


def running_stack(processes: Sequence[str] = STACK_PROCESSES) -> FakeSupervisor:
    """Alle Stack-Prozesse laufen — der wache Ausgangszustand."""
    return FakeSupervisor.running(processes)


class FakeProbe:
    """Bereitschaftsfrage mit steuerbarer Antwort.

    ``ready_after`` sagt, nach wie vielen Fragen der Stack als bereit gilt —
    so laesst sich ein Start simulieren, der Zeit braucht, ohne echte
    Wartezeit im Test.
    """

    def __init__(self, *, ready_after: int = 0) -> None:
        self.ready_after = ready_after
        self.calls = 0

    def ready(self) -> bool:
        self.calls += 1
        return self.calls > self.ready_after

    def close(self) -> None:
        pass


def probe(handler: Callable[[httpx.Request], httpx.Response]) -> ReadinessProbe:
    """Echte :class:`ReadinessProbe` auf einem Attrappen-Transport.

    Die Adresse ist der Bootstrap-Vorgabewert (``AOFF_API_HEALTH_URL``) —
    der Transport ist ohnehin eine Attrappe, aber so steht im Test dieselbe
    URL wie im Betrieb.
    """
    return ReadinessProbe(
        EnvSettings().api_health_url,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


class RecordingProxyTransport:
    """API-Dienst-Attrappe fuer den Proxy: merkt sich, was ankam.

    Liefert per Vorgabe eine Lookup-Antwort; ``responder`` ersetzt das durch
    eine eigene Antwort (z. B. das nackte HTTP 405 des Batch-Endpunkts).
    """

    def __init__(self, responder: Callable[[httpx.Request], httpx.Response] | None = None) -> None:
        self.responder = responder
        #: Alle empfangenen Anfragen, in Reihenfolge.
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        # Der Rumpf muss hier gelesen werden, solange die Anfrage lebt.
        request.read()
        self.requests.append(request)
        if self.responder is not None:
            return self.responder(request)
        return streamed(
            200,
            json.dumps({"status": "ok", "results": []}).encode(),
            {"content-type": "application/json", "access-control-allow-origin": "*"},
        )

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]

    def payload(self, index: int = -1) -> Any:
        """Der Rumpf einer empfangenen Anfrage als Text."""
        return self.requests[index].content.decode()
