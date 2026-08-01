"""HTTP-Schicht des Waechters (FastAPI) — ARCHITECTURE §4, §7.

Stand Phase 17 traegt sie zwei Routen:

===============  ======  =================================================
``/status``      GET     Stack-Zustand, Datenstand, letzter Update-Lauf,
                         Version — **weckt nie** (§7, §8.2)
``/v2/{...}``    alle    Lookup-Cache, Reverse-Proxy auf ``acoustid-api``,
                         **mit Weck-Logik** (§7 „Fehlerverhalten") und der
                         Aktivitaetsmeldung fuer den Idle-Stopp
===============  ======  =================================================

Seit Phase 17 weckt `/v2/*` nur noch dann, wenn die Antwort nicht schon im
Lookup-Cache liegt — die zweite Stelle, an der Invariante §8.2 baulich
haengt (:func:`_proxy`).

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

from acoustid_watchdog.cache import (
    MAX_CACHEABLE_BODY_BYTES,
    CachedResponse,
    RequestPlan,
    is_cacheable_response,
    plan_request,
)
from acoustid_watchdog.proxy import error_response
from acoustid_watchdog.service import WatchdogService
from acoustid_watchdog.status import build_status
from acoustid_watchdog.wake import DEFAULT_RETRY_AFTER_S, StackNotReadyError
from shared import setup_logging
from shared.config import Config
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
    """Cache fragen, sonst wecken, weiterleiten, Antwort zurueckgeben.

    Die Reihenfolge ist der ganze Punkt der beiden Phasen:

    1. **Cache** (Phase 17). Liegt die Antwort schon da, geht sie sofort
       zurueck — kein Docker-Kontakt, kein API-Kontakt, kein Weckvorgang.
       Genau hier ist Invariante §8.2 baulich erfuellt: der Zweig kehrt
       zurueck, **bevor** irgendetwas den Stack anfassen koennte.
    2. **Bereit machen** (Phase 15/16), erst dann weiterleiten. Wie lange
       eine Anfrage dafuer gehalten werden darf, steht in
       ``wake.hold_timeout_s`` (§6).
    3. **Einlagern**, wenn die Antwort dafuer taugt (HTTP 200 und
       ``status: "ok"`` — :func:`~acoustid_watchdog.cache.
       is_cacheable_response`).

    Die Laufzeit-Konfiguration wird dabei **bei jeder Anfrage frisch
    gelesen** (``cache.enabled``, ``cache.max_size_mb``,
    ``wake.hold_timeout_s``) — die Admin-UI kann sie aendern, ohne dass der
    Waechter neu starten muss (Muster der :class:`~acoustid_watchdog.
    lifecycle.IdleStopper`).

    Zwei Faelle beantwortet der Waechter selbst (§7 „Fehlerverhalten"):

    * Der Stack wird nicht rechtzeitig bereit oder laesst sich nicht
      starten -> ``503`` mit ``Retry-After``.
    * Die API bricht die Uebertragung ab -> ebenfalls ``503``; zusaetzlich
      gilt die Bereitschaft als verfallen, damit die naechste Anfrage
      wieder prueft (der Stack kann von Hand gestoppt worden sein).

    Alles andere — auch jeder Fehler der API — geht unveraendert durch.

    Hier steht ausserdem die eine Zeile, an der der Idle-Stopp haengt: eine
    Anfrage unter ``/v2/`` ist **Aktivitaet** (ARCHITECTURE §6
    „Idle-Definition") und verschiebt den Auto-Stopp. Gezaehlt wird die
    ankommende Anfrage, nicht die fertige Antwort — sonst hielte ein Client,
    der mitten in der Uebertragung abbricht, den Stack nicht wach, obwohl er
    ihn gerade benutzt hat. `/status` und die Admin-UI zaehlen bewusst
    nicht: sie beruehren das Array nie (Invariante §8.2) und duerfen es
    folglich auch nicht wachhalten.

    **Ein Cache-Hit zaehlt aus demselben Grund nicht.** Er braucht das
    Array nicht — er hat es ja gerade nicht angefasst. Ein wacher Stack,
    den nur noch Treffer erreichen, darf einschlafen; die naechste Anfrage,
    die wirklich zur API muss, weckt ihn wieder. Wuerde ein Treffer die
    Uhr anfassen, hielte ausgerechnet der Cache den Stack wach, den er
    ueberfluessig machen soll.
    """
    service: WatchdogService = request.app.state.service
    config = _runtime_config(service)
    plan = await plan_request(request, cache_enabled=config.cache.enabled)

    if plan.key is not None:
        cached = await run_in_threadpool(service.cache.get, plan.key)
        if cached is not None:
            return cached.to_response()

    service.activity.touch()
    try:
        await service.wake.ensure_ready(timeout_s=config.wake.hold_timeout_s)
    except StackNotReadyError as error:
        _LOG.warning(
            "Anfrage abgewiesen, Stack nicht bereit",
            extra={"path": request.url.path, "error": str(error)},
        )
        return error_response(503, str(error), retry_after_s=error.retry_after_s)

    try:
        return await _forward(request, service, plan, config)
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


async def _forward(
    request: Request, service: WatchdogService, plan: RequestPlan, config: Config
) -> Response:
    """Weiterleiten und, wo es sich lohnt, die Antwort auswerten.

    Zwei Nachbereitungen haengen hier dran, beide aus Phase 17:

    * **Einlagern** einer erfolgreichen Lookup-Antwort.
    * **Cache leeren** nach einer erfolgreichen lokalen Submission
      (Invariante §8.6). Gemessen wird am HTTP-Status: die API beantwortet
      einen fehlgeschlagenen Submit nie mit ``200`` (eigene Statuszeile je
      Fehlercode, docs/api-submit.md). Zu oft leeren waere ohnehin die
      harmlose Richtung — der Cache fuellt sich von selbst wieder.
    """
    if plan.key is None:
        response = await service.proxy.forward(request, content=plan.content)
        if plan.invalidates and response.status_code == 200:
            await run_in_threadpool(service.invalidate_cache, "submission")
        return response

    response, body = await service.proxy.forward_capturing(
        request, limit=MAX_CACHEABLE_BODY_BYTES, content=plan.content
    )
    if body is not None and is_cacheable_response(response, body):
        await run_in_threadpool(
            service.cache.put,
            plan.key,
            CachedResponse.capture(response, body),
            max_size_bytes=config.cache.max_size_mb * 1024 * 1024,
        )
    return response


def _runtime_config(service: WatchdogService) -> Config:
    """Die laufende Konfiguration — oder die Defaults aus §6.

    Eine unlesbare ``config.yaml`` darf den Proxy nicht lahmlegen; dann
    gelten die dokumentierten Vorgabewerte. Dieselbe Haltung wie im
    :class:`~acoustid_watchdog.lifecycle.IdleStopper`.
    """
    try:
        return service.config
    except Exception:
        _LOG.exception("Laufzeit-Konfiguration nicht lesbar, Vorgabewerte werden benutzt")
        return Config()


def build_app() -> FastAPI:
    """Anwendungsfabrik fuer uvicorn (``acoustid_watchdog.main:build_app``)."""
    return create_app()
