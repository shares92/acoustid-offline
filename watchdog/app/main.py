"""HTTP-Schicht des Waechters (FastAPI) — ARCHITECTURE §4, §7.

Stand M2.5 traegt sie vier Routen:

===============  ======  =================================================
``/status``      GET     Stack-Zustand, Datenstand, letzter Update-Lauf,
                         Version — **weckt nie** (§7, §8.2), und **ohne
                         Auth und ohne Rate-Limit**
``/metrics``     GET     Kennzahlen im Prometheus-Format, **nur** bei
                         ``metrics.enabled`` (§6) — sonst 404. Weckt
                         ebenfalls nie (:mod:`acoustid_watchdog.metrics`)
``/v2/{...}``    alle    Rate-Limit, Key-Pruefung, Lookup-Cache,
                         Reverse-Proxy auf den API-Dienst, **mit
                         Weck-Logik** (§7 „Fehlerverhalten") und der
                         Aktivitaetsmeldung fuer den Idle-Stopp
``/_health``     alle    **404** — der interne Healthcheck des API-Dienstes
                         wird nie weitergereicht (:data:`DENIED_PATHS`)
===============  ======  =================================================

Seit Phase 17 weckt `/v2/*` nur noch dann, wenn die Antwort nicht schon im
Lookup-Cache liegt — die zweite Stelle, an der Invariante §8.2 baulich
haengt (:func:`_proxy`). Seit Phase 18 stehen Rate-Limit und Key-Pruefung
**davor**: auch eine abgewiesene Anfrage weckt nichts, und ein Cache-Treffer
ist genauso geschuetzt wie eine weitergeleitete Anfrage (§7 „Durchsetzungsort
Auth & Rate-Limit").

`/status` bleibt bewusst offen. Es ist die Bereitschaftsanzeige aus §7,
zugleich der Container-Healthcheck und die Datenquelle der Admin-Statuskarte
— eine Auskunft, fuer die es keinen Key gibt und die auch dann noch
funktionieren muss, wenn alle Keys falsch sind.

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

Es folgt die Admin-UI unter ``/admin`` (M8). Alles laeuft ueber **einen**
Port (``MMO_PORT``, Default 8080; ARCHITECTURE §6 „Feste Werte"), weil der
Waechter der einzige nach aussen sichtbare Dienst ist.

Seit M2.5 laeuft im Lifespan ein dritter Dauerlaeufer: der Zeitplan
(:mod:`acoustid_watchdog.scheduler`). Er ist der einzige, der die Instanz
von selbst aufweckt — Idle-Stopp und Zustandsabgleich raeumen nur auf.

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
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.concurrency import run_in_threadpool

from acoustid_watchdog.auth import AuthOutcome, AuthResult
from acoustid_watchdog.cache import (
    MAX_CACHEABLE_BODY_BYTES,
    CachedResponse,
    RequestPlan,
    is_cacheable_response,
    plan_request,
)
from acoustid_watchdog.metrics import CONTENT_TYPE as METRICS_CONTENT_TYPE
from acoustid_watchdog.metrics import render as render_metrics
from acoustid_watchdog.proxy import (
    ERROR_INVALID_API_KEY,
    ERROR_MESSAGES,
    ERROR_MISSING_PARAMETER,
    ERROR_RATE_LIMIT,
    ERROR_REQUEST_TOO_LARGE,
    ERROR_SERVICE_UNAVAILABLE,
    error_response,
)
from acoustid_watchdog.ratelimit import UNKNOWN_CLIENT, WINDOW_S
from acoustid_watchdog.service import WatchdogService
from acoustid_watchdog.status import build_status
from acoustid_watchdog.wake import DEFAULT_RETRY_AFTER_S, StackNotReadyError
from shared import setup_logging
from shared.config import Config
from shared.env import EnvSettings
from shared.models import AuthMode

__all__ = ["DENIED_PATHS", "PROXY_METHODS", "SERVICE_NAME", "build_app", "create_app"]

_LOG = logging.getLogger(__name__)

#: Name in jeder Logzeile (`service`) — zugleich der Container-Name aus
#: ARCHITECTURE §6.
SERVICE_NAME: Final = "acoustid-watchdog"

#: Methoden, die der Proxy annimmt. Absichtlich alle gaengigen: welche
#: unter ``/v2`` erlaubt sind, sagt die API (siehe Modul-Docstring).
PROXY_METHODS: Final = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

#: Pfade, die der Waechter **nie** weiterreicht — mit ausdruecklicher Route
#: statt „faellt hinten runter".
#:
#: Heute gaebe es sie ohnehin nicht: der Proxy kennt nur ``/v2/{path}``, und
#: alles andere ist 404. Genau darin liegt aber das Risiko (R12 der
#: M0-Analyse): der interne Healthcheck des API-Dienstes
#: (``acoustid_api.health``) ist **nur deshalb** geschuetzt. Die
#: Scope-Erweiterung (M3-M7) bringt vier weitere Pfadfamilien —
#: ``/caa/*``, ``/discogs/*``, ``/tadb/*``, ``/v1/*`` —, und spaetestens
#: eine breitere Allowlist wuerde ``/_health`` erreichbar machen. Die Regel
#: steht deshalb jetzt da, mitsamt Test: sie schreibt die Invariante fest,
#: statt sie einem Routing-Zufall zu ueberlassen.
DENIED_PATHS: Final = ("/_health",)


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
            # Der dritte Dauerlaeufer (M2.5) — der einzige, der die Instanz
            # von selbst aufweckt.
            asyncio.create_task(running.scheduler.run(), name="scheduler"),
            # Und der vierte: die Logdatei, die `tee` schreibt, haelt sonst
            # niemand klein (:mod:`acoustid_watchdog.logrotate`).
            asyncio.create_task(running.logrotate.run(), name="log-rotator"),
        ]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            # Auf einen laufenden Job **warten**, ihm aber kein eigenes
            # Signal schicken (`JobManager.shutdown`): das SIGTERM kommt
            # von supervisord an die ganze Prozessgruppe, und ein zweites
            # bedeutete im Importer „sofort beenden". Wer hier nicht
            # wartet, hinterlaesst einen Waisen unter `tini` — mit
            # Busy-Marke und offener `update_run`-Zeile.
            await running.job_manager.shutdown()
            # Auf das Ende der Dauerlaeufer warten, sonst meldet asyncio
            # „Task was destroyed but it is pending".
            await asyncio.gather(*tasks, return_exceptions=True)
            if owns_service:
                await app.state.service.aclose()

    app = FastAPI(
        title="musicmeta-offline Waechter",
        summary="Status, Proxy und Admin-UI der selbst gehosteten Instanz",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.get("/status")
    async def status(request: Request) -> JSONResponse:
        return await _status(request)

    @app.get("/metrics")
    async def metrics(request: Request) -> Response:
        return await _metrics(request)

    for denied_path in DENIED_PATHS:
        # Vor der Proxy-Route registriert: FastAPI nimmt die erste passende,
        # und eine spaetere Allowlist darf diese hier nicht ueberholen.
        app.add_api_route(
            denied_path,
            _denied,
            methods=PROXY_METHODS,
            include_in_schema=False,
        )

    @app.api_route("/v2/{path:path}", methods=PROXY_METHODS)
    async def proxy(request: Request, path: str) -> Response:
        return await _proxy(request)

    return app


async def _denied(request: Request) -> Response:
    """Ein gesperrter Pfad — 404, ohne den Stack anzufassen.

    **404 und nicht 403**: der Waechter gibt nach aussen nicht preis, dass
    es diesen Endpunkt intern gibt (dieselbe Haltung wie bei den
    503-Antworten, die keine Prozessnamen nennen). Und ausdruecklich ohne
    Rate-Limit, Auth, Cache und Weck-Logik: die Antwort steht fest, bevor
    irgendetwas geprueft werden muesste.
    """
    _LOG.info("Gesperrter Pfad abgewiesen", extra={"path": request.url.path})
    return JSONResponse({"status": "error", "error": {"message": "not found"}}, status_code=404)


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
        data: dict[str, Any] = await run_in_threadpool(
            build_status, service.db, service.state, service.settings
        )
    except Exception:
        _LOG.exception("Statusabfrage fehlgeschlagen")
        return JSONResponse(
            {"status": "error", "error": {"message": "Statusabfrage fehlgeschlagen"}},
            status_code=500,
        )
    return JSONResponse(data)


async def _metrics(request: Request) -> Response:
    """Kennzahlen im Prometheus-Format — **nur** bei ``metrics.enabled`` (§6).

    Abgeschaltet antwortet der Pfad mit **404** und nicht mit 403: der
    Waechter gibt nach aussen nicht preis, dass es diesen Endpunkt gibt
    (dieselbe Haltung wie bei :data:`DENIED_PATHS`).

    Wie `/status` ist er offen — kein Key, kein Rate-Limit — und **weckt
    nie**: gelesen werden nur Zaehler aus dem Speicher und die
    Zustandsdatenbank auf dem Cache-Pool (:mod:`acoustid_watchdog.metrics`).
    """
    service: WatchdogService = request.app.state.service
    if not _runtime_config(service).metrics.enabled:
        return await _denied(request)
    try:
        body = await run_in_threadpool(render_metrics, service)
    except Exception:
        _LOG.exception("Kennzahlen nicht erhebbar")
        return PlainTextResponse("# Kennzahlen nicht erhebbar\n", status_code=500)
    return PlainTextResponse(body, media_type=METRICS_CONTENT_TYPE)


async def _proxy(request: Request) -> Response:
    """Bremsen, pruefen, Cache fragen, sonst wecken und weiterleiten.

    Die Reihenfolge ist der ganze Punkt der Phasen 17 und 18 — und sie ist
    **von aussen nach innen** gebaut: jeder Schritt ist teurer als der davor,
    also steht der billigste vorn.

    1. **Rate-Limit** (Phase 18, §6 ``ratelimit.per_ip_per_min``). Eine
       Rechnung auf einer Liste im Speicher, ohne Datenbank, ohne Rumpf.
       Sie steht vor allem anderen, weil sie sonst nur das schuetzte, was
       hinter ihr liegt — eine Flut ungueltiger Keys wuerde die Key-Pruefung
       ungebremst treffen.
    2. **Auth** (Phase 18, §6 ``auth.mode``). Nur im ``apikey``-Modus: ein
       Indexzugriff auf die Zustandsdatenbank. Sie steht **vor** dem Cache,
       weil sonst ausgerechnet der billige Weg der ungeschuetzte waere: eine
       Antwort aus dem Cache erreicht die API nie, und die API prueft
       ohnehin keine Keys (§7 „Durchsetzungsort Auth & Rate-Limit").
    3. **Cache** (Phase 17). Liegt die Antwort schon da, geht sie sofort
       zurueck — kein Docker-Kontakt, kein API-Kontakt, kein Weckvorgang.
       Genau hier ist Invariante §8.2 baulich erfuellt: der Zweig kehrt
       zurueck, **bevor** irgendetwas den Stack anfassen koennte.
    4. **Bereit machen** (Phase 15/16), erst dann weiterleiten. Wie lange
       eine Anfrage dafuer gehalten werden darf, steht in
       ``wake.hold_timeout_s`` (§6).
    5. **Einlagern**, wenn die Antwort dafuer taugt (HTTP 200 und
       ``status: "ok"`` — :func:`~acoustid_watchdog.cache.
       is_cacheable_response`).

    Die ersten beiden Schritte laufen **ausschliesslich aus Waechter-Daten**
    (eine Liste im Speicher, eine SQLite auf dem Cache-Pool) — sie koennen
    den Stack gar nicht anfassen. Damit gilt Invariante §8.2 auch fuer eine
    abgewiesene Anfrage: sie weckt nie.

    Eine technische Fussnote zur Reihenfolge: ``plan_request`` liegt im Code
    **vor** der Auth-Pruefung, weil der ``client``-Parameter bei einer
    POST-Anfrage im Rumpf steht und der Rumpf nur **einmal** gelesen werden
    darf. Gelesen wird dabei nichts als der Rumpf — geprueft wird weiterhin
    vor jedem Cache-Zugriff und vor jedem Weckvorgang.

    Die Laufzeit-Konfiguration wird dabei **bei jeder Anfrage frisch
    gelesen** (``ratelimit.per_ip_per_min``, ``auth.mode``,
    ``auth.allow_known_client_keys``, ``cache.enabled``,
    ``cache.max_size_mb``, ``wake.hold_timeout_s``) — die Admin-UI kann sie
    aendern, ohne dass der Waechter neu starten muss (Muster der
    :class:`~acoustid_watchdog.lifecycle.IdleStopper`).

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

    limited = _rate_limit(request, service, config)
    if limited is not None:
        return limited

    apikey_mode = config.auth.mode is AuthMode.APIKEY
    plan = await plan_request(request, cache_enabled=config.cache.enabled, need_client=apikey_mode)
    if apikey_mode:
        rejected = await _authenticate(request, service, plan, config)
        if rejected is not None:
            return rejected

    if plan.key is not None:
        cached = await run_in_threadpool(service.cache.get, plan.key)
        if cached is not None:
            return cached.to_response()

    service.activity.touch()
    try:
        await service.wake.ensure_ready(timeout_s=config.wake.hold_timeout_s)
    except StackNotReadyError as error:
        # Der Grund steht im Log (und, wo er einen Zustandswechsel bedeutet,
        # im Ereignis-Log) — nicht in der Antwort: sie nennt keine
        # Containernamen und keine internen Adressen (Phase 18).
        _LOG.warning(
            "Anfrage abgewiesen, Stack nicht bereit",
            extra={"path": request.url.path, "error": str(error)},
        )
        return error_response(
            503,
            ERROR_MESSAGES[ERROR_SERVICE_UNAVAILABLE],
            retry_after_s=error.retry_after_s,
        )

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
            ERROR_MESSAGES[ERROR_SERVICE_UNAVAILABLE],
            retry_after_s=DEFAULT_RETRY_AFTER_S,
        )


def _rate_limit(request: Request, service: WatchdogService, config: Config) -> Response | None:
    """Das IP-Limit (§6 ``ratelimit.per_ip_per_min``) — ``None`` = darf durch.

    Gilt in **beiden** Auth-Modi und fuer alle ``/v2/*``-Routen. `/status`
    bleibt bewusst aussen vor: es ist die Bereitschaftsanzeige (§7), zugleich
    der Container-Healthcheck und die Datenquelle der Admin-Statuskarte, die
    laut §6 alle 5 s pollt. Ein Limit darauf koennte im ungluecklichen Fall
    die eigene Ueberwachung aussperren — und schuetzen muss es dort nichts:
    die Antwort kommt aus dem Speicher und beruehrt weder Stack noch Array.

    Gemessen wird die **direkte** Gegenstelle; ``X-Forwarded-For`` wird nicht
    ausgewertet (Begruendung im :mod:`~acoustid_watchdog.ratelimit`-Docstring).
    """
    client_ip = request.client.host if request.client else UNKNOWN_CLIENT
    limit = config.ratelimit.per_ip_per_min
    decision = service.ratelimit.check(client_ip, limit=limit)
    if decision.allowed:
        return None

    _LOG.warning(
        "Anfrage abgewiesen, Rate-Limit ueberschritten",
        extra={
            "path": request.url.path,
            "client_ip": client_ip,
            "limit_per_min": limit,
            "retry_after_s": decision.retry_after_s,
        },
    )
    # Die Meldung des Originals nennt eine Rate **je Sekunde** (Code 14);
    # unser Schluessel steht je Minute. Umgerechnet statt umformuliert: der
    # Wortlaut bleibt der der Fehlertabelle, die Zahl beschreibt unser Limit.
    return error_response(
        429,
        ERROR_MESSAGES[ERROR_RATE_LIMIT].format(rate=limit / WINDOW_S),
        code=ERROR_RATE_LIMIT,
        retry_after_s=decision.retry_after_s,
    )


async def _authenticate(
    request: Request, service: WatchdogService, plan: RequestPlan, config: Config
) -> Response | None:
    """Die Key-Pruefung des ``apikey``-Modus — ``None`` = darf durch.

    Die drei moeglichen Absagen tragen die Codes, die auch das Original
    schickt (Phase-1-Bericht, Fehlertabelle):

    ============================  =====  =====  ============================
    Fall                          Code   HTTP   Meldung
    ============================  =====  =====  ============================
    ``client`` fehlt              2      400    missing required parameter …
    ``client`` unbekannt/gesperrt 4      400    invalid API key
    Rumpf zu gross fuer den Key   19     413    request too large
    ============================  =====  =====  ============================

    Der dritte Fall ist kein eigener Vertrag, sondern eine vorgezogene
    Antwort: einen Rumpf ueber 1 MiB beantwortet die API selbst mit 19/413
    (``acoustid_api.main.MAX_BODY_BYTES``) — unabhaengig davon, welcher Key
    darin steht. Ihn dafuer erst durchzureichen hiesse, den Stack fuer eine
    Anfrage zu wecken, deren Antwort schon feststeht.
    """
    if plan.client_unreadable:
        _LOG.warning(
            "Anfrage abgewiesen, Rumpf zu gross fuer die Key-Pruefung",
            extra={"path": request.url.path},
        )
        return error_response(
            413,
            ERROR_MESSAGES[ERROR_REQUEST_TOO_LARGE],
            code=ERROR_REQUEST_TOO_LARGE,
        )

    result: AuthResult = await run_in_threadpool(
        service.auth.check,
        plan.client,
        allow_known=config.auth.allow_known_client_keys,
    )
    if result.ok:
        _LOG.debug(
            "Anfrage autorisiert",
            extra={"path": request.url.path, "key_id": result.key_id, "key_label": result.label},
        )
        return None

    _LOG.info(
        "Anfrage abgewiesen, Key nicht akzeptiert",
        extra={
            "path": request.url.path,
            "client_ip": request.client.host if request.client else None,
            "reason": result.outcome.value,
        },
    )
    if result.outcome is AuthOutcome.MISSING:
        return error_response(
            400,
            ERROR_MESSAGES[ERROR_MISSING_PARAMETER].format(name="client"),
            code=ERROR_MISSING_PARAMETER,
        )
    return error_response(400, ERROR_MESSAGES[ERROR_INVALID_API_KEY], code=ERROR_INVALID_API_KEY)


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
