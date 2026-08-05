"""``/v2/lookup`` gegen echte Postgres **und** echten acoustid-index (§5.3).

Marker `integration` + `db` + `index`: braucht beide Dienste — Steuerung
ueber `--integration` bzw. `ACOUSTID_INTEGRATION_TESTS` (siehe conftest.py im
Repo-Wurzelverzeichnis), Adressen aus `MMO_DB_*` und `MMO_INDEX_URL`.

Lokal::

    docker compose -f docker-compose.yml -f tests/docker-compose.test.yml up -d db index
    MMO_DB_HOST=127.0.0.1 MMO_INDEX_URL=http://127.0.0.1:6081 \\
        uv run pytest api/tests --integration=require

Der Nachweis, um den es hier geht, ist die Definition of Done der Phase: ein
Fingerprint, der wie im Betrieb eingespielt wurde, wird ueber die echte
HTTP-Schnittstelle mit **seiner** AcoustID und einem plausiblen Score
wiedergefunden — und alles, was nicht passt, faellt an der richtigen Stelle
heraus (Laengenfenster, Score-Cutoff, Merge-Verkettung).

Gearbeitet wird mit **echten** Vollvektoren aus einem Tages-Delta.
Synthetische Hashes wuerden die Frage nicht beantworten: sie haben andere
Bitstatistik, und genau die entscheidet ueber den Score.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import NamedTuple
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

from acoustid_api.main import create_app
from acoustid_api.service import ApiService
from shared.config import Config
from shared.env import EnvSettings
from shared.fingerprint import encode_fingerprint
from shared.fpindex import FpIndexClient, Insert, extract_query

pytestmark = [pytest.mark.integration, pytest.mark.db, pytest.mark.index]

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPO_ROOT / "tests/fixtures/acoustid-dumps/2026-07-22-fingerprint-update.jsonl.gz"

#: Wie in ARCHITECTURE §6 (`index.query_hashes`, Default 120).
QUERY_HASHES = Config().index.query_hashes

DAY = date(2026, 7, 22)
STAMP = "2026-07-22T12:00:00+00:00"


class Sample(NamedTuple):
    """Ein echter Fingerprint aus dem Tages-Delta."""

    vector: list[int]
    length: int

    def encoded(self) -> str:
        """Als Chromaprint-Zeichenkette, wie ein Client sie schickt."""
        return encode_fingerprint(self.vector)


@pytest.fixture(scope="module")
def samples() -> list[Sample]:
    """Ein paar lange, echte Vollvektoren aus dem Delta."""
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
            if len(found) == 6:
                break
    if len(found) < 6:  # pragma: no cover - haengt an der Fixture
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
def client(scratch_env: EnvSettings, index: FpIndexClient) -> Iterator[TestClient]:
    """Die echte App auf Wegwerf-Datenbank und Wegwerf-Index."""
    pool = ConnectionPool(
        scratch_env.db_dsn().get_secret_value(),
        min_size=1,
        max_size=2,
        kwargs={"autocommit": True},
        open=False,
    )
    service = ApiService(pool, index, Config())
    service.open()
    try:
        with TestClient(create_app(service)) as test_client:
            yield test_client
    finally:
        pool.close()


# --- Bestand aufbauen ------------------------------------------------------


def seed(
    db: psycopg.Connection,
    index: FpIndexClient,
    *,
    fingerprint_id: int,
    track_id: int,
    vector: list[int],
    length: int,
    new_id: int | None = None,
    gid: UUID | None = None,
) -> UUID:
    """Legt Track und Fingerprint an und spielt ihn in den Index ein.

    Das ist genau der Weg des Importers: Postgres haelt den Vollvektor, der
    Index bekommt nur den Query-Extrakt.
    """
    track_gid = gid or uuid4()
    db.execute(
        "INSERT INTO track (id, gid, new_id, created, src_day) "
        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
        (track_id, track_gid, new_id, STAMP, DAY),
    )
    db.execute(
        "INSERT INTO fingerprint (id, fingerprint, length, track_id, created, src_day) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (fingerprint_id, vector, length, track_id, STAMP, DAY),
    )
    hashes = extract_query(vector, max_hashes=QUERY_HASHES)
    index.update([Insert(doc_id=fingerprint_id, hashes=hashes)])
    return track_gid


def lookup(client: TestClient, sample: Sample, **extra: str) -> dict:
    """Ein Lookup, wie ihn ein Client schickt (POST, Formular-Rumpf)."""
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


# --- Die Definition of Done ------------------------------------------------


def test_a_seeded_fingerprint_is_found_with_its_own_acoustid(
    db: psycopg.Connection, index: FpIndexClient, client: TestClient, samples: list[Sample]
) -> None:
    sample = samples[0]
    gid = seed(db, index, fingerprint_id=1, track_id=1, vector=sample.vector, length=sample.length)
    payload = lookup(client, sample)
    assert payload == {"status": "ok", "results": [{"id": str(gid), "score": 1.0}]}


def test_a_slightly_different_recording_still_matches(
    db: psycopg.Connection, index: FpIndexClient, client: TestClient, samples: list[Sample]
) -> None:
    """Rauschen in den untersten Bits: der Query-Extrakt bleibt gleich,
    der Score sinkt — muss aber deutlich ueber dem Cutoff bleiben."""
    sample = samples[0]
    gid = seed(db, index, fingerprint_id=1, track_id=1, vector=sample.vector, length=sample.length)
    noisy = Sample(
        vector=[
            value ^ 0b101 if position % 3 == 0 else value
            for position, value in enumerate(sample.vector)
        ],
        length=sample.length,
    )
    results = lookup(client, noisy)["results"]
    assert [item["id"] for item in results] == [str(gid)]
    assert 0.4 < results[0]["score"] < 1.0


def test_an_unknown_fingerprint_yields_no_results(
    db: psycopg.Connection, index: FpIndexClient, client: TestClient, samples: list[Sample]
) -> None:
    seed(
        db,
        index,
        fingerprint_id=1,
        track_id=1,
        vector=samples[0].vector,
        length=samples[0].length,
    )
    assert lookup(client, samples[1])["results"] == []


def test_an_empty_index_yields_no_results(client: TestClient, samples: list[Sample]) -> None:
    assert lookup(client, samples[0])["results"] == []


# --- Filter ----------------------------------------------------------------


def test_the_length_window_filters_the_candidate(
    db: psycopg.Connection, index: FpIndexClient, client: TestClient, samples: list[Sample]
) -> None:
    """Dieselbe Aufnahme, aber eine gemeldete Laenge weit daneben."""
    sample = samples[0]
    seed(db, index, fingerprint_id=1, track_id=1, vector=sample.vector, length=sample.length)
    far = Sample(vector=sample.vector, length=sample.length + 20)
    assert lookup(client, far)["results"] == []
    # Mit einer groesseren Toleranz kommt derselbe Treffer zurueck.
    assert len(lookup(client, far, maxdurationdiff="30")["results"]) == 1


def test_a_weak_candidate_stays_below_the_cutoff(
    db: psycopg.Connection, index: FpIndexClient, client: TestClient, samples: list[Sample]
) -> None:
    """Gleicher Anfang, voellig anderer Rest: der Index findet ihn, der Score nicht."""
    sample = samples[0]
    other = samples[1]
    mixed = sample.vector[:250] + other.vector[250 : len(sample.vector)]
    seed(db, index, fingerprint_id=1, track_id=1, vector=mixed, length=sample.length)
    payload = lookup(client, sample)
    assert payload["results"] == []


def test_two_fingerprints_of_the_same_track_yield_one_result(
    db: psycopg.Connection, index: FpIndexClient, client: TestClient, samples: list[Sample]
) -> None:
    sample = samples[0]
    gid = seed(db, index, fingerprint_id=1, track_id=1, vector=sample.vector, length=sample.length)
    variant = [value ^ 0b11 for value in sample.vector]
    seed(
        db,
        index,
        fingerprint_id=2,
        track_id=1,
        vector=variant,
        length=sample.length,
        gid=gid,
    )
    results = lookup(client, sample)["results"]
    assert [item["id"] for item in results] == [str(gid)]


# --- Merge-Verkettung ------------------------------------------------------


def test_a_merged_track_answers_with_the_target_acoustid(
    db: psycopg.Connection, index: FpIndexClient, client: TestClient, samples: list[Sample]
) -> None:
    """``fingerprint.track_id`` zeigt auf den zurueckgezogenen Track."""
    sample = samples[0]
    target = uuid4()
    db.execute(
        "INSERT INTO track (id, gid, created, src_day) VALUES (%s, %s, %s, %s)",
        (2, target, STAMP, DAY),
    )
    seed(
        db,
        index,
        fingerprint_id=1,
        track_id=1,
        vector=sample.vector,
        length=sample.length,
        new_id=2,
    )
    results = lookup(client, sample)["results"]
    assert [item["id"] for item in results] == [str(target)]


def test_a_merge_chain_is_followed_to_the_end(
    db: psycopg.Connection, index: FpIndexClient, client: TestClient, samples: list[Sample]
) -> None:
    """a -> b -> c: die Antwort traegt die AcoustID von c."""
    sample = samples[0]
    final = uuid4()
    db.execute(
        "INSERT INTO track (id, gid, created, src_day) VALUES (%s, %s, %s, %s)",
        (3, final, STAMP, DAY),
    )
    db.execute(
        "INSERT INTO track (id, gid, new_id, created, src_day) VALUES (%s, %s, %s, %s, %s)",
        (2, uuid4(), 3, STAMP, DAY),
    )
    seed(
        db,
        index,
        fingerprint_id=1,
        track_id=1,
        vector=sample.vector,
        length=sample.length,
        new_id=2,
    )
    assert [item["id"] for item in lookup(client, sample)["results"]] == [str(final)]


def test_trackid_lookup_follows_the_merge_chain(
    db: psycopg.Connection, index: FpIndexClient, client: TestClient
) -> None:
    old = uuid4()
    new = uuid4()
    db.execute(
        "INSERT INTO track (id, gid, created, src_day) VALUES (%s, %s, %s, %s)",
        (2, new, STAMP, DAY),
    )
    db.execute(
        "INSERT INTO track (id, gid, new_id, created, src_day) VALUES (%s, %s, %s, %s, %s)",
        (1, old, 2, STAMP, DAY),
    )
    response = client.get("/v2/lookup", params={"client": "testkey", "trackid": str(old)})
    assert response.json() == {"status": "ok", "results": [{"id": str(new), "score": 1.0}]}


def test_an_unknown_trackid_is_no_error(client: TestClient) -> None:
    response = client.get("/v2/lookup", params={"client": "testkey", "trackid": str(uuid4())})
    assert response.status_code == 200
    assert response.json()["results"] == []


# --- Batchprotokoll --------------------------------------------------------


def test_the_original_batch_protocol_answers_every_part(
    db: psycopg.Connection, index: FpIndexClient, client: TestClient, samples: list[Sample]
) -> None:
    first, second = samples[0], samples[1]
    gid_one = seed(
        db, index, fingerprint_id=1, track_id=1, vector=first.vector, length=first.length
    )
    gid_two = seed(
        db, index, fingerprint_id=2, track_id=2, vector=second.vector, length=second.length
    )
    response = client.post(
        "/v2/lookup",
        data={
            "client": "testkey",
            "batch": "1",
            "fingerprint.0": first.encoded(),
            "duration.0": str(first.length),
            "fingerprint.1": second.encoded(),
            "duration.1": str(second.length),
        },
    )
    payload = response.json()
    assert [part["index"] for part in payload["fingerprints"]] == [0, 1]
    assert payload["fingerprints"][0]["results"][0]["id"] == str(gid_one)
    assert payload["fingerprints"][1]["results"][0]["id"] == str(gid_two)


# --- Eigener Batch-Endpunkt (Phase 13) -------------------------------------


def lookup_batch(client: TestClient, queries: list[dict], **envelope: object) -> dict:
    """Ein Batch, wie ihn ein Client schickt (POST, JSON-Rumpf)."""
    response = client.post(
        "/v2/lookup/batch",
        json={"client": "testkey", "queries": queries, **envelope},
    )
    assert response.status_code == 200, response.text
    return response.json()


def query(sample: Sample, **extra: object) -> dict:
    return {"fingerprint": sample.encoded(), "duration": sample.length, **extra}


def test_the_batch_endpoint_answers_every_entry_in_order(
    db: psycopg.Connection, index: FpIndexClient, client: TestClient, samples: list[Sample]
) -> None:
    first, second, unknown = samples[0], samples[1], samples[2]
    gid_one = seed(
        db, index, fingerprint_id=1, track_id=1, vector=first.vector, length=first.length
    )
    gid_two = seed(
        db, index, fingerprint_id=2, track_id=2, vector=second.vector, length=second.length
    )

    payload = lookup_batch(client, [query(second), query(unknown), query(first)])
    responses = payload["responses"]

    assert [entry["index"] for entry in responses] == [0, 1, 2]
    assert [entry["status"] for entry in responses] == ["ok", "ok", "ok"]
    assert [item["id"] for item in responses[0]["results"]] == [str(gid_two)]
    assert responses[1]["results"] == []
    assert [item["id"] for item in responses[2]["results"]] == [str(gid_one)]


def test_the_batch_endpoint_finds_delta_stock_and_a_local_submission(
    db: psycopg.Connection, index: FpIndexClient, client: TestClient, samples: list[Sample]
) -> None:
    """Beide Dokument-ID-Bereiche in **einer** Anfrage (ARCHITECTURE §5.3)."""
    imported, submitted = samples[0], samples[1]
    delta_gid = seed(
        db, index, fingerprint_id=1, track_id=1, vector=imported.vector, length=imported.length
    )
    response = client.post(
        "/v2/submit",
        data={
            "client": "testkey",
            "user": "usertestkey",
            "fingerprint": submitted.encoded(),
            "duration": str(submitted.length),
            "mbid": "b81f83ee-4da4-11e0-9ed8-0025225356f3",
        },
    )
    assert response.status_code == 200, response.text
    local_gid = db.execute("SELECT local_track_gid::text FROM local_submission").fetchall()[0][0]

    payload = lookup_batch(client, [query(imported), query(submitted)])
    responses = payload["responses"]
    assert [item["id"] for item in responses[0]["results"]] == [str(delta_gid)]
    assert [item["id"] for item in responses[1]["results"]] == [local_gid]


def test_a_broken_entry_does_not_stop_the_real_pipeline(
    db: psycopg.Connection, index: FpIndexClient, client: TestClient, samples: list[Sample]
) -> None:
    sample = samples[0]
    gid = seed(db, index, fingerprint_id=1, track_id=1, vector=sample.vector, length=sample.length)
    responses = lookup_batch(
        client,
        [query(sample), {"fingerprint": "kaputt!!", "duration": 241}, query(sample)],
    )["responses"]

    assert [entry["status"] for entry in responses] == ["ok", "error", "ok"]
    assert responses[1]["error"]["code"] == 3
    assert [item["id"] for item in responses[0]["results"]] == [str(gid)]
    assert [item["id"] for item in responses[2]["results"]] == [str(gid)]


def test_meta_in_the_batch_comes_from_the_own_database(
    db: psycopg.Connection, index: FpIndexClient, client: TestClient, samples: list[Sample]
) -> None:
    """Ohne MusicBrainz-Spiegel (§8.7): MBIDs und ``sources`` bleiben."""
    sample = samples[0]
    mbid = uuid4()
    seed(db, index, fingerprint_id=1, track_id=1, vector=sample.vector, length=sample.length)
    db.execute(
        "INSERT INTO track_mbid (id, track_id, mbid, submission_count, disabled, created, "
        "src_day) VALUES (%s, %s, %s, %s, false, %s, %s)",
        (1, 1, mbid, 9, STAMP, DAY),
    )
    responses = lookup_batch(client, [query(sample)], meta="recordings sources")["responses"]
    assert responses[0]["results"][0]["recordings"] == [{"id": str(mbid), "sources": 9}]


def test_the_batch_limit_holds_against_the_real_app(
    client: TestClient, samples: list[Sample]
) -> None:
    response = client.post(
        "/v2/lookup/batch",
        json={"client": "testkey", "queries": [query(samples[0])] * 101},
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == 19


# --- Rows ohne Vektor ------------------------------------------------------


def test_a_fingerprint_row_without_a_vector_is_no_candidate(
    db: psycopg.Connection, index: FpIndexClient, client: TestClient, samples: list[Sample]
) -> None:
    """Der Index kann eine ID kennen, die in Postgres (noch) keinen Vektor hat."""
    sample = samples[0]
    db.execute(
        "INSERT INTO track (id, gid, created, src_day) VALUES (%s, %s, %s, %s)",
        (1, uuid4(), STAMP, DAY),
    )
    db.execute(
        "INSERT INTO fingerprint (id, track_id, length, src_day) VALUES (%s, %s, %s, %s)",
        (1, 1, sample.length, DAY),
    )
    index.update([Insert(doc_id=1, hashes=extract_query(sample.vector, max_hashes=QUERY_HASHES))])
    assert lookup(client, sample)["results"] == []
