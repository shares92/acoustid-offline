"""Migrationsdateien und Runner-API ohne Datenbank (ARCHITECTURE §5.2)."""

import hashlib
import logging
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from shared.db import (
    CORE,
    GROUPS,
    INDEXES,
    MigrationError,
    migrations,
    normalise_groups,
)
from shared.db.__main__ import main
from shared.models import SubmissionStatus

REPO_ROOT = Path(__file__).resolve().parents[2]

CORE_VERSIONS = (
    "0001_track.sql",
    "0002_fingerprint.sql",
    "0003_track_mbid.sql",
    "0004_meta.sql",
    "0005_track_meta.sql",
    "0006_track_puid.sql",
    "0007_import_state.sql",
    "0008_local_submission.sql",
)
INDEX_VERSIONS = (
    "0101_track.sql",
    "0102_fingerprint.sql",
    "0103_track_mbid.sql",
    "0104_track_meta.sql",
    "0105_local_submission.sql",
)

#: Die sieben Zieltabellen aus ARCHITECTURE §5.2 (plus `import_state`).
TABLES = (
    "track",
    "fingerprint",
    "track_mbid",
    "meta",
    "track_meta",
    "track_puid",
    "import_state",
)

#: Tabelle der eigenen Einreichungen (ARCHITECTURE §5.2 „Weitere Tabellen",
#: Spalten seit Phase 11). Steht bewusst getrennt: sie stammt nicht aus dem
#: Dump, und ihr DDL wandert erst mit dem Doku-Sweep der Phase in den
#: SQL-Block von §5.2.
LOCAL_TABLE = "local_submission"

ALL_TABLES = (*TABLES, LOCAL_TABLE)


def statements_of(sql_text: str) -> list[str]:
    """SQL ohne Kommentare und Formatierung, in Einzelanweisungen zerlegt."""
    without_comments = re.sub(r"--[^\n]*", "", sql_text)
    parts = (re.sub(r"\s+", " ", part).strip() for part in without_comments.split(";"))
    return [part for part in parts if part]


@pytest.fixture
def _restore_logging() -> Iterator[None]:
    """Der CLI-Einstiegspunkt richtet das Logging ein — danach zuruecksetzen."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


# --- Dateien und Reihenfolge -----------------------------------------------


def test_all_expected_files_are_packaged() -> None:
    assert tuple(item.version for item in migrations()) == CORE_VERSIONS + INDEX_VERSIONS


def test_groups_are_applied_core_first() -> None:
    assert GROUPS == (CORE, INDEXES)
    assert tuple(item.version for item in migrations([CORE])) == CORE_VERSIONS
    assert tuple(item.version for item in migrations([INDEXES])) == INDEX_VERSIONS


def test_numbers_rise_across_group_boundaries() -> None:
    """Nur so ergibt der Bootstrap-Weg dieselbe Reihenfolge wie ein Gesamtlauf."""
    numbers = [item.number for item in migrations()]
    assert numbers == sorted(numbers)
    assert len(set(numbers)) == len(numbers)
    assert max(item.number for item in migrations([CORE])) < min(
        item.number for item in migrations([INDEXES])
    )


def test_checksum_is_the_sha256_of_the_file() -> None:
    for item in migrations():
        assert item.checksum == hashlib.sha256(item.statements.encode("utf-8")).hexdigest()
        assert item.statements.strip()


def test_group_order_is_normalised_and_deduplicated() -> None:
    assert normalise_groups(None) == GROUPS
    assert normalise_groups([INDEXES, CORE]) == (CORE, INDEXES)
    assert normalise_groups([CORE, CORE]) == (CORE,)


def test_unknown_group_is_rejected_by_name() -> None:
    with pytest.raises(MigrationError, match="indizes"):
        migrations(["indizes"])


# --- Inhalt: exakt ARCHITECTURE §5.2 ---------------------------------------


def test_schema_matches_architecture_word_for_word() -> None:
    """Das DDL aus ARCHITECTURE §5.2 ist verbindlich — Anweisung fuer Anweisung.

    Die einzige zugelassene Abweichung ist ``local_submission``: §5.2 fuehrt
    die Tabelle bisher nur in „Weitere Tabellen" (Spalten werden in ihrer
    Phase festgelegt), das DDL wandert mit dem Doku-Sweep der Phase 11 in den
    SQL-Block. Sobald es dort steht, ist die Differenz leer und dieser Test
    zieht wieder ohne Ausnahme — deshalb wird sie **berechnet** und nicht
    einfach uebersprungen.
    """
    architecture = (REPO_ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
    block = re.search(r"```sql\n(.*?)```", architecture, flags=re.DOTALL)
    assert block is not None, "ARCHITECTURE.md enthaelt keinen SQL-Block"

    documented = sorted(statements_of(block[1]))
    packaged = sorted(
        statement for item in migrations() for statement in statements_of(item.statements)
    )
    missing = [statement for statement in documented if statement not in packaged]
    extra = [statement for statement in packaged if statement not in documented]
    assert missing == []
    assert all(LOCAL_TABLE in statement for statement in extra), extra


def test_every_table_is_created_exactly_once() -> None:
    created = [
        match[1]
        for item in migrations([CORE])
        for statement in statements_of(item.statements)
        if (match := re.match(r"CREATE TABLE (\w+)", statement))
    ]
    assert sorted(created) == sorted(ALL_TABLES)


def test_local_submission_carries_the_full_status_domain() -> None:
    """`new` -> `indexed` -> `forwarded` | `forward_failed` (§5.2, §8.9).

    Die Uebergaenge ab `forwarded` baut erst Phase 12 — die Domaene steht
    aber schon jetzt vollstaendig in der Tabelle, sonst braeuchte Phase 12
    eine Schema-Migration fuer einen Wert, der laengst entschieden ist.
    """
    body = " ".join(
        statement for item in migrations([CORE]) for statement in statements_of(item.statements)
    )
    assert "CHECK (status IN ('new', 'indexed', 'forwarded', 'forward_failed'))" in body
    assert {status.value for status in SubmissionStatus} == {
        "new",
        "indexed",
        "forwarded",
        "forward_failed",
    }


def test_the_local_track_id_sequence_stops_at_the_reserved_range() -> None:
    """Der Suchindex nimmt nur u32-Dokument-IDs (empirisch, Phase 11).

    Dokument-ID = 2^31 + `local_track_id`; die Sequenz darf deshalb nicht
    ueber 2^31-1 hinaus und schon gar nicht umlaufen — ein Umlauf wuerde
    lokale Einreichungen auf fremde Dokumente legen.
    """
    body = " ".join(
        statement for item in migrations([CORE]) for statement in statements_of(item.statements)
    )
    assert (
        "CREATE SEQUENCE local_submission_track_id_seq AS integer "
        "MINVALUE 1 MAXVALUE 2147483647 NO CYCLE" in body
    )


def test_schema_has_no_foreign_keys() -> None:
    """Bewusst keine FKs — Begruendung in ARCHITECTURE §5.2 und in 0001."""
    body = " ".join(
        statement for item in migrations() for statement in statements_of(item.statements)
    ).upper()
    assert "REFERENCES" not in body
    assert "FOREIGN KEY" not in body


def test_core_creates_tables_and_indexes_only_indexes() -> None:
    core_body = " ".join(
        statement for item in migrations([CORE]) for statement in statements_of(item.statements)
    ).upper()
    index_body = " ".join(
        statement for item in migrations([INDEXES]) for statement in statements_of(item.statements)
    ).upper()
    assert "CREATE INDEX" not in core_body
    assert "CREATE UNIQUE INDEX" not in core_body
    assert "CREATE TABLE" not in index_body
    assert index_body.count("CREATE INDEX") + index_body.count("CREATE UNIQUE INDEX") == 11


def test_lz4_compression_is_set_before_the_bulk_import() -> None:
    """`SET COMPRESSION` wirkt nur auf neue Werte — daher Gruppe `core`."""
    core_body = " ".join(
        statement for item in migrations([CORE]) for statement in statements_of(item.statements)
    )
    assert "ALTER TABLE fingerprint ALTER COLUMN fingerprint SET COMPRESSION lz4" in core_body


def test_partial_indexes_keep_their_predicates() -> None:
    index_body = " ".join(
        statement for item in migrations([INDEXES]) for statement in statements_of(item.statements)
    )
    assert "ON track (new_id) WHERE new_id IS NOT NULL" in index_body
    assert "WHERE fingerprint IS NULL OR track_id IS NULL" in index_body
    assert "WHERE indexed_at IS NULL" in index_body
    # Arbeitsvorrat der Submit-Indexierung (Muster `fingerprint_idx_unindexed`).
    assert "ON local_submission (id) WHERE status = 'new'" in index_body


# --- Kommandozeile ---------------------------------------------------------


def test_cli_lists_the_selected_group(
    capsys: pytest.CaptureFixture[str], _restore_logging: None
) -> None:
    assert main(["--list", "--groups", CORE]) == 0
    printed = capsys.readouterr().out
    assert all(version in printed for version in CORE_VERSIONS)
    assert INDEX_VERSIONS[0] not in printed


def test_cli_reports_an_unknown_group(
    capsys: pytest.CaptureFixture[str], _restore_logging: None
) -> None:
    assert main(["--groups", "quatsch"]) == 2
    assert "quatsch" in capsys.readouterr().err
