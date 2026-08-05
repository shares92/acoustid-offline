"""Plattenplatz-Guard (Invariante §8.8) — reine Rechenlogik, kein Dateisystem.

Der Guard bekommt seine Messfunktion uebergeben; die Randfaelle (Platte
fast voll, Guard abgeschaltet, Pfad noch nicht angelegt) sind damit ohne
echte volle Platte pruefbar.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from acoustid_importer.diskguard import (
    BYTES_PER_GB,
    DiskGuard,
    evaluate,
    measure,
    require_free_space,
)
from acoustid_importer.errors import DiskSpaceError, ImporterError


@dataclass
class _Usage:
    """Ersatz fuer ``shutil.disk_usage``; liefert feste Werte und zaehlt mit."""

    free_gb: float
    total_gb: float = 1000.0
    calls: list[Path] | None = None

    def __call__(self, path: Path) -> _Usage:
        if self.calls is not None:
            self.calls.append(path)
        return self

    @property
    def free(self) -> int:
        return int(self.free_gb * BYTES_PER_GB)

    @property
    def total(self) -> int:
        return int(self.total_gb * BYTES_PER_GB)


class _Broken:
    """Messfunktion, die scheitert (Pfad nicht lesbar)."""

    def __call__(self, path: Path) -> _Usage:
        raise OSError(13, "Permission denied")


# --- Bewertung --------------------------------------------------------------


def test_the_reserve_is_read_as_binary_gigabytes() -> None:
    """`min_free_gb` wird als GiB gelesen — die strengere Lesart."""
    space = evaluate(Path("/data"), total_bytes=0, free_bytes=0, min_free_gb=50)
    assert space.min_free_bytes == 50 * 1024**3


def test_exactly_the_reserve_is_still_enough() -> None:
    space = evaluate(
        Path("/data"), total_bytes=100 * BYTES_PER_GB, free_bytes=50 * BYTES_PER_GB, min_free_gb=50
    )
    assert space.ok
    assert space.shortfall_bytes == 0


def test_one_byte_less_than_the_reserve_is_not_enough() -> None:
    space = evaluate(
        Path("/data"),
        total_bytes=100 * BYTES_PER_GB,
        free_bytes=50 * BYTES_PER_GB - 1,
        min_free_gb=50,
    )
    assert not space.ok
    assert space.shortfall_bytes == 1


def test_a_negative_reserve_is_a_usage_error() -> None:
    with pytest.raises(ValueError, match="min_free_gb"):
        evaluate(Path("/data"), total_bytes=0, free_bytes=0, min_free_gb=-1)


def test_the_measurement_is_machine_readable_for_the_report() -> None:
    space = evaluate(Path("/data"), total_bytes=10, free_bytes=4, min_free_gb=0)
    assert space.as_dict() == {
        "path": "/data",
        "total_bytes": 10,
        "free_bytes": 4,
        "min_free_bytes": 0,
        "ok": True,
    }


# --- Messen -----------------------------------------------------------------


def test_a_missing_directory_is_measured_at_its_nearest_existing_parent(tmp_path: Path) -> None:
    """Das Arbeitsverzeichnis entsteht erst beim ersten Download."""
    seen: list[Path] = []
    usage = _Usage(free_gb=100.0, calls=seen)

    space = measure(tmp_path / "dumps" / "tief", min_free_gb=10, usage=usage)

    assert seen == [tmp_path]
    assert space.path == tmp_path


def test_an_unreadable_path_ends_the_run_instead_of_guessing(tmp_path: Path) -> None:
    with pytest.raises(DiskSpaceError, match="nicht messbar") as caught:
        measure(tmp_path, min_free_gb=10, usage=_Broken())
    assert caught.value.free_bytes is None


def test_enough_space_passes_quietly(tmp_path: Path) -> None:
    space = require_free_space(tmp_path, min_free_gb=50, usage=_Usage(free_gb=51.0))
    assert space.ok


def test_too_little_space_names_the_numbers_and_the_config_key(tmp_path: Path) -> None:
    with pytest.raises(DiskSpaceError) as caught:
        require_free_space(tmp_path, min_free_gb=50, usage=_Usage(free_gb=3.5))

    message = str(caught.value)
    assert "3.5 GiB frei" in message
    assert "50.0 GiB" in message
    assert "disk.min_free_gb" in message
    assert "resumierbar" in message
    assert caught.value.min_free_bytes == 50 * BYTES_PER_GB
    # Ein Guard-Abbruch ist ein Importer-Fehler wie jeder andere und kann
    # damit pauschal abgefangen werden.
    assert isinstance(caught.value, ImporterError)


# --- Wiederholung waehrend des Laufs ---------------------------------------


def test_the_guard_checks_again_after_the_configured_number_of_files(tmp_path: Path) -> None:
    seen: list[Path] = []
    guard = DiskGuard(
        tmp_path,
        min_free_gb=10,
        every_files=3,
        every_bytes=1 << 60,
        usage=_Usage(free_gb=100.0, calls=seen),
    )

    assert [guard.after_file(1) is not None for _ in range(6)] == [
        False,
        False,
        True,
        False,
        False,
        True,
    ]
    assert guard.checks == 2
    assert seen == [tmp_path, tmp_path]


def test_the_guard_checks_again_after_the_configured_number_of_bytes(tmp_path: Path) -> None:
    """Eine einzige Fingerprint-Tagesdatei kann mehrere GB gross sein."""
    guard = DiskGuard(
        tmp_path, min_free_gb=10, every_files=1000, every_bytes=100, usage=_Usage(free_gb=100.0)
    )

    assert guard.after_file(60) is None
    assert guard.after_file(60) is not None
    assert guard.checks == 1


def test_a_check_resets_both_counters(tmp_path: Path) -> None:
    guard = DiskGuard(
        tmp_path, min_free_gb=10, every_files=2, every_bytes=100, usage=_Usage(free_gb=100.0)
    )
    guard.after_file(90)
    guard.check()
    assert not guard.due()
    assert guard.after_file(90) is None


def test_a_disabled_guard_never_measures_and_never_stops(tmp_path: Path) -> None:
    """`min_free_gb: 0` heisst laut §6: keine Reserve gefordert."""
    seen: list[Path] = []
    guard = DiskGuard(tmp_path, min_free_gb=0, every_files=1, usage=_Usage(free_gb=0.0, calls=seen))

    assert guard.check() is None
    assert guard.after_file(10**12) is None
    assert guard.checks == 0
    assert seen == []


def test_the_guard_stops_the_run_when_the_reserve_is_gone(tmp_path: Path) -> None:
    guard = DiskGuard(tmp_path, min_free_gb=50, every_files=1, usage=_Usage(free_gb=1.0))
    with pytest.raises(DiskSpaceError):
        guard.after_file(1)


def test_the_closing_measurement_never_stops_the_run(tmp_path: Path) -> None:
    """Am Ende ist zu wenig Platz nur noch eine Zahl fuer den Report."""
    guard = DiskGuard(tmp_path, min_free_gb=50, every_files=1, usage=_Usage(free_gb=1.0))
    space = guard.measure()
    assert space is not None
    assert not space.ok


def test_an_unmeasurable_path_leaves_the_closing_measurement_empty(tmp_path: Path) -> None:
    guard = DiskGuard(tmp_path, min_free_gb=50, usage=_Broken())
    assert guard.measure() is None


def test_impossible_intervals_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="mindestens 1"):
        DiskGuard(tmp_path, min_free_gb=1, every_files=0)
    with pytest.raises(ValueError, match="mindestens 1"):
        DiskGuard(tmp_path, min_free_gb=1, every_bytes=0)
    with pytest.raises(ValueError, match="min_free_gb"):
        DiskGuard(tmp_path, min_free_gb=-1)
