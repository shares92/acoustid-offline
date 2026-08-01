"""Zustandsdatenbank: Schema, Migrationen, Transaktionen (Phase 14)."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from acoustid_watchdog.store import (
    DB_FILENAME,
    MIGRATIONS,
    SCHEMA_VERSION,
    Database,
    StoreError,
    utc_now,
)

# ARCHITECTURE §5 "SQLite (Waechter, Cache)" — der Lookup-Cache bekommt in
# Phase 17 eine eigene Ablage und gehoert bewusst nicht dazu.
EXPECTED_TABLES = {"api_key", "admin_user", "update_run", "event_log"}


def _table_names(db: Database) -> set[str]:
    with db.transaction() as tx:
        rows = tx.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {str(row["name"]) for row in rows if not str(row["name"]).startswith("sqlite_")}


def test_schema_has_exactly_the_four_tables(db: Database) -> None:
    assert _table_names(db) == EXPECTED_TABLES


def test_database_lives_in_the_data_dir(data_dir: Path, db: Database) -> None:
    assert db.path == data_dir / DB_FILENAME
    assert db.path.is_file()


def test_migrations_are_applied_on_open(db: Database) -> None:
    assert db.schema_version == SCHEMA_VERSION == len(MIGRATIONS)


def test_migrate_is_idempotent(db: Database) -> None:
    """Ein zweiter Lauf ist ein No-Op — Neustarts duerfen nichts anfassen."""
    assert db.migrate() == 0
    assert db.schema_version == SCHEMA_VERSION


def test_reopening_keeps_the_data(data_dir: Path) -> None:
    with Database.for_data_dir(data_dir) as first, first.transaction() as tx:
        tx.execute(
            "INSERT INTO event_log (ts, level, source, message) VALUES (?, ?, ?, ?)",
            (utc_now(), "INFO", "test", "bleibt"),
        )
    with Database.for_data_dir(data_dir) as second:
        with second.transaction() as tx:
            row = tx.execute("SELECT message FROM event_log").fetchone()
        assert row["message"] == "bleibt"
        assert second.schema_version == SCHEMA_VERSION


def test_open_creates_missing_parent_directories(tmp_path: Path) -> None:
    with Database(tmp_path / "gibt" / "es" / "noch" / "nicht" / DB_FILENAME) as database:
        assert database.path.is_file()


def test_downgrade_is_refused(data_dir: Path) -> None:
    """Eine Datei aus einer neueren Version darf nicht stillschweigend laufen."""
    with Database.for_data_dir(data_dir) as database, database.transaction() as tx:
        tx.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 5}")
    with pytest.raises(StoreError, match=re.escape(f"Schemaversion {SCHEMA_VERSION + 5}")):
        Database.for_data_dir(data_dir).open()


def test_transaction_rolls_back_on_error(db: Database) -> None:
    with pytest.raises(RuntimeError), db.transaction() as tx:
        tx.execute(
            "INSERT INTO event_log (ts, level, source, message) VALUES (?, ?, ?, ?)",
            (utc_now(), "INFO", "test", "verworfen"),
        )
        raise RuntimeError("Abbruch mitten in der Transaktion")

    with db.transaction() as tx:
        assert tx.execute("SELECT COUNT(*) FROM event_log").fetchone()[0] == 0


def test_nested_transactions_commit_once(db: Database) -> None:
    """Verschachtelte Aufrufe teilen sich die aeussere Transaktion."""
    with db.transaction() as outer:
        outer.execute(
            "INSERT INTO event_log (ts, level, source, message) VALUES (?, ?, ?, ?)",
            (utc_now(), "INFO", "test", "aussen"),
        )
        with db.transaction() as inner:
            inner.execute(
                "INSERT INTO event_log (ts, level, source, message) VALUES (?, ?, ?, ?)",
                (utc_now(), "INFO", "test", "innen"),
            )

    with db.transaction() as tx:
        assert tx.execute("SELECT COUNT(*) FROM event_log").fetchone()[0] == 2


def test_nested_rollback_discards_the_whole_transaction(db: Database) -> None:
    with pytest.raises(RuntimeError), db.transaction() as outer:
        outer.execute(
            "INSERT INTO event_log (ts, level, source, message) VALUES (?, ?, ?, ?)",
            (utc_now(), "INFO", "test", "aussen"),
        )
        with db.transaction() as inner:
            inner.execute(
                "INSERT INTO event_log (ts, level, source, message) VALUES (?, ?, ?, ?)",
                (utc_now(), "INFO", "test", "innen"),
            )
        raise RuntimeError("nach der inneren Transaktion")

    with db.transaction() as tx:
        assert tx.execute("SELECT COUNT(*) FROM event_log").fetchone()[0] == 0


def test_access_without_open_is_refused(data_dir: Path) -> None:
    database = Database.for_data_dir(data_dir)
    with pytest.raises(StoreError, match="nicht geoeffnet"), database.transaction():
        pass


def test_close_is_idempotent(data_dir: Path) -> None:
    database = Database.for_data_dir(data_dir).open()
    database.close()
    database.close()


def test_admin_user_table_holds_at_most_one_row(db: Database) -> None:
    """Ein Admin — als Schema-Eigenschaft, nicht als Verabredung (§11)."""
    now = utc_now()
    with db.transaction() as tx:
        tx.execute(
            "INSERT INTO admin_user (id, login, password_hash, created_at, updated_at)"
            " VALUES (1, 'admin', 'x', ?, ?)",
            (now, now),
        )
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as tx:
        tx.execute(
            "INSERT INTO admin_user (id, login, password_hash, created_at, updated_at)"
            " VALUES (2, 'zweiter', 'x', ?, ?)",
            (now, now),
        )


def test_api_key_hash_is_unique(db: Database) -> None:
    with db.transaction() as tx:
        tx.execute(
            "INSERT INTO api_key (label, key_hash, created_at) VALUES (?, ?, ?)",
            ("Picard", "hash-1", utc_now()),
        )
    with pytest.raises(sqlite3.IntegrityError), db.transaction() as tx:
        tx.execute(
            "INSERT INTO api_key (label, key_hash, created_at) VALUES (?, ?, ?)",
            ("beets", "hash-1", utc_now()),
        )


def test_utc_now_is_iso_utc() -> None:
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", utc_now())
