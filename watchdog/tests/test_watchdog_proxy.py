"""Reverse-Proxy ``/v2/*`` — durchreichen, nicht umformen (Phase 15).

Geprueft wird an der echten Anwendung (``TestClient``) mit einer
API-Attrappe dahinter: so laufen Routing, Weck-Logik und Proxy in genau der
Reihenfolge mit, in der sie im Betrieb stehen.

Der rote Faden aller Tests: **was ankommt, geht weiter; was zurueckkommt,
geht zurueck.** Der Proxy hat keine eigene Meinung ueber Methoden, Rumpf,
Kopfzeilen oder Fehlerbilder der API.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Callable

import httpx
from fastapi.testclient import TestClient
from watchdog_stubs import FakeDaemon, RecordingProxyTransport, docker_client, probe, streamed

from acoustid_watchdog.config_store import ConfigStore
from acoustid_watchdog.events import EventLevel, recent_events
from acoustid_watchdog.main import create_app
from acoustid_watchdog.proxy import HOP_BY_HOP_HEADERS, ReverseProxy
from acoustid_watchdog.service import WatchdogService
from acoustid_watchdog.store import Database
from shared.env import EnvSettings
from shared.models import StackState

FINGERPRINT = "AQABz0qUkZK4oOfhL-CPc4e5C_wW2H2QH9uPLsdxHT2"

Responder = Callable[[httpx.Request], httpx.Response]


def app_with(daemon: FakeDaemon, env_settings: EnvSettings, responder: Responder) -> TestClient:
    """Waechter mit einer API-Attrappe, die ``responder`` beantwortet.

    Fuer die Faelle, in denen die Antwort der API selbst der Pruefgegenstand
    ist; sonst genuegen die Fixtures ``client``/``upstream``.
    """
    service = WatchdogService(
        env_settings,
        Database.for_data_dir(env_settings.data_dir),
        ConfigStore.from_path(env_settings.config_path),
        docker=docker_client(daemon),
        probe=probe(lambda request: httpx.Response(200 if daemon.all_running else 503)),
        proxy=ReverseProxy(
            env_settings.api_base_url,
            client=httpx.AsyncClient(transport=httpx.MockTransport(responder)),
        ),
    ).open()
    return TestClient(create_app(service))


# --- Weiterleitung ----------------------------------------------------------


def test_get_is_forwarded_with_path_and_query(
    client: TestClient, upstream: RecordingProxyTransport
) -> None:
    response = client.get(f"/v2/lookup?client=abc&fingerprint={FINGERPRINT}&duration=641")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "results": []}

    forwarded = upstream.last
    assert forwarded.method == "GET"
    assert forwarded.url.path == "/v2/lookup"
    # Der Query-String geht als rohe Bytes weiter — keine Normalisierung,
    # an der die Chromaprint-Base64 haengen koennte.
    assert forwarded.url.query.decode() == f"client=abc&fingerprint={FINGERPRINT}&duration=641"
    assert forwarded.headers["host"] == "acoustid-api:8080"


def test_post_body_arrives_unchanged(client: TestClient, upstream: RecordingProxyTransport) -> None:
    body = f"client=abc&fingerprint={FINGERPRINT}&duration=641"

    client.post(
        "/v2/lookup",
        content=body.encode(),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    assert upstream.payload() == body
    assert upstream.last.headers["content-type"] == "application/x-www-form-urlencoded"


def test_gzip_body_is_not_unpacked_by_the_proxy(
    client: TestClient, upstream: RecordingProxyTransport
) -> None:
    """Das Entpacken (und die 1-MiB-Grenze) ist Sache der API.

    Wuerde der Proxy hier entpacken, gaebe es zwei Stellen mit einer
    Groessengrenze — und die Fehlerantwort 19/413 kaeme aus der falschen.
    """
    packed = gzip.compress(b"client=abc&duration=641")

    client.post(
        "/v2/lookup",
        content=packed,
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "content-encoding": "gzip",
        },
    )

    assert upstream.last.content == packed
    assert upstream.last.headers["content-encoding"] == "gzip"


def test_json_body_of_the_batch_endpoint_survives(
    client: TestClient, upstream: RecordingProxyTransport
) -> None:
    payload = {"client": "abc", "queries": [{"fingerprint": FINGERPRINT, "duration": 641}]}

    client.post("/v2/lookup/batch", json=payload)

    assert json.loads(upstream.payload()) == payload


def test_large_body_passes_without_a_limit_in_the_proxy(
    client: TestClient, upstream: RecordingProxyTransport
) -> None:
    """Der Proxy zaehlt keine Bytes — er streamt.

    Die 1-MiB-Grenze (Fehler 19/413) gehoert der API; ein zweiter Zaehler
    hier wuerde Picards Batch-Verkleinerung an der falschen Stelle
    ausloesen. Deshalb geht auch ein Rumpf jenseits der Grenze durch — die
    Absage kommt von der API.
    """
    body = b"x" * (2 * 1024 * 1024)

    client.post("/v2/lookup", content=body)

    assert len(upstream.last.content) == len(body)


def test_hop_by_hop_headers_are_dropped(
    client: TestClient, upstream: RecordingProxyTransport
) -> None:
    """Verbindungsgebundene Kopfzeilen gelten nur fuer eine Strecke.

    Der Client redet mit dem Waechter, der Waechter mit der API — die
    Rahmung der beiden Verbindungen ist unabhaengig. Der Wunsch des Clients
    (``Connection: close``) darf deshalb nicht auf die zweite Strecke
    durchschlagen; ``Connection`` und ``Host`` setzt dort httpx selbst.
    """
    client.get(
        "/v2/lookup",
        headers={"connection": "close", "te": "trailers", "upgrade": "websocket"},
    )

    forwarded = upstream.last.headers
    assert {name.lower() for name in forwarded} & HOP_BY_HOP_HEADERS == {"connection", "host"}
    assert forwarded["connection"] == "keep-alive"
    assert forwarded["host"] == "acoustid-api:8080"


def test_client_headers_are_passed_through(
    client: TestClient, upstream: RecordingProxyTransport
) -> None:
    client.get("/v2/lookup", headers={"user-agent": "Picard/2.11", "x-forwarded-for": "10.0.0.9"})

    assert upstream.last.headers["user-agent"] == "Picard/2.11"
    assert upstream.last.headers["x-forwarded-for"] == "10.0.0.9"


# --- Antworten --------------------------------------------------------------


def test_api_error_is_passed_through_unchanged(
    daemon: FakeDaemon, env_settings: EnvSettings
) -> None:
    """Eine AcoustID-Fehlerantwort bleibt Byte fuer Byte dieselbe."""
    body = b'{"status": "error", "error": {"code": 2, "message": "missing parameter"}}'

    def responder(request: httpx.Request) -> httpx.Response:
        return streamed(400, body, {"content-type": "application/json"})

    with app_with(daemon, env_settings, responder) as client:
        response = client.get("/v2/lookup")

    assert response.status_code == 400
    assert response.content == body
    assert response.headers["content-type"] == "application/json"


def test_bare_405_of_the_batch_endpoint_stays_bare(
    daemon: FakeDaemon, env_settings: EnvSettings
) -> None:
    """`GET /v2/lookup/batch` liefert FastAPIs nacktes 405 (Hinweis Phase 13).

    Der Proxy erfindet daraus **kein** AcoustID-Fehlerbild: die Antwort
    waere dann eine andere als bei einem direkten Aufruf des Dienstes, und
    die 19er-Tabelle hat fuer diesen Fall keinen passenden Code.
    """

    def responder(request: httpx.Request) -> httpx.Response:
        return streamed(
            405,
            b'{"detail":"Method Not Allowed"}',
            {"content-type": "application/json", "allow": "POST"},
        )

    with app_with(daemon, env_settings, responder) as client:
        response = client.get("/v2/lookup/batch")

    assert response.status_code == 405
    assert response.json() == {"detail": "Method Not Allowed"}
    assert response.headers["allow"] == "POST"


def test_cors_header_of_the_api_survives(daemon: FakeDaemon, env_settings: EnvSettings) -> None:
    """``Access-Control-Allow-Origin: *`` ist Teil des Vertrags (§7)."""

    def responder(request: httpx.Request) -> httpx.Response:
        return streamed(200, b'{"status": "ok"}', {"access-control-allow-origin": "*"})

    with app_with(daemon, env_settings, responder) as client:
        response = client.get("/v2/lookup")

    assert response.headers["access-control-allow-origin"] == "*"


def test_unreachable_api_becomes_503_with_retry_after(
    daemon: FakeDaemon, env_settings: EnvSettings
) -> None:
    """Bricht die API weg, antwortet der Waechter selbst — im AcoustID-Format."""

    def responder(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with app_with(daemon, env_settings, responder) as client:
        response = client.get("/v2/lookup")

    assert response.status_code == 503
    assert response.headers["retry-after"] == "30"
    assert response.json()["status"] == "error"
    assert response.json()["error"]["code"] == 13


# --- Zusammenspiel mit der Weck-Logik ---------------------------------------


def test_request_wakes_the_sleeping_stack(
    client: TestClient, daemon: FakeDaemon, upstream: RecordingProxyTransport
) -> None:
    """Der Kern der Phase: eine Anfrage weckt den Stack und wird beantwortet."""
    assert daemon.all_running is False

    response = client.get("/v2/lookup?client=abc")

    assert response.status_code == 200
    assert daemon.all_running is True
    assert len(upstream.requests) == 1


def test_wake_timeout_answers_503(daemon: FakeDaemon, env_settings: EnvSettings) -> None:
    """Der Stack wird nicht bereit -> 503 + Retry-After, ohne Weiterleitung."""
    forwarded: list[httpx.Request] = []

    def responder(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        forwarded.append(request)
        return streamed(200)

    service = WatchdogService(
        env_settings,
        Database.for_data_dir(env_settings.data_dir),
        ConfigStore.from_path(env_settings.config_path),
        docker=docker_client(daemon),
        # Antwortet nie mit 200 — der Stack wird also nie bereit.
        probe=probe(lambda request: httpx.Response(503)),
        proxy=ReverseProxy(
            env_settings.api_base_url,
            client=httpx.AsyncClient(transport=httpx.MockTransport(responder)),
        ),
    ).open()
    config = service.config
    service.config_store.save(
        config.model_copy(update={"wake": config.wake.model_copy(update={"hold_timeout_s": 1})})
    )

    with TestClient(create_app(service)) as client:
        response = client.get("/v2/lookup?client=abc")

    assert response.status_code == 503
    assert response.headers["retry-after"] == "30"
    assert response.json()["error"]["code"] == 13
    assert forwarded == []
    # Die Container wurden trotzdem gestartet — sie brauchen nur laenger.
    assert daemon.all_running is True


def test_start_failure_answers_503_without_naming_internals(
    env_settings: EnvSettings, upstream: RecordingProxyTransport
) -> None:
    """Stack-Start-Fehler -> 503 + Ereignis (§7, Phase 16), Text generisch.

    Seit Phase 18 nennt die Antwort **keine Interna** mehr: bis dahin stand
    der Containername im Fehlertext — im LAN unkritisch, bei einer nach
    aussen exponierten Instanz eine Auskunft ueber den inneren Aufbau an
    jeden, der fragt. Der Grund steht jetzt nur noch im Ereignis-Log; der
    Client bekommt den Wortlaut, den auch das Original zu Code 13 schickt.

    Und der Weg zurueck: der Betreiber legt den fehlenden Container an, die
    naechste Anfrage weckt — ohne Neustart des Waechters.
    """
    # `acoustid-api` gibt es nicht: der Weckvorgang scheitert am Daemon.
    daemon = FakeDaemon({"acoustid-db": False, "acoustid-index": False})
    service = WatchdogService(
        env_settings,
        Database.for_data_dir(env_settings.data_dir),
        ConfigStore.from_path(env_settings.config_path),
        docker=docker_client(daemon),
        probe=probe(lambda request: httpx.Response(200 if daemon.all_running else 503)),
        proxy=ReverseProxy(
            env_settings.api_base_url,
            client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
        ),
    ).open()

    with TestClient(create_app(service)) as client:
        response = client.get("/v2/lookup?client=abc")

        assert response.status_code == 503
        assert response.json()["error"]["code"] == 13
        # Der Wortlaut des Originals — und kein Wort ueber den Aufbau.
        assert response.json()["error"]["message"] == (
            "service currently unavailable, try again later"
        )
        assert "acoustid-api" not in response.text
        assert service.state.state is StackState.ERROR
        assert upstream.requests == []

        failure = next(
            event
            for event in recent_events(service.db, limit=20)
            if event.message == "Stack-Start fehlgeschlagen"
        )
        assert failure.level is EventLevel.ERROR
        assert failure.source == "wake"
        # Der Grund ist nicht verloren — er steht im Ereignis-Log.
        assert "acoustid-api" in str(failure.extra)

        daemon.containers["acoustid-api"] = False  # Container ist da
        assert client.get("/v2/lookup?client=abc").status_code == 200

    assert service.state.state is StackState.READY
    assert daemon.all_running is True


def test_every_forwarded_v2_request_counts_as_activity(client: TestClient) -> None:
    """Die Uhr des Idle-Stopps haengt am Proxy-Pfad (§6 „Idle-Definition").

    Zwei **verschiedene** Anfragen, damit die zweite wirklich weitergeleitet
    wird: seit Phase 17 zaehlt ein Cache-Treffer bewusst nicht als
    Aktivitaet (er benutzt das Array ja nicht — siehe
    ``test_watchdog_cache.py``).
    """
    service: WatchdogService = client.app.state.service
    before = service.activity.requests

    client.get("/v2/lookup?client=abc&fingerprint=eins")
    client.get("/v2/lookup?client=abc&fingerprint=zwei")

    assert service.activity.requests == before + 2
    assert service.activity.idle_s < 1


def test_status_is_no_activity(client: TestClient) -> None:
    """`/status` beruehrt das Array nie und haelt es folglich nicht wach."""
    service: WatchdogService = client.app.state.service
    before = service.activity.requests

    client.get("/status")

    assert service.activity.requests == before


def test_status_never_touches_docker(client: TestClient, daemon: FakeDaemon) -> None:
    """Invariante §8.2: der Statusendpunkt weckt nie — auch nicht ueber Docker.

    Der Waechter fragt beim Start einmal nach dem Zustand; danach darf
    `/status` keinen einzigen Docker-Aufruf mehr ausloesen.
    """
    daemon.calls.clear()

    assert client.get("/status").status_code == 200

    assert daemon.calls == []


def test_second_request_does_not_wake_again(client: TestClient, daemon: FakeDaemon) -> None:
    client.get("/v2/lookup?client=abc")
    daemon.calls.clear()

    client.get("/v2/lookup?client=abc")

    assert daemon.count("start") == 0
