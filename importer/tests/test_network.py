"""End-to-end gegen data.acoustid.org — Marker ``network``, per Vorgabe aus.

Belegt, dass das URL-Schema, das Monats-``index.json`` und das Range-Resume
gegen den **echten** Server stimmen. Bewusst am kleinsten Objekt der ganzen
Quelle: der leeren Tagesdatei vom 23.07.2026 (23 Byte). Der gesamte Test
verursacht ein paar Kilobyte Verkehr (das Listing) und bleibt damit im Rahmen
des Fair-Use-Hinweises der Fixture-README.

Laufen lassen::

    uv run pytest -m network --network

In der CI laeuft er nicht (siehe conftest.py im Repo-Wurzelverzeichnis).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from acoustid_importer.download import DeltaDownloader
from acoustid_importer.errors import DownloadError
from acoustid_importer.parser import DeltaReader
from acoustid_importer.streams import BASE_URL, EMPTY_GZ_SIZE, DeltaFile, Stream

pytestmark = pytest.mark.network

#: Die kleinste echte Datei der Quelle (leerer Tag im groessten Strom).
EMPTY_DAY = DeltaFile(date(2026, 7, 23), Stream.FINGERPRINT)


def test_the_url_scheme_and_index_json_match_the_real_server(tmp_path: Path) -> None:
    assert EMPTY_DAY.url() == (f"{BASE_URL}/2026/2026-07/2026-07-23-fingerprint-update.jsonl.gz")
    with DeltaDownloader(tmp_path) as downloader:
        assert downloader.expected_size(EMPTY_DAY) == EMPTY_GZ_SIZE
        result = downloader.fetch(EMPTY_DAY)

    assert result.size == EMPTY_GZ_SIZE
    assert result.empty is True
    assert result.attempts == 1
    assert list(DeltaReader(result.path)) == [], "leere Datei = null Records, kein Fehler"


def test_the_real_server_answers_range_requests(tmp_path: Path) -> None:
    """Die Grundlage des Resume: 206 mit Content-Range (§5.1)."""
    with DeltaDownloader(tmp_path) as downloader:
        whole = downloader.fetch(EMPTY_DAY).path
        content = whole.read_bytes()
        whole.unlink()
        part = whole.with_name(whole.name + ".part")
        part.write_bytes(content[:10])

        result = downloader.fetch(EMPTY_DAY)

    assert result.resumed is True, "der Server hat den Range-Wunsch beantwortet"
    assert result.path.read_bytes() == content


def test_a_day_before_the_history_does_not_exist(tmp_path: Path) -> None:
    """Gegenprobe: der Monat vor 2011-08 hat kein Listing."""
    with DeltaDownloader(tmp_path) as downloader, pytest.raises(DownloadError):
        downloader.expected_size(DeltaFile(date(2011, 7, 19), Stream.TRACK))
