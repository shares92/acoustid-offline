"""HTTP-Schicht des API-Dienstes (FastAPI) — ARCHITECTURE §4, §7.

Hier sitzt alles, was mit dem Transport zu tun hat, und nichts weiter:

* **``GET`` und ``POST`` auf derselben Route.** Parameter kommen aus dem
  Query-String **und** aus einem ``application/x-www-form-urlencoded``-Rumpf
  (kein JSON-Rumpf — das Original kennt keinen).
* **gzip-Rumpf.** ``Content-Encoding: gzip`` wird entpackt; pyacoustid
  (beets) schickt so.
* **1-MiB-Grenze** auf dem Rumpf, **vor und nach** dem Entpacken. Ein
  Ueberschreiten ist Fehler 19 / HTTP 413 — Picard verkleinert seine
  Batches genau daraufhin, ein stilles Abschneiden waere schaedlich.
* **CORS:** ``Access-Control-Allow-Origin: *`` auf jeder Antwort.
* **Fehlerformat:** jeder :class:`~acoustid_api.errors.AcoustidError` wird
  zur AcoustID-Fehlerantwort mit seinem HTTP-Status; alles Unerwartete wird
  protokolliert und zu Fehler 5 / HTTP 500.

**Keine Authentifizierung, kein Rate-Limit.** Beides setzt der Waechter am
Proxy durch (ARCHITECTURE §7 „Durchsetzungsort Auth & Rate-Limit"); der
Dienst hier hat keinen Host-Port und ist nur im Compose-Netz erreichbar.

Die Route ist ``async``, die Arbeit dahinter nicht: Datenbank, Index und
Rescoring sind synchron und laufen deshalb ueber
``run_in_threadpool``. So blockiert ein laufendes Rescoring nicht den
Event-Loop, und es bleibt bei genau einer (synchronen) Implementierung.
"""

from __future__ import annotations

import logging
import zlib
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, Final
from urllib.parse import parse_qsl

from fastapi import FastAPI, Request, Response
from starlette.concurrency import run_in_threadpool

from acoustid_api.errors import AcoustidError, InternalError, RequestTooLargeError, error_payload
from acoustid_api.formats import ResponseFormat
from acoustid_api.lookup import handle_lookup
from acoustid_api.params import LookupParams, RequestValues, parse_format, parse_lookup
from acoustid_api.service import ApiService
from shared import setup_logging
from shared.env import EnvSettings

__all__ = ["MAX_BODY_BYTES", "SERVICE_NAME", "create_app"]

_LOG = logging.getLogger(__name__)

#: Name in jeder Logzeile (`service`) — zugleich der Container-Name aus
#: ARCHITECTURE §6.
SERVICE_NAME: Final = "acoustid-api"

#: ``MAX_CONTENT_LENGTH`` des Originals: 1 MiB, gemessen am **entpackten**
#: Rumpf (und schon vorher an ``Content-Length``, damit ein zu grosser
#: Upload nicht erst vollstaendig gelesen wird).
MAX_BODY_BYTES: Final = 1024 * 1024

#: Der einzige Rumpf-Typ, den das Original entgegennimmt. Ohne Angabe wird
#: ebenfalls als Formular gelesen (nachsichtig, schadet nicht).
_FORM_TYPE: Final = "application/x-www-form-urlencoded"

#: Rumpf-Zeichensatz. Das Original liest ebenfalls UTF-8; kaputte Bytes
#: sollen keine Anfrage abbrechen.
_ENCODING: Final = "utf-8"


def create_app(service: ApiService | None = None) -> FastAPI:
    """Baut die FastAPI-Anwendung.

    Args:
        service: Fertiger Dienst (Tests, eingebettete Nutzung). Ohne Angabe
            entsteht er beim Start aus der Umgebung und wird beim
            Herunterfahren wieder geschlossen.
    """
    owns_service = service is None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if owns_service:
            settings = EnvSettings.from_env()
            setup_logging(SERVICE_NAME, settings.log_level)
            app.state.service = ApiService.from_env(settings)
            app.state.service.open()
        else:
            app.state.service = service
        try:
            yield
        finally:
            if owns_service:
                app.state.service.close()

    app = FastAPI(
        title="acoustid-offline API",
        summary="AcoustID-kompatibler Lookup gegen den eigenen Bestand",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def add_cors_header(request: Request, call_next: Callable) -> Response:
        response = await call_next(request)
        response.headers["Access-Control-Allow-Origin"] = "*"
        return response

    @app.api_route("/v2/lookup", methods=["GET", "POST"])
    async def lookup(request: Request) -> Response:
        return await _lookup(request)

    return app


async def _lookup(request: Request) -> Response:
    """Ein Lookup von der Leitung bis zur fertigen Antwort."""
    # Vorbelegung fuer den Fall, dass die Anfrage scheitert, bevor `format`
    # ueberhaupt gelesen werden konnte (zu grosser Rumpf, kaputtes `format`) —
    # dann antwortet auch das Original in JSON.
    response_format = ResponseFormat()
    try:
        values = await _read_values(request)
        response_format = parse_format(values)
        params = parse_lookup(values)
        service: ApiService = request.app.state.service
        data = await run_in_threadpool(_run_lookup, service, params)
    except AcoustidError as error:
        return _render(response_format, error_payload(error), error.http_status)
    except Exception:
        _LOG.exception("Unbehandelter Fehler im Lookup")
        error = InternalError()
        return _render(response_format, error_payload(error), error.http_status)
    return _render(response_format, {"status": "ok", **data}, 200)


def _run_lookup(service: ApiService, params: LookupParams) -> dict[str, Any]:
    """Synchroner Teil: Verbindung ziehen, Pipeline laufen lassen."""
    with service.pool.connection() as connection:
        return handle_lookup(connection, service.matcher, params)


def _render(response_format: ResponseFormat, data: dict[str, Any], status_code: int) -> Response:
    rendered = response_format.render(data)
    return Response(
        content=rendered.body.encode(_ENCODING),
        status_code=status_code,
        media_type=rendered.content_type,
    )


async def _read_values(request: Request) -> RequestValues:
    """Query-String und Rumpf zu einer Parametersicht zusammenfuehren.

    Raises:
        RequestTooLargeError: Der Rumpf ueberschreitet 1 MiB — vor oder nach
            dem Entpacken.
    """
    query = parse_qsl(request.url.query, keep_blank_values=True)
    form: list[tuple[str, str]] = []
    if request.method != "GET":
        body = await _read_body(request)
        if body:
            form = parse_qsl(body.decode(_ENCODING, errors="replace"), keep_blank_values=True)
    return RequestValues(query, form)


async def _read_body(request: Request) -> bytes:
    """Rumpf lesen, Groesse begrenzen, bei Bedarf entpacken."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise RequestTooLargeError()

    media_type = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if media_type and media_type != _FORM_TYPE:
        # Multipart und JSON-Rumpf kennt das Original an dieser Route nicht;
        # der Query-String bleibt trotzdem gueltig.
        _LOG.debug("Rumpf ignoriert", extra={"content_type": media_type})
        return b""

    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise RequestTooLargeError()
        chunks.append(chunk)
    body = b"".join(chunks)

    if (request.headers.get("content-encoding") or "").strip().lower() != "gzip":
        return body
    return _gunzip(body)


def _gunzip(body: bytes) -> bytes:
    """gzip-Rumpf entpacken, mit derselben 1-MiB-Grenze.

    Ein Rumpf, der sich nicht entpacken laesst, gilt als leer: die
    Fehlertabelle der API kennt keinen Code fuer „kaputter Rumpf", und das
    Original antwortet dort mit einem nackten HTTP 400 ausserhalb des
    AcoustID-Formats. Der Grund steht im Log, und die Anfrage scheitert
    anschliessend am fehlenden Pflichtparameter (Fehler 2).
    """
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        body = decompressor.decompress(body, MAX_BODY_BYTES + 1)
    except zlib.error as exc:
        _LOG.warning("gzip-Rumpf nicht lesbar", extra={"error": str(exc)})
        return b""
    if len(body) > MAX_BODY_BYTES or decompressor.unconsumed_tail:
        raise RequestTooLargeError()
    return body


def build_app() -> FastAPI:
    """Anwendungsfabrik fuer uvicorn (``acoustid_api.main:build_app``)."""
    return create_app()
