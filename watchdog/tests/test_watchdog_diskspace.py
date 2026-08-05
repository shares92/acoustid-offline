"""Plattenplatz-Guard des Waechters: jeder Schreibpfad (E11, §8.8)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from acoustid_watchdog.diskspace import (
    BYTES_PER_GB,
    DiskSpace,
    measure,
    shortfalls,
    survey,
    write_paths,
)
from shared.config import BackupConfig, Config
from shared.env import EnvSettings

GIB = BYTES_PER_GB


@dataclass(frozen=True, slots=True)
class FakeUsage:
    total: int
    used: int
    free: int


def _usage(by_path: dict[str, int], *, total: int = 1000 * GIB):
    """Messfunktion mit vorgegebenen Freiwerten je Pfad."""

    def usage(path: Path) -> FakeUsage:
        free = by_path.get(str(path), 500 * GIB)
        return FakeUsage(total=total, used=total - free, free=free)

    return usage


# --- Der Messwert selbst ----------------------------------------------------


def test_the_unit_is_gibibytes_like_in_the_importer() -> None:
    """Die strengere Lesart: wer 100 fordert, bekommt 107,4 SI-GB Reserve."""
    from acoustid_importer.diskguard import BYTES_PER_GB as IMPORTER_BYTES_PER_GB

    assert BYTES_PER_GB == IMPORTER_BYTES_PER_GB == 1024**3


def test_enough_space_is_ok() -> None:
    space = DiskSpace(
        Path("/import"), total_bytes=1000 * GIB, free_bytes=200 * GIB, min_free_bytes=100 * GIB
    )
    assert space.ok is True
    assert space.shortfall_bytes == 0
    assert space.free_gb == 200.0
    assert space.min_free_gb == 100.0
    assert "/import" in str(space)


def test_too_little_space_reports_the_shortfall() -> None:
    space = DiskSpace(
        Path("/data/db"), total_bytes=1000 * GIB, free_bytes=30 * GIB, min_free_bytes=100 * GIB
    )
    assert space.ok is False
    assert space.shortfall_bytes == 70 * GIB
    assert space.as_dict() == {
        "path": "/data/db",
        "total_bytes": 1000 * GIB,
        "free_bytes": 30 * GIB,
        "min_free_bytes": 100 * GIB,
        "ok": False,
    }


# --- Welche Pfade geprueft werden -------------------------------------------


def test_write_paths_cover_the_three_mounts(tmp_path: Path) -> None:
    settings = EnvSettings(
        data_dir=tmp_path / "config", dump_dir=tmp_path / "import", db_data_root=tmp_path / "db"
    )
    paths = write_paths(settings, Config())
    assert paths == (tmp_path / "import", tmp_path / "db", tmp_path / "config")


def test_the_backup_directory_joins_when_configured(tmp_path: Path) -> None:
    """Ein volles Backup-Ziel ist derselbe Fehler wie ein volles /import."""
    settings = EnvSettings(data_dir=tmp_path / "config")
    config = Config(backup=BackupConfig(dir=str(tmp_path / "backup")))
    assert (tmp_path / "backup") in write_paths(settings, config)


def test_without_a_backup_directory_nothing_is_added(tmp_path: Path) -> None:
    settings = EnvSettings(data_dir=tmp_path / "config")
    assert len(write_paths(settings, Config())) == 3


def test_the_search_index_mount_is_not_checked(tmp_path: Path) -> None:
    """Der Index-Mount hat keinen `MMO_`-Wert — der Waechter kennt ihn nicht.

    (Der Vorgabewert des Containers ist ``/index``; er kommt aus
    ``ACOUSTID_INDEX_DIR`` und steht bewusst nicht im Bootstrap-Schema.)
    """
    settings = EnvSettings(
        data_dir=tmp_path / "config", dump_dir=tmp_path / "import", db_data_root=tmp_path / "db"
    )
    assert Path("/index") not in write_paths(settings, Config())


# --- Die Messung ------------------------------------------------------------


def test_measure_falls_back_to_the_nearest_existing_parent(tmp_path: Path) -> None:
    """Ein Backup-Verzeichnis entsteht erst beim ersten Lauf."""
    missing = tmp_path / "gibt" / "es" / "nicht"
    space = measure(missing, min_free_gb=100, usage=_usage({str(tmp_path): 300 * GIB}))
    assert space is not None
    # Gemeldet wird der **angefragte** Pfad; gemessen wurde das Elternverzeichnis.
    assert space.path == missing
    assert space.free_gb == 300.0


def test_an_unmeasurable_path_is_not_an_error(tmp_path: Path) -> None:
    def broken(_path: Path) -> FakeUsage:
        raise OSError("kein Zugriff")

    assert measure(tmp_path, min_free_gb=100, usage=broken) is None


def test_survey_measures_each_filesystem_once(tmp_path: Path) -> None:
    """Drei Mounts auf einem Pool sind eine Auskunft, nicht drei."""
    for name in ("config", "import", "db"):
        (tmp_path / name).mkdir()
    settings = EnvSettings(
        data_dir=tmp_path / "config", dump_dir=tmp_path / "import", db_data_root=tmp_path / "db"
    )
    spaces = survey(write_paths(settings, Config()), min_free_gb=100, usage=_usage({}))
    # Alle drei liegen im selben tmp_path — also dasselbe Dateisystem.
    assert len(spaces) == 1
    assert spaces[0].path == tmp_path / "import"


def test_survey_is_off_when_the_guard_is_off(tmp_path: Path) -> None:
    """``disk.min_free_gb = 0`` schaltet den Guard ab (§6)."""
    assert survey([tmp_path], min_free_gb=0, usage=_usage({})) == []


def test_shortfalls_names_only_the_paths_that_are_too_full(tmp_path: Path) -> None:
    spaces = [
        DiskSpace(
            Path("/import"), total_bytes=1000 * GIB, free_bytes=500 * GIB, min_free_bytes=100 * GIB
        ),
        DiskSpace(
            Path("/data/db"), total_bytes=1000 * GIB, free_bytes=3 * GIB, min_free_bytes=100 * GIB
        ),
    ]
    assert [space.path for space in shortfalls(spaces)] == [Path("/data/db")]


def test_a_full_array_is_found_even_when_the_cache_is_empty(tmp_path: Path) -> None:
    """Der Fall, den E11 beschreibt: ein freies /import sagt nichts ueber /data/db.

    Die beiden Pfade liegen hier auf **verschiedenen** Dateisystemen (je
    eine eigene Messfunktion) — genau dann muss die Ausweitung greifen,
    und genau das konnte der Importer-Guard nie sehen.
    """
    dump_dir = tmp_path / "import"
    db_dir = tmp_path / "db"
    for path in (dump_dir, db_dir):
        path.mkdir()

    spaces = [
        space
        for space in (
            measure(dump_dir, min_free_gb=100, usage=_usage({str(dump_dir): 900 * GIB})),
            measure(db_dir, min_free_gb=100, usage=_usage({str(db_dir): 12 * GIB})),
        )
        if space is not None
    ]
    too_full = shortfalls(spaces)
    assert [space.path for space in too_full] == [db_dir]
    assert too_full[0].free_gb == 12.0
