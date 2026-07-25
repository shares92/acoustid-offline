"""Bootstrap-Bulk-Modus gegen eine echte Postgres (§5.2 Regel 6).

Marker `integration`: laeuft nur mit erreichbarer Datenbank — Steuerung ueber
`--integration` bzw. `ACOUSTID_INTEGRATION_TESTS` (siehe conftest.py im
Repo-Wurzelverzeichnis).

Die Frage, um die es hier geht, ist eine Sicherheitsfrage: **werden die
unsicheren Einstellungen wirklich wieder zurueckgenommen?** Und zwar auch
dann, wenn der Lauf mitten im Massenimport an einem Fehler zerbricht. Ein
Mock kann das nicht beantworten — nur eine echte Sitzung, die man hinterher
befragt.
"""

from __future__ import annotations

import psycopg
import pytest

from acoustid_importer.bulk import (
    BULK_SETTINGS,
    INDEX_BUILD_SETTINGS,
    bulk_session,
    current_settings,
    flush_wal,
    session_settings,
)
from acoustid_importer.errors import BulkModeError
from shared.env import EnvSettings

pytestmark = pytest.mark.integration


def setting(conn: psycopg.Connection, name: str = "synchronous_commit") -> str:
    row = conn.execute(f"SHOW {name}").fetchone()
    assert row is not None
    return str(row[0])


@pytest.fixture
def scratch_dsn(env_settings: EnvSettings, db: psycopg.Connection) -> str:
    """Zugang zu **derselben** Wegwerf-Datenbank fuer eine zweite Sitzung."""
    return env_settings.model_copy(update={"db_name": db.info.dbname}).db_dsn().get_secret_value()


# --- Setzen und Zuruecknehmen ----------------------------------------------


def test_the_bulk_setting_is_active_inside_and_gone_afterwards(db: psycopg.Connection) -> None:
    before = setting(db)

    with bulk_session(db):
        assert setting(db) == "off"

    assert setting(db) == before


def test_the_setting_is_taken_back_even_when_the_import_fails(db: psycopg.Connection) -> None:
    """Der Fall, der zaehlt: der Massenimport zerbricht mittendrin."""
    before = setting(db)

    with pytest.raises(RuntimeError, match="Massenimport"), bulk_session(db):
        assert setting(db) == "off"
        raise RuntimeError("Massenimport gescheitert")

    assert setting(db) == before


def test_the_previous_value_is_restored_not_the_configured_default(db: psycopg.Connection) -> None:
    """`RESET` kaeme auf den Konfigurationswert zurueck, nicht auf diesen hier."""
    db.execute("SET synchronous_commit = local")

    with bulk_session(db):
        assert setting(db) == "off"

    assert setting(db) == "local"
    db.execute("RESET synchronous_commit")


def test_nothing_is_changed_persistently(db: psycopg.Connection, scratch_dsn: str) -> None:
    """Eine zweite Sitzung derselben Datenbank darf nichts davon merken.

    Genau das unterscheidet ``SET`` von ``ALTER SYSTEM``/``ALTER DATABASE``.
    """
    with bulk_session(db):
        assert setting(db) == "off"
        with psycopg.connect(scratch_dsn) as other:
            assert setting(other) != "off"


def test_a_dead_session_takes_its_settings_with_it(scratch_dsn: str) -> None:
    """Der eigentliche Schutz: unsichere Einstellungen ueberleben den Prozess nicht."""
    with psycopg.connect(scratch_dsn, autocommit=True) as conn, bulk_session(conn):
        assert setting(conn) == "off"
        conn.close()

    with psycopg.connect(scratch_dsn, autocommit=True) as fresh:
        assert setting(fresh) != "off"


def test_a_disabled_block_changes_nothing(db: psycopg.Connection) -> None:
    """Update-Laeufe gehen durch denselben Code, nur ohne Wirkung."""
    before = setting(db)
    with bulk_session(db, enabled=False) as previous:
        assert setting(db) == before
        assert previous == {}


def test_the_index_build_settings_are_session_scoped_too(db: psycopg.Connection) -> None:
    before = setting(db, "maintenance_work_mem")

    with session_settings(db, INDEX_BUILD_SETTINGS):
        assert setting(db, "maintenance_work_mem") == "1GB"

    assert setting(db, "maintenance_work_mem") == before


def test_the_documented_bulk_setting_is_the_only_one(db: psycopg.Connection) -> None:
    """Was hier steht, ist begruendet — fsync & Co. bleiben bewusst draussen."""
    assert dict(BULK_SETTINGS) == {"synchronous_commit": "off"}
    assert current_settings(db, BULK_SETTINGS).keys() == {"synchronous_commit"}


# --- Verweigerte Verbindungen ----------------------------------------------


def test_a_connection_without_autocommit_is_refused(service_db: psycopg.Connection) -> None:
    """Ein SET in einer spaeter zurueckgerollten Transaktion waere wieder weg."""
    with pytest.raises(BulkModeError, match="autocommit"), bulk_session(service_db):
        pass  # pragma: no cover - der Block wird nie betreten


def test_an_open_transaction_is_refused(service_db: psycopg.Connection) -> None:
    service_db.autocommit = True
    service_db.execute("BEGIN")
    try:
        with pytest.raises(BulkModeError, match="offene Transaktion"), bulk_session(service_db):
            pass  # pragma: no cover - der Block wird nie betreten
    finally:
        service_db.execute("ROLLBACK")


def test_an_unknown_setting_is_named(db: psycopg.Connection) -> None:
    unknown = {"gibt_es_nicht": "1"}
    with pytest.raises(BulkModeError, match="unbekannt"), session_settings(db, unknown):
        pass  # pragma: no cover - der Block wird nie betreten


# --- Abschluss --------------------------------------------------------------


def test_the_checkpoint_makes_the_bulk_import_durable(db: psycopg.Connection) -> None:
    assert flush_wal(db) is True
