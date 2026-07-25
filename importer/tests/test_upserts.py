"""Upsert-Bausteine ohne Datenbank (ARCHITECTURE §5.2, Import-Regeln 2 und 3).

Alles hier ist pure Logik: aus einem Record wird eine Parameterliste, aus
einem Strom ein Statement. Geprueft wird gegen die beiden Quellen, die es
gibt — die Import-Regeln aus §5.2 und das tatsaechliche DDL der Migrationen
(``shared/shared/db/sql/core/*.sql``). Eine Spalte, die im Upsert steht, aber
nicht in der Tabelle, faellt so schon ohne Postgres auf.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest

from acoustid_importer.errors import DbImportError
from acoustid_importer.records import (
    FingerprintRecord,
    MetaRecord,
    TrackFingerprintRecord,
    TrackMbidRecord,
    TrackMetaRecord,
    TrackPuidRecord,
    TrackRecord,
    spec_for,
)
from acoustid_importer.streams import IMPORT_ORDER, Stream
from acoustid_importer.upserts import (
    BOOKKEEPING_COLUMNS,
    UPSERTS,
    upsert_for,
)

DAY = date(2026, 7, 22)

REPO_ROOT = Path(__file__).resolve().parents[2]
CORE_SQL_DIR = REPO_ROOT / "shared/shared/db/sql/core"


def ddl_columns(table: str) -> set[str]:
    """Spaltennamen einer Tabelle aus der Migrations-SQL (ohne Postgres)."""
    wanted = f"CREATE TABLE {table} "
    matches = [path for path in CORE_SQL_DIR.glob("*.sql") if wanted in _text(path)]
    assert len(matches) == 1, f"genau eine Migration muss {table} anlegen, gefunden: {matches}"
    body = _text(matches[0]).split(wanted, 1)[1]
    body = body[body.index("(") + 1 : body.index(");")]
    names: set[str] = set()
    for line in body.splitlines():
        stripped = re.sub(r"--.*$", "", line).strip()
        if not stripped:
            continue
        names.add(stripped.split()[0])
    return names


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --- Vollstaendigkeit und Form ---------------------------------------------


def test_every_stream_has_an_upsert() -> None:
    assert set(UPSERTS) == set(IMPORT_ORDER)


@pytest.mark.parametrize("stream", IMPORT_ORDER, ids=lambda s: s.value)
def test_the_statement_targets_the_documented_table(stream: Stream) -> None:
    upsert = upsert_for(stream)
    assert upsert.table == stream.table
    assert upsert.statement.startswith(f"INSERT INTO {stream.table} (")
    assert "ON CONFLICT (id) DO UPDATE" in upsert.statement


@pytest.mark.parametrize("stream", IMPORT_ORDER, ids=lambda s: s.value)
def test_placeholders_match_the_column_count(stream: Stream) -> None:
    upsert = upsert_for(stream)
    assert upsert.statement.count("%s") == len(upsert.columns)


@pytest.mark.parametrize("stream", IMPORT_ORDER, ids=lambda s: s.value)
def test_bookkeeping_columns_come_last_and_are_written_on_conflict(stream: Stream) -> None:
    """`src_day` und `imported_at` sind [P] — sie haengen an jedem Strom."""
    upsert = upsert_for(stream)
    assert upsert.columns[-len(BOOKKEEPING_COLUMNS) :] == BOOKKEEPING_COLUMNS
    assert "src_day = EXCLUDED.src_day" in upsert.statement
    assert "imported_at = now()" in upsert.statement


@pytest.mark.parametrize("stream", IMPORT_ORDER, ids=lambda s: s.value)
def test_the_conflict_key_is_never_overwritten(stream: Stream) -> None:
    assert "id" not in upsert_for(stream).update_columns


# --- Regel 3: alle Felder des Stroms, keine Teil-Patches -------------------


@pytest.mark.parametrize("stream", IMPORT_ORDER, ids=lambda s: s.value)
def test_all_fields_of_the_stream_reach_a_column(stream: Stream) -> None:
    """Jedes Feld aus §5.1 wird geschrieben — ausser `fingerprint_id`.

    `track_fingerprint.fingerprint_id` ist laut §5.1 identisch mit `id` und
    hat in der Tabelle keine eigene Spalte; alles andere muss ankommen,
    sonst waere der Upsert ein Teil-Patch (Import-Regel 3).
    """
    upsert = upsert_for(stream)
    expected = set(spec_for(stream).names)
    if stream is Stream.TRACK_FINGERPRINT:
        expected -= {"fingerprint_id"}
    assert set(upsert.data_columns) == expected


@pytest.mark.parametrize("stream", IMPORT_ORDER, ids=lambda s: s.value)
def test_every_data_column_except_the_key_is_updated_on_conflict(stream: Stream) -> None:
    """Eine geschriebene Spalte, die ein Konflikt nicht anfasst, waere ein Leck."""
    upsert = upsert_for(stream)
    assert set(upsert.update_columns) == (set(upsert.columns) - {"id"}) | {"imported_at"}


def test_track_mbid_resets_disabled_explicitly() -> None:
    """Die Reaktivierungs-Falle aus §5.1: `disabled` muss mitgeschrieben werden."""
    upsert = upsert_for(Stream.TRACK_MBID)
    assert "disabled" in upsert.data_columns
    assert "disabled = EXCLUDED.disabled" in upsert.statement


# --- Regel 2: zwei disjunkte Upserts auf `fingerprint` ---------------------


def test_both_fingerprint_streams_write_the_same_table() -> None:
    assert upsert_for(Stream.FINGERPRINT).table == "fingerprint"
    assert upsert_for(Stream.TRACK_FINGERPRINT).table == "fingerprint"


def test_the_two_fingerprint_upserts_do_not_touch_each_others_columns() -> None:
    vector = upsert_for(Stream.FINGERPRINT)
    mapping = upsert_for(Stream.TRACK_FINGERPRINT)
    assert vector.data_update_columns == {"fingerprint", "length"}
    assert mapping.data_update_columns == {"track_id", "submission_count", "updated"}
    assert not vector.data_update_columns & mapping.data_update_columns


@pytest.mark.parametrize("stream", [Stream.FINGERPRINT, Stream.TRACK_FINGERPRINT])
def test_created_is_only_filled_never_overwritten(stream: Stream) -> None:
    assert (
        "created = COALESCE(fingerprint.created, EXCLUDED.created)" in upsert_for(stream).statement
    )


# --- Record -> Parameterliste ----------------------------------------------


def test_a_track_record_becomes_its_parameter_row() -> None:
    upsert = upsert_for(Stream.TRACK)
    record = TrackRecord(
        id=7,
        gid="1c4d2b2b-0000-4000-8000-000000000001",
        new_id=None,
        created="2026-07-22T00:00:18.312881+00:00",
        updated=None,
    )
    assert upsert.columns == ("id", "gid", "new_id", "created", "updated", "src_day")
    assert upsert.row(record, DAY) == (
        7,
        "1c4d2b2b-0000-4000-8000-000000000001",
        None,
        "2026-07-22T00:00:18.312881+00:00",
        None,
        DAY,
    )


def test_absent_fields_arrive_as_null_not_as_omission() -> None:
    """Import-Regel 3: der Parser hat schon `None` gesetzt, wir schreiben es."""
    record = MetaRecord(
        id=3,
        track="Titel",
        artist=None,
        album=None,
        album_artist=None,
        track_no=None,
        disc_no=None,
        year=None,
        created="2026-07-22T00:00:00+00:00",
    )
    row = upsert_for(Stream.META).row(record, DAY)
    assert row == (3, "Titel", None, None, None, None, None, None, "2026-07-22T00:00:00+00:00", DAY)


def test_a_disabled_flag_of_false_is_written_out() -> None:
    record = TrackMbidRecord(
        id=1,
        track_id=2,
        mbid="1c4d2b2b-0000-4000-8000-000000000002",
        submission_count=1,
        disabled=False,
        created="2026-07-22T00:00:00+00:00",
        updated=None,
    )
    assert upsert_for(Stream.TRACK_MBID).row(record, DAY)[4] is False


def test_the_fingerprint_vector_is_passed_through_unchanged() -> None:
    """Der Vollvektor geht roh (signed int32) in die Postgres — §5.1."""
    record = FingerprintRecord(
        id=99, fingerprint=[1, -1900322695, 3], length=210, created="2026-07-22T00:00:00+00:00"
    )
    row = upsert_for(Stream.FINGERPRINT).row(record, DAY)
    assert row == (99, [1, -1900322695, 3], 210, "2026-07-22T00:00:00+00:00", DAY)


def test_track_fingerprint_drops_the_redundant_fingerprint_id() -> None:
    record = TrackFingerprintRecord(
        id=99,
        track_id=5,
        fingerprint_id=99,
        submission_count=2,
        created="2026-07-22T00:00:00+00:00",
        updated="2026-07-23T00:00:00+00:00",
    )
    upsert = upsert_for(Stream.TRACK_FINGERPRINT)
    assert "fingerprint_id" not in upsert.columns
    assert upsert.row(record, DAY) == (
        99,
        5,
        2,
        "2026-07-22T00:00:00+00:00",
        "2026-07-23T00:00:00+00:00",
        DAY,
    )


def test_a_diverging_fingerprint_id_is_a_hard_error() -> None:
    """Waere sie je verschieden, haenge die Zuordnung an der falschen Zeile."""
    record = TrackFingerprintRecord(
        id=99,
        track_id=5,
        fingerprint_id=100,
        submission_count=2,
        created="2026-07-22T00:00:00+00:00",
        updated=None,
    )
    with pytest.raises(DbImportError, match="fingerprint_id"):
        upsert_for(Stream.TRACK_FINGERPRINT).row(record, DAY)


@pytest.mark.parametrize(
    ("stream", "record"),
    [
        (
            Stream.TRACK_META,
            TrackMetaRecord(
                id=1,
                track_id=2,
                meta_id=3,
                submission_count=4,
                created="2026-07-22T00:00:00+00:00",
                updated=None,
            ),
        ),
        (
            Stream.TRACK_PUID,
            TrackPuidRecord(
                id=1,
                track_id=2,
                puid="1c4d2b2b-0000-4000-8000-000000000003",
                submission_count=4,
                created="2026-07-22T00:00:00+00:00",
                updated=None,
            ),
        ),
    ],
    ids=lambda value: getattr(value, "value", ""),
)
def test_the_row_has_one_value_per_column(stream: Stream, record: object) -> None:
    upsert = upsert_for(stream)
    assert len(upsert.row(record, DAY)) == len(upsert.columns)


# --- Abgleich mit dem echten Schema ----------------------------------------


@pytest.mark.parametrize("stream", IMPORT_ORDER, ids=lambda s: s.value)
def test_every_written_column_exists_in_the_migration_ddl(stream: Stream) -> None:
    upsert = upsert_for(stream)
    columns = ddl_columns(upsert.table)
    assert set(upsert.columns) <= columns
    assert set(upsert.update_columns) <= columns
