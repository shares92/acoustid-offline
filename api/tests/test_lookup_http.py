"""``/v2/lookup`` ueber HTTP — Transport, Grenzen, Antwortformen.

Hier laeuft die echte Anwendung, aber mit einem Attrappen-Dienst (siehe
conftest.py): Datenbank und Suchindex kommen in den Integrationstests dazu.
Geprueft wird alles, was zwischen Leitung und Pipeline passiert — und genau
das ist der Teil, an dem Kompatibilitaet haengt: GET wie POST, Query-String
wie Formular-Rumpf, gzip, 1-MiB-Grenze, CORS, Fehlerformat.
"""

from __future__ import annotations

import gzip
import json
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from original_examples import ERROR_TABLE, LOOKUP_BATCH_EXAMPLE, LOOKUP_OK_EXAMPLE
from stubs import StubConnection, StubMatcher, StubService, make_match

from acoustid_api.errors import ServiceUnavailableError
from acoustid_api.main import MAX_BODY_BYTES, create_app
from shared.fingerprint import encode_fingerprint

VECTOR = [0x22222220 + index * 16 for index in range(300)]
FINGERPRINT = encode_fingerprint(VECTOR)
GID = UUID("b81f83ee-4da4-11e0-9ed8-0025225356f3")

BASE = {"client": "testkey", "fingerprint": FINGERPRINT, "duration": "241"}

_ERROR_STATUS = {code: status for code, status, _ in ERROR_TABLE}


# --- Erfolgsfall -----------------------------------------------------------


def test_get_returns_the_documented_shape(client: TestClient, matcher: StubMatcher) -> None:
    matcher.matches = [make_match(score=1.0, gid=GID)]
    response = client.get("/v2/lookup", params=BASE)
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "results": [{"id": str(GID), "score": 1.0}]}
    # Dieselbe Struktur wie das Original-Beispiel, nur mit echten Werten.
    assert set(response.json()) == set(LOOKUP_OK_EXAMPLE)
    assert set(response.json()["results"][0]) == set(LOOKUP_OK_EXAMPLE["results"][0])


def test_post_with_a_form_body_works_the_same(client: TestClient, matcher: StubMatcher) -> None:
    matcher.matches = [make_match(score=0.87, gid=GID)]
    response = client.post("/v2/lookup", data=BASE)
    assert response.status_code == 200
    assert response.json()["results"] == [{"id": str(GID), "score": 0.87}]


def test_query_string_and_body_are_merged(client: TestClient, matcher: StubMatcher) -> None:
    response = client.post(
        "/v2/lookup",
        params={"client": "testkey"},
        data={"fingerprint": FINGERPRINT, "duration": "241"},
    )
    assert response.status_code == 200
    assert matcher.calls == [(len(VECTOR), 241, 7)]


def test_no_match_yields_an_empty_result_list(client: TestClient) -> None:
    response = client.get("/v2/lookup", params=BASE)
    assert response.json() == {"status": "ok", "results": []}


def test_max_duration_diff_reaches_the_pipeline(client: TestClient, matcher: StubMatcher) -> None:
    client.get("/v2/lookup", params={**BASE, "maxdurationdiff": "30"})
    assert matcher.calls == [(len(VECTOR), 241, 30)]


def test_content_type_is_json_with_uppercase_charset(client: TestClient) -> None:
    response = client.get("/v2/lookup", params=BASE)
    assert response.headers["content-type"] == "application/json; charset=UTF-8"


# --- Batch -----------------------------------------------------------------


def test_batch_answers_every_part_with_its_index(client: TestClient, matcher: StubMatcher) -> None:
    matcher.matches = [make_match(gid=GID)]
    response = client.post(
        "/v2/lookup",
        data={
            "client": "testkey",
            "batch": "1",
            "fingerprint.0": FINGERPRINT,
            "duration.0": "241",
            "fingerprint.3": FINGERPRINT,
            "duration.3": "180",
        },
    )
    payload = response.json()
    assert set(payload) == set(LOOKUP_BATCH_EXAMPLE)
    assert [part["index"] for part in payload["fingerprints"]] == [0, 3]
    assert payload["fingerprints"][0]["results"] == [{"id": str(GID), "score": 1.0}]


def test_batch_without_suffix_reports_a_null_index(
    client: TestClient, matcher: StubMatcher
) -> None:
    response = client.get("/v2/lookup", params={**BASE, "batch": "1"})
    assert response.json()["fingerprints"] == [{"index": None, "results": []}]


def test_without_batch_only_the_first_part_is_answered(
    client: TestClient, matcher: StubMatcher
) -> None:
    response = client.post(
        "/v2/lookup",
        data={
            "client": "testkey",
            "fingerprint.0": FINGERPRINT,
            "duration.0": "241",
            "fingerprint.1": FINGERPRINT,
            "duration.1": "180",
        },
    )
    assert "results" in response.json()
    assert matcher.calls == [(len(VECTOR), 241, 7)]


# --- Formate ---------------------------------------------------------------


def test_xml_format(client: TestClient, matcher: StubMatcher) -> None:
    matcher.matches = [make_match(gid=GID)]
    response = client.get("/v2/lookup", params={**BASE, "format": "xml"})
    assert response.headers["content-type"] == "text/xml; charset=UTF-8"
    assert f"<id>{GID}</id>" in response.text
    assert "<status>ok</status>" in response.text


def test_jsonp_format(client: TestClient) -> None:
    response = client.get("/v2/lookup", params={**BASE, "format": "jsonp", "jsoncallback": "cb"})
    assert response.headers["content-type"] == "application/javascript; charset=UTF-8"
    assert response.text.startswith("cb(")


def test_unknown_format_answers_in_json(client: TestClient) -> None:
    response = client.get("/v2/lookup", params={**BASE, "format": "yaml"})
    assert response.status_code == 400
    assert response.headers["content-type"] == "application/json; charset=UTF-8"
    assert response.json() == {
        "status": "error",
        "error": {"code": 1, "message": 'unknown format "yaml"'},
    }


def test_errors_keep_the_requested_format(client: TestClient) -> None:
    """Ein Fehler in xml kommt auch als xml — sonst stolpert der Parser."""
    response = client.get("/v2/lookup", params={"format": "xml"})
    assert response.status_code == 400
    assert response.headers["content-type"] == "text/xml; charset=UTF-8"
    assert "<code>2</code>" in response.text


# --- Fehler ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({}, 2),
        ({"client": "k"}, 2),
        ({"client": "k", "fingerprint": FINGERPRINT}, 2),
        ({"client": "k", "fingerprint": "kaputt!!", "duration": "241"}, 3),
        ({**BASE, "maxdurationdiff": "99"}, 11),
        ({**BASE, "trackid": "keine-uuid"}, 7),
    ],
)
def test_parameter_errors_use_the_acoustid_format(
    client: TestClient, params: dict[str, str], code: int
) -> None:
    response = client.get("/v2/lookup", params=params)
    payload = response.json()
    assert payload["status"] == "error"
    assert payload["error"]["code"] == code
    assert response.status_code == _ERROR_STATUS[code]


def test_index_trouble_becomes_error_thirteen() -> None:
    """Kein stiller Nicht-Treffer, wenn der Index nicht antwortet."""
    service = StubService(StubMatcher(error=ServiceUnavailableError()))
    with TestClient(create_app(service)) as client:  # type: ignore[arg-type]
        response = client.get("/v2/lookup", params=BASE)
    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": 13,
        "message": "service currently unavailable, try again later",
    }


def test_unexpected_failure_becomes_error_five() -> None:
    service = StubService(StubMatcher(error=RuntimeError("Datenbank weg")))
    with TestClient(create_app(service), raise_server_exceptions=False) as client:  # type: ignore[arg-type]
        response = client.get("/v2/lookup", params=BASE)
    assert response.status_code == 500
    assert response.json() == {
        "status": "error",
        "error": {"code": 5, "message": "internal error"},
    }


def test_an_unknown_trackid_is_no_error_but_no_result(client: TestClient) -> None:
    response = client.get("/v2/lookup", params={"client": "k", "trackid": str(GID)})
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "results": []}


def test_a_known_trackid_scores_one() -> None:
    service = StubService(connection=StubConnection([(42, GID)]))
    with TestClient(create_app(service)) as client:  # type: ignore[arg-type]
        response = client.get("/v2/lookup", params={"client": "k", "trackid": str(GID)})
    assert response.json()["results"] == [{"id": str(GID), "score": 1.0}]


# --- Rumpf: gzip und Groesse -----------------------------------------------


def test_gzip_body_is_unpacked(client: TestClient, matcher: StubMatcher) -> None:
    """pyacoustid (beets) schickt so."""
    body = gzip.compress(f"client=testkey&fingerprint={FINGERPRINT}&duration=241".encode())
    response = client.post(
        "/v2/lookup",
        content=body,
        headers={
            "Content-Encoding": "gzip",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    assert response.status_code == 200
    assert matcher.calls == [(len(VECTOR), 241, 7)]


def test_broken_gzip_body_does_not_crash(client: TestClient) -> None:
    response = client.post(
        "/v2/lookup",
        content=b"kein gzip",
        headers={
            "Content-Encoding": "gzip",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == 2


def test_a_body_above_one_mib_is_error_nineteen(client: TestClient) -> None:
    response = client.post(
        "/v2/lookup",
        content=b"a" * (MAX_BODY_BYTES + 1),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 413
    assert response.json() == {
        "status": "error",
        "error": {"code": 19, "message": "request too large"},
    }


def test_a_gzip_bomb_is_error_nineteen(client: TestClient) -> None:
    """Die Grenze gilt fuer den **entpackten** Rumpf, nicht fuer die Leitung."""
    body = gzip.compress(b"x" * (MAX_BODY_BYTES + 1024))
    assert len(body) < MAX_BODY_BYTES
    response = client.post(
        "/v2/lookup",
        content=body,
        headers={
            "Content-Encoding": "gzip",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == 19


def test_a_body_just_below_the_limit_is_accepted(client: TestClient, matcher: StubMatcher) -> None:
    filler = "x" * (MAX_BODY_BYTES - 4096)
    response = client.post(
        "/v2/lookup",
        data={**BASE, "clientversion": filler},
    )
    assert response.status_code == 200


def test_a_json_body_is_ignored_but_the_query_string_still_counts(
    client: TestClient,
) -> None:
    """Das Original kennt an dieser Route keinen JSON-Rumpf."""
    response = client.post(
        "/v2/lookup",
        params=BASE,
        content=json.dumps({"client": "andere"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 200


# --- CORS -------------------------------------------------------------------


def test_every_response_allows_any_origin(client: TestClient) -> None:
    ok = client.get("/v2/lookup", params=BASE)
    error = client.get("/v2/lookup", params={})
    assert ok.headers["access-control-allow-origin"] == "*"
    assert error.headers["access-control-allow-origin"] == "*"
