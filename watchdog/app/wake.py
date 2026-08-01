"""Wake-on-request: eine Anfrage weckt den Stack (ARCHITECTURE §3, §7).

Der Kern des On-Demand-Betriebs. Drei Bausteine, bewusst getrennt:

* :class:`StackController` — welche Container zum Stack gehoeren und in
  welcher Reihenfolge sie starten. Er kennt die Namen aus ARCHITECTURE §6
  („Feste Werte") und benutzt :mod:`acoustid_watchdog.docker`; sonst nichts.
* :class:`ReadinessProbe` — die Bereitschaftsfrage an den API-Dienst
  (interner Healthcheck, DECISIONS 2026-08-01). Sie ist der einzige
  verlaessliche Punkt, an dem „bereit" mehr heisst als „Prozess laeuft":
  der Endpunkt prueft Datenbank **und** Suchindex.
* :class:`WakeCoordinator` — haelt Anfragen, waehrend gestartet wird, und
  sorgt dafuer, dass gleichzeitige Anfragen **einen** Weckvorgang ausloesen.

**Warum genau ein Weckvorgang.** Ein zweiter, gleichzeitiger Start waere
nicht nur Verschwendung: er wuerde `docker start` waehrend eines laufenden
Starts erneut absetzen und die Zustandsanzeige flackern lassen. Der
Koordinator haelt deshalb genau eine Aufgabe; jede weitere Anfrage haengt
sich mit ihrer **eigenen** Frist daran (``wake.hold_timeout_s``, §6). Wer
zuerst kam, wartet nicht laenger als wer spaeter kam — jeder bekommt seine
volle Haltezeit ab dem eigenen Eintreffen.

**Was hier NICHT steht.** Die vollstaendige Zustandsmaschine, der
Idle-Stopp und die Feinheiten der Startfehler sind Phase 16. Dieses Modul
setzt den Zustand nur so weit, wie das Wecken ihn zwangslaeufig aendert
(``sleeping`` -> ``starting`` -> ``ready``, bei einem Docker-Fehler
``error``), und stoppt nur auf ausdruecklichen Auftrag.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from typing import Any, Final

import httpx
from starlette.concurrency import run_in_threadpool

from acoustid_watchdog.docker import DockerClient, DockerError
from acoustid_watchdog.events import EventLevel
from acoustid_watchdog.state import StackStateTracker
from shared.models import StackState

__all__ = [
    "API_BASE_URL",
    "API_HEALTH_URL",
    "DEFAULT_POLL_INTERVAL_S",
    "DEFAULT_RETRY_AFTER_S",
    "STACK_CONTAINERS",
    "ReadinessProbe",
    "StackController",
    "StackNotReadyError",
    "WakeCoordinator",
]

_LOG = logging.getLogger(__name__)

#: Container des Stacks in **Startreihenfolge** (ARCHITECTURE §6 „Feste
#: Werte"). Der Importer fehlt bewusst: er ist ein One-Shot-Job und wird
#: vom Scheduler gestartet (Phase 19), nie vom Wecken.
#:
#: Gestoppt wird in umgekehrter Reihenfolge — erst der Leser, dann seine
#: Datenquellen.
STACK_CONTAINERS: Final[tuple[str, ...]] = (
    "acoustid-db",
    "acoustid-index",
    "acoustid-api",
)

#: Interner Healthcheck des API-Dienstes (DECISIONS 2026-08-01). Kein Teil
#: des §7-Vertrags und nicht unter ``/v2/`` — er beantwortet genau eine
#: Frage, die kein oeffentlicher Endpunkt zuverlaessig beantwortet: „sind
#: Datenbank und Index angebunden?". Adresse und Port sind fest wie die
#: Container-Namen (``docker-compose.yml``: ``expose: 8080``, kein
#: veroeffentlichter Port).
API_HEALTH_URL: Final = "http://acoustid-api:8080/_health"

#: Basis-URL des API-Dienstes fuer den Proxy (:mod:`acoustid_watchdog.proxy`).
API_BASE_URL: Final = "http://acoustid-api:8080"

#: Abstand zwischen zwei Bereitschaftsfragen waehrend des Weckens. Der Stack
#: braucht Sekunden bis Minuten (Postgres-Recovery, Index-mmap); haeufigeres
#: Fragen beschleunigt nichts und fuellt nur das Log.
DEFAULT_POLL_INTERVAL_S: Final = 1.0

#: ``Retry-After`` der 503-Antwort. Bewusst ein fester, bescheidener Wert:
#: wie lange der Start noch dauert, weiss niemand — 30 Sekunden sind lang
#: genug, dass ein Client nicht im Sekundentakt wiederkommt, und kurz genug,
#: dass er den fertigen Stack bald sieht.
DEFAULT_RETRY_AFTER_S: Final = 30

#: Leseschranke der Bereitschaftsfrage. Antwortet die API nicht binnen
#: weniger Sekunden, ist sie nicht bereit — die Frage wird ohnehin gleich
#: wiederholt.
DEFAULT_PROBE_TIMEOUT_S: Final = 5.0


class StackNotReadyError(Exception):
    """Der Stack war nicht rechtzeitig bereit (oder liess sich nicht starten).

    Traegt alles, was die 503-Antwort braucht (ARCHITECTURE §7
    „Fehlerverhalten"): einen Klartext und die Wartezeit fuer
    ``Retry-After``.
    """

    def __init__(self, message: str, *, retry_after_s: int = DEFAULT_RETRY_AFTER_S) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class ReadinessProbe:
    """Fragt den internen Healthcheck des API-Dienstes."""

    def __init__(
        self,
        url: str = API_HEALTH_URL,
        *,
        timeout_s: float = DEFAULT_PROBE_TIMEOUT_S,
        client: httpx.Client | None = None,
    ) -> None:
        """
        Args:
            url: Adresse des internen Healthchecks.
            timeout_s: Leseschranke einer Frage.
            client: Vorhandener ``httpx.Client`` (Tests). Wird dann **nicht**
                von :meth:`close` geschlossen.
        """
        self.url = url
        self._timeout_s = timeout_s
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout_s)

    def ready(self) -> bool:
        """``True``, wenn der API-Dienst mit HTTP 200 antwortet.

        Jeder andere Ausgang — Verbindung abgelehnt, Zeitueberschreitung,
        HTTP 503 des Healthchecks — heisst „noch nicht bereit" und ist
        waehrend eines Starts der **Normalfall**. Deshalb ein Bool und keine
        Ausnahme; der Grund steht im Debug-Log.
        """
        try:
            response = self._client.get(self.url, timeout=self._timeout_s)
        except httpx.HTTPError as exc:
            _LOG.debug("Bereitschaftsfrage ohne Antwort", extra={"error": str(exc)})
            return False
        if response.status_code != httpx.codes.OK:
            _LOG.debug(
                "Bereitschaftsfrage verneint",
                extra={"status": response.status_code},
            )
            return False
        return True

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


class StackController:
    """Startet und stoppt die Stack-Container — mehr Wissen hat er nicht."""

    def __init__(
        self,
        docker: DockerClient,
        containers: Sequence[str] = STACK_CONTAINERS,
    ) -> None:
        self.docker = docker
        self.containers = tuple(containers)

    def start(self) -> list[str]:
        """Startet alle Stack-Container in der festgelegten Reihenfolge.

        Der Aufruf wartet **nicht** auf Bereitschaft — das tut der
        :class:`WakeCoordinator` ueber den Healthcheck der API. Bewusst
        keine Abhaengigkeitspruefung zwischen den Containern: `depends_on`
        gilt nur fuer `compose up`, und die API haelt einen Neustart aus,
        wenn ihre Datenbank noch nicht da ist (``restart: unless-stopped``).

        Returns:
            Namen der Container, die dieser Aufruf wirklich gestartet hat.

        Raises:
            DockerError: Ein Container fehlt oder der Daemon antwortet nicht.
        """
        return [name for name in self.containers if self.docker.start(name)]

    def stop(self) -> list[str]:
        """Stoppt alle Stack-Container in umgekehrter Reihenfolge.

        Returns:
            Namen der Container, die dieser Aufruf wirklich gestoppt hat.
        """
        return [name for name in reversed(self.containers) if self.docker.stop(name)]

    def all_running(self) -> bool:
        """Laufen alle Stack-Container?

        Raises:
            DockerError: Ein Container fehlt oder der Daemon antwortet nicht.
        """
        return all(self.docker.inspect(name).running for name in self.containers)


class WakeCoordinator:
    """Haelt Anfragen, bis der Stack bereit ist — mit genau einem Weckvorgang."""

    def __init__(
        self,
        controller: StackController,
        probe: ReadinessProbe,
        state: StackStateTracker,
        *,
        log_event: Callable[..., None] | None = None,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        """
        Args:
            controller: Steuerung der Stack-Container.
            probe: Bereitschaftsfrage an den API-Dienst.
            state: Zustandsanzeige (``/status``, spaeter die Admin-UI).
            log_event: ``(level, message, extra)`` — Anschluss an das
                Ereignis-Log des Waechters. Ohne Angabe wird nur ins
                Containerlog geschrieben.
            poll_interval_s: Abstand zwischen zwei Bereitschaftsfragen.
        """
        self._controller = controller
        self._probe = probe
        self._state = state
        self._log_event = log_event
        self._poll_interval_s = poll_interval_s
        self._ready = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._wakes = 0

    @property
    def wakes(self) -> int:
        """Wie viele Weckvorgaenge dieser Prozess begonnen hat.

        Zaehlt **begonnene** Vorgaenge, nicht wartende Anfragen — genau die
        Groesse, an der sich „gleichzeitige Anfragen wecken nur einmal"
        pruefen laesst (und die Phase 22 als Metrik ausgibt).
        """
        return self._wakes

    @property
    def ready(self) -> bool:
        """Gilt der Stack als bereit (letzte Pruefung war erfolgreich)?"""
        return self._ready.is_set()

    # --- Anfragepfad --------------------------------------------------------

    async def ensure_ready(self, *, timeout_s: float) -> bool:
        """Sorgt dafuer, dass der Stack bereit ist, und haelt so lange.

        Args:
            timeout_s: Haltezeit dieser Anfrage (``wake.hold_timeout_s``).

        Returns:
            ``True``, wenn diese Anfrage auf einen Weckvorgang warten musste.

        Raises:
            StackNotReadyError: Frist abgelaufen oder Start fehlgeschlagen.
        """
        if self._ready.is_set():
            return False

        task = self._join(timeout_s)
        try:
            # `shield`: laeuft **unsere** Frist ab, stirbt der Weckvorgang
            # nicht mit — andere Anfragen warten weiter, und die naechste
            # findet einen fertig gestarteten Stack vor.
            await asyncio.wait_for(asyncio.shield(task), timeout_s)
        except TimeoutError as exc:
            raise StackNotReadyError(
                f"Stack wurde nicht binnen {timeout_s:g} s bereit",
            ) from exc
        return True

    def invalidate(self) -> None:
        """Verwirft die Bereitschaftsannahme.

        Der Proxy ruft das auf, wenn die API unerwartet nicht antwortet: der
        Stack kann von Hand gestoppt worden sein. Die naechste Anfrage
        prueft dann wieder — und weckt, wenn noetig.
        """
        if self._ready.is_set():
            self._ready.clear()
            _LOG.info("Bereitschaft verworfen — naechste Anfrage prueft erneut")

    # --- Weckvorgang --------------------------------------------------------

    def _join(self, timeout_s: float) -> asyncio.Task[None]:
        """Liefert die laufende Weck-Aufgabe oder startet die einzige neue.

        Zwischen Pruefung und ``create_task`` liegt kein ``await``; damit
        koennen zwei gleichzeitige Anfragen den Vorgang nicht doppelt
        ausloesen — ohne Sperre, die im Fehlerfall haengen bleiben koennte.
        """
        task = self._task
        if task is None or task.done():
            task = asyncio.create_task(self._wake(timeout_s))
            # Ohne diesen Abholer meldet asyncio „exception was never
            # retrieved", wenn alle Wartenden vorher in ihre Frist gelaufen
            # sind. Die Ausnahme selbst haben sie laengst gesehen.
            task.add_done_callback(_swallow)
            self._task = task
        return task

    async def _wake(self, max_wait_s: float) -> None:
        """Startet den Stack und wartet auf seine Bereitschaft.

        Args:
            max_wait_s: Obergrenze fuer diesen Vorgang. Sie stammt von der
                Anfrage, die ihn ausgeloest hat; spaeter dazugekommene
                Anfragen bringen ihre eigene Frist mit (:meth:`ensure_ready`).
        """
        self._wakes += 1
        started_at = time.monotonic()
        self._state.set(StackState.STARTING)
        self._event(EventLevel.INFO, "Stack wird geweckt")

        try:
            started = await run_in_threadpool(self._controller.start)
        except DockerError as exc:
            detail = str(exc)
            self._state.set(StackState.ERROR, detail=detail)
            self._event(EventLevel.ERROR, "Stack-Start fehlgeschlagen", {"error": detail})
            raise StackNotReadyError(f"Stack konnte nicht gestartet werden: {detail}") from exc

        _LOG.info(
            "Stack-Container gestartet",
            extra={"containers_started": started, "max_wait_s": max_wait_s},
        )

        deadline = started_at + max_wait_s
        while True:
            if await run_in_threadpool(self._probe.ready):
                waited_s = round(time.monotonic() - started_at, 1)
                self._ready.set()
                self._state.set(StackState.READY)
                self._event(
                    EventLevel.INFO,
                    "Stack ist bereit",
                    {"waited_s": waited_s, "containers_started": started},
                )
                return
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(min(self._poll_interval_s, max(deadline - time.monotonic(), 0)))

        # Kein Fehlerzustand: der Stack startet vermutlich noch, nur eben
        # laenger als eine Anfrage warten darf. `starting` bleibt stehen,
        # die naechste Anfrage haengt sich an einen neuen Vorgang.
        waited_s = round(time.monotonic() - started_at, 1)
        self._event(
            EventLevel.WARNING,
            "Stack war nicht rechtzeitig bereit",
            {"waited_s": waited_s},
        )
        raise StackNotReadyError(f"Stack wurde nicht binnen {max_wait_s:g} s bereit")

    # --- Start des Waechters ------------------------------------------------

    def refresh(self) -> StackState:
        """Erhebt den Zustand einmal aus Docker (Start des Waechters).

        Der Zustand liegt nur im Speicher (DECISIONS 2026-08-01, Punkt 6) —
        nach einem Neustart des Waechters muss er neu erhoben werden, sonst
        zeigt `/status` „schlafend", waehrend der Stack laeuft. Bewusst
        genuegsam: laufen alle Container **und** antwortet der Healthcheck,
        gilt der Stack als bereit; alles andere ist ``schlafend``, bis eine
        Anfrage weckt.

        Ein nicht erreichbarer Docker-Daemon ist hier kein Startfehler: der
        Waechter muss auch dann laufen (``/status``, Admin-UI), damit der
        Betreiber ueberhaupt sieht, dass etwas nicht stimmt.
        """
        try:
            running = self._controller.all_running()
        except DockerError as exc:
            _LOG.warning(
                "Stack-Zustand nicht ermittelbar — Docker antwortet nicht",
                extra={"error": str(exc)},
            )
            return self._state.state
        if running and self._probe.ready():
            self._ready.set()
            return self._state.set(StackState.READY).state
        return self._state.set(StackState.SLEEPING).state

    # --- Hilfen -------------------------------------------------------------

    def _event(self, level: EventLevel, message: str, extra: dict[str, Any] | None = None) -> None:
        if self._log_event is not None:
            self._log_event(level, message, extra)
        else:  # pragma: no cover - nur ohne angeschlossenes Ereignis-Log
            _LOG.log(level.logging_level, message, extra=extra or {})


def _swallow(task: asyncio.Task[None]) -> None:
    """Holt die Ausnahme einer beendeten Weck-Aufgabe ab."""
    if not task.cancelled():
        task.exception()
