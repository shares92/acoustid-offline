"""Empfangsseite des Reload-Signals im API-Dienst (Phase 15).

Gegenstueck zur Sendeseite des Waechters (Phase 14). Die Tests benutzen
bewusst den **echten** Sender (:class:`acoustid_watchdog.reload.ReloadSignal`)
statt handgeschriebener Dateien: das Signal ist ein Vertrag zwischen zwei
getrennten Images, und ein Test, der die Datei selbst schreibt, wuerde eine
Aenderung auf der anderen Seite nicht bemerken.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from stubs import StubService

from acoustid_api.reload import ConfigReloader
from acoustid_api.upstream import UpstreamForwarder
from acoustid_watchdog.reload import ReloadSignal
from shared.config import Config, save_config
from shared.models import SubmitMode


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    """``config.yaml`` mit den Defaults aus §6, wie beim Erststart."""
    path = tmp_path / "config.yaml"
    save_config(Config(), path)
    return path


@pytest.fixture
def signal(config_path: Path) -> ReloadSignal:
    """Die Sendeseite des Waechters auf derselben Datei."""
    return ReloadSignal.for_config(config_path)


def _reloader(
    config_path: Path, config: Config | None = None, *, with_upstream: bool = False
) -> ConfigReloader:
    """Reloader auf einem Attrappen-Dienst mit der angegebenen Konfiguration."""
    running = config or Config()
    service = StubService(
        config=running,
        upstream=UpstreamForwarder.from_config(running) if with_upstream else None,
    )
    return ConfigReloader(service, config_path)  # type: ignore[arg-type]


def _write(config_path: Path, config: Config, signal: ReloadSignal, reason: str) -> None:
    """Wie die Admin-UI: erst die Datei, dann die Marke (Phase 14)."""
    save_config(config, config_path)
    signal.emit(reason)


# --- Erkennung --------------------------------------------------------------


def test_marker_name_follows_the_config_file(config_path: Path) -> None:
    """``config.yaml`` -> ``config.yaml.reload`` — auf beiden Seiten gleich."""
    reloader = _reloader(config_path)

    assert reloader.marker_path.name == "config.yaml.reload"
    assert reloader.marker_path == ReloadSignal.for_config(config_path).path


def test_nothing_happens_without_a_signal(config_path: Path) -> None:
    reloader = _reloader(config_path)
    reloader.prime()

    assert reloader.check() is False
    assert reloader.reloads == 0


def test_existing_marker_at_start_is_not_a_change(config_path: Path, signal: ReloadSignal) -> None:
    """Der Start liest die config.yaml ohnehin — kein Grund, sofort neu zu laden."""
    signal.emit("frueher")
    reloader = _reloader(config_path)

    assert reloader.prime() == 1
    assert reloader.check() is False


def test_new_generation_triggers_a_reload(config_path: Path, signal: ReloadSignal) -> None:
    reloader = _reloader(config_path)
    reloader.prime()

    changed = Config()
    changed.acoustid.submit.mode = SubmitMode.OFF
    _write(config_path, changed, signal, "config_saved")

    assert reloader.check() is True
    assert reloader.reloads == 1
    assert reloader.service.config.acoustid.submit.mode is SubmitMode.OFF
    # Zweiter Blick auf dieselbe Generation laedt nicht noch einmal.
    assert reloader.check() is False


def test_restarted_counter_is_treated_as_a_change(config_path: Path, signal: ReloadSignal) -> None:
    """Faengt der Zaehler neu bei 1 an, wird neu geladen — der sichere Ausgang."""
    signal.emit("a")
    signal.emit("b")
    reloader = _reloader(config_path)
    reloader.prime()
    assert reloader.generation == 2

    signal.path.unlink()
    changed = Config()
    changed.acoustid.submit.mode = SubmitMode.OFF
    _write(config_path, changed, signal, "neu")

    assert reloader.check() is True
    assert reloader.generation == 1


def test_unreadable_marker_is_ignored(config_path: Path) -> None:
    """Das Signal ist ein Hinweis, kein Datenspeicher (wie auf der Sendeseite)."""
    reloader = _reloader(config_path)
    reloader.prime()
    reloader.marker_path.write_text("kein JSON", encoding="utf-8")

    assert reloader.check() is False


def test_invalid_config_keeps_the_running_one(config_path: Path, signal: ReloadSignal) -> None:
    """Eine kaputte Datei darf den laufenden Dienst nicht umstellen."""
    reloader = _reloader(config_path)
    reloader.prime()
    before = reloader.service.config

    config_path.write_text("wake:\n  hold_timeout_s: -5\n", encoding="utf-8")
    signal.emit("von Hand editiert")

    assert reloader.check() is False
    assert reloader.service.config is before
    # Die Generation gilt trotzdem als gesehen: erneutes Lesen derselben
    # kaputten Datei brauchte niemand.
    assert reloader.generation == 1


# --- Umfang der Uebernahme --------------------------------------------------


def test_submit_mode_and_upstream_are_applied(config_path: Path, signal: ReloadSignal) -> None:
    """Der Weiterleiter entsteht sonst nur beim Start — hier muss er neu gebaut werden."""
    reloader = _reloader(config_path)
    reloader.prime()
    assert reloader.service.upstream is None

    changed = Config()
    changed.acoustid.submit.upstream_app_key = "eigener-app-key"  # type: ignore[assignment]
    changed.acoustid.submit.mode = SubmitMode.LOCAL_UPSTREAM
    _write(config_path, changed, signal, "config_saved")

    assert reloader.check() is True
    assert reloader.service.config.acoustid.submit.mode is SubmitMode.LOCAL_UPSTREAM
    assert reloader.service.upstream is not None
    reloader.service.upstream.close()


def test_switching_upstream_off_drops_the_forwarder(
    config_path: Path, signal: ReloadSignal
) -> None:
    running = Config()
    running.acoustid.submit.upstream_app_key = "eigener-app-key"  # type: ignore[assignment]
    running.acoustid.submit.mode = SubmitMode.LOCAL_UPSTREAM
    save_config(running, config_path)
    reloader = _reloader(config_path, running, with_upstream=True)
    reloader.prime()
    assert reloader.service.upstream is not None

    _write(config_path, Config(), signal, "config_saved")

    assert reloader.check() is True
    assert reloader.service.upstream is None


def test_query_hashes_change_is_refused(config_path: Path, signal: ReloadSignal) -> None:
    """`index.query_hashes` verlangt einen Index-Neuaufbau (§6).

    Wuerde der Wert im laufenden Dienst wechseln, bildete die Suche einen
    anderen Query-Extrakt als der Bestand im Index — sie faende nichts
    mehr. Der laufende Wert bleibt deshalb stehen.
    """
    reloader = _reloader(config_path)
    reloader.prime()

    changed = Config()
    changed.acoustid.index.query_hashes = 80
    _write(config_path, changed, signal, "config_saved")

    assert reloader.check() is True
    assert reloader.service.config.acoustid.index.query_hashes == 120


def test_mb_dsn_change_is_refused(config_path: Path, signal: ReloadSignal) -> None:
    """Der MB-Pool und sein Selfcheck entstehen beim Start — nicht mittendrin."""
    reloader = _reloader(config_path)
    reloader.prime()

    changed = Config()
    changed.mb.dsn = "postgresql://mb/musicbrainz"  # type: ignore[assignment]
    _write(config_path, changed, signal, "config_saved")

    assert reloader.check() is True
    assert reloader.service.config.mb.configured is False


def test_keep_submitted_mbid_is_applied(config_path: Path, signal: ReloadSignal) -> None:
    """Wird je Anfrage gelesen — der Wechsel wirkt sofort."""
    reloader = _reloader(config_path)
    reloader.prime()

    changed = Config()
    changed.mb.keep_submitted_mbid = True
    _write(config_path, changed, signal, "config_saved")

    assert reloader.check() is True
    assert reloader.service.config.mb.keep_submitted_mbid is True
