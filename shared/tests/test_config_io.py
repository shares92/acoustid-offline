"""Laden und atomares Schreiben der config.yaml."""

import logging
import os
import stat
from pathlib import Path

import pytest
import yaml

from shared.config import (
    LEGACY_KEYS,
    Config,
    ConfigFileError,
    ConfigValidationError,
    config_to_dict,
    load_config,
    migrate_legacy_keys,
    read_legacy_keys,
    save_config,
)
from shared.models import AuthMode, SubmitMode

FULL_CONFIG = {
    "auth": {"mode": "apikey", "allow_known_client_keys": True},
    "acoustid": {
        "submit": {"mode": "local+upstream", "upstream_app_key": "app-key"},
        "update": {"time": "03:15"},
        "index": {"query_hashes": 80},
    },
    "discogs": {"update": {"check_time": "06:20"}, "token": "discogs-geheim"},
    "caa": {"crawl": {"enabled": True, "rate_per_s": 4}},
    "covers": {"negative_retry_days": 14},
    "tadb": {"api_key": "tadb-geheim"},
    "wake": {"hold_timeout_s": 45},
    "idle": {"timeout_min": 30},
    "disk": {"min_free_gb": 100},
    "cache": {"enabled": False, "max_size_mb": 1024},
    "ratelimit": {"per_ip_per_min": 60},
    "metrics": {"enabled": True},
    "notify": {
        "ntfy": {"url": "https://ntfy.example/acoustid"},
        "smtp": {
            "host": "mail.example",
            "port": 465,
            "user": "acoustid",
            "pass": "smtp-geheim",
            "from": "acoustid@example",
            "to": "admin@example",
        },
    },
    "backup": {"dir": "/mnt/backup", "time": "05:30", "include_covers": True},
    "mb": {
        "dsn": "postgresql://acoustid_ro:dsn-geheim@mb/musicbrainz",
        "keep_submitted_mbid": True,
    },
}

#: Eine echte v1-`config.yaml`, wie sie auf einer Bestandsinstanz liegt —
#: der Ausgangspunkt der M2-Migration (E9). Bewusst mit `submit.mode: off`:
#: das ist der Wert, dessen stiller Verlust (Risiko R7) angefangen haette,
#: Einreichungen anzunehmen, die der Betreiber abgeschaltet hatte.
V1_CONFIG = {
    "auth": {"mode": "apikey", "allow_known_client_keys": True},
    "submit": {"mode": "off", "upstream_app_key": ""},
    "wake": {"hold_timeout_s": 45},
    "idle": {"timeout_min": 30},
    "update": {"time": "03:15", "min_free_gb": 50},
    "cache": {"enabled": False, "max_size_mb": 1024},
    "ratelimit": {"per_ip_per_min": 60},
    "metrics": {"enabled": True},
    "notify": {
        "ntfy": {"url": "https://ntfy.example/acoustid"},
        "smtp": {
            "host": "mail.example",
            "port": 465,
            "user": "acoustid",
            "pass": "smtp-geheim",
            "from": "acoustid@example",
            "to": "admin@example",
        },
    },
    "backup": {"dir": "/mnt/backup", "time": "05:30"},
    "mb": {
        "dsn": "postgresql://acoustid_ro:dsn-geheim@mb/musicbrainz",
        "keep_submitted_mbid": True,
    },
    "index": {"query_hashes": 80},
}


def _write(path: Path, data: object) -> Path:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


# --- Laden -----------------------------------------------------------------


def test_missing_file_yields_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    assert load_config(path) == Config()
    assert not path.exists()


def test_missing_file_can_be_created(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "config.yaml"
    config = load_config(path, create_if_missing=True)
    assert config == Config()
    assert path.is_file()
    assert load_config(path) == Config()


def test_load_and_save_emit_usable_log_records(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Extra-Felder duerfen nicht mit LogRecord-Attributen kollidieren.

    `logging` bricht sonst mit KeyError ab — und zwar erst, wenn der Level
    das Ereignis wirklich durchlaesst.
    """
    path = tmp_path / "config.yaml"
    with caplog.at_level(logging.INFO, logger="shared.config"):
        load_config(path, create_if_missing=True)
        save_config(Config(), path)

    messages = [record.getMessage() for record in caplog.records]
    assert any("Defaults" in message for message in messages)
    assert any("geschrieben" in message for message in messages)
    assert all(record.config_path == str(path) for record in caplog.records)


def test_empty_file_yields_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("", encoding="utf-8")
    assert load_config(path) == Config()


def test_partial_file_is_filled_with_defaults(tmp_path: Path) -> None:
    path = _write(tmp_path / "config.yaml", {"acoustid": {"index": {"query_hashes": 80}}})
    config = load_config(path)
    assert config.acoustid.index.query_hashes == 80
    assert config.wake.hold_timeout_s == 90
    assert config.acoustid.update.time == "04:00"


def test_full_file_is_read_completely(tmp_path: Path) -> None:
    path = _write(tmp_path / "config.yaml", FULL_CONFIG)
    config = load_config(path)
    assert config_to_dict(config, reveal_secrets=True) == FULL_CONFIG


def test_unknown_keys_are_ignored_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _write(
        tmp_path / "config.yaml",
        {
            "acoustid": {"index": {"query_hashes": 80, "shard_count": 4}},
            "kaffee": {"sorte": "filter"},
            "notify": {"smtp": {"pasword": "tippfehler"}},
        },
    )
    with caplog.at_level(logging.WARNING, logger="shared.config"):
        config = load_config(path)

    assert config.acoustid.index.query_hashes == 80
    reported = {record.config_key for record in caplog.records}
    assert reported == {"acoustid.index.shard_count", "kaffee", "notify.smtp.pasword"}


def test_validation_error_names_the_key_path(tmp_path: Path) -> None:
    path = _write(tmp_path / "config.yaml", {"notify": {"smtp": {"port": 0, "host": "m"}}})
    with pytest.raises(ConfigValidationError) as excinfo:
        load_config(path)
    paths = [path for path, _ in excinfo.value.errors]
    assert "notify.smtp.port" in paths
    assert "notify.smtp.port" in str(excinfo.value)
    assert str(path) in str(excinfo.value)


def test_broken_yaml_raises_config_file_error(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("auth: {mode: none\n", encoding="utf-8")
    with pytest.raises(ConfigFileError, match="kein gueltiges YAML"):
        load_config(path)


def test_non_mapping_yaml_raises_config_file_error(tmp_path: Path) -> None:
    path = _write(tmp_path / "config.yaml", ["auth", "acoustid"])
    with pytest.raises(ConfigFileError, match="YAML-Mapping"):
        load_config(path)


def test_directory_instead_of_file_yields_defaults(tmp_path: Path) -> None:
    """`is_file()` statt `exists()`: ein Verzeichnis ist keine Config."""
    path = tmp_path / "config.yaml"
    path.mkdir()
    assert load_config(path) == Config()


# --- Schreiben -------------------------------------------------------------


def test_roundtrip_is_lossless(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    original = Config.model_validate(FULL_CONFIG)
    save_config(original, path)
    assert load_config(path) == original
    assert config_to_dict(load_config(path), reveal_secrets=True) == FULL_CONFIG


def test_roundtrip_of_defaults_is_lossless(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    save_config(Config(), path)
    assert load_config(path) == Config()


def test_written_file_has_no_unknown_keys(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Was save_config schreibt, laedt load_config ohne Warnung."""
    path = tmp_path / "config.yaml"
    save_config(Config.model_validate(FULL_CONFIG), path)
    with caplog.at_level(logging.WARNING, logger="shared.config"):
        load_config(path)
    assert caplog.records == []


def test_written_file_is_readable_yaml_with_header(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    save_config(Config(), path)
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# config.yaml")
    assert yaml.safe_load(text)["acoustid"]["index"]["query_hashes"] == 120


def test_secrets_are_written_in_clear_text(tmp_path: Path) -> None:
    """Die Datei ist die Quelle der Wahrheit — maskiert waere sie kaputt."""
    path = tmp_path / "config.yaml"
    save_config(Config.model_validate(FULL_CONFIG), path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["notify"]["smtp"]["pass"] == "smtp-geheim"
    assert "**********" not in path.read_text(encoding="utf-8")


def test_parent_directory_is_created(tmp_path: Path) -> None:
    path = tmp_path / "neu" / "tief" / "config.yaml"
    save_config(Config(), path)
    assert path.is_file()


def test_file_mode_stays_closed_for_others(tmp_path: Path) -> None:
    """Secrets im Klartext (ARCHITECTURE §8.10) — „andere" duerfen nie lesen.

    0640 statt 0600 seit M1b: der API-Dienst laeuft unprivilegiert und muss
    die Datei lesen koennen (Begruendung an ``_FILE_MODE``). Geprueft wird
    deshalb die **Zusage**, nicht die Zahl: kein Schreibrecht ausser fuer
    den Eigentuemer, und fuer „andere" gar nichts.
    """
    path = tmp_path / "config.yaml"
    save_config(Config(), path)

    mode = stat.S_IMODE(path.stat().st_mode)

    assert mode == 0o640
    assert not mode & stat.S_IRWXO, "world-readable"
    assert not mode & (stat.S_IWGRP | stat.S_IXGRP), "Gruppe darf nur lesen"


def test_write_leaves_no_temporary_files(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    save_config(Config(), path)
    assert [entry.name for entry in tmp_path.iterdir()] == ["config.yaml"]


def test_write_uses_rename_within_the_target_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Atomar heisst: dieselbe Partition, ein `os.replace` am Ende."""
    path = tmp_path / "config.yaml"
    seen: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def spy(src, dst, **kwargs):  # type: ignore[no-untyped-def]
        seen.append((Path(src), Path(dst)))
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr(os, "replace", spy)
    save_config(Config(), path)

    assert len(seen) == 1
    src, dst = seen[0]
    assert src.parent == path.parent
    assert dst == path


def test_failed_write_keeps_the_previous_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "config.yaml"
    save_config(Config(), path)
    before = path.read_text(encoding="utf-8")

    def boom(src, dst, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("kein Platz mehr")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="kein Platz mehr"):
        save_config(Config.model_validate(FULL_CONFIG), path)

    assert path.read_text(encoding="utf-8") == before
    assert [entry.name for entry in tmp_path.iterdir()] == ["config.yaml"]


def test_overwriting_replaces_the_content(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    save_config(Config(), path)
    save_config(Config.model_validate({"acoustid": {"index": {"query_hashes": 80}}}), path)
    assert load_config(path).acoustid.index.query_hashes == 80


# --- Schluessel-Umbenennung M2 (E9) ----------------------------------------
#
# Risiko R7 („stille Config-Amnesie") in Testform. Der teuerste Fall ist
# nicht der Absturz, sondern das lautlose Weiterlaufen mit Vorgabewerten.


def test_a_v1_config_file_is_read_completely(tmp_path: Path) -> None:
    """Der Migrationsfall am echten Bestand: kein Wert geht verloren."""
    path = _write(tmp_path / "config.yaml", V1_CONFIG)
    config = load_config(path)

    # Die vier umbenannten Pfade.
    assert config.acoustid.submit.mode is SubmitMode.OFF
    assert config.acoustid.update.time == "03:15"
    assert config.acoustid.index.query_hashes == 80
    assert config.disk.min_free_gb == 50
    # Und alles, was NICHT umbenannt wurde, steht unveraendert daneben.
    assert config.auth.mode is AuthMode.APIKEY
    assert config.wake.hold_timeout_s == 45
    assert config.idle.timeout_min == 30
    assert config.cache.max_size_mb == 1024
    assert config.ratelimit.per_ip_per_min == 60
    assert config.backup.dir == "/mnt/backup"
    assert config.mb.keep_submitted_mbid is True
    assert config.notify.smtp.password.get_secret_value() == "smtp-geheim"


def test_submit_mode_off_survives_the_migration(tmp_path: Path) -> None:
    """Der Kern von R7, als eigener Satz.

    Faellt `submit.mode` auf den Default zurueck, nimmt eine Instanz
    Einreichungen an, die der Betreiber ausdruecklich abgeschaltet hatte —
    und niemand merkt es, weil unbekannte Schluessel nur warnen.
    """
    path = _write(tmp_path / "config.yaml", {"submit": {"mode": "off"}})

    assert load_config(path).acoustid.submit.mode is SubmitMode.OFF


def test_the_min_free_gb_default_rises_only_where_nothing_was_set(tmp_path: Path) -> None:
    """Ein gesetzter Altwert bleibt; erst das Fehlen zieht den neuen Default."""
    with_value = _write(tmp_path / "gesetzt.yaml", {"update": {"min_free_gb": 50}})
    without = _write(tmp_path / "leer.yaml", {"auth": {"mode": "none"}})

    assert load_config(with_value).disk.min_free_gb == 50
    assert load_config(without).disk.min_free_gb == 100


def test_legacy_keys_are_reported_with_their_replacement(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _write(tmp_path / "config.yaml", V1_CONFIG)
    with caplog.at_level(logging.WARNING, logger="shared.config"):
        load_config(path)

    reported = {record.config_key: record.config_key_replacement for record in caplog.records}
    assert reported == dict(LEGACY_KEYS)


def test_legacy_keys_are_not_reported_as_unknown(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Sonst saehe der Betreiber die Meldung „wird ignoriert" — das Gegenteil."""
    path = _write(tmp_path / "config.yaml", V1_CONFIG)
    with caplog.at_level(logging.WARNING, logger="shared.config"):
        load_config(path)

    assert all("Unbekannter" not in record.getMessage() for record in caplog.records)


def test_the_new_key_wins_over_the_old_one(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Wer den neuen Schluessel eintraegt und den alten stehen laesst, meint den neuen."""
    path = _write(
        tmp_path / "config.yaml",
        {"submit": {"mode": "off"}, "acoustid": {"submit": {"mode": "local"}}},
    )
    with caplog.at_level(logging.WARNING, logger="shared.config"):
        config = load_config(path)

    assert config.acoustid.submit.mode is SubmitMode.LOCAL
    assert any("ignoriert" in record.getMessage() for record in caplog.records)


def test_a_v1_file_needs_no_second_migration_after_being_written(tmp_path: Path) -> None:
    """Nach dem Umschreiben ist die Datei sauber — sonst warnte jeder Start."""
    path = _write(tmp_path / "config.yaml", V1_CONFIG)
    save_config(load_config(path), path)

    assert read_legacy_keys(path) == []


def test_read_legacy_keys_finds_exactly_the_old_paths(tmp_path: Path) -> None:
    path = _write(tmp_path / "config.yaml", V1_CONFIG)

    assert {entry.old for entry in read_legacy_keys(path)} == set(LEGACY_KEYS)
    assert all(not entry.superseded for entry in read_legacy_keys(path))


def test_read_legacy_keys_is_quiet_about_a_current_file(tmp_path: Path) -> None:
    path = _write(tmp_path / "config.yaml", FULL_CONFIG)

    assert read_legacy_keys(path) == []


def test_read_legacy_keys_survives_a_broken_file(tmp_path: Path) -> None:
    """Die Fehlermeldung gehoert `load_config`, das gleich danach laeuft."""
    path = tmp_path / "config.yaml"
    path.write_text("auth: {mode: none\n", encoding="utf-8")

    assert read_legacy_keys(path) == []


def test_migrate_leaves_the_callers_mapping_alone() -> None:
    """Rein: derselbe Aufruf laeuft im Validator bei jedem `model_validate`."""
    original = {"submit": {"mode": "off"}}
    migrated, found = migrate_legacy_keys(original)

    assert original == {"submit": {"mode": "off"}}
    assert migrated == {"acoustid": {"submit": {"mode": "off"}}}
    assert [entry.old for entry in found] == ["submit.mode"]


def test_migrate_is_idempotent() -> None:
    once, _ = migrate_legacy_keys(V1_CONFIG)
    twice, found = migrate_legacy_keys(once)

    assert once == twice
    assert found == []


def test_a_legacy_section_that_is_not_a_mapping_stays_untouched() -> None:
    """`submit: local` ist kein Blattpfad — stilles Wegwerfen waere schlimmer."""
    migrated, found = migrate_legacy_keys({"submit": "local"})

    assert migrated == {"submit": "local"}
    assert found == []
