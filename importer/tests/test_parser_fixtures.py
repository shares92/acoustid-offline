"""Parser gegen die echten Tagesdateien (ARCHITECTURE §5.1).

Gearbeitet wird mit den zehn Original-Fixtures unter
`tests/fixtures/acoustid-dumps/` — sie liegen nicht im Repo (Lizenz,
Groesse), `tests/fixtures/fetch_fixtures.py` holt sie reproduzierbar nach.
Fehlen sie, werden die Tests mit Begruendung uebersprungen.

Erwartungswerte sind die Zeilenzahlen aus der Fixture-README; die Dateien
sind upstream unveraenderlich, die Zahlen also stabil. Abgedeckt sind alle
sieben Stroeme, beide Escaping-Epochen, die leeren Dateien und die
Randfaelle des 23.07.2026.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

import pytest

from acoustid_importer.parser import DeltaReader, Escaping
from acoustid_importer.records import (
    FingerprintRecord,
    MetaRecord,
    TrackFingerprintRecord,
    TrackMbidRecord,
    TrackMetaRecord,
    TrackPuidRecord,
    TrackRecord,
)
from acoustid_importer.streams import EMPTY_GZ_SIZE, Stream

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO_ROOT / "tests/fixtures/acoustid-dumps"
PARSER_LOGGER = "acoustid_importer.parser"

#: Datei -> (Strom, erwartete Zeilenzahl). Quelle: Fixture-README.
EXPECTED: dict[str, tuple[Stream, int]] = {
    "2011-08-19-meta-update.jsonl.gz": (Stream.META, 11_677),
    "2026-07-22-track-update.jsonl.gz": (Stream.TRACK, 1_693),
    "2026-07-22-meta-update.jsonl.gz": (Stream.META, 5_122),
    "2026-07-22-fingerprint-update.jsonl.gz": (Stream.FINGERPRINT, 2_214),
    "2026-07-22-track_fingerprint-update.jsonl.gz": (Stream.TRACK_FINGERPRINT, 2_214),
    "2026-07-22-track_mbid-update.jsonl.gz": (Stream.TRACK_MBID, 1_039),
    "2026-07-22-track_meta-update.jsonl.gz": (Stream.TRACK_META, 7_141),
    "2026-07-22-track_puid-update.jsonl.gz": (Stream.TRACK_PUID, 0),
    "2026-07-23-fingerprint-update.jsonl.gz": (Stream.FINGERPRINT, 0),
    "2026-07-23-track_mbid-update.jsonl.gz": (Stream.TRACK_MBID, 4),
}

RECORD_TYPES = {
    Stream.TRACK: TrackRecord,
    Stream.META: MetaRecord,
    Stream.FINGERPRINT: FingerprintRecord,
    Stream.TRACK_FINGERPRINT: TrackFingerprintRecord,
    Stream.TRACK_MBID: TrackMbidRecord,
    Stream.TRACK_META: TrackMetaRecord,
    Stream.TRACK_PUID: TrackPuidRecord,
}

INT32_MIN, INT32_MAX = -(2**31), 2**31 - 1


def fixture(name: str) -> Path:
    """Pfad einer Fixture oder Skip mit Beschaffungshinweis."""
    path = FIXTURE_DIR / name
    if not path.exists():
        pytest.skip(
            f"Fixture fehlt: {path.relative_to(REPO_ROOT)} — "
            "'uv run python tests/fixtures/fetch_fixtures.py' holt sie nach"
        )
    return path


def read(name: str, **kwargs: object) -> tuple[list[object], DeltaReader]:
    reader = DeltaReader(fixture(name), **kwargs)  # type: ignore[arg-type]
    return list(reader), reader


# --- Alle Fixtures ---------------------------------------------------------


@pytest.mark.parametrize(("name", "expectation"), sorted(EXPECTED.items()))
def test_every_fixture_parses_completely(name: str, expectation: tuple[Stream, int]) -> None:
    stream, lines = expectation
    records, reader = read(name)
    assert reader.stream is stream
    assert len(records) == lines
    assert reader.stats.records == lines
    assert reader.stats.lines == lines
    assert reader.stats.blank_lines == 0
    assert all(type(record) is RECORD_TYPES[stream] for record in records)


@pytest.mark.parametrize(("name", "expectation"), sorted(EXPECTED.items()))
def test_no_fixture_contains_an_unknown_field(
    name: str, expectation: tuple[Stream, int], caplog: pytest.LogCaptureFixture
) -> None:
    """§12.8: der Feldsatz aus §5.1 deckt die echten Dateien vollstaendig ab."""
    with caplog.at_level(logging.WARNING, logger=PARSER_LOGGER):
        _, reader = read(name)
    assert reader.stats.unknown_fields == {}
    assert [record.getMessage() for record in caplog.records if hasattr(record, "field")] == []


@pytest.mark.parametrize(("name", "expectation"), sorted(EXPECTED.items()))
def test_every_row_has_a_positive_id(name: str, expectation: tuple[Stream, int]) -> None:
    records, _ = read(name)
    assert all(record.id > 0 for record in records)  # type: ignore[attr-defined]


def test_all_fixtures_of_the_fetch_script_are_covered() -> None:
    """Wer eine Fixture ergaenzt, ergaenzt hier die Erwartung mit."""
    spec = importlib.util.spec_from_file_location(
        "fetch_fixtures", REPO_ROOT / "tests/fixtures/fetch_fixtures.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert sorted(module.FIXTURES) == sorted(EXPECTED)


# --- Leere Dateien ---------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["2026-07-22-track_puid-update.jsonl.gz", "2026-07-23-fingerprint-update.jsonl.gz"],
)
def test_the_empty_files_are_23_bytes_and_yield_nothing(name: str) -> None:
    path = fixture(name)
    assert path.stat().st_size == EMPTY_GZ_SIZE
    records, reader = read(name)
    assert records == []
    assert reader.stats.records == 0


# --- Strom-spezifische Zusagen ---------------------------------------------


def test_track_rows_carry_a_uuid_and_no_merge_in_this_day() -> None:
    records, _ = read("2026-07-22-track-update.jsonl.gz")
    first = records[0]
    assert first.id == 84763366
    assert first.gid == "f254f9f0-4cc3-4bde-adc5-f1eedb6542a4"
    assert first.created.startswith("2026-07-22T")
    # An diesem Tag gibt es weder Merges noch Aenderungen — beide Felder sind
    # laut §5.1 optional, und genau so kommen sie hier an.
    assert {record.new_id for record in records} == {None}
    assert {record.updated for record in records} == {None}


def test_meta_rows_keep_cjk_titles_and_leave_absent_fields_none() -> None:
    records, _ = read("2026-07-22-meta-update.jsonl.gz")
    first = records[0]
    assert first.track == "카발레리아 루스티카나, 간주곡"
    assert first.album == "죽기전에 꼭 들어야 할 클래식 명곡 100"
    assert first.track_no == 1
    assert first.disc_no is None and first.year is None
    # 765 Zeilen mit CJK-Zeichen (Fixture-README) und 5 Zeilen ohne `track`.
    assert sum(record.track is None for record in records) == 5
    assert all(record.created is not None for record in records)


def test_meta_rows_with_quotes_parse_cleanly() -> None:
    """§5.1: 52 Werte mit Anfuehrungszeichen — valides JSONL in der neuen Epoche."""
    records, _ = read("2026-07-22-meta-update.jsonl.gz")
    with_quotes = [
        record
        for record in records
        if any(
            isinstance(value, str) and '"' in value
            for value in (record.track, record.artist, record.album, record.album_artist)
        )
    ]
    assert len(with_quotes) == 52


def test_fingerprint_vectors_are_signed_int32_and_have_a_length() -> None:
    records, _ = read("2026-07-22-fingerprint-update.jsonl.gz")
    assert all(record.fingerprint for record in records), "kein leerer Vektor in echten Daten"
    values = [value for record in records for value in record.fingerprint]
    assert INT32_MIN <= min(values) <= max(values) <= INT32_MAX
    assert min(record.length for record in records) == 9
    assert max(record.length for record in records) == 1585


def test_track_fingerprint_is_the_second_projection_of_the_same_table() -> None:
    """§5.1: ``id == fingerprint_id``, und der Tag deckt dieselben IDs ab."""
    projection, _ = read("2026-07-22-track_fingerprint-update.jsonl.gz")
    vectors, _ = read("2026-07-22-fingerprint-update.jsonl.gz")
    assert all(record.id == record.fingerprint_id for record in projection)
    assert {record.id for record in projection} == {record.id for record in vectors}
    assert all(record.submission_count >= 1 for record in projection)


def test_track_mbid_disabled_only_appears_when_true() -> None:
    records, _ = read("2026-07-22-track_mbid-update.jsonl.gz")
    disabled = [record for record in records if record.disabled]
    assert len(disabled) == 9
    assert all(record.submission_count == 0 for record in disabled)
    assert sum(record.updated is not None for record in records) == 18
    assert all(record.disabled is False for record in records if record not in disabled)


def test_the_minimal_delta_covers_the_disabled_and_updated_edge_cases() -> None:
    records, _ = read("2026-07-23-track_mbid-update.jsonl.gz")
    assert [record.disabled for record in records] == [False, False, True, True]
    assert all(record.updated is not None for record in records), "alle vier sind Aenderungen"
    # Die beiden aktiven Zeilen tragen den Schluessel `disabled` nicht — der
    # Parser setzt False, statt ein altes True stehen zu lassen.
    assert [record.submission_count for record in records] == [2, 2, 0, 0]
    assert records[0].created.startswith("2023-11-13T")
    assert records[0].updated.startswith("2026-07-23T")


def test_track_meta_rows_are_plain_assignments() -> None:
    records, _ = read("2026-07-22-track_meta-update.jsonl.gz")
    assert all(record.track_id > 0 and record.meta_id > 0 for record in records)
    assert {record.updated for record in records} == {None}


# --- Alt-Epoche (COPY-Text-Escaping bis 2024-12-04) ------------------------

OLD_ERA = "2011-08-19-meta-update.jsonl.gz"

#: Zeilen dieser Datei, die ohne Unescape kein gueltiges JSON sind
#: (empirisch, Phase 6).
OLD_ERA_BROKEN_LINES = 85


def test_the_old_era_file_needs_the_copy_text_lesart() -> None:
    """Ohne Epochen-Behandlung waere der Bootstrap an Tag 1 gescheitert."""
    records, reader = read(OLD_ERA)
    assert len(records) == 11_677
    assert reader.stats.escaping_fallbacks == 0, "der Tag liegt vor dem Schnitt"
    quoted = [record for record in records if record.track and '"' in record.track]
    assert quoted, "genau diese Werte brechen das JSON ohne Unescape"
    assert all("\\" not in record.track for record in quoted)


def test_strict_mode_shows_how_many_lines_are_copy_escaped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger=PARSER_LOGGER):
        records, reader = read(OLD_ERA, escaping=Escaping.NONE)
    assert len(records) == 11_677, "der zeilenweise Rueckfall rettet jede Zeile"
    assert reader.stats.escaping_fallbacks == OLD_ERA_BROKEN_LINES
    assert sum("Escaping" in record.getMessage() for record in caplog.records) == 1


def test_the_old_era_still_uses_the_documented_field_set() -> None:
    records, reader = read(OLD_ERA)
    assert reader.stats.unknown_fields == {}
    assert any(record.year is not None for record in records)
    assert any(record.disc_no is not None for record in records)
    assert all(record.created is not None for record in records)
