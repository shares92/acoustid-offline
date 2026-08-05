"""Lookup-Cache des Waechters — Antworten ohne Array (Phase 17, §5, §8.6).

Das Ziel der Phase in einem Satz: **ein Cache-Hit weckt das Array nie.**
Wer denselben Fingerprint zweimal nachschlaegt, bekommt die zweite Antwort
aus einer SQLite-Datei auf dem SSD-Cache-Pool — ohne Docker-Kontakt, ohne
API-Kontakt, ohne Weckvorgang (Invariante §8.2, baulich: der Cache-Zweig in
:mod:`acoustid_watchdog.main` kommt **vor** ``ensure_ready``).

Ablage
------
**Eine eigene SQLite-Datei** (``lookup-cache.sqlite3``) im Datenverzeichnis,
getrennt von ``watchdog.sqlite3`` (DECISIONS 2026-08-01, Phase 14: die
Zustandsdatenbank soll keine Massenschreibvorgaenge tragen). Gegen einen
Dateicache (eine Datei je Schluessel) sprechen drei Dinge, die eine
Datenbank geschenkt gibt:

* **Groessenbuchhaltung.** ``SUM(size_bytes)`` statt eines zweiten
  Verzeichnis-Ledgers, der nach jedem Absturz mit der Wirklichkeit
  auseinanderlaeuft.
* **Verdraengung in einem Schritt.** Ein ``DELETE … ORDER BY``-Lauf ist
  atomar; ein Dateicache muesste Loeschen und Buchhaltung von Hand
  zusammenhalten.
* **Vollstaendige Invalidierung** (§8.6) ist ein ``DELETE FROM`` in einer
  Transaktion — nicht ein Verzeichnisbaum, der halb geloescht liegen bleibt.

**Ein kaputter Cache darf den Betrieb nie stoeren.** Jeder Zugriff faengt
:class:`sqlite3.Error` ab; im Fehlerfall wird die Datei weggeworfen und neu
angelegt (:meth:`LookupCache._recover`). Schlaegt auch das fehl, schaltet
sich der Cache still ab und der Waechter arbeitet wie vor Phase 17 weiter.
Schlimmstenfalls ist der Cache leer — er haelt nichts, was nicht jederzeit
neu berechnet werden koennte.

Schluessel
----------
``sha256`` ueber Pfad und **alle** Anfrageparameter bis auf ``client`` und
``clientversion``. Bewusst eine *Sperrliste* statt einer Erlaubnisliste: ein
vergessener antwortpraegender Parameter waere bei einer Erlaubnisliste eine
**falsche** Antwort, bei einer Sperrliste nur ein verpasster Treffer. Die
beiden gesperrten Namen sind die einzigen, von denen der Vertrag ausdruecklich
sagt, dass sie die Antwort nicht praegen (docs/api-lookup.md: ``client`` wird
nur auf Anwesenheit geprueft — die Pruefung selbst macht der Waechter, Phase 18
—, ``clientversion`` steht nur im Log).

Normalisierung — was **nicht** passiert, ist hier das Wesentliche:

* **Keine Umsortierung.** Die Paare gehen in Ankunftsreihenfolge in den
  Schluessel (Query-String vor Rumpf — genau die Sicht, die
  ``acoustid_api.params.RequestValues`` aufbaut). Ein Sortieren waere
  hitratenfreundlich, muesste aber beweisen, dass keine Auswertung von der
  Reihenfolge verschiedener Namen abhaengt. Der Preis ist gering: derselbe
  Client schickt seine Parameter immer in derselben Reihenfolge.
* **Keine Gross-/Kleinschreibungs-Anpassung**, weder an Namen noch an
  Werten. Die API ist hier streng (``format=JSON`` ist Fehler 1), eine
  Normalisierung wuerde zwei verschiedene Antworten auf einen Schluessel
  legen.
* **Keine Default-Werte.** ``maxdurationdiff=7`` und ein fehlendes
  ``maxdurationdiff`` bekommen verschiedene Schluessel. Das Nachbilden der
  Vorgabewerte hiesse, die Parametergrammatik der API ein zweites Mal zu
  schreiben — genau die zweite Vertragsquelle, die der Proxy vermeidet.
* **Dekodiert statt roh.** Prozent-Kodierung und ``+`` werden mit derselben
  Bibliothek aufgeloest, mit der die API sie liest (``urllib.parse.parse_qsl``
  ueber Starlettes Query-String bzw. den Formularrumpf). Deshalb treffen
  ``fingerprint=A%2DB`` und ``fingerprint=A-B`` denselben Eintrag — sie
  bekommen ja auch dieselbe Antwort.
* **Die Methode steht nicht im Schluessel.** ``GET`` und ``POST`` sind an
  ``/v2/lookup`` gleichwertig (docs/api-lookup.md); bei gleichen Parametern
  ist die Antwort dieselbe.

Was gecacht wird
----------------
Nur ``GET``/``POST`` auf **``/v2/lookup``**, und nur Antworten mit
**HTTP 200** und **JSON-Rumpf mit ``status: "ok"``**. Damit fallen alle
Fehlerantworten heraus, ohne dass der Waechter die Fehlertabelle der API
kennen muss — und ``format=xml``/``jsonp`` ebenfalls, weil ihr Rumpf kein
JSON ist. Konservativ und ohne zweite Spezifikation.

**``/v2/lookup/batch`` wird bewusst nicht gecacht.** Ein Batch-Rumpf mit bis
zu 100 Eintraegen wiederholt sich praktisch nie identisch; und der
Batch-Vertrag liefert Teilfehler *innerhalb* einer 200er-Antwort (Phase 13)
— ein Cache wuerde einen voruebergehenden Teilfehler festschreiben. Je
Eintrag zu cachen hiesse, die Antwort im Proxy neu zusammenzusetzen; dann
waere der Proxy eine zweite Vertragsquelle.

Was den Cache leert (§8.6)
--------------------------
:meth:`LookupCache.invalidate_all` — aufgerufen (a) vom Proxy-Pfad nach
jeder erfolgreichen lokalen Submission, (b) ab Phase 19 nach jedem
erfolgreichen Delta-Import, (c) als „Cache jetzt leeren" aus der Admin-UI
(Phase 25). Alle drei laufen ueber
:meth:`acoustid_watchdog.service.WatchdogService.invalidate_cache`, damit
jede Leerung im Ereignis-Log steht. Geleert wird **unabhaengig von
``cache.enabled``**: sonst haette ein zwischenzeitlich abgeschalteter Cache
nach dem Wiedereinschalten Eintraege von vor dem Import.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import sqlite3
import threading
import zlib
from collections.abc import AsyncIterator, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Final, Self
from urllib.parse import parse_qsl

from fastapi import Request, Response

from acoustid_watchdog.store import utc_now

__all__ = [
    "BATCH_PATH",
    "CACHEABLE_METHODS",
    "CACHEABLE_PATH",
    "CACHE_FILENAME",
    "EVICTION_WATERMARK",
    "IGNORED_PARAMETERS",
    "MAX_CACHEABLE_BODY_BYTES",
    "SUBMIT_PATH",
    "CacheCounters",
    "CachedResponse",
    "LookupCache",
    "RequestPlan",
    "cache_key",
    "is_cacheable_response",
    "plan_request",
]

_LOG = logging.getLogger(__name__)

#: Dateiname des Caches im Datenverzeichnis (``MMO_DATA_DIR``). Wie bei der
#: Zustandsdatenbank **keine** eigene ``MMO_``-Variable: der Pfad ist eine
#: Eigenschaft des Waechters, kein Bootstrap-Wert (DECISIONS 2026-08-01,
#: Phase 14, Punkt 7).
CACHE_FILENAME: Final = "lookup-cache.sqlite3"

#: Die einzige Route, deren Antworten in den Cache gehen (Modul-Docstring).
CACHEABLE_PATH: Final = "/v2/lookup"

#: Die Route, deren Erfolg den Cache leert (Invariante §8.6).
SUBMIT_PATH: Final = "/v2/submit"

#: Die eine Route, die ihre Parameter im **JSON**-Rumpf traegt statt im
#: Formular (ARCHITECTURE §7, Phase 13). Sie wird nicht gecacht; ihr
#: ``client`` muss die Auth-Pruefung trotzdem finden koennen (Phase 18).
BATCH_PATH: Final = "/v2/lookup/batch"

#: Methoden, die ``/v2/lookup`` beantworten (ARCHITECTURE §7). ``HEAD`` ist
#: bewusst nicht dabei: eine HEAD-Antwort hat keinen Rumpf.
CACHEABLE_METHODS: Final[frozenset[str]] = frozenset({"GET", "POST"})

#: Parameter, die die Antwort **nicht** praegen und deshalb aus dem
#: Schluessel fallen (Modul-Docstring).
IGNORED_PARAMETERS: Final[frozenset[str]] = frozenset({"client", "clientversion"})

#: Groesste Menge, die fuer den Cache gepuffert wird — je Richtung. Es ist
#: dieselbe Grenze, die die API auf den Anfragerumpf zieht
#: (``acoustid_api.main.MAX_BODY_BYTES``): darueber antwortet sie mit
#: Fehler 19/413, eine cachefaehige Antwort kann es also nicht mehr geben.
#: Was groesser ist, laeuft weiter durch den Streaming-Pfad des Proxys — der
#: Waechter haelt nie eine ganze Anfrage im Speicher, nur weil es einen
#: Cache gibt.
MAX_CACHEABLE_BODY_BYTES: Final = 1024 * 1024

#: Bis wohin die Verdraengung raeumt, wenn die Obergrenze ueberschritten ist.
#: Nicht bis exakt auf die Grenze: sonst kostete jede Einlagerung eine
#: Loeschung (und die Datei fragmentierte im Sekundentakt).
EVICTION_WATERMARK: Final = 0.9

#: Zeichensatz des Anfragerumpfs — dieselbe nachsichtige Lesart wie in der
#: API (``acoustid_api.main._ENCODING``), damit beide Seiten dieselben Paare
#: sehen.
_ENCODING: Final = "utf-8"

#: Schema-Schritte der Cache-Datei. Wie in
#: :mod:`acoustid_watchdog.store` ueber ``PRAGMA user_version`` — nur dass
#: hier ein Schemabruch billig waere: der Cache darf jederzeit weggeworfen
#: werden.
_MIGRATIONS: Final[tuple[tuple[str, ...], ...]] = (
    (
        """
        CREATE TABLE entry (
            key          TEXT    PRIMARY KEY,
            created_at   TEXT    NOT NULL,
            last_used_at TEXT    NOT NULL,
            used_seq     INTEGER NOT NULL,
            uses         INTEGER NOT NULL DEFAULT 0,
            size_bytes   INTEGER NOT NULL,
            status_code  INTEGER NOT NULL,
            headers      TEXT    NOT NULL,
            body         BLOB    NOT NULL
        )
        """,
        # Die Verdraengung sucht die am laengsten ungenutzten Eintraege.
        # Sortiert wird nach ``used_seq``, **nicht** nach der Zeitspalte:
        # ISO-Zeitstempel haben Millisekunden, und mehrere Zugriffe in
        # derselben Millisekunde waeren nicht mehr unterscheidbar — der
        # zuletzt benutzte Eintrag koennte dann als aeltester herausfliegen.
        # ``last_used_at`` bleibt daneben stehen, weil es lesbar ist.
        "CREATE INDEX entry_lru_idx ON entry (used_seq)",
    ),
)

_SCHEMA_VERSION: Final = len(_MIGRATIONS)

#: Kopfzeilen, die eine Cache-Antwort **nicht** wiederholt. Alles andere aus
#: der Originalantwort (``content-type``, ``access-control-allow-origin``,
#: eine etwaige ``content-encoding``) wird unveraendert mitgeschrieben und
#: wieder ausgeliefert — die Antwort aus dem Cache ist bytegleich zu der,
#: die die API gegeben hat.
#:
#: * ``date`` beschreibt **diese** Antwort, nicht die von vorgestern.
#: * ``content-length`` setzt Starlette aus dem Rumpf neu; da der Rumpf
#:   bytegleich gespeichert ist, kommt derselbe Wert heraus.
#:
#: Eine Markierung wie ``X-Cache: HIT`` gibt es bewusst nicht: sie waere ein
#: sichtbarer Unterschied zwischen „mit Proxy" und „ohne Proxy" (Begruendung
#: wie in :mod:`acoustid_watchdog.proxy`). Wer wissen will, wie oft der
#: Cache greift, liest die Zaehler (Phase 22).
_VOLATILE_HEADERS: Final[frozenset[str]] = frozenset({"date", "content-length"})


# --- Antwort ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CachedResponse:
    """Eine eingelagerte Antwort: Status, Kopfzeilen, roher Rumpf."""

    status_code: int
    headers: tuple[tuple[str, str], ...]
    body: bytes

    @classmethod
    def capture(cls, response: Response, body: bytes) -> Self:
        """Baut den Cache-Eintrag aus einer weitergeleiteten Antwort."""
        return cls(
            status_code=response.status_code,
            headers=tuple(
                (name.decode("latin-1"), value.decode("latin-1"))
                for name, value in response.raw_headers
                if name.decode("latin-1").lower() not in _VOLATILE_HEADERS
            ),
            body=body,
        )

    def to_response(self) -> Response:
        """Die Antwort, wie der Client sie sieht — bytegleich zum Original."""
        reply = Response(content=self.body, status_code=self.status_code)
        # Wie im Proxy die Kopfzeilen roh setzen: ueber `headers=` wuerde
        # Starlette mehrfach vorkommende Namen zusammenfalten und eigene
        # Vorgaben (`content-type`) dazwischenschieben. Das Ersetzen loescht
        # allerdings auch die `content-length`, die Starlette beim Bau
        # errechnet hat — sie kommt hier wieder dazu, aus demselben,
        # bytegleichen Rumpf und damit mit demselben Wert wie im Original.
        reply.raw_headers = [
            (name.encode("latin-1"), value.encode("latin-1")) for name, value in self.headers
        ] + [(b"content-length", str(len(self.body)).encode("latin-1"))]
        return reply

    @property
    def size_bytes(self) -> int:
        """Grobe Groesse des Eintrags — die Einheit der Buchhaltung."""
        return len(self.body) + sum(len(name) + len(value) for name, value in self.headers)


# --- Schluessel und Vorpruefung ---------------------------------------------


def cache_key(path: str, items: Sequence[tuple[str, str]]) -> str:
    """Der Schluessel einer Anfrage: ``sha256`` ueber Pfad und Parameter.

    Args:
        path: Anfragepfad (``/v2/lookup``); steht mit im Schluessel, damit
            eine spaeter hinzukommende cachefaehige Route nicht kollidiert.
        items: Parameterpaare in Ankunftsreihenfolge — Query-String zuerst,
            dann der Formularrumpf (die Sicht der API). ``client`` und
            ``clientversion`` werden hier entfernt.

    Die Kodierung ist JSON und damit eindeutig: ein Wert, der ``&`` oder
    ``=`` enthaelt, kann keinen anderen Schluessel vortaeuschen.
    """
    relevant = [(name, value) for name, value in items if name not in IGNORED_PARAMETERS]
    payload = json.dumps(
        [_SCHEMA_VERSION, path, relevant], ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode(_ENCODING)).hexdigest()


@dataclass(frozen=True, slots=True)
class RequestPlan:
    """Was der Proxy-Pfad mit dieser Anfrage vorhat.

    Attributes:
        key: Cache-Schluessel, oder ``None``, wenn die Anfrage nicht
            cachefaehig ist (falsche Route, abgeschalteter Cache, zu grosser
            oder unlesbarer Rumpf).
        content: Ersatz fuer ``request.stream()`` beim Weiterleiten. Gesetzt,
            sobald der Rumpf fuer den Schluessel gelesen wurde — dieselben
            Bytes gehen dann weiter, nur eben aus dem Puffer.
        invalidates: Diese Anfrage leert den Cache, wenn sie gelingt
            (``/v2/submit``, Invariante §8.6).
        client: Der Parameter ``client`` der Anfrage — nur gefuellt, wenn
            :func:`plan_request` mit ``need_client=True`` gerufen wurde
            (``apikey``-Modus, Phase 18).
        client_unreadable: Der Rumpf war zu gross, um ``client`` darin zu
            suchen. Dann ist ``client`` nicht aussagekraeftig; die
            Auth-Pruefung antwortet mit Fehler 19/413 — genau dem, was die
            API auf einen solchen Rumpf ohnehin antworten wuerde.
    """

    key: str | None = None
    content: bytes | AsyncIterator[bytes] | None = None
    invalidates: bool = False
    client: str | None = None
    client_unreadable: bool = False


async def plan_request(
    request: Request, *, cache_enabled: bool, need_client: bool = False
) -> RequestPlan:
    """Prueft eine eingehende Anfrage auf Cachefaehigkeit — und liest den Key.

    Liest dafuer, und nur dafuer, den Anfragerumpf einer ``POST``-Anfrage —
    gedeckelt auf :data:`MAX_CACHEABLE_BODY_BYTES`. Reisst der Deckel, geht
    die Anfrage ohne Cache weiter und der schon gelesene Anfang wird dem
    Reststrom vorangestellt; der Waechter puffert also nie mehr als ein
    Megabyte, egal was ein Client schickt.

    Args:
        cache_enabled: ``cache.enabled`` (§6).
        need_client: Der ``apikey``-Modus braucht den ``client``-Parameter
            (Phase 18). Dann wird der Rumpf **auch** auf Routen gelesen, die
            nicht gecacht werden — ``/v2/submit`` und ``/v2/lookup/batch``
            tragen ihren ``client`` schliesslich ebenso im Rumpf. Es bleibt
            bei **einer** Lesung: was hier gepuffert wurde, geht als
            ``content`` weiter.

    Die Lesart ist die der API (``acoustid_api.params.RequestValues``):
    Query-String vor Rumpf, der erste Treffer gewinnt. Nur ``/v2/lookup/
    batch`` traegt seine Huelle als JSON — dort steht ``client`` im
    Objekt, und der Query-String hat trotzdem Vorrang
    (``acoustid_api.params.parse_lookup_batch``).
    """
    path = request.url.path
    invalidates = path == SUBMIT_PATH
    cacheable = cache_enabled and path == CACHEABLE_PATH and request.method in CACHEABLE_METHODS
    if not cacheable and not need_client:
        return RequestPlan(invalidates=invalidates)

    query = parse_qsl(request.url.query, keep_blank_values=True)
    client = _first(query, "client") if need_client else None
    if request.method == "GET":
        # Wie bisher: eine GET-Anfrage hat keinen Rumpf, den diese API
        # auswerten wuerde — Schluessel und Key stehen im Query-String.
        return RequestPlan(
            key=cache_key(path, query) if cacheable else None,
            invalidates=invalidates,
            client=client,
        )

    # **Genau ein** Stromobjekt: ein zweites ``request.stream()`` wuerde
    # Starlette mit „Stream consumed" quittieren. Derselbe Generator wird
    # deshalb weitergereicht, wenn der Rest noch gebraucht wird.
    stream = request.stream()
    body, complete = await _read_capped(stream)
    if not complete:
        # Zu gross fuer den Cache — aber nicht fuer den Betrieb: der
        # gelesene Anfang plus der Rest des Stroms ist wieder die ganze
        # Anfrage. Steht der Key noch aus und nicht schon im Query-String,
        # kann er hier nicht mehr gefunden werden.
        return RequestPlan(
            content=_prepend(body, stream),
            invalidates=invalidates,
            client=client,
            client_unreadable=need_client and client is None,
        )

    raw, oversized = _decompress(body, request.headers.get("content-encoding"))
    if raw is None:
        # Unlesbarer (oder fremd kodierter) Rumpf: kein Schluessel, und der
        # Key kann nur noch aus dem Query-String kommen. Ein ``client``, den
        # die API aus einem kaputten gzip-Rumpf auch nicht laese, fehlt dann
        # eben — Fehler 2 ist die richtige Antwort. Nur wenn der **entpackte**
        # Rumpf ueber der Grenze liegt, ist es derselbe Fall wie oben: die
        # API antwortet darauf mit 19/413, unabhaengig vom Key.
        return RequestPlan(
            content=body,
            invalidates=invalidates,
            client=client,
            client_unreadable=oversized and need_client and client is None,
        )

    if need_client and client is None:
        client = _json_client(raw) if path == BATCH_PATH else _first(_parse_form(raw), "client")

    key = cache_key(path, [*query, *_parse_form(raw)]) if cacheable else None
    return RequestPlan(key=key, content=body, invalidates=invalidates, client=client)


def _first(items: Sequence[tuple[str, str]], name: str) -> str | None:
    """Der erste Wert dieses Parameters — die Lesart von ``req.values``."""
    for key, value in items:
        if key == name:
            return value
    return None


async def _read_capped(stream: AsyncIterator[bytes]) -> tuple[bytes, bool]:
    """Liest den Rumpf bis zur Deckelung; ``False`` = es kam noch mehr."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in stream:
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_CACHEABLE_BODY_BYTES:
            return b"".join(chunks), False
    return b"".join(chunks), True


async def _prepend(head: bytes, rest: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Setzt den schon gelesenen Anfang wieder vor den Reststrom."""
    if head:
        yield head
    async for chunk in rest:
        yield chunk


def _decompress(body: bytes, content_encoding: str | None) -> tuple[bytes | None, bool]:
    """Der Rumpf im Klartext, plus „war er zu gross?".

    ``Content-Encoding: gzip`` wird hier **fuer Schluessel und Key** entpackt
    (pyacoustid und beets schicken so). Weitergeleitet wird trotzdem der
    unveraenderte, gepackte Rumpf — das Entpacken und die 1-MiB-Grenze
    bleiben Sache der API (:mod:`acoustid_watchdog.proxy`). Ohne diesen
    Schritt kaeme der Schluessel aus komprimierten Bytes: zwei gleiche
    Anfragen mit verschiedenen gzip-Einstellungen waeren dann verschiedene
    Eintraege.

    Returns:
        ``(Klartext, zu_gross)``. ``(None, False)`` heisst „nicht lesbar",
        ``(None, True)`` heisst „entpackt ueber der Grenze" — die beiden
        muessen sich unterscheiden lassen, weil die API auf das zweite mit
        19/413 antwortet und auf das erste mit dem Fehler des fehlenden
        Parameters (Phase 18).
    """
    if not content_encoding:
        return body, False
    if content_encoding.strip().lower() != "gzip":
        return None, False
    try:
        raw = gzip.decompress(body)
    except OSError, EOFError, zlib.error:
        # Ein kaputter gzip-Rumpf gilt der API als leer; hier gilt er
        # als nicht cachefaehig — die Antwort darauf ist ein Fehler und
        # wuerde ohnehin nicht eingelagert.
        return None, False
    if len(raw) > MAX_CACHEABLE_BODY_BYTES:
        return None, True
    return raw, False


def _parse_form(raw: bytes) -> list[tuple[str, str]]:
    """Der Formularrumpf als Paare — dieselbe Lesart wie in der API."""
    return parse_qsl(raw.decode(_ENCODING, errors="replace"), keep_blank_values=True)


def _json_client(raw: bytes) -> str | None:
    """``client`` aus der JSON-Huelle von ``/v2/lookup/batch`` (Phase 13).

    Unlesbares JSON, ein nacktes Array, ein ``client``, der keine Zeichenkette
    ist: alles ergibt ``None`` — und damit Fehler 2, den die API auf denselben
    Rumpf ebenfalls gaebe (``parse_lookup_batch``).
    """
    try:
        payload = json.loads(raw)
    except ValueError, UnicodeDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    client = payload.get("client")
    return client if isinstance(client, str) else None


def is_cacheable_response(response: Response, body: bytes) -> bool:
    """Darf diese Antwort eingelagert werden?

    Genau dann, wenn sie **HTTP 200** traegt und ihr Rumpf ein JSON-Objekt
    mit ``status: "ok"`` ist. Der zweite Teil ist die eigentliche Pruefung:
    er laesst Fehlerantworten draussen, ohne dass der Waechter die
    Fehlertabelle der API nachbauen muesste, und schliesst ``format=xml``
    und ``format=jsonp`` gleich mit aus (deren Rumpf ist kein JSON).
    """
    if response.status_code != 200 or not body:
        return False
    try:
        payload = json.loads(body)
    except ValueError, UnicodeDecodeError:
        return False
    return isinstance(payload, dict) and payload.get("status") == "ok"


# --- Ablage -----------------------------------------------------------------


@dataclass(slots=True)
class CacheCounters:
    """Zaehler fuer die Kennzahlen der Phase 22 und fuer die Tests."""

    hits: int = 0
    misses: int = 0
    stores: int = 0
    evictions: int = 0
    invalidations: int = 0
    errors: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "stores": self.stores,
            "evictions": self.evictions,
            "invalidations": self.invalidations,
            "errors": self.errors,
        }


class LookupCache:
    """Die Cache-Datei — eine Verbindung hinter einem Lock, selbstheilend.

    Jede oeffentliche Methode ist gutmuetig: sie meldet Fehler ins Log,
    zaehlt sie mit und gibt einen unschaedlichen Wert zurueck. Ein Cache,
    der eine Anfrage scheitern lassen kann, waere schlechter als gar keiner.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.counters = CacheCounters()
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._total_bytes = 0
        # Monotone Nutzungsnummer statt einer Zeitspalte (siehe `_MIGRATIONS`).
        self._seq = 0
        self._disabled = False

    @classmethod
    def for_data_dir(cls, data_dir: str | Path) -> Self:
        """Der Cache am Vorgabeort innerhalb des Datenverzeichnisses."""
        return cls(Path(data_dir) / CACHE_FILENAME)

    # --- Lebenszyklus -------------------------------------------------------

    def open(self) -> Self:
        """Oeffnet (und migriert) die Datei; scheitert nie nach aussen."""
        with self._lock:
            if self._connection is not None or self._disabled:
                return self
            try:
                self._connect()
            except (OSError, sqlite3.Error) as error:
                _LOG.warning(
                    "Lookup-Cache nicht nutzbar, wird neu angelegt",
                    extra={"cache_path": str(self.path), "error": str(error)},
                )
                if not self._recover():
                    _LOG.error(
                        "Lookup-Cache bleibt abgeschaltet — der Waechter arbeitet ohne ihn",
                        extra={"cache_path": str(self.path)},
                    )
        return self

    def close(self) -> None:
        """Schliesst die Verbindung; ein zweiter Aufruf ist ein No-Op."""
        with self._lock:
            if self._connection is None:
                return
            # Ein Schliessen, das scheitert, aendert nichts: die Verbindung
            # gilt danach so oder so als weg.
            with suppress(sqlite3.Error):
                self._connection.close()
            self._connection = None

    def __enter__(self) -> Self:
        return self.open()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # --- Zugriffe -----------------------------------------------------------

    @property
    def available(self) -> bool:
        """Steht der Cache zur Verfuegung?"""
        return self._connection is not None and not self._disabled

    @property
    def entries(self) -> int:
        """Zahl der eingelagerten Antworten (Diagnose, Phase 22)."""
        with self._lock:
            if not self.available:
                return 0
            try:
                row = self._require().execute("SELECT COUNT(*) FROM entry").fetchone()
            except sqlite3.Error as error:
                self._failed("Cache-Zaehlung", error)
                return 0
            return int(row[0])

    @property
    def total_bytes(self) -> int:
        """Belegung nach der eigenen Buchhaltung."""
        with self._lock:
            return self._total_bytes if self.available else 0

    def get(self, key: str) -> CachedResponse | None:
        """Die eingelagerte Antwort zu diesem Schluessel, wenn es sie gibt.

        Ein Treffer setzt zugleich die Nutzungsmarke — die Verdraengung
        raeumt nach „am laengsten nicht benutzt" (siehe :meth:`put`).
        """
        with self._lock:
            if not self.available:
                return None
            try:
                connection = self._require()
                row = connection.execute(
                    "SELECT status_code, headers, body FROM entry WHERE key = ?", (key,)
                ).fetchone()
                if row is None:
                    self.counters.misses += 1
                    return None
                self._seq += 1
                connection.execute(
                    "UPDATE entry SET last_used_at = ?, used_seq = ?, uses = uses + 1 "
                    "WHERE key = ?",
                    (utc_now(), self._seq, key),
                )
            except sqlite3.Error as error:
                self._failed("Cache-Abfrage", error)
                return None
            self.counters.hits += 1
            return CachedResponse(
                status_code=int(row["status_code"]),
                headers=tuple((name, value) for name, value in json.loads(row["headers"])),
                body=bytes(row["body"]),
            )

    def put(self, key: str, entry: CachedResponse, *, max_size_bytes: int) -> bool:
        """Lagert eine Antwort ein und verdraengt, wenn noetig.

        Args:
            key: Schluessel aus :func:`cache_key`.
            entry: Die einzulagernde Antwort.
            max_size_bytes: Obergrenze aus ``cache.max_size_mb``, bei jeder
                Anfrage frisch gelesen (Muster
                :class:`~acoustid_watchdog.lifecycle.IdleStopper`).

        Returns:
            ``True``, wenn der Eintrag jetzt im Cache liegt.

        **Verdraengung nach LRU.** Ein Lookup-Cache lebt von Wiederholung:
        wer eine Mediathek zweimal durchlaeuft, fragt dieselben Fingerprints
        in derselben Naehe wieder ab. Die Zugriffszeit sagt darueber mehr
        als das Alter (FIFO wuerde gerade die dauernd gebrauchten Eintraege
        herauswerfen) und mehr als ein Trefferzaehler (LFU haelt Eintraege
        fest, die einmal beliebt waren).
        """
        size = entry.size_bytes
        with self._lock:
            if not self.available or size > max_size_bytes:
                # Ein Eintrag, der allein die Grenze sprengt, wuerde beim
                # Aufraeumen sofort wieder herausfliegen.
                return False
            headers = json.dumps([[name, value] for name, value in entry.headers])
            now = utc_now()
            try:
                connection = self._require()
                connection.execute("BEGIN")
                try:
                    previous = connection.execute(
                        "SELECT size_bytes FROM entry WHERE key = ?", (key,)
                    ).fetchone()
                    self._seq += 1
                    connection.execute(
                        """
                        INSERT INTO entry
                            (key, created_at, last_used_at, used_seq, uses, size_bytes,
                             status_code, headers, body)
                        VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?)
                        ON CONFLICT (key) DO UPDATE SET
                            created_at   = excluded.created_at,
                            last_used_at = excluded.last_used_at,
                            used_seq     = excluded.used_seq,
                            size_bytes   = excluded.size_bytes,
                            status_code  = excluded.status_code,
                            headers      = excluded.headers,
                            body         = excluded.body
                        """,
                        (key, now, now, self._seq, size, entry.status_code, headers, entry.body),
                    )
                    total = (
                        self._total_bytes + size - (int(previous["size_bytes"]) if previous else 0)
                    )
                    total, removed = self._evict(connection, total, max_size_bytes)
                except BaseException:
                    connection.rollback()
                    raise
                connection.commit()
                self._total_bytes = total
                if removed:
                    # Ausserhalb der Transaktion: `incremental_vacuum` ist
                    # innerhalb einer wirkungslos.
                    connection.execute("PRAGMA incremental_vacuum")
            except sqlite3.Error as error:
                self._failed("Cache-Einlagerung", error)
                return False
            self.counters.stores += 1
            return True

    def invalidate_all(self) -> int:
        """Leert den Cache vollstaendig (§8.6); liefert die Zahl der Eintraege.

        Aufrufer ist immer
        :meth:`~acoustid_watchdog.service.WatchdogService.invalidate_cache`,
        damit die Leerung auch im Ereignis-Log steht.
        """
        with self._lock:
            if not self.available:
                return 0
            try:
                connection = self._require()
                connection.execute("BEGIN")
                try:
                    removed = int(connection.execute("SELECT COUNT(*) FROM entry").fetchone()[0])
                    connection.execute("DELETE FROM entry")
                except BaseException:
                    connection.rollback()
                    raise
                connection.commit()
                # Der Platz geht an das Dateisystem zurueck, statt als
                # Freiliste in der Datei stehen zu bleiben.
                connection.execute("PRAGMA incremental_vacuum")
            except sqlite3.Error as error:
                self._failed("Cache-Leerung", error)
                return 0
            self._total_bytes = 0
            self.counters.invalidations += 1
            return removed

    # --- Innenleben ---------------------------------------------------------

    def _require(self) -> sqlite3.Connection:
        connection = self._connection
        if connection is None:  # pragma: no cover - `available` prueft vorher
            raise sqlite3.OperationalError("Lookup-Cache ist nicht geoeffnet")
        return connection

    def _connect(self) -> None:
        """Oeffnet die Datei, setzt die PRAGMAs, migriert, zaehlt zusammen."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        # `incremental` muss vor der ersten Tabelle stehen, sonst bleibt es
        # wirkungslos: nur so kann `incremental_vacuum` die Datei nach einer
        # Verdraengung wirklich schrumpfen lassen.
        connection.execute("PRAGMA auto_vacuum = INCREMENTAL")
        connection.execute("PRAGMA journal_mode = WAL")
        # Der Cache ist Wegwerfware: ein verlorener Eintrag nach einem
        # Stromausfall kostet einen Lookup, kein Datum. Dafuer lohnt kein
        # `FULL`.
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA busy_timeout = 5000")
        self._connection = connection
        self._migrate(connection)
        row = connection.execute(
            "SELECT COALESCE(SUM(size_bytes), 0), COALESCE(MAX(used_seq), 0) FROM entry"
        ).fetchone()
        self._total_bytes = int(row[0])
        # Dort weiterzaehlen, wo der letzte Prozess aufgehoert hat — sonst
        # saehen frisch eingelagerte Eintraege aelter aus als die alten.
        self._seq = int(row[1])

    def _migrate(self, connection: sqlite3.Connection) -> None:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version > _SCHEMA_VERSION:
            # Ein Downgrade waere bei der Zustandsdatenbank ein Fehler; hier
            # ist es einer der Faelle, fuer die es `_recover` gibt.
            raise sqlite3.DatabaseError(
                f"Cache-Schema {version} ist neuer als die bekannte Version {_SCHEMA_VERSION}"
            )
        for number, statements in enumerate(_MIGRATIONS[version:], start=version + 1):
            connection.execute("BEGIN")
            for statement in statements:
                connection.execute(statement)
            connection.execute(f"PRAGMA user_version = {number}")
            connection.commit()

    def _evict(
        self, connection: sqlite3.Connection, total: int, max_size_bytes: int
    ) -> tuple[int, int]:
        """Raeumt bis auf :data:`EVICTION_WATERMARK` der Obergrenze herunter.

        Returns:
            Belegung und Zahl der geloeschten Eintraege danach.
        """
        if total <= max_size_bytes:
            return total, 0
        target = int(max_size_bytes * EVICTION_WATERMARK)
        removed = 0
        freed = 0
        for row in connection.execute(
            "SELECT key, size_bytes FROM entry ORDER BY used_seq"
        ).fetchall():
            if total - freed <= target:
                break
            connection.execute("DELETE FROM entry WHERE key = ?", (row["key"],))
            freed += int(row["size_bytes"])
            removed += 1
        self.counters.evictions += removed
        _LOG.info(
            "Lookup-Cache verdraengt",
            extra={
                "removed": removed,
                "freed_bytes": freed,
                "total_bytes": total - freed,
                "max_size_bytes": max_size_bytes,
            },
        )
        return total - freed, removed

    def _failed(self, what: str, error: Exception) -> None:
        """Ein Zugriff ist gescheitert: melden, zaehlen, Datei neu anlegen."""
        self.counters.errors += 1
        _LOG.warning(
            f"{what} fehlgeschlagen, Lookup-Cache wird neu angelegt",
            extra={"cache_path": str(self.path), "error": str(error)},
        )
        self._recover()

    def _recover(self) -> bool:
        """Wirft die Cache-Datei weg und legt sie neu an.

        Der Cache haelt nichts, was nicht neu berechnet werden koennte —
        Wegwerfen ist deshalb die richtige Antwort auf jeden Defekt. Gelingt
        auch das Neuanlegen nicht (Platte voll, Verzeichnis nicht
        beschreibbar), schaltet sich der Cache ab und der Waechter arbeitet
        ohne ihn weiter.
        """
        self.close()
        self._total_bytes = 0
        self._seq = 0
        try:
            for suffix in ("", "-wal", "-shm"):
                Path(f"{self.path}{suffix}").unlink(missing_ok=True)
            self._connect()
        except (OSError, sqlite3.Error) as error:
            _LOG.error(
                "Lookup-Cache liess sich nicht neu anlegen",
                extra={"cache_path": str(self.path), "error": str(error)},
            )
            self.close()
            self._disabled = True
            return False
        return True
