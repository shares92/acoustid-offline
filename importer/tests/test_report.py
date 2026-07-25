"""Exit-Codes, Ergebnis-Report und Hochrechnung — pure Logik (Phase 8).

Der Report ist die Schnittstelle zum Waechter (Phase 19): er fuellt daraus
``update_run``. Deshalb pruefen diese Tests nicht nur „laeuft durch", sondern
die Zusagen des Formats — welche Schluessel es gibt, dass jedes Ergebnis
genau einen Exit-Code hat und dass fehlende Messwerte ``null`` bleiben statt
erfunden zu werden.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from acoustid_importer.dbimport import FileImport
from acoustid_importer.download import Download
from acoustid_importer.indexfeed import IndexFeedReport
from acoustid_importer.report import (
    HISTORY_GZ_BYTES,
    REPORT_SCHEMA,
    RESULT_BY_EXIT_CODE,
    ErrorInfo,
    ExitCode,
    JobMode,
    RunReport,
    RunResult,
    RunTally,
    build_report,
    project,
)
from acoustid_importer.streams import DeltaFile, Stream
from acoustid_importer.worklist import Gap

DAY = date(2026, 7, 22)
STARTED = datetime(2026, 7, 25, 4, 0, tzinfo=UTC)


def delta(stream: Stream, day: date = DAY) -> DeltaFile:
    return DeltaFile(day, stream)


def imported(
    stream: Stream,
    *,
    rows: int = 10,
    size: int = 1000,
    day: date = DAY,
    duration_s: float = 1.0,
    skipped: bool = False,
    fallbacks: int = 0,
    unknown: tuple[str, ...] = (),
) -> FileImport:
    file = delta(stream, day)
    return FileImport(
        file=file,
        path=Path("/dumps") / file.name,
        rows=rows,
        lines=rows,
        batches=1,
        file_size=size,
        duration_s=duration_s,
        skipped=skipped,
        escaping_fallbacks=fallbacks,
        unknown_fields=unknown,
    )


# --- Exit-Codes -------------------------------------------------------------


def test_every_result_has_exactly_one_exit_code_and_back() -> None:
    """Der Waechter muss vom Code auf das Ergebnis schliessen koennen."""
    codes = {result: result.exit_code for result in RunResult}
    assert len(set(codes.values())) == len(RunResult)
    assert {code: result for result, code in codes.items()} == RESULT_BY_EXIT_CODE


def test_the_documented_exit_codes_do_not_move() -> None:
    """Die Zahlen stehen in docs/importer-job.md und im Waechter — sie sind fix."""
    assert (int(ExitCode.OK), int(ExitCode.ERROR), int(ExitCode.USAGE)) == (0, 1, 2)
    assert int(RunResult.DISK_GUARD.exit_code) == 3
    assert int(RunResult.DOWNLOAD_FAILED.exit_code) == 4
    assert int(RunResult.GAPS.exit_code) == 5
    assert int(RunResult.IMPORT_FAILED.exit_code) == 6
    assert int(RunResult.INDEX_FEED_FAILED.exit_code) == 7
    assert int(RunResult.ABORTED.exit_code) == 8


def test_only_ok_counts_as_a_good_run() -> None:
    assert RunResult.OK.ok
    assert not any(result.ok for result in RunResult if result is not RunResult.OK)


# --- Mitschrift -------------------------------------------------------------


def test_the_tally_adds_up_rows_files_and_bytes_per_stream() -> None:
    tally = RunTally()
    tally.add_import(imported(Stream.TRACK, rows=3, size=100, duration_s=0.5))
    tally.add_import(imported(Stream.META, rows=7, size=200, duration_s=1.5))
    tally.add_import(imported(Stream.META, rows=5, size=300, day=date(2026, 7, 23)))

    assert tally.rows == 15
    assert tally.rows_by_stream == {"track": 3, "meta": 12}
    assert tally.files_by_stream == {"track": 1, "meta": 2}
    assert tally.gz_bytes == 600
    assert tally.import_duration_s == pytest.approx(3.0)
    assert (tally.first_day, tally.last_day) == (DAY, date(2026, 7, 23))
    assert tally.counts.imported == 3


def test_skipped_files_count_as_skipped_and_change_nothing_else() -> None:
    """Resume-Fall: die Datei stand schon als erledigt in `import_state`."""
    tally = RunTally()
    tally.add_import(imported(Stream.TRACK, rows=0, size=999, skipped=True))

    assert tally.counts.skipped == 1
    assert (tally.counts.imported, tally.rows, tally.gz_bytes) == (0, 0, 0)
    assert tally.first_day is None


def test_empty_day_files_are_counted_but_are_no_gap() -> None:
    """23-Byte-gz ist regulaer (§5.1) — sie zaehlt als eingespielt."""
    tally = RunTally()
    tally.add_import(imported(Stream.TRACK_PUID, rows=0, size=23))

    assert tally.counts.empty == 1
    assert tally.counts.imported == 1


def test_the_tally_keeps_parser_findings_for_the_report() -> None:
    tally = RunTally()
    tally.add_import(imported(Stream.META, fallbacks=12, unknown=("gid",)))
    tally.add_import(imported(Stream.META, fallbacks=3, unknown=("gid", "merged_into")))

    assert tally.escaping_fallbacks == 15
    assert tally.unknown_fields == {"gid", "merged_into"}


def test_reused_files_do_not_count_as_downloaded_bytes() -> None:
    tally = RunTally()
    file = delta(Stream.TRACK)
    tally.add_download(Download(file, Path("/dumps") / file.name, 500, reused=True))
    tally.add_download(Download(file, Path("/dumps") / file.name, 700, resumed=True))

    assert tally.downloaded_bytes == 700
    assert tally.counts.downloaded == 1
    assert tally.counts.resumed == 1


# --- Hochrechnung -----------------------------------------------------------


def test_the_projection_scales_duration_and_size_linearly() -> None:
    """Ein Prozent des Bestands in 100 s heisst: 10.000 s fuer alles."""
    projection = project(
        measured_gz_bytes=1_000,
        measured_duration_s=100.0,
        total_gz_bytes=100_000,
        measured_db_bytes=4_000,
        measured_index_documents=50,
    )

    assert projection.coverage == pytest.approx(0.01)
    assert projection.throughput_gz_bytes_s == pytest.approx(10.0)
    assert projection.estimated_total_duration_s == pytest.approx(10_000.0)
    assert projection.estimated_total_hours == pytest.approx(10_000 / 3600)
    assert projection.estimated_db_bytes == 400_000
    assert projection.estimated_index_documents == 5_000


def test_without_measured_bytes_nothing_is_invented() -> None:
    projection = project(measured_gz_bytes=0, measured_duration_s=12.0, measured_db_bytes=17)

    assert projection.throughput_gz_bytes_s is None
    assert projection.estimated_total_duration_s is None
    assert projection.estimated_db_bytes is None
    assert projection.measured_db_bytes == 17


def test_without_measured_time_the_throughput_stays_unknown() -> None:
    projection = project(measured_gz_bytes=1_000, measured_duration_s=0.0)
    assert projection.throughput_gz_bytes_s is None
    assert projection.estimated_total_duration_s is None


def test_the_default_reference_is_the_documented_history_volume() -> None:
    """414 GB gz laut ARCHITECTURE §5.1."""
    assert HISTORY_GZ_BYTES == 414 * 10**9
    assert project(measured_gz_bytes=1, measured_duration_s=1.0).total_gz_bytes == HISTORY_GZ_BYTES


def test_a_reference_of_zero_is_a_usage_error() -> None:
    with pytest.raises(ValueError, match="total_gz_bytes"):
        project(measured_gz_bytes=1, measured_duration_s=1.0, total_gz_bytes=0)


# --- Report -----------------------------------------------------------------


def full_report(**kwargs: Any) -> RunReport:
    tally = RunTally()
    tally.files_planned = 3
    tally.add_import(imported(Stream.TRACK, rows=5, size=100))
    return build_report(
        mode=JobMode.BOOTSTRAP,
        result=RunResult.OK,
        tally=tally,
        started_at=STARTED,
        duration_s=1.5,
        finished_at=datetime(2026, 7, 25, 4, 0, 2, tzinfo=UTC),
        **kwargs,
    )


def test_the_report_carries_the_schema_version_and_the_exit_code() -> None:
    document = full_report().as_dict()

    assert document["schema"] == REPORT_SCHEMA
    assert document["mode"] == "bootstrap"
    assert document["result"] == "ok"
    assert document["exit_code"] == 0


def test_the_report_is_valid_json_with_the_documented_top_level_keys() -> None:
    document = json.loads(full_report().to_json())

    assert set(document) == {
        "schema",
        "mode",
        "result",
        "exit_code",
        "started_at",
        "finished_at",
        "duration_s",
        "import_duration_s",
        "files",
        "rows",
        "rows_by_stream",
        "files_by_stream",
        "gz_bytes",
        "downloaded_bytes",
        "days",
        "gaps",
        "escaping_fallbacks",
        "unknown_fields",
        "index_feed",
        "measurements",
        "projection",
        "error",
        "warnings",
    }
    assert document["files"] == {
        "planned": 3,
        "imported": 1,
        "skipped": 0,
        "downloaded": 0,
        "resumed": 0,
        "empty": 0,
    }
    assert document["days"] == {"first": "2026-07-22", "last": "2026-07-22"}


def test_a_run_without_index_feed_says_null_instead_of_zero() -> None:
    document = full_report().as_dict()
    assert document["index_feed"] is None
    assert document["projection"] is None
    assert document["error"] is None


def test_the_index_feed_summary_keeps_the_numbers_of_the_feed_report() -> None:
    feed = IndexFeedReport(
        documents=2214,
        batches=3,
        scanned=2214,
        incomplete=0,
        empty_queries=1,
        last_id=99,
        version=4,
        duration_s=2.5,
    )
    document = full_report(index_feed=feed).as_dict()

    assert document["index_feed"] == {
        "documents": 2214,
        "batches": 3,
        "scanned": 2214,
        "incomplete": 0,
        "empty_queries": 1,
        "last_id": 99,
        "version": 4,
        "duration_s": 2.5,
        "exhausted": True,
    }


def test_gaps_appear_as_file_names_because_that_is_what_a_human_needs() -> None:
    gap = Gap(delta(Stream.META, date(2011, 9, 1)))
    document = full_report(gaps=(gap,)).as_dict()

    assert document["gaps"] == ["2011-09-01-meta-update.jsonl.gz"]


def test_an_error_keeps_type_and_message() -> None:
    report = build_report(
        mode=JobMode.UPDATE,
        result=RunResult.DISK_GUARD,
        tally=RunTally(),
        started_at=STARTED,
        duration_s=0.1,
        error=OSError("kein Platz"),
    )

    assert report.exit_code is ExitCode.DISK_GUARD
    assert report.as_dict()["error"] == {"type": "OSError", "message": "kein Platz"}
    assert ErrorInfo.of(ValueError("x")).type == "ValueError"


def test_the_report_is_written_atomically(tmp_path: Path) -> None:
    """Der Waechter liest die Datei, sobald der Prozess endet."""
    target = tmp_path / "berichte" / "lauf.json"
    written = full_report().write(target)

    assert written == target
    assert json.loads(target.read_text(encoding="utf-8"))["schema"] == REPORT_SCHEMA
    assert not (tmp_path / "berichte" / "lauf.json.part").exists()


def test_the_summary_is_one_readable_line_for_the_log() -> None:
    report = full_report(
        projection=project(measured_gz_bytes=100, measured_duration_s=1.0, total_gz_bytes=360_000)
    )
    summary = report.summary()

    assert summary.startswith("bootstrap: ok")
    assert "1 Dateien" in summary
    assert "5 Zeilen" in summary
    assert "Hochrechnung Vollimport ~1.0 h" in summary
