"""API-Key-Pruefung des ``apikey``-Modus (ARCHITECTURE §6, §7; Phase 18).

**Der Waechter prueft, die API nie.** ARCHITECTURE §7 „Durchsetzungsort Auth
& Rate-Limit" (Entscheid 2026-07-25): der API-Dienst kennt keine Keys, er
prueft ``client`` nur auf Anwesenheit. Der Grund ist der Cache: eine Antwort
aus :mod:`acoustid_watchdog.cache` erreicht die API nie: laege die Pruefung
dort, waere ausgerechnet der billige Weg der ungeschuetzte.

Modi (``auth.mode``, §6)
------------------------
==========  ==================================================================
``none``    ``client`` wird akzeptiert und **ignoriert**. Der Waechter liest
            ihn nicht einmal — dieses Modul wird gar nicht erst aufgerufen.
            Ob ``client`` fehlt, entscheidet weiterhin die API (Fehler 2).
``apikey``  ``client`` muss ein aktiver Key aus der Tabelle ``api_key``
            sein; optional zusaetzlich einer der bekannten Drittclient-Keys
            (:data:`KNOWN_CLIENT_KEYS`, Schalter
            ``auth.allow_known_client_keys``, default aus).
==========  ==================================================================

Warum ``sha256`` und kein KDF
-----------------------------
Die Keys sind **selbst erzeugte Zufallswerte** (Phase 26 legt sie an, wie
das Admin-Passwort ueber ``secrets``) — keine vom Menschen gewaehlten
Passwoerter. Damit faellt der einzige Grund fuer argon2 & Co. weg: es gibt
kein Woerterbuch, gegen das sich ein 128-Bit-Zufallswert durchprobieren
liesse, und das Bremsen einer Offline-Suche schuetzt nichts, was nicht schon
durch die Entropie geschuetzt waere.

Dagegen stehen zwei harte Kosten, die ein KDF hier haette:

* **Pro Anfrage.** argon2 ist absichtlich speicherhart (Vorgabe: 64 MiB,
  ~50 ms). Beim Admin-Login ist das genau richtig — einmal je Sitzung. Auf
  dem Lookup-Pfad, der laut Rate-Limit 120 Anfragen je Minute und IP
  aushalten soll, waere es eine selbst gebaute Denial-of-Service-Flaeche.
* **Pro Schluessel.** Ein KDF salzt je Eintrag; „welcher Key ist das?" waere
  dann nicht mehr ein Indexzugriff, sondern ein ``verify()`` gegen **jeden**
  gespeicherten Key. Die Spalte ``api_key.key_hash`` ist genau deshalb schon
  in Phase 14 als ``UNIQUE`` angelegt worden.

Verglichen wird trotzdem konstant-zeitig (:func:`secrets.compare_digest`).
Beim Tabellen-Treffer ist das Vorsicht ohne Not — verglichen werden zwei
Hashes, aus deren Laufzeit sich nichts ueber den Key ableiten liesse. Bei
den bekannten Drittclient-Keys steht dagegen wirklich Klartext gegen
Klartext; dort ist es die richtige Vergleichsfunktion.

„Zuletzt benutzt" — gedrosselt
------------------------------
``api_key.last_used_at`` speist die Key-Liste der Admin-UI (Phase 26): sie
beantwortet „wird dieser Key ueberhaupt noch benutzt?". Dafuer genuegt
Minutenaufloesung. Jede Anfrage mit einer Schreibtransaktion auf der
Zustandsdatenbank zu bezahlen, waere dagegen teuer und gegen die Linie der
Phase 14 (die Zustandsdatenbank soll keine Massenschreibvorgaenge tragen —
deshalb hat der Lookup-Cache eine eigene Datei). Geschrieben wird deshalb
hoechstens alle :data:`DEFAULT_TOUCH_INTERVAL_S` Sekunden **je Key**; der
Merker dafuer liegt im Speicher und darf einen Neustart verlieren.

Was hier **nicht** passiert
---------------------------
Kein Ereignis-Log. Eine abgewiesene Anfrage ist im ``apikey``-Modus der
Normalfall (jeder Scanner im Netz erzeugt sie), und der Ringpuffer fasst
5000 Eintraege — ein Nachmittag Grundrauschen wuerde die Betriebshistorie
loeschen. Gezaehlt wird stattdessen (:class:`AuthCounters`, Kennzahlen der
Phase 22); der Einzelfall steht im Containerlog.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from acoustid_watchdog.store import Database, utc_now

__all__ = [
    "DEFAULT_TOUCH_INTERVAL_S",
    "KNOWN_CLIENT_KEYS",
    "ApiKeyAuthenticator",
    "AuthCounters",
    "AuthOutcome",
    "AuthResult",
    "hash_key",
]

_LOG = logging.getLogger(__name__)

#: Die fest einkodierten Application-Keys bekannter Drittclients, die
#: ``auth.allow_known_client_keys`` zusaetzlich zur Tabelle zulaesst
#: (DECISIONS 2026-07-25; Werte aus dem Phase-1-Bericht, Abschnitt
#: „Client-Verhalten"). Sie stehen **oeffentlich** in den Quelltexten von
#: Picard und beets — deshalb ist der Schalter default aus: wer ihn
#: einschaltet, macht seine Instanz fuer jeden benutzbar, der die Keys
#: nachschlaegt. Der Zweck ist ein anderer: einen unveraenderten Picard
#: gegen die eigene Instanz laufen lassen, ohne ihn patchen zu muessen.
KNOWN_CLIENT_KEYS: Final[dict[str, str]] = {
    "v8pQ6oyB": "Picard",
    "1vOwZtEn": "beets",
}

#: Mindestabstand zweier Schreibvorgaenge auf ``last_used_at`` je Key.
#: Bewusst **kein** §6-Schluessel: der Betreiber hat keinen Grund, daran zu
#: drehen (Muster aus DECISIONS 2026-08-01, Punkt 2).
DEFAULT_TOUCH_INTERVAL_S: Final = 60.0


def hash_key(key: str) -> str:
    """Der Hash, unter dem ein Key in ``api_key.key_hash`` steht.

    ``sha256`` ueber die UTF-8-Bytes, hexadezimal — ungesalzen und damit
    nachschlagbar (Modul-Docstring). Dieselbe Funktion benutzt die Admin-UI
    beim Anlegen eines Keys (Phase 26); sie ist die einzige Stelle, an der
    das Verfahren steht.
    """
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class AuthOutcome(StrEnum):
    """Ausgang einer Pruefung — je Wert genau eine Antwort an den Client."""

    #: Key ist gueltig (Tabelle oder Whitelist): Anfrage geht weiter.
    OK = "ok"
    #: ``client`` fehlt oder ist leer -> Fehler 2 / HTTP 400.
    MISSING = "missing"
    #: ``client`` ist unbekannt oder abgeschaltet -> Fehler 4 / HTTP 400.
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class AuthResult:
    """Das Ergebnis samt dem, was ins Log gehoert."""

    outcome: AuthOutcome
    #: ``api_key.id`` bei einem Tabellen-Treffer, sonst ``None``.
    key_id: int | None = None
    #: Sprechender Name: das Label des Keys bzw. der Clientname aus der
    #: Whitelist. Nie der Key selbst.
    label: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome is AuthOutcome.OK


@dataclass(slots=True)
class AuthCounters:
    """Zaehler fuer die Kennzahlen der Phase 22 und fuer die Tests."""

    #: Anfragen, die ueberhaupt geprueft wurden (nur im ``apikey``-Modus).
    checked: int = 0
    accepted: int = 0
    rejected_missing: int = 0
    rejected_invalid: int = 0
    #: Treffer ueber ``auth.allow_known_client_keys`` (Teilmenge von
    #: ``accepted``) — sichtbar, weil das der bewusst unsichere Weg ist.
    accepted_known_client: int = 0

    @property
    def rejected(self) -> int:
        """Alle abgewiesenen Anfragen."""
        return self.rejected_missing + self.rejected_invalid

    def as_dict(self) -> dict[str, int]:
        return {
            "checked": self.checked,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "rejected_missing": self.rejected_missing,
            "rejected_invalid": self.rejected_invalid,
            "accepted_known_client": self.accepted_known_client,
        }


class ApiKeyAuthenticator:
    """Prueft ``client`` gegen ``api_key`` — synchron, wie alles an SQLite."""

    def __init__(
        self,
        db: Database,
        *,
        touch_interval_s: float = DEFAULT_TOUCH_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """
        Args:
            db: Zustandsdatenbank mit der Tabelle ``api_key``.
            touch_interval_s: Mindestabstand zweier ``last_used_at``-
                Schreibvorgaenge je Key.
            clock: Zeitquelle der Drosselung; ``time.monotonic``, weil eine
                Zeitumstellung sie nicht verstellen darf. Tests geben eine
                eigene Uhr mit.
        """
        self.db = db
        self.counters = AuthCounters()
        self._touch_interval_s = touch_interval_s
        self._clock = clock
        self._lock = threading.Lock()
        #: ``api_key.id`` -> Zeitpunkt des letzten Schreibvorgangs. Waechst
        #: mit der Zahl der Keys, nicht mit der der Anfragen.
        self._touched: dict[int, float] = {}

    def check(self, client: str | None, *, allow_known: bool) -> AuthResult:
        """Prueft einen ``client``-Wert.

        Args:
            client: Der Wert des Parameters, oder ``None``/``""``, wenn er
                fehlte.
            allow_known: ``auth.allow_known_client_keys`` — laesst die
                Keys aus :data:`KNOWN_CLIENT_KEYS` zusaetzlich durch.

        Reihenfolge: erst die Tabelle, dann die Whitelist. Nur so bekommt
        ein Key, den der Betreiber selbst angelegt hat, seine Buchfuehrung
        (``last_used_at``) — auch wenn er zufaellig gleich heisst wie ein
        bekannter Drittclient-Key.

        Ein Datenbankfehler ist **kein** stiller Durchlass: er wird zu
        ``INVALID``. Die Alternative — im Zweifel durchlassen — hiesse, dass
        eine kaputte SQLite den ``apikey``-Modus abschaltet.
        """
        self.counters.checked += 1
        if not client:
            self.counters.rejected_missing += 1
            return AuthResult(AuthOutcome.MISSING)

        result = self._from_table(client)
        if result is None and allow_known:
            result = self._from_known_clients(client)

        if result is None:
            self.counters.rejected_invalid += 1
            return AuthResult(AuthOutcome.INVALID)

        self.counters.accepted += 1
        if result.key_id is None:
            self.counters.accepted_known_client += 1
        return result

    # --- Quellen ------------------------------------------------------------

    def _from_table(self, client: str) -> AuthResult | None:
        """Sucht den Key in ``api_key``; ``None`` = kein aktiver Treffer."""
        digest = hash_key(client)
        try:
            with self.db.transaction() as tx:
                row = tx.execute(
                    "SELECT id, label, key_hash, active FROM api_key WHERE key_hash = ?",
                    (digest,),
                ).fetchone()
        except Exception:
            _LOG.exception("API-Key-Pruefung fehlgeschlagen, Anfrage gilt als unautorisiert")
            return None

        # Ein abgeschalteter Key ist von einem unbekannten nicht zu
        # unterscheiden — auch nicht in der Antwort (beide: Fehler 4). Wer
        # einen Key sperrt, will nicht, dass sein Traeger erfaehrt, dass es
        # ihn gab.
        if row is None or not row["active"]:
            return None
        if not secrets.compare_digest(str(row["key_hash"]), digest):  # pragma: no cover
            # Unerreichbar, solange SQLite Gleichheit auf TEXT so versteht
            # wie Python. Steht hier, damit der Vergleich im Code steht und
            # nicht nur in der Abfrage (Modul-Docstring).
            return None

        key_id = int(row["id"])
        self._touch(key_id)
        return AuthResult(AuthOutcome.OK, key_id=key_id, label=str(row["label"]))

    def _from_known_clients(self, client: str) -> AuthResult | None:
        """Prueft die bekannten Drittclient-Keys — konstant-zeitig."""
        for known, name in KNOWN_CLIENT_KEYS.items():
            if secrets.compare_digest(client, known):
                return AuthResult(AuthOutcome.OK, label=name)
        return None

    # --- Buchfuehrung -------------------------------------------------------

    def _touch(self, key_id: int) -> None:
        """Schreibt ``last_used_at`` — hoechstens alle paar Sekunden je Key."""
        now = self._clock()
        with self._lock:
            last = self._touched.get(key_id)
            if last is not None and now - last < self._touch_interval_s:
                return
            self._touched[key_id] = now

        try:
            with self.db.transaction() as tx:
                tx.execute("UPDATE api_key SET last_used_at = ? WHERE id = ?", (utc_now(), key_id))
        except Exception:
            # Buchfuehrung ist Komfort: eine Anfrage darf daran nicht
            # scheitern. Der naechste Versuch kommt nach der Drosselfrist.
            _LOG.warning("last_used_at nicht geschrieben", extra={"key_id": key_id}, exc_info=True)
