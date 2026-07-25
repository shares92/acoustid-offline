"""Index-Client gegen das echte acoustid-index-Image (ARCHITECTURE §5.3).

Marker `integration` + `index`: laeuft nur mit erreichbarem Index — Steuerung
ueber `--integration` bzw. `ACOUSTID_INTEGRATION_TESTS` (siehe conftest.py im
Repo-Wurzelverzeichnis), Adresse aus `AOFF_INDEX_URL`.

Lokal::

    docker compose -f docker-compose.yml -f tests/docker-compose.test.yml up -d index
    AOFF_INDEX_URL=http://127.0.0.1:6081 uv run pytest -m "integration and index" \\
        --integration=require

Gearbeitet wird mit **echten** Fingerprint-Vektoren aus den Tages-Deltas
(`tests/fixtures/acoustid-dumps/`, nicht im Repo — `fetch_fixtures.py`).
Synthetische Hashes wuerden die Kernfrage nicht beantworten: ob ein Vektor,
der durch `extract_query` gegangen ist, sich mit derselben Query wiederfindet
und sich von einem anderen Fingerprint unterscheidet.

Jeder Test bekommt einen frisch angelegten, eigenen Index und raeumt ihn
wieder ab; der Server kann beliebig viele nebeneinander halten.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple
from uuid import uuid4

import pytest

from shared.env import EnvSettings
from shared.fpindex import (
    Delete,
    FpIndexClient,
    FpIndexNotFoundError,
    FpIndexVersionMismatchError,
    Insert,
    extract_query,
)

pytestmark = [pytest.mark.integration, pytest.mark.index]

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPO_ROOT / "tests/fixtures/acoustid-dumps/2026-07-22-fingerprint-update.jsonl.gz"

#: Wie in ARCHITECTURE §6 (`index.query_hashes`, Default 120).
QUERY_HASHES = 120


class Fingerprint(NamedTuple):
    """Eine Zeile aus dem Fingerprint-Delta."""

    fp_id: int
    vector: list[int]
    length: int

    def query(self, max_hashes: int = QUERY_HASHES) -> list[int]:
        return extract_query(self.vector, max_hashes=max_hashes)


@pytest.fixture(scope="module")
def fingerprints() -> list[Fingerprint]:
    """Die ersten echten Fingerprints aus dem Tages-Delta."""
    if not FIXTURE.exists():
        pytest.skip(
            f"Fixture fehlt: {FIXTURE.relative_to(REPO_ROOT)} — "
            "'uv run python tests/fixtures/fetch_fixtures.py' holt sie nach"
        )
    rows: list[Fingerprint] = []
    with gzip.open(FIXTURE, "rt", encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            vector = record.get("fingerprint")
            if not vector:
                continue
            rows.append(Fingerprint(record["id"], vector, record["length"]))
            if len(rows) == 20:
                break
    assert len(rows) >= 3, "Fixture liefert zu wenige Fingerprints"
    return rows


@pytest.fixture
def index() -> Iterator[FpIndexClient]:
    """Frischer, leerer Index; wird nach dem Test wieder geloescht."""
    settings = EnvSettings.from_env()
    name = f"pytest{uuid4().hex}"
    with FpIndexClient(settings.index_url, name) as client:
        client.ensure_index()
        try:
            yield client
        finally:
            client.delete_index()


# --- Grundfunktionen -------------------------------------------------------


def test_the_server_is_alive_and_reports_metrics(index: FpIndexClient) -> None:
    assert index.server_health() is True
    assert "aindex_" in index.metrics()


def test_ensure_index_is_idempotent(index: FpIndexClient) -> None:
    """Ein zweiter Aufruf ist kein Fehler — der Bootstrap darf ihn wiederholen."""
    first = index.ensure_index()
    second = index.ensure_index()
    assert first == second
    assert first.ready is True


def test_a_fresh_index_is_healthy_empty_and_at_version_zero(index: FpIndexClient) -> None:
    assert index.index_health() is True
    index.require_ready()
    info = index.index_info()
    assert info.version == 0
    assert info.metadata == {}
    assert info.num_docs == 0


def test_an_unknown_index_is_reported_as_missing() -> None:
    settings = EnvSettings.from_env()
    with FpIndexClient(settings.index_url, f"pytest{uuid4().hex}") as client:
        assert client.index_health() is False
        with pytest.raises(FpIndexNotFoundError):
            client.index_info()
        with pytest.raises(FpIndexNotFoundError):
            client.search([16])


# --- Der eigentliche Nachweis: einfuegen und wiederfinden ------------------


def test_a_real_fingerprint_is_found_again_with_its_own_query(
    index: FpIndexClient, fingerprints: list[Fingerprint]
) -> None:
    """Der Kern von Phase 5: extract_query -> update -> search findet zurueck."""
    subject = fingerprints[0]
    query = subject.query()
    assert 100 <= len(query) <= QUERY_HASHES, "echte Vektoren fuellen die Query fast aus"

    version = index.update([Insert(doc_id=subject.fp_id, hashes=query)])
    assert version == 1

    hits = index.search(query, limit=40)
    assert hits, "der eigene Fingerprint muss sich wiederfinden"
    assert hits[0].doc_id == subject.fp_id
    # Score = Anzahl der Query-Hashes im Dokument. Dokument und Query sind
    # hier identisch, also muessen alle vorkommen.
    assert hits[0].score == len(query)


def test_a_different_fingerprint_does_not_become_the_top_hit(
    index: FpIndexClient, fingerprints: list[Fingerprint]
) -> None:
    """Zwei fremde Aufnahmen duerfen sich nicht gegenseitig ausstechen."""
    subject, other = fingerprints[0], fingerprints[1]
    assert subject.vector != other.vector

    index.update(
        [
            Insert(doc_id=subject.fp_id, hashes=subject.query()),
            Insert(doc_id=other.fp_id, hashes=other.query()),
        ]
    )

    for wanted, foreign in ((subject, other), (other, subject)):
        hits = index.search(wanted.query(), limit=40)
        assert hits[0].doc_id == wanted.fp_id
        assert hits[0].score == len(wanted.query())
        foreign_scores = [hit.score for hit in hits if hit.doc_id == foreign.fp_id]
        assert all(score < hits[0].score for score in foreign_scores), (
            "ein fremder Fingerprint darf hoechstens weit hinter dem eigenen liegen"
        )


def test_an_unknown_fingerprint_finds_nothing_of_ours(
    index: FpIndexClient, fingerprints: list[Fingerprint]
) -> None:
    """Der Normalfall eines Lookups auf eine unbekannte Aufnahme."""
    known, unknown = fingerprints[0], fingerprints[2]
    index.update([Insert(doc_id=known.fp_id, hashes=known.query())])

    hits = index.search(unknown.query(), limit=40)
    assert all(hit.doc_id != known.fp_id for hit in hits), (
        f"fremde Query traf unser Dokument: {hits}"
    )


def test_a_batch_of_many_fingerprints_stays_addressable(
    index: FpIndexClient, fingerprints: list[Fingerprint]
) -> None:
    """Wie der Importer schreibt: ein Batch, danach jedes Dokument einzeln suchen."""
    index.update([Insert(doc_id=row.fp_id, hashes=row.query()) for row in fingerprints])
    assert index.index_info().num_docs == len(fingerprints)

    for row in fingerprints:
        hits = index.search(row.query(), limit=40)
        assert hits[0].doc_id == row.fp_id


# --- Atomares Schreiben, Versionen, Metadaten -----------------------------


def test_every_update_raises_the_version_by_one(
    index: FpIndexClient, fingerprints: list[Fingerprint]
) -> None:
    assert index.update([Insert(doc_id=fingerprints[0].fp_id, hashes=fingerprints[0].query())]) == 1
    assert index.update([Insert(doc_id=fingerprints[1].fp_id, hashes=fingerprints[1].query())]) == 2
    assert index.index_info().version == 2


def test_expected_version_guards_the_batch(
    index: FpIndexClient, fingerprints: list[Fingerprint]
) -> None:
    """Optimistisches Sperren: bei falscher Erwartung wird nichts geschrieben."""
    first, second = fingerprints[0], fingerprints[1]
    assert index.update([Insert(doc_id=first.fp_id, hashes=first.query())], expected_version=0) == 1

    with pytest.raises(FpIndexVersionMismatchError) as caught:
        index.update([Insert(doc_id=second.fp_id, hashes=second.query())], expected_version=0)
    assert caught.value.status_code == 409
    assert caught.value.error_code == "VersionMismatch"

    # Der abgelehnte Batch darf keine Spur hinterlassen.
    assert index.index_info().version == 1
    assert index.search(second.query(), limit=40) == []

    # Mit der richtigen Erwartung geht derselbe Batch durch.
    retry = [Insert(doc_id=second.fp_id, hashes=second.query())]
    assert index.update(retry, expected_version=1) == 2


def test_metadata_survives_a_batch(index: FpIndexClient, fingerprints: list[Fingerprint]) -> None:
    """Phase 7 legt hier den Fortschritt des Index-Feeds ab."""
    last = fingerprints[-1]
    index.update(
        [Insert(doc_id=row.fp_id, hashes=row.query()) for row in fingerprints],
        metadata={"last_fp_id": str(last.fp_id)},
    )
    assert index.get_metadata() == {"last_fp_id": str(last.fp_id)}


def test_the_last_change_of_a_batch_wins(
    index: FpIndexClient, fingerprints: list[Fingerprint]
) -> None:
    """Doppelte IDs im selben Batch: der letzte Eintrag zaehlt."""
    first, second = fingerprints[0], fingerprints[1]
    index.update(
        [
            Insert(doc_id=4711, hashes=first.query()),
            Insert(doc_id=4711, hashes=second.query()),
        ]
    )
    assert index.search(second.query(), limit=40)[0].doc_id == 4711
    assert all(hit.doc_id != 4711 for hit in index.search(first.query(), limit=40))


# --- Loeschen --------------------------------------------------------------


def test_a_deleted_document_is_gone_from_the_results(
    index: FpIndexClient, fingerprints: list[Fingerprint]
) -> None:
    subject = fingerprints[0]
    index.update([Insert(doc_id=subject.fp_id, hashes=subject.query())])
    assert index.search(subject.query(), limit=40)[0].doc_id == subject.fp_id

    index.delete_doc(subject.fp_id)
    assert index.search(subject.query(), limit=40) == []


def test_delete_inside_a_batch_works_too(
    index: FpIndexClient, fingerprints: list[Fingerprint]
) -> None:
    first, second = fingerprints[0], fingerprints[1]
    index.update(
        [
            Insert(doc_id=first.fp_id, hashes=first.query()),
            Insert(doc_id=second.fp_id, hashes=second.query()),
        ]
    )
    index.update([Delete(doc_id=first.fp_id)])

    assert index.search(first.query(), limit=40) == []
    assert index.search(second.query(), limit=40)[0].doc_id == second.fp_id


def test_deleting_an_unknown_document_is_harmless(index: FpIndexClient) -> None:
    index.delete_doc(999_999_999)


def test_a_deleted_index_is_really_gone(index: FpIndexClient) -> None:
    index.delete_index()
    assert index.index_health() is False
    # Die Fixture raeumt danach noch einmal ab — das muss folgenlos bleiben.


# --- Fehlerverhalten des Servers ------------------------------------------


def test_the_server_rejects_values_outside_u32_before_we_send_them(
    index: FpIndexClient, fingerprints: list[Fingerprint]
) -> None:
    """Rohe (signed) Vektoren duerfen nie ungeprueft in den Index gehen."""
    with pytest.raises(ValueError, match="ausserhalb von u32"):
        index.update([Insert(doc_id=1, hashes=fingerprints[0].vector)])


def test_the_search_timeout_becomes_a_search_timeout_error(
    index: FpIndexClient, fingerprints: list[Fingerprint]
) -> None:
    """Serverseitige Frist: HTTP 500 + `Timeout`, kein Teilergebnis.

    Eine Frist von 1 ms ist auf jeder Hardware zu knapp; sollte der Server
    trotzdem rechtzeitig fertig werden, ist das kein Fehlschlag des Clients.
    """
    from shared.fpindex import FpIndexSearchTimeoutError

    index.update([Insert(doc_id=row.fp_id, hashes=row.query()) for row in fingerprints])
    try:
        index.search(fingerprints[0].query(), timeout_ms=1, limit=40)
    except FpIndexSearchTimeoutError as error:
        assert error.status_code == 500
        assert error.error_code == "Timeout"
