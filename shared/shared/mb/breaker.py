"""Circuit-Breaker vor der MusicBrainz-Verbindung (§5.4).

Der Spiegel ist ein fremder Dienst ohne Zusagen. Faellt er aus, kostet
**jede** Anfrage erst den Verbindungsversuch (``connect_timeout`` 2 s) und
dann die Wartezeit im Pool — bei einem Lookup mit 20 Fingerprints waere das
eine Antwortzeit im zweistelligen Sekundenbereich fuer eine Antwort, die
ohnehin degradiert ist. Der Breaker macht daraus ein sofortiges
:class:`~shared.mb.errors.MbUnavailable`.

Drei Zustaende, wie ueblich:

* **geschlossen** — alles laeuft; Fehler werden in einem gleitenden Fenster
  gezaehlt.
* **offen** — nach :data:`FAILURE_THRESHOLD` Fehlern innerhalb von
  :data:`FAILURE_WINDOW_S` Sekunden; jeder Aufruf scheitert sofort, bis
  :data:`COOLDOWN_S` Sekunden vergangen sind.
* **halb offen** — nach der Abkuehlzeit darf **ein** Aufruf probieren;
  gelingt er, ist der Breaker wieder geschlossen, sonst wieder offen.

Die Schwellen sind bewusst **dokumentierte Konstanten und keine
Config-Schluessel**: sie beschreiben das Verhalten gegenueber einem
ausgefallenen Nachbardienst, nicht eine Betreiber-Entscheidung. Wer sie
verstellen koennen muesste, haette ein anderes Problem.

Der Breaker ist **thread-sicher** (der API-Dienst arbeitet den synchronen
Teil im Threadpool ab) und benutzt eine monotone Uhr — eine Zeitumstellung
darf ihn nicht oeffnen.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Final

__all__ = [
    "COOLDOWN_S",
    "FAILURE_THRESHOLD",
    "FAILURE_WINDOW_S",
    "CircuitBreaker",
]

#: So viele Fehler innerhalb des Fensters oeffnen den Breaker.
FAILURE_THRESHOLD: Final = 3

#: Laenge des gleitenden Fehlerfensters in Sekunden.
FAILURE_WINDOW_S: Final = 30.0

#: So lange bleibt der Breaker offen, bevor ein Versuch erlaubt ist.
COOLDOWN_S: Final = 30.0


class CircuitBreaker:
    """Zaehlt Fehler und sperrt voruebergehend.

    Args:
        threshold: Fehler im Fenster, ab denen gesperrt wird.
        window_s: Laenge des gleitenden Fensters.
        cooldown_s: Sperrzeit nach dem Oeffnen.
        clock: Monotone Uhr; nur fuer Tests austauschbar.
    """

    def __init__(
        self,
        *,
        threshold: int = FAILURE_THRESHOLD,
        window_s: float = FAILURE_WINDOW_S,
        cooldown_s: float = COOLDOWN_S,
        clock: object = None,
    ) -> None:
        self.threshold = threshold
        self.window_s = window_s
        self.cooldown_s = cooldown_s
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._failures: deque[float] = deque()
        self._opened_at: float | None = None
        #: Zaehlt, wie oft der Breaker geoeffnet hat (Log/Metrik).
        self.trips = 0

    @property
    def is_open(self) -> bool:
        """``True``, solange der Breaker sperrt (ohne Halb-offen-Versuch)."""
        with self._lock:
            return self._opened_at is not None and not self._cooldown_over()

    def allows(self) -> bool:
        """Darf der naechste Aufruf durch?

        Nach Ablauf der Abkuehlzeit gibt genau dieser Aufruf ``True`` zurueck
        (halb offen) — der Zustand bleibt aber „offen", bis ein Erfolg
        gemeldet wird.
        """
        with self._lock:
            if self._opened_at is None:
                return True
            return self._cooldown_over()

    def record_success(self) -> None:
        """Ein Aufruf hat funktioniert: Fenster leeren, Sperre aufheben."""
        with self._lock:
            self._failures.clear()
            self._opened_at = None

    def record_failure(self) -> bool:
        """Ein Aufruf ist gescheitert.

        Returns:
            ``True``, wenn der Breaker durch diesen Fehler **neu** geoeffnet
            hat — nur dann soll der Aufrufer laut loggen.
        """
        with self._lock:
            now = self._now()
            if self._opened_at is not None:
                # Der Halb-offen-Versuch ist gescheitert: Sperre erneuern.
                self._opened_at = now
                return False
            self._failures.append(now)
            self._forget_old(now)
            if len(self._failures) < self.threshold:
                return False
            self._opened_at = now
            self._failures.clear()
            self.trips += 1
            return True

    def reset(self) -> None:
        """Zurueck in den Ausgangszustand (Tests, manueller Eingriff)."""
        with self._lock:
            self._failures.clear()
            self._opened_at = None

    def _now(self) -> float:
        return float(self._clock())  # type: ignore[operator]

    def _cooldown_over(self) -> bool:
        assert self._opened_at is not None
        return self._now() - self._opened_at >= self.cooldown_s

    def _forget_old(self, now: float) -> None:
        while self._failures and now - self._failures[0] > self.window_s:
            self._failures.popleft()
