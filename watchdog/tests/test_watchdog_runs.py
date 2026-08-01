"""Lauf-Historie ``update_run``: Anlegen, Abschliessen, Datenstand (Phase 14)."""

from __future__ import annotations

import pytest

from acoustid_watchdog.runs import (
    RunKind,
    RunResult,
    finish_run,
    latest_data_sequence,
    latest_run,
    latest_successful_run,
    running_runs,
    start_run,
)
from acoustid_watchdog.store import Database


def test_started_run_is_running_and_has_no_result(db: Database) -> None:
    run_id = start_run(db, RunKind.UPDATE)
    run = latest_run(db)
    assert run is not None
    assert run.id == run_id
    assert run.kind is RunKind.UPDATE
    assert run.running is True
    assert run.result is None
    assert run.finished_at is None


def test_finish_run_writes_the_result(db: Database) -> None:
    run_id = start_run(db, RunKind.UPDATE)
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
    run_id = start_run(db, RunKind.UPDATE)
    finished = finish_run(db, run_id, RunResult.FAILED, error="Plattenplatz zu knapp")
    assert finished.result is RunResult.FAILED
    assert finished.error == "Plattenplatz zu knapp"


def test_finish_run_rejects_unknown_ids(db: Database) -> None:
    with pytest.raises(KeyError):
        finish_run(db, 4711, RunResult.SUCCESS)


def test_latest_run_can_be_narrowed_to_one_kind(db: Database) -> None:
    update_id = start_run(db, RunKind.UPDATE)
    backup_id = start_run(db, RunKind.BACKUP)

    newest = latest_run(db)
    assert newest is not None and newest.id == backup_id

    newest_update = latest_run(db, RunKind.UPDATE)
    assert newest_update is not None and newest_update.id == update_id


def test_latest_successful_run_skips_failures(db: Database) -> None:
    good = start_run(db, RunKind.UPDATE)
    finish_run(db, good, RunResult.SUCCESS, last_sequence="2026-07-20")
    bad = start_run(db, RunKind.UPDATE)
    finish_run(db, bad, RunResult.FAILED, error="kaputt")

    assert latest_run(db, RunKind.UPDATE).id == bad  # type: ignore[union-attr]
    assert latest_successful_run(db, RunKind.UPDATE).id == good  # type: ignore[union-attr]


def test_running_runs_lists_only_unfinished_ones(db: Database) -> None:
    done = start_run(db, RunKind.UPDATE)
    finish_run(db, done, RunResult.SUCCESS)
    open_id = start_run(db, RunKind.BACKUP)

    assert [run.id for run in running_runs(db)] == [open_id]


# --- Datenstand -------------------------------------------------------------


def test_data_sequence_is_none_before_the_first_import(db: Database) -> None:
    assert latest_data_sequence(db) is None


def test_data_sequence_survives_a_run_without_new_deltas(db: Database) -> None:
    """Ein erfolgreicher Lauf ohne neue Tagesdatei setzt den Stand nicht zurueck."""
    first = start_run(db, RunKind.UPDATE)
    finish_run(db, first, RunResult.SUCCESS, files_imported=2, last_sequence="2026-07-22")
    idle = start_run(db, RunKind.UPDATE)
    finish_run(db, idle, RunResult.SUCCESS, files_imported=0)

    data = latest_data_sequence(db)
    assert data is not None
    assert data.id == first
    assert data.last_sequence == "2026-07-22"


def test_data_sequence_ignores_failed_runs_and_backups(db: Database) -> None:
    good = start_run(db, RunKind.UPDATE)
    finish_run(db, good, RunResult.SUCCESS, last_sequence="2026-07-22")

    bad = start_run(db, RunKind.UPDATE)
    finish_run(
        db, bad, RunResult.FAILED, last_sequence="2026-07-23", error="mittendrin abgebrochen"
    )

    backup = start_run(db, RunKind.BACKUP)
    finish_run(db, backup, RunResult.SUCCESS, last_sequence="2026-07-24")

    data = latest_data_sequence(db)
    assert data is not None and data.last_sequence == "2026-07-22"


def test_as_dict_leaves_the_report_out(db: Database) -> None:
    """Die Statusantwort wird gepollt — der Report gehoert in die Detailsicht."""
    run_id = start_run(db, RunKind.UPDATE)
    finished = finish_run(db, run_id, RunResult.SUCCESS, report={"gross": "x" * 1000})
    payload = finished.as_dict()
    assert "report" not in payload
    assert payload["result"] == "success"
    assert payload["result_display"] == "erfolgreich"
    assert payload["kind_display"] == "Update"


def test_result_column_rejects_unknown_values(db: Database) -> None:
    """Der CHECK im Schema haelt die Statusmaschine zusammen."""
    import sqlite3

    run_id = start_run(db, RunKind.UPDATE)
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as tx:
        tx.execute("UPDATE update_run SET result = 'vielleicht' WHERE id = ?", (run_id,))
