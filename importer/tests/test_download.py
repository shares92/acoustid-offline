"""Downloader gegen einen lokalen HTTP-Server (ARCHITECTURE §5.1).

**Kein echtes Netz.** Der Testserver (stdlib ``http.server``) spielt
data.acoustid.org nach: Monats-``index.json``, gzip-Dateien, Range-Requests
— und auf Wunsch die Stoerungen, die dort dokumentiert haeufig sind:
abbrechende Verbindungen, 5xx-Antworten, ignorierte Range-Wuensche, falsche
Groessen.

Gewartet wird nie wirklich: der Downloader bekommt eine Ersatz-Wartefunktion
und die Testfaelle pruefen die Backoff-Folge als Zahlenreihe.
"""

from __future__ import annotations

import gzip
import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from acoustid_importer.download import USER_AGENT, DeltaDownloader
from acoustid_importer.errors import DeltaNotFoundError, DownloadError, SizeMismatchError
from acoustid_importer.streams import DeltaFile, Stream
from shared.env import EnvSettings

DAY = date(2026, 7, 22)
FILE = DeltaFile(DAY, Stream.META)
OTHER = DeltaFile(DAY, Stream.TRACK)

#: Ein glaubwuerdiger, kleiner Dateiinhalt (gueltiges gzip-JSONL).
PAYLOAD = gzip.compress(
    b"".join(
        json.dumps({"id": index, "track": f"Titel {index}"}).encode() + b"\n"
        for index in range(1, 200)
    )
)
#: Die echte leere Tagesdatei: 23 Byte, wie der Go-Exporter sie schreibt
#: (``gzip.compress(b"")`` waeren 20). Inhalt ist ein leerer gzip-Strom.
EMPTY_PAYLOAD = bytes.fromhex("1f8b08000000000000ff010000ffff0000000000000000")


@dataclass
class Faults:
    """Stoerungen, die der Testserver einbauen soll."""

    #: Je Datei-Anfrage einer dieser Statuscodes (dann leerer Rumpf).
    statuses: list[int] = field(default_factory=list)
    #: Je Datei-Anfrage: nach so vielen Byte die Verbindung abbrechen.
    aborts: list[int] = field(default_factory=list)
    #: Jede Datei-Anfrage nach so vielen Byte abbrechen.
    always_abort_after: int | None = None
    #: Range-Wunsch ignorieren und mit 200 die ganze Datei senden.
    ignore_range: bool = False
    #: Fuer diese Monate gibt es kein ``index.json``.
    missing_months: set[str] = field(default_factory=set)
    #: Kaputte Antwort statt des Listings.
    broken_index: bool = False
    #: Nicht die echte Groesse ins Listing schreiben.
    index_sizes: dict[str, int] = field(default_factory=dict)
    #: Eintraege, die im Listing stehen, aber keine Datei haben (-> 404).
    extra_index: dict[str, int] = field(default_factory=dict)


class _Source(ThreadingHTTPServer):
    """Testserver mit Dateien, Stoerungen und Anfrageprotokoll."""

    daemon_threads = True
    files: dict[str, bytes]
    faults: Faults
    log: list[tuple[str, str | None, str | None]]

    def handle_error(self, request: object, client_address: object) -> None:
        """Abbrechende Verbindungen sind hier Absicht — kein Stacktrace."""
        return


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _Source  # type: ignore[assignment]

    def log_message(self, *args: object) -> None:
        return

    def do_GET(self) -> None:
        name = self.path.rsplit("/", 1)[-1]
        self.server.log.append(
            (self.path, self.headers.get("Range"), self.headers.get("User-Agent"))
        )
        if name == "index.json":
            self._serve_index()
        else:
            self._serve_file(name)

    def _serve_index(self) -> None:
        faults = self.server.faults
        month = self.path.strip("/").split("/")[1]
        if month in faults.missing_months:
            self._send(404, b"kein Listing")
            return
        if faults.broken_index:
            self._send(200, b"{kein json", content_type="application/json")
            return
        entries: list[dict[str, object]] = [{"name": f"{month}-unterordner/"}]
        for name, data in self.server.files.items():
            if name.startswith(month):
                entries.append({"name": name, "size": faults.index_sizes.get(name, len(data))})
        for name, size in faults.extra_index.items():
            entries.append({"name": name, "size": size})
        self._send(200, json.dumps(entries).encode(), content_type="application/json")

    def _serve_file(self, name: str) -> None:
        faults = self.server.faults
        data = self.server.files.get(name)
        if data is None:
            self._send(404, b"nicht da")
            return
        if faults.statuses:
            self._send(faults.statuses.pop(0), b"")
            return

        range_header = self.headers.get("Range")
        start = 0
        if range_header and not faults.ignore_range:
            start = int(range_header.removeprefix("bytes=").split("-")[0])
            body = data[start:]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        else:
            body = data
            self.send_response(200)
        self.send_header("Content-Type", "application/gzip")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

        cut = faults.aborts.pop(0) if faults.aborts else faults.always_abort_after
        if cut is None:
            self.wfile.write(body)
            return
        self.wfile.write(body[:cut])
        self.wfile.flush()
        self.close_connection = True

    def _send(self, status: int, body: bytes, *, content_type: str = "text/plain") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)


@pytest.fixture
def source() -> Iterator[_Source]:
    """Laufender Testserver mit zwei Dateien und einer leeren."""
    server = _Source(("127.0.0.1", 0), _Handler)
    server.files = {
        FILE.name: PAYLOAD,
        OTHER.name: PAYLOAD[:100] + PAYLOAD[100:],
        DeltaFile(DAY, Stream.TRACK_PUID).name: EMPTY_PAYLOAD,
    }
    server.faults = Faults()
    server.log = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@dataclass
class _Clock:
    """Ersatz fuer ``time.sleep``; merkt sich die Wartezeiten."""

    waits: list[float] = field(default_factory=list)

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def downloader(source: _Source, clock: _Clock, tmp_path: Path) -> Iterator[DeltaDownloader]:
    host, port = source.server_address[:2]
    with DeltaDownloader(
        tmp_path / "dumps",
        base_url=f"http://{host}:{port}",
        sleep=clock,
        read_timeout_s=5.0,
        connect_timeout_s=5.0,
    ) as instance:
        yield instance


def file_requests(source: _Source, name: str) -> list[tuple[str, str | None, str | None]]:
    return [entry for entry in source.log if entry[0].endswith(name)]


# --- Gutfall ---------------------------------------------------------------


def test_a_file_is_downloaded_validated_and_renamed(
    downloader: DeltaDownloader, source: _Source
) -> None:
    result = downloader.fetch(FILE)
    assert result.path == downloader.path_for(FILE) == downloader.dest_dir / FILE.name
    assert result.path.read_bytes() == PAYLOAD
    assert result.size == len(PAYLOAD)
    assert (result.reused, result.resumed, result.attempts) == (False, False, 1)
    assert not list(downloader.dest_dir.glob("*.part")), "kein Rest unter .part"
    assert file_requests(source, FILE.name)[0][2] == USER_AGENT


def test_an_empty_file_is_a_normal_download(downloader: DeltaDownloader) -> None:
    empty = DeltaFile(DAY, Stream.TRACK_PUID)
    result = downloader.fetch(empty)
    assert result.empty is True
    assert result.path.read_bytes() == EMPTY_PAYLOAD


def test_a_validated_file_is_not_downloaded_again(
    downloader: DeltaDownloader, source: _Source
) -> None:
    downloader.fetch(FILE)
    again = downloader.fetch(FILE)
    assert (again.reused, again.attempts) == (True, 0)
    assert len(file_requests(source, FILE.name)) == 1


def test_a_file_with_the_wrong_size_is_fetched_again(
    downloader: DeltaDownloader, source: _Source, caplog: pytest.LogCaptureFixture
) -> None:
    target = downloader.dest_dir
    target.mkdir(parents=True)
    (target / FILE.name).write_bytes(b"zu kurz")
    with caplog.at_level("WARNING", logger="acoustid_importer.download"):
        result = downloader.fetch(FILE)
    assert result.reused is False
    assert result.path.read_bytes() == PAYLOAD
    assert any("falsche Groesse" in record.getMessage() for record in caplog.records)


def test_a_stale_part_next_to_a_finished_file_is_removed(
    downloader: DeltaDownloader, source: _Source
) -> None:
    """Beim Bootstrap kostet jeder liegengebliebene Rest echten Platz."""
    downloader.dest_dir.mkdir(parents=True)
    (downloader.dest_dir / FILE.name).write_bytes(PAYLOAD)
    part = downloader.dest_dir / (FILE.name + ".part")
    part.write_bytes(PAYLOAD[:20])

    assert downloader.fetch(FILE).reused is True
    assert not part.exists()
    assert file_requests(source, FILE.name) == []


def test_fetch_all_keeps_the_order(downloader: DeltaDownloader) -> None:
    files = [OTHER, FILE]
    assert [result.file for result in downloader.fetch_all(files)] == files


def test_from_env_uses_the_dump_dir(tmp_path: Path) -> None:
    env = EnvSettings.from_env({"AOFF_DUMP_DIR": str(tmp_path / "deltas")})
    with DeltaDownloader.from_env(env) as instance:
        assert instance.dest_dir == tmp_path / "deltas"
        assert instance.base_url == "https://data.acoustid.org"


def test_max_attempts_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        DeltaDownloader(tmp_path, max_attempts=0)


# --- Resume per Range ------------------------------------------------------


def test_an_existing_part_is_resumed_with_a_range_request(
    downloader: DeltaDownloader, source: _Source
) -> None:
    downloader.dest_dir.mkdir(parents=True)
    part = downloader.dest_dir / (FILE.name + ".part")
    part.write_bytes(PAYLOAD[:500])

    result = downloader.fetch(FILE)
    assert result.resumed is True
    assert result.path.read_bytes() == PAYLOAD
    assert file_requests(source, FILE.name)[0][1] == "bytes=500-"


def test_an_aborted_transfer_continues_where_it_stopped(
    downloader: DeltaDownloader, source: _Source, clock: _Clock
) -> None:
    source.faults.aborts = [400]

    result = downloader.fetch(FILE)
    assert (result.attempts, result.resumed) == (2, True)
    assert result.path.read_bytes() == PAYLOAD
    ranges = [entry[1] for entry in file_requests(source, FILE.name)]
    assert ranges == [None, "bytes=400-"], "der zweite Versuch laedt nicht von vorn"
    assert clock.waits == [1.0]


def test_a_too_large_part_is_discarded_and_the_file_reloaded(
    downloader: DeltaDownloader, source: _Source
) -> None:
    downloader.dest_dir.mkdir(parents=True)
    part = downloader.dest_dir / (FILE.name + ".part")
    part.write_bytes(PAYLOAD + b"zuviel")

    result = downloader.fetch(FILE)
    assert result.resumed is False
    assert result.path.read_bytes() == PAYLOAD
    assert file_requests(source, FILE.name)[0][1] is None


def test_a_server_without_range_support_starts_over(
    downloader: DeltaDownloader, source: _Source, caplog: pytest.LogCaptureFixture
) -> None:
    source.faults.ignore_range = True
    downloader.dest_dir.mkdir(parents=True)
    (downloader.dest_dir / (FILE.name + ".part")).write_bytes(PAYLOAD[:500])

    with caplog.at_level("INFO", logger="acoustid_importer.download"):
        result = downloader.fetch(FILE)
    assert result.resumed is False
    assert result.path.read_bytes() == PAYLOAD
    assert any("Range" in record.getMessage() for record in caplog.records)


# --- Retries und Backoff ---------------------------------------------------


def test_a_5xx_answer_is_retried(
    downloader: DeltaDownloader, source: _Source, clock: _Clock
) -> None:
    source.faults.statuses = [503, 502]
    result = downloader.fetch(FILE)
    assert result.attempts == 3
    assert result.path.read_bytes() == PAYLOAD
    assert clock.waits == [1.0, 2.0]


def test_giving_up_after_five_attempts(
    downloader: DeltaDownloader, source: _Source, clock: _Clock
) -> None:
    source.faults.statuses = [503] * 5
    with pytest.raises(DownloadError, match="nach 5 Versuchen"):
        downloader.fetch(FILE)
    assert clock.waits == [1.0, 2.0, 4.0, 8.0], "exponentiell, vier Pausen bei fuenf Versuchen"
    assert len(file_requests(source, FILE.name)) == 5


def test_an_endless_abort_still_makes_progress_and_then_gives_up(
    downloader: DeltaDownloader, source: _Source, clock: _Clock
) -> None:
    source.faults.always_abort_after = 10
    with pytest.raises(DownloadError, match="nach 5 Versuchen"):
        downloader.fetch(FILE)
    assert len(clock.waits) == 4
    part = downloader.dest_dir / (FILE.name + ".part")
    assert part.stat().st_size == 50, "jeder der fuenf Versuche hat 10 Byte geschafft"
    assert [entry[1] for entry in file_requests(source, FILE.name)] == [
        None,
        "bytes=10-",
        "bytes=20-",
        "bytes=30-",
        "bytes=40-",
    ]


def test_the_backoff_is_capped(source: _Source, clock: _Clock, tmp_path: Path) -> None:
    host, port = source.server_address[:2]
    source.faults.statuses = [503] * 8
    with (
        DeltaDownloader(
            tmp_path, base_url=f"http://{host}:{port}", sleep=clock, max_attempts=8
        ) as instance,
        pytest.raises(DownloadError),
    ):
        instance.fetch(FILE)
    assert clock.waits == [1.0, 2.0, 4.0, 8.0, 10.0, 10.0, 10.0]


def test_a_404_is_not_retried(downloader: DeltaDownloader, source: _Source) -> None:
    """Fehlt die Datei am Server, hilft Wiederholen nicht (Regel 5: Luecke)."""
    # Im Listing vorhanden, als Datei nicht — der Download bekommt 404.
    del source.files[FILE.name]
    source.faults.extra_index = {FILE.name: len(PAYLOAD)}

    with pytest.raises(DeltaNotFoundError, match="404"):
        downloader.fetch(FILE)
    assert len(file_requests(source, FILE.name)) == 1


# --- Groessenvalidierung ---------------------------------------------------


def test_a_file_missing_from_the_index_is_a_gap(
    downloader: DeltaDownloader, source: _Source
) -> None:
    del source.files[FILE.name]
    with pytest.raises(DeltaNotFoundError, match=r"steht nicht im index\.json"):
        downloader.fetch(FILE)
    assert file_requests(source, FILE.name) == [], "ohne Listing-Treffer kein Download"


def test_a_size_mismatch_is_a_hard_error(downloader: DeltaDownloader, source: _Source) -> None:
    source.faults.index_sizes[FILE.name] = len(PAYLOAD) + 5
    with pytest.raises(SizeMismatchError, match=r"index\.json"):
        downloader.fetch(FILE)
    assert not (downloader.dest_dir / FILE.name).exists()


def test_a_size_mismatch_while_resuming_is_a_hard_error(
    downloader: DeltaDownloader, source: _Source
) -> None:
    source.faults.index_sizes[FILE.name] = len(PAYLOAD) - 5
    downloader.dest_dir.mkdir(parents=True)
    (downloader.dest_dir / (FILE.name + ".part")).write_bytes(PAYLOAD[:100])
    with pytest.raises(SizeMismatchError, match="Server meldet"):
        downloader.fetch(FILE)


# --- gzip-Integritaet -----------------------------------------------------


def test_a_broken_gzip_stream_is_rejected(downloader: DeltaDownloader, source: _Source) -> None:
    source.files[FILE.name] = b"\x1f\x8bkein gueltiges gzip"
    with pytest.raises(DownloadError, match="gzip-Strom defekt"):
        downloader.fetch(FILE)
    assert not (downloader.dest_dir / FILE.name).exists()
    assert not (downloader.dest_dir / (FILE.name + ".part")).exists()


def test_the_gzip_check_can_be_switched_off(downloader: DeltaDownloader, source: _Source) -> None:
    """Fuer den Bootstrap: der Parser liest den Strom ohnehin direkt danach."""
    source.files[FILE.name] = b"\x1f\x8bkein gueltiges gzip"
    result = downloader.fetch(FILE, verify_gzip=False)
    assert result.path.exists()


# --- Monats-Listing -------------------------------------------------------


def test_the_month_index_is_fetched_once_and_cached(
    downloader: DeltaDownloader, source: _Source
) -> None:
    downloader.fetch(FILE)
    downloader.fetch(OTHER)
    assert len(file_requests(source, "index.json")) == 1
    downloader.clear_index_cache()
    downloader.expected_size(FILE)
    assert len(file_requests(source, "index.json")) == 2


def test_directory_entries_without_a_size_are_ignored(
    downloader: DeltaDownloader, source: _Source
) -> None:
    index = downloader.month_index(DAY)
    assert set(index) == set(source.files)
    assert all(isinstance(size, int) for size in index.values())


def test_a_missing_month_listing_is_a_gap(downloader: DeltaDownloader, source: _Source) -> None:
    source.faults.missing_months = {"2026-07"}
    with pytest.raises(DeltaNotFoundError, match="kein Listing"):
        downloader.expected_size(FILE)


def test_a_broken_month_listing_is_an_error(downloader: DeltaDownloader, source: _Source) -> None:
    source.faults.broken_index = True
    with pytest.raises(DownloadError, match="kein JSON"):
        downloader.expected_size(FILE)


def test_the_listing_is_retried_too(
    downloader: DeltaDownloader, source: _Source, clock: _Clock
) -> None:
    source.faults.statuses = []
    source.faults.missing_months = set()
    # Der Statusvorrat gilt nur fuer Dateien; das Listing wird ueber einen
    # kurzzeitig unerreichbaren Server geprueft: erst 503, dann normal.
    original = _Handler._serve_index
    calls = {"n": 0}

    def flaky(self: _Handler) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            self._send(503, b"")
            return
        original(self)

    _Handler._serve_index = flaky  # type: ignore[method-assign]
    try:
        assert downloader.expected_size(FILE) == len(PAYLOAD)
    finally:
        _Handler._serve_index = original  # type: ignore[method-assign]
    assert clock.waits == [1.0]


def test_repr_names_target_and_source(downloader: DeltaDownloader) -> None:
    assert "DeltaDownloader(dest_dir=" in repr(downloader)
    assert downloader.base_url in repr(downloader)
