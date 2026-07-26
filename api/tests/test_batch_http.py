"""``POST /v2/lookup/batch`` ueber HTTP — Reihenfolge, Teilfehler, Grenzen.

Der eigene Endpunkt aus ARCHITECTURE §7 hat kein Original-Vorbild; geprueft
wird deshalb nicht Kompatibilitaet, sondern der **eigene** Vertrag:

* Die Antwort steht in Anfragereihenfolge und traegt je Eintrag ihren Index.
* Ein kaputter Eintrag zwischen zwei guten reisst die anderen nicht.
* Was der ganzen Anfrage fehlt (``client``, ``queries``, Rumpfgrenze, mehr
  als 100 Eintraege), beendet sie im gewohnten Fehlerformat.
* ``meta`` kostet **ein** Bundel MB-Abfragen, nicht eins je Eintrag.

Wie in `test_lookup_http.py` laeuft die echte Anwendung mit Attrappen
(siehe conftest.py); Datenbank und Suchindex kommen in
`test_lookup_integration.py` dazu.
"""

from __future__ import annotations

import gzip
import json
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from original_examples import ERROR_TABLE
from stubs import StubConnection, StubMatcher, StubService, make_match

from acoustid_api.errors import ServiceUnavailableError
from acoustid_api.main import MAX_BODY_BYTES, create_app
from acoustid_api.params import MAX_BATCH_QUERIES
from shared.fingerprint import encode_fingerprint

VECTOR = [0x22222220 + index * 16 for index in range(300)]
FINGERPRINT = encode_fingerprint(VECTOR)
GID = UUID("b81f83ee-4da4-11e0-9ed8-0025225356f3")
MBID = "5f1b1f4c-1111-4111-8111-111111111111"

QUERY = {"fingerprint": FINGERPRINT, "duration": 241}

_ERROR_STATUS = {code: status for code, status, _ in ERROR_TABLE}


def post(client: TestClient, payload: Any, **kwargs: Any) -> Any:
    return client.post("/v2/lookup/batch", json=payload, **kwargs)


def batch(client: TestClient, queries: list[Any], **envelope: Any) -> dict[str, Any]:
    response = post(client, {"client": "testkey", "queries": queries, **envelope})
    assert response.status_code == 200, response.text
    return response.json()


# --- Erfolgsfall und Reihenfolge --------------------------------------------


def test_the_documented_shape_comes_back(client: TestClient, matcher: StubMatcher) -> None:
    matcher.matches = [make_match(score=0.98, gid=GID)]
    payload = batch(client, [QUERY])
    assert payload == {
        "status": "ok",
        "responses": [{"index": 0, "status": "ok", "results": [{"id": str(GID), "score": 0.98}]}],
    }


def test_the_answer_keeps_the_order_of_the_request(client: TestClient) -> None:
    """Die Zusicherung des Endpunkts — und sein Index macht sie nachpruefbar."""
    queries = [{**QUERY, "duration": 200 + step} for step in range(5)]
    payload = batch(client, queries)
    assert [entry["index"] for entry in payload["responses"]] == [0, 1, 2, 3, 4]


def test_every_entry_reaches_the_pipeline_with_its_own_values(
    client: TestClient, matcher: StubMatcher
) -> None:
    batch(
        client,
        [
            {**QUERY, "duration": 241},
            {**QUERY, "duration": 180, "maxdurationdiff": 30},
        ],
    )
    assert matcher.calls == [(len(VECTOR), 241, 7), (len(VECTOR), 180, 30)]


def test_an_empty_query_list_is_answered_with_an_empty_array(client: TestClient) -> None:
    """Ein leerer Batch ist eine gueltige Anfrage ohne Arbeit — kein Fehler."""
    assert batch(client, []) == {"status": "ok", "responses": []}


def test_a_trackid_entry_works_like_in_the_lookup() -> None:
    service = StubService(connection=StubConnection([(42, GID)]))
    with TestClient(create_app(service)) as client:  # type: ignore[arg-type]
        payload = batch(client, [{"trackid": str(GID)}])
    assert payload["responses"][0]["results"] == [{"id": str(GID), "score": 1.0}]


def test_the_response_is_always_json(client: TestClient) -> None:
    """`format` hat an diesem Endpunkt keine Wirkung — auch nicht als Fehler."""
    response = client.post("/v2/lookup/batch?format=xml", json={"client": "k", "queries": []})
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json; charset=UTF-8"


def test_client_may_come_from_the_query_string(client: TestClient) -> None:
    response = client.post("/v2/lookup/batch?client=testkey", json={"queries": [QUERY]})
    assert response.status_code == 200


def test_the_query_string_wins_over_the_envelope(client: TestClient, caplog: Any) -> None:
    with caplog.at_level("INFO"):
        response = client.post(
            "/v2/lookup/batch?client=ausdemurl",
            json={"client": "ausdemrumpf", "queries": []},
        )
    assert response.status_code == 200
    assert any(getattr(record, "client", None) == "ausdemurl" for record in caplog.records)


def test_get_is_not_offered(client: TestClient) -> None:
    """Der Endpunkt lebt von seinem Rumpf; ein GET mit Rumpf ist kein Vertrag."""
    assert client.get("/v2/lookup/batch").status_code == 405


def test_every_response_allows_any_origin(client: TestClient) -> None:
    ok = post(client, {"client": "k", "queries": []})
    error = post(client, {"queries": []})
    assert ok.headers["access-control-allow-origin"] == "*"
    assert error.headers["access-control-allow-origin"] == "*"


# --- Teilfehler --------------------------------------------------------------


def test_a_broken_entry_between_two_good_ones_does_not_break_them(
    client: TestClient, matcher: StubMatcher
) -> None:
    """Die Definition of Done der Phase 13."""
    matcher.matches = [make_match(score=0.9, gid=GID)]
    payload = batch(client, [QUERY, {"fingerprint": "kaputt!!", "duration": 241}, QUERY])

    responses = payload["responses"]
    assert [entry["status"] for entry in responses] == ["ok", "error", "ok"]
    assert responses[1] == {
        "index": 1,
        "status": "error",
        "error": {"code": 3, "message": "invalid fingerprint"},
    }
    assert responses[0]["results"] == responses[2]["results"] == [{"id": str(GID), "score": 0.9}]
    # Der kaputte Eintrag hat die Pipeline gar nicht erst erreicht.
    assert len(matcher.calls) == 2


@pytest.mark.parametrize(
    ("query", "code"),
    [
        ({"fingerprint": FINGERPRINT}, 2),
        ({"duration": 241}, 2),
        ({}, 2),
        ({"fingerprint": "kaputt!!", "duration": 241}, 3),
        ({"trackid": "keine-uuid"}, 7),
        ({**QUERY, "maxdurationdiff": 99}, 11),
        ({**QUERY, "duration": 0}, 2),
        ({**QUERY, "duration": "abc"}, 2),
        ({**QUERY, "duration": None}, 2),
        ({**QUERY, "fingerprint": ["nicht", "skalar"]}, 2),
        ("kein Objekt", 2),
        (42, 2),
    ],
)
def test_entry_errors_use_the_acoustid_format(client: TestClient, query: Any, code: int) -> None:
    entry = batch(client, [query])["responses"][0]
    assert entry["status"] == "error"
    assert entry["error"]["code"] == code
    assert entry["index"] == 0


def test_a_partial_failure_keeps_http_200(client: TestClient) -> None:
    """Ein anderer Status wuerde Clients die ganze Antwort verwerfen lassen."""
    response = post(client, {"client": "k", "queries": [{"fingerprint": "kaputt!!"}]})
    assert response.status_code == 200


# --- Anfragefehler (die reissen alles) ---------------------------------------


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"queries": [QUERY]}, 2),
        ({"client": "k"}, 2),
        ({"client": "k", "queries": {"nicht": "eine Liste"}}, 2),
        ({"client": "k", "queries": [QUERY], "maxdurationdiff": 99}, 11),
        ([{"client": "k"}], 2),
        ("nacktes Array erwartet uns nicht", 2),
    ],
)
def test_request_errors_end_the_whole_request(client: TestClient, payload: Any, code: int) -> None:
    response = post(client, payload)
    assert response.status_code == _ERROR_STATUS[code]
    assert response.json() == {
        "status": "error",
        "error": {"code": code, "message": response.json()["error"]["message"]},
    }
    assert response.json()["error"]["code"] == code


def test_a_bare_array_body_names_the_missing_field(client: TestClient) -> None:
    """ARCHITECTURE §7 spricht von einem Array; der Vertrag ist die Huelle."""
    response = client.post("/v2/lookup/batch?client=testkey", json=[QUERY])
    assert response.status_code == 400
    assert response.json()["error"] == {
        "code": 2,
        "message": 'missing required parameter "queries"',
    }


def test_a_broken_json_body_does_not_crash(client: TestClient) -> None:
    response = client.post(
        "/v2/lookup/batch?client=k",
        content=b"{kein JSON",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == 2


def test_a_missing_body_does_not_crash(client: TestClient) -> None:
    response = client.post("/v2/lookup/batch?client=k")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == 2


def test_index_trouble_becomes_error_thirteen_for_the_whole_batch() -> None:
    """Der Suchindex gehoert der Anfrage, nicht einem Eintrag."""
    service = StubService(StubMatcher(error=ServiceUnavailableError()))
    with TestClient(create_app(service)) as client:  # type: ignore[arg-type]
        response = post(client, {"client": "k", "queries": [QUERY, QUERY]})
    assert response.status_code == 503
    assert response.json() == {
        "status": "error",
        "error": {"code": 13, "message": "service currently unavailable, try again later"},
    }


def test_unexpected_failure_becomes_error_five() -> None:
    service = StubService(StubMatcher(error=RuntimeError("Datenbank weg")))
    with TestClient(create_app(service), raise_server_exceptions=False) as client:  # type: ignore[arg-type]
        response = post(client, {"client": "k", "queries": [QUERY]})
    assert response.status_code == 500
    assert response.json() == {
        "status": "error",
        "error": {"code": 5, "message": "internal error"},
    }


# --- Grenzen -----------------------------------------------------------------


def test_one_hundred_entries_are_accepted(client: TestClient) -> None:
    payload = batch(client, [QUERY] * MAX_BATCH_QUERIES)
    assert len(payload["responses"]) == MAX_BATCH_QUERIES


def test_one_hundred_and_one_entries_are_error_nineteen(client: TestClient) -> None:
    response = post(client, {"client": "k", "queries": [QUERY] * (MAX_BATCH_QUERIES + 1)})
    assert response.status_code == 413
    assert response.json() == {
        "status": "error",
        "error": {"code": 19, "message": "request too large"},
    }


def test_the_limit_is_checked_before_any_entry_is_parsed(client: TestClient) -> None:
    """101 kaputte Eintraege kosten keine 101 Dekodierversuche."""
    response = post(
        client,
        {"client": "k", "queries": [{"fingerprint": "kaputt!!"}] * (MAX_BATCH_QUERIES + 1)},
    )
    assert response.json()["error"]["code"] == 19


def test_a_body_above_one_mib_is_error_nineteen(client: TestClient) -> None:
    response = client.post(
        "/v2/lookup/batch?client=k",
        content=b"a" * (MAX_BODY_BYTES + 1),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == 19


def test_a_gzip_body_is_unpacked(client: TestClient, matcher: StubMatcher) -> None:
    body = gzip.compress(json.dumps({"client": "testkey", "queries": [QUERY]}).encode())
    response = client.post(
        "/v2/lookup/batch",
        content=body,
        headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
    )
    assert response.status_code == 200
    assert matcher.calls == [(len(VECTOR), 241, 7)]


def test_a_gzip_bomb_is_error_nineteen(client: TestClient) -> None:
    body = gzip.compress(b"x" * (MAX_BODY_BYTES + 1024))
    assert len(body) < MAX_BODY_BYTES
    response = client.post(
        "/v2/lookup/batch?client=k",
        content=body,
        headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == 19


def test_a_body_without_a_content_type_is_still_read(
    client: TestClient, matcher: StubMatcher
) -> None:
    """Der Rumpf ist der Vertrag, nicht sein Etikett."""
    response = client.post(
        "/v2/lookup/batch",
        content=json.dumps({"client": "testkey", "queries": [QUERY]}).encode(),
    )
    assert response.status_code == 200
    assert matcher.calls == [(len(VECTOR), 241, 7)]


# --- meta --------------------------------------------------------------------


def meta_client(rows: list[tuple[object, ...]]) -> tuple[TestClient, StubConnection]:
    """Testclient mit einer Attrappe, die ``track_mbid``-Zeilen liefert.

    Ohne MusicBrainz-Anbindung (``mb=None``) — der degradierte Betrieb aus
    Invariante §8.7 und zugleich der Weg, der ohne Dienste pruefbar ist.
    """
    connection = StubConnection(rows)
    matcher = StubMatcher([make_match(score=0.9, track_id=7, gid=GID)])
    return TestClient(create_app(StubService(matcher, connection))), connection  # type: ignore[arg-type]


def test_meta_from_the_envelope_reaches_every_entry() -> None:
    client, connection = meta_client([(7, MBID, 12)])
    with client:
        payload = batch(client, [QUERY, QUERY], meta="recordings sources")

    for entry in payload["responses"]:
        assert entry["results"][0]["recordings"] == [{"id": MBID, "sources": 12}]
    # **Ein** Bundel fuer die ganze Anfrage — nicht eins je Eintrag.
    assert len(connection.queries) == 1


def test_meta_as_a_json_list_works_too() -> None:
    client, _ = meta_client([(7, MBID, 12)])
    with client:
        payload = batch(client, [{**QUERY, "meta": ["recordings", "sources"]}])
    assert payload["responses"][0]["results"][0]["recordings"] == [{"id": MBID, "sources": 12}]


def test_an_entry_may_override_the_envelope_meta() -> None:
    client, connection = meta_client([(7, MBID, 12)])
    with client:
        payload = batch(client, [QUERY, {**QUERY, "meta": "0"}], meta="recordings")

    assert payload["responses"][0]["results"][0]["recordings"] == [{"id": MBID}]
    assert "recordings" not in payload["responses"][1]["results"][0]
    # Nur die Gruppe mit Zweig fragt nach; `meta=0` kostet keine Abfrage.
    assert len(connection.queries) == 1


def test_two_different_meta_plans_are_two_bundles() -> None:
    client, connection = meta_client([(7, MBID, 12)])
    with client:
        batch(client, [{**QUERY, "meta": "recordings"}, {**QUERY, "meta": "recordingids"}])
    assert len(connection.queries) == 2


def test_without_meta_no_metadata_query_runs() -> None:
    client, connection = meta_client([(7, MBID, 12)])
    with client:
        payload = batch(client, [QUERY])
    assert payload["responses"][0]["results"] == [{"id": str(GID), "score": 0.9}]
    assert connection.queries == []
