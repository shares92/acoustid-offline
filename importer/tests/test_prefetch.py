"""Download-Prefetch: laden, waehrend importiert wird (Phase 8).

Kein Netz und kein Dateisystem — der Downloader ist hier eine Attrappe, die
sich anhalten laesst. Nur so sind die Fragen pruefbar, um die es geht:
Laeuft der Ladethread wirklich voraus? Bleibt die Reihenfolge (Import-Regel
1)? Kommt ein Ladefehler beim Aufrufer an? Und hoert der Thread auf, wenn
der Lauf abbricht — ohne den Prozess haengen zu lassen?
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest

from acoustid_importer.download import Download
from acoustid_importer.errors import DownloadError
from acoustid_importer.prefetch import Prefetcher
from acoustid_importer.streams import IMPORT_ORDER, DeltaFile

DAY = date(2026, 7, 22)
FILES = tuple(DeltaFile(DAY, stream) for stream in IMPORT_ORDER)

#: Grosszuegig; die Tests warten nie wirklich so lange.
TIMEOUT_S = 5.0


class FakeDownloader:
    """Downloader-Attrappe: protokolliert, kann blockieren und scheitern."""

    def __init__(
        self,
        *,
        fail_on: DeltaFile | None = None,
        gate: threading.Event | None = None,
        gate_after: int = 0,
    ) -> None:
        self.fetched: list[DeltaFile] = []
        self.verify_flags: list[bool] = []
        self.fail_on = fail_on
        self.gate = gate
        self.gate_after = gate_after
        #: Wird gesetzt, sobald der Thread vor dem Tor steht.
        self.at_gate = threading.Event()

    def fetch(self, file: DeltaFile, *, verify_gzip: bool = True) -> Download:
        if self.gate is not None and len(self.fetched) >= self.gate_after:
            self.at_gate.set()
            self.gate.wait(TIMEOUT_S)
        if file == self.fail_on:
            raise DownloadError(f"{file.name}: erfunden")
        self.fetched.append(file)
        self.verify_flags.append(verify_gzip)
        return Download(file, Path("/dumps") / file.name, size=100 + len(self.fetched))


def test_the_files_arrive_in_the_order_of_the_work_list() -> None:
    """Import-Regel 1 steckt in der Arbeitsliste — der Prefetch darf sie nicht ruehren."""
    downloader = FakeDownloader()

    with Prefetcher(downloader, FILES, ahead=3) as prefetcher:
        received = [download.file for download in prefetcher]

    assert received == list(FILES)
    assert downloader.fetched == list(FILES)


def test_the_next_files_are_loaded_while_the_current_one_is_imported() -> None:
    """Der Punkt der ganzen Uebung: die Leitung steht nie still."""
    downloader = FakeDownloader()

    with Prefetcher(downloader, FILES, ahead=2) as prefetcher:
        stream = iter(prefetcher)
        first = next(stream)
        # Waehrend der Aufrufer die erste Datei „importiert", laedt der
        # Thread weiter — bis die Warteschlange voll ist (ahead=2).
        _wait_for(lambda: prefetcher.pending == 2)

        assert first.file == FILES[0]
        assert len(downloader.fetched) >= 3
        rest = [download.file for download in stream]

    assert [first.file, *rest] == list(FILES)


def test_the_lookahead_is_bounded_because_it_costs_disk_space() -> None:
    downloader = FakeDownloader()

    with Prefetcher(downloader, FILES, ahead=1) as prefetcher:
        stream = iter(prefetcher)
        next(stream)
        _wait_for(lambda: len(downloader.fetched) >= 2)
        # Eine ausgeliefert, eine in der Warteschlange, eine im Zugriff:
        # mehr als drei darf der Thread nicht vorgelaufen sein.
        assert len(downloader.fetched) <= 3


def test_the_statistics_count_files_bytes_and_reuse() -> None:
    downloader = FakeDownloader()

    with Prefetcher(downloader, FILES[:3], ahead=2) as prefetcher:
        for _ in prefetcher:
            pass
        stats = prefetcher.stats

    assert stats.files == 3
    assert stats.downloaded == 3
    assert stats.bytes == 101 + 102 + 103


def test_a_download_error_reaches_the_caller_unchanged() -> None:
    """Kein verschlucktes Thread-Traceback — der Job-Rumpf soll ihn sehen."""
    downloader = FakeDownloader(fail_on=FILES[1])

    with pytest.raises(DownloadError, match="meta-update"), Prefetcher(downloader, FILES) as p:
        for _ in p:
            pass


def test_the_files_before_the_error_are_still_delivered() -> None:
    downloader = FakeDownloader(fail_on=FILES[2])
    seen: list[DeltaFile] = []

    with pytest.raises(DownloadError), Prefetcher(downloader, FILES, ahead=1) as prefetcher:
        for download in prefetcher:
            seen.append(download.file)

    assert seen == [FILES[0], FILES[1]]


def test_the_verify_flag_is_passed_through_for_the_bootstrap() -> None:
    """Im Bootstrap wird die gzip-Pruefung gespart (DECISIONS 2026-07-25)."""
    downloader = FakeDownloader()

    with Prefetcher(downloader, FILES[:2], verify_gzip=False) as prefetcher:
        list(prefetcher)

    assert downloader.verify_flags == [False, False]


def test_leaving_the_loop_early_stops_the_loading_thread() -> None:
    """Guard-Abbruch mitten im Lauf: der Thread darf nicht weiterlaufen."""
    downloader = FakeDownloader()

    with Prefetcher(downloader, FILES, ahead=1) as prefetcher:
        for _ in prefetcher:
            break

    _wait_for(lambda: not _threads_named("delta-prefetch"))
    assert len(downloader.fetched) < len(FILES)


def test_close_returns_even_while_a_transfer_is_running() -> None:
    """Eine laufende Uebertragung wird zu Ende gefuehrt, dann ist Schluss."""
    gate = threading.Event()
    downloader = FakeDownloader(gate=gate, gate_after=1)
    prefetcher = Prefetcher(downloader, FILES, ahead=1)

    stream = iter(prefetcher)
    assert next(stream).file == FILES[0]
    assert downloader.at_gate.wait(TIMEOUT_S)

    closer = threading.Thread(target=prefetcher.close)
    closer.start()
    gate.set()
    closer.join(TIMEOUT_S)

    assert not closer.is_alive()
    assert not _threads_named("delta-prefetch")


def test_close_is_idempotent_and_may_come_before_the_first_file() -> None:
    prefetcher = Prefetcher(FakeDownloader(), FILES)
    prefetcher.close()
    prefetcher.close()


def test_a_prefetcher_runs_exactly_once() -> None:
    prefetcher = Prefetcher(FakeDownloader(), FILES[:1])
    list(prefetcher)

    with pytest.raises(RuntimeError, match="einmal"):
        list(prefetcher)


def test_an_empty_work_list_is_no_error() -> None:
    with Prefetcher(FakeDownloader(), ()) as prefetcher:
        assert list(prefetcher) == []


def test_a_lookahead_below_one_is_a_usage_error() -> None:
    with pytest.raises(ValueError, match="ahead"):
        Prefetcher(FakeDownloader(), FILES, ahead=0)


def test_the_repr_names_the_size_of_the_job() -> None:
    assert "files=7" in repr(Prefetcher(FakeDownloader(), FILES, ahead=2))


# --- Hilfen -----------------------------------------------------------------


def _wait_for(condition: Callable[[], object], timeout_s: float = TIMEOUT_S) -> None:
    """Wartet, bis ``condition()`` wahr ist — sonst scheitert der Test."""
    pause = threading.Event()
    for _ in range(int(timeout_s / 0.01)):
        if condition():
            return
        pause.wait(0.01)
    raise AssertionError("Bedingung wurde nicht rechtzeitig wahr")


def _threads_named(name: str) -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == name]
