"""Der One-Shot-Job gegen eine echte Postgres (Phase 8).

Marker `integration`: laeuft nur mit erreichbarer Datenbank — Steuerung ueber
`--integration` bzw. `ACOUSTID_INTEGRATION_TESTS` (siehe conftest.py im
Repo-Wurzelverzeichnis), Zugang aus den `AOFF_DB_*`-Variablen.

Lokal::

    docker compose -f docker-compose.yml -f tests/docker-compose.test.yml up -d db
    AOFF_DB_HOST=127.0.0.1 uv run pytest importer/tests --integration=require

Die Quelle ist ein lokaler HTTP-Server (stdlib), der die Original-Fixtures
ausliefert — echtes Netz braucht hier niemand, echte Daten dagegen schon:
nur an ihnen zeigt sich, dass der ganze Weg stimmt.

Vier Fragen stehen im Mittelpunkt:

1. **Bootstrap-Reihenfolge** (Import-Regel 6): laeuft der Massenimport
   wirklich ohne Sekundaerindizes, und kommen sie danach?
2. **Plattenplatz-Guard** (§8.8): bricht der Lauf kontrolliert ab — und
   setzt der naechste sauber fort, ohne Duplikate und ohne Luecke?
3. **Exit-Codes und Report**: sagt der Job einer Maschine richtig, was war?
4. **Probelauf**: entstehen Messwerte und eine Hochrechnung?
"""

from __future__ import annotations

import gzip
import json
import shutil
import threading
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from acoustid_importer.diskguard import BYTES_PER_GB
from acoustid_importer.job import RunOptions, run
from acoustid_importer.report import ExitCode, JobMode, RunResult
from acoustid_importer.streams import IMPORT_ORDER, DeltaFile, Stream
from shared.config import Config
from shared.env import EnvSettings
from shared.fpindex import FpIndexClient

pytestmark = pytest.mark.integration

DAY = date(2026, 7, 22)
NEXT_DAY = DAY + timedelta(days=1)

#: Zeilenzahlen des vollstaendigen Tages laut Fixture-README (Phase 7).
EXPECTED_ROWS: Mapping[str, int] = {
    "track": 1_693,
    "meta": 5_122,
    "fingerprint": 2_214,
    "track_fingerprint": 2_214,
    "track_mbid": 1_039,
    "track_meta": 7_141,
    "track_puid": 0,
}
TOTAL_ROWS = sum(EXPECTED_ROWS.values())

#: Sekundaerindizes aus der Migrationsgruppe `indexes` (ARCHITECTURE §5.2).
SECONDARY_INDEXES = frozenset(
    {
        "track_idx_gid",
        "track_idx_new_id",
        "fingerprint_idx_track_id",
        "fingerprint_idx_incomplete",
        "fingerprint_idx_unindexed",
        "track_mbid_idx_track_id",
        "track_mbid_idx_mbid",
        "track_meta_idx_track_id",
    }
)


# --- Quelle -----------------------------------------------------------------


class _Source(ThreadingHTTPServer):
    """Testserver: liefert die Tagesdateien eines Verzeichnisses aus."""

    daemon_threads = True
    root: Path

    def handle_error(self, request: object, client_address: object) -> None:
        return


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _Source  # type: ignore[assignment]

    def log_message(self, *args: object) -> None:
        return

    def do_GET(self) -> None:  # Name kommt aus der stdlib
        """Liefert `index.json` eines Monats oder eine Tagesdatei aus."""
        parts = self.path.strip("/").split("/")
        name = parts[-1]
        if name == "index.json":
            month = parts[-2]
            entries = [
                {"name": item.name, "size": item.stat().st_size}
                for item in sorted(self.server.root.glob(f"{month}-*"))
            ]
            self._send(200, json.dumps(entries).encode(), "application/json")
            return
        path = self.server.root / name
        if not path.is_file():
            self._send(404, b"nicht da")
            return
        self._send(200, path.read_bytes(), "application/gzip")

    def _send(self, status: int, body: bytes, content_type: str = "text/plain") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def source(tmp_path: Path) -> Iterator[_Source]:
    """Laufender Testserver ueber einem leeren Quellverzeichnis."""
    server = _Source(("127.0.0.1", 0), _Handler)
    server.root = tmp_path / "quelle"
    server.root.mkdir()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def base_url(server: _Source) -> str:
    host, port = server.server_address[:2]
    return f"http://{host}:{port}"


def publish_day(server: _Source, dumps: Path, day: date = DAY) -> None:
    """Legt die sieben Original-Tagesdateien in die Quelle."""
    for stream in IMPORT_ORDER:
        name = DeltaFile(day, stream).name
        shutil.copyfile(dumps / name, server.root / name)


def publish_empty_day(server: _Source, day: date) -> None:
    """Sieben leere Tagesdateien — der Normalfall einer Daten-Flaute (§5.1)."""
    for stream in IMPORT_ORDER:
        path = server.root / DeltaFile(day, stream).name
        with gzip.open(path, "wb"):
            pass


# --- Umgebung ---------------------------------------------------------------


def settings_for(conn: psycopg.Connection, env: EnvSettings, dump_dir: Path) -> EnvSettings:
    """`AOFF_`-Umgebung, die auf die Wegwerf-Datenbank des Tests zeigt."""
    return env.model_copy(update={"db_name": conn.info.dbname, "dump_dir": dump_dir})


def options(server: _Source, **overrides: Any) -> RunOptions:
    """Lauf-Optionen fuer genau den einen Fixture-Tag."""
    defaults: dict[str, Any] = {
        "mode": JobMode.BOOTSTRAP,
        "first_day": DAY,
        "end_date": DAY,
        "base_url": base_url(server),
        "feed_index": False,
        "min_free_gb": 0,
        "today": date(2026, 7, 25),
    }
    return RunOptions(**{**defaults, **overrides})


def table_counts(conn: psycopg.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in ("track", "meta", "fingerprint", "track_mbid", "track_meta", "track_puid"):
        row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
        assert row is not None
        counts[table] = int(row[0])
    return counts


def index_names(conn: psycopg.Connection) -> set[str]:
    rows = conn.execute("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'").fetchall()
    return {row[0] for row in rows}


def finished_files(conn: psycopg.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT stream, day FROM import_state WHERE finished_at IS NOT NULL"
    ).fetchall()
    return {DeltaFile(day, Stream(name)).name for name, day in rows}


@dataclass
class _Usage:
    """Messfunktion mit vorgegebenem Verlauf: erst Platz, dann keiner mehr."""

    free_gbs: Sequence[float]
    calls: list[Path] = field(default_factory=list)

    def __call__(self, path: Path) -> _Usage:
        self.calls.append(path)
        return self

    @property
    def free(self) -> int:
        index = min(len(self.calls) - 1, len(self.free_gbs) - 1)
        return int(self.free_gbs[index] * BYTES_PER_GB)

    @property
    def total(self) -> int:
        return 1000 * BYTES_PER_GB


# --- Bootstrap --------------------------------------------------------------


def test_a_bootstrap_run_builds_the_schema_imports_the_day_and_then_the_indexes(
    empty_db: psycopg.Connection,
    env_settings: EnvSettings,
    source: _Source,
    dumps: Path,
    tmp_path: Path,
) -> None:
    """Der ganze Weg auf einer leeren Datenbank: core -> Massenimport -> indexes."""
    publish_day(source, dumps)
    assert index_names(empty_db) == set()

    report = run(
        options(source),
        settings=settings_for(empty_db, env_settings, tmp_path / "dumps"),
        config=Config(),
    )

    assert report.result is RunResult.OK
    assert report.exit_code is ExitCode.OK
    assert (report.files.planned, report.files.imported) == (7, 7)
    assert report.rows == TOTAL_ROWS
    assert report.rows_by_stream == dict(EXPECTED_ROWS)
    assert table_counts(empty_db) == {
        "track": 1_693,
        "meta": 5_122,
        "fingerprint": 2_214,
        "track_mbid": 1_039,
        "track_meta": 7_141,
        "track_puid": 0,
    }
    assert index_names(empty_db) >= SECONDARY_INDEXES


def test_the_mass_import_really_runs_without_the_secondary_indexes(
    empty_db: psycopg.Connection,
    env_settings: EnvSettings,
    source: _Source,
    dumps: Path,
    tmp_path: Path,
) -> None:
    """Import-Regel 6, direkt beobachtet: zwischen den Dateien gibt es keine."""
    publish_day(source, dumps)
    seen: list[set[str]] = []

    def watch() -> bool:
        seen.append(index_names(empty_db))
        return False

    report = run(
        options(source),
        settings=settings_for(empty_db, env_settings, tmp_path / "dumps"),
        config=Config(),
        stop=watch,
    )

    assert report.result is RunResult.OK
    assert len(seen) == 7
    assert all(names & SECONDARY_INDEXES == set() for names in seen)
    assert index_names(empty_db) >= SECONDARY_INDEXES


def test_an_aborted_bootstrap_keeps_what_it_has_and_leaves_the_indexes_for_later(
    empty_db: psycopg.Connection,
    env_settings: EnvSettings,
    source: _Source,
    dumps: Path,
    tmp_path: Path,
) -> None:
    """SIGTERM mitten im Lauf: Exit-Code 8, Stand resumierbar (§8.4)."""
    publish_day(source, dumps)
    calls = 0

    def stop() -> bool:
        nonlocal calls
        calls += 1
        return calls > 2

    settings = settings_for(empty_db, env_settings, tmp_path / "dumps")
    report = run(options(source), settings=settings, config=Config(), stop=stop)

    assert report.result is RunResult.ABORTED
    assert report.exit_code is ExitCode.ABORTED
    assert report.files.imported == 2
    assert finished_files(empty_db) == {
        "2026-07-22-track-update.jsonl.gz",
        "2026-07-22-meta-update.jsonl.gz",
    }
    # Der Indexbau gehoert ans Ende des Massenimports — nicht in die Mitte.
    assert index_names(empty_db) & SECONDARY_INDEXES == set()

    rest = run(options(source), settings=settings, config=Config())

    assert rest.result is RunResult.OK
    assert rest.files.imported == 5
    assert table_counts(empty_db)["fingerprint"] == 2_214
    assert len(finished_files(empty_db)) == 7


# --- Plattenplatz-Guard -----------------------------------------------------


def test_the_disk_guard_stops_the_run_with_its_own_exit_code(
    empty_db: psycopg.Connection,
    env_settings: EnvSettings,
    source: _Source,
    dumps: Path,
    tmp_path: Path,
) -> None:
    """Invariante §8.8: erst Platz pruefen, dann laden — sonst Abbruch."""
    publish_day(source, dumps)
    usage = _Usage([0.5])

    report = run(
        options(source, min_free_gb=50, disk_usage=usage),
        settings=settings_for(empty_db, env_settings, tmp_path / "dumps"),
        config=Config(),
    )

    assert report.result is RunResult.DISK_GUARD
    assert report.exit_code is ExitCode.DISK_GUARD
    assert report.files.imported == 0
    assert report.error is not None
    assert "update.min_free_gb" in report.error.message
    # Vor dem ersten Byte — und vor der ersten Migration: die Datenbank ist
    # unberuehrt geblieben (§8.8 „vor jedem Import").
    assert index_names(empty_db) == set()


def test_a_guard_stop_in_the_middle_leaves_a_resumable_state(
    empty_db: psycopg.Connection,
    env_settings: EnvSettings,
    source: _Source,
    dumps: Path,
    tmp_path: Path,
) -> None:
    """Die Platte laeuft waehrend des Laufs voll — kein Duplikat, keine Luecke."""
    publish_day(source, dumps)
    # Messaufrufe: 1 = Guard vor dem Lauf, 2 = derselbe Wert fuer den Report,
    # danach je Tagesdatei einer. Der fuenfte Aufruf sieht eine volle Platte —
    # also mitten im Tag, nach der dritten Datei.
    usage = _Usage([100.0, 100.0, 100.0, 100.0, 0.5])
    settings = settings_for(empty_db, env_settings, tmp_path / "dumps")
    guarded = options(source, min_free_gb=50, disk_usage=usage, guard_every_files=1)

    report = run(guarded, settings=settings, config=Config())

    assert report.result is RunResult.DISK_GUARD
    assert report.files.imported == 3
    assert finished_files(empty_db) == {
        "2026-07-22-track-update.jsonl.gz",
        "2026-07-22-meta-update.jsonl.gz",
        "2026-07-22-fingerprint-update.jsonl.gz",
    }
    assert table_counts(empty_db)["fingerprint"] == 2_214
    assert index_names(empty_db) & SECONDARY_INDEXES == set()

    # Aufgeraeumt, naechster Lauf: er setzt fort, statt neu anzufangen.
    rest = run(options(source), settings=settings, config=Config())

    assert rest.result is RunResult.OK
    assert rest.files.imported == 4
    assert rest.rows == (
        EXPECTED_ROWS["track_fingerprint"]
        + EXPECTED_ROWS["track_mbid"]
        + EXPECTED_ROWS["track_meta"]
        + EXPECTED_ROWS["track_puid"]
    )
    assert table_counts(empty_db) == {
        "track": 1_693,
        "meta": 5_122,
        "fingerprint": 2_214,
        "track_mbid": 1_039,
        "track_meta": 7_141,
        "track_puid": 0,
    }
    assert len(finished_files(empty_db)) == 7
    assert index_names(empty_db) >= SECONDARY_INDEXES


# --- Probelauf --------------------------------------------------------------


def test_the_probe_run_measures_and_projects_onto_the_full_history(
    empty_db: psycopg.Connection,
    env_settings: EnvSettings,
    source: _Source,
    dumps: Path,
    tmp_path: Path,
) -> None:
    """Der Zweck des begrenzten Laufs: Zahlen fuer die Bootstrap-Planung."""
    publish_day(source, dumps)
    gz_bytes = sum((dumps / DeltaFile(DAY, stream).name).stat().st_size for stream in IMPORT_ORDER)

    report = run(
        options(source, total_gz_bytes=414 * 10**9),
        settings=settings_for(empty_db, env_settings, tmp_path / "dumps"),
        config=Config(),
    )

    projection = report.projection
    assert projection is not None
    assert projection.measured_gz_bytes == gz_bytes == report.gz_bytes
    assert projection.throughput_gz_bytes_s is not None
    assert projection.throughput_gz_bytes_s > 0
    assert projection.estimated_total_hours is not None
    assert projection.estimated_total_hours > 0
    assert projection.measured_db_bytes is not None
    assert projection.measured_db_bytes > 0
    assert projection.estimated_db_bytes is not None
    assert projection.estimated_db_bytes > projection.measured_db_bytes

    measurements = report.measurements
    assert measurements["db_after"]["total_bytes"] > measurements["db_before"]["total_bytes"]
    assert measurements["db_after"]["tables"]["fingerprint"] > 0
    # Auch bei abgeschaltetem Guard (min_free_gb 0) steht der Platz im Report.
    assert measurements["disk_before"]["min_free_bytes"] == 0
    assert measurements["disk_after"]["free_bytes"] > 0
    assert "Hochrechnung Vollimport" in report.summary()


# --- Taeglicher Lauf --------------------------------------------------------


def test_an_update_run_uses_the_migrated_schema_and_no_bulk_mode(
    db: psycopg.Connection,
    env_settings: EnvSettings,
    source: _Source,
    dumps: Path,
    tmp_path: Path,
) -> None:
    publish_day(source, dumps)
    update = options(source, mode=JobMode.UPDATE)

    assert not update.use_bulk
    report = run(
        update,
        settings=settings_for(db, env_settings, tmp_path / "dumps"),
        config=Config(),
    )

    assert report.result is RunResult.OK
    assert report.mode is JobMode.UPDATE
    assert report.rows == TOTAL_ROWS
    assert index_names(db) >= SECONDARY_INDEXES


def test_a_second_run_finds_nothing_to_do(
    db: psycopg.Connection,
    env_settings: EnvSettings,
    source: _Source,
    dumps: Path,
    tmp_path: Path,
) -> None:
    """Idempotenz: der Waechter darf jeden Tag anstossen, auch zweimal."""
    publish_day(source, dumps)
    settings = settings_for(db, env_settings, tmp_path / "dumps")
    update = options(source, mode=JobMode.UPDATE)

    run(update, settings=settings, config=Config())
    again = run(update, settings=settings, config=Config())

    assert again.result is RunResult.OK
    assert (again.files.planned, again.files.imported, again.rows) == (0, 0, 0)
    assert table_counts(db)["track"] == 1_693


def test_an_empty_day_is_imported_like_any_other(
    db: psycopg.Connection,
    env_settings: EnvSettings,
    source: _Source,
    dumps: Path,
    tmp_path: Path,
) -> None:
    """23-Byte-gz ist regulaer (§5.1) — sie schliesst den Tag in `import_state`."""
    publish_day(source, dumps)
    publish_empty_day(source, NEXT_DAY)

    report = run(
        options(source, mode=JobMode.UPDATE, end_date=NEXT_DAY),
        settings=settings_for(db, env_settings, tmp_path / "dumps"),
        config=Config(),
    )

    assert report.result is RunResult.OK
    assert report.files.imported == 14
    assert report.files.empty == 8  # sieben leere Tage plus track_puid vom 22.
    assert report.last_day == NEXT_DAY
    assert len(finished_files(db)) == 14


# --- Fehlerwege -------------------------------------------------------------


def test_a_gap_in_the_history_stops_the_run_with_its_own_exit_code(
    db: psycopg.Connection,
    env_settings: EnvSettings,
    source: _Source,
    tmp_path: Path,
) -> None:
    """Import-Regel 5: fehlende Tage werden gemeldet, nie automatisch geholt."""
    publish_empty_day(source, NEXT_DAY)
    for stream in IMPORT_ORDER:
        db.execute(
            "INSERT INTO import_state (stream, day, file_name, finished_at) "
            "VALUES (%s, %s, %s, now())",
            (stream.value, NEXT_DAY, DeltaFile(NEXT_DAY, stream).name),
        )

    report = run(
        options(source, mode=JobMode.UPDATE, end_date=NEXT_DAY),
        settings=settings_for(db, env_settings, tmp_path / "dumps"),
        config=Config(),
    )

    assert report.result is RunResult.GAPS
    assert report.exit_code is ExitCode.GAPS
    assert len(report.gaps) == 7
    assert "2026-07-22-track-update.jsonl.gz" in report.gaps
    assert report.files.imported == 0


def test_a_missing_file_upstream_ends_as_a_download_failure(
    empty_db: psycopg.Connection,
    env_settings: EnvSettings,
    source: _Source,
    dumps: Path,
    tmp_path: Path,
) -> None:
    """Zur Ladezeit bemerkte Luecke — ein Wiederholen wuerde nicht helfen."""
    publish_day(source, dumps)
    (source.root / DeltaFile(DAY, Stream.TRACK_MBID).name).unlink()

    report = run(
        options(source),
        settings=settings_for(empty_db, env_settings, tmp_path / "dumps"),
        config=Config(),
    )

    assert report.result is RunResult.DOWNLOAD_FAILED
    assert report.exit_code is ExitCode.DOWNLOAD
    assert report.error is not None
    assert "track_mbid" in report.error.message
    # Was vorher durchlief, bleibt committet — der naechste Lauf setzt dort an.
    assert report.files.imported == 4
    assert len(finished_files(empty_db)) == 4


# --- Arbeitsverzeichnis -----------------------------------------------------


def test_imported_day_files_are_removed_to_keep_the_disk_free(
    empty_db: psycopg.Connection,
    env_settings: EnvSettings,
    source: _Source,
    dumps: Path,
    tmp_path: Path,
) -> None:
    """414 GB Bootstrap-Dateien aufzuheben, waere teuer und nutzlos."""
    publish_day(source, dumps)
    dump_dir = tmp_path / "dumps"

    run(
        options(source),
        settings=settings_for(empty_db, env_settings, dump_dir),
        config=Config(),
    )

    assert sorted(item.name for item in dump_dir.iterdir()) == []


def test_the_day_files_can_be_kept_for_a_closer_look(
    empty_db: psycopg.Connection,
    env_settings: EnvSettings,
    source: _Source,
    dumps: Path,
    tmp_path: Path,
) -> None:
    publish_day(source, dumps)
    dump_dir = tmp_path / "dumps"

    run(
        options(source, keep_dumps=True),
        settings=settings_for(empty_db, env_settings, dump_dir),
        config=Config(),
    )

    assert len(list(dump_dir.iterdir())) == 7


# --- Mit Suchindex ----------------------------------------------------------


@pytest.fixture
def index_settings(env_settings: EnvSettings) -> Iterator[EnvSettings]:
    """Eigener, frischer Indexname je Test; wird danach wieder geloescht."""
    settings = env_settings.model_copy(update={"index_name": f"pytest{uuid4().hex}"})
    try:
        yield settings
    finally:
        with FpIndexClient.from_env(settings) as client:
            client.delete_index()


@pytest.mark.db
@pytest.mark.index
def test_a_bootstrap_run_also_fills_the_search_index(
    empty_db: psycopg.Connection,
    index_settings: EnvSettings,
    source: _Source,
    dumps: Path,
    tmp_path: Path,
) -> None:
    """Der ganze Weg bis zum Ende: Delta -> Postgres -> acoustid-index."""
    publish_day(source, dumps)
    settings = settings_for(empty_db, index_settings, tmp_path / "dumps")

    report = run(options(source, feed_index=True), settings=settings, config=Config())

    assert report.result is RunResult.OK
    assert report.index_feed is not None
    assert report.index_feed["documents"] == 2_214
    assert report.index_feed["incomplete"] == 0
    assert report.measurements["index_after"]["documents"] == 2_214
    assert report.measurements["index_before"]["documents"] == 0

    projection = report.projection
    assert projection is not None
    assert projection.measured_index_documents == 2_214
    assert projection.estimated_index_documents is not None
    assert projection.estimated_index_documents > 2_214

    row = empty_db.execute("SELECT count(*) FROM fingerprint WHERE indexed_at IS NULL").fetchone()
    assert row is not None and row[0] == 0


@pytest.mark.db
@pytest.mark.index
def test_an_aborted_run_leaves_the_index_untouched(
    empty_db: psycopg.Connection,
    index_settings: EnvSettings,
    source: _Source,
    dumps: Path,
    tmp_path: Path,
) -> None:
    """Nach einem Abbruch wird nicht gefuettert — der naechste Lauf holt es nach."""
    publish_day(source, dumps)
    settings = settings_for(empty_db, index_settings, tmp_path / "dumps")

    report = run(
        options(source, feed_index=True),
        settings=settings,
        config=Config(),
        stop=lambda: True,
    )

    assert report.result is RunResult.ABORTED
    assert report.index_feed is None
    assert report.files.imported == 0
