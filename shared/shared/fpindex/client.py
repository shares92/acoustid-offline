"""Synchroner HTTP-Client fuer den acoustid-index (ARCHITECTURE §5.3).

Der Index ist der Kandidatenlieferant der Matching-Pipeline: er haelt je
Fingerprint nur den Query-Extrakt (:func:`shared.fpindex.extract_query`) und
liefert auf eine Suche IDs mit einem Integer-Score, der nur der
Vorsortierung dient. Den AcoustID-Score rechnet Phase 9 gegen den Vollvektor
aus Postgres.

**Warum synchron.** Erster Nutzer ist der Importer — ein One-Shot-Job, der
Batches nacheinander schreibt; Nebenlaeufigkeit braucht er nicht. Der
API-Service kommt spaeter dazu.

**Async bleibt offen.** Die gesamte Protokollarbeit liegt in
:mod:`shared.fpindex.wire`; dieser Client fuegt nur den Transport hinzu, und
der steckt vollstaendig in der einen Methode :meth:`FpIndexClient._send`.
Ein Async-Client erbt von :class:`FpIndexClient`, ersetzt ``httpx.Client``
durch ``httpx.AsyncClient`` und spiegelt die oeffentlichen Methoden — ohne
das Protokoll ein zweites Mal zu implementieren.

**Zeitgrenzen.** ``_search`` traegt eine eigene, serverseitige Frist
(``timeout_ms``); laeuft sie ab, antwortet der Server mit HTTP 500 und
``Timeout``. Unsere HTTP-Leseschranke wird deshalb je Suche aus dieser Frist
plus :data:`READ_TIMEOUT_MARGIN_S` gebildet — der Server soll zuerst
antworten duerfen, sonst verschenken wir seine Fehlermeldung an ein
Client-Timeout.

Beispiel::

    with FpIndexClient.from_env() as index:
        index.ensure_index()
        version = index.update([Insert(doc_id=1, hashes=extract_query(fp, max_hashes=120))])
        hits = index.search(extract_query(fp, max_hashes=120), limit=40)
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from http import HTTPStatus
from types import TracebackType
from typing import Final, Self

import httpx

from shared.env import EnvSettings
from shared.fpindex import wire
from shared.fpindex.errors import (
    FpIndexNotFoundError,
    FpIndexNotReadyError,
    FpIndexTimeoutError,
    FpIndexTransportError,
)
from shared.fpindex.wire import (
    Change,
    IndexInfo,
    IndexState,
    SearchResult,
    WireRequest,
)

__all__ = [
    "DEFAULT_CONNECT_TIMEOUT_S",
    "DEFAULT_INDEX_NAME",
    "DEFAULT_SEARCH_LIMIT",
    "DEFAULT_SEARCH_TIMEOUT_MS",
    "DEFAULT_TIMEOUT_S",
    "READ_TIMEOUT_MARGIN_S",
    "FpIndexClient",
]

_LOG = logging.getLogger(__name__)

#: Name des Suchindex im Server. Ein Server kann mehrere halten; wir fahren
#: genau einen. Aenderbar ueber ``MMO_INDEX_NAME``.
DEFAULT_INDEX_NAME: Final = "main"

#: Leseschranke fuer alles ausser der Suche (Sekunden).
DEFAULT_TIMEOUT_S: Final = 30.0

#: Verbindungsaufbau im Compose-Netz — kurz, der Nachbar ist einen Hop weit weg.
DEFAULT_CONNECT_TIMEOUT_S: Final = 5.0

#: Zuschlag auf die serverseitige Suchfrist, damit dessen Timeout-Antwort
#: (HTTP 500) uns erreicht, bevor unsere eigene Frist zuschlaegt.
READ_TIMEOUT_MARGIN_S: Final = 5.0

#: Serverseitige Suchfrist in Millisekunden (ARCHITECTURE §5.3).
DEFAULT_SEARCH_TIMEOUT_MS: Final = 2000

#: Kandidatenzahl je Suche (ARCHITECTURE §5.3: 20 bis 40).
DEFAULT_SEARCH_LIMIT: Final = 40


class FpIndexClient:
    """Synchroner Client fuer genau einen Index eines acoustid-index-Servers."""

    def __init__(
        self,
        base_url: str,
        index_name: str = DEFAULT_INDEX_NAME,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
        client: httpx.Client | None = None,
    ) -> None:
        """
        Args:
            base_url: Basis-URL des Servers, z. B. ``http://acoustid-index:6081``.
            index_name: Name des Index auf diesem Server.
            timeout_s: Leseschranke fuer alle Aufrufe ausser ``search``.
            connect_timeout_s: Schranke fuer den Verbindungsaufbau.
            client: Vorhandener ``httpx.Client`` (Tests, gemeinsamer Pool).
                Wird dann **nicht** von :meth:`close` geschlossen.
        """
        if not wire.valid_index_name(index_name):
            raise ValueError(
                f"Ungueltiger Indexname {index_name!r}: erlaubt sind Buchstaben, "
                "Ziffern, '_' und '-'; das erste Zeichen muss ein Buchstabe "
                "oder eine Ziffer sein"
            )
        self.base_url = base_url.rstrip("/")
        self.index_name = index_name
        self._timeout = httpx.Timeout(timeout_s, connect=connect_timeout_s)
        self._connect_timeout_s = connect_timeout_s
        self._owns_client = client is None
        self._client = client or httpx.Client(base_url=self.base_url, timeout=self._timeout)

    @classmethod
    def from_env(
        cls,
        env: EnvSettings | None = None,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
        client: httpx.Client | None = None,
    ) -> Self:
        """Baut den Client aus ``MMO_INDEX_URL`` und ``MMO_INDEX_NAME``."""
        settings = env or EnvSettings.from_env()
        return cls(
            settings.index_url,
            settings.index_name,
            timeout_s=timeout_s,
            connect_timeout_s=connect_timeout_s,
            client=client,
        )

    # --- Lebenszyklus ------------------------------------------------------

    def close(self) -> None:
        """Schliesst den HTTP-Pool, falls dieser Client ihn angelegt hat."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(base_url={self.base_url!r}, index_name={self.index_name!r})"

    # --- Operationen -------------------------------------------------------

    def ensure_index(self, *, generation: int | None = None) -> IndexState:
        """Legt den Index an; ein bereits vorhandener ist kein Fehler.

        ``PUT /:index`` ohne Erwartungen ist idempotent — der Server
        antwortet auch beim zweiten Aufruf mit 200 und dem aktuellen Zustand.
        Nur mit ``generation`` kann er mit 409 widersprechen (die vorhandene
        Generation ist aelter oder neuer als die verlangte).

        Args:
            generation: Erwartete Generation. Nur fuer den bewussten
                Neuaufbau eines Index gedacht; im Normalbetrieb weglassen.

        Raises:
            FpIndexAlreadyExistsError: ``generation`` passt nicht zur
                vorhandenen.
        """
        status, content = self._send(wire.put_index_request(self.index_name, generation=generation))
        payload = self._ok(status, content)
        state = wire.parse_index_state(payload)
        _LOG.info(
            "Index bereit",
            extra={
                "index": self.index_name,
                "version": state.version,
                "generation": state.generation,
                "ready": state.ready,
            },
        )
        return state

    def index_info(self) -> IndexInfo:
        """``GET /:index`` — Version, Metadaten und Statistik.

        Raises:
            FpIndexNotFoundError: Der Index existiert nicht.
        """
        status, content = self._send(wire.get_index_request(self.index_name))
        return wire.parse_index_info(self._ok(status, content))

    def get_metadata(self) -> Mapping[str, str]:
        """Nur die Metadaten aus :meth:`index_info`.

        Der Index speichert hier freie Schluessel/Wert-Paare, die ein
        ``_update`` mitschreiben kann — Phase 7 legt darin den Fortschritt
        des Index-Feeds ab (z. B. die zuletzt eingespielte ``fingerprint.id``),
        damit Index und Postgres nach einem Abbruch abgeglichen werden koennen.
        """
        return self.index_info().metadata

    def index_health(self) -> bool:
        """``GET /:index/_health`` — existiert der Index und antwortet er?

        Bewusst ein Bool und keine Ausnahme: das ist der Aufruf fuer
        Statusanzeigen und Wartepunkte. ``False`` heisst „noch nicht da"
        (404) oder „laedt noch" (503); ein Netzfehler bleibt eine Ausnahme.

        Achtung: der Server prueft hier nur die Existenz. Ob er *bereit* ist,
        zeigt er ausschliesslich durch 503 ``IndexNotReady`` auf den
        Nutzrouten.
        """
        status, content = self._send(wire.index_health_request(self.index_name))
        if status == HTTPStatus.OK:
            return True
        if status in (HTTPStatus.NOT_FOUND, HTTPStatus.SERVICE_UNAVAILABLE):
            _LOG.debug(
                "Index nicht verfuegbar",
                extra={"index": self.index_name, "status": status},
            )
            return False
        raise wire.status_error(status, content)

    def server_health(self) -> bool:
        """``GET /_health`` — reine Lebendpruefung des Servers.

        Prueft inhaltlich nichts (auch ein Server ohne jeden Index antwortet
        mit 200) und taugt daher nur zur Frage „laeuft der Prozess?".
        """
        status, _ = self._send(wire.server_health_request())
        return status == HTTPStatus.OK

    def update(
        self,
        changes: Iterable[Change],
        *,
        metadata: Mapping[str, str] | None = None,
        expected_version: int | None = None,
    ) -> int:
        """``POST /:index/_update`` — ein atomarer Batch.

        Alle Changes landen in einer Transaktion (ein Segment, ein
        fsync, eine neue Versionsnummer): entweder alle oder keiner. Bei
        doppelten IDs im Batch gewinnt der letzte Eintrag.

        Args:
            changes: :class:`~shared.fpindex.Insert`- und
                :class:`~shared.fpindex.Delete`-Eintraege. Empfohlene
                Batchgroesse: :data:`~shared.fpindex.RECOMMENDED_BATCH_SIZE`;
                harte Grenze ist der 16-MiB-Rumpf.
            metadata: Ersetzt die Metadaten des Index. Ein Batch **ohne**
                Changes, nur mit Metadaten, ist zulaessig und erhoeht die
                Version ebenfalls.
            expected_version: Optimistisches Sperren. Passt der Wert nicht
                zur aktuellen Version, wird nichts geschrieben.

        Returns:
            Die neue Version des Index.

        Raises:
            FpIndexVersionMismatchError: ``expected_version`` passte nicht.
            ValueError: Eine ID oder ein Hash liegt ausserhalb des
                Wertebereichs, oder der Rumpf ueberschreitet 16 MiB.
        """
        request = wire.update_request(
            self.index_name,
            changes,
            metadata=metadata,
            expected_version=expected_version,
        )
        status, content = self._send(request)
        version = wire.parse_version(self._ok(status, content))
        _LOG.debug(
            "Index-Batch geschrieben",
            extra={"index": self.index_name, "version": version},
        )
        return version

    def search(
        self,
        query: Sequence[int],
        *,
        timeout_ms: int = DEFAULT_SEARCH_TIMEOUT_MS,
        limit: int = DEFAULT_SEARCH_LIMIT,
    ) -> list[SearchResult]:
        """``POST /:index/_search`` — Kandidaten zu einer Query.

        Args:
            query: Deduplizierte u32-Hashes aus
                :func:`~shared.fpindex.extract_query`. Duplikate wuerden den
                Score verfaelschen, weil der Server sie doppelt zaehlt.
            timeout_ms: Serverseitige Frist. Laeuft sie ab, gibt es kein
                Teilergebnis, sondern :class:`FpIndexSearchTimeoutError`.
            limit: Kandidatenzahl, 1…:data:`~shared.fpindex.MAX_SEARCH_LIMIT`.

        Returns:
            Treffer absteigend nach Score. Der Server filtert selbst
            (absoluter Mindestscore ``(len(query)+19)//20`` und 10 % des
            besten Treffers) — eine leere Liste ist der Normalfall fuer
            unbekannte Fingerprints.

        Raises:
            FpIndexSearchTimeoutError: Serverseitige Frist abgelaufen.
            FpIndexNotFoundError: Der Index existiert nicht.
        """
        request = wire.search_request(self.index_name, query, timeout_ms=timeout_ms, limit=limit)
        read_timeout_s = timeout_ms / 1000 + READ_TIMEOUT_MARGIN_S
        status, content = self._send(request, read_timeout_s=read_timeout_s)
        return wire.parse_search(self._ok(status, content))

    def delete_doc(self, doc_id: int, *, missing_ok: bool = True) -> None:
        """``DELETE /:index/:id`` — ein Dokument als geloescht markieren.

        Der Server setzt einen Tombstone; das Dokument taucht in Suchen nicht
        mehr auf, zaehlt in der Statistik aber weiter mit. Ein unbekanntes
        Dokument ist fuer den Server kein Fehler.

        Args:
            doc_id: Dokument-ID (bei uns die ``fingerprint.id``).
            missing_ok: Bei ``False`` wird ein fehlender **Index** (404) zur
                Ausnahme; sonst wird er verschluckt.
        """
        status, content = self._send(wire.delete_doc_request(self.index_name, doc_id))
        if status == HTTPStatus.NOT_FOUND and missing_ok:
            _LOG.debug(
                "Loeschen ohne Wirkung (Index unbekannt)",
                extra={"index": self.index_name, "doc_id": doc_id},
            )
            return
        self._ok(status, content)

    def delete_index(self, *, missing_ok: bool = True) -> None:
        """``DELETE /:index`` — den ganzen Index verwerfen.

        Der einzige Weg, einen Index zu leeren: ein Truncate gibt es nicht.
        Danach muss :meth:`ensure_index` neu anlegen.
        """
        status, content = self._send(wire.delete_index_request(self.index_name))
        if status == HTTPStatus.NOT_FOUND and missing_ok:
            return
        self._ok(status, content)
        _LOG.warning("Index geloescht", extra={"index": self.index_name})

    def metrics(self) -> str:
        """``GET /_metrics`` — Prometheus-Text des Servers (unveraendert).

        Die einzige Antwort, die kein msgpack ist; sie wird deshalb roh als
        UTF-8 durchgereicht (``aindex_docs``, ``aindex_search_duration_seconds``,
        ``aindex_checkpoints_total`` …).
        """
        status, content = self._send(wire.metrics_request())
        if status >= HTTPStatus.BAD_REQUEST:
            raise wire.status_error(status, content)
        return content.decode("utf-8", errors="replace")

    def require_ready(self) -> None:
        """Wirft, wenn der Index nicht benutzbar ist — ohne zu warten.

        Gedacht als Startpruefung eines Jobs: erst ``ensure_index``, dann
        dieser Aufruf. Er unterscheidet die drei Faelle, die der Server
        anbietet: Index fehlt (404), Index laedt (503), Index nutzbar.

        Raises:
            FpIndexNotFoundError: Der Index existiert nicht.
            FpIndexNotReadyError: Der Index laedt noch.
        """
        status, content = self._send(wire.index_health_request(self.index_name))
        if status == HTTPStatus.OK:
            return
        if status == HTTPStatus.NOT_FOUND:
            raise FpIndexNotFoundError(
                f"Index {self.index_name!r} existiert nicht — ensure_index() zuerst",
                status_code=status,
                error_code="IndexNotFound",
            )
        if status == HTTPStatus.SERVICE_UNAVAILABLE:
            raise FpIndexNotReadyError(
                f"Index {self.index_name!r} laedt noch",
                status_code=status,
                error_code="IndexNotReady",
            )
        raise wire.status_error(status, content)

    # --- Transport ---------------------------------------------------------

    def _send(
        self, request: WireRequest, *, read_timeout_s: float | None = None
    ) -> tuple[int, bytes]:
        """Einziger IO-Punkt: schickt eine :class:`WireRequest` los.

        Ein Async-Client ueberschreibt genau diese Methode (bzw. ihr
        ``await``-Pendant) und erbt den gesamten Rest.
        """
        timeout = (
            self._timeout
            if read_timeout_s is None
            else httpx.Timeout(read_timeout_s, connect=self._connect_timeout_s)
        )
        try:
            response = self._client.request(
                request.method,
                f"{self.base_url}{request.path}",
                content=request.body,
                headers=dict(wire.HEADERS),
                timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            raise FpIndexTimeoutError(
                f"{request.method} {request.path}: Frist abgelaufen ({exc})"
            ) from exc
        except httpx.HTTPError as exc:
            raise FpIndexTransportError(
                f"{request.method} {request.path}: keine Antwort vom Index ({exc})"
            ) from exc
        return response.status_code, response.content

    @staticmethod
    def _ok(status: int, content: bytes) -> object:
        """Erfolgsrumpf dekodieren oder die passende Ausnahme werfen."""
        if status >= HTTPStatus.BAD_REQUEST:
            raise wire.status_error(status, content)
        return wire.decode_body(content)
