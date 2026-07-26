"""Der reservierte Dokument-ID-Bereich lokaler Einreichungen (Phase 11).

Die ganze Auffindbarkeit eigener Submissions haengt an einer einzigen Zahl:
:data:`~acoustid_api.store.LOCAL_DOC_ID_BASE`. Sie muss **oberhalb** von
allem liegen, was ``fingerprint.id`` je annehmen kann, und der ganze Bereich
muss **innerhalb** dessen bleiben, was der Suchindex als Dokument-ID
akzeptiert. Beides steht hier als Rechnung, damit eine spaetere Aenderung
nicht still eine Kollision erzeugt.
"""

from __future__ import annotations

import pytest
from stubs import StubConnection

from acoustid_api.store import (
    LOCAL_DOC_ID_BASE,
    MAX_LOCAL_TRACK_ID,
    lookup_meta_ids,
    split_doc_ids,
)
from shared.fpindex.query import UINT32_MASK

#: Groesster Wert des Postgres-Typs `integer` — der Typ von `fingerprint.id`.
POSTGRES_INT_MAX = 2**31 - 1


def test_the_range_starts_above_every_possible_fingerprint_id() -> None:
    assert LOCAL_DOC_ID_BASE == POSTGRES_INT_MAX + 1


def test_the_range_ends_at_the_largest_document_id_the_index_takes() -> None:
    """Empirisch (Phase 11): ab 2^32 antwortet der Index `IntegerOverflow`."""
    assert LOCAL_DOC_ID_BASE + MAX_LOCAL_TRACK_ID == UINT32_MASK


def test_the_two_ranges_are_the_same_size() -> None:
    """Der Suchindex wird exakt halbiert — Delta unten, Eigenes oben."""
    assert MAX_LOCAL_TRACK_ID == POSTGRES_INT_MAX


@pytest.mark.parametrize(
    ("doc_ids", "expected"),
    [
        ([], ([], [])),
        ([1, 2, 3], ([1, 2, 3], [])),
        ([LOCAL_DOC_ID_BASE, LOCAL_DOC_ID_BASE + 7], ([], [0, 7])),
        ([5, LOCAL_DOC_ID_BASE + 1, 3], ([3, 5], [1])),
        ([POSTGRES_INT_MAX, LOCAL_DOC_ID_BASE], ([POSTGRES_INT_MAX], [0])),
        ([9, 9, LOCAL_DOC_ID_BASE + 2, LOCAL_DOC_ID_BASE + 2], ([9], [2])),
    ],
)
def test_document_ids_are_split_at_the_boundary(
    doc_ids: list[int], expected: tuple[list[int], list[int]]
) -> None:
    assert split_doc_ids(doc_ids) == expected


def test_local_ids_never_reach_the_usermeta_query() -> None:
    """`track_meta.track_id` ist `integer` — eine lokale ID waere ein Fehler.

    `usermeta` deckt lokale Einreichungen bewusst noch nicht ab; die IDs
    duerfen deshalb gar nicht erst in die Abfrage geraten.
    """
    connection = StubConnection(rows=[(1, 42)])
    result = lookup_meta_ids(connection, [1, LOCAL_DOC_ID_BASE + 5])  # type: ignore[arg-type]
    assert result == {1: [42]}
    assert len(connection.queries) == 1
    assert "track_meta" in connection.queries[0]


def test_a_request_with_only_local_ids_asks_nothing_at_all() -> None:
    connection = StubConnection(rows=[(1, 42)])
    assert lookup_meta_ids(connection, [LOCAL_DOC_ID_BASE + 5]) == {}  # type: ignore[arg-type]
    assert connection.queries == []
