"""Migrationen gegen eine echte Postgres (ARCHITECTURE §5.2).

Marker `integration`: laeuft nur mit erreichbarer Datenbank — Steuerung ueber
`--integration` bzw. `ACOUSTID_INTEGRATION_TESTS` (siehe conftest.py im
Repo-Wurzelverzeichnis). Jeder Test bekommt eine frisch angelegte, leere
Datenbank; die konfigurierte Datenbank selbst wird nur zum Anlegen und
Loeschen benutzt.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import NamedTuple
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from shared.db import (
    BOOKKEEPING_TABLE,
    CORE,
    INDEXES,
    MigrationDriftError,
    MigrationError,
    applied_versions,
    apply,
    apply_from_env,
    migrations,
    pending,
)
from shared.env import EnvSettings

pytestmark = pytest.mark.integration

TABLES = (
    "track",
    "fingerprint",
    "track_mbid",
    "meta",
    "track_meta",
    "track_puid",
    "import_state",
)

#: Tabelle der eigenen Einreichungen (Phase 11). Getrennt von :data:`TABLES`,
#: weil sie nicht aus dem Dump stammt und deshalb weder `src_day` noch
#: `imported_at` traegt.
LOCAL_TABLE = "local_submission"

ALL_TABLES = (*TABLES, LOCAL_TABLE)

#: Spalten je Tabelle: (Name, Typ laut format_type, nullable) — ARCHITECTURE §5.2.
EXPECTED_COLUMNS: dict[str, tuple[tuple[str, str, bool], ...]] = {
    "track": (
        ("id", "integer", False),
        ("gid", "uuid", False),
        ("new_id", "integer", True),
        ("created", "timestamp with time zone", False),
        ("updated", "timestamp with time zone", True),
        ("src_day", "date", False),
        ("imported_at", "timestamp with time zone", False),
    ),
    "fingerprint": (
        ("id", "integer", False),
        ("fingerprint", "integer[]", True),
        ("length", "integer", True),
        ("track_id", "integer", True),
        ("submission_count", "integer", True),
        ("created", "timestamp with time zone", True),
        ("updated", "timestamp with time zone", True),
        ("indexed_at", "timestamp with time zone", True),
        ("src_day", "date", False),
        ("imported_at", "timestamp with time zone", False),
    ),
    "track_mbid": (
        ("id", "integer", False),
        ("track_id", "integer", False),
        ("mbid", "uuid", False),
        ("submission_count", "integer", False),
        ("disabled", "boolean", False),
        ("created", "timestamp with time zone", False),
        ("updated", "timestamp with time zone", True),
        ("src_day", "date", False),
        ("imported_at", "timestamp with time zone", False),
    ),
    "meta": (
        ("id", "integer", False),
        ("track", "character varying", True),
        ("artist", "character varying", True),
        ("album", "character varying", True),
        ("album_artist", "character varying", True),
        ("track_no", "integer", True),
        ("disc_no", "integer", True),
        ("year", "integer", True),
        ("created", "timestamp with time zone", True),
        ("src_day", "date", False),
        ("imported_at", "timestamp with time zone", False),
    ),
    "track_meta": (
        ("id", "integer", False),
        ("track_id", "integer", False),
        ("meta_id", "integer", False),
        ("submission_count", "integer", False),
        ("created", "timestamp with time zone", False),
        ("updated", "timestamp with time zone", True),
        ("src_day", "date", False),
        ("imported_at", "timestamp with time zone", False),
    ),
    "track_puid": (
        ("id", "integer", False),
        ("track_id", "integer", False),
        ("puid", "uuid", False),
        ("submission_count", "integer", False),
        ("created", "timestamp with time zone", False),
        ("updated", "timestamp with time zone", True),
        ("src_day", "date", False),
        ("imported_at", "timestamp with time zone", False),
    ),
    "import_state": (
        ("stream", "text", False),
        ("day", "date", False),
        ("file_name", "text", False),
        ("file_size", "bigint", True),
        ("row_count", "bigint", True),
        ("started_at", "timestamp with time zone", False),
        ("finished_at", "timestamp with time zone", True),
    ),
    # Phase 11: alle Felder des Submit-Vertrags plus Buchfuehrung.
    "local_submission": (
        ("id", "bigint", False),
        ("local_track_id", "integer", False),
        ("local_track_gid", "uuid", False),
        ("status", "text", False),
        ("fingerprint", "integer[]", False),
        ("length", "integer", False),
        ("bitrate", "integer", True),
        ("fileformat", "character varying", True),
        ("mbid", "uuid", True),
        ("puid", "uuid", True),
        ("foreignid", "character varying", True),
        ("track", "character varying", True),
        ("artist", "character varying", True),
        ("album", "character varying", True),
        ("album_artist", "character varying", True),
        ("track_no", "integer", True),
        ("disc_no", "integer", True),
        ("year", "integer", True),
        ("client", "character varying", True),
        ("client_version", "character varying", True),
        ("submitted_by", "character varying", True),
        ("created", "timestamp with time zone", False),
        ("indexed_at", "timestamp with time zone", True),
        ("forwarded_at", "timestamp with time zone", True),
        ("forward_attempts", "integer", False),
        ("forward_error", "text", True),
    ),
}

EXPECTED_PRIMARY_KEYS = {
    "track": "PRIMARY KEY (id)",
    "fingerprint": "PRIMARY KEY (id)",
    "track_mbid": "PRIMARY KEY (id)",
    "meta": "PRIMARY KEY (id)",
    "track_meta": "PRIMARY KEY (id)",
    "track_puid": "PRIMARY KEY (id)",
    "import_state": "PRIMARY KEY (stream, day)",
    "local_submission": "PRIMARY KEY (id)",
}

EXPECTED_INDEXES = {
    "track": {"track_pkey", "track_idx_gid", "track_idx_new_id"},
    "fingerprint": {
        "fingerprint_pkey",
        "fingerprint_idx_track_id",
        "fingerprint_idx_incomplete",
        "fingerprint_idx_unindexed",
    },
    "track_mbid": {"track_mbid_pkey", "track_mbid_idx_track_id", "track_mbid_idx_mbid"},
    "meta": {"meta_pkey"},
    "track_meta": {"track_meta_pkey", "track_meta_idx_track_id"},
    "track_puid": {"track_puid_pkey"},
    "import_state": {"import_state_pkey"},
    "local_submission": {
        "local_submission_pkey",
        "local_submission_idx_unindexed",
        "local_submission_idx_track_id",
        "local_submission_idx_track_gid",
    },
}


class Scratch(NamedTuple):
    """Frisch angelegte, leere Datenbank samt passender Bootstrap-Umgebung."""

    conn: psycopg.Connection
    settings: EnvSettings


@contextmanager
def _scratch_database(settings: EnvSettings, *, autocommit: bool = False) -> Iterator[Scratch]:
    name = f"aoff_test_{uuid4().hex[:12]}"
    admin_dsn = settings.db_dsn().get_secret_value()
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    scratch = settings.model_copy(update={"db_name": name})
    try:
        with psycopg.connect(scratch.db_dsn().get_secret_value(), autocommit=autocommit) as conn:
            yield Scratch(conn=conn, settings=scratch)
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.fixture(scope="session")
def env_settings() -> EnvSettings:
    """Zugang aus den `AOFF_DB_*`-Variablen — derselbe Weg wie im Betrieb."""
    return EnvSettings.from_env()


@pytest.fixture
def empty(env_settings: EnvSettings) -> Iterator[Scratch]:
    """Leere Datenbank; autocommit, damit Introspektion zwischen zwei
    `apply`-Aufruf(en) keine Transaktion offen laesst."""
    with _scratch_database(env_settings, autocommit=True) as scratch:
        yield scratch


@pytest.fixture(scope="module")
def migrated(env_settings: EnvSettings) -> Iterator[Scratch]:
    """Einmal je Modul: leere Datenbank mit allen Gruppen angewendet.

    Bewusst mit einer normalen Verbindung (kein autocommit) — so wie ein
    Service sie beim Start aufbaut.
    """
    with _scratch_database(env_settings) as scratch:
        apply(scratch.conn)
        yield scratch


def _columns(conn: psycopg.Connection, table: str) -> list[tuple[str, str, bool]]:
    rows = conn.execute(
        """
        SELECT a.attname,
               pg_catalog.format_type(a.atttypid, a.atttypmod),
               NOT a.attnotnull
          FROM pg_catalog.pg_attribute a
         WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped
         ORDER BY a.attnum
        """,
        (table,),
    ).fetchall()
    return [(name, type_name, nullable) for name, type_name, nullable in rows]


def _indexes(conn: psycopg.Connection, table: str) -> dict[str, str]:
    rows = conn.execute(
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' AND tablename = %s",
        (table,),
    ).fetchall()
    return dict(rows)


# --- Vollstaendiges Schema --------------------------------------------------


def test_all_documented_tables_exist(migrated: Scratch) -> None:
    rows = migrated.conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    ).fetchall()
    assert {row[0] for row in rows} == {*ALL_TABLES, BOOKKEEPING_TABLE}


@pytest.mark.parametrize("table", ALL_TABLES)
def test_columns_match_architecture(migrated: Scratch, table: str) -> None:
    assert _columns(migrated.conn, table) == list(EXPECTED_COLUMNS[table])


@pytest.mark.parametrize("table", ALL_TABLES)
def test_primary_keys_match_architecture(migrated: Scratch, table: str) -> None:
    rows = migrated.conn.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = %s::regclass AND contype = 'p'",
        (table,),
    ).fetchall()
    assert [row[0] for row in rows] == [EXPECTED_PRIMARY_KEYS[table]]


def test_bookkeeping_defaults_are_set(migrated: Scratch) -> None:
    rows = migrated.conn.execute(
        """
        SELECT c.relname, a.attname, pg_get_expr(d.adbin, d.adrelid)
          FROM pg_attrdef d
          JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum
          JOIN pg_class c ON c.oid = d.adrelid
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public' AND c.relname <> %s
        """,
        (BOOKKEEPING_TABLE,),
    ).fetchall()
    defaults = {(table, column): expression for table, column, expression in rows}
    assert defaults == {
        **{(table, "imported_at"): "now()" for table in TABLES if table != "import_state"},
        ("track_mbid", "disabled"): "false",
        ("import_state", "started_at"): "now()",
        ("local_submission", "id"): "nextval('local_submission_id_seq'::regclass)",
        ("local_submission", "status"): "'new'::text",
        ("local_submission", "created"): "now()",
        ("local_submission", "forward_attempts"): "0",
    }


@pytest.mark.parametrize("table", ALL_TABLES)
def test_indexes_match_architecture(migrated: Scratch, table: str) -> None:
    assert set(_indexes(migrated.conn, table)) == EXPECTED_INDEXES[table]


def test_partial_index_predicates_are_kept(migrated: Scratch) -> None:
    track = _indexes(migrated.conn, "track")
    fingerprint = _indexes(migrated.conn, "fingerprint")

    assert track["track_idx_gid"].startswith("CREATE UNIQUE INDEX")
    assert "WHERE" not in track["track_idx_gid"]
    assert "WHERE (new_id IS NOT NULL)" in track["track_idx_new_id"]
    assert (
        "WHERE ((fingerprint IS NULL) OR (track_id IS NULL))"
        in (fingerprint["fingerprint_idx_incomplete"])
    )
    assert "WHERE (indexed_at IS NULL)" in fingerprint["fingerprint_idx_unindexed"]
    assert "WHERE" not in fingerprint["fingerprint_idx_track_id"]


@pytest.mark.parametrize("table", ["fingerprint", LOCAL_TABLE])
def test_fingerprint_vector_is_lz4_compressed(migrated: Scratch, table: str) -> None:
    """`l` = lz4; `p` waere pglz, leer der Server-Default."""
    row = migrated.conn.execute(
        "SELECT attcompression FROM pg_attribute "
        "WHERE attrelid = %s::regclass AND attname = 'fingerprint'",
        (table,),
    ).fetchone()
    assert row is not None
    assert row[0] == "l"


def test_the_status_domain_is_enforced(migrated: Scratch) -> None:
    """Ein unbekannter Status ist ein Datenbankfehler, kein Tippfehler im Code."""
    with migrated.conn.transaction(force_rollback=True):
        migrated.conn.execute(
            "INSERT INTO local_submission (local_track_id, local_track_gid, fingerprint, "
            "length, status) VALUES (1, gen_random_uuid(), ARRAY[1,2,3], 200, 'indexed')"
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            migrated.conn.execute(
                "INSERT INTO local_submission (local_track_id, local_track_gid, fingerprint, "
                "length, status) VALUES (2, gen_random_uuid(), ARRAY[1,2,3], 200, 'quatsch')"
            )


def test_the_local_track_id_sequence_cannot_leave_the_reserved_range(migrated: Scratch) -> None:
    """Dokument-ID = 2^31 + `local_track_id`, und der Index nimmt nur u32."""
    row = migrated.conn.execute(
        "SELECT seqmax, seqcycle FROM pg_sequence "
        "WHERE seqrelid = 'local_submission_track_id_seq'::regclass"
    ).fetchone()
    assert row is not None
    assert row[0] == 2**31 - 1
    assert row[1] is False


def test_schema_has_no_foreign_keys(migrated: Scratch) -> None:
    """Bewusst keine FKs (ARCHITECTURE §5.2)."""
    row = migrated.conn.execute(
        "SELECT count(*) FROM pg_constraint "
        "WHERE contype = 'f' AND connamespace = 'public'::regnamespace"
    ).fetchone()
    assert row is not None
    assert row[0] == 0


def test_every_migration_is_recorded(migrated: Scratch) -> None:
    expected = [item.version for item in migrations()]
    assert list(applied_versions(migrated.conn)) == expected
    assert pending(migrated.conn) == ()


# --- Verhalten des Runners --------------------------------------------------


def test_apply_on_an_empty_database_applies_everything(empty: Scratch) -> None:
    assert applied_versions(empty.conn) == ()
    report = apply(empty.conn)
    assert report.applied == tuple(item.version for item in migrations())
    assert report.skipped == ()
    assert report.changed


def test_second_run_is_a_noop(empty: Scratch) -> None:
    first = apply(empty.conn)
    stamps_before = empty.conn.execute(
        f"SELECT version, applied_at FROM {BOOKKEEPING_TABLE} ORDER BY version"
    ).fetchall()

    second = apply(empty.conn)

    assert second.applied == ()
    assert second.skipped == first.applied
    assert not second.changed
    stamps_after = empty.conn.execute(
        f"SELECT version, applied_at FROM {BOOKKEEPING_TABLE} ORDER BY version"
    ).fetchall()
    assert stamps_after == stamps_before


def test_core_group_leaves_the_secondary_indexes_out(empty: Scratch) -> None:
    """Bootstrap-Fall: erst `core`, nach dem Massenimport `indexes`."""
    core = apply(empty.conn, groups=[CORE])
    assert core.applied == tuple(item.version for item in migrations([CORE]))

    tables = empty.conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    ).fetchall()
    assert {row[0] for row in tables} == {*ALL_TABLES, BOOKKEEPING_TABLE}
    for table in ALL_TABLES:
        assert set(_indexes(empty.conn, table)) == {f"{table}_pkey"}
    assert [item.version for item in pending(empty.conn)] == [
        item.version for item in migrations([INDEXES])
    ]

    later = apply(empty.conn, groups=[INDEXES])
    assert later.applied == tuple(item.version for item in migrations([INDEXES]))
    assert set(_indexes(empty.conn, "fingerprint")) == EXPECTED_INDEXES["fingerprint"]


def test_apply_from_env_uses_the_bootstrap_variables(
    empty: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AOFF_DB_NAME", empty.settings.db_name)
    monkeypatch.setenv("AOFF_DB_HOST", empty.settings.db_host)
    monkeypatch.setenv("AOFF_DB_PORT", str(empty.settings.db_port))
    monkeypatch.setenv("AOFF_DB_USER", empty.settings.db_user)
    monkeypatch.setenv("AOFF_DB_PASSWORD", empty.settings.db_password.get_secret_value())

    report = apply_from_env(groups=[CORE])

    assert report.applied == tuple(item.version for item in migrations([CORE]))
    assert list(applied_versions(empty.conn)) == list(report.applied)


def test_changed_file_is_reported_as_drift(empty: Scratch) -> None:
    apply(empty.conn, groups=[CORE])
    empty.conn.execute(
        f"UPDATE {BOOKKEEPING_TABLE} SET checksum = 'anders' WHERE version = %s",
        ("0001_track.sql",),
    )

    with pytest.raises(MigrationDriftError, match="0001_track"):
        apply(empty.conn, groups=[CORE])


def test_connection_with_open_transaction_is_rejected(empty: Scratch) -> None:
    """Sonst waeren die Einzeltransaktionen nur Savepoints des Aufrufers."""
    with psycopg.connect(empty.settings.db_dsn().get_secret_value()) as conn:
        conn.execute("SELECT 1")  # oeffnet implizit eine Transaktion
        with pytest.raises(MigrationError, match="offene Transaktion"):
            apply(conn)
