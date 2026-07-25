"""Parser-Verhalten an synthetischen Zeilen (§5.2 Regel 3/4, §12.8).

Hier steht das Regelwerk: Absent-Semantik, Feld-Sanity-Check, harte
Fehler mit Position, Escaping-Epochen. Die echten Tagesdateien pruefen
`test_parser_fixtures.py`.

Die Warnungen werden bewusst mit durchlaessigem Log-Level geprueft
(`caplog.at_level`): nur dann baut das stdlib-`logging` den LogRecord
wirklich und ein reservierter Feldname in `extra` faellt auf (LEARNINGS
„reservierte LogRecord-Feldnamen").
"""

from __future__ import annotations

import gzip
import json
import logging
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from acoustid_importer.errors import ParseError
from acoustid_importer.parser import (
    COPY_TEXT_LAST_DAY,
    DeltaReader,
    Escaping,
    escaping_for_day,
    unescape_copy_text,
)
from acoustid_importer.records import (
    FingerprintRecord,
    MetaRecord,
    TrackMbidRecord,
    TrackRecord,
)
from acoustid_importer.streams import Stream

PARSER_LOGGER = "acoustid_importer.parser"

DAY = date(2026, 7, 22)
OLD_DAY = date(2011, 8, 19)
SOURCE = "2026-07-22-track_mbid-update.jsonl.gz"


def line(**fields: Any) -> bytes:
    """Eine JSONL-Zeile aus Feldern (wie der Export sie schreiben wuerde)."""
    return json.dumps(fields, ensure_ascii=False).encode("utf-8") + b"\n"


def reader(
    lines: list[bytes],
    stream: Stream = Stream.TRACK_MBID,
    *,
    day: date | None = DAY,
    source: str = SOURCE,
    escaping: Escaping = Escaping.AUTO,
) -> DeltaReader:
    return DeltaReader.from_lines(lines, stream, source=source, day=day, escaping=escaping)


MBID_LINE = {
    "id": 31302229,
    "track_id": 84763366,
    "mbid": "e42cb621-4ac5-4ae7-8a63-77f662374beb",
    "submission_count": 1,
    "created": "2026-07-22T00:00:00.214169+00:00",
}


# --- Absent-Semantik (Import-Regel 3) --------------------------------------


def test_missing_keys_become_none() -> None:
    records = list(
        reader(
            [
                line(
                    id=84763366,
                    gid="f254f9f0-4cc3-4bde-adc5-f1eedb6542a4",
                    created="2026-07-22T00:00:00+00:00",
                )
            ],
            Stream.TRACK,
            source="2026-07-22-track-update.jsonl.gz",
        )
    )
    assert records == [
        TrackRecord(
            id=84763366,
            gid="f254f9f0-4cc3-4bde-adc5-f1eedb6542a4",
            new_id=None,
            created="2026-07-22T00:00:00+00:00",
            updated=None,
        )
    ]


def test_missing_disabled_becomes_false_not_unchanged() -> None:
    """Die Reaktivierungs-Falle: fehlender Schluessel heisst ausdruecklich False."""
    records = list(reader([line(**MBID_LINE)]))
    assert records[0].disabled is False


def test_disabled_true_is_kept() -> None:
    records = list(reader([line(**MBID_LINE, disabled=True)]))
    assert records[0] == TrackMbidRecord(
        id=31302229,
        track_id=84763366,
        mbid="e42cb621-4ac5-4ae7-8a63-77f662374beb",
        submission_count=1,
        disabled=True,
        created="2026-07-22T00:00:00.214169+00:00",
        updated=None,
    )


def test_meta_row_with_only_an_id_is_all_none() -> None:
    records = list(
        reader([line(id=401636052)], Stream.META, source="2026-07-22-meta-update.jsonl.gz")
    )
    assert records == [
        MetaRecord(
            id=401636052,
            track=None,
            artist=None,
            album=None,
            album_artist=None,
            track_no=None,
            disc_no=None,
            year=None,
            created=None,
        )
    ]


def test_explicit_null_is_treated_like_an_absent_key() -> None:
    """`json_strip_nulls` liefert keine Nulls — falls doch, gilt dasselbe."""
    records = list(reader([line(**MBID_LINE, updated=None)]))
    assert records[0].updated is None


# --- Feld-Sanity-Check (§12.8) ---------------------------------------------


def test_unknown_field_warns_once_per_file_and_field(caplog: pytest.LogCaptureFixture) -> None:
    lines = [line(**MBID_LINE, merged_into=7) for _ in range(5)]
    with caplog.at_level(logging.WARNING, logger=PARSER_LOGGER):
        parser = reader(lines)
        records = list(parser)

    assert len(records) == 5, "unbekannte Felder duerfen den Import nicht stoppen"
    warnings = [rec for rec in caplog.records if rec.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].field == "merged_into"
    assert warnings[0].delta_file == SOURCE
    assert warnings[0].line == 1
    assert parser.stats.unknown_fields == {"merged_into": 5}


def test_two_unknown_fields_warn_once_each(caplog: pytest.LogCaptureFixture) -> None:
    lines = [
        line(**MBID_LINE, merged_into=7),
        line(**MBID_LINE, source_id=3),
        line(**MBID_LINE, merged_into=8, source_id=4),
    ]
    with caplog.at_level(logging.WARNING, logger=PARSER_LOGGER):
        parser = reader(lines)
        list(parser)
    assert sorted(rec.field for rec in caplog.records) == ["merged_into", "source_id"]
    assert parser.stats.unknown_fields == {"merged_into": 2, "source_id": 2}


def test_known_rows_cost_no_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=PARSER_LOGGER):
        parser = reader([line(**MBID_LINE)])
        list(parser)
    assert caplog.records == []
    assert parser.stats.unknown_fields == {}


# --- Harte Fehler mit Position (Import-Regel 4) -----------------------------


def test_missing_required_field_is_a_hard_error_with_position() -> None:
    lines = [
        line(**MBID_LINE),
        line(id=1, track_id=2, submission_count=1, created="2026-07-22T00:00:00+00:00"),
    ]
    with pytest.raises(ParseError) as caught:
        list(reader(lines))
    error = caught.value
    assert error.line_no == 2
    assert error.source == SOURCE
    assert "'mbid'" in str(error)
    assert str(error).startswith(f"{SOURCE}:2:")


def test_broken_json_is_a_hard_error_with_position_and_excerpt() -> None:
    lines = [line(**MBID_LINE), b'{"id": 1, "track_id":\n']
    with pytest.raises(ParseError) as caught:
        list(reader(lines))
    assert caught.value.line_no == 2
    assert "kein gueltiges JSON" in str(caught.value)
    assert '{"id": 1, "track_id":' in str(caught.value)


def test_a_json_array_is_not_a_row() -> None:
    with pytest.raises(ParseError, match="JSON-Objekt erwartet, list bekommen"):
        list(reader([b"[1, 2, 3]\n"]))


@pytest.mark.parametrize(
    ("field_name", "value", "expected"),
    [
        ("id", "31302229", "int erwartet, str bekommen"),
        ("id", True, "int erwartet, bool bekommen"),
        ("id", 3.5, "int erwartet, float bekommen"),
        ("submission_count", None, "int erwartet, null bekommen"),
        ("mbid", "nicht-uuid", "uuid erwartet, str bekommen"),
        ("created", 1_755_000_000, "timestamp erwartet, int bekommen"),
        ("created", "gestern", "timestamp erwartet, str bekommen"),
        ("disabled", "true", "bool erwartet, str bekommen"),
    ],
)
def test_wrong_types_are_hard_errors(field_name: str, value: Any, expected: str) -> None:
    row = dict(MBID_LINE)
    row[field_name] = value
    with pytest.raises(ParseError) as caught:
        list(reader([line(**row)]))
    assert expected in str(caught.value)
    assert caught.value.line_no == 1


def test_fingerprint_vector_must_contain_only_ints() -> None:
    created = "2011-08-19T07:26:44+00:00"
    good = line(id=1, fingerprint=[-1252264585, 42], length=137, created=created)
    bad = line(id=2, fingerprint=[1, 2.5], length=137, created=created)
    parser = reader(
        [good, bad], Stream.FINGERPRINT, source="2026-07-22-fingerprint-update.jsonl.gz"
    )
    with pytest.raises(ParseError, match=r"int32\[\] erwartet, list bekommen"):
        list(parser)


def test_an_empty_vector_is_accepted() -> None:
    records = list(
        reader(
            [line(id=1, fingerprint=[], length=1, created="2011-08-19T07:26:44+00:00")],
            Stream.FINGERPRINT,
            source="2026-07-22-fingerprint-update.jsonl.gz",
        )
    )
    assert records == [
        FingerprintRecord(id=1, fingerprint=[], length=1, created="2011-08-19T07:26:44+00:00")
    ]


def test_timestamps_with_and_without_fraction_and_offset_are_accepted() -> None:
    for created in (
        "2011-08-19T07:26:45.6684+00:00",
        "2026-07-22T00:00:00+00:00",
        "2026-07-22T00:00:00Z",
        "2026-07-22 00:00:00",
        "2026-07-22T00:00:00.123456+0200",
    ):
        row = dict(MBID_LINE) | {"created": created}
        assert next(iter(reader([line(**row)]))).created == created


# --- Leerzeilen, leere und kaputte Dateien ---------------------------------


def test_blank_lines_are_skipped() -> None:
    parser = reader([b"\n", line(**MBID_LINE), b"   \n"])
    assert len(list(parser)) == 1
    assert parser.stats.lines == 3
    assert parser.stats.blank_lines == 2


def test_an_empty_gz_file_yields_no_records(tmp_path: Path) -> None:
    # gzip.compress schreibt 20 Byte, der Go-Exporter upstream 23 — beides
    # ist ein leerer, gueltiger Strom. Die echten 23-Byte-Dateien pruefen
    # die Fixture-Tests.
    path = tmp_path / "2026-07-22-track_puid-update.jsonl.gz"
    path.write_bytes(gzip.compress(b""))
    parser = DeltaReader(path)
    assert list(parser) == []
    assert parser.stats == type(parser.stats)()


def test_a_broken_gz_file_is_a_parse_error(tmp_path: Path) -> None:
    path = tmp_path / "2026-07-22-meta-update.jsonl.gz"
    path.write_bytes(b"\x1f\x8b" + b"kaputt" * 4)
    with pytest.raises(ParseError, match="gzip-Strom nicht lesbar"):
        list(DeltaReader(path))


def test_a_truncated_gz_file_is_a_parse_error(tmp_path: Path) -> None:
    whole = gzip.compress(line(**MBID_LINE) * 200)
    path = tmp_path / "2026-07-22-track_mbid-update.jsonl.gz"
    path.write_bytes(whole[: len(whole) // 2])
    with pytest.raises(ParseError, match="gzip-Strom nicht lesbar"):
        list(DeltaReader(path))


# --- Datei-Identitaet und Wiederverwendung ---------------------------------


def test_stream_and_day_come_from_the_file_name(tmp_path: Path) -> None:
    path = tmp_path / "2011-08-19-meta-update.jsonl.gz"
    path.write_bytes(gzip.compress(line(id=1)))
    parser = DeltaReader(path)
    assert parser.stream is Stream.META
    assert parser.day == OLD_DAY
    assert parser.source == path.name


def test_a_foreign_file_name_needs_an_explicit_stream(tmp_path: Path) -> None:
    path = tmp_path / "irgendwas.jsonl.gz"
    path.write_bytes(gzip.compress(line(id=1)))
    with pytest.raises(ValueError, match="Kein Delta-Dateiname"):
        DeltaReader(path)
    parser = DeltaReader(path, stream=Stream.META, day=DAY)
    assert [record.id for record in parser] == [1]


def test_iterating_twice_re_reads_the_file_and_resets_the_stats(tmp_path: Path) -> None:
    path = tmp_path / "2026-07-22-track_mbid-update.jsonl.gz"
    path.write_bytes(gzip.compress(line(**MBID_LINE) * 3))
    parser = DeltaReader(path)
    assert len(list(parser)) == 3
    assert parser.stats.records == 3
    assert len(list(parser)) == 3
    assert parser.stats.records == 3


def test_repr_names_source_and_stream() -> None:
    assert repr(reader([])) == f"DeltaReader(source={SOURCE!r}, stream='track_mbid')"


# --- Escaping-Epochen ------------------------------------------------------


def test_escaping_for_day_switches_at_the_measured_cut() -> None:
    assert date(2024, 12, 4) == COPY_TEXT_LAST_DAY
    assert escaping_for_day(date(2024, 12, 4)) is Escaping.COPY_TEXT
    assert escaping_for_day(date(2024, 12, 5)) is Escaping.NONE
    assert escaping_for_day(OLD_DAY) is Escaping.COPY_TEXT
    assert escaping_for_day(None) is Escaping.NONE


def test_unescape_copy_text_only_halves_backslashes() -> None:
    assert unescape_copy_text(rb'"Viva (12\\" Remix)"') == rb'"Viva (12\" Remix)"'
    assert unescape_copy_text(rb'"Sanctus\\\\"') == rb'"Sanctus\\"'
    assert unescape_copy_text(rb'"nichts zu tun"') == rb'"nichts zu tun"'


#: Wie der Export bis 2024-12-04 aussah: JSON, ueber das COPY noch einmal
#: sein Text-Escaping gelegt hat (jeder Backslash verdoppelt). Der Titel
#: enthaelt ein Zoll-Zeichen, die Zeile ist deshalb kein gueltiges JSON.
OLD_ERA_LINE = (
    rb'{"id":153238,"track":"Test (12\\" Remix)",'
    rb'"created":"2011-08-19T07:31:59.239425+00:00"}'
)


def test_an_old_era_line_is_read_with_copy_text_escaping() -> None:
    parser = reader(
        [OLD_ERA_LINE], Stream.META, day=OLD_DAY, source="2011-08-19-meta-update.jsonl.gz"
    )
    records = list(parser)
    assert records[0].track == 'Test (12" Remix)'
    assert parser.stats.escaping_fallbacks == 0, "im Alt-Modus ist das der Normalfall"


def test_an_old_era_line_in_strict_mode_falls_back_once(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=PARSER_LOGGER):
        parser = reader(
            [OLD_ERA_LINE, OLD_ERA_LINE],
            Stream.META,
            day=OLD_DAY,
            source="2011-08-19-meta-update.jsonl.gz",
            escaping=Escaping.NONE,
        )
        records = list(parser)
    assert [record.track for record in records] == ['Test (12" Remix)'] * 2
    assert parser.stats.escaping_fallbacks == 2
    assert len([rec for rec in caplog.records if "Escaping" in rec.getMessage()]) == 1


def test_a_modern_line_in_old_mode_falls_back_too() -> None:
    """Andere Richtung: ein echter Backslash im Wert (nur in der neuen Epoche
    moeglich) wuerde vom Unescape zerstoert — der Rueckfall rettet die Zeile."""
    modern = line(id=1, track="C:\\Musik", created="2026-07-22T00:00:00+00:00")
    parser = reader(
        [modern],
        Stream.META,
        day=DAY,
        source="2026-07-22-meta-update.jsonl.gz",
        escaping=Escaping.COPY_TEXT,
    )
    assert next(iter(parser)).track == "C:\\Musik"
    assert parser.stats.escaping_fallbacks == 1


def test_unreadable_in_both_modes_is_a_hard_error() -> None:
    with pytest.raises(ParseError, match="kein gueltiges JSON"):
        list(reader([b'{"id":1,"track":"abgeschnitten' + b"\n"], Stream.META, day=OLD_DAY))


def test_utf8_stays_intact() -> None:
    parser = reader(
        [line(id=1, track="카발레리아 루스티카나, 간주곡", artist="Klaus-Peter Hahn & Co")],
        Stream.META,
        source="2026-07-22-meta-update.jsonl.gz",
    )
    record = next(iter(parser))
    assert record.track == "카발레리아 루스티카나, 간주곡"
