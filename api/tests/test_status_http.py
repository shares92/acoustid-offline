"""``GET/POST /v2/submission_status`` ueber HTTP — Mapping, Formate, Grenzen.

Der Endpunkt ist klein, sein Vertrag dafuer praezise: zwei Statuswoerter, ein
optionales ``result.id``, unbekannte IDs still ``pending`` — nie 404. Geprueft
wird gegen die woertliche Abschrift des Forschungsberichts
(`original_examples.py`) und gegen die **ganze** Statusmaschine der
Datenbank, damit kein Zustand unbeantwortet bleibt.

An der Stelle von Postgres steht :class:`FakeDb` (siehe `stubs.py`); den
Nachweis gegen echtes SQL fuehrt `test_submit_integration.py`.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from original_examples import (
    ERROR_TABLE,
    SUBMISSION_STATUS_IMPORTED_EXAMPLE,
    SUBMISSION_STATUS_PENDING_EXAMPLE,
)
from stubs import FakeDb, StubConnection, StubService

from acoustid_api.main import create_app
from acoustid_api.params import MAX_STATUS_IDS
from shared.config import Config
from shared.models import SubmissionStatus, SubmitMode

GID = UUID("b81f83ee-4da4-11e0-9ed8-0025225356f3")

_ERROR_STATUS = {code: status for code, status, _ in ERROR_TABLE}


@pytest.fixture
def db() -> FakeDb:
    return FakeDb()


@pytest.fixture
def client(db: FakeDb) -> TestClient:
    service = StubService(connection=StubConnection(handler=db))
    with TestClient(create_app(service)) as test_client:  # type: ignore[arg-type]
        yield test_client


def ask(client: TestClient, *ids: Any, **extra: str) -> dict[str, Any]:
    response = client.get(
        "/v2/submission_status",
        params=[
            ("client", "testkey"),
            *[("id", str(item)) for item in ids],
            *extra.items(),
        ],
    )
    assert response.status_code == 200, response.text
    return response.json()


# --- Statusabbildung ---------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "answer"),
    [
        (SubmissionStatus.NEW, "pending"),
        (SubmissionStatus.INDEXED, "imported"),
        (SubmissionStatus.FORWARDED, "imported"),
        (SubmissionStatus.FORWARD_FAILED, "imported"),
    ],
)
def test_every_state_of_the_machine_has_an_answer(
    client: TestClient, db: FakeDb, status: SubmissionStatus, answer: str
) -> None:
    """Alle vier Werte der CHECK-Domaene aus ARCHITECTURE §5.2."""
    row = db.add(status=status.value, local_track_gid=GID)
    entry = ask(client, row["id"])["submissions"][0]
    assert entry["id"] == row["id"]
    assert entry["status"] == answer
    if answer == "imported":
        assert entry["result"] == {"id": str(GID)}
    else:
        assert "result" not in entry


def test_the_status_machine_is_covered_completely(client: TestClient, db: FakeDb) -> None:
    """Kein Zustand faellt durch — sonst antwortete er still ``pending``."""
    rows = [db.add(status=status.value) for status in SubmissionStatus]
    answers = ask(client, *[row["id"] for row in rows])["submissions"]
    assert [entry["status"] for entry in answers] == [
        "pending",
        "imported",
        "imported",
        "imported",
    ]


def test_the_documented_shapes_come_back(client: TestClient, db: FakeDb) -> None:
    pending = db.add(status="new")
    imported = db.add(status="indexed", local_track_gid=GID)

    payload = ask(client, pending["id"])
    assert set(payload) == set(SUBMISSION_STATUS_PENDING_EXAMPLE)
    assert set(payload["submissions"][0]) == set(
        SUBMISSION_STATUS_PENDING_EXAMPLE["submissions"][0]
    )

    payload = ask(client, imported["id"])
    assert set(payload["submissions"][0]) == set(
        SUBMISSION_STATUS_IMPORTED_EXAMPLE["submissions"][0]
    )
    assert set(payload["submissions"][0]["result"]) == set(
        SUBMISSION_STATUS_IMPORTED_EXAMPLE["submissions"][0]["result"]
    )


def test_result_id_is_the_acoustid_of_the_submission(client: TestClient, db: FakeDb) -> None:
    gid = uuid4()
    row = db.add(status="indexed", local_track_gid=gid)
    assert ask(client, row["id"])["submissions"][0]["result"] == {"id": str(gid)}


# --- Unbekannte IDs ----------------------------------------------------------


def test_an_unknown_id_stays_pending_and_never_404(client: TestClient) -> None:
    response = client.get("/v2/submission_status", params={"client": "k", "id": "999999"})
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "submissions": [{"id": 999999, "status": "pending"}],
    }


def test_known_and_unknown_ids_mix_without_trouble(client: TestClient, db: FakeDb) -> None:
    row = db.add(status="indexed", local_track_gid=GID)
    payload = ask(client, row["id"], 999999)
    assert [entry["status"] for entry in payload["submissions"]] == ["imported", "pending"]


# --- Mehrfaches `id` ---------------------------------------------------------


def test_multiple_ids_are_answered_in_request_order(client: TestClient, db: FakeDb) -> None:
    rows = [db.add(status="indexed") for _ in range(3)]
    wanted = [rows[2]["id"], rows[0]["id"], rows[1]["id"]]
    payload = ask(client, *wanted)
    assert [entry["id"] for entry in payload["submissions"]] == wanted


def test_a_repeated_id_is_answered_twice(client: TestClient, db: FakeDb) -> None:
    """Beantwortet wird, was gefragt wurde — auch doppelt."""
    row = db.add(status="new")
    payload = ask(client, row["id"], row["id"])
    assert [entry["id"] for entry in payload["submissions"]] == [row["id"], row["id"]]


def test_all_ids_are_fetched_in_a_single_query(db: FakeDb) -> None:
    connection = StubConnection(handler=db)
    service = StubService(connection=connection)
    rows = [db.add(status="indexed") for _ in range(5)]
    with TestClient(create_app(service)) as client:  # type: ignore[arg-type]
        ask(client, *[row["id"] for row in rows])
    assert len(connection.queries) == 1


def test_unreadable_ids_are_skipped(client: TestClient, db: FakeDb) -> None:
    row = db.add(status="indexed")
    payload = ask(client, "abc", row["id"], -5, 0)
    assert [entry["id"] for entry in payload["submissions"]] == [row["id"]]


def test_only_unreadable_ids_are_like_none_at_all(client: TestClient) -> None:
    response = client.get("/v2/submission_status", params={"client": "k", "id": "abc"})
    assert response.status_code == 400
    assert response.json()["error"] == {
        "code": 2,
        "message": 'missing required parameter "id"',
    }


# --- Transport ---------------------------------------------------------------


def test_post_with_a_form_body_works_the_same(client: TestClient, db: FakeDb) -> None:
    row = db.add(status="indexed", local_track_gid=GID)
    response = client.post(
        "/v2/submission_status", data={"client": "testkey", "id": str(row["id"])}
    )
    assert response.status_code == 200
    assert response.json()["submissions"][0]["status"] == "imported"


def test_query_string_and_body_are_merged(client: TestClient, db: FakeDb) -> None:
    row = db.add(status="new")
    response = client.post(
        "/v2/submission_status", params={"client": "testkey"}, data={"id": str(row["id"])}
    )
    assert response.status_code == 200
    assert response.json()["submissions"][0]["id"] == row["id"]


def test_every_response_allows_any_origin(client: TestClient) -> None:
    ok = client.get("/v2/submission_status", params={"client": "k", "id": "1"})
    error = client.get("/v2/submission_status")
    assert ok.headers["access-control-allow-origin"] == "*"
    assert error.headers["access-control-allow-origin"] == "*"


def test_the_endpoint_answers_even_with_submit_switched_off(db: FakeDb) -> None:
    """Der Endpunkt liest nur — ``submit.mode = off`` geht ihn nichts an."""
    config = Config.model_validate({"submit": {"mode": SubmitMode.OFF.value}})
    service = StubService(connection=StubConnection(handler=db), config=config)
    row = db.add(status="indexed", local_track_gid=GID)
    with TestClient(create_app(service)) as client:  # type: ignore[arg-type]
        assert ask(client, row["id"])["submissions"][0]["status"] == "imported"


# --- Formate -----------------------------------------------------------------


def test_xml_format(client: TestClient, db: FakeDb) -> None:
    row = db.add(status="indexed", local_track_gid=GID)
    response = client.get(
        "/v2/submission_status",
        params={"client": "k", "id": str(row["id"]), "format": "xml"},
    )
    assert response.headers["content-type"] == "text/xml; charset=UTF-8"
    assert "<submissions><submission>" in response.text
    assert "<status>imported</status>" in response.text
    assert f"<result><id>{GID}</id></result>" in response.text


def test_jsonp_format(client: TestClient) -> None:
    response = client.get(
        "/v2/submission_status",
        params={"client": "k", "id": "1", "format": "jsonp", "jsoncallback": "cb"},
    )
    assert response.headers["content-type"] == "application/javascript; charset=UTF-8"
    assert response.text.startswith("cb(")


def test_unknown_format_answers_in_json(client: TestClient) -> None:
    response = client.get(
        "/v2/submission_status", params={"client": "k", "id": "1", "format": "yaml"}
    )
    assert response.status_code == 400
    assert response.json()["error"] == {"code": 1, "message": 'unknown format "yaml"'}


def test_errors_keep_the_requested_format(client: TestClient) -> None:
    response = client.get("/v2/submission_status", params={"format": "xml"})
    assert response.status_code == 400
    assert response.headers["content-type"] == "text/xml; charset=UTF-8"
    assert "<code>2</code>" in response.text


# --- Fehler und Grenzen ------------------------------------------------------


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({}, 2),
        ({"client": "k"}, 2),
        ({"id": "1"}, 2),
    ],
)
def test_parameter_errors_use_the_acoustid_format(
    client: TestClient, params: dict[str, str], code: int
) -> None:
    response = client.get("/v2/submission_status", params=params)
    assert response.status_code == _ERROR_STATUS[code]
    assert response.json()["error"]["code"] == code


def test_one_hundred_ids_are_accepted(client: TestClient) -> None:
    payload = ask(client, *range(1, MAX_STATUS_IDS + 1))
    assert len(payload["submissions"]) == MAX_STATUS_IDS


def test_one_hundred_and_one_ids_are_error_nineteen(client: TestClient) -> None:
    response = client.get(
        "/v2/submission_status",
        params=[("client", "k"), *[("id", str(item)) for item in range(MAX_STATUS_IDS + 1)]],
    )
    assert response.status_code == 413
    assert response.json() == {
        "status": "error",
        "error": {"code": 19, "message": "request too large"},
    }


def test_unreadable_ids_count_towards_the_limit(client: TestClient) -> None:
    """Die Grenze ist ein Missbrauchsschutz, kein Qualitaetsurteil."""
    response = client.get(
        "/v2/submission_status",
        params=[("client", "k"), *[("id", "abc")] * (MAX_STATUS_IDS + 1)],
    )
    assert response.json()["error"]["code"] == 19


def test_unexpected_failure_becomes_error_five() -> None:
    def explode(query: str, params: Any) -> None:
        raise RuntimeError("Datenbank weg")

    service = StubService(connection=StubConnection(handler=explode))
    with TestClient(create_app(service), raise_server_exceptions=False) as client:  # type: ignore[arg-type]
        response = client.get("/v2/submission_status", params={"client": "k", "id": "1"})
    assert response.status_code == 500
    assert response.json() == {
        "status": "error",
        "error": {"code": 5, "message": "internal error"},
    }
