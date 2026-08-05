"""supervisord-Steuerung ueber XML-RPC auf dem Unix-Socket (E1, M1b).

Der Nachfolger von ``docker.py`` und der **einzige** Ort im Projekt, an dem
Prozesse gestartet und gestoppt werden (Invariante §8.1: „Der Waechter
weckt, sonst niemand"). Das Modul kennt weder die Prozessnamen des Projekts
noch die Weck-Logik — es kann fuenf Dinge, und zwar fuer beliebige
Programme:

======================================  ===================================
``getProcessInfo(name)``                Zustand eines Programms
``getAllProcessInfo()``                 Zustand aller Programme (ein Aufruf)
``startProcess(name, wait)``            starten
``stopProcess(name, wait)``             stoppen
``signalProcess(name, signal)``         Signal schicken
======================================  ===================================

**Warum supervisord und keine eigene Prozessverwaltung** (E1): es ist der
einzige Kandidat mit einer echten Steuerungs-API zur Laufzeit, und seine
Faults bilden die Idempotenz-Semantik von ``docker.py`` exakt ab:

=========================  ==========  ==========================================
Docker-Engine (v1)         supervisord  Bedeutung
=========================  ==========  ==========================================
``204`` (gestartet)        ``True``     dieser Aufruf hat gestartet
``304`` (lief schon)       ``ALREADY_STARTED``  lief schon -> :meth:`start` = ``False``
``204`` (gestoppt)         ``True``     dieser Aufruf hat gestoppt
``304`` (stand schon)      ``NOT_RUNNING``      stand schon -> :meth:`stop` = ``False``
``404`` (kein Container)   ``BAD_NAME``         Programm gibt es nicht = **Image-Bug**
=========================  ==========  ==========================================

**Warum keine Fremdbibliothek.** Die Gegenstelle ist XML-RPC ueber HTTP auf
einem Unix-Socket; beides kann die Standardbibliothek. Gebraucht werden ein
Transport, der statt TCP eine Socketdatei oeffnet, und die Uebersetzung von
``xmlrpc.client.Fault`` in die Fehlerbasis des Projekts. Das Ergebnis ist
wie bei ``docker.py`` ein Modul, das man in einer Sitzung ganz liest.

**Warum synchron.** Der Vertrag
(:class:`~acoustid_watchdog.control.ProcessGroupController`) ist synchron,
und die Aufrufer schieben ihn ohnehin in den Threadpool
(``run_in_threadpool`` in :mod:`acoustid_watchdog.wake` und
:mod:`acoustid_watchdog.lifecycle` — dieselbe Mechanik wie
``asyncio.to_thread``, nur eine Ebene hoeher). Eine async-Fassung haette
zwei Ereignisschleifen-Kontexte zu bedienen, ohne dass eine Zeile weniger
blockierte: ``stopProcess(wait=True)`` **darf** blockieren, bis der Prozess
steht — genau darauf wartet der Idle-Stopp.

**Leseschranke.** Sie muss ueber dem groessten ``stopwaitsecs`` der
supervisord-Konfiguration liegen (Postgres: 300 s): ``stopProcess`` kehrt
erst zurueck, wenn der Prozess gestoppt ist oder supervisord ihn nach Ablauf
der Frist mit ``SIGKILL`` beendet hat. Eine kuerzere Schranke wuerde uns
einen Fehler melden, waehrend der Stopp noch geordnet laeuft.
"""

from __future__ import annotations

import http.client
import logging
import socket
import xmlrpc.client
from contextlib import suppress
from dataclasses import dataclass
from enum import IntEnum
from types import TracebackType
from typing import Any, Final, Self

from acoustid_watchdog.control import ProcessControlError

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "RUNNING_STATES",
    "SUPERVISOR_SOCKET",
    "Fault",
    "ProcessInfo",
    "ProcessState",
    "SupervisorClient",
    "SupervisorError",
    "SupervisorUnavailableError",
    "UnknownProcessError",
]

_LOG = logging.getLogger(__name__)

#: Pfad des supervisord-Sockets im Container. Fest verdrahtet wie zuvor der
#: Docker-Socket (ARCHITECTURE §6 „Feste Werte"): er steht in
#: ``supervisor/supervisord.conf``, und eine eigene ``MMO_``-Variable haette
#: keinen zweiten moeglichen Wert (Muster aus DECISIONS 2026-08-01, Punkt 7).
#: Bewusst kurz — AF_UNIX-Namen sind auf ~104 Byte begrenzt, und ein Pfad
#: unter ``/config`` waere auf manchen Wirten schon zu lang.
SUPERVISOR_SOCKET: Final = "/run/supervisor.sock"

#: Leseschranke aller Aufrufe. Groesser als das groesste ``stopwaitsecs``
#: (Postgres 300 s) plus Luft fuer den ``SIGKILL``-Nachlauf.
DEFAULT_TIMEOUT_S: Final = 360.0

#: Die Adresse ist der Socket; der Hostname steht nur in der ``Host``-Zeile.
_BASE_URL: Final = "http://supervisor"


class ProcessState(IntEnum):
    """Prozesszustaende von supervisord (``supervisor.states.ProcessStates``).

    Die Zahlenwerte sind die des Originals — sie stehen so im ``state``-Feld
    von ``getProcessInfo``.

    ==========  =============================================================
    STOPPED     steht — **auf Anforderung** gestoppt oder nie gestartet
    STARTING    startet gerade (``startsecs`` laeuft noch)
    RUNNING     laeuft
    BACKOFF     Start gescheitert, Wiederholung laeuft (``startretries``)
    STOPPING    faehrt gerade herunter (``stopwaitsecs`` laeuft)
    EXITED      hat sich **selbst** beendet — erwartet oder nicht
    FATAL       gab auf: alle Startversuche verbraucht
    UNKNOWN     supervisord selbst ist durcheinander
    ==========  =============================================================

    Genau hier liegt der Unterschied zur Docker-Sicht und der Grund fuer die
    neue Zustandskante ``ready→error``: unter Docker war „laeuft nicht"
    eindeutig gutartig. Hier ist ``STOPPED`` gutartig (jemand wollte es so)
    und ``EXITED``/``FATAL``/``BACKOFF`` sind es nicht — der Prozess ist von
    selbst weg. Ein Absturz darf sich nicht als Schlaf maskieren
    (M0-Analyse §2.1, R8).
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
#: ``startProcess`` mit ``ALREADY_STARTED`` abbricht (``supervisor.states.
#: RUNNING_STATES``). ``BACKOFF`` gehoert dazu, obwohl kein Prozess lebt:
#: der Startversuch laeuft noch.
RUNNING_STATES: Final = frozenset(
    {ProcessState.STARTING, ProcessState.RUNNING, ProcessState.BACKOFF}
)

#: Zustaende, in denen ein Prozess **von selbst** weg ist. Der Unterschied
#: zu ``STOPPED`` ist die ganze Zustandskante ``ready→error``.
CRASHED_STATES: Final = frozenset(
    {ProcessState.BACKOFF, ProcessState.EXITED, ProcessState.FATAL, ProcessState.UNKNOWN}
)


class Fault(IntEnum):
    """Fehlercodes von supervisord (``supervisor.xmlrpc.Faults``).

    Nur die, die der Waechter je zu sehen bekommt. Die beiden ersten sind
    keine Fehler im eigentlichen Sinn, sondern die Art, wie XML-RPC
    Idempotenz ausdrueckt: „lief schon" / „stand schon".
    """

    #: Unbekanntes Programm. Im Betrieb ein Bug im Image (die Programme
    #: stehen in der supervisord-Konfiguration), nie ein Betriebsfehler.
    BAD_NAME = 10
    #: Das ``command=`` des Programms zeigt ins Leere bzw. ist nicht
    #: ausfuehrbar. Wie ``BAD_NAME`` ein Image-Bug — supervisord prueft das
    #: **vor** allem anderen, auch vor „laeuft schon".
    NO_FILE = 20
    NOT_EXECUTABLE = 21
    #: supervisord selbst konnte den Auftrag nicht ausfuehren.
    FAILED = 30
    #: Der Prozess ist waehrend des Startens gestorben (``wait=True``).
    ABNORMAL_TERMINATION = 40
    #: Der Start ist gescheitert — **der** Fehler eines Weckvorgangs.
    SPAWN_ERROR = 50
    ALREADY_STARTED = 60
    NOT_RUNNING = 70


#: Faults, die einen Fehler im **Image** bedeuten: das Programm gibt es
#: nicht oder sein Kommando ist unbrauchbar. Kein Betriebszustand, den ein
#: Weckversuch heilen koennte.
_IMAGE_FAULTS: Final = frozenset({Fault.BAD_NAME, Fault.NO_FILE, Fault.NOT_EXECUTABLE})


class SupervisorError(ProcessControlError):
    """supervisord konnte einen Auftrag nicht ausfuehren.

    Unterklasse der technikfreien Basis
    :class:`~acoustid_watchdog.control.ProcessControlError`: die Weck-Logik
    faengt nur die Basis, dieses Modul benennt den Grund genauer.
    """


class SupervisorUnavailableError(SupervisorError):
    """Der Socket antwortet nicht — supervisord steht oder ist unerreichbar.

    Bewusst eine eigene Klasse: das ist ein Fehler der Steuerung selbst,
    kein Fehler des angefragten Prozesses. Fuer den Waechter heisst er
    „Zustand unbekannt", nicht „Stack kaputt".
    """


class UnknownProcessError(SupervisorError):
    """Das Programm steht nicht in der supervisord-Konfiguration.

    Im Betrieb heisst das: das Image passt nicht zum Code. Die Namen sind
    fest (:mod:`acoustid_watchdog.stack`), also ist das ein Bau- und kein
    Betriebsfehler — deshalb eine eigene Klasse, die man im Log sofort
    erkennt.
    """


@dataclass(frozen=True, slots=True)
class ProcessInfo:
    """Was ``getProcessInfo`` ueber ein Programm sagt (der genutzte Ausschnitt)."""

    name: str
    state: ProcessState
    #: Zustandsname von supervisord (``RUNNING``, ``FATAL``, …) — auch fuer
    #: unbekannte Zahlenwerte gefuellt, damit das Log lesbar bleibt.
    statename: str
    #: 0, wenn kein Prozess lebt (``STOPPED``, ``BACKOFF``, ``EXITED``, …).
    pid: int = 0
    #: Grund eines gescheiterten Starts, sonst leer.
    spawnerr: str = ""

    @property
    def running(self) -> bool:
        """Laeuft der Prozess wirklich (nicht nur „startet gerade")?"""
        return self.state is ProcessState.RUNNING

    @property
    def crashed(self) -> bool:
        """Ist der Prozess **von selbst** weg?

        ``STOPPED`` ist ausdruecklich **nicht** dabei: das ist der
        Idle-Stopp, der Gutzustand dieses Projekts.
        """
        return self.state in CRASHED_STATES

    @classmethod
    def from_payload(cls, payload: Any) -> Self:
        """Baut die Momentaufnahme aus der XML-RPC-Antwort."""
        if not isinstance(payload, dict) or "name" not in payload:
            raise SupervisorError(f"unerwartete Antwort von supervisord: {payload!r}")
        raw = payload.get("state")
        try:
            state = ProcessState(int(raw))  # type: ignore[arg-type]
        except TypeError, ValueError:
            # Ein unbekannter Zahlenwert ist kein Grund, den Waechter
            # anzuhalten — er bedeutet „ich verstehe diesen Zustand nicht",
            # und das ist genau UNKNOWN.
            _LOG.warning(
                "Unbekannter supervisord-Zustand",
                extra={"program": payload.get("name"), "state": raw},
            )
            state = ProcessState.UNKNOWN
        return cls(
            name=str(payload["name"]),
            state=state,
            statename=str(payload.get("statename", state.name)),
            pid=int(payload.get("pid") or 0),
            spawnerr=str(payload.get("spawnerr") or ""),
        )


class SupervisorClient:
    """Fuenf Operationen auf der XML-RPC-Schnittstelle, mehr nicht."""

    def __init__(
        self,
        socket_path: str = SUPERVISOR_SOCKET,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        proxy: Any | None = None,
    ) -> None:
        """
        Args:
            socket_path: Unix-Socket von supervisord (``[unix_http_server]``).
            timeout_s: Leseschranke aller Aufrufe; muss ueber dem groessten
                ``stopwaitsecs`` liegen (Modul-Docstring).
            proxy: Fertige Gegenstelle (Tests). Muss den Namensraum
                ``supervisor`` mit den Original-Methodennamen anbieten —
                genau wie ``xmlrpc.client.ServerProxy``.
        """
        self.socket_path = socket_path
        self._timeout_s = timeout_s
        self._owns_proxy = proxy is None
        self._proxy = proxy if proxy is not None else _server_proxy(socket_path, timeout_s)

    # --- Lebenszyklus -------------------------------------------------------

    def close(self) -> None:
        """Schliesst die Verbindung, falls dieser Client sie angelegt hat."""
        if not self._owns_proxy:
            return
        # ``ServerProxy("close")`` liefert die Schliessfunktion des
        # Transports. Ein Fehler dabei ist nie interessant: wir geben etwas
        # auf, das ohnehin weg soll.
        with suppress(OSError):
            self._proxy("close")()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(socket_path={self.socket_path!r})"

    # --- Operationen --------------------------------------------------------

    def inspect(self, name: str) -> ProcessInfo:
        """Zustand eines Programms.

        Raises:
            UnknownProcessError: Das Programm ist nicht konfiguriert.
            SupervisorUnavailableError: Der Socket antwortet nicht.
        """
        payload = self._call("getProcessInfo", name)
        return ProcessInfo.from_payload(payload)

    def states(self) -> dict[str, ProcessInfo]:
        """Zustand **aller** Programme in einem Aufruf.

        Der Weg des Zustands-Pollers
        (:class:`~acoustid_watchdog.lifecycle.StatePoller`): supervisord
        beantwortet die Frage einmal fuer alles, statt n-mal einzeln. Ein
        Eventlistener waere die Push-Variante — er ist aber ein eigener, von
        supervisord gespawnter Bruecken-Prozess und gehoert nicht in M1b
        (M0-Analyse §2.1).
        """
        payload = self._call("getAllProcessInfo")
        if not isinstance(payload, list):
            raise SupervisorError(f"unerwartete Antwort auf getAllProcessInfo: {payload!r}")
        infos = [ProcessInfo.from_payload(entry) for entry in payload]
        return {info.name: info for info in infos}

    def start(self, name: str) -> bool:
        """Startet ein Programm und wartet auf ``startsecs``.

        Returns:
            ``True``, wenn dieser Aufruf es gestartet hat; ``False``, wenn es
            schon lief (``ALREADY_STARTED``). Der Aufruf ist damit
            idempotent — genau das braucht ein Weckvorgang, den mehrere
            Anfragen ausloesen koennen (dieselbe Semantik wie HTTP 304 der
            Docker-Engine-API).

        ``wait=True`` mit Absicht: erst danach hat supervisord den Prozess
        ``startsecs`` lang beobachtet. Ohne das Warten kaeme ein Programm,
        das sofort wieder stirbt, als „gestartet" zurueck, und der
        Weckvorgang liefe in seinen Timeout statt in eine Fehlermeldung.

        Raises:
            UnknownProcessError: Das Programm ist nicht konfiguriert (oder
                sein ``command=`` zeigt ins Leere) — ein Image-Bug.
            SupervisorError: Der Start ist gescheitert (``SPAWN_ERROR``,
                ``ABNORMAL_TERMINATION``).
            SupervisorUnavailableError: Der Socket antwortet nicht.
        """
        try:
            self._call("startProcess", name, True)
        except _IdempotentFaultError as benign:
            if benign.code is not Fault.ALREADY_STARTED:
                raise benign.as_error() from benign.fault
            _LOG.info("Prozess lief bereits", extra={"program": name})
            return False
        _LOG.info("Prozess gestartet", extra={"program": name})
        return True

    def stop(self, name: str) -> bool:
        """Stoppt ein Programm und wartet, bis es steht.

        Returns:
            ``True``, wenn dieser Aufruf es gestoppt hat; ``False``, wenn es
            schon stand (``NOT_RUNNING``).

        Der Aufruf blockiert bis zu ``stopwaitsecs`` des Programms — bei
        Postgres also bis der Fast Shutdown seinen Checkpoint geschrieben
        hat. Das ist gewollt: der Idle-Stopp darf nicht melden „schlaeft",
        waehrend die Datenbank noch schreibt.

        Raises:
            UnknownProcessError: Das Programm ist nicht konfiguriert.
            SupervisorError: supervisord konnte nicht stoppen.
            SupervisorUnavailableError: Der Socket antwortet nicht.
        """
        try:
            self._call("stopProcess", name, True)
        except _IdempotentFaultError as benign:
            if benign.code is not Fault.NOT_RUNNING:
                raise benign.as_error() from benign.fault
            _LOG.info("Prozess stand bereits", extra={"program": name})
            return False
        _LOG.info("Prozess gestoppt", extra={"program": name})
        return True

    def signal(self, name: str, signal: str) -> bool:
        """Schickt einem laufenden Programm ein Signal.

        Der Weg, auf dem ein Job in M2.5 sein ``SIGTERM`` bekommt, und die
        einzige Operation, die den Zustand **nicht** aendert — was das Signal
        bewirkt, entscheidet der Prozess.

        Args:
            name: Programmname aus der supervisord-Konfiguration.
            signal: Name (``TERM``, ``HUP``, ``KILL``) oder Nummer als Text.

        Returns:
            ``True``, wenn das Signal zugestellt wurde; ``False``, wenn das
            Programm nicht lief (``NOT_RUNNING``) — ein gestoppter Prozess
            ist kein Fehler, sondern schon das Ziel.
        """
        try:
            self._call("signalProcess", name, signal)
        except _IdempotentFaultError as benign:
            if benign.code is not Fault.NOT_RUNNING:
                raise benign.as_error() from benign.fault
            return False
        return True

    # --- Transport ----------------------------------------------------------

    def _call(self, method: str, *args: Any) -> Any:
        """Einziger IO-Punkt des Moduls.

        Uebersetzt die drei Fehlerwelten von XML-RPC in die des Projekts:
        Transportfehler -> :class:`SupervisorUnavailableError`, Faults ueber
        Programme -> :class:`UnknownProcessError` bzw.
        :class:`_IdempotentFaultError` (den die Aufrufer als ``False`` deuten),
        alles andere -> :class:`SupervisorError`.
        """
        try:
            return getattr(self._proxy.supervisor, method)(*args)
        except xmlrpc.client.Fault as fault:
            raise _translate(fault, method) from fault
        except (OSError, xmlrpc.client.ProtocolError, xmlrpc.client.ResponseError) as exc:
            raise SupervisorUnavailableError(
                f"{method}: keine Antwort von supervisord ueber {self.socket_path} ({exc})"
            ) from exc


class _IdempotentFaultError(SupervisorError):
    """Ein Fault, der gutartig sein *kann* — der Aufrufer entscheidet.

    ``ALREADY_STARTED`` ist fuer :meth:`SupervisorClient.start` gutartig und
    fuer :meth:`SupervisorClient.stop` bedeutungslos; ``NOT_RUNNING`` genau
    umgekehrt. Deshalb wird hier nur transportiert, nicht entschieden.

    Unterklasse von :class:`SupervisorError` mit Absicht: entkommt so ein
    Fault einem Aufrufer, der ihn nicht erwartet hat (``inspect``,
    ``states``), ist er trotzdem ein
    :class:`~acoustid_watchdog.control.ProcessControlError` und wird von der
    Weck-Logik gefangen — statt als fremde Ausnahme durchzuschlagen.
    """

    def __init__(self, code: Fault, fault: xmlrpc.client.Fault) -> None:
        super().__init__(fault.faultString)
        self.code = code
        self.fault = fault

    def as_error(self) -> SupervisorError:
        """Derselbe Fault als echter Fehler (falscher Aufrufer)."""
        return SupervisorError(f"{self.code.name}: {self.fault.faultString}")


def _translate(fault: xmlrpc.client.Fault, method: str) -> Exception:
    """``xmlrpc.client.Fault`` -> Fehlerklasse des Projekts."""
    try:
        code = Fault(fault.faultCode)
    except ValueError:
        return SupervisorError(f"{method}: supervisord meldet Fault {fault.faultCode} ({fault})")
    if code in _IMAGE_FAULTS:
        return UnknownProcessError(f"{method}: {code.name} — {fault.faultString}")
    if code in (Fault.ALREADY_STARTED, Fault.NOT_RUNNING):
        return _IdempotentFaultError(code, fault)
    return SupervisorError(f"{method}: {code.name} — {fault.faultString}")


# --- XML-RPC ueber einen Unix-Socket ----------------------------------------


class _UnixConnection(http.client.HTTPConnection):
    """HTTP-Verbindung, die statt TCP eine Socketdatei oeffnet."""

    def __init__(self, socket_path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self.socket_path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


class _UnixTransport(xmlrpc.client.Transport):
    """XML-RPC-Transport auf :class:`_UnixConnection`."""

    def __init__(self, socket_path: str, timeout: float) -> None:
        super().__init__()
        self.socket_path = socket_path
        self.timeout = timeout

    def make_connection(self, host: Any) -> http.client.HTTPConnection:
        # Kein Verbindungs-Cache: die Aufrufe kommen selten (Weckvorgang,
        # Poller alle 15 s), und eine offene Verbindung ueber Minuten waere
        # nur eine weitere Sache, die kaputtgehen kann.
        return _UnixConnection(self.socket_path, self.timeout)


def _server_proxy(socket_path: str, timeout_s: float) -> xmlrpc.client.ServerProxy:
    """Die echte Gegenstelle — ein ``ServerProxy`` auf dem Unix-Socket."""
    return xmlrpc.client.ServerProxy(_BASE_URL, transport=_UnixTransport(socket_path, timeout_s))
