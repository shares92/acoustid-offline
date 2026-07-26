"""``/v2/submit`` gegen echte Postgres **und** echten acoustid-index.

Marker `integration` + `db` + `index`: braucht beide Dienste — Steuerung
ueber `--integration` bzw. `ACOUSTID_INTEGRATION_TESTS` (siehe conftest.py im
Repo-Wurzelverzeichnis), Adressen aus `AOFF_DB_*` und `AOFF_INDEX_URL`.

Lokal::

    docker compose -f docker-compose.yml -f tests/docker-compose.test.yml up -d db index
    AOFF_DB_HOST=127.0.0.1 AOFF_INDEX_URL=http://127.0.0.1:6081 \\
        uv run pytest api/tests --integration=require

Der Nachweis, um den es geht, ist die Definition of Done der Phase 11: eine
Einreichung wandert von ``new`` nach ``indexed`` und wird anschliessend vom
**echten** Lookup gefunden — mit ihrer eigenen AcoustID und den eingereichten
MBIDs. Dazu die beiden Faelle, die man nur mit echten Diensten sieht: ein
ausgefallener Suchindex (Einreichung bleibt ``new``, spaeterer Submit holt
nach) und die Frage, ob der reservierte Dokument-ID-Bereich mit importierten
Fingerprints kollidiert.

Der letzte Abschnitt gehoert der **Upstream-Warteschlange (Phase 12)**: dort
geht es um die Anweisungen selbst — Arbeitsvorrat, die vier Phase-12-Spalten
und die 7-Fehler-Grenze in echtem SQL. An api.acoustid.org geht auch dort
nichts hinaus; der Dienst bleibt ein ``httpx.MockTransport``
(`upstream_mock.py`).

Gearbeitet wird mit **echten** Vollvektoren aus einem Tages-Delta;
synthetische Hashes haetten eine andere Bitstatistik, und genau die
entscheidet ueber den Score.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any, NamedTuple
from uuid import uuid4

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool
from upstream_mock import APP_KEY, MockUpstream, error_response, make_forwarder

from acoustid_api.main import create_app
from acoustid_api.service import ApiService
from acoustid_api.store import (
    LOCAL_DOC_ID_BASE,
    load_forward_queue,
    mark_forward_failed,
    mark_forwarded,
    reset_forward_attempts,
)
from acoustid_api.upstream import MAX_FORWARD_ATTEMPTS, drain_queue, retry_forward
from shared.config import Config
from shared.env import EnvSettings
from shared.fingerprint import encode_fingerprint
from shared.fpindex import FpIndexClient, Insert, extract_query
from shared.models import SubmitMode

pytestmark = [pytest.mark.integration, pytest.mark.db, pytest.mark.index]

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPO_ROOT / "tests/fixtures/acoustid-dumps/2026-07-22-fingerprint-update.jsonl.gz"

QUERY_HASHES = Config().index.query_hashes

DAY = date(2026, 7, 22)
STAMP = "2026-07-22T12:00:00+00:00"

MBID = "b81f83ee-4da4-11e0-9ed8-0025225356f3"
OTHER_MBID = "c0a1c0de-4da4-11e0-9ed8-0025225356f3"


class Sample(NamedTuple):
    """Ein echter Fingerprint aus dem Tages-Delta."""

    vector: list[int]
    length: int

    def encoded(self) -> str:
        return encode_fingerprint(self.vector)


@pytest.fixture(scope="module")
def samples() -> list[Sample]:
    if not FIXTURE.exists():
        pytest.skip(
            f"Fixture fehlt: {FIXTURE.relative_to(REPO_ROOT)} — "
            "'uv run python tests/fixtures/fetch_fixtures.py' holt sie nach"
        )
    found: list[Sample] = []
    with gzip.open(FIXTURE, "rt", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            vector = row.get("fingerprint")
            length = row.get("length")
            if vector and length and len(vector) >= 500:
                found.append(Sample(vector=vector, length=int(length)))
            if len(found) == 4:
                break
    if len(found) < 4:  # pragma: no cover - haengt an der Fixture
        pytest.skip("zu wenige lange Vektoren in der Fixture")
    return found


@pytest.fixture
def index() -> Iterator[FpIndexClient]:
    """Frischer, leerer Index; wird nach dem Test wieder geloescht."""
    settings = EnvSettings.from_env()
    with FpIndexClient(settings.index_url, f"pytest{uuid4().hex}") as client:
        client.ensure_index()
        try:
            yield client
        finally:
            client.delete_index()


@pytest.fixture
def pool(scratch_env: EnvSettings) -> Iterator[ConnectionPool]:
    connections = ConnectionPool(
        scratch_env.db_dsn().get_secret_value(),
        min_size=1,
        max_size=2,
        kwargs={"autocommit": True},
        open=False,
    )
    try:
        yield connections
    finally:
        connections.close()


@pytest.fixture
def client(pool: ConnectionPool, index: FpIndexClient) -> Iterator[TestClient]:
    """Die echte App auf Wegwerf-Datenbank und Wegwerf-Index."""
    service = ApiService(pool, index, Config())
    service.open()
    with TestClient(create_app(service)) as test_client:
        yield test_client


def submit(client: TestClient, sample: Sample, **extra: str) -> dict[str, Any]:
    """Ein Submit, wie ihn ein Client schickt (POST, Formular-Rumpf)."""
    response = client.post(
        "/v2/submit",
        data={
            "client": "testkey",
            "user": "usertestkey",
            "fingerprint": sample.encoded(),
            "duration": str(sample.length),
            "mbid": MBID,
            **extra,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def lookup(client: TestClient, sample: Sample, **extra: str) -> dict[str, Any]:
    response = client.post(
        "/v2/lookup",
        data={
            "client": "testkey",
            "fingerprint": sample.encoded(),
            "duration": str(sample.length),
            **extra,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def rows(db: psycopg.Connection) -> list[tuple[Any, ...]]:
    return db.execute(
        "SELECT id, local_track_id, local_track_gid::text, status, mbid::text, length "
        "FROM local_submission ORDER BY id"
    ).fetchall()


# --- Die Definition of Done ------------------------------------------------


def test_a_submission_becomes_indexed_and_is_then_found_by_lookup(
    db: psycopg.Connection, client: TestClient, samples: list[Sample]
) -> None:
    sample = samples[0]
    payload = submit(client, sample)

    stored = rows(db)
    assert [row[3] for row in stored] == ["indexed"]
    assert payload["submissions"] == [{"id": stored[0][0], "status": "pending"}]

    results = lookup(client, sample)["results"]
    assert [item["id"] for item in results] == [stored[0][2]]
    assert results[0]["score"] == 1.0


def test_the_document_lands_in_the_reserved_range(
    db: psycopg.Connection, index: FpIndexClient, client: TestClient, samples: list[Sample]
) -> None:
    submit(client, samples[0])
    local_track_id = rows(db)[0][1]
    hits = index.search(extract_query(samples[0].vector, max_hashes=QUERY_HASHES), limit=40)
    assert [hit.doc_id for hit in hits] == [LOCAL_DOC_ID_BASE + local_track_id]
    assert index.index_info().stats["max_doc_id"] >= LOCAL_DOC_ID_BASE


def test_several_mbids_are_several_rows_but_one_lookup_result(
    db: psycopg.Connection, client: TestClient, samples: list[Sample]
) -> None:
    sample = samples[0]
    response = client.post(
        "/v2/submit",
        content="&".join(
            [
                "client=testkey",
                "user=usertestkey",
                f"fingerprint={sample.encoded()}",
                f"duration={sample.length}",
                f"mbid={MBID}",
                f"mbid={OTHER_MBID}",
            ]
        ),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["submissions"]) == 2

    stored = rows(db)
    assert {row[4] for row in stored} == {MBID, OTHER_MBID}
    assert len({row[1] for row in stored}) == 1

    results = lookup(client, sample)["results"]
    assert len(results) == 1
    assert results[0]["id"] == stored[0][2]


def test_the_submitted_mbids_reach_the_meta_answer(
    client: TestClient, samples: list[Sample]
) -> None:
    """Ohne MusicBrainz-Spiegel: MBIDs und `sources` kommen aus dem eigenen
    Bestand — bei lokalen Einreichungen eben aus `local_submission`."""
    sample = samples[0]
    submit(client, sample)
    submit(client, sample)  # zweite Einreichung derselben MBID
    results = lookup(client, sample, meta="recordings sources")["results"]
    recordings = [recording for item in results for recording in item["recordings"]]
    assert [recording["id"] for recording in recordings] == [MBID, MBID]
    assert [recording["sources"] for recording in recordings] == [1, 1]


def test_the_local_acoustid_can_be_looked_up_by_trackid(
    db: psycopg.Connection, client: TestClient, samples: list[Sample]
) -> None:
    submit(client, samples[0])
    gid = rows(db)[0][2]
    response = client.get("/v2/lookup", params={"client": "testkey", "trackid": gid})
    assert response.json() == {"status": "ok", "results": [{"id": gid, "score": 1.0}]}


# --- Ausgefallener Suchindex ------------------------------------------------


def test_an_unreachable_index_keeps_the_submission_new(
    db: psycopg.Connection, pool: ConnectionPool, samples: list[Sample]
) -> None:
    """Gespeichert ist gespeichert: HTTP 200, Status bleibt `new`."""
    broken = ApiService(pool, FpIndexClient("http://127.0.0.1:1", "tot"), Config())
    broken.open()
    with TestClient(create_app(broken)) as client:
        payload = submit(client, samples[0])
    assert payload["submissions"][0]["status"] == "pending"
    assert [row[3] for row in rows(db)] == ["new"]


def test_a_later_submission_catches_the_backlog_up(
    db: psycopg.Connection,
    pool: ConnectionPool,
    index: FpIndexClient,
    samples: list[Sample],
) -> None:
    broken = ApiService(pool, FpIndexClient("http://127.0.0.1:1", "tot"), Config())
    broken.open()
    with TestClient(create_app(broken)) as client:
        submit(client, samples[0])
    assert [row[3] for row in rows(db)] == ["new"]

    healthy = ApiService(pool, index, Config())
    healthy.open()
    with TestClient(create_app(healthy)) as client:
        submit(client, samples[1], mbid=OTHER_MBID)
        # Beide Einreichungen sind jetzt indexiert — auch die aus dem Rueckstand.
        assert [row[3] for row in rows(db)] == ["indexed", "indexed"]
        assert len(lookup(client, samples[0])["results"]) == 1
        assert len(lookup(client, samples[1])["results"]) == 1


# --- Zusammenspiel mit dem Delta-Bestand ------------------------------------


def test_the_reserved_range_does_not_collide_with_imported_fingerprints(
    db: psycopg.Connection, index: FpIndexClient, client: TestClient, samples: list[Sample]
) -> None:
    """Derselbe Klang aus beiden Welten: zwei Dokumente, zwei AcoustIDs."""
    sample = samples[0]
    imported_gid = uuid4()
    db.execute(
        "INSERT INTO track (id, gid, created, src_day) VALUES (%s, %s, %s, %s)",
        (1, imported_gid, STAMP, DAY),
    )
    db.execute(
        "INSERT INTO fingerprint (id, fingerprint, length, track_id, created, src_day) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (1, sample.vector, sample.length, 1, STAMP, DAY),
    )
    index.update([Insert(doc_id=1, hashes=extract_query(sample.vector, max_hashes=QUERY_HASHES))])

    submit(client, sample)
    local_gid = rows(db)[0][2]

    results = lookup(client, sample)["results"]
    assert {item["id"] for item in results} == {str(imported_gid), local_gid}
    # Der importierte Fingerprint gewinnt den Gleichstand: kleinere Dokument-ID.
    assert results[0]["id"] == str(imported_gid)


def test_an_imported_fingerprint_is_still_found_after_a_submission(
    db: psycopg.Connection, index: FpIndexClient, client: TestClient, samples: list[Sample]
) -> None:
    """Der Submit darf den Delta-Bestand nicht anfassen (§5.2, Import-Regel 2)."""
    imported, submitted = samples[0], samples[1]
    imported_gid = uuid4()
    db.execute(
        "INSERT INTO track (id, gid, created, src_day) VALUES (%s, %s, %s, %s)",
        (1, imported_gid, STAMP, DAY),
    )
    db.execute(
        "INSERT INTO fingerprint (id, fingerprint, length, track_id, created, src_day) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (1, imported.vector, imported.length, 1, STAMP, DAY),
    )
    index.update([Insert(doc_id=1, hashes=extract_query(imported.vector, max_hashes=QUERY_HASHES))])

    submit(client, submitted)

    assert [item["id"] for item in lookup(client, imported)["results"]] == [str(imported_gid)]
    counts = db.execute("SELECT count(*) FROM track").fetchone()
    assert counts is not None
    assert counts[0] == 1


# --- Filter -----------------------------------------------------------------


def test_the_length_window_applies_to_local_submissions_too(
    client: TestClient, samples: list[Sample]
) -> None:
    sample = samples[0]
    submit(client, sample)
    far = Sample(vector=sample.vector, length=sample.length + 20)
    assert lookup(client, far)["results"] == []
    assert len(lookup(client, far, maxdurationdiff="30")["results"]) == 1


def test_a_different_recording_does_not_match_the_submission(
    client: TestClient, samples: list[Sample]
) -> None:
    submit(client, samples[0])
    assert lookup(client, samples[1])["results"] == []


def test_the_stored_vector_survives_the_round_trip(
    db: psycopg.Connection, client: TestClient, samples: list[Sample]
) -> None:
    """Der Vektor wird als signed int32 gespeichert und unveraendert gelesen."""
    sample = samples[0]
    submit(client, sample)
    stored = db.execute("SELECT fingerprint FROM local_submission").fetchone()
    assert stored is not None
    assert [value & 0xFFFFFFFF for value in stored[0]] == [
        value & 0xFFFFFFFF for value in sample.vector
    ]


# --- Upstream-Warteschlange (Phase 12) --------------------------------------
#
# Hier geht es um die Anweisungen selbst: dass der Arbeitsvorrat die richtigen
# Zeilen findet, dass die vier Phase-12-Spalten so beschrieben werden wie in
# §5.2 vorgesehen und dass die 7-Fehler-Grenze in SQL greift. Der HTTP-Verkehr
# bleibt auch hier eine Attrappe (`upstream_mock.py`) — an api.acoustid.org
# geht in keinem Test etwas hinaus.

UPSTREAM_CONFIG = Config.model_validate(
    {"submit": {"mode": SubmitMode.LOCAL_UPSTREAM, "upstream_app_key": APP_KEY}}
)


@pytest.fixture
def upstream() -> MockUpstream:
    return MockUpstream()


def upstream_service(
    pool: ConnectionPool, index: FpIndexClient, upstream: MockUpstream
) -> ApiService:
    """Die echte App im Modus `local+upstream`, Upstream als Attrappe."""
    service = ApiService(pool, index, UPSTREAM_CONFIG, upstream=make_forwarder(upstream))
    service.open()
    return service


def queue(db: psycopg.Connection, **kwargs: Any) -> list[Any]:
    return load_forward_queue(db, limit=100, max_attempts=MAX_FORWARD_ATTEMPTS, **kwargs)


def test_the_queue_holds_what_is_indexed_but_not_forwarded(
    db: psycopg.Connection, client: TestClient, samples: list[Sample]
) -> None:
    submit(client, samples[0], bitrate="320", fileformat="FLAC", artist="Die Band")
    waiting = queue(db)
    assert len(waiting) == 1
    entry = waiting[0]
    assert entry.local_track_id == rows(db)[0][1]
    assert entry.duration == samples[0].length
    assert entry.mbids == (MBID,)
    # Der user-Key steht in `submitted_by` und wird von dort durchgereicht.
    assert entry.submitted_by == "usertestkey"
    assert (entry.bitrate, entry.fileformat, entry.artist) == (320, "FLAC", "Die Band")
    assert [value & 0xFFFFFFFF for value in entry.hashes] == [
        value & 0xFFFFFFFF for value in samples[0].vector
    ]


def test_two_mbids_are_one_queue_entry_with_two_mbids(
    db: psycopg.Connection, client: TestClient, samples: list[Sample]
) -> None:
    sample = samples[0]
    response = client.post(
        "/v2/submit",
        content="&".join(
            [
                "client=testkey",
                "user=usertestkey",
                f"fingerprint={sample.encoded()}",
                f"duration={sample.length}",
                f"mbid={MBID}",
                f"mbid={OTHER_MBID}",
            ]
        ),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 200, response.text
    waiting = queue(db)
    assert len(waiting) == 1
    assert set(waiting[0].mbids) == {MBID, OTHER_MBID}


def test_an_unindexed_submission_is_not_in_the_queue(
    db: psycopg.Connection, pool: ConnectionPool, samples: list[Sample]
) -> None:
    broken = ApiService(pool, FpIndexClient("http://127.0.0.1:1", "tot"), Config())
    broken.open()
    with TestClient(create_app(broken)) as client:
        submit(client, samples[0])
    assert [row[3] for row in rows(db)] == ["new"]
    assert queue(db) == []


def test_the_status_columns_are_written_as_specified(
    db: psycopg.Connection, client: TestClient, samples: list[Sample]
) -> None:
    submit(client, samples[0])
    local_track_id = rows(db)[0][1]

    assert mark_forward_failed(db, local_track_id, "HTTP 503") == 1
    failed = db.execute(
        "SELECT status, forward_attempts, forward_error, forwarded_at "
        "FROM local_submission WHERE local_track_id = %s",
        (local_track_id,),
    ).fetchone()
    assert failed == ("forward_failed", 1, "HTTP 503", None)

    assert mark_forwarded(db, local_track_id) == 1
    done = db.execute(
        "SELECT status, forward_attempts, forward_error, forwarded_at IS NOT NULL "
        "FROM local_submission WHERE local_track_id = %s",
        (local_track_id,),
    ).fetchone()
    assert done == ("forwarded", 1, None, True)
    # Eine bereits weitergeleitete Gruppe wechselt nicht noch einmal.
    assert mark_forwarded(db, local_track_id) == 0
    assert queue(db) == []


def test_all_rows_of_a_group_switch_together_in_sql(
    db: psycopg.Connection, client: TestClient, samples: list[Sample]
) -> None:
    sample = samples[0]
    client.post(
        "/v2/submit",
        content="&".join(
            [
                "client=testkey",
                "user=usertestkey",
                f"fingerprint={sample.encoded()}",
                f"duration={sample.length}",
                f"mbid={MBID}",
                f"mbid={OTHER_MBID}",
            ]
        ),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    local_track_id = rows(db)[0][1]
    assert mark_forwarded(db, local_track_id) == 2
    assert {row[3] for row in rows(db)} == {"forwarded"}


def test_the_seven_error_limit_holds_in_sql(
    db: psycopg.Connection, client: TestClient, samples: list[Sample]
) -> None:
    submit(client, samples[0])
    local_track_id = rows(db)[0][1]
    for expected in range(1, MAX_FORWARD_ATTEMPTS + 1):
        assert mark_forward_failed(db, local_track_id, "HTTP 503") == expected
        assert (queue(db) == []) is (expected >= MAX_FORWARD_ATTEMPTS)

    revived = reset_forward_attempts(db, min_attempts=MAX_FORWARD_ATTEMPTS)
    assert revived == [local_track_id]
    assert len(queue(db)) == 1


def test_the_whole_way_ends_in_forwarded(
    db: psycopg.Connection,
    pool: ConnectionPool,
    index: FpIndexClient,
    upstream: MockUpstream,
    samples: list[Sample],
) -> None:
    """Die Definition of Done der Phase, gegen echte Dienste."""
    service = upstream_service(pool, index, upstream)
    with TestClient(create_app(service)) as client:
        payload = submit(client, samples[0])
        assert payload["submissions"][0]["status"] == "pending"
        # Lokal bleibt alles auffindbar — die Weiterleitung ist eine Zugabe.
        assert len(lookup(client, samples[0])["results"]) == 1

    assert [row[3] for row in rows(db)] == ["forwarded"]
    assert upstream.count == 1
    assert upstream.field("client") == APP_KEY
    assert upstream.field("user") == "usertestkey"
    assert upstream.field("fingerprint.0") == samples[0].encoded()
    assert upstream.values("mbid.0") == [MBID]
    assert queue(db) == []


def test_a_failed_forward_is_retried_by_the_next_drain(
    db: psycopg.Connection,
    pool: ConnectionPool,
    index: FpIndexClient,
    samples: list[Sample],
) -> None:
    """Der Weg aus §8.9: der Update-Lauf holt den Fehlversuch nach."""
    broken = MockUpstream([httpx.Response(503, text="Wartung")])
    service = ApiService(pool, index, UPSTREAM_CONFIG, upstream=make_forwarder(broken))
    service.open()
    with TestClient(create_app(service)) as client:
        submit(client, samples[0])
    assert [row[3] for row in rows(db)] == ["forward_failed"]

    healthy = MockUpstream()
    service.upstream = make_forwarder(healthy)
    report = drain_queue(db, service, max_attempts=1)
    assert (report.attempted, report.forwarded) == (1, 1)
    assert [row[3] for row in rows(db)] == ["forwarded"]
    assert healthy.count == 1


def test_the_manual_retry_works_against_the_real_columns(
    db: psycopg.Connection,
    pool: ConnectionPool,
    index: FpIndexClient,
    samples: list[Sample],
) -> None:
    refuse = MockUpstream(default=lambda: error_response(4, "invalid API key"))
    service = ApiService(pool, index, UPSTREAM_CONFIG, upstream=make_forwarder(refuse))
    service.open()
    with TestClient(create_app(service)) as client:
        submit(client, samples[0])
    for _ in range(MAX_FORWARD_ATTEMPTS - 1):
        drain_queue(db, service, max_attempts=1)

    attempts = db.execute("SELECT DISTINCT forward_attempts FROM local_submission").fetchall()
    assert attempts == [(MAX_FORWARD_ATTEMPTS,)]
    assert queue(db) == []

    healthy = MockUpstream()
    service.upstream = make_forwarder(healthy)
    report = retry_forward(db, service, max_attempts=1)
    assert report.forwarded == 1
    assert healthy.count == 1
    assert [row[3] for row in rows(db)] == ["forwarded"]


def test_outside_the_upstream_mode_the_drain_does_nothing(
    db: psycopg.Connection,
    pool: ConnectionPool,
    index: FpIndexClient,
    upstream: MockUpstream,
    samples: list[Sample],
) -> None:
    service = ApiService(pool, index, Config(), upstream=make_forwarder(upstream))
    service.open()
    with TestClient(create_app(service)) as client:
        submit(client, samples[0])
    assert [row[3] for row in rows(db)] == ["indexed"]
    assert drain_queue(db, service).empty
    assert upstream.count == 0
