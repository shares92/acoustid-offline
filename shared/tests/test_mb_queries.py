"""Die Regeln der MB-Abfrageschicht — ohne Datenbank pruefbar (Phase 10).

Der wichtigste Test hier ist :func:`test_only_one_module_knows_musicbrainz`:
er haelt die Kapselung fest, auf der ARCHITECTURE §5.4 aufbaut. Sobald ein
zweites Modul MusicBrainz-Tabellen kennt, ist das jaehrliche Schema-Update
kein Diff mehr, sondern eine Suche.

Der Rest prueft die Eigenschaften, die man in einer SQL-Zeichenkette leicht
verliert: Schema-Qualifizierung, explizite Cast-Angabe bei Array-Parametern
(LEARNINGS „psycopg3 schickt Python-Strings als *unknown*") und die
Kurzschluesse bei leerer Eingabe — die duerfen die Datenbank gar nicht erst
anfassen.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from shared.mb import queries

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Die Regel aus dem Phase-1-Bericht. Bewusst zusammengesetzt, damit dieser
#: Test nicht selbst darauf anschlaegt.
TABLE_PREFIX = queries.SCHEMA + "."

#: Verzeichnisse mit Produktivcode (Tests und Fixtures duerfen alles).
SOURCE_DIRS = ("shared/shared", "api/app", "importer/app", "watchdog/app")


def _source_files() -> list[Path]:
    found: list[Path] = []
    for directory in SOURCE_DIRS:
        found.extend(sorted((REPO_ROOT / directory).rglob("*.py")))
    return found


def test_only_one_module_knows_musicbrainz() -> None:
    """CI-Grep-Regel: ``musicbrainz.`` steht nur in ``mb/queries.py``."""
    allowed = REPO_ROOT / "shared/shared/mb/queries.py"
    offenders = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in _source_files()
        if path != allowed and TABLE_PREFIX in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], (
        "MusicBrainz-Tabellennamen gehoeren ausschliesslich in "
        "shared/shared/mb/queries.py (ARCHITECTURE §5.4)"
    )


def test_the_query_module_really_uses_the_prefix() -> None:
    """Gegenprobe: der Test oben waere sonst trivial gruen."""
    text = (REPO_ROOT / "shared/shared/mb/queries.py").read_text(encoding="utf-8")
    assert text.count(TABLE_PREFIX) > 10


# --- Eigenschaften der Anweisungen ------------------------------------------


def _statements() -> dict[str, str]:
    """Alle SQL-Konstanten des Moduls (Name -> Text)."""
    return {
        name: value
        for name, value in vars(queries).items()
        if name.startswith("_") and name.endswith("SQL") and isinstance(value, str)
    }


def test_every_statement_is_schema_qualified() -> None:
    for name, statement in _statements().items():
        if name == "_SELFCHECK_SQL":
            # Der Selfcheck liest den Systemkatalog, nicht das MB-Schema.
            assert "pg_catalog." in statement
            continue
        tables = re.findall(r"\b(?:FROM|JOIN)\s+(\S+)", statement)
        assert tables, name
        for table in tables:
            assert table.startswith(TABLE_PREFIX), f"{name}: {table} ist nicht schema-qualifiziert"


def test_array_parameters_carry_an_explicit_cast() -> None:
    """``= ANY(%s)`` ohne Cast ist der Klassiker aus den LEARNINGS."""
    for name, statement in _statements().items():
        for match in re.finditer(r"= ANY\((%\(\w+\)s)([^)]*)\)", statement):
            assert match.group(2).startswith("::"), f"{name}: {match.group(0)} ohne Cast"


def test_no_statement_selects_everything() -> None:
    for name, statement in _statements().items():
        assert "SELECT *" not in statement, name


def test_the_release_row_query_is_fully_ordered() -> None:
    """Ohne vollstaendige Sortierung waere die Kappung nicht reproduzierbar."""
    assert "ORDER BY r.gid, rel.id, m.position, t.position, t.id" in queries._RELEASE_ROWS_SQL
    assert "LIMIT %(limit)s" in queries._RELEASE_ROWS_SQL


def test_the_view_fallback_matches_the_view_definition() -> None:
    """Der Rueckfallweg vereinigt genau die beiden Basistabellen."""
    fallback = queries._RELEASE_EVENTS_UNION_SQL
    assert "release_country" in fallback
    assert "release_unknown_country" in fallback
    assert "UNION ALL" in fallback
    assert TABLE_PREFIX + queries.RELEASE_EVENT_VIEW not in fallback
    assert TABLE_PREFIX + queries.RELEASE_EVENT_VIEW in queries._RELEASE_EVENTS_VIEW_SQL


# --- Erwartungsliste --------------------------------------------------------


def test_expected_columns_cover_the_seventeen_relations() -> None:
    """Fallstricke des Berichts: die Sonderfaelle stehen wirklich drin."""
    expected = queries.EXPECTED_COLUMNS
    assert "recording_gid_redirect" in expected  # Redirect-Aufloesung
    assert "replication_control" in expected  # Schema-Guard + Staleness
    assert "release_unknown_country" in expected  # Rueckfall ohne View
    assert "iso_3166_1" in expected  # Laendercode statt Area-ID
    assert "track_count" in expected["medium"]  # inkl. Data-Tracks
    assert "child_order" in expected["release_group_secondary_type"]
    # `area` brauchen wir nicht (wir joinen iso_3166_1 direkt).
    assert "area" not in expected


def test_expected_columns_appear_in_the_statements() -> None:
    """Keine Erwartung ohne Abfrage — sonst waere der Selfcheck Theater."""
    text = "\n".join(_statements().values())
    for relation in queries.EXPECTED_COLUMNS:
        assert relation in text, f"{relation} wird nirgends abgefragt"


# --- Kurzschluesse ----------------------------------------------------------


class _RefusingConnection:
    """Verbindung, die jede Abfrage als Testfehler wertet."""

    def execute(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover - Fehlerfall
        raise AssertionError("leere Eingabe darf die Datenbank nicht anfassen")


@pytest.mark.parametrize(
    ("call", "empty"),
    [
        (queries.resolve_recording_redirects, {}),
        (queries.existing_recording_mbids, set()),
        (queries.recordings_by_mbids, {}),
        (queries.artist_credits, {}),
        (queries.release_counts, {}),
        (queries.release_events, {}),
        (queries.release_groups, {}),
        (queries.release_group_secondary_types, {}),
    ],
)
def test_empty_input_never_reaches_the_database(call: Any, empty: Any) -> None:
    assert call(_RefusingConnection(), []) == empty


def test_release_rows_short_circuit_on_empty_input() -> None:
    result = queries.recording_release_rows(_RefusingConnection(), [])
    assert result.rows == []
    assert result.truncated is False
