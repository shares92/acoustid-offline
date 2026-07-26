"""Draht-Format des acoustid-index: msgpack, Kurzfeldnamen, Fehlerabbildung.

Dieses Modul kennt das Protokoll, aber kein HTTP: es baut aus Aufrufen
:class:`WireRequest`-Werte (Methode, Pfad, Rumpf) und liest aus
``(Status, Bytes)`` wieder Python-Objekte. Damit ist der komplette
Protokollteil ohne laufenden Server testbar — und ein spaeterer
Async-Client braucht nur eine andere Transportschicht, keine zweite
Protokollimplementierung.

**Content-Type-Falle.** Ohne ``Content-Type``-Header nimmt der Server
**msgpack** an; ein JSON-Rumpf ohne Header endet in HTTP 400
``InvalidFormat``. Umgekehrt steuert ``Accept`` das Antwortformat, und
ohne ``Accept`` antwortet er ebenfalls msgpack. Wir setzen darum **immer
beide Header explizit** (:data:`HEADERS`) und verlassen uns nie auf
Defaults.

**Kurzfeldnamen.** Antworten des Servers benutzen ausschliesslich
Ein-Buchstaben-Felder. Bei Anfragen akzeptiert er beides; wir schicken die
Kurzform — sie ist kompakter und deckungsgleich mit dem, was zurueckkommt.
Empirisch gegen das gepinnte Image geprueft (Phase 5)::

    PUT    /:index      {e?, g?}                 -> {v, r, g}
    GET    /:index                               -> {v, m, s{…}}
    POST   /:index/_update  {c[{i{i,h}}|{d{i}}], m?, e?} -> {v}
    POST   /:index/_search  {q, t, l}            -> {r[{i, s}]}
    DELETE /:index/:id                           -> {}
    DELETE /:index                               -> {d}
    Fehler (alle Routen)                         -> {e: "<Kennung>"}

Ausnahme von der Kurzform: die **Statistik** unter ``s`` benutzt lange
Feldnamen (``num_docs`` …) — nicht vereinheitlicht, sondern so belassen,
wie der Server sie liefert.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any, Final

import msgpack

from shared.fpindex.errors import (
    FpIndexAlreadyExistsError,
    FpIndexBadRequestError,
    FpIndexConflictError,
    FpIndexNotFoundError,
    FpIndexNotReadyError,
    FpIndexProtocolError,
    FpIndexSearchTimeoutError,
    FpIndexServerError,
    FpIndexStatusError,
    FpIndexVersionMismatchError,
)
from shared.fpindex.query import UINT32_MASK

__all__ = [
    "CONTENT_TYPE",
    "HEADERS",
    "MAX_BODY_BYTES",
    "MAX_SEARCH_LIMIT",
    "MAX_SEARCH_TIMEOUT_MS",
    "RECOMMENDED_BATCH_SIZE",
    "Change",
    "Delete",
    "IndexInfo",
    "IndexState",
    "Insert",
    "SearchResult",
    "WireRequest",
    "decode_body",
    "delete_doc_request",
    "delete_index_request",
    "get_index_request",
    "index_health_request",
    "metrics_request",
    "parse_index_info",
    "parse_index_state",
    "parse_search",
    "parse_version",
    "put_index_request",
    "search_request",
    "server_health_request",
    "status_error",
    "update_request",
    "valid_index_name",
]

#: Wire-Format beider Richtungen.
CONTENT_TYPE: Final = "application/vnd.msgpack"

#: Immer mitschicken — sonst greift die Content-Type-Falle.
HEADERS: Final[Mapping[str, str]] = {"Content-Type": CONTENT_TYPE, "Accept": CONTENT_TYPE}

#: Harte Rumpfgrenze des Servers (``max_body_size``).
MAX_BODY_BYTES: Final = 16 * 1024 * 1024

#: Empfohlene Batchgroesse fuer ``_update`` (offizieller Consumer, benchmark.py).
RECOMMENDED_BATCH_SIZE: Final = 1000

#: Der Server kappt ``limit`` auf diesen Wert (empirisch: 250 -> 100).
MAX_SEARCH_LIMIT: Final = 100

#: Dokumentierte Obergrenze von ``timeout`` in Millisekunden.
MAX_SEARCH_TIMEOUT_MS: Final = 10_000

#: Groesste Dokument-ID, die der Server annimmt. **u32, nicht u64** —
#: empirisch gegen das gepinnte Image geprueft (Phase 11): 2^31 und 2^32-1
#: werden angenommen und unveraendert wiedergefunden, ab 2^32 antwortet der
#: Server mit HTTP 400 ``IntegerOverflow``. Kein stiller Ueberlauf, aber auch
#: kein Platz oberhalb von u32: der reservierte Bereich fuer lokale
#: Einreichungen liegt deshalb in [2^31, 2^32-1] (docs/api-submit.md).
_MAX_DOC_ID: Final = UINT32_MASK
_MAX_HASH: Final = UINT32_MASK

# Zulaessige Indexnamen (empirisch: Punkt, Leerzeichen, fuehrender
# Unterstrich und Schraegstrich werden mit 400 abgelehnt).
_NAME_START = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
_NAME_REST = _NAME_START | {"_", "-"}


# --- Datenmodelle ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Insert:
    """Ein Dokument anlegen oder ersetzen.

    ``doc_id`` ist bei uns die ``fingerprint.id`` aus Postgres, ``hashes``
    das Ergebnis von :func:`shared.fpindex.extract_query`.
    """

    doc_id: int
    hashes: Sequence[int]


@dataclass(frozen=True, slots=True)
class Delete:
    """Ein Dokument loeschen (Tombstone)."""

    doc_id: int


#: Ein Eintrag im ``_update``-Batch.
Change = Insert | Delete


@dataclass(frozen=True, slots=True)
class SearchResult:
    """Ein Kandidat aus ``_search``.

    ``score`` ist **kein** AcoustID-Score, sondern die Anzahl der
    Query-Hashes, die im Dokument vorkommen (0…len(query)). Er dient nur der
    Vorsortierung; der echte Score entsteht im Rescoring gegen den
    Vollvektor (ARCHITECTURE §5.3).
    """

    doc_id: int
    score: int


@dataclass(frozen=True, slots=True)
class IndexState:
    """Antwort auf ``PUT /:index``."""

    version: int
    ready: bool
    generation: int


@dataclass(frozen=True, slots=True)
class IndexInfo:
    """Antwort auf ``GET /:index`` — Version, Metadaten, Statistik."""

    version: int
    metadata: Mapping[str, str] = field(default_factory=dict)
    stats: Mapping[str, int] = field(default_factory=dict)

    @property
    def num_docs(self) -> int | None:
        """Dokumentzahl laut Statistik (Tombstones zaehlen mit)."""
        return self.stats.get("num_docs")


@dataclass(frozen=True, slots=True)
class WireRequest:
    """Eine fertig kodierte Anfrage — noch ohne Transport."""

    method: str
    path: str
    body: bytes | None = None


# --- Kodierung -------------------------------------------------------------


def valid_index_name(name: str) -> bool:
    """Prueft den Indexnamen gegen die Regeln des Servers.

    Erlaubt sind Buchstaben, Ziffern, ``_`` und ``-``; das erste Zeichen muss
    ein Buchstabe oder eine Ziffer sein. Alles andere quittiert der Server
    mit HTTP 400 (``InvalidIndexName`` bzw. ``InvalidCharacter``) — den
    Fehlgriff faengt der Client lieber vor dem Absenden ab.
    """
    if not name:
        return False
    return name[0] in _NAME_START and all(char in _NAME_REST for char in name[1:])


def _pack(payload: Mapping[str, Any]) -> bytes:
    body: bytes = msgpack.packb(payload, use_bin_type=True)
    if len(body) > MAX_BODY_BYTES:
        raise ValueError(
            f"Rumpf ist {len(body)} Byte gross, der Index nimmt hoechstens "
            f"{MAX_BODY_BYTES} Byte an — Batch kleiner schneiden "
            f"(empfohlen: {RECOMMENDED_BATCH_SIZE} Changes)"
        )
    return body


def put_index_request(
    index: str,
    *,
    expect_does_not_exist: bool = False,
    generation: int | None = None,
) -> WireRequest:
    """``PUT /:index`` — Index anlegen (idempotent, solange ohne Erwartungen)."""
    payload: dict[str, Any] = {}
    if expect_does_not_exist:
        payload["e"] = True
    if generation is not None:
        payload["g"] = generation
    return WireRequest("PUT", f"/{index}", _pack(payload))


def get_index_request(index: str) -> WireRequest:
    """``GET /:index`` — Version, Metadaten und Statistik."""
    return WireRequest("GET", f"/{index}")


def index_health_request(index: str) -> WireRequest:
    """``GET /:index/_health`` — prueft nur, ob der Index existiert."""
    return WireRequest("GET", f"/{index}/_health")


def server_health_request() -> WireRequest:
    """``GET /_health`` — reine Lebendpruefung, prueft inhaltlich nichts."""
    return WireRequest("GET", "/_health")


def metrics_request() -> WireRequest:
    """``GET /_metrics`` — Prometheus-Text (die einzige Nicht-msgpack-Antwort)."""
    return WireRequest("GET", "/_metrics")


def delete_index_request(index: str) -> WireRequest:
    """``DELETE /:index`` — kompletter Index weg (es gibt kein Truncate)."""
    return WireRequest("DELETE", f"/{index}")


def delete_doc_request(index: str, doc_id: int) -> WireRequest:
    """``DELETE /:index/:id`` — einzelnes Dokument (Tombstone)."""
    _check_doc_id(doc_id)
    return WireRequest("DELETE", f"/{index}/{doc_id}")


def update_request(
    index: str,
    changes: Iterable[Change],
    *,
    metadata: Mapping[str, str] | None = None,
    expected_version: int | None = None,
) -> WireRequest:
    """``POST /:index/_update`` — ein atomarer Batch aus Inserts und Deletes.

    Bei doppelten IDs im selben Batch gewinnt der **letzte** Eintrag
    (empirisch bestaetigt).
    """
    encoded: list[dict[str, Any]] = []
    for change in changes:
        match change:
            case Insert(doc_id=doc_id, hashes=hashes):
                _check_doc_id(doc_id)
                encoded.append({"i": {"i": doc_id, "h": _check_hashes(doc_id, hashes)}})
            case Delete(doc_id=doc_id):
                _check_doc_id(doc_id)
                encoded.append({"d": {"i": doc_id}})
            case _:
                raise TypeError(f"unbekannter Change-Typ: {type(change).__name__}")

    payload: dict[str, Any] = {"c": encoded}
    if metadata is not None:
        payload["m"] = dict(metadata)
    if expected_version is not None:
        payload["e"] = expected_version
    return WireRequest("POST", f"/{index}/_update", _pack(payload))


def search_request(
    index: str,
    query: Sequence[int],
    *,
    timeout_ms: int,
    limit: int,
) -> WireRequest:
    """``POST /:index/_search`` — Kandidatensuche.

    ``query`` muss dedupliziert sein (Duplikate zaehlen im Score doppelt);
    :func:`shared.fpindex.extract_query` liefert genau das.
    """
    if not 1 <= timeout_ms <= MAX_SEARCH_TIMEOUT_MS:
        # Der Server nimmt groessere Werte kommentarlos an und deckelt still;
        # eine stille Deckelung ist schlimmer als eine klare Absage.
        raise ValueError(
            f"timeout_ms muss zwischen 1 und {MAX_SEARCH_TIMEOUT_MS} liegen, war {timeout_ms}"
        )
    if not 1 <= limit <= MAX_SEARCH_LIMIT:
        raise ValueError(f"limit muss zwischen 1 und {MAX_SEARCH_LIMIT} liegen, war {limit}")
    payload = {"q": _check_hashes(None, query), "t": timeout_ms, "l": limit}
    return WireRequest("POST", f"/{index}/_search", _pack(payload))


def _check_doc_id(doc_id: int) -> int:
    if not 0 <= doc_id <= _MAX_DOC_ID:
        raise ValueError(
            f"Dokument-ID {doc_id} liegt ausserhalb von u32 — der Server quittiert "
            "das mit HTTP 400 IntegerOverflow fuer den ganzen Batch"
        )
    return doc_id


def _check_hashes(doc_id: int | None, hashes: Sequence[int]) -> list[int]:
    """Sichert den u32-Bereich zu, bevor der Server mit 400 antwortet.

    Der Index kennt nur u32; negative Werte (signed int32 aus Postgres) und
    Werte >= 2**32 quittiert er mit ``IntegerOverflow`` fuer den **ganzen**
    Batch — hier faellt stattdessen der schuldige Wert auf.
    """
    values = list(hashes)
    for value in values:
        if not 0 <= value <= _MAX_HASH:
            where = "Query" if doc_id is None else f"Dokument {doc_id}"
            raise ValueError(
                f"{where}: Hash {value} liegt ausserhalb von u32 — "
                "Vollvektoren sind signed int32 und muessen erst durch "
                "extract_query()"
            )
    return values


# --- Dekodierung -----------------------------------------------------------


def decode_body(content: bytes) -> Any:
    """msgpack-Rumpf lesen; leerer Rumpf wird zu ``None``."""
    if not content:
        return None
    try:
        return msgpack.unpackb(content, raw=False)
    except Exception as exc:  # msgpack wirft verschiedene Typen
        raise FpIndexProtocolError(
            f"Antwort ist kein gueltiges msgpack ({len(content)} Byte): {exc}"
        ) from exc


def _mapping(payload: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise FpIndexProtocolError(f"{what}: Objekt erwartet, kam {type(payload).__name__}")
    return payload


def _int(payload: Mapping[str, Any], key: str, what: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise FpIndexProtocolError(f"{what}: Feld {key!r} fehlt oder ist keine Ganzzahl")
    return value


def parse_index_state(payload: Any) -> IndexState:
    """``{v, r, g}`` -> :class:`IndexState`."""
    body = _mapping(payload, "PUT /:index")
    ready = body.get("r")
    return IndexState(
        version=_int(body, "v", "PUT /:index"),
        ready=bool(ready),
        generation=_int(body, "g", "PUT /:index"),
    )


def parse_index_info(payload: Any) -> IndexInfo:
    """``{v, m, s}`` -> :class:`IndexInfo`."""
    body = _mapping(payload, "GET /:index")
    metadata = body.get("m") or {}
    stats = body.get("s") or {}
    return IndexInfo(
        version=_int(body, "v", "GET /:index"),
        metadata=dict(_mapping(metadata, "GET /:index (m)")),
        stats=dict(_mapping(stats, "GET /:index (s)")),
    )


def parse_version(payload: Any) -> int:
    """``{v}`` -> neue Indexversion nach ``_update``."""
    return _int(_mapping(payload, "POST /:index/_update"), "v", "POST /:index/_update")


def parse_search(payload: Any) -> list[SearchResult]:
    """``{r: [{i, s}]}`` -> Kandidatenliste, absteigend nach Score."""
    body = _mapping(payload, "POST /:index/_search")
    results = body.get("r")
    if results is None:
        return []
    if not isinstance(results, list):
        raise FpIndexProtocolError("POST /:index/_search: Feld 'r' ist keine Liste")
    out: list[SearchResult] = []
    for item in results:
        hit = _mapping(item, "POST /:index/_search (r[])")
        out.append(
            SearchResult(
                doc_id=_int(hit, "i", "Suchtreffer"),
                score=_int(hit, "s", "Suchtreffer"),
            )
        )
    return out


def status_error(status_code: int, content: bytes) -> FpIndexStatusError:
    """Baut aus einer Fehlerantwort die passende Ausnahme.

    Der Rumpf ist ``{"e": "<Kennung>"}`` — oder leer: ``GET
    /:index/_health`` antwortet bei unbekanntem Index mit einer nackten 404.
    """
    error_code: str | None = None
    try:
        payload = decode_body(content)
    except FpIndexProtocolError:
        payload = None
    if isinstance(payload, Mapping):
        raw = payload.get("e")
        if isinstance(raw, str):
            error_code = raw

    label = error_code or "ohne Fehlerkennung"
    message = f"acoustid-index antwortete mit HTTP {status_code} ({label})"

    if status_code == HTTPStatus.NOT_FOUND:
        return FpIndexNotFoundError(message, status_code=status_code, error_code=error_code)
    if status_code == HTTPStatus.CONFLICT:
        if error_code == "VersionMismatch":
            return FpIndexVersionMismatchError(
                message, status_code=status_code, error_code=error_code
            )
        if error_code and error_code.endswith("IndexAlreadyExists"):
            return FpIndexAlreadyExistsError(
                message, status_code=status_code, error_code=error_code
            )
        return FpIndexConflictError(message, status_code=status_code, error_code=error_code)
    if status_code == HTTPStatus.SERVICE_UNAVAILABLE:
        return FpIndexNotReadyError(message, status_code=status_code, error_code=error_code)
    if status_code >= HTTPStatus.INTERNAL_SERVER_ERROR:
        if error_code == "Timeout":
            return FpIndexSearchTimeoutError(
                f"Suchfrist des Index abgelaufen (HTTP {status_code}, kein Teilergebnis)",
                status_code=status_code,
                error_code=error_code,
            )
        return FpIndexServerError(message, status_code=status_code, error_code=error_code)
    if status_code >= HTTPStatus.BAD_REQUEST:
        return FpIndexBadRequestError(message, status_code=status_code, error_code=error_code)
    # Alles andere (unerwartete 1xx/2xx/3xx an dieser Stelle) bleibt generisch.
    return FpIndexStatusError(message, status_code=status_code, error_code=error_code)
