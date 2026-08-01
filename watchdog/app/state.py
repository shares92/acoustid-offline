"""Zustand des schlafenden Stacks, wie der Waechter ihn kennt (§7, §9).

Die fuenf Zustaende stehen schon fest (:class:`shared.models.StackState`:
``sleeping``, ``starting``, ``ready``, ``stopping``, ``error``); die
vollstaendige Zustandsmaschine mit ihren Uebergaengen und dem Idle-Stopp ist
Phase 16, die Docker-Steuerung dahinter Phase 15. Phase 14 braucht davon
genau so viel, wie `/status` und spaeter der Statusindikator der Admin-UI
ablesen: **welcher Zustand gilt, seit wann, und warum** (bei ``error`` der
Fehlertext).

**Warum im Speicher und nicht in der SQLite.** Der Zustand beschreibt, was
die Container gerade tun — beim Start des Waechters ist ein gespeicherter
Wert bestenfalls veraltet und schlimmstenfalls falsch (der Betreiber kann
den Stack zwischenzeitlich von Hand gestartet haben). Ab Phase 15 wird er
beim Start aus Docker erhoben; bis dahin ist ``sleeping`` die richtige und
einzige Annahme: nichts im Waechter kann etwas anderes bewirkt haben.

``sleeping`` ist ausdruecklich der **Gutzustand**, kein Mangel
(ARCHITECTURE §9) — der ganze Betrieb ist darauf ausgelegt, dass das Array
steht.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Self

from acoustid_watchdog.store import utc_now
from shared.models import StackState

__all__ = ["StackStateTracker", "StackStatus"]

_LOG = logging.getLogger(__name__)


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
    """Haelt den aktuellen Stack-Zustand — threadsicher und schreibarm."""

    def __init__(
        self, state: StackState = StackState.SLEEPING, *, since: str | None = None
    ) -> None:
        self._lock = threading.Lock()
        self._status = StackStatus(state=state, since=since or utc_now())

    @classmethod
    def sleeping(cls) -> Self:
        """Der Startwert eines frisch gestarteten Waechters."""
        return cls(StackState.SLEEPING)

    @property
    def status(self) -> StackStatus:
        """Aktueller Zustand samt Zeitpunkt und Detail."""
        with self._lock:
            return self._status

    @property
    def state(self) -> StackState:
        return self.status.state

    def set(self, state: StackState, *, detail: str | None = None) -> StackStatus:
        """Setzt den Zustand; liefert die neue Momentaufnahme.

        Ein Wechsel auf denselben Zustand laesst ``since`` stehen — sonst
        wuerde jede Pruefschleife die Anzeige „seit 0 Sekunden" erzeugen —
        und wird nicht protokolliert. Nur ein aendernder Wechsel ist ein
        Ereignis.
        """
        with self._lock:
            previous = self._status
            if previous.state is state and previous.detail == detail:
                return previous
            self._status = StackStatus(state=state, since=utc_now(), detail=detail)
            new_status = self._status

        _LOG.info(
            "Stack-Zustand gewechselt",
            extra={
                "stack_state": new_status.state.value,
                "stack_state_previous": previous.state.value,
                "stack_detail": detail,
            },
        )
        return new_status
