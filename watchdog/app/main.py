"""HTTP-Schicht des Waechters (FastAPI) — ARCHITECTURE §4, §7.

Stand Phase 14 traegt sie genau eine Route:

===============  ====  ===================================================
``/status``      GET   Stack-Zustand, Datenstand, letzter Update-Lauf,
                       Version — **weckt nie** (§7, §8.2)
===============  ====  ===================================================

Es folgen der Reverse-Proxy ``/v2/*`` mit Weck-Logik (Phase 15), ``/metrics``
(Phase 22) und die Admin-UI unter ``/admin`` (Phasen 23-27). Alles laeuft
ueber **einen** Port (``AOFF_PORT``, Default 8080; ARCHITECTURE §6 „Feste
Werte"), weil der Waechter der einzige nach aussen sichtbare Dienst ist.

Wie im API-Dienst sind die Routen ``async`` und die Arbeit dahinter nicht:
SQLite ist synchron, also geht die Abfrage ueber ``run_in_threadpool``. Und
wie dort ist der Zugriff auf OpenAPI-/Doku-Routen abgeschaltet — der
Waechter ist keine oeffentliche Entwickler-API, und `/status` ist in §7
beschrieben.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from acoustid_watchdog.service import WatchdogService
from acoustid_watchdog.status import build_status
from shared import setup_logging
from shared.env import EnvSettings

__all__ = ["SERVICE_NAME", "build_app", "create_app"]

_LOG = logging.getLogger(__name__)

#: Name in jeder Logzeile (`service`) — zugleich der Container-Name aus
#: ARCHITECTURE §6.
SERVICE_NAME: Final = "acoustid-watchdog"


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
        try:
            yield
        finally:
            if owns_service:
                app.state.service.close()

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


def build_app() -> FastAPI:
    """Anwendungsfabrik fuer uvicorn (``acoustid_watchdog.main:build_app``)."""
    return create_app()
