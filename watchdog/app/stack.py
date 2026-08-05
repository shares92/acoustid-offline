"""Der Stack als Prozessgruppe: Namen, Startreihenfolge, Bereitschaft (M1b).

Die Uebersetzung von „drei Container mit ``depends_on``" nach „drei Prozesse
in einem Container". Was Compose frueher geschenkt hat, steht hier
ausdruecklich:

* **Welche Prozesse zum Stack gehoeren** und in welcher Reihenfolge sie
  starten (:data:`STACK_PROCESSES`) — die Namen sind die der
  ``supervisor/supervisord.conf``.
* **Wer beim Idle-Stopp stehen bleibt** (:data:`RESIDENT_PROCESSES`): der
  Suchindex bleibt resident (E12, bewusste Abweichung von v2 §1.2/§3). Sein
  Kaltstart liest den kompletten Index per ``MAP_POPULATE``; auf dem
  SSD-Cache haelt er kein Array wach, und ein mitgestoppter Index waere mit
  ``wake.hold_timeout_s`` (90 s) nicht vereinbar.
* **Wann ein Prozess „da" ist** (:class:`ReadinessGate`) — der eigentliche
  Grund fuer dieses Modul.

**Warum sequenziell mit Gates.** Compose hielt die Reihenfolge ueber
``depends_on: service_healthy``; supervisord kennt keine Abhaengigkeiten. Ein
Gruppenstart ohne Gates liefe in einen Fehler, der erst 30 s spaeter
sichtbar wuerde: der API-Dienst wartet beim Start ``timeout=30`` auf seinen
Datenbank-Pool (``api/app/service.py``) und stirbt danach — supervisord
wuerde ihn dreimal neu starten und dann in ``FATAL`` legen, waehrend die
Datenbank gerade ihre Recovery beendet. Also erst Postgres, dann Index, dann
API — und dazwischen wird gefragt, nicht geraten.

**Harte und weiche Gates.** Nur eines ist zwingend:

==========  =======  ==================================================
``db``      hart     ohne sie stirbt die API nach 30 s
``index``   weich    die API startet ohne ihn; er meldet sich nach dem
                     ``MAP_POPULATE`` von selbst
``api``     weich    die verbindliche Bereitschaftsfrage stellt danach
                     der :class:`~acoustid_watchdog.wake.WakeCoordinator`
                     mit der Frist der wartenden Anfrage
==========  =======  ==================================================

Ein weiches Gate, das ablaeuft, ist eine Logzeile und kein Abbruch: die
Weck-Logik hat eine eigene, laengere Frist, die dem **Vorgang** gehoert.

**Gates nur fuer selbst Gestartetes.** Was schon lief, wird nicht abgefragt.
Das ist nicht Sparsamkeit, sondern Notwendigkeit: der residente Index laeuft
seit dem Containerstart und kann mitten im ``MAP_POPULATE`` stecken —
darauf zu warten wuerde jeden Weckvorgang um Minuten verlaengern, obwohl
Postgres und API laengst bereit waeren.
"""

from __future__ import annotations

import logging
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import httpx

from acoustid_watchdog.control import GroupStatus, ProcessControlError
from acoustid_watchdog.process import ProcessState, SupervisorClient
from shared.env import EnvSettings

__all__ = [
    "DEFAULT_GATE_POLL_S",
    "RESIDENT_PROCESSES",
    "STACK_PROCESSES",
    "PostgresVersionDrift",
    "ReadinessGate",
    "ServiceGroupController",
    "check_postgres_version",
    "default_gates",
]

_LOG = logging.getLogger(__name__)

#: Prozesse des Stacks in **Startreihenfolge** — die Namen der
#: ``[program:*]``-Abschnitte in ``supervisor/supervisord.conf``. Der
#: Waechter selbst fehlt: er ist der Prozess, der diesen Code ausfuehrt.
#: Jobs fehlen ebenfalls (E10) — sie sind Subprozesse des Waechters.
#:
#: Gestoppt wird in umgekehrter Reihenfolge — erst der Leser, dann seine
#: Datenquellen.
STACK_PROCESSES: Final[tuple[str, ...]] = ("db", "index", "api")

#: Prozesse, die der Idle-Stopp **nicht** anfasst (E12). Sie gehoeren zum
#: Stack (``inspect`` erwartet sie laufend), werden aber nie gestoppt.
RESIDENT_PROCESSES: Final[frozenset[str]] = frozenset({"index"})

#: Abstand zweier Bereitschaftsfragen innerhalb eines Gates.
DEFAULT_GATE_POLL_S: Final = 0.5

#: Fristen der drei Gates. Die Datenbank bekommt die grosse: eine Recovery
#: nach einem harten Stopp kann Minuten dauern, und genau dann darf die API
#: nicht schon starten.
DEFAULT_GATE_TIMEOUTS: Final[dict[str, float]] = {"db": 180.0, "index": 60.0, "api": 30.0}

#: Leseschranke der HTTP-Gates. Antwortet ein Dienst nicht binnen weniger
#: Sekunden, ist er nicht bereit — gefragt wird gleich wieder.
_HTTP_TIMEOUT_S: Final = 5.0


@dataclass(frozen=True, slots=True)
class ReadinessGate:
    """„Ist dieser Prozess benutzbar?" — eine Frage, die man wiederholen darf.

    Absichtlich kein Protocol mit Klassenhierarchie: ein Gate ist eine
    Funktion ohne Argumente, die ``True``/``False`` liefert und **nie**
    wirft. Waehrend eines Starts ist „noch nicht" der Normalfall und keine
    Ausnahme wert (dieselbe Haltung wie
    :meth:`~acoustid_watchdog.wake.ReadinessProbe.ready`).
    """

    #: Prozessname aus :data:`STACK_PROCESSES`.
    name: str
    #: Die Frage selbst.
    check: Callable[[], bool]
    #: Wie lange gewartet wird, bevor das Gate aufgibt.
    timeout_s: float
    #: Ist das Gate zwingend? Nur die Datenbank ist es (Modul-Docstring).
    required: bool = False
    #: Klartext fuer Log und Fehlermeldung.
    description: str = ""

    def wait(self, *, poll_s: float = DEFAULT_GATE_POLL_S) -> bool:
        """Fragt, bis die Antwort ``True`` ist oder die Frist ablaeuft."""
        deadline = time.monotonic() + self.timeout_s
        while True:
            if self.check():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(poll_s, remaining))


class ServiceGroupController:
    """Startet und stoppt die Stack-Prozesse — mehr Wissen hat er nicht.

    Die supervisord-Fassung des
    :class:`~acoustid_watchdog.control.ProcessGroupController`; ein Test
    haelt fest, dass sie das Protokoll erfuellt.
    """

    def __init__(
        self,
        supervisor: SupervisorClient,
        *,
        processes: Sequence[str] = STACK_PROCESSES,
        resident: frozenset[str] = RESIDENT_PROCESSES,
        gates: Sequence[ReadinessGate] = (),
        version_guard: Callable[[], None] | None = None,
    ) -> None:
        """
        Args:
            supervisor: Steuerweg zu supervisord.
            processes: Prozessnamen in Startreihenfolge.
            resident: Prozesse, die der Idle-Stopp nicht anfasst (E12).
            gates: Bereitschaftsfragen; ohne Angabe wird nur gestartet und
                nicht gewartet (Tests, die die Gates selbst stellen).
            version_guard: Wird vor dem ersten Start gerufen und darf
                :class:`~acoustid_watchdog.control.ProcessControlError`
                werfen — der Versions-Drift-Guard (E14). Ohne Angabe wird
                nicht geprueft.
        """
        self.supervisor = supervisor
        self.processes = tuple(processes)
        self.resident = frozenset(resident)
        self._gates = {gate.name: gate for gate in gates}
        self._version_guard = version_guard

    def __repr__(self) -> str:
        return f"{type(self).__name__}(processes={self.processes!r})"

    # --- Der Vertrag --------------------------------------------------------

    def start(self) -> list[str]:
        """Startet die Stack-Prozesse **der Reihe nach**, mit Gate je Schritt.

        Returns:
            Namen der Prozesse, die **dieser** Aufruf gestartet hat — was
            schon lief, steht nicht darin (Idempotenz).

        Raises:
            ProcessControlError: supervisord antwortet nicht, ein Start ist
                gescheitert, ein zwingendes Gate ist abgelaufen oder der
                Versions-Drift-Guard hat abgelehnt.
        """
        if self._version_guard is not None:
            self._version_guard()

        started: list[str] = []
        for name in self.processes:
            if self.supervisor.start(name):
                started.append(name)
                self._gate(name)
            else:
                # Lief schon. Ein **zwingendes** Gate wird trotzdem gestellt:
                # „laeuft" heisst nicht „nimmt Verbindungen an". Die Datenbank
                # kann von Hand gestartet worden sein und noch in der Recovery
                # stecken, oder ein vorheriger Weckvorgang ist genau an ihrem
                # Gate abgelaufen und hat sie laufend zurueckgelassen —
                # ohne diese Zeile startete die API dagegen und stuerbe nach
                # 30 s (`api/app/service.py`), bis `startretries` verbraucht
                # sind.
                self._gate(name, only_required=True)
        return started

    def stop(self) -> list[str]:
        """Stoppt die Stack-Prozesse in umgekehrter Reihenfolge.

        Die residenten Prozesse (E12) bleiben stehen — sie sind der Grund,
        warum diese Methode nicht einfach „alles aus" heisst.

        Returns:
            Namen der Prozesse, die dieser Aufruf gestoppt hat.
        """
        stopped: list[str] = []
        for name in reversed(self.processes):
            if name in self.resident:
                _LOG.debug("Prozess bleibt resident", extra={"program": name})
                continue
            if self.supervisor.stop(name):
                stopped.append(name)
        return stopped

    def inspect(self) -> GroupStatus:
        """Erhebt den Zustand aller Stack-Prozesse in einem Aufruf.

        Liefert **beide** Enden getrennt: „laeuft alles" und „steht alles,
        was gestoppt werden kann". Dazwischen liegt der Teilzustand, und der
        darf nie als Schlaf durchgehen (R8) — der Aufrufer sieht ihn an
        :attr:`~acoustid_watchdog.control.GroupStatus.partial`.

        Ein Prozess, den es in supervisord gar nicht gibt, zaehlt als
        „laeuft nicht" **und** als abgestuerzt: das ist ein Image-Bug, und
        auch er soll nicht als Schlaf durchgehen.
        """
        infos = self.supervisor.states()
        running = True
        sleeping = True
        crashed: list[str] = []
        states: list[tuple[str, str]] = []
        for name in self.processes:
            info = infos.get(name)
            if info is None:
                running = False
                crashed.append(name)
                states.append((name, "MISSING"))
                continue
            states.append((name, info.statename))
            if not info.running:
                running = False
            if info.crashed:
                crashed.append(name)
            # Schlafen heisst: die stoppbaren Prozesse sind **gestoppt**.
            # `STARTING` ist es ausdruecklich nicht — dort laeuft gerade ein
            # Start (womoeglich der Autorestart nach einem Absturz, E15).
            if name not in self.resident and info.state is not ProcessState.STOPPED:
                sleeping = False
        return GroupStatus(
            running=running,
            sleeping=sleeping,
            crashed=tuple(crashed),
            states=tuple(states),
        )

    # --- Innenleben ---------------------------------------------------------

    def _gate(self, name: str, *, only_required: bool = False) -> None:
        """Wartet auf die Bereitschaft eines Prozesses.

        Args:
            name: Prozessname.
            only_required: Nur zwingende Gates stellen. Den Weg nimmt ein
                Prozess, der schon lief — beim residenten Index (E12) waere
                das weiche Gate dann falsch: er kann seit dem Containerstart
                im ``MAP_POPULATE`` stecken, und darauf zu warten verlaengerte
                jeden Weckvorgang um Minuten.
        """
        gate = self._gates.get(name)
        if gate is None:
            return
        if only_required and not gate.required:
            _LOG.debug("Weiches Gate uebersprungen, Prozess lief bereits", extra={"program": name})
            return
        started_at = time.monotonic()
        if gate.wait():
            _LOG.info(
                "Prozess bereit",
                extra={"program": name, "waited_s": round(time.monotonic() - started_at, 1)},
            )
            return
        detail = gate.description or name
        if gate.required:
            raise ProcessControlError(
                f"{name} war nach {gate.timeout_s:g} s nicht bereit ({detail})"
            )
        # Weiches Gate: der Start geht weiter. Die verbindliche Frist gehoert
        # dem Weckvorgang, nicht diesem Schritt.
        _LOG.warning(
            "Bereitschaft noch nicht erreicht, Start laeuft weiter",
            extra={"program": name, "waited_s": round(gate.timeout_s, 1), "gate": detail},
        )


# --- Die drei Gates ---------------------------------------------------------


def default_gates(
    settings: EnvSettings,
    *,
    timeouts: dict[str, float] | None = None,
    pg_isready: str | None = None,
) -> tuple[ReadinessGate, ...]:
    """Die Bereitschaftsfragen des Betriebs, aus den Bootstrap-Werten gebaut.

    Args:
        settings: Adressen und Zugaenge (``AOFF_*``).
        timeouts: Fristen je Prozess; ohne Angabe
            :data:`DEFAULT_GATE_TIMEOUTS`.
        pg_isready: Pfad des ``pg_isready``-Programms; ohne Angabe wird es
            im ``PATH`` gesucht.
    """
    limits = {**DEFAULT_GATE_TIMEOUTS, **(timeouts or {})}
    index_url = settings.index_url.rstrip("/")
    return (
        ReadinessGate(
            name="db",
            check=_postgres_ready(settings, pg_isready),
            timeout_s=limits["db"],
            required=True,
            description=f"pg_isready {settings.db_host}:{settings.db_port}",
        ),
        ReadinessGate(
            name="index",
            check=_http_ok(f"{index_url}/_health"),
            timeout_s=limits["index"],
            description=f"{index_url}/_health",
        ),
        ReadinessGate(
            name="api",
            check=_http_ok(settings.api_health_url),
            timeout_s=limits["api"],
            description=settings.api_health_url,
        ),
    )


def _postgres_ready(settings: EnvSettings, pg_isready: str | None) -> Callable[[], bool]:
    """Gate der Datenbank: ``pg_isready``.

    **Warum das Programm und keine psycopg-Verbindung.** Der Waechter haelt
    bewusst keinen Zugang zum Array (§8.2, :mod:`acoustid_watchdog.service`)
    — ``pg_isready`` beantwortet die Frage „nimmt der Server Verbindungen
    an?" ohne Passwort, ohne Treiber und ohne eine Verbindung, die jemand
    versehentlich fuer eine Abfrage benutzen koennte. Es liegt im selben
    Image wie der Server (Postgres-Client-Paket) und kostet Millisekunden.

    Exit-Codes: 0 = nimmt Verbindungen an, 1 = lehnt ab (startet noch),
    2 = keine Antwort, 3 = Aufruffehler. Nur 0 ist bereit.
    """
    program = pg_isready or "pg_isready"
    command = [
        program,
        "--host",
        settings.db_host,
        "--port",
        str(settings.db_port),
        "--username",
        settings.db_user,
        "--dbname",
        settings.db_name,
        "--timeout",
        "3",
    ]

    def check() -> bool:
        try:
            # Feste Argumentliste ohne Shell, Programmpfad aus dem Image —
            # keine Nutzereingabe kommt hier her.
            result = subprocess.run(command, capture_output=True, timeout=10, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            _LOG.debug("pg_isready nicht ausfuehrbar", extra={"error": str(exc)})
            return False
        return result.returncode == 0

    return check


def _http_ok(url: str) -> Callable[[], bool]:
    """Gate ueber HTTP: ``GET`` muss mit 200 antworten.

    Jeder andere Ausgang — Verbindung abgelehnt, Zeitueberschreitung, 404
    (Index noch nicht angelegt), 503 (laedt noch) — heisst „noch nicht
    bereit" und ist waehrend eines Starts der Normalfall.
    """

    def check() -> bool:
        try:
            response = httpx.get(url, timeout=_HTTP_TIMEOUT_S)
        except httpx.HTTPError as exc:
            _LOG.debug("Bereitschaftsfrage ohne Antwort", extra={"url": url, "error": str(exc)})
            return False
        return response.status_code == httpx.codes.OK

    return check


# --- Versions-Drift-Guard (E14) ---------------------------------------------


@dataclass(frozen=True, slots=True)
class PostgresVersionDrift:
    """Ein Datenbestand, den dieses Image nicht bedienen kann."""

    #: Major-Version, die das Image mitbringt.
    expected: int
    #: Gefundene Major-Versionen im Datenverzeichnis.
    found: tuple[int, ...]

    def __str__(self) -> str:
        found = ", ".join(str(major) for major in self.found)
        return (
            f"Datenbestand von PostgreSQL {found} gefunden, dieses Image bringt "
            f"{self.expected} mit — kein Start ohne Migration "
            f"(docs/migration-v1-v2.md)"
        )


def check_postgres_version(root: Path, expected: int) -> PostgresVersionDrift | None:
    """Prueft, ob unter ``root`` ein Bestand einer **anderen** Major liegt.

    Der Guard aus E14: genau eine Major-Version steckt im Image, und ein
    Bestand einer anderen darf nicht angefasst werden — ein Postgres 18
    startet auf einem 17er-Datenverzeichnis nicht, aber die Fehlermeldung
    im Prozesslog saehe niemand. Deshalb verweigert der Waechter den Start
    und sagt, warum (Log **und** Ereignis-Log; die Notification kommt mit
    M2.5).

    Erkannt wird ein Bestand an der Datei ``PG_VERSION`` — ein leeres oder
    frisch angelegtes Verzeichnis ist kein Drift.

    Args:
        root: Wurzel der Datenverzeichnisse (``/data/db``).
        expected: Major-Version des Images.

    Returns:
        ``None``, wenn alles passt; sonst der Befund.
    """
    try:
        entries = sorted(root.iterdir())
    except OSError:
        # Kein Datenverzeichnis (noch nicht gemountet, Rechte) ist kein
        # Drift: darueber beschwert sich Postgres selbst, deutlich genauer.
        return None
    found: list[int] = []
    for entry in entries:
        if not entry.is_dir() or entry.name == str(expected):
            continue
        if not (entry / "PG_VERSION").is_file():
            continue
        try:
            found.append(int(entry.name))
        except ValueError:
            # Ein Verzeichnis, das keine Major-Version benennt, aber ein
            # Cluster enthaelt — melden, ohne zu raten.
            _LOG.warning("Unerwartetes Cluster-Verzeichnis", extra={"path": str(entry)})
    if not found:
        return None
    return PostgresVersionDrift(expected=expected, found=tuple(sorted(found)))
