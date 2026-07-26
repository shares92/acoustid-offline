"""``/v2/submit`` ueber HTTP — Antwortformat, Modi, Statusmaschine.

Hier laeuft die echte Anwendung; an der Stelle von Postgres steht
:class:`FakeDb`, eine Handvoll Zeilen im Arbeitsspeicher, die genau so viel
von ``local_submission`` kann wie der Submit-Pfad anfasst. Was dadurch
pruefbar wird, ohne einen Dienst zu starten: das zeichengenaue Antwortformat,
die drei Modi, die stille Verwerfung, der reservierte Dokument-ID-Bereich und
das Verhalten bei ausgefallenem Suchindex. Der Nachweis gegen die echten
Dienste steht in `test_submit_integration.py`.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Sequence
from typing import Any

import pytest
from fastapi.testclient import TestClient
from original_examples import ERROR_TABLE, SUBMIT_OK_EXAMPLE
from stubs import StubConnection, StubIndex, StubService

from acoustid_api.main import MAX_BODY_BYTES, create_app
from acoustid_api.store import LOCAL_DOC_ID_BASE
from acoustid_api.submit import MAX_INDEX_BATCH
from shared.config import Config
from shared.fingerprint import encode_fingerprint
from shared.fpindex import FpIndexTransportError, extract_query
from shared.models import SubmitMode

VECTOR = [0x22222220 + index * 16 for index in range(300)]
FINGERPRINT = encode_fingerprint(VECTOR)
MBID = "b81f83ee-4da4-11e0-9ed8-0025225356f3"
OTHER_MBID = "c0a1c0de-4da4-11e0-9ed8-0025225356f3"

BASE = {
    "client": "testkey",
    "user": "usertestkey",
    "fingerprint": FINGERPRINT,
    "duration": "241",
    "mbid": MBID,
}

_ERROR_STATUS = {code: status for code, status, _ in ERROR_TABLE}


class FakeDb:
    """`local_submission` als Liste von Zeilen — nur was der Submit braucht."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self._track_ids = 0
        self._row_ids = 0

    def __call__(self, query: str, params: Any) -> Sequence[tuple[Any, ...]] | None:
        statement = query.strip()
        if "nextval" in statement:
            self._track_ids += 1
            return [(self._track_ids,)]
        if statement.startswith("INSERT INTO local_submission"):
            self._row_ids += 1
            self.rows.append({**params, "id": self._row_ids, "status": "new"})
            return [(self._row_ids,)]
        if statement.startswith("UPDATE local_submission"):
            for row in self.rows:
                if row["status"] == "new" and row["local_track_id"] in params["ids"]:
                    row["status"] = "indexed"
            return []
        if statement.startswith("SELECT DISTINCT ON (local_track_id)") and "'new'" in statement:
            pending: list[tuple[Any, ...]] = []
            seen: set[int] = set()
            for row in self.rows:
                if row["status"] != "new" or row["local_track_id"] in seen:
                    continue
                seen.add(row["local_track_id"])
                pending.append((row["local_track_id"], row["fingerprint"]))
            return pending[: params["limit"]]
        return None

    @property
    def status_by_track(self) -> dict[int, set[str]]:
        result: dict[int, set[str]] = {}
        for row in self.rows:
            result.setdefault(row["local_track_id"], set()).add(row["status"])
        return result


@pytest.fixture
def db() -> FakeDb:
    return FakeDb()


@pytest.fixture
def index() -> StubIndex:
    return StubIndex()


@pytest.fixture
def service(db: FakeDb, index: StubIndex) -> StubService:
    return StubService(connection=StubConnection(handler=db), index=index)


@pytest.fixture
def client(service: StubService) -> TestClient:
    with TestClient(create_app(service)) as test_client:  # type: ignore[arg-type]
        yield test_client


def submit(client: TestClient, **extra: str) -> dict[str, Any]:
    response = client.post("/v2/submit", data={**BASE, **extra})
    assert response.status_code == 200, response.text
    return response.json()


# --- Antwortformat ----------------------------------------------------------


def test_the_documented_shape_comes_back(client: TestClient) -> None:
    payload = submit(client)
    assert payload == {"status": "ok", "submissions": [{"id": 1, "status": "pending"}]}
    assert set(payload) == set(SUBMIT_OK_EXAMPLE)
    assert set(payload["submissions"][0]) == set(SUBMIT_OK_EXAMPLE["submissions"][0])


def test_get_works_exactly_like_post(client: TestClient) -> None:
    response = client.get("/v2/submit", params=BASE)
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "submissions": [{"id": 1, "status": "pending"}]}


def test_query_string_and_body_are_merged(client: TestClient, db: FakeDb) -> None:
    response = client.post(
        "/v2/submit",
        params={"client": "testkey", "user": "usertestkey"},
        data={"fingerprint": FINGERPRINT, "duration": "241", "mbid": MBID},
    )
    assert response.status_code == 200
    assert db.rows[0]["client"] == "testkey"


def test_the_status_is_always_pending(client: TestClient, index: StubIndex) -> None:
    """Auch wenn schon alles indexiert ist — der Vertrag kennt nur `pending`."""
    payload = submit(client)
    assert index.doc_ids  # wirklich indexiert
    assert payload["submissions"][0]["status"] == "pending"


def test_the_index_field_is_a_string_and_only_with_a_suffix(client: TestClient) -> None:
    without = submit(client)
    assert "index" not in without["submissions"][0]

    response = client.post(
        "/v2/submit",
        data={
            "client": "testkey",
            "user": "usertestkey",
            "fingerprint.0": FINGERPRINT,
            "duration.0": "241",
            "mbid.0": MBID,
        },
    )
    entry = response.json()["submissions"][0]
    assert entry["index"] == "0"
    assert isinstance(entry["index"], str)


def test_every_mbid_gets_its_own_submission_id(client: TestClient, db: FakeDb) -> None:
    response = client.post(
        "/v2/submit",
        content="&".join(
            [
                "client=testkey",
                "user=usertestkey",
                f"fingerprint.0={FINGERPRINT}",
                "duration.0=241",
                f"mbid.0={MBID}",
                f"mbid.0={OTHER_MBID}",
            ]
        ),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    payload = response.json()
    assert payload["submissions"] == [
        {"id": 1, "index": "0", "status": "pending"},
        {"id": 2, "index": "0", "status": "pending"},
    ]
    # Zwei Zeilen, aber eine Aufnahme: gleiche Gruppe, gleiche AcoustID.
    assert {row["mbid"] for row in db.rows} == {MBID, OTHER_MBID}
    assert len({row["local_track_id"] for row in db.rows}) == 1
    assert len({row["local_track_gid"] for row in db.rows}) == 1


def test_two_parts_answer_in_order(client: TestClient) -> None:
    response = client.post(
        "/v2/submit",
        data={
            "client": "testkey",
            "user": "usertestkey",
            "fingerprint.0": FINGERPRINT,
            "duration.0": "241",
            "mbid.0": MBID,
            "fingerprint.1": FINGERPRINT,
            "duration.1": "199",
            "mbid.1": OTHER_MBID,
        },
    )
    assert [entry["index"] for entry in response.json()["submissions"]] == ["0", "1"]


def test_a_submission_without_metadata_is_dropped_silently(
    client: TestClient, db: FakeDb, index: StubIndex
) -> None:
    response = client.post(
        "/v2/submit",
        data={
            "client": "testkey",
            "user": "usertestkey",
            "fingerprint": FINGERPRINT,
            "duration": "241",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "submissions": []}
    assert db.rows == []
    assert index.batches == []


# --- Formate und Transport --------------------------------------------------


def test_content_type_is_json_with_uppercase_charset(client: TestClient) -> None:
    response = client.post("/v2/submit", data=BASE)
    assert response.headers["content-type"] == "application/json; charset=UTF-8"


def test_cors_header_is_present(client: TestClient) -> None:
    assert client.post("/v2/submit", data=BASE).headers["access-control-allow-origin"] == "*"


def test_xml_wraps_the_submissions_list(client: TestClient) -> None:
    response = client.post("/v2/submit", data={**BASE, "format": "xml"})
    assert response.headers["content-type"] == "text/xml; charset=UTF-8"
    assert "<submissions><submission>" in response.text
    assert "<status>pending</status>" in response.text


def test_jsonp_wraps_the_same_json(client: TestClient) -> None:
    response = client.post("/v2/submit", data={**BASE, "format": "jsonp"})
    assert response.headers["content-type"] == "application/javascript; charset=UTF-8"
    assert response.text.startswith("jsonAcoustidApi({")


def test_a_gzip_body_is_unpacked(client: TestClient) -> None:
    """pyacoustid (beets) schickt seine Submits so."""
    body = "&".join(f"{name}={value}" for name, value in BASE.items())
    response = client.post(
        "/v2/submit",
        content=gzip.compress(body.encode()),
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "content-encoding": "gzip",
        },
    )
    assert response.status_code == 200
    assert response.json()["submissions"][0]["status"] == "pending"


def test_a_body_above_one_mib_is_error_19(client: TestClient) -> None:
    """Picard verkleinert seine Submit-Pakete genau daraufhin."""
    response = client.post(
        "/v2/submit",
        content=b"x" * (MAX_BODY_BYTES + 1),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == _ERROR_STATUS[19] == 413
    assert response.json()["error"]["code"] == 19


@pytest.mark.parametrize(
    ("data", "code"),
    [
        ({"format": "yaml"}, 1),
        ({"user": "usertestkey"}, 2),
        ({**BASE, "fingerprint": "kaputt"}, 3),
        ({**BASE, "duration": "99999"}, 8),
        ({**BASE, "bitrate": "-1"}, 9),
        ({**BASE, "foreignid": "ohne-doppelpunkt"}, 10),
        ({**BASE, "mbid": "keine-uuid"}, 7),
    ],
)
def test_parameter_errors_keep_code_and_http_status(
    client: TestClient, data: dict[str, str], code: int
) -> None:
    response = client.post("/v2/submit", data=data)
    assert response.json()["status"] == "error"
    assert response.json()["error"]["code"] == code
    assert response.status_code == _ERROR_STATUS[code]


def test_an_unexpected_failure_becomes_error_5(client: TestClient, service: StubService) -> None:
    service.connection.handler = None  # jede Anweisung liefert jetzt nichts
    response = client.post("/v2/submit", data=BASE)
    assert response.status_code == 500
    assert response.json()["error"]["code"] == 5


# --- Modi -------------------------------------------------------------------


def test_mode_off_answers_error_12(db: FakeDb, index: StubIndex) -> None:
    config = Config.model_validate({"submit": {"mode": SubmitMode.OFF}})
    service = StubService(connection=StubConnection(handler=db), index=index, config=config)
    with TestClient(create_app(service)) as client:  # type: ignore[arg-type]
        response = client.post("/v2/submit", data=BASE)
    assert response.status_code == _ERROR_STATUS[12] == 400
    assert response.json() == {
        "status": "error",
        "error": {"code": 12, "message": "not allowed"},
    }
    assert db.rows == []


def test_mode_off_refuses_before_reading_any_parameter(db: FakeDb) -> None:
    """Der Modus schlaegt die Parameterpruefung — nichts wird dekodiert."""
    config = Config.model_validate({"submit": {"mode": SubmitMode.OFF}})
    service = StubService(connection=StubConnection(handler=db), config=config)
    with TestClient(create_app(service)) as client:  # type: ignore[arg-type]
        response = client.post("/v2/submit", data={})
    assert response.json()["error"]["code"] == 12


def test_mode_off_still_honours_the_requested_format(db: FakeDb) -> None:
    config = Config.model_validate({"submit": {"mode": SubmitMode.OFF}})
    service = StubService(connection=StubConnection(handler=db), config=config)
    with TestClient(create_app(service)) as client:  # type: ignore[arg-type]
        response = client.post("/v2/submit", data={**BASE, "format": "xml"})
    assert response.headers["content-type"] == "text/xml; charset=UTF-8"
    assert "<code>12</code>" in response.text


def test_mode_local_upstream_behaves_like_local_for_now(db: FakeDb, index: StubIndex) -> None:
    """Die Weiterleitung kommt in Phase 12 — gespeichert wird schon jetzt."""
    config = Config.model_validate(
        {"submit": {"mode": SubmitMode.LOCAL_UPSTREAM, "upstream_app_key": "geheim"}}
    )
    service = StubService(connection=StubConnection(handler=db), index=index, config=config)
    with TestClient(create_app(service)) as client:  # type: ignore[arg-type]
        response = client.post("/v2/submit", data=BASE)
    assert response.json()["submissions"] == [{"id": 1, "status": "pending"}]
    assert db.status_by_track == {1: {"indexed"}}


# --- Statusmaschine und Index ----------------------------------------------


def test_a_stored_submission_reaches_the_reserved_doc_id_range(
    client: TestClient, db: FakeDb, index: StubIndex
) -> None:
    submit(client)
    assert index.doc_ids == [LOCAL_DOC_ID_BASE + 1]
    assert index.batches[0][0].hashes == extract_query(VECTOR, max_hashes=120)
    assert db.status_by_track == {1: {"indexed"}}


def test_the_index_is_written_before_the_status_changes(
    client: TestClient, db: FakeDb, index: StubIndex
) -> None:
    """Umgekehrt waere es stiller Datenverlust (§5.3, Muster Index-Feed)."""
    seen: list[dict[int, set[str]]] = []
    original = index.update

    def spy(changes: Any, **kwargs: Any) -> int:
        seen.append(db.status_by_track)
        return original(changes, **kwargs)

    index.update = spy  # type: ignore[method-assign]
    submit(client)
    assert seen == [{1: {"new"}}]
    assert db.status_by_track == {1: {"indexed"}}


def test_an_unreachable_index_keeps_the_submission_new_and_answers_200(
    db: FakeDb, index: StubIndex
) -> None:
    index.error = FpIndexTransportError("kein Netz")
    service = StubService(connection=StubConnection(handler=db), index=index)
    with TestClient(create_app(service)) as client:  # type: ignore[arg-type]
        response = client.post("/v2/submit", data=BASE)
    assert response.status_code == 200
    assert response.json()["submissions"][0]["status"] == "pending"
    assert db.status_by_track == {1: {"new"}}


def test_the_next_submission_catches_up_the_backlog(db: FakeDb, index: StubIndex) -> None:
    index.error = FpIndexTransportError("kein Netz")
    service = StubService(connection=StubConnection(handler=db), index=index)
    with TestClient(create_app(service)) as client:  # type: ignore[arg-type]
        client.post("/v2/submit", data=BASE)
        assert db.status_by_track == {1: {"new"}}

        index.error = None
        client.post("/v2/submit", data={**BASE, "duration": "199", "mbid": OTHER_MBID})

    assert db.status_by_track == {1: {"indexed"}, 2: {"indexed"}}
    assert index.doc_ids == [LOCAL_DOC_ID_BASE + 1, LOCAL_DOC_ID_BASE + 2]


def test_a_silent_fingerprint_is_marked_indexed_without_a_document(
    db: FakeDb, index: StubIndex
) -> None:
    """Nur Stille ergibt keinen Query-Extrakt — die Zeile darf trotzdem nicht
    ewig im Arbeitsvorrat liegen bleiben (wie beim Index-Feed)."""
    silence = encode_fingerprint([627964279] * 200)
    service = StubService(connection=StubConnection(handler=db), index=index)
    with TestClient(create_app(service)) as client:  # type: ignore[arg-type]
        response = client.post("/v2/submit", data={**BASE, "fingerprint": silence})
    assert response.status_code == 200
    assert index.batches == []
    assert db.status_by_track == {1: {"indexed"}}


def test_the_catch_up_is_bounded_per_request(db: FakeDb, index: StubIndex) -> None:
    """Ein Rueckstand darf eine einzelne Anfrage nicht beliebig lange aufhalten."""
    for number in range(MAX_INDEX_BATCH + 5):
        db.rows.append(
            {
                "id": number + 1,
                "local_track_id": number + 1,
                "fingerprint": VECTOR,
                "status": "new",
            }
        )
    db._track_ids = MAX_INDEX_BATCH + 5
    db._row_ids = MAX_INDEX_BATCH + 5
    service = StubService(connection=StubConnection(handler=db), index=index)
    with TestClient(create_app(service)) as client:  # type: ignore[arg-type]
        client.post("/v2/submit", data=BASE)
    assert len(index.doc_ids) == MAX_INDEX_BATCH
    assert sum(1 for row in db.rows if row["status"] == "new") == 6


# --- Was gespeichert wird ---------------------------------------------------


def test_all_fields_reach_the_database(client: TestClient, db: FakeDb) -> None:
    submit(
        client,
        bitrate="320",
        fileformat="FLAC",
        track=" Der Titel ",
        artist="Die Band",
        album="Das Album",
        albumartist="Diverse",
        year="1999",
        trackno="4",
        discno="1",
        puid=OTHER_MBID,
        foreignid="spotify:4711",
        clientversion="2.14",
    )
    row = db.rows[0]
    assert row["length"] == 241
    assert (row["bitrate"], row["fileformat"]) == (320, "FLAC")
    assert (row["track"], row["artist"], row["album"]) == ("Der Titel", "Die Band", "Das Album")
    assert (row["album_artist"], row["year"], row["track_no"], row["disc_no"]) == (
        "Diverse",
        1999,
        4,
        1,
    )
    assert (row["puid"], row["foreignid"]) == (OTHER_MBID, "spotify:4711")
    assert (row["client"], row["client_version"], row["user"]) == ("testkey", "2.14", "usertestkey")


def test_the_vector_is_stored_as_signed_int32(client: TestClient, db: FakeDb) -> None:
    """Die Spalte ist `integer[]`; der Dekoder liefert vorzeichenlose Hashes."""
    high = [0xFFFFFFF0, 0x80000000, 0x00000010]
    client.post("/v2/submit", data={**BASE, "fingerprint": encode_fingerprint(high)})
    stored = db.rows[0]["fingerprint"]
    assert all(-(2**31) <= value < 2**31 for value in stored)
    assert [value & 0xFFFFFFFF for value in stored] == high


def test_the_stored_event_is_logged_for_the_watchdog(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Der Waechter verwirft daraufhin seinen Lookup-Cache (§8.6, Phase 17)."""
    with caplog.at_level("INFO", logger="acoustid_api.submit"):
        submit(client)
    events = [record for record in caplog.records if getattr(record, "event", None)]
    assert [record.event for record in events] == ["local_submission_stored"]
    assert events[0].submissions == 1
    assert len(events[0].acoustids) == 1


def test_the_response_is_serialised_with_sorted_keys(client: TestClient) -> None:
    """`json.dumps(..., sort_keys=True)` wie im Original."""
    response = client.post("/v2/submit", data=BASE)
    assert list(json.loads(response.text)) == ["status", "submissions"]
    assert response.text.index('"status"') < response.text.index('"submissions"')
