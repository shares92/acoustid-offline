"""Index-Feed gegen echte Postgres **und** echten acoustid-index (§5.3).

Marker `integration` + `db` + `index`: braucht beide Dienste — Steuerung
ueber `--integration` bzw. `ACOUSTID_INTEGRATION_TESTS` (siehe conftest.py im
Repo-Wurzelverzeichnis), Adressen aus `AOFF_DB_*` und `AOFF_INDEX_URL`.

Lokal::

    docker compose -f docker-compose.yml -f tests/docker-compose.test.yml up -d db index
    AOFF_DB_HOST=127.0.0.1 AOFF_INDEX_URL=http://127.0.0.1:6081 \\
        uv run pytest importer/tests --integration=require

Der Nachweis, um den es geht: nach Delta-Import und Feed findet die Suche
mit **derselben** Query, die der Feed indexiert hat, genau den Fingerprint
wieder, aus dem sie stammt — und ``fingerprint.indexed_at`` steht erst dann.
Gearbeitet wird mit echten Vektoren aus dem Tages-Delta; synthetische Hashes
wuerden die Frage nicht beantworten.

Jeder Test bekommt einen eigenen, frisch angelegten Index (der Server kann
beliebig viele halten) und raeumt ihn wieder ab.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import date
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from acoustid_importer.dbimport import import_file, import_files
from acoustid_importer.errors import IndexFeedError
from acoustid_importer.indexfeed import METADATA_LAST_ID, feed_index
from acoustid_importer.streams import DeltaFile, Stream
from shared.env import EnvSettings
from shared.fpindex import FpIndexClient, Insert, extract_query

pytestmark = [pytest.mark.integration, pytest.mark.db, pytest.mark.index]

DAY = date(2026, 7, 22)
STAMP = "2026-07-22T12:00:00+00:00"

#: Wie in ARCHITECTURE §6 (`index.query_hashes`, Default 120).
QUERY_HASHES = 120


@pytest.fixture
def index() -> Iterator[FpIndexClient]:
    """Frischer, leerer Index; wird nach dem Test wieder geloescht."""
    settings = EnvSettings.from_env()
    with FpIndexClient(settings.index_url, f"pytest{uuid4().hex}") as client:
        try:
            yield client
        finally:
            client.delete_index()


def count(conn: psycopg.Connection, where: str = "TRUE") -> int:
    row = conn.execute(f"SELECT count(*) FROM fingerprint WHERE {where}").fetchone()
    assert row is not None
    return int(row[0])


def vectors(conn: psycopg.Connection, limit: int = 3) -> list[tuple[int, list[int]]]:
    """Die ersten Vektoren aus der Datenbank, aufsteigend nach id."""
    return [
        (row[0], row[1])
        for row in conn.execute(
            "SELECT id, fingerprint FROM fingerprint ORDER BY id LIMIT %s", (limit,)
        ).fetchall()
    ]


# --- Der volle Weg: Delta -> Postgres -> Index -----------------------------


@pytest.fixture
def day_in_db(db: psycopg.Connection, dumps: Path) -> psycopg.Connection:
    """Die Fingerprint-Zeilen des 22.07.2026 in der Datenbank."""
    files = [
        dumps / "2026-07-22-fingerprint-update.jsonl.gz",
        dumps / "2026-07-22-track_fingerprint-update.jsonl.gz",
    ]
    for _ in import_files(db, files):
        pass
    return db


def test_a_real_day_reaches_the_index_in_batches_of_a_thousand(
    day_in_db: psycopg.Connection, index: FpIndexClient
) -> None:
    """2.214 Fingerprints, Batches à 1000, aufsteigend nach id (§5.3)."""
    report = feed_index(day_in_db, index, max_hashes=QUERY_HASHES)

    assert report.documents == 2_214
    assert report.batches == 3  # 1000 + 1000 + 214
    assert report.scanned == 2_214
    assert report.incomplete == 0
    assert report.empty_queries == 0
    assert report.exhausted
    assert report.last_id == 104_078_665  # hoechste id des Tages
    assert index.index_info().num_docs == 2_214


def test_after_the_feed_every_row_is_marked_indexed(
    day_in_db: psycopg.Connection, index: FpIndexClient
) -> None:
    assert count(day_in_db, "indexed_at IS NULL") == 2_214
    feed_index(day_in_db, index, max_hashes=QUERY_HASHES)
    assert count(day_in_db, "indexed_at IS NULL") == 0
    assert count(day_in_db, "indexed_at IS NOT NULL") == 2_214


def test_an_indexed_fingerprint_is_found_again_with_its_own_query(
    day_in_db: psycopg.Connection, index: FpIndexClient
) -> None:
    """Der Kern von Phase 7: was der Feed schreibt, findet die Suche wieder."""
    feed_index(day_in_db, index, max_hashes=QUERY_HASHES)

    for fp_id, vector in vectors(day_in_db):
        query = extract_query(vector, max_hashes=QUERY_HASHES)
        hits = index.search(query, limit=40)
        assert hits, f"Fingerprint {fp_id} nicht wiedergefunden"
        assert hits[0].doc_id == fp_id
        assert hits[0].score == len(query)


def test_a_second_feed_has_nothing_left_to_do(
    day_in_db: psycopg.Connection, index: FpIndexClient
) -> None:
    """Der Arbeitsvorrat ist der Partialindex — nach dem Feed ist er leer."""
    first = feed_index(day_in_db, index, max_hashes=QUERY_HASHES)
    version_after_first = index.index_info().version

    second = feed_index(day_in_db, index, max_hashes=QUERY_HASHES)
    assert second.documents == 0
    assert second.batches == 0
    assert second.scanned == 0
    assert first.documents > 0
    # Ohne Arbeit kein Schreibzugriff: die Version bleibt stehen.
    assert index.index_info().version == version_after_first


def test_the_last_handed_over_id_lands_in_the_index_metadata(
    day_in_db: psycopg.Connection, index: FpIndexClient
) -> None:
    report = feed_index(day_in_db, index, max_hashes=QUERY_HASHES)
    assert index.get_metadata() == {METADATA_LAST_ID: str(report.last_id)}


def test_max_rows_stops_the_run_and_the_next_one_continues(
    day_in_db: psycopg.Connection, index: FpIndexClient
) -> None:
    """Teil-Laeufe (Probelauf, Phase 8) duerfen nichts ueberspringen."""
    first = feed_index(day_in_db, index, max_hashes=QUERY_HASHES, batch_size=50, max_rows=100)
    assert first.documents == 100
    assert first.batches == 2
    assert not first.exhausted
    assert count(day_in_db, "indexed_at IS NOT NULL") == 100

    second = feed_index(day_in_db, index, max_hashes=QUERY_HASHES)
    assert second.documents == 2_114
    assert count(day_in_db, "indexed_at IS NULL") == 0
    assert index.index_info().num_docs == 2_214


# --- Sonderfaelle des Arbeitsvorrats ---------------------------------------


def test_a_row_without_a_vector_stays_in_the_backlog(
    db: psycopg.Connection, index: FpIndexClient, write_delta: Callable[..., Path]
) -> None:
    """Nur `track_fingerprint` war da — der Vektor kann noch kommen."""
    import_file(
        db,
        write_delta(
            DeltaFile(DAY, Stream.TRACK_FINGERPRINT).name,
            [
                {
                    "id": 7,
                    "track_id": 1,
                    "fingerprint_id": 7,
                    "submission_count": 1,
                    "created": STAMP,
                }
            ],
        ),
    )

    report = feed_index(db, index, max_hashes=QUERY_HASHES)
    assert report.documents == 0
    assert report.incomplete == 1
    assert count(db, "indexed_at IS NULL") == 1

    # Sobald der Vektor da ist, greift derselbe Arbeitsvorrat.
    import_file(
        db,
        write_delta(
            DeltaFile(DAY, Stream.FINGERPRINT).name,
            [{"id": 7, "fingerprint": list(range(1000, 1300)), "length": 30, "created": STAMP}],
        ),
    )
    later = feed_index(db, index, max_hashes=QUERY_HASHES)
    assert later.documents == 1
    assert later.incomplete == 0
    assert count(db, "indexed_at IS NULL") == 0


def test_a_vector_without_indexable_hashes_is_marked_done(
    db: psycopg.Connection, index: FpIndexClient, write_delta: Callable[..., Path]
) -> None:
    """Nur Stille: es gibt nichts zu uebergeben, aber auch nichts zu wiederholen."""
    import_file(
        db,
        write_delta(
            DeltaFile(DAY, Stream.FINGERPRINT).name,
            [
                {"id": 1, "fingerprint": [627964279, 627964279], "length": 5, "created": STAMP},
                {"id": 2, "fingerprint": [], "length": 0, "created": STAMP},
                {"id": 3, "fingerprint": list(range(2000, 2300)), "length": 30, "created": STAMP},
            ],
        ),
    )

    report = feed_index(db, index, max_hashes=QUERY_HASHES)
    assert report.empty_queries == 2
    assert report.documents == 1
    assert report.last_id == 3
    assert count(db, "indexed_at IS NULL") == 0
    assert index.index_info().num_docs == 1


def test_documents_are_handed_over_in_ascending_id_order(
    db: psycopg.Connection, index: FpIndexClient, write_delta: Callable[..., Path]
) -> None:
    """Aufsteigend nach id — das macht den Index rund 15 % kleiner (§5.3)."""
    rows = [
        {
            "id": fp_id,
            "fingerprint": list(range(fp_id * 1000, fp_id * 1000 + 300)),
            "length": 30,
            "created": STAMP,
        }
        for fp_id in (30, 10, 20)
    ]
    import_file(db, write_delta(DeltaFile(DAY, Stream.FINGERPRINT).name, rows))

    seen: list[list[int]] = []
    original_update = index.update

    def recording_update(changes, **kwargs):  # type: ignore[no-untyped-def]
        block = list(changes)
        seen.append([change.doc_id for change in block])
        return original_update(block, **kwargs)

    index.update = recording_update  # type: ignore[method-assign]
    feed_index(db, index, max_hashes=QUERY_HASHES, batch_size=2)

    assert seen == [[10, 20], [30]]


# --- Sicherungen ------------------------------------------------------------


def test_the_database_is_only_marked_after_the_index_took_the_batch(
    db: psycopg.Connection, index: FpIndexClient, write_delta: Callable[..., Path]
) -> None:
    """Reihenfolge-Invariante: erst der Index, dann `indexed_at`.

    Andersherum waeren als indexiert markierte Fingerprints moeglich, die
    der Index nie gesehen hat — ein stiller Datenverlust im Lookup.
    """
    import_file(
        db,
        write_delta(
            DeltaFile(DAY, Stream.FINGERPRINT).name,
            [
                {
                    "id": fp_id,
                    "fingerprint": list(range(fp_id * 1000, fp_id * 1000 + 300)),
                    "length": 30,
                    "created": STAMP,
                }
                for fp_id in (1, 2)
            ],
        ),
    )

    def failing_update(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("Index weg")

    index.ensure_index()
    index.update = failing_update  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="Index weg"):
        feed_index(db, index, max_hashes=QUERY_HASHES)

    assert count(db, "indexed_at IS NULL") == 2


def test_a_foreign_writer_between_two_batches_stops_the_feed(
    db: psycopg.Connection, index: FpIndexClient, write_delta: Callable[..., Path]
) -> None:
    """`expected_version` faengt einen zweiten Schreiber am selben Index ab.

    Nachgestellt wird der ungemuetliche Fall: der Fremdschreiber kommt
    **zwischen** zwei Batches des laufenden Feeds. Ein Schreiber vor dem
    Start waere harmlos — der Feed liest die Version ja erst dann.
    """
    import_file(
        db,
        write_delta(
            DeltaFile(DAY, Stream.FINGERPRINT).name,
            [
                {
                    "id": fp_id,
                    "fingerprint": list(range(fp_id * 1000, fp_id * 1000 + 300)),
                    "length": 30,
                    "created": STAMP,
                }
                for fp_id in (1, 2)
            ],
        ),
    )

    original_update = index.update
    calls = 0

    def meddling_update(changes, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 2:
            original_update([Insert(doc_id=999, hashes=[16, 32, 48])])
        return original_update(changes, **kwargs)

    index.update = meddling_update  # type: ignore[method-assign]
    with pytest.raises(IndexFeedError, match="zweiter Schreiber"):
        feed_index(db, index, max_hashes=QUERY_HASHES, batch_size=1)

    # Der erste Batch ist durch, der zweite gar nicht erst geschrieben.
    assert count(db, "indexed_at IS NOT NULL") == 1
    assert count(db, "indexed_at IS NULL") == 1

    # Ohne die Sicherung laeuft der Rest durch — der Arbeitsvorrat ist intakt.
    index.update = original_update  # type: ignore[method-assign]
    report = feed_index(db, index, max_hashes=QUERY_HASHES, guard_version=False)
    assert report.documents == 1
    assert count(db, "indexed_at IS NULL") == 0


def test_the_feed_creates_the_index_if_it_is_missing(
    db: psycopg.Connection, index: FpIndexClient
) -> None:
    """Der Importer ist laut Compose der Dienst, der den Index anlegt."""
    assert index.index_health() is False
    report = feed_index(db, index, max_hashes=QUERY_HASHES)
    assert report.documents == 0
    assert index.index_health() is True


@pytest.mark.parametrize(("batch_size", "max_rows"), [(0, None), (1, 0)])
def test_nonsense_sizes_are_refused(
    db: psycopg.Connection, index: FpIndexClient, batch_size: int, max_rows: int | None
) -> None:
    with pytest.raises(ValueError):
        feed_index(db, index, max_hashes=QUERY_HASHES, batch_size=batch_size, max_rows=max_rows)
