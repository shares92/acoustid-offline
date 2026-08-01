"""``config.yaml`` und Reload-Signal des Waechters (Phase 14).

Der Waechter ist der einzige Schreiber der Laufzeit-Konfiguration; API und
Importer lesen dieselbe Datei read-only. Geprueft wird deshalb beides: dass
der Roundtrip verlustfrei ist und dass jedes Schreiben ein Signal
hinterlaesst, an dem der API-Dienst ab Phase 15 erkennt, dass er neu laden
muss.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from acoustid_watchdog.config_store import ConfigStore
from acoustid_watchdog.reload import RELOAD_SUFFIX, ReloadSignal
from shared.config import Config, load_config
from shared.models import AuthMode

# --- ConfigStore ------------------------------------------------------------


def test_first_load_creates_the_file_with_the_defaults(config_store: ConfigStore) -> None:
    assert not config_store.path.exists()
    config = config_store.load()
    assert config_store.path.is_file()
    assert config == Config()


def test_config_is_cached_between_accesses(config_store: ConfigStore) -> None:
    first = config_store.config
    assert config_store.config is first


def test_save_round_trip_is_lossless(config_store: ConfigStore) -> None:
    config = config_store.load()
    changed = config.model_copy(
        update={"auth": config.auth.model_copy(update={"mode": AuthMode.APIKEY})}
    )
    config_store.save(changed)

    assert config_store.config.auth.mode is AuthMode.APIKEY
    assert load_config(config_store.path).auth.mode is AuthMode.APIKEY


def test_saved_file_keeps_mode_0600(config_store: ConfigStore) -> None:
    """Die Datei enthaelt Secrets im Klartext (ARCHITECTURE §8.10)."""
    config_store.save(config_store.load())
    assert stat.S_IMODE(config_store.path.stat().st_mode) == 0o600


def test_load_rereads_the_file_from_disk(config_store: ConfigStore) -> None:
    config_store.load()
    config_store.path.write_text("auth:\n  mode: apikey\n", encoding="utf-8")
    assert config_store.load().auth.mode is AuthMode.APIKEY


# --- Reload-Signal ----------------------------------------------------------


def test_reload_marker_sits_next_to_the_config(config_store: ConfigStore) -> None:
    """Gleiches Verzeichnis = gleicher Mount wie die config.yaml."""
    assert config_store.signal.path.parent == config_store.path.parent
    assert config_store.signal.path.name == config_store.path.name + RELOAD_SUFFIX


def test_no_marker_before_the_first_save(config_store: ConfigStore) -> None:
    assert config_store.signal.read() is None


def test_save_emits_a_marker(config_store: ConfigStore) -> None:
    marker = config_store.save(config_store.load())
    assert marker.generation == 1
    assert marker.reason == "config_saved"

    stored = config_store.signal.read()
    assert stored == marker


def test_generation_counts_up_across_saves(config_store: ConfigStore) -> None:
    config = config_store.load()
    generations = [config_store.save(config).generation for _ in range(3)]
    assert generations == [1, 2, 3]


def test_generation_survives_a_restart_of_the_watchdog(
    config_store: ConfigStore, tmp_path: Path
) -> None:
    """Der Zaehler lebt in der Datei — ein Neustart darf ihn nicht zuruecksetzen."""
    config_store.save(config_store.load())
    config_store.save(config_store.load())

    restarted = ConfigStore.from_path(config_store.path)
    assert restarted.save(restarted.load()).generation == 3


def test_marker_is_written_after_the_file(config_store: ConfigStore) -> None:
    """Wer die Marke sieht, findet garantiert schon den neuen Inhalt vor."""
    config = config_store.load()
    changed = config.model_copy(
        update={"auth": config.auth.model_copy(update={"mode": AuthMode.APIKEY})}
    )
    config_store.save(changed)

    marker = config_store.signal.read()
    assert marker is not None
    assert load_config(config_store.path).auth.mode is AuthMode.APIKEY


def test_marker_is_readable_for_the_api_container(config_store: ConfigStore) -> None:
    """Kein Secret drin — und der API-Container laeuft nicht zwingend als dieselbe UID."""
    config_store.save(config_store.load())
    assert stat.S_IMODE(config_store.signal.path.stat().st_mode) == 0o644


def test_marker_content_is_plain_json(config_store: ConfigStore) -> None:
    config_store.save(config_store.load(), reason="test")
    data = json.loads(config_store.signal.path.read_text(encoding="utf-8"))
    assert set(data) == {"generation", "ts", "reason"}
    assert data["reason"] == "test"


@pytest.mark.parametrize("content", ["", "kein json", '{"generation": "viele"}', "{}"])
def test_unusable_marker_counts_as_absent(tmp_path: Path, content: str) -> None:
    """Ein kaputter Hinweis darf keinen Start verhindern — er wird ersetzt."""
    signal = ReloadSignal(tmp_path / "config.yaml.reload")
    signal.path.write_text(content, encoding="utf-8")
    assert signal.read() is None
    assert signal.emit("nach-reparatur").generation == 1


def test_emit_creates_missing_directories(tmp_path: Path) -> None:
    signal = ReloadSignal(tmp_path / "noch" / "nicht" / "da" / "config.yaml.reload")
    assert signal.emit("erstmalig").generation == 1
    assert signal.path.is_file()
