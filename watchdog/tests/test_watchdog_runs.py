"""Lauf-Historie ``update_run``: Anlegen, Abschliessen, Datenstand (Phase 14)."""

from __future__ import annotations

import pytest

from acoustid_watchdog.runs import (
    RunKind,
    RunResult,
    abandon_stale_runs,
    finish_run,
    latest_data_sequence,
    latest_run,
    latest_run_since,
    latest_successful_run,
    open_run,
    run_totals,
    running_runs,
    start_run,
)
from acoustid_watchdog.store import Database, utc_now


def test_started_run_is_running_and_has_no_result(db: Database) -> None:
    run_id = start_run(db, RunKind.ACOUSTID_DELTA)
    run = latest_run(db)
    assert run is not None
    assert run.id == run_id
    assert run.kind is RunKind.ACOUSTID_DELTA
    assert run.running is True
    assert run.result is None
    assert run.finished_at is None


def test_finish_run_writes_the_result(db: Database) -> None:
    run_id = start_run(db, RunKind.ACOUSTID_DELTA)
    finished = finish_run(
        db,
        run_id,
        RunResult.SUCCESS,
        files_imported=3,
        rows_imported=120_000,
        last_sequence="2026-07-22",
        report={"exit_code": 0, "files": 3},
    )
    assert finished.running is False
    assert finished.result is RunResult.SUCCESS
    assert finished.files_imported == 3
    assert finished.rows_imported == 120_000
    assert finished.last_sequence == "2026-07-22"
    assert finished.finished_at is not None
    assert finished.report == {"exit_code": 0, "files": 3}


def test_failed_run_keeps_its_error(db: Database) -> None:
    run_id = start_run(db, RunKind.ACOUSTID_DELTA)
    finished = finish_run(db, run_id, RunResult.FAILED, error="Plattenplatz zu knapp")
    assert finished.result is RunResult.FAILED
    assert finished.error == "Plattenplatz zu knapp"


def test_finish_run_rejects_unknown_ids(db: Database) -> None:
    with pytest.raises(KeyError):
        finish_run(db, 4711, RunResult.SUCCESS)


def test_latest_run_can_be_narrowed_to_one_kind(db: Database) -> None:
    update_id = start_run(db, RunKind.ACOUSTID_DELTA)
    backup_id = start_run(db, RunKind.BACKUP)

    newest = latest_run(db)
    assert newest is not None and newest.id == backup_id

    newest_update = latest_run(db, RunKind.ACOUSTID_DELTA)
    assert newest_update is not None and newest_update.id == update_id


def test_latest_successful_run_skips_failures(db: Database) -> None:
    good = start_run(db, RunKind.ACOUSTID_DELTA)
    finish_run(db, good, RunResult.SUCCESS, last_sequence="2026-07-20")
    bad = start_run(db, RunKind.ACOUSTID_DELTA)
    finish_run(db, bad, RunResult.FAILED, error="kaputt")

    assert latest_run(db, RunKind.ACOUSTID_DELTA).id == bad  # type: ignore[union-attr]
    assert latest_successful_run(db, RunKind.ACOUSTID_DELTA).id == good  # type: ignore[union-attr]


def test_running_runs_lists_only_unfinished_ones(db: Database) -> None:
    done = start_run(db, RunKind.ACOUSTID_DELTA)
    finish_run(db, done, RunResult.SUCCESS)
    open_id = start_run(db, RunKind.BACKUP)

    assert [run.id for run in running_runs(db)] == [open_id]


# --- Datenstand -------------------------------------------------------------


def test_data_sequence_is_none_before_the_first_import(db: Database) -> None:
    assert latest_data_sequence(db) is None


def test_data_sequence_survives_a_run_without_new_deltas(db: Database) -> None:
    """Ein erfolgreicher Lauf ohne neue Tagesdatei setzt den Stand nicht zurueck."""
    first = start_run(db, RunKind.ACOUSTID_DELTA)
    finish_run(db, first, RunResult.SUCCESS, files_imported=2, last_sequence="2026-07-22")
    idle = start_run(db, RunKind.ACOUSTID_DELTA)
    finish_run(db, idle, RunResult.SUCCESS, files_imported=0)

    data = latest_data_sequence(db)
    assert data is not None
    assert data.id == first
    assert data.last_sequence == "2026-07-22"


def test_data_sequence_ignores_failed_runs_and_backups(db: Database) -> None:
    good = start_run(db, RunKind.ACOUSTID_DELTA)
    finish_run(db, good, RunResult.SUCCESS, last_sequence="2026-07-22")

    bad = start_run(db, RunKind.ACOUSTID_DELTA)
    finish_run(
        db, bad, RunResult.FAILED, last_sequence="2026-07-23", error="mittendrin abgebrochen"
    )

    backup = start_run(db, RunKind.BACKUP)
    finish_run(db, backup, RunResult.SUCCESS, last_sequence="2026-07-24")

    data = latest_data_sequence(db)
    assert data is not None and data.last_sequence == "2026-07-22"


def test_as_dict_leaves_the_report_out(db: Database) -> None:
    """Die Statusantwort wird gepollt — der Report gehoert in die Detailsicht."""
    run_id = start_run(db, RunKind.ACOUSTID_DELTA)
    finished = finish_run(db, run_id, RunResult.SUCCESS, report={"gross": "x" * 1000})
    payload = finished.as_dict()
    assert "report" not in payload
    assert payload["result"] == "success"
    assert payload["result_display"] == "erfolgreich"
    assert payload["kind_display"] == "AcoustID-Delta"


def test_result_column_rejects_unknown_values(db: Database) -> None:
    """Der CHECK im Schema haelt die Statusmaschine zusammen."""
    import sqlite3

    run_id = start_run(db, RunKind.ACOUSTID_DELTA)
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as tx:
        tx.execute("UPDATE update_run SET result = 'vielleicht' WHERE id = ?", (run_id,))


# --- Die Job-Arten aus E10 (M2.5) -------------------------------------------


@pytest.mark.parametrize("kind", list(RunKind))
def test_every_run_kind_passes_the_schema_check(db: Database, kind: RunKind) -> None:
    """Enum und CHECK-Constraint sagen dasselbe — sonst faellt es erst im Betrieb auf."""
    run_id = start_run(db, kind)
    run = latest_run(db, kind)
    assert run is not None and run.id == run_id and run.kind is kind
    assert kind.display_name


def test_data_sequence_ignores_the_other_job_kinds(db: Database) -> None:
    """Nur der AcoustID-Delta-Lauf setzt den Datenstand von `/status`."""
    delta = start_run(db, RunKind.ACOUSTID_DELTA)
    finish_run(db, delta, RunResult.SUCCESS, last_sequence="2026-07-22")
    for other in (RunKind.DISCOGS_DUMP, RunKind.CAA_CRAWL, RunKind.QUEUE_SEND):
        run_id = start_run(db, other)
        finish_run(db, run_id, RunResult.SUCCESS, last_sequence="2026-08-01")

    data = latest_data_sequence(db)
    assert data is not None and data.id == delta and data.last_sequence == "2026-07-22"


def test_latest_run_since_answers_the_schedulers_question(db: Database) -> None:
    """„Lief dieser Job seit X schon?" — die Faelligkeitsfrage."""
    assert latest_run_since(db, RunKind.ACOUSTID_DELTA, "2026-08-05T00:00:00.000Z") is None

    run_id = start_run(db, RunKind.ACOUSTID_DELTA, started_at="2026-08-05T04:00:00.000Z")
    assert latest_run_since(db, RunKind.ACOUSTID_DELTA, "2026-08-05T00:00:00.000Z").id == run_id  # type: ignore[union-attr]
    # Die Grenze des Folgetags sieht ihn nicht mehr — dann ist der Job faellig.
    assert latest_run_since(db, RunKind.ACOUSTID_DELTA, "2026-08-06T00:00:00.000Z") is None
    # Und eine andere Art zaehlt nicht mit.
    assert latest_run_since(db, RunKind.BACKUP, "2026-08-05T00:00:00.000Z") is None


def test_run_totals_count_by_kind_and_outcome(db: Database) -> None:
    done = start_run(db, RunKind.ACOUSTID_DELTA)
    finish_run(db, done, RunResult.SUCCESS)
    failed = start_run(db, RunKind.ACOUSTID_DELTA)
    finish_run(db, failed, RunResult.FAILED, error="kaputt")
    start_run(db, RunKind.BACKUP)

    assert run_totals(db) == {
        ("acoustid-delta", "success"): 1,
        ("acoustid-delta", "failed"): 1,
        ("backup", "running"): 1,
    }


def test_abandon_stale_runs_closes_only_the_older_ones(db: Database) -> None:
    """K1: die Grenze ist der eigene Prozessstart — Eigenes bleibt unangetastet."""
    old = start_run(db, RunKind.ACOUSTID_DELTA, started_at="2026-08-05T04:00:00.000Z")
    mine = start_run(db, RunKind.BACKUP, started_at="2026-08-05T06:00:00.000Z")

    closed = abandon_stale_runs(db, before="2026-08-05T05:00:00.000Z", error="hart beendet")

    assert [run.id for run in closed] == [old]
    assert closed[0].result is RunResult.ABORTED
    assert closed[0].error == "hart beendet"
    assert closed[0].finished_at is not None
    # Der eigene Lauf laeuft weiter.
    assert [run.id for run in running_runs(db)] == [mine]


def test_abandon_stale_runs_leaves_finished_rows_alone(db: Database) -> None:
    run_id = start_run(db, RunKind.ACOUSTID_DELTA, started_at="2026-08-05T04:00:00.000Z")
    finish_run(db, run_id, RunResult.SUCCESS, files_imported=3)

    assert abandon_stale_runs(db, before="2026-08-05T05:00:00.000Z", error="x") == []
    run = latest_run(db, RunKind.ACOUSTID_DELTA)
    assert run is not None and run.result is RunResult.SUCCESS and run.files_imported == 3


def test_abandon_stale_runs_on_an_empty_table_is_a_no_op(db: Database) -> None:
    assert abandon_stale_runs(db, before=utc_now(), error="x") == []


def test_the_idle_stop_is_free_again_after_reconciliation(db: Database) -> None:
    """Der eigentliche Gewinn: die Job-Sperre des Idle-Stopps faellt weg (§8.5)."""
    from acoustid_watchdog.lifecycle import DatabaseJobs

    start_run(db, RunKind.ACOUSTID_DELTA, started_at="2026-08-05T04:00:00.000Z")
    jobs = DatabaseJobs(db)
    assert jobs.running_jobs() != []

    abandon_stale_runs(db, before="2026-08-05T05:00:00.000Z", error="hart beendet")

    assert jobs.running_jobs() == []


def test_open_run_only_answers_for_unfinished_rows(db: Database) -> None:
    run_id = start_run(db, RunKind.ACOUSTID_DELTA)
    assert open_run(db, run_id) is not None

    finish_run(db, run_id, RunResult.SUCCESS)
    assert open_run(db, run_id) is None
    assert open_run(db, 4711) is None


def test_duration_is_none_while_a_run_is_open(db: Database) -> None:
    run_id = start_run(db, RunKind.ACOUSTID_DELTA, started_at="2026-08-05T04:00:00.000Z")
    open_run = latest_run(db)
    assert open_run is not None and open_run.duration_s is None

    finished = finish_run(db, run_id, RunResult.SUCCESS, finished_at="2026-08-05T04:12:30.000Z")
    assert finished.duration_s == 750.0
