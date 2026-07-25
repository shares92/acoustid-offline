"""Stroeme, Dateinamen und Feldvertrag (ARCHITECTURE §5.1, §5.2 Regel 1).

Der letzte Test dieser Datei ist ein Doku-Waechter: er liest die
Strom-Tabelle aus ARCHITECTURE.md §5.1 und vergleicht sie Feld fuer Feld
mit :data:`acoustid_importer.records.SPECS`. Damit kann der Feldvertrag
nicht unbemerkt von der Spezifikation abdriften — dieselbe Idee wie der
DDL-Test aus Phase 4.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest

from acoustid_importer.records import SPECS, spec_for
from acoustid_importer.streams import (
    BASE_URL,
    EMPTY_GZ_SIZE,
    FIRST_DAY,
    IMPORT_ORDER,
    DeltaFile,
    Stream,
    days_between,
    month_index_url,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
ARCHITECTURE = REPO_ROOT / "ARCHITECTURE.md"


def test_import_order_is_the_order_from_rule_1() -> None:
    assert [stream.value for stream in IMPORT_ORDER] == [
        "track",
        "meta",
        "fingerprint",
        "track_fingerprint",
        "track_mbid",
        "track_meta",
        "track_puid",
    ]
    assert [stream.order for stream in IMPORT_ORDER] == list(range(7))


def test_target_tables_follow_the_schema() -> None:
    """Beide Fingerprint-Stroeme schreiben in dieselbe Tabelle (Regel 2)."""
    assert Stream.FINGERPRINT.table == "fingerprint"
    assert Stream.TRACK_FINGERPRINT.table == "fingerprint"
    assert Stream.TRACK_MBID.table == "track_mbid"


def test_first_day_and_empty_size_are_the_documented_constants() -> None:
    assert date(2011, 8, 19) == FIRST_DAY
    assert EMPTY_GZ_SIZE == 23


@pytest.mark.parametrize("stream", list(Stream))
def test_file_name_and_url_follow_the_path_scheme(stream: Stream) -> None:
    delta = DeltaFile(date(2026, 7, 22), stream)
    assert delta.name == f"2026-07-22-{stream.value}-update.jsonl.gz"
    assert delta.url() == f"{BASE_URL}/2026/2026-07/{delta.name}"
    assert delta.url("http://127.0.0.1:8000/") == f"http://127.0.0.1:8000/2026/2026-07/{delta.name}"


def test_month_index_url() -> None:
    assert month_index_url(date(2011, 8, 19)) == f"{BASE_URL}/2011/2011-08/index.json"


def test_file_names_round_trip() -> None:
    for stream in Stream:
        delta = DeltaFile(date(2011, 8, 19), stream)
        assert DeltaFile.from_name(delta.name) == delta


@pytest.mark.parametrize(
    "name",
    [
        "2026-07-22-meta-update.jsonl",
        "2026-07-22-meta.jsonl.gz",
        "meta-update.jsonl.gz",
        "2026-7-22-meta-update.jsonl.gz",
        "",
    ],
)
def test_from_name_rejects_foreign_names(name: str) -> None:
    with pytest.raises(ValueError, match="Kein Delta-Dateiname"):
        DeltaFile.from_name(name)


def test_from_name_rejects_unknown_streams() -> None:
    with pytest.raises(ValueError, match="Unbekannter Strom"):
        DeltaFile.from_name("2026-07-22-track_isrc-update.jsonl.gz")


def test_sort_key_orders_days_first_then_rule_1() -> None:
    files = [
        DeltaFile(date(2026, 7, 23), Stream.TRACK),
        DeltaFile(date(2026, 7, 22), Stream.TRACK_PUID),
        DeltaFile(date(2026, 7, 22), Stream.TRACK),
    ]
    assert [item.sort_key for item in sorted(files, key=lambda f: f.sort_key)] == [
        (date(2026, 7, 22), 0),
        (date(2026, 7, 22), 6),
        (date(2026, 7, 23), 0),
    ]


def test_days_between_is_inclusive_and_empty_when_reversed() -> None:
    assert days_between(date(2026, 7, 22), date(2026, 7, 24)) == [
        date(2026, 7, 22),
        date(2026, 7, 23),
        date(2026, 7, 24),
    ]
    assert days_between(date(2026, 7, 22), date(2026, 7, 22)) == [date(2026, 7, 22)]
    assert days_between(date(2026, 7, 22), date(2026, 7, 21)) == []


# --- Doku-Waechter: §5.1-Tabelle gegen die Specs ---------------------------

#: Eine Zeile der Strom-Tabelle: | `<strom>-update` | `<tabelle>` | Felder | Anteil |
_TABLE_ROW = re.compile(
    r"^\|\s*`(?P<stream>\w+)-update`\s*\|\s*`(?P<table>\w+)`\s*\|(?P<rest>.*)\|"
)


def _fields_from_architecture() -> dict[str, list[tuple[str, bool]]]:
    """Liest je Strom die Feldliste aus ARCHITECTURE §5.1.

    Optionale Felder stehen dort in Klammern — auch gruppiert
    (``(`track`, `artist`, …)``). Genau diese Klammertiefe wird hier
    ausgewertet.
    """
    table: dict[str, list[tuple[str, bool]]] = {}
    for line in ARCHITECTURE.read_text(encoding="utf-8").splitlines():
        match = _TABLE_ROW.match(line)
        if match is None:
            continue
        cell = match["rest"].rsplit("|", 1)[0]
        fields: list[tuple[str, bool]] = []
        depth = 0
        for token in re.finditer(r"[()]|`(\w+)`", cell):
            if token.group() == "(":
                depth += 1
            elif token.group() == ")":
                depth = max(depth - 1, 0)
            else:
                fields.append((token.group(1), depth > 0))
        table[match["stream"]] = fields
    return table


def test_the_architecture_table_lists_all_seven_streams() -> None:
    assert sorted(_fields_from_architecture()) == sorted(stream.value for stream in Stream)


@pytest.mark.parametrize("stream", list(Stream))
def test_specs_match_the_architecture_table(stream: Stream) -> None:
    documented = _fields_from_architecture()[stream.value]
    spec = spec_for(stream)
    assert [name for name, _ in documented] == list(spec.names), (
        "Feldnamen oder ihre Reihenfolge weichen von ARCHITECTURE §5.1 ab"
    )
    assert [name for name, optional in documented if optional] == list(spec.optional_names), (
        "Pflicht/optional weicht von ARCHITECTURE §5.1 ab (Klammern in der Tabelle)"
    )


def test_every_stream_has_exactly_one_spec() -> None:
    assert list(SPECS) == list(IMPORT_ORDER)
