"""Transaktionaler DB-Import gegen eine echte Postgres (ARCHITECTURE §5.2, §8.3/§8.4).

Marker `integration`: laeuft nur mit erreichbarer Datenbank — Steuerung ueber
`--integration` bzw. `ACOUSTID_INTEGRATION_TESTS` (siehe conftest.py im
Repo-Wurzelverzeichnis), Zugang aus den `MMO_DB_*`-Variablen.

Lokal::

    docker compose -f docker-compose.yml -f tests/docker-compose.test.yml up -d db
    MMO_DB_HOST=127.0.0.1 uv run pytest importer/tests --integration=require

Zwei Sorten Eingabedaten, mit Absicht:

* **Original-Fixtures** (`tests/fixtures/acoustid-dumps/`, nicht im Repo —
  `fetch_fixtures.py`) fuer die Frage „landet ein echter Tag vollstaendig und
  richtig in den sieben Tabellen?". Erwartungswerte sind die Zeilenzahlen der
  Fixture-README; die Dateien sind upstream unveraenderlich.
* **Synthetische Tagesdateien** fuer die Faelle, die keine Fixture belegt:
  Reaktivierung ueber zwei Tage (`disabled` fehlt wieder), das Treffen der
  beiden Fingerprint-Stroeme in beiden Reihenfolgen, eine kaputte Zeile und
  der Wiederanlauf nach einem Abbruch.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date, timedelta
from pathlib import Path

import psycopg
import pytest

from acoustid_importer import state
from acoustid_importer.dbimport import FileImport, import_file, import_files
from acoustid_importer.errors import DbImportError, ParseError
from acoustid_importer.streams import IMPORT_ORDER, DeltaFile, Stream

pytestmark = pytest.mark.integration

DAY = date(2026, 7, 22)
NEXT_DAY = date(2026, 7, 23)

#: Zeilenzahlen des vollstaendigen Tages laut Fixture-README.
EXPECTED_ROWS = {
    "2026-07-22-track-update.jsonl.gz": 1_693,
    "2026-07-22-meta-update.jsonl.gz": 5_122,
    "2026-07-22-fingerprint-update.jsonl.gz": 2_214,
    "2026-07-22-track_fingerprint-update.jsonl.gz": 2_214,
    "2026-07-22-track_mbid-update.jsonl.gz": 1_039,
    "2026-07-22-track_meta-update.jsonl.gz": 7_141,
    "2026-07-22-track_puid-update.jsonl.gz": 0,
}

STAMP = "2026-07-22T12:00:00+00:00"
GID = "1c4d2b2b-0000-4000-8000-00000000000{}"


def count(conn: psycopg.Connection, table: str, where: str = "TRUE") -> int:
    """Zeilenzahl einer Tabelle; `table`/`where` sind hier immer Konstanten."""
    row = conn.execute(f"SELECT count(*) FROM {table} WHERE {where}").fetchone()
    assert row is not None
    return int(row[0])


def synthetic_day(
    write_delta: Callable[..., Path],
    day: date,
    rows: dict[Stream, list[dict[str, object]]] | None = None,
    *,
    broken: Stream | None = None,
) -> list[Path]:
    """Schreibt alle sieben Tagesdateien eines Tages, per Vorgabe leer.

    Leere Dateien sind der Normalfall der Quelle (§5.1) — so entsteht ein
    vollstaendiger Tag, ueber den die Arbeitsliste sauber rechnen kann.
    """
    content = rows or {}
    paths: list[Path] = []
    for stream in IMPORT_ORDER:
        name = DeltaFile(day, stream).name
        if stream is broken:
            paths.append(write_delta(name, raw_lines=['{"id": 1, "kaputt']))
            continue
        paths.append(write_delta(name, content.get(stream, [])))
    return paths


def by_name(results: Iterable[FileImport]) -> dict[str, FileImport]:
    return {result.file.name: result for result in results}


# --- Ein echter Tag ---------------------------------------------------------


@pytest.fixture(scope="module")
def imported(module_db: psycopg.Connection, full_day: tuple[Path, ...]) -> dict[str, FileImport]:
    """Der 22.07.2026 vollstaendig eingespielt — einmal je Modul."""
    return by_name(import_files(module_db, full_day))


def test_every_stream_of_a_real_day_reports_the_documented_row_count(
    imported: dict[str, FileImport],
) -> None:
    assert {name: result.rows for name, result in imported.items()} == EXPECTED_ROWS


def test_the_rows_arrive_in_the_seven_tables(
    module_db: psycopg.Connection, imported: dict[str, FileImport]
) -> None:
    assert count(module_db, "track") == 1_693
    assert count(module_db, "meta") == 5_122
    assert count(module_db, "track_mbid") == 1_039
    assert count(module_db, "track_meta") == 7_141
    assert count(module_db, "track_puid") == 0
    # Beide Fingerprint-Stroeme beschreiben dieselben 2.214 Zeilen.
    assert count(module_db, "fingerprint") == 2_214


def test_the_two_fingerprint_streams_meet_in_one_complete_row(
    module_db: psycopg.Connection, imported: dict[str, FileImport]
) -> None:
    """Nach beiden Stroemen ist keine Zeile mehr unvollstaendig (§5.2)."""
    assert count(module_db, "fingerprint", "fingerprint IS NULL OR track_id IS NULL") == 0
    row = module_db.execute(
        "SELECT id, array_length(fingerprint, 1), length, track_id, submission_count, "
        "created, updated, indexed_at FROM fingerprint ORDER BY id LIMIT 1"
    ).fetchone()
    assert row is not None
    fp_id, vector_length, length, track_id, submissions, created, updated, indexed_at = row
    assert fp_id == 104_076_452
    assert vector_length and vector_length > 100
    assert length > 0
    assert track_id > 0
    assert submissions >= 0
    assert created is not None
    assert updated is None  # dieser Tag liefert kein `updated`
    assert indexed_at is None  # der Index-Feed laeuft getrennt


def test_the_bookkeeping_columns_carry_the_source_day(
    module_db: psycopg.Connection, imported: dict[str, FileImport]
) -> None:
    for table in ("track", "meta", "fingerprint", "track_mbid", "track_meta"):
        assert count(module_db, table, "src_day <> DATE '2026-07-22'") == 0
        assert count(module_db, table, "imported_at IS NULL") == 0


def test_a_disabled_mbid_stays_disabled(
    module_db: psycopg.Connection, imported: dict[str, FileImport]
) -> None:
    """Die neun `disabled: true`-Zeilen des Tages (Fixture-README)."""
    assert count(module_db, "track_mbid", "disabled") == 9


def test_values_survive_the_roundtrip(
    module_db: psycopg.Connection, imported: dict[str, FileImport]
) -> None:
    row = module_db.execute(
        "SELECT gid, new_id, created, updated FROM track ORDER BY id LIMIT 1"
    ).fetchone()
    assert row is not None
    gid, new_id, created, updated = row
    assert str(gid)
    assert new_id is None  # dieser Tag liefert weder new_id noch updated
    assert updated is None
    assert created.year == 2026


def test_import_state_holds_one_finished_row_per_stream(
    module_db: psycopg.Connection, imported: dict[str, FileImport]
) -> None:
    for stream in IMPORT_ORDER:
        file = DeltaFile(DAY, stream)
        found = state.state_for(module_db, file)
        assert found is not None, file.name
        assert found.done
        assert found.file_name == file.name
        assert found.row_count == EXPECTED_ROWS[file.name]
        assert found.file_size == imported[file.name].file_size
        assert found.duration_s is not None
        assert found.duration_s >= 0


def test_the_worklist_sees_the_day_as_done(
    module_db: psycopg.Connection, imported: dict[str, FileImport]
) -> None:
    """Die Anbindung der Phase-6-Rechnung an die Buchfuehrung."""
    assert state.imported_days(module_db) == {stream: frozenset({DAY}) for stream in IMPORT_ORDER}

    plan = state.plan(module_db, today=NEXT_DAY + timedelta(days=1), first_day=DAY)
    assert plan.gaps == ()
    assert [item.name for item in plan.files] == [
        DeltaFile(NEXT_DAY, stream).name for stream in IMPORT_ORDER
    ]


def test_an_empty_day_file_is_a_regular_day(
    module_db: psycopg.Connection, imported: dict[str, FileImport]
) -> None:
    """23 Byte gz, null Zeilen — erledigt, keine Luecke (§5.1)."""
    result = imported["2026-07-22-track_puid-update.jsonl.gz"]
    assert result.rows == 0
    assert result.empty
    found = state.state_for(module_db, DeltaFile(DAY, Stream.TRACK_PUID))
    assert found is not None
    assert found.done
    assert found.row_count == 0


# --- Wiederholung und Idempotenz -------------------------------------------


def test_a_finished_file_is_skipped_on_the_next_run(db: psycopg.Connection, dumps: Path) -> None:
    path = dumps / "2026-07-22-track-update.jsonl.gz"
    first = import_file(db, path)
    assert first.rows == 1_693
    assert not first.skipped

    second = import_file(db, path)
    assert second.skipped
    assert second.rows == 0
    assert count(db, "track") == 1_693


def test_importing_the_same_file_twice_changes_nothing(db: psycopg.Connection, dumps: Path) -> None:
    """Jede Zeile ist ein Upsert — ein zweiter Lauf ist folgenlos."""
    path = dumps / "2026-07-22-track_mbid-update.jsonl.gz"
    import_file(db, path)
    before = db.execute(
        "SELECT id, track_id, mbid, submission_count, disabled, created, updated, src_day "
        "FROM track_mbid ORDER BY id"
    ).fetchall()

    again = import_file(db, path, skip_done=False)
    assert again.rows == 1_039
    after = db.execute(
        "SELECT id, track_id, mbid, submission_count, disabled, created, updated, src_day "
        "FROM track_mbid ORDER BY id"
    ).fetchall()
    assert after == before
    assert count(db, "track_mbid") == 1_039


def test_the_old_escaping_epoch_imports_completely(db: psycopg.Connection, dumps: Path) -> None:
    """Der erste Tag der Historie — nur mit COPY-Unescape lesbar (§5.1)."""
    path = dumps / "2011-08-19-meta-update.jsonl.gz"
    if not path.exists():
        pytest.skip("Fixture 2011-08-19-meta-update fehlt")
    result = import_file(db, path)
    assert result.rows == 11_677
    assert count(db, "meta") == 11_677
    assert count(db, "meta", "src_day = DATE '2011-08-19'") == 11_677


def test_the_old_epoch_values_arrive_unescaped_in_the_database(
    db: psycopg.Connection, dumps: Path
) -> None:
    r"""Der Durchstich der Alt-Epoche bis in die Spalte (Phase 7, Task-Chip).

    Die Zeilenzahl oben belegt nur, dass jede Zeile *lesbar* war. Hier steht
    der Wert selbst: in der Datei liegt ``(12\\" Remix)`` — ueber dem JSON
    liegt das Text-Escaping von ``COPY … TO STDOUT``, das jeden Backslash
    verdoppelt. Ohne Unescape waere die Zeile kein gueltiges JSON gewesen;
    mit ihm muss in der Spalte ein **echtes** Anfuehrungszeichen und
    **kein** Backslash stehen — sonst haette der Parser die Ebene zwar
    entfernt, aber der Wert waere trotzdem verfaelscht.

    Die Gegenprobe steht im Parser-Test (`test_unescape_copy_text_only_halves_backslashes`);
    dieser Test haelt fest, dass zwischen Parser und Spalte niemand mehr
    etwas daran aendert.
    """
    path = dumps / "2011-08-19-meta-update.jsonl.gz"
    if not path.exists():
        pytest.skip("Fixture 2011-08-19-meta-update fehlt")
    import_file(db, path)

    row = db.execute(
        "SELECT track, artist, album, track_no, disc_no, year, created, src_day "
        "FROM meta WHERE id = 153238"
    ).fetchone()
    assert row is not None
    track, artist, album, track_no, disc_no, year, created, src_day = row
    assert track == 'Viva Las Vegas (12" Remix)'
    assert "\\" not in track
    assert artist == "ZZ Top"
    # `&` und `,` gehen unveraendert durch — das Escaping betraf nur Backslashes.
    assert album == "Chrome, Smoke & BBQ"
    assert (track_no, disc_no, year) == (14, 4, 2003)
    assert created is not None and created.year == 2011
    assert src_day == date(2011, 8, 19)

    # Und keine einzige Zeile des Tages traegt noch einen Backslash aus dem
    # COPY-Escaping (die Datei enthaelt keine echten Backslashes in Werten).
    assert (
        count(
            db,
            "meta",
            "track LIKE '%\\\\%' OR artist LIKE '%\\\\%' "
            "OR album LIKE '%\\\\%' OR album_artist LIKE '%\\\\%'",
        )
        == 0
    )


# --- Upsert-Semantik ueber zwei Tage ---------------------------------------


def test_a_missing_disabled_key_reactivates_the_mbid(
    db: psycopg.Connection, write_delta: Callable[..., Path]
) -> None:
    """Die Reaktivierungs-Falle aus §5.1 — Import-Regel 3."""
    disabled_row = {
        "id": 1,
        "track_id": 10,
        "mbid": GID.format(1),
        "submission_count": 3,
        "disabled": True,
        "created": STAMP,
    }
    import_file(db, write_delta(DeltaFile(DAY, Stream.TRACK_MBID).name, [disabled_row]))
    assert count(db, "track_mbid", "disabled") == 1

    # Am naechsten Tag fehlt der Schluessel — das heisst false, nicht "wie gehabt".
    revived = {key: value for key, value in disabled_row.items() if key != "disabled"}
    import_file(db, write_delta(DeltaFile(NEXT_DAY, Stream.TRACK_MBID).name, [revived]))
    assert count(db, "track_mbid", "disabled") == 0
    assert count(db, "track_mbid") == 1


def test_src_day_marks_the_day_of_the_last_application(
    db: psycopg.Connection, write_delta: Callable[..., Path]
) -> None:
    row = {"id": 1, "gid": GID.format(1), "created": STAMP}
    import_file(db, write_delta(DeltaFile(DAY, Stream.TRACK).name, [row]))
    import_file(
        db,
        write_delta(DeltaFile(NEXT_DAY, Stream.TRACK).name, [{**row, "updated": STAMP}]),
    )
    found = db.execute("SELECT src_day, updated FROM track WHERE id = 1").fetchone()
    assert found is not None
    assert found[0] == NEXT_DAY
    assert found[1] is not None


@pytest.mark.parametrize("vector_first", [True, False], ids=["vektor-zuerst", "zuordnung-zuerst"])
def test_the_fingerprint_streams_never_erase_each_other(
    db: psycopg.Connection, write_delta: Callable[..., Path], vector_first: bool
) -> None:
    """Import-Regel 2: disjunkte DO-UPDATE-Spalten, `created` nur gefuellt."""
    vector = write_delta(
        DeltaFile(DAY, Stream.FINGERPRINT).name,
        [{"id": 42, "fingerprint": [1, -2, 3], "length": 210, "created": STAMP}],
    )
    mapping = write_delta(
        DeltaFile(DAY, Stream.TRACK_FINGERPRINT).name,
        [
            {
                "id": 42,
                "track_id": 7,
                "fingerprint_id": 42,
                "submission_count": 5,
                "created": STAMP,
                "updated": STAMP,
            }
        ],
    )
    order = (vector, mapping) if vector_first else (mapping, vector)
    for path in order:
        import_file(db, path)

    row = db.execute(
        "SELECT fingerprint, length, track_id, submission_count, created, updated "
        "FROM fingerprint WHERE id = 42"
    ).fetchone()
    assert row is not None
    hashes, length, track_id, submissions, created, updated = row
    assert hashes == [1, -2, 3]
    assert length == 210
    assert track_id == 7
    assert submissions == 5
    assert created is not None  # per COALESCE gefuellt, nie ueberschrieben
    assert updated is not None  # kommt aus dem Zuordnungsstrom


def test_a_track_fingerprint_row_with_a_foreign_fingerprint_id_is_refused(
    db: psycopg.Connection, write_delta: Callable[..., Path]
) -> None:
    path = write_delta(
        DeltaFile(DAY, Stream.TRACK_FINGERPRINT).name,
        [
            {
                "id": 42,
                "track_id": 7,
                "fingerprint_id": 43,
                "submission_count": 5,
                "created": STAMP,
            }
        ],
    )
    with pytest.raises(DbImportError, match="fingerprint_id"):
        import_file(db, path)
    assert count(db, "fingerprint") == 0
    assert state.state_for(db, DeltaFile(DAY, Stream.TRACK_FINGERPRINT)) is None


# --- Abbruch und Wiederanlauf (Invariante §8.4) ----------------------------


def test_a_broken_line_rolls_back_the_whole_file(
    db: psycopg.Connection, write_delta: Callable[..., Path]
) -> None:
    """Import-Regel 4: Parse-Fehler = harter Abbruch der Datei-Transaktion."""
    path = write_delta(
        DeltaFile(DAY, Stream.TRACK).name,
        [{"id": index, "gid": GID.format(index), "created": STAMP} for index in range(1, 4)],
        raw_lines=['{"id": 4, "gid": '],
    )
    with pytest.raises(ParseError) as caught:
        import_file(db, path)
    assert caught.value.line_no == 4

    # Weder die drei guten Zeilen noch die Buchfuehrung haben ueberlebt.
    assert count(db, "track") == 0
    assert state.state_for(db, DeltaFile(DAY, Stream.TRACK)) is None


def test_the_next_run_continues_where_the_last_one_stopped(
    db: psycopg.Connection, write_delta: Callable[..., Path]
) -> None:
    """Der Resume-Fall: Abbruch mitten im Lauf, danach sauber weiter."""
    tracks = [{"id": 1, "gid": GID.format(1), "created": STAMP}]
    metas = [{"id": 1, "artist": "Wer", "created": STAMP}]
    paths = synthetic_day(
        write_delta,
        DAY,
        {Stream.TRACK: tracks, Stream.META: metas},
        broken=Stream.FINGERPRINT,
    )

    done: list[FileImport] = []
    with pytest.raises(ParseError):
        for result in import_files(db, paths):
            done.append(result)

    # Genau die beiden Dateien vor der kaputten sind drin.
    assert [item.file.stream for item in done] == [Stream.TRACK, Stream.META]
    assert state.imported_days(db) == {
        Stream.TRACK: frozenset({DAY}),
        Stream.META: frozenset({DAY}),
        **{stream: frozenset() for stream in IMPORT_ORDER[2:]},
    }

    # Die kaputte Datei kommt repariert nach (upstream sind die Dateien
    # unveraenderlich — hier steht sie fuer einen abgebrochenen Download).
    write_delta(
        DeltaFile(DAY, Stream.FINGERPRINT).name,
        [{"id": 9, "fingerprint": [1, 2, 3], "length": 30, "created": STAMP}],
    )

    plan = state.plan(db, today=NEXT_DAY, first_day=DAY)
    assert plan.gaps == ()
    assert [item.stream for item in plan.files] == list(IMPORT_ORDER[2:])

    directory = paths[0].parent
    second = by_name(import_files(db, (directory / item.name for item in plan.files)))
    assert all(not result.skipped for result in second.values())

    # Kein Duplikat, keine Luecke: jeder Tag genau einmal, alles erledigt.
    assert count(db, "track") == 1
    assert count(db, "meta") == 1
    assert count(db, "fingerprint") == 1
    assert state.imported_days(db) == {stream: frozenset({DAY}) for stream in IMPORT_ORDER}
    assert state.plan(db, today=NEXT_DAY, first_day=DAY).files == ()


def test_the_plan_can_hand_over_its_delta_files_with_the_paths(
    db: psycopg.Connection, write_delta: Callable[..., Path]
) -> None:
    """Der uebliche Aufruf aus dem Job-Rumpf: Pfad und `DeltaFile` als Paar.

    Der Dateiname muss dann nicht mehr zurueckgelesen werden — die
    Arbeitsliste weiss ohnehin, um welchen Tag und Strom es geht.
    """
    paths = synthetic_day(
        write_delta, DAY, {Stream.TRACK: [{"id": 1, "gid": GID.format(1), "created": STAMP}]}
    )
    plan = state.plan(db, today=NEXT_DAY, first_day=DAY)
    directory = paths[0].parent

    results = by_name(import_files(db, [(directory / item.name, item) for item in plan.files]))
    assert set(results) == {item.name for item in plan.files}
    assert results[DeltaFile(DAY, Stream.TRACK).name].rows == 1
    assert count(db, "track") == 1


def test_an_unknown_stream_in_the_bookkeeping_is_skipped_not_fatal(
    db: psycopg.Connection,
) -> None:
    """Ein aelterer/neuerer Schemastand darf den Lauf nicht abbrechen."""
    db.execute(
        "INSERT INTO import_state (stream, day, file_name, finished_at) "
        "VALUES ('erfunden', DATE '2026-07-22', 'x.jsonl.gz', now())"
    )
    assert state.imported_days(db) == {stream: frozenset() for stream in IMPORT_ORDER}


def test_a_rerun_of_the_whole_plan_skips_what_is_done(
    db: psycopg.Connection, write_delta: Callable[..., Path]
) -> None:
    """Ein wiederholter Lauf derselben Liste darf nichts doppelt schreiben."""
    paths = synthetic_day(
        write_delta, DAY, {Stream.TRACK: [{"id": 1, "gid": GID.format(1), "created": STAMP}]}
    )
    first = by_name(import_files(db, paths))
    assert sum(result.rows for result in first.values()) == 1

    second = by_name(import_files(db, paths))
    assert all(result.skipped for result in second.values())
    assert count(db, "track") == 1


def test_a_gap_in_the_past_is_reported_not_filled(
    db: psycopg.Connection, write_delta: Callable[..., Path]
) -> None:
    """Import-Regel 5: ein uebersprungener Tag ist eine Luecke, kein Nachholfall."""
    for path in synthetic_day(write_delta, DAY):
        import_file(db, path)
    later = DAY + timedelta(days=2)
    for path in synthetic_day(write_delta, later):
        import_file(db, path)

    plan = state.plan(db, today=later + timedelta(days=1), first_day=DAY)
    assert plan.files == ()
    assert {gap.day for gap in plan.gaps} == {DAY + timedelta(days=1)}
    assert len(plan.gaps) == len(IMPORT_ORDER)


# --- Verbindungszustand -----------------------------------------------------


def test_an_open_transaction_is_refused(
    service_db: psycopg.Connection, write_delta: Callable[..., Path]
) -> None:
    """Sonst waere die Datei-Transaktion nur ein Savepoint des Aufrufers."""
    service_db.execute("SELECT 1")  # oeffnet implizit eine Transaktion
    path = write_delta(DeltaFile(DAY, Stream.TRACK).name, [])
    with pytest.raises(DbImportError, match="offene Transaktion"):
        import_file(service_db, path)


def test_a_service_connection_without_autocommit_works(
    service_db: psycopg.Connection, write_delta: Callable[..., Path]
) -> None:
    """So verbindet sich ein Container: normale Verbindung, kein autocommit."""
    path = write_delta(
        DeltaFile(DAY, Stream.TRACK).name,
        [{"id": 1, "gid": GID.format(1), "created": STAMP}],
    )
    assert import_file(service_db, path).rows == 1

    # Der Beweis, dass die Datei-Transaktion wirklich committet wurde und
    # nicht bloss am offenen Commit des Aufrufers haengt: ein Rollback
    # danach darf nichts wegnehmen.
    service_db.rollback()
    assert count(service_db, "track") == 1
    assert state.is_done(service_db, DeltaFile(DAY, Stream.TRACK))


def test_batch_size_does_not_change_the_result(db: psycopg.Connection, dumps: Path) -> None:
    """Die Blockgroesse ist eine Stellschraube, kein Semantikschalter."""
    path = dumps / "2026-07-22-track-update.jsonl.gz"
    result = import_file(db, path, batch_rows=7)
    assert result.rows == 1_693
    assert result.batches == 242  # ceil(1693 / 7)
    assert count(db, "track") == 1_693

    with pytest.raises(ValueError, match="batch_rows"):
        import_file(db, path, batch_rows=0)
