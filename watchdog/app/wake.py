"""Wake-on-request: eine Anfrage weckt den Stack (ARCHITECTURE §3, §7).

Der Kern des On-Demand-Betriebs. Zwei Bausteine, bewusst getrennt:

* :class:`ReadinessProbe` — die Bereitschaftsfrage an den API-Dienst
  (interner Healthcheck, DECISIONS 2026-08-01). Sie ist der einzige
  verlaessliche Punkt, an dem „bereit" mehr heisst als „Prozess laeuft":
  der Endpunkt prueft Datenbank **und** Suchindex. Ihre Adresse ist ein
  Bootstrap-Wert (``MMO_API_HEALTH_URL``, :mod:`shared.env`) und keine
  Modulkonstante — im Ein-Container-Betrieb ist sie eine andere.
* :class:`WakeCoordinator` — haelt Anfragen, waehrend gestartet wird,
  sorgt dafuer, dass gleichzeitige Anfragen **einen** Weckvorgang ausloesen,
  legt den Stack wieder schlafen und erhebt seinen Zustand ueber die
  Steuerung.

**Welche Prozesse zum Stack gehoeren, steht hier nicht** (seit M1b): das
weiss :mod:`acoustid_watchdog.stack`, und der Koordinator kennt davon nur
den Vertrag
(:class:`~acoustid_watchdog.control.ProcessGroupController`). Genau
dadurch war der Ein-Container-Umbau ein Adapter-Tausch und kein Eingriff
in die Weck-Logik.

**Warum genau ein Weckvorgang.** Ein zweiter, gleichzeitiger Start waere
nicht nur Verschwendung: er wuerde waehrend eines laufenden Starts erneut
starten und die Zustandsanzeige flackern lassen. Der Koordinator haelt
deshalb genau eine Aufgabe; jede weitere Anfrage haengt sich mit ihrer
**eigenen** Frist daran (``wake.hold_timeout_s``, §6). Wer zuerst kam,
wartet nicht laenger als wer spaeter kam — jeder bekommt seine volle
Haltezeit ab dem eigenen Eintreffen, und **der Vorgang selbst laeuft
mindestens so lange wie sein geduldigster Wartender** (Phase 16: die Frist
des Vorgangs wird beim Dazukommen verlaengert, sie wird nicht mehr von der
ersten Anfrage geerbt).

**Wecken und Stoppen schliessen einander aus.** Beides laeuft als genau
eine Aufgabe, und ein Weckvorgang wartet zuerst einen laufenden Stopp ab
(:meth:`WakeCoordinator.stop_stack`). Eine Anfrage, die waehrend
``stopping`` eintrifft, wird also nicht abgewiesen und ueberholt den Stopp
auch nicht: sie wartet, bis der Stack steht, und weckt ihn dann wieder —
konservativ, weil ein halb gestoppter Stack kein bedienbarer Stack ist und
Start und Stopp sich sonst ins Gehege kaemen.

**Was hier NICHT steht.** Wann der Idle-Stopp faellig ist und wann der
Zustand nachgefuehrt wird, entscheiden die beiden Dauerlaeufer in
:mod:`acoustid_watchdog.lifecycle`; die erlaubten Zustandswechsel stehen in
:mod:`acoustid_watchdog.state`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from contextlib import suppress
from typing import Any, Final

import httpx
from starlette.concurrency import run_in_threadpool

from acoustid_watchdog.control import GroupStatus, ProcessControlError, ProcessGroupController
from acoustid_watchdog.events import EventLevel
from acoustid_watchdog.notify import Notification, stack_start_failed
from acoustid_watchdog.state import StackStateTracker
from shared.models import StackState

__all__ = [
    "DEFAULT_POLL_INTERVAL_S",
    "DEFAULT_RETRY_AFTER_S",
    "ReadinessProbe",
    "StackNotReadyError",
    "WakeCoordinator",
]

_LOG = logging.getLogger(__name__)

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
        url: str,
        *,
        timeout_s: float = DEFAULT_PROBE_TIMEOUT_S,
        client: httpx.Client | None = None,
    ) -> None:
        """
        Args:
            url: Adresse des internen Healthchecks
                (``MMO_API_HEALTH_URL``). Bewusst ohne Vorgabewert — wie
                bei :class:`~acoustid_watchdog.proxy.ReverseProxy`: eine
                hier eingebaute Adresse waere ein zweiter Ort, an dem die
                Umgebung steht, und wuerde bei einer Umstellung still die
                falsche bleiben.
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


class WakeCoordinator:
    """Haelt Anfragen, bis der Stack bereit ist — mit genau einem Weckvorgang."""

    def __init__(
        self,
        controller: ProcessGroupController,
        probe: ReadinessProbe,
        state: StackStateTracker,
        *,
        log_event: Callable[..., None] | None = None,
        notify: Callable[[Notification], Any] | None = None,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        """
        Args:
            controller: Steuerung des Stacks. Bewusst nur ueber das
                Protokoll getypt: der Koordinator kennt ``start``,
                ``stop``, ``inspect`` — nicht die Technik dahinter.
            probe: Bereitschaftsfrage an den API-Dienst.
            state: Zustandsanzeige (``/status``, spaeter die Admin-UI).
            log_event: ``(level, message, extra)`` — Anschluss an das
                Ereignis-Log des Waechters. Ohne Angabe wird nur ins
                Containerlog geschrieben.
            notify: Anschluss an die Benachrichtigungen (M2.5). Erwartet
                wird der **nicht blockierende** Weg
                (:meth:`~acoustid_watchdog.notify.Notifier.send_background`):
                der Weckvorgang laeuft in der Ereignisschleife, und beide
                Kanaele sind synchron.
            poll_interval_s: Abstand zwischen zwei Bereitschaftsfragen.
        """
        self._controller = controller
        self._probe = probe
        self._state = state
        self._log_event = log_event
        self._notify = notify
        self._poll_interval_s = poll_interval_s
        # Bewusst ein einfaches Flag und kein ``asyncio.Event``: niemand
        # *wartet* darauf (gewartet wird auf die Weck-Aufgabe), gelesen und
        # gesetzt wird es dagegen auch aus dem Threadpool — und
        # ``Event.set()`` ist nicht threadsicher.
        self._ready = False
        self._task: asyncio.Task[None] | None = None
        self._stop_task: asyncio.Task[bool] | None = None
        #: Frist des laufenden Weckvorgangs (``time.monotonic``); wird von
        #: jedem Dazukommenden auf seine eigene Haltezeit verlaengert.
        self._deadline = 0.0
        self._wakes = 0
        self._stops = 0
        #: Aufeinanderfolgende erfolglose Abfragen der Steuerung
        #: (:meth:`observe`).
        self._control_failures = 0
        #: Zuletzt gemeldete abgestuerzte Prozesse. Nur eine **Aenderung**
        #: ist ein Ereignis wert: der Poller fragt alle 15 s, und ein
        #: dauerhaft toter Prozess wuerde den Ringpuffer sonst an einem
        #: Nachmittag leeren (Muster von :attr:`_control_failures`).
        self._crashed: tuple[str, ...] = ()
        #: Dasselbe fuer den Teilzustand „laeuft, aber nicht vollstaendig"
        #: (:meth:`_observe_partial`).
        self._partial: tuple[str, ...] = ()
        #: Zuletzt erhobener Zustand je Prozess (``("db", "RUNNING")``, …).
        #: Der Poller fragt ihn ohnehin alle 15 s ab; gemerkt wird er, damit
        #: `/metrics` den Prozess-Zustand ausweisen kann, **ohne** selbst
        #: mit supervisord zu sprechen (M2.5).
        self._process_states: tuple[tuple[str, str], ...] = ()

    @property
    def wakes(self) -> int:
        """Wie viele Weckvorgaenge dieser Prozess begonnen hat.

        Zaehlt **begonnene** Vorgaenge, nicht wartende Anfragen — genau die
        Groesse, an der sich „gleichzeitige Anfragen wecken nur einmal"
        pruefen laesst (und die Phase 22 als Metrik ausgibt).
        """
        return self._wakes

    @property
    def stops(self) -> int:
        """Wie viele Stoppvorgaenge dieser Prozess begonnen hat."""
        return self._stops

    @property
    def ready(self) -> bool:
        """Gilt der Stack als bereit (letzte Pruefung war erfolgreich)?"""
        return self._ready

    @property
    def process_states(self) -> tuple[tuple[str, str], ...]:
        """Zuletzt erhobener Zustand je Prozess — leer vor dem ersten Abgleich.

        Eine **Momentaufnahme aus dem Speicher**: `/metrics` liest sie,
        statt selbst mit supervisord zu sprechen. Aktualisiert wird sie im
        Takt des Pollers (15 s, :class:`acoustid_watchdog.lifecycle.
        StatePoller`).
        """
        return self._process_states

    @property
    def busy(self) -> bool:
        """Laeuft gerade ein Weck- oder Stoppvorgang?

        Die Frage des Hintergrund-Pollers: waehrend eines Vorgangs sind die
        Container in Bewegung, und was Docker in diesem Moment meldet, sagt
        ueber den Zustand weniger aus als der Vorgang selbst.
        """
        return _running(self._task) or _running(self._stop_task)

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
        if self._ready:
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
        if self._ready:
            self._ready = False
            _LOG.info("Bereitschaft verworfen — naechste Anfrage prueft erneut")

    # --- Weckvorgang --------------------------------------------------------

    def _join(self, timeout_s: float) -> asyncio.Task[None]:
        """Liefert die laufende Weck-Aufgabe oder startet die einzige neue.

        Zwischen Pruefung und ``create_task`` liegt kein ``await``; damit
        koennen zwei gleichzeitige Anfragen den Vorgang nicht doppelt
        ausloesen — ohne Sperre, die im Fehlerfall haengen bleiben koennte.

        Die Frist des Vorgangs waechst mit jedem Dazukommenden auf dessen
        eigene Haltezeit (Phase-15-Luecke, geschlossen in Phase 16): sonst
        endete der Vorgang nach der Frist der **ersten** Anfrage, und ein
        spaeter Wartender saehe seine 503 vor Ablauf seiner eigenen Zeit.
        """
        deadline = time.monotonic() + timeout_s
        task = self._task
        if task is None or task.done():
            self._deadline = deadline
            task = asyncio.create_task(self._wake())
            # Ohne diesen Abholer meldet asyncio „exception was never
            # retrieved", wenn alle Wartenden vorher in ihre Frist gelaufen
            # sind. Die Ausnahme selbst haben sie laengst gesehen.
            task.add_done_callback(_swallow)
            self._task = task
        else:
            self._deadline = max(self._deadline, deadline)
        return task

    async def _wake(self) -> None:
        """Startet den Stack und wartet auf seine Bereitschaft.

        Die Obergrenze ist ``self._deadline`` — sie gehoert dem Vorgang,
        nicht einer einzelnen Anfrage, und wird waehrend des Wartens
        weitergelesen (:meth:`_join`).
        """
        # Ein laufender Stopp geht vor: erst faellt der Stack ganz, dann
        # wird er wieder geweckt. `shield`, damit unsere eigene Frist den
        # Stopp nicht abbricht.
        stop_task = self._stop_task
        if _running(stop_task) and stop_task is not None:
            _LOG.info("Weckvorgang wartet auf den laufenden Stopp")
            with suppress(Exception):
                await asyncio.shield(stop_task)

        self._wakes += 1
        started_at = time.monotonic()
        self._state.to(StackState.STARTING)
        self._event(EventLevel.INFO, "Stack wird geweckt")

        try:
            started = await run_in_threadpool(self._controller.start)
        except ProcessControlError as exc:
            detail = str(exc)
            self._state.to(StackState.ERROR, detail=detail)
            self._event(EventLevel.ERROR, "Stack-Start fehlgeschlagen", {"error": detail})
            # Der Stack bleibt bewusst im Fehlerzustand stehen (Phase 16) —
            # ohne Meldung saehe der Betreiber davon nur die 503-Antworten.
            self._send_notification(stack_start_failed(detail))
            raise StackNotReadyError(f"Stack konnte nicht gestartet werden: {detail}") from exc

        _LOG.info("Stack-Prozesse gestartet", extra={"processes_started": started})

        while True:
            if await run_in_threadpool(self._probe.ready):
                waited_s = round(time.monotonic() - started_at, 1)
                self._ready = True
                self._state.to(StackState.READY)
                self._event(
                    EventLevel.INFO,
                    "Stack ist bereit",
                    {"waited_s": waited_s, "processes_started": started},
                )
                return
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(self._poll_interval_s, remaining))

        # Kein Fehlerzustand: der Stack startet vermutlich noch, nur eben
        # laenger als eine Anfrage warten darf. `starting` bleibt stehen,
        # die naechste Anfrage haengt sich an einen neuen Vorgang.
        waited_s = round(time.monotonic() - started_at, 1)
        self._event(
            EventLevel.WARNING,
            "Stack war nicht rechtzeitig bereit",
            {"waited_s": waited_s},
        )
        raise StackNotReadyError(f"Stack wurde nicht binnen {waited_s:g} s bereit")

    # --- Schlafen legen -----------------------------------------------------

    async def stop_stack(self, *, reason: str) -> bool:
        """Legt den Stack schlafen (Idle-Stopp, spaeter der Admin-Knopf).

        Args:
            reason: Warum gestoppt wird (``"idle"``, spaeter ``"manual"``) —
                geht als Feld ins Ereignis-Log.

        Returns:
            ``True``, wenn **dieser** Aufruf den Stack gestoppt hat;
            ``False``, wenn der Stack nicht bereit war, gerade geweckt wird,
            ein anderer Stopp schon lief oder der Stopp scheiterte (dann
            steht der Zustand auf ``error``).
        """
        stop_task = self._stop_task
        if _running(stop_task) and stop_task is not None:
            # Ein zweiter Stopp haette nichts zu tun; das Ergebnis gehoert
            # dem ersten.
            with suppress(Exception):
                await asyncio.shield(stop_task)
            return False
        if _running(self._task):
            _LOG.info("Stopp verworfen — der Stack wird gerade geweckt")
            return False
        if self._state.state is not StackState.READY:
            return False

        task = asyncio.create_task(self._stop(reason))
        task.add_done_callback(_swallow)
        self._stop_task = task
        return await task

    async def _stop(self, reason: str) -> bool:
        """Stoppt die Stack-Container und fuehrt den Zustand nach."""
        self._stops += 1
        # **Vor** dem ersten `docker stop`: ab jetzt ist der Stack nicht
        # mehr bereit, auch wenn noch alles laeuft. Eine Anfrage, die genau
        # hier eintrifft, wartet auf das Ende des Stopps und weckt dann.
        self._ready = False
        self._state.to(StackState.STOPPING)
        self._event(EventLevel.INFO, "Stack wird schlafen gelegt", {"reason": reason})

        started_at = time.monotonic()
        try:
            stopped = await run_in_threadpool(self._controller.stop)
        except ProcessControlError as exc:
            detail = str(exc)
            self._state.to(StackState.ERROR, detail=detail)
            self._event(
                EventLevel.ERROR,
                "Stack-Stopp fehlgeschlagen",
                {"error": detail, "reason": reason},
            )
            return False

        self._state.to(StackState.SLEEPING)
        self._event(
            EventLevel.INFO,
            "Stack schlaeft",
            {
                "reason": reason,
                "processes_stopped": stopped,
                "took_s": round(time.monotonic() - started_at, 1),
            },
        )
        return True

    # --- Zustand aus der Steuerung erheben ----------------------------------

    def refresh(self) -> StackState:
        """Erhebt den Zustand einmal aus der Steuerung (Start des Waechters).

        Der Zustand liegt nur im Speicher (DECISIONS 2026-08-01, Punkt 6) —
        nach einem Neustart des Waechters muss er neu erhoben werden, sonst
        zeigt `/status` „schlafend", waehrend der Stack laeuft.
        """
        return self.observe(announce=False)

    def observe(self, *, announce: bool = True) -> StackState:
        """Gleicht den gefuehrten Zustand mit der Steuerung ab.

        Die Antwort auf die Phase-15-Luecke „von Hand gestoppter Stack":
        beim Start (:meth:`refresh`) und danach im Takt des Pollers
        (:class:`acoustid_watchdog.lifecycle.StatePoller`) fragt der
        Waechter die Prozesse und korrigiert seine Anzeige. Erst dadurch
        zeigt `/status` die Wahrheit — und die erste Anfrage nach einem
        Stopp von Hand weckt, statt ins Leere zu laufen.

        Bewusst zurueckhaltend, damit die Anzeige nicht flackert:

        * **Ein Prozess ist von selbst weggefallen** -> ``fehler``. Neu in
          M1b und der Grund fuer die Kante ``ready→error``: unter Docker
          war „laeuft nicht" eindeutig gutartig, unter einem
          Prozess-Supervisor nicht mehr. ``STOPPED`` ist der Idle-Stopp,
          ``EXITED``/``FATAL``/``BACKOFF`` sind ein Absturz — und ein
          Absturz darf sich nie als Schlaf maskieren (R8).
        * **Alle stoppbaren Prozesse stehen** -> ``schlafend``. Gemeint ist
          wirklich ``STOPPED``, nicht „laeuft nicht": ein Prozess in
          ``STARTING`` (Autorestart nach einem Absturz, E15) schlaeft nicht.
        * **Alles laeuft und der Healthcheck antwortet** -> ``bereit``.
        * **Alles dazwischen** — halb gestartet, ein einzelner Prozess von
          Hand gestoppt, ein Start unterwegs -> der gefuehrte Zustand bleibt
          stehen, die Bereitschaft wird verworfen
          (:meth:`_observe_partial`). Nie ``schlafend``: eine laufende
          Postgres haelt das Array wach, und genau das waere die Anzeige,
          die niemand hinterfragt. Ein einzelner verpasster Healthcheck
          macht aus ``bereit`` ebenfalls kein ``schlafend`` (dafuer gibt es
          den Weg ueber :meth:`invalidate` im Proxy).

        Eine nicht erreichbare Steuerung ist kein Fehlerzustand des
        Stacks: der Waechter muss auch dann laufen (``/status``, Admin-UI),
        damit der Betreiber ueberhaupt sieht, dass etwas nicht stimmt.

        Args:
            announce: Auffaellige Aenderungen zusaetzlich als Ereignis
                melden. Beim Start ist das unerwuenscht — dort ist jeder
                Zustand „neu", ohne dass jemand etwas getan haette.
        """
        try:
            status = self._controller.inspect()
        except ProcessControlError as exc:
            self._control_failures += 1
            # Nur der erste Fehlschlag einer Serie ist eine Warnung wert;
            # eine dauerhaft tote Steuerung wuerde sonst das Log fluten.
            log = _LOG.warning if self._control_failures == 1 else _LOG.debug
            log(
                "Stack-Zustand nicht ermittelbar — die Prozess-Steuerung antwortet nicht",
                extra={"error": str(exc), "attempts": self._control_failures},
            )
            return self._state.state
        if self._control_failures:
            _LOG.info(
                "Prozess-Steuerung antwortet wieder",
                extra={"attempts": self._control_failures},
            )
            self._control_failures = 0

        self._process_states = status.states
        previous = self._state.state
        if status.crashed:
            return self._observe_crash(status.crashed, previous, announce=announce)
        if self._crashed:
            _LOG.info("Kein Prozess mehr im Fehlerzustand", extra={"was": list(self._crashed)})
            self._crashed = ()

        if status.sleeping:
            self._partial = ()
            self.invalidate()
            if previous is StackState.ERROR:
                # Der Fehlerzustand bleibt stehen, bis ihn ein Weckversuch
                # aufloest oder der Stack nachweislich wieder laeuft.
                # „schlafend" waere die schoenere, aber falsche Anzeige —
                # und der Betreiber saehe den Fehler nur noch im Log.
                return previous
            changed = self._state.try_to(StackState.SLEEPING)
            if changed is not None and announce and previous is StackState.READY:
                self._event(EventLevel.WARNING, "Stack wurde ausserhalb des Waechters gestoppt")
            return self._state.state

        if not status.running:
            return self._observe_partial(status, announce=announce)
        self._partial = ()

        if not self._probe.ready():
            return self._state.state

        self._ready = True
        changed = self._state.try_to(StackState.READY)
        if changed is not None and announce and previous is not StackState.STARTING:
            self._event(EventLevel.INFO, "Stack wurde ausserhalb des Waechters gestartet")
        return self._state.state

    def _observe_partial(self, status: GroupStatus, *, announce: bool) -> StackState:
        """Weder ganz wach noch ganz schlafend — die Anzeige bleibt stehen.

        Der Fall, den es unter Docker nicht gab und der die gefaehrlichste
        Fehlanzeige des Projekts waere (R8): laeuft die Datenbank noch,
        waehrend die API steht, haelt sie das Array wach — „schlafend" waere
        dann eine Luege, die niemand bemerkt, weil sie wie der Gutzustand
        aussieht.

        Was hier passiert und was ausdruecklich **nicht**:

        * Der gefuehrte Zustand bleibt, wie er ist. Ein laufender Weckvorgang
          (``startet``) soll nicht ueberholt werden, und fuer „teilweise
          wach" gibt es bewusst keinen eigenen Zustand — die naechste
          Anfrage startet ohnehin nach, was fehlt (``start()`` ist
          idempotent).
        * Die **Bereitschaft wird verworfen**: was halb steht, beantwortet
          keine Anfragen.
        * Gemeldet wird nur die **Aenderung** — der Poller fragt alle 15 s,
          und ein dauerhaft halb gestoppter Stack wuerde den Ringpuffer
          sonst leerlaufen lassen (dasselbe Muster wie beim Absturz).
        """
        stopped = tuple(name for name, state in status.states if state != "RUNNING")
        new = stopped != self._partial
        self._partial = stopped
        self.invalidate()
        if new:
            _LOG.warning(
                "Stack ist nur teilweise wach — Anzeige bleibt stehen",
                extra={"not_running": list(stopped), "stack_state": self._state.state.value},
            )
            if announce:
                self._event(
                    EventLevel.WARNING,
                    "Stack ist nur teilweise wach",
                    {"not_running": list(stopped), "stack_state": self._state.state.value},
                )
        return self._state.state

    def _observe_crash(
        self, crashed: tuple[str, ...], previous: StackState, *, announce: bool
    ) -> StackState:
        """Ein Prozess ist von selbst weggefallen (M1b, Kante ``ready→error``).

        Der Zustand wird **nachsichtig** gesetzt (``try_to``): waehrend
        eines Weckvorgangs gehoert die Anzeige dem Vorgang, und aus
        ``schlafend`` heraus gibt es keine Kante in den Fehler — ein
        Prozess, den niemand gestartet hat, ist kein Betriebsfehler. Der
        Befund steht dann trotzdem im Ereignis-Log: gemeldet wird, was
        passiert ist, angezeigt nur, was die Zustandsmaschine erlaubt.
        """
        detail = "Prozess unerwartet beendet: " + ", ".join(crashed)
        new = crashed != self._crashed
        self._crashed = crashed
        if new:
            _LOG.error("Prozess unerwartet beendet", extra={"processes": list(crashed)})
        self.invalidate()
        changed = self._state.try_to(StackState.ERROR, detail=detail)
        if new and announce and changed is None:
            # Kein Zustandswechsel (also kein Ereignis aus dem Tracker) —
            # der Befund darf trotzdem nicht verschwinden.
            self._event(
                EventLevel.WARNING,
                "Prozess unerwartet beendet",
                {"processes": list(crashed), "stack_state": self._state.state.value},
            )
        return self._state.state

    # --- Hilfen -------------------------------------------------------------

    def _event(self, level: EventLevel, message: str, extra: dict[str, Any] | None = None) -> None:
        if self._log_event is not None:
            self._log_event(level, message, extra)
        else:  # pragma: no cover - nur ohne angeschlossenes Ereignis-Log
            _LOG.log(level.logging_level, message, extra=extra or {})

    def _send_notification(self, notification: Notification) -> None:
        """Meldet nach draussen, wenn ein Kanal eingerichtet ist.

        Ein Fehler beim Melden darf den Weckvorgang nicht mitreissen: die
        Ausnahme, die gerade gemeldet werden soll, ist die wichtigere.
        """
        if self._notify is None:
            return
        try:
            self._notify(notification)
        except Exception:  # pragma: no cover - defensiv
            _LOG.exception("Benachrichtigung konnte nicht ausgeloest werden")


def _swallow(task: asyncio.Task[Any]) -> None:
    """Holt die Ausnahme einer beendeten Weck-/Stopp-Aufgabe ab."""
    if not task.cancelled():
        task.exception()


def _running(task: asyncio.Task[Any] | None) -> bool:
    """Gibt es diese Aufgabe, und ist sie noch unterwegs?"""
    return task is not None and not task.done()
