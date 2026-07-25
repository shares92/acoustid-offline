"""Arbeitsliste und Lueckenpruefung (§5.2 Regel 1 und 5).

„Heute" ist in allen Tests ein Parameter — die Logik darf die Uhr nicht
selbst lesen, sonst waeren die Randfaelle (Start der Historie, Grenze
gestern/heute) nicht pruefbar. Der letzte Test haelt das fest.
"""

from __future__ import annotations

import ast
from datetime import date, timedelta
from pathlib import Path

import pytest

from acoustid_importer import worklist
from acoustid_importer.errors import GapError
from acoustid_importer.streams import FIRST_DAY, IMPORT_ORDER, DeltaFile, Stream, days_between
from acoustid_importer.worklist import (
    Gap,
    ImportPlan,
    newest_available_day,
    pending_files,
    plan_from_state,
)

DAY_1 = FIRST_DAY
DAY_2 = FIRST_DAY + timedelta(days=1)
DAY_3 = FIRST_DAY + timedelta(days=2)


def names(files: tuple[DeltaFile, ...]) -> list[str]:
    return [item.name for item in files]


# --- Reihenfolge -----------------------------------------------------------


def test_the_newest_available_day_is_yesterday() -> None:
    """Die Datei eines Tages kann es erst nach Ablauf des Tages geben."""
    assert newest_available_day(date(2026, 7, 25)) == date(2026, 7, 24)


def test_a_fresh_instance_starts_at_the_first_day_of_the_history() -> None:
    files = pending_files({}, today=DAY_2)
    assert names(files) == [
        "2011-08-19-track-update.jsonl.gz",
        "2011-08-19-meta-update.jsonl.gz",
        "2011-08-19-fingerprint-update.jsonl.gz",
        "2011-08-19-track_fingerprint-update.jsonl.gz",
        "2011-08-19-track_mbid-update.jsonl.gz",
        "2011-08-19-track_meta-update.jsonl.gz",
        "2011-08-19-track_puid-update.jsonl.gz",
    ]


def test_days_run_strictly_chronologically_and_streams_by_rule_1() -> None:
    files = pending_files({}, today=DAY_3 + timedelta(days=1))
    assert [item.day for item in files] == [DAY_1] * 7 + [DAY_2] * 7 + [DAY_3] * 7
    for offset in range(0, 21, 7):
        assert [item.stream for item in files[offset : offset + 7]] == list(IMPORT_ORDER)
    assert list(files) == sorted(files, key=lambda item: item.sort_key)


def test_the_full_bootstrap_matches_the_documented_volume() -> None:
    """ARCHITECTURE §5.1: 5.454 Tage, 38.178 Dateien (Stand 2026-07-25)."""
    files = pending_files({}, today=date(2026, 7, 25))
    assert len(days_between(FIRST_DAY, date(2026, 7, 24))) == 5_454
    assert len(files) == 38_178
    assert files[0] == DeltaFile(FIRST_DAY, Stream.TRACK)
    assert files[-1] == DeltaFile(date(2026, 7, 24), Stream.TRACK_PUID)


# --- Randfaelle ------------------------------------------------------------


def test_nothing_to_do_before_the_history_starts() -> None:
    assert pending_files({}, today=FIRST_DAY) == ()
    assert pending_files({}, today=FIRST_DAY - timedelta(days=100)) == ()


def test_todays_file_is_never_part_of_the_list() -> None:
    today = date(2026, 7, 25)
    files = pending_files(dict.fromkeys(IMPORT_ORDER, date(2026, 7, 22)), today=today)
    assert {item.day for item in files} == {date(2026, 7, 23), date(2026, 7, 24)}


def test_everything_done_yields_an_empty_list() -> None:
    done = dict.fromkeys(IMPORT_ORDER, date(2026, 7, 24))
    assert pending_files(done, today=date(2026, 7, 25)) == ()


def test_a_state_ahead_of_the_calendar_yields_nothing() -> None:
    """Sollte nie vorkommen — aber es darf keine Datei aus der Zukunft geben."""
    done = dict.fromkeys(IMPORT_ORDER, date(2026, 8, 1))
    assert pending_files(done, today=date(2026, 7, 25)) == ()


def test_streams_resume_independently() -> None:
    """Ein abgebrochener Lauf laesst die Stroeme auf verschiedenen Tagen stehen."""
    done: dict[Stream, date | None] = dict.fromkeys(IMPORT_ORDER, DAY_2)
    done[Stream.TRACK_META] = DAY_1
    done[Stream.TRACK_PUID] = None
    files = pending_files(done, today=DAY_3 + timedelta(days=1))
    assert names(files) == [
        # Der nie importierte Strom beginnt am Anfang der Historie …
        "2011-08-19-track_puid-update.jsonl.gz",
        # … der zurueckgebliebene an seinem naechsten Tag …
        "2011-08-20-track_meta-update.jsonl.gz",
        "2011-08-20-track_puid-update.jsonl.gz",
        # … und ab dem naechsten offenen Tag laufen alle sieben zusammen.
        "2011-08-21-track-update.jsonl.gz",
        "2011-08-21-meta-update.jsonl.gz",
        "2011-08-21-fingerprint-update.jsonl.gz",
        "2011-08-21-track_fingerprint-update.jsonl.gz",
        "2011-08-21-track_mbid-update.jsonl.gz",
        "2011-08-21-track_meta-update.jsonl.gz",
        "2011-08-21-track_puid-update.jsonl.gz",
    ]


def test_a_missing_stream_key_counts_as_never_imported() -> None:
    files = pending_files({Stream.TRACK: DAY_1}, today=DAY_2 + timedelta(days=1))
    assert names(files)[0] == "2011-08-19-meta-update.jsonl.gz"
    assert "2011-08-19-track-update.jsonl.gz" not in names(files)


def test_an_unknown_stream_in_the_state_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="Unbekannte Stroeme"):
        pending_files({"tracks": DAY_1}, today=DAY_2)  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="Unbekannte Stroeme"):
        plan_from_state({"tracks": [DAY_1]}, today=DAY_2)  # type: ignore[dict-item]


# --- Lueckenpruefung (Regel 5) ---------------------------------------------


def test_a_complete_state_has_no_gaps() -> None:
    state = dict.fromkeys(IMPORT_ORDER, days_between(DAY_1, DAY_3))
    plan = plan_from_state(state, today=DAY_3 + timedelta(days=1))
    assert plan.gaps == ()
    assert plan.files == ()


def test_a_hole_in_the_past_is_reported_as_a_gap() -> None:
    state: dict[Stream, list[date]] = {
        stream: days_between(DAY_1, DAY_3) for stream in IMPORT_ORDER
    }
    state[Stream.META] = [DAY_1, DAY_3]  # DAY_2 fehlt
    plan = plan_from_state(state, today=DAY_3 + timedelta(days=1))
    assert [gap.file.name for gap in plan.gaps] == ["2011-08-20-meta-update.jsonl.gz"]
    assert plan.gaps[0].day == DAY_2
    assert plan.gaps[0].stream is Stream.META
    assert "nie importiert" in str(plan.gaps[0])
    # Eine Luecke wird gemeldet, nicht stillschweigend nachgeholt: sonst
    # wuerde ein alter Tag neuere Werte ueberschreiben.
    assert plan.files == ()


def test_gaps_are_sorted_by_day_and_then_by_rule_1() -> None:
    state = {
        Stream.TRACK: [DAY_1, DAY_3],
        Stream.META: [DAY_1, DAY_3],
        Stream.FINGERPRINT: [DAY_2, DAY_3],
    }
    plan = plan_from_state(state, today=DAY_3 + timedelta(days=1))
    assert [(gap.day, gap.stream.value) for gap in plan.gaps] == [
        (DAY_1, "fingerprint"),
        (DAY_2, "track"),
        (DAY_2, "meta"),
    ]


def test_an_imported_but_empty_day_is_not_a_gap() -> None:
    """Leere Datei = regulaerer Tag; nur eine *fehlende* Datei ist eine Luecke."""
    state = dict.fromkeys(IMPORT_ORDER, days_between(DAY_1, DAY_2))
    plan = plan_from_state(state, today=DAY_3)
    assert plan.gaps == ()


def test_days_after_the_progress_are_work_not_gaps() -> None:
    state = {stream: [DAY_1] for stream in IMPORT_ORDER}
    plan = plan_from_state(state, today=DAY_3 + timedelta(days=1))
    assert plan.gaps == ()
    assert {item.day for item in plan.files} == {DAY_2, DAY_3}
    assert plan.days == (DAY_2, DAY_3)
    assert len(plan) == 14


def test_duplicates_and_order_in_the_state_do_not_matter() -> None:
    state = {stream: [DAY_2, DAY_1, DAY_2] for stream in IMPORT_ORDER}
    plan = plan_from_state(state, today=DAY_3)
    assert plan.gaps == ()
    assert plan.files == ()


def test_raise_on_gaps_reports_every_gap() -> None:
    gaps = tuple(Gap(DeltaFile(DAY_1 + timedelta(days=offset), Stream.META)) for offset in range(7))
    plan = ImportPlan((), gaps)
    with pytest.raises(GapError) as caught:
        plan.raise_on_gaps()
    assert caught.value.gaps == gaps
    assert "7 fehlende Tagesdatei(en)" in str(caught.value)
    assert "und 2 weitere" in str(caught.value)
    ImportPlan(()).raise_on_gaps()  # ohne Luecken: kein Fehler


# --- Reinheit der Logik ----------------------------------------------------


def test_the_worklist_never_reads_the_clock() -> None:
    """`today` ist Parameter — sonst waeren die Randfaelle nicht testbar."""
    tree = ast.parse(Path(worklist.__file__).read_text(encoding="utf-8"))
    called = {
        ast.unparse(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute | ast.Name)
    }
    assert not {name for name in called if name.endswith((".today", ".now", ".utcnow"))}
