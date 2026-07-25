"""Kommandozeile des One-Shot-Jobs (Phase 8) — ohne Datenbank und ohne Netz.

Geprueft wird der Vertrag nach aussen: Argumente werden zu
:class:`RunOptions`, das Ergebnis wird zum Exit-Code, und der Report landet
dort, wo er hingehoert (stdout oder Datei). Der Lauf selbst ist hier eine
Attrappe — er hat eigene Integrationstests.
"""

from __future__ import annotations

import json
import signal
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from acoustid_importer import __main__ as cli
from acoustid_importer.job import RunOptions
from acoustid_importer.report import (
    ExitCode,
    JobMode,
    RunReport,
    RunResult,
    RunTally,
    build_report,
)

STARTED = datetime(2026, 7, 25, 4, 0, tzinfo=UTC)


def report_of(result: RunResult = RunResult.OK) -> RunReport:
    return build_report(
        mode=JobMode.UPDATE,
        result=result,
        tally=RunTally(),
        started_at=STARTED,
        duration_s=0.5,
        finished_at=STARTED,
    )


@pytest.fixture
def fake_run(monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[RunOptions]]:
    """Ersetzt den Lauf durch eine Attrappe; gibt die gesehenen Optionen zurueck."""

    def install(result: RunResult = RunResult.OK) -> list[RunOptions]:
        seen: list[RunOptions] = []

        def _run(options: RunOptions, **kwargs: object) -> RunReport:
            seen.append(options)
            return report_of(result)

        monkeypatch.setattr(cli, "run", _run)
        return seen

    return install


# --- Argumente -> Optionen --------------------------------------------------


def test_the_default_is_a_daily_update_run(fake_run: Callable[..., list[RunOptions]]) -> None:
    seen = fake_run()
    cli.main([])

    options = seen[0]
    assert options.mode is JobMode.UPDATE
    assert not options.use_bulk
    assert options.gzip_check
    assert options.feed_index
    assert not options.keep_dumps


def test_the_bootstrap_switches_bulk_mode_on_and_gzip_check_off(
    fake_run: Callable[..., list[RunOptions]],
) -> None:
    """Import-Regel 6 plus DECISIONS 2026-07-25 (`verify_gzip` im Bootstrap aus)."""
    seen = fake_run()
    cli.main(["--mode", "bootstrap"])

    assert seen[0].mode is JobMode.BOOTSTRAP
    assert seen[0].use_bulk
    assert not seen[0].gzip_check


def test_the_gzip_check_can_be_forced_on_in_the_bootstrap(
    fake_run: Callable[..., list[RunOptions]],
) -> None:
    seen = fake_run()
    cli.main(["--mode", "bootstrap", "--verify-gzip"])
    assert seen[0].gzip_check


def test_the_probe_run_is_a_date_limit(fake_run: Callable[..., list[RunOptions]]) -> None:
    seen = fake_run()
    cli.main(["--mode", "bootstrap", "--end-date", "2011-12-31"])

    options = seen[0]
    assert options.end_date == date(2011, 12, 31)
    # Die Arbeitsliste denkt in „heute"; der letzte einzuschliessende Tag
    # ist deren Vortag.
    assert options.effective_today(now=date(2026, 7, 25)) == date(2012, 1, 1)


def test_an_end_date_in_the_future_does_not_invent_files(
    fake_run: Callable[..., list[RunOptions]],
) -> None:
    seen = fake_run()
    cli.main(["--end-date", "2099-01-01"])
    assert seen[0].effective_today(now=date(2026, 7, 25)) == date(2026, 7, 25)


def test_the_switches_reach_the_run_options(fake_run: Callable[..., list[RunOptions]]) -> None:
    seen = fake_run()
    cli.main(
        [
            "--max-days",
            "3",
            "--max-files",
            "7",
            "--batch-rows",
            "500",
            "--prefetch",
            "4",
            "--index-batch-size",
            "250",
            "--no-index-feed",
            "--keep-dumps",
            "--min-free-gb",
            "10",
            "--index-dir",
            "/index",
            "--total-bytes",
            "1000",
            "--no-migrate",
            "--no-checkpoint",
        ]
    )

    options = seen[0]
    assert (options.max_days, options.max_files) == (3, 7)
    assert (options.batch_rows, options.prefetch_ahead) == (500, 4)
    assert options.index_batch_size == 250
    assert not options.feed_index
    assert options.keep_dumps
    assert options.min_free_gb == 10
    assert options.index_data_dir == Path("/index")
    assert options.total_gz_bytes == 1000
    assert not options.migrate
    assert not options.checkpoint


def test_the_guard_can_be_switched_off_explicitly(
    fake_run: Callable[..., list[RunOptions]],
) -> None:
    """`update.min_free_gb: 0` heisst laut §6: keine Reserve gefordert."""
    seen = fake_run()
    cli.main(["--min-free-gb", "0"])
    assert seen[0].min_free_gb == 0


def test_gaps_are_never_skipped_from_the_command_line() -> None:
    """Import-Regel 5: ein Nachholen alter Tage verfaelscht neuere Staende."""
    assert not RunOptions().allow_gaps
    with pytest.raises(SystemExit):
        cli.main(["--allow-gaps"])


@pytest.mark.parametrize(
    "argument", ["--max-days", "--max-files", "--batch-rows", "--prefetch", "--index-batch-size"]
)
def test_a_zero_makes_no_run_and_ends_with_the_usage_code(
    argument: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main([argument, "0"]) == int(ExitCode.USAGE)
    assert "mindestens 1" in capsys.readouterr().err


def test_a_negative_reserve_ends_with_the_usage_code(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--min-free-gb", "-1"]) == int(ExitCode.USAGE)
    assert "negativ" in capsys.readouterr().err


# --- Ergebnis nach aussen ---------------------------------------------------


def test_the_exit_code_is_the_result_of_the_run(
    fake_run: Callable[..., list[RunOptions]], capsys: pytest.CaptureFixture[str]
) -> None:
    fake_run(RunResult.DISK_GUARD)
    assert cli.main([]) == int(ExitCode.DISK_GUARD)
    capsys.readouterr()


def test_a_good_run_ends_with_zero(fake_run: Callable[..., list[RunOptions]]) -> None:
    fake_run(RunResult.OK)
    assert cli.main([]) == 0


def test_the_report_goes_to_stdout_by_default(
    fake_run: Callable[..., list[RunOptions]], capsys: pytest.CaptureFixture[str]
) -> None:
    """Log auf stderr, Report auf stdout — getrennt weiterverarbeitbar."""
    fake_run()
    cli.main([])

    document = json.loads(capsys.readouterr().out)
    assert document["result"] == "ok"
    assert document["exit_code"] == 0


def test_the_report_can_be_written_to_a_file(
    fake_run: Callable[..., list[RunOptions]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_run()
    target = tmp_path / "lauf.json"
    cli.main(["--report", str(target)])

    assert json.loads(target.read_text(encoding="utf-8"))["result"] == "ok"
    assert capsys.readouterr().out == ""


def test_an_unwritable_report_path_still_shows_the_report(
    fake_run: Callable[..., list[RunOptions]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Der Report ist zu wertvoll, um ihn an einem Schreibfehler zu verlieren."""
    fake_run()
    blocked = tmp_path / "datei"
    blocked.write_text("belegt", encoding="utf-8")

    assert cli.main(["--report", str(blocked / "lauf.json")]) == 0
    assert json.loads(capsys.readouterr().out)["result"] == "ok"


def test_an_invalid_environment_ends_with_the_usage_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AOFF_PORT", "keine-zahl")
    assert cli.main([]) == int(ExitCode.USAGE)
    assert "Fehler" in capsys.readouterr().err


# --- Signale ----------------------------------------------------------------


def test_a_termination_signal_asks_for_a_graceful_stop() -> None:
    """`docker stop` beendet die laufende Tagesdatei, dann ist Schluss (§8.4)."""
    previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
    flag = threading.Event()
    try:
        cli._install_signal_handlers(flag)
        signal.raise_signal(signal.SIGTERM)
        assert flag.is_set()
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
