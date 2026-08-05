"""Die beiden Dauerlaeufer des Waechters (Phase 16).

Wach werden kann der Stack von selbst — schlafen gehen nicht. Dafuer und
fuer die Frage „stimmt meine Anzeige ueberhaupt noch?" laufen im Waechter
zwei Hintergrundaufgaben, beide im Lifespan der Anwendung gestartet und
beendet (:mod:`acoustid_watchdog.main`):

======================  ===================================================
:class:`IdleStopper`    legt den Stack nach ``idle.timeout_min`` ohne
                        Nutzung schlafen — aber nur im Ruhezustand
                        (Invariante §8.5)
:class:`StatePoller`    gleicht den gefuehrten Zustand mit der
                        Prozess-Steuerung ab, damit ein von Hand
                        gestoppter oder gestarteter Stack auffaellt
                        (Phase-15-Luecke) — und seit M1b, damit ein
                        **Absturz** auffaellt (Kante ``ready→error``)
======================  ===================================================

**Was „Leerlauf" heisst** (ARCHITECTURE §6, „Feste Werte"): *keine
API-Anfrage im Timeout-Fenster UND kein laufender Import-/Backup-Job.*
Beide Haelften stehen hier als eigene, kleine Bausteine:

* :class:`ActivityTracker` — wann zuletzt eine Anfrage durch den Proxy
  lief. Nur ``/v2/*`` zaehlt; `/status` und die Admin-UI beruehren das
  Array nie und duerfen es folglich auch nicht wachhalten (Invariante
  §8.2).
* :class:`JobSource` — „laeuft gerade ein Job?". Heute beantwortet
  :class:`DatabaseJobs` die Frage aus der Lauf-Historie ``update_run``:
  ein Lauf ohne Ergebnis laeuft noch (:func:`acoustid_watchdog.runs.
  running_runs`). Genau dort melden sich ab Phase 19 (Update-Zyklus) und
  Phase 21 (Backup) an — sie legen den Lauf ohnehin mit
  :func:`~acoustid_watchdog.runs.start_run` an, bevor sie arbeiten. Der
  Idle-Stopp braucht dafuer keine Zeile Aenderung.

**Ein laufender Job schiebt den Leerlauf auf.** Er sperrt den Stopp nicht
nur fuer den Moment, er setzt die Leerlaufuhr zurueck: sonst faellt der
Stack in dem Augenblick schlafen, in dem ein langer Import fertig wird —
die ganze Timeout-Frist waere ja waehrend des Laufs verstrichen. Nach dem
Job hat der Betreiber (und die Admin-UI) wieder das volle Fenster.

**Warum zwei Aufgaben und nicht eine.** Sie haben verschiedene Takte und
verschiedene Kosten: der Zustandsabgleich fragt die Prozess-Steuerung
(billig, aber nach aussen) und soll zeitnah greifen, der Idle-Stopp rechnet
nur mit einer Uhr und darf gemuetlich sein. Zusammengelegt muesste einer
der beiden im falschen Takt laufen.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from typing import Final, Protocol

from starlette.concurrency import run_in_threadpool

from acoustid_watchdog.runs import running_runs
from acoustid_watchdog.state import StackStateTracker
from acoustid_watchdog.store import Database
from acoustid_watchdog.wake import WakeCoordinator
from shared.config import Config, IdleConfig
from shared.models import StackState

__all__ = [
    "DEFAULT_IDLE_CHECK_INTERVAL_S",
    "DEFAULT_STATE_POLL_INTERVAL_S",
    "ActivityTracker",
    "DatabaseJobs",
    "IdleStopper",
    "JobSource",
    "StatePoller",
]

_LOG = logging.getLogger(__name__)

#: Abstand zweier Zustandsabgleiche mit der Prozess-Steuerung. Ein Abgleich
#: kostet **einen** ``getAllProcessInfo``-Aufruf auf dem lokalen Socket (und
#: einen Healthcheck, wenn alles laeuft) — Millisekunden; unter Docker waren
#: es noch drei. 15 Sekunden sind der Kompromiss aus „ein
#: von Hand gestoppter Stack faellt schnell auf" (die Admin-UI pollt
#: `/status` alle 5 s, §6) und „der Waechter ist leise": im Normalbetrieb
#: sind das vier Aufrufe je Minute auf einen Unix-Socket, ohne eine Zeile
#: Log. Bewusst **kein** §6-Schluessel: der Betreiber hat keinen Grund,
#: daran zu drehen (Muster aus DECISIONS 2026-08-01, Punkt 2).
DEFAULT_STATE_POLL_INTERVAL_S: Final = 15.0

#: Abstand zweier Faelligkeitspruefungen des Idle-Stopps. Die kleinste
#: sinnvolle Frist ist eine Minute (``idle.timeout_min``), die Vorgabe 15;
#: eine halbe Minute Aufloesung genuegt also und kostet nichts (eine
#: SQLite-Abfrage, sonst nur eine Subtraktion).
DEFAULT_IDLE_CHECK_INTERVAL_S: Final = 30.0


class ActivityTracker:
    """Wann zuletzt eine ``/v2/``-Anfrage lief — die Uhr des Idle-Stopps.

    **Zwei Dinge, bewusst getrennt** (F8): die *Uhr* („wann war zuletzt
    etwas los?") und der *Zaehler* („wie viele **echte** Anfragen gab es?").
    Beide werden zurueckgesetzt bzw. erhoeht, wenn eine Anfrage durch den
    Proxy laeuft — aber der Idle-Stopp schiebt die Uhr auch dann, wenn nur
    ein **Job** laeuft (:meth:`defer`), und das ist keine Nutzung.

    Die Vermischung war ein echter Fehler: der Idle-Stopp rief bei jedem
    offenen Lauf ``touch()``, und der Job-Zyklus verglich anschliessend
    denselben Zaehler, um zu entscheiden, ob er schlafen legen darf. Jeder
    Job, der laenger als ein Pruefintervall lief, sah damit „es kamen
    Anfragen" — und der Stack schlief nach einem Import praktisch **nie**
    wieder ein (die Definition of Done verlangt genau das Gegenteil).
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        """
        Args:
            clock: Zeitquelle; ``time.monotonic``, weil eine Zeitumstellung
                oder ein NTP-Sprung den Stack nicht schlafen legen darf.
                Tests geben eine eigene Uhr mit.
        """
        self._clock = clock
        self._lock = threading.Lock()
        # Ein frisch gestarteter Waechter gilt als „gerade benutzt": sonst
        # waere die erste Pruefung nach einem Neustart sofort faellig.
        self._last = clock()
        self._requests = 0
        self._defers = 0

    def touch(self) -> None:
        """Merkt eine **echte** Anfrage vor (Aufrufer: der Proxy-Pfad).

        Setzt die Uhr **und** erhoeht den Zaehler.
        """
        with self._lock:
            self._last = self._clock()
            self._requests += 1

    def defer(self) -> None:
        """Schiebt nur die Uhr — ein laufender Job ist keine Anfrage.

        Der Weg des Idle-Stopps: ein Job schiebt den Leerlauf auf, damit
        der Stack nicht in dem Augenblick einschlaeft, in dem ein langer
        Import fertig wird. Er darf dabei aber nicht wie eine Nutzung
        aussehen — sonst legte der Zyklus den Stack anschliessend nie
        wieder schlafen (Klassen-Docstring).
        """
        with self._lock:
            self._last = self._clock()
            self._defers += 1

    @property
    def idle_s(self) -> float:
        """Sekunden seit der letzten Anfrage (oder dem letzten Job-Aufschub)."""
        with self._lock:
            return max(self._clock() - self._last, 0.0)

    @property
    def requests(self) -> int:
        """Wie viele **echte** Anfragen dieser Prozess gesehen hat.

        Die Zahl, an der der Job-Zyklus ablesen kann, ob die Instanz
        waehrend seines Laufs benutzt wurde — Job-Aufschuebe zaehlen
        ausdruecklich nicht mit.
        """
        with self._lock:
            return self._requests

    @property
    def defers(self) -> int:
        """Wie oft ein laufender Job den Leerlauf aufgeschoben hat (Diagnose)."""
        with self._lock:
            return self._defers


class JobSource(Protocol):
    """„Laeuft gerade ein Job?" — die Sperre aus Invariante §8.5.

    Absichtlich winzig und nicht an SQLite gebunden: Phase 19 (Update) und
    Phase 21 (Backup) melden ihre Laeufe ueber die Lauf-Historie an, ein
    Test ueber eine Attrappe.
    """

    def running_jobs(self) -> list[str]:
        """Beschreibungen der laufenden Jobs; leere Liste = nichts laeuft."""
        ...


class DatabaseJobs:
    """Laufende Jobs aus der Lauf-Historie ``update_run``."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def running_jobs(self) -> list[str]:
        """Jeder Lauf ohne Ergebnis laeuft noch (:mod:`acoustid_watchdog.runs`)."""
        return [f"{run.kind.display_name} #{run.id}" for run in running_runs(self.db)]


class IdleStopper:
    """Legt den Stack schlafen, wenn niemand ihn braucht (§8.5)."""

    def __init__(
        self,
        coordinator: WakeCoordinator,
        state: StackStateTracker,
        activity: ActivityTracker,
        jobs: JobSource,
        config: Callable[[], Config],
        *,
        interval_s: float = DEFAULT_IDLE_CHECK_INTERVAL_S,
    ) -> None:
        """
        Args:
            coordinator: Weck- und Stopp-Koordination.
            state: Zustandsanzeige — gestoppt wird nur aus ``bereit``.
            activity: Uhr der letzten Anfrage.
            jobs: Auskunft ueber laufende Import-/Backup-Jobs.
            config: Zugriff auf die **aktuelle** Laufzeit-Konfiguration
                (``idle.timeout_min`` wird bei jeder Pruefung frisch
                gelesen, damit eine Aenderung in der Admin-UI ohne Neustart
                greift — wie ``wake.hold_timeout_s`` im Proxy).
            interval_s: Abstand zweier Faelligkeitspruefungen.
        """
        self._coordinator = coordinator
        self._state = state
        self._activity = activity
        self._jobs = jobs
        self._config = config
        self.interval_s = interval_s
        #: Wie oft ein laufender Job den Stopp aufgeschoben hat (Tests,
        #: spaeter die Kennzahlen der Phase 22).
        self.blocked_by_jobs = 0

    # --- Einzelschritt ------------------------------------------------------

    @property
    def timeout_s(self) -> float:
        """``idle.timeout_min`` in Sekunden, aus der laufenden Konfiguration.

        Eine unlesbare ``config.yaml`` darf den Betrieb nicht anhalten; dann
        gilt der Vorgabewert aus §6 — dieselbe Haltung wie im Proxy.
        """
        try:
            timeout_min = self._config().idle.timeout_min
        except Exception:
            _LOG.exception("Laufzeit-Konfiguration nicht lesbar, Vorgabewert wird benutzt")
            timeout_min = IdleConfig().timeout_min
        return timeout_min * 60.0

    async def check(self) -> bool:
        """Eine Faelligkeitspruefung.

        Returns:
            ``True``, wenn diese Runde den Stack schlafen gelegt hat.
        """
        if self._state.state is not StackState.READY:
            # Was nicht bereit ist, wird nicht gestoppt: schlafend ist schon
            # das Ziel, startet/stoppt gehoert einem laufenden Vorgang, und
            # ein Stack im Fehlerzustand laeuft nicht verlaesslich.
            return False
        if self._coordinator.busy:
            return False

        jobs = await run_in_threadpool(self._jobs.running_jobs)
        if jobs:
            # Ein laufender Job schiebt den Leerlauf auf — die Uhr beginnt
            # nach ihm von vorn (Modul-Docstring). **`defer` und nicht
            # `touch`**: er ist keine Anfrage, und der Job-Zyklus liest den
            # Anfragezaehler, um zu entscheiden, ob er schlafen legen darf
            # (F8, `ActivityTracker`-Docstring).
            self._activity.defer()
            self.blocked_by_jobs += 1
            _LOG.debug("Idle-Stopp aufgeschoben, Job laeuft", extra={"jobs": jobs})
            return False

        idle_s = self._activity.idle_s
        timeout_s = self.timeout_s
        if idle_s < timeout_s:
            return False

        _LOG.info(
            "Leerlauf erreicht, Stack wird schlafen gelegt",
            extra={"idle_s": round(idle_s), "timeout_s": round(timeout_s)},
        )
        return await self._coordinator.stop_stack(reason="idle")

    # --- Schleife -----------------------------------------------------------

    async def run(self) -> None:
        """Prueft bis zum Abbruch periodisch auf Leerlauf.

        Laeuft als Hintergrundaufgabe im Lifespan; der Abbruch beim
        Herunterfahren ist ``CancelledError`` und beendet sie sofort.
        """
        while True:
            await asyncio.sleep(self.interval_s)
            try:
                await self.check()
            except Exception:
                # Eine Hintergrundschleife darf an nichts sterben — sonst
                # bliebe der Stack fuer immer wach.
                _LOG.exception("Idle-Pruefung fehlgeschlagen")


class StatePoller:
    """Fuehrt den Stack-Zustand aus der Prozess-Steuerung nach.

    Zwei Aufgaben in einem Takt (Phase-15-Luecke und M1b): einen von Hand
    gestoppten oder gestarteten Stack bemerken — und einen **Absturz**.
    Das zweite ist neu und der Grund, warum der Poller nicht nach „laeuft
    alles?" fragt, sondern nach dem Zustand jedes Prozesses: unter Docker
    war „laeuft nicht" gutartig, unter supervisord unterscheidet sich
    gestoppt von abgestuerzt (:meth:`acoustid_watchdog.wake.
    WakeCoordinator.observe`).

    Gefragt wird per **Polling**, nicht per Push: ein supervisord-
    Eventlistener ist ein eigener, von supervisord gespawnter Prozess mit
    stdin/stdout-Protokoll — also ein Brueckenprozess und ein eigener
    Ausbauschritt, kein Bestandteil von M1b (M0-Analyse §2.1).
    """

    def __init__(
        self,
        coordinator: WakeCoordinator,
        *,
        interval_s: float = DEFAULT_STATE_POLL_INTERVAL_S,
    ) -> None:
        self._coordinator = coordinator
        self.interval_s = interval_s
        #: Wie viele Abgleiche stattgefunden haben (Tests, Diagnose).
        self.checks = 0

    async def check(self) -> StackState | None:
        """Ein Abgleich mit der Prozess-Steuerung.

        Returns:
            Der Zustand nach dem Abgleich, oder ``None``, wenn dieser Takt
            uebersprungen wurde, weil gerade geweckt oder gestoppt wird.

        Waehrend eines Weck- oder Stoppvorgangs sind die Prozesse in
        Bewegung; eine Momentaufnahme wuerde dann nur die Anzeige flackern
        lassen (``startet`` -> ``schlafend`` -> ``bereit``) und im
        schlimmsten Fall einen laufenden Vorgang uebergehen.
        """
        if self._coordinator.busy:
            return None
        self.checks += 1
        # Der Abgleich spricht ueber den Socket mit supervisord und ggf. per
        # HTTP mit der API — beides synchron, also in den Threadpool.
        return await run_in_threadpool(self._coordinator.observe)

    async def run(self) -> None:
        """Gleicht bis zum Abbruch periodisch ab."""
        while True:
            await asyncio.sleep(self.interval_s)
            try:
                await self.check()
            except Exception:
                _LOG.exception("Zustandsabgleich fehlgeschlagen")
