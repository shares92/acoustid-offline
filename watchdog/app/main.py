"""HTTP-Schicht des Waechters (FastAPI) — ARCHITECTURE §4, §7.

Stand Phase 16 traegt sie zwei Routen:

===============  ======  =================================================
``/status``      GET     Stack-Zustand, Datenstand, letzter Update-Lauf,
                         Version — **weckt nie** (§7, §8.2)
``/v2/{...}``    alle    Reverse-Proxy auf ``acoustid-api``, **mit
                         Weck-Logik** (§7 „Fehlerverhalten") und der
                         Aktivitaetsmeldung fuer den Idle-Stopp
===============  ======  =================================================

Dazu kommen die beiden Dauerlaeufer des Lebenszyklus
(:mod:`acoustid_watchdog.lifecycle`): sie leben im Lifespan der Anwendung,
werden beim Herunterfahren abgebrochen und abgewartet.

Die beiden sind bewusst gegenlaeufig gebaut: `/status` fasst den Stack unter
keinen Umstaenden an, `/v2/*` weckt ihn immer, wenn er schlaeft. Genau
dieser Unterschied ist die Invariante §8.2.

Der Proxy nimmt **alle** Methoden an, auch die, die es unter ``/v2`` nicht
gibt. Was erlaubt ist, entscheidet die API — sie ist die Spezifikation, und
ihre Antwort geht unveraendert zurueck (auch das nackte HTTP 405 auf
``GET /v2/lookup/batch``, Hinweis aus Phase 13). Ein eigenes Methodenraster
im Proxy waere eine zweite Vertragsquelle mit eigener Fehlermenge.

Es folgen ``/metrics`` (Phase 22) und die Admin-UI unter ``/admin``
(Phasen 23-27). Alles laeuft ueber **einen** Port (``AOFF_PORT``, Default
8080; ARCHITECTURE §6 „Feste Werte"), weil der Waechter der einzige nach
aussen sichtbare Dienst ist.

Wie im API-Dienst sind die Routen ``async`` und die Arbeit dahinter nicht:
SQLite ist synchron, also geht die Abfrage ueber ``run_in_threadpool``. Und
wie dort ist der Zugriff auf OpenAPI-/Doku-Routen abgeschaltet — der
Waechter ist keine oeffentliche Entwickler-API, und `/status` ist in §7
beschrieben.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from acoustid_watchdog.proxy import error_response
from acoustid_watchdog.service import WatchdogService
from acoustid_watchdog.status import build_status
from acoustid_watchdog.wake import DEFAULT_RETRY_AFTER_S, StackNotReadyError
from shared import setup_logging
from shared.config import WakeConfig
from shared.env import EnvSettings

__all__ = ["PROXY_METHODS", "SERVICE_NAME", "build_app", "create_app"]

_LOG = logging.getLogger(__name__)

#: Name in jeder Logzeile (`service`) — zugleich der Container-Name aus
#: ARCHITECTURE §6.
SERVICE_NAME: Final = "acoustid-watchdog"

#: Methoden, die der Proxy annimmt. Absichtlich alle gaengigen: welche
#: unter ``/v2`` erlaubt sind, sagt die API (siehe Modul-Docstring).
PROXY_METHODS: Final = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def create_app(service: WatchdogService | None = None) -> FastAPI:
    """Baut die FastAPI-Anwendung des Waechters.

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
            app.state.service = WatchdogService.from_env(settings).open()
        else:
            app.state.service = service
        # Die beiden Dauerlaeufer des Lebenszyklus (Phase 16). Sie gehoeren
        # zur laufenden Anwendung, nicht zum Dienst: nur hier gibt es einen
        # Ereignisschleifen-Kontext, in dem sie leben und wieder sterben
        # koennen. Beide schlafen zuerst und pruefen dann — ein kurzer Test
        # mit eingebettetem Dienst loest also keinen einzigen Aufruf aus.
        running: WatchdogService = app.state.service
        tasks = [
            asyncio.create_task(running.poller.run(), name="stack-poller"),
            asyncio.create_task(running.idle.run(), name="idle-stopper"),
        ]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            # Auf das Ende warten, sonst meldet asyncio beim Herunterfahren
            # „Task was destroyed but it is pending".
            await asyncio.gather(*tasks, return_exceptions=True)
            if owns_service:
                await app.state.service.aclose()

    app = FastAPI(
        title="acoustid-offline Waechter",
        summary="Status, Proxy und Admin-UI der selbst gehosteten Instanz",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.get("/status")
    async def status(request: Request) -> JSONResponse:
        return await _status(request)

    @app.api_route("/v2/{path:path}", methods=PROXY_METHODS)
    async def proxy(request: Request, path: str) -> Response:
        return await _proxy(request)

    return app


async def _status(request: Request) -> JSONResponse:
    """Statusabfrage — ausschliesslich aus Waechter-Daten.

    Faellt die Zustandsdatenbank aus, antwortet der Endpunkt mit HTTP 500
    statt mit einem geschoenten „alles gut": ein Statusendpunkt, der einen
    Ausfall als Zustand meldet, ist schlimmer als einer, der schweigt. Der
    Grund steht im Log; das AcoustID-Fehlerformat gilt hier nicht — es
    gehoert zu ``/v2/*`` und hat fuer diesen eigenen Endpunkt keinen
    passenden Code.
    """
    service: WatchdogService = request.app.state.service
    try:
        data: dict[str, Any] = await run_in_threadpool(build_status, service.db, service.state)
    except Exception:
        _LOG.exception("Statusabfrage fehlgeschlagen")
        return JSONResponse(
            {"status": "error", "error": {"message": "Statusabfrage fehlgeschlagen"}},
            status_code=500,
        )
    return JSONResponse(data)


async def _proxy(request: Request) -> Response:
    """Wecken, weiterleiten, Antwort unveraendert zurueckgeben.

    Die Reihenfolge ist der ganze Punkt der Phase: **erst** bereit machen,
    **dann** weiterleiten. Wie lange eine Anfrage dafuer gehalten werden
    darf, steht in ``wake.hold_timeout_s`` (§6) und wird bei jeder Anfrage
    frisch gelesen — die Admin-UI kann den Wert aendern, ohne dass der
    Waechter neu starten muss.

    Zwei Faelle beantwortet der Waechter selbst (§7 „Fehlerverhalten"):

    * Der Stack wird nicht rechtzeitig bereit oder laesst sich nicht
      starten -> ``503`` mit ``Retry-After``.
    * Die API bricht die Uebertragung ab -> ebenfalls ``503``; zusaetzlich
      gilt die Bereitschaft als verfallen, damit die naechste Anfrage
      wieder prueft (der Stack kann von Hand gestoppt worden sein).

    Alles andere — auch jeder Fehler der API — geht unveraendert durch.

    Hier steht ausserdem die eine Zeile, an der der Idle-Stopp haengt: jede
    Anfrage unter ``/v2/`` ist **Aktivitaet** (ARCHITECTURE §6
    „Idle-Definition") und verschiebt den Auto-Stopp. Gezaehlt wird die
    ankommende Anfrage, nicht die fertige Antwort — sonst hielte ein Client,
    der mitten in der Uebertragung abbricht, den Stack nicht wach, obwohl er
    ihn gerade benutzt hat. `/status` und die Admin-UI zaehlen bewusst
    nicht: sie beruehren das Array nie (Invariante §8.2) und duerfen es
    folglich auch nicht wachhalten.
    """
    service: WatchdogService = request.app.state.service
    service.activity.touch()
    try:
        hold_timeout_s = service.config.wake.hold_timeout_s
    except Exception:
        # Eine unlesbare config.yaml darf den Proxy nicht lahmlegen; der
        # Default aus §6 ist dann die richtige Annahme.
        _LOG.exception("Laufzeit-Konfiguration nicht lesbar, Vorgabewert wird benutzt")
        hold_timeout_s = WakeConfig().hold_timeout_s

    try:
        await service.wake.ensure_ready(timeout_s=hold_timeout_s)
    except StackNotReadyError as error:
        _LOG.warning(
            "Anfrage abgewiesen, Stack nicht bereit",
            extra={"path": request.url.path, "error": str(error)},
        )
        return error_response(503, str(error), retry_after_s=error.retry_after_s)

    try:
        return await service.proxy.forward(request)
    except httpx.HTTPError as error:
        service.wake.invalidate()
        _LOG.warning(
            "Weiterleitung fehlgeschlagen",
            extra={"path": request.url.path, "error": str(error)},
        )
        return error_response(
            503,
            f"API nicht erreichbar: {error}",
            retry_after_s=DEFAULT_RETRY_AFTER_S,
        )


def build_app() -> FastAPI:
    """Anwendungsfabrik fuer uvicorn (``acoustid_watchdog.main:build_app``)."""
    return create_app()
