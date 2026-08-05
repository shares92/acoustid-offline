"""Backup-Job: was gesichert wird — und was ausdruecklich nicht (K9, M2.5).

Die Dateiteile laufen ohne Dienste; der Gesamtlauf braucht eine Postgres
und traegt deshalb den Marker ``integration``.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from acoustid_importer.backup import (
    EXCLUDED_FILENAMES,
    REPORT_SCHEMA,
    STATE_DB_FILENAME,
    _copy_config,
    _copy_sqlite,
    main,
    run_backup,
)
from shared.env import EnvSettings


def _state_db(path: Path, rows: int = 3) -> Path:
    """Eine kleine Waechter-SQLite mit Inhalt (im WAL-Modus wie im Betrieb)."""
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("CREATE TABLE api_key (id INTEGER PRIMARY KEY, label TEXT)")
    connection.executemany(
        "INSERT INTO api_key (label) VALUES (?)", [(f"key-{index}",) for index in range(rows)]
    )
    connection.commit()
    connection.close()
    return path


# --- Die Dateiteile ---------------------------------------------------------


def test_the_state_database_is_copied_consistently(tmp_path: Path) -> None:
    """Online-Backup-API statt Dateikopie: der Waechter schreibt weiter."""
    source = _state_db(tmp_path / STATE_DB_FILENAME)
    target = tmp_path / "kopie.sqlite3"

    block = _copy_sqlite(source, target)

    assert block["present"] is True
    assert block["file"] == "kopie.sqlite3"
    with sqlite3.connect(target) as copy:
        assert copy.execute("SELECT count(*) FROM api_key").fetchone()[0] == 3


def test_a_missing_state_database_is_not_an_error(tmp_path: Path) -> None:
    block = _copy_sqlite(tmp_path / "gibt-es-nicht.sqlite3", tmp_path / "kopie.sqlite3")
    assert block == {"file": None, "present": False}


def test_the_configuration_keeps_its_restrictive_mode(tmp_path: Path) -> None:
    """Die Sicherung enthaelt dieselben Secrets — sie darf nicht offener liegen (§6)."""
    source = tmp_path / "config.yaml"
    source.write_text("auth:\n  mode: none\n", encoding="utf-8")
    target = tmp_path / "kopie.yaml"

    block = _copy_config(source, target)

    assert block["present"] is True
    assert target.stat().st_mode & 0o777 == 0o640


def test_a_missing_configuration_is_not_an_error(tmp_path: Path) -> None:
    assert _copy_config(tmp_path / "weg.yaml", tmp_path / "kopie.yaml")["present"] is False


def test_the_lookup_cache_is_named_as_excluded() -> None:
    """Ein Name in der Sperrliste ist nachweisbar, ein weggelassener nicht."""
    assert "lookup-cache.sqlite3" in EXCLUDED_FILENAMES


def test_the_state_db_name_matches_the_watchdog() -> None:
    """Der Importer haengt nicht vom Waechter-Paket ab — der Name muss trotzdem stimmen."""
    from acoustid_watchdog.store import DB_FILENAME

    assert STATE_DB_FILENAME == DB_FILENAME


# --- Der Gesamtlauf ---------------------------------------------------------


@pytest.mark.integration
def test_a_backup_writes_the_three_parts_and_nothing_else(
    db: psycopg.Connection, env_settings: EnvSettings, tmp_path: Path
) -> None:
    """Definition of Done: die Unikate sind drin, der Lookup-Cache nicht."""
    data_dir = tmp_path / "config"
    data_dir.mkdir()
    _state_db(data_dir / STATE_DB_FILENAME)
    # Der Lookup-Cache liegt daneben — und darf nicht mitkommen.
    (data_dir / "lookup-cache.sqlite3").write_bytes(b"SQLite format 3\x00")
    (data_dir / "config.yaml").write_text("auth:\n  mode: none\n", encoding="utf-8")

    db.execute(
        "INSERT INTO local_submission (local_track_id, local_track_gid, fingerprint, length)"
        " VALUES (2147483648, gen_random_uuid(), ARRAY[1,2,3], 137)"
    )
    settings = env_settings.model_copy(
        update={
            "db_name": db.info.dbname,
            "data_dir": data_dir,
            "config_path": data_dir / "config.yaml",
        }
    )
    target = tmp_path / "backup"
    target.mkdir()

    report = run_backup(settings, target, now=datetime(2026, 8, 5, 4, 45, tzinfo=UTC))

    assert report["result"] == "ok"
    assert report["rows"] == 1
    directory = Path(report["directory"])
    assert directory.name == "backup-20260805-044500"
    names = {item.name for item in directory.iterdir()}
    assert names == {
        "local_submission.copy.gz",
        STATE_DB_FILENAME,
        "config.yaml",
        "manifest.json",
    }
    # Das ``.part``-Verzeichnis ist weg — nur der fertige Satz bleibt.
    assert not list(target.glob("*.part"))

    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == REPORT_SCHEMA
    assert manifest["local_submission"]["columns"][0] == "id"
    assert "local_submission_track_id_seq" in manifest["local_submission"]["sequences"]
    assert manifest["excluded"] == ["lookup-cache.sqlite3"]

    # Der COPY-Text laesst sich wieder einlesen — das ist der Sinn des Formats.
    dump = gzip.decompress((directory / "local_submission.copy.gz").read_bytes())
    assert dump.count(b"\n") == 1
    assert b"{1,2,3}" in dump


@pytest.mark.integration
def test_a_backup_without_the_table_still_saves_the_rest(
    empty_db: psycopg.Connection, env_settings: EnvSettings, tmp_path: Path
) -> None:
    """Eine frische Instanz vor dem Bootstrap ist kein Fehlerfall."""
    data_dir = tmp_path / "config"
    data_dir.mkdir()
    _state_db(data_dir / STATE_DB_FILENAME)
    settings = env_settings.model_copy(
        update={"db_name": empty_db.info.dbname, "data_dir": data_dir}
    )
    target = tmp_path / "backup"
    target.mkdir()

    report = run_backup(settings, target)

    assert report["result"] == "ok"
    assert report["rows"] is None
    manifest = report["manifest"]
    assert manifest["local_submission"]["present"] is False
    assert manifest["state_db"]["present"] is True


@pytest.mark.integration
def test_the_cover_switch_only_warns_for_now(
    db: psycopg.Connection, env_settings: EnvSettings, tmp_path: Path
) -> None:
    """``backup.include_covers`` steht im Schema, die Ablage kommt mit M4."""
    settings = env_settings.model_copy(update={"db_name": db.info.dbname, "data_dir": tmp_path})
    target = tmp_path / "backup"
    target.mkdir()

    report = run_backup(settings, target, include_covers=True)

    assert report["result"] == "ok"
    assert any("M4" in warning for warning in report["warnings"])
    assert report["manifest"]["include_covers"] is True


@pytest.mark.integration
def test_the_cli_writes_its_report(
    db: psycopg.Connection,
    env_settings: EnvSettings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Der Vertrag mit dem Waechter: Exit-Code und Report-Datei (E10)."""
    monkeypatch.setenv("MMO_DB_NAME", db.info.dbname)
    monkeypatch.setenv("MMO_DATA_DIR", str(tmp_path))
    target = tmp_path / "backup"
    target.mkdir()
    report_path = tmp_path / "jobs" / "backup.json"

    code = main(["--target", str(target), "--report", str(report_path)])

    assert code == 0
    document = json.loads(report_path.read_text(encoding="utf-8"))
    assert document["schema"] == REPORT_SCHEMA
    assert document["result"] == "ok"
    assert document["exit_code"] == 0


def test_the_cli_reports_a_failure_instead_of_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auch ein gescheiterter Lauf hinterlaesst einen Report (docs/importer-job.md)."""
    monkeypatch.setenv("MMO_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MMO_DB_PASSWORD", "x")
    monkeypatch.setenv("MMO_DB_HOST", "127.0.0.1")
    monkeypatch.setenv("MMO_DB_PORT", "1")  # dort lauscht nichts
    report_path = tmp_path / "backup.json"

    code = main(["--target", str(tmp_path / "backup"), "--report", str(report_path)])

    assert code == 1
    document = json.loads(report_path.read_text(encoding="utf-8"))
    assert document["result"] == "failed"
    assert document["error"]["message"]
