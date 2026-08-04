"""Attrappen der Waechter-Tests (Phase 15).

Der Waechter spricht ab Phase 15 mit zwei Gegenstellen: dem Docker-Daemon
(Unix-Socket) und dem API-Dienst (HTTP). Beide werden hier durch
``httpx.MockTransport`` ersetzt — bewusst **nicht** durch Attrappen der
Client-Klassen: so laufen Pfadbildung, Statusauswertung und
Kopfzeilen-Behandlung der echten Module mit durch den Test.

Seit M1a steht daneben :class:`FakeSupervisor`, die Gegenstelle des
Ein-Container-Umbaus (HANDOFF v2 §5, DECISIONS 2026-08-04 E1). Sie ersetzt
:class:`FakeDaemon` **nicht** — solange der Waechter Container steuert,
werden beide gebraucht: der Daemon fuer den laufenden Betrieb, der
Supervisor fuer den Adapter, der in M1b danebentritt.

Bewusst ein eigenes Modul und nicht die conftest.py: pytest laedt alle
`conftest`-Module unter demselben Namen, ein ``from conftest import …``
wuerde je nach Sammelreihenfolge im falschen Paket landen (gleiche
Begruendung wie in ``api/tests/stubs.py``).
"""

from __future__ import annotations

import json
import time
import xmlrpc.client
from collections.abc import Callable, Iterable, Mapping
from enum import IntEnum
from typing import Any, Final, Self

import httpx

from acoustid_watchdog.docker import DockerClient
from acoustid_watchdog.wake import ReadinessProbe
from shared.env import EnvSettings

__all__ = [
    "RUNNING_STATES",
    "STOPPED_STATES",
    "FakeDaemon",
    "FakeProbe",
    "FakeSupervisor",
    "Fault",
    "ProcessState",
    "RecordingProxyTransport",
    "docker_client",
    "probe",
    "streamed",
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


class FakeDaemon:
    """Ein Docker-Daemon aus einem Wörterbuch von Containerzustaenden.

    Versteht genau die drei Routen, die :mod:`acoustid_watchdog.docker`
    benutzt, und merkt sich jeden Aufruf — daran laesst sich pruefen, dass
    ein Weckvorgang wirklich nur einmal startet.
    """

    def __init__(self, containers: dict[str, bool] | None = None) -> None:
        #: Containername -> laeuft gerade. Fehlt ein Name, antwortet der
        #: Daemon mit 404 (wie fuer einen nie angelegten Container).
        self.containers = dict(containers or {})
        #: ``(methode, name)`` in Aufrufreihenfolge.
        self.calls: list[tuple[str, str]] = []
        #: Aufrufe, die mit HTTP 500 beantwortet werden sollen.
        self.fail_on: set[str] = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        parts = request.url.path.strip("/").split("/")
        if len(parts) != 3 or parts[0] != "containers":
            return httpx.Response(404, json={"message": f"unbekannt: {request.url.path}"})
        _, name, action = parts
        self.calls.append((action, name))

        if name in self.fail_on:
            return httpx.Response(500, json={"message": f"{name}: Daemon-Fehler"})
        if name not in self.containers:
            return httpx.Response(404, json={"message": f"No such container: {name}"})

        if action == "json":
            running = self.containers[name]
            return httpx.Response(
                200,
                json={
                    "Name": f"/{name}",
                    "State": {
                        "Status": "running" if running else "exited",
                        "Running": running,
                        "Health": {"Status": "healthy" if running else "unhealthy"},
                    },
                },
            )
        if action in ("start", "stop"):
            wanted = action == "start"
            if self.containers[name] == wanted:
                return httpx.Response(304)
            self.containers[name] = wanted
            return httpx.Response(204)
        return httpx.Response(404, json={"message": f"unbekannte Aktion: {action}"})

    # --- Auswertung ---------------------------------------------------------

    def count(self, action: str, name: str | None = None) -> int:
        """Wie oft wurde ``action`` (optional fuer ``name``) aufgerufen?"""
        return sum(
            1
            for call_action, call_name in self.calls
            if call_action == action and (name is None or call_name == name)
        )

    @property
    def all_running(self) -> bool:
        return all(self.containers.values())


def docker_client(daemon: FakeDaemon) -> DockerClient:
    """Echter :class:`DockerClient` auf einem Attrappen-Daemon."""
    return DockerClient(client=httpx.Client(transport=httpx.MockTransport(daemon)))


class ProcessState(IntEnum):
    """Prozesszustaende von supervisord (``supervisor.states.ProcessStates``).

    Die Zahlenwerte sind die des Originals: sie stehen so im
    ``state``-Feld von ``getProcessInfo``, und der echte Client wird sie
    genauso lesen. Der ganze Lebenslauf eines Prozesses:

    ==========  =============================================================
    STOPPED     steht — entweder nie gestartet oder geordnet gestoppt
    STARTING    startet gerade (``startsecs`` laeuft noch)
    RUNNING     laeuft
    BACKOFF     Start gescheitert, Wiederholung laeuft (``startretries``)
    STOPPING    faehrt gerade herunter (``stopwaitsecs`` laeuft)
    EXITED      hat sich selbst beendet — erwartet oder nicht
    FATAL       gab auf: alle Startversuche verbraucht
    UNKNOWN     supervisord selbst ist durcheinander
    ==========  =============================================================

    Der Unterschied zur Docker-Sicht ist genau der Grund fuer diese
    Attrappe: ``FakeDaemon`` kennt „laeuft ja/nein", hier ist „steht" nicht
    mehr eindeutig gutartig — ``STOPPED`` heisst gestoppt, ``EXITED`` und
    ``FATAL`` heissen abgestuerzt (M0-Analyse §2.1, Kante ``ready→error``).
    """

    STOPPED = 0
    STARTING = 10
    RUNNING = 20
    BACKOFF = 30
    STOPPING = 40
    EXITED = 100
    FATAL = 200
    UNKNOWN = 1000


#: Zustaende, die supervisord als „laeuft" zaehlt — die Bedingung, an der
#: ``startProcess`` mit ``ALREADY_STARTED`` abbricht.
RUNNING_STATES: Final = frozenset(
    {ProcessState.STARTING, ProcessState.RUNNING, ProcessState.BACKOFF}
)

#: Zustaende, in denen ein Prozess endgueltig steht. ``STOPPING`` fehlt hier
#: mit Absicht — es ist weder das eine noch das andere, und ``stopProcess``
#: beantwortet es (wie das Original) trotzdem mit ``NOT_RUNNING``.
STOPPED_STATES: Final = frozenset(
    {ProcessState.STOPPED, ProcessState.EXITED, ProcessState.FATAL, ProcessState.UNKNOWN}
)


class Fault(IntEnum):
    """Fehlercodes von supervisord (``supervisor.xmlrpc.Faults``).

    Nur die, die der Waechter je zu sehen bekommt. Die beiden ersten sind
    keine Fehler im eigentlichen Sinn, sondern die Art, wie XML-RPC
    Idempotenz ausdrueckt: „lief schon" / „stand schon" — das Gegenstueck
    zu HTTP 304 der Docker-Engine-API.
    """

    ALREADY_STARTED = 60
    NOT_RUNNING = 70
    #: Unbekanntes Programm. Im Betrieb ein Bug im Image (die Programme
    #: stehen in der supervisord-Konfiguration), nie ein Betriebsfehler.
    BAD_NAME = 10
    #: Der Start ist gescheitert — **der** Fehler eines Weckvorgangs.
    #: supervisord wirft ihn aus ``startProcess``, wenn das Spawnen
    #: misslingt oder der Prozess die Startphase nicht ueberlebt; nicht
    #: ``FAILED``.
    SPAWN_ERROR = 50
    #: Der Auftrag scheiterte an supervisord selbst (alles ausser dem
    #: Start).
    FAILED = 30


class FakeSupervisor:
    """Ein supervisord aus einem Woerterbuch von Prozesszustaenden.

    Das Gegenstueck zu :class:`FakeDaemon` fuer den Ein-Container-Umbau:
    gleicher Zuschnitt (Zustands-Woerterbuch, :attr:`calls`, :attr:`fail_on`,
    :attr:`all_running`), aber die Sprache von supervisord statt der der
    Docker-Engine-API.

    Sie versteht die Methoden, die der kommende ``SupervisorClient``
    braucht (M1b), und zwar unter ihren echten Namen::

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
