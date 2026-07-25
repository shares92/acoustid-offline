"""MB-Client: Optionen, Fehleruebersetzung, Selfcheck-Diff, Ausfall (Phase 10).

Der Ausfall laesst sich ohne Docker pruefen: eine DSN auf einen Port, an dem
niemand lauscht, ist genau der Fall, den Invariante §8.7 meint. Getestet
wird, dass daraus **kein** Fehler nach oben dringt, sondern ein sauberes
:class:`~shared.mb.MbUnavailable` — und dass der Circuit-Breaker danach
sofort sperrt, statt jede weitere Anfrage warten zu lassen.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg_pool import PoolTimeout

from shared.config import Config
from shared.mb import client as mb_client
from shared.mb import queries
from shared.mb.client import (
    IDLE_IN_TRANSACTION_TIMEOUT_MS,
    STATEMENT_TIMEOUT_MS,
    MbClient,
    connection_options,
    translate_error,
)
from shared.mb.errors import MbQueryError, MbSchemaMismatch, MbUnavailable

#: Port 1 ist reserviert; ein Verbindungsversuch scheitert sofort.
DEAD_DSN = "host=127.0.0.1 port=1 dbname=musicbrainz_db user=acoustid_ro connect_timeout=1"


# --- Verbindungsoptionen ----------------------------------------------------


def test_connection_options_carry_every_safeguard() -> None:
    options = connection_options()
    assert f"-c statement_timeout={STATEMENT_TIMEOUT_MS}" in options
    assert "-c default_transaction_read_only=on" in options
    assert f"-c idle_in_transaction_session_timeout={IDLE_IN_TRANSACTION_TIMEOUT_MS}" in options
    assert f"-c search_path={queries.SCHEMA},public" in options


def test_the_client_is_optional() -> None:
    """Leerer ``mb.dsn`` heisst „keine Anbindung", nicht „Fehler"."""
    assert MbClient.from_config(Config()) is None

    config = Config.model_validate({"mb": {"dsn": DEAD_DSN}})
    client = MbClient.from_config(config)
    assert client is not None
    client.close()


# --- Fehleruebersetzung -----------------------------------------------------


def test_pool_timeout_becomes_unavailable() -> None:
    assert isinstance(translate_error(PoolTimeout("leer")), MbUnavailable)


def test_operational_errors_become_unavailable() -> None:
    assert isinstance(translate_error(psycopg.OperationalError("weg")), MbUnavailable)


def test_a_cancelled_statement_becomes_unavailable() -> None:
    """``statement_timeout`` ist ein Betriebs-, kein Programmfehler."""
    assert isinstance(translate_error(psycopg.errors.QueryCanceled("zu lang")), MbUnavailable)


@pytest.mark.parametrize(
    "error",
    [
        psycopg.errors.UndefinedTable("keine Tabelle"),
        psycopg.errors.UndefinedColumn("keine Spalte"),
        psycopg.errors.InsufficientPrivilege("kein Recht"),
        psycopg.errors.InvalidSchemaName("kein Schema"),
    ],
)
def test_schema_and_permission_errors_degrade(error: Exception) -> None:
    assert isinstance(translate_error(error), MbSchemaMismatch)


def test_everything_else_is_a_query_error() -> None:
    assert isinstance(translate_error(psycopg.errors.SyntaxError("bumm")), MbQueryError)
    assert isinstance(translate_error(ValueError("bumm")), MbQueryError)


def test_no_driver_exception_survives_the_translation() -> None:
    for error in (psycopg.OperationalError("a"), psycopg.errors.SyntaxError("b")):
        translated = translate_error(error)
        assert not isinstance(translated, psycopg.Error)


# --- Selfcheck-Diff ---------------------------------------------------------


def full_catalog() -> dict[str, frozenset[str]]:
    """Ein Spiegel, der genau die Erwartung erfuellt."""
    catalog = dict(queries.EXPECTED_COLUMNS)
    catalog[queries.RELEASE_EVENT_VIEW] = frozenset({"release", "country", "date_year"})
    return catalog


def test_a_complete_mirror_has_no_missing_columns() -> None:
    assert mb_client._missing_columns(full_catalog()) == []


def test_additional_columns_are_no_mismatch() -> None:
    """Die MB-Schema-Aenderungen waren bisher rein additiv."""
    catalog = full_catalog()
    catalog["recording"] = catalog["recording"] | {"video", "comment", "brandneu"}
    assert mb_client._missing_columns(catalog) == []


def test_a_missing_column_is_reported_with_its_relation() -> None:
    catalog = full_catalog()
    catalog["recording"] = catalog["recording"] - {"length"}
    assert mb_client._missing_columns(catalog) == ["recording.length"]


def test_a_missing_relation_reports_all_its_columns() -> None:
    catalog = full_catalog()
    del catalog["recording_gid_redirect"]
    assert mb_client._missing_columns(catalog) == [
        "recording_gid_redirect.gid",
        "recording_gid_redirect.new_id",
    ]


def test_the_view_is_not_part_of_the_expectation() -> None:
    """Fehlt sie, greift der Rueckfallweg — kein Mismatch."""
    catalog = full_catalog()
    del catalog[queries.RELEASE_EVENT_VIEW]
    assert mb_client._missing_columns(catalog) == []


# --- Staleness --------------------------------------------------------------


def test_a_fresh_mirror_is_not_stale() -> None:
    assert mb_client._staleness(datetime.now(UTC) - timedelta(hours=12)) is None


def test_the_warn_threshold_is_thirty_six_hours() -> None:
    stale = mb_client._staleness(datetime.now(UTC) - timedelta(hours=37))
    assert stale is not None
    assert stale.critical is False


def test_the_crit_threshold_is_seven_days() -> None:
    stale = mb_client._staleness(datetime.now(UTC) - timedelta(days=8))
    assert stale is not None
    assert stale.critical is True


def test_a_mirror_that_never_replicated_is_critical() -> None:
    stale = mb_client._staleness(None)
    assert stale is not None
    assert stale.critical is True


def test_a_naive_timestamp_is_read_as_utc() -> None:
    """Der Spiegel liefert je nach Spaltentyp mit oder ohne Zeitzone."""
    naive = (datetime.now(UTC) - timedelta(hours=1)).replace(tzinfo=None)
    assert mb_client._staleness(naive) is None


# --- Ausfall ----------------------------------------------------------------


@pytest.fixture
def dead_client() -> Iterator[MbClient]:
    """Client auf einen Port, an dem niemand lauscht."""
    client = MbClient(DEAD_DSN, pool_timeout_s=0.3)
    client.open()
    try:
        yield client
    finally:
        client.close()


def test_an_unreachable_mirror_raises_mb_unavailable(dead_client: MbClient) -> None:
    with pytest.raises(MbUnavailable), dead_client.session():  # pragma: no cover
        pass


def test_the_breaker_opens_after_three_failures(dead_client: MbClient) -> None:
    for _ in range(3):
        with pytest.raises(MbUnavailable), dead_client.session():  # pragma: no cover
            pass
    assert dead_client.breaker.is_open is True

    started = time.monotonic()
    with pytest.raises(MbUnavailable, match="gesperrt"), dead_client.session():  # pragma: no cover
        pass
    # Gesperrt heisst sofort — nicht noch einmal die Pool-Wartezeit.
    assert time.monotonic() - started < 0.2


def test_the_startup_check_never_raises(dead_client: MbClient) -> None:
    status = dead_client.startup_check()
    assert status.reachable is False
    assert status.schema_ok is False
    assert status.detail


def test_the_connection_test_never_raises(dead_client: MbClient) -> None:
    assert dead_client.check_connection().reachable is False


def test_lookup_metadata_reports_the_outage(dead_client: MbClient) -> None:
    with pytest.raises(MbUnavailable):
        dead_client.lookup_metadata(["11111111-1111-1111-1111-111111111111"])
