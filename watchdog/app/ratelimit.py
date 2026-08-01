"""IP-Rate-Limit am Proxy (ARCHITECTURE §6, §7; Phase 18).

``ratelimit.per_ip_per_min`` (§6, Default 120) begrenzt, wie viele
``/v2/``-Anfragen **eine Client-IP** je Minute stellen darf. Ueberschreitung
beantwortet der Waechter selbst: ``429`` mit ``Retry-After`` (§7
„Fehlerverhalten").

**Aktiv in beiden Auth-Modi**, und **vor** der Key-Pruefung: das Limit ist
der Schutz, der auch dann noch traegt, wenn gar nichts anderes greift — auch
gegen einen Absender, der nur unbrauchbare Keys schickt. Und wie die
Key-Pruefung liegt es beim Waechter, nicht bei der API: ein Cache-Treffer
erreicht die API nie (:mod:`acoustid_watchdog.cache`), waere dort also
ungebremst.

Bauart: gleitendes Fenster
--------------------------
Je IP eine Liste der Zeitpunkte der letzten Anfragen; erlaubt ist, wer
innerhalb der letzten 60 Sekunden weniger als ``per_ip_per_min`` Eintraege
hat. Die Alternative — ein **festes** Minutenfenster mit einem Zaehler — ist
billiger, laesst aber an der Fenstergrenze das Doppelte durch (120 in der
letzten Sekunde der einen Minute, 120 in der ersten der naechsten). Genau
der Burst, gegen den das Limit steht, kaeme damit durch.

Der Preis ist eine Liste je aktiver IP, die nie laenger wird als das Limit
selbst (aeltere Eintraege interessieren niemanden mehr): bei Vorgabewerten
120 Zeitstempel, wenige Kilobyte. Dafuer ist auch ``Retry-After`` **exakt**
statt geraten — es ist die Zeit, bis der aelteste noch zaehlende Eintrag aus
dem Fenster faellt.

Speicherbegrenzung
------------------
Zwei Riegel, beide noetig:

* **Deckel** (:data:`DEFAULT_MAX_TRACKED_IPS`): mehr IPs werden nicht
  gefuehrt; die am laengsten nicht gesehene faellt heraus (LRU). Das begrenzt
  den Speicher hart, auch unter einer Flut gefaelschter Absender.
* **Aufraeumen** (:data:`SWEEP_INTERVAL_S`): einmal je Minute fliegt raus,
  wessen letzte Anfrage aelter als das Fenster ist. Ohne diesen Schritt
  blieben Feierabend-IPs bis zum Deckel liegen und gaeben den Speicher nie
  zurueck.

Ein herausgefallener Eintrag verschenkt hoechstens ein Kontingent — die
betreffende IP faengt bei null an. Das ist die harmlose Richtung: der
Deckel ist ein Speicherschutz, kein zweites Limit.

Welche IP
---------
Die **direkte** Gegenstelle (``request.client.host``). ``X-Forwarded-For``
wird bewusst **nicht** ausgewertet: der Kopf ist frei waehlbar, und ohne
eine Liste vertrauenswuerdiger Proxys wuerde das Limit damit wertlos (jede
Anfrage koennte sich eine eigene IP geben). Im dokumentierten Betrieb
(ARCHITECTURE §3: der Waechter ist der einzige veroeffentlichte Port) ist
die direkte Gegenstelle die richtige Antwort. Fuer den Betrieb hinter einem
TLS-Proxy braucht es eine bewusste Entscheidung ueber vertrauenswuerdige
Absender — sie steht als Klaerungspunkt offen und wird hier nicht geraten.
"""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

__all__ = [
    "DEFAULT_MAX_TRACKED_IPS",
    "SWEEP_INTERVAL_S",
    "UNKNOWN_CLIENT",
    "WINDOW_S",
    "IpRateLimiter",
    "RateLimitCounters",
    "RateLimitDecision",
]

#: Laenge des gleitenden Fensters. ``per_ip_per_min`` sagt „je Minute" —
#: mehr Freiheit gibt es hier nicht zu gewinnen.
WINDOW_S: Final = 60.0

#: Wie viele IPs hoechstens gefuehrt werden (Modul-Docstring). 2048 ist fuer
#: eine Privatinstanz reichlich (im LAN sind es einstellige Zahlen) und
#: kostet im schlimmsten Fall wenige Megabyte.
DEFAULT_MAX_TRACKED_IPS: Final = 2048

#: Abstand zweier Aufraeumlaeufe ueber alle gefuehrten IPs.
SWEEP_INTERVAL_S: Final = 60.0

#: Ersatzschluessel, wenn die Anfrage keine Gegenstelle nennt (bei ASGI
#: moeglich, z. B. ueber einen Unix-Socket). Alle solchen Anfragen teilen
#: sich dann **ein** Kontingent — begrenzt statt unbegrenzt, denn ein
#: Absender ohne Adresse ist kein Grund, das Limit fallenzulassen.
UNKNOWN_CLIENT: Final = "-"


@dataclass(slots=True)
class RateLimitCounters:
    """Zaehler fuer die Kennzahlen der Phase 22 und fuer die Tests."""

    checked: int = 0
    allowed: int = 0
    rejected: int = 0
    #: IPs, die der Deckel oder das Aufraeumen entfernt hat.
    evicted: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "checked": self.checked,
            "allowed": self.allowed,
            "rejected": self.rejected,
            "evicted": self.evicted,
        }


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    """Darf diese Anfrage durch — und wenn nein, wann wieder?"""

    allowed: bool
    #: Sekunden bis zum naechsten freien Platz; nur bei ``allowed=False``
    #: gesetzt und immer mindestens 1 (ein ``Retry-After: 0`` waere eine
    #: Einladung, sofort wiederzukommen).
    retry_after_s: int = 0

    @property
    def rejected(self) -> bool:
        return not self.allowed


class IpRateLimiter:
    """Gleitendes Minutenfenster je IP — im Speicher, threadsicher.

    Threadsicher, weil der Proxy-Pfad zwar ``async`` ist, aber Starlette
    Anfragen ueber einen Threadpool bedienen kann und der Idle-Stopper
    nebenher laeuft. Die Arbeit unter dem Lock ist O(1) bis O(Limit) auf
    einer Liste — sie geht deshalb **nicht** ueber ``run_in_threadpool``:
    das Umschalten wuerde mehr kosten als die Rechnung selbst.
    """

    def __init__(
        self,
        *,
        max_tracked_ips: int = DEFAULT_MAX_TRACKED_IPS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """
        Args:
            max_tracked_ips: Deckel der gefuehrten IPs (Modul-Docstring).
            clock: Zeitquelle; ``time.monotonic``, weil eine Zeitumstellung
                oder ein NTP-Sprung kein Kontingent verschenken darf. Tests
                geben eine eigene Uhr mit.
        """
        self.counters = RateLimitCounters()
        self._max_tracked_ips = max_tracked_ips
        self._clock = clock
        self._lock = threading.Lock()
        #: IP -> Zeitpunkte im Fenster, aelteste zuerst. ``OrderedDict``,
        #: weil der Deckel die am laengsten nicht gesehene IP entfernt.
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()
        self._last_sweep = clock()

    @property
    def tracked_ips(self) -> int:
        """Wie viele IPs gerade gefuehrt werden (Tests, Kennzahlen)."""
        with self._lock:
            return len(self._hits)

    def check(self, ip: str, *, limit: int) -> RateLimitDecision:
        """Bucht eine Anfrage und sagt, ob sie durchdarf.

        Args:
            ip: Die Client-IP (:data:`UNKNOWN_CLIENT`, wenn keine bekannt).
            limit: ``ratelimit.per_ip_per_min`` — **bei jeder Anfrage frisch
                gelesen**, damit eine Aenderung in der Admin-UI sofort
                greift. Ein kleiner gewordenes Limit wirkt dadurch auch auf
                Eintraege, die noch im Fenster liegen.

        Returns:
            Die Entscheidung; bei Ablehnung mit der Wartezeit fuer
            ``Retry-After``.

        Eine abgelehnte Anfrage wird **nicht** mitgezaehlt: sonst haelt sich
        ein Client, der stur weiter anfragt, sein eigenes Fenster dauerhaft
        voll und kaeme nie wieder herein. Das Limit soll bremsen, nicht
        aussperren.
        """
        now = self._clock()
        with self._lock:
            self._sweep(now)
            hits = self._hits.get(ip)
            if hits is None:
                hits = deque()
                self._hits[ip] = hits
            self._hits.move_to_end(ip)

            cutoff = now - WINDOW_S
            while hits and hits[0] <= cutoff:
                hits.popleft()

            self.counters.checked += 1
            if len(hits) >= limit:
                # Frei wird der Platz, wenn der aelteste noch zaehlende
                # Eintrag aus dem Fenster faellt. Bei einem verkleinerten
                # Limit ist das nicht der erste der Liste, sondern der
                # ``len - limit``-te.
                oldest_relevant = hits[len(hits) - limit]
                wait_s = max(1, math.ceil(oldest_relevant + WINDOW_S - now))
                self.counters.rejected += 1
                return RateLimitDecision(allowed=False, retry_after_s=wait_s)

            hits.append(now)
            self._evict()
            self.counters.allowed += 1
            return RateLimitDecision(allowed=True)

    # --- Speicherhaushalt ---------------------------------------------------

    def _sweep(self, now: float) -> None:
        """Wirft IPs weg, deren letzte Anfrage aus dem Fenster gefallen ist.

        Laeuft hoechstens alle :data:`SWEEP_INTERVAL_S` Sekunden und geht
        dann einmal ueber die (gedeckelte) Tabelle — im Betrieb Mikrosekunden.
        """
        if now - self._last_sweep < SWEEP_INTERVAL_S:
            return
        self._last_sweep = now
        cutoff = now - WINDOW_S
        stale = [ip for ip, hits in self._hits.items() if not hits or hits[-1] <= cutoff]
        for ip in stale:
            del self._hits[ip]
        self.counters.evicted += len(stale)

    def _evict(self) -> None:
        """Haelt den Deckel ein: die am laengsten nicht gesehene IP fliegt."""
        while len(self._hits) > self._max_tracked_ips:
            self._hits.popitem(last=False)
            self.counters.evicted += 1
