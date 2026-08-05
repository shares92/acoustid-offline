"""Rotation der Waechter-Logdatei (M2.5, offener Punkt 6 aus PROGRESS)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from acoustid_watchdog.logrotate import (
    DEFAULT_BACKUPS,
    DEFAULT_MAX_BYTES,
    LOG_DIRNAME,
    WATCHDOG_LOG_FILENAME,
    LogRotator,
)
from acoustid_watchdog.service import WatchdogService


def _logfile(tmp_path: Path, size: int = 0) -> Path:
    path = tmp_path / LOG_DIRNAME / WATCHDOG_LOG_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


# --- Faelligkeit ------------------------------------------------------------


def test_a_small_file_is_left_alone(tmp_path: Path) -> None:
    rotator = LogRotator(_logfile(tmp_path, 100), max_bytes=1000)
    assert rotator.due() is False
    assert rotator.rotate() is False


def test_a_missing_file_is_not_an_error(tmp_path: Path) -> None:
    """Ausserhalb des Containers gibt es sie nicht — dort schreibt kein `tee`."""
    rotator = LogRotator(tmp_path / "gibt-es-nicht.log")
    assert rotator.due() is False
    assert rotator.rotate() is False


def test_zero_max_bytes_switches_the_rotation_off(tmp_path: Path) -> None:
    rotator = LogRotator(_logfile(tmp_path, 10_000), max_bytes=0)
    assert rotator.due() is False


# --- Der eigentliche Trick --------------------------------------------------


def test_the_file_keeps_its_inode(tmp_path: Path) -> None:
    """Der ganze Punkt: `tee` haelt den Deskriptor offen.

    Nach einem ``rename`` schriebe es unbeirrt in die umbenannte Datei
    weiter und die neue bliebe fuer immer leer. Ein ``truncate`` auf
    derselben Inode wirkt dagegen sofort.
    """
    path = _logfile(tmp_path, 2000)
    inode_before = path.stat().st_ino

    assert LogRotator(path, max_bytes=1000).rotate() is True

    assert path.stat().st_ino == inode_before
    assert path.stat().st_size == 0


def test_an_open_appender_writes_into_the_truncated_file(tmp_path: Path) -> None:
    """Der Nachweis am offenen Deskriptor — so haelt es sich `tee`."""
    path = _logfile(tmp_path, 2000)
    with path.open("ab") as appender:
        assert LogRotator(path, max_bytes=1000).rotate() is True
        appender.write(b"nach der Rotation\n")
        appender.flush()

    assert path.read_bytes() == b"nach der Rotation\n"


def test_the_content_survives_in_the_first_generation(tmp_path: Path) -> None:
    path = _logfile(tmp_path)
    path.write_bytes(b"alte Zeilen\n" * 200)

    LogRotator(path, max_bytes=100).rotate()

    assert path.with_name(f"{WATCHDOG_LOG_FILENAME}.1").read_bytes().startswith(b"alte Zeilen")
    assert path.stat().st_size == 0


# --- Die Generationen -------------------------------------------------------


def test_generations_are_shifted_and_the_oldest_falls_off(tmp_path: Path) -> None:
    path = _logfile(tmp_path)
    rotator = LogRotator(path, max_bytes=10, backups=3)

    for marker in (b"eins", b"zwei", b"drei", b"vier"):
        path.write_bytes(marker * 10)
        assert rotator.rotate() is True

    assert path.with_name(f"{WATCHDOG_LOG_FILENAME}.1").read_bytes().startswith(b"vier")
    assert path.with_name(f"{WATCHDOG_LOG_FILENAME}.2").read_bytes().startswith(b"drei")
    assert path.with_name(f"{WATCHDOG_LOG_FILENAME}.3").read_bytes().startswith(b"zwei")
    # „eins" ist herausgefallen — mehr als `backups` Generationen gibt es nicht.
    assert not path.with_name(f"{WATCHDOG_LOG_FILENAME}.4").exists()
    assert rotator.rotations == 4


def test_the_limits_match_the_other_logs() -> None:
    """In /config/logs sieht der Betreiber ueberall dasselbe Muster."""
    assert DEFAULT_MAX_BYTES == 10 * 1024 * 1024
    assert DEFAULT_BACKUPS == 3


# --- Widrigkeiten -----------------------------------------------------------


def test_an_unwritable_directory_is_only_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ein volles /config darf den Waechter nicht anhalten."""
    path = _logfile(tmp_path, 2000)

    def broken(*args: object, **kwargs: object) -> None:
        raise OSError("kein Platz mehr")

    monkeypatch.setattr("acoustid_watchdog.logrotate.shutil.copy2", broken)
    rotator = LogRotator(path, max_bytes=1000)

    assert rotator.rotate() is False
    assert rotator.rotations == 0
    assert path.stat().st_size == 2000  # nichts angefasst


def test_the_loop_survives_a_failing_rotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rotator = LogRotator(_logfile(tmp_path, 2000), max_bytes=1000)
    rotator.interval_s = 0.01
    calls = 0

    def explode() -> bool:
        nonlocal calls
        calls += 1
        raise RuntimeError("kaputt")

    monkeypatch.setattr(rotator, "rotate", explode)

    async def scenario() -> None:
        task = asyncio.create_task(rotator.run())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert calls >= 2


# --- Der Ort ----------------------------------------------------------------


def test_the_service_watches_the_file_tee_writes(service: WatchdogService) -> None:
    """`/config/logs/watchdog.log` — derselbe Pfad wie in supervisord.conf."""
    assert service.logrotate.path == service.settings.data_dir / "logs" / "watchdog.log"


def test_the_path_matches_the_supervisor_configuration() -> None:
    """Ein Tippfehler hier bliebe unbemerkt — die Datei waechse einfach weiter."""
    from pathlib import Path as _Path

    conf = (_Path(__file__).resolve().parents[2] / "supervisor/supervisord.conf").read_text(
        encoding="utf-8"
    )
    assert f"/config/{LOG_DIRNAME}/{WATCHDOG_LOG_FILENAME}" in conf
