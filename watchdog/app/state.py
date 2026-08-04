"""Zustandsmaschine des Stacks (§7, §9) — fuenf Zustaende, feste Uebergaenge.

Der Lebenszyklus einer schlafenden Instanz in einer Tabelle::

    schlafend --Anfrage--> startet --bereit--> bereit --Leerlauf--> stoppt
        ^                     |                  |                    |
        |                     `--Startfehler--> fehler                |
        `-----------------------------------------------------------'

Phase 14 hielt davon nur die Anzeige („welcher Zustand gilt, seit wann,
warum"), Phase 15 setzte sie beim Wecken. Phase 16 macht daraus eine
richtige Maschine: **welcher Wechsel erlaubt ist, steht hier** — einmal,
als Tabelle (:data:`ALLOWED_TRANSITIONS`), und nicht verteilt ueber die
Aufrufer.

**Warum ueberhaupt eine Uebergangstabelle.** Ohne sie kann jeder Aufrufer
jeden Zustand setzen, und ein Fehler faellt erst in der Anzeige auf — z. B.
„bereit", waehrend der Stack gerade gestoppt wird, oder „fehler" aus dem
Nichts. Mit ihr ist jeder Weg belegt: jede Kante der Tabelle hat genau
einen Aufrufer, und jede fehlende Kante ist eine gepruefte Zusage.

============  ==========================================================
von           nach (und wer den Wechsel ausloest)
============  ==========================================================
``sleeping``  ``starting`` (eine Anfrage weckt),
              ``ready`` (Poller: von Hand gestarteter Stack)
``starting``  ``ready`` (bereit), ``error`` (Startfehler der Steuerung),
              ``sleeping`` (Poller: kein Weckvorgang mehr, Stack steht)
``ready``     ``stopping`` (Idle-Stopp), ``starting`` (erneutes Wecken
              nach verworfener Bereitschaft), ``sleeping`` (Poller: von
              Hand gestoppter Stack), ``error`` (Poller: ein Prozess ist
              im Betrieb abgestuerzt — seit M1b)
``stopping``  ``sleeping`` (Stopp fertig), ``error`` (Stopp gescheitert)
``error``     ``starting`` (naechster Weckversuch — der Weg aus dem
              Fehler heraus), ``ready`` (Poller: Stack laeuft wieder)
============  ==========================================================

**Die Kante ``ready`` -> ``error`` ist neu** (M1b) und war in v1
ausdruecklich verboten. Der Grund fuer beides ist derselbe Satz aus
verschiedenen Welten: unter Docker war „laeuft nicht" eindeutig gutartig —
ein Container, den der Betreiber von Hand gestoppt hatte, schlief eben.
Unter einem Prozess-Supervisor unterscheidet sich „gestoppt" (``STOPPED``,
genau das will der Idle-Stopp) von „von selbst weggefallen" (``EXITED``,
``FATAL``, ``BACKOFF``). Ohne diese Kante muesste ein Absturz im laufenden
Betrieb als ``schlafend`` angezeigt werden — er wuerde sich also als Schlaf
maskieren, und der Betreiber saehe einen Gutzustand (M0-Analyse §2.1, R8).

**Nicht erlaubt** sind unter anderem: ``sleeping`` -> ``stopping`` (was
steht, wird nicht gestoppt), ``sleeping`` -> ``error`` (was niemand
gestartet hat, kann nicht abstuerzen; der Poller meldet einen solchen
Befund ins Ereignis-Log, ohne den Zustand zu aendern), ``stopping`` ->
``starting`` (erst faellt der Stack ganz, dann weckt ihn die naechste
Anfrage — :meth:`acoustid_watchdog.wake.WakeCoordinator.stop_stack`) und
``error`` -> ``stopping`` (ein Stack im Fehlerzustand gilt nicht als
laufend).

**Streng oder nachsichtig.** Zwei Wege, bewusst getrennt:

* :meth:`StackStateTracker.to` **wirft** bei einem verbotenen Wechsel. Den
  Weg nehmen die Stellen, die ihren Ausgangszustand kennen (Wecken,
  Stoppen) — dort waere ein verbotener Wechsel ein Programmfehler.
* :meth:`StackStateTracker.try_to` **protokolliert** und laesst den Zustand
  stehen. Den Weg nimmt der Hintergrund-Poller: er erhebt, was die
  Prozess-Steuerung sagt, und darf einen laufenden Weck- oder Stoppvorgang
  nicht ueberholen.

**Warum im Speicher und nicht in der SQLite.** Der Zustand beschreibt, was
die Prozesse gerade tun — beim Start des Waechters ist ein gespeicherter
Wert bestenfalls veraltet und schlimmstenfalls falsch (der Betreiber kann
den Stack zwischenzeitlich von Hand gestartet haben). Er wird deshalb beim
Start aus der Steuerung erhoben und danach laufend nachgefuehrt
(:class:`acoustid_watchdog.lifecycle.StatePoller`).

``sleeping`` ist ausdruecklich der **Gutzustand**, kein Mangel
(ARCHITECTURE §9) — der ganze Betrieb ist darauf ausgelegt, dass das Array
steht.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, Self

from acoustid_watchdog.store import utc_now
from shared.models import StackState

__all__ = [
    "ALLOWED_TRANSITIONS",
    "InvalidStackTransitionError",
    "StackStateTracker",
    "StackStatus",
]

_LOG = logging.getLogger(__name__)


#: Erlaubte Zustandswechsel (Begruendung je Kante: Modul-Docstring).
#: Ein Wechsel auf **denselben** Zustand ist immer erlaubt und aendert
#: hoechstens den Fehlertext; er steht deshalb in keiner Zeile.
ALLOWED_TRANSITIONS: Final[dict[StackState, frozenset[StackState]]] = {
    StackState.SLEEPING: frozenset({StackState.STARTING, StackState.READY}),
    StackState.STARTING: frozenset({StackState.READY, StackState.ERROR, StackState.SLEEPING}),
    StackState.READY: frozenset(
        {
            StackState.STARTING,
            StackState.STOPPING,
            StackState.SLEEPING,
            # Seit M1b: Prozessabsturz im laufenden Betrieb (Modul-Docstring).
            StackState.ERROR,
        }
    ),
    StackState.STOPPING: frozenset({StackState.SLEEPING, StackState.ERROR}),
    StackState.ERROR: frozenset({StackState.STARTING, StackState.READY}),
}


class InvalidStackTransitionError(RuntimeError):
    """Ein Zustandswechsel, den :data:`ALLOWED_TRANSITIONS` nicht kennt."""

    def __init__(self, source: StackState, target: StackState) -> None:
        super().__init__(
            f"Zustandswechsel {source.value} -> {target.value} ist nicht vorgesehen "
            f"(erlaubt: {', '.join(sorted(s.value for s in ALLOWED_TRANSITIONS[source]))})"
        )
        self.source = source
        self.target = target


@dataclass(frozen=True, slots=True)
class StackStatus:
    """Momentaufnahme des Stack-Zustands."""

    state: StackState
    #: Seit wann dieser Zustand gilt (ISO-8601, UTC).
    since: str
    #: Klartext bei ``error``, sonst ``None`` (ARCHITECTURE §7:
    #: „Stack-Start-Fehler -> 503 + Fehlertext").
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "state_display": self.state.display_name,
            "since": self.since,
            "detail": self.detail,
        }


class StackStateTracker:
    """Fuehrt den Stack-Zustand — threadsicher, mit Uebergangspruefung."""

    def __init__(
        self,
        state: StackState = StackState.SLEEPING,
        *,
        since: str | None = None,
        on_transition: Callable[[StackStatus, StackStatus], None] | None = None,
    ) -> None:
        """
        Args:
            state: Anfangszustand.
            since: Zeitpunkt, seit dem er gilt (Vorgabe: jetzt).
            on_transition: ``(vorher, nachher)`` — wird nach **jedem**
                wirklichen Wechsel gerufen, ausserhalb der Sperre. Der
                Anschluss ans Ereignis-Log (:class:`WatchdogService`); ohne
                Angabe bleibt es beim Containerlog.
        """
        self._lock = threading.Lock()
        self._status = StackStatus(state=state, since=since or utc_now())
        #: Frei setzbar, damit auch ein von aussen mitgegebener Tracker
        #: (Tests, eingebettete Nutzung) ans Ereignis-Log kommt.
        self.on_transition = on_transition

    @classmethod
    def sleeping(cls) -> Self:
        """Der Startwert eines frisch gestarteten Waechters."""
        return cls(StackState.SLEEPING)

    # --- Lesen --------------------------------------------------------------

    @property
    def status(self) -> StackStatus:
        """Aktueller Zustand samt Zeitpunkt und Detail."""
        with self._lock:
            return self._status

    @property
    def state(self) -> StackState:
        return self.status.state

    def allows(self, target: StackState) -> bool:
        """Waere ein Wechsel auf ``target`` gerade erlaubt?"""
        current = self.state
        return target is current or target in ALLOWED_TRANSITIONS[current]

    # --- Schreiben ----------------------------------------------------------

    def to(self, target: StackState, *, detail: str | None = None) -> StackStatus:
        """Wechselt den Zustand; liefert die neue Momentaufnahme.

        Ein Wechsel auf denselben Zustand mit demselben Detail laesst
        ``since`` stehen — sonst erzeugte jede Pruefschleife die Anzeige
        „seit 0 Sekunden" — und ist kein Ereignis; die Antwort ist dann die
        unveraenderte Momentaufnahme.

        Raises:
            InvalidStackTransitionError: Der Wechsel steht nicht in
                :data:`ALLOWED_TRANSITIONS`.
        """
        status = self._switch(target, detail, strict=True)
        return status if status is not None else self.status

    def try_to(self, target: StackState, *, detail: str | None = None) -> StackStatus | None:
        """Wie :meth:`to`, aber ohne Ausnahme bei verbotenem Wechsel.

        Returns:
            Die neue Momentaufnahme, oder ``None``, wenn der Wechsel
            verboten war (dann bleibt der Zustand stehen und im Log steht
            der Grund). Ein Wechsel „auf sich selbst" liefert ebenfalls
            ``None`` — es hat sich nichts geaendert.
        """
        return self._switch(target, detail, strict=False)

    def _switch(
        self, target: StackState, detail: str | None, *, strict: bool
    ) -> StackStatus | None:
        with self._lock:
            previous = self._status
            if previous.state is target and previous.detail == detail:
                return None
            if target is not previous.state and target not in ALLOWED_TRANSITIONS[previous.state]:
                if strict:
                    raise InvalidStackTransitionError(previous.state, target)
                _LOG.warning(
                    "Zustandswechsel verworfen",
                    extra={
                        "stack_state": previous.state.value,
                        "stack_state_wanted": target.value,
                    },
                )
                return None
            self._status = StackStatus(state=target, since=utc_now(), detail=detail)
            current = self._status

        _LOG.info(
            "Stack-Zustand gewechselt",
            extra={
                "stack_state": current.state.value,
                "stack_state_previous": previous.state.value,
                "stack_detail": detail,
            },
        )
        self._announce(previous, current)
        return current

    def _announce(self, previous: StackStatus, current: StackStatus) -> None:
        """Meldet den Wechsel ans Ereignis-Log — ausserhalb der Sperre.

        Ein Fehler beim Protokollieren darf die Zustandsfuehrung nicht
        anhalten: der Zustand ist die Wahrheit ueber die Container, das
        Ereignis nur seine Mitschrift.
        """
        if self.on_transition is None:
            return
        try:
            self.on_transition(previous, current)
        except Exception:  # pragma: no cover - defensiv
            _LOG.exception("Zustandswechsel konnte nicht protokolliert werden")
