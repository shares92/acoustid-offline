"""Reverse-Proxy ``/v2/*`` -> ``acoustid-api`` (ARCHITECTURE §3, §7).

Der Waechter ist der einzige Dienst mit veroeffentlichtem Port; alles, was
ein Client an ``/v2/...`` schickt, geht durch dieses Modul. Es hat genau
eine Aufgabe: **die Anfrage unveraendert weitergeben und die Antwort
unveraendert zurueckgeben**.

**Warum so wenig.** Die API ist bug-fuer-bug kompatibel zu
api.acoustid.org (ARCHITECTURE §7, docs/api-lookup.md). Jede Umformung im
Proxy waere eine zweite, konkurrierende Spezifikation:

* **Fehlerantworten bleiben, wie sie sind.** Auch das nackte HTTP 405 auf
  ``GET /v2/lookup/batch`` (Hinweis aus Phase 13) wird durchgereicht.
  Ein hier erfundenes AcoustID-Fehlerbild waere eine Antwort, die der
  direkt angesprochene Dienst nie gibt — und damit ein Unterschied
  zwischen „mit Proxy" und „ohne Proxy", den niemand vertraglich zugesagt
  hat.
* **Rumpf und Kopfzeilen bleiben roh.** ``Content-Encoding: gzip``
  entpackt die API selbst (pyacoustid/beets schicken so), die 1-MiB-Grenze
  zieht sie selbst — der Proxy zaehlt und entpackt deshalb nichts.
  Uebertragen wird gestreamt, in beide Richtungen: der Proxy haelt nie
  eine ganze Anfrage im Speicher.
* **Der Query-String wird nicht angefasst.** Er geht als rohe Bytes
  weiter; eine Normalisierung koennte Prozent-Kodierungen veraendern, an
  denen die Parameterlesart haengt (Chromaprint-Base64).

Eigene Antworten gibt der Proxy nur dort, wo es keine fremde gibt: wenn der
Stack nicht rechtzeitig bereit wird (``503`` + ``Retry-After``, §7), wenn
die API waehrend der Uebertragung wegbricht — und seit Phase 18 dort, wo
der Waechter die Anfrage gar nicht erst durchlaesst (fehlender/ungueltiger
Key, ueberschrittenes Rate-Limit). Alle tragen das AcoustID-Fehlerformat
mit einem Code aus derselben Tabelle, aus der die API ihre Fehler nimmt
(``ERROR_CODES`` in :mod:`acoustid_api.errors`); der Waechter haelt die
gebrauchten Codes hier nach, weil beide Dienste getrennte Images sind
(DECISIONS 2026-07-25 „Getrennte Images").

**Die eigenen Fehlertexte nennen keine Interna** (Phase 18, Hinweis aus
Phase 15). Bis dahin stand im 503 der Containername oder die interne URL,
an der das Wecken scheiterte — im LAN unkritisch, bei einer nach aussen
exponierten Instanz eine Auskunft ueber den inneren Aufbau an jeden, der
fragt. Der Client bekommt jetzt den Wortlaut, den auch das Original zu
Code 13 schickt (:data:`ERROR_MESSAGES`); der Grund steht unveraendert im
Containerlog und, wo er einen Zustandswechsel bedeutet, im Ereignis-Log.

Seit Phase 17 gibt es **eine** Ausnahme vom Streaming, und zwar nur dort,
wo der Lookup-Cache sie braucht: :meth:`ReverseProxy.forward_capturing`
sammelt den Antwortrumpf bis zu einer Grenze ein, damit er eingelagert
werden kann. Ueberschreitet er sie, faellt der Aufruf still auf den
Streaming-Pfad zurueck — der Waechter haelt nie eine ganze Antwort im
Speicher, nur weil es einen Cache gibt. Was der Client bekommt, ist in
beiden Faellen dasselbe.

Auth-Pruefung und Rate-Limit liegen ebenfalls auf diesem Weg, stehen aber
in eigenen Modulen (:mod:`acoustid_watchdog.auth`,
:mod:`acoustid_watchdog.ratelimit`); die Reihenfolge setzt
:func:`acoustid_watchdog.main._proxy`.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Final

import httpx
from fastapi import Request, Response
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

__all__ = [
    "ERROR_INVALID_API_KEY",
    "ERROR_MESSAGES",
    "ERROR_MISSING_PARAMETER",
    "ERROR_RATE_LIMIT",
    "ERROR_REQUEST_TOO_LARGE",
    "ERROR_SERVICE_UNAVAILABLE",
    "HOP_BY_HOP_HEADERS",
    "RequestContent",
    "ReverseProxy",
    "error_response",
]

_LOG = logging.getLogger(__name__)

#: Was statt ``request.stream()`` weitergereicht werden darf: fertige Bytes
#: (der gepufferte Rumpf des Lookup-Caches) oder ein Ersatzstrom.
type RequestContent = bytes | AsyncIterator[bytes] | None

#: Kopfzeilen, die nur fuer **eine** Verbindung gelten (RFC 9110 §7.6.1).
#: Sie duerfen nicht weitergereicht werden: die Rahmung der Nachricht
#: bestimmt jede Strecke selbst. ``host`` steht hier mit dazu, weil ihn der
#: Zielclient neu setzt.
HOP_BY_HOP_HEADERS: Final[frozenset[str]] = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "host",
    }
)

#: Die Codes der AcoustID-Fehlertabelle, die der Waechter selbst erzeugt.
#: Sie sind die Teilmenge von ``acoustid_api.errors.ERROR_CODES``, die auf
#: seinem Weg entstehen kann — nachgehalten, weil Waechter und API getrennte
#: Images sind (Modul-Docstring).
#:
#: =====  ======  ===============================================
#: Code   HTTP    Wann
#: =====  ======  ===============================================
#: 2      400     ``client`` fehlt im ``apikey``-Modus (Phase 18)
#: 4      400     ``client`` ist kein gueltiger Key (Phase 18)
#: 13     503     Stack nicht bereit / API weggebrochen (Phase 15)
#: 14     429     IP-Rate-Limit ueberschritten (Phase 18)
#: 19     413     Rumpf zu gross, um den Key zu lesen (Phase 18)
#: =====  ======  ===============================================
ERROR_MISSING_PARAMETER: Final = 2
ERROR_INVALID_API_KEY: Final = 4
ERROR_SERVICE_UNAVAILABLE: Final = 13
ERROR_RATE_LIMIT: Final = 14
ERROR_REQUEST_TOO_LARGE: Final = 19

#: Die festen Meldungstexte zu diesen Codes — **wortgleich** zu
#: :mod:`acoustid_api.errors` und damit zum Original. Zwei tragen einen
#: Platzhalter, den der Aufrufer fuellt (``client`` bzw. die Rate).
#:
#: Der Wortlaut ist Vertragsteil: Clients werten zwar den Code aus, aber die
#: Vergleichbarkeit mit den Original-Antworten ist genau der Punkt, an dem
#: „bug-fuer-bug kompatibel" ueberpruefbar bleibt. Und er ist zugleich die
#: gewuenschte Zurueckhaltung: kein Text nennt einen Containernamen, eine
#: URL oder einen Grund, den nur der Betreiber angeht.
ERROR_MESSAGES: Final[dict[int, str]] = {
    ERROR_MISSING_PARAMETER: 'missing required parameter "{name}"',
    ERROR_INVALID_API_KEY: "invalid API key",
    ERROR_SERVICE_UNAVAILABLE: "service currently unavailable, try again later",
    ERROR_RATE_LIMIT: "rate limit ({rate:f} requests per second) exceeded, try again later",
    ERROR_REQUEST_TOO_LARGE: "request too large",
}

#: Leseschranke einer weitergeleiteten Anfrage. Grosszuegig: ein Batch mit
#: hundert Fingerprints rechnet synchron im Threadpool der API (Hinweis aus
#: Phase 13), und ein Abbruch hier waere fuer den Client nicht von einem
#: Fehler zu unterscheiden.
DEFAULT_TIMEOUT_S: Final = 120.0

#: Verbindungsaufbau im Compose-Netz — der Nachbar ist einen Hop weit weg.
DEFAULT_CONNECT_TIMEOUT_S: Final = 5.0


def error_response(
    status_code: int,
    message: str,
    *,
    code: int = ERROR_SERVICE_UNAVAILABLE,
    retry_after_s: int | None = None,
) -> Response:
    """Eine eigene Antwort im AcoustID-Fehlerformat.

    Args:
        status_code: HTTP-Status (503 beim Wecken, 400/429/413 in Phase 18).
        message: Meldungstext — aus :data:`ERROR_MESSAGES`, nie ein
            interner Grund (Modul-Docstring).
        code: Fehlercode der AcoustID-Tabelle.
        retry_after_s: Setzt ``Retry-After`` in Sekunden.

    ``Retry-After`` schickt das Original **nicht** (auch nicht bei 429). Der
    Waechter setzt es trotzdem, weil ARCHITECTURE §7 „Fehlerverhalten" es
    fuer beide Faelle ausdruecklich verlangt: er weiss, anders als das
    Original, ungefaehr *wann* es wieder geht. Ein zusaetzlicher Kopf bricht
    keinen Client — Picard etwa ignoriert ihn schlicht (Phase-1-Bericht).
    """
    payload = {"status": "error", "error": {"code": code, "message": message}}
    headers = {"Access-Control-Allow-Origin": "*"}
    if retry_after_s is not None:
        headers["Retry-After"] = str(retry_after_s)
    return Response(
        content=json.dumps(payload) + "\n",
        status_code=status_code,
        media_type="application/json",
        headers=headers,
    )


class ReverseProxy:
    """Reicht ``/v2/*`` an den API-Dienst durch — streamend, unveraendert."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """
        Args:
            base_url: Basis-URL des API-Dienstes, z. B.
                ``http://acoustid-api:8080``.
            timeout_s: Leseschranke einer weitergeleiteten Anfrage.
            connect_timeout_s: Schranke fuer den Verbindungsaufbau.
            client: Vorhandener ``httpx.AsyncClient`` (Tests). Wird dann
                **nicht** von :meth:`aclose` geschlossen.
        """
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s, connect=connect_timeout_s),
        )

    async def aclose(self) -> None:
        """Schliesst den HTTP-Pool, falls dieser Proxy ihn angelegt hat."""
        if self._owns_client:
            await self._client.aclose()

    async def forward(self, request: Request, *, content: RequestContent = None) -> Response:
        """Leitet eine Anfrage weiter und liefert die Antwort zurueck.

        Args:
            request: Die eingehende Anfrage.
            content: Ersatz fuer ``request.stream()``. Gesetzt, wenn der
                Rumpf schon gelesen wurde (Lookup-Cache, Phase 17) —
                weitergegeben werden dann dieselben Bytes.

        Raises:
            httpx.HTTPError: Die API war nicht erreichbar oder brach ab. Der
                Aufrufer (``main``) entscheidet, was der Client sieht — nur
                er weiss, ob gerade geweckt wurde.
        """
        response = await self._send(request, content)
        proxied = StreamingResponse(
            response.aiter_raw(),
            status_code=response.status_code,
            # Der Rumpf laeuft noch, wenn diese Funktion zurueckkehrt; die
            # Verbindung darf erst danach zurueck in den Pool.
            background=BackgroundTask(response.aclose),
        )
        # Die Kopfzeilen roh setzen statt ueber `headers=`: Starlette nimmt
        # dort ein Mapping und wuerde mehrfach vorkommende Namen zu einem
        # zusammenfalten. Ausserdem setzt es sonst eigene Vorgaben
        # (`content-type`), die die Antwort der API veraendern wuerden.
        proxied.raw_headers = _filtered(response.headers)
        return proxied

    async def forward_capturing(
        self, request: Request, *, limit: int, content: RequestContent = None
    ) -> tuple[Response, bytes | None]:
        """Wie :meth:`forward`, sammelt den Rumpf aber bis ``limit`` ein.

        Returns:
            Die Antwort fuer den Client und ihren Rumpf — oder ``None``
            statt des Rumpfs, wenn er groesser als ``limit`` war. Dann ist
            die Antwort wieder ein Strom und nichts wird eingelagert.

        Die Bytes gehen roh durch (``aiter_raw``): was eingelagert wird, ist
        genau das, was die API geschickt hat, samt ihrer ``content-length``
        und einer etwaigen ``content-encoding``.
        """
        response = await self._send(request, content)
        chunks: list[bytes] = []
        size = 0
        try:
            stream = response.aiter_raw()
            async for chunk in stream:
                chunks.append(chunk)
                size += len(chunk)
                if size > limit:
                    # Zu gross: der gelesene Anfang plus der Rest ist wieder
                    # die ganze Antwort — der Client merkt nichts.
                    proxied = StreamingResponse(
                        _chain(chunks, stream),
                        status_code=response.status_code,
                        background=BackgroundTask(response.aclose),
                    )
                    proxied.raw_headers = _filtered(response.headers)
                    return proxied, None
        except BaseException:
            await response.aclose()
            raise
        await response.aclose()

        body = b"".join(chunks)
        buffered = Response(content=body, status_code=response.status_code)
        buffered.raw_headers = _filtered(response.headers)
        return buffered, body

    async def _send(self, request: Request, content: RequestContent) -> httpx.Response:
        """Schickt die Anfrage an die API und protokolliert die Weiterleitung."""
        started_at = time.monotonic()
        response = await self._client.send(self._build_request(request, content), stream=True)
        _LOG.info(
            "Anfrage weitergeleitet",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round((time.monotonic() - started_at) * 1000, 1),
                "client_ip": request.client.host if request.client else None,
            },
        )
        return response

    def _build_request(self, request: Request, content: RequestContent = None) -> httpx.Request:
        """Baut die Anfrage an die API — Pfad und Query als rohe Bytes."""
        scope = request.scope
        raw_path: bytes = scope.get("raw_path") or scope["path"].encode()
        query: bytes = scope.get("query_string", b"")
        url = httpx.URL(self.base_url).copy_with(
            raw_path=raw_path + (b"?" + query if query else b"")
        )
        headers = [
            (name, value)
            for name, value in request.headers.raw
            if name.decode("latin-1").lower() not in HOP_BY_HOP_HEADERS
        ]
        if content is None:
            # Ohne Rumpf **kein** `content`: httpx wuerde einem leeren
            # Iterator sonst `Transfer-Encoding: chunked` verpassen — auf
            # einem GET eine Merkwuerdigkeit, die manche Server abweisen.
            content = request.stream() if _has_body(request) else None
        return self._client.build_request(
            request.method,
            url,
            headers=headers,
            content=content,
        )


async def _chain(head: list[bytes], rest: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Der schon gelesene Anfang, dann der Rest des Stroms."""
    for chunk in head:
        yield chunk
    async for chunk in rest:
        yield chunk


def _has_body(request: Request) -> bool:
    """Bringt diese Anfrage einen Rumpf mit?"""
    if request.headers.get("transfer-encoding"):
        return True
    declared = request.headers.get("content-length")
    return declared is not None and declared.isdigit() and int(declared) > 0


def _filtered(headers: httpx.Headers) -> list[tuple[bytes, bytes]]:
    """Antwort-Kopfzeilen ohne die verbindungsgebundenen, als rohe Bytes.

    Alles andere bleibt unveraendert — auch ``Content-Encoding``,
    ``Content-Length`` und das ``Access-Control-Allow-Origin: *`` der API
    (§7). Der Rumpf wird roh durchgereicht (``aiter_raw``), die Angaben
    passen also weiterhin.
    """
    return [
        (name, value)
        for name, value in headers.raw
        if name.decode("latin-1").lower() not in HOP_BY_HOP_HEADERS
    ]
