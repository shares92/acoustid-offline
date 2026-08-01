"""Interner Healthcheck ``GET /_health`` (Phase 15).

Der Endpunkt ist die Bereitschaftsfrage des Waechters — an ihm haengt, ob
eine gehaltene Client-Anfrage durchgelassen oder mit 503 abgewiesen wird.
Deshalb wird hier nicht nur „antwortet er" geprueft, sondern auch, dass er
in beide Richtungen ehrlich ist: keine bestandene Pruefung ohne Datenbank
und Index, aber auch kein Nichtbestehen wegen MusicBrainz.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi.testclient import TestClient
from stubs import StubConnection, StubIndex, StubPool, StubService

from acoustid_api.health import HEALTH_PATH
from acoustid_api.main import create_app


@contextmanager
def _client(service: StubService) -> Iterator[TestClient]:
    """Testclient auf einen eigens gebauten Attrappen-Dienst."""
    with TestClient(create_app(service)) as test_client:  # type: ignore[arg-type]
        yield test_client


def test_path_is_outside_the_public_contract() -> None:
    """Nicht unter ``/v2/`` — der §7-Vertrag bleibt unberuehrt."""
    assert HEALTH_PATH == "/_health"
    assert not HEALTH_PATH.startswith("/v2")


def test_ready_stack_answers_200(client: TestClient) -> None:
    response = client.get(HEALTH_PATH)

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "version": "0.0.1",
        "checks": {"db": "ok", "index": "ok"},
    }


def test_database_check_really_asks_the_database(service: StubService) -> None:
    """Ein ``SELECT 1`` muss wirklich auf der Verbindung landen.

    Sonst wuerde der Healthcheck nur bestaetigen, dass ein Pool-Objekt
    existiert — und der Waechter liesse Anfragen auf eine Datenbank los,
    die noch in der Recovery steckt.
    """
    with _client(service) as client:
        client.get(HEALTH_PATH)

    assert service.connection.queries == ["SELECT 1"]


def test_unreachable_database_answers_503(service: StubService) -> None:
    service.pool = StubPool(service.connection, error=OSError("connection refused"))

    with _client(service) as client:
        response = client.get(HEALTH_PATH)

    assert response.status_code == 503
    assert response.json()["status"] == "error"
    assert response.json()["checks"]["db"] == "connection refused"
    assert response.json()["checks"]["index"] == "ok"


def test_missing_index_answers_503() -> None:
    """Vor dem Bootstrap gibt es den Index nicht — dann ist nichts bereit."""
    service = StubService(index=StubIndex(healthy=False))

    with _client(service) as client:
        response = client.get(HEALTH_PATH)

    assert response.status_code == 503
    assert response.json()["checks"]["index"] == "Index 'main' fehlt oder laedt noch"


def test_unreachable_index_answers_503() -> None:
    service = StubService(index=StubIndex(health_error=OSError("no route to host")))

    with _client(service) as client:
        response = client.get(HEALTH_PATH)

    assert response.status_code == 503
    assert response.json()["checks"]["index"] == "no route to host"


def test_missing_musicbrainz_does_not_spoil_readiness() -> None:
    """Invariante §8.7: ohne MB laeuft der Dienst degradiert weiter.

    Waere MB Teil der Bereitschaft, wuerde ein Ausfall beim Spiegel jede
    Anfrage in ein 503 des Waechters laufen lassen — obwohl Lookups ohne
    Metadaten einwandfrei funktionieren.
    """
    service = StubService(mb=None)

    with _client(service) as client:
        assert client.get(HEALTH_PATH).status_code == 200


def test_reasons_stay_single_line() -> None:
    """Mehrzeilige Treiberfehler wuerden das JSON-Log unleserlich machen."""
    service = StubService(
        connection=StubConnection(),
    )
    service.pool = StubPool(service.connection, error=OSError("kaputt\n  mit Zeilenumbruch"))

    with _client(service) as client:
        reason = client.get(HEALTH_PATH).json()["checks"]["db"]

    assert reason == "kaputt mit Zeilenumbruch"


def test_health_is_not_a_lookup_route(client: TestClient) -> None:
    """Kein ``format``, kein AcoustID-Fehlerformat — der Endpunkt ist intern."""
    response = client.get(f"{HEALTH_PATH}?format=xml")

    assert response.headers["content-type"].startswith("application/json")
    assert "error" not in response.json()
